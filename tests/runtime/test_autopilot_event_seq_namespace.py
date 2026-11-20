"""Autopilot event-seq 高位命名空间隔离的看护用例（PR #1864 原子回归 BH10）。

背景：autopilot 执行不写 events 表，但 agent_tasks.event_seq 必须非 0 且不能与
真实事件序号碰撞——历史实现直接传 exec_id，重放/已有 events 的库会命中旧事件
状态，被 executor 误判为 event:failed 而提前 kill。修复改用 900M+ 高位命名空间；
本用例锁死该映射，防止后续改动把 autopilot 序号退回真实事件区间。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.engine.scheduler import (  # noqa: E402
    _AUTOPILOT_EVENT_SEQ_BASE,
    _autopilot_event_seq,
)


def test_autopilot_event_seq_uses_isolated_high_namespace():
    assert _AUTOPILOT_EVENT_SEQ_BASE >= 900_000_000, (
        "autopilot event_seq 必须落在 900M+ 高位命名空间，避开真实 events 序号"
    )
    assert _autopilot_event_seq(0) == _AUTOPILOT_EVENT_SEQ_BASE
    assert _autopilot_event_seq(1) == _AUTOPILOT_EVENT_SEQ_BASE + 1
    assert _autopilot_event_seq(12345) == _AUTOPILOT_EVENT_SEQ_BASE + 12345


def test_autopilot_event_seq_is_strictly_increasing_and_nonzero():
    prev = 0
    for exec_id in (0, 1, 2, 99, 1000):
        seq = _autopilot_event_seq(exec_id)
        assert seq > 0
        assert seq > prev
        prev = seq