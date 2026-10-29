"""Issue #516 M2：任务进度快照（checkpoint）单元测试。

覆盖 TaskCheckpoint 原子落盘/加载/清理/损坏降级、resume_context 格式化、
inject_resume 注入语义、TaskResourceStack.dispose 清理快照。
"""
import json
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.runtime.checkpoint import TaskCheckpoint, checkpoint_path, inject_resume
from src.runtime.resource_stack import TaskResourceStack


@pytest.fixture(autouse=True)
def _ckpt_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPE_CHECKPOINT_DIR", str(tmp_path / "ckpts"))
    yield tmp_path


class TestFlushLoad:
    def test_flush_then_load(self):
        ck = TaskCheckpoint("t-1", interval=1e9)
        ck.feed("step_start", "step one")
        ck.feed("tool_use", {"tool": "bash", "cmd": "ls"})
        ck.feed("text", "x" * 1000)  # 超长文本截断
        assert ck.flush() is True
        snap = ck.load()
        assert snap is not None
        assert snap["task_id"] == "t-1"
        assert snap["event_count"] == 3
        assert len(snap["events"]) == 3
        assert snap["events"][0]["type"] == "step_start"
        assert len(snap["events"][2]["text"]) <= 400

    def test_interval_autoflush(self):
        ck = TaskCheckpoint("t-2", interval=0.0)  # 每条都触发
        ck.feed("text", "a")
        snap = ck.load()
        assert snap is not None and snap["event_count"] == 1

    def test_tail_bounded(self):
        ck = TaskCheckpoint("t-3", interval=1e9, tail=5)
        for i in range(20):
            ck.feed("text", f"e{i}")
        ck.flush()
        snap = ck.load()
        assert len(snap["events"]) == 5
        assert snap["event_count"] == 20  # 总数仍全量计数
        assert snap["events"][-1]["text"] == "e19"

    def test_missing_file_load_none(self):
        ck = TaskCheckpoint("never-flushed", interval=1e9)
        assert ck.load() is None

    def test_corrupt_file_load_none(self):
        ck = TaskCheckpoint("t-4", interval=1e9)
        os.makedirs(os.path.dirname(ck.path), exist_ok=True)
        with open(ck.path, "w", encoding="utf-8") as f:
            f.write("{half-written")
        assert ck.load() is None

    def test_clear_idempotent(self):
        ck = TaskCheckpoint("t-5", interval=0.0)
        ck.feed("text", "x")
        assert os.path.exists(ck.path)
        ck.clear()
        ck.clear()
        assert not os.path.exists(ck.path)
        assert not os.path.exists(ck.path + ".tmp")

    def test_no_tmp_leftover_after_flush(self):
        ck = TaskCheckpoint("t-6", interval=0.0)
        ck.feed("text", "x")
        assert not os.path.exists(ck.path + ".tmp")

    def test_path_sanitization(self):
        p = checkpoint_path("../../etc/passwd")
        # 归一为目录内单文件名：不含路径分隔符（无目录穿越）
        assert "/" not in os.path.basename(p)
        assert "\\" not in os.path.basename(p)
        assert os.path.dirname(p) == os.path.dirname(checkpoint_path("safe-id"))


class TestResumeContext:
    def test_resume_context_contains_tail(self):
        ck = TaskCheckpoint("t-7", interval=1e9)
        ck.feed("step_start", "built feature X")
        ck.feed("tool_use", "ran tests")
        ck.flush()
        ctx = ck.resume_context()
        assert "built feature X" in ctx
        assert "ran tests" in ctx
        assert "timed out" in ctx

    def test_resume_context_empty_without_snapshot(self):
        ck = TaskCheckpoint("t-8", interval=1e9)
        assert ck.resume_context() == ""


class TestInjectResume:
    def test_no_marker_no_injection(self):
        ck = TaskCheckpoint("t-9", interval=1e9)
        ck.feed("text", "progress")
        ck.flush()
        env, prompt = {}, "DO THE TASK"
        p2, resumed = inject_resume({"_grape_resume_attempt": 1}, env, prompt, ck)
        assert resumed is False and p2 == prompt and env == {}

    def test_marker_with_snapshot_injects(self):
        ck = TaskCheckpoint("t-10", interval=1e9)
        ck.feed("text", "implemented M1")
        ck.flush()
        env, prompt = {}, "DO THE TASK"
        p2, resumed = inject_resume({"_grape_resume_attempt": 2}, env, prompt, ck)
        assert resumed is True
        assert p2.startswith(prompt)
        assert "implemented M1" in p2
        assert env["GRAPE_RESUME_CHECKPOINT"] == ck.path

    def test_marker_without_snapshot_degrades(self):
        ck = TaskCheckpoint("t-11", interval=1e9)  # 从未落盘
        env, prompt = {}, "DO THE TASK"
        p2, resumed = inject_resume({"_grape_resume_attempt": 2}, env, prompt, ck)
        assert resumed is False and p2 == prompt and env == {}

    def test_corrupt_snapshot_degrades(self):
        ck = TaskCheckpoint("t-12", interval=1e9)
        os.makedirs(os.path.dirname(ck.path), exist_ok=True)
        with open(ck.path, "w", encoding="utf-8") as f:
            f.write("garbage")
        p2, resumed = inject_resume({"_grape_resume_attempt": 2}, {}, "P", ck)
        assert resumed is False and p2 == "P"


class TestResourceStackCleanup:
    def test_dispose_clears_checkpoint(self):
        ck = TaskCheckpoint("t-13", interval=0.0)
        ck.feed("text", "x")
        assert os.path.exists(ck.path)
        res = TaskResourceStack(task_id="t-13")
        res.track_checkpoint(ck)
        res.dispose()
        assert not os.path.exists(ck.path)

    def test_dispose_without_checkpoint_is_noop(self):
        res = TaskResourceStack(task_id="t-14")
        res.dispose()  # 不应抛
