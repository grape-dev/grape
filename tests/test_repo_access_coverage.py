"""check_repo_access 覆盖度回归测试——写端点归属校验（越权 403）。

覆盖修复的 7 处写端点：
- repos.py: PUT /bot-token, PUT /deploy-url, POST /{owner}/{repo}/repair-host
- events.py: POST /{seq}/terminate, POST /{seq}/redispatch
- agents.py: PUT /{name}/{owner}/{repo}, POST .../clean

模式：alice 拥有 gitcode:alice/repo1；bob 拥有 gitcode:bob/repo2。
bob 对 alice 的仓库执行写操作 → 403；alice → 越过归属校验（后续业务校验接管）。
鉴权用 dependency_overrides 注入身份（仿 test_git_repo_api.py）。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

ALICE_REPO = "gitcode:alice/repo1"
BOB_REPO = "gitcode:bob/repo2"

_current_uid = {"id": 1}


def _fake_user():
    return {"id": _current_uid["id"], "username": f"ra_{'alice' if _current_uid['id'] == 1 else 'bob'}",
            "auth_type": "jwt"}


@pytest.fixture(scope="module")
def client():
    from src.api.main import app
    from src.api.deps import get_current_user, get_current_user_or_api_key
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _seed(client):
    """托管仓库 + user_repos 归属关系 + 一条归属 alice 的事件。"""
    from src.user import db as user_db
    from src.session_manager import db, db_lock

    user_db.add_managed_repo(ALICE_REPO, "alice", "repo1")
    user_db.add_managed_repo(BOB_REPO, "bob", "repo2")
    user_db.mark_user_repo_join(1, ALICE_REPO, is_join=True, is_admin=True)
    user_db.mark_user_repo_join(2, BOB_REPO, is_join=True, is_admin=True)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE seq=?", (9101,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status, title) VALUES (?,?,?,?,?)",
            (9101, ALICE_REPO, "issue_opened", "failed", "t"))
        db.commit()
    yield
    _current_uid["id"] = 1


def _as(uid):
    _current_uid["id"] = uid


# ===== repos.py =====

def test_bot_token_cross_user_403(client):
    """bob 不可改 alice 仓库的 bot token（高危：凭据劫持）。"""
    _as(2)
    r = client.put("/api/repos/bot-token", json={"full_name": "alice/repo1", "bot_token": "tk_x"})
    assert r.status_code == 403, r.text


def test_bot_token_owner_ok(client):
    _as(1)
    r = client.put("/api/repos/bot-token", json={"full_name": "alice/repo1", "bot_token": "tk_owner"})
    assert r.status_code == 200, r.text


def test_deploy_url_cross_user_403(client):
    _as(2)
    r = client.put("/api/repos/deploy-url", json={"full_name": "alice/repo1", "deploy_url": "http://x"})
    assert r.status_code == 403, r.text


def test_repair_host_cross_user_403(client):
    _as(2)
    r = client.post("/api/repos/alice/repo1/repair-host")
    assert r.status_code == 403, r.text


# ===== events.py =====

def test_terminate_cross_user_403(client):
    """bob 不可终止 alice 的事件（terminate 会 fail_task 杀进程）。"""
    _as(2)
    r = client.post("/api/events/9101/terminate")
    assert r.status_code == 403, r.text


def test_terminate_owner_passes_acl(client):
    """归属人 terminate 自己的事件 → 越过归属校验，撞到状态检查（400 非法状态）。"""
    _as(1)
    r = client.post("/api/events/9101/terminate")
    assert r.status_code == 400, r.text  # "事件状态为 failed，无法终止"


def test_redispatch_cross_user_403(client):
    _as(2)
    r = client.post("/api/events/9101/redispatch")
    assert r.status_code == 403, r.text


# ===== agents.py =====

def test_agent_config_cross_user_403(client):
    """读有校验、写也要有：bob 不可改 alice 仓库的 Agent 配置。"""
    _as(2)
    r = client.put("/api/agents/vod/alice/repo1", json={})
    assert r.status_code == 403, r.text


def test_agent_clean_cross_user_403(client):
    _as(2)
    r = client.post("/api/agents/vod/alice/repo1/clean")
    assert r.status_code == 403, r.text


def test_agent_clean_owner_passes_acl(client):
    """归属人 clean 自己仓库 → 越过归属校验（clean 幂等，200）。"""
    _as(1)
    r = client.post("/api/agents/vod/alice/repo1/clean")
    assert r.status_code == 200, r.text
