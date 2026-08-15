"""
微信 ↔ AstrBot 桥接（OneBot v11 版）
=====================================
消息接收：WeFlow SSE 推送
AI 服务：AstrBot 通过 aiocqhttp (OneBot v11) 接入
消息发送：bridge 接收 AstrBot 的 API 调用 → WeFlow API / UIA

架构：
  WeFlow ──SSE──→ bridge.py ──WS 客户端──→ AstrBot (aiocqhttp 服务端)
                   ↑ 连接 ws://127.0.0.1:19777  ↑ 监听端口，等待客户端连入
                   发送 OneBot 事件             返回 API 响应
"""

# 共享状态：所有模块通过 import state 访问这些变量
import hashlib
import threading
from typing import Optional

# ============ 状态控制 ============

running = False
paused = threading.Event()
paused.clear()
run_lock = threading.Lock()
bridge_thread = None

# ============ OneBot WebSocket 客户端管理 ============

_ob_ws = None          # WebSocket 连接实例
_ob_ws_loop = None     # 事件循环
_ob_ws_ready = threading.Event()
_self_id_int = 0       # 启动时从 config 初始化


def _wxid_to_int(wxid: str) -> int:
    """将微信 wxid 映射为稳定的整数 ID。"""
    # 内置 hash() 带随机种子，每次重启结果不同，必须用稳定哈希
    return int(hashlib.md5(wxid.encode("utf-8")).hexdigest()[:12], 16) % (2**31)


# ============ 桥接实例 / 发送器 ============

bridge_instance = None
bridge_lock = threading.Lock()
sender_instance = None
_ob_id_to_contact: dict[int, str] = {}  # OneBot user_id/group_id → 微信联系名
ob_client_started = False

# 记录机器人最近发出的文本（内容 → 发送时间戳），供 bridge_core 做自回复去重
sent_recently: dict = {}

# 群聊回复模式（运行时可变，启动时从 config 初始化）
group_reply_mode = "mention"

# 图片描述配置（运行时可变，Web 面板保存后即时生效）
image_caption_provider = "ollama"
image_caption_model = "llava:7b"
image_caption_api_key = ""
image_caption_api_base = ""
image_caption_prompt = "请用中文简短描述这张图片的内容"
ollama_base_url = "http://127.0.0.1:61000"
ollama_timeout = 60
