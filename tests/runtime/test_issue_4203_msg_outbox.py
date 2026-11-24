"""Issue #4203：消息批次网络错误连续失败落 outbox（SIGKILL 不丢）。

seam（src/domains/runtime/engine/agent.py）：
- `_flush_report_batch(client, tid, batch)`：网络错误（client.report_messages 返回 {}）
  连续 GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER（默认 3）个窗口后：
  1) 批次截断后**批量**落 WorkerReportQueue（kind=messages，#msg- 后缀）；
  2) 全部落盘才返回 "ok"（调用方清空内存 batch）；落盘未成功返回 "retry" 保留 batch。
- 未达阈值：返回 "retry" 保留 batch（原语义不变）。
- 成功送达/终态/凭据吊销：streak 清零。
- 阈值环境变量非法值回落 3（不抛 ValueError 打断上报）。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.runtime.engine import agent  # noqa: E402
from src.domains.runtime.engine.worker_report_queue import WorkerReportQueue  # noqa: E402


class _NetErrorClient:
    """report_messages 恒网络错误（返回 {}）。"""

    async def report_messages(self, task_id, messages):
        return {}


class _OkClient:
    async def report_messages(self, task_id, messages):
        return {"status": "ok", "seq_start": 1, "seq_end": len(messages)}


class _TerminalClient:
    async def report_messages(self, task_id, messages):
        return None


@pytest.fixture()
def outbox(tmp_path, monkeypatch):
    """隔离的补报队列（落盘到 tmp），替换 agent 单例。"""
    q = WorkerReportQueue(path=str(tmp_path / "worker-report-queue.jsonl"))
    monkeypatch.setattr(agent, "_report_queue_obj", q)
    return q


@pytest.fixture(autouse=True)
def clean_streak():
    agent._msg_flush_retry_streak.clear()
    yield
    agent._msg_flush_retry_streak.clear()


def _batch(n=2):
    return [{"type": "log", "data": f"msg-{i}"} for i in range(n)]


def _run(coro):
    return asyncio.run(coro)


class TestFlushPersistAfterStreak:
    def test_below_threshold_keeps_batch_in_memory(self, outbox, monkeypatch):
        monkeypatch.setenv("GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER", "3")
        out = _run(agent._flush_report_batch(_NetErrorClient(), "t-1", _batch()))
        assert out == "retry"
        assert outbox.size() == 0            # 未落盘
        assert agent._msg_flush_retry_streak["t-1"] == 1

    def test_reaches_threshold_persists_and_ok(self, outbox, monkeypatch):
        monkeypatch.setenv("GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER", "3")
        client = _NetErrorClient()
        tid = "t-2"
        # 第 1、2 个窗口：内存重试
        assert _run(agent._flush_report_batch(client, tid, _batch())) == "retry"
        assert _run(agent._flush_report_batch(client, tid, _batch())) == "retry"
        assert outbox.size() == 0
        # 第 3 个窗口：落盘 + 清空（返回 ok）
        assert _run(agent._flush_report_batch(client, tid, _batch())) == "ok"
        assert agent._msg_flush_retry_streak.get(tid) is None
        assert outbox.size() == 2
        entries = sorted(outbox.pending(), key=lambda e: e["task_id"])
        for e in entries:
            assert e["kind"] == "messages"
            assert e["task"] == tid
            assert "#msg-" in e["task_id"]          # #975 共享键格式
            assert isinstance(e["message"], dict)   # 单条 dict（截断语义）

    def test_success_resets_streak(self, outbox, monkeypatch):
        monkeypatch.setenv("GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER", "3")
        tid = "t-3"
        assert _run(agent._flush_report_batch(_NetErrorClient(), tid, _batch())) == "retry"
        assert _run(agent._flush_report_batch(_OkClient(), tid, _batch())) == "ok"
        assert agent._msg_flush_retry_streak.get(tid) is None
        # 成功后重新计数：又是 1/3 而非 2/3
        assert _run(agent._flush_report_batch(_NetErrorClient(), tid, _batch())) == "retry"
        assert agent._msg_flush_retry_streak[tid] == 1

    def test_terminal_clears_streak(self, outbox, monkeypatch):
        tid = "t-4"
        assert _run(agent._flush_report_batch(_NetErrorClient(), tid, _batch())) == "retry"
        assert _run(agent._flush_report_batch(_TerminalClient(), tid, _batch())) == "terminal"
        assert agent._msg_flush_retry_streak.get(tid) is None

    def test_persist_survives_process_restart(self, outbox, monkeypatch, tmp_path):
        """落盘后进程被杀：新进程从 jsonl 恢复，消息仍可续投（重放语义）。"""
        monkeypatch.setenv("GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER", "1")
        assert _run(agent._flush_report_batch(_NetErrorClient(), "t-5", _batch())) == "ok"
        # 模拟 SIGKILL 后重启：从同一文件重建队列
        q2 = WorkerReportQueue(path=str(tmp_path / "worker-report-queue.jsonl"))
        assert q2.size() == 2
        real = q2.pending()[0]
        assert real["task"] == "t-5"


class TestPersistFailureKeepsBatch:
    """QA 复审 Required：落盘失败绝不能清空内存 batch（docstring 与行为必须一致）。"""

    def test_persist_failure_returns_retry_not_ok(self, outbox, monkeypatch):
        monkeypatch.setenv("GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER", "1")
        # 模拟磁盘写失败：add_many 返回 0（已回滚，条目不在内存队列里）
        monkeypatch.setattr(outbox, "add_many", lambda entries: 0)
        tid = "t-fail"
        assert _run(agent._flush_report_batch(_NetErrorClient(), tid, _batch())) == "retry"
        assert outbox.size() == 0
        # 批次保留语义：streak 继续累加，下个窗口仍会重试（不清零、不谎报 ok）
        assert agent._msg_flush_retry_streak.get(tid) == 1

    def test_persist_exception_also_retry(self, outbox, monkeypatch):
        monkeypatch.setenv("GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER", "1")

        def _boom(entries):
            raise OSError("disk full")
        monkeypatch.setattr(outbox, "add_many", _boom)
        assert _run(agent._flush_report_batch(_NetErrorClient(), "t-boom", _batch())) == "retry"

    def test_add_many_rolls_back_on_flush_failure(self, tmp_path, monkeypatch):
        q = WorkerReportQueue(path=str(tmp_path / "q.jsonl"))
        monkeypatch.setattr(q, "_flush", lambda: False)
        n = q.add_many([{"kind": "messages", "task_id": f"x-{i}", "task": "x",
                         "message": {}} for i in range(3)])
        assert n == 0
        assert q.size() == 0           # 未持久化的条目不留内存幽灵

    def test_add_many_single_flush(self, tmp_path):
        q = WorkerReportQueue(path=str(tmp_path / "q.jsonl"))
        calls = []
        real_flush = q._flush

        def _spy():
            calls.append(1)
            return real_flush()
        q._flush = _spy
        n = q.add_many([{"kind": "messages", "task_id": f"y-{i}", "task": "y",
                         "message": {"i": i}} for i in range(50)])
        assert n == 50 and len(calls) == 1     # 50 条只重写一次 jsonl
        assert WorkerReportQueue(path=str(tmp_path / "q.jsonl")).size() == 50


class TestEnvParsing:
    def test_invalid_persist_after_falls_back_to_3(self, monkeypatch):
        for bad in ("", "abc", "  "):
            monkeypatch.setenv("GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER", bad)
            assert agent._msg_flush_persist_after() == 3

    def test_invalid_env_does_not_break_flush(self, outbox, monkeypatch):
        monkeypatch.setenv("GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER", "not-a-number")
        # 未达默认阈值 3 → retry；不抛 ValueError
        assert _run(agent._flush_report_batch(_NetErrorClient(), "t-bad", _batch())) == "retry"

    def test_zero_or_negative_clamped_to_1(self, monkeypatch):
        monkeypatch.setenv("GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER", "0")
        assert agent._msg_flush_persist_after() == 1


class TestCredentialRevokedClearsStreak:
    def test_revoked_pops_streak(self, outbox, monkeypatch):
        from src.domains.runtime.engine.client import CredentialRevokedError

        class _RevokedClient:
            async def report_messages(self, task_id, messages):
                raise CredentialRevokedError("revoked")

        tid = "t-rev"
        assert _run(agent._flush_report_batch(_NetErrorClient(), tid, _batch())) == "retry"
        assert agent._msg_flush_retry_streak.get(tid) == 1
        with pytest.raises(CredentialRevokedError):
            _run(agent._flush_report_batch(_RevokedClient(), tid, _batch()))
        assert agent._msg_flush_retry_streak.get(tid) is None
