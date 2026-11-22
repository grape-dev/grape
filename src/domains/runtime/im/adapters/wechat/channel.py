"""微信 Channel 实现（Issue #1728 M1：getupdates 长轮询 + 游标持久化）。

生命周期纪律（与 feishu / dingtalk / wecom / slack / telegram 同构）：

  - **长轮询**：`POST /ilink/bot/getupdates`（默认 35s），游标 `get_updates_buf`
    变化即持久化到 `grape_t_channel_installation.config["cursor"]`（回调注入），重启续接不重投；
  - **`ret = -14`（风控）**：账号会话暂停 1h —— 记 `session_paused_risk`、落
    `grape_t_channel_health`（paused / last_pause_at / next_retry_at / 连续次数 / 告警态），
    按阈值升级为「微信渠道不可用」告警；暂停期间不轮询，但等待可被人工 `retry_now` 唤醒
    （Issue #3799）；退出前 `notifystop`；
  - **自消息丢弃**：`message_type=2`（Bot 自己）由 inbound 直接过滤，避免自回环；
  - **异常不穿透接收循环**：单条消息解析/分发异常兜住并计数，只有链路级失败
    （网络/5xx）才抛 `ChannelError`；`ret=-14` 后暂停重连；
  - `notifystart` / `notifystop` 失败按 (instance, reason, 小时窗口) 聚合，窗口内 ≤1 条汇总
    （Issue #3799：原先逐条 WARNING，暂停期间每小时 30 条刷屏）；
  - lease 复查失配 → 优雅让渡。

依赖注入：`update_fetcher` / `cursor_loader` / `cursor_persist` / `lease_checker` /
`sleep` / `health`（健康状态机）/ `log_agg`（失败日志聚合）全部可替身，测试不触网。
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
from ...channel_health import HourlyLogAggregator, log_summary
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
        health: Any = None,
        log_agg: HourlyLogAggregator | None = None,
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
        # Issue #3799：健康状态机（注入自引擎级 tracker，缺省不启用 → 行为与旧版一致）
        self._health = health
        self._log_agg = log_agg if log_agg is not None else HourlyLogAggregator()
        # 人工 retry_now 的唤醒闸：暂停等待 ctx.wait() 与它二选一先到
        self._retry_wake = asyncio.Event()
        self.retry_requests = 0
        # 连接循环是否在跑（跨线程 request_retry 用；也让管理台能区分"没连上"）
        self._loop_running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # 是否正处于暂停等待（Issue #3799 审查 Optional）：健康渠道上的 retry_now
        # 不置唤醒闸，避免闸滞留到下一次 ret=-14 跳过 1h 等待立即重试。
        self._pause_wait_active = False

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
            except Exception as e:
                log.debug("[wechat] metrics record skipped: %s", e)

    # ---------------------------------------------------------- 渠道健康（#3799）

    @property
    def health_key(self) -> str:
        """健康状态行主键（安装 id；缺省安装视图用 `_install_tag`，与日志标签一致）。"""
        raw = str(getattr(self._cfg, "installation_id", "") or "")
        if not raw or raw.strip("0-") == "":
            return self._install_tag()          # 全零 UUID（未落库的兜底视图）
        return raw

    def _record_pause(self, reason: str) -> None:
        tracker = self._health
        if tracker is None:
            return
        try:
            tracker.record_pause(self.health_key, "wechat", reason=reason,
                                 next_retry_at=time.time() + self._pause_seconds)
        except Exception as e:      # 健康态记录失败不得影响接收循环
            log.warning("[wechat:%s] health record pause failed: %s",
                        self._install_tag(), e)

    def _record_resume(self) -> None:
        tracker = self._health
        if tracker is None:
            return
        try:
            tracker.record_resume(self.health_key, "wechat")
        except Exception as e:
            log.warning("[wechat:%s] health record resume failed: %s",
                        self._install_tag(), e)

    def request_retry(self) -> bool:
        """人工 `retry_now`：唤醒暂停等待，立即重新 getupdates（Issue #3799）。

        返回 False = 本进程没有这条安装的连接循环在跑（由管理台报 channel_not_running，
        不假装成功）。
        """
        if not self._loop_running:
            return False
        if not self._pause_wait_active:
            # 渠道健康时无事可唤醒（PR #1983 审查 Optional）：不置闸——置了会滞留，
            # 下一次 ret=-14 将跳过 1h 等待立即重试（账号级风控下有加重风险）。
            # 返回 True：连接循环在跑，不向管理台报 channel_not_running。
            return True
        self.retry_requests += 1
        loop = self._loop
        wake = self._retry_wake
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and not loop.is_closed() and running is not loop:
            # 管理台线程调用：Event.set 必须在持有 loop 的线程上执行
            try:
                loop.call_soon_threadsafe(wake.set)
                return True
            except RuntimeError as e:
                log.warning("[wechat:%s] retry schedule failed: %s",
                            self._install_tag(), e)
                return False
        wake.set()
        return True

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
        self._loop_running = True
        self._retry_wake.clear()        # 每次建连重置唤醒闸，避免上一次遗留的 retry 立即触发
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        try:
            while not ctx.cancelled:
                try:
                    response = await self._fetch()
                except WechatApiError as e:
                    if e.session_paused:
                        if await self._on_session_paused(ctx, e):
                            # 人工 retry_now 唤醒：重放 notifystart 后立即重新 getupdates
                            await self._notify_start()
                            continue
                        return
                    raise ChannelError(f"wechat getupdates failed: {e}") from e
                except ChannelError:
                    raise
                except Exception as e:
                    raise ChannelError(f"wechat getupdates error: {e!r}") from e

                # 轮询成功 = getupdates 已恢复：清 paused / 告警 resolved（未暂停时零开销）
                self._record_resume()
                await self._consume(response, ctx)
                self._advance_cursor(as_str(response.get("get_updates_buf")))
        finally:
            self.connected = False
            self._loop_running = False
            self._loop = None
            await self._notify_stop()

    async def _fetch(self) -> dict:
        if self._update_fetcher is not None:
            return await self._update_fetcher(self._cursor, self._poll_timeout)
        if self._client is None:
            raise ChannelError("wechat client not configured")
        return await self._client.get_updates(self._cursor, timeout=self._poll_timeout)

    async def _on_session_paused(self, ctx: CancelContext, error: WechatApiError) -> bool:
        """`ret=-14` 暂停分支（Issue #3799）：状态机 + 指标/告警，等待可被人工唤醒。

        返回 True = 被 `request_retry()` 唤醒（调用方重放 notifystart 后立即重新 poll）；
        False = 自然等到暂停时长结束 / ctx 取消（调用方按旧语义结束连接，交给 Supervisor 重连）。
        """
        self.session_paused += 1
        self.paused_for = self._pause_seconds
        self._record_degraded("session_paused_risk")
        reason = f"ret={error.ret or error.errcode}"
        self._record_pause(reason)
        if ctx.cancelled or self._pause_seconds <= 0:
            return False
        self._pause_wait_active = True
        ctx_wait = asyncio.ensure_future(ctx.wait())
        retry_wait = asyncio.ensure_future(self._retry_wake.wait())
        retry_hit = False
        try:
            done, _pending = await asyncio.wait(
                {ctx_wait, retry_wait}, timeout=self._pause_seconds,
                return_when=asyncio.FIRST_COMPLETED)
            retry_hit = retry_wait in done and not retry_wait.cancelled()
        finally:
            for fut in (ctx_wait, retry_wait):
                if not fut.done():
                    fut.cancel()
            # 唤醒闸只在本次等待结束后复位：等待期间到达的 retry（含 race）不丢。
            self._retry_wake.clear()
            self._pause_wait_active = False
        if retry_hit and not ctx.cancelled:
            log.info("[wechat:%s] manual retry requested → resume polling now "
                     "(paused_for=%.0fs)", self._install_tag(),
                     max(0.0, self._pause_seconds))
        return retry_hit and not ctx.cancelled

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

    def _notify_failure(self, reason: str, exc: Exception) -> None:
        """notifystart/notifystop 失败 → 按 (instance, reason, 小时窗口) 聚合（Issue #3799）。

        窗口内只累加，滚动时输出 1 条汇总；单次失败在窗口内不打印（原先逐条 WARNING，
        暂停期间每小时 30 条刷屏，与真实异常混叠）。
        """
        try:
            summary = self._log_agg.note(self._install_tag(), reason, detail=repr(exc))
        except Exception as agg_error:
            log.warning("[wechat] notify failure aggregation failed: %s", agg_error)
            return
        if summary is not None:
            log_summary("[wechat] notifystart/notifystop", summary)

    def _flush_log_agg(self) -> None:
        try:
            summaries = self._log_agg.flush()
        except Exception as e:
            log.warning("[wechat] notify failure flush failed: %s", e)
            return
        for summary in summaries:
            log_summary("[wechat] notifystart/notifystop", summary)

    async def _notify_start(self) -> None:
        # notify 属于协议客户端能力，sender 在引擎里被 CountingSender 包了一层，
        # 不带 notify_*；优先走 client，兜底再试 sender（缺失即跳过，不算故障）。
        notifier = getattr(self._client, "notify_start", None) or getattr(self._sender, "notify_start", None)
        if notifier is None:
            return
        try:
            await notifier()
        except Exception as e:
            self._notify_failure("notifystart", e)

    async def _notify_stop(self) -> None:
        notifier = getattr(self._client, "notify_stop", None) or getattr(self._sender, "notify_stop", None)
        if notifier is None:
            return
        try:
            await notifier()
        except Exception as e:
            self._notify_failure("notifystop", e)

    async def disconnect(self) -> None:
        self.connected = False
        self._loop_running = False
        for task in list(self._handler_tasks):
            task.cancel()
        self._handler_tasks.clear()
        await self._notify_stop()
        self._flush_log_agg()
        # sender（可能被 CountingSender 透明包装）自己的聚合窗口也要收尾
        # （PR #1983 审查 Required：sender 侧无 flush 调用点会丢最后一个窗口）。
        inner = getattr(self._sender, "_inner", self._sender)
        sender_flush = getattr(inner, "flush_log_agg", None)
        if callable(sender_flush):
            try:
                sender_flush()
            except Exception as e:
                log.warning("[wechat:%s] sender log agg flush failed: %s",
                            self._install_tag(), e)

    async def send(self, out: OutboundMessage) -> SendResult:
        return await self._sender.send(out)


__all__ = ["SEEN_MAX", "WechatChannel"]