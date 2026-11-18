"""通知扇出并发回归（2026-11-18）。

现象：待审核通知微信先到、飞书晚 1~2 秒，用户体感「不是同时收到」。
原因：notify_user 逐渠道顺序 await；outbox 消费者逐行顺序 await。
契约：多渠投递必须并发（同一 (installation, chat) 内保序）。
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine.agent import _backend_for_engine  # noqa: F401,E402
from src.domains.runtime.im import notify as notify_mod  # noqa: E402


@pytest.mark.asyncio
async def test_fanout_delivers_channels_concurrently(monkeypatch):
    """两个渠道的投递时间窗必须重叠（顺序 await 时是首尾相接）。"""
    spans = []

    async def _fake_handle(target, **kw):
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(0.3)
        spans.append((target["channel_type"], start, asyncio.get_running_loop().time()))

    # 6 个渠道（与前端渠道列表一致）：并发层不区分渠道类型，必须全部重叠
    types = ["feishu", "wecom", "dingtalk", "slack", "telegram", "wechat"]
    targets = [{"installation_id": f"inst-{t}", "channel_type": t} for t in types]
    monkeypatch.setattr(notify_mod, "_handle_target", _fake_handle)

    await notify_mod.notify_user(1, "pending_review", "x", targets=targets, dedupe=False)

    assert len(spans) == len(types)
    assert {s[0] for s in spans} == set(types)
    spans.sort(key=lambda s: s[1])
    first_end = spans[0][2]
    second_start = spans[1][1]
    assert second_start < first_end, f"渠道投递没有重叠（仍顺序）: {spans}"
    # 并发时总耗时 ≈ 单渠耗时，而不是 6 倍
    total = max(s[2] for s in spans) - min(s[1] for s in spans)
    assert total < 0.6, f"6 渠总耗时 {total:.2f}s，疑似仍串行"
