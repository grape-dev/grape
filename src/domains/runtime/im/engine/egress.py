"""HandlerEgress —— handler 出站统一门面（Issue #1008）。

出站收敛为纯 Replier：handler_async / vod_handler_async 的全部发送调用
改为 `egress.<method>` → OutboundMessage → Replier.reply → Channel.send，
核心路径零平台名（平台差异下沉到适配器 capabilities()/send 实现）。

双模式灰度（秒级可回滚）：
  - GRAPE_IM_EGRESS_MODE=replier（默认）：OutboundMessage → Replier → Channel.send；
  - GRAPE_IM_EGRESS_MODE=legacy：1:1 透传 legacy 平台 API（FeishuAsync），
    行为与改造前一致（parity 回滚路径）。

失败语义（修复「deliver skipped: empty chat_id」静默丢弃事故）：
  - 空路由（chat_id 与 msg_id 双空）→ metrics.record_failed + 结构化 warning 日志；
  - SendResult 失败 → 由 CountingSender 计 failed；
  - 能力位缺失降级（REACTION/MESSAGE_EDIT/RICH_CARD）→ metrics.record_degraded。

方法签名保持 legacy 位置参数（ctx, msg_id/open_id, ...），chat_id 作可选尾参
（钉钉 BindingKey=conversation_id 必须显式给；飞书可回落 msg_id reply）。
下载类（download_image/download_file）与 get_user_name 属适配器入站/身份能力，
本层仅透传（P4 MediaRef 两跳另案处理）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Protocol

from ..channel.capability import Capability
from ..channel.message import OutboundMessage, SendResult

log = logging.getLogger(__name__)

EGRESS_ENV = "GRAPE_IM_EGRESS_MODE"
EGRESS_MODE_FILE_ENV = "GRAPE_IM_EGRESS_MODE_FILE"


def egress_mode() -> str:
    """出站模式：'replier'（默认）| 'legacy'（1:1 透传 legacy 平台 API）。

    读取顺序与 engine_mode 同款：覆盖文件（运行期可改 → 秒级回滚）> env > 默认 replier。
    """
    path = os.environ.get(EGRESS_MODE_FILE_ENV) or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "im_egress_mode",
    )
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip().lower()
        if value in ("replier", "legacy"):
            return value
    except Exception:
        pass
    env = os.environ.get(EGRESS_ENV, "replier").strip().lower()
    return "legacy" if env in ("legacy", "0", "off", "false") else "replier"


class _ReplierLike(Protocol):
    def caps(self) -> Capability: ...
    async def reply(self, out: OutboundMessage) -> SendResult: ...
    async def edit(self, out: OutboundMessage) -> SendResult: ...


class _MetricsLike(Protocol):
    def record_failed(self) -> None: ...
    def record_degraded(self, reason: str = "") -> None: ...


def _is_feishu_id_valid(msg_id: str) -> bool:
    """飞书消息 ID 形态校验（平台语义下沉适配器侧，核心不感知 om_ 含义）。"""
    return bool(msg_id) and msg_id.startswith("om_")


def _always_valid(msg_id: str) -> bool:
    """钉钉：conversationId 路由 + sessionWebhook 回复目标，msgId 形态无关。"""
    return bool(msg_id)


class HandlerEgress:
    """出站门面：replier 模式走 OutboundMessage → Replier → Channel.send。

    legacy 模式（或 replier 未就绪）1:1 透传 legacy_api（FeishuAsync 同名方法），
    保证 legacy 开关下行为与改造前完全一致。
    """

    def __init__(
        self,
        legacy_api: Any,
        replier: Optional[_ReplierLike] = None,
        *,
        metrics: Optional[_MetricsLike] = None,
        mode: Optional[str] = None,
        msg_id_validator: Optional[Any] = None,
        default_route: str = "",
        card_renderer: Any = None,
    ) -> None:
        self._api = legacy_api
        self._replier = replier
        self._metrics = metrics
        self._mode = mode or egress_mode()
        self._msg_id_valid = msg_id_validator or _is_feishu_id_valid
        # 本次分发消息的 BindingKey（飞书 chat_id / 钉钉 conversation_id）：
        # 装配层按消息注入，handler 无需逐调用点传 chat_id（钉钉空路由事故的兜底路由）。
        self._default_route = default_route
        # Issue #1095：渠道 CardRenderer（wiring 按渠道注入；None → 文本降级）
        self._card_renderer = card_renderer

    @property
    def default_route(self) -> str:
        return self._default_route

    @default_route.setter
    def default_route(self, value: str) -> None:
        self._default_route = value or ""

    # ---------------- 模式与能力探测

    @property
    def mode(self) -> str:
        return "legacy" if (self._replier is None or self._mode == "legacy") else "replier"

    def caps(self) -> Capability:
        if self.mode != "replier" or self._replier is None:
            # legacy：飞书全量（旧路径无降级分支）
            return Capability.TEXT | Capability.RICH_CARD | Capability.QUOTE_REPLY \
                | Capability.ATTACHMENT | Capability.MESSAGE_EDIT | Capability.REACTION
        return self._replier.caps()

    def message_id_valid(self, msg_id: str) -> bool:
        """回复目标可用性探测（Issue #1008：删除共享代码 om_ 前缀假设）。

        legacy：飞书 om_ 前缀判定（与改造前一致）；
        replier：适配器声明的校验器（飞书 om_ / 钉钉恒真——走 conversation 路由）。
        """
        if self.mode != "replier":
            return _is_feishu_id_valid(msg_id)
        return bool(self._msg_id_valid(msg_id or ""))

    # ---------------- 内部

    def _record_failed(self, reason: str, **kw) -> None:
        if self._metrics is not None:
            self._metrics.record_failed()
        log.warning("[egress] deliver skipped: %s %s", reason, kw or "")

    def _record_degraded(self, reason: str, **kw) -> None:
        if self._metrics is not None:
            try:
                self._metrics.record_degraded(reason)
            except TypeError:  # 旧 metrics 替身（无 reason 形参）
                self._metrics.record_degraded()
        log.info("[egress] degraded deliver: %s %s", reason, kw or "")

    def _render_card(self, card, renderer=None):
        """Card IR → 平台载荷（Issue #1095）。

        返回 (payload, degraded_reasons)。card 为 dict（存量 lark JSON）时原样透传
        （legacy 模式回滚路径）；renderer 缺失 → 文本降级（card_text_fallback）。
        legacy 模式固定飞书（legacy 1:1 透传本就是飞书专属路径）→ 默认飞书 renderer。
        """
        from ..channel.card import Card, card_ir_enabled, render_text
        if not isinstance(card, Card):
            return card, []
        if card_ir_enabled() and renderer is not None and (self.caps() & Capability.RICH_CARD):
            rendered = renderer.render(card)
            return rendered.payload, list(rendered.degraded)
        if self.mode != "replier":
            from ..channel.renderers.feishu import FeishuCardRenderer
            rendered = FeishuCardRenderer().render(card)
            return rendered.payload, list(rendered.degraded)
        return render_text(card), ["card_text_fallback"]

    async def _send(self, out: OutboundMessage) -> SendResult:
        assert self._replier is not None
        if not out.chat_id and not (out.reply_to and out.reply_to.message_id) \
                and not (out.metadata or {}).get("msg_id"):
            self._record_failed("empty chat_id", text=(out.text or "")[:60])
            return SendResult(ok=False, error="empty chat_id")
        # Issue #1095 空内容硬护栏：三路径任一发出空内容 → failed + 告警（不再 ok=True 静默）
        md = out.metadata or {}
        if md.get("kind") == "text" and not (out.text or "").strip() \
                and not md.get("raw_card"):
            self._record_failed("card_empty_guard", route=out.chat_id[:30])
            self._record_degraded("card_empty_guard")
            return SendResult(ok=False, error="empty content guard")
        if md.get("kind") == "raw_card" and _card_payload_empty(md.get("raw_card")):
            # Issue #1095 复审：raw_card 渲染后空载荷同样失败，护栏覆盖全部出站路径
            self._record_failed("card_empty_guard", route=out.chat_id[:30])
            self._record_degraded("card_empty_guard")
            return SendResult(ok=False, error="empty content guard")
        res = await self._replier.reply(out)
        if (res and res.ok) and md.get("degraded"):
            self._record_degraded(str(md.get("degraded")))
        if (res and res.ok):
            for reason in md.get("card_degraded") or []:
                self._record_degraded(str(reason))
        if res is not None and not res.ok:
            self._record_failed("send failed", error=(res.error or "")[:80])
        return res

    def _out(self, chat_id: str, text: str, msg_id: str = "", **md) -> OutboundMessage:
        from ..channel.message import ReplyCtx
        route = chat_id or self._default_route
        metadata: dict = dict(md)
        if msg_id:
            metadata["msg_id"] = msg_id
        reply_to = ReplyCtx(message_id=msg_id) if msg_id else None
        return OutboundMessage(chat_id=route, text=text, reply_to=reply_to,
                               metadata=metadata)

    # ---------------- 发送族（replier：OutboundMessage → Replier → Channel.send）

    async def reply_text(self, ctx, msg_id: str, text: str, chat_id: str = "") -> str:
        if self.mode != "replier":
            return await self._api.reply_text(ctx, msg_id, text)
        res = await self._send(self._out(chat_id, text, msg_id, kind="text"))
        return res.message_id if (res and res.ok) else ""

    async def reply_card(self, ctx, msg_id: str, text: str,
                         title: str = "DevOps Bot", template: str = "blue",
                         chat_id: str = "") -> str:
        if self.mode != "replier":
            return await self._api.reply_card(ctx, msg_id, text, title=title, template=template)
        res = await self._send(self._out(chat_id, text, msg_id,
                                         kind="card", title=title, template=template))
        return res.message_id if (res and res.ok) else ""

    async def send_text_to_user(self, ctx, open_id: str, text: str,
                                chat_id: str = "") -> str:
        if self.mode != "replier":
            return await self._api.send_text_to_user(ctx, open_id, text)
        route = chat_id or open_id
        res = await self._send(self._out(route, text, "", kind="text", target_user=open_id))
        return res.message_id if (res and res.ok) else ""

    async def send_card_to_user(self, ctx, open_id: str, text: str,
                                title: str = "DevOps Bot", template: str = "blue",
                                chat_id: str = "", **kw) -> str:
        if self.mode != "replier":
            return await self._api.send_card_to_user(ctx, open_id, text,
                                                     title=title, template=template, **kw)
        route = chat_id or open_id
        res = await self._send(self._out(route, text, "", kind="card", title=title,
                                         template=template, target_user=open_id))
        return res.message_id if (res and res.ok) else ""

    async def send_card_to_chat(self, ctx, chat_id: str, text: str,
                                fallback_open_id: str = "", **kw) -> str:
        if self.mode != "replier":
            return await self._api.send_card_to_chat(ctx, chat_id, text,
                                                     fallback_open_id=fallback_open_id, **kw)
        res = await self._send(self._out(chat_id, text, "", kind="card",
                                         target_user=fallback_open_id,
                                         **{k: v for k, v in (kw or {}).items()
                                            if k in ("title", "template")}))
        return res.message_id if (res and res.ok) else ""

    async def send_quick_cmd_card(self, ctx, open_id: str, chat_id: str = "") -> str:
        """快捷命令卡（飞书菜单卡）。无 RICH_CARD 渠道 → 降级为文本帮助（内容不丢）。"""
        if self.mode != "replier":
            return await self._api.send_quick_cmd_card(ctx, open_id)
        if not (self.caps() & Capability.RICH_CARD):
            self._record_degraded("quick_cmd_text_fallback", target=open_id[:20])
            return await self.reply_text(ctx, "", _QUICK_CMD_HELP, chat_id=chat_id or open_id)
        res = await self._send(self._out(chat_id or open_id, _QUICK_CMD_HELP, "",
                                         kind="card", title="DevOps Bot 命令",
                                         target_user=open_id))
        return res.message_id if (res and res.ok) else ""

    async def send_raw_card(self, ctx, msg_id: str, card, chat_id: str = "",
                            card_degraded: list[str] | None = None) -> bool:
        """自定义卡片发送（Issue #1095：Card IR 优先，存量 lark dict 仅 legacy 回滚路径）。

        replier：Card → 渠道 renderer 渲染（无 RICH_CARD/renderer → 文本降级）；
        legacy：渲染为 lark JSON 后走原 _send_card_reply 路径（含 [帮助] 按钮追加）。
        card_degraded：调用方（如 update_card_by_token）已渲染但需透传的降级归因。
        """
        payload, degraded = self._render_card(card, self._card_renderer)
        degraded = list(degraded) + list(card_degraded or [])
        if not isinstance(payload, dict):
            # 文本降级（Card IR + 无 renderer / 开关 off / dict 存量不该到这里）
            for reason in (degraded or ["card_text_fallback"]):
                self._record_degraded(reason)
            res = await self._send(self._out(chat_id, str(payload), msg_id, kind="text"))
            return bool(res and res.ok)
        if self.mode != "replier":
            return await legacy_send_card_reply(ctx, self._api, msg_id, payload)
        if not (self.caps() & Capability.RICH_CARD):
            from ..channel.card import Card, render_text
            text = render_text(card) if isinstance(card, Card) else _flatten_card(payload)
            for reason in (degraded or ["card_text_fallback"]):
                self._record_degraded(reason)
            res = await self._send(self._out(chat_id, text, msg_id, kind="text"))
            return bool(res and res.ok)
        res = await self._send(self._out(chat_id, "", msg_id, kind="raw_card",
                                          raw_card=payload,
                                          card_degraded=degraded))
        return bool(res and res.ok)

    # send_card 别名：批3 迁移后的首选调用名（语义 = 发送 Card IR）
    async def send_card(self, ctx, msg_id: str, card, chat_id: str = "") -> bool:
        return await self.send_raw_card(ctx, msg_id, card, chat_id=chat_id)

    async def add_reaction(self, ctx, msg_id: str, emoji: str) -> None:
        """表情回执：能力位 REACTION 缺失 → 显式降级 no-op（计 degraded，非静默）。"""
        if self.mode != "replier":
            await self._api.add_reaction(ctx, msg_id, emoji)
            return
        if not (self.caps() & Capability.REACTION):
            self._record_degraded("reaction_unsupported", emoji=emoji)
            return
        try:
            await self._api.add_reaction(ctx, msg_id, emoji)
        except Exception as e:
            log.warning("[egress] add_reaction failed: %s", e)

    async def update_card_by_token(self, ctx, action_token: str, card,
                                    chat_id: str = "") -> bool:
        """原地更新（卡片 action token）：无 MESSAGE_EDIT → 降级追加新卡（内容不丢）。

        Issue #1095：card 接受 Card IR（渲染后走 token patch）或存量 lark dict。
        """
        payload, degraded = self._render_card(card, self._card_renderer)
        if not isinstance(payload, dict):
            # 无 renderer 渠道：降级为文本追加（内容不丢）
            for reason in (degraded or ["card_text_fallback"]):
                self._record_degraded(reason)
            res = await self._send(self._out(chat_id, str(payload), "", kind="text"))
            return bool(res and res.ok)
        if self.mode != "replier":
            return await self._api.update_card_by_token(ctx, action_token, payload)
        if not (self.caps() & Capability.MESSAGE_EDIT):
            self._record_degraded("card_edit_append")
            return await self.send_raw_card(ctx, "", payload, chat_id=chat_id,
                                            card_degraded=degraded)
        try:
            return await self._api.update_card_by_token(ctx, action_token, payload)
        except Exception as e:
            log.warning("[egress] update_card_by_token failed: %s", e)
            return False

    async def update_message(self, ctx, message_id: str, text: str,
                             chat_id: str = "") -> bool:
        """流式原地更新：无 MESSAGE_EDIT → 降级追加消息。"""
        if self.mode != "replier":
            return await self._api.update_message(ctx, message_id, text)
        if not (self.caps() & Capability.MESSAGE_EDIT):
            self._record_degraded("message_edit_append")
            res = await self._send(self._out(chat_id, text, message_id, kind="text"))
            return bool(res and res.ok)
        try:
            return await self._api.update_message(ctx, message_id, text)
        except Exception as e:
            log.warning("[egress] update_message failed: %s", e)
            return False

    # ---------------- 适配器入站/身份能力（透传，Issue #1008 范围外）

    async def get_user_name(self, ctx, open_id: str, user_id_type: str = "open_id") -> str:
        try:
            return await self._api.get_user_name(ctx, open_id, user_id_type=user_id_type)
        except Exception as e:
            log.warning("[egress] get_user_name failed: %s", e)
            return ""

    async def download_image(self, ctx, image_key: str, message_id: str = "") -> bytes:
        return await self._api.download_image(ctx, image_key, message_id=message_id)

    async def download_file(self, ctx, file_key: str, message_id: str = "",
                            file_type: str = "file") -> bytes:
        return await self._api.download_file(ctx, file_key, message_id=message_id,
                                             file_type=file_type)


_QUICK_CMD_HELP = (
    "**🤖 DevOps Bot 命令**\n"
    "/repo — 列出并绑定仓库\n"
    "/chat — 会话列表\n"
    "/task — 任务列表\n"
    "/new — 新建会话\n"
    "/where — 当前位置\n"
    "/help — 本帮助"
)


def _card_payload_empty(payload: Any) -> bool:
    """渲染后卡片载荷是否无任何可见内容（空内容硬护栏，Issue #1095）。"""
    if not isinstance(payload, dict):
        return not str(payload or "").strip()
    if payload.get("msgtype") == "actionCard":
        ac = payload.get("actionCard") or {}
        return not str(ac.get("text") or "").strip() and not (ac.get("btns") or [])
    if payload.get("msgtype") == "markdown":
        return not str((payload.get("markdown") or {}).get("text") or "").strip()
    if payload.get("msgtype") == "text":
        return not str((payload.get("text") or {}).get("content") or "").strip()
    if "text" in payload and ("reply_markup" in payload or "parse_mode" in payload):
        # Telegram（Issue #1613）：卡片载荷 = {"text", "parse_mode"[, "reply_markup"]}。
        # 旧实现只认 lark/msgtype 形态，不认识该形态 → 落到 _flatten_card 恒为空，
        # 有内容的卡片会被空载荷护栏误拦；无按钮的纯文本卡片不含 reply_markup，
        # 因此同时认 parse_mode，避免文本卡片被误判为空。正文为空且无内联键盘按钮才算空。
        keyboard = payload.get("reply_markup") or {}
        rows = keyboard.get("inline_keyboard") if isinstance(keyboard, dict) else None
        has_buttons = any(isinstance(row, list) and row for row in (rows or []))
        return not str(payload.get("text") or "").strip() and not has_buttons
    if "text" in payload and not any(
            key in payload for key in ("msgtype", "blocks", "header", "elements")):
        # WeChat（Issue #1728）：renderer 输出 {"text": ...} 纯文本载荷。
        # 旧实现落到 _flatten_card → 恒为空串，有内容的卡片被误判为空并触发
        # card_empty_guard，微信所有卡片（含 /repo）都会发送失败。
        return not str(payload.get("text") or "").strip()
    if isinstance(payload.get("blocks"), list):
        # Slack Block Kit（Issue #1309）：旧实现只认 lark/msgtype 形态，不认识 Slack
        # 载荷，落到 _flatten_card 抽 blocks 文本 → 恒为空串，有内容的卡片被误判为空
        # 而被护栏拦下（线上 /repo 等全部卡片报「当前渠道不支持卡片消息」）。
        # 这里按块类型判可见内容：空 blocks 仍算空，有任何标准块即非空。
        _visible = {"header", "section", "actions", "context", "divider", "input"}
        return not any(
            isinstance(b, dict) and str(b.get("type") or "") in _visible
            for b in payload["blocks"])
    return not _flatten_card(payload).strip()


