"""IM 通知扇出（A4）回归测试：任务分配 / 待澄清 / 待审核 / 完成 → 全部已连接渠道。

覆盖：
  1. 多渠道都收到（RICH_CARD 渠道发卡片，无卡片渠道降级文本）
  2. 离线渠道跳过，其余照发（一渠失败不影响其他渠）
  3. 单渠失败重试后成功
  4. 无 p2p 绑定 → 跳过并计数（不误发空 chat_id）
  5. 同一事件去重（防 agent 重试风暴）
  6. 文本降级登记 pending → 回数字接入任务会话
  7. 找不到归属用户的 issue 事件 → 不发
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.im import notify as N
from src.im.channel.capability import Capability
from src.im.channel.message import SendResult


class FakeChannel:
    def __init__(self, caps=Capability.TEXT, results=None):
        self.caps = caps
        self.sent = []
        self.calls = 0
        self._results = list(results or [])

    def capabilities(self):
        return self.caps

    async def send(self, out):
        self.calls += 1
        self.sent.append(out)
        if self._results:
            return self._results.pop(0)
        return SendResult(ok=True, message_id=f"m{self.calls}")


class FakeSup:
    def __init__(self, mapping):
        self._channels = {k: (v, None) for k, v in mapping.items()}


class FakeEngine:
    def __init__(self, mapping):
        self.supervisor = FakeSup(mapping)


@pytest.fixture(autouse=True)
def _clean():
    N._reset_for_tests()
    yield
    N._reset_for_tests()


TARGETS = [
    {"installation_id": "inst-feishu", "channel_type": "feishu", "chat_id": "oc_fs"},
    {"installation_id": "inst-wechat", "channel_type": "wechat", "chat_id": "o9_wx"},
    {"installation_id": "inst-slack", "channel_type": "slack", "chat_id": "D0sl"},
]


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _engine_all():
    return FakeEngine({
        "inst-feishu": FakeChannel(Capability.TEXT | Capability.RICH_CARD),
        "inst-wechat": FakeChannel(Capability.TEXT),          # 微信：纯文本 + 编号
        "inst-slack": FakeChannel(Capability.TEXT | Capability.RICH_CARD),
    })


def test_fanout_all_channels(monkeypatch):
    eng = _engine_all()
    pending = {}
    import src.im.handler_async as h
    monkeypatch.setattr(h, "_set_pending_selection", lambda cid, p: pending.update({cid: p}))

    res = _run(N.notify_user(1, "pending_review", "请确认交付审核点",
                             repo_key="gitcode:o/r", issue_number=42,
                             conv_id="conv-1", targets=TARGETS, engine=eng))
    assert set(res["sent"]) == {"feishu", "wechat", "slack"}
    assert res["failed"] == []
    for k in ("inst-feishu", "inst-wechat", "inst-slack"):
        ch = eng.supervisor._channels[k][0]
        assert len(ch.sent) == 1, k
        assert ch.sent[0].chat_id in ("oc_fs", "o9_wx", "D0sl")
    # 卡片渠道：raw_card 载荷
    fe = eng.supervisor._channels["inst-feishu"][0].sent[0]
    assert isinstance(fe.metadata.get("raw_card"), dict)
    # 微信：文本降级 + 编号选项 + 登记 pending
    wx = eng.supervisor._channels["inst-wechat"][0].sent[0]
    assert not wx.metadata.get("raw_card")
    assert "1." in wx.text and ("回复数字" in wx.text or "进入任务会话" in wx.text)
    assert pending.get("o9_wx", {}).get("action") == "chat"
    assert pending["o9_wx"]["items"][0]["id"] == "conv-1"
    assert N.stats()["sent_ok"] == 3
    assert N.stats()["degraded_text"] == 1


def test_offline_channel_skipped_others_delivered():
    eng = FakeEngine({  # 企微没连上
        "inst-wechat": FakeChannel(Capability.TEXT),
    })
    targets = [
        {"installation_id": "inst-wechat", "channel_type": "wechat", "chat_id": "o9_wx"},
        {"installation_id": "inst-wecom", "channel_type": "wecom", "chat_id": "Ma"},
    ]
    res = _run(N.notify_user(1, "task", "你有一条新任务", targets=targets, engine=eng))
    assert res["sent"] == ["wechat"]
    assert res["skipped"]["wecom"] == "offline"
    assert N.stats()["skipped_offline"] == 1


def test_retry_then_success(monkeypatch):
    monkeypatch.setattr(N, "_RETRY_BACKOFF", 0)
    ch = FakeChannel(Capability.TEXT, results=[SendResult(ok=False, error="429"), SendResult(ok=True)])
    eng = FakeEngine({"inst-wechat": ch})
    res = _run(N.notify_user(1, "awaiting", "需要补充信息", issue_number=7,
                             targets=[{"installation_id": "inst-wechat", "channel_type": "wechat",
                                       "chat_id": "o9"}], engine=eng))
    assert res["sent"] == ["wechat"]
    assert ch.calls == 2


def test_all_retries_fail_isolated(monkeypatch):
    monkeypatch.setattr(N, "_RETRY_BACKOFF", 0)
    bad = FakeChannel(Capability.TEXT, results=[SendResult(ok=False, error="500"),
                                                SendResult(ok=False, error="500")])
    good = FakeChannel(Capability.TEXT)
    eng = FakeEngine({"inst-bad": bad, "inst-good": good})
    res = _run(N.notify_user(1, "done", "任务完成", targets=[
        {"installation_id": "inst-bad", "channel_type": "wechat", "chat_id": "a"},
        {"installation_id": "inst-good", "channel_type": "wecom", "chat_id": "b"},
    ], engine=eng))
    assert res["failed"] == ["wechat"]
    assert res["sent"] == ["wecom"]
    assert len(good.sent) == 1, "一个渠道失败不得影响其他渠道"


def test_no_binding_skipped():
    eng = _engine_all()
    res = _run(N.notify_user(1, "task", "x", targets=[
        {"installation_id": "inst-ghost", "channel_type": "telegram", "chat_id": ""},
    ], engine=eng))
    assert res["sent"] == []
    assert res["skipped"]["telegram"] == "no_binding"
    assert N.stats()["skipped_no_binding"] == 1


def test_dedupe_same_event():
    eng = _engine_all()
    first = _run(N.notify_user(1, "awaiting", "q1", issue_number=9, targets=TARGETS, engine=eng))
    second = _run(N.notify_user(1, "awaiting", "q1", issue_number=9, targets=TARGETS, engine=eng))
    assert len(first["sent"]) == 3
    assert second.get("skipped") == "dedupe"
    assert N.stats()["deduped"] == 1


def test_dedupe_key_differs_by_issue():
    eng = _engine_all()
    _run(N.notify_user(1, "awaiting", "q", issue_number=1, targets=TARGETS, engine=eng))
    res = _run(N.notify_user(1, "awaiting", "q", issue_number=2, targets=TARGETS, engine=eng))
    assert len(res["sent"]) == 3


def test_issue_event_without_owner_is_noop(monkeypatch):
    monkeypatch.setattr(N, "_issue_owner", lambda repo, num: (0, "", ""))
    called = []
    monkeypatch.setattr(N, "schedule_notify", lambda *a, **kw: called.append(a))
    N.notify_issue_event("gitcode:o/r", 5, "done", "完成")
    assert called == []


def test_card_contains_issue_and_repo():
    card = N.build_card("pending_review", text="审核点：A/B", repo_key="gitcode:owner/repo",
                        issue_number=12, conv_id="c1")
    from src.im.channel.card import render_text
    txt = render_text(card)
    assert "#12" in txt and "owner/repo" in txt and "审核点" in txt
    assert "task-open" in txt            # 动作命令提示保留
    assert "gitcode.com/owner/repo/issues/12" in txt