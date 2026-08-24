"""agents 后端直写会话 + Save 全量提交的回归测试。"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest.mock import patch

from src.api.main import app  # FastAPI 实例（TestClient 需要挂载到 app）
from src.session_manager import db, db_lock


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_agent_session WHERE repo_key LIKE '%test-agents-backend%'")
        db.commit()


def setup_function():
    _clean()


def teardown_function():
    _clean()


def test_backend_onchange_writes_current_session():
    """前端的「选择即写」→ PUT /agents/{owner}/{repo} 只更新当前 Agent 的 backend 字段"""
    from fastapi.testclient import TestClient

    # 预置当前 agent 会话行（模拟配置页打开时已有会话）
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session "
            "(id, repo_key, agent_type, backend, skill, skill_id, repo_gitcode_token, chat_session_id, status, last_active_at) "
            "VALUES ('ag-test-1','gitcode:owner/test-agents','chat','old-engine','',0,'',0,'active',datetime('now'))"
        )
        db.commit()

    with TestClient(app) as client:
        r = client.put("/agents/owner/test-agents", json={"backends": ["new-engine"]})
        assert r.status_code == 200

        with db_lock:
            c = db.cursor()
            c.execute("SELECT backend FROM chat_agent_session WHERE agent_id='ag-test-1'")
            row = c.fetchone()
        assert row and row[0] == "new-engine", f"backend 应被直写"