"""企微 Channel 实现（Issue #1254 批 1：长连接 + 订阅 + 心跳 + 接收循环）。

生命周期纪律（与 feishu / dingtalk 适配器同构）：
  - **订阅先行**：握手后立刻发 `aibot_subscribe{bot_id,secret}`，10s 内必须收到
    `errcode=0` ack，否则按连接失败处理（凭证类错误 → PermanentChannelError，
    Supervisor 置 revoked 不再重试；网络类 → ChannelError 退避重连）；
  - **心跳 30s / 读超时 90s**：官方建议 30s ping；90s 无任何字节判定断链
    （交 Supervisor 退避重连，绝不静默假活）；
  - **ack 兑现**：读循环里所有「无 cmd + 带 errcode」帧交给 transport.resolve，
    出站/上传的请求因此在同一连接上等到回执；
  - **收帧即下载媒体**：COS 直链 5 分钟过期，必须在收帧当刻下载解密（resolvers.py）；
  - **seen 去重**（A3）：内存 FIFO + 持久化钩子（同 msgId 重投只处理一次）；
  - **fire-and-forget + semaphore 限流**（A4）：单 bot 慢消息不拖垮进程；
  - **异常不穿透接收循环**（钉钉 #1008 教训）：任何解析/下载/分发异常都被兜住并计数，
    只有链路级失败才抛 ChannelError。

依赖注入：ws_connect / media_resolver / lease_checker / seen 钩子全部可替身，
测试用假 WS，不触网。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
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
from ...channel.types import ChatType, Type
from .inbound import (
    CMD_PING,
    WECOM_WS_URL,
    build_ping_frame,
    build_subscribe_frame,
    event_to_inbound,
    frame_body,
    frame_cmd,
    frame_req_id,
    is_ack_frame,
    is_disconnect_event,
    is_event_frame,
    media_items,
    parse_frame,
    template_card_event_to_inbound,
)
from .outbound import (
    WecomReplyRegistry,
    WecomSender,
    WecomTransport,
    WecomTransportError,
    default_reply_registry,
)
from .resolvers import WecomMediaCache, WecomMediaResolver

log = logging.getLogger(__name__)

SUBSCRIBE_ACK_TIMEOUT = 10.0   # 官方：订阅须 10s 内收到 errcode=0
PING_INTERVAL = 30.0           # 官方建议 30s 一次心跳
READ_TIMEOUT = 90.0            # 90s 无字节 → 判定断链
SEEN_MAX = 2000                # A3：内存去重集上限
OPEN_TIMEOUT = 10.0

LeaseChecker = Callable[[], bool]


def _trim_seen(seen: dict, max_size: int) -> None:
    """FIFO 裁剪（按插入序淘汰最旧 msgId，保留最近 max_size//2 条）。"""
    keep = max_size // 2
    overflow = len(seen) - keep
    if overflow > 0:
        for key in list(seen)[:overflow]:
            del seen[key]


def _is_permanent_credentials_error(errcode: Any) -> bool:
    """凭证类永久失败判定（A5 短路）：订阅被拒的错误码不需重试。

    企微对 bot_id/secret 错误的返回是 errcode!=0（如 40001/40013 类），
    这里把「非 0 且非明显网络/限流」统一视为凭证失败 —— 反复重连只会触发
    订阅频率保护（官方明确警告），宁可 revoked 等人工修配置。
    """
    try:
        code = int(errcode)
    except Exception:
        return False
    if code == 0:
        return False
    return code not in (-1, 45009, 45011)   # -1 网络类 / 45009,45011 限流类 → 可重试


