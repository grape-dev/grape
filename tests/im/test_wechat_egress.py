"""微信 Egress→Renderer→Sender 集成测试（Issue #1728 复审）。

模块内替身测试无法覆盖跨模块契约：renderer 输出 `{"text": ...}` 纯文本载荷，
Egress 的空载荷护栏（`_card_payload_empty`）若不识别该形态，卡片会被误判为空
并触发 `card_empty_guard`，微信所有卡片（含 `/repo`）发送失败。
"""
import asyncio
import json
from types import SimpleNamespace

from src.im.adapters.wechat.outbound import WechatSender
from src.im.adapters.wechat.protocol import WechatClient
from src.im.channel.card import Action, Card, Tone, row_block, text_block
from src.im.channel.renderers.wechat import WechatCardRenderer
from src.im.engine.egress import HandlerEgress, _always_valid, _card_payload_empty
from src.im.engine.metrics import EngineMetrics
from src.im.engine.replier import Replier


async def _no_sleep(_delay):
    return None


def _client(calls):
    async def http_post(url, payload, headers, timeout):
        calls.append((url, payload))
        return 200, {}, json.dumps({"ret": 0, "msg": {"message_id": 1}}).encode()

    return WechatClient("tok", http_post=http_post, sleep=_no_sleep)


def _ctx():
    return SimpleNamespace(app_id="app_1", name="t")


def _repo_card():
    blocks = [
        text_block("**📁 已托管仓库列表**\n1. owner/a（owner）\n2. owner/b（member）"),
        row_block(
            Action(id="bind", label="1", params={"num": 1}),
            Action(id="bind", label="2", params={"num": 2}),
        ),
    ]
    return Card(title="📁 选择仓库", tone=Tone.DEFAULT, blocks=tuple(blocks))


def _egress(calls, metrics):
    sender = WechatSender(_client(calls), metrics=metrics)
    return HandlerEgress(
        None, Replier(sender), metrics=metrics, mode="replier",
        msg_id_validator=_always_valid, card_renderer=WechatCardRenderer(),
        default_route="u@im.wechat")


def test_wechat_text_payload_is_not_empty():
    assert _card_payload_empty({"text": "【标题】\n1. 选项"}) is False
    assert _card_payload_empty({"text": "  "}) is True
    assert _card_payload_empty({"text": "", "reply_markup": {"inline_keyboard": []}}) is True


def test_egress_renders_wechat_card_and_sends_text():
    calls, m = [], EngineMetrics()
    ok = asyncio.run(_egress(calls, m).send_card(
        _ctx(), "m1", _repo_card(), chat_id="u@im.wechat"))

    assert ok is True
    send_calls = [p for u, p in calls if u.endswith("/sendmessage")]
    assert send_calls, "card must reach WechatSender /sendmessage"
    body = send_calls[0]["msg"]["item_list"][0]["text_item"]["text"]
    assert "【📁 选择仓库】" in body
    assert "1. owner/a（owner）" in body
    assert m.degraded_by_reason.get("card_empty_guard", 0) == 0
    assert m.messages_failed == 0


def test_egress_empty_wechat_card_still_guarded():
    calls, m = [], EngineMetrics()
    ok = asyncio.run(_egress(calls, m).send_card(
        _ctx(), "m1", Card(), chat_id="u@im.wechat"))

    assert ok is False
    assert calls == []
    assert m.degraded_by_reason.get("card_empty_guard", 0) == 1
    assert m.messages_failed == 1