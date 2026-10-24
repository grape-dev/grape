"""Issue #461: 项目场景详情页登记场景访问地址（地球仪图标）测试。

覆盖：
- GET/PUT /api/scenes/{scene_id}/deploy-url 正常读写、清空
- 服务端校验：http(s) 前缀、≤512 长度、javascript:/引号 XSS 拒绝（沿用 #446 规则）
- 归属校验：bob 不可改 alice 仓库的场景（403）
- 404（场景不存在/未激活）、400（缺 repo）
- 场景级隔离：同仓库两场景互不影响；需求中心 managed_repos.deploy_url 不受影响

模式：仿 test_repo_access_coverage.py（alice/bob 两用户 + dependency_overrides）。
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
    return {"id": _current_uid["id"], "username": f"i461_{'alice' if _current_uid['id'] == 1 else 'bob'}",
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
    """托管仓库 + 归属关系 + 两个测试场景。"""
    from src.user import db as user_db
    from src.session_manager import db, db_lock

    user_db.add_managed_repo(ALICE_REPO, "alice", "repo1")
    user_db.add_managed_repo(BOB_REPO, "bob", "repo2")
    user_db.mark_user_repo_join(1, ALICE_REPO, is_join=True, is_admin=True)
    user_db.mark_user_repo_join(2, BOB_REPO, is_join=True, is_admin=True)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (ALICE_REPO,))
        for sid in ("i461-s1", "i461-s2"):
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active, deploy_url) VALUES (?,?,?,?,1,'')",
                (sid, f"I461 Scene {sid}", ALICE_REPO, "[]"))
        db.commit()
    yield
    _current_uid["id"] = 1
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (ALICE_REPO,))
        db.commit()


def _as(uid):
    _current_uid["id"] = uid


_S1 = "/api/scenes/i461-s1/deploy-url?repo=alice/repo1"


# ===== 正常读写 =====

def test_scene_deploy_url_set_and_get(client):
    _as(1)
    r = client.put(_S1, json={"deploy_url": "http://1.2.3.4:5000"})
    assert r.status_code == 200, r.text
    assert r.json()["deploy_url"] == "http://1.2.3.4:5000"
    r2 = client.get(_S1)
    assert r2.status_code == 200, r2.text
    assert r2.json()["deploy_url"] == "http://1.2.3.4:5000"


def test_scene_deploy_url_https_ok(client):
    _as(1)
    r = client.put(_S1, json={"deploy_url": "https://example.com/app"})
    assert r.status_code == 200, r.text


def test_scene_deploy_url_empty_clears(client):
    _as(1)
    client.put(_S1, json={"deploy_url": "http://x.example.com"})
    r = client.put(_S1, json={"deploy_url": ""})
    assert r.status_code == 200, r.text
    r2 = client.get(_S1)
    assert r2.json()["deploy_url"] == ""


def test_scene_deploy_url_get_default_empty(client):
    _as(1)
    r = client.get("/api/scenes/i461-s2/deploy-url?repo=alice/repo1")
    assert r.status_code == 200, r.text
    assert r.json()["deploy_url"] == ""


# ===== 校验（沿用 #446 XSS 防护规则） =====

def test_scene_deploy_url_reject_javascript(client):
    _as(1)
    r = client.put(_S1, json={"deploy_url": "javascript:alert(1)"})
    assert r.status_code == 400, r.text


def test_scene_deploy_url_reject_xss_attempt(client):
    _as(1)
    r = client.put(_S1, json={"deploy_url": 'x" onmouseover="alert(1)'})
    assert r.status_code == 400, r.text


def test_scene_deploy_url_reject_too_long(client):
    _as(1)
    r = client.put(_S1, json={"deploy_url": "http://" + "a" * 510})
    assert r.status_code == 400, r.text


def test_scene_deploy_url_strips_whitespace(client):
    _as(1)
    r = client.put(_S1, json={"deploy_url": "  http://x.example.com  "})
    assert r.status_code == 200, r.text
    assert r.json()["deploy_url"] == "http://x.example.com"


# ===== 归属校验 =====

def test_scene_deploy_url_cross_user_403(client):
    _as(2)
    r = client.put(_S1, json={"deploy_url": "http://evil.example.com"})
    assert r.status_code == 403, r.text


def test_scene_deploy_url_get_cross_user_403(client):
    _as(2)
    r = client.get(_S1)
    assert r.status_code == 403, r.text


# ===== 404 / 400 =====

def test_scene_deploy_url_scene_not_found_404(client):
    _as(1)
    r = client.put("/api/scenes/i461-nope/deploy-url?repo=alice/repo1",
                   json={"deploy_url": "http://x.example.com"})
    assert r.status_code == 404, r.text


def test_scene_deploy_url_missing_repo_400(client):
    _as(1)
    r = client.get("/api/scenes/i461-s1/deploy-url")
    assert r.status_code == 400, r.text


def test_scene_deploy_url_inactive_scene_404(client):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR REPLACE INTO devops_pipeline_scene "
                  "(scene_id, name, repo_key, nodes_json, is_active, deploy_url) VALUES (?,?,?,?,0,'')",
                  ("i461-off", "I461 Off", ALICE_REPO, "[]"))
        db.commit()
    _as(1)
    try:
        r = client.put("/api/scenes/i461-off/deploy-url?repo=alice/repo1",
                       json={"deploy_url": "http://x.example.com"})
        assert r.status_code == 404, r.text
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id='i461-off'")
            db.commit()


# ===== 场景级隔离（与需求中心互不影响） =====

def test_scene_deploy_url_scene_level_isolation(client):
    _as(1)
    client.put(_S1, json={"deploy_url": "http://scene1.example.com"})
    r2 = client.get("/api/scenes/i461-s2/deploy-url?repo=alice/repo1")
    assert r2.json()["deploy_url"] == "", "另一场景不应被写入"


def test_scene_deploy_url_does_not_touch_repo_level(client):
    """场景级写不得影响需求中心 managed_repos.deploy_url。"""
    from src.session_manager import db, db_lock
    _as(1)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET deploy_url='' WHERE repo_key=?", (ALICE_REPO,))
        db.commit()
    client.put(_S1, json={"deploy_url": "http://scene-level.example.com"})
    with db_lock:
        c = db.cursor()
        c.execute("SELECT deploy_url FROM managed_repos WHERE repo_key=?", (ALICE_REPO,))
        row = c.fetchone()
    assert (row[0] if row else "") == "", "需求中心（仓库级）deploy_url 不应被场景级写入影响"


def test_repo_level_deploy_url_does_not_touch_scene(client):
    """需求中心（仓库级）写不得影响场景级。"""
    from src.session_manager import db, db_lock
    _as(1)
    client.put("/api/repos/deploy-url", json={"full_name": "alice/repo1",
                                              "deploy_url": "http://repo-level.example.com"})
    r = client.get(_S1)
    assert r.json()["deploy_url"] == "", "场景级 deploy_url 不应被仓库级写入影响"
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET deploy_url='' WHERE repo_key=?", (ALICE_REPO,))
        db.commit()


def test_db_migration_column_exists():
    """init_database 后 devops_pipeline_scene 应有 deploy_url 列。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()]
    assert "deploy_url" in cols
