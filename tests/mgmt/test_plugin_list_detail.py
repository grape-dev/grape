"""Issue #1084 插件市场 B1：列表/详情/分类切片测试（seam：GET /api/plugins、/by-id/{id}、/categories）。

覆盖：
- 列表分页/排序/scope 过滤/分类过滤
- 三态可见性：user scope 仅本人可见；official 全员可见
- 详情：zip_url（OBS 签名 URL）+ liked_by_me + 非 owner 访问 user 插件 403
- 分类端点：返回 grape_t_plugin_category 列表
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
_other_uid = None
_other_headers = {}


def setup_module():
    global _uid, _headers, _other_uid, _other_headers
    from src.domains.mgmt.auth.grape_account_db import (create_grape_account,
                                           get_grape_account_by_username)
    for attr in ("_uid", "_other_uid"):
        _uname = f"test_plist_{uuid.uuid4().hex[:8]}"
        acc = create_grape_account(_uname, "", "test123") or get_grape_account_by_username(_uname)
        if not acc:
            raise RuntimeError(f"create_grape_account({_uname}) failed")
        if attr == "_uid":
            _uid = acc
        else:
            _other_uid = acc
    from src.api.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    _headers = {"Authorization": f"Bearer {create_access_token(_uid['id'], _uid['username'], get_token_version(_uid['id']))}"}
    _other_headers = {"Authorization": f"Bearer {create_access_token(_other_uid['id'], _other_uid['username'], get_token_version(_other_uid['id']))}"}


def teardown_module():
    with db_lock:
        db.execute("DELETE FROM grape_t_plugin WHERE name LIKE 'plist-%'")
        db.commit()


def _insert_plugin(name, version="1.0.0", scope="user", owner_id=None, category_id=0,
                   like_count=0, description=""):
    """直接插入插件行（列表/详情切片不需要走上传链）。"""
    owner_id = str(owner_id if owner_id is not None else _uid["id"])
    with db_lock:
        c = db.cursor()
        pid = uuid7_str()
        c.execute(
            "INSERT OR IGNORE INTO grape_t_plugin (id, name, version, display_name, description, manifest, "
            "category_id, like_count, object_key, scope, owner_id, created_by, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active')",
            (pid, name, version, name, description, "{}", category_id, like_count,
             f"test/plugins/{scope}/{owner_id}/{name}/{name}-{version}.zip",
             scope, owner_id, str(_uid["id"])))
        db.commit()
        return pid


@pytest.fixture(autouse=True)
def _fake_storage(monkeypatch):
    from src.domains.runtime.storage import StorageBackend

    class FakeStorage(StorageBackend):
        def __init__(self):
            self.objects = {}
            self.default_expires = 300

        def backend_name(self):
            return "obs"

        def save_bytes(self, key, data):
            self.objects[key] = data
            return key

        def read_bytes(self, key):
            return self.objects.get(key)

        def read_file(self, key, local_path):
            data = self.objects.get(key)
            if data is None:
                return False
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(data)
            return True

        def save_stream(self, key, source):
            with open(source, "rb") as f:
                self.objects[key] = f.read()
            return key

        def exists(self, key):
            return key in self.objects

        def list_keys(self, prefix):
            return [k for k in self.objects if k.startswith(prefix)]

        def delete_object(self, key):
            return key in self.objects

        def delete_prefix(self, prefix):
            return 0

        def temporary_url(self, key, expires=300):
            if key not in self.objects:
                return ""
            return f"https://obs.test/{key}?expires={expires}"

    fake = FakeStorage()
    # 预置对象：详情 zip_url 只对存在的对象签发
    with db_lock:
        pass
    monkeypatch.setattr("src.domains.runtime.storage.get_storage", lambda: fake)
    from src.domains.runtime.storage import reset_storage
    reset_storage()
    yield fake


class TestPluginList:
    def test_list_returns_user_plugins_with_paging(self):
        _insert_plugin("plist-a", version="1.0.0")
        _insert_plugin("plist-b", version="2.0.0")
        r = client.get("/api/plugins", params={"scope": "user", "page": 1, "page_size": 10},
                       headers=_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        names = [p["name"] for p in body["plugins"]]
        assert "plist-a" in names and "plist-b" in names
        assert body["total"] >= 2

    def test_list_user_scope_hidden_from_others(self):
        _insert_plugin("plist-secret", version="1.0.0", owner_id=_uid["id"])
        r = client.get("/api/plugins", params={"scope": "user", "page": 1, "page_size": 50},
                       headers=_other_headers)
        assert r.status_code == 200
        names = [p["name"] for p in r.json()["plugins"]]
        assert "plist-secret" not in names

    def test_list_sort_hot_by_like_count(self):
        _insert_plugin("plist-cold", like_count=1)
        _insert_plugin("plist-hot", like_count=99)
        r = client.get("/api/plugins", params={"scope": "user", "sort": "hot",
                                               "page": 1, "page_size": 50},
                       headers=_headers)
        assert r.status_code == 200
        plugins = [p for p in r.json()["plugins"] if p["name"].startswith("plist-")]
        counts = [p["like_count"] for p in plugins]
        assert counts == sorted(counts, reverse=True)

    def test_list_filter_by_category(self):
        with db_lock:
            c = db.cursor()
            cat_id = uuid7_str()
            c.execute("INSERT OR IGNORE INTO grape_t_plugin_category (id, name, slug, is_active) "
                      "VALUES (?, 'plist-cat-x', 'plist-cat-x', 1)", (cat_id,))
            db.commit()
        _insert_plugin("plist-catmatch", category_id=cat_id)
        _insert_plugin("plist-catmiss", category_id=0)
        r = client.get("/api/plugins", params={"scope": "user", "category_id": cat_id,
                                               "page": 1, "page_size": 50},
                       headers=_headers)
        assert r.status_code == 200
        names = [p["name"] for p in r.json()["plugins"]]
        assert "plist-catmatch" in names and "plist-catmiss" not in names
        # category_name 回填
        matched = [p for p in r.json()["plugins"] if p["name"] == "plist-catmatch"][0]
        assert matched["category_name"] == "plist-cat-x"

    def test_list_requires_auth(self):
        r = client.get("/api/plugins")
        assert r.status_code == 401


class TestPluginDetail:
    def test_detail_with_zip_url_and_like_fields(self, _fake_storage):
        pid = _insert_plugin("plist-detail")
        # 预置存储对象使 zip_url 可签发（三态读路径一致）
        with db_lock:
            c = db.cursor()
            key = c.execute("SELECT object_key FROM grape_t_plugin WHERE id=?", (pid,)).fetchone()[0]
        _fake_storage.objects[key] = b"plugin-zip-bytes"
        r = client.get(f"/api/plugins/by-id/{pid}", headers=_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == pid
        assert body["name"] == "plist-detail"
        assert body["zip_url"].startswith("https://obs.test/")
        assert body["liked_by_me"] is False
        assert body["like_count"] == 0

    def test_detail_user_plugin_forbidden_for_others(self):
        pid = _insert_plugin("plist-private")
        r = client.get(f"/api/plugins/by-id/{pid}", headers=_other_headers)
        assert r.status_code == 403

    def test_detail_404(self):
        r = client.get("/api/plugins/by-id/99999999", headers=_headers)
        assert r.status_code == 404


class TestPluginCategories:
    def test_categories_listed(self):
        r = client.get("/api/plugins/categories", headers=_headers)
        assert r.status_code == 200, r.text
        cats = r.json()["categories"]
        assert isinstance(cats, list) and len(cats) >= 1
        assert {"id", "name", "slug"} <= set(cats[0].keys())
