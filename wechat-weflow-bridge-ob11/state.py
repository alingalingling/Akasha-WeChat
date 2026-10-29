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
import time
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
    """将微信 wxid / 会话标识映射为**跨进程稳定**的整数 ID。

    不要用内置 hash()：Python 对 str 的 hash 每个进程都会随机化
    （PYTHONHASHSEED），桥接一重启，同一个联系人就会算出不同的 ID，
    AstrBot 会把新 ID 当成一个全新会话 —— 这正是面板里「每个私聊/群聊
    都出现两遍」的原因。这里改用 MD5 前 8 字节，重启后结果不变。
    """
    if not wxid:
        return 0
    digest = hashlib.md5(wxid.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (2 ** 31)


# ============ 桥接实例 / 发送器 ============

bridge_instance = None
bridge_lock = threading.Lock()
sender_instance = None
_ob_id_to_contact: dict[int, str] = {}  # OneBot user_id/group_id → 微信联系名
_contact_lock = threading.Lock()


def set_contact(ob_id, contact: str) -> None:
    """记录 OneBot ID → 微信联系名映射（供回复时定位）。"""
    with _contact_lock:
        _ob_id_to_contact[ob_id] = contact


def get_contact(ob_id, default=None):
    """按 OneBot ID 取微信联系名。

    优先查直连映射表；查不到时**反查名册**（2026-09-15 事故修复）：
    定时任务/跨会话发送只知道 OneBot ID，而 AstrBot 发来的 group_id/user_id
    正是本模块 `_wxid_to_int()` 算出来的整数。如果映射表是空的（重启后尚未
    收到该会话的消息就会这样），以前会退化成"拿原始 ID 当联系人名"→ 切窗口
    必然失败 → 消息发到当时打开的那个聊天。这里补一条反查，保证拿得到真名。
    """
    with _contact_lock:
        hit = _ob_id_to_contact.get(ob_id)
    if hit:
        return hit
    try:
        want = int(ob_id)
    except (TypeError, ValueError):
        return default
    # ① 群：chatroom 的整数 ID → 群名
    for room, name in known_groups().items():
        try:
            if _wxid_to_int(room) == want and name:
                return name
        except Exception:
            continue
    # ② 人：私聊的整数 ID → 常用名
    #    注意：微信聊天标题显示的是**备注名**（WeFlow contacts 的 displayName），
    #    昵称（nickname）只在搜索结果里出现。这里必须优先用聊天标题那个名字，
    #    否则会出现"搜索切到了 李四，标题却是 张三"的错位。
    for wxid, info in all_persons().items():
        try:
            if info.get("uid") != want:
                continue
        except Exception:
            continue
        nm = get_chat_name(wxid)
        if not nm:
            names = info.get("names") or []
            nm = names[0] if names else None
        if nm:
            return nm
    # ③ 兜底：任何已知会话名（含「文件传输助手」这类**不是联系人**的会话）。
    #    这类会话不在名册里，以前会退化成裸 ID → 发送时去搜索一串数字
    #    （实测 06:00 的早安任务就出现了 "会话列表未找到 '1085276398'"）。
    for wxid, name in list(_chat_names.items()):
        if not name:
            continue
        try:
            if _wxid_to_int(wxid) == want:
                return name
        except Exception:
            continue
    return default


# ============ 只读 API 支撑数据（群信息 / 成员昵称） ============
#
# AstrBot 收到消息后会回调 get_group_info / get_group_member_info /
# get_group_member_list / get_stranger_info 来补全「群名、成员昵称、管理员」。
# 桥接这边能提供的是：群显示名（联系人缓存）、以及**见过发言的成员**的名字。
# 拿不到的（成员总数、角色）就不编造。

_ob_id_to_session: dict[int, str] = {}      # OneBot group_id/user_id → 微信 username（xxx@chatroom / wxid）
_member_names: dict[int, str] = {}          # OneBot user_id → 展示名
_group_members: dict[int, dict] = {}        # OneBot group_id → {user_id: name}
_api_lock = threading.Lock()


def set_session_id(ob_id, session_id: str) -> None:
    """记录 OneBot ID → 微信原始 username（回查群名/会话用）。"""
    if ob_id and session_id:
        with _api_lock:
            _ob_id_to_session[ob_id] = session_id


def get_session_id(ob_id) -> Optional[str]:
    """按 OneBot ID 取微信原始 username。"""
    with _api_lock:
        return _ob_id_to_session.get(ob_id)


def set_member_name(user_id, name: str) -> None:
    """记录 OneBot user_id → 展示名（群成员与好友通用）。"""
    if user_id and name:
        with _api_lock:
            _member_names[user_id] = name


def get_member_name(user_id) -> Optional[str]:
    """按 OneBot user_id 取展示名。"""
    with _api_lock:
        return _member_names.get(user_id)


def add_group_member(group_id, user_id, name: str) -> None:
    """记录「这个群里见过这个成员」，供 get_group_member_list 使用。"""
    if not group_id or not user_id:
        return
    with _api_lock:
        members = _group_members.setdefault(group_id, {})
        members[user_id] = name or str(user_id)


def get_group_members(group_id) -> dict:
    """取该群已知成员 {user_id: name}（只含发过言的，不含完整名单）。"""
    with _api_lock:
        return dict(_group_members.get(group_id, {}))


def known_groups() -> dict:
    """取联系人缓存里所有群会话 {微信 username: 显示名}。"""
    with _api_lock:
        names = dict(_chat_names)
    return {k: v for k, v in names.items() if "@chatroom" in k}


def all_chat_names() -> dict:
    """取联系人缓存里的**全部**会话 {微信 username: 显示名}。

    与 known_groups 的区别：这个包含私聊好友、以及「文件传输助手 / 微信团队 /
    公众号 / 微信支付」这类**非好友的特殊会话** —— 它们不在 persons.json（那里
    只装好友），只有这张表才有名字。UMO 别名解析要靠它兜底。
    """
    with _api_lock:
        return dict(_chat_names)


def member_count_of_group(username: str) -> int:
    """从缓存的群名里抠出成员数（微信群名常带 "(13)" 后缀）；没有则 0。"""
    if not username:
        return 0
    name = _chat_names.get(username) or ""
    m = _re.search(r"\((\d+)\)\s*$", name)
    return int(m.group(1)) if m else 0


ob_client_started = False
ob_thread = None       # OneBot 客户端线程引用（停止时用来等待它退出）

# ============ 消息 ID 与「引用回复」支持 ============
#
# AstrBot 开启 reply_with_quote 后，回复会带 {"type":"reply","data":{"id":N}} 段。
# 桥接必须：① 给每条收到的消息分配 message_id ② 记住 id → 原文映射
# ③ 发送时把 reply 段翻译成可读文本前缀（微信 UIA 无法原生引用回复）。

import time as _time

_msg_id_seq = 0
_msg_store: dict[int, dict] = {}          # message_id → {"sender","content","contact","ts"}
_MSG_STORE_MAX = 500                      # 最多记住最近 500 条，防止内存无限增长
_msg_lock = threading.Lock()


def next_message_id() -> int:
    """分配一条自增的 OneBot message_id（进程内唯一即可）。"""
    global _msg_id_seq
    with _msg_lock:
        _msg_id_seq += 1
        return _msg_id_seq


def remember_message(mid: int, sender: str, content: str, contact: str,
                     user_id=None, group_id=None, is_group: bool = False) -> None:
    """记录一条已推送消息，供后续 reply 段 / get_msg 回查。"""
    with _msg_lock:
        _msg_store[int(mid)] = {
            "sender": sender or "?",
            "content": (content or "")[:120],
            "contact": contact,
            "user_id": user_id,
            "group_id": group_id,
            "is_group": bool(is_group),
            "ts": _time.time(),
        }
        if len(_msg_store) > _MSG_STORE_MAX:
            for k in sorted(_msg_store)[: len(_msg_store) - _MSG_STORE_MAX]:
                _msg_store.pop(k, None)


def get_message(mid) -> Optional[dict]:
    """按 message_id 回查原消息；查不到返回 None。"""
    try:
        with _msg_lock:
            return _msg_store.get(int(mid))
    except (TypeError, ValueError):
        return None


# ============ 自己发出的内容（拦截微信侧回声） ============
#
# 桥接通过 UIA 发出的消息，WeFlow 也会读到并推回来。如果不记录，
# 机器人会把自己的回复当成用户输入再回一遍（自问自答循环）。

_recent_sent: dict[str, float] = {}
_recent_sent_lock = threading.Lock()
_RECENT_SENT_MAX = 500


# ── 发送批次进度（检测"静默残缺"） ─────────────────────────────────
# AstrBot 会把一条长回复拆成多个 send_msg 依次下发；桥接逐个发到微信。
# 若中途进程重启/异常，剩余气泡会永久丢失，而日志里没有任何错误。
# 事故（2026-09-20 18:10）：6 个气泡只发出 3 个就重启，群友只看到半句话。
# 这里记录"本批应收/已发"，退出时把未发完的批次汇总写进 exit_reason 日志。
_send_batch_lock = threading.Lock()
_send_batches: dict = {}          # target_id -> {"expected": n, "done": m, "ts": t}


def begin_send_batch(target_id, expected: int) -> None:
    """登记一个发送批次。"""
    with _send_batch_lock:
        _send_batches[target_id] = {
            "expected": int(expected), "done": 0, "ts": _time.time()
        }


def note_send_progress(target_id) -> None:
    """标记该批次又成功发出一个文本段。"""
    with _send_batch_lock:
        b = _send_batches.get(target_id)
        if b:
            b["done"] += 1


def incomplete_batches() -> list:
    """返回尚未发完的批次（expected > done）。"""
    with _send_batch_lock:
        return [
            {"target": t, "expected": b["expected"], "done": b["done"],
             "age": round(_time.time() - b["ts"], 1)}
            for t, b in _send_batches.items()
            if b["done"] < b["expected"]
        ]


def note_sent_text(text: str) -> None:
    """记录一条刚由桥接发出的文本。"""
    key = (text or "").strip()[:200]
    if not key:
        return
    now = _time.time()
    with _recent_sent_lock:
        _recent_sent[key] = now
        if len(_recent_sent) > _RECENT_SENT_MAX:
            for k, ts in list(_recent_sent.items()):
                if now - ts > 300:
                    _recent_sent.pop(k, None)


def was_sent_recently(text: str, window: float = 120) -> bool:
    """这段文本是不是桥接自己在 window 秒内发出去的（用于丢弃回声）。"""
    key = (text or "").strip()[:200]
    if not key:
        return False
    with _recent_sent_lock:
        ts = _recent_sent.get(key, 0)
    return ts > 0 and (_time.time() - ts) <= window


# ============ 群聊 @ 追踪 + 图片暂存 ============
#
# 语义：群图片只在「与同一个人的 @ 属于同一次请求」时才读取。
#   - @ 之前发的图：暂存在 (群, 发送者) 名下，等同一人发 @ 文本时取出并入同一次推送
#   - @ 之后紧跟着的图：recent_group_mention 命中 → 立即读取
# 换一个人 @ 不会消费别人暂存的图。

_group_mentions: dict[tuple, float] = {}      # (session_id, sender) → 最后 @ 时刻
_group_image_stash: dict[tuple, list] = {}    # (session_id, sender) → [(ts, data)]


def note_group_mention(session_id: str, sender: str) -> None:
    """记录：该群里这个人刚刚 @ 了机器人。"""
    if session_id:
        _group_mentions[(session_id, sender or "")] = time.time()


def recent_group_mention(session_id: str, sender: str, window_seconds: float) -> bool:
    """这个人在该群里 window_seconds 秒内是否 @ 过机器人。"""
    if not session_id:
        return False
    ts = _group_mentions.get((session_id, sender or ""), 0)
    return ts > 0 and (time.time() - ts) <= window_seconds


def stash_group_image(session_id: str, sender: str, data: dict, max_keep: int = 3) -> None:
    """暂存群图片，等待同一人随后 @ 时消费。每人每群最多留 max_keep 张（新的挤掉旧的）。"""
    key = (session_id, sender or "")
    lst = _group_image_stash.setdefault(key, [])
    lst.append((time.time(), data))
    while len(lst) > max_keep:
        lst.pop(0)


def pop_group_images(session_id: str, sender: str, window_seconds: float) -> list:
    """取出这个人在该群里暂存、且仍在有效期内的图片（取出即清空）。"""
    key = (session_id, sender or "")
    lst = _group_image_stash.pop(key, [])
    now = time.time()
    return [d for ts, d in lst if (now - ts) <= window_seconds]


# ============ 联系人名称缓存（WeFlow username/chatroom → 显示名） ============
#
# WeFlow 的 SSE 推送里 groupName / sourceName 偶发缺失（部分群或系统消息），
# 桥接会退化成用 sessionId（xxx@chatroom）当联系人名，AstrBot 面板于是显示原始 ID。
# 这里维护一份持久化映射：启动时从 WeFlow /api/v1/contacts 全量预取，
# 之后每条消息带来的名字都会回写缓存，字段再缺失也能查到。

import json as _json
import os as _os
import re as _re

_chat_names: dict = {}
_chat_names_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "chat_names.json")


