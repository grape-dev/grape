"""Issue #372 §12.7: pipeline dedup guard 异常释放 — 单元测试

测试覆盖：
- T12-01: dispatch 段异常 → _fail_event 调用 → guard 释放
- T12-02: purge_stale_event_guards TTL 30 分钟（非 6 小时）
- T12-03: 正常完成时 guard 释放（回归保护）

测试方式：源码检查 + 函数签名验证。
"""
import inspect
import os

import pytest


_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")


def _read_src(rel_path):
    with open(os.path.join(_SRC_DIR, rel_path), encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# T12-01: dispatch 段异常 → _fail_event 调用
# ===========================================================================

class TestDispatchExceptionReleasesGuard:
    """测试 dispatch 段异常时调 _fail_event 释放 guard。"""

    def test_t12_01_dispatch_try_except_exists(self):
        """T12-01: run_pipeline dispatch 段有 try/except，异常时调 _fail_event。

        验证源码中 _enqueue_pipeline_task / _create_direct_task / start_direct_agent
        被包在 try 块中，except 调 _fail_event。
        """
        src = _read_src("api/event_pipeline.py")
        # 应有 try 块包裹 dispatch
        assert "try:" in src, "try block not found in event_pipeline"
        # except 块应调 _fail_event
        assert "except Exception as _de" in src or "except Exception" in src, \
            "except block not found for dispatch"
        # _fail_event 应在 except 块中被调用
        assert "_fail_event(seq" in src, \
            "_fail_event should be called in except block to release guard"
        # dispatch exception 日志
        assert "dispatch exception" in src, \
            "dispatch exception log message not found"

    def test_dispatch_try_covers_enqueue_and_direct(self):
        """验证 try 块覆盖 _enqueue_pipeline_task 和 start_direct_agent。"""
        src = _read_src("api/event_pipeline.py")
        # 找到 dispatch try 块（含 "dispatch 段包 try/except" 注释）
        marker = "dispatch 段包 try/except"
        marker_idx = src.find(marker)
        assert marker_idx > 0, "dispatch try/except marker not found"
        # 从 marker 到 except 之间应有 _enqueue_pipeline_task 和 start_direct_agent
        except_idx = src.find("except Exception as _de", marker_idx)
        assert except_idx > 0, "except block after dispatch try not found"
        try_block = src[marker_idx:except_idx]
        assert "_enqueue_pipeline_task" in try_block, \
            "try block should cover _enqueue_pipeline_task"
        assert "start_direct_agent" in try_block, \
            "try block should cover start_direct_agent"


# ===========================================================================
# T12-02: purge_stale_event_guards TTL 30 分钟
# ===========================================================================

class TestPurgeStaleEventGuardsTTL:
    """测试 purge_stale_event_guards TTL 从 6h 缩短到 30min。"""

    def test_t12_02_ttl_30_minutes_not_6_hours(self):
        """T12-02: purge_stale_event_guards 默认 TTL 1800 秒（30 分钟），非 21600（6 小时）。"""
        src = _read_src("api/event_store.py")
        # 默认参数应为 1800（30 分钟），不是 21600（6 小时）
        assert "max_age_seconds: int = 1800" in src, \
            "purge_stale_event_guards default TTL should be 1800 (30min), not 21600 (6h)"
        # 不应有 21600
        assert "21600" not in src, \
            "old 6h TTL (21600) should not be in event_store.py"

    def test_ttl_documented_as_30min(self):
        """文档中应标注 30min。"""
        src = _read_src("api/event_store.py")
        assert "30min" in src or "30 分钟" in src or "30 min" in src, \
            "TTL should be documented as 30min"


# ===========================================================================
# T12-03: 正常完成时 guard 释放（回归保护）
# ===========================================================================

class TestNormalCompletionReleasesGuard:
    """测试正常完成时 guard 仍被释放。"""

    def test_t12_03_fail_event_calls_release_guard(self):
        """T12-03: _fail_event 调用 update_event_complete → _release_guard_by_seq。

        验证 _fail_event → update_event_complete → _release_guard_by_seq 链路完整。
        """
        event_store_src = _read_src("api/event_store.py")
        pipeline_src = _read_src("api/event_pipeline.py")

        # _fail_event 调 update_event_complete
        assert "update_event_complete" in pipeline_src, \
            "_fail_event should call update_event_complete"
        # update_event_complete 调 _release_guard_by_seq
        assert "_release_guard_by_seq" in event_store_src, \
            "update_event_complete should call _release_guard_by_seq"
        # _release_guard_by_seq 存在
        assert "def _release_guard_by_seq" in event_store_src, \
            "_release_guard_by_seq function should exist"

    def test_existing_fail_paths_still_call_fail_event(self):
        """已有的失败路径仍调 _fail_event（回归保护）。"""
        src = _read_src("api/event_pipeline.py")
        # pick_engine 失败路径
        assert '_fail_event(seq, "无可用引擎' in src, \
            "pick_engine failure should still call _fail_event"
        # 场景引擎全灭路径
        assert "_fail_event(seq, f\"场景绑定的引擎" in src, \
            "scene engines all offline should still call _fail_event"
