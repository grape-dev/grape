"""Worker 安装包 + 下发端点测试（见 worker-server-delivery-design.md）。"""
import hashlib, io, os, sys, tarfile

import pytest



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
        # 无 git 环境时版本指纹 = manifest 内容 sha256 指纹；内容变化触发指纹变化。
        # （mtime 不参与指纹——内容不变不应重建包，原断言 utime 触发指纹变化是错的）
        m1 = wp._manifest_version()
        cf = os.path.join(wp.ROOT, "src", "config.py")
        with open(cf, "r", encoding="utf-8") as f:
            original = f.read()
        try:
            with open(cf, "a", encoding="utf-8") as f:
                f.write("\n# test_version_changes_rebuild touch\n")
            m2 = wp._manifest_version()
            assert m2 != m1, "manifest 指纹应随文件内容变化"
        finally:
            with open(cf, "w", encoding="utf-8") as f:
                f.write(original)
        # 恢复后指纹复原
        m3 = wp._manifest_version()
        assert m3 == m1, "恢复内容后指纹应复原"

    def test_manifest_files_exist(self):
        from src.worker_package import MANIFEST, ROOT
        for item in MANIFEST:
            path = os.path.join(ROOT, item)
            assert os.path.exists(path), f"manifest 项缺失: {item}"

    def test_worker_package_import_closure(self, tmp_path):
        """import 闭包守卫：包解压后必须在干净子进程里可 import（防 MANIFEST 漏文件）。

        背景（2026-10-14 五台 worker 心跳全断事故）：8908cadb1 给 src/config.py 加了
        顶层 ``from src.auth.security_config import ...``，config.py 在 worker 包里但
        src/auth/ 不在 → runtime-agent 启动即 ModuleNotFoundError，systemd 无限重启。
        当时的 test_build_package_contents 只防"多打文件"（服务端泄露），不防"漏打
        文件"（import 失败）——本测试补上后半边。

        实现要点：必须用 subprocess 干净隔离。曾实测：主进程 sys.path 带着仓库路径时，
        src.auth 会从仓库（而非包内）解析成功，产生假阳性（import OK 但 worker 实崩）。
        """
        import subprocess
        from src.worker_package import build_package
        p = build_package()
        with tarfile.open(p) as tf:
            tf.extractall(tmp_path, filter="data")
        pkg_root = os.path.join(str(tmp_path), "grape-worker")
        assert os.path.isfile(os.path.join(pkg_root, "src", "config.py"))

        code = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import src.config\n"           # 启动链第一环（事故死因）
            "import src.runtime.agent\n"    # worker 主模块全量闭包
            "print('IMPORT_CLOSURE_OK')\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", code, pkg_root],
            capture_output=True, text=True, timeout=300,
        )
        assert r.returncode == 0, (
            f"worker 包 import 闭包失败（stdout={r.stdout.strip()[:500]} "
            f"stderr={r.stderr.strip()[-800:]}）——MANIFEST 缺文件或新增了顶层服务端 import"
        )
        assert "IMPORT_CLOSURE_OK" in r.stdout


class TestGrapeCliWheel:
    def test_skip_when_wheel_present(self, monkeypatch, tmp_path):
        """wheels/ 已含 grape-*.whl → 不触发网络/clone/pip wheel（离线安全）。"""
        import src.worker_package as wp
        (tmp_path / "grape-0.1.0-py3-none-any.whl").write_text("dummy", encoding="utf-8")
        called = []
        monkeypatch.setattr(wp, "_git", lambda *a, **k: called.append("git"))
        monkeypatch.setattr(wp.subprocess, "run", lambda *a, **k: called.append("pip"))
        wp._ensure_grape_cli_wheel(str(tmp_path))
        assert called == []  # 已有 wheel：什么都不做

    def test_builds_wheel_when_missing(self, monkeypatch, tmp_path):
        """wheel 缺失 → 触发 clone + pip wheel 写入同一目录。"""
        import src.worker_package as wp
        from types import SimpleNamespace
        events = []

        def _fake_git(args):
            events.append("git:" + args[3])  # src 目录

        def _fake_run(cmd, **kw):
            if cmd[:3] == [sys.executable, "-m", "pip"] and "wheel" in cmd:
                (tmp_path / "grape-1.0.0-py3-none-any.whl").write_text("x", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(wp, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(wp, "_git", _fake_git)
        monkeypatch.setattr(wp.subprocess, "run", _fake_run)
        wp._ensure_grape_cli_wheel(str(tmp_path))
        assert events and (tmp_path / "grape-1.0.0-py3-none-any.whl").exists()


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


# ===== B5 (Issue #257): 版本归一化测试 =====

class TestNormalizeWorkerVersion:
    """B5: worker_package.normalize_worker_version 老版本串兼容。"""

    def test_new_format_unchanged(self):
        from src.worker_package import normalize_worker_version
        assert normalize_worker_version("h-abc123def456") == "h-abc123def456"

    def test_old_format_strips_suffix(self):
        from src.worker_package import normalize_worker_version
        result = normalize_worker_version("h-abc123def4567890-w111abcd-p222abcd")
        assert result == "h-abc123def456"
        assert "-w" not in result
        assert "-p" not in result

    def test_empty_string(self):
        from src.worker_package import normalize_worker_version
        assert normalize_worker_version("") == ""

    def test_old_new_same_code_matches(self):
        """老格式和新格式指向同一代码版本时归一化后相等。"""
        from src.worker_package import normalize_worker_version
        old_format = "h-abc123def4567890-w111-p222"
        new_format = "h-abc123def456"
        assert normalize_worker_version(old_format) == normalize_worker_version(new_format)
