"""Issue #975：任务日志上报批次遇 413/4xx 不得被当成功吞掉。

覆盖：
- client.report_messages 三态：413/400 抛 ReportBatchError、410 → None、网络错误 → {}
- agent._flush_report_batch：413 拆批重发成功（无缺号）、单条仍失败走占位+补报队列、
  网络错误保留 batch（不清空）、410 终态
- worker_report_queue：kind=messages 补报条目重放出队
- service.report_messages seq_start 预留 + peek_next_seq + list_task_messages 无缺号
"""
import asyncio
import json

import pytest

from src.runtime.client import ReportBatchError
from tests.test_runtime_client import _Ctx, _FakeResp, _client, _run


# ---------- client.report_messages 三态 ----------

class TestReportMessagesThreeStates:
    def test_413_raises_report_batch_error(self):
        c = _client(_Ctx(_FakeResp(413, text="payload too large")))
        with pytest.raises(ReportBatchError) as ei:
            _run(c.report_messages("t1", [{"type": "text", "data": "x"}]))
        assert ei.value.status == 413

    def test_400_raises_report_batch_error(self):
        c = _client(_Ctx(_FakeResp(400, text="bad request")))
        with pytest.raises(ReportBatchError):
            _run(c.report_messages("t1", [{"type": "text", "data": "x"}]))

    def test_410_returns_none(self):
        c = _client(_Ctx(_FakeResp(410, text="task already completed")))
        assert _run(c.report_messages("t1", [{"type": "text", "data": "x"}])) is None

    def test_network_error_returns_empty_dict_not_batch_error(self):
        """网络错误（重试耗尽）保持旧语义返回 {}（可重试），不得抛 ReportBatchError。"""
        import asyncio as _aio
        from unittest.mock import AsyncMock, patch
        c = _client([RuntimeError("conn reset")] * 5 + [_Ctx(_FakeResp(200, {"status": "running"}))])
        with patch.object(_aio, "sleep", new=AsyncMock()):
            resp = _run(c.report_messages("t1", [{"type": "text", "data": "x"}]))
        assert resp == {}

    def test_success_returns_receipt(self):
        c = _client(_Ctx(_FakeResp(200, {"status": "ok", "seq_start": 1, "seq_end": 3})))
        resp = _run(c.report_messages("t1", [{"type": "text", "data": "x"}] * 3))
        assert resp.get("status") == "ok" and resp.get("seq_end") == 3


# ---------- agent._flush_report_batch 降级链路 ----------

class _ScriptedClient:
    """模拟 RuntimeClient.report_messages 语义的假客户端（记录全部送达消息）。"""

    def __init__(self, policy):
        self.policy = policy          # async (messages) -> resp | raise
        self.delivered = []           # 送达的原始消息（按送达顺序）
        self.calls = 0

    async def report_messages(self, task_id, messages):
        self.calls += 1
        return await self.policy(self, messages)


def _size_limited_policy(limit_msgs=20, huge=8000):
    """413 策略：批次条数 > limit_msgs 或含超大消息（data > huge）→ 413；否则成功。"""

    async def policy(cli, messages):
        if len(messages) > limit_msgs or any(
                isinstance(m.get("data"), str) and len(m["data"]) > huge for m in messages):
            raise ReportBatchError(413, "payload too large")
        cli.delivered.extend(messages)
        return {"status": "ok", "seq_start": 0, "seq_end": 0}

    return policy


