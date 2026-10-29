"""
OneBot WebSocket 客户端模块。

维护到 AstrBot aiocqhttp 服务端的 WebSocket 长连接，
推送事件并从 AstrBot 接收 API 请求。
"""

import asyncio
import json
import logging
import threading

import websockets
from websockets.asyncio.server import ServerConnection, serve

import state
import config
from ob_protocol import _handle_ob_api

log = logging.getLogger("ob11-bridge")


def _run_ob_client():
    """后台线程：维护到 AstrBot 的 WebSocket 连接。"""
    _loop = asyncio.new_event_loop()
    state._ob_ws_loop = _loop
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ob_client_main())
    finally:
        try:
            _loop.close()
        except Exception:
            pass
        if state._ob_ws_loop is _loop:
            state._ob_ws_loop = None


async def _ob_client_main():
    """WebSocket 客户端主协程：连接 AstrBot，发送事件，接收 API 响应。"""
    while state.running:
        try:
            log.info(f"[OB11] 正在连接 AstrBot: {config.ASTRBOT_OB_URL}")
            async with websockets.connect(
                config.ASTRBOT_OB_URL,
                # 收帧上限：websockets ≥10 默认 1MB。AstrBot 侧发来的 send_group_msg
                # 带图片时是 base64 内联（arknights 公告卡片实测 1.5MB JPEG → ~2MB 帧），
                # 2026-10-03 超帧直接压断连接 → AstrBot 挂满 180s 超时 → 卡片丢失。
                # 抬到 16MB 兜住常规渲染卡片（AstrBot 侧 aiocqhttp 服务端同样不设限）。
                max_size=16 * 1024 * 1024,
                additional_headers={
                    "X-Self-ID": str(state._self_id_int),
                    "X-Client-Role": "Universal",
                    "User-Agent": "OneBot/11",
                }
            ) as ws:
                state._ob_ws = ws
                state._ob_ws_ready.set()
                log.info(f"[OB11] ✅ 已连接到 AstrBot")

                # 心跳保活：每 15 秒发 ping
                async def _keepalive():
                    while True:
                        await asyncio.sleep(15)
                        try:
                            await ws.ping()
                        except Exception:
                            break
                ka_task = asyncio.create_task(_keepalive())

                # ⚠️ 必须串行消费（单 worker），不能再用 create_task 并发处理。
                # AstrBot 的「分条回复」是按顺序 await 逐条 send 的，桥接这边若并发
                # 处理，多条发送会互相竞争 UIA 锁，最终落屏顺序会乱
                # （实测：整句被拆开后顺序颠倒，同一件事前面说过后面又说一遍）。
                # 队列保证「到达顺序 == 发送顺序」。响应延迟只会等于上一条发送耗时
                # （约 5~8s），远小于 AstrBot 的 API 超时（180s），安全。
                api_q: asyncio.Queue = asyncio.Queue()

                async def _api_worker():
                    while True:
                        item = await api_q.get()
                        t0 = asyncio.get_running_loop().time()
                        try:
                            await _handle_ob_api(item)
                        except Exception as e:
                            log.error(f"[OB11] 处理 API 异常: {e}")
                        finally:
                            api_q.task_done()
                            dt = asyncio.get_running_loop().time() - t0
                            if dt >= 30:
                                log.warning(
                                    f"[OB11] 单条 API 处理耗时 {dt:.1f}s"
                                    f"（{item.get('action')}）——若持续如此请检查 UIA"
                                )

                wk = asyncio.create_task(_api_worker())
                try:
                    # 持续接收 API 请求：这里只入队，慢活在 worker 里顺序做
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            await api_q.put(data)
                        except json.JSONDecodeError:
                            log.warning(f"[OB11] 收到无效 JSON")
                        except Exception as e:
                            log.error(f"[OB11] 接收异常: {e}")
                finally:
                    ka_task.cancel()
                    wk.cancel()

        except websockets.exceptions.ConnectionClosed:
            log.warning(f"[OB11] 连接断开，5 秒后重连")
        except (ConnectionRefusedError, OSError) as e:
            log.warning(f"[OB11] 无法连接 AstrBot ({e})，5 秒后重试")
        except Exception as e:
            log.error(f"[OB11] 连接异常: {e}")

        state._ob_ws = None
        if not state.running:
            break
        await asyncio.sleep(5)

    state._ob_ws = None
