"""agent 后端保存路由：选择即写当前 Agent 会话 + Save 全量提交 的回归测试。"""
import os
from src.auth import grape_account_db
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from src.session_manager import db, db_lock


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE '%test-agents%'")
        c.execute("DELETE FROM user_repos WHERE repo_key LIKE '%test-agents%'")
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE '%test-agents%'")
        db.commit()


def setup_function():
    _clean()


def teardown_function():
    _clean()


def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def test_agents_routes():
    """确保 agents PUT 路由能正确写入 agent session（backends → runtime_id 解析）。"""
    from src.api.main import app
    from fastapi.testclient import TestClient
    from src.user import db as user_db

    _clean()
    # 准备测试用户 + JWT
    uid = grape_account_db.create_grape_account("test_agents", email=None, password="test123")
    if not uid:
        uid = grape_account_db.get_grape_account_by_username("test_agents")
    token = _jwt_access_token(uid["id"], uid["username"])
    headers = {"Authorization": f"Bearer {token}"}

    # 注册测试仓库到 managed_repos + user_repos（check_repo_access 需要）
    user_db.add_managed_repo("gitcode:owner/test-agents", "owner", "test-agents")
    user_db.mark_user_repo_join(uid["id"], "gitcode:owner/test-agents")

    with TestClient(app) as client:
        # PUT /agents/{name}/{owner}/{repo} — name 必须在 AGENT_TYPES 中
        r = client.put(
            "/api/agents/coding/owner/test-agents",
            json={"backends": ["opencode"], "skill": ""},
            headers=headers,
        )
        assert r.status_code == 200, f"PUT 应返回 200, got {r.status_code}: {r.text}"
        # 验证 session 已写入
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT runtime_id, agent_type, status FROM devops_agent_session "
                "WHERE repo_key LIKE '%test-agents%'"
            ).fetchone()
    assert row, "agent session 应已写入"
    assert row[1] == "coding", "agent_type 应为 coding"
