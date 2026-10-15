"""issues pending-review（待审核·Agent 显式阶段审核点）测试（issue-pending-review-explicit-design.md）。

任意 devops agent（vod/issue/coding/qa/sre）经会话 tk_ 显式：pending-review（进入待审核，不改 stage）、
review-pass（判定通过→processing/done）、review-reject（判定未通过→processing/awaiting_input）。
与 awaiting-input 对称（等用户双态）。
"""
import os
import sys
import asyncio
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import issues as issues_mod
from src.session_manager import db, db_lock
from src.user import db as user_db
from src.auth import grape_account_db

client = TestClient(app)

_uid = None
_BOT_TOKEN = "bot_review_test_token"


def _swallow(coro, *a, **k):
    try:
        coro.close()
    except Exception:
        pass
    return None


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
        c.execute("DELETE FROM grape_accounts WHERE username='review_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='review_eric'")
        db.commit()
    _uid = grape_account_db.create_grape_account("review_user", email=None, password="pw123")
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("review_user")
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
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='review_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='review_eric'")
        db.commit()


def _seed(status="processing", issue_number=21, assignee="review_eric", stage="vod"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key='gitcode:org/test' AND issue_number=?", (issue_number,))
        c.execute("DELETE FROM chat_conversations WHERE repo_key='gitcode:org/test' AND title=?", (f"Issue #{issue_number}",))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status, stage) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_uid["id"], assignee, "gitcode:org/test", issue_number, "待审核需求", "d", status, stage))
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
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
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
            with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
                r = client.post(f"/api/issues/{cid}/pending-review",
                                headers=_session_headers(agent_type=agent),
                                json={"review_points": "审核点"})
            assert r.status_code == 200 and r.json()["changed"] is True

    def test_review_points_written_to_conversation(self):
        from src.api.issues_center import _run_pending_review_notify
        from src.session_manager import get_messages
        cid = _seed(issue_number=23)
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_session_headers(agent_type="qa"),
                            json={"review_points": "请确认是否合并", "references": "PR#23"})
        assert r.status_code == 200 and r.json()["changed"] is True
        with db_lock:
            conv = db.cursor().execute(
                "SELECT conversation_id FROM issue WHERE id=?", (cid,)).fetchone()
        assert conv and conv[0]
        asyncio.run(_run_pending_review_notify(None, None, _uid["id"], conv[0], "",
                                               "请确认是否合并", "PR#23",
                                               "gitcode:org/test", 23, {}))
        msgs = get_messages(conv[0], limit=30)
        assert any("请确认是否合并" in (m.get("content") or "") for m in msgs)

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

    def test_feishu_open_id_by_user_id(self):
        # user_id 定位优先（assignee='' 时仍可触达）；_get_feishu_open_id('') 为空证明兜底必要
        from src.api.issues_center import _get_feishu_open_id, _get_feishu_open_id_by_user_id
        assert _get_feishu_open_id_by_user_id(_uid["id"]) == "ou_review"
        assert _get_feishu_open_id("") == ""

    def test_pending_review_with_empty_assignee_ok(self):
        # assignee 为空仍能设待审核（通知 open_id 走 user_id 定位）；不崩溃、状态正确
        cid = _seed(issue_number=24, assignee="")
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{cid}/pending-review",
                            headers=_session_headers(agent_type="qa"),
                            json={"review_points": "请确认", "references": ""})
        assert r.status_code == 200
        assert r.json()["changed"] is True
        assert _status(issue_number=24)[0] == "pending_review"


class TestReviewPassLastNodeNormalize:
    """review-pass-last-node-force-done-design.md：末节点触达时 target=processing → 归一 done。"""

    def test_last_node_processing_normalized_to_done(self):
        # 默认场景末节点 sre（next=done）+ status=pending_review
        cid = _seed(status="pending_review", issue_number=31, stage="sre")
        r = client.post(f"/api/issues/{cid}/review-pass",
                        headers=_session_headers(agent_type="sre"),
                        json={"target": "processing", "evidence": "用户确认 ok"})
        assert r.status_code == 200
        data = r.json()
        assert data["result"] == "passed_done"
        assert data["status"] == "completed"
        st = _status(issue_number=31)
        assert st[0] == "completed"
        assert st[1] == "done"

    def test_middle_node_processing_not_normalized(self):
        # 中继节点 qa（next=sre，非末节点）→ 保持 processing
        cid = _seed(status="pending_review", issue_number=32, stage="qa")
        r = client.post(f"/api/issues/{cid}/review-pass",
                        headers=_session_headers(agent_type="qa"),
                        json={"target": "processing"})
        assert r.status_code == 200
        data = r.json()
        assert data["result"] == "passed_processing"
        assert data["status"] == "processing"
        st = _status(issue_number=32)
        assert st[0] == "processing"
        assert st[1] == "qa"  # stage 不变

    def test_explicit_done_unaffected(self):
        cid = _seed(status="pending_review", issue_number=33, stage="sre")
        r = client.post(f"/api/issues/{cid}/review-pass",
                        headers=_session_headers(agent_type="sre"),
                        json={"target": "done"})
        assert r.status_code == 200
        assert r.json()["result"] == "passed_done"

    def test_last_node_active_target_done_force_completed(self):
        """#2266 复诊：末节点 + status=active（CLI 从未进入 pending_review）+ target=done → 放行 done。"""
        cid = _seed(status="active", issue_number=34, stage="sre")
        r = client.post(f"/api/issues/{cid}/review-pass",
                        headers=_session_headers(agent_type="sre"),
                        json={"target": "done", "evidence": "用户确认合入 PR #N"})
        assert r.status_code == 200
        data = r.json()
        assert data["result"] == "passed_done"
        assert data["status"] == "completed"
        st = _status(issue_number=34)
        assert st[0] == "completed"
        assert st[1] == "done"

    def test_middle_node_active_target_done_noop(self):
        """守卫：中继节点（qa，next=sre）status=active + target=done → noop（防中间节点误终态）。"""
        cid = _seed(status="active", issue_number=35, stage="qa")
        r = client.post(f"/api/issues/{cid}/review-pass",
                        headers=_session_headers(agent_type="qa"),
                        json={"target": "done"})
        assert r.status_code == 200
        data = r.json()
        assert data["result"] == "noop"
        st = _status(issue_number=35)
        assert st[0] == "active"

    def test_is_last_stage_node_helper(self):
        from src.api.issues_center import _is_last_stage_node
        _seed(status="processing", issue_number=41, stage="sre")
        assert _is_last_stage_node("gitcode:org/test", 41) is True
        _seed(status="processing", issue_number=42, stage="qa")
        assert _is_last_stage_node("gitcode:org/test", 42) is False
        _seed(status="processing", issue_number=43, stage="done")
        assert _is_last_stage_node("gitcode:org/test", 43) is False  # 终态 → 保守 False
        _seed(status="processing", issue_number=44, stage="")
        assert _is_last_stage_node("gitcode:org/test", 44) is False  # 未知 → 保守 False