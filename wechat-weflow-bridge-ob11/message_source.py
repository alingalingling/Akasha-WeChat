"""
消息源抽象层：把 WeFlow / Alpha 两套 HTTP API 差异挡在这一层。

为什么需要它（2026-10-18）：
  Alpha 虽然是 WeFlow 的继任者，但接口**不是**无脑兼容：
    · 鉴权：WeFlow 走 query access_token；Alpha 走 Authorization: Bearer
    · 推送：Alpha /api/v1/push/messages 必须带 accountId
    · 群成员：WeFlow 用 talker=；Alpha 用 chatroomId=
    · 群发言人：WeFlow 给昵称；Alpha 给 wxid
    · 图片：WeFlow 常给 [图片] 占位符；Alpha 给微信原生 XML
  桥接核心只跟本模块的统一接口打交道，切换 message_source 即可换后端。

注意：这里**不做**「把 Alpha 伪装成 WeFlow」——每个 Source 保留自己的原生调用方式，
只在出口把字段规整成 bridge_core 已经能理解的形态。
"""

import logging
import re

import requests

import config
import state

log = logging.getLogger("ob11-bridge")


def extract_media_url(item: dict) -> str:
    """从 Alpha 历史/SSE 消息中提取可下载媒体 URL。

    Alpha 0.2.0 的 `/api/v1/messages` 在不同阶段会返回：
      - 旧兼容字段：`mediaUrl`
      - 新结构：`media.resources[].url`
    只把已 resolve 的资源当作可用图片，避免把 unresolved 占位资源
    交给下载层后误报“取图失败”。
    """
    if not isinstance(item, dict):
        return ""
    for key in ("mediaUrl", "media_url"):
        value = str(item.get(key) or "")
        if value:
            return value

    media = item.get("media")
    if not isinstance(media, dict):
        return ""
    for res in media.get("resources") or []:
        if not isinstance(res, dict):
            continue
        status = str(res.get("status") or "").lower()
        if status and status != "resolved":
            continue
        value = str(res.get("url") or res.get("mediaUrl") or res.get("media_url") or "")
        if value:
            return value
    return ""


def extract_message_id(item: dict) -> str:
    """统一取得消息 ID：兼容 SSE 与 /api/v1/messages 两套字段名。"""
    if not isinstance(item, dict):
        return ""
    for key in ("rawid", "platform_message_id", "server_id",
                "source_id", "local_id", "id"):
        value = item.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return ""


def infer_event_type(item: dict) -> int:
    """从 Alpha 消息字段推断微信原生类型码，兼容 snake_case。"""
    if not isinstance(item, dict):
        return 0
    for key in ("type", "msgType", "sourceType", "source_type"):
        value = item.get(key)
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number:
            return number

    # 某些历史记录只给 kind=media + mimeType，没有 source_type。
    media = item.get("media")
    mime = ""
    if isinstance(media, dict):
        for res in media.get("resources") or []:
            if isinstance(res, dict) and res.get("mimeType"):
                mime = str(res.get("mimeType") or "").lower()
                break
    if mime.startswith("image/"):
        return 3
    if mime.startswith("video/"):
        return 43
    if mime.startswith("audio/"):
        return 34
    return 0


class MessageSourceError(RuntimeError):
    """消息源不可用/配置错误。message 里不得包含 token。"""


class BaseMessageSource:
    name = "base"

    @property
    def base_url(self) -> str:
        raise NotImplementedError

    def sse_url(self) -> str:
        raise NotImplementedError

    def sse_headers(self) -> dict:
        return {"Accept": "text/event-stream", "Cache-Control": "no-cache"}

    def check(self) -> tuple[bool, int, str]:
        raise NotImplementedError

    def fetch_contacts(self) -> list:
        raise NotImplementedError

    def fetch_group_members(self, room: str) -> list:
        raise NotImplementedError

    def fetch_messages(self, talker: str, *, limit: int = 5,
                       media: bool = False, order: str | None = None,
                       offset: int | None = None) -> dict:
        raise NotImplementedError

    def download_media(self, url: str) -> requests.Response:
        return requests.get(url, timeout=30)

    def normalize_event(self, data: dict) -> dict:
        return data


