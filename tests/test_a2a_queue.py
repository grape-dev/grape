"""A2A 队列化改造测试：queue 模式入队 / direct 模式线程 / pipeline executing 派生。

见 docs/new-version2/a2a-queue-refactor-design.md §五·1 用例 T1-T8。
"""
import json, os
import pytest

from src.session_manager import db, db_lock
from src.runtime import service


class TestA2AQueue:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks")
            c.execute("DELETE FROM events")
            c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            db.commit()

    def _a2a_body(self, agent="coding", issue_number="5"):
        return {
            "agent": agent,
            "payload": {
                "repository": "owner/repo",
                "account": "owner/repo",
                "issue_number": issue_number,
                "pr_number": "",
                "context": "测试 A2A 交办",
            },
        }

    def teardown_method(self):
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def _post_a2a(self, client, body):
        return client.post("/api/a2a", json=body)

    def _task_counts(self):
        with db_lock:
            c = db.cursor()
            return {
                "tasks": c.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0],
                "a2a_events": c.execute("SELECT COUNT(*) FROM events WHERE event_type='a2a_event'").fetchone()[0],
            }

    def _latest_task(self):
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT id, event_seq, issue_id, agent_type, status, server_url "
                "FROM agent_tasks ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {"id": row[0], "event_seq": row[1], "issue_id": row[2],
                "agent_type": row[3], "status": row[4], "server_url": row[5] or ""}

    # T1: queue 模式 A2A 入队
    def test_t1_queue_mode_enqueues(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_local"
        client = TestClient(app)
        r = self._post_a2a(client, self._a2a_body())
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "ok", r.text
        assert r.json().get("seq", 0) > 0, r.text
        t = self._latest_task()
        assert t is not None, "queue 模式应创建 agent_tasks"
        assert t["agent_type"] == "coding"
        assert t["issue_id"] == "#5"
        assert t["status"] in ("queued", "dispatched", "running")
        assert t["server_url"], "task 应带 server_url"

    # T2: direct 模式 A2A 不建任务
    def test_t2_direct_mode_no_task(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "direct"
        client = TestClient(app)
        before = self._task_counts()["tasks"]
        r = self._post_a2a(client, self._a2a_body())
        assert r.status_code == 200, r.text
        after = self._task_counts()
        assert after["tasks"] == before, "direct 模式不应创建 agent_tasks"
        assert after["a2a_events"] >= 1, "direct 模式仍应创建 a2a event"

    # T3: task 与 event 关联（event_seq / issue_id / server_url）
    def test_t3_task_event_link(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_local"
        client = TestClient(app)
        self._post_a2a(client, self._a2a_body())
        t = self._latest_task()
        assert t and t["event_seq"], "task 应有 event_seq"
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT seq, issue_id, agent_type FROM events WHERE seq=?", (t["event_seq"],)).fetchone()
        assert row, "event_seq 应存在对应 event"
        assert row[2] == "coding"
        from src.user.db import get_server_base_url
        assert t["server_url"] == get_server_base_url()

    # T5: worker 消费后 events 回写 completed
    def test_t5_worker_complete_writes_event(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.runtime import service as svc
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_local"
        client = TestClient(app)
        self._post_a2a(client, self._a2a_body())
        t = self._latest_task()
        assert t, "应已入队"
        # 模拟 worker 完成：complete_task 会回写 event
        svc.complete_task(t["id"], "test done", exit_code=0)
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT status FROM events WHERE seq=?", (t["event_seq"],)).fetchone()
            task = c.execute("SELECT status FROM agent_tasks WHERE id=?", (t["id"],)).fetchone()
        assert task[0] == "completed"
        assert row[0] == "completed", "events 应回写 completed"

    # T6: queue_remote 同样入队
    def test_t6_remote_mode_enqueues(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        client = TestClient(app)
        self._post_a2a(client, self._a2a_body())
        assert self._latest_task() is not None, "queue_remote 应创建 agent_tasks"

    # T7: pipeline executing 派生（A2A 入队后 executing=true）
    # 用 queue_remote（无本地 worker 立即消费），任务保持 queued，_running_issue_ids 稳定命中
    def test_t7_pipeline_executing(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.api.routes.pipeline import _running_issue_ids
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        client = TestClient(app)
        self._post_a2a(client, self._a2a_body())
        running = _running_issue_ids("gitcode:owner/repo")
        assert "#5" in running, f"A2A 入队后 _running_issue_ids 应含 #5, got {running}"

    # T8: webhook 事件路径不回归（queue 模式入队不直接执行）
    def test_t8_webhook_still_enqueues(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        import src.api.event_pipeline as ep
        self._clean()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_local"
        raw = json.dumps({
            "event_type": "issue_comment",
            "payload": {"issue": {"number": 5, "title": "t", "body": "x"}, "action": "comment"},
            "issue_id": "#5",
            "repository": "owner/repo",
        }).encode()
        headers = {"X-GitCode-Event": "Note Hook"}
        with db_lock:
            db.cursor().execute(
                "INSERT OR REPLACE INTO event_route_config (repo_key, event_type, skill) VALUES (?,?,?)",
                ("", "issue_comment", "issue"))
            db.commit()
        ep.run_pipeline(raw, headers, account="owner/repo")
        with db_lock:
            tasks = db.cursor().execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
        assert tasks >= 1, "webhook queue 模式仍应入队（回归）"
