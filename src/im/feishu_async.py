"""纯异步飞书多Bot连接管理器 — 支持100+Bot单进程隔离运行

架构:
  BotContext  — 单Bot完整隔离上下文（token缓存/并发槽/指标/WS连接）
  FeishuAsync — aiohttp 飞书API客户端（token/消息发送/WS端点获取）
  BotManager  — 编排所有Bot的启动/停止/健康检查/热加载

不依赖 lark-oapi SDK，直接调用飞书 Connections OpenAPI + WebSocket 长连接。
"""
import json, asyncio, time, logging, re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable, Optional, Any

import aiohttp
import websockets

from src.config import log

FEISHU_BASE = "https://open.feishu.cn/open-apis"
WS_ENDPOINT_URL = "https://open.feishu.cn/callback/ws/endpoint"


_SEEN_MSG_TTL = 86400 * 2
_SEEN_MSG_MAX_LOAD = 2000


def _load_seen_msg_ids(app_id: str) -> set:
    try:
        from src.session_manager import db, db_lock
        cutoff = time.time() - _SEEN_MSG_TTL
        with db_lock:
            c = db.cursor()
            c.execute("SELECT message_id FROM bot_seen_messages WHERE app_id=? AND seen_at>? ORDER BY seen_at DESC LIMIT ?",
                      (app_id, cutoff, _SEEN_MSG_MAX_LOAD))
            return {r[0] for r in c.fetchall()}
    except Exception:
        return set()


def _persist_seen_msg_id(app_id: str, message_id: str):
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR IGNORE INTO bot_seen_messages (app_id, message_id, seen_at) VALUES (?,?,?)",
                      (app_id, message_id, time.time()))
            db.commit()
    except Exception:
        pass


def _cleanup_seen_msg_ids(app_id: str):
    try:
        from src.session_manager import db, db_lock
        cutoff = time.time() - _SEEN_MSG_TTL
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM bot_seen_messages WHERE app_id=? AND seen_at<?", (app_id, cutoff))
            db.commit()
    except Exception:
        pass


class BotState(Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class BotMetrics:
    messages_received: int = 0
    messages_sent: int = 0
    messages_failed: int = 0
    reconnect_count: int = 0
    last_message_at: float = 0.0
    connected_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict:
        return {
            "messages_received": self.messages_received,
            "messages_sent": self.messages_sent,
            "messages_failed": self.messages_failed,
            "reconnect_count": self.reconnect_count,
            "last_message_at": self.last_message_at,
            "connected_at": self.connected_at,
            "last_error": self.last_error,
        }


@dataclass
class BotContext:
    """单Bot完全隔离的上下文 — 无任何全局共享状态"""
    app_id: str
    app_secret: str
    bot_type: str = "chat_bot"
    name: str = ""

    state: BotState = BotState.IDLE
    metrics: BotMetrics = field(default_factory=BotMetrics)

    token: str = ""
    token_expire: float = 0.0

    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(10))
    max_concurrent: int = 10

    reconnect_delay: float = 5.0
    max_reconnect_delay: float = 300.0
    ws_ping_interval: float = 20.0

    _task: Optional[asyncio.Task] = None
    _ws: Optional[Any] = None
    _session: Optional[aiohttp.ClientSession] = None
    _running: bool = False
    _handle_tasks: set = field(default_factory=set)
    _seen_msg_ids: set = field(default_factory=set)

    def __post_init__(self):
        if not self.name:
            self.name = f"{self.bot_type}_{self.app_id[:12]}"
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self._seen_msg_ids = _load_seen_msg_ids(self.app_id)
        if self._seen_msg_ids:
            log.info(f"[{self.name}] loaded {len(self._seen_msg_ids)} seen msg ids from DB")

    @property
    def is_connected(self) -> bool:
        return self.state == BotState.CONNECTED

    def to_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "bot_type": self.bot_type,
            "name": self.name,
            "state": self.state.value,
            "metrics": self.metrics.to_dict(),
            "max_concurrent": self.max_concurrent,
        }


