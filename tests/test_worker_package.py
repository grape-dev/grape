"""Worker 安装包 + 下发端点测试（见 worker-server-delivery-design.md）。"""
import hashlib, io, os, re, subprocess, sys, tarfile

import pytest



@pytest.fixture(autouse=True)
def _clean_pkg_cache(monkeypatch, tmp_path):
    """隔离包缓存到临时目录，避免污染 data/cache。"""
    import src.worker_package as wp
    monkeypatch.setattr(wp, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(wp, "PKG_PATH", os.path.join(str(tmp_path), "grape-worker.tar.gz"))
    monkeypatch.setattr(wp, "VERSION_PATH", os.path.join(str(tmp_path), "VERSION"))
    # Issue #499: wheels 目录同样隔离到临时目录，且多平台矩阵下载只走 fake
    # （写 dummy wheel），杜绝测试联网 / 触碰仓库 data/cache 真实 wheels。
    wheels_dir = os.path.join(str(tmp_path), "wheels")

    def _fake_download(dest, platform_args, py_version):
        os.makedirs(dest, exist_ok=True)
        n = len([f for f in os.listdir(dest) if f.endswith(".whl")])
        with open(os.path.join(dest, f"dep-{n}-py3-none-any.whl"), "w", encoding="utf-8") as f:
            f.write("dummy")
        with open(os.path.join(dest, "grape-0.0.0-py3-none-any.whl"), "w", encoding="utf-8") as f:
            f.write("dummy")
        return 0

    monkeypatch.setattr(wp, "WHEELS_DIR", wheels_dir)
    monkeypatch.setattr(wp, "_pip_download_wheels", _fake_download)
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
        assert "grape-worker/src/api/merge_gate.py" in names
        assert "grape-worker/src/runtime/worktree_guard.py" in names
        assert "grape-worker/src/agent/executor.py" in names
        assert "grape-worker/requirements-worker.txt" in names
        # 不应泄漏服务端代码 / 配置 / 静态资源（内置 Python stdlib 含 statistics.py 等，跳过）
        for n in names:
            if n.startswith("grape-worker/python"):
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
            "import src.observability.otel\n"  # PR #1077：worker 侧 span/中继依赖（漏打=静默失效）
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


class TestEnsureWheelsMultiPlatform:
    """Issue #499: _ensure_wheels 按平台矩阵预置多平台 wheels（linux x86_64/aarch64 + win_amd64）。"""

    def _fake_downloader(self, tmp_path, calls):
        import src.worker_package as wp

        def _fake(dest, platform_args, py_version):
            calls.append((tuple(platform_args), py_version))
            os.makedirs(dest, exist_ok=True)
            n = len([f for f in os.listdir(dest) if f.endswith(".whl")])
            with open(os.path.join(dest, f"dep-{n}-py3-none-any.whl"), "w", encoding="utf-8") as f:
                f.write("dummy")
            return 0

        return _fake

    def test_downloads_per_platform_matrix(self, monkeypatch, tmp_path):
        """按 WHEEL_PLATFORMS 矩阵逐平台各下载一份（--platform 精确指定）。"""
        import src.worker_package as wp
        wd = tmp_path / "wheels"
        wd.mkdir()
        monkeypatch.setattr(wp, "WHEELS_DIR", str(wd))
        calls = []
        monkeypatch.setattr(wp, "_pip_download_wheels", self._fake_downloader(tmp_path, calls))
        wp._ensure_wheels("3.12")
        plats = [c[0] for c in calls]
        assert ("--platform", "manylinux2014_x86_64") in plats, "缺 linux x86_64 平台 wheels"
        assert ("--platform", "manylinux2014_aarch64") in plats, "缺 linux aarch64 平台 wheels"
        assert ("--platform", "win_amd64") in plats, "缺 win_amd64 平台 wheels"
        assert all(c[1] == "3.12" for c in calls), "py_version 应透传给每次下载"

    def test_cache_marker_short_circuit(self, monkeypatch, tmp_path):
        """marker 命中且有 wheel → 不重复下载。"""
        import src.worker_package as wp
        wd = tmp_path / "wheels"
        wd.mkdir()
        monkeypatch.setattr(wp, "WHEELS_DIR", str(wd))
        calls = []
        monkeypatch.setattr(wp, "_pip_download_wheels", self._fake_downloader(tmp_path, calls))
        wp._ensure_wheels("3.12")
        n = len(calls)
        wp._ensure_wheels("3.12")
        assert len(calls) == n, "marker 命中不应重复下载"
        # marker 内容 = 平台矩阵键 + py_version + requirements 哈希
        marker = (wd / ".req-hash").read_text(encoding="utf-8").strip()
        assert marker == wp._wheels_cache_key("3.12")

    def test_single_platform_failure_nonfatal(self, monkeypatch, tmp_path):
        """单平台下载失败不阻断其余平台（安装端在线兜底）。"""
        import src.worker_package as wp
        wd = tmp_path / "wheels"
        wd.mkdir()
        monkeypatch.setattr(wp, "WHEELS_DIR", str(wd))
        calls = []

        def _fake(dest, platform_args, py_version):
            calls.append(tuple(platform_args))
            if "--platform" in platform_args and "win_amd64" in platform_args:
                return 1  # win_amd64 失败
            os.makedirs(dest, exist_ok=True)
            n = len([f for f in os.listdir(dest) if f.endswith(".whl")])
            with open(os.path.join(dest, f"dep-{n}-py3-none-any.whl"), "w", encoding="utf-8") as f:
                f.write("dummy")
            return 0

        monkeypatch.setattr(wp, "_pip_download_wheels", _fake)
        wp._ensure_wheels("3.12")  # 不抛异常
        assert len(calls) == 3, "失败平台也应继续处理后续平台"
        assert (wd / ".req-hash").exists(), "失败后 marker 仍应写入（避免每次重试全量）"

    def test_cache_key_contains_platform_matrix(self):
        """缓存键包含平台矩阵——矩阵变更（增删平台）触发重新下载。"""
        import src.worker_package as wp
        key = wp._wheels_cache_key("3.12")
        assert "manylinux2014_x86_64" in key
        assert "manylinux2014_aarch64" in key
        assert "win_amd64" in key


class TestGrapeCliWheel:
    def test_other_wheels_untouched_during_grape_build(self, monkeypatch, tmp_path):
        """#896 前置清理由只删 grape-*.whl——其他依赖 wheel（click 等）不动。"""
        import src.worker_package as wp
        from types import SimpleNamespace

        click_whl = tmp_path / "click-8.1.7-py3-none-any.whl"
        click_whl.write_text("click-bytes", encoding="utf-8")

        def _fake_run(cmd, **kw):
            if cmd[:3] == [sys.executable, "-m", "pip"] and "wheel" in cmd:
                (tmp_path / "grape-1.0.0-py3-none-any.whl").write_text("g", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(wp, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(wp, "_git", lambda *a, **k: None)
        monkeypatch.setattr(wp.subprocess, "run", _fake_run)
        wp._ensure_grape_cli_wheel(str(tmp_path))
        assert click_whl.read_text(encoding="utf-8") == "click-bytes"

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

    def test_stale_wheel_is_replaced_by_fresh_build(self, monkeypatch, tmp_path):
        """陈旧 wheel（内容与远端不一致，如 0.3.3-48a08d1 缺 GRAPE_AUTH_FILE）必须被重建替换。

        #896：缓存的 grape wheel 一旦过期就永远过期——`pip wheel` 只在同一
        文件名已存在时报错，`pip install` 对同版本号也不重新解压。导致某次
        服务端升级包（已含令牌穿透运行时）里带的仍是缺
        GRAPE_AUTH_FILE 读文件逻辑的旧 CLI：dsh 任务拿到 auth 文件路径，
        CLI 却不读 → 穿透失效，且永不自愈。
        修法：构建前先清掉旧 grape-*.whl，保证 wheels 里永远是当前 HEAD 的
        grape-cli main 快照。
        """
        import src.worker_package as wp
        from types import SimpleNamespace

        stale = tmp_path / "grape-0.3.3-py3-none-any.whl"
        stale.write_text("stale-bytes", encoding="utf-8")

        def _fake_run(cmd, **kw):
            if cmd[:3] == [sys.executable, "-m", "pip"] and "wheel" in cmd:
                stale.write_text("fresh-bytes", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(wp, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(wp, "_git", lambda *a, **k: None)
        monkeypatch.setattr(wp.subprocess, "run", _fake_run)
        wp._ensure_grape_cli_wheel(str(tmp_path))
        assert stale.read_text(encoding="utf-8") == "fresh-bytes", \
            "旧 grape wheel 应在重建前被清除，不允许陈旧产物存活"

    def test_stale_wheel_removal_failure_logs_warning_and_continues(self, monkeypatch, tmp_path, caplog):
        """删除旧 grape wheel 失败 → 记录 warning 且不中断构建（#896 审查意见）。"""
        import logging

        import src.worker_package as wp
        from types import SimpleNamespace

        stale = tmp_path / "grape-0.3.3-py3-none-any.whl"
        stale.write_text("stale-bytes", encoding="utf-8")

        def _fake_run(cmd, **kw):
            if cmd[:3] == [sys.executable, "-m", "pip"] and "wheel" in cmd:
                stale.write_text("fresh-bytes", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(wp, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(wp, "_git", lambda *a, **k: None)
        monkeypatch.setattr(wp.subprocess, "run", _fake_run)
        monkeypatch.setattr(wp.os, "remove", lambda p: (_ for _ in ()).throw(OSError("busy")))
        with caplog.at_level(logging.WARNING, logger=wp.log.name):
            wp._ensure_grape_cli_wheel(str(tmp_path))
        assert any("failed to remove stale grape wheel" in r.message for r in caplog.records), \
            "删除失败必须 log.warning，不允许静默吞掉"


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


class TestFindPythonsByArch:
    """Issue #515: _find_pythons/_scan_pythons 按架构扫描 standalone Python（双架构支持）。"""

    def _make_python(self, uv_dir, ver, arch, platform="unknown-linux-gnu"):
        """在假 UV_PYTHON_DIR 下构造 cpython-<ver>-<arch>-<platform> standalone 目录。"""
        d = os.path.join(str(uv_dir), f"cpython-{ver}-{arch}-{platform}")
        lib = os.path.join(d, "lib", f"python3.{ver.split('.')[1]}")
        os.makedirs(os.path.join(d, "bin"), exist_ok=True)
        os.makedirs(lib, exist_ok=True)
        py = os.path.join(d, "bin", "python3")
        with open(py, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(py, 0o755)
        with open(os.path.join(lib, "os.py"), "w", encoding="utf-8") as f:
            f.write("")
        return d

    def test_scan_pythons_parses_both_naming_formats(self, monkeypatch, tmp_path):
        """兼容 cpython-<ver>-<arch>-unknown-linux-gnu（新）与 cpython-<ver>-linux-<arch>-gnu（旧）。"""
        import src.worker_package as wp
        uv_dir = tmp_path / "uv_python"
        uv_dir.mkdir()
        self._make_python(uv_dir, "3.12.8", "x86_64", "unknown-linux-gnu")
        # 旧式命名：cpython-<ver>-linux-<arch>-gnu（本机 3.11.15 实测格式）
        # 旧式命名手工构造：cpython-3.11.15-linux-aarch64-gnu
        old = os.path.join(str(uv_dir), "cpython-3.11.15-linux-aarch64-gnu")
        os.makedirs(os.path.join(old, "bin"), exist_ok=True)
        os.makedirs(os.path.join(old, "lib", "python3.11"), exist_ok=True)
        with open(os.path.join(old, "bin", "python3"), "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(os.path.join(old, "bin", "python3"), 0o755)
        with open(os.path.join(old, "lib", "python3.11", "os.py"), "w", encoding="utf-8") as f:
            f.write("")
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))
        found = wp._scan_pythons()
        assert "x86_64" in found, f"应识别新式目录中的 x86_64: {found}"
        assert found["x86_64"].endswith("cpython-3.12.8-x86_64-unknown-linux-gnu")
        assert "aarch64" in found, f"应识别旧式目录中的 aarch64: {found}"
        assert found["aarch64"].endswith("cpython-3.11.15-linux-aarch64-gnu")

    def test_scan_pythons_takes_highest_version_per_arch(self, monkeypatch, tmp_path):
        """同架构多版本 → 版本最高者优先。"""
        import src.worker_package as wp
        uv_dir = tmp_path / "uv_python"
        uv_dir.mkdir()
        self._make_python(uv_dir, "3.11.9", "aarch64", "unknown-linux-gnu")
        self._make_python(uv_dir, "3.12.8", "aarch64", "unknown-linux-gnu")
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))
        found = wp._scan_pythons()
        assert "aarch64" in found
        assert found["aarch64"].endswith("cpython-3.12.8-aarch64-unknown-linux-gnu")

    def test_scan_pythons_ignores_non_linux_and_missing_bin(self, monkeypatch, tmp_path):
        """非 linux（mac 等）standalone 与缺 bin/python3 的目录不进打包候选。"""
        import src.worker_package as wp
        uv_dir = tmp_path / "uv_python"
        uv_dir.mkdir()
        self._make_python(uv_dir, "3.12.8", "aarch64", "apple-darwin")  # mac 不入包
        self._make_python(uv_dir, "3.12.8", "x86_64", "unknown-linux-gnu")
        # 缺 bin/python3 的目录
        broken = os.path.join(str(uv_dir), "cpython-3.13.0-x86_64-unknown-linux-gnu")
        os.makedirs(os.path.join(broken, "lib", "python3.13"), exist_ok=True)
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))
        found = wp._scan_pythons()
        assert "x86_64" in found
        assert "aarch64" not in found, "非 linux standalone 不应进入打包候选"

    def test_find_pythons_installs_missing_arch_via_uv(self, monkeypatch, tmp_path):
        """缺失架构时用 uv python install 完整请求串补齐（双架构完整返回）。

        QA S1 修正：uv python install 无 --arch 参数（本机 uv 0.12.3 实测
        `unexpected argument '--arch'` rc=2），交叉架构安装改用官方请求格式
        cpython-3.12-linux-<arch>-gnu（实测成功安装 cpython-3.12.13-linux-x86_64-gnu）。
        """
        import src.worker_package as wp
        uv_dir = tmp_path / "uv_python"
        uv_dir.mkdir()
        self._make_python(uv_dir, "3.12.8", "aarch64", "unknown-linux-gnu")
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))
        calls = []
        fake_uv = os.path.join(str(tmp_path), "uv-fake")
        with open(fake_uv, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(fake_uv, 0o755)

        def _fake_which(name):
            return fake_uv if name == "uv" else None

        def _fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == fake_uv and cmd[1:3] == ["python", "install"]:
                # 实现以完整请求串 cpython-3.12-linux-<arch>-gnu 显式指定缺失架构；
                # uv 实际落盘目录带 patch 版本（3.12 → 3.12.13）
                m = re.match(r"cpython-([\d.]+)-linux-(\w+)-gnu", cmd[3])
                if m:
                    ver = m.group(1)
                    patch = ver if ver.count(".") == 2 else ver + ".13"
                    self._make_python(uv_dir, patch, m.group(2), "unknown-linux-gnu")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(wp.shutil, "which", _fake_which)
        monkeypatch.setattr(wp.subprocess, "run", _fake_run)
        found = wp._find_pythons()
        assert "aarch64" in found and "x86_64" in found, f"应补齐缺失架构: {found}"
        assert any(
            c[0] == fake_uv and c[1:3] == ["python", "install"]
            and re.match(r"cpython-[\d.]+-linux-x86_64-gnu$", c[3])
            for c in calls
        ), f"uv 应用完整请求串指定 x86_64 架构: {calls}"

    def test_find_pythons_none_returns_empty(self, monkeypatch, tmp_path):
        """找不到任何 python（无 uv/安装失败）→ 返回空 dict，不抛异常。"""
        import src.worker_package as wp
        uv_dir = tmp_path / "empty_uv"
        uv_dir.mkdir()
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))
        monkeypatch.setattr(wp.shutil, "which", lambda name: None)
        assert wp._find_pythons() == {}