def _flatten_card(card: dict) -> str:
    """降级路径：lark 卡片 JSON → 纯文本（保内容 + 动作命令提示）。

    Issue #1095：补齐按钮/下拉（此前只抽 div.text 会丢交互语义）——
    按钮按 value.cmd 渲染为「回复 `cmd`」提示，下拉选项按编号列出。
    """
    lines: list[str] = []
    header = (card or {}).get("header") or {}
    title = (header.get("title") or {}).get("content", "")
    if title:
        lines.append(f"**{title}**")
    elements = (card or {}).get("elements") or []
    if not elements and isinstance((card or {}).get("body"), dict):
        elements = (card["body"] or {}).get("elements") or []
    for el in elements:
        if not isinstance(el, dict):
            continue
        text = el.get("text") or {}
        content = text.get("content", "") if isinstance(text, dict) else ""
        if content:
            lines.append(str(content))
        tag = el.get("tag")
        if tag == "action":
            for btn in el.get("actions") or []:
                label = ((btn.get("text") or {}).get("content") or "").strip()
                cmd = ((btn.get("value") or {}).get("cmd") or "").strip()
                if cmd:
                    lines.append(f"{label}: 回复 `{cmd}`" if label else f"回复 `{cmd}`")
        elif tag == "select_static":
            from ..channel.card import numbered_option_label
            for i, opt in enumerate(el.get("options") or [], 1):
                label = ((opt.get("text") or {}).get("content") or "").strip()
                lines.append(numbered_option_label(i, label))
    return "\n".join(lines)


