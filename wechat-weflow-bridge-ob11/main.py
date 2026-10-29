"""
入口与生命周期管理模块。

负责桥接的启动、停止、主循环重连逻辑，以及命令行入口。
"""

import json
import logging
import os
import sys
import threading
import time

import requests

import state
import config
import exit_reason
from senders import create_sender
from ob_client import _run_ob_client
from bridge_core import WeFlowBridge
from message_source import create_message_source
from web_panel import WebHandler, PAGE
from http.server import HTTPServer

log = logging.getLogger("ob11-bridge")


# ============ 启动 / 停止 ============


def _start_bridge():
    with state.run_lock:
        if state.running:
            return
        state.running = True
    state.paused.clear()
    state.sender_instance = create_sender()

    # 启动即把管理员名单（wxid → UID）重放一遍到 AstrBot 配置：
    # 桥接重启/换机后无需人工再同步。失败不影响启动。
    try:
        import people
        if people.load_admins():
            people.sync_admins_to_astrbot()
    except Exception as e:
        log.warning(f"管理员同步跳过: {e}")

    if not state.ob_client_started:
        t = threading.Thread(target=_run_ob_client, daemon=True, name="ob11-client")
        t.start()
        state.ob_thread = t
        state.ob_client_started = True

    state.bridge_thread = threading.Thread(target=_bridge_loop, daemon=True, name="bridge")
    state.bridge_thread.start()
    log.info("[Web] 已启动")


def _stop_bridge():
    with state.run_lock:
        state.running = False

    # 切断 SSE 长连接，让 _bridge_loop 的 listen_sse() 从阻塞中退出
    with state.bridge_lock:
        if state.bridge_instance and state.bridge_instance._sse_session:
            try:
                state.bridge_instance._sse_session.close()
                log.info("[Web] SSE 连接已断开")
            except Exception as e:
                log.warning(f"[Web] 断开 SSE 异常: {e}")

    # 关闭 WebSocket 连接，让 _ob_client_main 从 async for 中退出
    _ws = state._ob_ws
    _loop = state._ob_ws_loop
    if _ws:
        try:
            if _loop and _loop.is_running():
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    _ws.close(), _loop
                )
                log.info("[Web] WebSocket 连接已关闭")
        except Exception as e:
            log.warning(f"[Web] 关闭 WebSocket 异常: {e}")

    state._ob_ws_ready.clear()

    # 等旧的客户端线程真正退出再允许重启 —— 否则 stop→start 会留下两个客户端线程
    _t = getattr(state, "ob_thread", None)
    if _t is not None and _t.is_alive():
        _t.join(timeout=5)
        if _t.is_alive():
            log.warning("[Web] OB 客户端线程未能及时退出，将在后台自行结束")
    state.ob_thread = None

    # 重置启动标记，让下次 start 能重新拉起 WebSocket 客户端线程
    state.ob_client_started = False
    state._ob_ws_loop = None

    log.info("[Web] 已停止")


def _bridge_loop():
    import ctypes
    ctypes.windll.ole32.CoInitialize(None)

    if not config.ACCESS_TOKEN:
        log.error("❌ 未配置 access_token")
        state.running = False
        return

    log.info(f"Bridge | 消息源: {config.MESSAGE_SOURCE.upper()} | "
             f"OB11: {config.ASTRBOT_OB_URL} | 发送: {config.SEND_METHOD}")
    # Alpha 专属前置检查（地址/令牌/账号），错误信息里不含 token。
    if config.MESSAGE_SOURCE == "alpha":
        if not config.ALPHA_BASE_URL or not config.ALPHA_ACCESS_TOKEN:
            log.error("❌ 未配置 alpha_base_url / alpha_access_token")
            state.running = False
            return

    bridge = WeFlowBridge(state.sender_instance)
    with state.bridge_lock:
        state.bridge_instance = bridge

    # WeFlow/Alpha 的自检差异（WeFlow 旧接口不带 talker 也能探活；
    # Alpha /api/v1/messages 必须带 talker，所以这里探 accounts 而不是 messages）
    # 统一封在 message_source.check() 里。
    ok, status, msg = create_message_source().check()
    if not ok:
        log.error(f"❌ {msg}")
        state.running = False
        return
    log.info(f"✅ {msg}")

    while state.running:
        try:
            bridge.listen_sse()
        except Exception as e:
            log.error(f"SSE: {e}")
        if not state.running:
            break
        log.warning("⚠️ SSE 断开，10s 后重连")
        for _ in range(10):
            if not state.running:
                break
            time.sleep(1)

    with state.bridge_lock:
        state.bridge_instance = None


