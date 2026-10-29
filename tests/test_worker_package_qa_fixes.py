"""QA 审查修复的失败测试（先红后绿）。

PR #720 QA 审查 2 Required + 2 Suggestion：

R1（缓存）build_package() 重建路径在 _find_pythons() 补齐缺失架构后调用
    package_version()，60s TTL 缓存大概率命中补装前的单架构指纹 → 包内打的是
    双架构、缓存目录 VERSION 戳的是单架构版本，指纹声明不成立。修复：
    重建路径重算版本必须绕过 TTL 缓存（force）。
R2（静默失败）_find_pythons() 的 uv install 无 returncode 检查：uv 非零退出
    （如 --arch 参数不存在）不触发 except，静默失败。修复：returncode != 0 时
    log.warning 附 stderr 尾部。
S1（能力版本）uv python install 的 --arch 参数实际不存在（本机 uv 0.12.3 实测
    `unexpected argument '--arch'`，rc=2）→ 交叉架构安装改用完整请求串
    `cpython-3.12-linux-<arch>-gnu`（uv 官方 Python 请求格式，实测成功安装
    cpython-3.12.13-linux-x86_64-gnu）。同名目录不带 patch 版本也可匹配，
    uv 版本无关。
S2（注释过时）install-worker.sh 第 1 步注释仍写旧格式 python/ 单目录，
    新包为 python-<arch>/ 分目录。
"""
import logging
import os
import re
import subprocess

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SH = os.path.join(ROOT, "scripts", "install-worker.sh")


def _script():
    assert os.path.isfile(SH), "scripts/install-worker.sh should exist"
    with open(SH, encoding="utf-8") as f:
        return f.read()


def _make_python(uv_dir, ver, arch, platform="unknown-linux-gnu", with_patch=True):
    """在假 UV_PYTHON_DIR 下构造 cpython-<ver>-<arch>-<platform> standalone 目录。

    with_patch=False 时用短版本目录名（如 cpython-3.12-linux-aarch64-gnu），
    对应 uv 实际安装产物的两种命名（带/不带 patch 版本）。
    """
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


@pytest.fixture(autouse=True)
def _reset_version_cache():
    """package_version 的 TTL 缓存是模块级状态，逐测试复位防串扰。"""
    import src.worker_package as wp
    monkey = pytest.MonkeyPatch()
    monkey.setattr(wp, "_cached_package_version", "")
    monkey.setattr(wp, "_cached_package_version_ts", 0.0)
    yield
    monkey.undo()


class TestFindPythonsUVInstallFailureAlarm:
    """R2：uv install 非零退出（含 --arch 不存在 rc=2）必须告警，不得静默。"""

    def _setup(self, monkeypatch, tmp_path, exit_code, stderr_text):
        import src.worker_package as wp
        uv_dir = tmp_path / "uv_python"
        uv_dir.mkdir()
        _make_python(uv_dir, "3.12.8", "aarch64")  # 仅本机架构，x86_64 缺失
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))

        fake_uv = os.path.join(str(tmp_path), "uv-fake")
        with open(fake_uv, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nexit %d\n" % exit_code)
        os.chmod(fake_uv, 0o755)

        def _fake_which(name):
            return fake_uv if name == "uv" else None

        def _fake_run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, exit_code, stdout="", stderr=stderr_text)

        monkeypatch.setattr(wp.shutil, "which", _fake_which)
        monkeypatch.setattr(wp.subprocess, "run", _fake_run)
        return wp

    def test_uv_install_nonzero_exit_warns(self, monkeypatch, tmp_path, caplog):
        """uv python install rc!=0 → log.warning（附 stderr 尾部），且不抛异常。"""
        wp = self._setup(monkeypatch, tmp_path, exit_code=2,
                         stderr_text="error: unexpected argument '--arch' found")
        with caplog.at_level(logging.WARNING, logger="devops-bot"):
            found = wp._find_pythons()
        assert "aarch64" in found, "已有架构应保留"
        assert "x86_64" not in found, "补装失败的架构不应出现"
        warns = [r for r in caplog.records
                 if r.levelno == logging.WARNING and "uv" in r.getMessage()
                 and "x86_64" in r.getMessage()]
        assert warns, f"uv 安装失败应产生 WARNING，实得: {[r.getMessage() for r in caplog.records]}"
        assert "unexpected argument" in warns[0].getMessage(), \
            f"告警应附 stderr 尾部便于定位，实得: {warns[0].getMessage()}"

    def test_uv_install_success_no_warning(self, monkeypatch, tmp_path, caplog):
        """uv 安装成功（rc=0）→ 无失败告警（正常路径不打 WARNING）。"""
        wp = self._setup(monkeypatch, tmp_path, exit_code=0, stderr_text="")
        # rc=0 时 fake 不落盘文件，scan 后仍只有 aarch64 —— 只断言无失败告警
        with caplog.at_level(logging.WARNING, logger="devops-bot"):
            wp._find_pythons()
        fails = [r for r in caplog.records
                 if r.levelno == logging.WARNING and "uv python install" in r.getMessage()]
        assert not fails, f"成功路径不应有失败告警: {[r.getMessage() for r in fails]}"


