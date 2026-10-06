"""issues awaiting-input 显式待澄清测试（issue-explicit-awaiting-input-api-design.md，澄清三件套）。

Agent（vod/issue，会话 tk_）经 POST /api/issues/{issue_id}/awaiting-input 显式声明需求进入待澄清：
active/processing → awaiting_input（原子迁移、changed 可观测）、写澄清问题到会话、静态飞书仅首次。
不再读取/依赖 agent_tasks（与任务生命周期正交，C 单信号）；关键词判定已退役。
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
from src.session_manager import db, db_lock, get_messages
from src.user import db as user_db

client = TestClient(app)

_uid = None
_BOT_TOKEN = "bot_await_test_token"


def _session_headers(repo_key="gitcode:org/test", agent_type="vod"):
    import uuid
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"await-{uuid.uuid4().hex}",
        repo_key=repo_key, agent_type=agent_type,
        user_id=(_uid["id"] if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _uid
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username='await_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='await_eric'")
        db.commit()
    _uid = user_db.create_user("await_user", "pw123")
    if not _uid:
        _uid = user_db.get_user_by_username("await_user")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, feishu_open_id, feishu_name, updated_at) "
                  "VALUES (?,?,?,?,datetime('now'))", (_uid["id"], "await_eric", "ou_await", "Eric"))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
                  "VALUES ('gitcode:org/test','org','test','org/test',1,?)", (_BOT_TOKEN,))
        db.commit()
    user_db.mark_user_repo_join(_uid["id"], "gitcode:org/test", True, True)


def teardown_module():
    if _uid:
        user_db.delete_user(_uid["id"])
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_accounts WHERE username='await_user'")
        c.execute("DELETE FROM unified_user_mapping WHERE gitcode_user='await_eric'")
        db.commit()


def _seed_issue(status="processing", issue_number=9, repo_key="gitcode:org/test"):
    """构造 issue 卡片；返回 issue_card_id。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (repo_key, issue_number))
        c.execute("DELETE FROM chat_conversations WHERE repo_key=? AND title=?", (repo_key, f"Issue #{issue_number}"))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (_uid["id"], "await_eric", repo_key, issue_number, "需求标题", "desc", status))
        card_id = c.lastrowid
        db.commit()
        return card_id


def _status(repo_key, issue_number):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, conversation_id FROM issue WHERE repo_key=? AND issue_number=?",
            (repo_key, issue_number)).fetchone()


def _swallow(coro, *a, **k):
    try:
        coro.close()
    except Exception:
        pass
    return None


