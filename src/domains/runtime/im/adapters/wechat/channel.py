"""微信 Channel 实现（Issue #1728 M1：getupdates 长轮询 + 游标持久化）。

生命周期纪律（与 feishu / dingtalk / wecom / slack / telegram 同构）：

  - **长轮询**：`POST /ilink/bot/getupdates`（默认 35s），游标 `get_updates_buf`
    变化即持久化到 `grape_t_channel_installation.config["cursor"]`（回调注入），重启续接不重投；
  - **`ret = -14`（风控）**：账号会话暂停 1h —— 记 `session_paused_risk`，
    暂停期间不轮询不重试（ctx 可取消），退出前 `notifystop`；
  - **自消息丢弃**：`message_type=2`（Bot 自己）由 inbound 直接过滤，避免自回环；
  - **异常不穿透接收循环**：单条消息解析/分发异常兜住并计数，只有链路级失败
    （网络/5xx）才抛 `ChannelError`；`ret=-14` 后暂停重连；
  - `notifystart` / `notifystop` 包在连接生命周期里，失败仅 WARNING；
  - lease 复查失配 → 优雅让渡。

依赖注入：`update_fetcher` / `cursor_loader` / `cursor_persist` / `lease_checker` /
`sleep` 全部可替身，测试不触网。
"""
from __future__ import annotations

import asyncio
import time
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
from .inbound import message_to_inbound
from .protocol import (
    LONGPOLL_TIMEOUT,
    SESSION_PAUSE_SECONDS,
    WechatApiError,
    as_list,
    as_str,
    context_token_of,
)
from .resolvers import WechatRouteRegistry, WechatUserResolver

log = logging.getLogger(__name__)

SEEN_MAX = 2000


def _trim_seen(seen: dict, max_size: int) -> None:
    keep = max_size // 2
    overflow = len(seen) - keep
    if overflow > 0:
        for key in list(seen)[:overflow]:
            del seen[key]


