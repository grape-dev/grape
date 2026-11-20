"""[插件市场-A1] 发布门禁 API 测试（Issue #1079）。

POST /api/plugin-market/validate          manifest JSON 门禁（schema + 可选探测）
POST /api/plugin-market/validate-package  插件包 zip 门禁（schema + 禁二进制 + 可选探测）
上传/上架前强校验接入点：拒绝时返回结构化错误清单（路径+原因+修复提示）。
"""
import io
import json
import os
import zipfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest
from fastapi.testclient import TestClient

from src.api import auth_token
from src.api.main import app
from src.api.db_setup import init_database
from src.kernel.session_manager import db, db_lock
init_database()

client = TestClient(app)

pytestmark = pytest.mark.usefixtures("plugin_market_stubs")


def _token():
    return auth_token.issue_session_token(
        session_key="t_pm_validate", repo_key="gitcode:org/u-repo", agent_type="coding")


@pytest.fixture(autouse=True)
def _clean_tokens():
    with db_lock:
        db.cursor().execute("DELETE FROM session_api_tokens WHERE session_key LIKE 't_pm_%'")
        db.commit()
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM session_api_tokens WHERE session_key LIKE 't_pm_%'")
        db.commit()


def _post_validate(manifest, token=None):
    return client.post(
        "/api/plugin-market/validate",
        json={"manifest": manifest},
        headers={"Authorization": f"Bearer {token or _token()}"})


def _zip_bytes(manifest, extra_files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.json", json.dumps(manifest))
        for name, content in (extra_files or {"bin/run.sh": "#!/bin/sh\necho ok\n"}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _post_package(manifest, extra_files=None, token=None):
    return client.post(
        "/api/plugin-market/validate-package",
        files={"file": ("plugin.zip", _zip_bytes(manifest, extra_files), "application/zip")},
        headers={"Authorization": f"Bearer {token or _token()}"})


class TestAuth:
    def test_validate_requires_auth(self):
        assert client.post("/api/plugin-market/validate", json={"manifest": {}}).status_code == 401

    def test_validate_package_requires_auth(self):
        assert client.post("/api/plugin-market/validate-package").status_code == 401


class TestManifestGate:
    def test_valid_manifest_passes(self):
        r = _post_validate(self._manifest_factory())
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is True
        assert body["errors"] == []

    def test_probe_true_without_exec_whitelist_refused(self):
        # 安全门禁：probe=true 会以子进程真实执行 command；exec 白名单未声明必须拒绝而非执行
        m = self._manifest_factory()
        m.pop("permissions")
        r = client.post(
            "/api/plugin-market/validate",
            json={"manifest": m, "probe": True},
            headers={"Authorization": f"Bearer {_token()}"})
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert any(e["check"] == "probe" and e["code"] == "probe.exec_whitelist" for e in body["errors"])
        assert all(e["code"] != "probe.invalid" for e in body["errors"])

    def test_invalid_manifest_reports_field_paths(self):
        m = self._manifest_factory()
        m["version"] = "v1"
        r = _post_validate(m)
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert any("version" in e["path"] and e["hint"] for e in body["errors"])

    def test_missing_manifest_field_rejected(self):
        r = _post_validate({"name": "x"})
        assert r.status_code == 200
        assert r.json()["valid"] is False


class TestPackageGate:
    def test_text_only_package_passes(self):
        r = _post_package(self._manifest_factory())
        assert r.status_code == 200
        assert r.json()["valid"] is True

    def test_binary_in_package_rejected_with_path(self):
        r = _post_package(self._manifest_factory(), extra_files={"bin/evil": b"\x7fELFjunk"})
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert any(e["check"] == "binary" and "bin/evil" in e["path"] for e in body["errors"])

    def test_broken_zip_400(self):
        r = client.post(
            "/api/plugin-market/validate-package",
            files={"file": ("plugin.zip", b"not a zip", "application/zip")},
            headers={"Authorization": f"Bearer {_token()}"})
        assert r.status_code == 400

    def test_package_without_manifest_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("README.md", "no manifest here")
        r = client.post(
            "/api/plugin-market/validate-package",
            files={"file": ("plugin.zip", buf.getvalue(), "application/zip")},
            headers={"Authorization": f"Bearer {_token()}"})
        assert r.status_code == 200
        body = r.json()
        assert body["valid"] is False
        assert any("plugin.json" in e["reason"] for e in body["errors"])

    def test_zip_bomb_total_extract_size_rejected(self):
        # zip 炸弹防护：压缩包远小于上传上限，但解压后总量超限必须 400 拒绝（不落盘解压）
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("plugin.json", json.dumps(self._manifest_factory()))
            zf.writestr("blob1.txt", b"0" * (60 * 1024 * 1024))
            zf.writestr("blob2.txt", b"0" * (60 * 1024 * 1024))
        r = client.post(
            "/api/plugin-market/validate-package",
            files={"file": ("plugin.zip", buf.getvalue(), "application/zip")},
            headers={"Authorization": f"Bearer {_token()}"})
        assert r.status_code == 400
        assert "解压后总大小" in r.json()["detail"]["message"]

    def test_oversized_package_rejected_413(self):
        # 上传流分块读取：累计超限立即 413，不全量缓冲超大包
        oversized = b"0" * (50 * 1024 * 1024 + 1)  # MAX_PACKAGE_BYTES + 1
        r = client.post(
            "/api/plugin-market/validate-package",
            files={"file": ("plugin.zip", oversized, "application/zip")},
            headers={"Authorization": f"Bearer {_token()}"})
        assert r.status_code == 413
        assert "大小上限" in r.json()["detail"]["message"]


class TestRouteContract:
    def test_validate_package_route_is_sync_for_threadpool(self):
        # 事件循环防护：zip 校验/解压/子进程探测均为阻塞操作，
        # 路由必须是同步 def（FastAPI 自动进线程池执行），不得 async def 阻塞事件循环
        import inspect

        from src.domains.mgmt.routes import plugin_market as pm

        assert inspect.iscoroutinefunction(pm.validate_plugin_package) is False
        assert inspect.iscoroutinefunction(pm.validate_plugin) is False
