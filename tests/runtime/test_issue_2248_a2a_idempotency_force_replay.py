"""Issue #2248：固定 idempotency_key 不再永久吞掉修复轮重派。

覆盖：
- 幂等键命中终态旧事件 + 无 force → 返回原事件（idempotent:true，状态可判定）；
- 幂等键命中终态旧事件 + force=true → 越过命中短路，新轮次键落库并返回新 seq；
- 修复轮使用带轮次/head sha 的新键 → 正常新建事件，不判 duplicate；
- 幂等键命中活跃事件 + force=true → 仍返回原事件（不重复并发派发）。

测试 seam：`/api/a2a` HTTP 响应 + events 表行数（公开行为）。
"""
from tests.runtime._a2a_guard_support import A2AGuardTestBase


class TestA2AIdempotencyForceReplay2248(A2AGuardTestBase):

    def _body(self, *, key, agent="coding", issue_number="5", force=False):
        body = {
            "agent": agent,
            "payload": {
                "repository": "owner/repo",
                "account": "owner/repo",
                "issue_number": issue_number,
                "pr_number": "",
                "context": f"Issue #2248 修复轮重派测试 key={key}",
            },
        }
        if key is not None:
            body["idempotency_key"] = key
        if force:
            body["force"] = True
        return body

    def _post(self, client, body):
        return client.post("/api/a2a", json=body)

    def _a2a_event_count(self):
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            return db.cursor().execute(
                "SELECT COUNT(*) FROM events WHERE event_type='a2a_event'"
                " AND repo_key LIKE 'gitcode:owner/repo%'").fetchone()[0]

    def _complete(self, seq):
        from src.api.event_store import update_event_complete
        update_event_complete(seq, "completed", result="首轮 QA 完成")

    def _age_event(self, seq, delta="-1 hour"):
        """把事件创建时间前移，模拟修复轮已越过派发门禁时间窗（#2241 范围外）。"""
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE events SET created_at=datetime('now', ?) WHERE seq=?", (delta, seq))
            db.commit()

    def test_no_force_returns_existing_event(self):
        """key 命中 + 无 force → 返回原事件，不产生第二条。"""
        client = self._setup()
        r1 = self._post(client, self._body(key="issue-5-a2a-qa-r1"))
        assert r1.status_code == 200, r1.text
        seq1 = r1.json()["seq"]
        assert seq1 > 0, r1.text
        self._complete(seq1)

        r2 = self._post(client, self._body(key="issue-5-a2a-qa-r1"))
        assert r2.status_code == 200, r2.text
        j2 = r2.json()
        assert j2.get("idempotent") is True, j2
        assert j2.get("seq") == seq1, j2
        assert j2.get("existing_status") == "completed", j2
        assert j2.get("replayable") is True, j2
        assert self._a2a_event_count() == 1, "同键无 force 重复请求不得创建新事件"

    def test_force_true_replays_terminal_key_hit(self):
        """key 命中 + force=true 且旧事件终态 → 新 seq（修复轮可重派）。"""
        client = self._setup()
        r1 = self._post(client, self._body(key="issue-5-a2a-qa-r1"))
        seq1 = r1.json()["seq"]
        assert seq1 > 0, r1.text
        self._complete(seq1)

        r2 = self._post(client, self._body(key="issue-5-a2a-qa-r1", force=True))
        assert r2.status_code == 200, r2.text
        j2 = r2.json()
        assert j2.get("seq", 0) > 0, j2
        assert j2.get("seq") != seq1, "force 重派必须返回新事件 seq"
        assert j2.get("handoff_result") == "ok", j2
        assert self._a2a_event_count() == 2, "force 重派应新增一条事件"

    def test_new_round_key_dispatches_new_event(self):
        """修复轮使用轮次/sha 新键 → 新事件，不判 duplicate。"""
        client = self._setup()
        r1 = self._post(client, self._body(key="issue-5-a2a-qa-r1"))
        seq1 = r1.json()["seq"]
        assert seq1 > 0, r1.text
        self._complete(seq1)
        self._age_event(seq1)

        r2 = self._post(client, self._body(key="issue-5-a2a-qa-r2-abc1234"))
        assert r2.status_code == 200, r2.text
        j2 = r2.json()
        assert j2.get("seq", 0) > 0, j2
        assert j2.get("seq") != seq1, j2
        assert not j2.get("idempotent"), j2
        assert self._a2a_event_count() == 2

    def test_force_true_active_hit_still_returns_existing(self):
        """命中活跃事件时 force 不重复并发派发，仍返回原事件。"""
        client = self._setup()
        r1 = self._post(client, self._body(key="issue-5-a2a-qa-active"))
        seq1 = r1.json()["seq"]
        assert seq1 > 0, r1.text

        r2 = self._post(client, self._body(key="issue-5-a2a-qa-active", force=True))
        assert r2.status_code == 200, r2.text
        j2 = r2.json()
        assert j2.get("idempotent") is True, j2
        assert j2.get("seq") == seq1, j2
        assert j2.get("existing_status") == "running", j2
        assert j2.get("replayable") is False, j2
        assert self._a2a_event_count() == 1