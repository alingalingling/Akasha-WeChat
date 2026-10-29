"""
Web 控制面板模块。

提供可视化控制页面（http://127.0.0.1:WEB_PORT），
支持启停/暂停/恢复桥接，显示运行状态和日志，
以及在线编辑 config.json 配置。
"""

import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import state
import config

log = logging.getLogger("ob11-bridge")


def _read_config() -> dict:
    """读 config.json；万一文件已损坏，自动回退到最近的可用备份。

    为什么要有回退：这个文件被面板与启动流程两边读写，历史上损坏过
    （现场留下 config.json.bak_broken_201419）。损坏后若直接抛异常，
    面板所有"保存"都会失败，用户只能手动修 JSON —— 所以这里尽量自愈。
    """
    try:
        with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"[Web] config.json 读取失败（{e}），尝试从备份恢复")
        d = os.path.dirname(config.CONFIG_FILE) or "."
        base = os.path.basename(config.CONFIG_FILE)
        try:
            cands = sorted(
                (os.path.join(d, n) for n in os.listdir(d)
                 if n.startswith(base + ".") and "broken" not in n
                 and not n.endswith(".tmp")),
                key=lambda p: os.path.getmtime(p), reverse=True)
        except OSError:
            raise
        for p in cands:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                log.warning(f"[Web] 已从备份恢复配置: {os.path.basename(p)}")
                return data
            except Exception:
                continue
        raise


def _atomic_write_json(path: str, data: dict) -> None:
    """原子写 JSON：先写临时文件、再 os.replace 顶替。

    为什么必须原子：以前是 `open(path,'w')` 就地改写。面板被连点两下
    （或开了两个标签页）时两次保存会并发，后一次的写入落在已写一半的文件上，
    产生 `Extra data: line 35 column 2` 这种"拼接了两份 JSON"的损坏文件。
    os.replace 在同一分区上是原子的 —— 读到的永远是完整的旧版或完整的新版。
    """
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Akasha-WeChat Bridge</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><text y='28' font-size='28'>💎</text></svg>">
<style>
:root {
  --bg-base: #f4f1ea;
  --bg-panel: rgba(255, 253, 247, .86);
  --bg-card: rgba(255, 255, 255, .92);
  --bg-elevated: #fffdf7;
  --bg-hover: rgba(217, 166, 72, .08);
  --bg-active: rgba(217, 166, 72, .14);
  --bg-input: #fffefb;
  --bg-log: #26272b;
  --text-strong: #2f3135;
  --text-main: #464b52;
  --text-muted: #7d838c;
  --text-soft: #a3a8af;
  --text-inverse: #ffffff;
  --accent: #d9a648;
  --accent-strong: #a88428;
  --accent-deep: #8a6b20;
  --line: #dedbd1;
  --line-soft: #e9e6dc;
  --line-strong: #c9c4b6;
  --success: #5f8f5f;
  --success-bg: #ecf4ec;
  --success-line: #cfe3cf;
  --warning: #b9802b;
  --warning-bg: #fbf2df;
  --warning-line: #ecd7a7;
  --danger: #b5524a;
  --danger-bg: #f9eceb;
  --danger-line: #e7c6c3;
  --info: #68788c;
  --info-bg: #eef2f6;
  --info-line: #d4dce4;
  --shadow-sm: 0 1px 3px rgba(47, 49, 53, .07);
  --shadow-md: 0 3px 12px rgba(47, 49, 53, .08);
  --shadow-lg: 0 10px 32px rgba(47, 49, 53, .13);
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
  --radius-xl: 20px;
  --font-ui: "Segoe UI", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  --font-display: "Bahnschrift", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  --font-mono: "Cascadia Mono", Consolas, "Courier New", monospace;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

html, body { height: 100%; }

body {
  font-family: var(--font-ui);
  background:
    radial-gradient(1200px 600px at 80% -10%, rgba(217, 166, 72, .13), transparent 60%),
    linear-gradient(135deg, #f7f5ef 0%, #efede5 45%, #e6e4dc 100%);
  color: var(--text-main);
  height: 100vh;
  margin: 0;
  overflow: hidden;
  letter-spacing: .01em;
  -webkit-font-smoothing: antialiased;
}

.container {
  display: flex;
  width: 100vw;
  height: 100vh;
  background: var(--bg-panel);
  backdrop-filter: blur(24px);
  overflow: hidden;
}

/* ===== 侧边栏 ===== */
.sidebar {
  width: 168px;
  min-width: 168px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  padding: 22px 14px;
  background: linear-gradient(180deg, rgba(255,253,247,.96) 0%, rgba(247,245,239,.94) 100%);
  border-right: 1px solid var(--line);
  box-shadow: 1px 0 0 rgba(47,49,53,.02), 4px 0 18px rgba(47,49,53,.04);
  z-index: 10;
}

.sidebar .logo {
  font-family: var(--font-display);
  font-size: 21px;
  font-weight: 800;
  color: var(--text-strong);
  letter-spacing: .18em;
  text-transform: uppercase;
  margin: 4px 8px 24px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.sidebar .logo::after {
  content: "";
  flex: 1;
  height: 2px;
  background: linear-gradient(90deg, var(--accent), transparent);
  border-radius: 2px;
}

.sidebar .nav-item {
  width: 100%;
  height: 46px;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 10px;
  padding: 0 14px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-muted);
  font-size: 13.5px;
  font-weight: 650;
  cursor: pointer;
  transition: background .18s ease, color .18s ease, transform .18s ease, box-shadow .18s ease;
  position: relative;
}
.sidebar .nav-item .icon { font-size: 17px; line-height: 1; }
.sidebar .nav-item:hover { background: var(--bg-hover); color: var(--accent-strong); }
.sidebar .nav-item.active {
  background: linear-gradient(90deg, rgba(217,166,72,.18), rgba(217,166,72,.06));
  color: var(--accent-deep);
  box-shadow: inset 0 0 0 1px rgba(217,166,72,.25);
}
.sidebar .nav-item.active::before {
  content: "";
  position: absolute;
  left: 0; top: 10px; bottom: 10px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--accent);
}
.sidebar .nav-item.active:hover { color: var(--accent-deep); }

.sidebar .side-foot {
  margin-top: auto;
  font-size: 10.5px;
  color: var(--text-soft);
  text-align: center;
  line-height: 1.7;
  padding: 12px 8px 0;
}

/* ===== 内容区 ===== */
.content {
  flex: 1;
  height: 100vh;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 22px 28px 18px;
  overflow-y: auto;
}
.content::-webkit-scrollbar { width: 8px; }
.content::-webkit-scrollbar-track { background: transparent; }
.content::-webkit-scrollbar-thumb { background: var(--line-strong); border-radius: 8px; }
.content::-webkit-scrollbar-thumb:hover { background: var(--accent); }

.tab-page { display: none; flex-direction: column; gap: 16px; height: 100%; min-height: 0; }
.tab-page.active { display: flex; }

/* ===== 头部 ===== */
.header {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding-bottom: 4px;
}
.header h1 {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 800;
  color: var(--text-strong);
  letter-spacing: .04em;
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}
.header h1 .pname {
  font-family: var(--font-display);
  font-size: 21px;
  font-weight: 800;
  color: var(--text-strong);
  letter-spacing: .05em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: min(52vw, 640px);
}
.header h1 .en {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 800;
  color: var(--text-strong);
  letter-spacing: .08em;
  margin-right: 8px;
}
.header h1 .cn {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-main);
  letter-spacing: .12em;
}
.header h1 .ver {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-soft);
  letter-spacing: .08em;
  padding: 3px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--bg-card);
}
.header .sub { font-size: 12.5px; color: var(--text-muted); }

.badge {
  background: var(--bg-card);
  color: var(--text-muted);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 5px 12px;
  font-size: 11.5px;
  font-weight: 700;
  letter-spacing: .04em;
  box-shadow: var(--shadow-sm);
}
.badge.ok { background: var(--success-bg); color: var(--success); border-color: var(--success-line); }
.badge.warn { background: var(--warning-bg); color: var(--warning); border-color: var(--warning-line); }

/* ===== 卡片 ===== */
.card {
  background: var(--bg-card);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-lg);
  padding: 18px 20px;
  box-shadow: var(--shadow-sm);
}
.card > h3 {
  display: flex;
  align-items: center;
  gap: 8px;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 750;
  color: var(--text-strong);
  letter-spacing: .05em;
  margin-bottom: 4px;
}
.card > h3 .ic { font-size: 16px; line-height: 1; }
.card > .card-sub {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.7;
  margin-bottom: 12px;
}

/* ===== 控制面板 ===== */
.status-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.status-card {
  background: var(--bg-card);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  box-shadow: var(--shadow-sm);
  position: relative;
  overflow: hidden;
}
.status-card::after {
  content: "";
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 3px;
  background: linear-gradient(180deg, var(--accent), rgba(217,166,72,.2));
}
.status-card .label {
  font-size: 11px;
  color: var(--text-soft);
  letter-spacing: .12em;
  text-transform: uppercase;
  margin-bottom: 5px;
}
.status-card .value {
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 750;
  color: var(--text-strong);
  letter-spacing: .02em;
}

.btn-row { display: flex; gap: 10px; flex-wrap: wrap; }

.mode-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  background: var(--bg-card);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  padding: 11px 16px;
  font-size: 13px;
  color: var(--text-main);
  box-shadow: var(--shadow-sm);
}
.mode-value { font-weight: 800; color: var(--accent-deep); }

.log-box {
  flex: 1;
  min-height: 220px;
  background: var(--bg-log);
  color: #e7e5df;
  border: 1px solid rgba(255,255,255,.06);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.65;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.03);
}
.log-box::-webkit-scrollbar { width: 8px; }
.log-box::-webkit-scrollbar-thumb { background: #4b4d52; border-radius: 8px; }
.log-box::-webkit-scrollbar-thumb:hover { background: var(--accent-strong); }

/* ===== 按钮 ===== */
.btn {
  border: 0;
  border-radius: var(--radius-md);
  padding: 9px 18px;
  font-family: var(--font-ui);
  font-size: 13px;
  font-weight: 700;
  letter-spacing: .03em;
  color: var(--text-inverse);
  cursor: pointer;
  transition: transform .16s ease, box-shadow .16s ease, background .16s ease, opacity .16s ease;
  box-shadow: var(--shadow-sm);
}
.btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: var(--shadow-md); }
.btn:active:not(:disabled) { transform: translateY(0); }
.btn:disabled { opacity: .42; cursor: not-allowed; box-shadow: none; transform: none; }

