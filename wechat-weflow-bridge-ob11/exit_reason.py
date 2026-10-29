# -*- coding: utf-8 -*-
"""退出归因：区分「自己崩了」和「被别人杀了」。

为什么需要它（2026-09-19 实测）
-------------------------------
桥接某天 11:58 从日志里**干净消失**：最后一条是正常的"名册已刷新"，
没有任何 Traceback，8766 端口也没了。结果是**无法判断**到底是
  ① 代码抛出未捕获异常挂掉，
  ② 被 taskkill / 任务管理器 / 系统关机杀掉，
  ③ 主动 sys.exit()（如"已有一个实例在运行"）。
三种情况的排查方向完全不同，但日志长一个样。

本模块在进程退出时写一条**带归因**的记录到 `data/exit_reason.log`：

    2026-09-19 12:03:29 [退出] 原因=被外部终止(信号15/TerminateProcess)
                             uptime=1h23m  最后阶段=运行中
    2026-09-19 12:31:04 [退出] 原因=未捕获异常: KeyError('foo')
                             traceback=...
    2026-09-19 12:40:00 [退出] 原因=主动退出: 已有一个桥接在运行(code=1)

为什么能做到（Windows 的实际语义）
----------------------------------
· **TerminateProcess（taskkill /F、任务管理器"结束任务"）不会触发任何
  Python 清理** —— atexit 和 finally 都不跑，我们只能事后看"有没有记录"
  来判断：有正常记录=自己退的；**没有记录=被杀**。
· Ctrl+C / 关闭控制台窗口 → 会送 CTRL_CLOSE_EVENT / SIGINT，Python 能捕获。
· 所以策略是**两条腿**：
    a) 写"启动"记录（供事后算 uptime、判断上次是否正常收尾）
    b) 注册 atexit + 信号处理 + excepthook，正常路径都能留下原因
  下次启动时若发现"上次没有退出记录"，就明确标注**上次是被强杀的**。

用法
----
    from exit_reason import install, set_phase, note_exit
    install()                  # 入口最早处调用一次
    set_phase("运行中")         # 关键阶段，便于定位"死在哪一步"
    note_exit("已有一个桥接在运行", code=1)   # 主动退出前记录原因
"""

import atexit
import io
import os
import sys
import time
import traceback

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_LOG = os.path.join(_DIR, "exit_reason.log")

_start_ts = time.time()
_phase = "启动中"
_installed = False
_exit_noted = False


