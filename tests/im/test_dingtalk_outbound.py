"""钉钉出站发送器测试（Issue #936 P2，§7.6 分块发送 + 能力位 + 错误包装）。"""
import asyncio
import time

import pytest

from src.im.adapters.dingtalk.markdown import DINGTALK_MARKDOWN_LIMIT
from src.im.adapters.dingtalk.outbound import DingtalkSender
from src.im.channel import Capability, OutboundMessage


def test_plain_text_goes_text_channel():
    calls = []

    def send_text(chat_id, text):
        calls.append(("text", chat_id, text))
        return "dt_mid"

    def send_markdown(chat_id, content, chunk_marker=""):
        calls.append(("md", chat_id, content))
        return "dt_md"

    sender = DingtalkSender(send_text, send_markdown)
    res = asyncio.run(sender.send(OutboundMessage(chat_id="cid_1", text="plain hello")))
    assert res.ok and res.message_id == "dt_mid"
    assert calls[-1][0] == "text"


def test_markdown_goes_markdown_channel_with_marker():
    calls = []

    def send_markdown(chat_id, content, chunk_marker=""):
        calls.append((content, chunk_marker))
        return f"dt_{len(calls)}"

    sender = DingtalkSender(lambda *a, **k: None, send_markdown)
    res = asyncio.run(sender.send(
        OutboundMessage(chat_id="cid_1", text="# 标题\n- 列表\n- 项")))
    assert res.ok
    assert calls[0][1] == ""            # 单块不带序号


def test_long_markdown_chunked_with_indices():
    """超 15000 字节 markdown → 分块逐条发送，块序号 (i/n)。"""
    calls = []

    def send_markdown(chat_id, content, chunk_marker=""):
        calls.append((content, chunk_marker))
        return f"dt_{len(calls)}"

    sender = DingtalkSender(lambda *a, **k: None, send_markdown)
    text = "# 报告\n" + "详细内容行\n" * 2000
    assert len(text.encode()) > DINGTALK_MARKDOWN_LIMIT
    res = asyncio.run(sender.send(OutboundMessage(chat_id="cid_1", text=text)))
    assert res.ok
    n = len(calls)
    assert n > 1
    assert calls[0][1] == f"(1/{n})"
    assert calls[-1][1] == f"({n}/{n})"
    for content, _ in calls:
        assert len(content.encode("utf-8")) <= DINGTALK_MARKDOWN_LIMIT


def test_chunk_fences_closed_in_send():
    """分块发送的每块围栏闭合（渲染不塌方）。"""
    calls = []

    def send_markdown(chat_id, content, chunk_marker=""):
        calls.append(content)

    sender = DingtalkSender(lambda *a, **k: None, send_markdown)
    text = "```python\n" + "x = 1\n" * 3000 + "```"
    asyncio.run(sender.send(OutboundMessage(chat_id="c", text=text)))
    assert len(calls) > 1
    for c in calls:
        assert c.count("```") % 2 == 0


def test_capabilities_declared():
    """能力位真实反映 send() 实际支持。

    Issue #1095 批4 后：钉钉有 DingtalkCardRenderer（lark 卡片 → actionCard），
    因此 RICH_CARD 是真实能力；仍未实现 ATTACHMENT / CARD_ACTION。
    """
    sender = DingtalkSender(lambda *a, **k: None, lambda *a, **k: None)
    caps = sender.capabilities()
    assert caps & Capability.TEXT
    assert caps & Capability.RICH_CARD        # renderer 就绪（#1095 批4）
    assert not (caps & Capability.ATTACHMENT)   # 未实现不声明
    assert not (caps & Capability.CARD_ACTION)  # 回调需卡片实例 API（批5）


def test_plain_text_with_inline_pipe_stays_text_channel():
    """正文含普通 "|" / "* " 等行内字符不得误判 markdown（启发式收紧）。"""
    calls = []

    def send_text(chat_id, text):
        calls.append(("text", chat_id, text))
        return "dt_mid"

    def send_markdown(chat_id, content, chunk_marker=""):
        calls.append(("md", chat_id, content))
        return "dt_md"

    sender = DingtalkSender(send_text, send_markdown)
    res = asyncio.run(sender.send(OutboundMessage(
        chat_id="cid_1", text="a | b 或 he * she 的普通句子")))
    assert res.ok
    assert calls[-1][0] == "text"


