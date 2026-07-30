"""Feishu (飞书) IM integration for DevOps Bot.
Uses official lark-oapi SDK for WebSocket connection.
Uses direct HTTP for message sending (SDK Client is async-only).
"""
import json, os, logging, re, time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError
import lark_oapi
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

# 全局 Feishu 消息并发限制（等待+运行最多 5 个）
import concurrent.futures
import threading
_feishu_slot = threading.BoundedSemaphore(5)
_feishu_executor = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="fs")

from src.config import log

FEISHU_BASE = "https://open.feishu.cn/open-apis"
_TOKEN = None
_TOKEN_EXPIRE = 0
_TOKEN_LOCK = threading.Lock()

FEISHU_BINDINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feishu_bindings (
    chat_id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,
    source TEXT DEFAULT 'gitcode',
    bind_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)
"""


def ensure_configured():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT bot_app_id FROM im_bot_config WHERE bot_type='chat_bot' LIMIT 1")
            row = c.fetchone()
            if row and row[0]:
                return True
        except Exception:
            pass
    return False


def get_tenant_token() -> str:
    """Get Feishu tenant_access_token with caching"""
    global _TOKEN, _TOKEN_EXPIRE
    import time
    if time.time() < _TOKEN_EXPIRE and _TOKEN:
        return _TOKEN
    with _TOKEN_LOCK:
        if time.time() < _TOKEN_EXPIRE and _TOKEN:
            return _TOKEN
        # 从 im_bot_config 获取 Chat Agent 凭据
        app_id = ""
        app_secret = ""
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            try:
                c.execute("SELECT bot_app_id, bot_app_secret FROM im_bot_config WHERE bot_type='chat_bot' LIMIT 1")
                row = c.fetchone()
                if row and row[0] and row[1]:
                    app_id = row[0]
                    app_secret = row[1]
            except Exception:
                pass
        if not app_id or not app_secret:
            return ""
        url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
        payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
        try:
            resp = urlopen(Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=10)
            data = json.loads(resp.read())
            _TOKEN = data.get("tenant_access_token", "")
            expire = data.get("expire", 7200)
            _TOKEN_EXPIRE = time.time() + expire - 300
            return _TOKEN
        except Exception as e:
            log.error(f"Feishu: token error: {e}")
            return ""


def get_tenant_token_from(app_id: str, app_secret: str) -> str:
    """根据自定义 app_id/app_secret 获取 tenant_access_token（不从 chat_config 读取）"""
    import time
    if not app_id or not app_secret:
        return ""
    url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    try:
        resp = urlopen(Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=10)
        data = json.loads(resp.read())
        return data.get("tenant_access_token", "")
    except Exception as e:
        log.warning(f"Feishu: get_tenant_token_from failed: {e}")
        return ""


def get_user_name(user_id: str) -> str:
    """通过飞书 API 获取用户昵称"""
    if not user_id or not ensure_configured():
        return user_id
    token = get_tenant_token()
    if not token:
        return user_id
    url = f"{FEISHU_BASE}/im/v1/users/{user_id}"
    try:
        resp = urlopen(Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=10)
        data = json.loads(resp.read())
        name = data.get("data", {}).get("user", {}).get("name", "") or ""
        return name if name else user_id
    except:
        return user_id


def reply_text(message_id: str, text: str) -> str:
    """Reply to a Feishu message. Returns the reply message_id or empty string on failure."""
    if not message_id or not text or not ensure_configured():
        return ""
    token = get_tenant_token()
    if not token:
        return ""
    url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply"
    payload = json.dumps({
        "content": json.dumps({"text": text[:2000]}),
        "msg_type": "text"
    }).encode()
    try:
        resp = urlopen(Request(url, data=payload, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }), timeout=15)
        data = json.loads(resp.read())
        reply_id = data.get("data", {}).get("message_id", "")
        log.info(f"Feishu: reply sent, rid={reply_id[:20]}, status={resp.status}")
        return reply_id
    except URLError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:200]
        except:
            pass
        log.error(f"Feishu: reply failed: {e.code if hasattr(e,'code') else e.reason} {err_body}")
        return ""
    except Exception as e:
        log.error(f"Feishu: reply exception: {e}")
        return ""


def _build_card_payload(text: str) -> dict:
    """Build Feishu interactive card v2 payload"""
    return {
        "elements": [{
            "tag": "div",
            "text": {"tag": "lark_md", "content": text[:2000]}
        }],
        "header": {"title": {"tag": "plain_text", "content": "DevOps Bot"}, "template": "blue"}
    }


def reply_card(message_id: str, text: str) -> str:
    """Reply with a card that can be updated later. Returns the reply message_id."""
    if not message_id or not text or not ensure_configured():
        return ""
    token = get_tenant_token()
    if not token:
        return ""
    url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply"
    payload = json.dumps({
        "content": json.dumps(_build_card_payload(text)),
        "msg_type": "interactive"
    }).encode()
    try:
        resp = urlopen(Request(url, data=payload, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }), timeout=15)
        data = json.loads(resp.read())
        reply_id = data.get("data", {}).get("message_id", "")
        log.info(f"Feishu: card sent, rid={reply_id[:20]}")
        return reply_id
    except URLError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:200]
        except:
            pass
        log.warning(f"Feishu: card reply failed: {e.code if hasattr(e,'code') else e.reason} {err_body}")
        return ""
    except Exception as e:
        log.warning(f"Feishu: card exception: {e}")
        return ""


def send_card_to_user(open_id: str, text: str) -> str:
    """Proactively send a card to a Feishu user by open_id. Returns message_id."""
    if not open_id or not text or not ensure_configured():
        return ""
    token = get_tenant_token()
    if not token:
        return ""
    url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id"
    payload = json.dumps({
        "receive_id": open_id,
        "content": json.dumps(_build_card_payload(text)),
        "msg_type": "interactive"
    }).encode()
    try:
        resp = urlopen(Request(url, data=payload, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }), timeout=15)
        data = json.loads(resp.read())
        msg_id = data.get("data", {}).get("message_id", "")
        log.info(f"Feishu: card sent to user (open_id={open_id[:20]}, msg_id={msg_id[:20]})")
        return msg_id
    except URLError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:200]
        except:
            pass
        log.warning(f"Feishu: send to user failed: {e.code if hasattr(e,'code') else e.reason} {err_body}")
        return ""
    except Exception as e:
        log.warning(f"Feishu: send to user exception: {e}")
        return ""


def update_message(message_id: str, text: str) -> bool:
    """Edit/replace an existing message sent by the bot."""
    if not message_id or not text or not ensure_configured():
        return False
    token = get_tenant_token()
    if not token:
        return False
    url = f"{FEISHU_BASE}/im/v1/messages/{message_id}"
    payload = json.dumps({
        "content": json.dumps({"text": text[:2000]}),
        "msg_type": "text"
    }).encode()
    try:
        req = Request(url, data=payload, method="PATCH", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        })
        resp = urlopen(req, timeout=15)
        log.info(f"Feishu: message updated, status={resp.status}")
        return True
    except URLError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:200]
        except:
            pass
        log.error(f"Feishu: update failed: {e.code if hasattr(e,'code') else e.reason} {err_body}")
        return False


def add_reaction(message_id: str, emoji_type: str = "OK") -> str:
    """Add an emoji reaction to a Feishu message. Returns reaction_id on success."""
    if not message_id or not ensure_configured():
        return ""
    token = get_tenant_token()
    if not token:
        return ""
    url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reactions"
    payload = json.dumps({
        "reaction_type": {"emoji_type": emoji_type}
    }).encode()
    try:
        resp = urlopen(Request(url, data=payload, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }), timeout=10)
        data = json.loads(resp.read())
        reaction_id = data.get("data", {}).get("reaction_id", "")
        log.info(f"Feishu: reaction added ({emoji_type}), rid={reaction_id[:20]}")
        return reaction_id
    except URLError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:200]
        except:
            pass
        log.info(f"Feishu: reaction skipped ({emoji_type}): {e.code if hasattr(e,'code') else e.reason} {err_body}")
        return ""
    except Exception as e:
        log.info(f"Feishu: reaction exception ({emoji_type}): {e}")
        return ""


def get_chat_members(chat_id: str) -> list:
    """Get all members of a Feishu group chat. Returns [{name, open_id}]."""
    if not chat_id or not ensure_configured():
        return []
    token = get_tenant_token()
    if not token:
        return []
    members = []
    page_token = ""
    while True:
        url = f"{FEISHU_BASE}/im/v1/chats/{chat_id}/members?member_id_type=open_id&page_size=100"
        if page_token:
            url += f"&page_token={page_token}"
        try:
            resp = urlopen(Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=15)
            data = json.loads(resp.read())
            items = data.get("data", {}).get("items", [])
            for m in items:
                members.append({
                    "name": m.get("name", ""),
                    "open_id": m.get("member_id", "")
                })
            if not data.get("data", {}).get("has_more", False):
                break
            page_token = data.get("data", {}).get("page_token", "")
            if not page_token:
                break
        except URLError as e:
            log.warning(f"Feishu: get members failed: {e.code if hasattr(e,'code') else e.reason}")
            break
        except Exception as e:
            log.warning(f"Feishu: get members exception: {e}")
            break
    log.info(f"Feishu: got {len(members)} members from chat {chat_id[:20]}")
    return members


def get_chat_id_for_repo(repo_key: str) -> str:
    """Get the bound Feishu chat_id for a repo."""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT chat_id FROM feishu_bindings WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            return row[0] if row else ""
    except Exception as e:
        log.warning(f"Feishu: get chat_id for repo error: {e}")
        return ""


def sync_repo_feishu_mapping(repo_key: str, chat_id: str) -> int:
    """Sync group members to feishu_users by nick_name matching. Returns match count."""
    members = get_chat_members(chat_id)
    if not members:
        return 0
    # Get GitCode collaborators
    parts = repo_key.replace("gitcode:", "").split("/", 1)
    if len(parts) < 2:
        return 0
    owner, repo = parts
    # 从 managed_repos 获取 repo 绑定的 Bot Token
    from src.user.db import get_bot_token_by_repo
    api_token = get_bot_token_by_repo(owner, repo)
    if not api_token:
        log.warning(f"Feishu sync: no bot token for {repo_key}, skipping")
        return 0
    try:
        collabs = []
        page = 1
        while True:
            resp = urlopen(Request(
                f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators?per_page=100&page={page}",
                headers={"Authorization": f"Bearer {api_token}"}
            ), timeout=10)
            data = json.loads(resp.read())
            if not data:
                break
            collabs.extend(data)
            page += 1
    except Exception as e:
        log.warning(f"Feishu sync: failed to fetch collaborators for {repo_key}: {e}")
        return 0
    from src.session_manager import set_feishu_open_id
    full_key = "gitcode:" + repo_key if not repo_key.startswith("gitcode:") else repo_key
    matched = 0
    for collab in collabs:
        nick = collab.get("nick_name", "")
        login = collab.get("username", "")
        if not nick or not login:
            continue
        for m in members:
            if m.get("name") == nick:
                set_feishu_open_id(full_key, login, m["open_id"], nick)
                matched += 1
                break
    log.info(f"Feishu sync: {repo_key} -> {matched} users matched from {len(members)} group members")
    return matched


BIND_PATTERNS = [
    re.compile(r'(?:绑定|连接|关联|设置|使用|切换到|帮我绑定)\s*(?:到)?\s*([\w.-]+/[\w.-]+)'),
    re.compile(r'(?:把|将|让)\s*([\w.-]+/[\w.-]+)\s*(?:绑定|连上|设置)'),
]


def detect_bind_intent(text: str) -> str:
    """自然语言绑定检测：用户说'绑定到 owner/repo' 等，返回 repo_key 或空"""
    for pat in BIND_PATTERNS:
        m = pat.search(text)
        if m:
            repo = m.group(1)
            log.info(f"Feishu: detected bind intent -> {repo}")
            return repo
    return ""


CLEAN_PATTERNS = [
    r'(?:清除|清理|清空|重置)(?:会话|缓存|上下文|Agent|agent|记忆)',
    r'(?:重来|从头开始)(?:吧|一下)?',
]


def detect_clean_intent(text: str) -> bool:
    """自然语言清理检测：用户说'清除会话缓存'等"""
    for pat in CLEAN_PATTERNS:
        if re.search(pat, text):
            log.info("Feishu: detected clean intent")
            return True
    return False


def resolve_repo(text: str, chat_id: str, sender_id: str, db, account: str = "default") -> str:
    # 1. 自然语言绑定检测
    bind_repo = detect_bind_intent(text)
    if bind_repo:
        return bind_repo
    # 2. 消息中显式匹配 owner/repo
    m = re.search(r'[\w.-]+/[\w.-]+', text)
    if m:
        return m.group(0)
    # 3. 查询群绑定表
    try:
        c = db.cursor()
        c.execute("SELECT repo_key FROM feishu_bindings WHERE chat_id=? AND account=?", (chat_id, account))
        row = c.fetchone()
        if row:
            return row[0]
    except Exception as e:
        log.warning(f"Feishu: resolve_repo db error: {e}")
    return ""


def process_command(text: str, chat_id: str, sender_id: str, db, account: str = "default") -> str:
    text = text.strip()
    parts = text.split()
    if not parts:
        return ""
    cmd = parts[0].lower()
    if cmd == "/bind" and len(parts) >= 2:
        repo_key = parts[1]
        try:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO feishu_bindings (chat_id, repo_key, source, bind_by, account) VALUES (?,?,?,?,?)",
                (chat_id, repo_key, "gitcode", sender_id, account)
            )
            db.commit()
            matched = sync_repo_feishu_mapping(repo_key, chat_id)
            if matched > 0:
                return f"✅ 已绑定 `{repo_key}`，同步了 {matched} 个用户映射"
            return f"✅ 已绑定当前群到 `{repo_key}`"
        except Exception as e:
            return f"❌ 绑定失败: {e}"
    if cmd == "/unbind":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM feishu_bindings WHERE chat_id=? AND account=?", (chat_id, account))
            row = c.fetchone()
            c.execute("DELETE FROM feishu_bindings WHERE chat_id=? AND account=?", (chat_id, account))
            if row:
                repo_key = row[0]
                full_key = "gitcode:" + repo_key if not repo_key.startswith("gitcode:") else repo_key
                c.execute("DELETE FROM feishu_users WHERE repo_key=? OR repo_key=?", (repo_key, full_key))
            db.commit()
            return "✅ 已解绑当前群并清除用户映射"
        except Exception as e:
            return f"❌ 解绑失败: {e}"
    if cmd == "/status":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key, created_at FROM feishu_bindings WHERE chat_id=? AND account=?", (chat_id, account))
            row = c.fetchone()
            if row:
                return f"📋 当前绑定: `{row[0]}` (since {row[1]})"
            return "📋 未绑定任何仓库。使用 `/bind owner/repo` 绑定"
        except Exception as e:
            return f"❌ 查询失败: {e}"
    if cmd == "/refresh":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM feishu_bindings WHERE chat_id=? AND account=?", (chat_id, account))
            row = c.fetchone()
            if not row:
                return "📋 当前群未绑定任何仓库。使用 `/bind owner/repo` 绑定后刷新。"
            matched = sync_repo_feishu_mapping(row[0], chat_id)
            return f"✅ 已刷新，同步了 {matched} 个用户映射"
        except Exception as e:
            return f"❌ 刷新失败: {e}"
    if cmd == "/autodev":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM feishu_bindings WHERE chat_id=? AND account=?", (chat_id, account))
            row = c.fetchone()
            if not row:
                return "📋 当前群未绑定任何仓库。使用 `/bind owner/repo` 绑定后设置。"
            repo_key = row[0]
            from src.session_manager import set_auto_impl_enabled, get_auto_impl_enabled
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                enabled = parts[1].lower() == "on"
                set_auto_impl_enabled(repo_key, enabled)
                return f"✅ 仓库 `{repo_key}` 自动实现已{'开启' if enabled else '关闭'}"
            current = get_auto_impl_enabled(repo_key)
            return f"📋 仓库 `{repo_key}` 自动实现：{'✅ 开启' if current else '❌ 关闭'}"
        except Exception as e:
            return f"❌ 设置失败: {e}"
    if cmd == "/notify":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM feishu_bindings WHERE chat_id=? AND account=?", (chat_id, account))
            row = c.fetchone()
            if not row:
                return "📋 当前群未绑定任何仓库。使用 `/bind owner/repo` 绑定后设置。"
            repo_key = row[0]
            from src.session_manager import set_notify_enabled, get_notify_enabled
            if len(parts) >= 2 and parts[1].lower() in ("on", "off"):
                enabled = parts[1].lower() == "on"
                set_notify_enabled(repo_key, enabled)
                return f"✅ 仓库 `{repo_key}` 飞书通知已{'开启' if enabled else '关闭'}"
            current = get_notify_enabled(repo_key)
            return f"📋 仓库 `{repo_key}` 飞书通知：{'✅ 开启' if current else '❌ 关闭'}"
        except Exception as e:
            return f"❌ 设置失败: {e}"
    if cmd in ("/clean", "/cleansession", "/reset", "/cleancache"):
        try:
            c = db.cursor()
            c.execute("SELECT repo_key FROM feishu_bindings WHERE chat_id=? AND account=?", (chat_id, account))
            row = c.fetchone()
            if not row:
                return "📋 当前群未绑定任何仓库。使用 `/bind owner/repo` 绑定后再清理。"
            repo_key = row[0]
            full_key = "gitcode:" + repo_key if not repo_key.startswith("gitcode:") else repo_key
            c.execute("UPDATE sessions SET opencode_session_id='', codearts_session_id='', backend='' WHERE repo_key=? OR repo_key=?", (repo_key, full_key))
            db.commit()
            return f"✅ 已清除仓库 `{full_key}` 的 Agent 会话缓存。下次事件将使用全新的 Agent 上下文。"
        except Exception as e:
            return f"❌ 清理失败: {e}"
    if cmd in ("/help", "/start"):
        return (
            f"🤖 DevOps Bot\n\n"
            f"**命令**:\n  `/bind owner/repo`  — 绑定当前群到仓库\n"
            f"  `/unbind`           — 解绑并清除用户映射\n  `/status`           — 查看绑定\n  `/refresh`          — 刷新用户映射\n"
            f"  `/autodev on/off`   — 自动实现开关\n  `/notify on/off`    — 飞书通知开关\n  `/clean`            — 清除 Agent 会话缓存\n"
            f"  `/help`             — 帮助\n\n"
            f"**直接提问**:\n  输入问题即可查询仓库状态"
        )
    return ""


# ====== WebSocket 事件处理 ======
# 通过 lark-oapi SDK 的 WSClient 建立长连接

_ws_clients: dict = {}


def parse_event_data(event):
    """从 SDK 事件对象中提取消息字段"""
    try:
        event_type = event.header.event_type()
        log.info(f"Feishu WS event: {event_type}")
        return {"type": "unknown"}
    except Exception as e:
        log.error(f"Feishu: parse event error: {e}")
        return {"type": "unknown"}


def start_ws(on_message_callback, account_name: str = "default"):
    """启动 WebSocket 长连接。支持多账户，每个 account 一个 WS 连接。"""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    import lark_oapi.ws.client as _lark_ws
    _lark_ws.loop = loop

    global _ws_clients

    # 从 im_bot_config 获取 Chat Agent 凭据
    app_id = ""
    app_secret = ""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT bot_app_id, bot_app_secret FROM im_bot_config WHERE bot_type='chat_bot' LIMIT 1")
            row = c.fetchone()
            if row and row[0] and row[1]:
                app_id = row[0]
                app_secret = row[1]
        except Exception:
            pass

    if not app_id or not app_secret:
        log.warning(f"Feishu: no chat_bot configured in im_bot_config, skipping WS")
        return

    # 记录当前 WS 使用的 app_id 到 meta 表（供后续查询 user_id 用）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('ws_feishu_app_id', ?)", (app_id,))
            db.commit()
    except Exception:
        pass

    # 创建事件处理器（注入 account_name）
    def _handle_with_account(event):
        return _handle_message(event, on_message_callback, account_name)

    handler = (lark_oapi.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_handle_with_account)
               .build())

    # 设置全局 APP_ID/APP_SECRET（lark-oapi 需要）
    lark_oapi.APP_ID = app_id
    lark_oapi.APP_SECRET = app_secret

    # 创建并启动 WS Client
    client = lark_oapi.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )
    _ws_clients[account_name] = client
    log.info(f"Feishu WS client starting (account={account_name})...")
    # 直接用自定义 loop 调用 _connect 和 _select，绕过模块级 loop 变量
    try:
        loop.run_until_complete(client._connect())
    except Exception as e:
        loop.run_until_complete(client._disconnect())
        if client._auto_reconnect:
            loop.run_until_complete(client._reconnect())
        else:
            raise
    loop.create_task(client._ping_loop())
    loop.run_until_complete(_lark_ws._select())


def get_user_id_by_app_id(app_id: str) -> int | None:
    """通过 feishu_app_id 查找关联的 user_id（chat_config）"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT user_id FROM chat_config WHERE feishu_app_id=?", (app_id,))
            row = c.fetchone()
            if row:
                return row[0]
        except Exception:
            pass
    return None


