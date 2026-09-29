"""project-info 会话 token（tk_）读取测试（issue-219：技能按需获取项目知识）。

覆盖：tk_ 仓库作用域匹配 → 200；不匹配 → 403；openapi x-token-scope=dual。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_repos_pi_auth.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.main import app
from src.api.db_setup import init_database

init_database()

_client = TestClient(app)
REPO = "gitcode:org/pi-repo"


@pytest.fixture(autouse=True)
def _clean():
    from src.session_manager import db, db_lock
    with db_lock:
        db.cursor().execute("DELETE FROM session_api_tokens")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM session_api_tokens")
        db.commit()


def _session_token(repo_key=REPO, agent_type="vod") -> str:
    return auth_token.issue_session_token(session_key="t_pi", repo_key=repo_key, agent_type=agent_type)


def _get(token, owner="org", repo="pi-repo"):
    return _client.get(f"/api/repos/{owner}/{repo}/project-info",
                       headers={"Authorization": f"Bearer {token}"})


def test_session_token_matching_repo_allowed():
    r = _get(_session_token(repo_key=REPO))
    assert r.status_code == 200
    assert r.json()["repo_key"] == REPO


def test_session_token_repo_mismatch_forbidden():
    wrong = auth_token.issue_session_token(session_key="t_other", repo_key="gitcode:org/other-repo", agent_type="vod")
    r = _get(wrong)
    assert r.status_code == 403


def test_no_token_401():
    assert _client.get("/api/repos/org/pi-repo/project-info").status_code == 401


def test_openapi_scope_is_dual():
    data = _client.get("/openapi.json").json()
    op = data["paths"]["/api/repos/{owner}/{repo}/project-info"]["get"]
    assert op["x-token-scope"] == "dual"