"""Issue #1667：A2A 去重引入 context/idempotency_key + queue/force 语义测试。

覆盖验收标准：
- 同 issue+agent、不同 context → 不再 duplicate_a2a；
- queue=true 命中同上下文活跃事件 → queued + queue_seq/existing_seq，前序终态后自动派发且仅一次；
- force=true → 立即新建 A2A 事件；
- duplicate 响应保持 #729 original_event 兼容；
- GET /api/a2a/status 可查询排队状态。
"""
import json
import os
import uuid

from src.session_manager import db, db_lock


class TestA2ADedupQueue1667:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            c.execute(
                "DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' "
                "OR repo_key LIKE 'owner/repo%' OR id LIKE 'sess-a2a-%'")
            c.execute(
                "DELETE FROM agent_runtimes WHERE credential_id IN "
                "(SELECT id FROM runtime_credentials WHERE name LIKE 'a2a-cred-%')")
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'a2a-cred-%'")
            try:
                c.execute("DELETE FROM skill WHERE skill_name='implementation' AND scope='repo'")
            except Exception:
                pass
            try:
                c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
            except Exception:
                pass
            db.commit()

    def _seed(self, agent="coding"):
        with db_lock:
            c = db.cursor()
            try:
                c.execute(
                    "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                    "VALUES ('gitcode:owner/repo', 'owner', 'repo', 'owner/repo')")
            except Exception:
                pass
            c.execute(
                "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
                "VALUES (?,?,?, 'global', 'tester', 'active')",
                ("h" + uuid.uuid4().hex, "a2a-cred-" + agent, "a2a-dev-" + agent))
            _cid = c.lastrowid
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
            _rid = c.lastrowid
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable) "
                "VALUES (?, 'gitcode:owner/repo', ?, ?, 'idle', 1)",
                ("sess-a2a-" + agent, agent, _rid))
            # 技能门禁（Issue #630）：提供可物化的 repo 技能，避免派发被 skill gate 阻断
            try:
                c.execute(
                    "INSERT INTO skill (skill_name, display_name, description, agent_type, "
                    "skill_path, scope, owner_id, is_active) "
                    "VALUES ('implementation','implementation','test skill','coding',"
                    "'implementation','repo','gitcode:owner/repo',1)")
            except Exception:
                pass
            db.commit()
        from src.config import SKILLS_DIR
        _sdir = os.path.join(SKILLS_DIR, "repo", "owner", "repo", "implementation")
        os.makedirs(_sdir, exist_ok=True)
        with open(os.path.join(_sdir, "SKILL.md"), "w", encoding="utf-8") as _f:
            _f.write("# implementation\n\ntest skill\n")

    def teardown_method(self):
        os.environ.pop("DEVOPS_EXECUTION_MODE", None)

    def _client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        return TestClient(app)

    def _body(self, context="测试 A2A 交办", queue=False, force=False, idem=""):
        body = {
            "agent": "coding",
            "payload": {
                "repository": "owner/repo",
                "account": "owner/repo",
                "issue_number": "5",
                "pr_number": "",
                "context": context,
            },
            "queue": queue,
            "force": force,
        }
        if idem:
            body["idempotency_key"] = idem
        return body

    def _setup(self):
        self._clean()
        self._seed()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        return self._client()

    def _event(self, seq):
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT seq, status, agent_type, issue_id, payload FROM events WHERE seq=?",
                (seq,)).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[4] or "{}")
        except Exception:
            payload = {}
        return {"seq": row[0], "status": row[1], "agent": row[2], "issue_id": row[3],
                "payload": payload}

    def _tasks_for(self, seq):
        with db_lock:
            c = db.cursor()
            return c.execute(
                "SELECT id, status FROM agent_tasks WHERE event_seq=?", (seq,)).fetchall()

    def test_same_context_duplicate_keeps_729_original_event(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(context="T3 补测"))
        assert first.status_code == 200, first.text
        assert first.json()["seq"] > 0
        first_seq = first.json()["seq"]

        dup = client.post("/api/a2a", json=self._body(context="T3 补测"))
        assert dup.status_code == 200, dup.text
        data = dup.json()
        assert data["skipped"] == "duplicate_a2a"
        assert data["queued"] is False
        assert data["reason"] == "active_a2a_event"
        assert data["existing_seq"] == first_seq
        assert data["original_event"]["seq"] == first_seq
        assert "task_ref" in data["original_event"]
        assert "result_ref" in data["original_event"]

    def test_different_context_is_not_duplicate(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(context="T3 补测"))
        assert first.json()["seq"] > 0
        second = client.post("/api/a2a", json=self._body(context="PR 交付"))
        assert second.status_code == 200, second.text
        data = second.json()
        assert data.get("skipped") != "duplicate_a2a", data
        assert data["seq"] > 0 and data["seq"] != first.json()["seq"]

    def test_queue_returns_queued_and_drains_once(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(context="同上下文排队的交办"))
        first_seq = first.json()["seq"]
        assert first_seq > 0

        queued = client.post("/api/a2a", json=self._body(context="同上下文排队的交办", queue=True))
        assert queued.status_code == 200, queued.text
        data = queued.json()
        assert data["skipped"] == "queued"
        assert data["queued"] is True
        assert data["existing_seq"] == first_seq
        assert data["queue_seq"] > 0
        queue_seq = data["queue_seq"]
        qev = self._event(queue_seq)
        assert qev["status"] == "queued"
        assert qev["payload"]["_a2a_queue"]["existing_seq"] == first_seq
        assert self._tasks_for(queue_seq) == [], "排队状态下不应创建任务"

        status = client.get(
            "/api/a2a/status",
            params={"repository": "owner/repo", "issue_number": "5", "agent": "coding"})
        assert status.status_code == 200, status.text
        sdata = status.json()
        assert sdata["queued_count"] >= 1
        assert any(e["queue_seq"] == queue_seq for e in sdata["queued"])

        from src.api.event_store import update_event_complete
        update_event_complete(first_seq, "completed", result="前序完成")
        qev = self._event(queue_seq)
        assert qev["status"] == "running", "前序终态后排队事件应被自动派发"
        tasks = self._tasks_for(queue_seq)
        assert len(tasks) == 1, f"排队事件应恰好派发一次任务, got {tasks}"

        update_event_complete(first_seq, "completed", result="重复终态回调")
        assert len(self._tasks_for(queue_seq)) == 1, "重复终态回调不应重复派发"

    def test_force_bypasses_active_dedup(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(context="强制下发上下文"))
        first_seq = first.json()["seq"]
        forced = client.post("/api/a2a", json=self._body(context="强制下发上下文", force=True))
        assert forced.status_code == 200, forced.text
        data = forced.json()
        assert data.get("skipped") != "duplicate_a2a", data
        assert data["seq"] > 0 and data["seq"] != first_seq

    def test_idempotency_key_distinguishes_context(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(context="ctx-a", idem="task-1:coding"))
        assert first.json()["seq"] > 0
        second = client.post("/api/a2a", json=self._body(context="ctx-a", idem="task-2:coding"))
        assert second.status_code == 200, second.text
        data = second.json()
        assert data.get("skipped") != "duplicate_a2a", data
        assert data["seq"] > 0 and data["seq"] != first.json()["seq"]

    def test_queue_idempotent_same_context(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(context="queue 幂等上下文"))
        assert first.json()["seq"] > 0
        q1 = client.post("/api/a2a", json=self._body(context="queue 幂等上下文", queue=True)).json()
        assert q1["queued"] is True and q1["queue_seq"] > 0
        q2 = client.post("/api/a2a", json=self._body(context="queue 幂等上下文", queue=True)).json()
        assert q2["queued"] is True
        assert q2["queue_seq"] == q1["queue_seq"]
        assert q2.get("idempotent") is True

    def test_force_cancels_superseded_queue(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(context="force 覆盖上下文"))
        first_seq = first.json()["seq"]
        q = client.post("/api/a2a", json=self._body(context="force 覆盖上下文", queue=True)).json()
        queue_seq = q["queue_seq"]
        forced = client.post("/api/a2a", json=self._body(context="force 覆盖上下文", force=True)).json()
        assert forced["seq"] > 0 and forced["seq"] != first_seq
        assert self._event(queue_seq)["status"] == "cancelled"
        from src.api.event_store import update_event_complete
        update_event_complete(first_seq, "completed", result="前序完成")
        assert self._tasks_for(queue_seq) == [], "被 force 取代的排队事件不应再派发"