def stop_ws(bot_key: str | None = None):
    """停止指定或所有 WS 连接"""
    global _ws_clients
    if bot_key:
        client = _ws_clients.pop(bot_key, None)
        if client:
            try:
                client.stop()
            except:
                pass
        return
    for name, client in list(_ws_clients.items()):
        try:
            client.stop()
        except:
            pass
    _ws_clients = {}


async def start_ws_async(on_message_callback, bot_app_id: str, bot_app_secret: str):
    """在运行中事件循环上创建并连接 Chat WS Client（使用 lark_oapi）"""
    global _ws_clients
    import lark_oapi.ws.client as _lark_ws
    _lark_ws.loop = asyncio.get_running_loop()

    def _handle_with_account(event):
        return _handle_message(event, on_message_callback, bot_app_id)

    import lark_oapi
    handler = (lark_oapi.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_handle_with_account)
               .build())

    # 预先获取 WS URL（使用 async HTTP，避免 _get_conn_url 的同步 requests.post 阻塞事件循环）
    from src.im.feishu_bot import _get_ws_endpoint_sync
    ws_url = _get_ws_endpoint_sync(bot_app_id, bot_app_secret)
    if not ws_url:
        log.warning(f"Chat WS: no ws endpoint for {bot_app_id[:20]}, skip")
        return

    client = lark_oapi.ws.Client(
        app_id=bot_app_id,
        app_secret=bot_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )
    client._get_conn_url = lambda: ws_url
        app_id=bot_app_id,
        app_secret=bot_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )
    ws_key = f"chat_{bot_app_id}"
    _ws_clients[ws_key] = client
    log.info(f"Chat WS connecting (bot_app_id={bot_app_id[:30]}...)")

    try:
        await client._connect()
    except Exception as e:
        log.warning(f"Chat WS connect failed for {bot_app_id[:20]}: {e}")
        return

    log.info(f"Chat WS connected (bot_app_id={bot_app_id[:30]}...)")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log.info(f"Chat WS cancelled (bot_app_id={bot_app_id[:30]}...)")


