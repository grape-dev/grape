"""Issue #2923：worker 部署闭包与自检覆盖 vcs / lock，提前拦截坏包。"""
import os
import re
import site
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "scripts", "install-worker.sh")


def _read_script():
    with open(SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def _extract_smoke_modules():
    """从 install-worker.sh 的冒烟自检 heredoc 中解析真实执行的模块清单。"""
    body = _read_script()
    start = body.index("modules = (")
    end = body.index(")", start)
    return re.findall(r'"([A-Za-z_][\w.]*)"', body[start:end])


def test_worker_manifest_covers_vcs_and_lock():
    """打包清单必须包含 branch guard PR 查询所需的 vcs 目录与 lock 别名入口。"""
    from src.kernel.worker_package import MANIFEST
    assert "src/domains/runtime/vcs/" in MANIFEST
    assert "src/kernel/" in MANIFEST, "lock.py canonical 实现所在目录"
    assert "src/__init__.py" in MANIFEST, "src.lock 历史别名入口"


def test_install_worker_smoke_check_covers_deploy_closure():
    """部署自检必须覆盖 vcs / branch guard / lock，且不得依赖服务端专属模块。"""
    modules = _extract_smoke_modules()
    assert "src.domains.runtime.vcs.api_client" in modules
    assert "src.domains.runtime.engine.branch_guard" in modules
    assert "src.kernel.lock" in modules
    assert "src.kernel.otel" in modules, "worker 埋点应走零第三方依赖的 canonical 入口"
    assert not any(m.startswith("src.domains.insight") for m in modules), (
        "自检不得 import 服务端专属模块（其父包会触发 prometheus_client 等 "
        "worker 未安装依赖，导致安装/升级被误拒）")
    assert "worker_code_version" in _read_script()


def test_manifest_version_is_path_independent(tmp_path, monkeypatch):
    """同一份代码在不同安装根目录下指纹必须一致（QA blocker 回归）。

    构建期 ROOT 与安装期 $INSTALL_DIR 不同；旧实现把绝对路径计入哈希，
    导致 `_version_consistency_check` 恒不相等、每次安装误报 stale。
    """
    import src.kernel.worker_package as wp

    def make_tree(root):
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
        (root / "src" / "b.py").write_text("B = 2\n", encoding="utf-8")

    build_root = tmp_path / "build-root"
    install_root = tmp_path / "opt" / "grape"
    make_tree(build_root)
    make_tree(install_root)

    monkeypatch.setattr(wp, "MANIFEST", ["src/a.py", "src/b.py"])

    monkeypatch.setattr(wp, "ROOT", str(build_root))
    build_version = wp._manifest_version()

    monkeypatch.setattr(wp, "ROOT", str(install_root))
    install_version = wp._manifest_version()

    assert build_version == install_version, (
        "同内容不同安装路径指纹必须一致，否则安装期版本一致性校验恒误报")

    # 内容变化仍必须改变指纹（防止改成纯路径哈希后失去漂移检测能力）。
    (install_root / "src" / "a.py").write_text("A = 3\n", encoding="utf-8")
    assert wp._manifest_version() != install_version


def _extract_function(name):
    """从 install-worker.sh 中提取单个 shell 函数定义（供真实执行）。"""
    with open(SCRIPT, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    marker = f"{name}() {{"
    try:
        start = lines.index(marker)
    except ValueError as exc:  # pragma: no cover - 脚本结构变化时快速失败
        raise AssertionError(f"未找到函数 {name}") from exc
    for end in range(start + 1, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[start:end + 1])
    raise AssertionError(f"函数 {name} 缺少闭合花括号")


@pytest.fixture(scope="module")
def clean_worker_venv(tmp_path_factory):
    """模拟 worker 运行环境：worker 依赖可用，但服务端专属 prometheus_client 缺失。

    New base 后 ``src.domains.runtime.engine.agent`` 顶层依赖 worker 依赖 ``requests``
    等，纯标准库 venv 已无法 import worker 入口；feature 意图是自检不得依赖服务端
    专属包（prometheus_client）。因此复用当前解释器的依赖并显式屏蔽 prometheus_client。

    自足性：不使用 ``--system-site-packages``——当 pytest 跑在项目 venv 内时，嵌套
    venv 的 system site-packages 会解析到**基础解释器**（可能没有 worker 依赖），
    导致用例随宿主环境漂移。改为把**当前解释器**的 site-packages 通过
    sitecustomize 显式写入 sys.path（排除仓库根目录，避免本仓库 ``src`` 泄入，
    破坏缺模块用例），保证任意宿主环境下依赖来源确定。
    """
    dep_dirs = []
    for path in list(sys.path) + [site.getusersitepackages()]:
        if (os.path.basename(str(path)) == "site-packages"
                and os.path.isdir(path) and not str(path).startswith(ROOT)
                and path not in dep_dirs):
            dep_dirs.append(str(path))
    venv = tmp_path_factory.mktemp("worker-venv") / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True, capture_output=True, text=True, timeout=120)
    site_dir = next((venv / "lib").glob("python*/site-packages"))
    (site_dir / "sitecustomize.py").write_text(
        "import sys\n"
        + "".join(f"sys.path.append({d!r})\n" for d in dep_dirs)
        + "\n"
        "class _BlockServerOnly:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'prometheus_client' or "
        "fullname.startswith('prometheus_client.'):\n"
        "            raise ModuleNotFoundError(\"No module named 'prometheus_client'\")\n"
        "        return None\n"
        "\n"
        "sys.meta_path.insert(0, _BlockServerOnly())\n",
        encoding="utf-8")
    return venv


def _run_smoke_check(tmp_path, venv, install_dir):
    """从 install-worker.sh 提取 `_smoke_check`，用真实 venv 执行 import 自检。"""
    harness = (
        'log() { echo "[install-worker] $*"; }\n'
        f"VENV='{venv}'\n"
        f"INSTALL_DIR='{install_dir}'\n"
        f"{_extract_function('_smoke_check')}\n"
        f"{_extract_function('_version_consistency_check')}\n"
        "_smoke_check\n"
        "exit $?\n"
    )
    env = dict(os.environ)
    env["DEVOPS_DB_PATH"] = str(tmp_path / "sessions.db")
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return subprocess.run(
        ["bash", "-c", harness], cwd=str(tmp_path), env=env,
        capture_output=True, text=True, timeout=180)


def _run_version_consistency_check(tmp_path, version_content):
    """构造临时安装目录，真实执行 `_version_consistency_check`。"""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    os.symlink(sys.executable, venv_bin / "python")

    install_dir = tmp_path / "install"
    pkg_dir = install_dir / "src" / "kernel"
    pkg_dir.mkdir(parents=True)
    (install_dir / "src" / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "worker_package.py").write_text(
        "def worker_code_version():\n"
        "    return 'h-abc123456789'\n",
        encoding="utf-8")
    if version_content is not None:
        (install_dir / "VERSION").write_text(version_content, encoding="utf-8")

    body = _extract_function("_version_consistency_check")
    harness = (
        "log() { echo \"[install-worker] $*\"; }\n"
        f"VENV='{tmp_path / 'venv'}'\n"
        f"INSTALL_DIR='{install_dir}'\n"
        f"{body}\n"
        "_version_consistency_check\n"
    )
    return subprocess.run(
        ["bash", "-c", harness], check=False,
        capture_output=True, text=True, timeout=60)


def test_version_consistency_check_matches_same_content(tmp_path):
    """包内 VERSION 与运行时指纹一致 → 输出 match，不得误报 stale。"""
    proc = _run_version_consistency_check(tmp_path, "h-abc123456789")
    assert proc.returncode == 0, proc.stderr
    assert "version match" in proc.stdout
    assert "may be stale" not in proc.stdout


def test_version_consistency_check_warns_on_drift(tmp_path):
    """包内 VERSION 与运行时指纹不一致 → 告警 stale。"""
    proc = _run_version_consistency_check(tmp_path, "h-deadbeef0000")
    assert proc.returncode == 0, proc.stderr
    assert "may be stale" in proc.stdout
    assert "h-deadbeef0000" in proc.stdout
    assert "h-abc123456789" in proc.stdout


def test_version_consistency_check_skips_without_version(tmp_path):
    """VERSION 为空（版本不可得）时跳过校验而非误报告警。"""
    proc = _run_version_consistency_check(tmp_path, "")
    assert proc.returncode == 0, proc.stderr
    assert "skipped" in proc.stdout
    assert "may be stale" not in proc.stdout


def test_smoke_check_runs_after_venv_and_deps_install():
    """全新安装也必须执行自检：调用点必须在建 venv + 装依赖之后（AC4）。"""
    with open(SCRIPT, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    call_lines = [i for i, line in enumerate(lines)
                  if line.startswith("_smoke_check || fail")]
    assert len(call_lines) == 1, "自检应只有一个调用点"
    venv_line = next(i for i, line in enumerate(lines)
                     if 'log "Creating venv' in line)
    deps_line = next(i for i, line in enumerate(lines)
                     if "WORKER_DEPS=" in line)
    assert call_lines[0] > venv_line, "自检必须晚于建 venv（覆盖全新安装）"
    assert call_lines[0] > deps_line, "自检必须晚于依赖安装"


def test_smoke_check_passes_in_clean_worker_venv(tmp_path, clean_worker_venv):
    """真实执行自检：worker 运行环境（无服务端专属 prometheus_client）下必须通过。

    回归 QA blocker：自检曾 import 转发壳
    `src.domains.insight.observability.otel`，其父包无条件注册 GSB 指标并
    import `prometheus_client`（worker 不装），该环境下必然失败 → 安装/升级被拒。
    """
    proc = _run_smoke_check(tmp_path, clean_worker_venv, ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "smoke check passed" in proc.stdout
    assert "smoke check failed" not in proc.stdout


def test_smoke_check_rejects_package_missing_modules(tmp_path, clean_worker_venv):
    """真实执行自检：安装目录缺 vcs 等模块时必须失败并列出缺失项。"""
    install_dir = tmp_path / "install"
    pkg_dir = install_dir / "src"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    proc = _run_smoke_check(tmp_path, clean_worker_venv, install_dir)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "smoke check failed" in proc.stdout
    assert "src.domains.runtime.vcs.api_client" in proc.stdout
