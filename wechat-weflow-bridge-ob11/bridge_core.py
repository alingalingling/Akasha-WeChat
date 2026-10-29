"""
桥接核心模块：WeFlowBridge 类。

职责：
1. 连接 WeFlow SSE 推送，接收微信消息
2. 消息缓冲合并（BUFFER_SECONDS）
3. 构造 OneBot 事件，推送给 AstrBot
4. 多层消息去重（rawid、内容、自回复）
"""

import base64
import json
import logging
import os
import queue
import re
import threading
import time
from collections import defaultdict
from datetime import datetime

import requests

import state
import config
import wx_msg_parser
from message_source import create_message_source, extract_media_url, extract_message_id
from ob_protocol import push_event, make_message_event

log = logging.getLogger("ob11-bridge")


def _looks_like_wxid(name: str) -> bool:
    """判断一个"名字"其实是 wxid / 内部 ID，而不是真人昵称。"""
    if not name:
        return True
    n = name.strip()
    if n.startswith("wxid_"):
        return True
    # 纯数字（内部 uid / 群 ID）也算
    if n.isdigit():
        return True
    return False


def _resolve_display_name(raw: str, fallback_contact: str = "") -> str:
    """把 WeFlow 给的"名字"规整成**人类可读的昵称**。

    为什么需要它（2026-09-18 实测）：
      WeFlow 对某些联系人推来的 `sourceName` 是 **wxid**（如
      `wxid_xxxxxxxxxxxxxxxx`）而不是昵称。桥接原样把它当 `nickname` 发给
      AstrBot，AstrBot 的 UMO 自动命名（`umo_auto_name`）就把这个名字
      记进了 `umo_aliases.auto_name` —— 结果面板「自定义规则」里那一行
      显示成 wxid，根本看不出是谁（用户报障）。

    策略：raw 看起来像 wxid/数字时，依次尝试
      ① `state` 里按 wxid 查已知昵称（persons.json 的名册）
      ② 调用方给的 contact（通常已经是解析过的显示名）
      ③ 实在没有才退回 raw
    """
    if not _looks_like_wxid(raw):
        return raw
    # ① 名册反查：persons.json 里 wxid → 当前会话名 → names[0]
    try:
        p = state.all_persons().get(raw)
        if isinstance(p, dict):
            cur = state.get_chat_name(raw)
            if cur and not _looks_like_wxid(cur):
                return cur
            names = p.get("names") or []
            for nm in names:
                if nm and not _looks_like_wxid(nm):
                    return nm
    except Exception:
        pass
    # ② contact 兜底
    if fallback_contact and not _looks_like_wxid(fallback_contact):
        return fallback_contact
    # ③ 最后退回原名（至少不比以前差）
    return raw or fallback_contact


def refresh_contact_names() -> tuple:
    """从 WeFlow /api/v1/contacts 全量刷新 联系人/群 → 显示名 缓存。

    返回 (本次新增条数, 缓存总条数, 错误信息或空串)。

    为什么是模块级函数而不是只放在 WeFlowBridge 里（2026-10-07）：
      ① 面板「成员与权限」的 /api/refresh-people 要在**桥接未启动**时也能
         拉新群 —— 用户把 bot 拉进新群后立刻打开面板，就该看到它；
      ② 后台 10 分钟名册刷新循环先调它，捕捉「启动后新加入的群」。
      以前只有启动时拉一次 contacts，新群若一直没人发言就永远进不了
      known_groups()，面板「群里管什么」表也就永远不出现这个群（用户报障）。
    """
    try:
        # WeFlow/Alpha 的分页、鉴权、字段差异统一封在 message_source。
        items = create_message_source().fetch_contacts()
        if isinstance(items, dict):
            items = items.get("contacts") or items.get("items") or []
        n = 0
        for it in items or []:
            if not isinstance(it, dict):
                continue
            username = it.get("username", "")
            name = it.get("displayName") or it.get("nickname") or ""
            if state.set_chat_name(username, name):
                n += 1
        return n, len(state.all_chat_names()), ""
    except Exception as e:
        return 0, len(state.all_chat_names()), str(e)


# ============ 桥接核心 ============


