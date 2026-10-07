"""issues pending-review（待审核·Agent 显式阶段审核点）测试（issue-pending-review-explicit-design.md）。

任意 devops agent（vod/issue/coding/qa/sre）经会话 tk_ 显式：pending-review（进入待审核，不改 stage）、
review-pass（判定通过→processing/done）、review-reject（判定未通过→processing/awaiting_input）。
与 awaiting-input 对称（等用户双态）。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.user import db as user_db

client = TestClient(app)

_uid = None
_BOT_TOKEN = "bot_review_test_token"


def _session_headers(repo_key="gitcode:org/test", agent_type="qa"):
    import uuid
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"rev-{uuid.uuid4().hex}", repo_key=repo_key, agent_type=agent_type,
        user_id=(_uid["id"] if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _uid
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username='review_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='review_eric'")
        db.commit()
    _uid = user_db.create_user("review_user", "pw123")
    if not _uid:
        _uid = user_db.get_user_by_username("review_user")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) "
                  "VALUES (?,?,?,?,datetime('now'))", (_uid["id"], "review_eric", "ou_review", "Eric"))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
                  "VALUES ('gitcode:org/test','org','test','org/test',1,?)", (_BOT_TOKEN,))
        db.commit()
    user_db.mark_user_repo_join(_uid["id"], "gitcode:org/test", True, True)


def teardown_module():
    if _uid:
        user_db.delete_user(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username='review_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='review_eric'")
        db.commit()


def _seed(status="processing", issue_number=21):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key='gitcode:org/test' AND issue_number=?", (issue_number,))
        c.execute("DELETE FROM chat_conversations WHERE repo_key='gitcode:org/test' AND title=?", (f"Issue #{issue_number}",))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (_uid["id"], "review_eric", "gitcode:org/test", issue_number, "待审核需求", "d", status))
        card_id = c.lastrowid
        db.commit()
        return card_id


def _status(issue_number=21):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, stage FROM issue WHERE repo_key='gitcode:org/test' AND issue_number=?",
            (issue_number,)).fetchone()


class TestPendingReview:
    def test_qa_sets_pending_review(self):
        cid = _seed()
        r = client.post(f"/api/issues/{cid}/pending-review",
                        headers=_session_headers(agent_type="qa"),
                        json={"review_points": "请确认是否合入该 PR（#11 变更）", "references": "PR#11"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True and data["issue_id"] == cid
        assert data["status"] == "pending_review" and data["changed"] is True
        st = _status()
        assert st[0] == "pending_review"
        assert st[1] == "vod"  # 不改 stage（seed 默认 stage 由 issue 建档为 vod/processing）

    def test_any_devops_agent_allowed(self):
        for agent in ("vod", "issue", "coding", "sre"):
            cid = _seed(issue_number=21 if agent != "sre" else 22)
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_session_headers(agent_type=agent),
                            json={"review_points": "审核点"})
            assert r.status_code == 200 and r.json()["changed"] is True

    def test_non_devops_agent_403(self):
        cid = _seed()
        r = client.post(f"/api/issues/{cid}/pending-review",
                        headers=_session_headers(agent_type="chat"), json={"review_points": "x"})
        assert r.status_code == 403

    def test_repo_mismatch_403(self):
        cid = _seed()
        r = client.post(f"/api/issues/{cid}/pending-review",
                        headers=_session_headers(repo_key="gitcode:org/other", agent_type="qa"),
                        json={"review_points": "x"})
        assert r.status_code == 403

    def test_non_session_403(self):
        cid = _seed()
        r = client.post(f"/api/issues/{cid}/pending-review",
                        headers={"Authorization": f"Bearer {_BOT_TOKEN}"}, json={"review_points": "x"})
        assert r.status_code in (401, 403)

    def test_terminal_or_duplicate_changed_false(self):
        cid = _seed(status="pending_review")
        r = client.post(f"/api/issues/{cid}/pending-review",
                        headers=_session_headers(agent_type="qa"), json={"review_points": "x"})
        assert r.json()["changed"] is False

    def test_review_pass_processing(self):
        cid = _seed(status="pending_review")
        r = client.post(f"/api/issues/{cid}/review-pass",
                        headers=_session_headers(agent_type="qa"), json={"target": "processing", "evidence": "用户确认 ok"})
        assert r.status_code == 200
        assert _status()[0] == "processing"

    def test_review_pass_done(self):
        cid = _seed(status="pending_review")
        r = client.post(f"/api/issues/{cid}/review-pass",
                        headers=_session_headers(agent_type="qa"), json={"target": "done"})
        assert r.status_code == 200
        st = _status()
        assert st[0] == "completed"
        assert st[1] == "done"

    def test_review_reject_to_awaiting_input(self):
        cid = _seed(status="pending_review")
        r = client.post(f"/api/issues/{cid}/review-reject",
                        headers=_session_headers(agent_type="qa"),
                        json={"target": "awaiting_input", "reason": "需求仍未明确"})
        assert r.status_code == 200
        assert _status()[0] == "awaiting_input"

    def test_review_reject_to_processing(self):
        cid = _seed(status="pending_review")
        r = client.post(f"/api/issues/{cid}/review-reject",
                        headers=_session_headers(agent_type="qa"), json={"target": "processing"})
        assert r.status_code == 200
        assert _status()[0] == "processing"

    def test_review_action_idempotent_when_not_pending(self):
        cid = _seed(status="processing")
        r = client.post(f"/api/issues/{cid}/review-pass",
                        headers=_session_headers(agent_type="qa"), json={"target": "processing"})
        assert r.json()["result"] == "noop"
        assert _status()[0] == "processing"