"""Issue #4203：消息批次网络错误连续失败落 outbox（SIGKILL 不丢）。

seam（src/domains/runtime/engine/agent.py）：
- `_flush_report_batch(client, tid, batch)`：网络错误（client.report_messages 返回 {}）
  连续 GRAPE_WORKER_MSG_FLUSH_PERSIST_AFTER（默认 3）个窗口后：
  1) 批次逐条落 WorkerReportQueue（kind=messages，#msg- 后缀 + 截断）；
  2) 返回 "ok"（调用方清空内存 batch）。
- 未达阈值：返回 "retry" 保留 batch（原语义不变）。
- 成功送达：streak 清零。
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
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


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
