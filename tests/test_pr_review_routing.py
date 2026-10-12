"""Issue #304: pull_request_review 事件阶段矩阵路由测试"""
import pytest

from src.session_manager import db, db_lock
from src.api import event_pipeline as ep
from src.api import session_store as ss
from src.api.event_pipeline import _resolve_issue_stage_agent, _STAGE_EVENT_AGENT_MAP


_CLEAN_PATTERNS = "test_% OR gitcode:test_% OR gitcode:org/% OR org/% OR gitcode:owner/% OR owner/% OR gitcode:tc_% OR gitcode:o/r OR o/r OR gitcode:a/% OR a/b OR gitcode:b/% OR gitcode:x/% OR gitcode:new/% OR gitcode:empty/% OR gitcode:navuser/% OR gitcode:nonexistent/% OR gitcode:repair/% OR gitcode:ghost/% OR gitcode:iso/% OR gitcode:iac/% OR gitcode:Jason7602/% OR gitcode:evapi/% OR gitcode:sweep/% OR gitcode:no-% OR other/% OR github:org/% OR github:other/% OR gitcode:chat:%"

_TABLES = ["issue_state", "issue", "events", "event_skipped", "agent_tasks"]


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        for tbl in _TABLES:
            c.execute(f"DELETE FROM {tbl} WHERE repo_key LIKE {_CLEAN_PATTERNS!r}".replace(f"{_CLEAN_PATTERNS!r}", "'" + _CLEAN_PATTERNS + "'"))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        for tbl in _TABLES:
            c.execute(f"DELETE FROM {tbl} WHERE repo_key LIKE '" + _CLEAN_PATTERNS + "'")
        db.commit()


RK = "gitcode:test/repo"
PR_REVIEW_PAYLOAD = {"pull_request": {"number": 10, "title": "feat: fix #5"},
                     "object_attributes": {"id": 10, "iid": 10, "title": "feat: fix #5"}}


class TestPRReviewStageRouting:
    """Issue #304: pull_request_review 事件各阶段路由测试"""

    def test_map_contains_pull_request_review(self):
        assert "pull_request_review" in _STAGE_EVENT_AGENT_MAP

    def test_pr_review_stage_coding_to_coding(self):
        ss.set_issue_stage(RK, "#5", "coding", "coding")
        assert _resolve_issue_stage_agent(RK, "pull_request_review", PR_REVIEW_PAYLOAD, "issue") == "coding"

    def test_pr_review_stage_qa_to_qa(self):
        ss.set_issue_stage(RK, "#5", "qa", "qa")
        assert _resolve_issue_stage_agent(RK, "pull_request_review", PR_REVIEW_PAYLOAD, "issue") == "qa"

    def test_pr_review_stage_sre_to_sre(self):
        ss.set_issue_stage(RK, "#5", "sre", "sre")
        assert _resolve_issue_stage_agent(RK, "pull_request_review", PR_REVIEW_PAYLOAD, "issue") == "sre"

    def test_pr_review_stage_issue_to_issue(self):
        ss.set_issue_stage(RK, "#5", "issue", "issue")
        assert _resolve_issue_stage_agent(RK, "pull_request_review", PR_REVIEW_PAYLOAD, "qa") == "issue"

    def test_pr_review_stage_vod_to_issue(self):
        ss.set_issue_stage(RK, "#5", "vod", "vod")
        assert _resolve_issue_stage_agent(RK, "pull_request_review", PR_REVIEW_PAYLOAD, "coding") == "issue"

    def test_pr_review_stage_done_no_dispatch(self):
        ss.set_issue_stage(RK, "#5", "done", "done")
        assert _resolve_issue_stage_agent(RK, "pull_request_review", PR_REVIEW_PAYLOAD, "qa") == ""

    def test_pr_review_no_issue_not_dispatched(self):
        pr_no_issue = {"pull_request": {"number": 99, "title": "docs: fix typo"},
                       "object_attributes": {"id": 99, "iid": 99, "title": "docs: fix typo"}}
        assert _resolve_issue_stage_agent(RK, "pull_request_review", pr_no_issue, "") == ""

    def test_pr_review_with_issue_but_no_stage_not_dispatched(self):
        assert _resolve_issue_stage_agent(RK, "pull_request_review", PR_REVIEW_PAYLOAD, "") == ""


class TestNoteHookPRNoRegression:
    """note_hook_pr 既有路由行为无回归"""

    def test_note_hook_pr_stage_coding_to_coding(self):
        ss.set_issue_stage(RK, "#5", "coding", "coding")
        assert _resolve_issue_stage_agent(RK, "note_hook_pr", PR_REVIEW_PAYLOAD, "issue") == "coding"

    def test_note_hook_pr_stage_qa_to_qa(self):
        ss.set_issue_stage(RK, "#5", "qa", "qa")
        assert _resolve_issue_stage_agent(RK, "note_hook_pr", PR_REVIEW_PAYLOAD, "issue") == "qa"

    def test_note_hook_pr_stage_sre_to_sre(self):
        ss.set_issue_stage(RK, "#5", "sre", "sre")
        assert _resolve_issue_stage_agent(RK, "note_hook_pr", PR_REVIEW_PAYLOAD, "issue") == "sre"