class WeFlowBridge:
    """WeFlow ↔ AstrBot 桥接器（OneBot v11 版）。"""

    # 同一「会话+成员+原因」在这个秒数内只打一条跳过日志（避免活跃群刷屏）
    SKIP_LOG_INTERVAL = 30

    def __init__(self, sender):
        self.sender = sender
        self.processed_ids = set()          # 已处理的 rawid（防 SSE 重放）
        self._processed_order = []          # 配合上限做 FIFO 淘汰，避免无限增长
        self.start_timestamp = int(time.time())
        self.pending_buffers = {}           # buffer_key → 待推送条目
        self.buffer_lock = threading.Lock()
        self.chat_histories = defaultdict(list)
        self.contact_map = {}
        self._sse_session = None
        self._recent_seen = {}
        self._sent_recently = {}
        self._sse_event_keys = {}
        self._pending_image = {}            # talkerId → {"caption": None|str, "event": threading.Event()}
        self._skip_stat = {}                # (会话, 成员, 原因) → {"count": n, "last": ts}
        self._skip_lock = threading.Lock()

    # ============ 内部工具 ============

    @staticmethod
    def _sender_of(data) -> str:
        """消息发送者显示名（群成员名优先，退回 sourceName）。"""
        return (data.get("senderName", "") or data.get("sender", "")
                or data.get("sourceName", "") or "未知")

    def _resolve_group_name(self, session_id_data: str, group_raw: str,
                            source_name: str) -> str:
        """群名解析：SSE groupName → 持久化缓存 → 发送者名，并去掉 "(12)" 成员数后缀。

        部分群/系统消息的 SSE groupName 会缺失，退路是启动时预取并持续回写的
        持久化缓存 —— 绝不把 xxx@chatroom 这种原始 ID 当群名送出去。
        """
        if group_raw:
            state.set_chat_name(session_id_data, group_raw)
        if not group_raw or state._is_bad_name(group_raw):
            group_raw = state.get_chat_name(session_id_data) or group_raw
        if not group_raw:
            group_raw = source_name
        return re.sub(r'\s*\(\d+\)\s*$', '', group_raw).strip()

    @staticmethod
    def _buffer_key(session_id_data: str, sender_in_group: str,
                    is_group: bool, base_name: str) -> str:
        """同一个人的同一次请求必须落在同一个缓冲键上（文本和图片共用）。"""
        if is_group and state.group_reply_mode == "batch":
            return f"__batch__{base_name}"
        if is_group and sender_in_group:
            return f"{session_id_data}_{sender_in_group}"
        return session_id_data

    def _ensure_entry(self, buffer_key, data, contact, is_group,
                      base_name, sender_in_group, session_id_data):
        """确保缓冲条目存在（调用方必须已持有 buffer_lock）。"""
        entry = self.pending_buffers.get(buffer_key)
        if entry is None:
            entry = {
                "messages": [],
                "pending_images": [],   # 待下载的图片原始数据（推送前统一处理）
                # 引用图片的元数据（svrid/md5）：下载同样放到推送前做，
                # 避免在 SSE 接收线程里同步翻页造成消息积压（实测每页 ~4s）。
                "pending_quote_images": [],
                "timer": None,
                "timer_version": 0,
                "processing": False,
                "contact": contact,
                "is_group": is_group,
                "source_name": data.get("sourceName", "") or "未知",
                "group_name": base_name if is_group else "",
                "sender_in_group": sender_in_group if is_group else "",
                "session_id_data": session_id_data,
                # Alpha 直接给发言人 wxid；WeFlow 没有（后续靠名册反查）。
                "sender_wxid": data.get("senderWxid", "") or data.get("talkerId", ""),
            }
            self.pending_buffers[buffer_key] = entry
        return entry

    def _schedule_flush(self, buffer_key, entry, delay=None):
        """（重新）安排缓冲到期推送（调用方必须已持有 buffer_lock）。"""
        if entry["processing"]:
            return
        if entry["timer"]:
            entry["timer"].cancel()
        entry["timer_version"] += 1
        version = entry["timer_version"]
        timer = threading.Timer(
            delay if delay is not None else config.BUFFER_SECONDS,
            lambda: self.process_sender(buffer_key, version),
        )
        timer.daemon = True
        timer.start()
        entry["timer"] = timer

    def _log_skip(self, data, reason: str):
        """记录「收到了、但被策略丢弃」的消息。

        以前这些丢弃是静默的（日志里什么都没有），会让人误以为桥接根本没收到消息。
        同一「会话+成员+原因」在 SKIP_LOG_INTERVAL 秒内只打一条，并把期间累计条数一起报出。
        """
        if not getattr(config, "LOG_SKIPPED_MESSAGES", True):
            return

        session_id = data.get("sessionId", "") or data.get("sourceName", "")
        who = self._sender_of(data)
        key = (session_id, who, reason)
        now = time.time()

        with self._skip_lock:
            st = self._skip_stat.get(key)
            if st is None:
                st = {"count": 0, "last": 0.0}
                self._skip_stat[key] = st
            st["count"] += 1
            if now - st["last"] < self.SKIP_LOG_INTERVAL:
                return
            n = st["count"]
            st["count"] = 0
            st["last"] = now

            # 顺手清理长时间没再出现的键，避免无限增长
            if len(self._skip_stat) > 1000:
                cutoff = now - 600
                for k in [k for k, v in self._skip_stat.items() if v["last"] < cutoff]:
                    self._skip_stat.pop(k, None)

        group = data.get("groupName", "")
        where = f"群[{group}] " if group else ""
        extra = f"（{self.SKIP_LOG_INTERVAL}s 内累计 {n} 条）" if n > 1 else ""
        log.info(f"⏭️ 跳过 {where}{who}: {reason}{extra}")

    def _prefetch_contact_names(self):
        """启动时从 WeFlow /api/v1/contacts 全量预取 联系人/群 → 显示名 映射。

        WeFlow 的 SSE 推送偶发缺失 groupName / sourceName（部分群、系统消息），
        没有这份缓存时桥接会把 xxx@chatroom 当群名发出去，AstrBot 面板就会
        显示原始 ID。预取失败不影响启动——后续消息仍会增量回写缓存。

        实际抓取逻辑在模块级 refresh_contact_names()（面板与定时刷新共用）。
        """
        n, total, err = refresh_contact_names()
        if err:
            log.warning(f"联系人名称预取失败（不影响运行，靠消息流增量补充）: {err}")
        else:
            log.info(f"📇 联系人名称缓存已预取（本次新增 {n} 条，共 {total} 条）")

    def _prefetch_group_rosters(self):
        """拉取所有已知群的成员名册（wxid ↔ 昵称），支撑「同人同 ID」。

        WeFlow 的 SSE 在群里只给昵称，而 /api/v1/group-members 能给全名单
        （含 wxid）。有了名册，群消息的发言人就能解析成稳定的 wxid，
        user_id 从而与私聊一致 —— AstrBot 的管理员列表加一次就处处生效。
        每 10 分钟自动刷新一次，捕捉进群/退群/改昵称。
        """
        rooms = list(state.known_groups().keys())
        ok = 0
        source = create_message_source()
        for room in rooms:
            try:
                members = source.fetch_group_members(room)
                if members:
                    state.set_group_roster(room, members)
                    for m in members:
                        if isinstance(m, dict) and m.get("wxid"):
                            nm = (m.get("groupNickname") or m.get("displayName")
                                  or m.get("nickname") or m.get("remark") or "")
                            state.remember_person(m["wxid"], nm)
                    ok += 1
            except Exception:
                continue
        if rooms:
            total_names = sum(state.roster_stats().values())
            log.info(f"👥 群成员名册已刷新（{ok}/{len(rooms)} 个群，{total_names} 条昵称映射）")
        # 名册刷新后顺手把 AstrBot 里"没名字/名字是 wxid"的会话补上可读名
        # （面板「自定义规则」显示的就是这个；见 people.sync_umo_aliases）
        try:
            import people
            people.sync_umo_aliases()
        except Exception as e:
            log.debug(f"UMO 别名同步跳过: {e}")

    def _roster_refresh_loop(self):
        """后台定时刷新（守护线程，10 分钟一轮）：
        先补拉 contacts（捕捉启动后新加入的群/新好友），再刷群成员名册。

        ⚠️ 只刷名册不拉 contacts 是不够的：名册遍历的是 known_groups()，
        而它来自 contacts 缓存 —— 新群若没人发言就永远不会被名册循环看到，
        面板「群里管什么」表也不会出现它（2026-10-07 用户报障）。
        """
        while state.running:
            time.sleep(600)
            if not state.running:
                break
            try:
                n, _total, err = refresh_contact_names()
                if n:
                    log.info(f"📇 联系人缓存定时刷新：新增 {n} 条（含新群/新好友）")
                elif err:
                    log.debug(f"联系人缓存定时刷新失败（下轮重试）: {err}")
            except Exception as e:
                log.debug(f"联系人缓存定时刷新异常（下轮重试）: {e}")
            try:
                self._prefetch_group_rosters()
            except Exception as e:
                log.debug(f"群名册刷新失败（下轮重试）: {e}")

    def ignore_reason(self, data):
        """返回该消息被忽略的原因；不该忽略则返回 None。"""
        content = data.get("content", "")
        msg_type = data.get("type", 0) or data.get("msgType", 0)
        if data.get("sourceName", "") in config.BOT_NICKNAMES:
            return "机器人自己发的"
        # 微信 4.x 有时会给 wxid 带上 _xxxx 后缀（WeFlow 的 talkerId 不带），
        # 两边都可能是"主干"或"带后缀"，用前缀互比避免漏掉自己发的消息。
        if config.BOT_WXID and data.get("talkerId", ""):
            _bot = config.BOT_WXID
            _talker = str(data.get("talkerId", ""))
            if _talker == _bot or _bot.startswith(_talker) or _talker.startswith(_bot):
                return "来自机器人本机账号"
        if msg_type in (34,):  # 34=语音
            return "语音消息（type=34）"
        # 语音占位符仍然丢弃（没有可读内容，且 bot 无法听）。
        #
        # 但**表情必须放行** —— 改动前这里把「[语音] / [表情]」一起丢弃，
        # 导致群里最高频的非文本表达 bot 完全无感（实测 2026-09-23：
        # 一天内被丢掉的真实发言十几条）。表情现在由 wx_msg_parser
        # 归一化后进缓冲区，不再走到这里。
        if content and "[语音]" in content:
            return "语音占位内容"
        if content and "[表情]" in content:
            return None  # 放行 —— 交给 wx_msg_parser 处理
        # 微信系统协议消息：「拍了拍」等不是人类发言，不该进模型上下文。
        # 事故（2026-09-20 16:49）：bot 把「"耳东亭氵川" 拍了拍 "辞星" 的ass」
        # 当成一条待回应的发言，还认真"解读"了一遍发到群里。
        # 形态：`"A" 拍了拍 "B"`（可能带后缀，如「的ass，下次别拍了[害羞]」）。
        if content and "拍了拍" in content:
            return "系统协议消息（拍了拍）"
        # 撤回/入群/退群等系统提示，同理
        if content and any(k in content for k in
                           ("撤回了一条消息", "邀请", "加入了群聊", "移出了群聊")):
            return "系统协议消息（群提示）"
        if not content or content.strip() == "":
            return "空内容"
        return None

    def should_ignore(self, data):
        """兼容上游原版签名：该消息要不要丢弃。

        判定条件与原版完全一致 —— 只是抽成了 ignore_reason() 以便顺带记录
        「为什么丢」，这里保留原方法名给外部/上游调用。
        """
        return self.ignore_reason(data) is not None

    def add_to_buffer(self, data):
        """将消息加入缓冲区，等待合并后统一推送给 AstrBot。"""
        content = data.get("content", "")
        source_name = data.get("sourceName", "") or data.get("talkerName", "") or "未知"

        session_id_data = data.get("sessionId", "") or source_name
        group_name_raw = data.get("groupName", "")
        is_group = (data.get("sessionType", "") == "group") or bool(group_name_raw) or "@chatroom" in session_id_data
        sender_in_group = self._sender_of(data)

        # 面板里被关闭回复的会话：所有消息（包括 @ 机器人）直接丢弃，
        # 机器人完全不掺和——用于"一个群里有多个机器人"的场景
        if state.is_session_muted(session_id_data):
            self._log_skip(data, "该会话已在面板关闭回复")
            return

        # 群里有人 @ 机器人 → 按 (群, 发送者) 记录时刻（供图片「同一次请求」判断）
        mentioned = is_group and any(f"@{n}" in content for n in config.BOT_NICKNAMES)
        if mentioned:
            state.note_group_mention(session_id_data, sender_in_group)

        # ---------- 图片 ----------
        # 两条路径都保留，由 config.IMAGE_CAPTION_ENABLED 决定：
        #   ① 开了「桥接侧图片描述」→ 走原版 process_image_message（取图 → 描述模型 → 注入文字）
        #   ② 没开（默认）→ 图片原样投递，作为标准 OneBot image 段交给 AstrBot，
        #      AstrBot 自己决定怎么理解（配了图片转述模型就用它，留空则主模型直看）
        if content == "[图片]":
            if (is_group and state.group_reply_mode == "mention"
                    and not state.recent_group_mention(session_id_data, sender_in_group,
                                                       config.IMAGE_AFTER_MENTION_WINDOW)):
                # 与「同一个人的 @」不属于同一次请求 → 暂存，等这个人随后 @
                state.stash_group_image(session_id_data, sender_in_group, data)
                self._log_skip(data, "群图片已暂存（同一人 @ 后才会读取）")
                return

            if config.IMAGE_CAPTION_ENABLED:
                # talkerId 覆写成本人文本所用的 buffer_key，图片才会和 @ 并成同一条推送
                base_name = (self._resolve_group_name(session_id_data, group_name_raw, source_name)
                             if is_group else "")
                data["talkerId"] = self._buffer_key(session_id_data, sender_in_group,
                                                    is_group, base_name)
                threading.Thread(target=self.process_image_message,
                                 args=(data,), daemon=True).start()
            else:
                self._enqueue_image(data, is_group, session_id_data, sender_in_group,
                                    group_name_raw, source_name)
            return

        # ---------- 富消息统一解析（方案 B）----------
        # 走到这里说明既不是图片、也不该被 ignore_reason() 丢掉。
        # 用 wx_msg_parser 把 XML 归一化成「人话」再进缓冲 ——
        # 改动前这里会把链接/文件/合并转发的整坨 XML 当文本喂给模型，
        # 而表情/语音则在 ignore_reason() 里被整条丢弃（bot 完全无感）。
        if config.PARSE_RICH_MESSAGES:
            parsed = wx_msg_parser.parse_content(content, config.BOT_NICKNAMES)
            if parsed.kind != wx_msg_parser.KIND_TEXT:
                self._handle_rich_message(
                    parsed, data, is_group, session_id_data, sender_in_group,
                    group_name_raw, source_name, mentioned)
                return
            # 纯文本：用剥离过 HTML 的干净文本继续走原逻辑
            content = parsed.text

        # ---------- 文本 ----------
        # 群聊指令（/sid 等）也必须 @ 机器人才放行：避免同一个群里多个机器人
        # 对同一条 "/help" 抢答。@ 了之后仍走无外壳透传（见 process_sender）。
        now = time.time()
        if state.was_sent_recently(content):
            log.info(f"⏭️ 自回复去重跳过: {content[:30]}")
            return

        if is_group and state.group_reply_mode == "mention" and not mentioned:
            self._log_skip(data, "群消息未 @机器人（mention 模式）")
            return

        # 群聊斜杠指令在任何模式下都必须 @ 机器人才放行：
        # 避免同一群里多个机器人对裸 /help 抢答。@ 了仍无外壳透传。
        if is_group and content.lstrip().startswith("/") and not mentioned:
            self._log_skip(data, "群指令未 @机器人（指令需 @ 触发）")
            return

        if is_group:
            base_name = self._resolve_group_name(session_id_data, group_name_raw, source_name)
            contact = base_name
        else:
            base_name = ""
            if source_name != "未知":
                state.set_chat_name(session_id_data, source_name)
            contact = source_name if source_name != "未知" else (
                state.get_chat_name(session_id_data) or source_name)

        buffer_key = self._buffer_key(session_id_data, sender_in_group, is_group, base_name)

        # 这个人 @ 之前发过的图 → 并入同一次请求（与文本同一个 buffer_key）
        extra_images = (state.pop_group_images(session_id_data, sender_in_group,
                                               config.IMAGE_MENTION_WINDOW)
                        if mentioned else [])

        with self.buffer_lock:
            entry = self._ensure_entry(buffer_key, data, contact, is_group,
                                       base_name, sender_in_group, session_id_data)
            if is_group and state.group_reply_mode == "batch" and sender_in_group:
                entry["messages"].append(f'成员"{sender_in_group}"在群"{base_name}"中对你说：{content}')
            else:
                entry["messages"].append(content)
            # 记录这一批消息里是否有人真的 @ 过机器人：
            # @ 段只在真 @ 时才加（process_sender 用）——否则 all 模式下每条消息
            # 都会被 AstrBot 当成 @ 唤醒，逐条必回，随机插话机制完全失效。
            if mentioned:
                entry["mentioned_any"] = True
            if extra_images:
                entry["pending_images"].extend(extra_images)
                log.info(f"🖼️ {len(extra_images)} 张暂存图片已并入本次消息 [{contact}]")
            self._schedule_flush(buffer_key, entry)

        log.info(f"📩 收到来自 {contact} 的消息，等待 {config.BUFFER_SECONDS}s 后统一推送")

    def _handle_rich_message(self, parsed, data, is_group, session_id_data,
                             sender_in_group, group_name_raw, source_name,
                             mentioned):
        """处理非文本富消息（表情/链接/文件/合并转发/系统提示）。

        这些消息在旧逻辑里要么把 XML 原样喂给模型，要么被整条丢弃。
        现在统一走这里：**转成人话进缓冲区**，让 bot 知道「发生了什么」。

        注意几个刻意的设计：
          - 系统消息（拍了拍/撤回/入群）**不进模型上下文** —— 它们不是人类发言。
            事故（2026-09-20）：bot 把「"A" 拍了拍 "B" 的ass」当成发言认真解读。
            但**表情包必须保留**，那是真实的表达（旧逻辑把两者一起丢了）。
          - 富消息同样遵守「群聊 mention 模式必须 @ 才响应」，避免刷屏。
        """
        # 系统消息：不推送，但要记日志（便于排查"bot 为什么不理我"）
        if parsed.is_ignorable:
            self._log_skip(data, f"系统协议消息（{parsed.system_kind or '未知'}）")
            return

        # 自回复去重：表情/链接也可能是自己发的
        if state.was_sent_recently(parsed.text):
            log.info(f"⏭️ 自回复去重跳过: {parsed.text[:30]}")
            return

        # 群聊 mention 模式：非 @ 的富消息同样不响应
        if is_group and state.group_reply_mode == "mention" and not mentioned:
            self._log_skip(data, "群消息未 @机器人（mention 模式）")
            return

        # 计算缓冲键与会话名（与文本路径保持一致）
        if is_group:
            base_name = self._resolve_group_name(session_id_data, group_name_raw, source_name)
            contact = base_name
        else:
            base_name = ""
            if source_name != "未知":
                state.set_chat_name(session_id_data, source_name)
            contact = source_name if source_name != "未知" else (
                state.get_chat_name(session_id_data) or source_name)

        buffer_key = self._buffer_key(session_id_data, sender_in_group, is_group, base_name)

        entry_text = parsed.text
        # 群聊批处理模式要套统一的「某某在群某某中说：」外壳
        if is_group and state.group_reply_mode == "batch" and sender_in_group:
            entry_text = (f'成员"{sender_in_group}"在群"{base_name}"中'
                          f'对你说：{entry_text}')

        sticker_image = None
        if (parsed.kind == wx_msg_parser.KIND_STICKER
                and parsed.sticker_url
                and config.STICKER_FETCH_ENABLED):
            # 带 md5 的表情：下载成 GIF 作为图片段投递。
            # 下载放锁外（网络耗时），失败不影响文字部分照常推送。
            sticker_image = self._fetch_sticker_segment(parsed)

        # 被引用的图片：这里只**记录元数据**，推送时再回查/下载。
        # 为什么需要（2026-10-29）：微信引用图片时，正文只有一句
        # 「〔引用 某某：[图片]〕」，AstrBot 收不到 image 段就不会触发识图，
        # 模型只能答"看不清"。靠 refermsg 的 svrid 回查 Alpha 历史拿到原图，
        # 再作为标准 OneBot image 段和文字一起推送。
        #
        # ⚠️ 刻意不在本函数（SSE 接收线程）里下载：回查历史实测每页 ~4s，
        # 翻 2 页 + 下载接近 10s，会把**所有会话**的消息接收一起卡住。
        # 改到 process_sender 的 Timer 线程里做，与普通图片下载同一条路径；
        # 失败只记日志，文字照常推送（能力只增不减）。
        quote_image = ((parsed.extra or {}).get("quote_image")
                       if parsed.kind == wx_msg_parser.KIND_QUOTE else None)

        with self.buffer_lock:
            entry = self._ensure_entry(buffer_key, data, contact, is_group,
                                       base_name, sender_in_group, session_id_data)
            entry["messages"].append(entry_text)
            if mentioned:
                entry["mentioned_any"] = True
            if sticker_image:
                entry.setdefault("pending_sticker_segments", []).append(sticker_image)
            if quote_image:
                entry["pending_quote_images"].append(
                    self._start_quote_image_fetch(session_id_data, quote_image, contact))
            self._schedule_flush(buffer_key, entry)

        log.info(f"📩 收到 {parsed.kind}（{parsed.text[:40]}）"
                 f"来自 {contact}，等待 {config.BUFFER_SECONDS}s 后统一推送")

    def _fetch_sticker_segment(self, parsed):
        """下载表情包 GIF，返回 OneBot image 段；失败返回 None。

        实测（2026-09-23）：emojiinfo 里解出的直链可用，返回
        HTTP 200 + 1168271 字节（与 XML 的 <totallen> 一致）+ GIF89a。

        直链带时效性 filekey，所以**当下就下载**，不能只存链接。
        """
        try:
            resp = requests.get(parsed.sticker_url, timeout=15)
            if resp.status_code != 200:
                log.warning(f"表情下载失败: HTTP {resp.status_code}")
                return None
            blob = resp.content
            if not blob:
                return None
            if len(blob) > config.STICKER_MAX_BYTES:
                log.warning(f"表情过大，跳过: {len(blob)} 字节")
                return None
            b64 = base64.b64encode(blob).decode()
            log.info(f"😀 表情已下载: md5={parsed.sticker_md5[:8]}… "
                     f"{len(blob)} 字节")
            return {"type": "image", "data": {"file": f"base64://{b64}"}}
        except Exception as e:
            log.warning(f"表情下载异常: {e}")
            return None

    def _start_quote_image_fetch(self, session_id_data, quote_image, contact):
        """在后台线程里回查/下载引用图片，返回一个结果持有器。

        为什么要并发：回查历史实测每页 ~4s，翻 2 页 + 下载接近 10s。
        接收时立刻开跑，正好与 5s 缓冲窗口重叠 —— 推送时通常已经拿到，
        SSE 接收线程也完全不会被网络 I/O 拖住。

        持有器结构：{"meta", "event", "segment"}。取不到就是 segment=None，
        调用方据此退回纯文本（能力只增不减）。
        """
        holder = {"meta": quote_image, "event": threading.Event(), "segment": None}

        def worker():
            try:
                holder["segment"] = self._fetch_quote_image_segment(
                    session_id_data, quote_image, contact)
            except Exception as e:
                log.warning(f"引用图片后台下载异常: {e}")
            finally:
                holder["event"].set()

        threading.Thread(target=worker, daemon=True).start()
        return holder

    def _await_quote_image_segment(self, holder):
        """等待后台回查结果；超时/失败返回 None（文字照常推送）。"""
        if not isinstance(holder, dict):
            return None
        event = holder.get("event")
        if event is not None:
            event.wait(timeout=config.QUOTE_IMAGE_FETCH_TIMEOUT)
        return holder.get("segment")

    def _fetch_quote_image_segment(self, session_id_data, quote_image, contact):
        """下载被引用的图片，返回 OneBot image 段；失败返回 None。

        只用 svrid 精确回查：md5/aeskey 是微信 CDN 那套加密元数据，
        不经解密拿不到图；而 Alpha 历史里同一条图片消息带的是已 resolve
        的 media URL，直接下就能拿到原字节（实测 87998 字节，与 XML
        `length` 完全一致）。
        """
        svrid = str((quote_image or {}).get("svrid") or "").strip()
        if not svrid:
            log.info("引用图片没有 svrid，无法回查（保留 [图片] 文本）")
            return None
        try:
            b64 = self._fetch_image_base64(
                {"sessionId": session_id_data, "rawid": svrid},
                max_pages=config.QUOTE_IMAGE_SEARCH_PAGES)
        except TypeError:
            # 兼容旧签名（防御性，正常不会走到）
            b64 = self._fetch_image_base64({"sessionId": session_id_data, "rawid": svrid})
        if not b64:
            log.warning(f"引用图片回查失败（svrid={svrid}，保留 [图片] 文本）[{contact}]")
            return None
        log.info(f"🖼️ 引用图片已下载（{len(b64) * 3 // 4 // 1024}KB，"
                 f"svrid={svrid}）[{contact}]")
        return {"type": "image", "data": {"file": f"base64://{b64}"}}

    def _enqueue_image(self, data, is_group, session_id_data, sender_in_group,
                       group_name_raw, source_name):
        """图片获准读取 → 排入对应缓冲条目。

        关键在于：图片和文本用**同一个 buffer_key**（见 _buffer_key），
        所以「图先到、@ 后到」或「@ 先到、图后到」都会合并成同一条推送。
        """
        if is_group:
            base_name = self._resolve_group_name(session_id_data, group_name_raw, source_name)
            contact = base_name
        else:
            base_name = ""
            contact = source_name if source_name != "未知" else (
                state.get_chat_name(session_id_data) or source_name)

        buffer_key = self._buffer_key(session_id_data, sender_in_group, is_group, base_name)
        with self.buffer_lock:
            entry = self._ensure_entry(buffer_key, data, contact, is_group,
                                       base_name, sender_in_group, session_id_data)
            entry["pending_images"].append(data)
            self._schedule_flush(buffer_key, entry)

        log.info(f"🖼️ 图片已排队，将与本次消息一起发送 [{contact}]")

    def process_sender(self, sender_id, version=None):
        """缓冲到期：下载图片、构造 OneBot 事件推送给 AstrBot。"""
        with self.buffer_lock:
            entry = self.pending_buffers.get(sender_id)
            if entry is None:
                return
            if version is not None and entry.get("timer_version", 0) != version:
                return
            msgs = entry["messages"].copy()
            pending_images = entry.get("pending_images", [])
            pending_stickers = entry.get("pending_sticker_segments", [])
            pending_quote_images = entry.get("pending_quote_images", [])
            if (not msgs and not pending_images and not pending_stickers
                    and not pending_quote_images):
                return
            entry["messages"] = []
            entry["pending_images"] = []
            entry["pending_sticker_segments"] = []
            entry["pending_quote_images"] = []
            entry["processing"] = True
            if entry["timer"]:
                entry["timer"].cancel()
                entry["timer"] = None

        contact = entry.get("contact", sender_id)
        is_group = entry.get("is_group", False)
        combined = "\n".join(msgs)
        # 本批是否有人真 @ 过（取出后立刻复位，避免"继承"到下一批）
        mentioned_any = bool(entry.pop("mentioned_any", False))

        # 图片下载放在锁外（网络耗时），转成标准 OneBot image 段所需的 base64。
        # 图片格式用 base64:// —— 与 AstrBot 自身发送图片时一致，不依赖文件路径可达性。
        image_segments = []
        for img_data in pending_images:
            b64 = self._fetch_image_base64(img_data)
            if b64:
                image_segments.append({"type": "image", "data": {"file": f"base64://{b64}"}})
        if pending_images:
            log.info(f"🖼️ 图片 {len(image_segments)}/{len(pending_images)} 张就绪 [{contact}]")

        # 表情包：在 _handle_rich_message 里已下载好（直链有时效，必须即时下），
        # 这里直接把现成的 image 段接在图片后面。
        if pending_stickers:
            image_segments.extend(pending_stickers)
            log.info(f"😀 表情 {len(pending_stickers)} 个就绪 [{contact}]")

        # 引用图片：回查在接收时已并发启动（见 _start_quote_image_fetch），
        # 这里只等结果，网络耗时已与 5s 缓冲窗口重叠。
        for holder in pending_quote_images:
            seg = self._await_quote_image_segment(holder)
            if seg:
                image_segments.append(seg)

        log.info(f"推送 {len(msgs)} 条消息"
                 + (f" + {len(image_segments)} 张图片" if image_segments else "")
                 + f" [{'群' if is_group else '私'}|{contact}]")

        # 构建 OneBot 事件。user_id 用「稳定的个人身份」：
        #   群聊 → 先查群名册把 昵称 解析成 wxid，user_id = md5(wxid)
        #   私聊 → sessionId 本来就是 wxid
        # 这样同一个人在私聊和所有群里是同一个 ID，AstrBot 的 admins_id
        # 加一次即全局生效。名册里查不到时退回旧行为（群ID_昵称）。
        if is_group:
            sender_name = entry.get("sender_in_group", "") or entry.get("source_name", "未知")
            # ① Alpha：事件里直接带 wxid —— 最权威，优先用。
            sender_wxid = (entry.get("sender_wxid") or "").strip()
            if sender_wxid:
                state.remember_person(sender_wxid, sender_name)
            else:
                # ② WeFlow：只有昵称，靠群名册反查 wxid。
                wxid = state.resolve_wxid_in_group(
                    entry.get("session_id_data", ""), sender_name)
                if wxid:
                    sender_wxid = wxid
                    state.remember_person(wxid, sender_name)
                else:
                    sender_wxid = entry.get("session_id_data", "") + "_" + sender_name
                    log.info(f"ℹ️ 群名册中未找到「{sender_name}」，本条退回旧式 ID"
                             f"（该成员的管理员识别要等名册刷新后生效）")
        else:
            sender_name = entry.get("source_name", contact)
            sender_wxid = entry.get("session_id_data", sender_id)
            state.remember_person(sender_wxid, sender_name)
        user_id = state._wxid_to_int(sender_wxid)

        if is_group:
            group_id = state._wxid_to_int(
                entry.get("session_id_data") or entry.get("group_name", contact))

            if state.group_reply_mode == "batch":
                # 批处理模式：消息已预格式化好，直接使用
                formatted = combined
            else:
                # 去掉消息中的 @机器人 纯文本，换为 OneBot at 元素
                clean_text = combined
                for nick in config.BOT_NICKNAMES:
                    at_pattern = f"@{nick}"
                    if at_pattern in clean_text:
                        clean_text = clean_text.replace(at_pattern, "").strip()

                # 指令直通：/sid、/help 这类指令必须让 AstrBot 在**文本最开头**就看到 "/"。
                # 一旦套上「某某在群某某中说：」外壳，AstrBot 收到的文本就不以 "/" 开头，
                # 内置指令会被当成普通聊天丢给大模型（私聊没有这层壳，所以私聊 /sid 一直正常）。
                if clean_text.startswith("/"):
                    formatted = clean_text
                else:
                    formatted = clean_text
                    if sender_name:
                        formatted = f'{sender_name}在群{entry.get("group_name", contact)}中说：{clean_text}'

                # 同人别名标记：这个发言人有昵称/备注两个名字时，
                # 附一个轻量标记，由 AstrBot 侧的 group_alias_note 补丁
                # 提取成「[同名说明]」，避免模型把同一个人当成两位好友
                # （实测：模型曾断言「李四 和 张三 是两位不同的朋友」）。
                # 标记不占正文语义，且补丁会把它从正文里移除。
                if formatted and not formatted.startswith("/"):
                    try:
                        _alts = [a for a in (state.aliases_of(sender_name) or [])
                                 if a and a != sender_name]
                        if _alts:
                            formatted += ("<alias>" + ";".join(
                                f"{a}={sender_name}" for a in sorted(set(_alts))
                            ) + "</alias>")
                    except Exception:
                        pass

            # 消息段顺序：@机器人（仅真 @ 过时）→ 图片 → 文本。
            # 不能无条件加 @：all 模式下非 @ 消息若被硬塞 [At:]，
            # AstrBot 会视为唤醒逐条回复，active_reply 随机插话就形同虚设。
            msg_segments = ([{"type": "at", "data": {"qq": str(state._self_id_int)}}]
                            if mentioned_any else [])
            msg_segments.extend(image_segments)
            if formatted:
                # 普通聊天保留一个前导空格做视觉分隔；指令则严格贴开头，避免任何歧义
                sep = "" if formatted.startswith("/") else " "
                msg_segments.append({"type": "text", "data": {"text": f"{sep}{formatted}"}})
            event = make_message_event("group", user_id, msg_segments,
                                       group_id=group_id,
                                       group_name=entry.get("group_name", contact),
                                       nickname=sender_name)
        else:
            sender_name = _resolve_display_name(entry.get("source_name", contact), contact)
            msg_segments = list(image_segments)
            if combined:
                msg_segments.append({"type": "text", "data": {"text": combined}})
            event = make_message_event("private", user_id, msg_segments,
                                       nickname=sender_name)

        # 记录 user_id → contact 映射，供 API 回复时查找；
        # 同时把「群名 / 成员昵称 / 原始会话 ID」记下来，供 AstrBot 回调
        # get_group_info、get_group_member_info 等只读接口时回答。
        if is_group:
            group_id = state._wxid_to_int(
                entry.get("session_id_data") or entry.get("group_name", contact))
            state.set_contact(group_id, contact)
            state.set_session_id(group_id, entry.get("session_id_data", ""))
            state.set_member_name(user_id, sender_name)
            state.add_group_member(group_id, user_id, sender_name)
        else:
            group_id = 0
            state.set_contact(user_id, contact)
            state.set_session_id(user_id, entry.get("session_id_data", ""))
            state.set_member_name(user_id, entry.get("source_name", contact))

        # 分配 message_id 并记录原文 —— AstrBot 开「引用回复」后
        # 会带 {"type":"reply","data":{"id":N}} 段回来，靠这个回查
        msg_id = state.next_message_id()
        event["message_id"] = msg_id
        state.remember_message(
            msg_id,
            sender_name if is_group else entry.get("source_name", contact),
            combined or "[图片]",
            contact,
            user_id=user_id,
            group_id=group_id,
            is_group=is_group,
        )

        if push_event(event):
            log.info(f"✅ 已推送至 AstrBot [{contact}]")
        else:
            log.warning(f"⚠️ 无 AstrBot 客户端在线 [{contact}]")

        with self.buffer_lock:
            entry = self.pending_buffers.get(sender_id)
            if entry is not None:
                entry["processing"] = False
                # 推送期间可能又来了消息 —— 必须重新安排，否则会一直压在缓冲里
                if (entry["messages"] or entry["pending_images"]
                        or entry.get("pending_quote_images")
                        or entry.get("pending_sticker_segments")):
                    self._schedule_flush(sender_id, entry)

    def listen_sse(self):
        """连接消息源 SSE 推流（WeFlow / Alpha）。"""
        source = create_message_source()
        sse_url = source.sse_url()
        # ⚠️ 日志里绝不打完整 URL：WeFlow 的 URL 里含 access_token 明文。
        # Alpha 的 URL 不含 token，但保持同一策略：只打路径，鉴权一律 Header。
        log.info(f"连接 {source.name.upper()} 推送服务: {source.base_url}/api/v1/push/messages")
        headers = source.sse_headers()

        try:
            self._sse_session = requests.get(
                sse_url, headers=headers, stream=True, timeout=None)
            if self._sse_session.status_code != 200:
                # 只打 code，不打 body —— 有些后端会把 token/内部路径回显在错误里。
                log.error(f"连接失败: HTTP {self._sse_session.status_code}")
                return
            log.info(f"✅ 已连接到 {source.name.upper()} 推送")
            self._prefetch_contact_names()
            # 群名册在后台线程拉取（可能要跑几十秒），不阻塞消息循环。
            # Alpha 群消息 sourceName 是 wxid，必须先有名册才能解析成昵称；
            # 名册未就绪期间消息**不会被丢**，只是暂时显示 wxid。
            threading.Thread(target=self._prefetch_group_rosters, daemon=True).start()
            threading.Thread(target=self._roster_refresh_loop, daemon=True).start()

            # SSE 是「event: xxx / data: {...}」成对出现的；
            # Alpha 的 stream.error 会主动断开连接，WeFlow 也有非 message 事件。
            # 只处理 message.new，其他事件忽略（ready/error 仅为控制信号）。
            current_event = ""
            for line in self._sse_session.iter_lines(decode_unicode=True):
                if not state.running:
                    break
                if not line:
                    current_event = ""
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                # stream.error 的 data 是 {"code":...}，不是消息体；
                # 打出来便于排障（code 无敏感信息），然后让连接关闭/重连。
                if current_event == "stream.error":
                    log.warning(f"⚠️ SSE stream.error: {data_str[:200]}")
                    continue
                if current_event and current_event != "message.new":
                    continue

                try:
                    data = json.loads(data_str)
                    # Alpha 原生字段归一（群 wxid → senderWxid 等）
                    data = source.normalize_event(data)
                    msg_time = data.get("timestamp", 0)
                    if msg_time < self.start_timestamp:
                        continue
                    raw_id = data.get("rawid", "")
                    if raw_id in self.processed_ids:
                        continue
                    self.processed_ids.add(raw_id)
                    self._processed_order.append(raw_id)
                    if len(self._processed_order) > 5000:   # 只留最近 5000 条，防内存持续增长
                        for _old in self._processed_order[:2500]:
                            self.processed_ids.discard(_old)
                        del self._processed_order[:2500]
                    ignore_reason = self.ignore_reason(data)
                    if ignore_reason:
                        self._log_skip(data, ignore_reason)
                    else:
                        # 展示名优先用归一后的 senderName；私聊仍是 sourceName。
                        shown = (data.get("senderName") or data.get("sourceName") or "")
                        log.info(f"📩 收到: {shown} → {data.get('content','')[:50]}")
                        self.add_to_buffer(data)
                except json.JSONDecodeError:
                    pass

        except requests.exceptions.ConnectionError:
            log.error(f"无法连接 {source.name.upper()}")
        except Exception as e:
            # 手动 stop 时 requests 会在 iter_lines 里抛各种「响应已关闭」异常，
            # 这不是故障 —— state.running=False 时静默退出即可。
            if state.running:
                log.error(f"SSE 异常: {e}")
        finally:
            self._sse_session = None

    def _fetch_image_base64(self, data, max_pages: int = 1) -> str | None:
        """取得图片字节并返回 base64。

        顺序：
          1. SSE/历史事件已经带的 `_alphaMediaUrl` —— 最准确，且不会受
             历史列表分页/排序影响；
          2. 回查消息列表，优先按 rawid/platform_message_id/server_id 精确匹配；
          3. 兼容 Alpha 的 `mediaUrl` 与 `media.resources[].url` 两套结构。

        图片大小超过 config.IMAGE_MAX_BYTES 时跳过。下载仍放在网络线程中执行。
        """
        talker = data.get("sessionId", "") or data.get("conversation_id", "")
        rawid = str(data.get("rawid", "") or "") or extract_message_id(data)
        source = create_message_source()
        try:
            media_url = str(data.get("_alphaMediaUrl") or "")

            if not media_url:
                # Alpha 0.2.0 实测：media=true + limit>100 + order=desc 会触发
                # 422 media.hydration_budget_exceeded（媒体注水预算）。50 安全。
                # 有 rawid 时按页找**精确匹配**那条；引用图片可能来自
                # 更早的历史（实测同一页也常见），所以允许多翻几页。
                # 没有 rawid（如「刚发的图」）只看第一页即可。
                pages = max(1, int(max_pages or 1)) if rawid else 1
                media_url = ""
                seen_any = False
                for page in range(pages):
                    payload = source.fetch_messages(
                        talker, limit=50, media=True,
                        # Alpha 默认升序（最旧在前）；要找「刚到的那张」必须倒序。
                        # WeFlow 忽略 order，保持原行为。
                        order="desc",
                        offset=(page * 50) if page else None,
                    )
                    messages = payload if isinstance(payload, list) else payload.get(
                        "messages", payload.get("data", []))
                    if not isinstance(messages, list):
                        messages = []
                    candidates = [m for m in messages if extract_media_url(m)]
                    if not candidates:
                        continue
                    seen_any = True
                    if rawid:
                        exact = [m for m in candidates
                                 if str(extract_message_id(m)) == rawid
                                 or str(m.get("rawid", "")) == rawid
                                 or str(m.get("platform_message_id", "")) == rawid
                                 or str(m.get("server_id", "")) == rawid]
                        if exact:
                            media_url = extract_media_url(exact[0])
                            break
                        # 本页没有 → 继续翻；全部翻完仍没有则放弃。
                        continue
                    media_url = extract_media_url(candidates[0])
                    break
                if not media_url:
                    log.warning(f"取图失败: 消息列表里没有可用图片 (talker={talker}, "
                                f"id={rawid or '-'}, pages={pages}, any={seen_any})")
                    return None

            if not media_url:
                log.warning(f"取图失败: 消息记录没有媒体 URL (talker={talker})")
                return None

            # Alpha: /api/v1/media/...，需 Bearer；WeFlow: 绝对 URL + query token。
            # 统一交给 source.download_media 处理。
            img = source.download_media(media_url)
            if img.status_code != 200:
                log.warning(f"取图失败: 下载 HTTP {img.status_code} (id={rawid or '-'})")
                return None
            if not img.content:
                log.warning(f"取图失败: 图片内容为空 (id={rawid or '-'})")
                return None
            if len(img.content) > config.IMAGE_MAX_BYTES:
                log.warning(f"图片过大（{len(img.content) / 1048576:.1f}MB），已跳过")
                return None

            log.info(f"🖼️ 图片已就绪（{len(img.content) / 1024:.0f}KB，"
                     f"{img.headers.get('Content-Type', '未知类型')}）")
            return base64.b64encode(img.content).decode()
        except Exception as e:
            log.warning(f"取图异常: {e}")
            return None

    # ============ 以下为原版「桥接侧图片描述」路径（config.IMAGE_CAPTION_ENABLED 开启时使用） ============

    def _fetch_wechat_image(self, talker: str) -> str | None:
        """从 WeFlow REST API 获取最新图片并保存到本地"""
        try:
            source = create_message_source()
            data = source.fetch_messages(talker, limit=20, media=True, order="desc")
            messages = data if isinstance(data, list) else data.get("messages", data.get("data", []))
            if not isinstance(messages, list):
                messages = []

            for msg in messages:
                if msg.get("mediaUrl"):
                    img_resp = source.download_media(msg["mediaUrl"])
                    if img_resp.status_code != 200:
                        continue

                    # 根据 Content-Type 确定扩展名
                    ct = img_resp.headers.get("Content-Type", "")
                    ext = ".jpg"
                    if "png" in ct: ext = ".png"
                    elif "gif" in ct: ext = ".gif"
                    elif "webp" in ct: ext = ".webp"

                    filename = f"wechat_{int(time.time())}{ext}"
                    save_dir = os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_images")
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, filename)

                    with open(save_path, "wb") as f:
                        f.write(img_resp.content)

                    log.info(f"✅ 微信图片已保存: {save_path}")
                    return save_path

            log.warning(f"消息列表无图片 mediaUrl (talker={talker})")
            return None
        except Exception as e:
            log.error(f"获取微信图片异常: {e}")
            return None

    def process_image_message(self, data):
        """处理图片消息：从 WeFlow 取图 → ollama 描述 → 注入缓冲区"""
        session_id = data.get("sessionId", "")
        source_name = data.get("sourceName", "") or "未知"
        group_name = data.get("groupName", "")
        rawid = data.get("rawid", "")

        log.info(f"🖼️ 收到图片: {source_name}" +
                 (f" (群:{group_name})" if group_name else ""))

        talker_id = data.get("talkerId", "") or data.get("sessionId", "")
        is_group = bool(group_name) or "@chatroom" in session_id

        # 注册待处理的图片（ollama 完成前标记为 pending）
        img_event = threading.Event()
        self._pending_image[talker_id] = {"caption": None, "event": img_event}

        try:
            # 取图 + ollama 描述
            image_path = self._fetch_wechat_image(session_id)
            caption = None
            if image_path:
                caption = caption_image_via_ollama(image_path)

            caption_text = caption if caption else None
            if caption_text:
                log.info(f"📝 图片描述: {caption_text[:60]}...")
            else:
                log.info("⚠️ 图片描述失败")
                caption_text = "（图片内容无法描述）"

            # 注入图片描述到缓冲区
            with self.buffer_lock:
                self._pending_image[talker_id] = {"caption": caption_text, "event": img_event}

                # 批处理模式用群共享 key
                if is_group and state.group_reply_mode == "batch" and group_name:
                    g_base = re.sub(r'\s*\(\d+\)\s*$', '', group_name).strip()
                    batch_key = f"__batch__{g_base}"
                    if batch_key in self.pending_buffers:
                        entry = self.pending_buffers[batch_key]
                        entry["messages"].insert(0, f'成员"{source_name}"在群"{group_name}"中对你说：[图片: {caption_text}]')
                        entry["image_ready"] = True
                        log.info(f"📝 图片已注入批处理队列")
                        return
                    # 没有文字排队，用 batch key 创建独立条目
                    self.pending_buffers[batch_key] = {
                        "messages": [f'成员"{source_name}"在群"{group_name}"中对你说：[图片: {caption_text}]'],
                        "timer": None,
                        "timer_version": 0,
                        "processing": False,
                        "contact": group_name,
                        "is_group": True,
                        "source_name": source_name,
                        "session_id_data": session_id,
                        "group_name": group_name,
                        "sender_in_group": source_name,
                    }
                    log.info(f"📩 图片无文本跟随，创建批处理图片条目")
                    version = 1
                    timer = threading.Timer(5, lambda v=version, sid=batch_key: self.process_sender(sid, v))
                    timer.daemon = True
                    timer.start()
                    self.pending_buffers[batch_key]["timer"] = timer
                    self.pending_buffers[batch_key]["timer_version"] = version
                elif talker_id in self.pending_buffers:
                    # 已有文本在排队，注入图片上下文
                    entry = self.pending_buffers[talker_id]
                    entry["messages"].insert(0, f"[图片: {caption_text}]")
                    entry["image_ready"] = True
                    log.info(f"📝 图片已注入待处理文本队列")
                else:
                    # 没有文本排队，创建单条图片消息处理
                    log.info(f"📩 图片无文本跟随，直接处理")
                    self.pending_buffers[talker_id] = {
                        "messages": [f"[图片: {caption_text}]"],
                        "timer": None,
                        "timer_version": 0,
                        "processing": False,
                        "contact": group_name if is_group and group_name else source_name,
                        "is_group": is_group,
                        "source_name": source_name,
                        "session_id_data": session_id,
                        "group_name": group_name if is_group else "",
                        "sender_in_group": "",
                    }
                    version = 1
                    timer = threading.Timer(2, lambda v=version, sid=talker_id: self.process_sender(sid, v))
                    timer.daemon = True
                    timer.start()
                    self.pending_buffers[talker_id]["timer"] = timer
                    self.pending_buffers[talker_id]["timer_version"] = version
        finally:
            # 确保 Event 被设置
            img_event.set()


