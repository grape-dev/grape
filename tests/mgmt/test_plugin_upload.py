"""Issue #1084 插件市场 B1：上传切片测试（seam：POST /api/plugins/upload 公开接口）。

覆盖：
- 合法插件 zip 上传成功：DB 落行（name/version/scope/owner）+ 对象存储落包 + 读路径键一致
- 非法 manifest（缺 manifest.json / 缺必填字段）→ 400 + 明确错误码，不入库
- L0 校验适配层：A1 未合入时降级提示（degraded 标记），A1 模块可用时委托
- scope 校验（user/repo）、repo scope 需指定 repo
"""
import io
from src.kernel.id_gen import uuid7_str
import os
import sys
import uuid
import zipfile

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
    _uname = f"test_plugin_{uuid.uuid4().hex[:8]}"
    _uid = create_grape_account(_uname, "", "test123") or get_grape_account_by_username(_uname)
    if not _uid:
        raise RuntimeError(f"create_grape_account({_uname}) failed")
    from src.api.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    _token = create_access_token(_uid["id"], _uid["username"], get_token_version(_uid["id"]))
    _headers = {"Authorization": f"Bearer {_token}"}


def teardown_module():
    with db_lock:
        db.execute("DELETE FROM grape_t_plugin WHERE scope='user' AND owner_id=?",
                   (str(_uid["id"]),))
        db.commit()


@pytest.fixture(autouse=True)
def _fake_storage(monkeypatch):
    """内存 OBS 模拟：monkeypatch get_storage 返回 FakeStorage（复用 test_skills_obs 模式）。"""
    import src.domains.runtime.storage as storage_mod
    fake = storage_mod._plugin_test_fake = _make_fake_storage(storage_mod)
    monkeypatch.setattr("src.domains.runtime.storage.get_storage", lambda: fake)
    storage_mod.reset_storage()
    yield fake


def _make_fake_storage(storage_mod):
    """构建 FakeStorage（内联定义，避免依赖既有测试模块的导入副作用）。"""
    from src.domains.runtime.storage import StorageBackend

    class FakeStorage(StorageBackend):
        def __init__(self):
            self.objects = {}
            self.deleted = []
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
            if key in self.objects:
                self.objects.pop(key, None)
                self.deleted.append(key)
                return True
            return False

        def delete_prefix(self, prefix):
            keys = [k for k in self.objects if k.startswith(prefix)]
            for k in keys:
                self.objects.pop(k, None)
            self.deleted.extend(keys)
            return len(keys)

        def temporary_url(self, key, expires=300):
            return f"https://obs.test/{key}?expires={expires}"

    return FakeStorage()


