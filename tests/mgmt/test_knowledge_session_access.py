"""知识读取接口会话 token（tk_）访问测试（issue-219：技能按需获取共享知识）。

覆盖：list/stats 的 tk_ 仓库作用域匹配（放行/不匹配 403）；detail 直接读取；用户通道不变。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_knowledge_session.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.main import app
from src.api.db_setup import init_database
from src.kernel.session_manager import db, db_lock

init_database()

_client = TestClient(app)
REPO = "gitcode:org/k-repo"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
        db.cursor().execute("DELETE FROM agent_shared_knowledge WHERE repo_key=?", (REPO,))
        db.cursor().execute("DELETE FROM agent_shared_knowledge WHERE repo_key='gitcode:org/other-k'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
        db.cursor().execute("DELETE FROM agent_shared_knowledge WHERE repo_key=?", (REPO,))
        db.cursor().execute("DELETE FROM agent_shared_knowledge WHERE repo_key='gitcode:org/other-k'")
        db.commit()


def _seed_knowledge(repo_key=REPO):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_shared_knowledge (repo_key, source, scope, file_path, name, description, status, archived) "
            "VALUES (?,?,?,?,?,?,?,0)",
            (repo_key, "agent", "project", "knowledge/nginx.md", "nginx 配置注意", "勿改 listen 端口", "accepted"))
        db.commit()
        return c.lastrowid


def _session_token(repo_key=REPO, agent_type="vod") -> str:
    return auth_token.issue_session_token(session_key="t_k", repo_key=repo_key, agent_type=agent_type)


def _list(token, repo="org/k-repo"):
    return _client.get("/api/knowledge", params={"repo": repo},
                       headers={"Authorization": f"Bearer {token}"})


def test_list_matching_repo_allowed():
    _seed_knowledge()
    r = _list(_session_token())
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_list_repo_mismatch_forbidden():
    _seed_knowledge()
    wrong = auth_token.issue_session_token(session_key="t_o", repo_key="gitcode:org/other-k", agent_type="vod")
    assert _list(wrong).status_code == 403


def test_stats_matching_repo_allowed():
    _seed_knowledge()
    tok = _session_token()
    r = _client.get("/api/knowledge/stats", params={"repo": "org/k-repo"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["agent"] == 1


def test_detail_readable_with_session():
    kid = _seed_knowledge()
    r = _client.get(f"/api/knowledge/{kid}", headers={"Authorization": f"Bearer {_session_token()}"})
    assert r.status_code == 200
    assert r.json()["name"] == "nginx 配置注意"


def test_no_token_401():
    assert _client.get("/api/knowledge", params={"repo": "org/k-repo"}).status_code == 401

@pytest.fixture(autouse=True, scope="module")
def _restore_shared_db_env():
    """混跑污染修复（D46/S68）：模块级私有库 env 全程不恢复——本文件跑完后，
    后续任何按 env 重开连接的导入/reload 全部错开共享测试库（实测混跑
    no such table / locked 族）。用例结束后恢复 conftest 的共享库路径。
    本文件自身用例在 fixture 生效前已绑定各自连接，不受影响。"""
    yield
    os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")
