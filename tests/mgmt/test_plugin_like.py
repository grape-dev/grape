"""Issue #1084 插件市场 B1：点赞切片测试（seam：POST /api/plugins/{id}/like toggle）。

覆盖：点赞 → liked=True + like_count+1；再点 → 取消点赞；插件不存在 404；
列表/详情的 liked_by_me 联动。
"""
import os
from src.kernel.id_gen import uuid7_str
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402

client = TestClient(app)

_uid = None
_headers = {}


def setup_module():
    global _uid, _headers
    from src.domains.mgmt.auth.grape_account_db import create_grape_account, get_grape_account_by_username
    _uname = f"test_plike_{uuid.uuid4().hex[:8]}"
    _uid = create_grape_account(_uname, "", "test123") or get_grape_account_by_username(_uname)
    if not _uid:
        raise RuntimeError(f"create_grape_account({_uname}) failed")
    from src.api.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    _headers = {"Authorization": f"Bearer {create_access_token(_uid['id'], _uid['username'], get_token_version(_uid['id']))}"}


def teardown_module():
    with db_lock:
        db.execute("DELETE FROM grape_t_plugin WHERE name LIKE 'plike-%'")
        db.execute("DELETE FROM grape_t_plugin_likes WHERE plugin_id NOT IN (SELECT id FROM grape_t_plugin)")
        db.commit()


def _insert_plugin(name, like_count=0):
    with db_lock:
        c = db.cursor()
        pid = uuid7_str()
        c.execute(
            "INSERT OR IGNORE INTO grape_t_plugin (id, name, version, display_name, description, manifest, "
            "like_count, object_key, scope, owner_id, created_by, status) "
            "VALUES (?,?,?,?,'{}','{}',?,?,'user',?,?,'active')",
            (pid, name, "1.0.0", name, like_count,
             f"test/plugins/user/{_uid['id']}/{name}/{name}-1.0.0.zip",
             str(_uid["id"]), name))
        db.commit()
        return pid


class TestPluginLike:
    def test_like_toggle_cycle(self):
        pid = _insert_plugin("plike-a")
        # 点赞
        r = client.post(f"/api/plugins/{pid}/like", headers=_headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"liked": True, "like_count": 1}
        # 详情联动 liked_by_me
        d = client.get(f"/api/plugins/by-id/{pid}", headers=_headers).json()
        assert d["liked_by_me"] is True and d["like_count"] == 1
        # 再点 → 取消
        r = client.post(f"/api/plugins/{pid}/like", headers=_headers)
        assert r.status_code == 200
        assert r.json() == {"liked": False, "like_count": 0}

    def test_like_unknown_plugin_404(self):
        r = client.post("/api/plugins/99999999/like", headers=_headers)
        assert r.status_code == 404

    def test_like_requires_auth(self):
        r = client.post("/api/plugins/1/like")
        assert r.status_code == 401
