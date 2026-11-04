"""Issue #885：场景自定义变量密钥字段脱敏（占位 + 按需取回）。

对齐 #474 credentials reveal 修复模式：

  1. GET /api/scenes/{scene_id}/advanced-options：is_secret=true 字段占位返回
     （value:"" + has_value 标记），不再明文下发；
  2. POST /api/scenes/{scene_id}/advanced-options/reveal：密码二次确认（错误/未设 → 400）、
     非密钥字段拒绝、字段/场景不存在 → 404、明文仅当次返回（no-store）、
     审计日志（scene_secret_reveal，不落明文）、高频限流（429）；
  3. 写入侧兼容：secret 空值回传 → 服务端保留旧值（回归断言）。

运行：python -m pytest tests/test_issue885_scene_secret_reveal.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

from fastapi.testclient import TestClient
from src.api.main import app
from src.session_manager import db, db_lock

client = TestClient(app)

RK = "gitcode:org/scene885"
SCENE = "scene-885"
REPO_PARAM = "org/scene885"

_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    from src.auth.password import hash_password
    from src.api.jwt_utils import create_access_token
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, token_version FROM grape_accounts WHERE username='test_885_reveal'")
        row = c.fetchone()
        if row:
            _uid = {"id": row[0]}
            ver = row[1] or 0
        else:
            c.execute(
                "INSERT INTO grape_accounts (username, email, password_hash, nickname, status, token_version) "
                "VALUES (?,?,?,?,1,0)",
                ("test_885_reveal", None, hash_password("test123"), "test_885_reveal"))
            db.commit()
            _uid = {"id": c.lastrowid}
            ver = 0
        # 清理残留
        c.execute("DELETE FROM grape_t_credential_reveal_log WHERE user_id=?", (_uid["id"],))
        c.execute("DELETE FROM audit_logs WHERE event_type='scene_secret_reveal' AND user_id=?", (_uid["id"],))
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=?", (SCENE,))
        c.execute("DELETE FROM user_repos WHERE user_id=? AND repo_key=?", (_uid["id"], RK))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        # 种子：托管仓库 + 用户可见 + 场景（含 secret 与非 secret 字段）
        c.execute(
            "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
            (RK, "org", "scene885", "scene885"))
        c.execute(
            "INSERT INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) VALUES (?,?,?,?,1,1)",
            (_uid["id"], RK, "org", "scene885"))
        c.execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active, advanced_options) "
            "VALUES (?,?,?,?,1,?)",
            (SCENE, "Scene 885", RK, "[]",
             '{"work_branch": "main", "custom_fields": ['
             '{"name": "DEPLOY_KEY", "value": "ultra-secret-885", "is_secret": true}, '
             '{"name": "PLAIN_ENV", "value": "prod", "is_secret": false}]}'))
        db.commit()
    _headers = {"Authorization": "Bearer " + create_access_token(_uid["id"], "test_885_reveal", ver)}


def teardown_module():
    if _uid:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_t_credential_reveal_log WHERE user_id=?", (_uid["id"],))
            c.execute("DELETE FROM audit_logs WHERE event_type='scene_secret_reveal' AND user_id=?", (_uid["id"],))
            c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id=?", (SCENE,))
            c.execute("DELETE FROM user_repos WHERE user_id=? AND repo_key=?", (_uid["id"], RK))
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (_uid["id"],))
            db.commit()


class TestAdvancedOptionsPlaceholder:
    """GET 占位化：is_secret 不再明文返回"""

    def test_get_masks_secret_but_keeps_plain(self):
        r = client.get(f"/api/scenes/{SCENE}/advanced-options",
                       params={"repo": REPO_PARAM}, headers=_headers)
        assert r.status_code == 200, r.text
        by_name = {f["name"]: f for f in r.json()["custom_fields"]}
        # secret 字段：占位返回
        sec = by_name["DEPLOY_KEY"]
        assert sec["is_secret"] is True
        assert sec["value"] == ""
        assert sec["has_value"] is True
        assert "ultra-secret-885" not in r.text
        # 非 secret 字段：保持明文（原行为）
        assert by_name["PLAIN_ENV"]["value"] == "prod"
        assert by_name["PLAIN_ENV"].get("has_value") in (None, True)


class TestSceneSecretReveal:
    """按需取回明文接口（Issue #885，对齐 #474 模式）"""

    def test_reveal_requires_password_confirm(self):
        for pwd in ("wrong-pass", ""):
            r = client.post(f"/api/scenes/{SCENE}/advanced-options/reveal",
                            params={"repo": REPO_PARAM},
                            json={"name": "DEPLOY_KEY", "password": pwd},
                            headers=_headers)
            assert r.status_code == 400, r.text
            assert "密码" in r.json()["detail"]["message"]

    def test_reveal_returns_plaintext_once_with_no_store(self):
        r = client.post(f"/api/scenes/{SCENE}/advanced-options/reveal",
                        params={"repo": REPO_PARAM},
                        json={"name": "DEPLOY_KEY", "password": "test123"},
                        headers=_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["name"] == "DEPLOY_KEY"
        assert data["value"] == "ultra-secret-885"
        assert "no-store" in (r.headers.get("cache-control", "").lower())
        # 回归：GET 仍占位，无明文
        rg = client.get(f"/api/scenes/{SCENE}/advanced-options",
                        params={"repo": REPO_PARAM}, headers=_headers)
        assert "ultra-secret-885" not in rg.text

    def test_reveal_non_secret_field_rejected(self):
        r = client.post(f"/api/scenes/{SCENE}/advanced-options/reveal",
                        params={"repo": REPO_PARAM},
                        json={"name": "PLAIN_ENV", "password": "test123"},
                        headers=_headers)
        assert r.status_code == 400, r.text

    def test_reveal_missing_field_or_scene(self):
        r = client.post(f"/api/scenes/{SCENE}/advanced-options/reveal",
                        params={"repo": REPO_PARAM},
                        json={"name": "NOT_EXIST", "password": "test123"},
                        headers=_headers)
        assert r.status_code == 404, r.text
        r2 = client.post("/api/scenes/scene-nope-885/advanced-options/reveal",
                         params={"repo": REPO_PARAM},
                         json={"name": "DEPLOY_KEY", "password": "test123"},
                         headers=_headers)
        assert r2.status_code == 404, r2.text

    def test_reveal_missing_repo_rejected(self):
        r = client.post(f"/api/scenes/{SCENE}/advanced-options/reveal",
                        json={"name": "DEPLOY_KEY", "password": "test123"},
                        headers=_headers)
        assert r.status_code == 400, r.text

    def test_reveal_writes_audit_log_without_plaintext(self):
        r = client.post(f"/api/scenes/{SCENE}/advanced-options/reveal",
                        params={"repo": REPO_PARAM},
                        json={"name": "DEPLOY_KEY", "password": "test123"},
                        headers=_headers)
        assert r.status_code == 200, r.text
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT detail FROM audit_logs WHERE event_type='scene_secret_reveal' "
                "AND user_id=? ORDER BY seq DESC LIMIT 1", (_uid["id"],))
            row = c.fetchone()
        assert row, "应写入 scene_secret_reveal 审计日志"
        assert "ultra-secret-885" not in row[0], "审计日志不得记录明文值"
        import json as _json
        detail = _json.loads(row[0])
        assert detail.get("scene_id") == SCENE
        assert detail.get("repo_key") == RK
        assert detail.get("name") == "DEPLOY_KEY"

    def test_reveal_high_frequency_alert(self):
        from src.api.routes.settings import _REVEAL_MAX_PER_WINDOW
        with db_lock:
            c = db.cursor()
            for _ in range(_REVEAL_MAX_PER_WINDOW):
                c.execute(
                    "INSERT INTO grape_t_credential_reveal_log (user_id, provider, field, ip) VALUES (?,?,?,?)",
                    (_uid["id"], "scene_advanced_options", "DEPLOY_KEY", "127.0.0.1"))
            db.commit()
        try:
            r = client.post(f"/api/scenes/{SCENE}/advanced-options/reveal",
                            params={"repo": REPO_PARAM},
                            json={"name": "DEPLOY_KEY", "password": "test123"},
                            headers=_headers)
            assert r.status_code == 429, r.text
            assert "value" not in r.json()
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM grape_t_credential_reveal_log WHERE user_id=?", (_uid["id"],))
                db.commit()


class TestWriteSideCompatibility:
    """写入侧兼容：secret 空值回传 → 保留旧值（Issue #885 回归）"""

    def test_save_blank_secret_keeps_stored_value(self):
        body = {
            "work_branch": "main",
            "custom_fields": [
                {"name": "DEPLOY_KEY", "value": "", "is_secret": True},
                {"name": "PLAIN_ENV", "value": "prod", "is_secret": False},
            ],
        }
        r = client.put(f"/api/scenes/{SCENE}/advanced-options",
                       params={"repo": REPO_PARAM}, json=body, headers=_headers)
        assert r.status_code == 200, r.text
        # 库内仍保留明文（注入/reveal 不受影响）
        with db_lock:
            c = db.cursor()
            c.execute("SELECT advanced_options FROM devops_pipeline_scene WHERE scene_id=?", (SCENE,))
            row = c.fetchone()
        import json as _json
        opts = _json.loads(row[0])
        by_name = {f["name"]: f for f in opts["custom_fields"]}
        assert by_name["DEPLOY_KEY"]["value"] == "ultra-secret-885"


if __name__ == "__main__":
    unittest.main()