.btn-pink {
  background: linear-gradient(135deg, #e0b45e, #c1902f);
  color: #fffdf7;
}
.btn-pink:hover:not(:disabled) { box-shadow: 0 4px 14px rgba(193, 144, 47, .32); }

.btn-red {
  background: linear-gradient(135deg, #c46a62, #a94a42);
}
.btn-red:hover:not(:disabled) { box-shadow: 0 4px 14px rgba(169, 74, 66, .28); }

.btn-amber {
  background: linear-gradient(135deg, #d3a24a, #b57e26);
  color: #fffdf7;
}
.btn-amber:hover:not(:disabled) { box-shadow: 0 4px 14px rgba(181, 126, 38, .28); }

.btn-green {
  background: linear-gradient(135deg, #6f9d6f, #4f7b4f);
}
.btn-green:hover:not(:disabled) { box-shadow: 0 4px 14px rgba(79, 123, 79, .26); }

.btn-outline {
  background: var(--bg-card);
  color: var(--accent-deep);
  border: 1px solid rgba(217, 166, 72, .45);
}
.btn-outline:hover:not(:disabled) { background: var(--bg-active); border-color: var(--accent); }

.btn-sm { padding: 6px 12px; font-size: 12px; border-radius: var(--radius-sm); }

/* ===== 设置表单 ===== */
.settings-scroll {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 14px;
  overflow-y: auto;
  padding: 2px 4px 8px 0;
  min-height: 0;
}
.settings-scroll::-webkit-scrollbar { width: 8px; }
.settings-scroll::-webkit-scrollbar-thumb { background: var(--line-strong); border-radius: 8px; }

.set-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  background: var(--bg-card);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  padding: 10px 14px;
  position: sticky;
  top: 0;
  z-index: 5;
  box-shadow: var(--shadow-sm);
}
.set-bar .search-input { flex: 1; min-width: 220px; }
.set-note { font-size: 11.5px; color: var(--text-muted); }

.settings-row {
  display: flex;
  flex-wrap: wrap;
  gap: 14px 18px;
}
.settings-field {
  flex: 1;
  min-width: 240px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.settings-field.wide { flex-basis: 100%; }
.settings-field label {
  font-size: 12px;
  color: var(--text-main);
  font-weight: 650;
  line-height: 1.55;
  letter-spacing: .02em;
}

.settings-field input,
.settings-field select,
.settings-field textarea {
  width: 100%;
  padding: 9px 11px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-strong);
  font-family: inherit;
  font-size: 12.5px;
  outline: none;
  transition: border-color .18s ease, box-shadow .18s ease, background .18s ease;
}
.settings-field input:focus,
.settings-field select:focus,
.settings-field textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(217, 166, 72, .14);
  background: #fff;
}
.settings-field textarea { resize: vertical; }

.restart-badge,
.new-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  border-radius: 6px;
  padding: 1px 6px;
  margin-left: 6px;
  vertical-align: 1px;
  letter-spacing: .04em;
}
.restart-badge { background: var(--warning-bg); color: var(--warning); border: 1px solid var(--warning-line); }
.new-badge { background: var(--info-bg); color: var(--info); border: 1px solid var(--info-line); }

.field-hint { font-size: 11px; color: var(--text-muted); line-height: 1.65; }

/* ===== 折叠层 ===== */
details.tier {
  background: var(--bg-card);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}
details.tier > summary {
  list-style: none;
  cursor: pointer;
  padding: 13px 18px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
  font-weight: 750;
  color: var(--text-main);
  user-select: none;
  transition: background .18s ease, color .18s ease;
}
details.tier > summary:hover { background: var(--bg-hover); color: var(--accent-deep); }
details.tier > summary::-webkit-details-marker { display: none; }
details.tier > summary .arrow {
  font-size: 11px;
  color: var(--text-soft);
  transition: transform .18s ease;
}
details.tier[open] > summary .arrow { transform: rotate(90deg); }
details.tier > summary .count { font-size: 11px; color: var(--text-soft); font-weight: 600; }
details.tier > .tier-body {
  padding: 2px 18px 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
details.tier.warn { border-color: var(--warning-line); }
details.tier.warn > summary { color: var(--warning); }
details.tier.warn > .tier-body { background: linear-gradient(180deg, rgba(251,242,223,.45), transparent); }

/* ===== 信息提示 ===== */
.hint-text {
  font-size: 12px;
  color: var(--text-main);
  line-height: 1.75;
  background: var(--info-bg);
  border: 1px solid var(--info-line);
  border-radius: var(--radius-md);
  padding: 10px 14px;
}
.hint-text b { color: var(--accent-deep); }
.hint-text.amber { background: var(--warning-bg); border-color: var(--warning-line); }
.hint-text.amber b { color: var(--warning); }

.info-line {
  flex-basis: 100%;
  max-width: 100%;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-main);
  background: var(--bg-elevated);
  border: 1px solid var(--line-soft);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-md);
  padding: 10px 14px;
}

/* ===== 开关 ===== */
.switch {
  position: relative;
  display: inline-block;
  width: 40px;
  height: 22px;
  flex: none;
}
.switch input {
  position: absolute;
  opacity: 0;
  width: 0;
  height: 0;
  margin: 0;
  padding: 0;
  border: 0;
  background: none;
  appearance: none;
  -webkit-appearance: none;
}
.switch .sl {
  position: absolute;
  inset: 0;
  background: #d7d2c7;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  cursor: pointer;
  transition: background .2s ease, border-color .2s ease;
}
.switch .sl::before {
  content: "";
  position: absolute;
  height: 16px;
  width: 16px;
  left: 2px;
  top: 2px;
  background: #fff;
  border-radius: 50%;
  box-shadow: 0 1px 3px rgba(0,0,0,.18);
  transition: transform .2s ease;
}
.switch input:checked + .sl {
  background: linear-gradient(135deg, #e0b45e, #c1902f);
  border-color: #b98f2d;
}
.switch input:checked + .sl::before { transform: translateX(18px); }
.switch-row {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  color: var(--text-main);
  cursor: pointer;
}
.switch-row .st {
  font-weight: 700;
  color: var(--text-muted);
  user-select: none;
}

/* ===== 保存条 ===== */
.save-bar {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  gap: 12px;
  background: rgba(255, 253, 247, .94);
  backdrop-filter: blur(10px);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  padding: 10px 14px;
  box-shadow: 0 -2px 18px rgba(47,49,53,.07);
  z-index: 5;
  margin-bottom: 12px;
}
.save-bar .spacer { flex: 1; }
.save-msg {
  font-size: 12px;
  font-weight: 700;
  color: var(--success);
  opacity: 0;
  transition: opacity .22s ease;
}
.save-msg.show { opacity: 1; }

/* ===== 表格 ===== */
.member-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 12.5px;
  background: var(--bg-card);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  overflow: hidden;
}
.member-table th {
  background: rgba(217,166,72,.10);
  color: var(--accent-deep);
  text-align: left;
  padding: 9px 10px;
  font-size: 11.5px;
  font-weight: 750;
  letter-spacing: .06em;
  white-space: nowrap;
}
.member-table td {
  padding: 8px 10px;
  border-top: 1px solid var(--line-soft);
  color: var(--text-main);
  word-break: break-all;
  vertical-align: middle;
}
.member-table tr:hover td { background: var(--bg-hover); }
.member-table .wxid { color: var(--text-muted); font-size: 11px; }
.member-table .uid { color: var(--text-muted); font-size: 11px; font-family: var(--font-mono); }
.member-table .gname { font-weight: 700; color: var(--text-strong); }
.member-table .center { text-align: center; }

.tbl-scroll {
  max-height: 420px;
  overflow-y: auto;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
}
.tbl-scroll .member-table { border: 0; border-radius: 0; }
.tbl-scroll::-webkit-scrollbar { width: 8px; }
.tbl-scroll::-webkit-scrollbar-thumb { background: var(--line-strong); border-radius: 8px; }
.tbl-scroll thead th { position: sticky; top: 0; z-index: 2; }

.admin-badge {
  display: inline-block;
  font-size: 10px;
  background: linear-gradient(135deg, #e0b45e, #c1902f);
  color: #fffdf7;
  border-radius: 6px;
  padding: 1px 6px;
  margin-left: 6px;
  font-weight: 700;
}
.copy-chip {
  display: inline-block;
  background: var(--bg-elevated);
  border: 1px solid var(--line);
  color: var(--text-muted);
  border-radius: 8px;
  padding: 2px 8px;
  font-size: 11px;
  font-family: var(--font-mono);
  cursor: pointer;
  white-space: nowrap;
  transition: background .15s ease, border-color .15s ease, color .15s ease;
}
.copy-chip:hover {
  background: var(--bg-active);
  border-color: var(--accent);
  color: var(--accent-deep);
}

.search-input {
  width: 260px;
  max-width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  font-family: inherit;
  font-size: 12px;
  color: var(--text-strong);
  background: var(--bg-input);
  outline: none;
  transition: border-color .18s ease, box-shadow .18s ease;
}
.search-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(217,166,72,.14);
}

.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.toolbar .spacer { flex: 1; }

/* ===== 标签 chip ===== */
.chip-wrap { display: flex; flex-wrap: wrap; gap: 8px 10px; padding: 4px 0; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-main);
  background: var(--bg-elevated);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 4px 11px;
  cursor: pointer;
  user-select: none;
  transition: background .18s ease, border-color .18s ease, color .18s ease;
}
.chip:hover { border-color: var(--accent); color: var(--accent-deep); }
.chip.on {
  background: linear-gradient(135deg, rgba(217,166,72,.18), rgba(217,166,72,.08));
  border-color: rgba(217,166,72,.55);
  color: var(--accent-deep);
  font-weight: 700;
}
.chip input { display: none; }
.chip .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #cfcabd;
  transition: background .18s ease;
}
.chip.on .dot { background: var(--accent-strong); }

/* ===== 成员子页签 ===== */
.seg-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--bg-card);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-md);
  padding: 6px;
  position: sticky;
  top: 0;
  z-index: 6;
  box-shadow: var(--shadow-sm);
}
.seg {
  border: 0;
  background: transparent;
  border-radius: var(--radius-sm);
  padding: 8px 18px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 750;
  color: var(--text-muted);
  cursor: pointer;
  white-space: nowrap;
  transition: background .18s ease, color .18s ease, box-shadow .18s ease;
}
.seg:hover { background: var(--bg-hover); color: var(--accent-deep); }
.seg.active {
  background: linear-gradient(135deg, #e0b45e, #c1902f);
  color: #fffdf7;
  box-shadow: 0 2px 10px rgba(193,144,47,.25);
}
.seg-bar .spacer { flex: 1; }

.members-sub { display: none; flex-direction: column; gap: 14px; }
.members-sub.active { display: flex; }

.dirty-dot {
  font-size: 11.5px;
  font-weight: 800;
  color: var(--warning);
  display: none;
  white-space: nowrap;
}
.dirty-dot.show { display: inline; }

.warn-badge {
  font-size: 11.5px;
  font-weight: 700;
  color: var(--danger);
  background: var(--danger-bg);
  border: 1px solid var(--danger-line);
  border-radius: 8px;
  padding: 4px 10px;
  display: none;
  white-space: nowrap;
}
.warn-badge.show { display: inline-block; }

.btn.need-restart {
  border-color: var(--warning);
  color: var(--warning);
  animation: pulseAmber 1.6s ease-in-out infinite;
}
@keyframes pulseAmber {
  0%, 100% { box-shadow: 0 0 0 0 rgba(185,128,43,.38); }
  50% { box-shadow: 0 0 0 7px rgba(185,128,43,0); }
}

/* ===== Toast ===== */
.toast {
  position: fixed;
  top: 24px;
  left: 50%;
  transform: translateX(-50%) translateY(-8px);
  padding: 10px 22px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 650;
  z-index: 999;
  opacity: 0;
  pointer-events: none;
  box-shadow: var(--shadow-lg);
  transition: opacity .25s ease, transform .25s ease;
}
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.toast.success { background: var(--success-bg); color: var(--success); border: 1px solid var(--success-line); }
.toast.error { background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger-line); }
.toast.info { background: var(--bg-card); color: var(--accent-deep); border: 1px solid rgba(217,166,72,.45); }

