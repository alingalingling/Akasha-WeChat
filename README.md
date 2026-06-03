# Akasha-WeChat

让微信小号接入LLM大模型，通过 **OneBot v11 协议** 与模型通信。

```
微信 ←→ WeFlow ──SSE──→ bridge ←→ AstrBot (aiocqhttp)
                                   反向 WebSocket
                              ws://127.0.0.1:11229/ws
```

## 特性

-  **消息接收** — WeFlow SSE 实时推送，无轮询无风控
-  **AI 回复** — 通过 AstrBot 调用任何 LLM（DeepSeek、Kimi、Claude 等）
-  **图片识别** — 支持 ollama llava / Kimi 等模型描述图片内容
-  **三种群聊模式** — 仅@回复 / 全部回复 / 批处理，Web 页面一键切换
-  **Web 控制面板** — 粉白主题，启停控制、状态监控、日志查看
-  **在线配置编辑** — 直接在网页上修改 config.json，无需碰文件
-  **消息缓冲** — 多条消息合并后推送，减少 AI 调用次数
-  **自回复防护** — 多层去重，防止 AI 和自己的消息循环

## 前置条件

| 依赖 | 说明 |
|------|------|
| Windows 系统 | 需要桌面微信 |
| [WeFlow](https://weflow.top) | 已安装并登录微信，开启 API 服务（端口 5031） |
| Python 3.10+ | 运行桥接脚本 |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 已部署运行的 AstrBot 实例，启用 aiocqhttp 适配器 |

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动

```bash
python main.py
```

首次启动会自动从 `config.example.json` 创建 `config.json`，然后打开 Web 面板填配置：

**http://127.0.0.1:8766** → 点「基础设置」→ 填写配置 → 保存配置 → 重启生效

### 3. 配置 AstrBot

-  1.在 AstrBot的机器人 中添加 创建新机器人
-  2.消息平台类别选择onebotv11，aiocqhttp 适配器（WebUI 或 `cmd_config.json`）：
-  3.反向websocket端口写：11229
-  4.其余默认即可，点击保存并开启该机器人

> 如果 AstrBot 运行在 Docker 中，确保端口映射到宿主机：`-p 11229:11229`

重启 AstrBot。

### 4. 启动桥接

```bash
python main.py
```

Web 控制面板：**http://127.0.0.1:8766**
打开web控制面板

## 配置项说明

所有配置可在 Web 面板「基础设置」中在线编辑。

| 字段 | 说明 |
|------|------|
| `weflow_base_url` | WeFlow API 地址，默认 `http://127.0.0.1:5031` |
| `access_token` | WeFlow Access Token |
| `bot_nicknames` | 机器人微信昵称列表，群聊 @ 检测用 |
| `bot_wxid` | 机器人自己的 wxid（可选，防自回复） |
| `send_method` | 选`"uia"`（UIA 自动化）就好 |
| `buffer_seconds` | 消息缓冲秒数，多条消息合并后推送 |
| `group_reply_mode` | `"mention"`（仅@回复）/ `"all"`（全部回复）/ `"batch"`（批处理） |
| `astrbot_ob_url` | 反向WebSocket 地址 应填 ws/127.0.0.1:11229/ws |
| `image_caption_provider` | 图片描述服务：`"ollama"` 或 `"openai"` |
| `image_caption_model` | 视觉模型名，如 `llava:7b` / `kimi-k2.6` |

## 工作原理

1. **接收消息** — 连接 WeFlow SSE 推送，实时接收微信消息
2. **缓冲合并** — 多条消息缓冲 N 秒后合并推送
3. **图片处理** — 图片消息自动下载 → 视觉模型描述 → 注入文本
4. **OneBot 推送** — 转 OneBot v11 事件，通过 WebSocket 推给 AstrBot
5. **AI 处理** — AstrBot 经插件流水线调用 LLM 生成回复
6. **回复发送** — AstrBot 调用 `send_msg` API，bridge 通过 UIA 发回微信

## 文件结构

```
wechat-weflow-bridge/
├── main.py              # 入口（启动桥接 + Web 服务）
├── state.py             # 共享全局状态
├── config.py            # 配置加载
├── senders.py           # 消息发送器（UIA / WeFlow API）
├── ob_client.py         # OneBot WebSocket 客户端
├── ob_protocol.py       # OneBot 协议处理（API 接收 + 事件推送）
├── bridge_core.py       # 桥接核心（缓冲 + SSE + 图片描述）
├── web_panel.py         # Web 控制面板（粉白主题 + 在线配置编辑）
├── uia_sender.py        # Windows UI Automation 发送器
├── config.json          # 配置文件（已 gitignore，需自行创建）
├── config.example.json  # 配置示例
├── requirements.txt     # Python 依赖
├── start.bat            # Windows 快捷启动
├── LICENSE              # MIT 许可证
└── README.md
```

## Web 控制面板

访问 **http://127.0.0.1:8766**

- **控制面板** — 查看桥接/AstrBot/WeFlow 连接状态、启停控制、群聊模式切换、实时日志
- **基础设置** — 在线编辑所有配置项，保存即写入 `config.json`

## 许可证

MIT

