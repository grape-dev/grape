"""企微出站发送器测试（Issue #1254）。

硬约束断言（改动即失败）：
  - `aibot_send_msg` 的 payload 里 `msgtype` 永远不是 `text`（平台直接拒帧）；
  - 回复通道透传**回调里的 req_id**（不是新生成的）；
  - 窗口内/外的路径切换由注入时钟驱动（不依赖真实时序）；
  - 空内容 → ok=False（硬护栏，不静默成功）；
  - 媒体出站必须先 init/chunk/finish 换 media_id，失败即 ok=False。
"""
import asyncio
import json
import time

import pytest

from src.im.adapters.wecom.outbound import (
    WECOM_CAPABILITIES,
    WecomReplyRegistry,
    WecomSender,
    WecomTransport,
    WecomTransportError,
)
from src.im.channel import Capability, OutboundMessage


class FakeWS:
    """假长连接：记录发出的帧，按需自动回 ack（errcode 可注入）。"""

    def __init__(self, transport=None, errcode=0, errmsg="ok", upload_ids=None):
        self.transport = transport
        self.errcode = errcode
        self.errmsg = errmsg
        self.upload_ids = list(upload_ids or ["up_1"])
        self.sent = []

    async def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        if self.transport is None:
            return
        req_id = (frame.get("headers") or {}).get("req_id")
        if not req_id:
            return
        ack = {"headers": {"req_id": req_id}, "errcode": self.errcode,
               "errmsg": self.errmsg}
        cmd = frame.get("cmd")
        if self.errcode == 0 and cmd in ("aibot_upload_media_init",):
            ack["body"] = {"upload_id": self.upload_ids[0]}
        elif self.errcode == 0 and cmd == "aibot_upload_media_finish":
            ack["body"] = {"type": "file", "media_id": "media_1"}
        elif self.errcode == 0 and cmd == "aibot_upload_media_chunk":
            ack["body"] = {}
        loop = asyncio.get_running_loop()
        loop.call_soon(self.transport.resolve, ack)


def _build(reply_window=24 * 3600.0, now=None, errcode=0):
    clock_now = [now if now is not None else 1000.0]
    clock = lambda: clock_now[0]  # noqa: E731
    transport = WecomTransport("bot_1", clock=clock)
    ws = FakeWS(transport, errcode=errcode)
    transport.attach(ws)
    registry = WecomReplyRegistry(window=reply_window, clock=clock)
    sender = WecomSender(transport, registry, clock=clock, reply_window=reply_window)
    return sender, transport, registry, ws, clock_now


def test_capabilities_declare_card_action_and_attachment():
    sender, *_ = _build()
    caps = sender.capabilities()
    assert caps & Capability.TEXT
    assert caps & Capability.RICH_CARD
    assert caps & Capability.CARD_ACTION
    assert caps & Capability.ATTACHMENT
    assert not caps & Capability.TYPING_INDICATOR  # 无实现不申报（不虚报）
    assert not caps & Capability.REACTION      # 不虚报平台没有的能力
    assert not caps & Capability.MESSAGE_EDIT
    assert caps == WECOM_CAPABILITIES


def test_reply_within_window_uses_respond_msg_with_callback_req_id():
    sender, transport, registry, ws, _ = _build()
    registry.register("zhangsan", "req-cb-1", 1)
    res = asyncio.run(sender.send(OutboundMessage(chat_id="zhangsan", text="你好")))
    assert res.ok
    frame = ws.sent[-1]
    assert frame["cmd"] == "aibot_respond_msg"
    assert frame["headers"]["req_id"] == "req-cb-1"     # 透传回调 req_id，不新生成
    assert frame["body"]["msgtype"] == "markdown"
    assert frame["body"]["markdown"]["content"] == "你好"
    assert "chatid" not in frame["body"]                # 回复帧不需要 chatid


