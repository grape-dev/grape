"""出站契约测试（Issue #1008 验收）。

覆盖 Spec 四类用例：
  ① 能力位降级（无 RICH_CARD → 纯文本 / 无 MESSAGE_EDIT → 追加 / 无 REACTION → no-op）
  ② 空路由计失败（metrics.failed + 结构化日志，不再静默丢弃）
  ③ 双渠道回放（同一 egress 契约驱动飞书/钉钉两个发送器）
  ④ legacy parity（legacy 模式 1:1 透传 legacy api）
  ⑤ om_ 假设删除（message_id_valid 按渠道声明探测）
  ⑥ 灰度开关 GRAPE_IM_EGRESS_MODE=replier|legacy
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.im.channel.capability import Capability
from src.im.channel.message import OutboundMessage, SendResult
from src.im.engine.egress import (
    HandlerEgress, egress_mode, _always_valid, _is_feishu_id_valid,
)
from src.im.engine.metrics import EngineMetrics
from src.im.engine.replier import Replier


class FakeSender:
    """可声明能力位的假 ChannelSender。"""

    def __init__(self, caps=Capability.TEXT, calls=None):
        self._caps = caps
        self.calls = calls if calls is not None else []

    def capabilities(self):
        return self._caps

    async def send(self, out: OutboundMessage) -> SendResult:
        self.calls.append(out)
        return SendResult(ok=True, message_id="mid_out_1")


class Metrics:
    def __init__(self):
        self.failed = 0
        self.degraded = 0

    def record_failed(self):
        self.failed += 1

    def record_degraded(self):
        self.degraded += 1


def _ctx():
    return SimpleNamespace(app_id="app_1", name="t")


# ---------------------------------------------------------------- ① 能力位降级

def test_text_only_channel_strips_markdown_and_counts_degraded():
    """无 RICH_CARD → Replier 剥离 markdown；降级路径可见。"""
    m = Metrics()
    sender = FakeSender(Capability.TEXT)
    eg = HandlerEgress(AsyncMock(), Replier(sender), metrics=m,
                       mode="replier", msg_id_validator=_always_valid)
    mid = asyncio.run(eg.reply_card(_ctx(), "m1", "**标题** `code`", chat_id="cid1"))
    assert mid == "mid_out_1"
    out = sender.calls[0]
    assert "**" not in out.text and "`" not in out.text
    assert out.chat_id == "cid1"
    assert out.metadata.get("degraded") == "text_fallback"


def test_message_edit_missing_degrades_to_append():
    """无 MESSAGE_EDIT → update_message 降级为追加发送（内容不丢，计 degraded）。"""
    m = Metrics()
    sender = FakeSender(Capability.TEXT | Capability.RICH_CARD)
    eg = HandlerEgress(AsyncMock(), Replier(sender), metrics=m,
                       mode="replier", msg_id_validator=_always_valid)
    ok = asyncio.run(eg.update_message(_ctx(), "m1", "流式增量", chat_id="cid1"))
    assert ok is True
    out = sender.calls[0]
    assert out.text == "流式增量"
    assert m.degraded == 1


def test_reaction_unsupported_is_explicit_noop():
    """无 REACTION → add_reaction 降级 no-op（计 degraded，非静默）。"""
    m = Metrics()
    api = AsyncMock()
    sender = FakeSender(Capability.TEXT)          # 无 REACTION
    eg = HandlerEgress(api, Replier(sender), metrics=m,
                       mode="replier", msg_id_validator=_always_valid)
    asyncio.run(eg.add_reaction(_ctx(), "m1", "OK"))
    api.add_reaction.assert_not_awaited()
    assert m.degraded == 1


# ---------------------------------------------------------------- ② 空路由计失败

def test_empty_route_records_failed_not_silent():
    """chat_id 与 msg_id 双空 → failed +1，零发送（钉钉事故不再静默丢弃）。"""
    m = Metrics()
    sender = FakeSender(Capability.TEXT)
    eg = HandlerEgress(AsyncMock(), Replier(sender), metrics=m,
                       mode="replier", default_route="", msg_id_validator=_always_valid)
    mid = asyncio.run(eg.reply_text(_ctx(), "", "hello"))
    assert mid == ""
    assert sender.calls == []
    assert m.failed == 1


def test_dingtalk_webhook_missing_route_raises_for_counting():
    """钉钉传输：conversation 无 sessionWebhook → 显式异常（可被计数），不静默成功。"""
    from src.im.adapters.dingtalk.outbound import (
        WebhookRegistry, send_markdown_via_webhook, send_text_via_webhook,
    )

    reg = WebhookRegistry()
    assert reg.lookup("cid_unknown") == ""
    with pytest.raises(LookupError):
        asyncio.run(send_text_via_webhook("cid_unknown", "hi", registry=reg))
    with pytest.raises(LookupError):
        asyncio.run(send_markdown_via_webhook("cid_unknown", "# hi", registry=reg))


# ---------------------------------------------------------------- ③ 双渠道回放

def test_dual_channel_replay_same_egress_contract():
    """同一 HandlerEgress 契约驱动飞书/钉钉两个发送器：路由/文本一致，渲染归渠道。"""
    feishu_calls, dingtalk_calls = [], []
    feishu = FakeSender(Capability.TEXT | Capability.RICH_CARD | Capability.REACTION
                        | Capability.MESSAGE_EDIT, feishu_calls)
    dingtalk = FakeSender(Capability.TEXT | Capability.RICH_CARD, dingtalk_calls)

    for sender, validator in ((feishu, _is_feishu_id_valid),
                              (dingtalk, _always_valid)):
        eg = HandlerEgress(AsyncMock(), Replier(sender), mode="replier",
                           msg_id_validator=validator, default_route="cid_9")
        asyncio.run(eg.reply_text(_ctx(), "mid_9", "你好"))
        asyncio.run(eg.reply_card(_ctx(), "mid_9", "# 标题\n- 列表"))

    assert [c.chat_id for c in feishu_calls] == ["cid_9", "cid_9"]
    assert [c.chat_id for c in dingtalk_calls] == ["cid_9", "cid_9"]
    # markdown 原文进 OutboundMessage（飞书渲染卡片 / 钉钉 15000 字节分块各自处理）
    assert feishu_calls[1].text == dingtalk_calls[1].text == "# 标题\n- 列表"


def test_dingtalk_msgid_shape_never_blocks_delivery():
    """钉钉 msgId 不以 om_ 开头 → message_id_valid 恒真 → 不进入换 id 分支（事故根因修复）。"""
    sender = FakeSender(Capability.TEXT)
    eg = HandlerEgress(AsyncMock(), Replier(sender), mode="replier",
                       msg_id_validator=_always_valid)
    assert eg.message_id_valid("dingtalk-msg-abc") is True
    assert eg.message_id_valid("") is False
    feishu_eg = HandlerEgress(AsyncMock(), Replier(sender), mode="replier",
                              msg_id_validator=_is_feishu_id_valid)
    assert feishu_eg.message_id_valid("dingtalk-msg-abc") is False
    assert feishu_eg.message_id_valid("om_x") is True


# ---------------------------------------------------------------- ④ legacy parity

def test_legacy_mode_passes_through_verbatim():
    """legacy 模式：1:1 透传 legacy api（签名/参数原样，与改造前一致）。"""
    api = AsyncMock()
    api.reply_text.return_value = "om_r"
    api.reply_card.return_value = "om_c"
    eg = HandlerEgress(api, None, mode="legacy")
    assert eg.mode == "legacy"
    out = asyncio.run(eg.reply_text(_ctx(), "om_1", "text"))
    api.reply_text.assert_awaited_once_with(_ctx(), "om_1", "text")
    assert out == "om_r"
    asyncio.run(eg.reply_card(_ctx(), "om_2", "card", title="T", template="green"))
    api.reply_card.assert_awaited_once_with(_ctx(), "om_2", "card", title="T", template="green")
    asyncio.run(eg.update_card_by_token(_ctx(), "tok", {"card": 1}))
    api.update_card_by_token.assert_awaited_once_with(_ctx(), "tok", {"card": 1})
    asyncio.run(eg.add_reaction(_ctx(), "om_3", "OK"))
    api.add_reaction.assert_awaited_once_with(_ctx(), "om_3", "OK")


def test_legacy_mode_uses_om_prefix_probe():
    """legacy 模式保留 om_ 判定（parity：与改造前分支行为一致）。"""
    eg = HandlerEgress(AsyncMock(), None, mode="legacy")
    assert eg.message_id_valid("om_x") is True
    assert eg.message_id_valid("msg_1") is False


# ---------------------------------------------------------------- ⑥ 灰度开关

def test_egress_mode_default_replier_and_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv("GRAPE_IM_EGRESS_MODE", raising=False)
    monkeypatch.setenv("GRAPE_IM_EGRESS_MODE_FILE", str(tmp_path / "no_such_file"))
    assert egress_mode() == "replier"
    monkeypatch.setenv("GRAPE_IM_EGRESS_MODE", "legacy")
    assert egress_mode() == "legacy"
    monkeypatch.setenv("GRAPE_IM_EGRESS_MODE", "replier")
    assert egress_mode() == "replier"


def test_egress_mode_file_overrides_env(monkeypatch, tmp_path):
    mode_file = tmp_path / "im_egress_mode"
    monkeypatch.setenv("GRAPE_IM_EGRESS_MODE", "replier")
    monkeypatch.setenv("GRAPE_IM_EGRESS_MODE_FILE", str(mode_file))
    mode_file.write_text("legacy")
    assert egress_mode() == "legacy"        # 运行期改文件 → 秒级回滚
    mode_file.write_text("replier")
    assert egress_mode() == "replier"
    mode_file.write_text("garbage")
    assert egress_mode() == "replier"       # 非法值回落 env


# ---------------------------------------------------------------- 补充：路由兜底

def test_default_route_used_when_call_site_omits_chat_id():
    """handler 未显式传 chat_id → 使用装配层注入的消息级 default_route（钉钉路由兜底）。"""
    sender = FakeSender(Capability.TEXT)
    eg = HandlerEgress(AsyncMock(), Replier(sender), mode="replier",
                       msg_id_validator=_always_valid, default_route="cid_ding")
    asyncio.run(eg.reply_text(_ctx(), "mid_1", "hi"))
    assert sender.calls[0].chat_id == "cid_ding"


def test_metrics_engine_degraded_key_exposed():
    """EngineMetrics 出站降级键进 snapshot（/health/realtime im 段可见）。"""
    m = EngineMetrics()
    m.record_degraded()
    snap = m.snapshot()
    assert snap["total_messages_degraded"] == 1


def test_raw_card_flattens_to_text_on_card_less_channel():
    """钉钉（无 RICH_CARD）发 raw_card：必须压平成文本发出，不得发空消息。

    2026-11-05 实障：能力位谎报 RICH_CARD → 跳过降级 → send_text("") → 400 → 用户
    看到的是"仓库列表发送失败"甚至"名下没有仓库"。
    """
    m = Metrics()
    sender = FakeSender(Capability.TEXT)          # 钉钉现状：只有文本能力
    eg = HandlerEgress(AsyncMock(), Replier(sender), metrics=m,
                       mode="replier", msg_id_validator=_always_valid)
    card = {"header": {"title": {"tag": "plain_text", "content": "📁 选择仓库"}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md",
                                                 "content": "1. XOcto/hello-blog-2"}}]}
    ok = asyncio.run(eg.send_raw_card(_ctx(), "m1", card, chat_id="cid1"))
    assert ok is True
    out = sender.calls[0]
    assert out.text.strip(), "卡片压平后不得为空"
    assert "XOcto/hello-blog-2" in out.text
    assert m.degraded == 1
