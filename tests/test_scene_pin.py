"""Issue #1735: 项目场景列表「置顶」（仓库共享 · pinned_at 倒序 · 任意 sort 置顶优先）。

覆盖验收 2-8：
- 迁移幂等（_fn_scene_pin_columns 连续执行两次，列不重复且 is_pinned 默认 0）
- 默认 sort 置顶优先，置顶内部 pinned_at DESC，全部取消后回原序
- sort=latest 置顶优先，未置顶部分保持 created_at DESC, id DESC
- 重复置顶不刷新 pinned_at；取消置顶两次后 is_pinned=0、pinned_at IS NULL
- 老库缺列降级：查询不报错、SQL 不含 is_pinned、is_pinned 回落 0
- 跨仓库/无权限：bob 用自己的仓库置顶 alice 场景 → 404，数据不被修改
- 接口契约：/scenes 与 /scenes/with-pipeline 均带 is_pinned/pinned_at；POST 返回体含 scene_id/is_pinned

模式：仿 tests/test_issue_461_scene_deploy_url.py（alice/bob + dependency_overrides）。
"""
import os
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

ALICE_REPO = "gitcode:alice/repo1"
BOB_REPO = "gitcode:bob/repo2"
ALICE_SLUG = "alice/repo1"

_current_uid = {"id": 1}


def _fake_user():
    return {"id": _current_uid["id"],
            "username": f"i1735_{'alice' if _current_uid['id'] == 1 else 'bob'}",
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
def _seed():
    """托管仓库 + 归属关系 + alice 仓库 3 个场景（id 升序 a/b/c，created_at 递增）。"""
    from src.user import db as user_db
    from src.session_manager import db, db_lock

    user_db.add_managed_repo(ALICE_REPO, "alice", "repo1")
    user_db.add_managed_repo(BOB_REPO, "bob", "repo2")
    user_db.mark_user_repo_join(1, ALICE_REPO, is_join=True, is_admin=True)
    user_db.mark_user_repo_join(2, BOB_REPO, is_join=True, is_admin=True)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (ALICE_REPO,))
        for sid, created in (("1735-a", "2026-01-01"),
                             ("1735-b", "2026-01-02"),
                             ("1735-c", "2026-01-03")):
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active, created_at) "
                "VALUES (?,?,?,?,1,?)",
                (sid, f"Scene {sid}", ALICE_REPO, "[]", created))
        db.commit()
    _current_uid["id"] = 1
    yield
    _current_uid["id"] = 1
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (ALICE_REPO,))
        db.commit()


def _as(uid):
    _current_uid["id"] = uid


def _ids(client, sort=None):
    url = f"/api/scenes?repo={ALICE_SLUG}"
    if sort:
        url += f"&sort={sort}"
    r = client.get(url)
    assert r.status_code == 200, r.text
    return [s["id"] for s in r.json()["scenes"]]


def _pin(client, sid, pinned=True, repo=ALICE_SLUG, expect=200):
    r = client.post(f"/api/scenes/{sid}/pin?repo={repo}", json={"pinned": pinned})
    assert r.status_code == expect, r.text
    return r


def _pinned_at(sid):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT pinned_at FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
                  (sid, ALICE_REPO))
        row = c.fetchone()
    return row[0] if row else None


# ===== 静态基线 =====

def test_baseline_ddl_and_migration_registered():
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "src/api/db_setup.py"), encoding="utf-8") as f:
        src = f.read()
    ddl_start = src.index("_PIPELINE_SCENE_DDL")
    ddl = src[ddl_start:src.index('"""', src.index("CREATE TABLE", ddl_start))]
    assert "is_pinned" in ddl and "pinned_at" in ddl
    assert src.count('version="202611100003"') == 1
    assert 'name="scene-pin-columns"' in src


# ===== 验收 2：迁移幂等 =====

def test_migration_idempotent_on_global_db():
    from src.api.db_setup import _fn_scene_pin_columns
    from src.session_manager import db, db_lock

    _fn_scene_pin_columns()
    _fn_scene_pin_columns()
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()]
    assert cols.count("is_pinned") == 1
    assert cols.count("pinned_at") == 1


