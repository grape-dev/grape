"""Slack Channel 实现（Issue #1309 批 1：Socket Mode 长连接 + 3s ack + 重连）。

生命周期纪律（与 feishu / dingtalk / wecom 适配器同构）：

  - **Socket Mode 双令牌**：`apps.connections.open`（App Token `xapp-`）换 WSS URL，
    连上后先收 `hello`，之后每个事件是一个 envelope（`envelope_id` + `payload`）；
  - **ACK 先行 3s**：读循环里 **先回 `{"envelope_id": ...}`**，再解析/分发——
    超时会被 Slack 重投（最多 3 次，同 `event_id`），ack 先行是幂等的第一道闸；
  - **去重**：内存 FIFO + 持久化钩子，键用 `event_id`（fallback `envelope_id`）；
    `ts` 因消息编辑会产生同 ts 新事件，不作去重键；
  - **heartbeat**：websockets 协议层 ping（默认 20s）保活；服务端 `disconnect`
    envelope 是显式断链信号 → `ChannelError` 交 Supervisor 退避重连；
  - **异常不穿透接收循环**（钉钉 #1008 教训）：单帧解析/分发异常兜住并计数，
    只有链路级失败才抛 ChannelError；
  - **凭证永久失败**（invalid_auth / not_authed / account_inactive / missing_scope）
    → `PermanentChannelError`（Supervisor 置 revoked，不再重试）；
  - lease 复查失配 → 优雅让渡。

依赖注入：connection_opener / ws_connect / lease_checker / seen 钩子全部可替身，
测试用假 WS，不触网。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ...channel.capability import Capability
from ...channel.channel import (
    CancelContext,
    Channel,
    ChannelConfig,
    ChannelError,
    PermanentChannelError,
)
from ...channel.message import OutboundMessage, SendResult
from ...channel.types import Type
from .inbound import block_actions_to_inbound, envelope_payload, event_to_inbound
from .outbound import SlackApiError, SlackClient, SlackSender
from .resolvers import SlackRouteRegistry

log = logging.getLogger(__name__)

ACK_DEADLINE_SECS = 3.0        # Slack 官方：envelope 必须 3s 内 ack
SEEN_MAX = 2000                # A3：内存去重集上限（与飞书/钉钉同款）
OPEN_TIMEOUT = 10.0
PING_INTERVAL = 20.0

# 凭证类永久失败（Supervisor 置 revoked 不再重试）
_PERMANENT_ERRORS = frozenset({
    "invalid_auth",
    "not_authed",
    "account_inactive",
    "token_revoked",
    "token_expired",
    "invalid_token",
    "missing_scope",
    "not_allowed_token_type",
    "ekm_access_denied",
})

ConnectionOpener = Callable[[str], Awaitable[str]]
LeaseChecker = Callable[[], bool]


def _trim_seen(seen: dict, max_size: int) -> None:
    """FIFO 裁剪（按插入序淘汰最旧键，保留最近 max_size//2 条）。"""
    keep = max_size // 2
    overflow = len(seen) - keep
    if overflow > 0:
        for key in list(seen)[:overflow]:
            del seen[key]


def is_permanent_credentials_error(message: str) -> bool:
    """凭证类永久失败判定（A5 短路）：错误串里出现已知 error code 即永久。"""
    text = str(message or "").lower()
    return any(code in text for code in _PERMANENT_ERRORS)


async def default_connection_opener(app_token: str) -> str:
    """POST /api/apps.connections.open（App Token）→ WSS URL。"""
    client = SlackClient(app_token, timeout=OPEN_TIMEOUT)
    body = await client.call("apps.connections.open")
    url = str(body.get("url") or "")
    if not url:
        raise SlackApiError("apps.connections.open returned no url")
    return url