def caption_image_via_ollama(image_path: str) -> str | None:
    """对图片进行文字描述，支持 ollama 和 OpenAI 兼容 API 两种后端。"""
    try:
        import base64
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        if config.IMAGE_CAPTION_PROVIDER == "openai":
            # OpenAI 兼容 API（mimo）
            resp = requests.post(
                f"{config.IMAGE_CAPTION_API_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.IMAGE_CAPTION_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": config.IMAGE_CAPTION_MODEL,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": config.IMAGE_CAPTION_PROMPT},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/jpeg;base64,{img_b64}"
                            }},
                        ],
                    }],
                    "max_tokens": 300,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                caption = resp.json()["choices"][0]["message"]["content"].strip()
                if caption:
                    log.info(f"🖼️ 图片描述: {caption[:80]}...")
                    return caption
            else:
                log.warning(f"mimo 返回 HTTP {resp.status_code}: {resp.text[:200]}")
        else:
            # ollama 原生 API
            resp = requests.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": config.IMAGE_CAPTION_MODEL,
                    "prompt": config.IMAGE_CAPTION_PROMPT,
                    "images": [img_b64],
                    "stream": False,
                },
                timeout=config.OLLAMA_TIMEOUT,
            )
            if resp.status_code == 200:
                caption = resp.json().get("response", "").strip()
                if caption:
                    log.info(f"🖼️ 图片描述: {caption[:80]}...")
                    return caption
            else:
                log.warning(f"ollama 返回 HTTP {resp.status_code}: {resp.text[:100]}")

    except requests.Timeout:
        log.warning(f"图片描述超时 (30s)")
    except Exception as e:
        log.warning(f"图片描述失败: {e}")
    return None
