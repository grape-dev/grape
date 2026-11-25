"""Issue #2292：pre-push 守卫 canonical lock 导入 + fail-open 修复指引 + 冒烟自检回归。

背景（用户反馈 5479/5492/5493/5508）：worker 部署树不再保证存在顶层
``src/lock.py``（#2010 之后 canonical 实现在 ``src/kernel/lock.py``），
``branch_guard`` 租约路径的 ``from src import lock`` 抛 ImportError，
pre-push 钩子非零退出阻断**全部** ``grape git push``——每个 coding 回合只能
``--no-verify`` 手工绕行，分支租约/漂移校验全部丢失。

与 #2147（PR #1275，同根因）同修法，本用例固化：
1. 租约三处导入走 canonical ``src.kernel.lock``，源码中旧顶层导入绝迹；
2. 钩子自检依赖 ``HOOK_DEP_MODULES`` 与 canonical 路径对齐（部署树不再恒 fail-open）；
3. 守卫自身异常（含 lock 模块缺失）fail-open：stderr 有 ``[branch-guard] WARNING``
   + 可执行修复指引，push 不被阻断；真实违规（租约冲突/PR 已合并）仍 return 1；
4. 钩子模板版本指纹随模板变化递增，存量 worktree 钩子自愈升级（#2028 教训）；
5. ``scripts/install-worker.sh`` 冒烟自检覆盖 ``src.kernel.lock``（含旧别名
   ``src.lock``）与新布局 ``branch_guard``，坏包直接拒绝升级并指认缺失模块。
"""

import io
import os
import re
import shlex
import subprocess
import sys

import pytest

from src.domains.runtime.engine import branch_guard as bg

REPO_KEY = "gitcode:owner/repo"
BRANCH = "feat/issue-2292"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_INSTALL_SCRIPT = os.path.join(REPO_ROOT, "scripts", "install-worker.sh")


def _clear_guard_state():
    from src.session_manager import db

    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.execute("DELETE FROM branch_guard_state")
        db.commit()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_guard_state():
    _clear_guard_state()
    yield
    _clear_guard_state()


def _ref_line(branch=BRANCH, local="local-sha", remote="old-sha"):
    return f"refs/heads/{branch} {local} refs/heads/{branch} {remote}\n"


# ===================== 1. canonical 导入（AC1） =====================

def test_kernel_lock_importable_on_canonical_layout():
    """新布局树上 ``from src.kernel import lock`` 可用（#2292 修复目标）。"""
    from src.kernel import lock

    assert hasattr(lock, "acquire_lock") and hasattr(lock, "renew_lock")
    assert hasattr(lock, "release_lock") and hasattr(lock, "_holder")


def test_branch_guard_source_has_no_legacy_top_level_lock_import():
    """旧导入 ``from src import lock`` 绝迹：部署树不再保证顶层 src/lock.py 存在。"""
    with open(os.path.abspath(bg.__file__), encoding="utf-8") as f:
        source = f.read()
    assert "from src import lock" not in source
    assert "from src.kernel import lock" in source


def test_hook_dep_modules_point_to_canonical_layout():
    """钩子自检依赖必须直接校验新布局导入路径，否则部署树守卫恒 fail-open。"""
    assert "src.kernel.lock" in bg.HOOK_DEP_MODULES
    assert "src.lock" not in bg.HOOK_DEP_MODULES
    assert bg._missing_hook_deps() == []


def test_lease_functions_run_without_import_error():
    """三处租约函数在新布局树上不再抛 ImportError（线上复现点）。"""
    lease = bg.acquire_branch_lease(REPO_KEY, BRANCH, expected_remote_sha="sha-1")
    assert lease["ok"] is True
    holder = bg._lease_holder("t_task")
    assert holder is not None and holder.startswith("t_task:")
    bg.release_branch_lease(REPO_KEY, BRANCH)


# ===================== 2. 钩子可加载 + 模板版本（AC2/AC5） =====================

