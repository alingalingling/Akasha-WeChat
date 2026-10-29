"""
OneBot v11 协议处理模块。

包括：
- make_message_event() — 构造 OneBot 消息事件 JSON
- push_event() — 通过 WebSocket 推送事件给 AstrBot
- _handle_ob_api() — 处理 AstrBot 发来的 API 请求（send_msg 等）
- _extract_text() — 从 OneBot message 段提取纯文本
"""

import asyncio
import base64
import json
import os
import re
import tempfile
import time
import logging

import requests

import state
import config

log = logging.getLogger("ob11-bridge")


# ============ 只读查询接口 ============
#
# AstrBot 收到消息后，会回调这些接口来补全上下文：
#   get_group_info         → 群名、群人数
#   get_group_member_info  → 成员昵称（解析 @ 时用）
#   get_group_member_list  → 成员列表（判断群主/管理员）
#   get_stranger_info      → 陌生人昵称
#   get_login_info         → 机器人自己的账号昵称
#
# 桥接这边能提供的是「群显示名」和「见过发言的成员名」；微信侧拿不到的
# （完整成员名单、群人数、角色）就不编造，按协议留空，AstrBot 会自行降级。


def _bot_nickname() -> str:
    """机器人自己显示用的昵称。"""
    try:
        return (config.BOT_NICKNAMES or ["Bot"])[0]
    except Exception:
        return "Bot"


def _member_entry(group_id: int, user_id: int, name: str, role: str = "member") -> dict:
    """按 OneBot 标准字段拼一个群成员对象。

    微信没有「群名片」概念，这里把已知的展示名同时放进 card / nickname，
    这样 AstrBot 一次调用就能拿到名字（否则它还会再调 get_stranger_info）。
    """
    return {
        "group_id": group_id,
        "user_id": user_id,
        "nickname": name or str(user_id),
        "card": name or "",
        "sex": "unknown",
        "age": 0,
        "area": "",
        "join_time": 0,
        "last_sent_time": 0,
        "level": "1",
        "role": role,
        "unfriendly": False,
        "title": "",
    }


def _api_get_login_info(_params: dict) -> dict:
    return {"user_id": state._self_id_int, "nickname": _bot_nickname()}


def _api_get_group_info(params: dict) -> dict:
    gid = int(params.get("group_id") or 0)
    username = state.get_session_id(gid) or ""
    name = state.get_contact(gid) or state.get_chat_name(username) or str(gid)
    # 微信群名可能带 "(13)" 成员数后缀，往回给的时候去掉
    name = re.sub(r"\s*\(\d+\)\s*$", "", name).strip() or str(gid)
    info = {
        "group_id": gid,
        "group_name": name,
        "group_memo": "",
        "group_create_time": 0,
        "group_level": 0,
    }
    # 群人数：只在缓存名里带 "(13)" 这种后缀时才知道，拿不到就不填（不编造）
    count = state.member_count_of_group(username)
    if count:
        info["member_count"] = count
    return info


def _api_get_group_member_info(params: dict) -> dict:
    gid = int(params.get("group_id") or 0)
    uid = int(params.get("user_id") or 0)
    name = state.get_member_name(uid) or ""
    if not name and uid and uid == state._self_id_int:
        name = _bot_nickname()
    return _member_entry(gid, uid, name)


def _api_get_group_member_list(params: dict) -> list:
    gid = int(params.get("group_id") or 0)
    members = state.get_group_members(gid)
    out = [_member_entry(gid, uid, name) for uid, name in members.items()]
    if state._self_id_int and state._self_id_int not in members:
        out.append(_member_entry(gid, state._self_id_int, _bot_nickname()))
    return out


def _api_get_group_list(_params: dict) -> list:
    out = []
    for username, name in state.known_groups().items():
        item = {"group_id": state._wxid_to_int(username), "group_name": name}
        count = state.member_count_of_group(username)
        if count:
            item["member_count"] = count
        out.append(item)
    return out


def _api_get_friend_list(_params: dict) -> list:
    # 微信侧好友列表未接入桥接，返回空（AstrBot 会自行降级）
    return []


def _api_get_stranger_info(params: dict) -> dict:
    uid = int(params.get("user_id") or 0)
    name = state.get_member_name(uid) or ""
    if not name and uid and uid == state._self_id_int:
        name = _bot_nickname()
    return {"user_id": uid, "nick": "", "nickname": name or str(uid),
            "sex": "unknown", "age": 0}


