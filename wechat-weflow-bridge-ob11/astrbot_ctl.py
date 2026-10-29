# -*- coding: utf-8 -*-
"""AstrBot 进程控制：面板「重启 AstrBot」按钮的后端。

为什么需要它
------------
AstrBot 的 `WhitelistCheckStage.initialize()` 和 `PersonaManager` **只在启动时
读一次**配置（白名单 / 人设 / 平台适配器）。所以「改了配置 → 必须重启」是常态，
以前只能手动关掉控制台窗口再双击 start.bat，很烦。

设计要点（都是踩过的坑）
------------------------
1. **按端口找进程，不按进程名**：机器上可能同时跑着桥接/模拟器/别的 python，
   按 `python.exe` 杀会误伤。AstrBot 一定监听 `ws_reverse_port`（默认 11229），
   用 `netstat -ano` 反查 PID 才准。

2. **必须 detach 启动**：如果用普通 `subprocess.Popen`，AstrBot 会成为桥接的
   子进程 —— 桥接一重启（改代码很常见）AstrBot 就跟着死。用
   `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 让它脱离，并重定向日志到
   `astrbot_run.log`（否则没有控制台，输出会丢）。

3. **等待就绪要轮询端口**：AstrBot 冷启动约 10-20 秒（加载 4 个人设 + 一堆
   provider）。只看进程存在不够 —— 要等到端口真的 LISTENING。

4. **锁文件**：AstrBot 用 `astrbot.lock` 防重复启动。强杀后锁可能残留，
   重启前要清掉，否则新进程直接 `Cannot acquire lock file` 退出。
"""
import logging
import os
import subprocess
import threading
import time

import config

log = logging.getLogger("ob11-bridge")

# AstrBot 反向 WS 端口（桥接作为客户端连过去），用来定位和探测进程
_OB_PORT = 11229
try:
    _u = config.ASTRBOT_OB_URL or ""
    if ":" in _u:
        _OB_PORT = int(_u.rsplit(":", 1)[-1].split("/")[0])
except Exception:
    pass


def _astrbot_root() -> str:
    """定位 runtime/astrbot 目录（含 run_astrbot.py 与 .venv）。

    注意：config.PROJECT_ROOT 是 **runtime/**（桥接目录的上一级），
    所以 astrbot 在 `PROJECT_ROOT/astrbot`，不是 `PROJECT_ROOT/runtime/astrbot`。
    两种布局都试，避免换目录后失效。
    """
    cfg_path = config.ASTRBOT_CONFIG_FILE or ""
    if cfg_path:
        # .../runtime/astrbot/data/cmd_config.json -> .../runtime/astrbot
        d = os.path.dirname(os.path.dirname(os.path.abspath(cfg_path)))
        if os.path.exists(os.path.join(d, "run_astrbot.py")):
            return d
    root = config.PROJECT_ROOT            # …/runtime
    cands = [
        os.path.join(root, "astrbot"),                 # runtime/astrbot  ← 本项目
        os.path.join(root, "runtime", "astrbot"),      # 万一 PROJECT_ROOT 是仓库根
        os.path.join(os.path.dirname(root), "astrbot"),
    ]
    for c in cands:
        if os.path.exists(os.path.join(c, "run_astrbot.py")):
            return c
    return ""


def _python_exe(root: str) -> str:
    for rel in (r".venv\Scripts\python.exe", r".venv/bin/python"):
        p = os.path.join(root, rel)
        if os.path.exists(p):
            return p
    return ""


def find_astrbot_pids() -> list:
    """返回正在监听 AstrBot 端口的 PID 列表（找不到 = 没在跑）。"""
    pids = []
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        ).stdout or ""
    except Exception as e:
        log.warning(f"[重启] netstat 失败: {e}")
        return pids
    needle = f":{_OB_PORT} "
    for line in out.splitlines():
        if needle not in line or "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if parts and parts[-1].isdigit():
            pid = int(parts[-1])
            if pid not in pids:
                pids.append(pid)
    return pids


def is_listening() -> bool:
    return bool(find_astrbot_pids())


def stop(timeout: float = 20.0) -> tuple:
    """停掉 AstrBot。返回 (ok, msg)。"""
    pids = find_astrbot_pids()
    if not pids:
        return True, "AstrBot 本来就没在运行"
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=15)
        except Exception as e:
            log.warning(f"[重启] 结束 PID {pid} 失败: {e}")
    # 等端口真正释放
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not find_astrbot_pids():
            return True, f"已停止（PID {', '.join(map(str, pids))}）"
        time.sleep(0.5)
    return False, f"停止超时，端口 {_OB_PORT} 仍被占用"


