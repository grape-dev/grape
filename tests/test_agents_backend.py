"""agents 后端直写会话 + Save 全量提交的回归测试。"""
import os
from src.auth import grape_account_db
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.main import app
from src.session_manager import db, db_lock


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE '%test-agents-backend%'")
        db.commit()


def setup_function():
    _clean()


def teardown_function():
    _clean()


def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def test_backend_onchange_writes_current_session():
    """PUT /api/agents/{name}/{owner}/{repo} 写入 agent session（backends 解析为 runtime_id）。"""
    from fastapi.testclient import TestClient
    from src.user import db as user_db

    _clean()
    uid = grape_account_db.create_grape_account("test_agents_be", email=None, password="test123")
    if not uid:
        uid = grape_account_db.get_grape_account_by_username("test_agents_be")
    token = _jwt_access_token(uid["id"], uid["username"])
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        r = client.put(
            "/api/agents/coding/owner/test-agents-backend",
            json={"backends": ["opencode"]},
            headers=headers,
        )
        assert r.status_code == 200, f"PUT 应返回 200, got {r.status_code}: {r.text}"

        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT runtime_id, agent_type FROM devops_agent_session "
                "WHERE repo_key LIKE '%test-agents-backend%'"
            ).fetchone()
        assert row, "agent session 应已写入"
        assert row[1] == "coding", "agent_type 应为 coding"
