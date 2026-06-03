# WeChat WeFlow Bridge (OneBot v11)

微信 ↔ AstrBot 桥接，基于 WeFlow SSE 推送 + OneBot v11 协议。

## 架构

```
微信 ←→ WeFlow ──SSE──→ bridge ──WebSocket 客户端──→ AstrBot
                            ↑                              ↑
                     main.py 启动                    aiocqhttp 服务端
                     Web 面板 :8766                 (0.0.0.0:11229)
```

启动方式：`python main.py`

## 模块文件

| 文件 | 职责 |
|------|------|
| `state.py` | 共享全局变量（running、_ob_ws、sender_instance 等） |
| `config.py` | 加载 config.json，提供配置常量 |
| `senders.py` | BaseSender、WeFlowApiSender、UiaSender、create_sender() |
| `ob_client.py` | WebSocket 客户端线程，连接 AstrBot |
| `ob_protocol.py` | 处理 AstrBot API 请求、构造 OneBot 事件、推送事件 |
| `bridge_core.py` | WeFlowBridge 类：消息缓冲、SSE 监听、图片描述 |
| `web_panel.py` | Web 控制面板：HTML/CSS/JS + API 端点 |
| `main.py` | 入口：启停逻辑、桥接循环 |

## 数据流

1. **WeFlow SSE** → `bridge_core.listen_sse()` 收到微信消息
2. **缓冲合并** → `add_to_buffer()` → `BUFFER_SECONDS` 后触发
3. **图片消息** → 下载原图 → 视觉模型描述 → 注入文本到缓冲区
4. **OneBot 事件推送** → `process_sender()` → `push_event()` → WebSocket → AstrBot
5. **AstrBot 回复** → `send_msg` API → `ob_protocol._handle_ob_api()` → UIA 发回微信

## 配置

所有配置在 Web 面板（:8766）「基础设置」中在线编辑，保存后写入 config.json。

群聊三种模式：`mention`（仅@回复）/ `all`（全部回复）/ `batch`（批处理）
图片描述：支持 ollama 本地 / OpenAI 兼容 API（如 Kimi k2.6）