class TestFindPythonsFullRequestString:
    """S1：交叉架构安装用完整请求串 cpython-3.12-linux-<arch>-gnu（uv 官方格式），
    不再用不存在的 --arch 参数（本机 uv 0.12.3 实测 `unexpected argument '--arch'`）。
    """

    def test_uv_install_uses_full_request_string(self, monkeypatch, tmp_path):
        """缺架构补装命令 = `uv python install cpython-3.12-linux-<arch>-gnu`。"""
        import src.worker_package as wp
        uv_dir = tmp_path / "uv_python"
        uv_dir.mkdir()
        _make_python(uv_dir, "3.12.8", "aarch64")
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))
        calls = []

        def _fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[1:3] == ["python", "install"]:
                m = re.match(r"cpython-[\d.]+-linux-(\w+)-gnu", cmd[3])
                if m:
                    # 实测行为：uv 落盘目录名带 patch 版本（3.12 → 3.12.13）
                    _make_python(uv_dir, "3.12.13", m.group(1))
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(wp.shutil, "which", lambda name: "/usr/bin/uv")
        monkeypatch.setattr(wp.subprocess, "run", _fake_run)
        found = wp._find_pythons()
        assert set(found) == {"x86_64", "aarch64"}, f"应补齐双架构: {found}"
        installs = [c for c in calls if c[1:3] == ["python", "install"]]
        assert installs, "应发起 uv python install"
        for c in installs:
            assert re.match(r"cpython-[\d.]+-linux-(x86_64|aarch64)-gnu$", c[3]), \
                f"应用完整请求串指定架构（而非 --arch 参数）: {c}"
            assert "--arch" not in c, f"--arch 参数在 uv 中不存在（实测 rc=2）: {c}"


class TestBuildPackageVersionFreshness:
    """R1：重建路径在架构补齐后，版本重算不得命中 TTL 缓存（指纹须与包内容一致）。"""

    def _isolate(self, monkeypatch, tmp_path):
        import src.worker_package as wp
        monkeypatch.setattr(wp, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(wp, "PKG_PATH", os.path.join(str(tmp_path), "grape-worker.tar.gz"))
        monkeypatch.setattr(wp, "VERSION_PATH", os.path.join(str(tmp_path), "VERSION"))
        monkeypatch.setattr(wp, "WHEELS_DIR", os.path.join(str(tmp_path), "wheels"))
        monkeypatch.setattr(wp, "_pip_download_wheels",
                            lambda dest, platform_args, py_version: 0)
        monkeypatch.setattr(wp, "_ensure_grape_cli_wheel", lambda whl_dir: whl_dir)
        uv_dir = tmp_path / "uv_python"
        uv_dir.mkdir()
        monkeypatch.setattr(wp, "UV_PYTHON_DIR", str(uv_dir))
        monkeypatch.setattr(wp.shutil, "which", lambda name: None)  # 防 uv 补齐干扰
        return wp, uv_dir

    def test_rebuild_version_recomputed_after_uv_install(self, monkeypatch, tmp_path):
        """心跳预热 TTL 缓存（单架构）→ x86_64 落盘 → 重建：VERSION 戳须为双架构指纹。"""
        import src.worker_package as wp
        wp, uv_dir = self._isolate(monkeypatch, tmp_path)
        _make_python(uv_dir, "3.12.8", "aarch64")

        # 心跳高频调用预热 TTL 缓存（此刻只有单架构指纹）——复现审查描述的缓存窗口
        v_single = wp.package_version()

        # TTL 窗口内缺失架构落盘（uv 补装完成的既成事实：目录出现而代码未变）
        _make_python(uv_dir, "3.12.13", "x86_64")

        # 重建触发：缓存目录 VERSION 记录缺失（新缓存目录/升级窗口）→ 进入重建路径
        try:
            os.remove(wp.VERSION_PATH)
        except FileNotFoundError:
            pass

        wp.build_package()
        ver = open(wp.VERSION_PATH, encoding="utf-8").read().strip()
        assert ver != v_single, \
            "重建路径必须重算版本（不得返回 TTL 缓存里的补装前单架构指纹）"
        # 指纹与包内容一致：VERSION 戳的 -p 段 == 当前双架构联合指纹
        py_fp = wp._pythons_fingerprint(wp._scan_pythons())
        assert f"-p{py_fp}" in ver, \
            f"VERSION 戳应与包内 python 内容一致（-p{py_fp} 不在 {ver} 中）"

    def test_cached_rebuild_not_disturbed(self, monkeypatch, tmp_path):
        """缓存命中路径不受 force 影响：架构无变化时二次构建不重建包（mtime 不变）。"""
        import src.worker_package as wp
        wp, uv_dir = self._isolate(monkeypatch, tmp_path)
        _make_python(uv_dir, "3.12.8", "aarch64")
        _make_python(uv_dir, "3.12.8", "x86_64")

        p1 = wp.build_package()
        mtime1 = os.path.getmtime(p1)
        wp.package_version()  # 预热 TTL 缓存（模拟心跳已调用）
        p2 = wp.build_package()
        assert os.path.getmtime(p2) == mtime1, "缓存有效时二次构建不应重建包"


class TestInstallWorkerCommentFreshness:
    """S2：install-worker.sh 第 1 步注释与新包 python-<arch>/ 分目录格式一致。"""

    def test_step1_comment_mentions_per_arch_dir(self):
        content = _script()
        # 第 1 步「准备 Python」的优先级注释应提到 python-<arch>/（新包格式）
        m = re.search(r"===== 1\. 准备 Python[^\n]*\n(.{0,400})", content, re.S)
        assert m, "应存在第 1 步「准备 Python」段"
        step1 = m.group(1)
        assert "python-<arch>" in step1, \
            "第 1 步注释应说明包内解释器为 python-<arch>/ 分目录（新包格式）"
        # 过时表述清理：不得再写「python/ 目录，standalone 免编译」旧格式说明
        assert not re.search(r"包内自带解释器（python/ 目录", step1), \
            "第 1 步注释不应再仅写旧格式 python/ 目录（已过时）"