def _load_chat_names() -> None:
    global _chat_names
    try:
        with open(_chat_names_path, encoding="utf-8") as f:
            _chat_names = {str(k): str(v) for k, v in _json.load(f).items()}
    except Exception:
        _chat_names = {}


def _save_chat_names() -> None:
    try:
        _os.makedirs(_os.path.dirname(_chat_names_path), exist_ok=True)
        with open(_chat_names_path, "w", encoding="utf-8") as f:
            _json.dump(_chat_names, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _is_bad_name(name: str) -> bool:
    """无法当联系人名用的值：空、chatroom 原始 ID 形态。"""
    if not name or not name.strip():
        return True
    name = name.strip()
    return "@chatroom" in name or "@openim" in name


def set_chat_name(username: str, name: str) -> bool:
    """记录/更新联系人显示名；坏值拒绝写入。返回是否真的更新了。"""
    if not username or _is_bad_name(name):
        return False
    name = name.strip()
    if _chat_names.get(username) != name:
        _chat_names[username] = name
        _save_chat_names()
        return True
    return False


def get_chat_name(username: str) -> Optional[str]:
    """查缓存的显示名；查不到返回 None。"""
    if not username:
        return None
    return _chat_names.get(username)


_load_chat_names()

# ============ 人员注册表（wxid → 展示名）与群成员名册 ============
#
# 目标：让「同一个人」在私聊和所有群里拥有**同一个** OneBot user_id，
# 这样 AstrBot 的 admins_id 只需要加一次，管理员身份就处处生效。
#   - 稳定身份 = 微信 wxid（WeFlow /api/v1/group-members 提供）
#   - user_id  = md5(wxid)（与私聊侧规则一致）
# SSE 推送在群里只给昵称（sourceName），不给 wxid，所以靠群名册做
# 「昵称 → wxid」解析；解析失败时退回旧行为（群ID_昵称）。

_persons: dict[str, dict] = {}          # wxid → {"names": [常用名在前], "uid": int}
_persons_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "persons.json")