def _clear_lock(root: str):
    """清掉残留的 astrbot.lock（强杀后常见，会导致新进程拒绝启动）。"""
    for name in ("astrbot.lock",):
        p = os.path.join(root, name)
        try:
            if os.path.exists(p):
                os.remove(p)
                log.info(f"[重启] 已清理残留锁文件 {name}")
        except Exception as e:
            log.warning(f"[重启] 清理 {name} 失败（可能仍被占用）: {e}")


def start() -> tuple:
    """分离式启动 AstrBot。返回 (ok, msg)。"""
    root = _astrbot_root()
    if not root:
        return False, "找不到 AstrBot 目录（缺 run_astrbot.py）"
    py = _python_exe(root)
    if not py:
        return False, f"找不到 AstrBot 的 python：{root}\\.venv\\Scripts\\python.exe"

    _clear_lock(root)
    log_path = os.path.join(root, "astrbot_run.log")
    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    env["no_proxy"] = "127.0.0.1,localhost,::1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    # 追加写日志（不覆盖，保留历史便于排查）
    try:
        fh = open(log_path, "a", encoding="utf-8", errors="replace")
    except Exception as e:
        return False, f"打不开日志文件 {log_path}: {e}"

    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen(
            [py, "-u", "run_astrbot.py", "run"],
            cwd=root, env=env, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
        )
    except Exception as e:
        return False, f"启动失败: {e}"
    finally:
        # 父进程不持有句柄；子进程已继承
        try:
            fh.close()
        except Exception:
            pass
    return True, "已发出启动命令"


def wait_ready(timeout: float = 90.0) -> tuple:
    """轮询直到端口 LISTENING。返回 (ok, msg)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if is_listening():
            return True, f"AstrBot 已就绪（用时 {time.time() - t0:.1f}s）"
        time.sleep(1.0)
    return False, f"等待就绪超时（{timeout:.0f}s）—— 看 astrbot_run.log 确认原因"


def restart(timeout: float = 90.0) -> tuple:
    """完整重启：停 → 起 → 等就绪。返回 (ok, msg)。"""
    log.info("[重启] 开始重启 AstrBot …")
    ok, msg = stop()
    if not ok:
        return False, msg
    time.sleep(1.5)          # 让端口彻底释放
    ok, msg2 = start()
    if not ok:
        return False, msg2
    ok, msg3 = wait_ready(timeout=timeout)
    if ok:
        log.info(f"[重启] ✅ {msg3}")
    else:
        log.error(f"[重启] ❌ {msg3}")
    return ok, msg3


# ============ 异步重启（面板按钮用） ============
#
# 重启要 10-25 秒，不能让 HTTP 请求干等（浏览器会超时、面板会卡住）。
# 所以：立刻返回「已开始」，后台线程干活，前端轮询 /api/astrbot-status。
_restart_state = {
    "running": False,
    "phase": "idle",     # idle | stopping | starting | waiting | done | failed
    "message": "",
    "started_at": 0.0,
    "finished_at": 0.0,
}
_lock = threading.Lock()


def status() -> dict:
    """给前端轮询用的状态快照（附带实时端口探测）。"""
    with _lock:
        snap = dict(_restart_state)
    snap["listening"] = is_listening()
    snap["port"] = _OB_PORT
    return snap


def is_restarting() -> bool:
    with _lock:
        return bool(_restart_state["running"])


def _set(phase: str, message: str, running=None):
    with _lock:
        _restart_state["phase"] = phase
        _restart_state["message"] = message
        if running is not None:
            _restart_state["running"] = running


def _do_restart(timeout: float = 90.0):
    try:
        _set("stopping", "正在停止 AstrBot …")
        ok, msg = stop()
        if not ok:
            _set("failed", msg, running=False)
            return
        time.sleep(1.5)

        _set("starting", "正在启动 AstrBot …")
        ok, msg = start()
        if not ok:
            _set("failed", msg, running=False)
            return

        _set("waiting", "等待 AstrBot 就绪（约 10-25 秒）…")
        ok, msg = wait_ready(timeout=timeout)
        _set("done" if ok else "failed", msg, running=False)
        log.info(f"[重启] {'✅' if ok else '❌'} {msg}")
    except Exception as e:
        _set("failed", f"重启异常: {e}", running=False)
        log.error(f"[重启] 异常: {e}")
    finally:
        with _lock:
            _restart_state["finished_at"] = time.time()


def restart_async(timeout: float = 90.0) -> bool:
    """后台重启。返回 False 表示已有任务在跑。"""
    with _lock:
        if _restart_state["running"]:
            return False
        _restart_state.update(running=True, phase="stopping",
                              message="正在停止 AstrBot …",
                              started_at=time.time(), finished_at=0.0)
    threading.Thread(target=_do_restart, args=(timeout,), daemon=True).start()
    return True
