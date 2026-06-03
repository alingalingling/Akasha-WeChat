"""
Web 控制面板模块。

提供可视化控制页面（http://127.0.0.1:WEB_PORT），
支持启停/暂停/恢复桥接，显示运行状态和日志，
以及在线编辑 config.json 配置。
"""

import json
import logging
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

import state
import config

log = logging.getLogger("ob11-bridge")


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Akasha 奈奈山</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><text y='28' font-size='28'>💎</text></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',sans-serif;background:linear-gradient(135deg,#fdf2f5,#fce4ec,#f8e8f0);height:100vh;color:#4a4a4a;display:flex;margin:0;overflow:hidden}

/* ===== 主容器 ===== */
.container{display:flex;width:100vw;height:100vh;background:rgba(255,255,255,0.75);backdrop-filter:blur(20px);overflow:hidden;border:none}

/* ===== 侧边栏 ===== */
.sidebar{width:120px;min-width:120px;background:linear-gradient(180deg,#fce4ec,#f8e8f0);display:flex;flex-direction:column;align-items:center;padding:24px 0;gap:4px;border-right:1px solid rgba(240,98,146,0.1);height:100vh}
.sidebar .logo{font-size:18px;font-weight:800;color:#d6336e;margin-bottom:24px;letter-spacing:3px;text-shadow:0 1px 3px rgba(214,51,110,0.15);font-family:'Quicksand','Segoe UI',sans-serif;writing-mode:horizontal-tb}
.sidebar .nav-item{width:100px;height:48px;border-radius:14px;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all .25s;color:#b06c7a;font-size:13px;font-weight:600;gap:6px;border:none;background:transparent;padding:0 12px}
.sidebar .nav-item .icon{font-size:20px;line-height:1}
.sidebar .nav-item:hover{background:rgba(240,98,146,0.08);color:#d4567a}
.sidebar .nav-item.active{background:linear-gradient(135deg,#f48fb1,#f06292);color:#fff;box-shadow:0 4px 12px rgba(240,98,146,0.25)}
.sidebar .nav-item.active:hover{color:#fff}

/* ===== 内容区 ===== */
.content{flex:1;padding:28px 32px;overflow-y:auto;display:flex;flex-direction:column;gap:16px;height:100vh}
.content::-webkit-scrollbar{width:4px}
.content::-webkit-scrollbar-thumb{background:#f0ced9;border-radius:4px}

.tab-page{display:none;flex-direction:column;gap:16px;height:100%}
.tab-page.active{display:flex}

/* ===== 标题栏 ===== */
@import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@600;700&family=Quicksand:wght@600;700&display=swap');
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px}
.header h1{font-size:26px;font-weight:700;display:flex;align-items:baseline;gap:10px}
.header h1 .en{font-family:'Quicksand','Segoe UI',sans-serif;background:linear-gradient(135deg,#d6336e,#f06292);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:1px}
.header h1 .cn{font-family:'Noto Serif SC','STSong','SimSun',serif;font-size:22px;font-weight:600;background:linear-gradient(135deg,#e8436e,#f06292);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:3px}
.header .badge{font-size:11px;color:#b06c7a;background:#fce4ec;padding:3px 10px;border-radius:20px;font-weight:500}

/* ===== 状态卡片 ===== */
.status-row{display:flex;gap:8px;flex-wrap:wrap}
.status-card{flex:1;min-width:90px;background:#fff;border-radius:14px;padding:12px 14px;text-align:center;box-shadow:0 1px 6px rgba(0,0,0,0.03);border:1px solid #f5e4e8}
.status-card .label{font-size:11px;color:#b06c7a;margin-bottom:4px}
.status-card .value{font-size:15px;font-weight:600}
.status-card .value.online{color:#4caf50}
.status-card .value.offline{color:#bdbdbd}
.status-card .value.busy{color:#ff9800}

/* ===== 按钮组 ===== */
.btn-row{display:flex;gap:8px;flex-wrap:wrap}
.btn{padding:10px 18px;border:none;border-radius:12px;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s;display:inline-flex;align-items:center;gap:6px}
.btn:disabled{opacity:0.35;cursor:not-allowed;filter:none!important}
.btn:active:not(:disabled){transform:scale(0.97)}
.btn-pink{background:linear-gradient(135deg,#f48fb1,#f06292);color:#fff;box-shadow:0 2px 8px rgba(240,98,146,0.2)}
.btn-pink:hover:not(:disabled){box-shadow:0 4px 14px rgba(240,98,146,0.3)}
.btn-green{background:linear-gradient(135deg,#81c784,#66bb6a);color:#fff;box-shadow:0 2px 8px rgba(102,187,106,0.2)}
.btn-green:hover:not(:disabled){box-shadow:0 4px 14px rgba(102,187,106,0.3)}
.btn-red{background:linear-gradient(135deg,#ef9a9a,#e57373);color:#fff;box-shadow:0 2px 8px rgba(229,115,115,0.2)}
.btn-red:hover:not(:disabled){box-shadow:0 4px 14px rgba(229,115,115,0.3)}
.btn-amber{background:linear-gradient(135deg,#ffcc80,#ffa726);color:#fff;box-shadow:0 2px 8px rgba(255,167,38,0.2)}
.btn-amber:hover:not(:disabled){box-shadow:0 4px 14px rgba(255,167,38,0.3)}
.btn-outline{background:#fff;color:#d4567a;border:1.5px solid #f0ced9}
.btn-outline:hover:not(:disabled){background:#fce4ec;border-color:#f06292}

/* ===== 模式行 ===== */
.mode-row{display:flex;align-items:center;gap:10px;font-size:13px;color:#7a5a62;flex-wrap:wrap}
.mode-row .mode-value{font-weight:600;color:#d4567a}

/* ===== 日志 ===== */
.log-box{flex:1;min-height:100px;background:#faf5f7;border:1px solid #f0e2e6;border-radius:14px;padding:12px;font-size:12px;font-family:'Cascadia Code','Fira Code',monospace;color:#6a4a52;overflow-y:auto;line-height:1.6;white-space:pre-wrap}
.log-box:empty::before{content:'等待连接...';color:#c0aab0}
.log-box::-webkit-scrollbar{width:4px}
.log-box::-webkit-scrollbar-thumb{background:#e0d0d4;border-radius:4px}

/* ===== 设置页面 ===== */
.settings-scroll{flex:1;overflow-y:auto;padding-right:4px}
.settings-scroll::-webkit-scrollbar{width:4px}
.settings-scroll::-webkit-scrollbar-thumb{background:#e0d0d4;border-radius:4px}
.settings-group{margin-bottom:18px}
.settings-group h3{font-size:13px;font-weight:600;color:#d4567a;margin-bottom:8px;padding-bottom:4px;border-bottom:1.5px solid #fce4ec}
.settings-row{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px}
.settings-field{flex:1;min-width:160px}
.settings-field label{display:block;font-size:11px;color:#b06c7a;margin-bottom:3px;font-weight:500}
.settings-field input,.settings-field select,.settings-field textarea{width:100%;padding:7px 10px;border:1.5px solid #f0e2e6;border-radius:10px;font-size:12px;outline:none;transition:border .2s;background:#fff;color:#4a4a4a;font-family:inherit}
.settings-field input:focus,.settings-field select:focus,.settings-field textarea:focus{border-color:#f06292;box-shadow:0 0 0 3px rgba(240,98,146,0.08)}
.settings-field textarea{resize:vertical;min-height:36px}
.settings-field select{cursor:pointer;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23b06c7a'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:28px}

/* ===== 保存按钮 ===== */
.save-bar{display:flex;justify-content:flex-end;align-items:center;gap:12px;padding-top:8px;border-top:1px solid #f0e2e6}
.save-bar .save-msg{font-size:12px;color:#66bb6a;opacity:0;transition:opacity .4s}
.save-bar .save-msg.show{opacity:1}

/* ===== Toast ===== */
.toast{position:fixed;top:24px;left:50%;transform:translateX(-50%);padding:10px 24px;border-radius:14px;font-size:13px;font-weight:500;z-index:999;opacity:0;transition:opacity .4s;pointer-events:none;box-shadow:0 4px 20px rgba(0,0,0,0.1)}
.toast.show{opacity:1}
.toast.success{background:#e8f5e9;color:#2e7d32;border:1px solid #c8e6c9}
.toast.error{background:#ffebee;color:#c62828;border:1px solid #ffcdd2}
.toast.info{background:#fce4ec;color:#ad1457;border:1px solid #f8bbd0}
</style>
</head>
<body>

<div class="toast" id="toast"></div>

<div class="container">

<!-- ===== 侧边栏 ===== -->
<div class="sidebar">
  <div class="logo">Akasha</div>
  <button class="nav-item active" data-tab="dashboard" onclick="switchTab('dashboard')">
    <span>控制面板</span>
  </button>
  <button class="nav-item" data-tab="settings" onclick="switchTab('settings')">
    <span>基础设置</span>
  </button>
</div>

<!-- ===== 内容区 ===== -->
<div class="content">

  <!-- ===== 面板页 ===== -->
  <div class="tab-page active" id="page-dashboard">
    <div class="header">
      <h1><span class="en">Akasha</span><span class="cn">奈奈山</span></h1>
      <div class="badge" id="statusText">加载中...</div>
    </div>

    <div class="status-row">
      <div class="status-card"><div class="label">桥接状态</div><div class="value" id="bridgeStatus">-</div></div>
      <div class="status-card"><div class="label">AstrBot</div><div class="value" id="obStatus">-</div></div>
      <div class="status-card"><div class="label">WeFlow</div><div class="value" id="weflowStatus">-</div></div>
      <div class="status-card"><div class="label">发送模式</div><div class="value" id="sendMethod" style="font-size:13px">-</div></div>
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
      <button class="btn btn-outline" id="btnToggleMode" style="padding:5px 14px;font-size:12px">切换</button>
    </div>

    <div class="log-box" id="log">等待连接...</div>
  </div>

  <!-- ===== 设置页 ===== -->
  <div class="tab-page" id="page-settings">
    <div class="header">
      <h1>配置编辑</h1>
      <div class="badge">config.json</div>
    </div>

    <div class="settings-scroll" id="settingsForm">
      <!-- 由 JS 动态渲染 -->
    </div>

    <div class="save-bar">
      <span class="save-msg" id="saveMsg">✅ 已保存</span>
      <button class="btn btn-pink" onclick="saveConfig()">💾 保存配置</button>
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

function showMsg(text) {
  var el = document.getElementById('saveMsg');
  el.textContent = text;
  el.className = 'save-msg show';
  setTimeout(function(){el.className='save-msg'}, 2500);
}

// ===== Tab 切换 =====
function switchTab(name) {
  document.querySelectorAll('.tab-page').forEach(function(p){p.classList.remove('active')});
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});
  document.querySelector('[data-tab="' + name + '"]').classList.add('active');
  if (name === 'settings') loadConfig();
}

// ===== 面板刷新 =====
var modeMap = {'mention':'仅@回复','all':'全部回复','batch':'批处理'};

function refreshDashboard() {
  fetch('/status').then(function(r){return r.json()}).then(function(s){
    var st = document.getElementById('bridgeStatus');
    if (!s.running) { st.textContent='未运行'; st.style.color='#bdbdbd';
    } else if (s.paused) { st.textContent='已暂停'; st.style.color='#ff9800';
    } else { st.textContent='运行中'; st.style.color='#4caf50'; }

    document.getElementById('statusText').textContent = s.running ? (s.paused ? '已暂停' : '运行中') : '未运行';
    document.getElementById('obStatus').textContent = s.ob_connected ? '已连接' : '未连接';
    document.getElementById('obStatus').style.color = s.ob_connected ? '#4caf50' : '#bdbdbd';
    document.getElementById('weflowStatus').textContent = s.weflow_connected ? '已连接' : '未连接';
    document.getElementById('weflowStatus').style.color = s.weflow_connected ? '#4caf50' : '#bdbdbd';
    document.getElementById('sendMethod').textContent = s.send_method;

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

// ===== 设置加载 =====
function loadConfig() {
  fetch('/api/config').then(function(r){return r.json()}).then(function(cfg){
    renderConfigForm(cfg);
  }).catch(function(e){
    document.getElementById('settingsForm').innerHTML = '<p style="color:#e57373;font-size:13px;">加载配置失败: ' + e.message + '</p>';
  });
}

function renderConfigForm(cfg) {
  var html = '';
  var groups = [
    {title:'WeFlow 连接', fields:[
      {key:'weflow_base_url', label:'WeFlow 地址', type:'text', ph:'http://127.0.0.1:5031'},
      {key:'access_token', label:'Access Token', type:'password', ph:'输入Token'},
      {key:'weflow_send_api', label:'发送 API 地址', type:'text', ph:'http://127.0.0.1:5031/api/v1/message'},
    ]},
    {title:'机器人', fields:[
      {key:'bot_nicknames', label:'机器人昵称（多个用逗号隔开）', type:'text', ph:'山山酱(^'},
      {key:'bot_wxid', label:'机器人 wxid', type:'text', ph:'wxid_xxx'},
      {key:'send_method', label:'发送方式', type:'select', opts:[{v:'uia',l:'UIA 自动化'},{v:'weflow_api',l:'WeFlow API'}]},
    ]},
    {title:'AstrBot 连接', fields:[
      {key:'astrbot_ob_url', label:'AstrBot OB 地址', type:'text', ph:'ws://127.0.0.1:11229/ws'},
      {key:'astrbot_attachments', label:'附件目录（AstrBot 存放图片的路径）', type:'text', ph:'C:\\astrbot\\attachments'},
    ]},
    {title:'桥接设置', fields:[
      {key:'buffer_seconds', label:'消息缓冲(秒)', type:'number', ph:'5'},
      {key:'group_reply_mode', label:'群聊回复模式', type:'select', opts:[{v:'mention',l:'仅@回复'},{v:'all',l:'全部回复'},{v:'batch',l:'批处理'}]},
      {key:'web_port', label:'Web 面板端口', type:'number', ph:'8766'},
    ]},
    {title:'图片描述', fields:[
      {key:'image_caption_provider', label:'描述服务', type:'select', opts:[{v:'ollama',l:'Ollama 本地'},{v:'openai',l:'OpenAI 兼容'}]},
      {key:'image_caption_model', label:'模型名', type:'text', ph:'kimi-k2.6 / llava:7b'},
      {key:'image_caption_api_key', label:'API Key', type:'password', ph:'sk-xxx (OpenAI模式时)'},
      {key:'image_caption_api_base', label:'API 地址', type:'text', ph:'https://api.moonshot.cn/v1'},
      {key:'image_caption_prompt', label:'描述提示词', type:'textarea', ph:'请用中文描述...'},
    ]},
    {title:'Ollama（使用本地模式时）', fields:[
      {key:'ollama_base_url', label:'Ollama 地址', type:'text', ph:'http://127.0.0.1:61000'},
      {key:'ollama_timeout', label:'超时(秒)', type:'number', ph:'60'},
    ]},
  ];

  groups.forEach(function(g){
    html += '<div class="settings-group"><h3>' + g.title + '</h3><div class="settings-row">';
    g.fields.forEach(function(f){
      var val = cfg[f.key] !== undefined ? cfg[f.key] : '';
      if (Array.isArray(val)) val = val.join(', ');
      html += '<div class="settings-field"><label>' + f.label + '</label>';
      if (f.type === 'select') {
        html += '<select id="cfg_' + f.key + '">';
        f.opts.forEach(function(o){html += '<option value="' + o.v + '"' + (val==o.v?' selected':'') + '>' + o.l + '</option>'});
        html += '</select>';
      } else if (f.type === 'textarea') {
        html += '<textarea id="cfg_' + f.key + '" placeholder="' + (f.ph||'') + '" rows="2">' + val + '</textarea>';
      } else if (f.type === 'number') {
        html += '<input type="number" id="cfg_' + f.key + '" value="' + val + '" placeholder="' + (f.ph||'') + '">';
      } else {
        html += '<input type="' + f.type + '" id="cfg_' + f.key + '" value="' + val.replace(/"/g,'&quot;') + '" placeholder="' + (f.ph||'') + '">';
      }
      html += '</div>';
    });
    html += '</div></div>';
  });

  document.getElementById('settingsForm').innerHTML = html;
}

// ===== 保存配置 =====
function saveConfig() {
  // 从表单收集数据
  var fields = document.querySelectorAll('#settingsForm [id^="cfg_"]');
  var data = {};
  fields.forEach(function(el){
    var key = el.id.replace('cfg_','');
    var val = el.value.trim();
    if (el.type === 'number') val = Number(val) || 0;
    // bot_nicknames: 逗号分隔转数组
    if (key === 'bot_nicknames') val = val ? val.split(/[,，]\\s*/).filter(Boolean) : [];
    data[key] = val;
  });

  fetch('/api/config', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(data),
  }).then(function(r){return r.json()}).then(function(res){
    if (res.ok) {
      showMsg('✅ 已保存（部分更改需重启生效）');
    } else {
      showMsg('❌ 保存失败');
    }
  }).catch(function(e){
    showMsg('❌ 保存失败: ' + e.message);
  });
}

// ===== 初始化 =====
refreshDashboard();
setInterval(refreshDashboard, 3000);
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
                "ob_url": config.ASTRBOT_OB_URL,
                "ob_connected": ob_connected,
                "weflow_connected": weflow_connected,
                "group_reply_mode": state.group_reply_mode,
                "log": "\n".join(log_lines),
            })
        elif self.path == "/api/config":
            try:
                with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.send_json(cfg)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
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
            mode_order = ["mention", "all", "batch"]
            idx = mode_order.index(state.group_reply_mode) if state.group_reply_mode in mode_order else -1
            new_mode = mode_order[(idx + 1) % len(mode_order)]
            state.group_reply_mode = new_mode
            try:
                with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg["group_reply_mode"] = new_mode
                with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=4)
                    f.write("\n")
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
                with open(config.CONFIG_FILE, "r", encoding="utf-8") as f:
                    current = json.load(f)
                current.update(new_cfg)
                # 保留 _comment 字段
                if "_comment" not in current:
                    current["_comment"] = "微信 ↔ AstrBot 桥接 - OneBot v11 版配置"

                with open(config.CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(current, f, ensure_ascii=False, indent=4)
                    f.write("\n")

                log.info(f"[Web] 配置已保存")
                # 运行时同步 group_reply_mode
                if "group_reply_mode" in new_cfg:
                    state.group_reply_mode = new_cfg["group_reply_mode"]

                self.send_json({"ok": True})
            except Exception as e:
                log.error(f"[Web] 保存配置异常: {e}")
                self.send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_json({"ok": False}, 404)

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args):
        pass
