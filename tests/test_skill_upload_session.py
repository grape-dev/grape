"""技能上传会话 token（tk_）测试（issue-219：agent 上传仓库技能）。

覆盖：scope=user → 403；scope=repo 仓库不匹配 → 403；repo 匹配 → 放行（进入后续处理）；
无 token → 401。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_skill_upload_session.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.main import app
from src.api.db_setup import init_database
from src.session_manager import db, db_lock

init_database()

_client = TestClient(app)
REPO = "gitcode:org/u-repo"
AGENT = "coding"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        db.cursor().execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
        db.commit()


def _session_token(repo_key=REPO, agent_type="coding") -> str:
    return auth_token.issue_session_token(session_key="t_ul", repo_key=repo_key, agent_type=agent_type)


def _upload(token, scope="repo", repo="org/u-repo"):
    return _client.post("/api/skills/upload",
                        data={"scope": scope, "repo": repo, "agent_type": AGENT},
                        files={"file": ("s.zip", b"not-a-real-zip-but-ok-for-scope-check", "application/zip")},
                        headers={"Authorization": f"Bearer {token}"})


def test_session_scope_user_forbidden():
    assert _upload(_session_token(), scope="user").status_code == 403


def test_session_repo_mismatch_forbidden():
    wrong = auth_token.issue_session_token(session_key="t_u2", repo_key="gitcode:org/other-u", agent_type="coding")
    assert _upload(wrong, repo="org/u-repo").status_code == 403


def test_session_repo_match_allowed_beyond_scope():
    # 作用域校验通过 → 进入后续处理（测试库 OBS 未启用/zip 非法 → 400/503 而非 403/401）
    r = _upload(_session_token(), repo="org/u-repo")
    assert r.status_code not in (401, 403)


def test_no_token_401():
    assert _client.post("/api/skills/upload",
                        data={"scope": "repo", "repo": "org/u-repo", "agent_type": AGENT},
                        files={"file": ("s.zip", b"x", "application/zip")}).status_code == 401