def _api_get_msg(params: dict) -> dict:
    """回查一条已推送的消息（AstrBot 解析「引用」段时会用）。"""
    rec = state.get_message(params.get("message_id"))
    if not rec:
        return {}
    is_group = bool(rec.get("is_group"))
    uid = int(rec.get("user_id") or 0)
    payload = {
        "post_type": "message",
        "message_type": "group" if is_group else "private",
        "message_id": int(params.get("message_id") or 0),
        "user_id": uid,
        "self_id": state._self_id_int,
        "time": int(rec.get("ts") or time.time()),
        "raw_message": rec.get("content") or "",
        "message": [{"type": "text", "data": {"text": rec.get("content") or ""}}],
        "font": 0,
        "sender": {"user_id": uid, "nickname": rec.get("sender") or ""},
    }
    if is_group:
        payload["group_id"] = int(rec.get("group_id") or 0)
    return payload


_READ_API = {
    "get_login_info": _api_get_login_info,
    "get_group_info": _api_get_group_info,
    "get_group_member_info": _api_get_group_member_info,
    "get_group_member_list": _api_get_group_member_list,
    "get_group_list": _api_get_group_list,
    "get_friend_list": _api_get_friend_list,
    "get_stranger_info": _api_get_stranger_info,
    "get_msg": _api_get_msg,
}


# ============ 出站昵称纠错 ============
#
# 现象：模型（尤其是中文语料为主的）会顺手把日语汉字"简体化"，
# 于是群友「広中依緒璃」被叫成「广中依绪璃」。人设里已经下了死命令，
# 但仍会漏，所以在出站前用工整的名册再兜一道。
#
# ⚠️ 踩过的坑（2026-09-17）：这张表是**手写**的，漏一个字整条纠错就静默失效。
# 实测名册里是「広中依緒璃」，表里有「広→广」却**没有「緒→绪」**，
# 于是生成的变体是「广中依緒璃」、而模型实际输出「广中依绪璃」，
# 两者不相等 → 一次都没替换成功（日志里 11 次错名、纠错计数 0）。
# 现在改成两层兜底（见 _build_fuzzy_name_map）：
#   ① 精确变体表（下面这张，覆盖常见日文汉字）
#   ② 逐位"只改其中一位"的模糊匹配 —— 模型只简体化名字里某几个字时也能认。
# 这样再漏字也只是匹配面变窄，不会整个失效。
#
# 安全边界：只有当变体**整串等于**某个已知名字的变形时才替换，
# 所以普通词（"广东""车站""关系"）不在名册里，完全不受影响。
_JA_TO_CN_VARIANT = {
    "広": "广", "沢": "泽", "気": "气", "桜": "樱", "円": "圆", "渋": "涩",
    "転": "转", "辺": "边", "経": "经", "続": "续", "単": "单", "売": "卖",
    "図": "图", "団": "团", "県": "县", "歳": "岁", "雑": "杂", "恵": "惠",
    "徳": "德", "増": "增", "営": "营", "層": "层", "掲": "揭", "拡": "扩",
    "廃": "废", "弐": "贰", "蔵": "藏", "緑": "绿", "軽": "轻", "黒": "黑",
    "歯": "齿", "涼": "凉", "浜": "滨", "験": "验", "険": "险", "権": "权",
    "観": "观", "歓": "欢", "歎": "叹", "歴": "历", "残": "残", "毎": "每",
    "様": "样", "総": "总", "繊": "纤", "継": "继", "締": "缔", "緩": "缓",
    "練": "练", "縁": "缘", "縦": "纵", "縮": "缩", "優": "优", "価": "价",
    "倹": "俭", "倫": "伦", "個": "个", "倣": "仿", "値": "值", "傷": "伤",
    # —— 2026-09-17 补漏：这些之前缺了，导致「広中依緒璃」这条彻底匹配不上。
    #    （「緒→绪」就是本 bug 的直接元凶）
    "緒": "绪", "緖": "绪", "録": "录", "錄": "录", "來": "来", "會": "会",
    "體": "体", "國": "国", "學": "学", "實": "实", "與": "与", "張": "张",
    "陳": "陈", "劉": "刘", "楊": "杨", "黃": "黄", "趙": "赵", "鈴": "铃",
    "羅": "罗", "蘭": "兰", "葉": "叶", "霧": "雾", "靜": "静", "響": "响",
    "樂": "乐", "風": "风", "鳥": "鸟", "魚": "鱼", "馬": "马", "龍": "龙",
    "鳳": "凤", "櫻": "樱", "織": "织", "綾": "绫", "純": "纯", "絢": "绚",
    "結": "结", "繪": "绘", "聲": "声", "聽": "听", "歸": "归", "處": "处",
    "戰": "战", "擊": "击", "術": "术", "醫": "医", "藥": "药", "護": "护",
    "寶": "宝", "宮": "宫", "島": "岛", "嶼": "屿", "嶺": "岭", "峽": "峡",
    "燈": "灯", "營": "营", "愛": "爱", "戀": "恋", "夢": "梦", "緣": "缘",
    "義": "义", "禮": "礼", "萬": "万", "億": "亿", "團": "团", "園": "园",
    "寫": "写", "書": "书", "畫": "画", "銀": "银", "錢": "钱", "鐵": "铁",
    "腦": "脑", "臉": "脸", "腳": "脚", "髮": "发", "膚": "肤", "麗": "丽",
    "豔": "艳", "艷": "艳", "鶯": "莺", "鴉": "鸦", "鵲": "鹊",
}