/* ===== 响应式 ===== */
@media (max-width: 1080px) {
  .status-row { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .sidebar { width: 76px; min-width: 76px; padding: 18px 10px; }
  .sidebar .logo { justify-content: center; font-size: 15px; margin: 4px 0 20px; }
  .sidebar .logo::after { display: none; }
  .sidebar .nav-item { justify-content: center; padding: 0; }
  .sidebar .nav-item span:not(.icon) { display: none; }
  .sidebar .side-foot { font-size: 9.5px; }
}

@media (max-width: 720px) {
  .container { flex-direction: column; }
  .sidebar {
    width: 100%;
    min-width: 0;
    height: auto;
    flex-direction: row;
    align-items: center;
    padding: 10px;
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }
  .sidebar .logo { margin: 0 12px 0 4px; font-size: 16px; }
  .sidebar .nav-item { width: auto; height: 38px; padding: 0 12px; }
  .sidebar .nav-item span:not(.icon) { display: inline; font-size: 12px; }
  .sidebar .nav-item.active::before { display: none; }
  .sidebar .side-foot { display: none; }
  .content { padding: 14px 14px 12px; height: calc(100vh - 59px); }
  .status-row { grid-template-columns: 1fr; }
  .header h1 .pname { max-width: 68vw; font-size: 18px; }
  .settings-field { min-width: 100%; }
  .save-bar { flex-wrap: wrap; }
  .save-bar .spacer { display: none; }
  .log-box { min-height: 180px; }
}
</style>
</head>
<body>

<div class="toast" id="toast"></div>

<div class="container">

<!-- ===== 侧边栏 ===== -->
<div class="sidebar">
  <div class="logo">Akasha</div>
  <button class="nav-item active" data-tab="dashboard" onclick="switchTab('dashboard')">
    <span class="icon">🏠</span><span>控制面板</span>
  </button>
  <button class="nav-item" data-tab="settings" onclick="switchTab('settings')">
    <span class="icon">⚙️</span><span>基础设置</span>
  </button>
  <button class="nav-item" data-tab="members" onclick="switchTab('members')">
    <span class="icon">👥</span><span>成员与权限</span>
  </button>
  <div class="side-foot">微信 ↔ AstrBot<br>桥接面板</div>
</div>

<!-- ===== 内容区 ===== -->
<div class="content">

  <!-- ===== 面板页 ===== -->
  <div class="tab-page active" id="page-dashboard">
    <div class="header">
      <h1><span class="pname" id="pname">Akasha-WeChat</span>
        <span class="ver" id="verTag">v-</span></h1>
      <div class="badge" id="statusText">加载中...</div>
    </div>

    <div class="status-row">
      <div class="status-card"><div class="label">桥接状态</div><div class="value" id="bridgeStatus">-</div></div>
      <div class="status-card"><div class="label">AstrBot</div><div class="value" id="obStatus">-</div></div>
      <div class="status-card"><div class="label">接收端</div><div class="value" id="recvStatus" style="font-size:13px">-</div></div>
      <div class="status-card"><div class="label">发送端</div><div class="value" id="sendMethod" style="font-size:13px">-</div></div>
    </div>

    <div class="btn-row">
      <button class="btn btn-pink" id="btnStart" onclick="action('start')">▶ 启动</button>
      <button class="btn btn-red" id="btnStop" onclick="action('stop')" disabled>■ 停止</button>
      <button class="btn btn-amber" id="btnPause" onclick="action('pause')" disabled>⏸ 暂停</button>
      <button class="btn btn-green" id="btnResume" onclick="action('resume')" style="display:none" disabled>▶ 恢复</button>
    </div>

    <div class="mode-row">
      <span>群聊模式:</span>
      <span class="mode-value" id="modeStatus">-</span>
      <button class="btn btn-outline btn-sm" id="btnToggleMode">切换</button>
      <span style="font-size:11.5px;color:#7d838c">标准 = @必回、非@由 AstrBot 掷骰决定 ｜ 批处理 = 整群消息合并成一条再处理</span>
    </div>

    <div class="log-box" id="log">等待连接...</div>

    <!-- 退出归因：区分「自己崩了」和「被别人杀了」 -->
    <div class="card" style="margin-top:12px">
      <h3><span class="ic">🧭</span>上次是怎么退的</h3>
      <div class="card-sub">
        进程被 <b>taskkill /F</b> 或任务管理器强杀时，Python <b>不会执行任何清理</b>，
        所以日志里不会留痕。这里用「有没有退出记录」来判定：
        <b>有记录 = 自己退的</b>（异常 / 主动退出），<b>没记录 = 被强杀</b>。
      </div>
      <div class="settings-row">
        <div class="settings-field" style="min-width:200px">
          <label>上次退出</label>
          <div id="erLast" class="field-hint" style="font-size:12.5px;color:#464b52">加载中…</div>
        </div>
        <div class="settings-field" style="min-width:170px;max-width:220px">
          <label>本次已运行</label>
          <div id="erUptime" class="field-hint" style="font-size:12.5px;color:#464b52">-</div>
        </div>
        <div class="settings-field" style="min-width:170px;max-width:220px">
          <label>当前阶段</label>
          <div id="erPhase" class="field-hint" style="font-size:12.5px;color:#464b52">-</div>
        </div>
        <div class="settings-field" style="max-width:150px;align-self:end">
          <button class="btn btn-outline btn-sm" onclick="loadExitReason()">🔄 刷新</button>
        </div>
      </div>
      <details style="margin-top:8px">
        <summary style="cursor:pointer;font-size:12.5px;color:#a88428">最近记录（点击展开）</summary>
        <div class="log-box" id="erHistory" style="min-height:120px;max-height:260px;margin-top:8px;font-size:11px">-</div>
      </details>
    </div>
  </div>

  <!-- ===== 设置页 ===== -->
  <div class="tab-page" id="page-settings">
    <div class="header">
      <h1><span class="en">Settings</span><span class="cn">基础设置</span></h1>
      <div class="badge">config.json</div>
      <span class="sub">改完记得重启桥接才生效（群聊模式除外，立即生效）</span>
    </div>

    <div class="set-bar">
      <input class="search-input" id="cfgSearch" placeholder="🔍 搜设置项，比如：图片 / 昵称 / 端口 / Token" oninput="filterSettings()">
      <span class="set-note" id="cfgSearchCount"></span>
    </div>

    <div class="settings-scroll" id="settingsForm">
      <!-- 由 JS 动态渲染 -->
    </div>

    <div class="save-bar">
      <span class="save-msg" id="saveMsg">✅ 已保存</span>
      <span class="set-note">💾 保存后<b style="color:#b9802b">重启桥接</b>生效；群聊模式改动立即生效</span>
      <div class="spacer"></div>
      <button class="btn btn-pink" onclick="saveConfig()">💾 保存配置</button>
    </div>
  </div>

  <!-- ===== 成员与权限页（2026-10-07 重设计：子页签 + 统一保存条） ===== -->
  <div class="tab-page" id="page-members">
    <div class="header">
      <h1><span class="en">People</span><span class="cn">成员与权限</span></h1>
      <div class="badge" id="membersBadge">加载中...</div>
    </div>

    <div class="seg-bar">
      <button class="seg active" data-sub="groups" onclick="switchMembersTab('groups')">💬 群聊</button>
      <button class="seg" data-sub="friends" onclick="switchMembersTab('friends')">🧑‍🤝‍🧑 私聊</button>
      <button class="seg" data-sub="styles" onclick="switchMembersTab('styles')">🎭 风格</button>
      <button class="seg" data-sub="admins" onclick="switchMembersTab('admins')">👑 管理员</button>
      <div class="spacer"></div>
      <button class="btn btn-outline btn-sm" id="btnRefreshPeople" onclick="refreshPeople()" title="从 WeFlow 重新拉一遍联系人 / 群列表；bot 被拉进新群后点它立即收录">🔄 刷新名单</button>
    </div>

    <div class="settings-scroll">

      <!-- ── 子页：群聊 ── -->
      <div class="members-sub active" id="sub-groups">

        <div class="card">
          <h3><span class="ic">💬</span>群里管什么</h3>
          <div class="card-sub">
            <b>回复</b> = 关掉就完全不理这个群（@ 也不理），<b>立即生效</b>；
            <b>插嘴</b> = 没人 @ 时它会不会自己说话，改完点右下「💾 保存」。
            bot 刚被拉进的群没列出来？点右上「🔄 刷新名单」。
          </div>
          <div class="toolbar" style="margin-bottom:8px">
            <input class="search-input" id="groupSearch" placeholder="🔍 搜群名 / 群 ID" oninput="renderGroups()">
            <div class="spacer"></div>
            <span class="set-note" id="groupCount"></span>
          </div>
          <table class="member-table">
            <thead><tr>
              <th style="width:52px" class="center" title="收到消息后要不要理这个群；关掉立即生效">回复</th>
              <th style="width:52px" class="center" title="没人 @ 时允许它自己插嘴；保存并重启 AstrBot 后生效">插嘴</th>
              <th>群名</th>
              <th style="width:250px">群 ID / UMO（点击复制）</th>
            </tr></thead>
            <tbody id="groupsBody"><tr><td colspan="4" style="color:#7d838c">加载中...</td></tr></tbody>
          </table>
        </div>

        <div class="card">
          <h3><span class="ic">🎲</span>随机插话（全局）</h3>
          <div class="card-sub">
            群消息没 @ 它时，按概率掷骰决定要不要插一嘴；关掉 = 只有 @ 才说话。
            生效前提：群聊模式为「标准」、该群用 /new 建过会话、保存后重启 AstrBot。
          </div>
          <div class="settings-row">
            <div class="settings-field" style="min-width:140px;max-width:180px">
              <label>总开关</label>
              <select id="ab_ar_enable" onchange="markDirty()"><option value="0">关闭</option><option value="1">开启</option></select>
            </div>
            <div class="settings-field" style="min-width:150px;max-width:200px">
              <label>概率（0~1，建议 0.02~0.05）</label>
              <input type="number" id="ab_ar_poss" step="0.01" min="0" max="1" placeholder="0.03" oninput="markDirty()">
            </div>
          </div>
        </div>

      </div>

      <!-- ── 子页：私聊 ── -->
      <div class="members-sub" id="sub-friends">
        <div class="card" id="fwCard">
          <h3><span class="ic">🧑‍🤝‍🧑</span>私聊白名单</h3>
          <div class="card-sub">
            开关开启后<b>只回复勾选的好友</b>（群消息不受这份名单影响）。
            改完点右下「💾 保存」，再「🔄 重启 AstrBot」才生效。
            新好友不在列表里？让对方先发一条消息，再点「🔄 刷新名单」。
          </div>
          <div class="settings-row" style="margin-bottom:8px">
            <div class="settings-field" style="min-width:170px;max-width:210px">
              <label>开关</label>
              <select id="fw_enable" onchange="syncWlEnable(this)"><option value="0">关闭（所有私聊都回）</option><option value="1">开启（只回勾选的好友）</option></select>
            </div>
            <div class="settings-field" style="flex:1;min-width:200px">
              <label>搜索好友</label>
              <input class="search-input" id="fwSearch" style="width:100%" placeholder="🔍 名字 / wxid / UID，输入即筛选" oninput="renderFriendList('fw_friends', currentWhitelist)">
            </div>
            <div class="settings-field" style="max-width:120px;align-self:end">
              <label style="visibility:hidden">.</label>
              <label class="chip" style="width:100%;justify-content:center"><input type="checkbox" id="fwOnlySel" onchange="renderFriendList('fw_friends', currentWhitelist)"><span class="dot"></span>只看已勾</label>
            </div>
          </div>
          <div class="chip-wrap" id="fw_friends" style="max-height:420px;overflow:auto;border:1px solid #dedbd1;border-radius:10px;padding:8px"></div>
          <div class="field-hint" style="margin-top:6px"><span id="fwCount">-</span>　（搜索只筛选显示，不影响已勾选项）</div>
        </div>
      </div>

      <!-- ── 子页：会话风格 ── -->
      <div class="members-sub" id="sub-styles">
        <div class="card">
          <h3><span class="ic">🎭</span>会话检索风格</h3>
          <div class="card-sub">
            这里管理「好想成为人类啊」的风格注入：每个会话可选一个专属风格档案。
            没有特殊设置的会话继续使用全局风格；保存后无需重启 AstrBot。
          </div>
          <div class="settings-row" style="margin-bottom:8px">
            <div class="settings-field" style="min-width:220px;max-width:280px">
              <label>全局风格（AstrBot 插件里设置）</label>
              <input id="hsGlobalStyle" disabled placeholder="-">
            </div>
            <div class="settings-field" style="flex:1;min-width:200px">
              <label>搜索会话</label>
              <input class="search-input" id="hsSearch" style="width:100%" placeholder="🔍 群名 / 好友名 / UMO" oninput="renderHumanizerStyles()">
            </div>
            <div class="settings-field" style="max-width:130px;align-self:end">
              <label style="visibility:hidden">.</label>
              <label class="chip" style="width:100%;justify-content:center"><input type="checkbox" id="hsOnlyCustom" onchange="renderHumanizerStyles()"><span class="dot"></span>只看自定义</label>
            </div>
          </div>
          <div class="tbl-scroll">
            <table class="member-table">
              <thead><tr>
                <th>会话</th>
                <th style="width:210px">使用风格</th>
                <th style="width:250px">UMO（点击复制）</th>
              </tr></thead>
              <tbody id="hsBody"><tr><td colspan="3" style="color:#7d838c">加载中...</td></tr></tbody>
            </table>
          </div>
          <div class="field-hint" style="margin-top:6px"><span id="hsCount">-</span>　（「跟随全局」不写入配置；风格档案仍需在人类风格插件中创建）</div>
        </div>
      </div>

      <!-- ── 子页：管理员 ── -->
      <div class="members-sub" id="sub-admins">
        <div class="card">
          <h3><span class="ic">👑</span>管理员</h3>
          <div class="card-sub">
            管理员可用跨群发消息等敏感能力。同一个人在私聊和所有群里是同一个 ID，勾一次全局生效。
            桥接侧保存后立即生效；AstrBot 侧需重启。
          </div>
          <div class="toolbar" style="margin-bottom:8px">
            <input class="search-input" id="memberSearch" placeholder="🔍 搜名字 / wxid" oninput="renderPeople()">
            <div class="spacer"></div>
            <span class="set-note">共 <span id="peopleCount">-</span> 人</span>
          </div>
          <div class="tbl-scroll">
          <table class="member-table">
            <thead><tr><th style="width:44px" class="center">管理</th><th>昵称</th><th style="width:190px">wxid</th><th>UID（/sid 显示的 ID）</th></tr></thead>
            <tbody id="peopleBody"><tr><td colspan="4" style="color:#7d838c">加载中...</td></tr></tbody>
          </table>
          </div>
        </div>
      </div>

    </div>

    <div class="save-bar">
      <span class="dirty-dot" id="dirtyDot">● 未保存</span>
      <span class="save-msg" id="abMsg">✅ 已保存</span>
      <span class="warn-badge" id="wlRestartWarn">🔴 已保存但还没生效 —— 点「重启 AstrBot」</span>
      <div class="spacer"></div>
      <span id="abRestartState" class="field-hint" style="margin:0"></span>
      <button class="btn btn-outline btn-sm" id="abRestartBtn" onclick="restartAstrbot()">🔄 重启 AstrBot</button>
      <button class="btn btn-pink" id="abSaveBtn" onclick="saveAll()">💾 保存</button>
    </div>
  </div>
</div>
</div>

<script>
// ===== 工具 =====
function toast(msg, type) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type + ' show';
  setTimeout(function(){t.className='toast'}, 2500);
}

