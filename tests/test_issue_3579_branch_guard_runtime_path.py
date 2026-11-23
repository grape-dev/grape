"""Issue #3579：pre-push 钩子不得固化失效 worktree venv 解释器路径。

背景：钩子安装发生在 issue worktree 的 venv 内时，``sys.executable`` 与守卫模块
绝对路径被写入共享 ``hooks/pre-push``；worktree 回收后钩子以 exit 127 硬阻断 push，
且 shell 层失败早于内联 Python 的 try/except，既有 fail-open 不可达。

QA 复审（2028-08 模板）追加回归：
- 守卫模块加载优先级反转为「部署态稳定根优先，worktree 兜底」：worktree 存在
  旧版/被篡改模块时仍加载部署态守卫；worktree 缺文件时走部署态来源；
- 部署态包导入前移除 sys.path 的 ''/cwd，worktree 的同名 src 包不得遮蔽部署树；
- 稳定根分类：issue 工作树内安装不记录根（防回收失效复发）；
- 解释器与 worker 同源解析（GRAPE_PYTHON → VIRTUAL_ENV → push 树 .venv → PATH）；
- 解释器缺失 → exit 0 + 单行告警；模块缺失 → exit 0 + 单行告警；
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


def _hook_env(**extra):
    """钩子执行环境：清理解释器高优先级来源 + PYTHONPATH，保证确定性。"""
    env = os.environ.copy()
    env.pop("GRAPE_PYTHON", None)
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    env.update(extra)
    return env


def test_template_uses_runtime_resolution_not_absolute_worktree_paths(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    hook = _install(repo)
    content = open(hook, encoding="utf-8").read()

    assert "# hook-interpreter:" not in content
    assert "# hook-module-path:" not in content
    assert sys.executable not in content
    assert bg._hook_module_path() not in content
    assert f"# hook-module-rel: {bg._hook_module_relpath()}" in content
    # QA 复审：新增稳定来源根 marker（值随安装环境，可能为空串）
    assert "# hook-module-root: " in content
    # QA 复审 Optional：解释器解析链与 worker 同源
    assert '"$GRAPE_PYTHON"' in content
    assert '"$VIRTUAL_ENV/bin/python3"' in content
    assert '"$ROOT/.venv/bin/python3"' in content
    assert "command -v python3" in content
    assert "git rev-parse --show-toplevel" in content


# ===================== QA 复审 Required：稳定来源根分类 =====================

def test_stable_module_root_not_recorded_for_issue_worktree(tmp_path, monkeypatch):
    """安装进程跑在 issues/<n>/<agent> 检出内 → 不记录根（回收后必失效）。"""
    issue_wt = tmp_path / "issues" / "123" / "coding"
    issue_wt.mkdir(parents=True)
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(bg, "_hook_root", lambda: str(issue_wt))
    assert bg._stable_module_root(str(repo)) == ""


def test_stable_module_root_not_recorded_when_root_is_push_worktree(tmp_path, monkeypatch):
    """安装进程根就是本次 push worktree（dogfood 自推）→ 不记录根。"""
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(bg, "_hook_root", lambda: str(repo))
    assert bg._stable_module_root(str(repo)) == ""


def test_stable_module_root_recorded_for_deployed_install_root(tmp_path, monkeypatch):
    """安装进程跑在部署态稳定安装根（非 issue worktree）→ 记录根。"""
    deployed = tmp_path / "worker-install"
    deployed.mkdir()
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(bg, "_hook_root", lambda: str(deployed))
    assert bg._stable_module_root(str(repo)) == os.path.realpath(str(deployed))


# ===================== QA 复审 Required：部署态优先加载链 =====================

def _fake_guard(root, marker, rc=0):
    """构造自包含守卫模块：hook_main 打印来源 marker 并返回 rc。

    Issue #3579 QA 复审 Required：``src`` 及各级父目录一并写入 ``__init__.py``，
    使其成为**常规包**而非 namespace 目录。CI 先 ``pip install -e .[all,dev]``，
    仓库 ``src`` 是常规包且会被加入 ``sys.path``；若部署态假守卫仍是 namespace
    目录，同名常规包会抢先解析回仓库真实守卫，包导入兜底用例永不可达。
    """
    module = root / "src" / "domains" / "runtime" / "engine" / "branch_guard.py"
    module.parent.mkdir(parents=True)
    for rel in ("", "domains", "domains/runtime", "domains/runtime/engine"):
        (root / "src" / rel / "__init__.py").write_text("", encoding="utf-8")
    module.write_text(
        "import sys\n"
        "def hook_main():\n"
        f"    print('loaded-from={marker}', file=sys.stderr)\n"
        f"    return {rc}\n",
        encoding="utf-8",
    )
    return root


def _run_hook(hook, cwd, env=None):
    return subprocess.run(["/bin/sh", hook, "origin", "url"], input=REF_LINE,
                          text=True, capture_output=True, env=env or _hook_env(),
                          cwd=str(cwd))


def test_runtime_prefers_deployed_guard_over_worktree_module(tmp_path, monkeypatch):
    """QA 用例 1：worktree 检出旧版模块时，加载来源必须是部署态守卫。

    旧行为缺陷：运行时优先 push worktree 内模块 → 旧基线 worktree 跑旧守卫
    （缺 #2314/#2890 修复），工作树内改 branch_guard.py 即可改写守卫语义。
    """
    deployed = _fake_guard(tmp_path / "deployed", "deployed", rc=0)
    worktree = _init_repo(tmp_path / "repo")
    _fake_guard(worktree, "worktree", rc=1)  # worktree 内是旧版/被篡改模块
    monkeypatch.setattr(bg, "_hook_root", lambda: str(deployed))
    hook = _install(worktree)
    assert f"# hook-module-root: {os.path.realpath(str(deployed))}" in \
        open(hook, encoding="utf-8").read()

    r = _run_hook(hook, cwd=worktree)

    assert r.returncode == 0, r.stderr
    assert "loaded-from=deployed" in r.stderr
    assert "loaded-from=worktree" not in r.stderr
    assert "fail-open" not in r.stderr


def test_runtime_falls_back_to_deployed_root_when_worktree_lacks_module(tmp_path, monkeypatch):
    """QA 用例 2：worktree 无守卫模块文件时，走部署态来源加载。"""
    deployed = _fake_guard(tmp_path / "deployed", "deployed", rc=0)
    worktree = _init_repo(tmp_path / "repo")  # 无 src/…/branch_guard.py
    monkeypatch.setattr(bg, "_hook_root", lambda: str(deployed))
    hook = _install(worktree)

    r = _run_hook(hook, cwd=worktree)

    assert r.returncode == 0, r.stderr
    assert "loaded-from=deployed" in r.stderr
    assert "fail-open" not in r.stderr


def test_deployed_import_not_shadowed_by_worktree_src_package(tmp_path):
    """部署态包导入兜底：worktree 的同名 src 包（旧布局，无该模块）不得遮蔽部署树。

    -c 模式下 sys.path 的 ''/cwd 指向 worktree；不清洗就会解析回同一棵树，
    部署安装根永远不可达（QA 复审 Required 指出的兜底失效形态）。
    """
    deployed = _fake_guard(tmp_path / "deploy-pkg", "deployed-import", rc=0)
    worktree = tmp_path / "old-worktree"
    engine = worktree / "src" / "domains" / "runtime" / "engine"
    engine.mkdir(parents=True)
    for rel in ("", "domains", "domains/runtime", "domains/runtime/engine"):
        (worktree / "src" / rel / "__init__.py").write_text("", encoding="utf-8")
    # worktree 有 src 包但缺 branch_guard.py（#699 迁移前的旧基线形态）

    code = bg._hook_runtime_entry_code()
    env = _hook_env(PYTHONPATH=str(deployed))
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY, str(worktree), ""],
                       input=REF_LINE, text=True, capture_output=True, env=env,
                       cwd=str(worktree))

    assert r.returncode == 0, r.stderr
    assert "loaded-from=deployed-import" in r.stderr
    assert "fail-open" not in r.stderr


def test_worktree_guard_used_when_no_stable_root_recorded(tmp_path, monkeypatch):
    """未记录稳定根（issue 工作树安装）时降级 worktree 模块，与 2028-07 行为一致。"""
    issue_wt = tmp_path / "issues" / "123" / "coding"
    issue_wt.mkdir(parents=True)
    worktree = _init_repo(tmp_path / "repo")
    _fake_guard(worktree, "worktree", rc=0)
    monkeypatch.setattr(bg, "_hook_root", lambda: str(issue_wt))
    hook = _install(worktree)
    assert "# hook-module-root: \n" in open(hook, encoding="utf-8").read()

    r = _run_hook(hook, cwd=worktree)

    assert r.returncode == 0, r.stderr
    assert "loaded-from=worktree" in r.stderr


# ===================== 解释器解析（QA 复审 Optional） =====================

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
    env = _hook_env(PATH=str(bindir))

    r = subprocess.run(["/bin/sh", hook, "origin", "url"], input=REF_LINE,
                       text=True, capture_output=True, env=env, cwd=str(repo))

    assert r.returncode == 0, r.stderr
    assert "python3" in r.stderr and "fail-open" in r.stderr
    warning_lines = [ln for ln in r.stderr.splitlines() if "[branch-guard] WARNING" in ln]
    assert len(warning_lines) == 1, r.stderr


def test_hook_prefers_grape_python_interpreter(tmp_path):
    """shell 解析顺序 1：GRAPE_PYTHON 可执行即优先于 PATH python3。"""
    repo = _init_repo(tmp_path / "repo")
    hook = _install(repo)
    seen = tmp_path / "guard-seen.txt"
    fake_py = tmp_path / "grape-python"
    fake_py.write_text('#!/bin/sh\ncat > "$GUARD_SEEN"\n', encoding="utf-8")
    fake_py.chmod(0o755)
    env = _hook_env(GRAPE_PYTHON=str(fake_py), GUARD_SEEN=str(seen))

    r = subprocess.run(["/bin/sh", hook, "origin", "url"], input=REF_LINE,
                       text=True, capture_output=True, env=env, cwd=str(repo))

    assert r.returncode == 0, r.stderr
    assert seen.read_text() == REF_LINE


def test_hook_uses_push_worktree_venv_python(tmp_path):
    """shell 解析顺序 3：GRAPE_PYTHON/VIRTUAL_ENV 缺失时用 push 树 .venv。"""
    repo = _init_repo(tmp_path / "repo")
    hook = _install(repo)
    seen = tmp_path / "guard-seen.txt"
    fake_bin = repo / ".venv" / "bin"
    fake_bin.mkdir(parents=True)
    fake_py = fake_bin / "python3"
    fake_py.write_text('#!/bin/sh\ncat > "$GUARD_SEEN"\n', encoding="utf-8")
    fake_py.chmod(0o755)
    env = _hook_env(GUARD_SEEN=str(seen))

    r = subprocess.run(["/bin/sh", hook, "origin", "url"], input=REF_LINE,
                       text=True, capture_output=True, env=env, cwd=str(repo))

    assert r.returncode == 0, r.stderr
    assert seen.read_text() == REF_LINE


def test_resolve_hook_interpreter_order(tmp_path, monkeypatch):
    """Python 侧 _resolve_hook_interpreter 与 shell 模板同一顺序。"""
    # 安装根下的 .venv 解释器兜底来源
    root = tmp_path / "worker-install"
    root.mkdir()
    venv_py = root / ".venv" / "bin" / "python3"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")
    venv_py.chmod(0o755)
    monkeypatch.setattr(bg, "_hook_root", lambda: str(root))

    # 1) GRAPE_PYTHON 最优先
    grape_py = tmp_path / "grape-python"
    grape_py.write_text("", encoding="utf-8")
    grape_py.chmod(0o755)
    monkeypatch.setenv("GRAPE_PYTHON", str(grape_py))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert bg._resolve_hook_interpreter() == str(grape_py)

    # 2) VIRTUAL_ENV/bin/python3 次之
    monkeypatch.delenv("GRAPE_PYTHON", raising=False)
    venv_dir = tmp_path / "venv"
    (venv_dir / "bin").mkdir(parents=True)
    active_py = venv_dir / "bin" / "python3"
    active_py.write_text("", encoding="utf-8")
    active_py.chmod(0o755)
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_dir))
    assert bg._resolve_hook_interpreter() == str(active_py)

    # 3) 安装根 .venv（近似 push 树 .venv）再次之
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert bg._resolve_hook_interpreter() == str(venv_py)

    # 4) 全部缺失 → PATH python3 兜底
    monkeypatch.setattr(bg, "_hook_root", lambda: str(tmp_path / "no-venv"))
    assert bg._resolve_hook_interpreter() == (shutil.which("python3") or "")


# ===================== 模块缺失 fail-open（未记录稳定根时） =====================

def test_missing_guard_module_fails_open(tmp_path, monkeypatch):
    """未记录稳定根且 worktree 无模块 → 部署态包导入失败 → fail-open。

    Issue #3579 QA 复审 Required：CI 先 ``pip install -e .[all,dev]``，仓库
    ``src`` 常规包常驻 ``sys.path``；直接跑钩子会让包导入兜底命中真实守卫，
    fail-open 断言变成依赖真实守卫的内部异常（本机未安装时才会真走缺模块路径）。
    这里在 ``PYTHONPATH`` 前置一条与守卫同级、但缺 ``branch_guard.py`` 的
    常规包链（``src``→``domains``→``runtime``→``engine``），模拟部署树缺模块；
    每一级都由 blocker 自己命中，editable 安装的 finder 无法插队解析回仓库树。
    """
    # issue 工作树内安装（不记录稳定根），保证任何环境（CI 检出）下行为一致
    issue_wt = tmp_path / "issues" / "123" / "coding"
    issue_wt.mkdir(parents=True)
    monkeypatch.setattr(bg, "_hook_root", lambda: str(issue_wt))
    repo = _init_repo(tmp_path / "repo")
    hook = _install(repo)

    blocker = tmp_path / "no-guard-pkg"
    for rel in ("", "domains", "domains/runtime", "domains/runtime/engine"):
        (blocker / "src" / rel).mkdir(parents=True, exist_ok=True)
        (blocker / "src" / rel / "__init__.py").write_text("", encoding="utf-8")

    r = _run_hook(hook, cwd=repo, env=_hook_env(PYTHONPATH=str(blocker)))

    assert r.returncode == 0, r.stderr
    assert "fail-open" in r.stderr
    assert "branch_guard" in r.stderr
    assert "ModuleNotFoundError" in r.stderr


# ===================== fail-closed 正常路径 =====================

def _run_runtime_code(tmp_path, rc):
    root = _fake_guard(tmp_path / f"root-{rc}", "runtime", rc=rc)
    code = bg._hook_runtime_entry_code()
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY, str(root)],
                       input=REF_LINE, text=True, capture_output=True, cwd=str(root))
    return r


def test_normal_path_guard_still_fail_closed(tmp_path):
    r = _run_runtime_code(tmp_path, 1)
    assert r.returncode == 1, r.stderr
    assert "loaded-from=runtime" in r.stderr
    assert "fail-open" not in r.stderr


def test_normal_path_guard_allow(tmp_path):
    r = _run_runtime_code(tmp_path, 0)
    assert r.returncode == 0, r.stderr
    assert "loaded-from=runtime" in r.stderr
    assert "fail-open" not in r.stderr


# ===================== 存量钩子可用性校验（校验路径与生效路径统一） =====================

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


def test_existing_hook_usable_validates_recorded_root(tmp_path, monkeypatch):
    """校验路径与运行时生效路径统一：按 marker 记录的根（而非安装进程根）校验存在性。"""
    deployed = _fake_guard(tmp_path / "deployed", "deployed", rc=0)
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(bg, "_hook_root", lambda: str(deployed))
    content = open(_install(repo), encoding="utf-8").read()
    recorded = os.path.realpath(str(deployed))
    assert f"# hook-module-root: {recorded}" in content
    assert bg._existing_hook_usable(content, REPO_KEY) is True

    # 记录根失效（worker 卸载/搬迁）→ 不可用 → 触发重写自愈
    stale_root = content.replace(f"# hook-module-root: {recorded}",
                                  "# hook-module-root: /nonexistent/worker-install")
    assert bg._existing_hook_usable(stale_root, REPO_KEY) is False

    # 旧版（2028-07）无 root marker → 不可用 → 按新模板重写
    no_root = "\n".join(ln for ln in content.splitlines()
                        if not ln.startswith("# hook-module-root: "))
    assert bg._existing_hook_usable(no_root, REPO_KEY) is False


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
    assert "# hook-module-root: " in upgraded
    assert "/stale/issues/1/coding" not in upgraded
    assert "command -v python3" in upgraded