_name_fix_cache = {"ts": 0.0, "map": {}, "fuzzy": {}}


def _build_name_variant_map() -> dict:
    """{简体化变体: 正确原名}，只收录名册里确实存在的名字。"""
    out = {}
    for name in state.known_display_names():
        if len(name) < 2:
            continue
        variant = "".join(_JA_TO_CN_VARIANT.get(ch, ch) for ch in name)
        if variant != name:
            out[variant] = name
    return out


def _build_fuzzy_name_map() -> dict:
    """{逐位简体化变体: 正确原名} —— 对每个名字生成"只改其中一位"的组合。

    这是为了修「表里漏一个字 → 整条纠错静默失效」那类问题：
    之前只有"全字都转"的单一键，漏一个映射就永远匹配不上。
    现在把每位独立的转换结果也登记进去，匹配面从 1 个键扩到 n 个键。
    实测：真名「広中依緒璃」→ 逐位表能同时认出
    「广中依緒璃」「広中依绪璃」两种"只改了一部分"的写法。
    """
    out = {}
    for name in state.known_display_names():
        if len(name) < 2:
            continue
        chars = list(name)
        for i, ch in enumerate(chars):
            conv = _JA_TO_CN_VARIANT.get(ch)
            if not conv or conv == ch:
                continue
            variant = "".join(chars[:i]) + conv + "".join(chars[i + 1:])
            out.setdefault(variant, name)
    return out


# ── 引号配对保护（patch_quote_split）──
# AstrBot 的 segmented_reply 只按句末标点（。？！~…）切分，不认配对符号。
# 当句末标点出现在引号/括号**内部**时（如「看到你发的“喵喵？”我就知道」），
# 切点会落在配对符号中间，收尾的 ” 被甩到下一条消息。
# 这里在进入发送循环前，把配对不平衡的相邻 text 段合并回一条。
_PAIRED_CHARS = (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"),
                 ("（", "）"), ("《", "》"), ("【", "】"),
                 ("(", ")"), ("[", "]"), ("{", "}"))


def _pair_balance_ok(text):
    """成对符号是否都闭合。返回 (ok, 未闭合的符号对)。"""
    for open_ch, close_ch in _PAIRED_CHARS:
        if text.count(open_ch) != text.count(close_ch):
            return False, f"{open_ch}{close_ch}"
    return True, ""


def _normalize_text_segments(message):
    """合并配对不平衡的相邻 text 段，返回新的消息链。

    只动 text 段；reply/at/image 等段原样保留在各自位置。
    遇到非 text 段会中断当前合并（避免跨图片、跨 @ 强行拼接）。
    """
    if not isinstance(message, list):
        return message
    out = []
    pending = None          # 累积中的文本（配对尚不平衡）
    for seg in message:
        if not isinstance(seg, dict) or seg.get("type") != "text":
            # 非 text 段：先把 pending 落地，再原样保留该段
            if pending is not None:
                out.append({"type": "text", "data": {"text": pending}})
                pending = None
            out.append(seg)
            continue
        raw = (seg.get("data") or {}).get("text", "")
        if pending is None:
            pending = raw
        else:
            pending += raw
        ok, _ = _pair_balance_ok(pending)
        if ok:
            out.append({"type": "text", "data": {"text": pending}})
            pending = None
    if pending is not None:
        out.append({"type": "text", "data": {"text": pending}})
    return out
# ── end patch_quote_split ──