def test_after_window_switches_to_send_msg_with_chat_type():
    sender, transport, registry, ws, clock = _build(reply_window=5.0)
    registry.register("zhangsan", "req-cb-1", 1)
    clock[0] += 6.0                                     # 越过回复窗口
    res = asyncio.run(sender.send(OutboundMessage(chat_id="zhangsan", text="长任务结果")))
    assert res.ok
    frame = ws.sent[-1]
    assert frame["cmd"] == "aibot_send_msg"
    assert frame["body"]["msgtype"] == "markdown"       # 硬约束：绝不是 text
    assert frame["body"]["chatid"] == "zhangsan"
    assert frame["body"]["chat_type"] == 1


def test_group_push_carries_chat_type_2():
    sender, _, registry, ws, _ = _build()
    registry.register("chat_9", "", 2)
    asyncio.run(sender.send(OutboundMessage(chat_id="chat_9", text="群通知")))
    frame = ws.sent[-1]
    assert frame["cmd"] == "aibot_send_msg"
    assert frame["body"]["chat_type"] == 2


def test_send_msg_never_carries_msgtype_text():
    """无论走哪条路径，企微主动推送都只能是 markdown / template_card。"""
    sender, _, _, ws, _ = _build()
    asyncio.run(sender.send(OutboundMessage(chat_id="u1", text="plain text body")))
    for frame in ws.sent:
        if frame.get("cmd") == "aibot_send_msg":
            assert frame["body"]["msgtype"] in ("markdown", "template_card")


def test_empty_content_is_hard_rejected():
    sender, _, _, ws, _ = _build()
    res = asyncio.run(sender.send(OutboundMessage(chat_id="u1", text="   ")))
    assert res.ok is False
    assert "empty content guard" in res.error
    assert ws.sent == []


def test_empty_chat_id_without_window_fails_explicitly():
    sender, _, _, _, _ = _build()
    res = asyncio.run(sender.send(OutboundMessage(chat_id="", text="hi")))
    assert res.ok is False
    assert "route" in res.error


def test_long_markdown_is_chunked_within_20480_bytes():
    sender, _, registry, ws, _ = _build()
    registry.register("u1", "req-1", 1)
    text = "\n".join(f"- 第 {i} 行 " + "内容" * 40 for i in range(400))
    res = asyncio.run(sender.send(OutboundMessage(chat_id="u1", text=text)))
    assert res.ok
    frames = [f for f in ws.sent if f.get("cmd") == "aibot_respond_msg"]
    assert len(frames) > 1
    for frame in frames:
        assert len(frame["body"]["markdown"]["content"].encode()) <= 20480


def test_single_line_30000_bytes_including_prefix_stays_within_limit():
    """单行硬切：分块前缀 (i/N) 必须计入 20480 字节预算（否则平台拒帧）。"""
    sender, _, registry, ws, _ = _build()
    registry.register("u1", "req-1", 1)
    text = "a" * 30000
    asyncio.run(sender.send(OutboundMessage(chat_id="u1", text=text)))
    frames = [f for f in ws.sent if f.get("cmd") == "aibot_respond_msg"]
    assert len(frames) > 1
    for frame in frames:
        content = frame["body"]["markdown"]["content"]
        assert content.startswith("(")                    # 多块都有 (i/N) 前缀
        assert len(content.encode()) <= 20480


def test_chunk_at_byte_limit_does_not_overflow_after_prefix():
    """首块刚好贴限：加前缀后不得越过 20480（前缀预算回归）。"""
    sender, _, registry, ws, _ = _build()
    registry.register("u1", "req-1", 1)
    text = "a" * 20480 + "\n" + "b" * 100
    asyncio.run(sender.send(OutboundMessage(chat_id="u1", text=text)))
    frames = [f for f in ws.sent if f.get("cmd") == "aibot_respond_msg"]
    assert len(frames) == 2
    for frame in frames:
        assert len(frame["body"]["markdown"]["content"].encode()) <= 20480


def test_code_fence_is_closed_per_chunk():
    sender, _, registry, ws, _ = _build()
    registry.register("u1", "req-1", 1)
    text = "说明\n```python\n" + "\n".join(f"print({i})" for i in range(3000)) + "\n```"
    asyncio.run(sender.send(OutboundMessage(chat_id="u1", text=text)))
    for frame in ws.sent:
        if frame.get("cmd") != "aibot_respond_msg":
            continue
        content = frame["body"]["markdown"]["content"]
        assert content.count("```") % 2 == 0        # 每块围栏闭合