def test_installed_hook_pins_canonical_module_and_version_line(tmp_path):
    """生成的 pre-push 固化仓库相对模块路径，且版本指纹可被存量钩子识别升级。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    hook = (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
    # Issue #3579：只写仓库相对路径，不写安装时 worktree 绝对路径。
    assert f"# hook-module-rel: {bg._hook_module_relpath()}" in hook
    assert bg._hook_module_path() not in hook
    assert bg.HOOK_VERSION_LINE in hook
    assert "src/runtime/branch_guard.py" not in hook, "旧硬编码布局路径绝迹"
    # 以钩子内联代码真实执行一次：模块加载成功 + 空 stdin 快路径 rc=0，无告警
    code = bg._hook_entry_code(bg._hook_root(), bg._hook_module_path())
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY],
                       input="", text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert "fail-open" not in r.stderr


def test_hook_template_version_is_bumped_past_2028_template():
    """#2292 变更了钩子模板告警文案：版本指纹必须 > 2028-04，否则存量钩子不升级。"""
    assert bg.HOOK_TEMPLATE_VERSION != "2028-04"
    year, _, seq = bg.HOOK_TEMPLATE_VERSION.partition("-")
    assert (year, seq) > ("2028", "04"), bg.HOOK_TEMPLATE_VERSION