# ── 占位符拦截（patch_placeholder_guard）──
# 明确是"占位符"而非人设发言的短文本。命中即整条丢弃。
_PLACEHOLDER_TEXTS = {
    "角色回复", "角色回复：", "角色回复:",
    "（角色回复）", "(角色回复)",
    "[角色回复]",
    "assistant", "Assistant",
    "<reply>", "</reply>",
}


def _is_placeholder_reply(message) -> bool:
    """整条消息是否只是占位符（没有任何真实文本）。

    只在「所有文本段拼起来恰好是占位符、且没有图片」时才算命中——
    正常的回复里带一句「角色回复」不会误伤。
    """
    if not isinstance(message, list):
        return False
    texts = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        if t == "image":
            return False          # 有图片就不是空占位
        if t == "text":
            texts.append((seg.get("data") or {}).get("text", "") or "")
    joined = "".join(texts).strip()
    if not joined:
        return False
    return joined in _PLACEHOLDER_TEXTS


# 微信媒体占位内容：WeFlow/桥接解析层把表情/图片写成这些占位符。
# 作为**引用目标**时必须跳过（UIA 气泡 Name 同样含占位符，会引错对象）。
_MEDIA_PLACEHOLDER_RE = re.compile(
    r"^\[(表情|图片|动画表情|视频|文件|语音|链接|名片|小程序|聊天记录|消息)\]$"
)


def _is_placeholder_content(content: str) -> bool:
    """内容是否为媒体占位符（如「[表情]」「[图片]」）。"""
    return bool(_MEDIA_PLACEHOLDER_RE.match((content or "").strip()))
# ── end patch_placeholder_guard ──


def fix_display_names(text: str) -> str:
    """把出站文本里被模型简体化的群友名/群名改回来。"""
    if not text:
        return text
    now = time.time()
    if now - _name_fix_cache["ts"] > 300:   # 名册 10 分钟刷一次，这里 5 分钟重建
        _name_fix_cache["map"] = _build_name_variant_map()
        _name_fix_cache["fuzzy"] = _build_fuzzy_name_map()
        _name_fix_cache["ts"] = now
    # 精确表优先（键更长更具体）
    for wrong, right in _name_fix_cache["map"].items():
        if wrong in text:
            text = text.replace(wrong, right)
            log.info(f"[OB11] 昵称纠错：{wrong} → {right}")
    # 逐位表兜底：模型只简体化了名字里某几个字时靠这一层
    for wrong, right in _name_fix_cache["fuzzy"].items():
        if wrong in text:
            text = text.replace(wrong, right)
            log.info(f"[OB11] 昵称纠错(逐位)：{wrong} → {right}")
    return text