class TestFlushReportBatch:
    @staticmethod
    def _batch(n=50, huge_at=None):
        msgs = [{"type": "text", "data": f"msg-{i}"} for i in range(n)]
        if huge_at is not None:
            msgs[huge_at] = {"type": "text", "data": "x" * 10000}
        return msgs

    def test_413_split_resend_delivers_all(self):
        """验收 2：413 后拆批重发成功 → 全部消息送达，无缺号。"""
        from src.runtime.agent import _flush_report_batch
        cli = _ScriptedClient(_size_limited_policy(limit_msgs=20))
        batch = self._batch(50)
        outcome = asyncio.run(_flush_report_batch(cli, "t1", batch))
        assert outcome == "ok"
        delivered = [m["data"] for m in cli.delivered]
        assert sorted(delivered) == sorted(f"msg-{i}" for i in range(50)), "拆批重发后必须无缺号"
        assert cli.calls > 1, "必须发生拆批（首批 50 > 20 触发 413）"

    def test_413_single_failure_degrades_with_placeholder(self):
        """验收 1：批次含超大消息 → 整批 413 → 拆批；单条仍失败 → 占位可见 + 落补报队列。"""
        from src.runtime.agent import _flush_report_batch
        cli = _ScriptedClient(_size_limited_policy(limit_msgs=5, huge=2000))
        queued = []
        batch = self._batch(50, huge_at=10)
        import src.runtime.agent as agent_mod
        orig = agent_mod._report_queue
        try:
            agent_mod._report_queue = lambda: type("Q", (), {"add": staticmethod(queued.append)})()
            outcome = asyncio.run(_flush_report_batch(cli, "t1", batch))
        finally:
            agent_mod._report_queue = orig
        assert outcome == "ok", "降级完成（送达+占位+入队）后按已处理清空 batch"
        # 49 条普通消息全部送达，无静默丢失
        delivered = [m["data"] for m in cli.delivered if isinstance(m.get("data"), str) and m["data"].startswith("msg-")]
        assert sorted(delivered) == sorted(f"msg-{i}" for i in range(50) if i != 10)
        # 占位条目可见（dropped:true + 丢失条数 + 原因码）
        placeholders = [m for m in cli.delivered if m.get("type") == "log"]
        assert placeholders, "必须有 dropped 占位送达"
        ph = json.loads(placeholders[0]["data"])
        assert ph.get("dropped") is True and ph.get("count") == 1 and "413" in ph.get("reason", "")
        # 失败消息进补报队列（截断后）
        assert len(queued) == 1
        assert queued[0]["kind"] == "messages" and queued[0]["task"] == "t1"
        assert len(queued[0]["message"]["data"]) <= 4000 + 20

    def test_network_error_keeps_batch(self):
        """网络错误（{}）→ retry：batch 不清空，等待下个窗口重试。"""
        from src.runtime.agent import _flush_report_batch

        async def policy(cli, messages):
            return {}

        cli = _ScriptedClient(policy)
        batch = self._batch(10)
        outcome = asyncio.run(_flush_report_batch(cli, "t1", batch))
        assert outcome == "retry"
        assert cli.delivered == []

    def test_410_terminal(self):
        from src.runtime.agent import _flush_report_batch

        async def policy(cli, messages):
            return None

        cli = _ScriptedClient(policy)
        assert asyncio.run(_flush_report_batch(cli, "t1", self._batch(10))) == "terminal"

    def test_credential_revoked_propagates(self):
        """401 凭据吊销不得被降级路径吞掉。"""
        from src.runtime.agent import _flush_report_batch
        from src.runtime.client import CredentialRevokedError

        async def policy(cli, messages):
            raise CredentialRevokedError("revoked")

        cli = _ScriptedClient(policy)
        with pytest.raises(CredentialRevokedError):
            asyncio.run(_flush_report_batch(cli, "t1", self._batch(10)))

    def test_degrade_failure_falls_back_to_queue_not_ok_silently(self):
        """QA 复审（Spec #3）：degrade_failed_batch 自身抛错时不得仍返回 "ok" 静默清空
        batch——兜底逐条落补报队列（可重放，不丢）；队列不可用才返回 "retry" 保留 batch。"""
        from src.runtime.agent import _flush_report_batch

        async def policy(cli, messages):
            raise ReportBatchError(413, "too large")

        cli = _ScriptedClient(policy)
        batch = self._batch(5)
        queued = []
        import src.runtime.agent as agent_mod
        orig_degrade = agent_mod.degrade_failed_batch if hasattr(agent_mod, "degrade_failed_batch") else None
        from src.runtime import message_report as mr
        orig_split = mr.split_resend
        orig_q = agent_mod._report_queue

        async def _boom(*a, **k):
            raise RuntimeError("degrade pipeline broken")

        mr.split_resend = _boom
        agent_mod._report_queue = lambda: type("Q", (), {"add": staticmethod(queued.append)})()
        try:
            outcome = asyncio.run(_flush_report_batch(cli, "t1", batch))
        finally:
            mr.split_resend = orig_split
            agent_mod._report_queue = orig_q
        assert outcome == "ok", "兜底入队成功（可重放）按已处理清空 batch"
        assert len(queued) == 5 and all(e["kind"] == "messages" and e["task"] == "t1" for e in queued)

    def test_degrade_failure_and_queue_failure_keeps_batch(self):
        """QA 复审（Spec #3）续：兜底入队也失败 → 返回 "retry" 保留 batch，绝不静默丢失。"""
        from src.runtime.agent import _flush_report_batch
        from src.runtime import message_report as mr
        import src.runtime.agent as agent_mod

        async def policy(cli, messages):
            raise ReportBatchError(413, "too large")

        cli = _ScriptedClient(policy)

        async def _boom(*a, **k):
            raise RuntimeError("degrade pipeline broken")

        orig_split = mr.split_resend
        orig_q = agent_mod._report_queue
        mr.split_resend = _boom

        class _BrokenQ:
            def add(self, entry):
                raise RuntimeError("disk full")

        agent_mod._report_queue = lambda: _BrokenQ()
        try:
            outcome = asyncio.run(_flush_report_batch(cli, "t1", self._batch(5)))
        finally:
            mr.split_resend = orig_split
            agent_mod._report_queue = orig_q
        assert outcome == "retry", "队列也不可用时必须保留 batch 等待重试"


