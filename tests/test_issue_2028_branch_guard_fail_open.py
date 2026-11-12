"""Issue #2028：worker 包漏发 src/lock.py + pre-push 守卫 fail-open/版本升级回归。

背景：worker 包只下发 src/kernel/ 未下发顶层 src/lock.py，branch_guard 的
``from src import lock`` 在 worker 侧 ImportError；且钩子模板把 ``hook_main()``
置于 try/except 之外，守卫自身异常逃逸 → fail-open 退化 fail-closed，阻断所有 push。
"""
import importlib
import io
import os
import subprocess
import sys
import tarfile

import pytest

from src.domains.runtime.engine import branch_guard as bg

REPO_KEY = "gitcode:owner/repo"
BRANCH = "feat/issue-2028"


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


# ===================== 发布闭包 =====================

def test_worker_package_manifest_includes_top_level_lock():
    from src.kernel.worker_package import MANIFEST, ROOT
    assert "src/lock.py" in MANIFEST
    assert os.path.isfile(os.path.join(ROOT, "src", "lock.py"))


def test_release_pack_manifest_includes_top_level_lock(tmp_path, monkeypatch):
    from scripts import release_pack
    monkeypatch.setattr(release_pack, "OUT_DIR", str(tmp_path))
    pkg = release_pack.pack_tools("test")
    assert pkg and os.path.isfile(pkg)
    with tarfile.open(pkg) as tf:
        names = tf.getnames()
    assert "src/lock.py" in names, names


# ===================== 钩子模板 fail-open =====================

def test_hook_template_module_path_points_to_existing_canonical_file():
    module_path = bg._hook_module_path()
    assert os.path.isfile(module_path), module_path
    assert module_path.endswith(os.path.join("src", "domains", "runtime", "engine",
                                             "branch_guard.py"))
    root = bg._hook_root()
    assert os.path.isfile(os.path.join(root, "src", "domains", "runtime", "engine",
                                       "branch_guard.py"))


def test_hook_entry_code_fail_open_when_hook_main_raises(tmp_path):
    fake = tmp_path / "fake_guard.py"
    fake.write_text("def hook_main():\n    raise RuntimeError('cannot import name lock')\n",
                    encoding="utf-8")
    code = bg._hook_entry_code(str(tmp_path), str(fake))
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY],
                       input="", text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert "fail-open" in r.stderr and "cannot import name lock" in r.stderr


def test_hook_entry_code_fail_open_when_module_missing(tmp_path):
    missing = str(tmp_path / "does_not_exist.py")
    code = bg._hook_entry_code(str(tmp_path), missing)
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY],
                       input="", text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert "fail-open" in r.stderr
    assert "does_not_exist.py" in r.stderr


def test_hook_entry_code_fail_open_when_spec_is_none(tmp_path):
    """Issue #2028 I-3：`spec_from_file_location` 返回 None（无法识别的扩展名等）
    时 `module_from_spec` 抛 AttributeError，必须走 fail-open（exit 0 + 告警），
    不得以非零退出 fail-closed。"""
    bogus = str(tmp_path / "branch_guard")  # 无 .py 扩展名 -> spec is None
    code = bg._hook_entry_code(str(tmp_path), bogus)
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY],
                       input="", text=True, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert "fail-open" in r.stderr


def test_hook_main_fail_open_on_guard_exception(monkeypatch, capsys):
    def _boom(*a, **k):
        raise RuntimeError("cannot import name lock from src")

    monkeypatch.setattr(bg, "acquire_branch_lease", _boom)
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 0
    err = capsys.readouterr().err
    assert "[branch-guard]" in err and "fail-open" in err


def test_hook_main_missing_dependency_reports_locatable_error(monkeypatch, capsys):
    real_import = importlib.import_module

    def _fake_import(name, *a, **k):
        if name == "src.lock":
            raise ImportError("cannot import name lock from src")
        return real_import(name, *a, **k)

    monkeypatch.setattr(bg.importlib, "import_module", _fake_import)
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 0
    err = capsys.readouterr().err
    assert "src.lock" in err and "fail-open" in err


def test_hook_main_still_fail_closed_on_real_block(monkeypatch, capsys):
    monkeypatch.setattr(bg, "acquire_branch_lease", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "merged", "number": 42,
                                         "base": "arch-weekly-11"})
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 1
    err = capsys.readouterr().err
    assert "PR #42" in err