function showMsg(id, text) {
  var el = document.getElementById(id);
  el.textContent = text;
  el.className = 'save-msg show';
  setTimeout(function(){el.className='save-msg'}, 3000);
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ===== Tab 切换 =====
function switchTab(name) {
  document.querySelectorAll('.tab-page').forEach(function(p){p.classList.remove('active')});
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});
  document.querySelector('[data-tab="' + name + '"]').classList.add('active');
  if (name === 'settings') loadConfig();
  if (name === 'members') switchTabMembers();
}

// ===== 面板刷新 =====
var modeMap = {'mention':'标准模式','all':'标准模式','batch':'批处理'};

// 退出归因：显示「上次是怎么退的」，并区分崩溃 / 主动 / 被强杀
function loadExitReason() {
  fetch('/api/exit-reason').then(function(r){return r.json()}).then(function(d){
    if (!d.ok) {
      document.getElementById('erLast').textContent = '读取失败: ' + (d.error || '?');
      return;
    }
    var cur = d.current || {};
    document.getElementById('erUptime').textContent = cur.uptime || '-';
    document.getElementById('erPhase').textContent = cur.phase || '-';

    var el = document.getElementById('erLast');
    var txt = d.last_exit || '（无记录）';
    el.textContent = txt;
    el.style.color = d.forced ? '#b5524a'
                   : (/未捕获异常/.test(txt) ? '#b9802b' : '#5f8f5f');
    if (d.forced) {
      el.textContent = '⚠️ ' + txt + '（上次进程被强杀，未留下任何清理记录）';
    }

    var h = document.getElementById('erHistory');
    h.textContent = (d.history && d.history.length)
      ? d.history.join('\\n') : '（暂无记录）';
  }).catch(function(e){
    document.getElementById('erLast').textContent = '请求失败: ' + e;
  });
}

function refreshDashboard() {
  fetch('/status').then(function(r){return r.json()}).then(function(s){
    var st = document.getElementById('bridgeStatus');
    if (!s.running) { st.textContent='未运行'; st.style.color='#7d838c';
    } else if (s.paused) { st.textContent='已暂停'; st.style.color='#b9802b';
    } else { st.textContent='运行中'; st.style.color='#5f8f5f'; }

    document.getElementById('statusText').textContent = s.running ? (s.paused ? '已暂停' : '运行中') : '未运行';
    document.getElementById('obStatus').textContent = s.ob_connected ? '已连接' : '未连接';
    document.getElementById('obStatus').style.color = s.ob_connected ? '#5f8f5f' : '#7d838c';
    // 接收端 = 消息源（weflow/alpha），SSE 是否连上
    var srcName = (s.message_source === 'alpha') ? 'Alpha' : 'WeFlow';
    document.getElementById('recvStatus').textContent = srcName + (s.weflow_connected ? ' 已连接' : ' 未连接');
    document.getElementById('recvStatus').style.color = s.weflow_connected ? '#5f8f5f' : '#7d838c';
    // 发送端 = 发送方式（uia/alpha_api）
    var sendName = (s.send_method === 'alpha_api') ? 'Alpha API' : 'UIA';
    document.getElementById('sendMethod').textContent = sendName;

    // 项目标识：标题与抬头由后端下发（改 config.PROJECT_NAME / VERSION 即可生效）
    if (s.project_version) {
      document.getElementById('verTag').textContent = 'v' + s.project_version;
      document.title = s.project_name + ' v' + s.project_version;
    }

    document.getElementById('btnStart').disabled = s.running;
    document.getElementById('btnStop').disabled = !s.running;
    if (s.paused) {
      document.getElementById('btnPause').style.display = 'none';
      document.getElementById('btnResume').style.display = 'inline-block';
      document.getElementById('btnResume').disabled = false;
    } else {
      document.getElementById('btnPause').style.display = 'inline-block';
      document.getElementById('btnPause').disabled = !s.running;
      document.getElementById('btnResume').style.display = 'none';
    }

    document.getElementById('modeStatus').textContent = modeMap[s.group_reply_mode] || s.group_reply_mode;

    var logEl = document.getElementById('log');
    var isAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
    logEl.textContent = s.log || '';
    if (isAtBottom) logEl.scrollTop = logEl.scrollHeight;
  });
}

function action(cmd) {
  fetch('/' + cmd, {method:'POST'}).then(function(){setTimeout(refreshDashboard,500)});
}

document.getElementById('btnToggleMode').onclick = function(){
  fetch('/mode', {method:'POST'}).then(function(){setTimeout(refreshDashboard,500)});
};

// ===== 设置定义 =====
// tier: common=常驻展示  adv=折叠（进阶）  legacy=折叠（上游遗留，一般别动）
var CFG_GROUPS = [
  {tier:'common', icon:'🔗', title:'消息链路', sub:'读微信消息的后端 + 回微信消息的方式（改完保存并重启桥接生效）', fields:[
    {key:'message_source', label:'接收端（读微信消息用哪个后端）', type:'select', opts:[
      {v:'weflow',l:'WeFlow（旧链路，已停维护，需 RunAsDate 冻结时钟）'},
      {v:'alpha',l:'Alpha（WeFlow 继任者，原生 API）'}], restart:1},
    {key:'send_method', label:'发送端（回微信消息用什么）', type:'select', opts:[
      {v:'uia',l:'UIA 自动化（默认，不花钱，微信 PC 客户端）'},
      {v:'alpha_api',l:'Alpha API（需 ¥100/月激活「发送文字消息」）'}], restart:1},
    {type:'info', text:'<b>端口提醒</b>：WeFlow 默认 5031、Alpha 默认也是 5031，两个同开会冲突 —— 本机把 Alpha 放在了 <b>5030</b>（设置 → 接口与集成 → 本机 HTTP API）。详见 docs/Alpha接入说明.md。'},
  ]},
  {tier:'common', icon:'🐱', title:'机器人身份', sub:'它在微信里叫什么、被什么名字唤醒', fields:[
    {type:'detect', label:'自动获取（从已登录的微信读取，改完仍可手动改）'},
    {key:'bot_nicknames', label:'机器人昵称（逗号分隔，@ 其中任何一个都会唤醒）', type:'text', ph:'小助, Bot, 小猫'},
    {key:'bot_wxid', label:'机器人自己的 wxid', type:'text', ph:'wxid_xxx', restart:1},
  ]},
  {tier:'common', icon:'💬', title:'消息行为', sub:'它怎么读群消息、怎么回话', fields:[
    {key:'buffer_seconds', label:'消息缓冲（秒）：同一个人连发多条时，等这么久再合并成一条', type:'number', ph:'5'},
    {key:'group_reply_mode', label:'群聊模式（控制台也能一键切换，此项立即生效）', type:'select', opts:[
      {v:'all',l:'标准模式（@必回；非@消息交给 AstrBot 掷骰决定）'},
      {v:'batch',l:'批处理（一段时间内整群消息合并成一条）'}]},
    {key:'mention_as_text', label:'把 @ 转成文字「@昵称」（微信原生 @ 发不出去时的替代方案）', type:'toggle', def:'true'},
    {key:'quote_reply_prefix', label:'引用回复转文字前缀（把「回复某条消息」变成〔回复 某某：原文〕）', type:'toggle', def:'false'},
    {key:'quote_reply_native', label:'原生引用气泡（右键原消息→引用，2026-09-15 已打通；引用时要先横向找准气泡，会多花 1~6 秒，找不到会自动降级普通发送）', type:'toggle', def:'false'},
  ]},
  {tier:'common', icon:'🖼️', title:'图片', sub:'群里发图怎么处理', fields:[
    {key:'image_mention_window', label:'群图片等待 @ 的时间窗（秒）：图先到，同一人在窗口内 @ 才会读图', type:'number', ph:'120'},
    {key:'image_max_bytes', label:'单张图片大小上限（字节），超过就跳过', type:'number', ph:'8388608'},
    {type:'info', text:'<b>「怎么理解图片」不在这里配</b>：图片默认原样转交 AstrBot，由 AstrBot 面板(6185) → 配置 里的主模型（多模态）或图片转述模型决定。下面「上游遗留」里的桥接侧转述是备用方案，默认关闭。'},
  ]},
  {tier:'adv', icon:'🔌', title:'连接', sub:'各后端的地址与令牌（消息源/发送端在上面「消息链路」选）', fields:[
    {key:'alpha_base_url', label:'Alpha 地址', type:'text', ph:'http://127.0.0.1:5030', restart:1},
    {key:'alpha_access_token', label:'Alpha Access Token', type:'password', ph:'输入 Alpha Token', restart:1},
    {key:'alpha_account_id', label:'Alpha 账号 ID（留空 = 自动取唯一就绪账号）', type:'text', ph:'留空自动检测', restart:1},
    {key:'weflow_base_url', label:'WeFlow 地址', type:'text', ph:'http://127.0.0.1:5031', restart:1},
    {key:'access_token', label:'WeFlow Access Token', type:'password', ph:'输入 Token', restart:1},
    {key:'astrbot_ob_url', label:'AstrBot OneBot 反向 WS 地址', type:'text', ph:'ws://127.0.0.1:11229/ws', restart:1},
    {key:'astrbot_attachments', label:'附件目录（AstrBot 存图片的路径）', type:'text', ph:'C:\\astrbot\\attachments'},
    {key:'astrbot_config_file', label:'AstrBot 主配置路径（成员与权限页同步用）', type:'text', ph:'留空 = 自动探测 ../AstrBot/data/cmd_config.json', restart:1},
  ]},
  {tier:'adv', icon:'🧰', title:'面板与排障', sub:'面板端口、日志开关', fields:[
    {key:'web_port', label:'Web 面板端口', type:'number', ph:'8766', restart:1},
    {key:'web_host', label:'Web 面板监听地址', type:'text', ph:'0.0.0.0', restart:1},
    {key:'log_skipped_messages', label:'记录被跳过的消息（排障时强烈建议开着）', type:'toggle', def:'true'},
    {key:'switch_method', label:'联系人切换方式（UIA 发送时怎么找到聊天窗口）', type:'select', opts:[
      {v:'auto',l:'自动（先会话列表，找不到再搜索）'},
      {v:'list',l:'仅会话列表点击'},
      {v:'search',l:'仅 Ctrl+F 搜索'}]},
  ]},
  {tier:'legacy', icon:'📦', title:'发送参数与上游遗留（默认不动）', sub:'发送端在上面「消息链路」选；这里只有细节参数和历史遗留', fields:[
    {key:'alpha_send_wait_seconds', label:'Alpha 发送等待秒数（0~120，仅 send_method=alpha_api 时用）', type:'number', ph:'30', restart:1},
    {key:'weflow_send_api', label:'WeFlow 发送 API（历史遗留，代码已不使用）', type:'text', ph:'http://127.0.0.1:5031/api/v1/message'},
    {key:'image_caption_model', label:'桥接侧图片转述模型名（留空 = 不启用，走 AstrBot 配置）', type:'text', ph:'留空即可'},
    {key:'image_caption_provider', label:'转述服务', type:'select', opts:[{v:'ollama',l:'Ollama 本地'},{v:'openai',l:'OpenAI 兼容'}]},
    {key:'image_caption_api_key', label:'转述 API Key（OpenAI 模式）', type:'password', ph:'sk-xxx'},
    {key:'image_caption_api_base', label:'转述 API 地址（OpenAI 模式）', type:'text', ph:'https://api.moonshot.cn/v1'},
    {key:'image_caption_prompt', label:'转述提示词', type:'textarea', ph:'请用中文描述...'},
    {key:'ollama_base_url', label:'Ollama 地址', type:'text', ph:'http://127.0.0.1:61000'},
    {key:'ollama_timeout', label:'Ollama 超时（秒）', type:'number', ph:'60'},
  ]},
];