def test_existing_hook_usable_accepts_freshly_installed_hook(tmp_path):
    """自愈校验路径可达：版本命中 + 元数据可用才幂等跳过（否则重写升级）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    hook = (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
    assert bg._existing_hook_usable(hook, REPO_KEY) is True
    assert bg._existing_hook_usable(hook, "gitcode:other/repo") is False


# ===================== 3. fail-open / fail-closed 边界（AC3） =====================

def test_hook_entry_code_fail_open_with_repair_hint_when_lock_missing(tmp_path):
    """模拟 lock 模块缺失的守卫：钩子进程必须 exit 0 且告警含可执行修复指引。"""
    fake = tmp_path / "fake_guard.py"
    fake.write_text(
        "raise ImportError(\"cannot import name 'lock' from 'src'\")\n",
        encoding="utf-8",
    )
    code = bg._hook_entry_code(str(tmp_path), str(fake))
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY],
                       input="", text=True, capture_output=True)
    assert r.returncode == 0, r.stderr          # push 不被阻断
    assert "[branch-guard] WARNING" in r.stderr
    assert "fail-open" in r.stderr
    # Issue #2923：只给可读中文结论，原始 ImportError 文案不得回流 stderr。
    assert "守卫自身异常" in r.stderr
    assert "cannot import name 'lock'" not in r.stderr
    assert "ImportError" not in r.stderr
    # 修复指引：指名重装/升级 worker 包，而不是只给裸 traceback
    assert "install-worker" in r.stderr or "升级" in r.stderr
    assert "Traceback" not in r.stderr           # 不给裸栈


def test_hook_main_fail_open_on_kernel_lock_import_error(monkeypatch, capsys):
    """线上形态：新布局 lock 导入失败不得阻断 push——告警 + return 0。"""

    def _boom(*a, **k):
        raise ModuleNotFoundError("No module named 'src.kernel.lock'",
                                  name="src.kernel.lock")

    monkeypatch.setattr(bg, "acquire_branch_lease", _boom)
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 0
    err = capsys.readouterr().err
    assert "[branch-guard] WARNING" in err and "fail-open" in err
    # Issue #2923：只给可读中文结论，不回传模块名/异常原文。
    assert "分支守卫运行异常" in err
    assert "src.kernel.lock" not in err


def test_hook_main_missing_dependency_reports_repair_hint(monkeypatch, capsys):
    """依赖自检命中：告警同时给出可执行修复动作（重装/升级 worker 包）。"""
    real_import = bg.importlib.import_module

    def _fake_import(name, *a, **k):
        if name == "src.kernel.lock":
            raise ImportError("No module named 'src.kernel.lock'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(bg.importlib, "import_module", _fake_import)
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 0
    err = capsys.readouterr().err
    # Issue #2923：只给可读中文结论，不回传模块名/ImportError 原文。
    assert "分支守卫依赖缺失" in err and "fail-open" in err
    assert "src.kernel.lock" not in err
    assert "install-worker" in err or "升级" in err


def test_hook_entry_code_fail_open_on_tree_missing_kernel_lock(tmp_path):
    """端到端模拟「部署树缺 src/kernel/lock.py」：钩子进程 exit 0 + 可照做的告警。

    这贴近用户反馈 5479 的真实形态：钩子按文件路径加载真实 branch_guard，但安装树
    里没有 lock 模块。push 不得被阻断，且 stderr 必须能直接指向修复动作。
    """
    tree = tmp_path / "tree"
    engine = tree / "src" / "domains" / "runtime" / "engine"
    engine.mkdir(parents=True)
    for rel in ("", "domains", "domains/runtime", "domains/runtime/engine"):
        (tree / "src" / rel / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
        (tree / "src" / rel / "__init__.py").write_text("", encoding="utf-8")
    # 坏包：只有 src/kernel/__init__.py，没有 lock.py
    (tree / "src" / "kernel").mkdir(parents=True, exist_ok=True)
    (tree / "src" / "kernel" / "__init__.py").write_text("", encoding="utf-8")
    with open(os.path.abspath(bg.__file__), encoding="utf-8") as f:
        (engine / "branch_guard.py").write_text(f.read(), encoding="utf-8")

    code = bg._hook_entry_code(str(tree), str(engine / "branch_guard.py"))
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY],
                       input=_ref_line(), text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert "[branch-guard] WARNING" in r.stderr
    assert "fail-open" in r.stderr
    # Issue #2923：告警只给可读中文结论，原始模块名/异常原文不得回流。
    assert "分支守卫依赖缺失" in r.stderr
    assert "src.kernel.lock" not in r.stderr
    assert "install-worker" in r.stderr or "升级" in r.stderr   # 可执行修复指引


def test_hook_main_still_blocks_on_lease_conflict(monkeypatch, capsys):
    """fail-open 只兜守卫自身异常：租约冲突等正常阻断仍 return 1。"""
    monkeypatch.setattr(
        bg, "acquire_branch_lease",
        lambda *a, **k: {"ok": False, "reason": "branch_locked",
                         "message": "分支被他人占用"},
    )
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 1
    err = capsys.readouterr().err
    assert "[branch-guard]" in err and "分支被他人占用" in err


def test_hook_main_still_blocks_when_pr_already_merged(monkeypatch, capsys):
    """真实违规不因 fail-open 兜底而放行。"""
    monkeypatch.setattr(bg, "acquire_branch_lease", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "merged", "number": 42,
                                         "base": "arch-weekly-11-zj"})
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 1
    assert "PR #42" in capsys.readouterr().err


# ===================== 4. install-worker.sh 冒烟自检（AC4） =====================

def test_install_worker_smoke_checks_lock_alias_and_branch_guard():
    """冒烟 import 列表覆盖 canonical lock、旧别名 src.lock 与新布局守卫。"""
    with open(_INSTALL_SCRIPT, encoding="utf-8") as f:
        text = f.read()
    assert '"src.kernel.lock"' in text
    assert '"src.domains.runtime.engine.branch_guard"' in text
    assert '"src.lock"' in text, "旧别名 src.lock 仍需可导入（存量导入方）"
    # 诊断路径必须指向真实存在的 canonical otel 模块（src/observability 不存在；
    # insight 域内同名模块只是转发薄壳，安装期优先校验内核路径）
    assert "src.observability.otel" not in text
    assert '"src.kernel.otel"' in text


def _smoke_func_source():
    with open(_INSTALL_SCRIPT, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"_smoke_check\(\) \{.*?\n\}", text, re.S)
    assert m, "install-worker.sh _smoke_check not found"
    return m.group(0)


def _run_smoke(install_dir, tmp_path):
    """提取 _smoke_check，以假 venv（透传真解释器）+ 指定安装树执行。"""
    venv_dir = tmp_path / "venv"
    venv_bin = venv_dir / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    fake_py = venv_bin / "python"
    fake_py.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
                       encoding="utf-8")
    fake_py.chmod(0o755)
    sh = "\n".join([
        f"VENV={shlex.quote(str(venv_dir))}",
        f"INSTALL_DIR={shlex.quote(str(install_dir))}",
        'log() { echo "[log] $*"; }',
        _smoke_func_source(),
        "_smoke_check; rc=$?",
        'echo "smoke_rc=$rc"',
    ])
    r = subprocess.run(["bash", "-c", sh], capture_output=True, text=True,
                       cwd=str(tmp_path))
    return r.stdout + r.stderr


def _write_pkg_skeleton(install_dir):
    """能过 config/agent 导入的最小安装树骨架（kernel/engine 视用例补齐）。"""
    src = install_dir / "src"
    engine = src / "domains" / "runtime" / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    (src / "kernel").mkdir(parents=True, exist_ok=True)
    (src / "kernel" / "__init__.py").write_text("", encoding="utf-8")
    # 真实 src/__init__.py：注册 src.lock → src.kernel.lock 的存量导入别名
    real_init = os.path.join(REPO_ROOT, "src", "__init__.py")
    with open(real_init, encoding="utf-8") as f:
        (src / "__init__.py").write_text(f.read(), encoding="utf-8")
    (src / "kernel" / "config.py").write_text("X = 1\n", encoding="utf-8")
    # Issue #2923 起冒烟自检逐模块覆盖 session_manager / otel / vcs.api_client。
    (src / "kernel" / "session_manager.py").write_text("X = 1\n", encoding="utf-8")
    (src / "kernel" / "otel.py").write_text("X = 1\n", encoding="utf-8")
    vcs = src / "domains" / "runtime" / "vcs"
    vcs.mkdir(parents=True, exist_ok=True)
    (vcs / "__init__.py").write_text("", encoding="utf-8")
    (vcs / "api_client.py").write_text("X = 1\n", encoding="utf-8")
    for rel in ("domains", "domains/runtime", "domains/runtime/engine"):
        (src / rel / "__init__.py").write_text("", encoding="utf-8")
    (engine / "agent.py").write_text("X = 1\n", encoding="utf-8")
    return src


def test_smoke_check_rejects_package_missing_kernel_lock(tmp_path):
    """坏包（缺 src/kernel/lock.py）：冒烟必须拒绝升级并指认缺失模块。"""
    install_dir = tmp_path / "grape"
    install_dir.mkdir()
    _write_pkg_skeleton(install_dir)
    out = _run_smoke(install_dir, tmp_path)
    assert "smoke_rc=1" in out
    assert "Upgrade rejected" in out and "src.kernel.lock" in out


def test_smoke_check_rejects_package_missing_branch_guard(tmp_path):
    """坏包（缺 branch_guard.py）：冒烟必须拒绝升级并指认缺失模块。"""
    install_dir = tmp_path / "grape"
    install_dir.mkdir()
    src = _write_pkg_skeleton(install_dir)
    (src / "kernel" / "lock.py").write_text("", encoding="utf-8")
    out = _run_smoke(install_dir, tmp_path)
    assert "smoke_rc=1" in out
    assert "Upgrade rejected" in out and "branch_guard" in out


def test_smoke_check_passes_on_complete_package(tmp_path):
    """正控：模块齐全时冒烟放行（防夹具自身失效造成的假阳性）。"""
    install_dir = tmp_path / "grape"
    install_dir.mkdir()
    src = _write_pkg_skeleton(install_dir)
    (src / "kernel" / "lock.py").write_text("X = 1\n", encoding="utf-8")
    (src / "domains" / "runtime" / "engine" / "branch_guard.py").write_text(
        "X = 1\n", encoding="utf-8")
    out = _run_smoke(install_dir, tmp_path)
    assert "smoke_rc=0" in out and "smoke check passed" in out


# ===================== 5. 发布闭包（AC1 打包侧） =====================

def test_worker_package_manifest_ships_canonical_lock():
    """worker 包必须下发 canonical lock（src/kernel/ 覆盖）与包别名入口。"""
    from src.kernel.worker_package import MANIFEST, ROOT
    assert "src/__init__.py" in MANIFEST
    assert "src/kernel/" in MANIFEST
    assert os.path.isfile(os.path.join(ROOT, "src", "kernel", "lock.py"))