class FeishuAsync:
    """纯异步飞书API客户端 — 基于 aiohttp，无阻塞调用"""

    def __init__(self, session: aiohttp.ClientSession = None):
        self._session = session
        self._own_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
                connector=aiohttp.TCPConnector(limit=200, limit_per_host=20),
            )
            self._own_session = True
        return self._session

    async def close(self):
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    # ── Token ──────────────────────────────────────────────

    async def get_tenant_token(self, ctx: BotContext) -> str:
        """获取 tenant_access_token（带缓存，提前5分钟过期）"""
        now = time.time()
        if ctx.token and now < ctx.token_expire:
            return ctx.token

        session = await self._get_session()
        url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
        payload = {"app_id": ctx.app_id, "app_secret": ctx.app_secret}
        try:
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                token = data.get("tenant_access_token", "")
                expire = data.get("expire", 7200)
                if token:
                    ctx.token = token
                    ctx.token_expire = now + expire - 300
                    return token
                ctx.metrics.last_error = f"token api error: code={data.get('code')}"
        except Exception as e:
            ctx.metrics.last_error = f"token error: {e}"
            log.warning(f"[{ctx.name}] token error: {e}")
        return ""

    # ── WS Endpoint ────────────────────────────────────────

    async def get_ws_endpoint(self, ctx: BotContext) -> str:
        """获取飞书 WebSocket 动态端点 URL"""
        session = await self._get_session()
        payload = {"AppID": ctx.app_id, "AppSecret": ctx.app_secret}
        try:
            async with session.post(
                WS_ENDPOINT_URL, json=payload,
                headers={"User-Agent": "devops-bot/2.0"},
            ) as resp:
                data = await resp.json()
                if data.get("code") == 0:
                    return data.get("data", {}).get("URL", "")
                ctx.metrics.last_error = f"endpoint api error: code={data.get('code')}"
        except Exception as e:
            ctx.metrics.last_error = f"endpoint error: {e}"
            log.warning(f"[{ctx.name}] endpoint error: {e}")
        return ""

    # ── Message Send ───────────────────────────────────────

    async def _ensure_token(self, ctx: BotContext) -> str:
        token = await self.get_tenant_token(ctx)
        if not token:
            log.warning(f"[{ctx.name}] no token available")
        return token

    async def reply_text(self, ctx: BotContext, message_id: str, text: str) -> str:
        """回复纯文本消息"""
        token = await self._ensure_token(ctx)
        if not token:
            return ""
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply"
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": text[:2000]}, ensure_ascii=False),
        }
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                reply_id = data.get("data", {}).get("message_id", "")
                ctx.metrics.messages_sent += 1
                return reply_id
        except Exception as e:
            ctx.metrics.messages_failed += 1
            ctx.metrics.last_error = f"reply_text error: {e}"
            log.warning(f"[{ctx.name}] reply_text failed: {e}")
        return ""

    async def reply_card(self, ctx: BotContext, message_id: str, text: str,
                         title: str = "DevOps Bot", template: str = "blue") -> str:
        """回复交互卡片消息"""
        token = await self._ensure_token(ctx)
        if not token:
            return ""
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply"
        card = {
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text[:2000]}}],
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        }
        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                reply_id = data.get("data", {}).get("message_id", "")
                ctx.metrics.messages_sent += 1
                return reply_id
        except Exception as e:
            ctx.metrics.messages_failed += 1
            ctx.metrics.last_error = f"reply_card error: {e}"
            log.warning(f"[{ctx.name}] reply_card failed: {e}")
        return ""

    async def send_card_to_user(self, ctx: BotContext, open_id: str, text: str,
                                title: str = "DevOps Bot") -> str:
        """主动发送卡片给用户"""
        token = await self._ensure_token(ctx)
        if not token:
            return ""
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id"
        card = {
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text[:2000]}}],
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        }
        payload = {
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                msg_id = data.get("data", {}).get("message_id", "")
                ctx.metrics.messages_sent += 1
                return msg_id
        except Exception as e:
            ctx.metrics.messages_failed += 1
            log.warning(f"[{ctx.name}] send_card_to_user failed: {e}")
        return ""

    async def update_message(self, ctx: BotContext, message_id: str, text: str) -> bool:
        """编辑/替换已有消息"""
        token = await self._ensure_token(ctx)
        if not token:
            return False
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages/{message_id}"
        card = {
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text[:2000]}}],
            "header": {"title": {"tag": "plain_text", "content": "DevOps Bot"}, "template": "blue"},
        }
        payload = {"content": json.dumps(card, ensure_ascii=False)}
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.patch(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    ctx.metrics.messages_sent += 1
                    return True
        except Exception as e:
            ctx.metrics.messages_failed += 1
            log.warning(f"[{ctx.name}] update_message failed: {e}")
        return False

    async def add_reaction(self, ctx: BotContext, message_id: str,
                           emoji_type: str = "OK") -> bool:
        """添加 emoji 回应"""
        token = await self._ensure_token(ctx)
        if not token:
            return False
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reactions"
        payload = {"reaction_type": {"emoji_type": emoji_type}}
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                return resp.status == 200
        except Exception as e:
            log.warning(f"[{ctx.name}] add_reaction failed: {e}")
        return False

    async def get_chat_members(self, ctx: BotContext, chat_id: str) -> list:
        """获取群聊全部成员（分页）"""
        token = await self._ensure_token(ctx)
        if not token:
            return []
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/chats/{chat_id}/members"
        headers = {"Authorization": f"Bearer {token}"}
        members = []
        page_token = ""
        try:
            while True:
                params = {"page_size": "100"}
                if page_token:
                    params["page_token"] = page_token
                async with session.get(url, params=params, headers=headers) as resp:
                    data = await resp.json()
                    items = data.get("data", {}).get("items", [])
                    members.extend(items)
                    page_token = data.get("data", {}).get("page_token", "")
                    if not data.get("data", {}).get("has_more", False):
                        break
        except Exception as e:
            log.warning(f"[{ctx.name}] get_chat_members failed: {e}")
        return members

    async def get_user_name(self, ctx: BotContext, user_id: str) -> str:
        """获取用户昵称"""
        token = await self._ensure_token(ctx)
        if not token or not user_id:
            return ""
        session = await self._get_session()
        url = f"{FEISHU_BASE}/contact/v3/users/{user_id}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.get(url, params={"user_id_type": "user_id"}, headers=headers) as resp:
                data = await resp.json()
                return data.get("data", {}).get("user", {}).get("name", "")
        except Exception as e:
            log.warning(f"[{ctx.name}] get_user_name failed: {e}")
        return ""


# ── 消息解析 ───────────────────────────────────────────────

def parse_ws_message(raw: str) -> dict | None:
    """解析飞书 WS 原始 JSON 消息，返回标准化的消息 dict 或 None"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    msg_type = data.get("type", "")

    if msg_type == "ping":
        return {"ws_type": "ping"}
    if msg_type == "pong":
        return {"ws_type": "pong"}

    if msg_type == "event":
        event = data.get("event", {})
        header = event.get("header", {})
        event_type = header.get("event_type", "")

        if event_type == "im.message.receive_v1":
            message = event.get("event", {}).get("message", {})
            sender = event.get("event", {}).get("sender", {})
            msg_content = message.get("content", "{}") or "{}"
            msg_type_inner = message.get("message_type", "text") or "text"

            text = ""
            if msg_type_inner == "text":
                try:
                    text = json.loads(msg_content).get("text", "")
                except json.JSONDecodeError:
                    text = msg_content
            elif msg_type_inner == "post":
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
            sender_open_id = ""
            sender_id_obj = sender.get("sender_id", {})
            if sender_id_obj:
                sender_id = sender_id_obj.get("user_id", "") or ""
                sender_open_id = sender_id_obj.get("open_id", "") or ""

            return {
                "ws_type": "message",
                "type": "message",
                "text": text,
                "chat_id": message.get("chat_id", ""),
                "sender_id": sender_id,
                "sender_open_id": sender_open_id,
                "message_id": message.get("message_id", ""),
                "message_type": msg_type_inner,
            }

        return {"ws_type": "event", "event_type": event_type}

    return None


# ── Protobuf 帧解析辅助 ─────────────────────────────────────

def _get_header(headers, key: str) -> str:
    for h in headers:
        if h.key == key:
            return h.value
    return ""


def _parse_event(event_data: dict) -> dict | None:
    """从飞书事件 JSON 提取标准化消息 dict"""
    header = event_data.get("header", {})
    event_type = header.get("event_type", "")

    if event_type != "im.message.receive_v1":
        return None

    event = event_data.get("event", {})
    message = event.get("message", {})
    sender = event.get("sender", {})
    msg_content = message.get("content", "{}") or "{}"
    msg_type = message.get("message_type", "text") or "text"

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
    sender_open_id = ""
    sender_id_obj = sender.get("sender_id", {})
    if sender_id_obj:
        sender_id = sender_id_obj.get("user_id", "") or ""
        sender_open_id = sender_id_obj.get("open_id", "") or ""

    return {
        "type": "message",
        "text": text,
        "chat_id": message.get("chat_id", ""),
        "sender_id": sender_id,
        "sender_open_id": sender_open_id,
        "message_id": message.get("message_id", ""),
        "message_type": msg_type,
    }


# ── 单Bot WS 协程 ─────────────────────────────────────────

async def run_bot(ctx: BotContext, api: FeishuAsync,
                  on_message: Callable[[BotContext, dict], Awaitable[None]]):
    """单Bot的WS连接主协程 — 完整生命周期：鉴权→获取端点→连接→接收→重连

    特性:
      - 指数退避重连（5s → 300s）
      - 消息并发限流（asyncio.Semaphore）
      - 消息处理异常不扩散
      - 优雅停止（CancelledError）
    """
    ctx._running = True
    delay = ctx.reconnect_delay
    _cleanup_seen_msg_ids(ctx.app_id)

    async def _periodic_cleanup():
        while ctx._running:
            await asyncio.sleep(3600)
            if ctx._running:
                _cleanup_seen_msg_ids(ctx.app_id)

    cleanup_task = asyncio.create_task(_periodic_cleanup(), name=f"cleanup-{ctx.name}")

    while ctx._running:
        try:
            ctx.state = BotState.CONNECTING

            ws_url = await api.get_ws_endpoint(ctx)
            if not ws_url:
                log.warning(f"[{ctx.name}] no ws endpoint, retry in {delay:.0f}s")
                ctx.state = BotState.RECONNECTING
                await asyncio.sleep(delay)
                delay = min(delay * 2, ctx.max_reconnect_delay)
                continue

            log.info(f"[{ctx.name}] connecting to WS...")
            async with websockets.connect(
                ws_url,
                ping_interval=ctx.ws_ping_interval,
                ping_timeout=ctx.ws_ping_interval + 10,
                close_timeout=5,
            ) as ws:
                ctx._ws = ws
                ctx.state = BotState.CONNECTED
                ctx.metrics.connected_at = time.time()
                delay = ctx.reconnect_delay
                log.info(f"[{ctx.name}] connected")

                from lark_oapi.ws.pb.pbbp2_pb2 import Frame
                from lark_oapi.ws.enum import FrameType, MessageType

                async for raw in ws:
                    if not isinstance(raw, bytes):
                        continue

                    try:
                        frame = Frame()
                        frame.ParseFromString(raw)
                        ft = FrameType(frame.method)
                    except Exception:
                        continue

                    if ft == FrameType.CONTROL:
                        hs = frame.headers
                        type_ = _get_header(hs, "type")
                        if type_ == MessageType.PING.value:
                            pass
                        continue

                    if ft != FrameType.DATA:
                        continue

                    hs = frame.headers
                    type_ = _get_header(hs, "type")
                    if type_ != MessageType.EVENT.value:
                        continue

                    try:
                        payload_str = frame.payload.decode("utf-8") if frame.payload else ""
                        event_data = json.loads(payload_str)
                    except Exception:
                        continue

                    parsed = _parse_event(event_data)
                    if not parsed or not parsed.get("text"):
                        continue

                    msg_id = parsed.get("message_id", "")
                    if msg_id:
                        if msg_id in ctx._seen_msg_ids:
                            continue
                        ctx._seen_msg_ids.add(msg_id)
                        _persist_seen_msg_id(ctx.app_id, msg_id)
                        if len(ctx._seen_msg_ids) > 2000:
                            ctx._seen_msg_ids = set(list(ctx._seen_msg_ids)[-1000:])

                    log.info(f"[{ctx.name}] received message: chat={parsed.get('chat_id','')[:20]} text={parsed.get('text','')[:50]}")
                    ctx.metrics.messages_received += 1
                    ctx.metrics.last_message_at = time.time()
                    if not ctx.semaphore.locked() or ctx.semaphore._value > 0:
                        t = asyncio.create_task(
                            _safe_handle(ctx, api, on_message, parsed),
                            name=f"msg-{ctx.name}-{ctx.metrics.messages_received}",
                        )
                        ctx._handle_tasks.add(t)
                        t.add_done_callback(ctx._handle_tasks.discard)
                    else:
                        log.warning(f"[{ctx.name}] concurrency limit reached, dropping message")

        except asyncio.CancelledError:
            ctx._running = False
            ctx.state = BotState.STOPPED
            log.info(f"[{ctx.name}] cancelled, stopping")
            break
        except websockets.ConnectionClosed as e:
            ctx.metrics.reconnect_count += 1
            ctx.metrics.last_error = f"ws closed: code={e.code} reason={e.reason}"
            log.warning(f"[{ctx.name}] ws closed: {e.code}, reconnect in {delay:.0f}s")
            ctx.state = BotState.RECONNECTING
            _cleanup_seen_msg_ids(ctx.app_id)
            await asyncio.sleep(delay)
            delay = min(delay * 2, ctx.max_reconnect_delay)
        except Exception as e:
            ctx.metrics.reconnect_count += 1
            ctx.metrics.last_error = f"connection error: {e}"
            log.warning(f"[{ctx.name}] error: {e}, reconnect in {delay:.0f}s")
            ctx.state = BotState.RECONNECTING
            _cleanup_seen_msg_ids(ctx.app_id)
            await asyncio.sleep(delay)
            delay = min(delay * 2, ctx.max_reconnect_delay)

    ctx.state = BotState.STOPPED
    cleanup_task.cancel()
    log.info(f"[{ctx.name}] stopped")


async def _safe_handle(ctx: BotContext, api: FeishuAsync,
                       on_message: Callable, parsed: dict):
    """安全处理单条消息 — semaphore限流 + 异常隔离"""
    async with ctx.semaphore:
        try:
            await on_message(ctx, parsed)
        except Exception as e:
            ctx.metrics.messages_failed += 1
            ctx.metrics.last_error = f"handle error: {e}"
            log.warning(f"[{ctx.name}] message handle error: {e}")


# ── BotManager ─────────────────────────────────────────────

class BotManager:
    """编排所有Bot的启动/停止/健康检查/热加载

    设计:
      - 单 aiohttp.ClientSession 共享（连接池复用）
      - 每个 Bot 一个 asyncio.Task（完全隔离）
      - 支持运行时动态增删Bot（热加载）
      - 统一健康检查和指标采集
    """

    def __init__(self, max_concurrent_per_bot: int = 10):
        self._bots: dict[str, BotContext] = {}
        self._api: FeishuAsync | None = None
        self._on_message: Callable | None = None
        self._max_concurrent = max_concurrent_per_bot

    @property
    def api(self) -> FeishuAsync:
        return self._api

    def set_message_handler(self, handler: Callable[[BotContext, dict], Awaitable[None]]):
        self._on_message = handler

    async def start(self):
        """初始化共享 aiohttp 会话"""
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
            connector=aiohttp.TCPConnector(limit=300, limit_per_host=30, keepalive_timeout=30),
        )
        self._api = FeishuAsync(session=session)

    async def stop(self):
        """优雅停止所有Bot"""
        for ctx in self._bots.values():
            ctx._running = False
            if ctx._task and not ctx._task.done():
                ctx._task.cancel()

        for ctx in self._bots.values():
            if ctx._task and not ctx._task.done():
                try:
                    await asyncio.wait_for(ctx._task, timeout=10)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            pending = list(ctx._handle_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        if self._api:
            await self._api.close()

        self._bots.clear()
        log.info("BotManager: all bots stopped")

    async def add_bot(self, app_id: str, app_secret: str,
                      bot_type: str = "chat_bot",
                      name: str = "",
                      max_concurrent: int = 0) -> BotContext:
        """运行时动态添加Bot"""
        if app_id in self._bots:
            log.warning(f"Bot {app_id} already exists, skip")
            return self._bots[app_id]

        ctx = BotContext(
            app_id=app_id,
            app_secret=app_secret,
            bot_type=bot_type,
            name=name,
            max_concurrent=max_concurrent or self._max_concurrent,
        )
        self._bots[app_id] = ctx

        if self._on_message:
            ctx._task = asyncio.create_task(
                run_bot(ctx, self._api, self._on_message),
                name=f"bot-{ctx.name}",
            )
            log.info(f"BotManager: started bot {ctx.name}")

        return ctx

    async def remove_bot(self, app_id: str):
        """运行时动态移除Bot"""
        ctx = self._bots.pop(app_id, None)
        if not ctx:
            return
        ctx._running = False
        if ctx._task and not ctx._task.done():
            ctx._task.cancel()
            try:
                await ctx._task
            except asyncio.CancelledError:
                pass
        log.info(f"BotManager: removed bot {ctx.name}")

    async def load_from_db(self):
        """从 global_im_bot_config 表加载所有Bot配置（secret 从 agent_session 取）"""
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT bot_app_id, bot_type FROM global_im_bot_config WHERE is_active=1")
            rows = c.fetchall()

        added = 0
        for bot_app_id, bot_type in rows:
            if not bot_app_id:
                continue
            # 获取 bot secret：vod_bot 从 devops_agent_session，chat_bot 从 chat_agent_session 全局配置行
            bot_app_secret = ""
            with db_lock:
                c2 = db.cursor()
                if bot_type == "chat_bot":
                    c2.execute("SELECT im_app_secret FROM chat_agent_session WHERE im_app_id=? AND im_app_secret!='' AND (repo_key='' OR repo_key IS NULL) LIMIT 1", (bot_app_id,))
                else:
                    c2.execute("SELECT im_app_secret FROM devops_agent_session WHERE im_app_id=? AND im_app_secret!='' LIMIT 1", (bot_app_id,))
                srow = c2.fetchone()
                if srow and srow[0]:
                    bot_app_secret = srow[0]
            if not bot_app_secret:
                continue
            if bot_app_id in self._bots:
                continue
            await self.add_bot(bot_app_id, bot_app_secret, bot_type)
            added += 1

        log.info(f"BotManager: loaded {added} bot(s) from DB, total={len(self._bots)}")

    def get_bot(self, app_id: str) -> BotContext | None:
        return self._bots.get(app_id)

    def get_all_bots(self) -> dict[str, BotContext]:
        return dict(self._bots)

    def health_check(self) -> dict:
        """统一健康检查"""
        total = len(self._bots)
        connected = sum(1 for b in self._bots.values() if b.is_connected)
        total_received = sum(b.metrics.messages_received for b in self._bots.values())
        total_sent = sum(b.metrics.messages_sent for b in self._bots.values())
        total_failed = sum(b.metrics.messages_failed for b in self._bots.values())
        return {
            "total_bots": total,
            "connected": connected,
            "disconnected": total - connected,
            "total_messages_received": total_received,
            "total_messages_sent": total_sent,
            "total_messages_failed": total_failed,
            "bots": {app_id: ctx.to_dict() for app_id, ctx in self._bots.items()},
        }