// ===== 设置加载 =====
function loadConfig() {
  fetch('/api/config').then(function(r){return r.json()}).then(function(cfg){
    renderConfigForm(cfg);
    filterSettings();
    // wxid 没填 → 自动从微信读一次（用户仍可手动改，自动值只填入不保存）
    if (!String(cfg.bot_wxid || '').trim()) detectBot(true);
  }).catch(function(e){
    document.getElementById('settingsForm').innerHTML = '<p style="color:#b5524a;font-size:13px;">加载配置失败: ' + e.message + '</p>';
  });
}

function renderConfigForm(cfg) {
  var TIER_META = {
    common: null,
    adv: {icon:'🔧', title:'进阶', note:'连接地址、端口、日志 —— 一般不用动'},
    legacy: {icon:'📦', title:'上游遗留', note:'默认关闭的备用方案，保持原样即可'},
  };
  var html = '';

  CFG_GROUPS.forEach(function(g){
    var meta = TIER_META[g.tier];
    var inner = '<div class="settings-row">';
    g.fields.forEach(function(f){ inner += renderField(f, cfg); });
    inner += '</div>';

    if (!meta) {
      html += '<div class="card" data-tier="common"><h3><span class="ic">' + g.icon + '</span>' + g.title + '</h3>'
        + '<div class="card-sub">' + g.sub + '</div>' + inner + '</div>';
    } else {
      html += '<details class="tier' + (g.tier === 'legacy' ? ' warn' : '') + '" data-tier="' + g.tier + '">'
        + '<summary><span class="arrow">▶</span>' + meta.icon + ' ' + g.title
        + ' <span class="count">' + meta.note + '</span></summary>'
        + '<div class="tier-body">' + inner + '</div></details>';
    }
  });

  document.getElementById('settingsForm').innerHTML = html;
}

// 同步开关右侧的「开/关」文字。
// 必须用 class 精确定位，不能用 span:last-child —— 那个选择器会先命中 .switch
// 内部的滑块 <span class="sl">（它是 .switch 的最后一个子元素），把文字写进滑块里，
// 表现为「字叠在圆点上、还被圆点遮住一半」（2026-09-15 用户反馈）。
function syncSwitchText(cb) {
  var row = cb.closest('.switch-row');
  var t = row && row.querySelector('.st');
  if (t) t.textContent = cb.checked ? '开' : '关';
}

function renderField(f, cfg) {
  if (f.type === 'detect') {
    return '<div class="settings-field wide"><label>' + f.label + '</label>'
      + '<div class="toolbar"><button class="btn btn-outline btn-sm" id="btnDetectBot" onclick="detectBot(false)">🔍 从微信自动获取</button>'
      + '<span id="detectMsg" class="field-hint">没填 wxid 时会自动跑一次</span></div></div>';
  }
  if (f.type === 'info') {
    return '<div class="info-line">' + f.text + '</div>';
  }
  var val = cfg[f.key] !== undefined ? cfg[f.key] : (f.def !== undefined ? f.def : '');
  if (f.key === 'group_reply_mode' && val === 'mention') val = 'all'; // 旧值兼容
  if (typeof val === 'boolean') val = val ? 'true' : 'false';
  if (Array.isArray(val)) val = val.join(', ');

  var badge = f.restart ? '<span class="restart-badge">需重启</span>' : '';
  var label = '<label>' + f.label + badge + '</label>';
  var id = ' id="cfg_' + f.key + '" data-key="' + f.key + '"';

  if (f.type === 'toggle') {
    var on = (val === true || val === 'true' || val === '1');
    return '<div class="settings-field wide"><label>' + f.label + badge + '</label>'
      + '<label class="switch-row"><span class="switch"><input type="checkbox"' + id
      + (on ? ' checked' : '')
      + ' onchange="syncSwitchText(this)">'
      + '<span class="sl"></span></span>'
      + '<span class="st">' + (on ? '开' : '关') + '</span></label></div>';
  }
  if (f.type === 'select') {
    var s = '<div class="settings-field">' + label + '<select' + id + '>';
    f.opts.forEach(function(o){ s += '<option value="' + o.v + '"' + (val == o.v ? ' selected' : '') + '>' + o.l + '</option>'; });
    return s + '</select></div>';
  }
  if (f.type === 'textarea') {
    return '<div class="settings-field wide">' + label + '<textarea' + id + ' rows="2" placeholder="' + esc(f.ph || '') + '">' + esc(val) + '</textarea></div>';
  }
  var t = f.type === 'number' ? 'number' : (f.type === 'password' ? 'password' : 'text');
  return '<div class="settings-field">' + label + '<input type="' + t + '"' + id + ' value="' + esc(val)
    + '" placeholder="' + esc(f.ph || '') + '"></div>';
}

function filterSettings() {
  var kw = (document.getElementById('cfgSearch').value || '').trim().toLowerCase();
  var form = document.getElementById('settingsForm');
  var total = 0, shown = 0;
  form.querySelectorAll('[data-tier]').forEach(function(block){
    var hit = !kw;
    block.querySelectorAll('.settings-field').forEach(function(fd){
      var match = !kw || fd.textContent.toLowerCase().indexOf(kw) >= 0;
      fd.style.display = match ? '' : 'none';
      if (match) { hit = true; shown++; }
    });
    block.querySelectorAll('.info-line').forEach(function(fd){
      var match = !kw || fd.textContent.toLowerCase().indexOf(kw) >= 0;
      fd.style.display = match ? '' : 'none';
      if (match) hit = true;
    });
    if (kw) {
      block.style.display = hit ? '' : 'none';
      if (block.tagName === 'DETAILS') block.open = hit;
    } else {
      block.style.display = '';
      if (block.tagName === 'DETAILS') block.open = false;
    }
  });
  CFG_GROUPS.forEach(function(g){ g.fields.forEach(function(f){ if (f.key) total++; }); });
  document.getElementById('cfgSearchCount').textContent = kw ? (shown + ' / ' + total + ' 项匹配') : '';
}

// ===== 保存配置 =====
var BOOL_KEYS = ['quote_reply_prefix', 'quote_reply_native', 'mention_as_text', 'log_skipped_messages'];

function saveConfig() {
  var fields = document.querySelectorAll('#settingsForm [id^="cfg_"]');
  var data = {};
  fields.forEach(function(el){
    var key = el.getAttribute('data-key') || el.id.replace('cfg_','');
    var val;
    if (el.type === 'checkbox') val = el.checked ? 'true' : 'false';
    else val = el.value.trim();
    if (el.type === 'number') val = Number(val) || 0;
    if (BOOL_KEYS.indexOf(key) >= 0) val = (val === 'true' || val === '1');
    if (key === 'bot_nicknames') val = val ? val.split(/[,，]\\s*/).filter(Boolean) : [];
    data[key] = val;
  });

  fetch('/api/config', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(data),
  }).then(function(r){return r.json()}).then(function(res){
    if (res.ok) {
      showMsg('saveMsg', '✅ 已保存，重启桥接后生效');
      toast('✅ 配置已保存', 'success');
    } else {
      showMsg('saveMsg', '❌ 保存失败');
    }
  }).catch(function(e){
    showMsg('saveMsg', '❌ 保存失败: ' + e.message);
  });
}

// ===== 成员与权限（2026-10-07 重设计：子页签 + 统一保存） =====
//
// 页面结构：三个子页签（群聊 / 私聊 / 管理员）+ 底部统一保存条。
// 保存语义只有一条规则：「回复」开关立即生效（桥接侧），其余全部
// 走右下「💾 保存」写入 AstrBot 配置，再「🔄 重启 AstrBot」生效。
var peopleData = null;
var peopleDirty = false;          // 有未保存修改（刷新名单前要确认）
var currentArBlacklist = [];      // 插话黑名单（服务端最近一次值）
var arOverrides = {};             // gid → 「插嘴」勾选（仅记录用户改动过的行）

// 当前私聊白名单（uid / UMO 混合）缓存：搜索、「只看已勾」触发局部重渲染
// 时用它，避免把「面板拉到的旧值」当成「用户正在编辑的新值」丢掉勾选。
var currentWhitelist = [];
var humanizerStyleOverrides = {};   // umo → style（当前服务端值）
var humanizerStyleDirty = false;    // 用户是否改过会话风格

// ---- 子页签 ----
function switchMembersTab(name) {
  document.querySelectorAll('#page-members .seg').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-sub') === name);
  });
  document.querySelectorAll('#page-members .members-sub').forEach(function(p){
    p.classList.toggle('active', p.id === 'sub-' + name);
  });
  try { history.replaceState(null, '', '#members/' + name); } catch (e) {}
}

