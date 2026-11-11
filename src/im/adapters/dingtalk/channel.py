"""钉钉 Channel 实现（Issue #936 P2，设计文档 §3.1 / §4.4 / §7.6）。

生命周期纪律（继承 feishu 适配器 A3/A4/A6 同构语义）：
  - **ACK 先行 3s**：钉钉 Stream 每帧有 3s 级 ACK 时限——收到消息帧先回
    `{"code":200,...}`（解析失败也 ack，防重投死循环），再 fire-and-forget 分发；
  - SYSTEM 心跳帧：立即回 code=200（保活），不分发 handler；
  - seen 去重（A3）：内存 set + 持久化钩子（同 app 重投只处理一次）；
  - fire-and-forget + semaphore 限流（A4）：单 bot 慢消息不拖垮进程；
  - 链路断开 → ChannelError（Supervisor 退避重连）；
    凭证永久失败 → PermanentChannelError（A5 短路置 revoked）；
  - lease 复查失配 → 优雅让渡（问题 A）。

不硬依赖 dingtalk-stream SDK：endpoint_fetcher / ws_connect / lease_checker /
seen 钩子全部可替身，测试不触网；SDK 缺失时 endpoint 报 ChannelError（装好即恢复）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable

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
from .inbound import (CARD_CALLBACK_TOPIC, build_ack_frame, card_callback_to_inbound,
                      event_to_inbound, out_track_id_of)
from .outbound import DingtalkSender

log = logging.getLogger(__name__)

ACK_DEADLINE_SECS = 3.0        # 钉钉 Stream ACK 时限（先 ACK 再处理）
SEEN_MAX = 2000                # A3：内存去重集上限（与飞书同款）
DINGTALK_OPENAPI = "https://api.dingtalk.com"
BOT_CALLBACK_TOPIC = "/v1.0/im/bot/messages/get"
OPEN_TIMEOUT = 10.0

EndpointFetcher = Callable[[str, str], Awaitable[tuple]]
LeaseChecker = Callable[[], bool]


def _local_ip() -> str:
    """本机内网 IP（钉钉 open connection 的 localIp 字段，可空）。"""
    try:
        import socket as _socket

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        return ""


def _card_switch_on() -> bool:
    """互动卡片总开关（转发到 cards.card_mode_enabled，测试可 monkeypatch 本函数）。"""
    from .cards import card_mode_enabled
    return card_mode_enabled()


def _subscriptions() -> list[dict]:
    """Stream 订阅清单：机器人消息 + 互动卡片回调 + EVENT 全量兜底。

    批 5：卡片按钮回调走同一个 WS（STREAM 模式），必须显式订阅
    `/v1.0/card/instances/callback`，否则建实例时 callbackType=STREAM 也收不到。
    """
    return [
        {"type": "CALLBACK", "topic": BOT_CALLBACK_TOPIC},
        *([{"type": "CALLBACK", "topic": CARD_CALLBACK_TOPIC}]
          if _card_switch_on() else []),
        {"type": "EVENT", "topic": "*"},
    ]


async def _default_endpoint(app_id: str, app_secret: str) -> tuple:
    """真实取址（Issue #936 P3）：POST /v1.0/gateway/connections/open。

    钉钉 Stream 的入口就是这一个 HTTP 接口：带 clientId/clientSecret + 订阅
    机器人消息回调 topic（CALLBACK），换回 {endpoint, ticket}；再拼成
    `{endpoint}?ticket=...` 的 WebSocket 地址交给上层 WS 循环。

    凭证错误不抛异常，返回 err_code 交由 connect 判定永久失败（A5 短路）；
    网络/5xx → ChannelError（可重试，Supervisor 退避重连）。
    """
    import httpx
    from urllib.parse import quote_plus

    body = {
        "clientId": app_id,
        "clientSecret": app_secret,
        "subscriptions": _subscriptions(),
        # ⚠️ ua 结尾必须是 "-union"：钉钉按该后缀分配联合接入点
        #   wss-open-connection-union.dingtalk.com；不带后缀会返回普通接入点，
        #   握手成功但服务端不下发任何数据（2026-11-04 实测踩坑）
        "ua": "grape-im-engine/1.0-union",
        "localIp": _local_ip(),
    }
    try:
        async with httpx.AsyncClient(timeout=OPEN_TIMEOUT) as client:
            resp = await client.post(
                f"{DINGTALK_OPENAPI}/v1.0/gateway/connections/open", json=body)
    except Exception as e:
        raise ChannelError(f"dingtalk open connection failed (app={app_id[:12]}): {e!r}") from e
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code != 200:
        code = data.get("code") or data.get("errcode") or str(resp.status_code)
        return "", str(code)
    endpoint = str(data.get("endpoint") or "")
    ticket = str(data.get("ticket") or "")
    if not endpoint or not ticket:
        return "", str(data.get("code") or "no_endpoint")
    return f"{endpoint}?ticket={quote_plus(ticket)}", ""


def _is_permanent_credentials_error(code: str) -> bool:
    """凭证类永久失败判定（A5）：invalidAppKey / 401 / 403 / appNotExist 等，其余可重试。"""
    c = str(code or "").strip().lower()
    if not c:
        return False
    if c in ("401", "403"):
        return True
    if "invalid" in c and any(k in c for k in ("app", "secret", "key", "client")):
        return True
    return "notexist" in c or "not_exist" in c


def _frames_debug() -> bool:
    """帧级调试开关（GRAPE_IM_DEBUG_FRAMES=1）：只打一行 headers/type，便于查收件黑洞。"""
    return os.environ.get("GRAPE_IM_DEBUG_FRAMES", "0") == "1"


def _trim_seen(seen: dict, max_size: int) -> None:
    """FIFO 裁剪：按插入序淘汰最旧 msgId，保留最新 max_size//2 条。

    替代 set 迭代序的随机裁剪（裁掉近期 ID 会导致重复处理）。
    """
    keep = max_size // 2
    overflow = len(seen) - keep
    if overflow > 0:
        for key in list(seen)[:overflow]:
            del seen[key]


class DingtalkChannel(Channel):
    """钉钉 Stream 长连接渠道。"""

    def __init__(
        self,
        cfg: ChannelConfig,
        sender: DingtalkSender,
        *,
        endpoint_fetcher: EndpointFetcher | None = None,
        ws_connect: Callable | None = None,
        lease_checker: LeaseChecker | None = None,
        seen_loader: Callable | None = None,
        seen_persist: Callable | None = None,
        seen_cleanup: Callable | None = None,
        max_concurrent: int = 20,
    ) -> None:
        self._cfg = cfg
        self._sender = sender
        self._endpoint = endpoint_fetcher
        self._ws_connect = ws_connect
        self._lease_checker = lease_checker
        self._seen_loader = seen_loader or (lambda app: set())
        self._seen_persist = seen_persist or (lambda app, mid: None)
        self._seen_cleanup = seen_cleanup or (lambda app: None)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._handler_tasks: set = set()
        self.acked_frames: list = []       # 测试观察哨：ACK 先行证据
        self.messages_handled: list = []   # 测试观察哨

    def type(self) -> Type:
        return Type.DINGTALK

    def capabilities(self) -> Capability:
        return self._sender.capabilities()

    async def connect(self, ctx: CancelContext) -> None:
        """阻塞运行接收循环直到 ctx 取消/链路断开（契约同 Channel.connect）。"""
        app_id = str(self._cfg.raw.get("app_id", ""))
        app_secret = str(self._cfg.raw.get("app_secret", ""))
        if self._endpoint is None:
            self._endpoint = _default_endpoint
        if self._ws_connect is None:
            import websockets
            self._ws_connect = websockets.connect

        if not ctx.cancelled and self._lease_checker is not None:
            # 问题 A：连接前复查 lease（token 失配 → 优雅让渡）
            try:
                if not self._lease_checker():
                    log.info("[dingtalk:%s] lease lost, graceful handover", app_id)
                    return
            except Exception as e:
                log.warning("[dingtalk:%s] lease check error: %s", app_id, e)

        seen: dict = {mid: None for mid in self._seen_loader(app_id)}  # 插入序 FIFO
        try:
            ws_url, err_code = await self._endpoint(app_id, app_secret)
        except ChannelError:
            raise
        except Exception as e:
            raise ChannelError(f"ws endpoint fetch failed: {e!r}") from e
        if not ws_url:
            if _is_permanent_credentials_error(err_code):
                raise PermanentChannelError(
                    f"permanent dingtalk failure: code={err_code}, app disabled")
            raise ChannelError(f"no ws endpoint: code={err_code}")

        # ping 保活必须开：钉钉服务端空闲会主动断流（ping_interval=None 实测每
        # 20-30s 断一次，表现为 channels 恒为 1 但 reconnects 持续增长）
        async with self._ws_connect(ws_url, ping_interval=20, ping_timeout=20, close_timeout=5) as ws:
            async for raw in ws:
                if ctx.cancelled:
                    break
                payload: dict = {}
                try:
                    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                    payload = json.loads(text)
                except Exception:
                    payload = {}
                if not payload:
                    continue

                headers = payload.get("headers") or {}
                event_type = str(headers.get("eventType", "") or "")
                frame_type = str(payload.get("type", "") or "")
                topic = str(headers.get("topic", "") or "")
                if _frames_debug():
                    log.info("[dingtalk] frame type=%s eventType=%s topic=%s data_type=%s",
                             frame_type, event_type, topic,
                             type(payload.get("data")).__name__)
                if event_type == "SYSTEM" or frame_type == "SYSTEM" or topic in ("ping", "disconnect"):
                    # 系统推送（ping/disconnect）：立即回 code=200（不消耗 ACK 时限）
                    await self._safe_send(ws, build_ack_frame(payload))
                    continue

                # ⚠️ ACK 先行 3s：解析失败也 ack（防重投死循环），随后才处理
                await self._safe_send(ws, build_ack_frame(payload))
                self.acked_frames.append(str(headers.get("messageId", "")))

                if topic == CARD_CALLBACK_TOPIC:
                    # 批 5：互动卡片按钮回调 → 翻译成"用户发了 /bind N"（chat 由 params 带回）
                    msg = card_callback_to_inbound(payload)
                    if msg is None:
                        # 模板按钮 params 是固定值（带不了 chat）→ 用 outTrackId 反查台账。
                        # 放线程里查（内存命中为主，未命中走一次索引查询），绝不阻塞事件循环。
                        tid = out_track_id_of(payload)
                        if tid:
                            from ...engine.outbound_cards import lookup_chat
                            chat = await asyncio.to_thread(lookup_chat, tid)
                            if chat:
                                msg = card_callback_to_inbound(payload, chat_resolver=lambda _t: chat)
                    if msg is None:
                        continue
                else:
                    msg = event_to_inbound(payload)
                    if msg is None:
                        continue
                    # Issue #1008 回复目标复用：登记 sessionWebhook（按 conversationId），
                    # 出站按 BindingKey 复用——不依赖 msgId 形态，空路由回复不再静默丢弃。
                    # 同时登记 staffId/conversationType：webhook 过期后回落机器人 API 需要它们。
                    _data = payload.get("data") or {}
                    if isinstance(_data, str):
                        # 钉钉 Stream 的 data 是 JSON 字符串（官方协议 §通用字段说明），
                        # 这里也必须按字符串解析，否则 _data.get(...) 直接 AttributeError，
                        # 整条 connect 任务被打死（2026-11-05 故障根因）。
                        try:
                            _data = json.loads(_data) if _data.strip() else {}
                        except Exception:
                            _data = {}
                    if not isinstance(_data, dict):
                        _data = {}
                    _wh = str(_data.get("sessionWebhook") or "")
                    from .outbound import default_webhook_registry
                    default_webhook_registry().register(
                        msg.source.chat_id, _wh,
                        staff_id=str(_data.get("senderStaffId") or ""),
                        chat_type=str(_data.get("conversationType") or ""))
                if msg.message_id:
                    if msg.message_id in seen:
                        continue
                    seen[msg.message_id] = None
                    self._seen_persist(app_id, msg.message_id)
                    if len(seen) > SEEN_MAX:
                        _trim_seen(seen, SEEN_MAX)

                t = asyncio.get_running_loop().create_task(
                    self._safe_handle(ctx, msg))
                self._handler_tasks.add(t)
                t.add_done_callback(self._handler_tasks.discard)

        if ctx.cancelled:
            return
        self._seen_cleanup(app_id)
        raise ChannelError(f"ws closed: {app_id}")

    async def _safe_send(self, ws, frame: dict) -> None:
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:
            log.warning("[dingtalk] send ack failed: %s", e)

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
                log.warning("[dingtalk] inbound handler crashed: %r", e)
                return
            if exc is not None:
                log.warning("[dingtalk] inbound handler infra error: %r", exc)

    async def disconnect(self) -> None:
        for t in list(self._handler_tasks):
            t.cancel()
        self._handler_tasks.clear()

    async def send(self, out: OutboundMessage) -> SendResult:
        return await self._sender.send(out)


__all__ = ["ACK_DEADLINE_SECS", "DingtalkChannel"]