async def _handle_ob_api(data: dict):
    """处理 AstrBot 发来的 API 请求。"""
    action = data.get("action", "")
    params = data.get("params", {})
    echo = data.get("echo", "")
    log.info(f"[OB11] API: {action} echo={echo}")

    # 只读查询类：数据必须随响应一起回去，所以在回响应之前先算好
    resp_data = {"status": "ok", "retcode": 0, "data": {}}
    handler = _READ_API.get(action)
    if handler is not None:
        try:
            resp_data["data"] = handler(params)
            log.debug(f"[OB11] {action} -> {json.dumps(resp_data['data'], ensure_ascii=False)[:200]}")
        except Exception as e:
            log.warning(f"[OB11] {action} 查询失败: {e}")

    # 先回响应（必须在处理消息前回，否则 AstrBot 超时）
    resp_sent = False
    if echo:
        resp_data["echo"] = echo
    # 如果 WS 暂时断连，等一会重试
    for retry in range(10):
        try:
            if state._ob_ws:
                await state._ob_ws.send(json.dumps(resp_data, ensure_ascii=False))
                resp_sent = True
                log.info(f"[OB11] 已回响应: {action}")
                break
            if retry < 9:
                await asyncio.sleep(0.5)
        except Exception as e:
            log.warning(f"[OB11] 回响应失败 (重试 {retry}/10): {e}")
            if retry < 9:
                await asyncio.sleep(0.5)
    if not resp_sent:
        log.warning(f"[OB11] 无法回响应（WS 未连接），消息仍尝试本地处理: {action}")

    if action in ("send_msg", "send_private_msg", "send_group_msg"):
        is_group = action == "send_group_msg"
        target_id = params.get("group_id" if is_group else "user_id", 0)
        message = params.get("message", [])
        contact = state.get_contact(target_id, str(target_id))

        # 原始消息链落日志：分条/引用/顺序类问题全靠它定位。
        # （正文里常含换行，排障时务必先合并续行再看，见 dump_bridge_window.py）
        try:
            log.info(f"[OB11] ← {action} target={target_id} chain="
                     f"{json.dumps(message, ensure_ascii=False)[:500]}")
        except Exception:
            pass

        # 引号配对保护：先合并配对不平衡的相邻文本段（见 patch_quote_split）
        message = _normalize_text_segments(message)

        # 占位文案拦截：模型/降级链路偶尔会吐出裸的占位符（不是真正的回复）。
        # 事故（2026-09-20 15:49）：openrouter 主力 404 → 切 agnes fallback 后，
        # 回复正文只有两个字「角色回复」，被原样发到群里。这类显然是占位符，
        # 不可能是人设想说的话，直接丢弃整条（宁可不回，也不要发垃圾）。
        if _is_placeholder_reply(message):
            log.warning(f"[OB11] 拦截占位符回复，已丢弃: "
                        f"{json.dumps(message, ensure_ascii=False)[:120]}")
            return

        # 发送前登记本条链的文本段总数，用于「发送残缺」检测（见 _send_progress）。
        # 事故（2026-09-20 18:10）：AstrBot 把一条长回复拆成 6 个气泡依次下发，
        # 桥接发到第 3 个时进程重启，后 3 个永久丢失——微信群友只看到半句话，
        # 而日志里毫无异常。这类"静默残缺"必须有痕迹。
        _tx = [s for s in message
               if isinstance(s, dict) and s.get("type") == "text"]
        state.begin_send_batch(target_id, len(_tx))

        # 逐段处理：文字和图片分别发送
        # 引用回复（可选）：AstrBot 开启 reply_with_quote 后，回复链开头是
        # {"type":"reply","data":{"id":N}} 段。
        #   · quote_reply_native（默认开）= 微信原生引用气泡：右键原消息 →
        #     菜单「引用」→ 发正文。失败自动降级普通发送，不影响送达。
        #   · quote_reply_prefix = 文本模拟「〔回复 某某：原文…〕」，仅在原生不可用时用。
        # 两者都关则忽略 reply 段，回复为纯文本。
        quote_prefix = None
        quote_target = None          # 原生引用要定位的原消息内容
        at_prefix = ""               # @ 段转成的文字前缀
        for seg in message:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {})

            if seg_type == "reply":
                orig = state.get_message(seg_data.get("id"))
                if config.QUOTE_REPLY_NATIVE:
                    content = (orig.get("content") or "").strip() if orig else ""
                    # 占位内容（[表情]/[图片] 等）不能做原生引用目标：
                    # UIA 里表情/图片气泡的 Name 也含「[表情]」，按内容找
                    # 气泡会命中**任意一个**表情包 —— 2026-09-30 实测把
                    # 引用头错插到无关表情包上。占位符连「引用了哪张」
                    # 都无法校验，宁可放弃引用（降级普通发送）也不引错。
                    if content and not _is_placeholder_content(content):
                        quote_target = content
                    elif content:
                        log.info(f"[OB11] 引用目标为占位内容 {content}，"
                                 f"跳过原生引用")
                elif orig and config.QUOTE_REPLY_PREFIX:
                    snippet = (orig.get("content") or "").replace("\n", " ")[:40]
                    quote_prefix = f"〔回复 {orig.get('sender','?')}：{snippet}〕\n"
                continue

            if seg_type == "at":
                # AstrBot 开 reply_with_mention 时会带 @ 段。微信侧无法做原生引用，
                # 而 UIA 也点不出 @（at 段此前被静默丢弃，导致群回复看不出在回谁）。
                # 这里把它降级成文字「@昵称 」，至少让群里知道在叫谁。
                qq = str(seg_data.get("qq", "")).strip()
                if qq and qq != "all" and config.MENTION_AS_TEXT:
                    try:
                        nm = state.get_member_name(int(qq))
                    except (TypeError, ValueError):
                        nm = None
                    at_prefix = f"@{nm or qq} "
                continue

            if seg_type == "text":
                text = seg_data.get("text", "")
                # 纯空白段直接跳过：AstrBot 分条回复时可能产出只剩空白/换行的段，
                # 发出去就是「空气泡」，还会把它携带的引用/At 一起浪费掉。
                # 跳过时**保留** quote_target / at_prefix，让它们并入下一段正文。
                if text and text.strip():
                    # AstrBot 常在正文前塞一个换行：它先发一个 `" "` 段、
                    # 再发 `"\n正文"` 段（见 bridge_run.log 里
                    # chain=[{reply},{at},{text:" "},{text:"\n…"}]）。
                    # 那个前导换行到微信里就是**空的首行**，气泡顶上多一块空白。
                    # 实测最近一次运行有 8 条这样发出去。这里去掉首尾空白，
                    # 正文中间的换行与缩进保持原样。
                    text = text.strip()
                    text = fix_display_names(text)
                    if quote_prefix:
                        text = quote_prefix + text
                        quote_prefix = None
                    if at_prefix and not text.lstrip().startswith("@"):
                        text = at_prefix + text
                    at_prefix = ""
                    if quote_target:
                        # 原生引用发出（内部失败会自动降级）
                        sent_ok = await asyncio.to_thread(
                            state.sender_instance.send_quote,
                            contact, quote_target, text)
                        quote_target = None
                    else:
                        sent_ok = await asyncio.to_thread(
                            state.sender_instance.send_text, contact, text)
                    if not sent_ok:
                        log.error(f"[OB11] 文字发送失败至 {contact}: {text[:50]}")
                        continue
                    # 记录自己发出的内容：这条消息会被 WeFlow 读回来，
                    # 若不拦截会被当成用户输入再回一遍（自问自答）
                    state.note_sent_text(text)
                    state.note_send_progress(target_id)
                    log.info(f"[OB11] 文字已发送至 {contact}: {text[:50]}")

            elif seg_type == "image":
                file_val = seg_data.get("file", "")
                if not file_val:
                    continue

                img_path = None

                # AstrBot 通过 aiocqhttp 发图片时用 base64:// 格式
                if file_val.startswith("base64://"):
                    try:
                        # 解码 + 写文件在线程池执行，避免大图卡死事件循环
                        b64_data = file_val[9:]
                        img_path = await asyncio.to_thread(_decode_base64_image, b64_data)
                        if img_path:
                            log.info(f"[OB11] 图片已解码: {os.path.basename(img_path)}")
                    except Exception as e:
                        log.warning(f"[OB11] base64 图片解码失败: {e}")
                else:
                    # 文件名模式：在附件目录找
                    if config.ASTRBOT_ATTACHMENTS:
                        candidates = [
                            os.path.join(config.ASTRBOT_ATTACHMENTS, file_val),
                            os.path.join(config.ASTRBOT_ATTACHMENTS, "wechat_images", file_val),
                        ]
                        for p in candidates:
                            if os.path.exists(p):
                                img_path = p
                                break
                        if not img_path:
                            log.warning(f"[OB11] 图片文件未找到: {file_val}")

                if img_path:
                    sent_ok = False
                    try:
                        # 使用线程池执行同步的 UIA 发送，避免阻塞事件循环
                        sent_ok = await asyncio.to_thread(
                            state.sender_instance.send_image, contact, img_path)
                    except Exception as e:
                        log.error(f"[OB11] 图片发送异常 → {contact}: {e}")
                        sent_ok = False

                    # ⚠️ 不能假报成功：send_image 失败时降级为发文件。
                    # （2026-09-19 实测：模型发来 SVG，UIA 发送失败，
                    #   旧代码却照样打印「图片已发送」，用户以为收到了）
                    if sent_ok:
                        log.info(f"[OB11] 图片已发送至 {contact}")
                        _rm = img_path
                    else:
                        log.warning(f"[OB11] 图片发送失败 → 降级为发文件给 {contact}")
                        _rm = None
                        try:
                            file_ok = await asyncio.to_thread(
                                state.sender_instance.send_file, contact, img_path)
                            if file_ok:
                                log.info(f"[OB11] 已作为文件发送至 {contact}")
                                _rm = img_path
                            else:
                                log.error(f"[OB11] 文件降级发送失败 → {contact}")
                        except Exception as e:
                            log.error(f"[OB11] 文件降级发送也失败 → {contact}: {e}")
                    # 只有真正送达（图或文件）才清理临时文件，失败保留供排查
                    if _rm and "tmp" in _rm:
                        try:
                            os.unlink(_rm)
                        except Exception:
                            pass

            elif seg_type == "file":
                # 文件段：AstrBot 发来的 File 组件，file 字段通常是 file:/// URI
                file_val = seg_data.get("file", "") or ""
                fname = seg_data.get("name", "") or ""
                fpath = None
                tmp_made = None

                try:
                    if file_val.startswith("file://"):
                        # file:///C:/... → 本地路径
                        from urllib.parse import unquote, urlparse
                        fpath = unquote(urlparse(file_val).path).lstrip("/") if False else None
                        # Windows: urlparse("file:///C:/x") → path="/C:/x"；
                        # 用 url2pathname 更稳
                        from urllib.request import url2pathname
                        fpath = url2pathname(urlparse(file_val).path)
                    elif file_val.startswith("base64://"):
                        import base64
                        suffix = os.path.splitext(fname)[1] or ".bin"
                        fd = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                        fd.write(base64.b64decode(file_val[9:]))
                        fd.close()
                        fpath = tmp_made = fd.name
                    elif file_val.startswith(("http://", "https://")):
                        suffix = os.path.splitext(fname)[1] or ".bin"
                        fd = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                        fd.close()
                        import urllib.request as _ur
                        _ur.urlretrieve(file_val, fd.name)
                        fpath = tmp_made = fd.name
                    else:
                        fpath = file_val

                    if fpath and os.path.isfile(fpath):
                        file_ok = await asyncio.to_thread(
                            state.sender_instance.send_file, contact, fpath)
                        if file_ok:
                            log.info(f"[OB11] 文件已发送至 {contact}: {os.path.basename(fpath)}")
                        else:
                            log.error(f"[OB11] 文件发送失败至 {contact}: {os.path.basename(fpath)}")
                    else:
                        log.warning(f"[OB11] 文件未找到，无法发送: {file_val[:120]}")
                except Exception as e:
                    log.error(f"[OB11] 文件发送失败: {e}")
                finally:
                    if tmp_made:
                        try:
                            os.unlink(tmp_made)
                        except Exception:
                            pass

            elif seg_type == "face":
                face_ok = await asyncio.to_thread(state.sender_instance.send_text, contact, "[表情]")
                if face_ok:
                    log.info(f"[OB11] 表情已发送至 {contact}")
                else:
                    log.error(f"[OB11] 表情发送失败至 {contact}")

            # 其他类型（record, video 等）忽略

        # 兜底：引用前缀没被任何文字段消费（如纯图片回复），单独发一条
        if quote_prefix:
            prefix_ok = await asyncio.to_thread(state.sender_instance.send_text, contact, quote_prefix.rstrip())
            if prefix_ok:
                log.info(f"[OB11] 引用前缀已单独发送至 {contact}")
            else:
                log.error(f"[OB11] 引用前缀发送失败至 {contact}")

    else:
        log.debug(f"[OB11] 未处理 API: {action}")

    # 注意：API 响应已在函数开头统一发送，此处不再重复


