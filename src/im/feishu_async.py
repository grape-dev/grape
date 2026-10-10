"""纯异步飞书多Bot连接管理器 — 支持100+Bot单进程隔离运行

架构:
  BotContext  — 单Bot完整隔离上下文（token缓存/并发槽/指标/WS连接）
  FeishuAsync — aiohttp 飞书API客户端（token/消息发送/WS端点获取）
  BotManager  — 编排所有Bot的启动/停止/健康检查/热加载

传输层使用 aiohttp + websockets 直连飞书 Connections OpenAPI；
WS 协议帧解析复用 lark-oapi 的 protobuf 定义（Frame / FrameType / MessageType）。
"""
import json, asyncio, time, logging, re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable, Optional, Any

import aiohttp
import websockets
from lark_oapi.ws.pb.pbbp2_pb2 import Frame
from lark_oapi.ws.enum import FrameType, MessageType

from src.config import log
from src.im.lark_md import to_lark_elements

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
    last_user_message_at: float = 0.0  # 仅用户消息更新（空闲超时检测用，PING 不更新）
    connected_at: float = 0.0
    last_error: str = ""
    disconnect_reasons: dict = field(default_factory=dict)  # {lease|ws_transport|shutdown: count}

    def to_dict(self) -> dict:
        return {
            "messages_received": self.messages_received,
            "messages_sent": self.messages_sent,
            "messages_failed": self.messages_failed,
            "reconnect_count": self.reconnect_count,
            "last_message_at": self.last_message_at,
            "connected_at": self.connected_at,
            "last_error": self.last_error,
            "disconnect_reasons": dict(self.disconnect_reasons),
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

    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(20))
    max_concurrent: int = 20

    reconnect_delay: float = 2.0
    max_reconnect_delay: float = 15.0
    # 本次停止/移出原因（重连账本，cluster-im-lease-stability-design.md §2.5）：
    #   lease=宽限期/优雅让渡断开；shutdown=stop()；否则 ws_transport
    stop_reason: str = ""
    # websockets 协议级 ping 禁用（None）：飞书 WS 不回协议级 pong，会导致 ping_timeout 断连。
    # 保活依赖飞书应用层 PING/PONG（run_bot 里收到 PING 帧回 PONG）。
    ws_ping_interval: float = 0.0  # 0 → websockets.connect(ping_interval=None) 禁用协议级 ping

    _task: Optional[asyncio.Task] = None
    _ws: Optional[Any] = None
    _session: Optional[aiohttp.ClientSession] = None
    _running: bool = False
    _handle_tasks: set = field(default_factory=set)
    _seen_msg_ids: set = field(default_factory=set)
    _lease_token: str = ""  # 本实例持有的 lease token（问题 A：重连后检查）

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


