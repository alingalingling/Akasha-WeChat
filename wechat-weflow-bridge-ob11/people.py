"""成员与管理员管理：面板数据源 + 与 AstrBot 配置（cmd_config.json）的双向同步。

管理员模型（v1.0.1-rc.3 起）：
  - 桥接维护 data/admins.json（真实 wxid 列表）作为唯一事实来源；
  - user_id = md5(wxid)，同一个人在私聊和所有群里是同一个 ID，
    所以管理员只需登记一次，处处生效；
  - 保存时自动把 wxid 映射成 UID 写进 AstrBot 的 admins_id。
    注意：AstrBot 只在启动时把 admins_id 读进内存对象，
    写文件后需重启 AstrBot（或在它的 WebUI 里再存一次）才生效。
"""

import json
import logging
import os

import config
import state

log = logging.getLogger("ob11-bridge")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")


# ============ 管理员名单（桥接侧） ============


def load_admins() -> list:
    """读取管理员 wxid 列表（保持顺序、去重）。"""
    try:
        with open(ADMINS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("wxids", [])
        return [str(w).strip() for w in (data or []) if str(w).strip()]
    except Exception:
        return []


def save_admins(wxids) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    uniq = list(dict.fromkeys(str(w).strip() for w in wxids if str(w).strip()))
    with open(ADMINS_FILE, "w", encoding="utf-8") as f:
        json.dump(uniq, f, ensure_ascii=False, indent=1)


# ============ AstrBot 配置文件读写（保留 BOM） ============


def _astrbot_cfg_path() -> str:
    return getattr(config, "ASTRBOT_CONFIG_FILE", "") or ""


def _read_astrbot_cfg():
    """返回 (cfg dict, 是否带 BOM)。文件不存在抛 FileNotFoundError。"""
    path = _astrbot_cfg_path()
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"AstrBot 配置不可用: {path or '(未配置)'}")
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return json.loads(raw.decode("utf-8-sig")), bom


def _write_astrbot_cfg(cfg, bom: bool) -> None:
    path = _astrbot_cfg_path()
    data = json.dumps(cfg, ensure_ascii=False, indent=4).encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    with open(path, "wb") as f:
        f.write(data)


def _plugin_blacklist_path() -> str:
    """插件自有的主动回复黑名单文件，不随 AstrBot 配置迁移丢失。"""
    cfg = _astrbot_cfg_path()
    if not cfg:
        return ""
    return os.path.join(os.path.dirname(cfg), "plugin_data",
                        "akasha_rc_patches", "active_reply_blacklist.json")


def _plugin_data_path(filename: str) -> str:
    """Locate a file owned by akasha_rc_patches under AstrBot plugin data."""
    cfg = _astrbot_cfg_path()
    if not cfg:
        return ""
    return os.path.join(os.path.dirname(cfg), "plugin_data",
                        "akasha_rc_patches", filename)


def _humanizer_style_override_path() -> str:
    return _plugin_data_path("humanizer_style_overrides.json")


def _read_json_file(path: str, default):
    if not path or not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"读取 JSON 失败（{path}）: {e}")
        return default


def _write_json_atomic(path: str, data: dict) -> None:
    if not path:
        raise FileNotFoundError("AstrBot 配置路径不可用")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _humanizer_config_path() -> str:
    cfg = _astrbot_cfg_path()
    if not cfg:
        return ""
    return os.path.join(os.path.dirname(cfg), "config",
                        "astrbot_plugin_wanna_be_human_config.json")


def _humanizer_styles_dir() -> str:
    cfg = _astrbot_cfg_path()
    if not cfg:
        return ""
    return os.path.join(os.path.dirname(cfg), "plugin_data",
                        "astrbot_plugin_wanna_be_human", "styles")


def _read_humanizer_styles() -> list:
    path = _humanizer_styles_dir()
    if not path or not os.path.isdir(path):
        return []
    names = []
    try:
        for fn in sorted(os.listdir(path)):
            if not fn.endswith(".json"):
                continue
            data = _read_json_file(os.path.join(path, fn), {}) or {}
            name = str(data.get("name") or "").strip()
            if name:
                names.append(name)
    except Exception as e:
        log.warning(f"读取人类风格档案失败：{e}")
    return list(dict.fromkeys(names))


