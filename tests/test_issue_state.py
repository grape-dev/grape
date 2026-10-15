"""issue_state 生命周期状态机测试（见 vod-skill-upgrade-plan.md L1/L8）"""
import pytest

from src.session_manager import db, db_lock
from src.api import session_store as ss


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM issue_state WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.cursor().execute("DELETE FROM issue WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM issue_state WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.cursor().execute("DELETE FROM issue WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        db.commit()


class TestIssueState:
    def test_initial_vod_default(self):
        assert ss.get_issue_owner("gitcode:test/repo", "#5") == "vod"
        assert ss.get_issue_stage_name("gitcode:test/repo", "#5") == "vod"
        assert ss.get_issue_stage("gitcode:test/repo", "#5") is None

    def test_set_and_get(self):
        ss.set_issue_stage("gitcode:test/repo", "#5", "vod", "vod", a2a_seq=None)
        st = ss.get_issue_stage("gitcode:test/repo", "#5")
        assert st and st["stage"] == "vod" and st["owner_agent"] == "vod"

    def test_stage_transition(self):
        # vod → issue → coding → qa → sre → done
        for stage, owner in [("issue", "issue"), ("coding", "coding"),
                             ("qa", "qa"), ("sre", "sre"), ("done", "done")]:
            ss.set_issue_stage("gitcode:test/repo", "#7", stage, owner)
            assert ss.get_issue_owner("gitcode:test/repo", "#7") == owner
            assert ss.get_issue_stage_name("gitcode:test/repo", "#7") == stage

    def test_upsert_idempotent(self):
        ss.set_issue_stage("gitcode:test/repo", "#9", "issue", "issue")
        ss.set_issue_stage("gitcode:test/repo", "#9", "coding", "coding")
        # Phase C3: set_issue_stage 只写 issue 单表（不再写 issue_state）
        with db_lock:
            cnt = db.cursor().execute("SELECT COUNT(*) FROM issue WHERE repo_key='gitcode:test/repo' AND issue_number=9").fetchone()[0]
        assert cnt == 1, "upsert 应保持单行"

    def test_rollback_allowed(self):
        ss.set_issue_stage("gitcode:test/repo", "#10", "qa", "qa")
        ss.set_issue_stage("gitcode:test/repo", "#10", "coding", "coding")
        assert ss.get_issue_owner("gitcode:test/repo", "#10") == "coding"

    def test_done_stops(self):
        ss.set_issue_stage("gitcode:test/repo", "#11", "done", "done")
        assert ss.get_issue_stage_name("gitcode:test/repo", "#11") == "done"
