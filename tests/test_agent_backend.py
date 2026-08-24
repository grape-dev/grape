"""agent 后端保存路由：选择即写当前 Agent 会话 + Save 全量提交 的回归测试。"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.api.routes import agents as agents_mod
from src.session_manager import db, db_lock


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_session WHERE repo_key LIKE '%test-agents%' OR agent_key LIKE '%test-agents%'")
        db.commit()


def setup_function():
    _clean()


def teardown_function():
    _clean()


def test_agents_routes():
    """确保 agents 路由暴露 & 基础健康：选择即写会话"""
    from src.api.main import app  # FastAPI 实例
    from fastapi.testclient import TestClient

    _clean()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_agent_session "
            "(agent_id, repo_key, agent_type, backend, skill, chat_session_id) "
            "VALUES ('agent-test','gitcode:owner/test-agents','chat','opencode','',0,'act')"
        )
        db.commit()
    with TestClient(app) as client:
        r = client.put("/agents/owner/test-agents", json={"backends": ["new-engine"], "skill": "opencode"})
        assert r.status_code == 200
        with db_lock:
            c = db.cursor()
            c.execute("SELECT backend FROM devops_agent_session WHERE agent_id='agent-test'")
            row = c.fetchone()
    assert row and row[0] == "newengine", "后端应被直写"