def _extract_text(message: list) -> str:
    """从 OneBot message 段中提取可发送的文本。"""
    text_parts = []
    for seg in message:
        if isinstance(seg, dict):
            t = seg.get("type", "")
            d = seg.get("data", {})
            if t == "text":
                text_parts.append(d.get("text", ""))
            elif t == "image":
                text_parts.append("[图片]")
            elif t == "face":
                text_parts.append("[表情]")
            elif t == "record":
                text_parts.append("[语音]")
            elif t == "video":
                text_parts.append("[视频]")
            elif t == "reply":
                if d.get("text"):
                    text_parts.append(f'"{d["text"]}"')
            elif t == "at":
                text_parts.append(f"@{d.get('qq', d.get('name', ''))}")
            else:
                # 其他未知类型也尝试提取文本
                text_parts.append(d.get("text", ""))
    return "".join(text_parts).strip()


# ============ OneBot 协议处理 ============


def make_message_event(message_type: str, user_id: int, message: list,
                       group_id: int = 0, group_name: str = "",
                       nickname: str = "") -> dict:
    """构造 OneBot v11 消息事件"""
    event = {
        "time": int(time.time()),
        "self_id": state._self_id_int,
        "post_type": "message",
    }
    if message_type == "group":
        event["message_type"] = "group"
        event["group_id"] = group_id
        event["user_id"] = user_id
        event["message"] = message
        event["raw_message"] = "".join(
            seg.get("data", {}).get("text", "") for seg in message
            if seg.get("type") == "text"
        )
        event["sender"] = {"user_id": user_id, "nickname": nickname or str(user_id)}
        event["group_name"] = group_name or str(group_id)
    else:
        event["message_type"] = "private"
        event["user_id"] = user_id
        event["message"] = message
        event["raw_message"] = "".join(
            seg.get("data", {}).get("text", "") for seg in message
            if seg.get("type") == "text"
        )
        event["sender"] = {"user_id": user_id, "nickname": nickname or str(user_id)}
    return event


def push_event(event: dict) -> bool:
    """通过 WebSocket 客户端连接向 AstrBot 推送事件。"""
    if not state._ob_ws or not state._ob_ws_loop:
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(
            state._ob_ws.send(json.dumps(event, ensure_ascii=False)),
            state._ob_ws_loop,
        )
        future.result(timeout=5)
        return True
    except Exception as e:
        log.warning(f"[OB11] 推送事件失败: {e}")
        return False


def _decode_base64_image(b64_data: str) -> str | None:
    """在线程池中执行：解码 base64 图片并保存为临时文件。

    ⚠️ 模型"画画"时可能产出 **SVG**（文本格式）而非位图——
    UIA 发送走 PIL 识别，SVG 会报 `cannot identify image file`（2026-09-19 实测）。
    所以解码后探测魔数：
      · PNG/JPEG/GIF/WebP 位图 → 原样落盘
      · SVG（`<?xml`/`<svg`）→ 用本机 Chromium headless 渲染成 PNG；
        渲染失败 → 返回 .svg 原文件，调用方降级为发文件（不静默丢弃）
    """
    import tempfile
    img_data = base64.b64decode(b64_data)
    head = img_data[:256].lstrip()
    is_svg = (head.startswith(b"<?xml") or head.startswith(b"<svg")
              or b"<svg" in img_data[:1024])

    if not is_svg:
        if head.startswith(b"\xff\xd8"):
            suffix = ".jpg"
        elif head.startswith(b"GIF8"):
            suffix = ".gif"
        elif head.startswith(b"RIFF") and img_data[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            suffix = ".png"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp.write(img_data)
        tmp.close()
        return tmp.name

    # ---- SVG：先落 .svg，再尝试渲染成 PNG ----
    tmp = tempfile.NamedTemporaryFile(suffix=".svg", delete=False)
    tmp.write(img_data)
    tmp.close()
    png_path = _render_svg_to_png(tmp.name)
    if png_path:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        return png_path
    return tmp.name        # 渲染失败：交回 .svg，调用方按"发文件"降级


def _render_svg_to_png(svg_path: str) -> str | None:
    """用本机 Chromium headless 把 SVG 渲染成 PNG。

    为什么选浏览器渲染：机器上没有 cairosvg/inkscape/ffmpeg，
    但 Playwright 的 Chromium 一定在（此前验证面板时已装），且对
    SVG 的字体/渐变保真度最好。返回 PNG 路径；失败返回 None。
    """
    import subprocess
    import glob as _glob
    from PIL import Image   # 仅用于校验渲染产物确实是位图

    cands = []
    pw = sorted(_glob.glob(os.path.expanduser(
        r"~\AppData\Local\ms-playwright\chromium-*\chrome-win64\chrome.exe")))
    if pw:
        cands.append(pw[-1])
    cands += [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]

    out_png = svg_path.rsplit(".", 1)[0] + "_render.png"
    # 从 SVG 里解析宽高（如 width="320" height="420" 或 viewBox），按比例设窗口；
    # 解析不出就用默认 800x1000
    w, h = 800, 1000
    try:
        import re as _re
        head = open(svg_path, "rb").read(4096).decode("utf-8", "ignore")
        m = _re.search(r'<svg[^>]*?width="([\d.]+)"[^>]*?height="([\d.]+)"', head)
        if m:
            w, h = max(1, int(float(m.group(1)))), max(1, int(float(m.group(2))))
        else:
            mv = _re.search(r'viewBox="[\d.\-]+[ ,]+[\d.\-]+[ ,]+([\d.]+)[ ,]+([\d.]+)"', head)
            if mv:
                w, h = max(1, int(float(mv.group(1)))), max(1, int(float(mv.group(2))))
    except Exception:
        pass
    for exe in cands:
        if not os.path.exists(exe):
            continue
        try:
            subprocess.run(
                [exe, "--headless", "--disable-gpu", "--hide-scrollbars",
                 f"--window-size={w},{h}",
                 f"--screenshot={out_png}", svg_path],
                capture_output=True, timeout=30,
            )
            if os.path.exists(out_png) and os.path.getsize(out_png) > 0:
                with Image.open(out_png) as im:
                    im.verify()   # 确认是可识别的位图
                return out_png
        except Exception:
            continue
    return None
