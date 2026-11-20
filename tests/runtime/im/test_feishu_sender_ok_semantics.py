"""飞书出站 SendResult 语义回归（2026-11-18）。

实障：待审核主动通知在飞书静默丢失 —— 底层 legacy 发送返回空串（失败），
`FeishuSender.send` 却恒返回 ok=True，outbox 因此把行记成 sent（监控显示正常、实际没发）。
契约：底层返回空 = 未发出 → ok=False；返回 "ok"/msg_id = 成功。
"""
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine.agent import _backend_for_engine  # noqa: F401,E402
from src.domains.runtime.im.adapters.feishu.outbound import FeishuSender  # noqa: E402
from src.domains.runtime.im.channel.message import OutboundMessage  # noqa: E402


def _out(text="待审核：请确认验收点", metadata=None):
    return OutboundMessage(chat_id="oc_test", text=text, metadata=metadata or {})


@pytest.mark.asyncio
async def test_empty_result_means_not_sent():
    """底层返回空串（404/权限失败等）→ ok 必须为 False。"""
    async def _fail(*a, **k):
        return ""

    sender = FeishuSender(_fail, _fail)
    res = await sender.send(_out(metadata={"kind": "text"}))
    assert res.ok is False
    assert res.error


@pytest.mark.asyncio
async def test_nonempty_result_means_sent():
    async def _ok(*a, **k):
        return "om_x_sent"

    sender = FeishuSender(_ok, _ok)
    res = await sender.send(_out())
    assert res.ok is True
    assert res.message_id == "om_x_sent"


@pytest.mark.asyncio
async def test_exception_still_not_ok():
    async def _boom(*a, **k):
        raise RuntimeError("network down")

    sender = FeishuSender(_boom, _boom)
    res = await sender.send(_out())
    assert res.ok is False
    assert "network down" in (res.error or "")