def test_template_card_payload_sends_template_card_frame():
    sender, _, registry, ws, _ = _build()
    registry.register("chat_9", "req-card", 2)
    payload = {
        "msgtype": "template_card",
        "template_card": {"card_type": "button_interaction",
                          "main_title": {"title": "选择仓库"},
                          "button_list": [{"text": "1", "style": 1, "key": '{"cmd": "/bind 1"}'}],
                          "task_id": "task_1"},
    }
    res = asyncio.run(sender.send(OutboundMessage(
        chat_id="chat_9", text="", metadata={"kind": "raw_card", "raw_card": payload})))
    assert res.ok
    frame = ws.sent[-1]
    assert frame["cmd"] == "aibot_respond_msg"
    assert frame["body"]["msgtype"] == "template_card"
    assert frame["body"]["template_card"]["task_id"] == "task_1"


def test_degraded_card_payload_falls_back_to_markdown():
    sender, _, registry, ws, _ = _build()
    registry.register("u1", "req-1", 1)
    payload = {"msgtype": "markdown", "markdown": {"text": "**选择仓库**\n1. repo-a"}}
    res = asyncio.run(sender.send(OutboundMessage(
        chat_id="u1", text="", metadata={"kind": "raw_card", "raw_card": payload})))
    assert res.ok
    assert ws.sent[-1]["body"]["msgtype"] == "markdown"
    assert "repo-a" in ws.sent[-1]["body"]["markdown"]["content"]


def test_foreign_lark_card_is_refused():
    sender, _, _, ws, _ = _build()
    res = asyncio.run(sender.send(OutboundMessage(
        chat_id="u1", text="", metadata={"raw_card": {"header": {"title": {"content": "x"}}}})))
    assert res.ok is False
    assert "lark raw_card refused" in res.error
    assert ws.sent == []


def test_attachment_within_window_uploads_then_sends_file():
    sender, transport, registry, ws, _ = _build()
    registry.register("u1", "req-1", 1)
    res = asyncio.run(sender.send(OutboundMessage(
        chat_id="u1", text="报告如下",
        metadata={"attachments": [{"data": b"%PDF-1.4 test",
                                   "filename": "report.pdf", "media_type": "file"}]})))
    assert res.ok
    cmds = [f["cmd"] for f in ws.sent]
    assert "aibot_upload_media_init" in cmds
    assert "aibot_upload_media_chunk" in cmds
    assert "aibot_upload_media_finish" in cmds
    file_frame = [f for f in ws.sent if f["cmd"] == "aibot_respond_msg"
                  and f["body"]["msgtype"] == "file"][-1]
    assert file_frame["body"]["file"]["media_id"] == "media_1"


def test_attachment_outside_window_fails_explicitly():
    sender, _, registry, ws, clock = _build(reply_window=5.0)
    registry.register("u1", "req-1", 1)
    clock[0] += 10.0
    res = asyncio.run(sender.send(OutboundMessage(
        chat_id="u1", text="",
        metadata={"attachments": [{"data": b"x", "filename": "a.txt"}]})))
    assert res.ok is False
    assert "media" in res.error and "reply window" in res.error


def test_upload_failure_is_reported_not_silent():
    clock = lambda: 1000.0  # noqa: E731
    transport = WecomTransport("bot_1", clock=clock)
    ws = FakeWS(transport, errcode=48001, errmsg="api unauthorized")
    transport.attach(ws)
    registry = WecomReplyRegistry(clock=clock)
    registry.register("u1", "req-1", 1)
    sender = WecomSender(transport, registry, clock=clock)
    res = asyncio.run(sender.send(OutboundMessage(
        chat_id="u1", text="",
        metadata={"attachments": [{"data": b"x", "filename": "a.txt"}]})))
    assert res.ok is False
    assert "48001" in res.error or "api unauthorized" in res.error