def test_hook_main_release_failure_keeps_block_verdict(monkeypatch, capsys):
    """租约释放 DB 异常不得把已判定的阻断（return 1）翻转为 fail-open 放行。"""
    monkeypatch.setattr(bg, "acquire_branch_lease", lambda *a, **k: {"ok": True})

    def _boom_release(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(bg, "release_branch_lease", _boom_release)
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: {"state": "merged", "number": 42,
                                         "base": "arch-weekly-11"})
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_ref_line()))
    assert bg.hook_main([REPO_KEY]) == 1
    err = capsys.readouterr().err
    assert "release lease failed" in err
    assert "database is locked" in err


# ===================== 存量钩子版本升级 =====================

class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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


def test_install_rewrites_outdated_guard_hook(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    stale = hooks / "pre-push"
    stale.write_text("#!/bin/sh\n" + bg.HOOK_MARKER + "\necho stale-template\n",
                     encoding="utf-8")

    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    content = stale.read_text(encoding="utf-8")
    assert bg.HOOK_VERSION_LINE in content
    assert "echo stale-template" not in content
    assert "fail-open" in content


def test_install_preserves_non_guard_hook(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    user_hook = hooks / "pre-push"
    user_hook.write_text("#!/bin/sh\necho user-hook\n", encoding="utf-8")

    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    preserved = hooks / "pre-push.grape-local"
    assert preserved.is_file() and "echo user-hook" in preserved.read_text(encoding="utf-8")
    assert bg.HOOK_VERSION_LINE in (hooks / "pre-push").read_text(encoding="utf-8")


def test_install_is_idempotent_for_current_template(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    hook = repo / ".git" / "hooks" / "pre-push"
    first = hook.read_text(encoding="utf-8")
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    assert hook.read_text(encoding="utf-8") == first


def _current_version_hook_with(content_replacements, base, seq={"n": 0}):
    """构造「版本指纹命中但固化路径失效」的存量钩子内容。"""
    seq["n"] += 1
    repo = _init_repo(base / f"gen-{seq['n']}")
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    content = (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
    for old, new in content_replacements:
        assert old in content, old
        content = content.replace(old, new)
    return content


def _write_stale_hook(tmp_path, stale):
    """初始化临时 git 仓库并写入失效的当前版本钩子，返回 hooks 目录。"""
    repo = _init_repo(tmp_path / "repo")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text(stale, encoding="utf-8")
    return repo, hooks


def test_install_rewrites_hook_when_interpreter_missing(tmp_path):
    stale = _current_version_hook_with([
        (sys.executable, "/nonexistent/venv/bin/python3.0"),
    ], tmp_path)
    repo, hooks = _write_stale_hook(tmp_path, stale)

    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    content = (hooks / "pre-push").read_text(encoding="utf-8")
    assert "/nonexistent/venv/bin/python3.0" not in content
    assert sys.executable in content


def test_install_rewrites_hook_when_module_path_missing(tmp_path):
    missing_module = "/nonexistent/src/domains/runtime/engine/branch_guard.py"
    stale = _current_version_hook_with([
        (bg._hook_module_path(), missing_module),
    ], tmp_path)
    repo, hooks = _write_stale_hook(tmp_path, stale)

    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    content = (hooks / "pre-push").read_text(encoding="utf-8")
    assert missing_module not in content
    assert bg._hook_module_path() in content


def test_install_rewrites_hook_when_repo_key_changed(tmp_path):
    stale = _current_version_hook_with([
        (REPO_KEY, "gitcode:other/repo"),
    ], tmp_path)
    repo, hooks = _write_stale_hook(tmp_path, stale)

    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    content = (hooks / "pre-push").read_text(encoding="utf-8")
    assert "gitcode:other/repo" not in content
    assert REPO_KEY in content


def test_existing_hook_usable_accepts_freshly_installed_hook(tmp_path):
    """Issue #2028（QA 第2轮 M-2）：全新安装的钩子必须通过可用性校验。

    2028-02 版解析多行内联代码恒 False，该函数是死代码、幂等跳过分支永不生效；
    本用例直接断言校验路径可达，防止再次退化。
    """
    repo = _init_repo(tmp_path / "repo")
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    hook = (repo / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")

    assert bg._existing_hook_usable(hook, REPO_KEY) is True
    # repo_key 不一致 → False（触发重写自愈）
    assert bg._existing_hook_usable(hook, "gitcode:other/repo") is False
    # marker 缺失（如 2028-02 旧模板无 marker）→ False → 重写升级
    assert bg._existing_hook_usable(
        hook.replace("# hook-interpreter: ", "# removed: "), REPO_KEY) is False