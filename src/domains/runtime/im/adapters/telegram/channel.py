"""Telegram Channel 实现（Issue #1613 批 1：getUpdates 长轮询 + offset 持久化）。

生命周期纪律（与 feishu / dingtalk / wecom / slack 适配器同构）：

  - **长轮询**：`getUpdates(offset, timeout=25, allowed_updates=[...])`，零公网、零域名；
    `offset = last_update_id + 1` 推进并持久化（回调注入），重启后从断点继续，不重投；
  - **同一 Bot Token 只能有一个 getUpdates 消费者**（多消费者会 409 抢 update），
    与「一安装一连接 + 租约」模型天然匹配；
  - **callback_query 先 ack**：收到即 `answerCallbackQuery(id)`，再异步派发业务
    （Telegram 对 ack 有时限，不 ack 会重投且客户端按钮一直转圈）；
  - **edited_message 显式忽略**（推进 offset 但不派发），防同一条被当新消息重复派发；
  - **异常不穿透接收循环**：单条 update 解析/分发异常兜住并计数，只有链路级失败
    （网络/401/404）才抛 ChannelError / PermanentChannelError；
  - lease 复查失配 → 优雅让渡。

依赖注入：update_fetcher / answer_callback / offset_loader / offset_persist /
lease_checker / seen 钩子全部可替身，测试用假服务器，不触网。
"""
from __future__ import annotations

import asyncio
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
from .cards import TelegramCardLedger
from .inbound import callback_query_to_inbound, update_to_inbound
from .outbound import ALLOWED_UPDATES, LONG_POLL_TIMEOUT, TelegramApiError, TelegramClient, TelegramSender
from .resolvers import TelegramRouteRegistry, TelegramUserResolver

log = logging.getLogger(__name__)

SEEN_MAX = 2000
_PERMANENT_CODES = frozenset({401, 404})


def _trim_seen(seen: dict, max_size: int) -> None:
    keep = max_size // 2
    overflow = len(seen) - keep
    if overflow > 0:
        for key in list(seen)[:overflow]:
            del seen[key]


