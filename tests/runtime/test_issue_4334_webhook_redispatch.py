"""Issue #4334：A2A 入口接入 watchdog 重派守卫的集成测试。

覆盖：
- qa 通过后的 watchdog 重派被 pipeline 终态短路，并在 event_skipped 真实留痕
  （issue_id 单井号、reason/detail 可追溯）；
- qa 打回（completed 但结论不通过）不得被误判终态，coding 返工重派必须放行。
"""
from src.kernel.session_manager import db, db_lock

from src.api import watchdog_redispatch_guard as guard
from tests.runtime._a2a_guard_support import A2AGuardTestBase, REPO_KEY


class TestWebhookRedispatchGuard(A2AGuardTestBase):
    def _body(self, context="原任务结论：已实现并通过 QA", issue_number="5", **payload):
        data = {
            "repository": "owner/repo",
            "account": "owner/repo",
            "issue_number": issue_number,
            "pr_number": "",
            "context": context,
            "redispatch_mode": "watchdog",
        }
        data.update(payload)
        return {"agent": "coding", "payload": data}

    def _seed_qa(self, result, status="completed"):
        from src.api.event_store import append_event
        return append_event(
            repo_key=REPO_KEY, event_type="a2a_event", title="A2A: qa",
            issue_id="#5", source="a2a", agent_type="qa",
            summary="A2A: → qa", payload={"issue_number": "5", "context": "seed-qa"},
            status=status, result=result, scene_id="")

    def test_qa_pass_redispatch_skipped_and_audited(self):
        client = self._setup()
        self._seed_qa("结论：通过，0 项需修复")
        resp = client.post("/api/a2a", json=self._body())
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("skipped") == guard.PIPELINE_TERMINAL_REASON, data
        assert data["seq"] == 0

        skips = self._skips_for("#5")
        assert any(r[0] == guard.PIPELINE_TERMINAL_REASON for r in skips), skips
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT issue_id, detail FROM event_skipped WHERE repo_key=? "
                "ORDER BY id DESC LIMIT 1", (REPO_KEY,)).fetchone()
        assert row is not None
        assert row[0] == "#5", row
        assert "criteria=" in row[1] and "qa_passed" in row[1], row

    def test_qa_rejection_allows_coding_redispatch(self):
        client = self._setup()
        self._seed_qa("审查不通过：需返工")
        resp = client.post("/api/a2a", json=self._body())
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("skipped") != guard.PIPELINE_TERMINAL_REASON, data
        assert data["seq"] > 0