class WeFlowSource(BaseMessageSource):
    """旧链路：WeFlow HTTP API + SSE。"""

    name = "weflow"

    @property
    def base_url(self) -> str:
        return (config.WE_FLOW_BASE_URL or "").rstrip("/")

    def sse_url(self) -> str:
        return (f"{self.base_url}/api/v1/push/messages"
                f"?access_token={config.ACCESS_TOKEN}")

    def check(self) -> tuple[bool, int, str]:
        try:
            r = requests.get(
                f"{self.base_url}/api/v1/messages",
                params={"access_token": config.ACCESS_TOKEN, "limit": 1},
                timeout=5,
            )
            if r.status_code == 200:
                return True, 200, "WeFlow API 正常"
            if r.status_code == 401:
                return False, 401, "WeFlow Access Token 无效"
            return False, r.status_code, f"WeFlow API HTTP {r.status_code}"
        except requests.exceptions.ConnectionError:
            return False, 0, "无法连接 WeFlow"
        except Exception as e:
            return False, 0, f"WeFlow API 异常: {e}"

    def fetch_contacts(self) -> list:
        # WeFlow 老接口一次给全量，没有分页字段。
        r = requests.get(
            f"{self.base_url}/api/v1/contacts",
            params={"access_token": config.ACCESS_TOKEN},
            timeout=15,
        )
        if r.status_code != 200:
            raise MessageSourceError(f"WeFlow contacts HTTP {r.status_code}")
        items = r.json() if r.status_code == 200 else []
        if isinstance(items, dict):
            items = items.get("contacts") or items.get("items") or []
        return items or []

    def fetch_group_members(self, room: str) -> list:
        r = requests.get(
            f"{self.base_url}/api/v1/group-members",
            params={"access_token": config.ACCESS_TOKEN, "talker": room},
            timeout=15,
        )
        if r.status_code != 200:
            raise MessageSourceError(f"WeFlow group-members HTTP {r.status_code}")
        return r.json().get("members") or []

    def fetch_messages(self, talker: str, *, limit: int = 5,
                       media: bool = False, order: str | None = None,
                       offset: int | None = None) -> dict:
        # WeFlow 老接口不支持 offset 分页，收下参数但不转发（保持原行为）。
        params = {
            "access_token": config.ACCESS_TOKEN,
            "talker": talker,
            "limit": limit,
        }
        if media:
            params["media"] = "true"
        # WeFlow 老接口没有稳定的 order 参数；保持原行为（由服务端默认排序）。
        r = requests.get(f"{self.base_url}/api/v1/messages", params=params, timeout=20)
        if r.status_code != 200:
            raise MessageSourceError(f"WeFlow messages HTTP {r.status_code}")
        return r.json()