def _read_humanizer_style_overrides() -> dict:
    data = _read_json_file(_humanizer_style_override_path(), {}) or {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return {}
    return {str(k).strip(): str(v or "").strip()
            for k, v in sessions.items() if str(k).strip() and str(v or "").strip()}


def _write_humanizer_style_overrides(sessions: dict) -> None:
    """Validate and atomically save per-session style choices.

    Clearing an empty override file remains possible while the humanizer is
    temporarily unavailable.  Selecting a real style still requires the style
    directory so a stale or misspelled profile cannot be persisted.
    """
    styles = set(_read_humanizer_styles())
    clean = {}
    for umo, style in (sessions or {}).items():
        umo, style = str(umo).strip(), str(style or "").strip()
        if not umo or not style or style == "__global__":
            continue
        if not styles:
            raise RuntimeError("未找到人类风格档案（AstrBot 或风格插件未就绪）")
        if style not in styles:
            raise ValueError(f"风格 {style} 不存在")
        clean[umo] = style
    _write_json_atomic(_humanizer_style_override_path(), {"sessions": clean})


def _read_plugin_blacklist() -> list:
    path = _plugin_blacklist_path()
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("entries") or []
        return [str(x).strip() for x in (data or []) if str(x).strip()]
    except Exception as e:
        log.warning(f"读取主动回复黑名单失败：{e}")
        return []


def _write_plugin_blacklist(entries) -> None:
    path = _plugin_blacklist_path()
    if not path:
        raise FileNotFoundError("AstrBot 配置路径不可用")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    data = {"entries": list(dict.fromkeys(
        str(x).strip() for x in (entries or []) if str(x).strip()))}
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _platform_id(cfg: dict) -> str:
    """从 AstrBot 配置里找 aiocqhttp 平台的平台 ID（拼 UMO 用）。"""
    platforms = cfg.get("platform") or []
    if isinstance(platforms, dict):
        platforms = [platforms]
    for p in platforms:
        if isinstance(p, dict) and "aiocqhttp" in str(p.get("type", "")):
            return str(p.get("id") or "wechat_bridge")
    for p in platforms:
        if isinstance(p, dict) and p.get("id"):
            return str(p["id"])
    return "wechat_bridge"


# ============ 同步 ============


def sync_admins_to_astrbot(wxids=None):
    """把管理员 wxid 映射成 UID 写入 AstrBot admins_id。

    返回 (是否写入成功, 说明文本)。同时总是更新桥接侧 admins.json。
    """
    if wxids is None:
        wxids = load_admins()
    else:
        save_admins(wxids)
    uids = [str(state._wxid_to_int(w)) for w in wxids if w]
    try:
        cfg, bom = _read_astrbot_cfg()
    except Exception as e:
        log.warning(f"同步管理员失败：{e}")
        return False, str(e)
    old = cfg.get("admins_id") or []
    cfg["admins_id"] = uids
    try:
        _write_astrbot_cfg(cfg, bom)
    except Exception as e:
        log.warning(f"写入 AstrBot admins_id 失败：{e}")
        return False, str(e)
    log.info(f"🔑 管理员已同步到 AstrBot：{old} -> {uids}（重启 AstrBot 后生效）")
    return True, f"已写入 {len(uids)} 个管理员（原 {len(old)} 个）"


# ============ 私聊白名单（AstrBot platform_settings.id_whitelist） ============
#
# 背景：AstrBot 开着 `enable_id_white_list` 时，不在 id_whitelist 里的会话
# 在 pipeline 第一阶段就被丢弃（whitelist_check.stage），表现就是
# "新加的好友发消息机器人不理"。白名单存的是 **UID**（数字串），
# 与桥接 persons.json 里的 uid 同源。
#
# ⚠️ AstrBot 的 WhitelistCheckStage.initialize() 只在**启动时**读一次白名单，
# 改完配置必须重启 AstrBot 才生效 —— 面板上要明确提示这一点。

def read_friend_whitelist():
    """读取 AstrBot 私聊白名单。返回 (uids_list, enabled, err)。"""
    try:
        cfg, _ = _read_astrbot_cfg()
    except Exception as e:
        return [], False, str(e)
    ps = cfg.get("platform_settings") or {}
    wl = ps.get("id_whitelist") or []
    uids = [str(i).strip() for i in wl if str(i).strip()]
    return uids, bool(ps.get("enable_id_white_list")), None


# ============ UMO 别名（面板「自定义规则」里显示的名字） ============
#
# 背景：AstrBot 面板里列出会话时显示的是 UMO
# （`wechat_bridge:FriendMessage:769462633`），数字 ID 根本看不出是谁。
# AstrBot 自己有一套 umo_aliases 表（auto_name / user_alias），
# 但它只在**唤醒阶段**（waking_check）才会记录，于是有两类会话没有名字：
#   ① WeFlow 把 `sourceName` 推成 wxid 时，记下来的 auto_name 就是 wxid；
#   ② 定时任务/主动发送等不经过唤醒阶段的会话，压根没有记录。
# 这里由桥接按自己的名册（persons.json / chat_names.json）补齐。

def _astrbot_db_path():
    """定位 AstrBot 的 data_v4.db。"""
    try:
        cfg, _ = _read_astrbot_cfg()
    except Exception:
        cfg = {}
    # 常见布局：<项目根>/runtime/astrbot/data/data_v4.db
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # runtime/
    for cand in (
        os.path.join(root, "astrbot", "data", "data_v4.db"),
        os.path.join(os.path.dirname(root), "AstrBot", "data", "data_v4.db"),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _display_name_for_session(umo: str) -> str:
    """用桥接名册把 UMO 翻成人类可读名字；翻不到返回空串。"""
    parts = (umo or "").split(":")
    if len(parts) < 3:
        return ""
    mtype, sid = parts[1], parts[2]
    if mtype == "GroupMessage":
        # UID → 群会话 ID → 群名
        try:
            session = state.get_session_id(int(sid))
        except Exception:
            session = None
        if not session:
            for sess, nm in state.known_groups().items():
                try:
                    if str(state._wxid_to_int(sess)) == str(sid):
                        session, nm0 = sess, nm
                        name = nm0
                        return name if name else ""
                except Exception:
                    continue
        if session:
            nm = state.get_chat_name(session)
            if nm:
                return nm
        return ""
    # 私聊：UID → wxid → 昵称
    #
    # ⚠️ 取名优先级（实测踩过）：`persons.json` 的 names 是**历史累积**的别名，
    # 第一条不一定是最新昵称（例：wxid_yyyyyyyyyyyyyyyy 的 names 是
    # ['李四', '张三']，而当前实际叫 张三）。
    # `chat_names.json` 才是"当前会话名"的权威来源，优先用它。
    #
    # 另外 persons.json **只装好友**，不含「文件传输助手 / 微信团队 / 公众号」
    # 这类非好友会话 —— 它们只在 chat_names.json 里有。
    # （实测：`FriendMessage:161333918` 就是 filehelper＝文件传输助手，
    #  只查 persons 会误判成"未识别的好友"。）
    for wxid, p in state.all_persons().items():
        try:
            if str(p.get("uid")) != str(sid):
                continue
            cur = state.get_chat_name(wxid)
            if cur and not cur.startswith("wxid_"):
                return cur
            for n in (p.get("names") or []):
                if n and not str(n).startswith("wxid_"):
                    return n
            return wxid
        except Exception:
            continue
    # 兜底：遍历 chat_names.json 的全部会话（含非好友的特殊会话）
    try:
        for sess in state.all_chat_names():
            try:
                if str(state._wxid_to_int(sess)) != str(sid):
                    continue
            except Exception:
                continue
            nm = state.get_chat_name(sess)
            if nm and not nm.startswith("wxid_"):
                return nm
    except Exception:
        pass
    return ""


def sync_umo_aliases() -> tuple:
    """给 AstrBot 里缺少/错误命名的会话补上可读名字。

    返回 (修正条数, 说明)。只改 auto_name，不动 user_alias（用户手填的优先）。
    """
    db = _astrbot_db_path()
    if not db:
        return 0, "未找到 AstrBot 数据库"
    import sqlite3
    try:
        c = sqlite3.connect(db)
    except Exception as e:
        return 0, f"打不开数据库: {e}"
    fixed = []
    try:
        # 找出所有出现过的会话（对话记录 + 已有别名）
        umos = set()
        for (u,) in c.execute("select distinct user_id from conversations where user_id is not null"):
            if u and ":" in u:
                umos.add(u)
        for (u,) in c.execute("select umo from umo_aliases"):
            if u:
                umos.add(u)
        have = {u: (a or "") for u, a in c.execute("select umo, auto_name from umo_aliases")}
        for umo in sorted(umos):
            want = _display_name_for_session(umo)
            cur = have.get(umo, "")
            # 需要修：没有记录 / 现在是 wxid 或纯数字 / 空
            bad = (not cur) or cur.startswith("wxid_") or cur.isdigit()
            if not want or not bad:
                continue
            c.execute(
                "insert into umo_aliases (created_at, updated_at, umo, creator_sender_id, auto_name, user_alias) "
                "values (datetime('now'), datetime('now'), ?, ?, ?, null) "
                "on conflict(umo) do update set auto_name=excluded.auto_name, updated_at=datetime('now')",
                (umo, umo.split(":")[-1], want))
            fixed.append(f"{umo.split(':')[-1]} → {want}")
        c.commit()
    except Exception as e:
        return 0, f"同步失败: {e}"
    finally:
        c.close()
    if fixed:
        log.info(f"🏷️ UMO 别名已修正 {len(fixed)} 条: {', '.join(fixed[:6])}")
    return len(fixed), (f"已修正 {len(fixed)} 条" if fixed else "无需修正")


def write_friend_whitelist(uids):
    """写入 AstrBot 私聊白名单（UID 列表）。返回 (ok, msg)。

    ⚠️ 两个坑（2026-09-18 修复）：
      ① 只传私聊 UID 会把**群条目整段抹掉**（id_whitelist 是同一份名单，
         群聊会因此全部失联）→ 这里与既有群条目合并。
      ② 裸数字 UID 在私聊里永远匹配不上（私聊没有 group_id），
         必须规整成完整 UMO → 走 normalize_whitelist_entries()。
    """
    uids = [str(u).strip() for u in uids if str(u).strip()]
    try:
        cfg, bom = _read_astrbot_cfg()
    except Exception as e:
        return False, str(e)
    ps = cfg.setdefault("platform_settings", {})
    old = ps.get("id_whitelist") or []
    # 保留既有群条目（裸数字或 GroupMessage UMO），私聊部分用新传入的替换
    kept_groups = [
        str(x).strip() for x in old
        if str(x).strip() and (
            "GroupMessage" in str(x) or str(x).strip().isdigit()
        )
    ]
    merged = normalize_whitelist_entries(kept_groups + uids)
    # 去重保序
    seen, final = set(), []
    for x in merged:
        if x not in seen:
            seen.add(x)
            final.append(x)
    ps["id_whitelist"] = final
    try:
        _write_astrbot_cfg(cfg, bom)
    except Exception as e:
        return False, str(e)
    log.info(f"🔑 私聊白名单已写入 AstrBot：{old} -> {final}（重启 AstrBot 后生效）")
    return True, f"已保存 {len(final)} 条（原 {len(old)} 条）"

def read_overview() -> dict:
    """面板「成员与权限」页所需的一切数据。"""
    admins = set(load_admins())
    bot_key = (config.BOT_WXID or "").strip()
    persons = []
    for wxid, p in state.all_persons().items():
        if bot_key and (wxid == bot_key or wxid.startswith(bot_key)):
            continue  # 机器人自己不入列
        names = p.get("names") or []
        persons.append({
            "wxid": wxid,
            "name": names[0] if names else wxid,
            "uid": p.get("uid"),
            "admin": wxid in admins,
        })
    persons.sort(key=lambda x: (not x["admin"], x["name"]))

    cfg = {}
    astrbot_ok = False
    try:
        cfg, _ = _read_astrbot_cfg()
        astrbot_ok = True
    except Exception as e:
        cfg_err = str(e)

    ps = cfg.get("platform_settings") or {}
    ltm = (cfg.get("provider_ltm_settings") or {}).get("active_reply") or {}
    groups = []
    pid = _platform_id(cfg) if astrbot_ok else "wechat_bridge"
    for session, name in sorted(state.known_groups().items(), key=lambda kv: kv[1]):
        gid = state._wxid_to_int(session)
        groups.append({
            "session": session,
            "name": name,
            "gid": gid,
            "umo": f"{pid}:GroupMessage:{gid}",
            "muted": state.is_session_muted(session),
        })

    humanizer_cfg = _read_json_file(_humanizer_config_path(), {}) if astrbot_ok else {}
    humanizer = ((humanizer_cfg or {}).get("style") or {}) if isinstance(humanizer_cfg, dict) else {}
    style_overrides = _read_humanizer_style_overrides()
    humanizer_sessions = []
    if astrbot_ok:
        for g in groups:
            humanizer_sessions.append({
                "umo": g["umo"], "name": g["name"], "kind": "group",
            })
        for p in persons:
            if p.get("uid") is None:
                continue
            humanizer_sessions.append({
                "umo": f"{pid}:FriendMessage:{p['uid']}",
                "name": p["name"], "kind": "private",
            })

    humanizer_styles = _read_humanizer_styles()
    global_style = str(humanizer.get("active_style") or "").strip()
    if not global_style and humanizer_styles:
        # The humanizer itself falls back to its first available profile when
        # active_style is empty, so the panel must show the same effective value.
        global_style = humanizer_styles[0]
    return {
        "ok": True,
        "astrbot_available": astrbot_ok,
        "astrbot_path": _astrbot_cfg_path() or "(未配置)",
        "astrbot_error": "" if astrbot_ok else cfg_err,
        "persons": persons,
        "groups": groups,
        "humanizer": {
            "available": astrbot_ok and bool(humanizer_styles),
            "global_style": global_style,
            "styles": humanizer_styles,
            "sessions": humanizer_sessions,
            "overrides": style_overrides,
        },
        "muted_sessions": state.muted_session_list(),
        "admins": sorted(admins),
        "id_whitelist_enable": bool(ps.get("enable_id_white_list", False)),
        "id_whitelist": ps.get("id_whitelist") or [],
        "ar_enable": bool(ltm.get("enable", False)),
        "ar_possibility": ltm.get("possibility_reply", 0.1),
        "ar_blacklist": _read_plugin_blacklist() or (ltm.get("blacklist") or []),
    }


def normalize_whitelist_entries(entries) -> list:
    """把白名单条目规整成 AstrBot 真正认的格式（**私聊必须写完整 UMO**）。

    ⚠️ 这里是一个反复咬人的坑（2026-09-18 定位）：
    AstrBot 的 `WhitelistCheckStage.process()` 判定是——

        if (event.unified_msg_origin not in self.whitelist
                and str(event.get_group_id()).strip() not in self.whitelist):

    也就是说它只认两种写法：
      · 完整 UMO，如 `wechat_bridge:FriendMessage:719415740`；
      · 群 ID（走 `get_group_id()` 兜底），如 `125237337`。
    **私聊没有 group_id**，所以往名单里写裸的数字 UID（如 `719415740`）
    → 私聊**永远匹配不上**，表现就是"这个好友发了多少条机器人都当没看见"。
    实测：11 次拒绝全部是私聊、群 0 次；群里一直正常，所以很难联想到白名单。

    规则：
      · 完整 UMO → 原样保留（并记下它的 UID，避免同一个人再出现裸 ID）；
      · 数字 ID 属于已知群 → 保持裸 ID；
      · 其他数字 ID → 视为私聊，补全成完整 UMO；
      · **同一个人只保留一条**：若该 UID 已有完整 UMO，就丢弃裸数字重复项。

    ⚠️ 最后这条是必需的（2026-09-18 发现）：否则面板每次保存都会把
    `1351824880` 和 `wechat_bridge:FriendMessage:1351824880` 两条一起写进去 ——
    功能上无害（UMO 已能匹配），但名单会越滚越脏、难以排查。
    """
    group_ids = set()
    try:
        import state as _st
        for sess in _st.known_groups():
            try:
                group_ids.add(str(_st._wxid_to_int(sess)))
            except Exception:
                continue
    except Exception:
        pass
    # 先收集所有完整 UMO 覆盖的 UID（私聊），用于压制重复裸 ID
    covered_uids = set()
    for x in (entries or []):
        v = str(x).strip()
        if ":" in v:
            parts = v.split(":")
            if len(parts) >= 3 and parts[1] == "FriendMessage":
                covered_uids.add(parts[-1].strip())

    out, seen = [], set()
    def _add(item):
        if item and item not in seen:
            seen.add(item)
            out.append(item)

    # 群条目（裸 ID）先入，保证顺序稳定
    for x in (entries or []):
        v = str(x).strip()
        if v and ":" not in v and v.isdigit() and v in group_ids:
            _add(v)
    # 再处理其余
    for x in (entries or []):
        v = str(x).strip()
        if not v:
            continue
        if ":" in v:                      # 已是完整 UMO
            _add(v)
        elif v.isdigit() and v in group_ids:
            continue                      # 群已在上面加过
        elif v.isdigit():                 # 私聊：已有 UMO 则跳过裸 ID
            if v in covered_uids:
                continue
            _add(f"wechat_bridge:FriendMessage:{v}")
        else:
            _add(v)
    return out


def update_astrbot_settings(payload: dict):
    """按面板提交更新 AstrBot 的白名单 / 主动回复设置。

    只动这几个键，其余配置一律不碰。返回 (成功?, 说明)。
    """
    try:
        cfg, bom = _read_astrbot_cfg()
    except Exception as e:
        return False, str(e)

    ps = cfg.setdefault("platform_settings", {})
    if "id_whitelist_enable" in payload:
        ps["enable_id_white_list"] = bool(payload["id_whitelist_enable"])
    if "id_whitelist" in payload:
        ps["id_whitelist"] = normalize_whitelist_entries(payload["id_whitelist"])

    ltm = cfg.setdefault("provider_ltm_settings", {})
    ar = ltm.setdefault("active_reply", {})
    if "ar_enable" in payload:
        ar["enable"] = bool(payload["ar_enable"])
    if "ar_possibility" in payload:
        try:
            ar["possibility_reply"] = max(0.0, min(1.0, float(payload["ar_possibility"])))
        except (TypeError, ValueError):
            pass
    if "ar_blacklist" in payload:
        entries = [str(x).strip() for x in payload["ar_blacklist"] if str(x).strip()]
        try:
            _write_plugin_blacklist(entries)
        except Exception as e:
            return False, f"写入主动回复黑名单失败: {e}"
        # 4.29 已删除该键；清掉旧副本，避免以后误以为它仍是权威来源。
        ar.pop("blacklist", None)
    # 白名单已被群表取代；保留空值只为兼容旧版读取。
    if "ar_whitelist" in payload or "ar_blacklist" in payload:
        ar["whitelist"] = []

    if "humanizer_style_overrides" in payload:
        raw_overrides = payload.get("humanizer_style_overrides") or {}
        if not isinstance(raw_overrides, dict):
            return False, "会话风格数据必须是对象"
        try:
            _write_humanizer_style_overrides(raw_overrides)
        except Exception as e:
            return False, f"写入会话风格失败: {e}"

    try:
        _write_astrbot_cfg(cfg, bom)
    except Exception as e:
        return False, str(e)
    log.info("⚙️ AstrBot 白名单/主动回复/会话风格设置已由面板更新")
    return True, "已写入 AstrBot 配置、插件黑名单与会话风格"