// ---- 数据加载 ----
function switchTabMembers() {
  document.getElementById('membersBadge').textContent = '加载中...';
  // 先用缓存立即渲染；再静默补拉一遍 WeFlow 联系人（bot 新进的群/新好友），
  // 拉到新东西且用户没在编辑时自动重渲染一次。
  // ⚠️ 不能先等 refresh 再渲染：WeFlow 没起时 refresh 要等 10s 超时，
  // 整个页签会白屏卡住。
  fetch('/api/people').then(function(r){return r.json()})
    .then(loadPeopleData)
    .catch(function(e){
      document.getElementById('membersBadge').textContent = '加载失败: ' + e.message;
    });
  fetch('/api/refresh-people', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
    .then(function(r){return r.json()})
    .then(function(res){
      if (res && res.ok && res.new > 0 && !peopleDirty) {
        fetch('/api/people').then(function(r){return r.json()}).then(loadPeopleData);
      }
    })
    .catch(function(){ /* WeFlow 不可用时静默忽略，缓存照常显示 */ });
}

function loadPeopleData(d) {
  peopleData = d;
  peopleDirty = false;
  arOverrides = {};
  updateDirtyHint();
  document.getElementById('membersBadge').textContent = d.astrbot_available
    ? (d.persons.length + ' 人 · ' + d.groups.length + ' 群 · AstrBot 已连接')
    : '⚠️ 未找到 AstrBot 配置: ' + (d.astrbot_error || d.astrbot_path);
  currentArBlacklist = (d.ar_blacklist || []).map(function(x){return String(x).trim()});
  var h = d.humanizer || {};
  humanizerStyleOverrides = {};
  humanizerStyleDirty = false;
  Object.keys(h.overrides || {}).forEach(function(k){
    humanizerStyleOverrides[String(k)] = String(h.overrides[k]);
  });
  var gs = document.getElementById('hsGlobalStyle');
  if (gs) gs.value = h.global_style || '-';
  renderHumanizerStyles();
  renderPeople();
  renderGroups(true);
  fillAstrbotForm(d);
}

// 手动刷新名单：POST /api/refresh-people（后端拉 WeFlow contacts，
// 群成员名册在后台线程补）→ 重新读 /api/people → 整页重渲染。
function refreshPeople() {
  if (peopleDirty && !confirm('有未保存的修改，刷新会丢弃它们。继续？')) return;
  var btn = document.getElementById('btnRefreshPeople');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 刷新中…'; }
  fetch('/api/refresh-people', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'})
    .then(function(r){return r.json()})
    .catch(function(e){ return {ok:false, error:String(e)}; })
    .then(function(res){
      return fetch('/api/people').then(function(r){return r.json()}).then(function(d){
        loadPeopleData(d);
        if (btn) { btn.disabled = false; btn.textContent = '🔄 刷新名单'; }
        if (res && res.error) toast('⚠️ WeFlow 联系人拉取失败：' + res.error + '（已按缓存显示）', 'error');
        else if (res && res.new > 0) toast('✅ 已刷新：新增 ' + res.new + ' 条，现在共 ' + d.groups.length + ' 个群', 'success');
        else toast('✅ 名单已是最新（' + d.groups.length + ' 个群）', 'info');
      });
    })
    .catch(function(e){
      if (btn) { btn.disabled = false; btn.textContent = '🔄 刷新名单'; }
      toast('❌ 刷新失败: ' + e.message, 'error');
    });
}

// ---- 未保存提示 ----
function markDirty() {
  peopleDirty = true;
  updateDirtyHint();
}

function updateDirtyHint() {
  var dot = document.getElementById('dirtyDot');
  if (dot) dot.classList.toggle('show', !!peopleDirty);
}

function copyText(text) {
  if (navigator.clipboard) { navigator.clipboard.writeText(text); }
  else {
    var ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
  }
  toast('已复制: ' + text, 'info');
}

// ---- 管理员子页 ----
function renderPeople() {
  if (!peopleData) return;
  var kw = (document.getElementById('memberSearch').value || '').toLowerCase();
  var admins = peopleData.admins;
  var rows = '';
  peopleData.persons.forEach(function(p){
    if (kw && p.name.toLowerCase().indexOf(kw) < 0 && p.wxid.toLowerCase().indexOf(kw) < 0) return;
    var isAdmin = admins.indexOf(p.wxid) >= 0;
    rows += '<tr>'
      + '<td class="center"><input type="checkbox" data-wxid="' + esc(p.wxid) + '"' + (isAdmin ? ' checked' : '') + ' onchange="markDirty()"></td>'
      + '<td>' + esc(p.name) + (isAdmin ? '<span class="admin-badge">管理员</span>' : '') + '</td>'
      + '<td class="wxid">' + esc(p.wxid) + '</td>'
      + '<td class="uid">' + esc(p.uid) + '</td>'
      + '</tr>';
  });
  document.getElementById('peopleBody').innerHTML = rows || '<tr><td colspan="4" style="color:#7d838c">没有匹配的成员（名册随消息与「刷新名单」更新）</td></tr>';
  var cnt = document.getElementById('peopleCount');
  if (cnt) cnt.textContent = peopleData.persons.length;
}

function scanAdminChecks() {
  // 把当前表格里的勾选状态并回 peopleData.admins（被搜索过滤掉的成员不受影响）
  var checked = {};
  document.querySelectorAll('#peopleBody input[type=checkbox]').forEach(function(cb){
    checked[cb.getAttribute('data-wxid')] = cb.checked;
  });
  peopleData.persons.forEach(function(p){
    if (!(p.wxid in checked)) return;
    var i = peopleData.admins.indexOf(p.wxid);
    if (checked[p.wxid] && i < 0) peopleData.admins.push(p.wxid);
    if (!checked[p.wxid] && i >= 0) peopleData.admins.splice(i, 1);
  });
}

// ---- 群聊子页 ----
// 群总表：「回复」开关立即生效；「插嘴」写 arOverrides，随统一保存提交。
// reset=true  → 忽略 DOM 现状，按服务端黑名单渲染（刚加载数据时）；
// reset=false → 保住用户未保存的勾选（搜索筛选触发重渲染时）。
function renderGroups(reset) {
  if (!peopleData) return;
  if (reset) arOverrides = {};
  var kw = ((document.getElementById('groupSearch') || {}).value || '').toLowerCase().trim();
  var rows = '', shown = 0;
  peopleData.groups.forEach(function(g){
    if (kw && g.name.toLowerCase().indexOf(kw) < 0
        && String(g.gid).indexOf(kw) < 0
        && String(g.session).toLowerCase().indexOf(kw) < 0) return;
    shown++;
    var blOn = arOverrides.hasOwnProperty(String(g.gid))
      ? arOverrides[String(g.gid)] : !inArBlacklist(g);
    rows += '<tr>'
      + '<td class="center" title="关闭 = 完全不理这个群（@ 也不理），立即生效">'
      +   '<label class="switch"><input type="checkbox" data-mute="' + esc(g.session) + '"'
      +   (g.muted ? '' : ' checked') + ' onchange="toggleMute(this)"><span class="sl"></span></label></td>'
      + '<td class="center" title="允许它在没人 @ 时自己插嘴（点右下保存 + 重启 AstrBot 生效）">'
      +   '<input type="checkbox" data-bl="' + esc(g.gid) + '"' + (blOn ? ' checked' : '')
      +   ' onchange="onArChange(this)"></td>'
      + '<td class="gname">' + esc(g.name) + '</td>'
      + '<td><span class="copy-chip" data-v="' + esc(g.gid) + '" onclick="copyText(this.dataset.v)" title="点击复制群 ID">' + esc(g.gid) + ' 📋</span> '
      +   '<span class="copy-chip" data-v="' + esc(g.umo) + '" onclick="copyText(this.dataset.v)" title="点击复制完整 UMO">UMO 📋</span></td>'
      + '</tr>';
  });
  document.getElementById('groupsBody').innerHTML = rows
    || '<tr><td colspan="4" style="color:#7d838c">'
    + (kw ? '没有匹配的群' : '暂无已知群 —— bot 被拉进群后点上方「🔄 刷新名单」') + '</td></tr>';
  var cnt = document.getElementById('groupCount');
  if (cnt) cnt.textContent = kw ? (shown + ' / ' + peopleData.groups.length + ' 个群')
                                : (peopleData.groups.length + ' 个群');
}

function onArChange(cb) {
  arOverrides[cb.getAttribute('data-bl')] = cb.checked;
  markDirty();
}

function inArBlacklist(g) {
  var bl = currentArBlacklist || [];
  return bl.indexOf(String(g.gid)) >= 0 || bl.indexOf(g.umo) >= 0 || bl.indexOf(g.session) >= 0;
}

function toggleMute(cb) {
  var session = cb.getAttribute('data-mute');
  var muted = !cb.checked;
  fetch('/api/session-toggle', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({session: session, muted: muted}),
  }).then(function(r){return r.json()}).then(function(res){
    if (res.ok) {
      toast(muted ? '🔇 已关闭该群回复（立即生效）' : '🔊 已恢复该群回复（立即生效）', 'success');
      // 同步进 peopleData，避免搜索重渲染把开关弹回旧状态
      peopleData.groups.forEach(function(g){ if (g.session === session) g.muted = muted; });
    } else {
      toast('❌ 操作失败: ' + res.error, 'error');
      cb.checked = !muted;
    }
  }).catch(function(e){
    toast('❌ 操作失败: ' + e.message, 'error');
    cb.checked = !muted;
  });
}

// ---- 私聊白名单（好友 chip） ----
// 私聊与群白名单是**同一份** id_whitelist —— 保存时统一收集器会把
// 「好友勾选 + 群勾选」合并后整表覆盖（见 collectFriendWhitelist）。

// 白名单条目 → 私聊 UID（好友勾选状态用）。
//
// ⚠️ 存储层存的是**完整 UMO**（`wechat_bridge:FriendMessage:719415740`），
// 因为 AstrBot 私聊只认完整 UMO；裸数字在私聊里永远匹配不上。
// 但 chip 的 data-uid 是纯数字，两者直接比会**永远不相等** →
// 表现就是「设置好的白名单在面板上不见了、重进又得重勾」。
// 所以这里统一抽成 UID 再比。
function wlEntryToUid(x) {
  x = String(x == null ? '' : x).trim();
  if (!x) return '';
  var parts = x.split(':');
  // 形如 platform:MessageType:id → 取最后一段（仅私聊；群 ID 也走这里但不会被好友列表用）
  if (parts.length >= 3) {
    if (parts[1] === 'GroupMessage') return '';   // 群条目不算好友勾选
    return parts[parts.length - 1].trim();
  }
  return x;  // 裸 ID（历史数据 / 群 ID）
}

// 好友 UID 集合（由白名单算出），供勾选判断与保存使用
function wlUidSet(list) {
  var s = {};
  (list || []).forEach(function(x){ var u = wlEntryToUid(x); if (u) s[u] = 1; });
  return s;
}

function fwMatches(p) {
  var kw = (document.getElementById('fwSearch').value || '').toLowerCase().trim();
  if (!kw) return true;
  return p.name.toLowerCase().indexOf(kw) >= 0
      || String(p.wxid || '').toLowerCase().indexOf(kw) >= 0
      || String(p.uid || '').toLowerCase().indexOf(kw) >= 0;
}

function renderFriendList(chipId, values) {
  // 搜索 / 只看已勾 会触发**局部重渲染**，此时不能丢用户已勾的项 ——
  // 所以进函数先把「当前实际勾选」并入 values 快照。
  var live = [];
  document.querySelectorAll('#' + chipId + ' input[type=checkbox]:checked').forEach(function(cb){
    live.push(cb.getAttribute('data-uid'));
  });
  var merged = [];
  (values || []).forEach(function(x){
    x = String(x).trim();
    if (x && merged.indexOf(x) < 0) merged.push(x);
  });
  live.forEach(function(x){ if (merged.indexOf(x) < 0) merged.push(x); });
  currentWhitelist = merged;

  var onlySel = document.getElementById('fwOnlySel') && document.getElementById('fwOnlySel').checked;
  var host = document.getElementById(chipId);
  var selSet = wlUidSet(merged);
  var html = '', shown = 0, selCount = 0;
  peopleData.persons.forEach(function(p){
    if (!p.uid && p.uid !== 0) return;
    var hit = !!selSet[String(p.uid)];
    if (hit) selCount++;
    if (onlySel && !hit) return;
    if (!fwMatches(p)) return;
    shown++;
    html += '<label class="chip' + (hit ? ' on' : '') + '">'
      + '<input type="checkbox" data-uid="' + esc(p.uid) + '"' + (hit ? ' checked' : '')
      + ' onchange="this.parentNode.classList.toggle(\\'on\\', this.checked);markDirty()"><span class="dot"></span>'
      + esc(p.name) + '</label>';
  });
  if (!shown) {
    var kw = kw0();
    html = '<span style="color:#7d838c;font-size:12px">'
         + (kw ? '没有匹配「' + esc(kw) + '」的好友（换个关键词试试）'
               : '暂无已知好友 —— 让对方先给你发一条消息，再点「🔄 刷新名单」')
         + '</span>';
  }
  host.innerHTML = html;
  var cnt = document.getElementById('fwCount');
  if (cnt) cnt.textContent = '已勾 ' + selCount + ' / 共 ' + peopleData.persons.length + ' 人' + (onlySel ? '（只看已勾）' : '');
}

function kw0(){
  var e = document.getElementById('fwSearch');
  return e ? e.value.trim() : '';
}

// 收集私聊白名单：好友勾选 + （群视图里勾的、不是已知好友 UID 的条目也要保住）
//
// ⚠️ 关键：搜索框会把不匹配的好友**从 DOM 里移除**。若只看 DOM 里的 checked，
// 被筛掉的那些已勾好友就会在保存时静默丢失。所以以 currentWhitelist（缓存）
// 为基准，再用 DOM 里的勾选状态**同步**：当前可见的按可见的来，不可见的保留缓存。
function collectFriendWhitelist() {
  // 以缓存为基准，但**剔除**好友视图管辖的条目（裸 UID 与私聊 UMO），
  // 好友勾选一律重新生成 —— 否则会同时留下旧 UMO 和新勾的裸 ID 两份重复项。
  var base = (currentWhitelist || []).filter(function(x){
    var s = String(x).trim();
    if (!s) return false;
    if (s.indexOf(':') >= 0) return s.split(':')[1] === 'GroupMessage';  // 只留群 UMO
    // 裸 ID：已知群保留；其余视为好友旧条目，丢弃后重建
    return peopleData.groups.some(function(g){ return String(g.gid) === s; });
  });
  var out = base.slice();
  var seen = wlUidSet(out);
  function push(v) { v = String(v == null ? '' : v).trim(); if (v && !seen[v]) { seen[v] = 1; out.push(v); } }

  // 1) 当前可见 chip：以 DOM 勾选为准（含取消勾选）
  document.querySelectorAll('#fw_friends input[type=checkbox]').forEach(function(cb){
    var uid = String(cb.getAttribute('data-uid') || '').trim();
    if (!uid) return;
    if (cb.checked) push(uid);
    else {  // 取消勾选：把该好友的旧条目（裸 ID 或 UMO）都移除
      for (var i = out.length - 1; i >= 0; i--) {
        if (wlEntryToUid(out[i]) === uid && String(out[i]).indexOf(':') >= 0) out.splice(i, 1);
      }
    }
  });
  // 2) 缓存里已勾但当前不可见（被搜索框筛掉）的好友：保留
  (currentWhitelist || []).forEach(function(x){
    var u = wlEntryToUid(x);
    if (!u) return;
    var isGroup = peopleData.groups.some(function(g){ return String(g.gid) === u; });
    if (!isGroup) push(u);
  });
  return out;
}

// 私聊白名单开关
function syncWlEnable(el) {
  markDirty();
}

// ---- 会话检索风格 ----
function styleOptionHtml(style, current) {
  var selected = style === current ? ' selected' : '';
  return '<option value="' + esc(style) + '"' + selected + '>' + esc(style) + '</option>';
}

function renderHumanizerStyles() {
  if (!peopleData) return;
  var h = peopleData.humanizer || {};
  var styles = h.styles || [];
  var kw = ((document.getElementById('hsSearch') || {}).value || '').toLowerCase().trim();
  var onlyCustom = document.getElementById('hsOnlyCustom') && document.getElementById('hsOnlyCustom').checked;
  var rows = '', custom = 0;
  (h.sessions || []).forEach(function(item){
    var cur = humanizerStyleOverrides.hasOwnProperty(item.umo)
      ? humanizerStyleOverrides[item.umo] : '__global__';
    if (cur !== '__global__') custom++;
    if (onlyCustom && cur === '__global__') return;
    if (kw && item.name.toLowerCase().indexOf(kw) < 0
        && item.umo.toLowerCase().indexOf(kw) < 0) return;
    var opts = '<option value="__global__"' + (cur === '__global__' ? ' selected' : '') + '>跟随全局（' + esc(h.global_style || '-') + '）</option>';
    styles.forEach(function(style){ opts += styleOptionHtml(style, cur); });
    rows += '<tr>'
      + '<td><span class="set-note">' + (item.kind === 'group' ? '群' : '私聊') + '</span> ' + esc(item.name) + '</td>'
      + '<td><select data-umo="' + esc(item.umo) + '" onchange="onHumanizerStyleChange(this)" style="width:100%">' + opts + '</select></td>'
      + '<td><span class="copy-chip" data-v="' + esc(item.umo) + '" onclick="copyText(this.dataset.v)" title="点击复制完整 UMO">' + esc(item.umo) + ' 📋</span></td>'
      + '</tr>';
  });
  document.getElementById('hsBody').innerHTML = rows
    || '<tr><td colspan="3" style="color:#7d838c">' + (kw ? '没有匹配的会话' : '暂无可用风格或会话') + '</td></tr>';
  var cnt = document.getElementById('hsCount');
  if (cnt) cnt.textContent = (h.available ? (custom + ' 个自定义 / ' + (h.sessions || []).length + ' 个会话') : '未找到人类风格插件配置');
}

function onHumanizerStyleChange(sel) {
  var umo = sel.getAttribute('data-umo');
  var val = sel.value;
  if (val === '__global__') delete humanizerStyleOverrides[umo];
  else humanizerStyleOverrides[umo] = val;
  humanizerStyleDirty = true;
  markDirty();
  var cnt = document.getElementById('hsCount');
  if (cnt) cnt.textContent = Object.keys(humanizerStyleOverrides).length + ' 个自定义 / ' + ((peopleData.humanizer || {}).sessions || []).length + ' 个会话';
}

// ---- 统一保存 ----
// 一次写入：管理员（/api/admins）+ 私聊/群白名单 + 随机插话（/api/astrbot）。
// 「插嘴」列取 arOverrides（用户改过的行）∪ 服务端黑名单（没改过的行），
// 保证被搜索筛掉的群不会被静默改状态。
function saveAll() {
  if (!peopleData) { toast('数据还没加载完，稍等一下再保存', 'error'); return; }
  scanAdminChecks();
  var bl = [];
  peopleData.groups.forEach(function(g){
    var on = arOverrides.hasOwnProperty(String(g.gid))
      ? arOverrides[String(g.gid)] : !inArBlacklist(g);
    if (!on) bl.push(String(g.gid));
  });
  var body = {
    id_whitelist_enable: document.getElementById('fw_enable').value === '1',
    // ⚠️ 私聊与群共用同一份 id_whitelist：统一收集器会合并两边勾选，
    // 并把好友规整成完整 UMO（裸 UID 在私聊里永远匹配不上）。
    id_whitelist: collectFriendWhitelist(),
    ar_enable: document.getElementById('ab_ar_enable').value === '1',
    ar_possibility: parseFloat(document.getElementById('ab_ar_poss').value),
    ar_blacklist: bl,
  };
  // Do not let an unavailable/misconfigured humanizer block unrelated saves.
  // Only submit this field when the user changed it. Unchanged stale data must
  // not block unrelated settings when a style profile was deleted externally.
  var savedHumanizerStyles = humanizerStyleDirty;
  if (savedHumanizerStyles) {
    body.humanizer_style_overrides = humanizerStyleOverrides;
  }
  var btn = document.getElementById('abSaveBtn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 保存中…'; }
  Promise.all([
    fetch('/api/admins', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({wxids: peopleData.admins.slice()})}).then(function(r){return r.json()}),
    fetch('/api/astrbot', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)}).then(function(r){return r.json()}),
  ]).then(function(res){
    if (btn) { btn.disabled = false; btn.textContent = '💾 保存'; }
    var a = res[0] || {}, c = res[1] || {};
    if (a.ok && c.ok) {
      peopleDirty = false;
      arOverrides = {};
      currentArBlacklist = bl.slice();
      updateDirtyHint();
      if (a.admins) { peopleData.admins = a.admins; renderPeople(); }
      humanizerStyleDirty = false;
      if (savedHumanizerStyles && peopleData.humanizer) {
        peopleData.humanizer.overrides = JSON.parse(JSON.stringify(humanizerStyleOverrides));
        renderHumanizerStyles();
      }
      showMsg('abMsg', '✅ 已保存');
      if (a.synced === false) {
        toast('⚠️ 已保存，但管理员写入 AstrBot 失败：' + (a.message || '?'), 'error');
      } else if (savedHumanizerStyles) {
        toast('✅ 已保存 —— 会话风格立即生效，其余项重启 AstrBot 后生效', 'success');
      } else {
        toast('✅ 已保存 —— 重启 AstrBot 后生效', 'success');
      }
      showRestartNeeded();
    } else {
      var errs = [a.error, c.error].filter(Boolean).join('；');
      showMsg('abMsg', '❌ 保存失败');
      toast('❌ 保存失败: ' + (errs || '未知错误'), 'error');
    }
  }).catch(function(e){
    if (btn) { btn.disabled = false; btn.textContent = '💾 保存'; }
    showMsg('abMsg', '❌ 保存失败');
    toast('❌ 保存失败: ' + e.message, 'error');
  });
}