class AlphaSource(BaseMessageSource):
    """新链路：Alpha 原生 HTTP API + SSE。"""

    name = "alpha"
    _CONTACT_PAGE_SIZE = 200
    _MAX_CONTACT_PAGES = 20

    def __init__(self):
        self._account_id = (config.ALPHA_ACCOUNT_ID or "").strip()

    @property
    def base_url(self) -> str:
        return config.ALPHA_BASE_URL

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {config.ALPHA_ACCESS_TOKEN}"}

    def _get(self, path: str, *, params: dict | None = None, timeout: float = 15):
        return requests.get(
            f"{self.base_url}{path}",
            params=params or {},
            headers=self._headers,
            timeout=timeout,
        )

    def ensure_account(self) -> str:
        """返回 Alpha accountId；未配置时自动选择唯一/当前选中账号。"""
        if self._account_id:
            return self._account_id
        r = self._get("/api/v1/accounts", timeout=8)
        if r.status_code == 401:
            raise MessageSourceError("Alpha Access Token 无效")
        if r.status_code != 200:
            raise MessageSourceError(f"Alpha accounts HTTP {r.status_code}")
        payload = r.json()
        accounts = payload.get("accounts") or []
        selected = [a for a in accounts if a.get("selected")]
        ready = [a for a in accounts if a.get("ready") or a.get("usable")]
        if selected:
            candidates = [a for a in selected if a in ready] or selected
        elif len(ready) == 1:
            candidates = ready
        elif len(accounts) == 1:
            candidates = accounts
        else:
            raise MessageSourceError(
                "Alpha 有多个账号且未配置 alpha_account_id"
            )
        if not candidates:
            raise MessageSourceError("Alpha 没有可用账号")
        self._account_id = str(candidates[0].get("id") or "")
        if not self._account_id:
            raise MessageSourceError("Alpha 账号响应缺少 id")
        return self._account_id

    def sse_url(self) -> str:
        account = self.ensure_account()
        # Alpha 要求 accountId；鉴权放 Header，避免 token 进 URL/日志。
        return (f"{self.base_url}/api/v1/push/messages?accountId={account}")

    def sse_headers(self) -> dict:
        return {
            **self._headers,
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }

    def check(self) -> tuple[bool, int, str]:
        if not self.base_url:
            return False, 0, "未配置 Alpha 地址"
        if not config.ALPHA_ACCESS_TOKEN:
            return False, 0, "未配置 Alpha Access Token"
        try:
            self.ensure_account()
            return True, 200, "Alpha API 正常"
        except MessageSourceError as e:
            return False, 0, str(e)
        except requests.exceptions.ConnectionError:
            return False, 0, "无法连接 Alpha"
        except Exception as e:
            return False, 0, f"Alpha API 异常: {e}"

    def fetch_contacts(self) -> list:
        account = self.ensure_account()
        out: list = []
        offset = 0
        for _ in range(self._MAX_CONTACT_PAGES):
            r = self._get(
                "/api/v1/contacts",
                params={"account_id": account, "limit": self._CONTACT_PAGE_SIZE,
                        "offset": offset},
                timeout=30,
            )
            if r.status_code != 200:
                raise MessageSourceError(f"Alpha contacts HTTP {r.status_code}")
            p = r.json()
            page = p.get("items") or p.get("contacts") or []
            out.extend(page)
            has_more = bool(p.get("hasMore"))
            next_offset = p.get("nextOffset")
            if not has_more or next_offset is None:
                break
            if not page:
                break
            offset = int(next_offset)
        else:
            raise MessageSourceError("Alpha contacts 分页超过安全上限")
        return out

    def fetch_group_members(self, room: str) -> list:
        account = self.ensure_account()
        # ⚠️ Alpha 参数名是 chatroomId，不是 WeFlow 的 talker。
        r = self._get(
            "/api/v1/group-members",
            params={"account_id": account, "chatroomId": room},
            timeout=30,
        )
        if r.status_code != 200:
            raise MessageSourceError(f"Alpha group-members HTTP {r.status_code}")
        return r.json().get("members") or []

    def fetch_messages(self, talker: str, *, limit: int = 5,
                       media: bool = False, order: str | None = None,
                       offset: int | None = None) -> dict:
        account = self.ensure_account()
        params = {
            "account_id": account,
            "talker": talker,
            "limit": limit,
        }
        if media:
            params["media"] = "true"
        if order:
            # Alpha 支持 desc；默认是升序（最旧在前），取「刚到的新图」必须倒序。
            params["order"] = order
        if offset:
            # 引用图片可能来自较早的历史消息，只查首页会找不到 —— 支持翻页。
            params["offset"] = int(offset)
        r = self._get("/api/v1/messages", params=params, timeout=30)
        if r.status_code != 200:
            raise MessageSourceError(f"Alpha messages HTTP {r.status_code}")
        return r.json()

    def download_media(self, url: str) -> requests.Response:
        # Alpha 的 mediaUrl 是 /api/v1/media/... 相对路径，且必须带 Bearer。
        if url.startswith("/"):
            url = f"{self.base_url}{url}"
        return requests.get(url, headers=self._headers, timeout=30)

    @staticmethod
    def _classify_content(content: str) -> str:
        """把 Alpha 推送的微信原生 XML 归一成桥接已有的占位符。

        - 顶层 <emoji> 保留原 XML：wx_msg_parser 能解出 md5/cdnurl，
          走既有表情下载链路。
        - 图片/视频/语音没有可读文本，转成占位符；图片随后由 messages API
          拿 mediaUrl 下载真实字节。
        - appmsg（链接/文件/合并转发）保留原 XML，交给 wx_msg_parser。
        """
        s = content or ""
        if not s:
            return s
        # 顶层表情（Alpha 实测形态：<msg><emoji ... cdnurl=...>...</msg>）
        if "<emoji" in s and "<appmsg" not in s:
            return s
        # 避免把 appmsg 里嵌的 <img> 缩略图误判成图片消息
        if "<appmsg" in s:
            return s
        if re.search(r"<img\b", s, re.I):
            return "[图片]"
        if re.search(r"<videomsg\b", s, re.I):
            return "[视频]"
        if (re.search(r"<voipmsg\b", s, re.I)
                or re.search(r"<voice\b", s, re.I)):
            return "[语音]"
        return s

    def normalize_event(self, data: dict) -> dict:
        """Alpha SSE / 历史记录 → bridge_core 兼容字段。

        2026-10-21 实测：同一 Alpha 版本的两套接口字段名并不完全一致。
        SSE 主要给 camelCase（`sessionId/sourceName/sourceType`），
        `/api/v1/messages` 则会给 snake_case（`conversation_id/sender_id/source_type`）。
        如果只认其中一套，图片会以“空内容”被跳过，地址/表情会变成 unknown。
        """
        if not isinstance(data, dict):
            return data
        out = dict(data)

        session_id = str(out.get("sessionId") or out.get("session_id")
                         or out.get("conversation_id") or out.get("conversationId") or "")
        session_type = str(out.get("sessionType") or out.get("session_type") or "")
        source_name = str(out.get("sourceName") or out.get("source_name")
                          or out.get("sender_id") or out.get("senderId") or "")
        sender_wxid = str(out.get("senderWxid") or out.get("sender_id")
                          or out.get("senderId") or "")
        sender_name = str(out.get("senderName") or out.get("sender_name") or "")

        if session_id:
            out["sessionId"] = session_id
        is_group = (
            session_type == "group"
            or session_type == "2"
            or "@chatroom" in session_id
            or str(out.get("conversation_id") or "").endswith("@chatroom")
        )

        # Alpha 群聊 sourceName 是发言成员 wxid；私聊是会话名。
        if is_group:
            wxid = sender_wxid or source_name
            if wxid:
                out["talkerId"] = wxid
                out["senderWxid"] = wxid
            resolved = (
                state.resolve_name_in_group(session_id, wxid)
                if session_id and wxid else ""
            )
            # sender_name 是历史接口已经给出的人类可读名；SSE 通常没有，
            # 这时才查群名册。不要把两套字段互相覆盖。
            out["senderName"] = sender_name or resolved or wxid or source_name
        else:
            out["talkerId"] = session_id
            out["senderWxid"] = sender_wxid or session_id
            out["senderName"] = sender_name or source_name or session_id

        if not out.get("rawid"):
            message_id = extract_message_id(out)
            if message_id:
                out["rawid"] = message_id

        # 历史接口用秒/毫秒时间戳两套字段；统一给桥接的 start_timestamp 门禁。
        if not out.get("timestamp"):
            sent_ms = out.get("sent_at_unix_ms") or out.get("sentAtUnixMs")
            if sent_ms:
                try:
                    out["timestamp"] = int(int(sent_ms) / 1000)
                except (TypeError, ValueError):
                    pass
            else:
                sent = out.get("sort_timestamp") or out.get("last_activity_unix_ms")
                if sent:
                    try:
                        value = int(sent)
                        out["timestamp"] = value // 1000 if value > 10_000_000_000 else value
                    except (TypeError, ValueError):
                        pass

        # Alpha 有的接口把正文放 content，有的放 text；两处都兼容。
        raw_content = str(out.get("content") or out.get("text") or "")
        if not raw_content.strip():
            # Alpha 0.2.0 实测：图片/视频/语音媒体事件可能是空内容。
            event_type = infer_event_type(out)
            if event_type == 3:
                out["content"] = "[图片]"
            elif event_type == 43:
                out["content"] = "[视频]"
            elif event_type == 34:
                out["content"] = "[语音]"
            else:
                out["content"] = raw_content
        else:
            out["content"] = self._classify_content(raw_content)

        # 让取图层优先直接使用 SSE/历史记录已带上的媒体地址，
        # 避免再次查询历史列表时因分页/排序变化而取到别的图片。
        media_url = extract_media_url(out)
        if media_url:
            out["_alphaMediaUrl"] = media_url
        out["_alphaRawContent"] = raw_content
        return out


def create_message_source() -> BaseMessageSource:
    if config.MESSAGE_SOURCE == "alpha":
        return AlphaSource()
    return WeFlowSource()
