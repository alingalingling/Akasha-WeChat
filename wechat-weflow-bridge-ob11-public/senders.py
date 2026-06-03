"""
消息发送器模块：BaseSender 基类、WeFlow API 发送器、工厂函数。

导入 UiaSender 后通过 create_sender() 工厂方法选择发送模式。
"""

import logging
import os
import requests
from uia_sender import UiaSender

log = logging.getLogger("ob11-bridge")

from config import WE_FLOW_SEND_API, ACCESS_TOKEN, SEND_METHOD


# ============ 消息发送 ============


class BaseSender:
    """消息发送器基类"""
    def send_text(self, contact: str, text: str) -> bool:
        raise NotImplementedError
    def send_image(self, contact: str, image_path: str) -> bool:
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


def create_sender() -> BaseSender:
    """根据配置创建发送器"""
    if SEND_METHOD == "weflow_api":
        log.info("使用 WeFlow API 发送消息")
        return WeFlowApiSender(WE_FLOW_SEND_API, ACCESS_TOKEN)
    else:
        log.info("使用 UIA 发送消息（微信 4.0+）")
        return UiaSender()