def test_line_anchored_markdown_still_goes_markdown_channel():
    """行首标题/列表/表格/代码块仍走 markdown 通道。"""
    for text in ("# 标题\n正文", "- 列表项\n- 另一项", "| a | b |\n|---|---|",
                 "```python\nx=1\n```"):
        calls = []

        def send_markdown(chat_id, content, chunk_marker=""):
            calls.append(content)
            return "dt_md"

        sender = DingtalkSender(lambda *a, **k: None, send_markdown)
        res = asyncio.run(sender.send(OutboundMessage(chat_id="c", text=text)))
        assert res.ok and calls, text


def test_send_error_wrapped_in_result():
    def boom(chat_id, text):
        raise RuntimeError("dingtalk 429")

    sender = DingtalkSender(boom, boom)
    res = asyncio.run(sender.send(OutboundMessage(chat_id="c", text="x")))
    assert res.ok is False and "429" in res.error


# ------------------------------------- #1008 Required：webhook 失败不得静默成功

class _FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = body

    async def text(self):
        import json as _json
        return _json.dumps(self._body)

    async def json(self, content_type=None):
        return self._body


class _FakeClientSession:
    response = None   # 测试注入 _FakeResponse
    raise_exc = None  # 测试注入传输异常

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    class _Post:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return False

    def post(self, *a, **k):
        import asyncio as _asyncio
        if _FakeClientSession.raise_exc is not None:
            raise _FakeClientSession.raise_exc
        return _FakeClientSession._Post(_FakeClientSession.response)


def _install_fake_aiohttp(monkeypatch):
    import sys, types
    fake = types.ModuleType("aiohttp")
    fake.ClientSession = _FakeClientSession
    fake.ClientTimeout = lambda **k: None
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    _FakeClientSession.raise_exc = None


def test_post_webhook_http_error_raises(monkeypatch):
    _install_fake_aiohttp(monkeypatch)
    _FakeClientSession.response = _FakeResponse(500, {"errcode": 0})
    from src.im.adapters.dingtalk.outbound import DingtalkWebhookError, _post_webhook
    with pytest.raises(DingtalkWebhookError):
        asyncio.run(_post_webhook("https://webhook", {"msgtype": "text"}))


def test_post_webhook_errcode_nonzero_raises(monkeypatch):
    """HTTP 200 但 errcode!=0（钉钉业务失败）→ raise，不得计成功。"""
    _install_fake_aiohttp(monkeypatch)
    _FakeClientSession.response = _FakeResponse(200, {"errcode": 310000, "errmsg": "sign error"})
    from src.im.adapters.dingtalk.outbound import DingtalkWebhookError, _post_webhook
    with pytest.raises(DingtalkWebhookError) as ei:
        asyncio.run(_post_webhook("https://webhook", {"msgtype": "text"}))
    assert "310000" in str(ei.value)


def test_post_webhook_transport_exception_raises(monkeypatch):
    _install_fake_aiohttp(monkeypatch)
    _FakeClientSession.raise_exc = OSError("conn reset")
    from src.im.adapters.dingtalk.outbound import DingtalkWebhookError, _post_webhook
    with pytest.raises(DingtalkWebhookError):
        asyncio.run(_post_webhook("https://webhook", {"msgtype": "text"}))


def test_post_webhook_success_returns_message_id(monkeypatch):
    _install_fake_aiohttp(monkeypatch)
    _FakeClientSession.response = _FakeResponse(200, {"errcode": 0, "messageId": "dt_1"})
    from src.im.adapters.dingtalk.outbound import _post_webhook
    assert asyncio.run(_post_webhook("https://webhook", {"msgtype": "text"})) == "dt_1"


