"""B3①：知识列表裸名解析回落 user_repos（member-only 仓，github: 前缀）。

背景：前端 knowledge.js 只传无前缀 fullName（example-user/demo）；后端
_resolve_repo_key 裸名只查 managed_repos，member-only 仓（仅 user_repos
有记录，repo_key 带 github: 前缀）解析失败回落 gitcode 前缀 → 403。
修复：managed_repos 未命中时追加查 user_repos，带前缀候选优先精确匹配。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_b3_knowledge_bare.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.db_setup import init_database
from src.kernel.session_manager import db, db_lock

init_database()

_client = TestClient(app)
REPO_KEY = "github:example-user/demo"
UID = 99831001


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_repos WHERE user_id=?", (UID,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (UID,))
        c.execute(
            "INSERT OR REPLACE INTO grape_accounts (id, username) VALUES (?, ?)",
            (UID, "b3_knowledge_tester"),
        )
        c.execute(
            "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_join) "
            "VALUES (?,?,?,?,1)",
            (UID, REPO_KEY, "example-user", "demo"),
        )
        c.execute(
            "INSERT INTO agent_shared_knowledge (repo_key, source, scope, file_path, name, description, status, archived) "
            "VALUES (?,?,?,?,?,?,?,0)",
            (REPO_KEY, "agent", "project", "knowledge/x.md", "b3 裸名测试", "member-only 仓", "accepted"),
        )
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_repos WHERE user_id=?", (UID,))
        c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=?", (REPO_KEY,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (UID,))
        db.commit()


def _jwt():
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    return create_access_token(UID, "b3_knowledge_tester", get_token_version(UID))


def test_bare_name_resolves_user_repos_member_only_200():
    """无前缀 fullName（managed_repos 无记录）→ user_repos 回落命中 → 200。"""
    r = _client.get("/api/knowledge", params={"repo": "example-user/demo"},
                    headers={"Authorization": f"Bearer {_jwt()}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["knowledge"][0]["repo_key"] == REPO_KEY


def test_prefixed_repo_key_still_200():
    r = _client.get("/api/knowledge", params={"repo": REPO_KEY},
                    headers={"Authorization": f"Bearer {_jwt()}"})
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_unjoined_repo_still_403():
    """裸名在 managed_repos/user_repos 均无记录 → 回落默认前缀 → 成员校验 403。"""
    r = _client.get("/api/knowledge", params={"repo": "example-user/not-joined"},
                    headers={"Authorization": f"Bearer {_jwt()}"})
    assert r.status_code == 403


@pytest.fixture(autouse=True, scope="module")
def _restore_shared_db_env():
    """模块级私有库 env 跑完恢复共享测试库（对齐 test_knowledge_session_access）。"""
    yield
    os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")
