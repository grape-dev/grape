"""Worker 安装包 + 下发端点测试（见 worker-server-delivery-design.md）。"""
import hashlib, io, os, sys, tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _clean_pkg_cache(monkeypatch, tmp_path):
    """隔离包缓存到临时目录，避免污染 data/cache。"""
    import src.worker_package as wp
    monkeypatch.setattr(wp, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(wp, "PKG_PATH", os.path.join(str(tmp_path), "grape-worker.tar.gz"))
    monkeypatch.setattr(wp, "VERSION_PATH", os.path.join(str(tmp_path), "VERSION"))
    yield


class TestWorkerPackage:
    def test_build_package_contents(self):
        from src.worker_package import build_package, MANIFEST
        p = build_package()
        assert os.path.isfile(p)
        tf = tarfile.open(p)
        names = tf.getnames()
        # 必须包含 VERSION / SHA256SUMS / 关键运行模块
        assert "grape-worker/VERSION" in names
        assert "grape-worker/SHA256SUMS" in names
        assert "grape-worker/src/config.py" in names
        assert "grape-worker/src/runtime/agent.py" in names
        assert "grape-worker/src/agent/executor.py" in names
        assert "grape-worker/requirements-worker.txt" in names
        # 不应泄漏服务端代码 / 配置 / 静态资源（内置 Python stdlib 含 statistics.py 等，跳过）
        for n in names:
            if n.startswith("grape-worker/python/"):
                continue
            assert "api/main.py" not in n
            assert "api/routes" not in n
            assert "static" not in n
            assert "configs" not in n
        tf.close()

    def test_sha256sums_match_payload(self):
        from src.worker_package import build_package
        p = build_package()
        tf = tarfile.open(p)
        sums = {}
        for line in tf.extractfile("grape-worker/SHA256SUMS").read().decode().splitlines():
            h, path = line.split("  ")
            sums[path] = h
        ok = 0
        for name in tf.getnames():
            if name in ("grape-worker/SHA256SUMS", "grape-worker/VERSION"):
                continue
            data = tf.extractfile(name).read()
            if hashlib.sha256(data).hexdigest() == sums.get(name):
                ok += 1
        tf.close()
        assert ok == len(sums)                       # 每个条目均有对应文件且哈希一致
        assert len(sums) >= 28                       # 原闭包文件数（28）
        assert any(k.endswith(".whl") for k in sums) # wheels 已随包下发（离线安装依赖）

    def test_version_cache_hit_reuse(self):
        import src.worker_package as wp
        p1 = wp.build_package()
        mtime1 = os.path.getmtime(p1)
        # 第二次调用命中缓存，不重建
        p2 = wp.build_package()
        assert os.path.getmtime(p2) == mtime1

    def test_version_changes_rebuild(self):
        import src.worker_package as wp
        # 无 git 环境时版本指纹 = manifest 内容指纹；mtime 变化应触发指纹变化
        m1 = wp._manifest_version()
        cf = os.path.join(wp.ROOT, "src", "config.py")
        old = os.path.getmtime(cf)
        os.utime(cf, (old + 5, old + 5))
        try:
            m2 = wp._manifest_version()
            assert m2 != m1
            p2 = wp.build_package()
            with open(wp.VERSION_PATH, encoding="utf-8") as f:
                assert f.read().strip() == wp.package_version()
        finally:
            os.utime(cf, (old, old))

    def test_manifest_files_exist(self):
        from src.worker_package import MANIFEST, ROOT
        for item in MANIFEST:
            path = os.path.join(ROOT, item)
            assert os.path.exists(path), f"manifest 项缺失: {item}"


class TestInstallEndpoints:
    def _auth_client(self):
        import hashlib as _h
        from fastapi.testclient import TestClient
        from src.api.main import app
        from src.session_manager import db, db_lock
        token = "rta_" + os.urandom(12).hex()
        h = _h.sha256(token.encode()).hexdigest()
        cred_name = "t-worker-" + os.urandom(4).hex()
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO runtime_credentials (name, token_hash, status, scope, owner_name) VALUES (?, ?, 'active', 'user', 'tester')",
                (cred_name, h),
            )
            db.commit()
        client = TestClient(app)
        return client, token

    def test_install_script_public(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        c = TestClient(app)
        r = c.get("/api/runtime/install")
        assert r.status_code == 200
        assert r.text.startswith("#!/usr/bin/env bash")

    def test_latest_requires_auth(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        c = TestClient(app)
        assert c.get("/api/runtime/install/latest").status_code == 401
        assert c.get("/api/runtime/install/package").status_code == 401

    def test_latest_and_package_with_auth(self):
        client, token = self._auth_client()
        r = client.get("/api/runtime/install/latest", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["version"]
        r = client.get("/api/runtime/install/package", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/gzip"
        tf = tarfile.open(fileobj=io.BytesIO(r.content))
        assert "grape-worker/src/runtime/agent.py" in tf.getnames()
        tf.close()

    def test_bad_token_rejected(self):
        client, _ = self._auth_client()
        r = client.get("/api/runtime/install/latest", headers={"Authorization": "Bearer rta_wrong"})
        assert r.status_code == 401
