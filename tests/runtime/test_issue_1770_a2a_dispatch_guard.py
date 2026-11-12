"""Issue #1770：a2a_event 派发去重/幂等门禁测试。

覆盖验收标准：
- 下游（qa）running 时派发上游（coding）→ 跳过，event_skipped 留痕且原因可读；
- qa 结束后再次派发可正常执行；
- 同一幂等键（issue + scene + target agent + 时间窗）重复派发被丢弃并留痕；
- 短时间窗内同 Issue 同目标 agent 多次派发产生告警观测；
- force=true 可越过门禁（保持人工强制派发语义）。
"""
from src.kernel.session_manager import db, db_lock

from tests.runtime._a2a_guard_support import A2AGuardTestBase


class TestA2ADispatchGuard1770(A2AGuardTestBase):

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
    def test_exclude_seq_skips_caller_own_running_event(self):
        """Issue #1973：exclude_seq 精确排除调用方自身 running 事件，其余下游仍拦截。"""
        from src.api.a2a_dispatch_guard import find_running_downstream
        self._clean()
        self._seed()
        qa_seq = self._seed_event(agent="qa", status="running")
        nodes = [{"id": "coding", "next": "qa"}, {"id": "qa", "next": ""}]

        own = find_running_downstream(
            "gitcode:owner/repo", "5", "coding", "default", nodes,
            exclude_seq=qa_seq)
        assert own == {}, "调用方自身 running 事件不应判为下游 running"

        other = find_running_downstream(
            "gitcode:owner/repo", "5", "coding", "default", nodes)
        assert other and other["seq"] == qa_seq, "异源下游 running 仍应拦截"

    def test_downstream_skip_without_queue_is_explicit_not_queued(self):
        """Issue #1973：未声明 queue 时必须显式 not_queued，不得返回 ok+skipped。"""
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")

        resp = client.post("/api/a2a", json=self._body(agent="coding"))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["skipped"] == "downstream_running", data
        assert data["status"] == "not_queued", data
        assert data["queued"] is False, data
        assert data["queueable"] is True and data["retry_with_queue"] is True, data
        assert data["downstream_event"]["seq"] == qa_seq

    def _complete(self, seq):
        from src.api.event_store import update_event_complete
        update_event_complete(seq, "completed", result="空跑完成")

    def test_top_level_force_resends_duplicate_key(self):
        """Issue #1942：幂等窗内顶层 force=true 一次成功重发。"""
        client = self._setup()
        first_seq = client.post("/api/a2a", json=self._body(agent="coding")).json()["seq"]
        assert first_seq > 0
        self._complete(first_seq)

        dup = client.post("/api/a2a", json=self._body(agent="coding")).json()
        assert dup["skipped"] == "duplicate_key", dup

        forced = client.post("/api/a2a", json=self._body(agent="coding", force=True))
        assert forced.status_code == 200, forced.text
        data = forced.json()
        assert data.get("skipped") not in ("duplicate_key", "active_dispatch"), data
        assert data["seq"] > 0 and data["seq"] != first_seq

    def test_payload_force_resends_duplicate_key(self):
        """Issue #1942：payload.force=true 与顶层 force 等价，可一次成功重发。"""
        client = self._setup()
        first_seq = client.post("/api/a2a", json=self._body(agent="coding")).json()["seq"]
        assert first_seq > 0
        self._complete(first_seq)

        dup = client.post("/api/a2a", json=self._body(agent="coding")).json()
        assert dup["skipped"] == "duplicate_key", dup

        body = self._body(agent="coding")
        body.pop("force", None)
        body["payload"]["force"] = True
        forced = client.post("/api/a2a", json=body)
        assert forced.status_code == 200, forced.text
        data = forced.json()
        assert data.get("skipped") not in ("duplicate_key", "active_dispatch"), data
        assert data["seq"] > 0 and data["seq"] != first_seq

    def test_changing_idempotency_key_still_duplicate(self):
        """Issue #1942：幂等窗口键 = issue+scene+agent，更换 idempotency_key 无效。"""
        client = self._setup()
        first = self._body(agent="coding")
        first["idempotency_key"] = "task-42:coding"
        first_seq = client.post("/api/a2a", json=first).json()["seq"]
        assert first_seq > 0
        self._complete(first_seq)

        second = self._body(agent="coding")
        second["idempotency_key"] = "task-43:coding"
        resp = client.post("/api/a2a", json=second)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["skipped"] == "duplicate_key", data
        assert data["reason"] == f"duplicate_key:{first_seq}"
        assert data["seq"] == 0
        assert len(self._events_for("coding")) == 1

    def test_guard_message_documents_force_position(self):
        """Issue #1942：三类门禁 message 均说明 force 位置与「换 key 无效」。"""
        import json as _json
        from src.domains.runtime.routes.webhook import _a2a_guard_skip_response
        info = {"seq": 7, "agent": "qa", "status": "running", "created_at": ""}
        for kind in ("downstream_running", "duplicate_key", "active_dispatch"):
            resp = _a2a_guard_skip_response(
                "gitcode:owner/repo", "5", "coding", "default", kind, dict(info))
            msg = _json.loads(bytes(resp.body).decode())["message"]
            assert "force=true to dispatch immediately" in msg, (kind, msg)
            assert "top level" in msg and "payload.force=true" in msg, (kind, msg)
            assert "equivalent" in msg, (kind, msg)
            assert "idempotency_key" in msg and "issue+scene+agent" in msg, (kind, msg)

    def test_duplicate_key_message_contains_recovery_hint(self):
        """Issue #1942：duplicate_key 响应的 message 可直接指导一次成功重发。"""
        client = self._setup()
        first_seq = client.post("/api/a2a", json=self._body(agent="coding")).json()["seq"]
        self._complete(first_seq)
        data = client.post("/api/a2a", json=self._body(agent="coding")).json()
        assert data["skipped"] == "duplicate_key", data
        msg = data["message"]
        assert "force=true to dispatch immediately" in msg
        assert "payload.force=true" in msg
        assert "Changing idempotency_key will not bypass this guard" in msg
        assert "issue+scene+agent" in msg