def _fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def _write(line: str) -> None:
    """追加写一行（失败也不抛 —— 退出路径上绝不能因日志失败而卡住）。"""
    try:
        os.makedirs(_DIR, exist_ok=True)
        with io.open(_LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception:
        pass


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _tail_prev_run() -> str:
    """检查上一次运行是否正常收尾；返回给启动日志用的一句话。

    判据：**本次启动之前**，日志里最后一条是不是"启动"记录。
    若是（说明上次没写下任何退出原因）→ 上次是被强杀的。
    """
    try:
        if not os.path.exists(_LOG):
            return ""
        with io.open(_LOG, encoding="utf-8", errors="replace") as f:
            lines = [x.rstrip("\n") for x in f if x.strip()]
        # 从后往前找最后一条「启动」
        last_start = -1
        for i in range(len(lines) - 1, -1, -1):
            if "[启动]" in lines[i]:
                last_start = i
                break
        if last_start < 0:
            return ""
        # 该「启动」之后有没有退出记录
        after = lines[last_start + 1:]
        exited = [x for x in after if "[退出]" in x]
        if exited:
            return ""
        return lines[last_start]
    except Exception:
        return ""


def install() -> None:
    """装好归因钩子。入口最早处调用，幂等。"""
    global _installed
    if _installed:
        return
    _installed = True

    # ① 上次是否被强杀
    prev = _tail_prev_run()
    if prev:
        _write(f"{_ts()} [异常] 上次运行未见退出记录 —— 判定为**被强制终止**"
               f"（taskkill /F 或任务管理器结束进程不会触发任何 Python 清理）")
        _write(f"{_ts()} [异常] 上次启动于: {prev.split('] ', 1)[-1]}")

    _write(f"{_ts()} [启动] pid={os.getpid()} python={sys.version.split()[0]} "
           f"argv={' '.join(sys.argv[1:]) or '(无)'}")

    # ② 正常退出（含 sys.exit / Ctrl+C 走 sys.exit 的路径）
    atexit.register(_on_exit)

    # ③ 未捕获异常
    def _hook(exc_type, exc, tb):
        _write(f"{_ts()} [退出] 原因=未捕获异常: {exc_type.__name__}: {exc}")
        _write("        traceback:\n" +
               "".join("          " + l for l in traceback.format_exception(exc_type, exc, tb)))
        _finalize("未捕获异常", ok=False)
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _hook

    # ④ 信号：SIGTERM / SIGBREAK（关控制台窗口）/ SIGINT
    try:
        import signal

        def _sig(signum, _frame):
            name = {
                getattr(signal, "SIGTERM", None): "SIGTERM(要求终止)",
                getattr(signal, "SIGINT", None): "SIGINT(Ctrl+C)",
                getattr(signal, "SIGBREAK", None): "SIGBREAK(控制台关闭/中断)",
            }.get(signum, f"信号{signum}")
            _write(f"{_ts()} [退出] 原因=收到{name}  阶段={_phase} "
                   f"uptime={_fmt_dur(time.time() - _start_ts)}")
            _finalize(f"信号 {name}", ok=False)
            # 恢复默认行为并重发，保证退出码正确
            try:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
            except Exception:
                sys.exit(128 + signum)

        for sname in ("SIGTERM", "SIGINT", "SIGBREAK"):
            s = getattr(signal, sname, None)
            if s is not None:
                try:
                    signal.signal(s, _sig)
                except Exception:
                    pass
    except Exception:
        pass


def set_phase(phase: str) -> None:
    """记录当前阶段，便于定位"死在哪一步"。"""
    global _phase
    _phase = phase


def note_exit(reason: str, code: int = 0) -> None:
    """主动退出前调用，写明原因（如"已有一个实例在运行"）。"""
    global _exit_noted
    _exit_noted = True
    _write(f"{_ts()} [退出] 原因=主动退出: {reason} (code={code}) 阶段={_phase} "
           f"uptime={_fmt_dur(time.time() - _start_ts)}")


def _finalize(reason: str, ok: bool) -> None:
    global _exit_noted
    if _exit_noted:
        return


def _on_exit() -> None:
    """atexit 回调 —— 只有**受控退出**才会走到这里。

    被 TerminateProcess（taskkill /F、任务管理器"结束任务"）杀掉时，
    这个函数**根本不会执行** —— 这正是我们区分两类退出的依据：
    日志里有退出记录 = 自己退的；没有任何记录 = 被强杀。
    """
    global _exit_noted
    if _exit_noted:
        return
    _exit_noted = True
    _write(f"{_ts()} [退出] 原因=正常退出(atexit 触发) 阶段={_phase} "
           f"uptime={_fmt_dur(time.time() - _start_ts)}")
    # 发送残缺审计：还有没发完的气泡就把它们记下来。
    # 这类"静默残缺"是用户报告的（2026-09-20 18:10 长回复只发出前 3 段），
    # 必须在退出时留下痕迹，否则事后无从追查。
    try:
        import state as _st
        left = _st.incomplete_batches()
        if left:
            _write(f"{_ts()} [警告] 退出时仍有 {len(left)} 个发送批次未完成 "
                   f"（部分气泡已丢失）: {left}")
    except Exception:
        pass


def summary(limit: int = 12) -> dict:
    """给面板用的退出归因摘要。

    返回 {ok, current: {pid, uptime, phase}, last_exit: str, forced: bool,
          history: [最近若干行]}
    """
    out = {
        "ok": True,
        "current": {
            "pid": os.getpid(),
            "uptime": _fmt_dur(time.time() - _start_ts),
            "phase": _phase,
        },
        "last_exit": "",
        "forced": False,
        "history": [],
    }
    try:
        if not os.path.exists(_LOG):
            return out
        with io.open(_LOG, encoding="utf-8", errors="replace") as f:
            lines = [x.rstrip("\n") for x in f if x.strip()]
        out["history"] = lines[-limit:]
        # 本次启动之后的退出记录 = 上次是怎么退的
        starts = [i for i, l in enumerate(lines) if "[启动]" in l]
        if len(starts) >= 2:
            prev_start, cur_start = starts[-2], starts[-1]
            between = [l for l in lines[prev_start + 1:cur_start]]
            exits = [l for l in between if "[退出]" in l]
            forceds = [l for l in between if "[异常]" in l and "强制终止" in l]
            if exits:
                out["last_exit"] = exits[-1]
            elif forceds:
                out["last_exit"] = "上次被**强制终止**（无退出记录）"
                out["forced"] = True
            else:
                out["last_exit"] = "（无记录）"
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
    return out
