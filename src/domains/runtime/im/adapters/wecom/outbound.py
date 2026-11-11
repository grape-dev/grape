"""企微出站 —— OutboundMessage → 长连接发送（Issue #1254 批 1/2/3）。

三条硬约束（官方文档 /document/path/101463，2026-10 版）：

1. **`aibot_send_msg` 只接受 markdown / template_card**（不接受 text、不接受裸文件）。
   照飞书/钉钉习惯发 text 会被平台直接拒帧 → 本层统一用 markdown 承载，
   并把「越过回复窗口的主动推送」固定为 markdown；单测直接断言 payload（硬约束）。
2. **回复通道 `aibot_respond_msg` 必须透传回调的 req_id**，且同一 req_id 的回复
   串行发送；官方口径是「收到消息回调后 24 小时内可回复」。
   （设计稿 §3.2 记的「约 5s」实为**欢迎语 / 卡片更新**的时限；回复窗口按官方 24h。
   本层默认 `REPLY_WINDOW_SECS=24h`，窗口外自动切 `aibot_send_msg`，
   窗口与时钟均可注入，测试用注入时钟覆盖两条路径。）
3. **媒体出站必须先换 media_id**：`aibot_upload_media_init/chunk/finish` 三步分片上传，
   单个分片 ≤512KB（base64 前）、≤100 片、会话 30 分钟有效、media_id 3 天有效。

失败语义（继承 #1008）：任何传输/错误码失败都返回 SendResult(ok=False, error=...)，
由 CountingSender 计 failed —— 不静默计成功。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from ...channel.capability import Capability
from ...channel.message import OutboundMessage, SendResult
from .cards import (
    CardLedger,
    build_template_card,
    default_card_ledger,
    template_card_enabled,
)
from .inbound import (
    CMD_RESPOND_MSG,
    CMD_SEND_MSG,
    CMD_UPLOAD_MEDIA_CHUNK,
    CMD_UPLOAD_MEDIA_FINISH,
    CMD_UPLOAD_MEDIA_INIT,
)
from .markdown import WECOM_MARKDOWN_LIMIT, render_markdown, split_markdown

log = logging.getLogger(__name__)

# 能力位申报（声明不协商）：不申报 REACTION / QUOTE_REPLY / THREAD_REPLY /
# MESSAGE_EDIT（v1 不做流式与原地更新）；CARD_ACTION 有真回调（template_card_event）
WECOM_CAPABILITIES = (Capability.TEXT | Capability.RICH_CARD | Capability.CARD_ACTION
                     | Capability.ATTACHMENT)

REPLY_WINDOW_SECS = 24 * 3600.0   # 官方：收到消息回调后 24h 内可回复
CHAT_DIR_TTL = 7 * 24 * 3600.0    # 会话类型台账（主动推送需要 chat_type）
REQUEST_TIMEOUT = 10.0
CHUNK_BYTES = 512 * 1024          # 上传分片上限（base64 编码前）
MAX_CHUNKS = 100
MEDIA_LIMITS = {"file": 20 * 1024 * 1024, "image": 10 * 1024 * 1024,
                "video": 10 * 1024 * 1024, "voice": 2 * 1024 * 1024}


def _redacted_frame(frame: dict) -> dict:
    """帧的记录副本：抹掉 body.secret（观察哨/日志永不出现明文密钥）。"""
    body = frame.get("body")
    if isinstance(body, dict) and body.get("secret"):
        copy = dict(frame)
        copy["body"] = {**body, "secret": "***"}
        return copy
    return frame


class WecomTransportError(RuntimeError):
    """长连接不可用 / 发送失败（非静默错误）。"""


class WecomTransport:
    """长连接帧传输层：req_id 信封 + ack 兑现 + 发送串行化。

    channel 建连成功后 `attach(ws)`；读循环把每个 ack 帧交给 `resolve()`；
    outbound/上传通过 `request()`（等 ack）或 `notify()`（不等 ack）发帧。
    """

    def __init__(self, bot_id: str = "", *, clock: Callable[[], float] = time.monotonic) -> None:
        self.bot_id = str(bot_id or "")
        self._ws: Any = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._clock = clock
        self.sent_frames: list[dict] = []   # 测试观察哨：真实发出的帧
        self.acks_missed = 0

    # ---------------- 生命周期

    def attach(self, ws: Any) -> None:
        self._ws = ws

    def detach(self, ws: Any = None) -> None:
        if ws is not None and ws is not self._ws:
            return
        self._ws = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(WecomTransportError("ws detached"))
        self._pending.clear()

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # ---------------- req_id / 帧

    def next_req_id(self, prefix: str = "grape") -> str:
        self._seq += 1
        return f"{prefix}-{self.bot_id[:8]}-{int(self._clock() * 1000)}-{self._seq}"

    def resolve(self, payload: dict) -> bool:
        """ack 帧 → 兑现等待中的 future（返回 True 表示已消费）。"""
        headers = payload.get("headers")
        req_id = ""
        if isinstance(headers, dict):
            req_id = str(headers.get("req_id") or "")
        if not req_id:
            return False
        fut = self._pending.pop(req_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        return True

    async def send_frame(self, frame: dict) -> None:
        """发一帧原始 WS 消息（发送串行化；未连接即显式失败）。"""
        ws = self._ws
        if ws is None:
            raise WecomTransportError("wecom long connection is not established")
        async with self._lock:
            await ws.send(json.dumps(frame, ensure_ascii=False))
        # 观察哨不落明文 secret：订阅帧 body 里的 secret 一律抹掉（token/密钥不入内存副本）
        self.sent_frames.append(_redacted_frame(frame))
        if len(self.sent_frames) > 500:
            del self.sent_frames[:250]

    # 兼容旧名（内部既有调用点）
    _send_frame = send_frame

    async def notify(self, cmd: str, body: dict | None = None) -> str:
        """发帧不等 ack（回复/推送帧平台必然回 ack，但调用方无需等待）。"""
        req_id = self.next_req_id(cmd.split("_")[-1])
        frame: dict[str, Any] = {"cmd": cmd, "headers": {"req_id": req_id}}
        if body is not None:
            frame["body"] = body
        await self._send_frame(frame)
        return req_id

    async def request(self, cmd: str, body: dict | None = None,
                      timeout: float = REQUEST_TIMEOUT) -> dict:
        """发帧并等 ack（订阅/上传/发送均用）；超时 → WecomTransportError。"""
        req_id = self.next_req_id(cmd.split("_")[-1])
        frame: dict[str, Any] = {"cmd": cmd, "headers": {"req_id": req_id}}
        if body is not None:
            frame["body"] = body
        return await self.request_frame(frame, timeout=timeout)

    async def request_frame(self, frame: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
        """按给定帧（自带 req_id）发出去并等对应 ack。"""
        headers = frame.get("headers")
        req_id = str((headers or {}).get("req_id") or "")
        if not req_id:
            raise WecomTransportError("frame without headers.req_id")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._send_frame(frame)
        except Exception:
            self._pending.pop(req_id, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(req_id, None)
            self.acks_missed += 1
            raise WecomTransportError(
                f"{frame.get('cmd', '?')} ack timeout after {timeout}s") from e
        finally:
            self._pending.pop(req_id, None)


class WecomReplyRegistry:
    """chat_id → (req_id 回复令牌, chat_type)。

    req_id 只在回复窗口内可用（窗口外切主动推送）；chat_type 长留存，
    主动推送 `aibot_send_msg` 必须带（1 单聊 / 2 群聊）。
    """

    def __init__(self, window: float = REPLY_WINDOW_SECS, chat_ttl: float = CHAT_DIR_TTL,
                 max_entries: int = 8192, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window
        self._chat_ttl = chat_ttl
        self._max = max(1, int(max_entries))
        self._clock = clock
        self._map: dict[str, dict] = {}

    def register(self, chat_id: str, req_id: str = "", chat_type: int = 1) -> None:
        if not chat_id:
            return
        now = self._clock()
        expired = [k for k, v in self._map.items()
                   if now - v["at"] > max(self._window, self._chat_ttl)]
        for k in expired:
            self._map.pop(k, None)
        if chat_id not in self._map and len(self._map) >= self._max:
            oldest = min(self._map, key=lambda k: self._map[k]["at"])
            self._map.pop(oldest, None)
        prev = self._map.get(chat_id) or {}
        self._map[chat_id] = {
            "req_id": req_id or prev.get("req_id", ""),
            "req_at": now if req_id else prev.get("req_at", 0.0),
            "chat_type": int(chat_type) if chat_type else int(prev.get("chat_type", 1) or 1),
            "at": now,
        }

    def push_only(self, chat_id: str, chat_type: int = 1) -> None:
        """把会话标记为"只能主动推送"：清掉回复令牌，保留 chat_type。

        卡片事件（template_card_event）的 req_id **不能**当回复令牌用 ——
        企微只对消息回调开回复窗口，拿事件 req_id 发 `aibot_respond_msg`
        会 ack 成功但消息不显示（2026-11-06 实障：点按钮"已提交"却收不到回执）。
        """
        if not chat_id:
            return
        self.register(chat_id, "", chat_type)
        entry = self._map.get(chat_id)
        if entry is not None:
            entry["req_id"] = ""
            entry["req_at"] = 0.0

    def reply_req_id(self, chat_id: str) -> str:
        entry = self._map.get(chat_id or "")
        if not entry:
            return ""
        if not entry.get("req_id"):
            return ""
        if self._clock() - float(entry.get("req_at") or 0.0) > self._window:
            return ""
        return str(entry["req_id"])

    def chat_type(self, chat_id: str) -> int:
        entry = self._map.get(chat_id or "")
        if not entry:
            return 1
        if self._clock() - float(entry.get("at") or 0.0) > self._chat_ttl:
            self._map.pop(chat_id, None)
            return 1
        return int(entry.get("chat_type") or 1)

    def __len__(self) -> int:
        return len(self._map)


_default_reply_registry = WecomReplyRegistry()


def default_reply_registry() -> WecomReplyRegistry:
    return _default_reply_registry


# ---------------------------------------------------------------- 上传

async def upload_media(transport: WecomTransport, data: bytes, filename: str,
                       media_type: str = "file", *, timeout: float = 30.0) -> str:
    """三步分片上传 → media_id（失败抛 WecomTransportError，不返回空串假装成功）。"""
    if not data:
        raise WecomTransportError("empty media payload")
    limit = MEDIA_LIMITS.get(media_type)
    if limit and len(data) > limit:
        raise WecomTransportError(
            f"media too large for {media_type}: {len(data)} > {limit} bytes")
    chunks = [data[i:i + CHUNK_BYTES] for i in range(0, len(data), CHUNK_BYTES)] or [b""]
    if len(chunks) > MAX_CHUNKS:
        raise WecomTransportError(f"too many chunks: {len(chunks)} > {MAX_CHUNKS}")

    init_ack = await transport.request(CMD_UPLOAD_MEDIA_INIT, {
        "type": media_type,
        "filename": (filename or "file.bin")[:256],
        "total_size": len(data),
        "total_chunks": len(chunks),
        "md5": hashlib.md5(data).hexdigest(),
    }, timeout=timeout)
    _check_ack(init_ack, CMD_UPLOAD_MEDIA_INIT)
    upload_id = str((init_ack.get("body") or {}).get("upload_id") or "")
    if not upload_id:
        raise WecomTransportError("upload init returned no upload_id")

    for index, chunk in enumerate(chunks):
        ack = await transport.request(CMD_UPLOAD_MEDIA_CHUNK, {
            "upload_id": upload_id,
            "chunk_index": index,
            "base64_data": base64.b64encode(chunk).decode("ascii"),
        }, timeout=timeout)
        _check_ack(ack, CMD_UPLOAD_MEDIA_CHUNK)

    finish_ack = await transport.request(CMD_UPLOAD_MEDIA_FINISH,
                                        {"upload_id": upload_id}, timeout=timeout)
    _check_ack(finish_ack, CMD_UPLOAD_MEDIA_FINISH)
    media_id = str((finish_ack.get("body") or {}).get("media_id") or "")
    if not media_id:
        raise WecomTransportError("upload finish returned no media_id")
    return media_id


def _check_ack(ack: dict, cmd: str) -> None:
    errcode = int(ack.get("errcode", -1) if ack.get("errcode") is not None else -1)
    if errcode != 0:
        raise WecomTransportError(
            f"{cmd} failed: errcode={errcode} errmsg={ack.get('errmsg', '')}")


# ---------------------------------------------------------------- Sender

class WecomSender:
    """出站发送器：markdown 承载 + respond/send 分流 + 卡片/媒体。

    transport 与 reply registry 均可注入（测试用假 WS，不触网）。
    """

    def __init__(self, transport: WecomTransport,
                 registry: WecomReplyRegistry | None = None, *,
                 ledger: CardLedger | None = None,
                 reply_window: float = REPLY_WINDOW_SECS,
                 clock: Callable[[], float] = time.monotonic,
                 timeout: float = REQUEST_TIMEOUT) -> None:
        self._transport = transport
        # 注意：registry/ledger 都实现了 __len__，空实例是 falsy —— 必须按 None 判定，
        # 否则「刚建好还没登记任何会话」的实例会被悄悄换成全局单例（跨安装串号）。
        self._registry = registry if registry is not None else default_reply_registry()
        self._ledger = ledger if ledger is not None else default_card_ledger()
        self._window = reply_window
        self._clock = clock
        self._timeout = timeout

    def capabilities(self) -> Capability:
        return WECOM_CAPABILITIES

    def chat_type_of(self, chat_id: str) -> int:
        """会话类型（1 单聊 / 2 群）：入站登记优先，未登记按单聊兜底（企微 send_msg 只认 1/2）。"""
        try:
            value = int(self._registry.chat_type(chat_id) or 1)
        except Exception:
            value = 1
        return value if value in (1, 2) else 1

    # ---------------- 帧构造

    def _route(self, out: OutboundMessage) -> tuple[str, int]:
        chat_id = out.chat_id or ""
        chat_type = int((out.metadata or {}).get("chat_type") or 0) \
            or self._registry.chat_type(chat_id)
        return chat_id, chat_type

    def _reply_req_id(self, out: OutboundMessage) -> str:
        """回复令牌：显式 req_id 优先，其次按 chat_id 查回复窗口内的回调 req_id。"""
        md = out.metadata or {}
        explicit = str(md.get("req_id") or "")
        if explicit:
            return explicit
        chat_id, _ = self._route(out)
        return self._registry.reply_req_id(chat_id)

    async def _respond(self, req_id: str, body: dict) -> str:
        """回复帧：req_id 必须透传回调里的那一个（不能新生成）。"""
        frame = {"cmd": CMD_RESPOND_MSG, "headers": {"req_id": req_id}, "body": body}
        await self._transport.send_frame(frame)
        return req_id

    async def _send_card(self, out: OutboundMessage, payload: dict, req_id: str) -> str:
        """发送模板卡片：payload 为 {"msgtype": "template_card", "template_card": {...}}。"""
        chat_id, chat_type = self._route(out)
        template = payload.get("template_card") or {}
        task_id = str(template.get("task_id") or "")
        if task_id:
            self._ledger.register(task_id, chat_id, chat_type)
        body: dict[str, Any] = {"msgtype": "template_card", "template_card": template}
        if req_id:
            return await self._respond(req_id, body)
        if not chat_id:
            raise WecomTransportError("no route: chat_id empty")
        body.update({"chatid": chat_id, "chat_type": chat_type})
        ack = await self._transport.request(CMD_SEND_MSG, body, timeout=self._timeout)
        _check_ack(ack, CMD_SEND_MSG)
        return str((ack.get("headers") or {}).get("req_id") or "")

    async def _send_attachment(self, out: OutboundMessage, item: dict,
                               req_id: str) -> str:
        data = item.get("data")
        if data is None and item.get("path"):
            with open(str(item["path"]), "rb") as f:
                data = f.read()
        data = bytes(data or b"")
        media_type = str(item.get("media_type") or item.get("type") or "file")
        if media_type not in MEDIA_LIMITS:
            media_type = "file"
        filename = str(item.get("filename") or item.get("name") or "file.bin")
        media_id = await upload_media(self._transport, data, filename, media_type)
        node = {"media_id": media_id}
        if media_type == "file":
            body: dict[str, Any] = {"msgtype": "file", "file": node}
        elif media_type == "image":
            body = {"msgtype": "image", "image": node}
        elif media_type == "voice":
            body = {"msgtype": "voice", "voice": node}
        else:
            body = {"msgtype": "video",
                    "video": {**node, "title": filename[:64], "description": ""}}
        if req_id:
            return await self._respond(req_id, body)
        # 主动推送只支持 markdown / template_card → 文件必须走回复通道
        raise WecomTransportError(
            "aibot_send_msg cannot carry media (markdown/template_card only); "
            "attachment requires an open reply window")

    # ---------------- 主入口

    async def send(self, out: OutboundMessage) -> SendResult:
        try:
            md = out.metadata or {}
            raw_card = md.get("raw_card")
            attachments = md.get("attachments") or md.get("files") or []
            text = out.text or ""
            if not (text or "").strip() and not raw_card and not attachments:
                # 空内容硬护栏（与 card_empty_guard 同口径）：发空即 failed，不静默 ok
                return SendResult(ok=False, error="empty content guard")

            req_id = self._reply_req_id(out)
            last_id = ""

            if isinstance(raw_card, dict):
                if raw_card.get("header") or raw_card.get("elements"):
                    # 平台格式泄漏护栏：lark JSON 不允许透传给企微
                    return SendResult(ok=False, error="lark raw_card refused on wecom")
                template = raw_card.get("template_card")
                if template_card_enabled() and template:
                    last_id = await self._send_card(out, raw_card, req_id)
                else:
                    fallback = str(((raw_card.get("markdown") or {}).get("text"))
                                   or raw_card.get("text") or "")
                    if not fallback.strip():
                        return SendResult(ok=False, error="empty card fallback content")
                    last_id = await self._send_markdown(out, fallback, req_id)

            if attachments:
                for item in attachments:
                    if not isinstance(item, dict):
                        continue
                    last_id = await self._send_attachment(out, item, req_id) or last_id

            if (text or "").strip():
                last_id = await self._send_markdown(out, text, req_id) or last_id

            return SendResult(ok=True, message_id=last_id)
        except WecomTransportError as e:
            log.warning("[wecom] send failed: %s", e)
            return SendResult(ok=False, error=str(e))
        except Exception as e:
            log.warning("[wecom] send crashed: %r", e)
            return SendResult(ok=False, error=str(e))

    @staticmethod
    def _prefixed_chunks(rendered: str) -> list[str]:
        """按「正文 + (i/N) 前缀」总字节 ≤20480 分块（前缀必须计入预算）。

        先按整限切，再用「限 - 最大前缀字节」重切；块数变化会改变前缀宽度，
        迭代到所有实际带前缀块都合法（最多 8 轮，宽度只会增不会减，必然收敛）。
        """
        limit = WECOM_MARKDOWN_LIMIT
        chunks = split_markdown(rendered, limit)
        if len(chunks) <= 1:
            return chunks
        for _ in range(8):
            n = len(chunks)
            reserve = max(len(f"({i + 1}/{n})\n".encode("utf-8"))
                          for i in range(n))
            budget = limit - reserve
            if budget <= 0:
                break
            chunks = split_markdown(rendered, budget)
            n2 = len(chunks)
            if all(len(f"({i + 1}/{n2})\n{c}".encode("utf-8")) <= limit
                   for i, c in enumerate(chunks)):
                return chunks
        return chunks

    async def _send_markdown(self, out: OutboundMessage, text: str, req_id: str) -> str:
        chunks = self._prefixed_chunks(render_markdown(text))
        last_id = ""
        for i, chunk in enumerate(chunks):
            body_text = chunk
            if len(chunks) > 1:
                body_text = f"({i + 1}/{len(chunks)})\n{chunk}"
            last_id = await self._send_markdown_chunk_body(out, body_text, req_id)
        return last_id

    async def _send_markdown_chunk_body(self, out: OutboundMessage, content: str,
                                        req_id: str) -> str:
        body = {"msgtype": "markdown", "markdown": {"content": content}}
        if req_id:
            return await self._respond(req_id, body)
        chat_id, chat_type = self._route(out)
        if not chat_id:
            raise WecomTransportError("no route: chat_id empty")
        ack = await self._transport.request(CMD_SEND_MSG, {
            **body, "chatid": chat_id, "chat_type": chat_type,
        }, timeout=self._timeout)
        _check_ack(ack, CMD_SEND_MSG)
        return str((ack.get("headers") or {}).get("req_id") or "")


# ---------------------------------------------------------------- handler 侧 api

class WecomAdapterApi:
    """装配层 `_api_overrides` 的企微 api（媒体两跳 + 降级空实现）。

    媒体入站已在收帧时下载落盘（URL 5 分钟有效），handler 拿到的 image_key /
    file_key 就是缓存键，这里按 key 取回字节 —— 不再打网络。
    """

    native_interactive_card = True

    def __init__(self, media_cache: Any = None, sender: Any = None) -> None:
        self._cache = media_cache
        # 主动推送（无回复令牌）要用发送器：企微没有飞书的 card-to-chat 通道，
        # aibot_send_msg 只接受 markdown / template_card，因此统一走 markdown。
        # 由装配层 _make_sender 注入（同一安装的 WecomSender）。
        self._sender = sender

    async def send_card_to_chat(self, ctx: Any, chat_id: str, text: str,
                                fallback_open_id: str = "", title: str = "",
                                template: str = "blue", **kw) -> str:
        """legacy handler 的"发卡片到会话"（绑定引导卡 / 未绑定仓库卡）。

        2026-11-06 实障：缺这个方法时引擎记 `deliver failed: ... has no attribute
        'send_card_to_chat'`，用户侧表现为"点了卡片没反应/收不到确认卡"。
        """
        if self._sender is None or not chat_id:
            return ""
        chat_type = kw.get("chat_type") or self._sender.chat_type_of(chat_id)
        out = OutboundMessage(chat_id=chat_id, text=text or "",
                              metadata={"chat_type": int(chat_type)})
        try:
            res = await self._sender.send(out)
        except Exception as e:
            log.warning("[wecom] send_card_to_chat failed chat=%s: %s", chat_id[-12:], e)
            return ""
        return getattr(res, "message_id", "") if getattr(res, "ok", False) else ""

    async def send_text_to_user(self, ctx: Any, open_id: str, text: str,
                                msg_id: str = "", **kw) -> str:
        """私聊定向文本：企微按 userid/chatid 路由，语义等同 send_card_to_chat。"""
        return await self.send_card_to_chat(ctx, open_id or kw.get("chat_id", ""), text, **kw)

    async def download_image(self, ctx: Any, image_key: str, message_id: str = "") -> bytes:
        return self._read(image_key)

    async def download_file(self, ctx: Any, file_key: str, message_id: str = "",
                            file_type: str = "file") -> bytes:
        return self._read(file_key)

    def _read(self, key: str) -> bytes:
        if not key or self._cache is None:
            return b""
        try:
            return bytes(self._cache.get(key) or b"")
        except Exception:
            return b""

    async def get_user_name(self, ctx: Any, user_id: str, user_id_type: str = "user_id") -> str:
        return ""      # 企微加密 userid 需自建应用对接换明文，v1 不做

    async def add_reaction(self, ctx: Any, message_id: str, emoji_type: str = "OK") -> bool:
        return False   # 企微无表情回执能力位（不虚报）

    async def reply_text(self, ctx: Any, message_id: str, text: str) -> str:
        raise NotImplementedError("legacy egress mode is not supported on wecom")

    async def reply_card(self, ctx: Any, message_id: str, text: str, **kw) -> str:
        raise NotImplementedError("legacy egress mode is not supported on wecom")


__all__ = [
    "REPLY_WINDOW_SECS",
    "WECOM_CAPABILITIES",
    "WECOM_MARKDOWN_LIMIT",
    "WecomAdapterApi",
    "WecomReplyRegistry",
    "WecomSender",
    "WecomTransport",
    "WecomTransportError",
    "build_template_card",
    "default_reply_registry",
    "upload_media",
]