// ---- 重启 AstrBot ----
// 后端后台线程干活，这里轮询状态显示进度。
// 重启期间桥接会自动断开再重连 AstrBot（ob_client.py 有重连循环），无需干预。
var _restartTimer = null;

function restartAstrbot() {
  var btn = document.getElementById('abRestartBtn');
  if (btn && btn.disabled) return;
  if (!confirm('确定重启 AstrBot 吗？\\n\\n约需 10-25 秒，期间机器人暂时不回复消息。')) return;
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 重启中…'; }
  pollRestartState();

  fetch('/api/astrbot-restart', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: '{}',
  }).then(function(r){return r.json()}).then(function(res){
    if (!res.ok) {
      toast('❌ 重启失败: ' + res.error, 'error');
      endRestartPoller();
    } else {
      toast('🔄 已开始重启 AstrBot，约 10-25 秒', 'info');
      if (!_restartTimer) _restartTimer = setInterval(pollRestartState, 1200);
    }
  }).catch(function(e){
    toast('❌ 请求失败: ' + e, 'error');
    endRestartPoller();
  });
}

function pollRestartState() {
  fetch('/api/astrbot-status').then(function(r){return r.json()}).then(function(s){
    var el = document.getElementById('abRestartState');
    var btn = document.getElementById('abRestartBtn');
    if (el) {
      el.textContent = (s.message || '') + (s.listening ? '（端口 ' + s.port + ' 在线）' : '');
      el.style.color = s.phase === 'failed' ? '#b5524a'
                     : s.phase === 'done' ? '#5f8f5f' : '#464b52';
    }
    if (s.phase === 'done' || s.phase === 'failed') {
      if (btn) { btn.disabled = false; btn.textContent = '🔄 重启 AstrBot'; }
      if (s.phase === 'done') {
        toast('✅ AstrBot 已重启完成', 'success');
        hideRestartNeeded();
      } else {
        toast('❌ 重启失败：' + (s.message || '看 astrbot_run.log'), 'error');
      }
      endRestartPoller();
    } else if (btn && s.running) {
      btn.disabled = true;
      btn.textContent = '⏳ 重启中…';
    }
  }).catch(function(){ /* 重启中面板自身不受影响，忽略瞬时错误 */ });
}

function endRestartPoller() {
  if (_restartTimer) { clearInterval(_restartTimer); _restartTimer = null; }
}

function showRestartNeeded() {
  var el = document.getElementById('wlRestartWarn');
  if (el) el.classList.add('show');
  var btn = document.getElementById('abRestartBtn');
  if (btn) btn.classList.add('need-restart');
}

function hideRestartNeeded() {
  var el = document.getElementById('wlRestartWarn');
  if (el) el.classList.remove('show');
  var btn = document.getElementById('abRestartBtn');
  if (btn) btn.classList.remove('need-restart');
}

// ---- 表单回填（白名单 / 随机插话） ----
function fillAstrbotForm(d) {
  document.getElementById('fw_enable').value = d.id_whitelist_enable ? '1' : '0';
  // 用服务端值重置缓存（重新拉取时旧编辑作废），再渲染好友 chip
  currentWhitelist = (d.id_whitelist || []).map(function(x){ return String(x).trim(); });
  renderFriendList('fw_friends', currentWhitelist);
  document.getElementById('ab_ar_enable').value = d.ar_enable ? '1' : '0';
  document.getElementById('ab_ar_poss').value = d.ar_possibility;
}

// ===== 自动获取机器人身份 =====
var detectBusy = false;

function detectBot(auto) {
  if (detectBusy) return;
  var msgEl = document.getElementById('detectMsg');
  var btn = document.getElementById('btnDetectBot');
  if (!msgEl) return;
  detectBusy = true;
  if (btn) btn.disabled = true;
  msgEl.textContent = '🔍 正在从微信读取…';

  fetch('/api/detect-bot').then(function(r){return r.json()}).then(function(d){
    detectBusy = false;
    if (btn) btn.disabled = false;
    if (!d.ok) {
      msgEl.textContent = '❌ ' + (d.error || '获取失败，请确认 WeFlow 已登录');
      return;
    }
    var wxEl = document.getElementById('cfg_bot_wxid');
    var nnEl = document.getElementById('cfg_bot_nicknames');
    var notes = [];
    if (d.wxid) {
      var cur = wxEl ? wxEl.value.trim() : '';
      if (!cur) { wxEl.value = d.wxid; notes.push('wxid 已填入 ' + d.wxid); }
      else if (cur !== d.wxid) {
        notes.push('检测到 ' + d.wxid + '（当前填的是 ' + cur + '）');
        if (!auto) { wxEl.value = d.wxid; notes.push('已替换为检测值'); }
      }
    }
    if (d.nicknames && d.nicknames.length) {
      var curNn = nnEl ? nnEl.value.trim() : '';
      if (!curNn) { nnEl.value = d.nicknames.join(', '); notes.push('昵称已填入 ' + d.nicknames.join('、')); }
      else if (!auto) { nnEl.value = d.nicknames.join(', '); notes.push('昵称已替换为 ' + d.nicknames.join('、')); }
    }
    msgEl.textContent = notes.length ? ('✅ ' + notes.join('；') + '。记得点保存') : '✅ 没找到更合适的值';
  }).catch(function(e){
    detectBusy = false;
    if (btn) btn.disabled = false;
    msgEl.textContent = '❌ 请求失败: ' + e.message;
  });
}

