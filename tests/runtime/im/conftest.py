"""tests/im 共享 fakes（QA #1314 S9：test_notify_outbox 与 test_im_notify_fanout
原先各自重复定义 FakeChannel/FakeSup/FakeEngine，收敛到本 conftest）。"""
from __future__ import annotations

import pytest

from src.domains.runtime.im.channel.capability import Capability
from src.domains.runtime.im.channel.message import SendResult


class FakeChannel:
    """最小 Channel 替身：能力位固定、send 结果可脚本化。"""

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
    def __init__(self, mapping, instance_id=""):
        self.supervisor = FakeSup(mapping)
        self.instance_id = instance_id


@pytest.fixture()
def fake_channel_factory():
    """按需构造 FakeChannel 的工厂（复杂用例自定义 caps/results）。"""
    return FakeChannel
