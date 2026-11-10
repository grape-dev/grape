"""钉钉出站 —— OutboundMessage → 钉钉发送（Issue #936 P2；#1008 回复目标复用）。

Markdown 为通用语；钉钉 markdown 上限 15000 字节 → split_markdown 分块发送
（围栏闭合，见 markdown.py）。附件两跳：出站引用（storage_key）由平台侧
机器人 API 消费，本层只透传。

Issue #1008 回复目标复用（钉钉适配器声明）：
  - 钉钉 Stream 消息自带 sessionWebhook（企业内部机器人回复入口，约 2h 有效）；
  - 入站翻译时按 conversationId 登记（WebhookRegistry），出站按 BindingKey
    （conversation_id，§5.2）复用最近登记的 webhook 回复——不依赖 msgId 形态，
    从根上消除「chat_id 为空 → 回复静默丢弃」；
  - 传输回调兼容同步/协程两种形态（webhook HTTP POST 为 async）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from ...channel.capability import Capability
from ...channel.message import OutboundMessage, SendResult
from .markdown import DINGTALK_MARKDOWN_LIMIT, split_markdown

log = logging.getLogger(__name__)

# Issue #1095 批4：RICH_CARD 语义收紧为「能渲染 Card IR」——
# DingtalkCardRenderer 就绪后本声明不再谎报（此前无 raw_card 分支会静默发空消息）。
# TODO(#936 P3): 出站附件透传未实现（OutboundMessage 亦无附件字段）——
# 在 send() 真正转发附件之前不得声明 Capability.ATTACHMENT，防止上层误判。
# 无 MESSAGE_EDIT / REACTION（webhook 通道不支持原地更新与表情）→ 出站编辑降级追加。
# CARD_ACTION：钉钉机器人 actionCard 按钮只支持 URL 跳转，动作回调需卡片实例 API（批5）→ 不申报。
DINGTALK_CAPABILITIES = Capability.TEXT | Capability.RICH_CARD

_WEBHOOK_TTL = 90 * 60.0   # sessionWebhook 官方有效期约 2h，保守 90 分钟

# 注册表容量上限（审查建议）：按登记时间淘汰最旧条目，防长运行多会话缓慢增长。
_WEBHOOK_REGISTRY_MAX_ENTRIES = 4096


class DingtalkWebhookError(RuntimeError):
    """钉钉 sessionWebhook 传输/业务失败（Issue #1008 失败语义）。

    非 2xx / errcode!=0 / 网络异常均抛出，由 DingtalkSender 包装为
    SendResult(ok=False) 并经 CountingSender 计 failed——不得静默计成功。
    """


class WebhookRegistry:
    """conversation_id → 最近 sessionWebhook（入站登记、出站复用）。

    同时记 staffId / conversationType：webhook 过期或缺失时，出站可回落机器人
    主动发送 API（群 groupMessages / 单聊 oToMessages）。容量有界，淘汰最旧条目。
    """

    def __init__(self, ttl: float = _WEBHOOK_TTL,
                 max_entries: int = _WEBHOOK_REGISTRY_MAX_ENTRIES) -> None:
        self._ttl = ttl
        self._max_entries = max(1, int(max_entries))
        self._map: dict[str, dict] = {}

    def register(self, conversation_id: str, webhook: str,
                 staff_id: str = "", chat_type: str = "") -> None:
        if not conversation_id:
            return
        now = time.monotonic()
        # 过期清扫：借登记时机剔除已失效条目，控制缓慢增长
        expired = [k for k, v in self._map.items() if now - v["at"] > self._ttl]
        for k in expired:
            self._map.pop(k, None)
        prev = self._map.get(conversation_id) or {}
        if conversation_id not in self._map and len(self._map) >= self._max_entries:
            oldest = min(self._map, key=lambda k: self._map[k]["at"])
            self._map.pop(oldest, None)
        self._map[conversation_id] = {
            "webhook": str(webhook or prev.get("webhook") or ""),
            "staff_id": str(staff_id or prev.get("staff_id") or ""),
            "chat_type": str(chat_type or prev.get("chat_type") or ""),
            "at": now,
        }

    def _entry(self, conversation_id: str) -> dict:
        entry = self._map.get(conversation_id or "")
        if not entry:
            return {}
        if time.monotonic() - entry["at"] > self._ttl:
            self._map.pop(conversation_id, None)
            return {}
        return entry

    def lookup(self, conversation_id: str) -> str:
        return str(self._entry(conversation_id).get("webhook") or "")

    def meta(self, conversation_id: str) -> dict:
        """会话元数据（staff_id / chat_type），供机器人 API 兜底路由。"""
        entry = self._entry(conversation_id)
        if not entry:
            return {}
        return {"staff_id": entry.get("staff_id", ""), "chat_type": entry.get("chat_type", "")}


_default_registry = WebhookRegistry()


def default_webhook_registry() -> WebhookRegistry:
    return _default_registry


async def _post_webhook(webhook: str, payload: dict) -> str:
    """sessionWebhook POST（钉钉机器人回复协议：msgtype text/markdown）。

    失败语义（Issue #1008）：非 2xx / errcode!=0 / 传输异常一律 raise
    DingtalkWebhookError，由 DingtalkSender 转 SendResult(ok=False) 计 failed。
    """
    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if not (200 <= resp.status < 300):
                    body = (await resp.text())[:200]
                    raise DingtalkWebhookError(
                        f"webhook HTTP {resp.status}: {body}")
                data = await resp.json(content_type=None)
    except DingtalkWebhookError:
        raise
    except Exception as e:
        raise DingtalkWebhookError(f"webhook transport error: {e}") from e
    data = data or {}
    errcode = data.get("errcode", 0)
    if errcode not in (0, None):
        raise DingtalkWebhookError(
            f"dingtalk errcode={errcode}: {data.get('errmsg', '')}")
    return str(data.get("messageId", "") or "")


async def send_text_via_webhook(conversation_id: str, text: str,
                                registry: WebhookRegistry | None = None) -> str:
    reg = registry or _default_registry
    webhook = reg.lookup(conversation_id)
    if not webhook:
        # 显式失败（Issue #1008 失败语义）：空路由不得静默成功
        raise LookupError(f"no sessionWebhook registered for conversation {conversation_id!r}")
    return await _post_webhook(webhook, {"msgtype": "text", "text": {"content": text or ""}})


async def send_markdown_via_webhook(conversation_id: str, text: str,
                                    registry: WebhookRegistry | None = None,
                                    chunk_marker: str = "") -> str:
    reg = registry or _default_registry
    webhook = reg.lookup(conversation_id)
    if not webhook:
        raise LookupError(f"no sessionWebhook registered for conversation {conversation_id!r}")
    title = (chunk_marker or "").strip("()") or "grape"
    return await _post_webhook(
        webhook, {"msgtype": "markdown", "markdown": {"title": title, "text": text or ""}})


async def send_action_card_via_webhook(conversation_id: str, title: str, text: str,
                                       btns: list[dict] | None = None,
                                       registry: WebhookRegistry | None = None) -> str:
    """actionCard 发送（Issue #1095 批4：Card IR → 钉钉卡片真实渲染）。

    btns：[{"title": ..., "actionURL": ...}]（钉钉 actionCard 按钮仅支持 URL 跳转）；
    空文本 + 空按钮 → 显式失败（空消息硬护栏，不再静默 ok=True）。
    """
    reg = registry or _default_registry
    webhook = reg.lookup(conversation_id)
    if not webhook:
        raise LookupError(f"no sessionWebhook registered for conversation {conversation_id!r}")
    card: dict = {"title": title or "grape", "text": text or ""}
    if btns:
        card["btns"] = btns
        card["btnOrientation"] = "0"
    if not str(card["text"]).strip() and not btns:
        raise DingtalkWebhookError("empty actionCard content refused (card_empty_guard)")
    return await _post_webhook(webhook, {"msgtype": "actionCard", "actionCard": card})


async def send_action_card_with_fallback(conversation_id: str, title: str, text: str,
                                          btns: list[dict] | None = None, *,
                                          robot: "DingtalkRobotApi | None" = None,
                                          registry: WebhookRegistry | None = None) -> str:
    """actionCard 发送：webhook 优先，缺失/过期回落机器人 API（markdown 承载内容）。

    Issue #1095 复审 Required2：sessionWebhook 失效时卡片不得直接 ok=False，
    按钮 URL 以「标题: URL」文本行保留，确保内容可达用户。
    """
    reg = registry or _default_registry
    card_text = text or ""
    if not str(card_text).strip() and not btns:
        raise DingtalkWebhookError("empty actionCard content refused (card_empty_guard)")
    try:
        return await send_action_card_via_webhook(
            conversation_id, title, card_text, btns=btns, registry=reg)
    except (LookupError, DingtalkWebhookError):
        if robot is None:
            raise
        fallback_text = card_text
        if btns:
            links = "\n".join(
                f"{b.get('title') or ''}: {b.get('actionURL') or ''}".strip(": ")
                for b in btns if b.get("actionURL"))
            if links:
                fallback_text = f"{fallback_text}\n\n{links}".strip()
        return await _fallback_to_robot(conversation_id, fallback_text, robot=robot,
                                        registry=reg, markdown=True, title=title)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


# ---------------------------------------------------------------- 机器人 API 兜底

_DINGTALK_API_BASE = "https://api.dingtalk.com"
_TOKEN_SAFETY = 300.0       # accessToken 提前 5 分钟过期
_API_TIMEOUT = 12.0


class DingtalkRobotApi:
    """机器人主动发送 API：accessToken 缓存 + 群/单聊发送 + 文件下载。

    sessionWebhook 是入站帧带出来的临时回复口（约 2h）；过期或进程重启后缓存
    为空时必须有一条主动通道，否则「能收不能回」。这里按会话类型分流：
      - conversationType=2（群）→ groupMessages/send(openConversationId)
      - 单聊 → oToMessages/batchSend(senderStaffId)
    """

    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = str(app_id or "")
        self._app_secret = str(app_secret or "")
        self._token = ""
        self._token_exp = 0.0

    async def _post(self, url: str, payload: dict, token: str = "") -> tuple[int, dict]:
        import httpx
        headers = {"Content-Type": "application/json"}
        if token:
            headers["x-acs-dingtalk-access-token"] = token
        async with httpx.AsyncClient(timeout=_API_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {}
        return resp.status_code, (body if isinstance(body, dict) else {})

    async def access_token(self) -> str:
        now = time.monotonic()
        if self._token and self._token_exp > now:
            return self._token
        code, body = await self._post(
            f"{_DINGTALK_API_BASE}/v1.0/oauth2/accessToken",
            {"appKey": self._app_id, "appSecret": self._app_secret})
        token = str(body.get("accessToken") or "")
        if code != 200 or not token:
            return ""
        try:
            expire = float(body.get("expireIn") or 7200)
        except Exception:
            expire = 7200.0
        self._token, self._token_exp = token, now + expire - _TOKEN_SAFETY
        return token

    async def send_group(self, conversation_id: str, text: str, *,
                         markdown: bool = True, title: str = "grape") -> str:
        token = await self.access_token()
        if not token or not conversation_id:
            return ""
        key = "sampleMarkdown" if markdown else "sampleText"
        param = ({"title": title or "grape", "text": text} if markdown
                 else {"content": text})
        code, body = await self._post(
            f"{_DINGTALK_API_BASE}/v1.0/robot/groupMessages/send",
            {"robotCode": self._app_id, "openConversationId": conversation_id,
             "msgKey": key, "msgParam": json.dumps(param, ensure_ascii=False)}, token)
        if code in (200, 201):
            return str(body.get("processQueryKey") or body.get("messageId") or "")
        raise DingtalkWebhookError(f"group send failed: HTTP {code} {str(body)[:200]}")

    async def send_p2p(self, staff_id: str, text: str, *,
                       markdown: bool = True, title: str = "grape") -> str:
        token = await self.access_token()
        if not token or not staff_id:
            return ""
        key = "sampleMarkdown" if markdown else "sampleText"
        param = ({"title": title or "grape", "text": text} if markdown
                 else {"content": text})
        code, body = await self._post(
            f"{_DINGTALK_API_BASE}/v1.0/robot/oToMessages/batchSend",
            {"robotCode": self._app_id, "userIds": [staff_id],
             "msgKey": key, "msgParam": json.dumps(param, ensure_ascii=False)}, token)
        if code in (200, 201):
            return str(body.get("processQueryKey") or body.get("messageId") or "")
        raise DingtalkWebhookError(f"p2p send failed: HTTP {code} {str(body)[:200]}")

    async def download(self, download_code: str) -> bytes:
        token = await self.access_token()
        if not token or not download_code:
            return b""
        code, body = await self._post(
            f"{_DINGTALK_API_BASE}/v1.0/robot/messageFiles/download",
            {"downloadCode": download_code, "robotCode": self._app_id}, token)
        url = str(body.get("downloadUrl") or "")
        if code != 200 or not url:
            return b""
        import httpx
        async with httpx.AsyncClient(timeout=_API_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
        return resp.content if resp.status_code == 200 else b""


async def _fallback_to_robot(conversation_id: str, text: str, *, robot: "DingtalkRobotApi",
                             registry: WebhookRegistry, markdown: bool,
                             title: str = "") -> str:
    meta = registry.meta(conversation_id)
    if meta.get("chat_type") == "2":
        return await robot.send_group(conversation_id, text, markdown=markdown, title=title)
    if meta.get("staff_id"):
        return await robot.send_p2p(meta["staff_id"], text, markdown=markdown, title=title)
    raise LookupError(
        f"no route for conversation {conversation_id!r} (webhook/identity missing)")


async def send_text_with_fallback(conversation_id: str, text: str, *,
                                  robot: "DingtalkRobotApi | None" = None,
                                  registry: WebhookRegistry | None = None) -> str:
    """文本发送：webhook 优先，失败/缺失回落机器人 API（失败仍然抛错，不静默）。"""
    reg = registry or _default_registry
    try:
        return await send_text_via_webhook(conversation_id, text, reg)
    except (LookupError, DingtalkWebhookError):
        if robot is None:
            raise
        return await _fallback_to_robot(conversation_id, text, robot=robot,
                                        registry=reg, markdown=False)


async def send_markdown_with_fallback(conversation_id: str, text: str, *,
                                      chunk_marker: str = "",
                                      robot: "DingtalkRobotApi | None" = None,
                                      registry: WebhookRegistry | None = None) -> str:
    """markdown 发送：webhook 优先，失败/缺失回落机器人 API。"""
    reg = registry or _default_registry
    title = (chunk_marker or "").strip("()") or "grape"
    try:
        return await send_markdown_via_webhook(conversation_id, text, reg,
                                               chunk_marker=chunk_marker)
    except (LookupError, DingtalkWebhookError):
        if robot is None:
            raise
        return await _fallback_to_robot(conversation_id, text, robot=robot,
                                        registry=reg, markdown=True, title=title)


class DingtalkAdapterApi:
    """装配层 _ctx_for 的钉钉 api（入站/媒体能力）。

    出站统一走 egress → Replier → DingtalkSender；这里只保留 handler 需要的
    下载与身份读取（媒体两跳），以及与飞书同名的降级空实现（表情/卡片更新）。
    legacy 出站模式（GRAPE_IM_EGRESS_MODE=legacy）在钉钉渠道不支持，走 replier。
    """

    native_interactive_card = False

    def __init__(self, app_id: str, app_secret: str,
                 registry: WebhookRegistry | None = None) -> None:
        self._robot = DingtalkRobotApi(app_id, app_secret)
        self._registry = registry or _default_registry

    async def download_image(self, ctx: Any, image_key: str, message_id: str = "") -> bytes:
        return await self._robot.download(image_key)

    async def download_file(self, ctx: Any, file_key: str, message_id: str = "",
                            file_type: str = "file") -> bytes:
        return await self._robot.download(file_key)

    async def get_user_name(self, ctx: Any, user_id: str, user_id_type: str = "user_id") -> str:
        return ""

    async def add_reaction(self, ctx: Any, message_id: str, emoji_type: str = "OK") -> bool:
        return False   # 钉钉 emotion 接口需 openMsgId，与内部 message_id 不同源

    async def reply_text(self, ctx: Any, message_id: str, text: str) -> str:
        raise NotImplementedError("legacy egress mode is not supported on dingtalk")

    async def reply_card(self, ctx: Any, message_id: str, text: str, **kw) -> str:
        raise NotImplementedError("legacy egress mode is not supported on dingtalk")


class DingtalkSender:
    """发送器：注入底层发送函数（send_text / send_markdown），便于测试替身。

    markdown 超限 → 自动 15000 字节分块逐条发送；块序号写进 SendResult.message_id
    （最后一块的平台 message_id）。附件（media_refs）走独立两跳通道，不混入文本块。
    回调兼容同步/协程两种形态（webhook 传输为 async）。
    """

    def __init__(
        self,
        send_text: Callable[..., Any],
        send_markdown: Callable[..., Any],
        send_action_card: Callable[..., Any] | None = None,
        card_mode: str = "action_card",
        card_template_id: str = "",
        card_client: Any = None,
    ) -> None:
        self._send_text = send_text
        self._send_markdown = send_markdown
        self._send_action_card = send_action_card
        # 批 5：interactive = 走互动卡片实例（按钮可回调）→ 才有 CARD_ACTION 能力位。
        # 默认 action_card = 今天的"文字卡 + 回数字"，能力位不虚报。
        self._card_mode = str(card_mode or "action_card").lower()
        self._card_template_id = str(card_template_id or "")
        self._card_client = card_client

    def capabilities(self) -> Capability:
        caps = DINGTALK_CAPABILITIES
        if self._card_mode == "interactive":
            caps |= Capability.CARD_ACTION
        return caps

    async def _send_interactive(self, out: OutboundMessage) -> SendResult | None:
        """批 5：卡片形状的出站 → 互动卡片实例（模板 + createAndDeliver）。

        返回 None 表示"没走通/不适用"，由调用方回落 actionCard / markdown（今天的路径）。
        只有卡片类出站（Card IR 渲染结果 / kind=card 字符串卡）才套模板；纯文本不受影响。
        """
        md = out.metadata or {}
        raw_card = md.get("raw_card")
        if isinstance(raw_card, dict) and raw_card.get("msgtype") == "actionCard":
            ac = raw_card.get("actionCard") or {}
            title = str(ac.get("title") or "DevOps Bot")
            text = str(ac.get("text") or "")
        elif str(md.get("kind") or "") == "card":
            from ...channel.card import hard_breaks
            title = str(md.get("title") or "DevOps Bot")
            text = hard_breaks(out.text or "")
        else:
            return None
        if not (text or "").strip():
            return None

        meta = default_webhook_registry().meta(out.chat_id) or {}
        conv_type = str(meta.get("chat_type") or "1")
        staff_id = str(meta.get("staff_id") or "")
        if conv_type != "2" and not staff_id:
            # 单聊场域必须是 IM_ROBOT.<staffId>；没有就交给回落路径（还能作为 actionCard 发出去）
            log.info("[dingtalk] interactive skip: no staff_id for %s", out.chat_id[:12])
            return None

        from .cards import new_out_track_id
        tid = new_out_track_id(out.chat_id)
        card_data = {"title": title, "body_md": text}
        try:
            ok, data = await self._card_client.create_and_deliver(
                self._card_template_id, tid, card_data,
                conversation_id=out.chat_id, conversation_type=conv_type,
                staff_id=staff_id,
            )
        except Exception as e:                      # 网络/凭证异常也回落，不打断回复
            log.warning("[dingtalk] interactive card error, fallback: %r", e)
            return None
        if not ok:
            log.warning("[dingtalk] interactive card failed, fallback: %r", data)
            return None
        try:
            from ...engine.outbound_cards import remember
            remember(tid, chat_id=out.chat_id, installation_id="", chat_type=conv_type,
                     sender_id=staff_id, template_id=self._card_template_id)
        except Exception as e:
            log.warning("[dingtalk] outbound card ledger failed: %r", e)
        return SendResult(ok=True, message_id=tid)

    async def send(self, out: OutboundMessage) -> SendResult:
        try:
            md = out.metadata or {}
            raw_card = md.get("raw_card")
            # 批 5：互动卡片模式优先（按钮可回调）；失败返回 None → 走下面的回落链路
            if self._card_mode == "interactive" and self._card_client and self._card_template_id:
                res = await self._send_interactive(out)
                if res is not None:
                    return res
            if isinstance(raw_card, dict) and raw_card.get("msgtype") == "actionCard":
                # Issue #1095：Card IR 经 DingtalkCardRenderer 渲染的 actionCard 载荷
                if self._send_action_card is None:
                    return SendResult(ok=False, error="actionCard transport unavailable")
                ac = raw_card.get("actionCard") or {}
                result = await _maybe_await(self._send_action_card(
                    out.chat_id, str(ac.get("title") or ""), str(ac.get("text") or ""),
                    btns=list(ac.get("btns") or [])))
                return SendResult(ok=True, message_id=str(result or ""))
            if isinstance(raw_card, dict) and ("header" in raw_card or "elements" in raw_card):
                # 平台格式泄漏护栏（Issue #1095）：lark JSON 不允许透传钉钉，显式失败
                return SendResult(ok=False, error="lark raw_card refused on dingtalk")
            if not (out.text or "").strip() and not raw_card:
                # 空内容硬护栏（Issue #1095）：发空即 failed，不再静默 ok=True
                return SendResult(ok=False, error="empty content guard")
            if str(md.get("kind") or "") == "card":
                # 字符串卡片（egress.reply_card → kind="card" + title，无 raw_card）：
                # 仍按卡片形态发 actionCard。修 2026-11-05 钉钉 /where 等 legacy 文本卡片
                # 被当成普通文本发出（标题丢失、无卡片外框、也不计 degraded）。
                # text 走 markdown 渲染，单换行不生效 → 补硬换行，保持原有分行。
                from ...channel.card import hard_breaks
                if self._send_action_card is None:
                    return SendResult(ok=False, error="actionCard transport unavailable")
                result = await _maybe_await(self._send_action_card(
                    out.chat_id, str(md.get("title") or "DevOps Bot"),
                    hard_breaks(out.text or ""), btns=[]))
                return SendResult(ok=True, message_id=str(result or ""))
            if _wants_markdown(out):
                chunks = split_markdown(out.text or "", DINGTALK_MARKDOWN_LIMIT)
                last_id = ""
                for i, chunk in enumerate(chunks):
                    marker = f"({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
                    last_id = str(await _maybe_await(self._send_markdown(
                        out.chat_id, chunk, chunk_marker=marker)) or "")
                return SendResult(ok=True, message_id=last_id)
            if not (out.text or "").strip():
                # 空内容护栏（Issue #1095 前）：钉钉 webhook/机器人 API 对空 text 返回
                # errcode!=0 / HTTP 400，但旧实现仍可能被上层当成功。显式失败，
                # 让调用方看到"没发出去"而不是"发出去了一条空消息"。
                return SendResult(ok=False, error="empty outbound text")
            result = await _maybe_await(self._send_text(out.chat_id, out.text or ""))
            return SendResult(ok=True, message_id=str(result or ""))
        except Exception as e:
            return SendResult(ok=False, error=str(e))


_MD_LINE_RE = re.compile(r"^(?:#{1,6}\s|[-*]\s|```|\|)", re.MULTILINE)


def _wants_markdown(out: OutboundMessage) -> bool:
    """行首锚定的 markdown 富文本判定（标题/列表/代码块/表格行）。

    行首锚定避免正文中普通 "|" / "* " 等字符把纯文本误判为富文本
    （纯文本走 markdown 通道会被平台渲染改写）。
    """
    return bool(_MD_LINE_RE.search(out.text or ""))


__all__ = ["DINGTALK_CAPABILITIES", "DINGTALK_MARKDOWN_LIMIT", "DingtalkSender",
           "DingtalkWebhookError", "WebhookRegistry", "default_webhook_registry",
           "send_text_via_webhook", "send_markdown_via_webhook",
           "send_action_card_via_webhook", "send_action_card_with_fallback",
           "DingtalkRobotApi", "DingtalkAdapterApi",
           "send_text_with_fallback", "send_markdown_with_fallback"]