def _fetch_gitcode_collaborators(owner: str, repo: str, api_token: str) -> list:
    """分页拉取 GitCode 仓库协作者（同步 urllib，快速场景）。

    独立为模块级函数便于单测注入替换（避免 mock urllib.request.urlopen
    在 async 上下文中的兼容问题）。
    """
    collabs = []
    import urllib.request
    page = 1
    while True:
        req = urllib.request.Request(
            f"https://gitcode.com/api/v5/repos/{owner}/{repo}/collaborators?per_page=100&page={page}",
            headers={"Authorization": f"Bearer {api_token}"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if not data:
            break
        collabs.extend(data)
        page += 1
    return collabs


def _fetch_github_collaborators(owner: str, repo: str, api_token: str) -> list:
    """分页拉取 GitHub 仓库协作者（同步 urllib，快速场景）。"""
    collabs = []
    import urllib.request
    page = 1
    while True:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/collaborators?per_page=100&page={page}",
            headers={"Authorization": f"Bearer {api_token}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "Grape-DevOps"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if not data:
            break
        collabs.extend(data)
        page += 1
    return collabs


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

    # WS endpoint 永久失败错误码（凭证无效/App 不存在，重试无意义）
    _WS_PERMANENT_ERROR_CODES = frozenset({10002, 10014, 10003})  # app not found / secret invalid / app disabled

    async def get_ws_endpoint(self, ctx: BotContext) -> tuple[str, int]:
        """获取飞书 WebSocket 动态端点 URL。

        返回 (url, error_code)。error_code=0 表示成功，非0 表示飞书 API 错误码。
        - 10014 = app secret invalid（永久失效）
        - 10002 = app not found（永久失效）
        - 其他 = 临时/未知
        """
        session = await self._get_session()
        payload = {"AppID": ctx.app_id, "AppSecret": ctx.app_secret}
        try:
            async with session.post(
                WS_ENDPOINT_URL, json=payload,
                headers={"User-Agent": "devops-bot/2.0"},
            ) as resp:
                data = await resp.json()
                code = data.get("code", 0)
                if code == 0:
                    return data.get("data", {}).get("URL", ""), 0
                ctx.metrics.last_error = f"endpoint api error: code={code}"
                return "", code
        except Exception as e:
            ctx.metrics.last_error = f"endpoint error: {e}"
            log.warning(f"[{ctx.name}] endpoint error: {e}")
        return "", -1

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
                _code = data.get("code", 0)
                reply_id = data.get("data", {}).get("message_id", "")
                if _code != 0:
                    ctx.metrics.messages_failed += 1
                    ctx.metrics.last_error = f"reply_text api code={_code}: {data.get('msg','')}"
                    log.warning(f"[{ctx.name}] reply_text failed: code={_code} msg={data.get('msg','')} text={text[:50]}")
                else:
                    ctx.metrics.messages_sent += 1
                    log.info(f"[{ctx.name}] reply_text ok: text={text[:50]}")
                return reply_id
        except Exception as e:
            ctx.metrics.messages_failed += 1
            ctx.metrics.last_error = f"reply_text error: {e}"
            log.warning(f"[{ctx.name}] reply_text exception: {e}")
        return ""

    async def reply_card(self, ctx: BotContext, message_id: str, text: str,
                         title: str = "DevOps Bot", template: str = "blue") -> str:
        """回复交互卡片消息（自动追加[帮助]按钮）"""
        token = await self._ensure_token(ctx)
        if not token:
            return ""
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply"
        elements = to_lark_elements(text, 2000)
        # 每条卡片消息底部追加[帮助]按钮
        elements.append({
            "tag": "action",
            "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "❓ 帮助"}, "value": {"cmd": "/help"}}]
        })
        card = {
            "elements": elements,
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
                _code = data.get("code", 0)
                if _code != 0:
                    ctx.metrics.messages_failed += 1
                    ctx.metrics.last_error = f"reply_card api code={_code}: {data.get('msg','')}"
                    log.warning(f"[{ctx.name}] reply_card api error: code={_code} msg={data.get('msg','')} msg_id={message_id}")
                    return ""
                reply_id = data.get("data", {}).get("message_id", "")
                ctx.metrics.messages_sent += 1
                log.debug(f"[{ctx.name}] reply_card: {text[:50]}")
                return reply_id
        except Exception as e:
            ctx.metrics.messages_failed += 1
            ctx.metrics.last_error = f"reply_card error: {e}"
            log.warning(f"[{ctx.name}] reply_card failed: {e}")
        return ""

    async def download_image(self, ctx: BotContext, image_key: str, message_id: str = "") -> bytes:
        """Download image from Feishu API. Returns raw bytes.

        优先用消息资源 API（GET /im/v1/messages/{msg_id}/resources/{file_key}?type=image），
        降级用图片 API（GET /im/v1/images/{image_key}?image_type=message）。
        """
        token = await self._ensure_token(ctx)
        if not token:
            return b""
        session = await self._get_session()
        headers = {"Authorization": f"Bearer {token}"}

        # 方法1：消息资源 API（需要 message_id，更可靠）
        if message_id and message_id.startswith("om_"):
            url1 = f"{FEISHU_BASE}/im/v1/messages/{message_id}/resources/{image_key}?type=image"
            try:
                async with session.get(url1, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        log.info(f"[{ctx.name}] downloaded image via resources API: {len(data)} bytes")
                        return data
                    else:
                        log.warning(f"[{ctx.name}] resources API download: HTTP {resp.status}, trying images API")
            except Exception as e:
                log.warning(f"[{ctx.name}] resources API error: {e}, trying images API")

        # 方法2：图片 API（降级方案）
        url2 = f"{FEISHU_BASE}/im/v1/images/{image_key}?image_type=message"
        try:
            async with session.get(url2, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    log.info(f"[{ctx.name}] downloaded image via images API: {len(data)} bytes")
                    return data
                else:
                    body_text = await resp.text()
                    log.warning(f"[{ctx.name}] download image failed: HTTP {resp.status} - {body_text[:200]}")
                    return b""
        except Exception as e:
            log.warning(f"[{ctx.name}] download image error: {e}")
            return b""

    async def download_file(self, ctx: BotContext, file_key: str, message_id: str = "", file_type: str = "file") -> bytes:
        """Download file (video/audio/file) from Feishu API. Returns raw bytes.

        Uses the message resources API: GET /im/v1/messages/{msg_id}/resources/{file_key}?type={file_type}
        For videos, type=video; for generic files, type=file.
        """
        token = await self._ensure_token(ctx)
        if not token:
            return b""
        session = await self._get_session()
        headers = {"Authorization": f"Bearer {token}"}

        if message_id and message_id.startswith("om_"):
            url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/resources/{file_key}?type={file_type}"
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        log.info(f"[{ctx.name}] downloaded {file_type} via resources API: {len(data)} bytes")
                        return data
                    else:
                        body_text = await resp.text()
                        log.warning(f"[{ctx.name}] download {file_type} failed: HTTP {resp.status} - {body_text[:200]}")
            except Exception as e:
                log.warning(f"[{ctx.name}] download {file_type} error: {e}")
        else:
            log.warning(f"[{ctx.name}] download {file_type}: no valid message_id for resources API")

        return b""

    async def send_card_to_chat(self, ctx: BotContext, chat_id: str, text: str,
                                    title: str = "DevOps Bot", template: str = "blue",
                                    fallback_open_id: str = "", actions: list | None = None) -> str:
            # 主动发卡片消息到聊天（非 reply，主窗口可见）
            # fallback_open_id: P2P 私聊场景 chat_id 方式可能被飞书拒绝(code=230001)，
            # 此时用 open_id 重发。
            token = await self._ensure_token(ctx)
            if not token:
                return ""
            session = await self._get_session()
            elements = to_lark_elements(text, 2000)
            elements.append({
                "tag": "action",
                "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "❓ 帮助"}, "value": {"cmd": "/help"}}]
            })
            if actions:
                elements.append({"tag": "action", "actions": actions})
            card = {
                "elements": elements,
                "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            }
            card_content = json.dumps(card, ensure_ascii=False)
            headers = {"Authorization": f"Bearer {token}"}

            # 第一次尝试：chat_id 方式
            url = f"{FEISHU_BASE}/im/v1/messages?receive_id={chat_id}&receive_id_type=chat_id"
            payload = {"msg_type": "interactive", "content": card_content}
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json()
                    code = data.get("code", -1)
                    msg_id = data.get("data", {}).get("message_id", "")
                    if code == 0:
                        ctx.metrics.messages_sent += 1
                        log.info(f"[{ctx.name}] send_card_to_chat ok: msg_id={msg_id} text={text[:50]}")
                        return msg_id
                    # P2P 私聊 chat_id 被拒绝 → 用 open_id 回退
                    if code == 230001 and fallback_open_id:
                        log.info(f"[{ctx.name}] chat_id rejected (P2P), retrying with open_id={fallback_open_id[:20]}")
                    else:
                        log.warning(f"[{ctx.name}] send_card_to_chat failed: code={code} msg={data.get('msg','')} chat_id={chat_id}")
                        return ""
            except Exception as e:
                ctx.metrics.messages_failed += 1
                ctx.metrics.last_error = f"send_card_to_chat error: {e}"
                log.warning(f"[{ctx.name}] send_card_to_chat exception: {e}")
                return ""

            # 回退：open_id 方式
            if fallback_open_id:
                url2 = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id"
                payload2 = {"receive_id": fallback_open_id, "msg_type": "interactive", "content": card_content}
                try:
                    async with session.post(url2, json=payload2, headers=headers) as resp:
                        data = await resp.json()
                        code = data.get("code", -1)
                        msg_id = data.get("data", {}).get("message_id", "")
                        if code == 0:
                            ctx.metrics.messages_sent += 1
                            log.info(f"[{ctx.name}] send_card_to_chat(open_id fallback) ok: msg_id={msg_id} text={text[:50]}")
                            return msg_id
                        else:
                            log.warning(f"[{ctx.name}] send_card_to_chat open_id fallback failed: code={code} msg={data.get('msg','')}")
                except Exception as e:
                    ctx.metrics.messages_failed += 1
                    log.warning(f"[{ctx.name}] send_card_to_chat open_id fallback exception: {e}")
            return ""

    async def send_text_to_user(self, ctx: BotContext, open_id: str, text: str) -> str:
        """主动发送文本消息给用户"""
        token = await self._ensure_token(ctx)
        if not token:
            return ""
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id"
        payload = {
            "receive_id": open_id,
            "msg_type": "text",
            "content": json.dumps({"text": text[:2000]}, ensure_ascii=False),
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
            log.warning(f"[{ctx.name}] send_text_to_user failed: {e}")
        return ""

    _SHORTCUT_MENU = [
        {"name": "📖 帮助", "action": {"tag": "message", "text": "/help"}},
        {"name": "🔄 新建对话", "action": {"tag": "message", "text": "/new"}},
        {"name": "📁 选择仓库", "action": {"tag": "message", "text": "/repo"}},
        {"name": "📋 任务列表", "action": {"tag": "message", "text": "/task"}},
        {"name": "📍 当前位置", "action": {"tag": "message", "text": "/where"}},
        {"name": "🧹 清除缓存", "action": {"tag": "message", "text": "/clean"}},
        {"name": "❌ 取消", "action": {"tag": "message", "text": "/cancel"}},
    ]

    async def set_bot_menu(self, ctx: BotContext) -> bool:
        """配置飞书 Bot 对话框上方快捷菜单（消息快捷菜单）

        ⚠️ 飞书开放平台暂无公开 API 设置 Bot 快捷菜单，
        需要在飞书智能体后台 / 开放平台后台手动配置。
        此方法仅用于检查 API 可用性。
        """
        log.info(f"[{ctx.name}] set_bot_menu: 飞书暂无公开菜单 API，请在智能体后台手动配置快捷提问")
        return False

    async def send_card_to_user(self, ctx: BotContext, open_id: str, text: str,
                                title: str = "DevOps Bot", actions: list | None = None) -> str:
        """主动发送卡片给用户"""
        token = await self._ensure_token(ctx)
        if not token:
            return ""
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id"
        elements = to_lark_elements(text, 2000)
        if actions:
            elements.append({"tag": "action", "actions": actions})
        card = {
            "elements": elements,
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
                log.info(f"[{ctx.name}] send_card_to_user ok: msg_id={msg_id} text={text[:50]}")
                return msg_id
        except Exception as e:
            ctx.metrics.messages_failed += 1
            log.warning(f"[{ctx.name}] send_card_to_user failed: {e}")
        return ""

    async def send_quick_cmd_card(self, ctx: BotContext, open_id: str) -> str:
            """发送欢迎卡片，包含快捷命令按钮

            用户点击按钮 → 飞书推送 card.action.trigger 事件 →
            _parse_card_action 解析 cmd → 等同于输入命令文本
            """
            token = await self._ensure_token(ctx)
            if not token:
                return ""
            session = await self._get_session()
            url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id"
            buttons = [
                {"tag": "button", "text": {"tag": "plain_text", "content": "📖 帮助"}, "value": {"cmd": "/help"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "🔄 新建对话"}, "value": {"cmd": "/new"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "📁 选择仓库"}, "value": {"cmd": "/repo"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "💬 选择会话"}, "value": {"cmd": "/chat"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "📋 任务列表"}, "value": {"cmd": "/task"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "📍 当前位置"}, "value": {"cmd": "/where"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "🧹 清除缓存"}, "value": {"cmd": "/clean"}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "❌ 取消"}, "value": {"cmd": "/cancel"}},
            ]
            card = {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": "🤖 Grape DevOps"}, "template": "blue"},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": "欢迎使用 Grape DevOps Bot 👋\n点击下方按钮快速执行命令："}},
                    {"tag": "hr"},
                    {"tag": "action", "actions": buttons[:3]},
                    {"tag": "action", "actions": buttons[3:6]},
                    {"tag": "action", "actions": buttons[6:]},
                ],
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
                    log.info(f"[{ctx.name}] send_quick_cmd_card sent to {open_id[:20]}: msg_id={msg_id[:20]}")
                    return msg_id
            except Exception as e:
                ctx.metrics.messages_failed += 1
                log.warning(f"[{ctx.name}] send_quick_cmd_card failed: {e}")
            return ""

    async def update_message(self, ctx: BotContext, message_id: str, text: str) -> bool:
        """编辑/替换已有消息"""
        token = await self._ensure_token(ctx)
        if not token:
            return False
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages/{message_id}"
        card = {
            "elements": to_lark_elements(text, 2000),
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
        """获取群聊全部成员（分页），返回规范化的 [{name, open_id}]。

        与旧同步版 feishu.get_chat_members 输出格式一致：每条为
        {"name": str, "open_id": str}（open_id 取 member_id 字段）。
        """
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
                params = {"member_id_type": "open_id", "page_size": "100"}
                if page_token:
                    params["page_token"] = page_token
                async with session.get(url, params=params, headers=headers) as resp:
                    data = await resp.json()
                    items = data.get("data", {}).get("items", [])
                    for m in items:
                        members.append({
                            "name": m.get("name", ""),
                            "open_id": m.get("member_id", ""),
                        })
                    page_token = data.get("data", {}).get("page_token", "")
                    if not data.get("data", {}).get("has_more", False) or not page_token:
                        break
        except Exception as e:
            log.warning(f"[{ctx.name}] get_chat_members failed: {e}")
        return members

    async def sync_repo_feishu_mapping(self, ctx: BotContext, repo_key: str,
                                       chat_id: str) -> int:
        """异步同步群成员 ↔ VCS 协作者映射（repo_key 带平台前缀）。

        等价值于旧同步 feishu.sync_repo_feishu_mapping（feishu.py:360-418）：
          - members = await self.get_chat_members(ctx, chat_id)
          - api_token = 按平台分流获取（GitHub → get_github_token，GitCode → get_bot_token_by_repo / 全局）
          - 协作者分页拉取（按平台选 API）→ 按昵称匹配写 unified_user_mapping
        """
        members = await self.get_chat_members(ctx, chat_id)
        if not members:
            return 0

        from src.repo_key import split_owner_repo, to_repo_key, parse_repo_key
        owner, repo = split_owner_repo(repo_key)
        if not owner or not repo:
            return 0
        repo_key = to_repo_key(repo_key)
        provider, _, _ = parse_repo_key(repo_key)

        # 按 provider 分流获取 token
        if provider == "github":
            from src.user.db import get_github_token
            api_token = get_github_token(repo_key=repo_key) or ""
        else:
            from src.user.db import get_bot_token_by_repo
            api_token = get_bot_token_by_repo(owner, repo)
            if not api_token:
                try:
                    import json as _json, os as _os
                    _gbt_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                                              "..", "..", "configs", "global-bot-token.json")
                    if _os.path.exists(_gbt_path):
                        with open(_gbt_path, "r", encoding="utf-8") as _f:
                            _gbt = _json.load(_f)
                        api_token = _gbt.get("gitcode_api_token", "") or ""
                except Exception:
                    pass
        if not api_token:
            log.warning(f"[{ctx.name}] sync mapping: no bot token for {repo_key}, skipping")
            return 0

        try:
            if provider == "github":
                collabs = _fetch_github_collaborators(owner, repo, api_token)
            else:
                collabs = _fetch_gitcode_collaborators(owner, repo, api_token)
        except Exception as e:
            log.warning(f"[{ctx.name}] sync mapping: failed to fetch collaborators for {repo_key}: {e}")
            return 0

        from src.session_manager import update_unified_user_info
        matched = 0
        for collab in collabs:
            # GitCode 协作者字段: nick_name/username；GitHub: login/name
            nick = collab.get("nick_name", "") or collab.get("login", "")
            login = collab.get("username", "") or collab.get("login", "")
            if not nick or not login:
                continue
            for m in members:
                if m.get("name") == nick:
                    update_unified_user_info(login, m["open_id"], nick, gitcode_nickname=nick, app_id=ctx.app_id)
                    matched += 1
                    break
        log.info(f"[{ctx.name}] sync mapping: {repo_key} -> {matched} users matched from {len(members)} group members")
        return matched

    async def get_user_name(self, ctx: BotContext, user_id: str, user_id_type: str = "user_id") -> str:
        token = await self._ensure_token(ctx)
        if not token or not user_id:
            return ""
        session = await self._get_session()
        url = f"{FEISHU_BASE}/contact/v3/users/{user_id}"
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.get(url, params={"user_id_type": user_id_type}, headers=headers) as resp:
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


def _build_ack_frame(frame) -> bytes:
    """构造飞书 WS ack 帧：payload 替换为 {"code":200}，其余字段不变，原样回传。

    飞书 WS 长连接要求对每条 DATA(EVENT) 帧回传 ack，否则认为消息未送达，
    会用新的 message_id 反复投递（导致消息重复消费）。ack 帧 = 原帧 payload
    替换为 ack 响应后序列化回传，headers（含 message_id）保持不变。
    """
    frame.payload = json.dumps({"code": 200}).encode("utf-8")
    return frame.SerializeToString()


def _parse_card_action(event_data: dict) -> dict | None:
    """解析飞书消息快捷菜单 / 卡片按钮点击事件（card.action.trigger）

    快捷菜单点击或卡片按钮点击时，飞书推送 card.action.trigger 事件。
    提取 action.value 中的 cmd 字段作为命令文本，等同于用户发送了该命令。
    """
    header = event_data.get("header", {})
    event_type = header.get("event_type", "")
    # CARD 类型事件可能无 header，直接从 event_data 取 action
    if event_type != "card.action.trigger":
        if "action" not in event_data and "action" not in event_data.get("event", {}):
            return None
    event = event_data.get("event", {})
    action = event.get("action", event_data.get("action", {}))
    value_raw = action.get("value", "{}")
    try:
        value = json.loads(value_raw) if isinstance(value_raw, str) else (value_raw or {})
    except (json.JSONDecodeError, TypeError):
        value = {}
    cmd = (value.get("cmd") or "").strip()
    if not cmd:
        log.info(f"card.action.trigger without cmd: {value_raw}")
        return None
    # card.action.trigger 字段结构：
    #   operator.open_id = 点击用户
    #   context.open_chat_id / open_message_id
    operator = event.get("operator", {}) or {}
    context_ = event.get("context", {}) or {}
    sender_open_id = operator.get("open_id", "") or event.get("open_id") or event_data.get("open_id") or ""
    chat_id = context_.get("open_chat_id") or event.get("open_chat_id", event.get("chat_id", "")) or ""
    msg_id = context_.get("open_message_id") or event.get("open_message_id") or ""
    return {
        "type": "message",
        "text": cmd,
        "chat_id": chat_id,
        "chat_type": "p2p",
        "sender_id": event.get("user_id") or event_data.get("user_id") or "",
        "sender_open_id": sender_open_id,
        "message_id": header.get("event_id") or msg_id,  # event_id 唯一，用于去重
        "_reply_id": msg_id,  # 真实消息 ID，用于回复
        "message_type": "text",
        "_card_value": value if isinstance(value, dict) else {},  # 按钮附加字段（cmd 之外的 目标上下文）
    }


def _parse_event(event_data: dict) -> dict | None:
    """从飞书事件 JSON 提取标准化消息 dict"""
    header = event_data.get("header", {})
    event_type = header.get("event_type", "")

    if event_type != "im.message.receive_v1":
        # 尝试解析 card.action.trigger（可能无 header，直接从 data 取）
        if event_type == "card.action.trigger" or header.get("event_type") is None:
            return _parse_card_action(event_data)
        return None

    event = event_data.get("event", {})
    message = event.get("message", {})
    sender = event.get("sender", {})
    msg_content = message.get("content", "{}") or "{}"
    msg_type = message.get("message_type", "text") or "text"

    text = ""
    image_key = ""
    file_key = ""
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

    elif msg_type == "image":
        # 图片消息：提取 image_key，后续由 handler 下载并理解
        try:
            img_data = json.loads(msg_content)
            image_key = img_data.get("image_key", "")
            if image_key:
                text = "[图片]"
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    elif msg_type == "media":
        # 视频/文件消息：提取 file_key，后续由 handler 下载并理解
        try:
            media_data = json.loads(msg_content)
            file_key = media_data.get("file_key", "")
            if file_key:
                text = "[视频]"
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    elif msg_type == "file":
        # 文件消息：提取 file_key
        try:
            file_data = json.loads(msg_content)
            file_key = file_data.get("file_key", "")
            if file_key:
                text = "[文件]"
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

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
        "chat_type": message.get("chat_type", ""),  # p2p=私聊 / group=群聊
        "sender_id": sender_id,
        "sender_open_id": sender_open_id,
        "message_id": message.get("message_id", ""),
        "message_type": msg_type,
        "image_key": image_key if msg_type == "image" else "",
        "file_key": file_key if msg_type in ("media", "file") else "",
    }


# ── 单Bot WS 协程 ─────────────────────────────────────────

async def _disable_bot(app_id: str):
    """永久停用 Bot（凭证失效/App 不存在），写入 DB is_active=0 防止重启后重载。
    同时释放 lease（遗漏修复 3）——防止 lease 脏数据。
    """
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE global_im_bot_config SET is_active=0, lease_token=NULL, lease_expires_at=NULL, lease_owner=NULL, lease_acquired_at=NULL WHERE bot_app_id=?", (app_id,))
            if c.rowcount > 0:
                db.commit()
                log.info(f"BotManager: disabled bot {app_id}")
    except Exception as e:
        log.warning(f"BotManager: disable bot {app_id} failed: {e}")


# ── 一致性哈希环 / lease 过期解析（cluster-im-lease-stability-design.md §2.2/§2.3）──


def _im_ring(instances: list, vnodes: int = 50) -> list:
    """一致性哈希环：[(hash, instance)] 升序。实例均匀打点，增删只影响邻近。"""
    import hashlib
    ring = []
    for inst in instances:
        for _vn in range(vnodes):
            h = int(hashlib.md5(f"{inst}:{_vn}".encode()).hexdigest()[:8], 16)
            ring.append((h, inst))
    ring.sort()
    return ring


def _im_owner(ring: list, app_id: str) -> str:
    """app_id 顺时针落到的实例（归属）。ring 空 → ''。"""
    import hashlib
    if not ring:
        return ""
    h = int(hashlib.md5(app_id.encode()).hexdigest()[:8], 16)
    for hv, inst in ring:
        if hv >= h:
            return inst
    return ring[0][1]


def _lease_expiry_ts(exp: str) -> float:
    """'YYYY-MM-DD HH:MM:SS'(UTC) → epoch 秒；解析失败返回 0。"""
    try:
        from datetime import datetime, timezone
        return datetime.strptime(exp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return 0.0


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

            ws_url, err_code = await api.get_ws_endpoint(ctx)
            if not ws_url:
                if err_code in FeishuAsync._WS_PERMANENT_ERROR_CODES:
                    log.warning(f"[{ctx.name}] permanent WS failure (code={err_code}), disabling bot")
                    try:
                        ctx.metrics.last_error = f"permanent failure: code={err_code}, bot disabled"
                        await _disable_bot(ctx.app_id)
                    except Exception:
                        pass
                    ctx.stop_reason = "ws_transport"
                    ctx._running = False
                    break
                log.warning(f"[{ctx.name}] no ws endpoint, retry in {delay:.0f}s")
                ctx.state = BotState.RECONNECTING
                await asyncio.sleep(delay)
                delay = min(delay * 2, ctx.max_reconnect_delay)
                continue

            log.info(f"[{ctx.name}] connecting to WS...")
            # ping_interval=None 禁用 websockets 协议级 ping（飞书不回协议级 pong 会触发超时断连）；
            # 保活由飞书应用层 PING/PONG 机制负责（上方收到 PING 帧回 PONG）
            _ws_ping = ctx.ws_ping_interval if ctx.ws_ping_interval > 0 else None
            async with websockets.connect(
                ws_url,
                ping_interval=_ws_ping,
                ping_timeout=(_ws_ping + 10) if _ws_ping else None,
                close_timeout=5,
            ) as ws:
                ctx._ws = ws
                ctx.state = BotState.CONNECTED
                ctx.metrics.connected_at = time.time()
                # [PITFALL 修复] 必须在 WS 连接建立时初始化 last_message_at / last_user_message_at，
                # 否则默认值 0 导致空闲超时检测立即触发（time() - 0 > 300）。
                # last_user_message_at 初始化为当前时间，给新连接 5 分钟宽限期。
                ctx.metrics.last_message_at = time.time()
                ctx.metrics.last_user_message_at = time.time()
                delay = ctx.reconnect_delay
                log.info(f"[{ctx.name}] connected")

                # 问题 A：重连后检查 lease 是否还在自己手里
                if ctx._lease_token:
                    try:
                        from src.session_manager import db, db_lock
                        with db_lock:
                            c = db.cursor()
                            c.execute(
                                "SELECT lease_token FROM global_im_bot_config WHERE bot_app_id=?",
                                (ctx.app_id,),
                            )
                            row = c.fetchone()
                            db_token = row[0] if row else None
                        if db_token != ctx._lease_token:
                            log.warning(f"[{ctx.name}] lease lost during reconnect, disconnecting")
                            ctx.stop_reason = "lease"
                            ctx._running = False
                            break
                    except Exception as e:
                        log.warning(f"[{ctx.name}] lease check error: {e}")

                # 主动 PING 保活（lark-oapi SDK 同款机制）：每 120 秒发应用层 PING 帧，
                # 飞书收到后回 PONG 保持连接活跃。不发 PING → 飞书判定死连接 → 停止推消息。
                _ws_service_id = 0
                try:
                    _ws_service_id = int(ws_url.split("fpid=")[1].split("&")[0]) if "fpid=" in ws_url else 0
                except Exception:
                    pass

                async def _ping_keepalive():
                    while ctx._running and ws is ctx._ws:
                        try:
                            await asyncio.sleep(120)
                            if ws is not ctx._ws:
                                break
                            # 空闲超时检测：如果超过 5 分钟没收到用户消息，
                            # 说明飞书已停推事件（WS URL 过期或飞书侧降级），
                            # 主动断开触发重连获取新 URL。
                            # 用 last_user_message_at 而非 last_message_at，
                            # 因为 PING 也会更新 last_message_at 导致检测失效。
                            idle_secs = time.time() - ctx.metrics.last_user_message_at
                            if idle_secs > 300:
                                log.warning(f"[{ctx.name}] idle {idle_secs:.0f}s > 300s, forcing reconnect")
                                try:
                                    await ws.close()
                                except Exception:
                                    pass
                                break
                            ping = Frame()
                            ping.method = FrameType.CONTROL.value
                            ping.service = _ws_service_id
                            ping.SeqID = 0
                            ping.LogID = 0
                            ping.headers.add(key="type", value=MessageType.PING.value)
                            await ws.send(ping.SerializeToString())
                            log.info(f"[{ctx.name}] keepalive ping sent")
                        except asyncio.CancelledError:
                            break
                        except Exception as _pe:
                            log.warning(f"[{ctx.name}] keepalive ping failed: {_pe}")
                            break

                _ping_task = asyncio.create_task(_ping_keepalive(), name=f"ping-{ctx.name}")

                try:
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
                                # 回 PONG 保活：飞书发应用层 PING，不回 PONG 会被判定死连接，
                                # 停止推送消息（WS 假死根因 — TCP 未断但消息不再到达）
                                try:
                                    for h in frame.headers:
                                        if h.key == "type":
                                            h.value = MessageType.PONG.value
                                    await ws.send(frame.SerializeToString())
                                except Exception as _pe:
                                    log.warning(f"[{ctx.name}] send pong failed: {_pe}")
                                ctx.metrics.last_message_at = time.time()
                            continue

                        if ft != FrameType.DATA:
                            continue

                        hs = frame.headers
                        type_ = _get_header(hs, "type")
                        if type_ not in (MessageType.EVENT.value, MessageType.CARD.value):
                            continue

                        try:
                            payload_str = frame.payload.decode("utf-8") if frame.payload else ""
                            event_data = json.loads(payload_str)
                            log.info(f"[WS] event_type={event_data.get('header',{}).get('event_type','?')} type={type_} keys={list(event_data.keys())}")
                        except Exception as e:
                            # 解析失败也需 ack，否则飞书重推导致死循环
                            try:
                                await ws.send(_build_ack_frame(frame))
                            except Exception:
                                pass
                            continue

                        # 统一 ack：无论消息是否被处理（seen 命中 / 无文本 / 处理中），
                        # 都必须回传 ack 帧，否则飞书会用新的 message_id 反复投递该消息。
                        try:
                            await ws.send(_build_ack_frame(frame))
                        except Exception as e:
                            log.warning(f"[{ctx.name}] send ack failed: {e}")

                        parsed = _parse_event(event_data)
                        if not parsed or not parsed.get("text"):
                            # CARD 类型 → 卡片按钮点击事件（card.action.trigger）
                            if type_ == MessageType.CARD.value:
                                parsed = _parse_card_action(event_data)
                                if parsed and parsed.get("text"):
                                    log.info(f"[WS] card action parsed: cmd={parsed['text'][:30]}")
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
                        ctx.metrics.last_user_message_at = time.time()
                        # 不检查 semaphore —— asyncio.Semaphore 自带 FIFO 排队，
                        # 满时自动阻塞等待，不存在丢消息。
                        t = asyncio.create_task(
                            _safe_handle(ctx, api, on_message, parsed),
                            name=f"msg-{ctx.name}-{ctx.metrics.messages_received}",
                        )
                        ctx._handle_tasks.add(t)
                        t.add_done_callback(ctx._handle_tasks.discard)
                finally:
                    _ping_task.cancel()
                    try:
                        await _ping_task
                    except (asyncio.CancelledError, Exception):
                        pass

        except asyncio.CancelledError:
            ctx._running = False
            ctx.state = BotState.STOPPED
            ctx.stop_reason = ctx.stop_reason or "shutdown"
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
    # 重连账本：记录本次断开归因（lease / ws_transport / shutdown）
    _reason = ctx.stop_reason or "ws_transport"
    ctx.metrics.disconnect_reasons[_reason] = ctx.metrics.disconnect_reasons.get(_reason, 0) + 1
    ctx.stop_reason = ""
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

    def __init__(self, max_concurrent_per_bot: int = 10, lease_store=None):
        self._bots: dict[str, BotContext] = {}
        self._api: FeishuAsync | None = None
        self._on_message: Callable | None = None
        self._max_concurrent = max_concurrent_per_bot
        self._lease_store = lease_store  # BotLeaseStore 或 None（旧模式）
        self._held_leases: dict[str, str] = {}  # {app_id: lease_token} 本实例持有的 lease
        self._eligible_bots: dict[str, dict] = {}  # 登记的 bot 配置（IDLE，未连接）
        self._grace_deadlines: dict[str, float] = {}  # {app_id: timestamp} 宽限期到期时间（问题 C）
        # cluster-im-lease-stability-design.md：抗抖/粘性/账本
        self._instance_id = (lease_store.instance_id if lease_store else "") or ""
        self._renew_fails: dict[str, int] = {}  # {app_id: 连续续约失败次数}
        self._handoff_apps: set[str] = set()     # 环迁移后本实例不再续约、随 lease 过期让渡的 bot
        self._acquire_cooldown: dict[str, float] = {}  # {app_id: time.monotonic 冷却截至}

    @property
    def api(self) -> FeishuAsync:
        return self._api

    def set_message_handler(self, handler: Callable[[BotContext, dict], Awaitable[None]]):
        self._on_message = handler

    async def start(self):
        """初始化共享 aiohttp 会话"""
        import asyncio
        # 记下主事件循环：web→飞书推送等后台线程需要把 aiohttp 调用投递回主 loop，
        # 避免跨事件循环复用共享 session（Timeout context manager should be used inside a task）
        self.loop = asyncio.get_running_loop()
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
            connector=aiohttp.TCPConnector(limit=300, limit_per_host=30, keepalive_timeout=30),
        )
        self._api = FeishuAsync(session=session)

    async def stop(self):
        """优雅停止所有Bot + 释放所有 lease"""
        for ctx in self._bots.values():
            ctx._running = False
            ctx.stop_reason = "shutdown"
            if ctx._task and not ctx._task.done():
                ctx._task.cancel()

        for ctx in self._bots.values():
            if ctx._task and not ctx._task.done():
                try:
                    await asyncio.wait_for(ctx._task, timeout=30)  # 问题 I：增大到 30 秒等消息处理完
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            pending = list(ctx._handle_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        if self._api:
            await self._api.close()

        # 释放所有 lease（遗漏修复：stop 时释放 lease 让其他实例立即接管）
        if self._lease_store and self._held_leases:
            self._lease_store.release_all(self._held_leases)
            self._held_leases.clear()

        self._grace_deadlines.clear()
        self._eligible_bots.clear()
        self._bots.clear()
        log.info("BotManager: all bots stopped")

    async def add_bot(self, app_id: str, app_secret: str,
                      bot_type: str = "chat_bot",
                      name: str = "",
                      max_concurrent: int = 0) -> BotContext:
        """运行时动态添加Bot。

        lease 模式下只登记到 _eligible_bots（IDLE），由 sweep 决定是否连接（待优化 3）。
        旧模式直接连接 WebSocket。
        """
        # 待优化 3：lease 模式下只登记不连接
        if self._lease_store:
            self._eligible_bots[app_id] = {
                "bot_type": bot_type,
                "app_secret": app_secret,
            }
            log.info(f"BotManager: registered bot {app_id} as IDLE (lease mode)")
            return None  # type: ignore

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

    async def remove_bot(self, app_id: str, reason: str = ""):
        """运行时动态移除Bot。lease 模式下同时释放 lease（待优化 3）。

        reason：重连账本归因（lease=宽限期/优雅让渡；shutdown=停止；其余 ws_transport）。
        """
        self._handoff_apps.discard(app_id)
        ctx = self._bots.pop(app_id, None)
        # lease 模式下释放 lease
        if self._lease_store:
            token = self._held_leases.pop(app_id, "")
            if token:
                self._lease_store.release(app_id, token)
            self._eligible_bots.pop(app_id, None)
        if not ctx:
            return
        ctx._running = False
        ctx.stop_reason = reason or "shutdown"
        if ctx._task and not ctx._task.done():
            ctx._task.cancel()
            try:
                await ctx._task
            except asyncio.CancelledError:
                pass
        log.info(f"BotManager: removed bot {ctx.name}")

    async def load_from_db(self):
        """从 DB 加载所有 Bot 配置。

        lease 模式（BOT_LEASE_DISABLED=false）：
          - 只登记 bot 配置到 _eligible_bots（state=IDLE），不连接 WebSocket
          - 连接由 lease_sweep_loop 决定——抢到 lease 的才连

        旧模式（BOT_LEASE_DISABLED=true 或 lease_store=None）：
          - 无条件 add_bot 连接 WebSocket（和原有行为一致）

        - chat_bot：以用户级 chat_agent_session（repo_key='' 的配置行）为准，
          每个用户配置的 im_app_id 都启动一个 bot（按 app_id 去重）；
          global_im_bot_config 的 chat_bot 登记作为兜底来源。
        - vod_bot：从 devops_agent_session（repo 维度）取 secret，
          以 global_im_bot_config 登记为准。
        - load_eligible_bots 时确保所有 bot 登记到 global_im_bot_config（遗漏修复 4）
        """
        from src.session_manager import db, db_lock

        bots: dict[str, str] = {}  # app_id -> bot_type（先去重）
        secrets: dict[str, str] = {}  # app_id -> secret

        # 1) chat_bot：用户级配置优先
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "SELECT im_app_id, im_app_secret FROM chat_agent_session "
                    "WHERE im_app_id!='' AND im_app_secret!='' AND user_id>0 AND (repo_key='' OR repo_key IS NULL)"
                )
                for cid, csec in c.fetchall():
                    if cid and cid not in bots:
                        bots[cid] = "chat_bot"
                        secrets[cid] = csec or ""
            except Exception:
                pass

        # 2) global_im_bot_config 登记表（chat 兜底 + vod 全部）
        with db_lock:
            c = db.cursor()
            c.execute("SELECT bot_app_id, bot_type FROM global_im_bot_config WHERE is_active=1")
            rows = c.fetchall()

        for bot_app_id, bot_type in rows:
            if not bot_app_id:
                continue
            if bot_type == "chat_bot":
                if bot_app_id in bots:
                    continue  # 用户级已加载
                # 兜底：从用户级配置补 secret
                with db_lock:
                    c2 = db.cursor()
                    c2.execute("SELECT im_app_secret FROM chat_agent_session WHERE im_app_id=? AND im_app_secret!='' AND (repo_key='' OR repo_key IS NULL) LIMIT 1", (bot_app_id,))
                    srow = c2.fetchone()
                if srow and srow[0]:
                    bots[bot_app_id] = "chat_bot"
                    secrets[bot_app_id] = srow[0]
            else:
                # vod_bot：从 devops_agent_session（repo 维度）取 secret
                with db_lock:
                    c2 = db.cursor()
                    c2.execute("SELECT im_app_secret FROM devops_agent_session WHERE im_app_id=? AND im_app_secret!='' LIMIT 1", (bot_app_id,))
                    srow = c2.fetchone()
                if srow and srow[0]:
                    bots[bot_app_id] = bot_type
                    secrets[bot_app_id] = srow[0]

        # 遗漏修复 4：确保所有 bot 登记到 global_im_bot_config
        for bot_app_id, bot_type in bots.items():
            with db_lock:
                c = db.cursor()
                c.execute("SELECT bot_app_id FROM global_im_bot_config WHERE bot_app_id=?", (bot_app_id,))
                if not c.fetchone():
                    c.execute(
                        "INSERT OR IGNORE INTO global_im_bot_config (bot_app_id, bot_type, is_active) VALUES (?, ?, 1)",
                        (bot_app_id, bot_type),
                    )
                    db.commit()

        # lease 模式：只登记不连接
        if self._lease_store:
            self._eligible_bots.clear()  # 清旧配置（已删的 bot 不再保留）
            for bot_app_id, bot_type in bots.items():
                self._eligible_bots[bot_app_id] = {
                    "bot_type": bot_type,
                    "app_secret": secrets.get(bot_app_id, ""),
                }
            log.info(f"BotManager: loaded {len(self._eligible_bots)} eligible bot(s) (lease mode, IDLE)")
            return

        # 旧模式：无条件连接
        added = 0
        for bot_app_id, bot_type in bots.items():
            if bot_app_id in self._bots:
                continue
            if not secrets.get(bot_app_id):
                continue
            await self.add_bot(bot_app_id, secrets[bot_app_id], bot_type)
            added += 1

        log.info(f"BotManager: loaded {added} bot(s) from DB, total={len(self._bots)}")

    async def lease_sweep_loop(self):
        """lease 扫描循环——每 SWEEP_INTERVAL 秒（cluster-im-lease-stability-design.md §2）：

        0. 实例心跳上报 + 一致性哈希环（均匀 + 粘性归属）
        1. 游标分页查可抢 bot → 仅归属实例 try_acquire（含 owner 存活保护）→ 连接
        2. 批量续约（失败连续达阈值才进宽限期；宽限期到才断开）
        3. 环迁移 → 优雅让渡（不再续约，随 lease 过期释放）
        """
        if not self._lease_store:
            return
        lease_store = self._lease_store  # 本地引用避免 Pyright None 检查
        import time as _time
        import socket as _socket
        from src.config import (
            BOT_LEASE_SWEEP_INTERVAL, BOT_LEASE_RENEW_INTERVAL, BOT_LEASE_GRACE,
            BOT_LEASE_RENEW_FAIL_THRESHOLD, IM_LEASE_STICKY, IM_INSTANCE_TIMEOUT,
            IM_STEAL_AFTER, IM_ACQUIRE_COOLDOWN, IM_HASH_VNODES,
        )

        sweep_count = 0
        while True:
            try:
                # 问题 M：lease 字段不存在时退回旧模式（无条件连接所有 bot）
                if lease_store._disabled:
                    log.warning("[lease] lease disabled (migration may have failed), falling back to legacy mode")
                    self._lease_store = None
                    await self.load_from_db()
                    return

                sweep_count += 1
                # 0. 实例心跳 + 环（owner 归属真值）
                try:
                    lease_store.heartbeat(_socket.gethostname())
                except Exception as e:
                    log.warning(f"[lease] heartbeat failed: {e}")
                actives = lease_store.list_active_instances(IM_INSTANCE_TIMEOUT) if IM_LEASE_STICKY else []
                ring = _im_ring(actives, IM_HASH_VNODES) if actives else []

                # 1. 游标分页查询可抢的 bot → 归属过滤 → try_acquire
                cursor = ""
                while True:
                    candidates = lease_store.list_expired_or_unheld(cursor=cursor, limit=500)
                    if not candidates:
                        break
                    for bot_info in candidates:
                        app_id = bot_info["bot_app_id"]
                        if app_id not in self._eligible_bots:
                            continue
                        if app_id in self._bots:
                            continue  # 已经在连
                        if IM_LEASE_STICKY and _im_owner(ring, app_id) != self._instance_id:
                            continue  # 非归属实例不抢（均匀 + 粘性）
                        if self._acquire_cooldown.get(app_id, 0.0) > _time.monotonic():
                            continue  # 上一轮连接失败冷却期
                        token = lease_store.try_acquire(app_id,
                                                        steal_after=IM_STEAL_AFTER,
                                                        instance_timeout=IM_INSTANCE_TIMEOUT)
                        if token:
                            self._held_leases[app_id] = token
                            eligible = self._eligible_bots[app_id]
                            try:
                                ctx = BotContext(
                                    app_id=app_id,
                                    app_secret=eligible["app_secret"],
                                    bot_type=eligible["bot_type"],
                                    max_concurrent=self._max_concurrent,
                                )
                                ctx._lease_token = token  # 问题 A：供重连后检查
                                self._bots[app_id] = ctx
                                if self._on_message and self._api:
                                    ctx._task = asyncio.create_task(
                                        run_bot(ctx, self._api, self._on_message),
                                        name=f"bot-{ctx.name}",
                                    )
                                    log.info(f"[lease] bot {ctx.name} connected after acquiring lease")
                            except Exception as e:
                                # 连接失败：释放 lease + 冷却，避免循环抢占
                                log.warning(f"[lease] bot {app_id} connect failed: {e}, releasing lease")
                                lease_store.release(app_id, token)
                                self._held_leases.pop(app_id, None)
                                self._bots.pop(app_id, None)
                                self._acquire_cooldown[app_id] = _time.monotonic() + IM_ACQUIRE_COOLDOWN
                                self._renew_fails.pop(app_id, None)
                                self._grace_deadlines.pop(app_id, None)
                    cursor = candidates[-1]["bot_app_id"]
                    if len(candidates) < 500:
                        break

                # 1.5 环迁移 → 优雅让渡：不属于本实例的 bot 标记为不再续约（仍在线，随 lease 过期释放）
                if IM_LEASE_STICKY and ring:
                    for app_id in list(self._held_leases.keys()):
                        if _im_owner(ring, app_id) != self._instance_id:
                            self._handoff_apps.add(app_id)
                        else:
                            self._handoff_apps.discard(app_id)

                # 2. 批量续约（每 RENEW_INTERVAL 秒续约一次；跳过让渡中的 bot）
                _renew_div = max(1, BOT_LEASE_RENEW_INTERVAL // BOT_LEASE_SWEEP_INTERVAL)
                if self._held_leases and sweep_count % _renew_div == 0:
                    to_renew = {a: t for a, t in self._held_leases.items() if a not in self._handoff_apps}
                    renew_results = lease_store.batch_renew(to_renew) if to_renew else {}
                    now = _time.time()
                    for app_id, success in renew_results.items():
                        if success:
                            self._renew_fails.pop(app_id, None)
                            self._grace_deadlines.pop(app_id, None)
                            continue
                        # 失败：单条重试一次（瞬时 DB 竞争）
                        token = self._held_leases.get(app_id, "")
                        retry_ok = False
                        if token:
                            try:
                                retry_ok = bool(lease_store.renew(app_id, token))
                            except Exception:
                                retry_ok = False
                        if retry_ok:
                            log.info(f"[lease] bot {app_id} renew retry succeeded (was transient)")
                            self._renew_fails.pop(app_id, None)
                            self._grace_deadlines.pop(app_id, None)
                            continue
                        # 连续失败达到阈值才进宽限期（抗瞬时抖动）
                        _fails = self._renew_fails.get(app_id, 0) + 1
                        self._renew_fails[app_id] = _fails
                        if _fails >= BOT_LEASE_RENEW_FAIL_THRESHOLD and app_id not in self._grace_deadlines:
                            self._grace_deadlines[app_id] = now + BOT_LEASE_GRACE
                            log.info(f"[lease] bot {app_id} renew failed x{_fails}, "
                                     f"grace period {BOT_LEASE_GRACE}s started")
                        else:
                            log.info(f"[lease] bot {app_id} renew failed x{_fails}/{BOT_LEASE_RENEW_FAIL_THRESHOLD}")

                # 2a. handoff：lease 已过期且 owner 已不是本实例 → 本地释放（优雅让渡，不强制断开前有横差）
                if self._handoff_apps:
                    now_ts = _time.time()
                    for app_id in list(self._handoff_apps):
                        try:
                            info = lease_store.lease_owner_info(app_id)
                            exp = (info.get("expires_at") or "").strip()
                            if info.get("owner") != self._instance_id and exp and _lease_expiry_ts(exp) <= now_ts:
                                self._handoff_apps.discard(app_id)
                                token = self._held_leases.pop(app_id, "")
                                if token:
                                    lease_store.release(app_id, token)
                                self._grace_deadlines.pop(app_id, None)
                                self._renew_fails.pop(app_id, None)
                                if app_id in self._bots:
                                    await self.remove_bot(app_id, reason="handoff")
                        except Exception as e:
                            log.warning(f"[lease] handoff check {app_id} error: {e}")

                # 2b. 每次 sweep 检查宽限期到期 → 断开（reason=lease）
                if self._grace_deadlines:
                    now = _time.time()
                    expired_grace = [aid for aid, dl in self._grace_deadlines.items() if now >= dl]
                    for app_id in expired_grace:
                        log.info(f"[lease] bot {app_id} grace period expired, disconnecting")
                        self._handoff_apps.discard(app_id)
                        token = self._held_leases.pop(app_id, "")
                        if token:
                            lease_store.release(app_id, token)
                        self._grace_deadlines.pop(app_id, None)
                        if app_id in self._bots:
                            await self.remove_bot(app_id, reason="lease")

                # 3. 检查 is_active=0 的 bot → 断开 + 释放 lease（问题 K）
                if self._held_leases:
                    inactive = lease_store.check_inactive_bots(self._held_leases)
                    for app_id in inactive:
                        log.info(f"[lease] bot {app_id} is_active=0, disconnecting")
                        self._handoff_apps.discard(app_id)
                        token = self._held_leases.pop(app_id, "")
                        if token:
                            lease_store.release(app_id, token)
                        if app_id in self._bots:
                            await self.remove_bot(app_id, reason="lease")

                # 4. 定期重新加载 bot 配置（问题 B：每 5 分钟重新加载）
                if sweep_count % (300 // BOT_LEASE_SWEEP_INTERVAL) == 0:  # 约 5 分钟
                    try:
                        await self.load_from_db()
                    except Exception as e:
                        log.warning(f"[lease] reload eligible bots failed: {e}")

            except Exception as e:
                # 问题 O：异常不杀死循环
                log.error(f"[lease] sweep error: {e}")
            await asyncio.sleep(BOT_LEASE_SWEEP_INTERVAL)

    def get_bot(self, app_id: str) -> BotContext | None:
        return self._bots.get(app_id)

    def get_all_bots(self) -> dict[str, BotContext]:
        return dict(self._bots)

    def health_check(self) -> dict:
        """统一健康检查 + lease 状态（问题 N）。"""
        total = len(self._bots)
        connected = sum(1 for b in self._bots.values() if b.is_connected)
        total_received = sum(b.metrics.messages_received for b in self._bots.values())
        total_sent = sum(b.metrics.messages_sent for b in self._bots.values())
        total_failed = sum(b.metrics.messages_failed for b in self._bots.values())
        # lease 状态（问题 N）
        lease_status = {}
        if self._lease_store and self._bots:
            lease_status = self._lease_store.get_lease_status(list(self._bots.keys()))
        # 重连账本（cluster-im-lease-stability-design.md §2.5）：各 bot 断开归因聚合
        flap = {"lease": 0, "ws_transport": 0, "shutdown": 0}
        total_recon = 0
        for b in self._bots.values():
            total_recon += b.metrics.reconnect_count
            for reason, cnt in (b.metrics.disconnect_reasons or {}).items():
                flap[reason] = flap.get(reason, 0) + cnt
        return {
            "total_bots": total,
            "connected": connected,
            "disconnected": total - connected,
            "total_messages_received": total_received,
            "total_messages_sent": total_sent,
            "total_messages_failed": total_failed,
            "total_reconnects": total_recon,
            "flap_by_cause": flap,
            "bots": {app_id: ctx.to_dict() for app_id, ctx in self._bots.items()},
            "lease": lease_status,
        }