class WechatChannel(Channel):
    """微信 getupdates 长轮询渠道。"""

    def __init__(
        self,
        cfg: ChannelConfig,
        sender: Any,
        *,
        client: Any = None,
        update_fetcher: Callable[[str, float], Awaitable[dict]] | None = None,
        route_registry: WechatRouteRegistry | None = None,
        users: WechatUserResolver | None = None,
        lease_checker: Callable[[], bool] | None = None,
        cursor_loader: Callable[[], str] | None = None,
        cursor_persist: Callable[[str], None] | None = None,
        metrics: Any = None,
        pause_seconds: float = SESSION_PAUSE_SECONDS,
        poll_timeout: float = LONGPOLL_TIMEOUT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_concurrent: int = 20,
    ) -> None:
        self._cfg = cfg
        self._sender = sender
        self._client = client
        self._update_fetcher = update_fetcher
        self._routes = route_registry if route_registry is not None else WechatRouteRegistry()
        self._users = users if users is not None else WechatUserResolver()
        self._lease_checker = lease_checker
        self._cursor_loader = cursor_loader or (lambda: "")
        self._cursor_persist = cursor_persist or (lambda value: None)
        self._metrics = metrics
        self._pause_seconds = float(pause_seconds or SESSION_PAUSE_SECONDS)
        self._poll_timeout = float(poll_timeout or LONGPOLL_TIMEOUT)
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._handler_tasks: set = set()
        raw = dict(cfg.raw or {})
        self.bot_token = as_str(raw.get("bot_token") or raw.get("app_secret"))
        self.bot_id = as_str(raw.get("ilink_bot_id") or raw.get("app_id"))
        self._cursor = ""
        self._seen: dict = {}
        self.connected = False
        self.messages_handled: list = []
        self.parse_fail = 0
        self.inbound_empty = 0
        self.session_paused = 0
        self.cursor_updates = 0
        self.paused_for = 0.0

    def type(self) -> Type:
        return Type.WECHAT

    def capabilities(self) -> Capability:
        return self._sender.capabilities()

    @property
    def cfg(self) -> ChannelConfig:
        return self._cfg

    @property
    def cursor(self) -> str:
        return self._cursor

    def _install_tag(self) -> str:
        return str(getattr(self._cfg, "installation_id", "") or "")[-12:] or "install"

    def _record_degraded(self, reason: str) -> None:
        if self._metrics is not None:
            try:
                self._metrics.record_degraded(reason)
            except Exception:
                pass

    # ---------------------------------------------------------- 连接

    async def connect(self, ctx: CancelContext) -> None:
        if not self.bot_token and self._client is None:
            raise PermanentChannelError(
                "wechat config missing bot_token（扫码绑定产出）")

        if not ctx.cancelled and self._lease_checker is not None:
            try:
                if not self._lease_checker():
                    log.info("[wechat:%s] lease lost, graceful handover",
                             self._install_tag())
                    return
            except Exception as e:
                log.warning("[wechat] lease check error: %s", e)

        try:
            self._cursor = as_str(self._cursor_loader())
        except Exception:
            self._cursor = ""

        await self._notify_start()
        self.connected = True
        try:
            while not ctx.cancelled:
                try:
                    response = await self._fetch()
                except WechatApiError as e:
                    if e.session_paused:
                        await self._on_session_paused(ctx, e)
                        return
                    raise ChannelError(f"wechat getupdates failed: {e}") from e
                except ChannelError:
                    raise
                except Exception as e:
                    raise ChannelError(f"wechat getupdates error: {e!r}") from e

                await self._consume(response, ctx)
                self._advance_cursor(as_str(response.get("get_updates_buf")))
        finally:
            self.connected = False
            await self._notify_stop()

    async def _fetch(self) -> dict:
        if self._update_fetcher is not None:
            return await self._update_fetcher(self._cursor, self._poll_timeout)
        if self._client is None:
            raise ChannelError("wechat client not configured")
        return await self._client.get_updates(self._cursor, timeout=self._poll_timeout)

    async def _on_session_paused(self, ctx: CancelContext, error: WechatApiError) -> None:
        self.session_paused += 1
        self.paused_for = self._pause_seconds
        self._record_degraded("session_paused_risk")
        log.warning("[wechat:%s] session paused by server (ret=%s), sleep %.0fs",
                    self._install_tag(), error.ret or error.errcode, self._pause_seconds)
        if ctx.cancelled or self._pause_seconds <= 0:
            return
        try:
            await asyncio.wait_for(ctx.wait(), timeout=self._pause_seconds)
        except asyncio.TimeoutError:
            pass

    async def _consume(self, response: dict, ctx: CancelContext) -> None:
        msgs = as_list(response.get("msgs"))
        for item in msgs:
            if ctx.cancelled:
                break
            try:
                await self._on_message(ctx, item)
            except (ChannelError, PermanentChannelError):
                raise
            except Exception as e:
                self.parse_fail += 1
                log.warning("[wechat] message handling failed: %r", e)

    async def _on_message(self, ctx: CancelContext, item: Any) -> None:
        msg = message_to_inbound(item, bot_id=self.bot_id)
        if msg is None:
            self.inbound_empty += 1
            self._record_degraded("inbound_empty")
            return
        # 排障（2026-11-10「稳定 6 分钟延迟」）：把平台侧消息时间与拉到时刻的差打出来，
        # 一眼区分「微信服务端压消息」还是「引擎/agent 慢」。
        try:
            created_ms = int((item or {}).get("create_time_ms") or 0)
        except Exception:
            created_ms = 0
        if created_ms > 0:
            lag = max(0.0, time.time() - created_ms / 1000.0)
            log.info("[wechat] inbound lag to pull = %.1fs (id=%s)", lag, msg.message_id)

        self._remember_route(msg)
        self._remember_sender(msg)

        if not self._mark_seen(msg.event_id):
            return

        # typing 指示：失败不影响入站（微信 typing 是可选体验）
        try:
            await self._sender.send_typing(msg.source.chat_id, status=1)
        except Exception:
            pass

        task = asyncio.get_running_loop().create_task(self._safe_handle(ctx, msg))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    def _remember_route(self, msg) -> None:
        try:
            self._routes.remember(
                msg.source.chat_id,
                context_token=context_token_of(msg.raw),
                message_id=msg.message_id)
        except Exception as e:
            log.warning("[wechat] route register failed: %r", e)

    def _remember_sender(self, msg) -> None:
        name = as_str((msg.raw or {}).get("sender_name"))
        if name:
            try:
                self._users.remember(msg.source.sender_id, name)
            except Exception:
                pass

    def _advance_cursor(self, next_cursor: str) -> None:
        if not next_cursor or next_cursor == self._cursor:
            return
        self._cursor = next_cursor
        self.cursor_updates += 1
        try:
            self._cursor_persist(next_cursor)
        except Exception as e:
            log.warning("[wechat] cursor persist failed: %r", e)

    def _mark_seen(self, key: str) -> bool:
        if not key:
            return True
        if key in self._seen:
            log.info("[wechat] duplicate message dropped: %s", key[:32])
            return False
        self._seen[key] = None
        if len(self._seen) > SEEN_MAX:
            _trim_seen(self._seen, SEEN_MAX)
        return True

    async def _safe_handle(self, ctx: CancelContext, msg) -> None:
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
                log.warning("[wechat] inbound handler crashed: %r", e)
                return
            if exc is not None:
                log.warning("[wechat] inbound handler infra error: %r", exc)

    async def _notify_start(self) -> None:
        # notify 属于协议客户端能力，sender 在引擎里被 CountingSender 包了一层，
        # 不带 notify_*；优先走 client，兜底再试 sender（缺失即跳过，不算故障）。
        notifier = getattr(self._client, "notify_start", None) or getattr(self._sender, "notify_start", None)
        if notifier is None:
            return
        try:
            await notifier()
        except Exception as e:
            log.warning("[wechat] notifystart failed (ignored): %r", e)

    async def _notify_stop(self) -> None:
        notifier = getattr(self._client, "notify_stop", None) or getattr(self._sender, "notify_stop", None)
        if notifier is None:
            return
        try:
            await notifier()
        except Exception as e:
            log.warning("[wechat] notifystop failed (ignored): %r", e)

    async def disconnect(self) -> None:
        self.connected = False
        for task in list(self._handler_tasks):
            task.cancel()
        self._handler_tasks.clear()
        await self._notify_stop()

    async def send(self, out: OutboundMessage) -> SendResult:
        return await self._sender.send(out)


__all__ = ["SEEN_MAX", "WechatChannel"]