def test_ensure_column_adds_once_on_temp_sqlite():
    from src.api.db_setup import _ensure_column

    conn = sqlite3.connect(":memory:")
    c = conn.cursor()
    c.execute("CREATE TABLE devops_pipeline_scene (id INTEGER PRIMARY KEY, scene_id TEXT)")
    _ensure_column(c, "devops_pipeline_scene", "is_pinned", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(c, "devops_pipeline_scene", "is_pinned", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(c, "devops_pipeline_scene", "pinned_at", "VARCHAR(32) DEFAULT NULL")
    _ensure_column(c, "devops_pipeline_scene", "pinned_at", "VARCHAR(32) DEFAULT NULL")
    cols = [r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()]
    assert cols.count("is_pinned") == 1
    assert cols.count("pinned_at") == 1
    c.execute("INSERT INTO devops_pipeline_scene (scene_id) VALUES ('x')")
    assert c.execute("SELECT is_pinned FROM devops_pipeline_scene").fetchone()[0] == 0
    conn.close()


# ===== 验收 3：默认 sort 置顶优先 =====

def test_default_sort_pinned_first(client):
    assert _ids(client) == ["1735-a", "1735-b", "1735-c"]

    # 先置顶 a 再置顶 c：置顶组内必须按 pinned_at DESC（c 后置顶 → 排前）。
    # 若错误地按 id 排序，结果会是 a,c,b，本断言可真实暴露 pinned_at 缺失。
    _pin(client, "1735-a")
    time.sleep(0.01)
    _pin(client, "1735-c")
    assert _ids(client) == ["1735-c", "1735-a", "1735-b"]

    time.sleep(0.01)
    _pin(client, "1735-b")
    assert _ids(client) == ["1735-b", "1735-c", "1735-a"]

    _pin(client, "1735-b", False)
    _pin(client, "1735-c", False)
    _pin(client, "1735-a", False)
    assert _ids(client) == ["1735-a", "1735-b", "1735-c"]


# ===== 验收 4：sort=latest 置顶优先 =====

def test_latest_sort_pinned_first_and_unpinned_created_desc(client):
    assert _ids(client, sort="latest") == ["1735-c", "1735-b", "1735-a"]

    # 先置顶 c 再置顶 a：置顶组按 pinned_at DESC → a,c；若按 created_at DESC 则 c,a。
    _pin(client, "1735-c")
    time.sleep(0.01)
    _pin(client, "1735-a")
    assert _ids(client, sort="latest") == ["1735-a", "1735-c", "1735-b"]

    _pin(client, "1735-a", False)
    _pin(client, "1735-c", False)
    assert _ids(client, sort="latest") == ["1735-c", "1735-b", "1735-a"]


# ===== 验收 5：重复置顶不刷新 / 取消幂等 =====

def test_repeat_pin_does_not_refresh_pinned_at(client):
    _pin(client, "1735-c")
    first = _pinned_at("1735-c")
    assert first
    time.sleep(0.01)
    r = _pin(client, "1735-c")
    assert r.json()["is_pinned"] == 1
    assert _pinned_at("1735-c") == first


def test_repeat_pin_tolerates_zero_rowcount(client, monkeypatch):
    """模拟 MySQL 未启用 CLIENT_FOUND_ROWS：匹配但值未变的 UPDATE 返回 0。

    路由已先做存在性校验，rowcount==0 不得再判 404（验收 5 重复置顶 200）。
    """
    from src.api import pipeline_config

    monkeypatch.setattr(pipeline_config, "set_scene_pinned", lambda *a, **k: 0)
    r = _pin(client, "1735-c")
    assert r.json() == {"message": "置顶状态已更新", "scene_id": "1735-c", "is_pinned": 1}


def test_unpin_twice_clears_state(client):
    _pin(client, "1735-c")
    _pin(client, "1735-c", False)
    _pin(client, "1735-c", False)
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT is_pinned, pinned_at FROM devops_pipeline_scene "
                  "WHERE scene_id=? AND repo_key=?", ("1735-c", ALICE_REPO))
        row = c.fetchone()
    assert row[0] == 0
    assert row[1] is None


# ===== 验收 6：缺列降级 =====

def test_missing_pin_columns_degrades(monkeypatch):
    import threading
    from src.api import pipeline_config

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    c = conn.cursor()
    c.execute("CREATE TABLE devops_pipeline_scene ("
              "id INTEGER PRIMARY KEY, scene_id TEXT, name TEXT, description TEXT, "
              "nodes_json TEXT, onboarding TEXT, repo_key TEXT, is_active INTEGER)")
    for sid in ("1735-a", "1735-b", "1735-c"):
        c.execute("INSERT INTO devops_pipeline_scene "
                  "(scene_id, name, description, nodes_json, onboarding, repo_key, is_active) "
                  "VALUES (?,?,?,?,?,?,1)",
                  (sid, sid, "", "[]", "repo-onboarding", "gitcode:x/y"))
    conn.commit()
    monkeypatch.setattr(pipeline_config, "_db", lambda: (conn, threading.Lock()))
    rows = pipeline_config._query_scenes_from_db(repo_key="gitcode:x/y")
    assert [r["scene_id"] for r in rows] == ["1735-a", "1735-b", "1735-c"]
    assert all(r["is_pinned"] == 0 and r["pinned_at"] == "" for r in rows)
    conn.close()


# ===== 验收 7：归属校验 =====

def test_bob_cannot_pin_alice_scene(client):
    _as(2)
    # 端点级：bob 用自己的仓库置顶 alice 场景 → 仓库内无该场景 → 404
    _pin(client, "1735-a", True, repo="bob/repo2", expect=404)
    # 端点级：bob 指定 alice 仓库 → 无仓库权限（check_repo_access 口径）→ 403
    _pin(client, "1735-a", True, repo=ALICE_SLUG, expect=403)

    _as(1)
    rows = [s for s in client.get(f"/api/scenes?repo={ALICE_SLUG}").json()["scenes"]
            if s["id"] == "1735-a"]
    assert rows[0]["is_pinned"] == 0
    assert _pinned_at("1735-a") is None


# ===== 验收 8：接口契约 =====

def test_list_contracts_carry_pin_fields(client):
    _pin(client, "1735-b")
    r = client.get(f"/api/scenes?repo={ALICE_SLUG}")
    item = next(s for s in r.json()["scenes"] if s["id"] == "1735-b")
    assert item["is_pinned"] == 1
    assert item["pinned_at"]

    r2 = client.get(f"/api/scenes/with-pipeline?repo={ALICE_SLUG}")
    item2 = next(s for s in r2.json()["scenes"] if s["id"] == "1735-b")
    assert item2["is_pinned"] == 1
    assert item2["pinned_at"] == item["pinned_at"]

    r3 = _pin(client, "1735-b")
    body = r3.json()
    assert body["scene_id"] == "1735-b"
    assert body["is_pinned"] == 1