class WecomChannel(Channel):
    """企微智能机器人长连接渠道。"""

    def __init__(
        self,
        cfg: ChannelConfig,
        sender: WecomSender,
        *,
        transport: WecomTransport | None = None,
        reply_registry: WecomReplyRegistry | None = None,
        media_resolver: WecomMediaResolver | None = None,
        ws_connect: Callable | None = None,
        lease_checker: LeaseChecker | None = None,
        seen_loader: Callable | None = None,
        seen_persist: Callable | None = None,
        seen_cleanup: Callable | None = None,
        max_concurrent: int = 20,
    ) -> None:
        self._cfg = cfg
        self._sender = sender
        bot_id = str(cfg.raw.get("bot_id") or cfg.raw.get("app_id") or "")
        # 机器人显示名用于群聊 @ 裁决（官方无独立 mention 字段，正文是 "@显示名 内容"）。
        # 安装配置未提供时留空 → strip_mention 保持「首 token 是 @」的兼容口径。
        self.bot_name = str(cfg.raw.get("bot_name") or cfg.raw.get("robot_name")
                            or cfg.raw.get("aibot_name") or "")
        self._transport = transport if transport is not None else WecomTransport(bot_id)
        self._registry = (reply_registry if reply_registry is not None
                          else default_reply_registry())
        self._resolver = (media_resolver if media_resolver is not None
                          else WecomMediaResolver())
        self._ws_connect = ws_connect
        self._lease_checker = lease_checker
        self._seen_loader = seen_loader or (lambda app: set())
        self._seen_persist = seen_persist or (lambda app, mid: None)
        self._seen_cleanup = seen_cleanup or (lambda app: None)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # 帧级任务（下载/解析）与 handler 任务分离：读循环只负责收帧与兑现 ack，
        # 媒体下载不再阻塞后续帧（否则 10s 请求 ack 可能被下载拖到超时）。
        self._download_semaphore = asyncio.Semaphore(max(1, min(4, max_concurrent)))
        self._frame_tasks: set = set()
        self._handler_tasks: set = set()
        self._ping_task: asyncio.Task | None = None
        self._stop_ping = False
        self.acked_frames: list[str] = []   # 测试观察哨
        self.messages_handled: list = []    # 测试观察哨
        self.parse_fail = 0
        self.subscribed = False
        self.bot_id = bot_id

    def type(self) -> Type:
        return Type.WECOM

    def capabilities(self) -> Capability:
        return self._sender.capabilities()

    @property
    def media_cache(self) -> WecomMediaCache:
        return self._resolver.cache

    # ---------------------------------------------------------- 连接

    async def connect(self, ctx: CancelContext) -> None:
        """阻塞运行接收循环直到 ctx 取消 / 链路断开（契约同 Channel.connect）。"""
        bot_id = str(self._cfg.raw.get("bot_id") or self._cfg.raw.get("app_id") or "")
        secret = str(self._cfg.raw.get("secret") or self._cfg.raw.get("app_secret") or "")
        if not bot_id or not secret:
            raise PermanentChannelError(
                "wecom config missing bot_id/secret (API 模式须填智能机器人 BotID + Secret)")

        if not ctx.cancelled and self._lease_checker is not None:
            try:
                if not self._lease_checker():
                    log.info("[wecom:%s] lease lost, graceful handover", bot_id)
                    return
            except Exception as e:
                log.warning("[wecom:%s] lease check error: %s", bot_id, e)

        ws_url = str(self._cfg.raw.get("ws_url") or WECOM_WS_URL)
        if self._ws_connect is None:
            import websockets
            self._ws_connect = lambda url: websockets.connect(   # noqa: E731
                url, ping_interval=None, ping_timeout=None,
                open_timeout=OPEN_TIMEOUT, close_timeout=5, max_size=2 ** 23)

        seen: dict = {mid: None for mid in self._seen_loader(bot_id)}
        try:
            async with self._ws_connect(ws_url) as ws:
                self._transport.attach(ws)
                # 读循环先起：订阅 ack / 发送 ack 都由它兑现（同一条连接）
                read_task = asyncio.get_running_loop().create_task(
                    self._read_loop(ctx, ws, bot_id, seen))
                sub_task = asyncio.get_running_loop().create_task(
                    self._subscribe(ws, bot_id, secret))
                try:
                    await self._await_subscribed(read_task, sub_task)
                finally:
                    self._stop_ping_task()
                    for task in (read_task, sub_task):
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except (asyncio.CancelledError, Exception):
                                pass
                    self._transport.detach(ws)
        except (ChannelError, PermanentChannelError):
            raise
        except Exception as e:
            raise ChannelError(f"wecom ws failed (bot={bot_id[:12]}): {e!r}") from e

        if ctx.cancelled:
            return
        self._seen_cleanup(bot_id)
        raise ChannelError(f"ws closed: {bot_id[:12]}")

    async def _await_subscribed(self, read_task: asyncio.Task,
                                sub_task: asyncio.Task) -> None:
        """等订阅成功后再进入「等读循环」；任一任务先失败 → 立刻把异常抛给 Supervisor。

        不能只 await 订阅：读循环与订阅同一条连接，若读循环先挂（例如服务端在订阅
        完成前就下发 kicked/断链），只等订阅会退化成 10s ack 超时，把真实原因盖掉。
        """
        done, pending = await asyncio.wait(
            {read_task, sub_task}, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
        if read_task in pending:
            await read_task

    async def _subscribe(self, ws: Any, bot_id: str, secret: str) -> None:
        """发订阅帧并等 ack（10s）：成功才进入接收循环。只发一次（平台有订阅频率保护）。"""
        req_id = self._transport.next_req_id("subscribe")
        frame = build_subscribe_frame(bot_id, secret, req_id)
        ack: dict = {}
        timeout = float(self._cfg.raw.get("subscribe_timeout") or SUBSCRIBE_ACK_TIMEOUT)
        try:
            ack = await self._transport.request_frame(frame, timeout=timeout)
        except WecomTransportError as e:
            raise ChannelError(f"wecom subscribe ack missing: {e}") from e
        errcode = ack.get("errcode", 0)
        if str(errcode).strip() not in ("0", "None", ""):
            if _is_permanent_credentials_error(errcode):
                raise PermanentChannelError(
                    f"wecom subscribe rejected: errcode={errcode} errmsg={ack.get('errmsg', '')}")
            raise ChannelError(
                f"wecom subscribe failed: errcode={errcode} errmsg={ack.get('errmsg', '')}")
        self.subscribed = True
        self._start_ping(ws)      # 订阅成功后立即起心跳（30s 一次 ping）
        log.info("[wecom:%s] subscribed", bot_id[:12])

    # ---------------------------------------------------------- 心跳

    def _start_ping(self, ws: Any) -> None:
        self._stop_ping = False
        self._ping_task = asyncio.get_running_loop().create_task(self._ping_loop(ws))

    def _stop_ping_task(self) -> None:
        self._stop_ping = True
        task = self._ping_task
        self._ping_task = None
        if task is not None:
            task.cancel()

    async def _ping_loop(self, ws: Any) -> None:
        interval = float(self._cfg.raw.get("ping_interval") or PING_INTERVAL)
        try:
            while not self._stop_ping:
                await asyncio.sleep(interval)
                if self._stop_ping:
                    return
                try:
                    req_id = self._transport.next_req_id("ping")
                    await ws.send(json.dumps(build_ping_frame(req_id), ensure_ascii=False))
                except Exception as e:
                    log.warning("[wecom] ping failed: %r", e)
                    return
        except asyncio.CancelledError:
            return

    # ---------------------------------------------------------- 接收循环

    async def _read_loop(self, ctx: CancelContext, ws: Any, bot_id: str,
                         seen: dict) -> None:
        read_timeout = float(self._cfg.raw.get("read_timeout") or READ_TIMEOUT)
        while not ctx.cancelled:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=read_timeout)
            except asyncio.TimeoutError as e:
                raise ChannelError(f"wecom read timeout ({read_timeout}s)") from e
            except asyncio.CancelledError:
                raise
            except Exception as e:
                raise ChannelError(f"wecom read failed: {e!r}") from e
            if raw is None:
                break
            try:
                await self._on_frame(ctx, raw, bot_id, seen)
            except (ChannelError, PermanentChannelError):
                raise
            except Exception as e:
                # 单帧异常绝不穿透（否则接收循环静默死亡，钉钉 #1008 教训）
                self.parse_fail += 1
                log.warning("[wecom] frame handling failed (bot=%s): %r", bot_id[:12], e)

    async def _on_frame(self, ctx: CancelContext, raw: Any, bot_id: str,
                        seen: dict) -> None:
        payload = parse_frame(raw)
        if not payload:
            self.parse_fail += 1
            log.info("[wecom] unparsable frame dropped (bot=%s)", bot_id[:12])
            return

        # ack（订阅/发送/上传的响应）→ 兑现等待中的请求
        if is_ack_frame(payload):
            self._transport.resolve(payload)
            return
        if self._transport.resolve(payload):
            return

        cmd = frame_cmd(payload)
        if cmd == CMD_PING or cmd == "pong":
            # 服务端心跳/回执：立即回一帧保活，不分发 handler
            try:
                await self._transport.notify(CMD_PING)
            except Exception:
                pass
            return

        if is_disconnect_event(payload):
            # 同一机器人新连接订阅成功 → 服务端踢掉旧连接（多副本抢连接的显式信号）
            raise ChannelError(f"wecom connection kicked by new subscription (bot={bot_id[:12]})")

        if is_event_frame(payload):
            await self._handle_event(ctx, payload, bot_id, seen)
            return

        self._spawn_message(ctx, payload, bot_id, seen)

    def _spawn_message(self, ctx: CancelContext, payload: dict, bot_id: str,
                       seen: dict) -> None:
        """消息帧后台处理：读循环立即返回继续兑现 ack/心跳，媒体下载不再阻塞。"""
        task = asyncio.get_running_loop().create_task(
            self._safe_message(ctx, payload, bot_id, seen))
        self._frame_tasks.add(task)
        task.add_done_callback(self._frame_tasks.discard)

    async def _safe_message(self, ctx: CancelContext, payload: dict, bot_id: str,
                            seen: dict) -> None:
        try:
            await self._handle_message(ctx, payload, bot_id, seen)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.parse_fail += 1
            log.warning("[wecom] message handling failed (bot=%s): %r", bot_id[:12], e)

    async def _handle_message(self, ctx: CancelContext, payload: dict, bot_id: str,
                              seen: dict) -> None:
        body = frame_body(payload)
        msg_id = str(body.get("msgid") or "")
        # 去重先于下载（并发帧任务下仍只处理一次）；下载失败/解析失败也保持已声称语义，
        # 避免同一 msgid 因重投重复触发下载。
        if msg_id:
            if msg_id in seen:
                return
            seen[msg_id] = None
            self._seen_persist(bot_id, msg_id)
            if len(seen) > SEEN_MAX:
                _trim_seen(seen, SEEN_MAX)

        items = media_items(payload)
        media_keys: list[str] = []
        if items:
            # 收帧即下载（COS URL 5 分钟有效）；受下载并发上限约束，失败项留空串且不移位
            async with self._download_semaphore:
                media_keys = await self._resolver.fetch(items, msg_id)

        msg = event_to_inbound(payload, media_keys=media_keys, bot_name=self.bot_name)
        if msg is None:
            self.parse_fail += 1
            log.info("[wecom] message frame not translatable (msgid=%s)", msg_id)
            return
        self._remember_reply_ctx(msg)
        self._dispatch(ctx, msg)

    async def _handle_event(self, ctx: CancelContext, payload: dict, bot_id: str,
                            seen: dict) -> None:
        msg = template_card_event_to_inbound(payload)
        if msg is None:
            # enter_chat / feedback_event 等：不派发（欢迎语与反馈 v1 不做）
            log.debug("[wecom] event ignored: %s", frame_req_id(payload))
            return
        # 卡片事件没有回复窗口：只能主动推送（事件 req_id 不能当回复令牌，
        # 见 WecomReplyRegistry.push_only —— 11-06 实障：拿它发 respond_msg 会被静默丢弃）
        self._remember_push_route(msg)
        if msg.message_id and msg.message_id in seen:
            return
        if msg.message_id:
            seen[msg.message_id] = None
            self._seen_persist(bot_id, msg.message_id)
        log.info("[wecom] card event → dispatch chat=%s action=%s",
                 (msg.source.chat_id or "")[-12:], (msg.text or "")[:24])
        self._dispatch(ctx, msg)

    def _remember_push_route(self, msg) -> None:
        """卡片事件会话登记：只记 chat_type，禁止复用事件 req_id 作回复令牌。"""
        chat_id = msg.source.chat_id
        chat_type = 2 if msg.source.chat_type == ChatType.GROUP else 1
        try:
            self._registry.push_only(chat_id, chat_type)
        except Exception as e:
            log.warning("[wecom] push route register failed: %r", e)

    def _remember_reply_ctx(self, msg) -> None:
        """登记 (chat_id → req_id, chat_type)：出站据此决定回复窗口内/外。"""
        chat_id = msg.source.chat_id
        req_id = frame_req_id(msg.raw) if isinstance(msg.raw, dict) else ""
        chat_type = 2 if msg.source.chat_type == ChatType.GROUP else 1
        try:
            self._registry.register(chat_id, req_id, chat_type)
        except Exception as e:
            log.warning("[wecom] reply ctx register failed: %r", e)

    def _dispatch(self, ctx: CancelContext, msg) -> None:
        task = asyncio.get_running_loop().create_task(self._safe_handle(ctx, msg))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

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
                log.warning("[wecom] inbound handler crashed: %r", e)
                return
            if exc is not None:
                log.warning("[wecom] inbound handler infra error: %r", exc)

    async def disconnect(self) -> None:
        self._stop_ping_task()
        for t in list(self._frame_tasks) + list(self._handler_tasks):
            t.cancel()
        self._frame_tasks.clear()
        self._handler_tasks.clear()
        self._transport.detach()

    async def send(self, out: OutboundMessage) -> SendResult:
        return await self._sender.send(out)


__all__ = [
    "PING_INTERVAL",
    "READ_TIMEOUT",
    "SUBSCRIBE_ACK_TIMEOUT",
    "WecomChannel",
]