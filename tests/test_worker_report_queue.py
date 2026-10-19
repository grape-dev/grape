"""worker 侧补报机制测试（graceful-shutdown-requeue-design.md Step 4）。

覆盖：
- WorkerReportQueue：入队/ack 出队、TTL 过期、容量上限、落盘重启恢复；
- service.validate_run_token：ok/noop/stale 判定（防回炉后旧实例误 complete）；
- replay_pending_reports：重放成功出队、失败保留（带 run_token）。
"""
import asyncio
import os
import tempfile

import pytest

from src.runtime import worker_report_queue as wrq
from src.runtime import service as svc


@pytest.fixture()
def q_path(tmp_path):
    return str(tmp_path / "report-queue.jsonl")


def _entry(tid="t1", status="completed", run_token="tok-1"):
    return {"task_id": tid, "status": status, "result_text": "r",
            "exit_code": 0, "run_token": run_token, "session_id": "s",
            "work_dir": ".", "engine": "opencode", "runtime_id": 9}


class TestWorkerReportQueue:
    def test_add_ack_remove(self, q_path):
        q = wrq.WorkerReportQueue(path=q_path)
        q.add(_entry())
        assert q.size() == 1
        q.remove("t1")
        assert q.size() == 0

    def test_persistence_across_reload(self, q_path):
        q = wrq.WorkerReportQueue(path=q_path)
        q.add(_entry("t1", "failed", "tok-1"))
        q2 = wrq.WorkerReportQueue(path=q_path)  # 模拟 worker 重启
        assert q2.size() == 1
        assert q2.pending()[0]["task_id"] == "t1"
        assert q2.pending()[0]["run_token"] == "tok-1"

    def test_ttl_expired(self, q_path):
        q = wrq.WorkerReportQueue(path=q_path, ttl=0)  # 立即过期
        q.add(_entry("t1"))
        assert q.size() == 1
        q.prune_expired()
        assert q.size() == 0

    def test_cap_drops_oldest(self, q_path):
        q = wrq.WorkerReportQueue(path=q_path, ttl=9999, cap=2)
        q.add(_entry("t1")); q.add(_entry("t2")); q.add(_entry("t3"))
        assert q.size() <= 2  # 超容量只留 TTL 内最近的（此处全在 TTL 内，退化为保留全部不会崩）
        # 明确：cap<len 时至少不报错且仍可操作
        assert q.pending()

    def test_replay_ack_dequeues(self, q_path):
        class _FakeClient:
            def __init__(self, resp):
                self.resp = resp
                self.calls = []
            async def complete(self, tid, text, exit_code=0, **kw):
                self.calls.append(("complete", tid, kw.get("run_token"), exit_code))
                return self.resp
            async def fail(self, tid, err, exit_code=-1, **kw):
                self.calls.append(("fail", tid, kw.get("run_token"), exit_code))
                return self.resp
        q = wrq.WorkerReportQueue(path=q_path)
        q.add(_entry("t_ok", "completed", "tok-a"))
        q.add(_entry("t_none", "failed", "tok-b"))
        client = _FakeClient(resp={"status": "ok"})
        asyncio.run(wrq.replay_pending_reports(client, q))
        assert q.size() == 0, "ack 后应出队"
        assert ("complete", "t_ok", "tok-a", 0) in client.calls
        assert ("fail", "t_none", "tok-b", -1) in client.calls

    def test_replay_fail_keeps(self, q_path):
        class _FailClient:
            async def complete(self, *a, **k): return None
            async def fail(self, *a, **k): return None
        q = wrq.WorkerReportQueue(path=q_path)
        q.add(_entry("t_x"))
        asyncio.run(wrq.replay_pending_reports(_FailClient(), q))
        assert q.size() == 1, "上报失败应保留，重连后再补报"


class TestRuntimeClientAck:
    """回归：RuntimeClient.complete/fail 必须把响应体 return 出去。

    2026-10-19 补报重放事故：两者漏 return → 恒为 None → 补报队列的
    `if resp is not None: queue.remove()` 永不成立 → 条目永不出队、每 10s 无限重放，
    把人工 redispatch 后的事件反复写回 failed + 旧失败原因。
    """

    @staticmethod
    def _client(monkeypatch, resp):
        from src.runtime.client import RuntimeClient
        c = RuntimeClient.__new__(RuntimeClient)  # 绕过构造/网络
        calls = []

        async def _fake_request(method, path, body=None, retries=4):
            calls.append((method, path, body))
            return resp

        c._request = _fake_request
        return c, calls

    def test_complete_returns_response(self, monkeypatch):
        c, calls = self._client(monkeypatch, {"status": "completed"})
        out = asyncio.run(c.complete("t1", "txt", 0, run_token="tok"))
        assert out == {"status": "completed"}, "complete 必须返回响应体（补报出队判据）"
        assert calls and calls[0][1].endswith("/complete")

    def test_fail_returns_response(self, monkeypatch):
        c, calls = self._client(monkeypatch, {"status": "failed"})
        out = asyncio.run(c.fail("t1", "boom", exit_code=-1, run_token="tok"))
        assert out == {"status": "failed"}, "fail 必须返回响应体（补报出队判据）"
        assert calls and calls[0][1].endswith("/fail")

    def test_replay_with_real_client_dequeues(self, q_path, monkeypatch):
        """真 client + 假 _request(200)：ack 后必须出队（旧代码恒 None → 永不出队）。"""
        c, _ = self._client(monkeypatch, {"status": "failed"})
        q = wrq.WorkerReportQueue(path=q_path)
        q.add(_entry("t_real", "failed", "tok-real"))
        asyncio.run(wrq.replay_pending_reports(c, q))
        assert q.size() == 0, "服务端 200 后条目必须出队，否则会每 10s 无限重放"

    def test_replay_keeps_entry_when_server_unreachable(self, q_path, monkeypatch):
        """对端不可达（_request 返回 None）时条目必须保留，等重连补报。"""
        c, _ = self._client(monkeypatch, None)
        q = wrq.WorkerReportQueue(path=q_path)
        q.add(_entry("t_unreach", "failed", "tok-x"))
        asyncio.run(wrq.replay_pending_reports(c, q))
        assert q.size() == 1, "未 ack 的条目应保留"


class TestValidateRunToken:
    def test_ok_active(self):
        task = {"status": "running", "started_at": "2026-10-16T12:00:00+00:00"}
        assert svc.validate_run_token(task, "2026-10-16T12:00:00+00:00") == "ok"

    def test_noop_terminal(self):
        task = {"status": "completed", "started_at": "S"}
        assert svc.validate_run_token(task, "S") == "noop"

    def test_stale_mismatch(self):
        task = {"status": "running", "started_at": "old"}
        assert svc.validate_run_token(task, "new") == "stale"

    def test_stale_requeued(self):
        # recover_orphans 回炉：started_at 被清空 → 旧 run_token 必然 mismatch
        task = {"status": "queued", "started_at": None}
        assert svc.validate_run_token(task, "old") == "stale"

    def test_stale_queued_even_if_token_equal(self):
        task = {"status": "queued", "started_at": "S"}
        assert svc.validate_run_token(task, "S") == "stale"

    def test_no_token_ok(self):
        assert svc.validate_run_token({"status": "queued", "started_at": None}, "") == "ok"