def start_web():
    # web_host 默认 0.0.0.0（局域网可访问）；只想本机访问时在 config.json 里改成 127.0.0.1
    host = getattr(config, "WEB_HOST", "") or "0.0.0.0"
    server = HTTPServer((host, config.WEB_PORT), WebHandler)
    if host in ("0.0.0.0", "::"):
        import socket
        try:
            lan = socket.gethostbyname(socket.gethostname())
            log.info(f"Web: http://127.0.0.1:{config.WEB_PORT}  (局域网: http://{lan}:{config.WEB_PORT})")
        except Exception:
            log.info(f"Web: http://0.0.0.0:{config.WEB_PORT}")
    else:
        log.info(f"Web: http://{host}:{config.WEB_PORT}")
    server.serve_forever()


# ============ 入口 ============

if __name__ == "__main__":
    # 退出归因：尽早装钩子，这样连"启动阶段就挂"也能记录下来。
    # 会写 data/exit_reason.log（含上次是否被强杀的判定）。
    exit_reason.install()
    exit_reason.set_phase("初始化")

    # 崩溃栈：C 层崩溃（comtypes/UIA 访问违例这类）不走 Python 异常处理，
    # 进程会**静默消失**，只留一句"被强制终止"，查不动（2026-09-25 早上
    # 桥接就是这样死的）。faulthandler 能在崩溃瞬间把各线程栈写盘。
    try:
        import faulthandler

        _data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(_data_dir, exist_ok=True)
        _fh_path = os.path.join(_data_dir, "faulthandler.log")
        if os.path.exists(_fh_path) and os.path.getsize(_fh_path) > 1024 * 1024:
            os.replace(_fh_path, _fh_path + ".1")     # 超 1MB 滚动一次
        _fh_file = open(_fh_path, "a", buffering=1, encoding="utf-8")
        faulthandler.enable(file=_fh_file, all_threads=True)
    except Exception:
        _fh_file = None

    # 从 config 初始化 state 中需要计算的值
    state._self_id_int = state._wxid_to_int(config.BOT_WXID or "wechat_bot")
    state.group_reply_mode = config.GROUP_REPLY_MODE

    PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.pid")

    def _win_proc_info(pid):
        """查询 Windows 进程：(是否存活, 镜像名, 已存活秒数)。

        为什么不能只判断「PID 在不在」：**Windows 会复用 PID** —— 陈旧
        pid 文件里的号可能已被别的程序占用，只看存活就会把新实例永久挡在
        门外。2026-09-25 事故：一键启动后新桥接自杀退出（日志
        「bridge.pid 已存在（PID 19868 仍在运行）」），面板一直不出现。
        """
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return (False, "", 0.0)
        try:
            name = ""
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
            age = 0.0
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if k32.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kernel), ctypes.byref(user)):
                ft = (created.dwHighDateTime << 32) | created.dwLowDateTime
                if ft:
                    # FILETIME = 1601-01-01 起的 100ns 计数
                    age = time.time() - (ft / 1e7 - 11644473600)
            return (True, name, max(age, 0.0))
        finally:
            k32.CloseHandle(h)

    def _kill_pid(pid):
        """强杀指定 PID。只有已确认是本项目 python 实例时才会走到这里。"""
        import ctypes

        k32 = ctypes.windll.kernel32
        PROCESS_TERMINATE = 0x0001
        h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, 1))
        finally:
            k32.CloseHandle(h)

    def port_in_use(port, host="127.0.0.1"):
        """Web 面板端口上是否已有人在监听。

        兜底用：gateway 的 HTTPServer 开了 allow_reuse_address，
        Windows 的 SO_REUSEADDR 允许**两个进程同时绑同一端口**，
        所以 pid 锁一旦被误删就再也拦不住第二个实例 —— 而两个实例
        会同时消费微信消息，群里每条消息被回复两次。
        这里用 connect 探测（TIME_WAIT 不会误报，只有真在监听才会连上）。
        """
        import socket
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    # ---- 单实例守卫 ----
    # 判据顺序（2026-09-25 事故后重写）：
    #   ① 端口有人在服务 ⇒ 铁证：另一个实例活着，拒绝启动（防一条消息回两次）
    #   ② 端口空闲 ⇒ 没有任何实例在服务，pid 文件只是残留锁，分三种情况：
    #        · PID 不存在 / 不是 python ⇒ 陈旧锁（含 PID 复用），删掉继续
    #        · PID 是 python 且存活很久 ⇒ 卡死的老实例，清掉它再接管（自愈）
    #        · PID 是 python 但刚启动   ⇒ 并发启动竞态，等端口，起来就让位
    if port_in_use(config.WEB_PORT):
        log.error(f"⚠️ Web 面板端口 {config.WEB_PORT} 已被占用 —— 极可能已有一个桥接在运行")
        log.error("   拒绝启动第二个实例（否则每条消息会被回复两次）。请先停掉旧实例。")
        exit_reason.note_exit(f"Web 端口 {config.WEB_PORT} 已被占用（疑似重复实例）", 1)
        sys.exit(1)

    # 新实例的启动窗口：这段时间内不动"别人的" pid。
    # 可用 BRIDGE_STARTUP_GRACE 覆盖（回归测试用来快速触发"老实例"分支）。
    STARTUP_GRACE = float(os.environ.get("BRIDGE_STARTUP_GRACE", "60") or 60)

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
        except (ValueError, OSError):
            old_pid = None

        alive, image, age = _win_proc_info(old_pid) if old_pid else (False, "", 0.0)

        if not alive:
            log.info(f"🧹 清理陈旧 bridge.pid（PID {old_pid} 已不存在）")
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
        elif not image.startswith("python"):
            log.warning(f"🧹 bridge.pid 里的 PID {old_pid} 现属「{image}」"
                        f"（PID 被系统复用，并非桥接），按陈旧锁清理")
            try:
                os.remove(PID_FILE)
            except OSError:
                pass
        elif age < STARTUP_GRACE:
            log.info(f"⏳ 有另一个桥接刚启动（PID {old_pid}，{age:.0f}s），等它就绪…")
            for _ in range(15):
                time.sleep(1)
                if port_in_use(config.WEB_PORT):
                    log.error(f"⚠️ 已有桥接在服务（PID {old_pid}），本实例退出以免重复回复")
                    exit_reason.note_exit(f"并发启动：PID {old_pid} 已就绪，让位退出", 1)
                    sys.exit(1)
            log.warning(f"⚠️ PID {old_pid} 启动 {age:.0f}s 后仍未监听 "
                        f"{config.WEB_PORT}，视为卡死的启动，接管")
            _kill_pid(old_pid)
        else:
            log.warning(f"⚠️ 发现卡死的老桥接（PID {old_pid}，已存活 {age/60:.0f} 分钟，"
                        f"端口 {config.WEB_PORT} 无响应），清理后接管")
            _kill_pid(old_pid)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        log.info("=" * 50)
        log.info(f" {getattr(config, 'PROJECT_NAME', 'Akasha-WeChat')} (OneBot v11)")
        log.info("=" * 50)
        log.info(f"Bridge 版本: {getattr(config, 'PROJECT_VERSION', '?')}（核心: 2026-06-03 OB11）")
        exit_reason.set_phase("启动桥接")
        _start_bridge()
        exit_reason.set_phase("启动 Web 面板")
        start_web()
    finally:
        exit_reason.set_phase("主循环结束（收尾）")
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
