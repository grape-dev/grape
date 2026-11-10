"""Issue #1770：a2a_event 派发去重/幂等门禁测试。

覆盖验收标准：
- 下游（qa）running 时派发上游（coding）→ 跳过，event_skipped 留痕且原因可读；
- qa 结束后再次派发可正常执行；
- 同一幂等键（issue + scene + target agent + 时间窗）重复派发被丢弃并留痕；
- 短时间窗内同 Issue 同目标 agent 多次派发产生告警观测；
- force=true 可越过门禁（保持人工强制派发语义）。
"""
import os
import uuid

from src.session_manager import db, db_lock


class TestA2ADispatchGuard1770:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            c.execute("DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            c.execute(
                "DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' "
                "OR repo_key LIKE 'owner/repo%' OR id LIKE 'sess-guard-%'")
            c.execute(
                "DELETE FROM agent_runtimes WHERE credential_id IN "
                "(SELECT id FROM runtime_credentials WHERE name LIKE 'guard-cred-%')")
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'guard-cred-%'")
            c.execute("DELETE FROM skill WHERE skill_name='implementation-guard'")
            try:
                c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
            except Exception:
                pass
            db.commit()

    def _seed(self):
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
                ("g" + uuid.uuid4().hex, "guard-cred-" + uuid.uuid4().hex[:6], "guard-dev"))
            _cid = c.lastrowid
            c.execute(
                "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, scope, owner_id, installed, healthy) "
                "VALUES ('opencode','opencode',?, 'online', datetime('now'), 'global', '', 1, 1)", (_cid,))
            _rid = c.lastrowid
            for _agent in ("coding", "qa"):
                c.execute(
                    "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, status, enable) "
                    "VALUES (?, 'gitcode:owner/repo', ?, ?, 'idle', 1)",
                    ("sess-guard-" + _agent, _agent, _rid))
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

    def _body(self, agent="coding", context="Issue #1770 门禁测试", force=False):
        return {
            "agent": agent,
            "payload": {
                "repository": "owner/repo",
                "account": "owner/repo",
                "issue_number": "5",
                "pr_number": "",
                "context": context,
            },
            "force": force,
        }

    def _setup(self):
        self._clean()
        self._seed()
        os.environ["DEVOPS_EXECUTION_MODE"] = "queue_remote"
        return self._client()

    def _seed_event(self, agent="qa", status="running", issue_id="#5"):
        from src.api.event_store import append_event
        return append_event(
            repo_key="gitcode:owner/repo", event_type="a2a_event",
            title=f"A2A: {agent}", issue_id=issue_id,
            source="a2a", agent_type=agent, summary=f"A2A: → {agent}",
            payload={"issue_number": "5", "context": f"seed-{agent}"},
            status=status)

    def _events_for(self, agent="coding", issue_id="#5"):
        with db_lock:
            c = db.cursor()
            return c.execute(
                "SELECT seq, status FROM events WHERE repo_key='gitcode:owner/repo' "
                "AND issue_id=? AND event_type='a2a_event' AND agent_type=? ORDER BY seq",
                (issue_id, agent)).fetchall()

    def _skips_for(self, issue_id="#5"):
        with db_lock:
            c = db.cursor()
            return c.execute(
                "SELECT reason, detail FROM event_skipped WHERE repo_key='gitcode:owner/repo' "
                "AND issue_id=? AND event_type='a2a_event' ORDER BY id", (issue_id,)).fetchall()

    def test_downstream_running_skips_upstream_dispatch(self):
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")
        assert qa_seq > 0

        resp = client.post("/api/a2a", json=self._body(agent="coding"))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["skipped"] == "downstream_running", data
        assert data["reason"] == f"downstream_running:{qa_seq}"
        assert data["downstream_event"]["seq"] == qa_seq
        assert data["downstream_event"]["agent"] == "qa"
        assert data["seq"] == 0
        assert self._events_for("coding") == [], "下游 running 时不应创建上游派发事件"

        skips = self._skips_for()
        assert any(r[0] == f"downstream_running:{qa_seq}" for r in skips), skips
        _reason, _detail = next(r for r in skips if r[0] == f"downstream_running:{qa_seq}")
        assert "downstream=qa" in _detail and f"seq={qa_seq}" in _detail

    def test_skipped_dispatch_does_not_move_issue_stage(self):
        client = self._setup()
        from src.api.session_store import set_issue_stage, get_issue_stage
        set_issue_stage("gitcode:owner/repo", "#5", "qa", "qa")
        qa_seq = self._seed_event(agent="qa", status="running")
        resp = client.post("/api/a2a", json=self._body(agent="coding"))
        assert resp.json()["skipped"] == "downstream_running"
        info = get_issue_stage("gitcode:owner/repo", "#5")
        assert info and info["stage"] == "qa", info
        assert qa_seq > 0

    def test_dispatch_resumes_after_downstream_completes(self):
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")
        blocked = client.post("/api/a2a", json=self._body(agent="coding")).json()
        assert blocked["skipped"] == "downstream_running"

        from src.api.event_store import update_event_complete
        update_event_complete(qa_seq, "completed", result="qa done")

        resp = client.post("/api/a2a", json=self._body(agent="coding"))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("skipped") not in ("downstream_running", "duplicate_key"), data
        assert data["seq"] > 0

    def test_duplicate_key_within_window_is_dropped(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(agent="coding"))
        first_seq = first.json()["seq"]
        assert first_seq > 0
        from src.api.event_store import update_event_complete
        update_event_complete(first_seq, "completed", result="空跑完成")

        dup = client.post("/api/a2a", json=self._body(agent="coding"))
        assert dup.status_code == 200, dup.text
        data = dup.json()
        assert data["skipped"] == "duplicate_key", data
        assert data["reason"] == f"duplicate_key:{first_seq}"
        assert data["existing_seq"] == first_seq
        assert len(self._events_for("coding")) == 1, "时间窗内重复派发不应创建新事件"
        assert any(r[0] == f"duplicate_key:{first_seq}" for r in self._skips_for())

    def test_force_bypasses_dispatch_guard(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(agent="coding"))
        first_seq = first.json()["seq"]
        from src.api.event_store import update_event_complete
        update_event_complete(first_seq, "completed", result="空跑完成")

        forced = client.post("/api/a2a", json=self._body(agent="coding", force=True))
        assert forced.status_code == 200, forced.text
        data = forced.json()
        assert data.get("skipped") not in ("duplicate_key", "downstream_running"), data
        assert data["seq"] > 0 and data["seq"] != first_seq

    def test_duplicate_expires_after_window(self):
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(agent="coding")).json()
        first_seq = first["seq"]
        from src.api.event_store import update_event_complete
        update_event_complete(first_seq, "completed", result="空跑完成")
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE events SET created_at=datetime('now','-1 hour') WHERE seq=?",
                      (first_seq,))
            db.commit()

        resp = client.post("/api/a2a", json=self._body(agent="coding"))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("skipped") != "duplicate_key", data
        assert data["seq"] > 0

    def test_storm_alert_on_repeated_dispatch(self):
        from src.api.a2a_dispatch_guard import detect_dispatch_storm
        self._clean()
        self._seed()
        self._seed_event(agent="coding", status="completed")
        result = detect_dispatch_storm(
            "gitcode:owner/repo", "5", "coding", upcoming=1)
        assert result["alerted"] is True
        assert result["count"] >= 2
        assert result["window"] > 0

    def test_idempotency_key_includes_scene_and_agent(self):
        from src.api.a2a_dispatch_guard import idempotency_key
        base = idempotency_key("5", "default", "coding")
        assert base.startswith("a2a:")
        assert base == idempotency_key("5", "default", "coding")
        assert base != idempotency_key("5", "default", "qa")
        assert base != idempotency_key("5", "other", "coding")
        assert base != idempotency_key("6", "default", "coding")