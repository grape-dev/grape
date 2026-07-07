"""
Feishu (飞书) IM integration for DevOps Bot.
Uses official lark-oapi SDK for WebSocket connection.
Uses direct HTTP for message sending (SDK Client is async-only).
"""
import json, os, threading, logging, re, time
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

log = logging.getLogger("devops-bot")

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_BOT_NAME = os.environ.get("FEISHU_BOT_NAME", "DevOps Bot")

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
    return bool(FEISHU_APP_ID and FEISHU_APP_SECRET)


def get_tenant_token() -> str:
    """Get Feishu tenant_access_token with caching"""
    global _TOKEN, _TOKEN_EXPIRE
    import time
    if time.time() < _TOKEN_EXPIRE and _TOKEN:
        return _TOKEN
    with _TOKEN_LOCK:
        if time.time() < _TOKEN_EXPIRE and _TOKEN:
            return _TOKEN
        url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
        payload = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
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


def reply_card(message_id: str, text: str) -> str:
    """Reply with a card that can be updated later. Returns the reply message_id."""
    if not message_id or not text or not ensure_configured():
        return ""
    token = get_tenant_token()
    if not token:
        return ""
    url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply"
    card = {
        "elements": [{"tag": "markdown", "content": text[:2000]}],
        "header": {"title": {"tag": "plain_text", "content": ""}, "template": "blue"}
    }
    payload = json.dumps({
        "content": json.dumps(card),
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


def resolve_repo(text: str, chat_id: str, sender_id: str, db) -> str:
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
        c.execute("SELECT repo_key FROM feishu_bindings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        if row:
            return row[0]
    except Exception as e:
        log.warning(f"Feishu: resolve_repo db error: {e}")
    return ""


def process_command(text: str, chat_id: str, sender_id: str, db) -> str:
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
                "INSERT OR REPLACE INTO feishu_bindings (chat_id, repo_key, source, bind_by) VALUES (?,?,?,?)",
                (chat_id, repo_key, "gitcode", sender_id)
            )
            db.commit()
            return f"✅ 已绑定当前群到 `{repo_key}`"
        except Exception as e:
            return f"❌ 绑定失败: {e}"
    if cmd == "/unbind":
        try:
            c = db.cursor()
            c.execute("DELETE FROM feishu_bindings WHERE chat_id=?", (chat_id,))
            db.commit()
            return "✅ 已解绑当前群"
        except Exception as e:
            return f"❌ 解绑失败: {e}"
    if cmd == "/status":
        try:
            c = db.cursor()
            c.execute("SELECT repo_key, created_at FROM feishu_bindings WHERE chat_id=?", (chat_id,))
            row = c.fetchone()
            if row:
                return f"📋 当前绑定: `{row[0]}` (since {row[1]})"
            return "📋 未绑定任何仓库。使用 `/bind owner/repo` 绑定"
        except Exception as e:
            return f"❌ 查询失败: {e}"
    if cmd in ("/help", "/start"):
        return (
            f"🤖 DevOps Bot\n\n"
            f"**命令**:\n  `/bind owner/repo`  — 绑定当前群到仓库\n"
            f"  `/unbind`           — 解绑\n  `/status`           — 查看绑定\n"
            f"  `/help`             — 帮助\n\n"
            f"**直接提问**:\n  输入问题即可查询仓库状态"
        )
    return ""


# ====== WebSocket 事件处理 ======
# 通过 lark-oapi SDK 的 WSClient 建立长连接

_ws_client = None


def parse_event_data(event):
    """从 SDK 事件对象中提取消息字段"""
    try:
        event_type = event.header.event_type()
        log.info(f"Feishu WS event: {event_type}")
        return {"type": "unknown"}
    except Exception as e:
        log.error(f"Feishu: parse event error: {e}")
        return {"type": "unknown"}


def start_ws(on_message_callback):
    """启动 WebSocket 长连接"""
    global _ws_client
    if not ensure_configured():
        return

    # 创建事件处理器
    handler = (lark_oapi.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(lambda event: _handle_message(event, on_message_callback))
               .build())

    # 创建并启动 WS Client
    lark_oapi.APP_ID = FEISHU_APP_ID
    lark_oapi.APP_SECRET = FEISHU_APP_SECRET
    _ws_client = lark_oapi.ws.Client(
        app_id=FEISHU_APP_ID,
        app_secret=FEISHU_APP_SECRET,
        event_handler=handler,
        auto_reconnect=True,
    )
    log.info("Feishu WS client starting...")
    _ws_client.start()


def stop_ws():
    global _ws_client
    if _ws_client:
        try:
            _ws_client.stop()
        except:
            pass


def _handle_message(event, on_message_callback):
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

        parsed = {
            "type": "message",
            "text": text,
            "chat_id": getattr(message, 'chat_id', "") or "",
            "sender_id": sender_id,
            "message_id": getattr(message, 'message_id', "") or "",
            "message_type": msg_type,
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
