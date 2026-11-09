"""Issue #487 回归测试：智能推荐规则保存/读取（repo_key 前缀解析 + 落库兜底 + 显式失败）。

覆盖：
1. PUT 保存后 GET 能读回 fallback_scene 与 rules（gitcode: 与 github: 双前缀）；
2. github: 托管仓（managed_repos 仅有 github: 行）读写使用真实前缀，不再错 key 假成功；
3. 场景列表为空 → PUT 返回 400（不再静默丢保存）；
4. managed_repos 无行时 UPDATE 0 行 → INSERT 兜底后成功落库（db 层直测）；
5. 未保存时 GET 返回默认空配置。
"""
import os
import json

os.environ.setdefault("DEVOPS_DB_PATH",
                      os.path.join(os.path.dirname(__file__), "..", "data", "test_sessions.db"))

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock
from src.user import db as user_db

client = TestClient(app)

REPO_GH = "github:sr/repo-gh"
REPO_GC = "gitcode:sr/repo-gc"
REPO_NO_SCENE = "gitcode:sr/no-scene"
REPO_NO_ROW = "gitcode:sr/no-row"

_uid = None
_token = ""
_headers = {}


def _jwt_access_token(user_id, username="sr_test"):
    from src.api.jwt_utils import create_access_token
    with db_lock:
        c = db.cursor()
        c.execute("SELECT token_version FROM grape_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    ver = row[0] if row else 0
    return create_access_token(user_id, username, ver)


def _set_scene(repo_key, scene_id):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active) "
            "VALUES (?,?,?,?,1)",
            (scene_id, scene_id, repo_key, json.dumps([{"id": "coding", "skill": "coding-cli"}])))
        db.commit()


def setup_module():
    global _uid, _token, _headers
    from src.auth.password import hash_password
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM grape_accounts WHERE username='sr_test'")
        row = c.fetchone()
        if row:
            _uid = {"id": row[0], "username": "sr_test"}
        else:
            c.execute("INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                      "VALUES (?,?,?,?,1,0)",
                      ("sr_test", None, hash_password("test123"), "sr_test"))
            db.commit()
            _uid = {"id": c.lastrowid, "username": "sr_test"}
    _token = _jwt_access_token(_uid["id"])
    _headers = {"Authorization": f"Bearer {_token}"}


def teardown_module():
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_accounts WHERE id=?", (_uid["id"],))
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (_uid["id"],))
            db.commit()


def _cleanup():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:sr/%' OR repo_key LIKE 'github:sr/%'")
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key LIKE 'gitcode:sr/%' OR repo_key LIKE 'github:sr/%'")
        db.commit()


def setup_function():
    _cleanup()
    with db_lock:
        c = db.cursor()
        # github: 托管仓（仅 github: 行，验证 _resolve_repo_key 前缀解析）
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                  (REPO_GH, "sr", "repo-gh", "sr/repo-gh"))
        # gitcode: 托管仓
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                  (REPO_GC, "sr", "repo-gc", "sr/repo-gc"))
        # 有托管行但无场景
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
                  (REPO_NO_SCENE, "sr", "no-scene", "sr/no-scene"))
        db.commit()
    _set_scene(REPO_GH, "scene-a")
    _set_scene(REPO_GH, "scene-b")
    _set_scene(REPO_GC, "scene-gc")
    _set_scene(REPO_NO_ROW, "scene-x")  # 场景存在但 managed_repos 无行（upsert 兜底用）


def teardown_function():
    _cleanup()


def test_get_default_when_not_saved():
    r = client.get("/api/repos/sr/repo-gh/scene-recommend-config", headers=_headers)
    assert r.status_code == 200
    assert r.json() == {"fallback_scene": "", "rules": []}


def test_save_then_readback_github_prefix():
    body = {"fallback_scene": "scene-b",
            "rules": [{"scene_id": "scene-a", "condition": "代码实现类任务"}]}
    r = client.put("/api/repos/sr/repo-gh/scene-recommend-config", json=body, headers=_headers)
    assert r.status_code == 200, r.text
    # 保存后读回（读取路径与写入 key 对齐）
    g = client.get("/api/repos/sr/repo-gh/scene-recommend-config", headers=_headers)
    assert g.status_code == 200
    cfg = g.json()
    assert cfg["fallback_scene"] == "scene-b"
    assert cfg["rules"] == [{"scene_id": "scene-a", "condition": "代码实现类任务"}]
    # DB 实际落库在 github: 前缀行（不再是 gitcode: 错 key 假成功）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT scene_recommend_config FROM managed_repos WHERE repo_key=?", (REPO_GH,))
        row = c.fetchone()
    assert row and row[0]
    assert json.loads(row[0])["fallback_scene"] == "scene-b"


def test_save_then_readback_gitcode_prefix():
    body = {"fallback_scene": "scene-gc", "rules": []}
    r = client.put("/api/repos/sr/repo-gc/scene-recommend-config", json=body, headers=_headers)
    assert r.status_code == 200, r.text
    g = client.get("/api/repos/sr/repo-gc/scene-recommend-config", headers=_headers)
    assert g.status_code == 200
    assert g.json()["fallback_scene"] == "scene-gc"


def test_empty_scenes_returns_400_not_fake_success():
    r = client.put("/api/repos/sr/no-scene/scene-recommend-config",
                   json={"fallback_scene": "whatever", "rules": []}, headers=_headers)
    assert r.status_code == 400, r.text
    assert "场景" in (r.json().get("detail") or {}).get("message", "")


def test_empty_fallback_scene_returns_400():
    r = client.put("/api/repos/sr/repo-gh/scene-recommend-config",
                   json={"fallback_scene": "  ", "rules": []}, headers=_headers)
    assert r.status_code == 400


def test_db_upsert_fallback_when_no_managed_row():
    """managed_repos 无行 → UPDATE 0 行 → INSERT 兜底后写入成功（#487 落库兜底）。"""
    ok = user_db.set_scene_recommend_config(REPO_NO_ROW, "scene-x", [{"scene_id": "scene-x", "condition": "c1"}])
    assert ok is True
    with db_lock:
        c = db.cursor()
        c.execute("SELECT scene_recommend_config, full_name FROM managed_repos WHERE repo_key=?", (REPO_NO_ROW,))
        row = c.fetchone()
    assert row and row[0]
    cfg = json.loads(row[0])
    assert cfg["fallback_scene"] == "scene-x"
    assert cfg["rules"] == [{"scene_id": "scene-x", "condition": "c1"}]


def test_db_empty_scenes_raises_value_error():
    import pytest
    with pytest.raises(ValueError):
        user_db.set_scene_recommend_config(REPO_NO_SCENE, "any", [])