class SlackChannel(Channel):
    """Slack Socket Mode 长连接渠道。"""

    def __init__(
        self,
        cfg: ChannelConfig,
        sender: SlackSender,
        *,
        connection_opener: ConnectionOpener | None = None,
        ws_connect: Callable | None = None,
        lease_checker: LeaseChecker | None = None,
        route_registry: SlackRouteRegistry | None = None,
        seen_loader: Callable | None = None,
        seen_persist: Callable | None = None,
        seen_cleanup: Callable | None = None,
        max_concurrent: int = 20,
        read_timeout: float = 0.0,
    ) -> None:
        self._cfg = cfg
        self._sender = sender
        self._opener = connection_opener or default_connection_opener
        self._ws_connect = ws_connect
        self._lease_checker = lease_checker
        self._routes = route_registry if route_registry is not None else SlackRouteRegistry()
        self._seen_loader = seen_loader or (lambda app: set())
        self._seen_persist = seen_persist or (lambda app, mid: None)
        self._seen_cleanup = seen_cleanup or (lambda app: None)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._handler_tasks: set = set()
        self._read_timeout = float(read_timeout or 0.0)
        self._ws: Any = None            # 当前连接（disconnect 主动关闭，防让渡后留僵尸连接）
        self.acked_frames: list[str] = []   # 测试观察哨：ack 先行证据
        self.handled_envelopes: list[str] = []
        self.messages_handled: list = []
        self.parse_fail = 0
        self.hello = False
        self.connected = False
        self.bot_token = str(cfg.raw.get("bot_token") or cfg.raw.get("app_id") or "")
        self.app_token = str(cfg.raw.get("app_token") or cfg.raw.get("app_secret") or "")

    def type(self) -> Type:
        return Type.SLACK

    def capabilities(self) -> Capability:
        return self._sender.capabilities()

    def _install_tag(self) -> str:
        """日志用安装标识：只用 installation_id，绝不打印 token 片段（安全硬约束）。"""
        return str(getattr(self._cfg, "installation_id", "") or "")[-12:] or "install"

    @property
    def cfg(self) -> ChannelConfig:
        return self._cfg

    # ---------------------------------------------------------- 连接

    async def connect(self, ctx: CancelContext) -> None:
        """阻塞运行接收循环直到 ctx 取消 / 链路断开（契约同 Channel.connect）。"""
        if not self.bot_token or not self.app_token:
            raise PermanentChannelError(
                "slack config missing bot_token/app_token "
                "（Socket Mode 需 Bot Token xoxb- + App Token xapp-）")

        if not ctx.cancelled and self._lease_checker is not None:
            try:
                if not self._lease_checker():
                    log.info("[slack:%s] lease lost, graceful handover", self._install_tag())
                    return
            except Exception as e:
                log.warning("[slack] lease check error: %s", e)

        try:
            ws_url = await self._opener(self.app_token)
        except PermanentChannelError:
            raise
        except SlackApiError as e:
            if is_permanent_credentials_error(str(e)):
                raise PermanentChannelError(f"permanent slack failure: {e}") from e
            raise ChannelError(f"slack connections.open failed: {e}") from e
        except Exception as e:
            raise ChannelError(f"slack connections.open error: {e!r}") from e
        if not ws_url:
            raise ChannelError("slack connections.open returned empty url")

        ws_connect = self._ws_connect
        if ws_connect is None:
            import websockets
            ws_connect = lambda url: websockets.connect(   # noqa: E731
                url, ping_interval=PING_INTERVAL, ping_timeout=PING_INTERVAL,
                open_timeout=OPEN_TIMEOUT, close_timeout=5, max_size=2 ** 23)

        seen: dict = {mid: None for mid in self._seen_loader(self.bot_token)}
        try:
            async with ws_connect(ws_url) as ws:
                self._ws = ws
                self.connected = True
                while not ctx.cancelled:
                    try:
                        if self._read_timeout > 0:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self._read_timeout)
                        else:
                            raw = await ws.recv()
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError as e:
                        raise ChannelError(
                            f"slack read timeout ({self._read_timeout}s)") from e
                    except Exception as e:
                        raise ChannelError(f"slack read failed: {e!r}") from e
                    if raw in (None, ""):
                        break
                    try:
                        await self._on_frame(ctx, ws, raw, seen)
                    except (ChannelError, PermanentChannelError):
                        raise
                    except Exception as e:
                        # 单帧异常绝不穿透（否则接收循环静默死亡）
                        self.parse_fail += 1
                        log.warning("[slack] frame handling failed: %r", e)
        except (ChannelError, PermanentChannelError):
            raise
        except Exception as e:
            raise ChannelError(f"slack ws failed: {e!r}") from e
        finally:
            self.connected = False
            self._ws = None

        if ctx.cancelled:
            return
        self._seen_cleanup(self.bot_token)
        raise ChannelError("slack ws closed")

    async def _on_frame(self, ctx: CancelContext, ws: Any, raw: Any, seen: dict) -> None:
        payload = _parse_frame(raw)
        if not payload:
            self.parse_fail += 1
            log.info("[slack] unparsable frame dropped")
            return

        frame_type = str(payload.get("type") or "")
        if frame_type == "hello":
            self.hello = True
            log.info("[slack] hello (connections=%s)", payload.get("num_connections"))
            return
        if frame_type == "disconnect":
            # 服务端显式断链（刷新/新连接顶替）→ 交 Supervisor 退避重连
            raise ChannelError(f"slack disconnect: {payload.get('reason') or 'server'}")

        envelope_id = str(payload.get("envelope_id") or "")
        if not envelope_id:
            self.parse_fail += 1
            return
        # ⚠️ ACK 先行 3s：解析/分发异常也已 ack（防重投死循环）
        await self._ack(ws, envelope_id)
        self.acked_frames.append(envelope_id)

        payload_type, inner = envelope_payload(payload)
        if payload_type == "events_api":
            msg = event_to_inbound(inner, envelope_id=envelope_id)
        elif payload_type == "interactive":
            msg = block_actions_to_inbound(inner, envelope_id=envelope_id)
        else:
            # slash_commands 等（v1 不做）——已 ack，直接忽略
            return
        if msg is None:
            return

        key = msg.event_id or envelope_id
        if key in seen:
            log.info("[slack] duplicate event dropped: %s", key[:24])
            return
        seen[key] = None
        self._seen_persist(self.bot_token, key)
        if len(seen) > SEEN_MAX:
            _trim_seen(seen, SEEN_MAX)

        self._remember_route(msg)
        self.handled_envelopes.append(envelope_id)
        task = asyncio.get_running_loop().create_task(self._safe_handle(ctx, msg))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _ack(self, ws: Any, envelope_id: str) -> None:
        try:
            await ws.send(json.dumps({"envelope_id": envelope_id}, ensure_ascii=False))
        except Exception as e:
            log.warning("[slack] ack send failed: %s", e)

    def _remember_route(self, msg) -> None:
        """登记 ts → channel（出站 chat.update / reactions.add 只有 ts）。"""
        channel = msg.source.chat_id
        try:
            if msg.message_id:
                self._routes.remember(msg.message_id, channel)
            if msg.reply_to and msg.reply_to.message_id:
                self._routes.remember(msg.reply_to.message_id, channel)
        except Exception as e:
            log.warning("[slack] route register failed: %r", e)

    async def _safe_handle(self, ctx: CancelContext, msg) -> None:
        """A4：semaphore 限流 + 异常隔离；handler 异常仅代表基础设施失败。"""
        handler = self._cfg.inbound_handler
        if handler is None:
            return
        self.messages_handled.append(msg)
        async with self._semaphore:
            if ctx.cancelled:
                return
            try:
                exc = await handler(ctx, msg)
            except Exception as e:
                log.warning("[slack] inbound handler crashed: %r", e)
                return
            if exc is not None:
                log.warning("[slack] inbound handler infra error: %r", exc)

    async def disconnect(self) -> None:
        self.connected = False
        for task in list(self._handler_tasks):
            task.cancel()
        self._handler_tasks.clear()
        ws = self._ws
        self._ws = None
        if ws is not None:
            # 优雅让渡时 Supervisor 只 cancel ctx（不 cancel 运行任务）→ 必须主动关连接，
            # 否则读循环会阻塞在 recv 上留下僵尸 socket（Slack 侧连接数被占满）。
            try:
                await ws.close()
            except Exception as e:
                log.warning("[slack] ws close failed: %r", e)

    async def send(self, out: OutboundMessage) -> SendResult:
        return await self._sender.send(out)


def _parse_frame(raw: Any) -> dict:
    """WS 帧 → dict（Slack 是 JSON 文本帧；bytes 也接受）。"""
    if isinstance(raw, dict):
        return raw
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        value = json.loads(text)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


__all__ = [
    "ACK_DEADLINE_SECS",
    "PING_INTERVAL",
    "SlackChannel",
    "default_connection_opener",
    "is_permanent_credentials_error",
]