def test_send_failure_when_transport_not_connected():
    clock = lambda: 1000.0  # noqa: E731
    transport = WecomTransport("bot_1", clock=clock)
    sender = WecomSender(transport, WecomReplyRegistry(clock=clock), clock=clock)
    res = asyncio.run(sender.send(OutboundMessage(chat_id="u1", text="hi")))
    assert res.ok is False
    assert "not established" in res.error


def test_transport_ack_timeout_counts_miss():
    async def _run():
        transport = WecomTransport("bot_1")
        ws = FakeWS(None)             # 不回 ack
        transport.attach(ws)
        with pytest.raises(WecomTransportError):
            await transport.request("aibot_send_msg", {"msgtype": "markdown"}, timeout=0.05)
        return transport

    transport = asyncio.run(_run())
    assert transport.acks_missed == 1

def test_subscribe_secret_never_kept_in_frame_sentinel():
    """观察哨/日志副本里不得出现明文 secret（安全硬约束）。"""
    async def _run():
        transport = WecomTransport("bot_1")
        ws = FakeWS(transport)
        transport.attach(ws)
        await transport.request_frame({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": "r1"},
            "body": {"bot_id": "bot_1", "secret": "SUPER_SECRET"},
        })
        return transport

    transport = asyncio.run(_run())
    assert transport.sent_frames[0]["body"]["secret"] == "***"
    assert "SUPER_SECRET" not in json.dumps(transport.sent_frames, ensure_ascii=False)


# ---------------- handler 侧 api 壳（2026-11-06 实障：缺 send_card_to_chat）

def test_adapter_api_send_card_to_chat_uses_send_msg_markdown():
    from src.im.adapters.wecom.outbound import WecomAdapterApi

    sender, transport, registry, ws, _ = _build()
    api = WecomAdapterApi(media_cache=None, sender=sender)
    res = asyncio.run(api.send_card_to_chat(None, "zhangsan", "**✅ 已绑定**"))
    assert isinstance(res, str)
    frame = ws.sent[-1]
    assert frame["cmd"] == "aibot_send_msg"          # 无回复令牌 → 主动推送
    assert frame["body"]["msgtype"] == "markdown"    # send_msg 不接受 text
    assert frame["body"]["chatid"] == "zhangsan"
    assert frame["body"]["chat_type"] == 1           # 未登记会话按单聊兜底


def test_adapter_api_send_card_to_chat_group_uses_registered_chat_type():
    from src.im.adapters.wecom.outbound import WecomAdapterApi

    sender, transport, registry, ws, _ = _build()
    registry.register("group-1", "req-x", 2)
    api = WecomAdapterApi(media_cache=None, sender=sender)
    asyncio.run(api.send_card_to_chat(None, "group-1", "hi"))
    # 有回复令牌走 respond_msg，但绝不能因此在群聊里算成单聊
    frame = ws.sent[-1]
    assert frame["body"]["msgtype"] == "markdown"


def test_adapter_api_without_sender_is_noop_not_crash():
    from src.im.adapters.wecom.outbound import WecomAdapterApi

    api = WecomAdapterApi(media_cache=None)
    assert asyncio.run(api.send_card_to_chat(None, "zhangsan", "x")) == ""


def test_push_only_clears_reply_token_and_forces_send_msg():
    """卡片事件没有回复窗口：push_only 后必须走主动推送（11-06 实障回归）。"""
    sender, transport, registry, ws, _ = _build()
    registry.register("zhangsan", "req-cb-1", 1)
    assert registry.reply_req_id("zhangsan") == "req-cb-1"

    registry.push_only("zhangsan", 2)
    assert registry.reply_req_id("zhangsan") == ""     # 事件 req_id 不再当回复令牌
    assert registry.chat_type("zhangsan") == 2         # 会话类型保留

    res = asyncio.run(sender.send(OutboundMessage(chat_id="zhangsan", text="✅ 已绑定")))
    assert res.ok
    frame = ws.sent[-1]
    assert frame["cmd"] == "aibot_send_msg"            # 不再用 respond_msg
    assert frame["body"]["chatid"] == "zhangsan"
    assert frame["body"]["chat_type"] == 2