# ---------- message_report 拆批单元 ----------

class TestSplitResend:
    def test_binary_split_depth_limited(self):
        from src.runtime.message_report import split_resend
        calls = []

        async def report(msgs):
            calls.append(len(msgs))
            if len(msgs) > 1:
                raise ReportBatchError(413, "too large")
            return {"status": "ok"}

        out = asyncio.run(split_resend(report, [{"type": "text", "data": str(i)} for i in range(16)]))
        assert out.delivered == 16 and not out.failed and not out.terminal
        assert 1 in calls, "最终必须拆到单条送达"

    def test_terminal_stops_resend(self):
        from src.runtime.message_report import split_resend

        async def report(msgs):
            if len(msgs) > 2:
                raise ReportBatchError(413, "x")
            return None  # 终态

        out = asyncio.run(split_resend(report, [{"type": "text", "data": str(i)} for i in range(8)]))
        assert out.terminal


# ---------- worker_report_queue messages 补报重放 ----------

class TestMessagesReplay:
    def test_replay_messages_ack_dequeues(self, tmp_path):
        from src.runtime import worker_report_queue as wrq
        delivered = []

        class _FakeClient:
            async def report_messages(self, tid, messages):
                delivered.append((tid, messages))
                return {"status": "ok", "seq_start": 1, "seq_end": 1}

        q = wrq.WorkerReportQueue(path=str(tmp_path / "q.jsonl"))
        q.add({"kind": "messages", "task_id": "t9#msg-1-0", "task": "t9",
               "message": {"type": "text", "data": "lost"}})
        asyncio.run(wrq.replay_pending_reports(_FakeClient(), q))
        assert q.size() == 0
        assert delivered and delivered[0][0] == "t9"

    def test_replay_messages_network_error_keeps(self, tmp_path):
        from src.runtime import worker_report_queue as wrq

        class _FakeClient:
            async def report_messages(self, tid, messages):
                return {}

        q = wrq.WorkerReportQueue(path=str(tmp_path / "q.jsonl"))
        q.add({"kind": "messages", "task_id": "t9#msg-2-0", "task": "t9",
               "message": {"type": "text", "data": "lost"}})
        asyncio.run(wrq.replay_pending_reports(_FakeClient(), q))
        assert q.size() == 1

    def test_replay_messages_4xx_degrades_and_dequeues(self, tmp_path):
        from src.runtime import worker_report_queue as wrq
        delivered = []

        class _FakeClient:
            async def report_messages(self, tid, messages):
                delivered.append(messages)
                raise ReportBatchError(413, "still too large")

        q = wrq.WorkerReportQueue(path=str(tmp_path / "q.jsonl"))
        q.add({"kind": "messages", "task_id": "t9#msg-3-0", "task": "t9",
               "message": {"type": "text", "data": "x" * 10000}})
        asyncio.run(wrq.replay_pending_reports(_FakeClient(), q))
        assert q.size() == 0, "单条仍 4xx：占位后出队，避免无限重放"
        # 占位尝试过（占位本身也可能失败，此处只验证不无限重放）
        assert delivered


# ---------- service seq 预留与回执 ----------