def test_webhook_failure_counts_failed_via_sender(monkeypatch):
    """传输层失败 → DingtalkSender 返回 ok=False（可被 CountingSender 计 failed）。"""
    _install_fake_aiohttp(monkeypatch)
    _FakeClientSession.raise_exc = OSError("boom")
    from src.im.adapters.dingtalk.outbound import WebhookRegistry
    from src.im.adapters.dingtalk.outbound import (
        DingtalkSender, send_markdown_via_webhook, send_text_via_webhook)
    reg = WebhookRegistry()
    reg.register("cid_1", "https://webhook")
    sender = DingtalkSender(send_text_via_webhook, send_markdown_via_webhook)
    res = asyncio.run(sender.send(OutboundMessage(chat_id="cid_1", text="hi")))
    assert res.ok is False and res.error


def test_webhook_registry_evicts_oldest_beyond_capacity():
    """容量上限：超出 max_entries 淘汰最早登记条目（QA #936 Nice-to-have）。"""
    from src.im.adapters.dingtalk.outbound import WebhookRegistry
    reg = WebhookRegistry(max_entries=2)
    reg.register("c1", "https://w/1")
    reg.register("c2", "https://w/2")
    reg.register("c3", "https://w/3")   # c1 被淘汰
    assert reg.lookup("c1") == ""
    assert reg.lookup("c2") == "https://w/2"
    assert reg.lookup("c3") == "https://w/3"


def test_registry_meta_and_webhook_ttl():
    from src.im.adapters.dingtalk.outbound import WebhookRegistry
    reg = WebhookRegistry(ttl=0.01)
    reg.register("cid", "https://hook", staff_id="s1", chat_type="1")
    assert reg.lookup("cid") == "https://hook"
    assert reg.meta("cid") == {"staff_id": "s1", "chat_type": "1"}
    time.sleep(0.02)
    assert reg.lookup("cid") == "" and reg.meta("cid") == {}


def test_empty_text_is_failed_not_success():
    """空正文必须显式失败（钉钉对空 text 返回 errcode!=0 / 400），不得假成功。"""
    calls = []
    async def send_text(chat_id, text):
        calls.append(text)
        return "mid"

    sender = DingtalkSender(send_text=send_text, send_markdown=send_text)
    import asyncio
    from src.im.channel.message import OutboundMessage
    res = asyncio.run(sender.send(OutboundMessage(chat_id="cid", text="")))
    assert res.ok is False and "empty" in res.error
    assert calls == []          # 没有真的发出去


def test_string_card_kind_goes_action_card():
    """kind="card" 的字符串卡片（egress.reply_card 路径，如 /where）必须按卡片发，
    不能被当成普通文本丢掉标题（2026-11-05 钉钉实机：/where 回成一条纯文字）。"""
    calls = []
    async def send_text(chat_id, text):
        calls.append(("text", text))
        return "mid_text"
    async def send_action_card(chat_id, title, text, btns=None, **kw):
        calls.append(("card", title, text, list(btns or [])))
        return "mid_card"

    sender = DingtalkSender(send_text=send_text, send_markdown=send_text,
                            send_action_card=send_action_card)
    import asyncio
    from src.im.channel.message import OutboundMessage
    out = OutboundMessage(chat_id="cid", text="**📍 当前位置**\n仓库：XOcto/deepseek-harness",
                          metadata={"kind": "card", "title": "DevOps Bot"})
    res = asyncio.run(sender.send(out))
    assert res.ok and res.message_id == "mid_card"
    # markdown 单换行不换行 → 发送前补「两空格硬换行」
    assert calls == [("card", "DevOps Bot", "**📍 当前位置**  \n仓库：XOcto/deepseek-harness", [])]


def test_string_card_without_transport_is_failed():
    """没有 actionCard 通道时必须显式失败，不得静默降级成文本。"""
    sender = DingtalkSender(send_text=lambda *a, **k: "mid", send_markdown=lambda *a, **k: "mid")
    import asyncio
    from src.im.channel.message import OutboundMessage
    res = asyncio.run(sender.send(OutboundMessage(
        chat_id="cid", text="**📍 当前位置**", metadata={"kind": "card", "title": "DevOps Bot"})))
    assert res.ok is False and "actionCard" in res.error
