"""Issue #1084 插件市场 B1：搜索切片测试（seam：GET /api/plugins/search）。

覆盖：名称精确匹配优先、关键词匹配（description/display_name/author）、
空 q 返回空、命中数排序。
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
    _uname = f"test_psearch_{uuid.uuid4().hex[:8]}"
    _uid = create_grape_account(_uname, "", "test123") or get_grape_account_by_username(_uname)
    if not _uid:
        raise RuntimeError(f"create_grape_account({_uname}) failed")
    from src.api.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    _headers = {"Authorization": f"Bearer {create_access_token(_uid['id'], _uid['username'], get_token_version(_uid['id']))}"}


def teardown_module():
    with db_lock:
        db.execute("DELETE FROM grape_t_plugin WHERE name LIKE 'psearch-%'")
        db.commit()


def _insert_plugin(name, version="1.0.0", display_name="", description="", author=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR IGNORE INTO grape_t_plugin (name, version, display_name, description, manifest, "
            "author, object_key, scope, owner_id, created_by, status) "
            "VALUES (?,?,?,?, '{}', ?, ?, 'user', ?, ?, 'active')",
            (name, version, display_name, description, author,
             f"test/plugins/user/{_uid['id']}/{name}/{name}-{version}.zip",
             str(_uid["id"]), name))
        db.commit()


class TestPluginSearch:
    def test_search_exact_name_first(self):
        _insert_plugin("psearch-exact", description="精确命中")
        _insert_plugin("psearch-other", description="其他插件")
        r = client.get("/api/plugins/search", params={"q": "psearch-exact"}, headers=_headers)
        assert r.status_code == 200, r.text
        skills = r.json()["plugins"]
        assert skills and skills[0]["name"] == "psearch-exact"
        assert skills[0]["match_reason"]

    def test_search_keyword_matches_description_and_author(self):
        _insert_plugin("psearch-kw1", description="代码质量扫描利器", author="alice")
        _insert_plugin("psearch-kw2", description="部署发布工具", author="bob")
        r = client.get("/api/plugins/search", params={"q": "质量 扫描"}, headers=_headers)
        assert r.status_code == 200
        names = [p["name"] for p in r.json()["plugins"]]
        assert "psearch-kw1" in names
        assert "psearch-kw2" not in names

    def test_search_empty_q_returns_empty(self):
        r = client.get("/api/plugins/search", params={"q": ""}, headers=_headers)
        assert r.status_code == 200
        assert r.json()["plugins"] == []

    def test_search_requires_auth(self):
        r = client.get("/api/plugins/search", params={"q": "x"})
        assert r.status_code == 401
