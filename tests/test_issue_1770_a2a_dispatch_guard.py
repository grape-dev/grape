"""Issue #1770：a2a_event 派发去重/幂等门禁测试。

覆盖验收标准：
- 下游（qa）running 时派发上游（coding）→ 跳过，event_skipped 留痕且原因可读；
- qa 结束后再次派发可正常执行；
- 同一幂等键（issue + scene + target agent + 时间窗）重复派发被丢弃并留痕；
- 短时间窗内同 Issue 同目标 agent 多次派发产生告警观测；
- force=true 可越过门禁（保持人工强制派发语义）。
"""
import os
import shutil
import uuid

from src.session_manager import db, db_lock


class TestA2ADispatchGuard1770:
    def _clean(self):
        with db_lock:
            c = db.cursor()
            c.execute(
                "DELETE FROM agent_tasks WHERE event_seq IN "
                "(SELECT seq FROM events WHERE repo_key LIKE 'gitcode:owner/repo%' "
                "OR repo_key LIKE 'owner/repo%')")
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            c.execute("DELETE FROM event_skipped WHERE repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
            c.execute(
                "DELETE FROM devops_agent_session WHERE repo_key LIKE 'gitcode:owner/repo%' "
                "OR repo_key LIKE 'owner/repo%' OR id LIKE 'sess-guard-%'")
            c.execute(
                "DELETE FROM agent_runtimes WHERE credential_id IN "
                "(SELECT id FROM runtime_credentials WHERE name LIKE 'guard-cred-%')")
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'guard-cred-%'")
            c.execute("DELETE FROM skill WHERE skill_name='implementation' AND scope='repo'")
            try:
                c.execute("DELETE FROM managed_repos WHERE repo_key='gitcode:owner/repo'")
            except Exception:
                pass
            db.commit()
        try:
            from src.config import SKILLS_DIR
            shutil.rmtree(
                os.path.join(SKILLS_DIR, "repo", "owner", "repo", "implementation"),
                ignore_errors=True)
        except Exception:
            pass

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

    def test_window_key_includes_scene_and_agent(self):
        from src.api.a2a_dispatch_guard import window_key
        base = window_key("5", "default", "coding")
        assert base.startswith("a2a:")
        assert base == window_key("5", "default", "coding")
        assert base != window_key("5", "default", "qa")
        assert base != window_key("5", "other", "coding")
        assert base != window_key("6", "default", "coding")

    def test_failed_dispatch_can_retry_immediately(self):
        """P2：失败事件不应计入时间窗去重，必须允许立即重试。"""
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(agent="coding")).json()
        first_seq = first["seq"]
        assert first_seq > 0
        from src.api.event_store import update_event_complete
        update_event_complete(first_seq, "failed", result="首次执行失败")

        retry = client.post("/api/a2a", json=self._body(agent="coding"))
        assert retry.status_code == 200, retry.text
        data = retry.json()
        assert data.get("skipped") not in ("duplicate_key", "active_dispatch"), data
        assert data["seq"] > 0 and data["seq"] != first_seq
        assert len(self._events_for("coding")) == 2

    def test_active_different_context_is_intercepted(self):
        """P3：同 issue+scene+agent 活跃时，不同 context 也不得另建第二个事件。"""
        client = self._setup()
        first = client.post("/api/a2a", json=self._body(agent="coding")).json()
        first_seq = first["seq"]
        assert first_seq > 0

        second = client.post(
            "/api/a2a", json=self._body(agent="coding", context="不同上下文的再次派发"))
        assert second.status_code == 200, second.text
        data = second.json()
        assert data["skipped"] == "active_dispatch", data
        assert data["reason"] == f"active_dispatch:{first_seq}"
        assert data["existing_seq"] == first_seq
        assert data["seq"] == 0
        assert len(self._events_for("coding")) == 1, "活跃期不同 context 不应创建第二个事件"
        assert any(r[0] == f"active_dispatch:{first_seq}" for r in self._skips_for())

    def test_repeated_skips_trigger_storm_alert(self):
        """P5：跳过路径不落 events，告警统计必须纳入 event_skipped 次数。"""
        client = self._setup()
        self._seed_event(agent="qa", status="running")
        for _ in range(2):
            resp = client.post("/api/a2a", json=self._body(agent="coding"))
            assert resp.json()["skipped"] == "downstream_running"

        from src.api.a2a_dispatch_guard import detect_dispatch_storm
        result = detect_dispatch_storm(
            "gitcode:owner/repo", "5", "coding", upcoming=0)
        assert result["alerted"] is True, result
        assert result["count"] >= 2, result

    def test_guarded_insert_is_atomic_under_concurrency(self):
        """P4：并发同键派发在同一 db_lock 内「复查+落库」，只允许一行。"""
        self._clean()
        self._seed()
        from concurrent.futures import ThreadPoolExecutor
        from src.api.a2a_context import a2a_context_key
        from src.api.a2a_dispatch_guard import append_guarded_a2a_event

        ctx_key = a2a_context_key("", "并发同键派发")
        payload = {"issue_number": "5", "context": "并发同键派发",
                   "idempotency_key": ""}

        def _dispatch(_i):
            return append_guarded_a2a_event(
                "gitcode:owner/repo", "5", "coding", "default",
                title="A2A: coding", summary="A2A: → coding",
                payload=dict(payload), context_key=ctx_key)

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(_dispatch, range(8)))

        ok = [r for r in results if r[0] == "ok"]
        dup = [r for r in results if r[0] in ("duplicate_key", "active_dispatch")]
        assert len(ok) == 1, f"并发同键应只落一行, got {results}"
        assert len(dup) == len(results) - 1, results
        assert len(self._events_for("coding")) == 1

    def test_dispatch_persists_issue_scene_id(self):
        """P6：普通 a2a 落库应持久化 Issue 场景维度，而非空 scene。"""
        client = self._setup()
        resp = client.post("/api/a2a", json=self._body(agent="coding")).json()
        assert resp["seq"] > 0
        with db_lock:
            row = db.cursor().execute(
                "SELECT scene_id FROM events WHERE seq=?", (resp["seq"],)).fetchone()
        assert row and row[0] == "default", row