"""飞书 Channel 实现（Issue #742 §2.2 / #760 P1）。

run_bot（feishu_async.py L1181-1431）生命周期硬功夫 A6 原样迁入 connect()：
  - 协议级 ping 禁用（ping_interval=None：飞书不回协议级 pong 会触发超时断连）；
  - 应用层 PING 保活（120s）+ 空闲 5min 强制重连（飞书停推事件检测）；
  - CONTROL PING 帧原位回写 PONG（WS 假死根因：TCP 未断但消息不再到达）；
  - 每 DATA 帧 ack 先行（解析失败也 ack，否则飞书用新 message_id 反复重投）；
  - seen 去重跨重启持久化（A3：内存 set + bot_seen_messages 表，上限 2000）；
  - fire-and-forget + semaphore 限流（A4：单 bot 慢消息不拖垮进程）；
  - 永久错误码 10002/10003/10014 → PermanentChannelError（A5 短路）；
  - 重连后 lease 复查（问题 A：token 失配 → 优雅退出让渡）。

注入点全部可替身（endpoint_fetcher / ws_connect / lease_checker /
seen 持久化钩子），测试不触网。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import websockets
from lark_oapi.ws.pb.pbbp2_pb2 import Frame
from lark_oapi.ws.enum import FrameType, MessageType

from ...channel.channel import (
    CancelContext, Channel, ChannelConfig, ChannelError, PermanentChannelError,
)
from ...channel.capability import Capability
from ...channel.message import InboundMessage, OutboundMessage, SendResult
from ...channel.types import Type
from .inbound import parsed_to_inbound
from .outbound import FeishuSender

log = logging.getLogger(__name__)

# 永久错误码短路（A5）：WS 端点 10002/10003/10014（app 不存在/secret 无效）
PERMANENT_WS_ERROR_CODES = ("10002", "10003", "10014")

PING_INTERVAL = 120.0        # 应用层 PING 保活周期（lark-oapi SDK 同款）
IDLE_RECONNECT_SECS = 300.0  # 空闲超时：5 分钟无用户消息 → 主动断开换新 URL
SEEN_MAX = 2000              # A3：内存去重集上限（超出裁剪至一半）

# 解析/帧辅助（原 feishu_async.py 抽出，见专题当前无 legacy 依赖）；
# seen 去重持久化在中立 seen_store（BotContext 也用同一份实现）。
from .wire import _build_ack_frame, _get_header, _parse_card_action, _parse_event  # noqa: E402
from ...seen_store import (  # noqa: E402
    _cleanup_seen_msg_ids, _load_seen_msg_ids, _persist_seen_msg_id,
)

EndpointFetcher = Callable[[str, str], Awaitable[tuple]]
LeaseChecker = Callable[[], bool]


class FeishuChannel(Channel):
    """飞书 WS 长连接渠道。connect() = run_bot 生命周期迁移（A6）。"""

    def __init__(
        self,
        cfg: ChannelConfig,
        sender: FeishuSender,
        *,
        endpoint_fetcher: Optional[EndpointFetcher] = None,
        ws_connect: Optional[Callable] = None,
        lease_checker: Optional[LeaseChecker] = None,
        seen_loader: Callable = _load_seen_msg_ids,
        seen_persist: Callable = _persist_seen_msg_id,
        seen_cleanup: Callable = _cleanup_seen_msg_ids,
        max_concurrent: int = 20,
        idle_reconnect_secs: float = IDLE_RECONNECT_SECS,
        ping_interval: float = PING_INTERVAL,
    ) -> None:
        self._cfg = cfg
        self._sender = sender
        self._endpoint = endpoint_fetcher
        self._ws_connect = ws_connect or websockets.connect
        self._lease_checker = lease_checker
        self._seen_loader = seen_loader
        self._seen_persist = seen_persist
        self._seen_cleanup = seen_cleanup
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._idle_reconnect_secs = idle_reconnect_secs
        self._ping_interval = ping_interval
        self._last_user_message_at = 0.0
        self._handler_tasks: set = set()
        self.messages_handled: list = []   # 测试观察哨

    def type(self) -> Type:
        return Type.FEISHU

    def capabilities(self) -> Capability:
        return self._sender.capabilities()

    async def connect(self, ctx: CancelContext) -> None:
        """阻塞运行接收循环直到 ctx 取消/链路断开。

        链路断开 → raise ChannelError（Supervisor 指数退避重连）；
        永久失败 → raise PermanentChannelError（Supervisor 置 revoked）；
        lease 失配 → 正常 return（优雅让渡，scan 周期后由归属实例接管）。
        必须容忍在新 ctx 上被重复调用（继承 run_bot 语义）。
        """
        app_id = str(self._cfg.raw.get("app_id", ""))
        app_secret = str(self._cfg.raw.get("app_secret", ""))
        if self._endpoint is None:
            # 默认端点：legacy FeishuAsync API 客户端（get_ws_endpoint(ctx) 签名适配）
            from ...feishu_async import BotContext, FeishuAsync
            api = FeishuAsync()
            bot_ctx = BotContext(app_id=app_id, app_secret=app_secret)

            async def _fetch(aid: str, secret: str):
                return await api.get_ws_endpoint(bot_ctx)

            self._endpoint = _fetch

        if not ctx.cancelled and self._lease_checker is not None:
            # 问题 A：连接前复查 lease 是否还在自己手里（token 失配 → 让渡）
            try:
                if not self._lease_checker():
                    log.info("[feishu:%s] lease lost, graceful handover", app_id)
                    return
            except Exception as e:
                log.warning("[feishu:%s] lease check error: %s", app_id, e)

        seen: set = self._seen_loader(app_id)   # A3：跨重启去重恢复
        try:
            ws_url, err_code = await self._endpoint(app_id, app_secret)
        except ChannelError:
            raise
        except Exception as e:
            # 契约：链路断开/基础设施异常一律归一为 ChannelError，
            # 供 Supervisor 退避重连分类（不让原生网络异常绕过分类）
            raise ChannelError(f"ws endpoint fetch failed: {e!r}") from e
        if not ws_url:
            if str(err_code) in PERMANENT_WS_ERROR_CODES:
                raise PermanentChannelError(
                    f"permanent WS failure: code={err_code}, app disabled")
            raise ChannelError(f"no ws endpoint: code={err_code}")

        _ws_service_id = 0
        try:
            if "fpid=" in ws_url:
                _ws_service_id = int(ws_url.split("fpid=")[1].split("&")[0])
        except Exception:
            pass

        # 协议级 ping 禁用（飞书不回协议级 pong）；保活由应用层 PING 负责
        async with self._ws_connect(
            ws_url, ping_interval=None, close_timeout=5,
        ) as ws:
            self._last_user_message_at = time.time()   # 空闲宽限期初始化（PITFALL）
            ping_task = asyncio.get_running_loop().create_task(
                self._ping_keepalive(ws, _ws_service_id, ctx))
            try:
                async for raw in ws:
                    if ctx.cancelled:
                        break
                    if not isinstance(raw, bytes):
                        continue
                    try:
                        frame = Frame()
                        frame.ParseFromString(raw)
                        ft = FrameType(frame.method)
                    except Exception:
                        continue

                    if ft == FrameType.CONTROL:
                        self._handle_control(ws, frame)
                        continue
                    if ft != FrameType.DATA:
                        continue

                    type_ = _get_header(frame.headers, "type")
                    if type_ not in (MessageType.EVENT.value, MessageType.CARD.value):
                        continue

                    # ⚠️ payload 提取必须先于 ack：_build_ack_frame 会原位改写
                    # frame.payload（legacy run_bot 同序：先解析后 ack）
                    event_data: dict = {}
                    try:
                        event_data = json.loads(
                            frame.payload.decode("utf-8") if frame.payload else "")
                    except Exception:
                        event_data = {}

                    # ack 先行：无论消息是否被处理都必须回传，否则飞书反复重投
                    # （解析失败也 ack，防重投死循环）
                    try:
                        await ws.send(_build_ack_frame(frame))
                    except Exception as e:
                        log.warning("[feishu:%s] send ack failed: %s", app_id, e)

                    if not event_data:
                        continue

                    parsed = _parse_event(event_data)
                    if not parsed or not parsed.get("text"):
                        if type_ == MessageType.CARD.value:
                            parsed = _parse_card_action(event_data)
                        if not parsed or not parsed.get("text"):
                            continue

                    msg_id = parsed.get("message_id", "")
                    if msg_id:
                        if msg_id in seen:
                            continue
                        seen.add(msg_id)
                        self._seen_persist(app_id, msg_id)
                        if len(seen) > SEEN_MAX:
                            seen = set(list(seen)[-int(SEEN_MAX / 2):])

                    self._last_user_message_at = time.time()
                    t = asyncio.get_running_loop().create_task(
                        self._safe_handle(ctx, parsed, event_data))
                    self._handler_tasks.add(t)
                    t.add_done_callback(self._handler_tasks.discard)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except (asyncio.CancelledError, Exception):
                    pass

        if ctx.cancelled:
            return
        # 链路断开 → Supervisor 按退避重连（含空闲超时主动断开场景）
        self._seen_cleanup(app_id)
        raise ChannelError(f"ws closed: {self._cfg.raw.get('app_id', '')}")

    async def _ping_keepalive(self, ws, service_id: int, ctx: CancelContext) -> None:
        """应用层 PING 保活：每 120s 发 PING；空闲超 idle 阈值主动断开换新 URL。"""
        while not ctx.cancelled:
            try:
                await asyncio.sleep(self._ping_interval)
                idle = time.time() - self._last_user_message_at
                if idle > self._idle_reconnect_secs:
                    log.info("[feishu] idle %.0fs, forcing reconnect", idle)
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
                ping = Frame()
                ping.method = FrameType.CONTROL.value
                ping.service = service_id
                ping.SeqID = 0
                ping.LogID = 0
                ping.headers.add(key="type", value=MessageType.PING.value)
                await ws.send(ping.SerializeToString())
            except asyncio.CancelledError:
                return
            except Exception:
                return

    def _handle_control(self, ws, frame) -> None:
        """CONTROL 帧处理：飞书发 PING → 原位回写 PONG（不回会被判死连接停推）。"""
        if _get_header(frame.headers, "type") != MessageType.PING.value:
            return

        async def _pong() -> None:
            try:
                for h in frame.headers:
                    if h.key == "type":
                        h.value = MessageType.PONG.value
                await ws.send(frame.SerializeToString())
            except Exception:
                pass

        try:
            asyncio.get_running_loop().create_task(_pong())
        except RuntimeError:
            pass

    async def _safe_handle(self, ctx: CancelContext, parsed: dict,
                           raw_event: dict) -> None:
        """A4：semaphore 限流 + 异常隔离；handler 返回异常仅代表基础设施失败。"""
        handler = self._cfg.inbound_handler
        if handler is None:
            return
        msg = parsed_to_inbound(parsed, raw_event)
        self.messages_handled.append(msg)
        async with self._semaphore:
            if ctx.cancelled:
                return
            try:
                exc = await handler(ctx, msg)
            except Exception as e:
                log.warning("[feishu] inbound handler crashed: %r", e)
                return
            if exc is not None:
                # 基础设施失败（产品性丢弃为 None）：记日志，重投递依赖去重 release 语义
                log.warning("[feishu] inbound handler infra error: %r", exc)

    async def disconnect(self) -> None:
        for t in list(self._handler_tasks):
            t.cancel()
        self._handler_tasks.clear()

    async def send(self, out: OutboundMessage) -> SendResult:
        return await self._sender.send(out)


__all__ = ["FeishuChannel", "PERMANENT_WS_ERROR_CODES"]