async def start_vod_ws_async(bot_app_id: str, bot_app_secret: str):
    """在运行中事件循环上创建并连接 VOD WS Client（使用 lark_oapi）"""
    global _ws_clients
    import lark_oapi.ws.client as _lark_ws
    _lark_ws.loop = asyncio.get_running_loop()

    from src.im.vod_handler import process_vod_message
    _vod_bot_id = bot_app_id

    def _handle_vod(event):
        try:
            header = getattr(event, 'header', None)
            if header is None:
                return
            event_type = getattr(header, 'event_type', None)
            if event_type != "im.message.receive_v1":
                return
            ev = getattr(event, 'event', None)
            if ev is None:
                return
            message = getattr(ev, 'message', None)
            sender = getattr(ev, 'sender', None)
            if message is None or sender is None:
                return
            msg_content = getattr(message, 'content', '{}') or '{}'
            msg_type = getattr(message, 'message_type', 'text') or 'text'
            text = ""
            if msg_type == "text":
                try:
                    text = json.loads(msg_content).get("text", "")
                except json.JSONDecodeError:
                    text = msg_content
            if not text:
                return
            from src.im.feishu import _feishu_slot, _feishu_executor
            message_dict = {
                "event": {
                    "message": {
                        "message_id": getattr(message, 'message_id', ''),
                        "chat_id": getattr(message, 'chat_id', ''),
                        "message_type": msg_type,
                        "content": msg_content,
                    },
                    "sender": {
                        "sender_id": {
                            "open_id": getattr(getattr(sender, 'sender_id', None), 'open_id', ''),
                        }
                    }
                }
            }
            if not _feishu_slot.acquire(blocking=False):
                log.warning("VOD IM: queue full, dropping")
                return
            try:
                process_vod_message(message_dict, _vod_bot_id)
            finally:
                _feishu_slot.release()
        except Exception as e:
            log.warning(f"VOD handle error: {e}")

    import lark_oapi
    handler = (lark_oapi.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_handle_vod)
               .build())

    client = lark_oapi.ws.Client(
        app_id=bot_app_id,
        app_secret=bot_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )
    ws_key = f"vod_{bot_app_id}"
    _ws_clients[ws_key] = client
    log.info(f"VOD WS connecting (bot_app_id={bot_app_id[:30]}...)")

    try:
        await client._connect()
    except Exception as e:
        log.warning(f"VOD WS connect failed for {bot_app_id[:20]}: {e}")
        return

    log.info(f"VOD WS connected (bot_app_id={bot_app_id[:30]}...)")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log.info(f"VOD WS cancelled (bot_app_id={bot_app_id[:30]}...)")