def _load_persons() -> None:
    global _persons
    try:
        with open(_persons_path, encoding="utf-8") as f:
            raw = _json.load(f)
        _persons = {}
        for wxid, p in raw.items():
            if not wxid or not isinstance(p, dict):
                continue
            _persons[wxid] = {
                "names": [str(n) for n in p.get("names", []) if n][:6],
                "uid": int(p.get("uid", _wxid_to_int(wxid))),
            }
    except Exception:
        _persons = {}


def _save_persons() -> None:
    try:
        _os.makedirs(_os.path.dirname(_persons_path), exist_ok=True)
        with open(_persons_path, "w", encoding="utf-8") as f:
            _json.dump(_persons, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def remember_person(wxid: str, name: str) -> None:
    """记录一个人的稳定身份与展示名（供面板「成员与权限」展示）。

    只收真实 wxid：合成键（群ID_昵称）含 @chatroom，不入册。
    """
    if not wxid or "@" in wxid:
        return
    with _api_lock:
        p = _persons.setdefault(wxid, {"names": [], "uid": _wxid_to_int(wxid)})
        p["uid"] = _wxid_to_int(wxid)
        name = (name or "").strip()
        if name and name not in p["names"]:
            p["names"].insert(0, name)
            del p["names"][6:]
        _save_persons()


def all_persons() -> dict:
    """全部已知人员 {wxid: {"names": [...], "uid": int}}（副本）。"""
    with _api_lock:
        return {k: dict(v) for k, v in _persons.items()}


def known_display_names() -> set:
    """全部已知展示名（人名候选名 + 各群成员名 + 群名）。

    供出站文本做「昵称纠错」：模型常把日语汉字自动简体化（広→广），
    叫错群友名字。只有能对应到已知名字时才纠正，避免误伤普通文字。
    """
    out = set()
    with _api_lock:
        for p in _persons.values():
            for n in (p.get("names") or []):
                if isinstance(n, str) and n.strip():
                    out.add(n.strip())
        for r in _group_rosters.values():
            for n in (r.get("names") or {}):
                if isinstance(n, str) and n.strip():
                    out.add(n.strip())
        for n in _chat_names.values():
            if isinstance(n, str) and n.strip():
                out.add(n.strip())
    return out


_group_rosters: dict[str, dict] = {}    # chatroom → {"names": {候选名: wxid}, "ts": 更新时刻}

# 别名表：主名 → 该人的其它叫法（昵称/群昵称/微信号…）
#
# 为什么需要它（2026-09-19 实测）：
#   同一个人有**昵称**和**备注**两个名字，而这两个名字会从两条不同通道
#   进入模型的上下文 ——
#     · 他自己的消息：桥接用备注名（如 张三）标注 sender
#     · 别人 @ 他：微信自动填的是**昵称**（如 李四），原样透传进上下文
#   于是同一分钟、同一个群里，同一个人显示成两个名字，且没有任何标记
#   说明是同一人 → 模型据此断言"李四 和 张三 是两位不同的朋友"。
#
#   实测该群 68 名成员里有 6 人昵称≠备注（含「爸爸/An帝y哥」「妈妈/Purple」），
#   所以这不是个例。WeFlow 的 /api/v1/group-members 同时给出
#   `nickname`（昵称）与 `remark`（备注），可据此**自动**生成别名表。
_aliases: dict[str, set] = {}           # 主名 → {其它叫法}
_alias_display: dict[str, str] = {}     # 其它叫法 → 主名（反查用）


def set_group_roster(chatroom: str, members: list) -> None:
    """写入某个群的成员名册（WeFlow /api/v1/group-members 的 members）。

    为每个成员登记多个候选名（群昵称/展示名/昵称/备注/微信号），
    群消息的 sourceName 命中任意一个都能解析出 wxid。
    同时登记「主名 ↔ 别名」，供上下文注入别名表（见 _aliases 说明）。
    """
    if not chatroom or not isinstance(members, list):
        return
    names: dict[str, str] = {}
    alias_pairs: list = []
    for m in members:
        if not isinstance(m, dict):
            continue
        wxid = (m.get("wxid") or "").strip()
        if not wxid:
            continue
        for key in ("groupNickname", "displayName", "nickname", "remark", "alias"):
            nm = (m.get(key) or "").strip()
            if nm:
                names.setdefault(nm, wxid)
        # 主名优先取备注（remark），与 displayName 一致；否则退群昵称/昵称
        primary = ((m.get("remark") or m.get("groupNickname")
                    or m.get("displayName") or m.get("nickname")) or "").strip()
        others = []
        for key in ("nickname", "groupNickname", "displayName", "alias"):
            nm = (m.get(key) or "").strip()
            if nm and nm != primary:
                others.append(nm)
        if primary and others:
            alias_pairs.append((primary, others))
    with _api_lock:
        _group_rosters[chatroom] = {"names": names, "ts": time.time()}
        for primary, others in alias_pairs:
            bucket = _aliases.setdefault(primary, set())
            for o in others:
                bucket.add(o)
                _alias_display.setdefault(o, primary)


def alias_table() -> dict:
    """{主名: [别名…]} —— 供群上下文注入，让模型知道"这俩是同一个人"。

    只返回真正存在歧义的条目（至少一个别名，且别名≠主名）。
    """
    with _api_lock:
        return {k: sorted(v) for k, v in _aliases.items() if v}


def primary_name(name: str) -> str:
    """把一个可能的名字解析成主名；不是别名则原样返回。"""
    if not name:
        return name
    with _api_lock:
        return _alias_display.get(name.strip(), name)


def aliases_of(name: str) -> list:
    """返回某主名的所有别名（不含主名自身）。没有则返回空列表。"""
    if not name:
        return []
    with _api_lock:
        return sorted(_aliases.get(name.strip(), set()))


def resolve_wxid_in_group(chatroom: str, display_name: str) -> Optional[str]:
    """按展示名在群名册里找 wxid；找不到返回 None。"""
    if not chatroom or not display_name:
        return None
    with _api_lock:
        roster = _group_rosters.get(chatroom)
    if not roster:
        return None
    return roster["names"].get(display_name.strip())


def roster_stats() -> dict:
    """{chatroom: 成员解析名条数}，供面板/日志观察。"""
    with _api_lock:
        return {k: len(v["names"]) for k, v in _group_rosters.items()}


def resolve_name_in_group(chatroom: str, wxid: str) -> str:
    """按 wxid 在群名册里反查可读名；找不到返回空串。

    Alpha 群消息 sourceName 给的是 wxid，不是昵称（与 WeFlow 相反）。
    名册里存的是「名字 → wxid」，没有反向索引，所以这里线性扫一遍。
    群成员最多几千，收消息时逐条扫也能接受；真正慢的话再建反向 dict。
    """
    if not chatroom or not wxid:
        return ""
    with _api_lock:
        roster = _group_rosters.get(chatroom)
    if not roster:
        return ""
    for name, sid in (roster.get("names") or {}).items():
        if sid == wxid:
            return name
    return ""


_load_persons()

# ============ 会话回复开关（面板可控静音） ============
#
# AstrBot 原生没有「按群关闭回复」的开关（白名单是全局的），所以在桥接层实现：
# 被静音的会话（群/私聊）的所有消息——包括 @ 机器人——都在桥接就被丢弃，
# 机器人完全不掺和该会话。适合"一个群里有多个机器人"的场景。

_muted_sessions: set = set()
_muted_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "muted_sessions.json")


def _load_muted_sessions() -> None:
    global _muted_sessions
    try:
        with open(_muted_path, encoding="utf-8") as f:
            _muted_sessions = {str(x) for x in _json.load(f)}
    except Exception:
        _muted_sessions = set()


def _save_muted_sessions() -> None:
    try:
        _os.makedirs(_os.path.dirname(_muted_path), exist_ok=True)
        with open(_muted_path, "w", encoding="utf-8") as f:
            _json.dump(sorted(_muted_sessions), f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def is_session_muted(session_id: str) -> bool:
    """该会话（微信群 sessionId / wxid）是否被面板关闭了回复。"""
    return bool(session_id) and session_id in _muted_sessions


def set_session_muted(session_id: str, muted: bool) -> None:
    if not session_id:
        return
    if muted:
        _muted_sessions.add(session_id)
    else:
        _muted_sessions.discard(session_id)
    _save_muted_sessions()


def muted_session_list() -> list:
    return sorted(_muted_sessions)


_load_muted_sessions()

# 群聊回复模式（运行时可变，启动时从 config 初始化）
group_reply_mode = "all"