class TelegramChannel(Channel):
    """Telegram getUpdates 长轮询渠道。"""

    def __init__(
        self,
        cfg: ChannelConfig,
        sender: TelegramSender,
        *,
        client: TelegramClient | None = None,
        update_fetcher: Callable[[int, float], Awaitable[list]] | None = None,
        answer_callback: Callable[[str], Awaitable[Any]] | None = None,
        lease_checker: Callable[[], bool] | None = None,
        route_registry: TelegramRouteRegistry | None = None,
        users: TelegramUserResolver | None = None,
        ledger: TelegramCardLedger | None = None,
        offset_loader: Callable[[], int] | None = None,
        offset_persist: Callable[[int], None] | None = None,
        seen_loader: Callable[[], set] | None = None,
        seen_persist: Callable[[str], None] | None = None,
        seen_cleanup: Callable[[], None] | None = None,
        bot_username: str = "",
        max_concurrent: int = 20,
        poll_timeout: float = LONG_POLL_TIMEOUT,
    ) -> None:
        self._cfg = cfg
        self._sender = sender
        self._client = client
        self._update_fetcher = update_fetcher
        self._answer_callback = answer_callback
        self._lease_checker = lease_checker
        self._routes = route_registry if route_registry is not None else TelegramRouteRegistry()
        self._users = users if users is not None else TelegramUserResolver()
        self._ledger = ledger
        self._offset_loader = offset_loader or (lambda: 0)
        self._offset_persist = offset_persist or (lambda value: None)
        self._seen_loader = seen_loader or (lambda: set())
        self._seen_persist = seen_persist or (lambda key: None)
        self._seen_cleanup = seen_cleanup or (lambda: None)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._handler_tasks: set = set()
        self._poll_timeout = float(poll_timeout or LONG_POLL_TIMEOUT)
        self._offset = 0
        self._seen: dict = {}
        self.answered_callbacks: list[str] = []   # 测试观察哨：ack 先行证据
        self.messages_handled: list = []
        self.updates_seen = 0
        self.parse_fail = 0
        self.connected = False
        self.bot_token = str(cfg.raw.get("bot_token") or cfg.raw.get("app_id") or "")
        self.bot_username = str(bot_username or cfg.raw.get("bot_username") or "").lstrip("@")

    def type(self) -> Type:
        return Type.TELEGRAM

    def capabilities(self) -> Capability:
        return self._sender.capabilities()

    @property
    def cfg(self) -> ChannelConfig:
        return self._cfg

    @property
    def offset(self) -> int:
        return self._offset

    def _install_tag(self) -> str:
        return str(getattr(self._cfg, "installation_id", "") or "")[-12:] or "install"

    # ---------------------------------------------------------- 连接

    async def connect(self, ctx: CancelContext) -> None:
        """阻塞运行长轮询直到 ctx 取消 / 链路断开（契约同 Channel.connect）。"""
        if not self.bot_token:
            raise PermanentChannelError(
                "telegram config missing bot_token（@BotFather 发放的 Bot Token）")

        if not ctx.cancelled and self._lease_checker is not None:
            try:
                if not self._lease_checker():
                    log.info("[telegram:%s] lease lost, graceful handover", self._install_tag())
                    return
            except Exception as e:
                log.warning("[telegram] lease check error: %s", e)

        try:
            self._offset = int(self._offset_loader() or 0)
        except Exception:
            self._offset = 0
        self._seen = {key: None for key in (self._seen_loader() or set())}

        if not self.bot_username and self._client is not None:
            try:
                me = await self._client.get_me()
                self.bot_username = str(me.get("username") or "").lstrip("@")
            except Exception as e:
                log.info("[telegram] getMe failed (username unknown): %r", e)

        self.connected = True
        try:
            while not ctx.cancelled:
                try:
                    updates = await self._fetch()
                except (ChannelError, PermanentChannelError):
                    raise
                except TelegramApiError as e:
                    if e.code in _PERMANENT_CODES:
                        raise PermanentChannelError(
                            f"permanent telegram failure: {e}") from e
                    raise ChannelError(f"telegram getUpdates failed: {e}") from e
                except Exception as e:
                    raise ChannelError(f"telegram getUpdates error: {e!r}") from e
                for update in updates or []:
                    if ctx.cancelled:
                        break
                    try:
                        await self._on_update(ctx, update)
                    except (ChannelError, PermanentChannelError):
                        raise
                    except Exception as e:
                        self.parse_fail += 1
                        log.warning("[telegram] update handling failed: %r", e)
        finally:
            self.connected = False
            self._seen_cleanup()

    async def _fetch(self) -> list:
        if self._update_fetcher is not None:
            return await self._update_fetcher(self._offset, self._poll_timeout)
        if self._client is None:
            raise ChannelError("telegram client not configured")
        return await self._client.get_updates(
            self._offset, timeout=self._poll_timeout, allowed_updates=ALLOWED_UPDATES)

    async def _on_update(self, ctx: CancelContext, update: Any) -> None:
        if not isinstance(update, dict):
            self.parse_fail += 1
            return
        try:
            update_id = int(update.get("update_id") or 0)
        except Exception:
            update_id = 0
        if update_id:
            self._advance_offset(update_id + 1)
        self.updates_seen += 1

        # edited_message 显式忽略：offset 已推进，但不派发（防重复）
        if "edited_message" in update and "message" not in update:
            return
        if "callback_query" in update:
            await self._on_callback(ctx, update.get("callback_query"))
            return
        if "message" in update:
            await self._on_message(ctx, update)
            return

    def _advance_offset(self, next_offset: int) -> None:
        if next_offset <= self._offset:
            return
        self._offset = next_offset
        try:
            self._offset_persist(self._offset)
        except Exception as e:
            log.warning("[telegram] offset persist failed: %r", e)

    async def _on_message(self, ctx: CancelContext, update: dict) -> None:
        msg = update_to_inbound(update, bot_username=self.bot_username)
        if msg is None:
            self.parse_fail += 1
            return
        self._remember_route(msg)
        self._remember_sender(msg)

        if msg.raw.get("media_too_large") and not (msg.text or "").strip():
            await self._reply_media_too_large(msg)
            return
        if msg.raw.get("media_too_large"):
            await self._reply_media_too_large(msg)

        if not self._mark_seen(msg.event_id):
            return
        task = asyncio.get_running_loop().create_task(self._safe_handle(ctx, msg))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _on_callback(self, ctx: CancelContext, callback: Any) -> None:
        if not isinstance(callback, dict):
            self.parse_fail += 1
            return
        cq_id = str(callback.get("id") or "")
        # ⚠️ ACK 先行：先 answerCallbackQuery 再派发业务（不 ack 会重投）
        if cq_id:
            try:
                if self._answer_callback is not None:
                    await self._answer_callback(cq_id)
                elif self._client is not None:
                    await self._client.answer_callback_query(cq_id)
                self.answered_callbacks.append(cq_id)
            except Exception as e:
                log.warning("[telegram] answerCallbackQuery failed: %s", e)

        msg = callback_query_to_inbound(callback, ledger=self._ledger)
        if msg is None:
            self.parse_fail += 1
            return
        self._remember_route(msg)
        self._remember_sender(msg)
        if not self._mark_seen(msg.event_id):
            return
        task = asyncio.get_running_loop().create_task(self._safe_handle(ctx, msg))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _reply_media_too_large(self, msg) -> None:
        try:
            await self._sender.send(OutboundMessage(
                chat_id=msg.source.chat_id,
                text="附件超过 20MB，Telegram Bot API 无法下载，请压缩后重发。",
                reply_to=msg.reply_to,
            ))
        except Exception as e:
            log.warning("[telegram] media too large receipt failed: %r", e)

    def _remember_route(self, msg) -> None:
        try:
            if msg.message_id:
                self._routes.remember(msg.message_id, msg.source.chat_id)
            if msg.reply_to and msg.reply_to.message_id:
                self._routes.remember(msg.reply_to.message_id, msg.source.chat_id)
        except Exception as e:
            log.warning("[telegram] route register failed: %r", e)

    def _remember_sender(self, msg) -> None:
        name = str((msg.raw or {}).get("sender_name") or "")
        if name:
            try:
                self._users.remember(msg.source.sender_id, name)
            except Exception:
                pass

    def _mark_seen(self, key: str) -> bool:
        if not key:
            return True
        if key in self._seen:
            log.info("[telegram] duplicate update dropped: %s", key[:24])
            return False
        self._seen[key] = None
        try:
            self._seen_persist(key)
        except Exception:
            pass
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
                log.warning("[telegram] inbound handler crashed: %r", e)
                return
            if exc is not None:
                log.warning("[telegram] inbound handler infra error: %r", exc)

    async def disconnect(self) -> None:
        self.connected = False
        for task in list(self._handler_tasks):
            task.cancel()
        self._handler_tasks.clear()

    async def send(self, out: OutboundMessage) -> SendResult:
        return await self._sender.send(out)


__all__ = ["SEEN_MAX", "TelegramChannel"]