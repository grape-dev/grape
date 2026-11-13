"""Issue #1973：A2A 派发门禁自锁与 queue 失效回归测试。

背景：同一 Issue 的调用方（qa）自身事件 running 时，``find_running_downstream``
把调用方自己的事件误判为「下游 running」，导致 QA→coding 打回必然 skipped；
且 ``queue=true`` 越不过该门禁、不落库，返回 ``queued=false`` 误导调用方。

覆盖验收标准：
- 同 Issue 源事件 running 中派发下游 → 放行（self exclude）；
- 真实下游 running + queue=true → 落 status=queued 行，a2a status queued_count 递增，
  阻塞解除后自动补派；
- 异源下游 running 仍被拦截（#1770 防抖不退化）；
- 未声明 queue 时返回显式 ``status=not_queued`` + ``queueable`` 语义，而非 ok+skipped。
"""
from src.kernel.session_manager import db, db_lock

from tests.runtime._a2a_guard_support import A2AGuardTestBase, REPO_KEY


class TestA2ASelfDispatchAndQueue1973(A2AGuardTestBase):

    def _body(self, agent="coding", queue=False, context="Issue #1973 自锁回归"):
        return {
            "agent": agent,
            "payload": {
                "repository": "owner/repo",
                "account": "owner/repo",
                "issue_number": "5",
                "pr_number": "",
                "context": context,
            },
            "queue": queue,
        }

    def test_self_running_excluded_allows_reverse_dispatch(self):
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")
        token = self._seed_caller_session(qa_seq, agent="qa")

        resp = client.post(
            "/api/a2a", json=self._body(agent="coding"),
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("skipped") != "downstream_running", data
        assert data["seq"] > 0, data
        assert len(self._events_for("coding")) == 1

    def test_other_source_still_blocked_by_downstream_running(self):
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")
        other_seq = self._seed_event(agent="qa", status="running")
        token = self._seed_caller_session(other_seq, agent="qa")

        resp = client.post(
            "/api/a2a", json=self._body(agent="coding"),
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["skipped"] == "downstream_running", data
        assert data["downstream_event"]["seq"] == qa_seq
        assert data["downstream_event"]["agent"] == "qa"
        assert self._events_for("coding") == []

    def test_downstream_skip_is_explicit_not_queued(self):
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")

        resp = client.post("/api/a2a", json=self._body(agent="coding", queue=False))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["skipped"] == "downstream_running", data
        assert data["status"] == "not_queued", data
        assert data["queued"] is False, data
        assert data["queueable"] is True and data["retry_with_queue"] is True, data
        assert data["action"] == "retry_with_queue_true", data
        assert data["downstream_event"]["seq"] == qa_seq
        assert self._events_for("coding") == [], "未声明 queue 不应落库"

    def test_queue_true_enqueues_and_auto_dispatches_after_downstream(self):
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")

        resp = client.post("/api/a2a", json=self._body(agent="coding", queue=True))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["skipped"] == "queued", data
        assert data["queued"] is True
        assert data["reason"] == "downstream_running"
        assert data["existing_seq"] == qa_seq
        queue_seq = data["queue_seq"]
        assert queue_seq > 0
        rows = self._events_for("coding")
        assert len(rows) == 1 and rows[0][0] == queue_seq and rows[0][1] == "queued"

        # a2a status 的 queued_count 必须体现刚落的排队行
        status = client.get(
            f"/api/a2a/status?repository=owner/repo&issue_number=5&agent=coding")
        assert status.status_code == 200, status.text
        assert status.json()["queued_count"] == 1, status.json()
        assert status.json()["queued"][0]["seq"] == queue_seq

        from src.api.event_store import update_event_complete
        update_event_complete(qa_seq, "completed", result="qa done")

        rows = self._events_for("coding")
        assert len(rows) == 1, rows
        assert rows[0][1] == "running", "下游完成后排队事件应被自动补派"
        with db_lock:
            c = db.cursor()
            tasks = c.execute(
                "SELECT id FROM agent_tasks WHERE event_seq=?", (queue_seq,)).fetchall()
        assert len(tasks) == 1, f"排队事件应恰好派发一次, got {tasks}"

    def test_queue_reuses_existing_row_for_same_context(self):
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")

        first = client.post("/api/a2a", json=self._body(agent="coding", queue=True)).json()
        second = client.post("/api/a2a", json=self._body(agent="coding", queue=True)).json()
        assert first["queue_seq"] > 0
        assert second["queued"] is True and second["idempotent"] is True, second
        assert second["queue_seq"] == first["queue_seq"]
        assert second["queue_seq"] not in second["blocking_seqs"], second
        assert len(self._events_for("coding")) == 1

    def test_queue_waits_for_all_downstream_blockers(self):
        """复审 a：多个下游活跃时，只等最新一个会提前补派，应等全部终态。"""
        client = self._setup()
        older = self._seed_event(agent="qa", status="running")
        newer = self._seed_event(agent="qa", status="running")

        data = client.post(
            "/api/a2a", json=self._body(agent="coding", queue=True)).json()
        assert data["skipped"] == "queued", data
        assert data["blocking_seqs"] == sorted([older, newer]), data

        from src.api.event_store import update_event_complete
        update_event_complete(newer, "completed", result="newer done")
        rows = self._events_for("coding")
        assert len(rows) == 1 and rows[0][1] == "queued", \
            f"更早的下游仍 running，不应提前补派: {rows}"

        update_event_complete(older, "completed", result="older done")
        rows = self._events_for("coding")
        assert len(rows) == 1 and rows[0][1] == "running", rows

    def test_queue_also_waits_for_active_target_agent(self):
        """复审 b：目标 agent 自身活跃时只等下游会补派出第二份同 agent 任务。"""
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")
        coding_seq = self._seed_event(agent="coding", status="running")

        data = client.post(
            "/api/a2a", json=self._body(agent="coding", queue=True)).json()
        assert data["skipped"] == "queued", data
        assert data["blocking_seqs"] == sorted([qa_seq, coding_seq]), data

        from src.api.event_store import update_event_complete
        update_event_complete(qa_seq, "completed", result="qa done")
        rows = self._events_for("coding")
        assert len(rows) == 2, rows
        assert [r[1] for r in rows].count("queued") == 1, \
            f"目标 agent 自身仍 running，不应补派: {rows}"

        update_event_complete(coding_seq, "completed", result="coding done")
        rows = self._events_for("coding")
        assert [r[1] for r in rows].count("running") == 1, rows
        assert [r[1] for r in rows].count("completed") == 1, rows

    def test_queue_advances_issue_stage_to_target(self):
        """复审 c：排队分支此前早于 stage 推进返回，Issue 归属应归目标 agent。"""
        from src.api.session_store import set_issue_stage, get_issue_stage
        client = self._setup()
        set_issue_stage(REPO_KEY, "#5", "qa", "qa")
        self._seed_event(agent="qa", status="running")

        data = client.post(
            "/api/a2a", json=self._body(agent="coding", queue=True)).json()
        assert data["skipped"] == "queued", data
        info = get_issue_stage(REPO_KEY, "#5")
        assert info and info["stage"] == "coding", info
    def test_queue_includes_already_queued_downstream_in_blockers(self):
        """复审残留 1：已排队的下游也必须进入 blocking_seqs，与 drain 活跃语义对齐。"""
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")
        queued_qa_seq = self._seed_event(agent="qa", status="queued")

        data = client.post(
            "/api/a2a", json=self._body(agent="coding", queue=True)).json()
        assert data["skipped"] == "queued", data
        assert qa_seq in data["blocking_seqs"], data
        assert queued_qa_seq in data["blocking_seqs"], data

    def test_queue_blocks_on_all_active_target_events_in_scene(self):
        """复审残留 2：目标 agent 同场景多个活跃事件全部纳入阻塞，避免漏判双跑。"""
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")
        coding_first = self._seed_event(agent="coding", status="running")
        coding_second = self._seed_event(agent="coding", status="running")

        data = client.post(
            "/api/a2a", json=self._body(agent="coding", queue=True)).json()
        assert data["skipped"] == "queued", data
        assert coding_first in data["blocking_seqs"], data
        assert coding_second in data["blocking_seqs"], data
        assert qa_seq in data["blocking_seqs"], data

    def test_queue_ignores_cross_scene_target_active_event(self):
        """复审残留 2：异场景目标活跃不应过度阻塞本单排队。"""
        client = self._setup()
        qa_seq = self._seed_event(agent="qa", status="running")
        other_scene_seq = self._seed_event(
            agent="coding", status="running", scene_id="other-scene")

        data = client.post(
            "/api/a2a", json=self._body(agent="coding", queue=True)).json()
        assert data["skipped"] == "queued", data
        assert qa_seq in data["blocking_seqs"], data
        assert other_scene_seq not in data["blocking_seqs"], data
