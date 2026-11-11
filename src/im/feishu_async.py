"""纯异步飞书多Bot连接管理器 — 支持100+Bot单进程隔离运行

架构:
  BotContext  — 单Bot完整隔离上下文（token缓存/并发槽/指标/WS连接）
  FeishuAsync — aiohttp 飞书API客户端（token/消息发送/WS端点获取）

注：旧 `BotManager`（编排启动/停止/健康检查/热加载 + 自建 WS 帧泵）已于 2026-11-10
（A2 M1/M2）下线；收发出站改由新引擎 FeishuChannel + Replier 承担，本模块只保留
BotContext / FeishuAsync 两件基础设施。

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
from src.im.seen_store import (  # Issue: 旧 IM 下线 M1 —— 原模块内实现迁到中立模块
    _SEEN_MSG_MAX_LOAD, _SEEN_MSG_TTL, _cleanup_seen_msg_ids, _load_seen_msg_ids, _persist_seen_msg_id,
)

FEISHU_BASE = "https://open.feishu.cn/open-apis"
WS_ENDPOINT_URL = "https://open.feishu.cn/callback/ws/endpoint"


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


def _resolve_uid_by_platform_login(login: str, provider: str) -> int:
    """FR-21.2: 从 external_identities 按 platform login 查 grape_user_id。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT ei.grape_user_id FROM external_identities ei "
            "WHERE ei.provider=? AND ei.external_login=? AND ei.status=1 LIMIT 1",
            (provider, login)).fetchone()
        return row[0] if row else 0


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
                                title: str = "DevOps Bot", actions: list | None = None,
                                template: str = "blue", fields: list | None = None,
                                note: str | None = None, card_v2: dict | None = None) -> str:
        """主动发送卡片给用户

        template: 头部模板色（blue/wathet/turquoise/green/yellow/orange/red/grey...）。
        fields:   结构化键值对字段元素（{"is_short":...,"text":{...}} 列表），正文后追加 div。
        note:     卡片底部灰色注释（如发送时间戳）。
        card_v2:  完整卡片 2.0 JSON（schema:"2.0"）——传入时忽略上述参数直接发送 2.0 卡
                  （autopilot 飞书订阅通知等新卡片用 2.0 排版）。
        """
        token = await self._ensure_token(ctx)
        if not token:
            return ""
        session = await self._get_session()
        url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=open_id"
        if card_v2 is not None:
            card = card_v2
        else:
            elements = to_lark_elements(text, 2000)
            if fields:
                elements.append({"tag": "div", "fields": fields})
            if actions:
                elements.append({"tag": "action", "actions": actions})
            if note:
                elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
            card = {
                "elements": elements,
                "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
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

    async def update_card_by_token(self, ctx: BotContext, action_token: str, card: dict) -> bool:
        """延迟更新卡片（飞书 interactive/v1/card/update）。

        用 card.action.trigger 事件回传的 token 原地更新同一张卡，不产生新消息。
        限制：token 30 分钟有效、同一 token 只能用 2 次；必须在 3s 内先 ACK 回调。
        card 为完整卡片 JSON（1.0 或 2.0 均可，飞书按内容自动识别）。
        """
        if not action_token:
            log.warning(f"[{ctx.name}] update_card_by_token: empty token")
            return False
        token = await self._ensure_token(ctx)
        if not token:
            return False
        session = await self._get_session()
        url = f"{FEISHU_BASE}/interactive/v1/card/update"
        payload = {"token": action_token, "card": card}
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if data.get("code", -1) == 0:
                    ctx.metrics.messages_sent += 1
                    log.info(f"[{ctx.name}] update_card_by_token ok")
                    return True
                log.warning(f"[{ctx.name}] update_card_by_token failed: code={data.get('code')} msg={data.get('msg','')[:60]}")
        except Exception as e:
            ctx.metrics.messages_failed += 1
            log.warning(f"[{ctx.name}] update_card_by_token error: {e}")
        return False

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

        # R20.7.3: 统一走 TokenResolver（GitHub 侧也走 Repo.bot_token → TokenResolver）
        from src.token_service import get_repo_bot_token
        api_token = get_repo_bot_token(repo_key) or ""
        if not api_token:
            log.warning(f"[{ctx.name}] sync mapping: no bot token for {repo_key}, skipping")
            # R20.9.1: token 过期时跳过 sync + 飞书群发通知卡片
            try:
                _error_code = ""
                from src.token_service import detect_and_mark_expired
                _error_code = detect_and_mark_expired(repo_key) or ""
                if _error_code:
                    _msg = f"⚠️ 仓库 {repo_key} 的 Token 已过期，协作者映射同步已跳过，请重新绑定 Token"
                    await self.send_card_to_chat(ctx, chat_id, _msg, title="Token 过期通知")
            except Exception as _te:
                log.warning(f"[{ctx.name}] sync mapping: token expired notification failed: {_te}")
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
                    if provider == "github":
                        # FR-21.2: GitHub 协作者 login 是 GitHub 真名，不能写 gitcode_user 列
                        # 先从 external_identities 查 grape_user_id，再只写飞书三列
                        _uid = _resolve_uid_by_platform_login(login, "github")
                        if _uid:
                            from src.session_manager import bind_feishu_identity
                            bind_feishu_identity(_uid, m["open_id"], feishu_name=nick, app_id=ctx.app_id)
                            matched += 1
                    else:
                        # GitCode 协作者 username 就是平台 login，写 gitcode_user 列正确
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
