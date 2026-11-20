"""Issue #2791（切片 D / Spec 验收「#1982 摘出、恢复时序不回退」）。

背景：首探时延修复（P1-13）把「安装完成」后的探测从「注册时 1 次 + 600s 周期」
改成「立即 + 60s 窗口内指数退避」——**探测频率变高**。这直接压到 #1982 的
摘出/恢复时序上：若窗口内的连续失败被误判成「偶发」，或一次 ok 不能立刻恢复，
引擎就会出现「摘出了却回不来 / 一次抖动就摘出」的回退。

这里锁的是服务端 ingest 的时序契约（worker 侧只改频率，不改语义）：
- 连续失败第 1 次：不摘出（偶发不摘除）；
- 连续失败第 2 次（>=2）：healthy=0 摘出；
- 任意一次 ok：healthy=1 恢复，并把连续失败计数清零；
- 同一观测重放（last_probe 相同）：幂等，不重复计数（#490）。
"""
import os
import sys
import uuid
from datetime import datetime, timezone

from probe_state_support import (  # noqa: E402  共用 #490/#1982/#2791 探活脚手架
    FakeRequest as _FakeRequest,
    cleanup as _cleanup,
    heartbeat as _heartbeat,
    insert_credential as _insert_credential,
    insert_runtime as _insert_runtime,
    observation as _obs,
    state as _state,
)


class TestReapRestoreTiming:
    """摘出/恢复时序：#1982 语义不因首探变密而回退。"""

    def test_single_failure_does_not_reap(self):
        cid = _insert_credential()
        name = f"engine-2791-{uuid.uuid4().hex[:8]}"
        try:
            rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
            _heartbeat(_obs(name, "fail", 1), [rid], cid)
            healthy, status, count, _err = _state(name, cid)
            assert status == "fail"
            assert count == 1, "首探窗口的第 1 次失败只计数，不应升级"
            assert healthy == 1, "偶发一次失败不得摘出（#1982）"
        finally:
            _cleanup(cid)

    def test_second_consecutive_failure_reaps(self):
        cid = _insert_credential()
        name = f"engine-2791-{uuid.uuid4().hex[:8]}"
        try:
            rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
            _heartbeat(_obs(name, "fail", 1), [rid], cid)
            _heartbeat(_obs(name, "fail", 2), [rid], cid)
            healthy, status, count, _err = _state(name, cid)
            assert count == 2
            assert status == "fail"
            assert healthy == 0, "连续第 2 次失败必须摘出（probe_fail_count>=2）"
        finally:
            _cleanup(cid)

    def test_one_ok_restores_immediately_even_inside_window(self):
        """窗口内退避重试拿到 ok：立刻恢复（不等到 600s 周期才回血）。"""
        cid = _insert_credential()
        name = f"engine-2791-{uuid.uuid4().hex[:8]}"
        try:
            rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
            for _i in (1, 2):
                _heartbeat(_obs(name, "fail", _i), [rid], cid)
            assert _state(name, cid)[0] == 0, "前置：应已摘出"
            _heartbeat(_obs(name, "ok", 3), [rid], cid)
            healthy, status, count, err = _state(name, cid)
            assert status == "ok"
            assert healthy == 1, "一次 ok 必须恢复 healthy"
            assert count == 0, "ok 必须清零连续失败计数（下一轮重新计数）"
            assert not err, "恢复后不留错误文案"
        finally:
            _cleanup(cid)

    def test_replayed_same_observation_is_idempotent(self):
        """同一观测重放不重复计数（#490 去重）——首探窗口不会因心跳重放而误摘出。"""
        cid = _insert_credential()
        name = f"engine-2791-{uuid.uuid4().hex[:8]}"
        try:
            rid = _insert_runtime(cid, name, healthy=1, probe_fail_count=0)
            same = _obs(name, "fail", 1)
            for _ in range(3):
                _heartbeat(same, [rid], cid)
            healthy, status, count, _err = _state(name, cid)
            assert count == 1, f"同一观测重放不得累加，实际 {count}"
            assert healthy == 1, "重放不得触发摘出"
        finally:
            _cleanup(cid)