// ===== 初始化 =====
// 支持 #settings / #members / #members/friends 深链：刷新后停留在原页签与子页签
var startHash = (location.hash || '').replace('#', '');
var startTab = startHash.split('/')[0];
var startSub = startHash.split('/')[1];
switchTab(['dashboard', 'settings', 'members'].indexOf(startTab) >= 0 ? startTab : 'dashboard');
if (['groups', 'friends', 'admins'].indexOf(startSub) >= 0) switchMembersTab(startSub);
refreshDashboard();
setInterval(refreshDashboard, 3000);
loadExitReason();
// 退出归因变化很慢（只有重启才会变），低频刷新即可
setInterval(loadExitReason, 60000);
</script>
</body>
</html>"""


class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            ob_connected = state._ob_ws is not None and state._ob_ws_ready.is_set()
            weflow_connected = state.bridge_instance is not None and state.bridge_instance._sse_session is not None
            log_lines = []
            try:
                with open("bridge.log", encoding="utf-8", errors="replace") as f:
                    log_lines = f.read().splitlines()[-200:]
            except Exception:
                pass
            self.send_json({
                "running": state.running,
                "paused": state.paused.is_set(),
                "send_method": config.SEND_METHOD,
                "message_source": config.MESSAGE_SOURCE,
                "ob_url": config.ASTRBOT_OB_URL,
                "ob_connected": ob_connected,
                "weflow_connected": weflow_connected,
                "group_reply_mode": state.group_reply_mode,
                # 项目标识：面板标题上的 _RC 标记与版本号从后端下发，
                # 这样以后改 VERSION 文件就够了，不用再回来改 HTML。
                "project_name": getattr(config, "PROJECT_NAME", "Akasha-WeChat"),
                "project_version": getattr(config, "PROJECT_VERSION", ""),
                "log": "\n".join(log_lines),
            })
        elif self.path == "/api/config":
            try:
                with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.send_json(cfg)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif self.path == "/api/detect-bot":
            self.send_json(self._detect_bot())
        elif self.path == "/api/people":
            import people
            try:
                self.send_json(people.read_overview())
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif self.path == "/api/exit-reason":
            # 退出归因：面板上直接看「上次是怎么退的、这次是否正常」
            import exit_reason as _er
            try:
                self.send_json(_er.summary())
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif self.path == "/api/sync-umo-names":
            # 手动触发：给 AstrBot 里"没名字/名字是 wxid"的会话补可读名
            # （面板「自定义规则」里那一列显示的就是它）
            import people
            try:
                n, msg = people.sync_umo_aliases()
                self.send_json({"ok": True, "fixed": n, "message": msg})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif self.path == "/api/astrbot-status":
            # 供前端轮询重启进度（GET）
            import astrbot_ctl
            try:
                self.send_json(astrbot_ctl.status())
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(PAGE.encode("utf-8"))

    def do_POST(self):
        if self.path == "/start":
            from main import _start_bridge
            _start_bridge()
            self.send_json({"ok": True})
        elif self.path == "/stop":
            from main import _stop_bridge
            _stop_bridge()
            self.send_json({"ok": True})
        elif self.path == "/pause":
            state.paused.set()
            log.info("[Web] 已暂停")
            self.send_json({"ok": True})
        elif self.path == "/resume":
            state.paused.clear()
            log.info("[Web] 已恢复")
            self.send_json({"ok": True})
        elif self.path == "/mode":
            mode_order = ["all", "batch"]
            idx = mode_order.index(state.group_reply_mode) if state.group_reply_mode in mode_order else -1
            new_mode = mode_order[(idx + 1) % len(mode_order)]
            state.group_reply_mode = new_mode
            try:
                # ⚠️ 以前是「读 → 就地改写 → 直接 dump 到原文件」。面板被连点
                # （或两个标签页同时切）时两次保存并发，后一次写入落在已写一半的
                # 文件上，就产生 `Extra data: line 35 column 2`。
                # 实测发生过，现场留下 config.json.bak_broken_201419。
                # 现在统一走原子写。
                cfg = _read_config()
                cfg["group_reply_mode"] = new_mode
                _atomic_write_json(config.CONFIG_FILE, cfg)
                log.info(f"[Web] 群聊模式已切换为: {new_mode}")
            except Exception as e:
                log.error(f"[Web] 保存配置失败: {e}")
            self.send_json({"ok": True, "group_reply_mode": new_mode})
        elif self.path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                new_cfg = json.loads(body)

                # 读取当前配置，仅覆盖前端传来的字段
                current = _read_config()
                current.update(new_cfg)
                # 保留 _comment 字段
                if "_comment" not in current:
                    current["_comment"] = "微信 ↔ AstrBot 桥接 - OneBot v11 版配置"

                _atomic_write_json(config.CONFIG_FILE, current)

                log.info(f"[Web] 配置已保存")
                # 运行时同步 group_reply_mode（旧值 mention 归一化为 all）
                if "group_reply_mode" in new_cfg:
                    state.group_reply_mode = ("all" if new_cfg["group_reply_mode"] == "mention"
                                              else new_cfg["group_reply_mode"])

                self.send_json({"ok": True})
            except Exception as e:
                log.error(f"[Web] 保存配置异常: {e}")
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif self.path == "/api/admins":
            import people
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                wxids = [str(w).strip() for w in (body.get("wxids") or []) if str(w).strip()]
                synced, msg = people.sync_admins_to_astrbot(wxids)
                self.send_json({"ok": True, "synced": synced, "message": msg,
                                "admins": people.load_admins()})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif self.path == "/api/astrbot":
            import people
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                ok, msg = people.update_astrbot_settings(body)
                self.send_json({"ok": ok, "error": "" if ok else msg})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif self.path == "/api/session-toggle":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                session = str(body.get("session", "")).strip()
                muted = bool(body.get("muted"))
                if not session:
                    self.send_json({"ok": False, "error": "缺少 session"}, 400)
                    return
                state.set_session_muted(session, muted)
                log.info(f"[Web] 会话 {session} 回复已{'关闭' if muted else '开启'}")
                self.send_json({"ok": True, "session": session, "muted": muted})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif self.path == "/api/refresh-people":
            # 立即从 WeFlow 补拉联系人/群缓存。
            # 场景：把 bot 拉进新群后，面板「群里管什么」表要马上能看到它，
            # 而不是等桥接重启或 10 分钟定时刷新（2026-10-07 用户报障）。
            try:
                import bridge_core
                n, total, err = bridge_core.refresh_contact_names()
                # 群成员名册慢（每群一个请求），放后台线程补，不阻塞面板响应
                inst = getattr(state, "bridge_instance", None)
                if inst is not None:
                    threading.Thread(target=inst._prefetch_group_rosters,
                                     daemon=True).start()
                self.send_json({
                    "ok": not err,
                    "new": n,
                    "total_chats": total,
                    "groups": len(state.known_groups()),
                    "persons": len(state.all_persons()),
                    "error": err,
                })
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif self.path == "/api/astrbot-restart":
            # 重启 AstrBot（白名单/人设等只在启动时读一次，改完必须重启）。
            # 耗时约 10-25 秒 → 放后台线程跑，立刻返回，前端轮询 /api/astrbot-status。
            import astrbot_ctl
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = {}
                if length:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except Exception:
                        body = {}
                if body.get("action") == "stop":
                    ok, msg = astrbot_ctl.stop()
                    self.send_json({"ok": ok, "message": msg})
                    return
                if astrbot_ctl.is_restarting():
                    self.send_json({"ok": False, "error": "已有一个重启在进行中"})
                    return
                astrbot_ctl.restart_async()
                log.info("[Web] 面板触发 AstrBot 重启")
                self.send_json({"ok": True, "message": "已开始重启，约 10-25 秒"})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_json({"ok": False}, 404)

    # ===== 自动探测机器人自己的 wxid / 昵称 =====
    def _detect_bot(self):
        """从 WeFlow 反查「我自己」是谁。

        两条路：
        ① 已在 /api/v1/messages 里出现 isSend=1 的消息 → senderUsername 就是本机微信的 wxid
           （这是最权威的来源，因为那条消息就是这台机器发出去的）
        ② 拿到 wxid 后去各群的 /api/v1/group-members 里找同名条目 → 拿到昵称
        """
        out = {"ok": False, "wxid": "", "nicknames": [], "source": "", "error": ""}
        try:
            import requests
        except Exception as e:
            out["error"] = f"缺少 requests: {e}"
            return out

        # WeFlow/Alpha 的鉴权、分页、参数名差异统一走 message_source。
        # 这里不直接用 fetch_contacts()：面板只需要群列表，逐页拉全量太重。
        from message_source import create_message_source
        source = create_message_source()
        if not source.base_url:
            out["error"] = "未配置消息源地址"
            return out

        def _get(path, **params):
            try:
                if source.name == "alpha":
                    # Alpha 需要 account_id + chatroomId（不是 WeFlow 的 talker）
                    params.setdefault("account_id", source.ensure_account())
                    if path == "/api/v1/group-members" and "talker" in params:
                        params["chatroomId"] = params.pop("talker")
                    r = requests.get(
                        f"{source.base_url}{path}", params=params,
                        headers={"Authorization": f"Bearer {config.ALPHA_ACCESS_TOKEN}"},
                        timeout=15)
                else:
                    params.setdefault("access_token", config.ACCESS_TOKEN)
                    r = requests.get(f"{source.base_url}{path}", params=params, timeout=15)
                if r.status_code != 200:
                    return None
                return r.json()
            except Exception:
                return None

        # 1) 群（会话）列表
        rooms = []
        try:
            d = _get("/api/v1/contacts") or {}
            rooms = [c.get("username", "") for c in (d.get("contacts") or d.get("items") or [])
                     if "@chatroom" in (c.get("username") or "")]
        except Exception:
            rooms = []
        if not rooms:
            # 退而求其次：用桥接自己已知的名册
            try:
                import state
                rooms = [v for v in getattr(state, "_ob_id_to_session", {}).values()
                         if "@chatroom" in str(v)]
            except Exception:
                rooms = []

        # 2) 先拿现有配置当种子去群成员里对（快：通常 1~2 次请求）
        cur = (config.BOT_WXID or "").strip()
        seeds = []
        if cur:
            seeds.append(cur)
            if "_" in cur:
                seeds.append(cur.rsplit("_", 1)[0])  # 去掉微信 4.x 可能带的 _xxxx 后缀

        wxid = ""
        names = []
        for room in rooms[:6]:
            try:
                d = _get("/api/v1/group-members", talker=room) or {}
            except Exception:
                continue
            members = d.get("members") or d.get("data") or []
            if not isinstance(members, list):
                members = []
            for m in members:
                mw = str(m.get("wxid") or m.get("username") or "")
                if not mw:
                    continue
                if not seeds:
                    continue
                hit = any(mw == s for s in seeds) or any(mw.startswith(s) for s in seeds)
                if hit:
                    wxid = mw
                    for k in ("displayName", "nickname", "groupNickname", "remark"):
                        v = str(m.get(k) or "").strip()
                        if v and v not in names:
                            names.append(v)
                    out["source"] = "group-members"
                    break
            if wxid:
                break

        # 3) 没找到（通常是因为还没填 wxid）→ 从「我发出的消息」里认领
        if not wxid:
            for room in rooms[:4]:
                try:
                    d = _get("/api/v1/messages", talker=room, limit=100)
                except Exception:
                    continue
                msgs = d if isinstance(d, list) else (
                    (d or {}).get("messages") or (d or {}).get("data") or [])
                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    # WeFlow: isSend/senderUsername；Alpha: direction/sender_id
                    is_own = (str(m.get("isSend")) in ("1", "true", "True")
                              or m.get("direction") in (2, "2"))
                    if is_own:
                        su = str(m.get("senderUsername")
                                 or m.get("sender_id") or "").strip()
                        if su.startswith("wxid_"):
                            wxid = su
                            out["source"] = "own-message"
                            break
                if wxid:
                    break

        if not wxid:
            out["error"] = "没识别到机器人自己（需要它在至少一个群里发过消息，或先手填 wxid）"
            return out

        # 4) 用确认过的 wxid 再扫一遍群成员，把各群昵称收集全
        if out["source"] != "group-members":
            for room in rooms[:6]:
                try:
                    d = _get("/api/v1/group-members", talker=room) or {}
                except Exception:
                    continue
                members = d.get("members") or d.get("data") or []
                for m in members or []:
                    if str(m.get("wxid") or "") == wxid:
                        for k in ("displayName", "nickname", "groupNickname", "remark"):
                            v = str(m.get(k) or "").strip()
                            if v and v not in names:
                                names.append(v)
                        break

        out.update(ok=True, wxid=wxid, nicknames=names)
        return out

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass
