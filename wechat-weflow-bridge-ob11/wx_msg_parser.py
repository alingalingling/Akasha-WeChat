"""微信消息统一解析层（方案 B）。

## 为什么需要它

WeFlow 推来的微信消息里，非文本类型的 `content` 是一坨**微信原生 XML**
（`<msg><appmsg>...`），不是纯文本。改动之前桥接的行为是：

- 认得出 `[图片]` / `[语音]` / `[表情]` 这几个 WeFlow 已归一化的占位符；
- 其余（链接分享、文件、合并转发、转账、系统消息）**整坨 XML 原样当文本**，
  或者解析不出内容就丢弃。

后果：bot 要么看到一堆 XML 标签，要么**根本不知道有人发了东西**。
实测（2026-09-23）最典型的是表情包 —— 群里最高频的非文本消息，
`ignore_reason()` 里一句 `"语音/表情占位内容"` 直接丢弃，bot 完全无感。

## 这个模块做什么

把 `content` 归一化成结构化的 `ParsedMessage`：

    kind        归一化类型（text/image/sticker/link/file/forward/system/...）
    text        给模型看的**人话**描述（永远非空，除非该类型确实无内容）
    sticker_md5 表情包的内容哈希（微信原生 emoticonmd5，天然去重键）
    sticker_url 表情包下载直链（从 emojiinfo 解 base64 得到，有时效）
    url         链接地址（link 类型）
    file_name   文件名（file 类型）

## 关于表情包的「见一次就认识」

`emoticonmd5` 是微信对表情内容的哈希 —— **同一个表情永远同一个 md5**。
这让我们可以做内容寻址缓存：

    首次见到 → 下载 GIF → 模型分析一次 → 记 {md5: 描述}
    以后再见 → 直接命中 → 0 次模型调用、0 次下载

缓存的落库与查表在 AstrBot 侧插件完成（`astrbot_plugin_wx_sticker_cache`），
本模块只负责**把 md5 和下载直链解析出来**，不做网络请求、不碰磁盘。

## 实测证据（2026-09-23）

表情包 XML 形如：

    <appmsg><type>8</type>
      <appattch>
        <emoticonmd5>40ac0a2bf0bf7825a6ad849947801b25</emoticonmd5>
        <totallen>1168271</totallen>
        <fileext>pic</fileext>
      </appattch>
      <emojiinfo>CiA0MGFjMGEy...（base64，内含 CDN 下载直链）</emojiinfo>
    </appmsg>

下载 `emojiinfo` 里解出的 URL：HTTP 200、1168271 字节（与 `<totallen>` 一致）、
文件头 `GIF89a`、300x300 —— 可直接当图片使用。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Optional


# ============ 归一化类型常量 ============
# 用字符串而非 Enum，方便日志阅读与 JSON 序列化。

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_STICKER = "sticker"      # 微信表情包（appmsg type=8）
KIND_LINK = "link"            # 链接分享（type=4/5）
KIND_FILE = "file"            # 文件（type=6）
KIND_FORWARD = "forward"      # 合并转发（type=19）
KIND_QUOTE = "quote"          # 引用回复（appmsg type=57）
KIND_SYSTEM = "system"        # 系统消息（拍了拍/撤回/转账等）
KIND_VOICE = "voice"
KIND_VIDEO = "video"
KIND_UNKNOWN = "unknown"


# ============ appmsg type 码 → 语义 ============
# 来源：实测抓取本项目 30 个会话、近 9000 条真实消息归纳（2026-09-23）。
APPMSG_TYPES = {
    4: (KIND_LINK, "链接分享"),
    5: (KIND_LINK, "公众号/分享"),
    6: (KIND_FILE, "文件"),
    8: (KIND_STICKER, "表情包"),
    19: (KIND_FORWARD, "合并转发"),
    57: (KIND_QUOTE, "引用回复"),
    36: (KIND_LINK, "小程序/分享"),
    51: (KIND_SYSTEM, "系统提示"),
    53: (KIND_LINK, "接龙"),
    62: (KIND_SYSTEM, "拍了拍"),
    87: (KIND_SYSTEM, "系统提示"),
    2000: (KIND_SYSTEM, "转账"),
}

# 微信原生 msg type 码（非 appmsg 包装的那一类）
WX_TYPES = {
    3: (KIND_IMAGE, "图片"),
    34: (KIND_VOICE, "语音"),
    43: (KIND_VIDEO, "视频"),
    47: (KIND_STICKER, "表情包"),
    49: (KIND_LINK, "appmsg 包装"),
    10000: (KIND_SYSTEM, "系统消息"),
}

# 系统消息关键词：这些内容的出现意味着「不是人类发言」，不该进模型上下文。
# 事故（2026-09-20 16:49）：bot 把「"耳东亭氵川" 拍了拍 "辞星" 的ass」
# 当成一条待回应的发言，还认真"解读"了一遍发到群里。
SYSTEM_KEYWORDS = (
    "拍了拍",
    "撤回了一条消息",
    "邀请",
    "加入了群聊",
    "移出了群聊",
    "退出了群聊",
    "开启了朋友验证",
    "你已添加了",
    "以上是打招呼的内容",
)

# 常见内置表情的 emoji 短码（微信自带的 [微笑][捂脸] 等）。
# 这些是**文本**形式出现在 content 里的，不用下载、不用模型。
_BUILTIN_EMOJI_RE = re.compile(r"\[(微笑|撇嘴|色|发呆|得意|流泪|害羞|闭嘴|睡|大哭|尴尬|发怒|调皮|呲牙|惊讶|难过|酷|冷汗|抓狂|吐|偷笑|可爱|白眼|傲慢|饥饿|困|惊恐|流汗|憨笑|大兵|奋斗|咒骂|疑问|嘘|晕|折磨|衰|骷髅|敲打|再见|擦汗|抠鼻|鼓掌|糗大了|坏笑|左哼哼|右哼哼|哈欠|鄙视|委屈|快哭了|阴险|亲亲|吓|可怜|菜刀|西瓜|啤酒|篮球|乒乓|咖啡|饭|猪头|玫瑰|凋谢|示爱|爱心|心碎|蛋糕|闪电|炸弹|刀|足球|瓢虫|便便|月亮|太阳|礼物|拥抱|强|弱|握手|胜利|抱拳|勾引|拳头|差劲|爱你|NO|OK|爱情|飞吻|跳跳|发抖|怄火|转圈|磕头|回头|跳绳|挥手|激动|街舞|献吻|左太极|右太极|微笑|愉快|捂脸|奸笑|机智|皱眉|耶|吃瓜|加油|汗|天啊|Emm|社会社会|旺柴|好的|打脸|哇|翻白眼|666|让我看看|叹气|苦涩|裂开|敲打|让我看看|生病|吐舌|鬼魂|合十|大怨种|思考|叮|庆祝|烟花|福|红包|發|鸡|可爱)\]")


@dataclass
class ParsedMessage:
    """一条微信消息的归一化结果。"""

    kind: str = KIND_TEXT
    text: str = ""                      # 给模型看的人话描述（尽量非空）
    raw: str = ""                       # 原始 content（排查问题用）

    # 表情包专属
    sticker_md5: str = ""
    sticker_url: str = ""
    sticker_size: int = 0               # 下载直链返回的字节数（totallen）

    # 链接 / 文件专属
    url: str = ""
    title: str = ""
    file_name: str = ""
    file_ext: str = ""

    # 合并转发专属
    summary: str = ""

    # 引用回复专属
    quote_text: str = ""
    quote_sender: str = ""

    # 系统消息专属
    system_kind: str = ""               # 拍了拍 / 撤回 / 入群 ...
    is_bot_target: bool = False         # 系统消息是否指向机器人自己

    extra: dict = field(default_factory=dict)

    @property
    def is_ignorable(self) -> bool:
        """是否属于「不该进模型上下文」的消息。

        系统消息（拍了拍、撤回、入群提示）不是人类发言 —— 但**要注意**：
        这跟早期的 `ignore_reason()` 不同，那个把表情包也一并丢了。
        表情包是真实的人类表达，必须保留。
        """
        return self.kind == KIND_SYSTEM


def _unescape(s: str) -> str:
    """微信 XML 里常见 CDATA 包裹与实体转义。"""
    if not s:
        return ""
    s = s.strip()
    if s.startswith("<![CDATA[") and s.endswith("]]>"):
        s = s[9:-3]
    # 只处理最常见的几个实体，避免过度解码破坏正文
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'")
             .replace("&amp;", "&"))


def _tag(xml: str, name: str) -> str:
    """取第一个 <name>...</name> 的内容（非贪婪）。"""
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
    return _unescape(m.group(1)) if m else ""


def _raw_tag(xml: str, name: str) -> str:
    """取第一个 <name>...</name> 的**原始**内容（不做实体解码）。

    与 `_tag` 的区别：`_tag` 会把 `&lt;` 解成 `<`。引用消息的
    `<refermsg><content>` 里嵌套了一整段被转义的微信 XML，解码后
    其中的 `<img>` 等标签会"浮"到外层，再去做 `_tag` 查找就可能
    误命中嵌套标签。需要在外层结构里找字段（如 `svrid`）时必须用
    原始文本。
    """
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
    return m.group(1) if m else ""


def _attr(xml: str, tag: str, name: str) -> str:
    """取标签属性，兼容 `name="x"` 与 `name = "x"` 两种微信写法。"""
    quoted = r"""["']([^"']*)["']"""
    # 优先精确限定属性名，避免 androidmd5 反向匹配 md5。
    pattern = rf"<{tag}\b[^>]*?\b{re.escape(name)}\s*=\s*{quoted}"
    m = re.search(pattern, xml, re.I | re.S)
    if m:
        return _unescape(m.group(1))

    # 兜底：微信 XML 属性间可能有换行；先截取标签再匹配。
    tag_match = re.search(rf"<{tag}\b([^>]*)>", xml, re.I | re.S)
    if not tag_match:
        return ""
    attrs = tag_match.group(1)
    m = re.search(rf"\b{re.escape(name)}\s*=\s*{quoted}", attrs, re.I | re.S)
    return _unescape(m.group(1)) if m else ""


def _appmsg_type(xml: str) -> Optional[int]:
    """取 <appmsg> 里的 <type>N</type>。

    注意：必须限定在 appmsg 内部 —— 顶层 `<msg>` 里也有别的数字标签，
    直接搜 `<type>` 会误命中。
    """
    m = re.search(r"<appmsg[^>]*>(.*?)</appmsg>", xml, re.S)
    scope = m.group(1) if m else xml
    t = re.search(r"<type>(\d+)</type>", scope)
    if not t:
        return None
    try:
        return int(t.group(1))
    except ValueError:
        return None


def _decode_emojiinfo(xml: str) -> tuple[str, str]:
    """从 <emojiinfo> 解出表情下载直链。

    微信把下载地址放在 `<emojiinfo>` 里的 **base64** 文本中，
    解码后是若干条 URL（实测第一条即可用，返回的字节数与 <totallen> 一致）。

    Returns:
        (url, md5) —— 任一解不出就返回空串。
    """
    raw_b64 = _tag(xml, "emojiinfo")
    if not raw_b64:
        return "", ""
    try:
        decoded = base64.b64decode(raw_b64).decode("utf-8", "ignore")
    except Exception:
        return "", ""

    urls = re.findall(r"https?://[^\s\x00-\x1f\"'<>]+", decoded)
    md5 = _tag(xml, "emoticonmd5")
    if not md5:
        # md5 也可能只存在于 emojiinfo 解出的内容里
        m = re.search(r"\b([0-9a-f]{32})\b", decoded)
        md5 = m.group(1) if m else ""
    return (urls[0] if urls else ""), md5


def _parse_sticker(xml: str) -> ParsedMessage:
    """解析表情包（appmsg type=8 / wx type=47）。"""
    md5 = _tag(xml, "emoticonmd5")
    url, url_md5 = _decode_emojiinfo(xml)
    if not md5:
        md5 = url_md5
    try:
        size = int(_tag(xml, "totallen") or 0)
    except ValueError:
        size = 0

    p = ParsedMessage(kind=KIND_STICKER, raw=xml,
                      sticker_md5=md5, sticker_url=url, sticker_size=size)
    # text 先留空 —— 由上层（缓存插件）填入描述。
    # 拿不到描述时用兜底文案，至少让模型知道「有人发了个表情」。
    p.text = "[表情]"
    return p


def _parse_link(xml: str) -> ParsedMessage:
    """解析链接分享（appmsg type=4/5/36/53）。"""
    title = _tag(xml, "title")
    des = _tag(xml, "des")
    url = _tag(xml, "url")
    p = ParsedMessage(kind=KIND_LINK, raw=xml, title=title, url=url)

    parts = []
    if title:
        parts.append(f"分享：{title}")
    if des:
        # des 可能是多行摘要，压成一行避免撑爆上下文
        parts.append(" ".join(des.split())[:200])
    if url:
        parts.append(url)
    p.text = " | ".join(parts) if parts else "[分享内容]"
    return p


def _parse_file(xml: str) -> ParsedMessage:
    """解析文件（appmsg type=6）。"""
    title = _tag(xml, "title")
    ext = _tag(xml, "fileext")
    try:
        size = int(_tag(xml, "totallen") or 0)
    except ValueError:
        size = 0
    p = ParsedMessage(kind=KIND_FILE, raw=xml, title=title,
                      file_name=title, file_ext=ext)
    if title:
        mb = f"{size / 1048576:.1f}MB" if size else ""
        p.text = f"发送了文件：{title}" + (f"（{mb}）" if mb else "")
    else:
        p.text = "[文件]"
    return p


def _extract_quote_image_meta(quote_text: str, refer_raw: str = "") -> dict:
    """从被引用消息的内嵌 XML 里取出图片元数据。

    背景（2026-10-29 实测）：微信引用一张图片时，`<refermsg><content>`
    里是「发送者 wxid: + 一坨被 `&lt;` 转义的图片 XML」，形如::

        wxid_xxx:
        <?xml version="1.0"?>
        <msg><img aeskey="..." cdnbigimgurl="305f02..." length="87998"
                  md5="3398762cea..." ...></msg>

    旧的 `_parse_quote` 只判断 `re.search(r"<img\b")` 就写死
    `[图片]`，md5 / length / svrid 全部丢掉 —— 结果桥接无法回查下载，
    AstrBot 收到的只有文本段，「识图」永远不触发（bot 只能答
    "看不清"）。

    这里只负责**取元数据**：`text` 结构保持不变（仍是 `[图片]`），
    下载由 bridge_core 完成。取不到就返回 {} —— 解析层永不因此抛错。
    """
    if not quote_text or not re.search(r"<img\b", quote_text, re.I):
        return {}
    try:
        try:
            size = int(_attr(quote_text, "img", "length") or 0)
        except (TypeError, ValueError):
            size = 0
        url_kind = ""
        for kind in ("cdnbigimgurl", "cdnmidimgurl", "cdnthumburl"):
            if _attr(quote_text, "img", kind):
                url_kind = kind
                break
        return {
            # 原消息 server_id：Alpha 历史接口用它精确回查那条图片消息
            "svrid": _tag(refer_raw, "svrid") if refer_raw else "",
            "md5": _attr(quote_text, "img", "md5"),
            "aeskey": _attr(quote_text, "img", "aeskey"),
            "size": size,
            "url_kind": url_kind,
        }
    except Exception:
        return {}


def _parse_quote(xml: str) -> ParsedMessage:
    """解析引用回复（appmsg type=57）。

    Alpha 推送的是微信原生 XML：用户自己的新回复在 <title>，
    被引用内容在 <refermsg><content>，引用者显示名在
    <refermsg><displayname>。旧解析不认识 type 57，会降成
    “[未识别消息 type=57]”，模型只能看到 title 片段。
    """
    title = _tag(xml, "title")
    quote_text = _tag(xml, "content")
    quote_sender = _tag(xml, "displayname")
    # refer_raw 必须在 quote_text 之外单独取：content 解码后内部标签会浮上来。
    refer_raw = _raw_tag(xml, "refermsg")
    p = ParsedMessage(kind=KIND_QUOTE, raw=xml, text=title,
                      quote_text=quote_text, quote_sender=quote_sender)

    # 被引用的是图片 → 记下 svrid/md5，供上层回查并下载成 image 段。
    # 注意：这里**只是记录**，text 仍然是 `[图片]`，行为不降级。
    quote_image = _extract_quote_image_meta(quote_text, refer_raw)
    if quote_image:
        p.extra["quote_image"] = quote_image

    if quote_text:
        if re.search(r"<(msg|appmsg|img)\b", quote_text, re.I):
            if re.search(r"<img\b", quote_text, re.I):
                ref_desc = "[图片]"
            elif re.search(r"<videomsg\b", quote_text, re.I):
                ref_desc = "[视频]"
            elif re.search(r"<voice\b", quote_text, re.I):
                ref_desc = "[语音]"
            elif _tag(quote_text, "title"):
                ref_desc = _tag(quote_text, "title")
            else:
                ref_desc = "[富媒体消息]"
        else:
            ref_desc = " ".join(quote_text.split())
        if len(ref_desc) > 160:
            ref_desc = ref_desc[:160] + "…"
        sender = quote_sender or "某人"
        prefix = f"〔引用 {sender}：{ref_desc}〕"
        p.text = prefix + ("\n" + title if title else "")
    elif title:
        p.text = title
    else:
        p.text = "[引用消息]"
    return p


def _parse_forward(xml: str) -> ParsedMessage:
    """解析合并转发（appmsg type=19）。

    `<des>` 里已经是「说话人: 内容」的多行摘要 —— 直接可用，
    实测能拿到真实的聊天记录文本。
    """
    title = _tag(xml, "title") or "聊天记录"
    des = _tag(xml, "des")
    p = ParsedMessage(kind=KIND_FORWARD, raw=xml, title=title, summary=des)
    if des:
        # 限长：转发记录可能很长，避免吃掉整个上下文预算
        lines = [ln.strip() for ln in des.splitlines() if ln.strip()]
        body = "\n".join(lines[:30])
        if len(lines) > 30:
            body += f"\n（共 {len(lines)} 条，已截断）"
        p.text = f"转发了一段{title}：\n{body}"
    else:
        p.text = f"转发了一段{title}"
    return p


def _strip_html(s: str) -> tuple[str, bool]:
    """剥掉内嵌 HTML 标签，保留可见文字。

    微信的一些系统提示是「纯文本 + <a href> 链接」的混合体（安全提醒、公告等）。
    这类内容不该把标签原样喂给模型。

    Returns:
        (清理后的文本, 是否原本含标签)
    """
    if "<" not in s:
        return s, False
    had = bool(re.search(r"</?\w+[^>]*>", s))
    if not had:
        return s, False
    # <br> / </p> 之类的换行标签先转成换行，避免文字粘连
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    # 其余标签直接去掉，保留标签之间的文字
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip(), True


def _parse_system(xml: str, bot_nicknames: tuple = ()) -> ParsedMessage:
    """解析系统消息（拍了拍 / 撤回 / 转账 / 入群等）。

    入参**既可能是 XML、也可能已经是纯文本**（调用方解出 <content> 后直接传进来）。
    早期版本无脑调 _tag()，导致纯文本输入取不到 title 而得到空 text ——
    撤回消息因此在日志里显示为空（2026-09-23 实测抓到）。
    """
    if "<" in xml and re.search(r"<\w+[^>]*>", xml):
        title = _tag(xml, "title")
        des = _tag(xml, "des")
        body = title or des
    else:
        body = xml.strip()
    p = ParsedMessage(kind=KIND_SYSTEM, raw=xml, text=body)

    if "拍了拍" in body:
        p.system_kind = "拍了拍"
        # 拍的是不是机器人自己？群里「拍了拍机器人」其实是一种互动，
        # 但历史上被误当成发言，所以这里只做标记，是否忽略交给上层。
        p.is_bot_target = any(n and n in body for n in bot_nicknames)
    elif "撤回" in body:
        p.system_kind = "撤回"
    elif "转账" in body or "收到转账" in body:
        p.system_kind = "转账"
    elif any(k in body for k in ("加入了群聊", "移出了群聊", "邀请")):
        p.system_kind = "群成员变动"
    else:
        p.system_kind = "其他系统消息"
    if not p.text:
        p.text = f"[系统消息:{p.system_kind}]"
    return p


def _parse_top_level_emoji(xml: str) -> ParsedMessage:
    """解析顶层 `<msg><emoji ...>` 自定义表情。

    2026-10-21 Alpha 历史/推送实测：自定义表情不总走 appmsg type=8，
    也会直接给 `<msg><emoji ... md5=... cdnurl=...></emoji></msg>`。
    旧入口虽把 emoji 列为 XML 标签，却没有对应分支，最终落到
    `[无法解析的消息]`。这里同时兼容：
      · md5/androidmd5
      · cdnurl/encrypturl/externurl
      · len/androidlen
    """
    md5 = (_attr(xml, "emoji", "md5")
           or _attr(xml, "emoji", "androidmd5")
           or _attr(xml, "emoji", "s60v3md5"))
    url = (_attr(xml, "emoji", "cdnurl")
           or _attr(xml, "emoji", "externurl")
           or _attr(xml, "emoji", "encrypturl"))
    try:
        size = int(_attr(xml, "emoji", "len")
                   or _attr(xml, "emoji", "androidlen")
                   or _attr(xml, "emoji", "s60v3len") or 0)
    except ValueError:
        size = 0

    p = ParsedMessage(kind=KIND_STICKER, raw=xml, text="[表情]",
                      sticker_md5=md5, sticker_url=url, sticker_size=size)
    return p


def parse_content(content: str, bot_nicknames: tuple = ()) -> ParsedMessage:
    """把微信原始 content 解析成结构化消息。

    这是本模块的**唯一入口**。永不抛异常 —— 解析失败一律降级为
    `KIND_TEXT` 并把原文放进 text，保证「再差也能当普通文本处理」，
    不会因为一个新消息类型就把消息整个丢掉。

    Args:
        content: WeFlow 推来的 content 字段（可能是纯文本，也可能是 XML）
        bot_nicknames: 机器人昵称列表，用于判断系统消息是否指向自己

    Returns:
        ParsedMessage
    """
    if not content or not content.strip():
        return ParsedMessage(kind=KIND_TEXT, text="", raw=content or "")

    s = content.strip()

    # ---------- 1. WeFlow 已归一化的占位符 ----------
    # 这几个是 WeFlow 明确告诉我们「这是什么」的，优先采信，不走 XML 解析。
    if s == "[图片]":
        return ParsedMessage(kind=KIND_IMAGE, text="[图片]", raw=s)
    if s == "[语音]":
        return ParsedMessage(kind=KIND_VOICE, text="[语音]", raw=s)
    if s == "[表情]":
        # 没有 md5 的表情占位符 —— 常见于微信内置小黄脸。
        # 属于真实表达，必须保留（对比：改动前会被整条丢弃）。
        return ParsedMessage(kind=KIND_STICKER, text="[表情]", raw=s)
    if s == "[视频]":
        return ParsedMessage(kind=KIND_VIDEO, text="[视频]", raw=s)
    if s == "[文件]":
        return ParsedMessage(kind=KIND_FILE, text="[文件]", raw=s)

    # ---------- 2. XML 消息 ----------
    # 入口判断：**只认微信已知的 XML 根标签**，而不是笼统的「含尖括号」——
    # 否则用户随手打的 `<3` 或 `<html>` 会被当 XML 解析。
    #
    # 注意必须包含 `sysmsg`：撤回/入群提示的根标签是 `<sysmsg>`，
    # 它**不含 `<msg` 字样**，早期用 `<msg in s` 判断导致 16 条撤回消息
    # 把整坨 XML 漏进正文（2026-09-23 实测抓到）。
    if re.search(r"<(msg|sysmsg|appmsg|emoji)\b", s):
        try:
            # ---------- 系统消息优先 ----------
            # 微信系统提示有两种包装，实测（2026-09-23）都会出现：
            #   ① <sysmsg type="revokemsg">  撤回 / 入群 / 拍一拍
            #   ② <appmsg><type>62/51/…    拍了拍 / 转账 / 系统提示
            # 早期只认 ②，导致 16 条撤回消息把整坨 XML 漏给模型。
            if "<sysmsg" in s:
                body = _tag(s, "content") or _tag(s, "title") or _tag(s, "des")
                if body:
                    return _parse_system(body, bot_nicknames)
                # sysmsg 里没有 content/title/des（少见）：至少把 type 属性带出来，
                # 便于日志排查，不给模型用。
                m = re.search(r"<sysmsg[^>]*\btype=\"([^\"]+)\"", s)
                st = m.group(1) if m else "unknown"
                return ParsedMessage(kind=KIND_SYSTEM, raw=s,
                                     text=f"[系统消息:{st}]", system_kind=st)

            # 顶层自定义表情：必须在 appmsg 判定之前处理。
            # 形态是 <msg><emoji ...></emoji></msg>，没有 <appmsg>。
            if "<emoji" in s and "<appmsg" not in s:
                return _parse_top_level_emoji(s)

            atype = _appmsg_type(s)

            # 系统关键词：只有标题/描述命中才算（避免正文里恰好提到）
            if any(k in s for k in SYSTEM_KEYWORDS):
                head = _tag(s, "title") or _tag(s, "des")
                if head and any(k in head for k in SYSTEM_KEYWORDS):
                    return _parse_system(s, bot_nicknames)

            if atype is not None:
                kind = APPMSG_TYPES.get(atype)
                if kind:
                    k, _label = kind
                    if k == KIND_STICKER:
                        return _parse_sticker(s)
                    if k == KIND_LINK:
                        return _parse_link(s)
                    if k == KIND_FILE:
                        return _parse_file(s)
                    if k == KIND_FORWARD:
                        return _parse_forward(s)
                    if k == KIND_QUOTE:
                        return _parse_quote(s)
                    if k == KIND_SYSTEM:
                        return _parse_system(s, bot_nicknames)
                # 未知 appmsg 类型：尽量取出 title 当文本，别丢
                title = _tag(s, "title")
                return ParsedMessage(
                    kind=KIND_UNKNOWN, raw=s,
                    text=f"[未识别消息 type={atype}]" + (f" {title}" if title else ""),
                    extra={"appmsg_type": atype},
                )

            # 有 XML 但没 appmsg：可能是系统提示（撤回/入群）
            if any(k in s for k in SYSTEM_KEYWORDS):
                return _parse_system(s, bot_nicknames)

            # 认不出的 XML：不要把标签喂给模型，抽 title/des 当兜底
            title = _tag(s, "title") or _tag(s, "des")
            if title:
                return ParsedMessage(kind=KIND_UNKNOWN, raw=s, text=title)
            return ParsedMessage(kind=KIND_UNKNOWN, raw=s, text="[无法解析的消息]")

        except Exception:
            # 解析层永不抛异常 —— 出错就按原文当普通文本
            return ParsedMessage(kind=KIND_TEXT, text=s, raw=s)

    # ---------- 3. 纯文本 ----------
    # 有些系统提示是「纯文本 + 内嵌 HTML 链接」的混合体，例如微信安全提醒：
    #   当前账号存在安全风险，部分功能暂时无法正常使用，<a href="https://...">查看详情</a>
    # 这类内容既不是 XML 消息，也不该把 <a href> 原样喂给模型。剥掉标签保留文字。
    clean, had_tags = _strip_html(s)
    p = ParsedMessage(kind=KIND_TEXT, text=clean, raw=s)
    if had_tags:
        # 含 HTML 的文本多半是系统通知，而不是人类发言 —— 标记出来供上层判断
        p.extra["had_html"] = True
    # 文本里含内置表情（[微笑] 等）时标记出来 —— 上层可以用它判断
    # 「这条消息带表情」，但它们本身已经是可读文本，不需要额外处理。
    emojis = _BUILTIN_EMOJI_RE.findall(clean)
    if emojis:
        p.extra["builtin_emojis"] = emojis
    return p