async def start_vod_ws_async(bot_app_id: str, bot_app_secret: str):
    """在运行中事件循环上创建并连接 VOD WS Client"""
    global _ws_clients
    import lark_oapi.ws.client as _lark_ws
    _lark_ws.loop = asyncio.get_running_loop()

    from src.im.vod_handler import process_vod_message

    def _handle_vod(event):
        try:
            header = getattr(event, 'header', None)
            if header is None:
                return
            event_type = getattr(header, 'event_type', None)
            if callable(event_type):
                event_type = event_type()
            if event_type != "im.message.receive_v1":
                return
            ev = getattr(event, 'event', None)
            if ev is None:
                return
            message = getattr(ev, 'message', None)
            sender = getattr(ev, 'sender', None)
            if message is None or sender is None:
                return
            msg_content = getattr(message, 'content', '{}') or '{}'
            msg_type = getattr(message, 'message_type', 'text') or 'text'
            text = ""
            if msg_type == "text":
                try:
                    text = json.loads(msg_content).get("text", "")
                except json.JSONDecodeError:
                    text = msg_content
            if not text:
                return
            message_dict = {
                "event": {
                    "message": {
                        "message_id": getattr(message, 'message_id', ''),
                        "chat_id": getattr(message, 'chat_id', ''),
                        "message_type": msg_type,
                        "content": msg_content,
                    },
                    "sender": {
                        "sender_id": {
                            "open_id": getattr(getattr(sender, 'sender_id', None), 'open_id', ''),
                        }
                    }
                }
            }
            process_vod_message(message_dict, bot_app_id)
        except Exception as e:
            log.warning(f"VOD handle error: {e}")

    import lark_oapi
    handler = (lark_oapi.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(_handle_vod)
               .build())

    # 预先获取 WS URL（避免 _get_conn_url 的同步 requests.post 阻塞事件循环）
    from src.im.feishu_bot import _get_ws_endpoint_sync
    ws_url = _get_ws_endpoint_sync(bot_app_id, bot_app_secret)
    if not ws_url:
        log.warning(f"VOD WS: no ws endpoint for {bot_app_id[:20]}, skip")
        return

    client = lark_oapi.ws.Client(
        app_id=bot_app_id,
        app_secret=bot_app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )
    client._get_conn_url = lambda: ws_url
    ws_key = f"vod_{bot_app_id}"
    _ws_clients[ws_key] = client
    log.info(f"VOD WS connecting (bot_app_id={bot_app_id[:30]}...)")

    try:
        await client._connect()
    except Exception as e:
        log.warning(f"VOD WS connect failed for {bot_app_id[:20]}: {e}")
        await client._disconnect()

    log.info(f"VOD WS connected (bot_app_id={bot_app_id[:30]}...)")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        log.info(f"VOD WS cancelled (bot_app_id={bot_app_id[:30]}...)")