async def legacy_send_card_reply(ctx, api, msg_id: str, card: dict) -> bool:
    """legacy 模式自定义卡片发送（原 handler_async._send_card_reply 原样迁移）。

    自动在卡片底部追加[帮助]按钮；card 2.0（schema=="2.0"）原样发送。
    """
    import json as _json
    if card.get("schema") == "2.0":
        token = await api._ensure_token(ctx)
        if not token:
            return False
        session = await api._get_session()
        url = "https://open.feishu.cn/open-apis/im/v1/messages/" + msg_id + "/reply"
        payload = {"msg_type": "interactive", "content": _json.dumps(card, ensure_ascii=False)}
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                if data.get("code", -1) == 0:
                    ctx.metrics.messages_sent += 1
                    return True
                log.warning(f"[{ctx.name}] legacy card reply(v2) failed: code={data.get('code')}")
                return False
        except Exception as e:
            ctx.metrics.messages_failed += 1
            log.warning(f"[{ctx.name}] legacy card reply(v2) error: {e}")
            return False
    elements = card.get("elements", [])
    has_help = any(
        any(btn.get("value", {}).get("cmd") == "/help" for btn in el.get("actions", []))
        for el in elements if el.get("tag") == "action"
    )
    if not has_help:
        elements.append({
            "tag": "action",
            "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "❓ 帮助"},
                         "value": {"cmd": "/help"}}]
        })
        card["elements"] = elements
    token = await api._ensure_token(ctx)
    if not token:
        return False
    session = await api._get_session()
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg_id}/reply"
    payload = {"msg_type": "interactive", "content": _json.dumps(card, ensure_ascii=False)}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json()
            if data.get("code", -1) == 0:
                ctx.metrics.messages_sent += 1
                return True
            log.warning(f"[{ctx.name}] legacy card reply failed: code={data.get('code')}")
            return False
    except Exception as e:
        log.warning(f"[{ctx.name}] legacy card reply error: {e}")
        return False


__all__ = ["HandlerEgress", "egress_mode", "legacy_send_card_reply",
           "EGRESS_ENV", "EGRESS_MODE_FILE_ENV"]