class TestBundlePythonByArch:
    """Issue #515: build_package 按架构分目录捆绑 python-x86_64/ python-aarch64/。"""

    def _make_python(self, uv_dir, ver, arch, platform="unknown-linux-gnu"):
        d = os.path.join(str(uv_dir), f"cpython-{ver}-{arch}-{platform}")
        lib = os.path.join(d, "lib", f"python3.{ver.split('.')[1]}")
        os.makedirs(os.path.join(d, "bin"), exist_ok=True)
        os.makedirs(lib, exist_ok=True)
        py = os.path.join(d, "bin", "python3")
        with open(py, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(py, 0o755)
        with open(os.path.join(lib, "os.py"), "w", encoding="utf-8") as f:
            f.write("")
        return d

    def test_build_package_bundles_per_arch_dirs(self, monkeypatch, tmp_path):
        """假双架构目录 → tar 内出现 python-x86_64/ 与 python-aarch64/，且无 python/ 单目录。"""
        import src.worker_package as wp
        uv_dir = tmp_path / "uv_python"
        uv_dir.mkdir()
        self._make_python(uv_dir, "3.12.8", "x86_64", "unknown-linux-gnu")
        self._make_python(uv_dir, "3.11.15", "aarch64", "unknown-linux-gnu")
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))
        monkeypatch.setattr(wp.shutil, "which", lambda name: None)  # 防 uv 补齐尝试
        p = wp.build_package()
        tf = tarfile.open(p)
        names = tf.getnames()
        tf.close()
        assert any(n.startswith("grape-worker/python-x86_64/") for n in names), "缺 python-x86_64/ 目录"
        assert any(n.startswith("grape-worker/python-aarch64/") for n in names), "缺 python-aarch64/ 目录"
        assert any(n.endswith("python-x86_64/bin/python3") for n in names), "x86_64 解释器可执行文件缺失"
        assert not any(n.startswith("grape-worker/python/") for n in names), "不应再有 python/ 单目录"

    def test_python_fingerprint_includes_all_arches(self, monkeypatch, tmp_path):
        """package_version 的 -p 指纹随任一架构目录内容变化而变化。"""
        import src.worker_package as wp
        uv_dir = tmp_path / "uv_python"
        uv_dir.mkdir()
        self._make_python(uv_dir, "3.12.8", "x86_64", "unknown-linux-gnu")
        self._make_python(uv_dir, "3.11.15", "aarch64", "unknown-linux-gnu")
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))
        monkeypatch.setattr(wp.shutil, "which", lambda name: None)
        found1 = wp._find_pythons()
        f1 = wp._pythons_fingerprint(found1)
        # 改动 aarch64 目录里的 os.py → 指纹变化
        aarch = found1["aarch64"]
        with open(os.path.join(aarch, "lib", "python3.11", "os.py"), "a", encoding="utf-8") as f:
            f.write("# touch\n")
        f2 = wp._pythons_fingerprint(wp._find_pythons())
        assert f1 != f2, "任一架构 python 变化应触发指纹变化"


