"""飞书出站测试 —— update_multi 坑断言（Issue #742 §7.6 / multica 陷阱 1）。"""
import asyncio

from src.im.adapters.feishu.outbound import FeishuSender, _wants_card
from src.im.channel import Capability, OutboundMessage


def test_markdown_goes_card_plain_goes_text():
    calls = []

    def reply_text(chat_id, text):
        calls.append(("text", chat_id, text))

    def reply_card(chat_id, content, update_multi=False):
        calls.append(("card", chat_id, content, update_multi))
        return "om_out"

    sender = FeishuSender(reply_text, reply_card)
    out_md = OutboundMessage(chat_id="oc_1", text="# 标题\n- 列表")
    res = asyncio.run(sender.send(out_md))
    assert res.ok
    assert calls[-1][0] == "card" and calls[-1][3] is True

    res = asyncio.run(sender.send(OutboundMessage(chat_id="oc_1", text="plain hello")))
    assert calls[-1][0] == "text"


def test_update_multi_always_true_for_card():
    """流式/多段更新必须 update_multi=True，否则静默 no-op（陷阱守卫）。"""
    seen = {}

    def reply_card(chat_id, content, update_multi=False):
        seen["update_multi"] = update_multi
        return "om"

    sender = FeishuSender(lambda *a: None, reply_card)
    asyncio.run(sender.send(OutboundMessage(chat_id="c", text="```code```")))
    assert seen["update_multi"] is True


def test_capabilities_declared():
    sender = FeishuSender(lambda *a: None, lambda *a, **k: None)
    caps = sender.capabilities()
    assert caps & Capability.RICH_CARD
    assert caps & Capability.TEXT


def test_send_error_wrapped_in_result():
    def boom(chat_id, text):
        raise RuntimeError("feishu 429")

    sender = FeishuSender(boom, boom)
    res = asyncio.run(sender.send(OutboundMessage(chat_id="c", text="x")))
    assert res.ok is False and "429" in res.error
