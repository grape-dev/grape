"""tasks awaiting-input 显式待澄清测试（issue-explicit-awaiting-input-api-design.md）。

Agent（vod/issue）经会话 token 显式声明任务进入 awaiting_input：
active/processing → awaiting_input（原子迁移、changed 可观测）、写澄清问题到会话、静态飞书通知仅首次。
关键词自动判定已退役（event_pipeline._detect_awaiting_input 移除），本接口为唯一契约路径。
"""
import os
import sys
import asyncio
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.api.routes import tasks as tasks_mod
from src.session_manager import db, db_lock, get_messages
from src.user import db as user_db

client = TestClient(app)

_uid = None
_BOT_TOKEN = "bot_await_test_token"


def _session_headers(task_id, repo_key="gitcode:org/test", agent_type="vod"):
    import uuid
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=task_id, repo_key=repo_key, agent_type=agent_type,
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


def _seed(task_id="t-await-1", issue_number=9, agent_type="vod", task_status="running",
          issue_status="processing", repo_key="gitcode:org/test"):
    """构造 issue 卡片 + agent_tasks 行；返回 issue_card_id。"""
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (repo_key, issue_number))
        c.execute("DELETE FROM chat_conversations WHERE repo_key=? AND title=?", (repo_key, f"Issue #{issue_number}"))
        c.execute("DELETE FROM chat_messages WHERE conversation_id NOT IN (SELECT id FROM chat_conversations)")
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (_uid["id"], "await_eric", repo_key, issue_number, "需求标题", "desc", issue_status))
        card_id = c.lastrowid
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, agent_type, "
            "skill, prompt, status, user_id, stream_ended, self_reported) "
            "VALUES (?, 'sess', 0, NULL, ?, ?, ?, 'vod-cli', 'p', ?, ?, 0, 0)",
            (task_id, f"#{issue_number}", repo_key, agent_type, task_status, _uid["id"] if _uid else 0))
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


class TestAwaitingInput:
    def test_questions_required(self):
        _seed()
        r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-1"), json={"questions": " "})
        assert r.status_code == 400

    def test_task_not_found(self):
        r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-999"), json={"questions": "q"})
        assert r.status_code == 404

    def test_vod_moves_to_awaiting_input(self):
        card_id = _seed()
        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-1"),
                            json={"questions": "请说明你期望的按钮颜色", "reason": "需求选项不明确"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["task_id"] == "t-await-1"
        assert data["issue_id"] == card_id
        assert data["status"] == "awaiting_input"
        assert data["changed"] is True
        row = _status("gitcode:org/test", 9)
        assert row and row[0] == "awaiting_input"
        assert row[1], "应回填 conversation_id"

    def test_issue_agent_allowed(self):
        _seed(task_id="t-await-2", agent_type="issue")
        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-2", agent_type="issue"),
                            json={"questions": "请补充验收标准"})
        assert r.status_code == 200
        assert r.json()["changed"] is True

    def test_agent_type_scope_403(self):
        _seed(task_id="t-await-3", agent_type="coding")
        r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-3", agent_type="coding"),
                        json={"questions": "q"})
        assert r.status_code == 403

    def test_session_agent_mismatch_403(self):
        _seed(task_id="t-await-4", agent_type="vod")
        # token 的 agent_type=issue 与任务 vod 不一致
        r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-4", agent_type="issue"),
                        json={"questions": "q"})
        assert r.status_code == 403

    def test_repo_scope_mismatch_403(self):
        _seed(task_id="t-await-5")
        r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-5", repo_key="gitcode:org/other"),
                        json={"questions": "q"})
        assert r.status_code == 403

    def test_non_session_token_403(self):
        _seed()
        r = client.post("/api/tasks/awaiting-input", headers={"Authorization": f"Bearer {_BOT_TOKEN}"},
                        json={"questions": "q"})
        assert r.status_code in (401, 403)

    def test_terminal_task_changed_false(self):
        _seed(task_id="t-await-6", task_status="completed")
        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-6"),
                            json={"questions": "q"})
        assert r.status_code == 200
        assert r.json()["changed"] is False
        assert _status("gitcode:org/test", 9)[0] == "processing"

    def test_already_awaiting_changed_false(self):
        _seed(task_id="t-await-7", issue_status="awaiting_input")
        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-7"),
                            json={"questions": "q"})
        assert r.status_code == 200
        assert r.json()["changed"] is False
        assert _status("gitcode:org/test", 9)[0] == "awaiting_input"

    def test_questions_written_to_conversation(self):
        _seed(task_id="t-await-8", issue_number=8)
        with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
            r = client.post("/api/tasks/awaiting-input", headers=_session_headers("t-await-8"),
                            json={"questions": "请提供部署环境清单"})
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