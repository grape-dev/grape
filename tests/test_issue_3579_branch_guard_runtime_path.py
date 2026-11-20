"""Issue #3579：pre-push 钩子不得固化失效 worktree venv 解释器路径。

背景：钩子安装发生在 issue worktree 的 venv 内时，``sys.executable`` 与守卫模块
绝对路径被写入共享 ``hooks/pre-push``；worktree 回收后钩子以 exit 127 硬阻断 push，
且 shell 层失败早于内联 Python 的 try/except，既有 fail-open 不可达。

回归覆盖：
- 模板只写仓库相对模块路径与运行时解释器解析；
- 解释器缺失 → exit 0 + 单行告警；
- 模块缺失 → exit 0 + 单行告警；
- 正常路径 → 守卫仍 fail-closed 返回 1。
"""
import os
import shutil
import subprocess
import sys

import pytest

from src.domains.runtime.engine import branch_guard as bg

REPO_KEY = "gitcode:owner/repo"
REF_LINE = "refs/heads/feat/x abc123 refs/heads/feat/x 000\n"


def _git(*args, cwd=None):
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True, env=env)


def _init_repo(path):
    path.mkdir()
    _git("init", cwd=path)
    return path


def _install(path):
    assert bg.install_pre_push_hook(str(path), REPO_KEY) is True
    return os.path.join(bg._hooks_dir(str(path)), "pre-push")


def test_template_uses_runtime_resolution_not_absolute_worktree_paths(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    hook = _install(repo)
    content = open(hook, encoding="utf-8").read()

    assert "# hook-interpreter:" not in content
    assert "# hook-module-path:" not in content
    assert sys.executable not in content
    assert bg._hook_module_path() not in content
    assert f"# hook-module-rel: {bg._hook_module_relpath()}" in content
    assert "command -v python3" in content
    assert "git rev-parse --show-toplevel" in content


def _hook_path_without_python(tmp_path):
    """构造 PATH：只含钩子执行必需的 git/coreutils，不含 python3。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("git", "mktemp", "dirname", "rm", "cat"):
        target = shutil.which(name)
        assert target, name
        os.symlink(target, bindir / name)
    return bindir


def test_missing_interpreter_fails_open_not_127(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    hook = _install(repo)
    bindir = _hook_path_without_python(tmp_path)
    env = os.environ.copy()
    env["PATH"] = str(bindir)
    env.pop("PYTHONPATH", None)

    r = subprocess.run(["/bin/sh", hook, "origin", "url"], input=REF_LINE,
                       text=True, capture_output=True, env=env, cwd=str(repo))

    assert r.returncode == 0, r.stderr
    assert "python3" in r.stderr and "fail-open" in r.stderr
    warning_lines = [ln for ln in r.stderr.splitlines() if "[branch-guard] WARNING" in ln]
    assert len(warning_lines) == 1, r.stderr


def test_missing_guard_module_fails_open(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    hook = _install(repo)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    r = subprocess.run(["/bin/sh", hook, "origin", "url"], input=REF_LINE,
                       text=True, capture_output=True, env=env, cwd=str(repo))

    assert r.returncode == 0, r.stderr
    assert "fail-open" in r.stderr
    assert "branch_guard" in r.stderr


def _fake_guard(root, rc):
    module = root / "src" / "domains" / "runtime" / "engine" / "branch_guard.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "import sys\n"
        "def hook_main():\n"
        f"    print('guard-ran rc={rc}', file=sys.stderr)\n"
        f"    return {rc}\n",
        encoding="utf-8",
    )
    return root


def _run_runtime_code(tmp_path, rc):
    root = _fake_guard(tmp_path / f"root-{rc}", rc)
    code = bg._hook_runtime_entry_code()
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY, str(root)],
                       input=REF_LINE, text=True, capture_output=True, cwd=str(root))
    return r


def test_normal_path_guard_still_fail_closed(tmp_path):
    r = _run_runtime_code(tmp_path, 1)
    assert r.returncode == 1, r.stderr
    assert "guard-ran rc=1" in r.stderr
    assert "fail-open" not in r.stderr


def test_normal_path_guard_allow(tmp_path):
    r = _run_runtime_code(tmp_path, 0)
    assert r.returncode == 0, r.stderr
    assert "guard-ran rc=0" in r.stderr
    assert "fail-open" not in r.stderr


def test_existing_hook_unusable_when_interpreter_missing(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    content = open(_install(repo), encoding="utf-8").read()
    assert bg._existing_hook_usable(content, REPO_KEY) is True

    monkeypatch.setattr(bg, "_resolve_hook_interpreter", lambda: "")
    assert bg._existing_hook_usable(content, REPO_KEY) is False


def test_existing_hook_unusable_when_module_relpath_mismatch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    content = open(_install(repo), encoding="utf-8").read()
    stale = content.replace(f"# hook-module-rel: {bg._hook_module_relpath()}",
                            "# hook-module-rel: src/runtime/branch_guard.py")
    assert bg._existing_hook_usable(stale, REPO_KEY) is False


def test_install_upgrades_legacy_absolute_path_template(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    hook = _install(repo)
    legacy = open(hook, encoding="utf-8").read()
    legacy = legacy.replace(
        f"# hook-module-rel: {bg._hook_module_relpath()}",
        "# hook-module-path: /stale/issues/1/coding/src/domains/runtime/engine/branch_guard.py")
    legacy = legacy.replace(
        "command -v python3", "# hook-interpreter: /stale/issues/1/coding/.venv/bin/python")
    with open(hook, "w", encoding="utf-8") as f:
        f.write(legacy)

    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    upgraded = open(hook, encoding="utf-8").read()
    assert "# hook-module-rel:" in upgraded
    assert "/stale/issues/1/coding" not in upgraded
    assert "command -v python3" in upgraded