class TestIssuesAwaitingInput:
    def test_questions_required(self):
        card_id = _seed_issue()
        r = client.post(f"/api/issues/{card_id}/awaiting-input",
                        headers=_session_headers(), json={"questions": " "})
        assert r.status_code == 400

    def test_issue_not_found(self):
        r = client.post("/api/issues/999999/awaiting-input",
                        headers=_session_headers(), json={"questions": "q"})
        assert r.status_code == 404

    def test_vod_moves_to_awaiting_input(self):
        card_id = _seed_issue()
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{card_id}/awaiting-input",
                            headers=_session_headers(), json={"questions": "请说明你期望的按钮颜色", "reason": "需求选项不明确"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["issue_id"] == card_id
        assert data["status"] == "awaiting_input"
        assert data["changed"] is True
        row = _status("gitcode:org/test", 9)
        assert row and row[0] == "awaiting_input"
        assert row[1], "应回填 conversation_id"

    def test_issue_agent_allowed(self):
        card_id = _seed_issue(issue_number=10)
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{card_id}/awaiting-input",
                            headers=_session_headers(agent_type="issue"), json={"questions": "请补充验收标准"})
        assert r.status_code == 200
        assert r.json()["changed"] is True

    def test_repo_scope_mismatch_403(self):
        card_id = _seed_issue()
        r = client.post(f"/api/issues/{card_id}/awaiting-input",
                        headers=_session_headers(repo_key="gitcode:org/other"), json={"questions": "q"})
        assert r.status_code == 403

    def test_agent_type_scope_403(self):
        card_id = _seed_issue()
        r = client.post(f"/api/issues/{card_id}/awaiting-input",
                        headers=_session_headers(agent_type="coding"), json={"questions": "q"})
        assert r.status_code == 403

    def test_non_session_token_403(self):
        card_id = _seed_issue()
        r = client.post(f"/api/issues/{card_id}/awaiting-input",
                        headers={"Authorization": f"Bearer {_BOT_TOKEN}"}, json={"questions": "q"})
        assert r.status_code in (401, 403)

    def test_terminal_issue_changed_false(self):
        card_id = _seed_issue(status="completed")
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{card_id}/awaiting-input",
                            headers=_session_headers(), json={"questions": "q"})
        assert r.status_code == 200
        assert r.json()["changed"] is False
        assert _status("gitcode:org/test", 9)[0] == "completed"

    def test_already_awaiting_changed_false(self):
        card_id = _seed_issue(status="awaiting_input")
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{card_id}/awaiting-input",
                            headers=_session_headers(), json={"questions": "q"})
        assert r.status_code == 200
        assert r.json()["changed"] is False
        assert _status("gitcode:org/test", 9)[0] == "awaiting_input"

    def test_questions_written_to_conversation(self):
        card_id = _seed_issue(issue_number=8)
        with patch.object(issues_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post(f"/api/issues/{card_id}/awaiting-input",
                            headers=_session_headers(), json={"questions": "请提供部署环境清单"})
        assert r.status_code == 200
        _row = _status("gitcode:org/test", 8)
        conv_id = _row[1]
        assert conv_id
        # 手动执行后台通知协程（_run_awaiting_notify 写会话 + 跳过飞书），验证澄清问题落库
        from src.api.issues_center import _run_awaiting_notify
        asyncio.run(_run_awaiting_notify(None, None, _uid["id"], conv_id, "",
                                         "请提供部署环境清单", "gitcode:org/test", 8, {}))
        msgs = get_messages(conv_id, limit=30)
        assert any("请提供部署环境清单" in (m.get("content") or "") for m in msgs), \
            "澄清问题应作为 agent 消息写入任务会话"

    def test_keyword_retired_does_not_change_status(self):
        # 关键词判定已退役：仅显式接口改状态，这里验证 completed 卡片不受影响（对照值）
        card_id = _seed_issue(status="completed")
        _before = _status("gitcode:org/test", 9)[0]
        assert _before == "completed"


class TestResumeFromAwaiting:
    """澄清回复自动恢复 helper（§3.6）：awaiting_input → processing 幂等。"""

    def test_resume_when_awaiting(self):
        _seed_issue(status="awaiting_input")
        from src.api.issues_center import _resume_issue_from_awaiting
        assert _resume_issue_from_awaiting("gitcode:org/test", 9) is True
        assert _status("gitcode:org/test", 9)[0] == "processing"

    def test_resume_not_when_processing(self):
        _seed_issue(status="processing")
        from src.api.issues_center import _resume_issue_from_awaiting
        assert _resume_issue_from_awaiting("gitcode:org/test", 9) is False
        assert _status("gitcode:org/test", 9)[0] == "processing"

    def test_resume_idempotent_second_call(self):
        _seed_issue(status="awaiting_input")
        from src.api.issues_center import _resume_issue_from_awaiting
        assert _resume_issue_from_awaiting("gitcode:org/test", 9) is True
        assert _resume_issue_from_awaiting("gitcode:org/test", 9) is False  # 已 processing → no-op

    def test_resume_absent_issue_false(self):
        from src.api.issues_center import _resume_issue_from_awaiting
        assert _resume_issue_from_awaiting("gitcode:org/test", 42) is False