def _handle_message(event, on_message_callback, account_name: str = "default"):
    """处理收到的飞书消息（在 WS 线程中调用）"""
    try:
        log.info(f"Feishu WS event received, type={type(event).__name__}")

        # event.header.event_type is a string, not a method
        header = getattr(event, 'header', None)
        if header is None:
            log.warning("Feishu: event has no header")
            return

        event_type = getattr(header, 'event_type', None)
        if callable(event_type):
            event_type = event_type()
        log.info(f"Feishu WS event: {event_type}")

        if event_type != "im.message.receive_v1":
            return

        ev = getattr(event, 'event', None)
        if ev is None:
            log.warning("Feishu: event has no event field")
            return

        message = getattr(ev, 'message', None)
        sender = getattr(ev, 'sender', None)
        if message is None or sender is None:
            log.warning("Feishu: event missing message or sender")
            return

        msg_content = getattr(message, 'content', '{}') or '{}'
        msg_type = getattr(message, 'message_type', 'text') or 'text'
        text = ""
        if msg_type == "text":
            try:
                text = json.loads(msg_content).get("text", "")
            except json.JSONDecodeError:
                text = msg_content
        elif msg_type == "post":
            try:
                content_data = json.loads(msg_content)
                content_body = content_data.get("content", {})
                if isinstance(content_body, dict):
                    for lang_data in content_body.values():
                        if isinstance(lang_data, list):
                            for paragraph in lang_data:
                                if isinstance(paragraph, list):
                                    for segment in paragraph:
                                        if segment.get("tag") == "text":
                                            text += segment.get("text", "") + " "
                elif isinstance(content_body, list):
                    for paragraph in content_body:
                        if isinstance(paragraph, list):
                            for segment in paragraph:
                                if segment.get("tag") == "text":
                                    text += segment.get("text", "") + " "
            except (json.JSONDecodeError, KeyError, TypeError):
                text = msg_content

        text = re.sub(r'@_?\w+\s*', '', text).strip()

        sender_id = ""
        if sender and sender.sender_id:
            sender_id = sender.sender_id.user_id or ""

        sender_open_id = ""
        if sender and sender.sender_id:
            sender_open_id = sender.sender_id.open_id or ""

        parsed = {
            "type": "message",
            "text": text,
            "chat_id": getattr(message, 'chat_id', "") or "",
            "sender_id": sender_id,
            "sender_open_id": sender_open_id,
            "message_id": getattr(message, 'message_id', "") or "",
            "message_type": msg_type,
            "account": account_name,
        }

        log.info(f"Feishu message: {text[:100]} (chat={parsed['chat_id'][:20]})")

        if on_message_callback and text:
            if not _feishu_slot.acquire(blocking=False):
                log.warning("Feishu: message queue full (max=5), dropping")
            else:
                def _run_with_slot(*args):
                    try:
                        on_message_callback(*args)
                    finally:
                        _feishu_slot.release()
                _feishu_executor.submit(_run_with_slot, parsed)

    except Exception as e:
        log.error(f"Feishu: WS message handler error: {e}", exc_info=True)