def test_manifest_covers_worktree_guard_deps():
    """Issue #752：worktree_guard 随 src/runtime/ 整目录进 worker 包并在 worker 内执行，
    其 `from src.api.<mod> import ...` 依赖必须一并入包。

    历史教训：MANIFEST 漏列 time_utils 导致 worker 启动即崩（2026-08-17）；漏列
    merge_gate 会让守卫的 fallback 扫描（unmerged 路径 / 旧 git）静默失效——异常被
    except 吞掉后返回空结果，守卫形同虚设。
    """
    import pathlib
    import re

    from src.worker_package import MANIFEST

    txt = pathlib.Path("src/runtime/worktree_guard.py").read_text(encoding="utf-8")
    deps = set(re.findall(r"from\s+src\.api\.([a-zA-Z0-9_]+)\s+import", txt))
    assert deps, "worktree_guard 应至少懒加载一个 src.api 依赖（否则本守卫失效）"
    missing = [d for d in sorted(deps) if f"src/api/{d}.py" not in MANIFEST]
    assert not missing, f"worker MANIFEST missing worktree_guard deps: {missing}"


class TestManifestCodegraphInstaller:
    """Issue #666 (M4)：worker 包随包下发 codegraph 离线安装脚本。"""

    def test_manifest_contains_install_codegraph(self):
        from src.worker_package import MANIFEST, ROOT
        assert "scripts/install-codegraph.sh" in MANIFEST
        assert os.path.isfile(os.path.join(ROOT, "scripts", "install-codegraph.sh"))