class TestServiceSeqRange:
    def _make_task(self, task_id="t_seq975"):
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR IGNORE INTO agent_tasks (id, agent_session_id, runtime_id, prompt, status) "
                      "VALUES (?, 's1', 1, 'p', 'running')", (task_id,))
            db.commit()

    def _clean(self, task_id="t_seq975"):
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM task_messages WHERE task_id=?", (task_id,))
            c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
            db.commit()

    def test_seq_start_reserved_and_receipt_logged(self, caplog):
        from src.runtime import service
        task_id = "t_seq975"
        self._clean(task_id)
        self._make_task(task_id)
        try:
            assert service.peek_next_seq(task_id) == 1
            service.report_messages(task_id, [{"type": "text", "data": "a"},
                                              {"type": "text", "data": "b"}], seq_start=1)
            msgs = service.list_task_messages(task_id)
            assert [m["seq"] for m in msgs] == [1, 2]
            assert service.peek_next_seq(task_id) == 3
            # 第二批沿用 MAX+1（不传 seq_start）
            service.report_messages(task_id, [{"type": "text", "data": "c"}])
            msgs = service.list_task_messages(task_id)
            assert [m["seq"] for m in msgs] == [1, 2, 3], "seq 连续无缺号"
        finally:
            self._clean(task_id)

    def test_enqueue_async_report_sync_fallback_with_seq_start(self):
        from src.runtime import service
        task_id = "t_seq975"
        self._clean(task_id)
        self._make_task(task_id)
        try:
            # _ASYNC_REPORT_QUEUE 未启动（测试环境）→ 同步兜底路径带 seq_start
            service._ASYNC_REPORT_QUEUE = None
            service.enqueue_async_report("messages", task_id,
                                         [{"type": "text", "data": "a"}], seq_start=5)
            msgs = service.list_task_messages(task_id)
            assert [m["seq"] for m in msgs] == [5], "预约 seq 范围必须被落库沿用"
        finally:
            self._clean(task_id)

    def test_report_messages_returns_actual_receipt_and_skips_non_dict(self):
        """QA 复审（Spec #1）：返回实际落库回执；非 dict 消息不占 seq、不计入回执。"""
        from src.runtime import service
        task_id = "t_seq975"
        self._clean(task_id)
        self._make_task(task_id)
        try:
            receipt = service.report_messages(
                task_id, ["raw-string", {"type": "text", "data": "a"},
                          {"type": "text", "data": "b"}], seq_start=1)
            assert receipt == {"count": 2, "seq_start": 1, "seq_end": 2}
            assert service.peek_next_seq(task_id) == 3
        finally:
            self._clean(task_id)

    def test_report_messages_seq_conflict_reallocates(self):
        """QA 复审（Spec #4）：预留 seq 与已存在行撞号 → 改用 MAX+1 重试，不静默丢消息。"""
        from src.runtime import service
        task_id = "t_seq975"
        self._clean(task_id)
        self._make_task(task_id)
        try:
            service.report_messages(task_id, [{"type": "text", "data": "a"}], seq_start=1)
            # 模拟并发重放：不传 seq_start → MAX+1=2；再传过期预留 1（已占用）应撞号重试
            service.report_messages(task_id, [{"type": "text", "data": "b"}], seq_start=1)
            msgs = service.list_task_messages(task_id)
            assert [m["seq"] for m in msgs] == [1, 2], "撞号必须重新分配而不是丢消息"
            assert service.peek_next_seq(task_id) == 3
        finally:
            self._clean(task_id)

    def test_enqueue_async_report_returns_sync_receipt(self):
        """QA 复审（Spec #1）：同步兜底路径把实际落库回执透传给路由层。"""
        from src.runtime import service
        task_id = "t_seq975"
        self._clean(task_id)
        self._make_task(task_id)
        try:
            service._ASYNC_REPORT_QUEUE = None
            receipt = service.enqueue_async_report(
                "messages", task_id, [{"type": "text", "data": "a"}], seq_start=1)
            assert receipt == {"count": 1, "seq_start": 1, "seq_end": 1}
        finally:
            self._clean(task_id)


# ---------- QA 复审：client 并发 error_sink / 共享 #msg- 常量 ----------

class TestQaReviewFixes:
    def test_report_messages_uses_per_call_error_sink(self):
        """并发覆盖竞态：report_messages 判定用本次调用私有 sink，不受其它请求污染。"""
        c = _client(_Ctx(_FakeResp(413, text="payload too large")))
        # 模拟并发请求覆盖实例级 last_http_error（网络错误）
        c.last_http_error = {"status": 0, "text": "conn reset by peer request"}
        with pytest.raises(ReportBatchError) as ei:
            _run(c.report_messages("t1", [{"type": "text", "data": "x"}]))
        assert ei.value.status == 413

    def test_msg_entry_id_helpers_roundtrip(self):
        from src.runtime.message_report import MSG_ENTRY_ID_SEP, build_msg_entry_id, real_task_id
        eid = build_msg_entry_id("t9", 1700000000.5, 3)
        assert eid.startswith(f"t9{MSG_ENTRY_ID_SEP}")
        assert real_task_id(eid) == "t9"
        assert real_task_id(eid, "t9") == "t9"
        assert real_task_id("plain-task-id") == "plain-task-id"

    def test_replay_uses_real_task_id_without_task_field(self, tmp_path):
        """补报条目缺 task 字段时仍能从 task_id 剥离 #msg- 后缀得到真实 task。"""
        from src.runtime import worker_report_queue as wrq
        delivered = []

        class _FakeClient:
            async def report_messages(self, tid, messages):
                delivered.append((tid, messages))
                return {"status": "ok", "seq_start": 1, "seq_end": 1}

        q = wrq.WorkerReportQueue(path=str(tmp_path / "q.jsonl"))
        q.add({"kind": "messages", "task_id": "t42#msg-123-0",
               "message": {"type": "text", "data": "lost"}})
        asyncio.run(wrq.replay_pending_reports(_FakeClient(), q))
        assert q.size() == 0
        assert delivered and delivered[0][0] == "t42"