def _make_plugin_zip(name: str = "hello-plugin", version: str = "1.0.0",
                     manifest: dict | None = None, extra_files: dict | None = None,
                     with_manifest: bool = True) -> bytes:
    """构造插件 zip：根目录 {name}/manifest.json + 可选附加文件。

    Issue #1502 项2：L0 完整校验生效后，合法 manifest 需满足 B1 schema
    （schema_version/category 必填，见 src/plugin_market/manifest.py）。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if manifest is None:
            manifest = {
                "schema_version": "1.0",
                "name": name,
                "version": version,
                "display_name": f"{name} 插件",
                "description": "测试插件",
                "author": "tester",
                "category": "other",
            }
        if with_manifest:
            import json as _json
            zf.writestr(f"{name}/manifest.json", _json.dumps(manifest, ensure_ascii=False))
        for rel, content in (extra_files or {}).items():
            zf.writestr(f"{name}/{rel}", content)
    return buf.getvalue()


class TestPluginUpload:
    def test_upload_valid_plugin_ok(self):
        zip_bytes = _make_plugin_zip("hello-plugin", "1.2.0")
        r = client.post("/api/plugins/upload", files={"file": ("hello-plugin.zip", zip_bytes,
                                                                "application/zip")},
                        data={"scope": "user"}, headers=_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "hello-plugin" in body.get("message", "")
        # DB 落行（version 字段就位）
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT name, version, scope, owner_id, object_key, status FROM grape_t_plugin "
                "WHERE name='hello-plugin' AND scope='user' AND owner_id=?",
                (str(_uid["id"]),)).fetchone()
        assert row is not None
        assert row[1] == "1.2.0"
        assert row[5] == "active"
        # 对象存储落包 + DB object_key 与存储键一致（三态读路径基础）
        import src.domains.runtime.storage as storage_mod
        fake = storage_mod._plugin_test_fake
        assert row[4] in fake.objects

    def test_upload_missing_manifest_rejected_400(self):
        zip_bytes = _make_plugin_zip("no-manifest", with_manifest=False)
        r = client.post("/api/plugins/upload", files={"file": ("no-manifest.zip", zip_bytes,
                                                               "application/zip")},
                        data={"scope": "user"}, headers=_headers)
        assert r.status_code == 400
        detail = r.json().get("detail", {})
        assert detail.get("code") == 400
        # 非法插件不入库
        with db_lock:
            c = db.cursor()
            n = c.execute("SELECT COUNT(*) FROM grape_t_plugin WHERE name='no-manifest'").fetchone()[0]
        assert n == 0

    def test_upload_manifest_missing_required_field_rejected(self):
        zip_bytes = _make_plugin_zip("bad-manifest", manifest={"name": "bad-manifest"})
        r = client.post("/api/plugins/upload", files={"file": ("bad-manifest.zip", zip_bytes,
                                                               "application/zip")},
                        data={"scope": "user"}, headers=_headers)
        assert r.status_code == 400
        detail = r.json().get("detail", {})
        assert detail.get("code") == 400
        assert any("version" in e for e in (detail.get("errors") or []))

    def test_upload_invalid_manifest_field_errors_via_a1(self):
        """Issue #1502 项2：非法 manifest → 400 字段级错误，且 validator=a1（L0 生效）。"""
        zip_bytes = _make_plugin_zip("l0-invalid", manifest={
            "schema_version": "1.0", "name": "l0-invalid", "version": "1.0.0"})
        r = client.post("/api/plugins/upload", files={"file": ("l0-invalid.zip", zip_bytes,
                                                               "application/zip")},
                        data={"scope": "user"}, headers=_headers)
        assert r.status_code == 400, r.text
        detail = r.json().get("detail", {})
        assert any("category" in e for e in (detail.get("errors") or [])), detail
        assert detail.get("validator") == "a1"
        assert detail.get("degraded") is False
        with db_lock:
            c = db.cursor()
            n = c.execute("SELECT COUNT(*) FROM grape_t_plugin WHERE name='l0-invalid'").fetchone()[0]
        assert n == 0

    def test_upload_valid_plugin_no_l0_degraded_hint(self):
        """Issue #1502 项2：合法包 → l0_validator 非 builtin、无 l0_degraded_hint。"""
        zip_bytes = _make_plugin_zip("l0-valid")
        r = client.post("/api/plugins/upload", files={"file": ("l0-valid.zip", zip_bytes,
                                                               "application/zip")},
                        data={"scope": "user"}, headers=_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("l0_validator") not in (None, "", "builtin")
        assert "l0_degraded_hint" not in body

    def test_upload_repo_scope_requires_repo(self):
        zip_bytes = _make_plugin_zip("repo-scope-plugin")
        r = client.post("/api/plugins/upload", files={"file": ("p.zip", zip_bytes,
                                                               "application/zip")},
                        data={"scope": "repo", "repo": ""}, headers=_headers)
        assert r.status_code == 400

    def test_upload_invalid_scope_rejected(self):
        zip_bytes = _make_plugin_zip("any-plugin")
        r = client.post("/api/plugins/upload", files={"file": ("p.zip", zip_bytes,
                                                               "application/zip")},
                        data={"scope": "official"}, headers=_headers)
        assert r.status_code == 400

    def test_upload_requires_auth(self):
        zip_bytes = _make_plugin_zip("authless-plugin")
        r = client.post("/api/plugins/upload", files={"file": ("p.zip", zip_bytes,
                                                               "application/zip")},
                        data={"scope": "user"})
        assert r.status_code == 401


class TestPluginUploadRootDetection:
    """Issue #1502 项3：插件根判定改为「manifest.json 所在目录」。

    兼容两种布局：根平铺（manifest.json 在 zip 根，含 scripts/ 等子目录）与
    <插件名>/ 单子目录前缀（回归不破坏）。
    """

    def _upload(self, name, zip_bytes):
        return client.post("/api/plugins/upload",
                           files={"file": (f"{name}.zip", zip_bytes, "application/zip")},
                           data={"scope": "user"}, headers=_headers)

    def test_flat_layout_with_subdirs_uploads_ok(self):
        """根平铺包：manifest.json 在 zip 根 + scripts/ 子目录 → 不再误判缺 manifest。"""
        manifest = {
            "schema_version": "1.0",
            "name": "flat-plugin",
            "version": "1.0.0",
            "category": "other",
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            import json as _json
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False))
            zf.writestr("README.md", "# flat")
            zf.writestr("scripts/run.sh", "#!/bin/sh\necho ok\n")
            zf.writestr("skills/main.md", "skill text")
        r = self._upload("flat-plugin", buf.getvalue())
        assert r.status_code == 200, r.text
        assert "flat-plugin" in r.json().get("message", "")

    def test_single_subdir_prefix_layout_still_ok(self):
        """回归：`<插件名>/` 前缀单子目录包继续可用。"""
        zip_bytes = _make_plugin_zip("prefixed-plugin", "2.0.0")
        r = self._upload("prefixed-plugin", zip_bytes)
        assert r.status_code == 200, r.text
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT version FROM grape_t_plugin WHERE name='prefixed-plugin' "
                            "AND scope='user' AND owner_id=?", (str(_uid["id"]),)).fetchone()
        assert row is not None and row[0] == "2.0.0"

    def test_no_manifest_anywhere_rejected(self):
        """两种布局均无 manifest.json → 仍然 400 PLUGIN_MANIFEST_MISSING。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.md", "# no manifest")
            zf.writestr("scripts/run.sh", "echo hi")
        r = self._upload("no-manifest-anywhere", buf.getvalue())
        assert r.status_code == 400
        detail = r.json().get("detail", {})
        assert detail.get("error_code") == "PLUGIN_MANIFEST_MISSING"


class TestPluginL0Adapter:
    def test_l0_adapter_default_uses_a1_not_degraded(self, monkeypatch):
        """Issue #1502 项2：默认（未设 env）即接入 A1 组合入口，不再恒 builtin 降级。"""
        from src.domains.mgmt.plugin_market import l0_adapter as plugin_l0
        monkeypatch.delenv("PLUGIN_L0_VALIDATOR_MODULE", raising=False)
        monkeypatch.setattr(plugin_l0, "_cache_a1", None)
        result = plugin_l0.validate_plugin_l0(
            {"schema_version": "1.0", "name": "p", "version": "1.0.0",
             "category": "other"}, ["p/manifest.json"])
        assert result["ok"] is True
        assert result["validator"] == "a1"
        assert result["degraded"] is False

    def test_l0_adapter_degrades_when_module_missing(self, monkeypatch):
        """降级语义保留：env 指向不存在的模块才走内置最小校验并显式标记 degraded。"""
        from src.domains.mgmt.plugin_market import l0_adapter as plugin_l0
        monkeypatch.setenv("PLUGIN_L0_VALIDATOR_MODULE", "src.definitely_not_a_module_xyz")
        monkeypatch.setattr(plugin_l0, "_cache_a1", None)
        result = plugin_l0.validate_plugin_l0({"name": "p", "version": "1.0.0"}, ["p/manifest.json"])
        assert result["ok"] is True
        assert result["validator"] == "builtin"
        assert result["degraded"] is True

    def test_l0_adapter_rejects_invalid_manifest(self):
        from src.domains.mgmt.plugin_market.l0_adapter import validate_plugin_l0
        result = validate_plugin_l0({"name": "p"}, ["p/manifest.json"])
        assert result["ok"] is False
        assert result["errors"]

    def test_l0_adapter_delegates_to_a1_module(self, monkeypatch):
        """A1 合入后（模块可导入且暴露 validate_plugin_manifest）自动委托。"""
        import types
        from src.domains.mgmt.plugin_market import l0_adapter as plugin_l0
        fake_mod = types.ModuleType("fake_a1_l0")
        calls = {}

        def _a1_validate(manifest, file_names):
            calls["hit"] = True
            return (False, ["A1 says no"])

        fake_mod.validate_plugin_manifest = _a1_validate
        monkeypatch.setenv("PLUGIN_L0_VALIDATOR_MODULE", "fake_a1_l0")
        monkeypatch.setattr(plugin_l0, "_load_a1_validator", lambda: ("fake_a1_l0", fake_mod))
        result = plugin_l0.validate_plugin_l0({"name": "p", "version": "1.0.0"}, [])
        assert calls.get("hit") is True
        assert result["ok"] is False
        assert result["validator"] == "a1"
        assert result["degraded"] is False
        assert "A1 says no" in result["errors"]
