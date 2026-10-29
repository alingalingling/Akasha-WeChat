"""
消息发送器模块：BaseSender 基类、WeFlow API 发送器、工厂函数。

导入 UiaSender 后通过 create_sender() 工厂方法选择发送模式。
"""

import logging
import os
import threading
import requests
from uia_sender import UiaSender

log = logging.getLogger("ob11-bridge")

from config import (WE_FLOW_SEND_API, ACCESS_TOKEN, SEND_METHOD,
                    ALPHA_BASE_URL, ALPHA_ACCESS_TOKEN, ALPHA_SEND_WAIT_SECONDS)


# ============ 消息发送 ============


class BaseSender:
    """消息发送器基类"""
    def send_text(self, contact: str, text: str) -> bool:
        raise NotImplementedError
    def send_image(self, contact: str, image_path: str) -> bool:
        raise NotImplementedError
    def send_file(self, contact: str, file_path: str) -> bool:
        raise NotImplementedError


class WeFlowApiSender(BaseSender):
    """基于 WeFlow API 的发送器"""
    def __init__(self, api_url: str, access_token: str):
        self.api_url = api_url
        self.access_token = access_token

    def send_text(self, contact: str, text: str) -> bool:
        try:
            resp = requests.post(
                self.api_url,
                json={"to": contact, "content": text, "type": "text"},
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                log.info(f"[WeFlowSender] 已发送至 {contact}: {text[:50]}...")
                return True
            else:
                log.error(f"[WeFlowSender] 发送失败: HTTP {resp.status_code}")
                return False
        except Exception as e:
            log.error(f"[WeFlowSender] 请求异常: {e}")
            return False

    def send_image(self, contact: str, image_path: str) -> bool:
        try:
            with open(image_path, "rb") as f:
                files = {"image": f}
                resp = requests.post(
                    self.api_url,
                    data={"to": contact, "type": "image"},
                    files=files,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    timeout=30,
                )
            if resp.status_code in (200, 201):
                log.info(f"[WeFlowSender] 图片已发送至 {contact}")
                return True
            else:
                log.error(f"[WeFlowSender] 图片发送失败: HTTP {resp.status_code}")
                return False
        except Exception as e:
            log.error(f"[WeFlowSender] 图片发送异常: {e}")
            return False


class AlphaApiSender(BaseSender):
    """基于 Alpha 原生 send-text API 的发送器。

    ⚠️ 需要在 Alpha 里激活「发送文字消息」（约 ¥100/月）并在
    「高级功能 → 发送文字消息」连接一次微信。未激活时返回
    409 SEND_CONNECTION_REQUIRED，本类会**返回 False 并打日志**，
    绝不假报成功。
    """

    def __init__(self, base_url: str, access_token: str,
                 wait_seconds: int = 30):
        self.base_url = (base_url or "").rstrip("/")
        self.access_token = access_token
        self.wait_seconds = max(0, min(120, int(wait_seconds or 30)))
        # Alpha 一次只允许一条未确认发送；并发时返回 409
        # MATERIAL_TEXT_SEND_RECOVERY_REQUIRED。桥接的 OB 客户端本身串行，
        # 这里再加锁做结构性兜底。
        self._lock = threading.Lock()

    def _send_text(self, contact: str, text: str) -> tuple[bool, str]:
        if not self.base_url:
            return False, "未配置 Alpha 地址"
        if not self.access_token:
            return False, "未配置 Alpha Access Token"
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/messages/send-text",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "username": contact,
                    "text": text,
                    "wait_seconds": self.wait_seconds,
                },
                timeout=self.wait_seconds + 15,
            )
        except Exception as e:
            return False, f"请求异常: {e}"

        if resp.status_code == 200:
            try:
                p = resp.json()
            except Exception:
                return False, f"响应不是 JSON: {resp.text[:100]}"
            st = str(p.get("state") or "")
            # delivered = 确认送达；written/unknown 都算「已交由 Alpha」，
            # 但 unknown 需要人工核对，不能盲重发 —— 返回 True 前必须打日志。
            if st in ("delivered", "written", "sending"):
                if st != "delivered":
                    log.warning(f"[AlphaSender] 状态={st}，run_id={p.get('run_id')} —— "
                                f"结果未完全确认，请勿盲目重发同一条")
                log.info(f"[AlphaSender] 已发送至 {contact}: {text[:50]}... state={st}")
                return True, st
            log.error(f"[AlphaSender] 发送失败: state={st} resp={resp.text[:200]}")
            return False, st or "unknown"

        # 结构化错误（401/403/404/409/423）
        try:
            err = (resp.json() or {}).get("error") or {}
            code = err.get("code") or str(resp.status_code)
            msg = err.get("message") or resp.text[:200]
        except Exception:
            code, msg = str(resp.status_code), resp.text[:200]
        log.error(f"[AlphaSender] 发送失败: HTTP {resp.status_code} {code} {msg}")
        return False, f"{code}: {msg}"

    def send_text(self, contact: str, text: str) -> bool:
        with self._lock:
            ok, _ = self._send_text(contact, text)
        return ok

    def send_image(self, contact: str, image_path: str) -> bool:
        # Alpha 0.2.0 只开放 send-text；图片/文件继续走 UIA，这里显式失败
        # 并提示，避免上层误以为已发出。
        log.error("[AlphaSender] send_image 尚未开放（Alpha 0.2.0 只有 send-text）")
        return False

    def send_file(self, contact: str, file_path: str) -> bool:
        log.error("[AlphaSender] send_file 尚未开放（Alpha 0.2.0 只有 send-text）")
        return False


def create_sender() -> BaseSender:
    """根据配置创建发送器"""
    if SEND_METHOD == "alpha_api":
        log.info("使用 Alpha API 发送文字消息（需在 Alpha 里激活发送功能）")
        return AlphaApiSender(ALPHA_BASE_URL, ALPHA_ACCESS_TOKEN,
                              ALPHA_SEND_WAIT_SECONDS)
    else:
        # config.py 已把历史值 "weflow_api" 归一化成 "uia"。
        log.info("使用 UIA 发送消息（微信 4.0+）")
        return UiaSender()
