"""Issue #2933：只读日志目录 / 只读 TMPDIR 下 pre-push 守卫不得阻断 push。

背景：容器/沙箱把 worktree gitdir 与 data/logs 挂成只读时，``src/kernel/config.py``
导入期构造 ``TimedRotatingFileHandler`` 抛 OSError → 钩子非零退出 → git 在鉴权前
报 ``failed to push some refs``；钩子 shell 层 ``TMPDIR`` 不可写时同样直接 exit 1。
本用例集覆盖：导入期日志降级、shell 层 fail-open、只读 hooks 目录可观测、
旧模板升级，以及三类真实违规的 fail-closed 语义不变。
"""
import os
import subprocess
import sys

import pytest

from src.domains.runtime.engine import branch_guard as bg

REPO_KEY = "gitcode:owner/repo"
BRANCH = "feat/issue-2933"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _run_py(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          text=True, capture_output=True, timeout=120)


# ===================== 导入期日志降级 =====================

def test_import_survives_readonly_log_dir():
    """日志目录 makedirs 抛 OSError：导入成功、降级标志置位、仅一条中文提示。"""
    r = _run_py(
        "import os\n"
        "_real = os.makedirs\n"
        "def _fake(path, *a, **k):\n"
        "    if os.path.basename(str(path)) == 'logs':\n"
        "        raise OSError(30, 'Read-only file system')\n"
        "    return _real(path, *a, **k)\n"
        "os.makedirs = _fake\n"
        "import src.kernel.config as config\n"
        "print('DEGRADED=%s' % config.LOG_FILE_DEGRADED)\n"
        "config.log.info('probe-log-line')\n"
        "print('IMPORT_OK')\n"
    )
    assert r.returncode == 0, r.stderr
    assert "IMPORT_OK" in r.stdout
    assert "DEGRADED=True" in r.stdout
    assert "Traceback" not in r.stderr
    assert r.stderr.count("日志目录不可写") == 1
    assert "已降级为仅 stderr 输出" in r.stderr


def test_import_survives_file_handler_construction_failure():
    """TimedRotatingFileHandler 构造抛 OSError：同样降级，不阻断导入。"""
    r = _run_py(
        "import logging, logging.handlers\n"
        "class _Boom(logging.Handler):\n"
        "    def __init__(self, *a, **k):\n"
        "        raise OSError(30, 'Read-only file system')\n"
        "logging.handlers.TimedRotatingFileHandler = _Boom\n"
        "import src.kernel.config as config\n"
        "print('DEGRADED=%s' % config.LOG_FILE_DEGRADED)\n"
        "config.log.warning('probe-warning')\n"
        "print('IMPORT_OK')\n"
    )
    assert r.returncode == 0, r.stderr
    assert "IMPORT_OK" in r.stdout
    assert "DEGRADED=True" in r.stdout
    assert "Traceback" not in r.stderr
    assert r.stderr.count("日志目录不可写") == 1


def test_log_file_not_degraded_in_normal_env():
    import src.kernel.config as config
    assert config.LOG_FILE_DEGRADED is False
    assert hasattr(config, "LOG_FILE_DEGRADED_REASON")


# ===================== 钩子 shell 层 stdin 暂存 fail-open =====================

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


def test_hook_shell_fail_open_when_tmpdir_unwritable(tmp_path):
    """TMPDIR 不可写（用普通文件冒充目录）：钩子 shell 层不得 exit 1。"""
    repo = _init_repo(tmp_path / "repo")
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    hook = os.path.join(bg._hooks_dir(str(repo)), "pre-push")

    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("file", encoding="utf-8")
    env = os.environ.copy()
    env["TMPDIR"] = str(not_a_dir)

    payload = "refs/heads/feat/x abc123 refs/heads/feat/x 000\n"
    r = subprocess.run(["sh", hook, "origin", "url"], input=payload, text=True,
                       capture_output=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "fail-open" in r.stderr
    assert "--no-verify" in r.stderr
    warning_lines = [ln for ln in r.stderr.splitlines() if "[branch-guard] WARNING" in ln]
    assert len(warning_lines) == 1, r.stderr
    assert "无法暂存" in warning_lines[0] and "--no-verify" in warning_lines[0]


def test_hook_template_fail_open_warning_mentions_no_verify():
    """_fail_open / 钩子内联异常告警都必须给 --no-verify 指引。"""
    import io
    from unittest import mock

    with mock.patch.object(bg.sys, "stderr", io.StringIO()) as buf:
        bg._fail_open("boom")
    assert "--no-verify" in buf.getvalue()

    code = bg._hook_entry_code(str(ROOT), os.path.join(ROOT, "no_such_module.py"))
    r = subprocess.run([sys.executable, "-c", code, REPO_KEY],
                       input="", text=True, capture_output=True)
    assert r.returncode == 0
    assert "fail-open" in r.stderr and "--no-verify" in r.stderr


def test_hook_template_version_bumped():
    assert bg.HOOK_TEMPLATE_VERSION == "2028-05"
    assert bg.HOOK_VERSION_LINE in ("# hook-template-version: " + bg.HOOK_TEMPLATE_VERSION)


# ===================== 只读 hooks 目录：安装失败可观测 =====================

def test_install_failure_is_not_silent(tmp_path, monkeypatch):
    """hooks 目录只读导致 makedirs 抛 OSError 时，必须留下「未升级」告警。"""
    repo = _init_repo(tmp_path / "repo")
    warnings = []

    class _Recorder:
        def warning(self, message):
            warnings.append(message)

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(bg, "log", _Recorder())

    def _boom(*a, **k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(bg.os, "makedirs", _boom)
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is False
    assert warnings, "安装失败不得静默"
    assert "not upgraded" in warnings[0]
    assert "Read-only file system" in warnings[0]


def test_install_upgrades_stale_2028_03_template(tmp_path):
    """存量 2028-03 旧模板可被重写为当前版本（2028-05）。"""
    repo = _init_repo(tmp_path / "repo")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    stale = hooks / "pre-push"
    stale.write_text("#!/bin/sh\n" + bg.HOOK_MARKER
                     + "\n# hook-template-version: 2028-03\nexit 1\n",
                     encoding="utf-8")

    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is True
    content = stale.read_text(encoding="utf-8")
    assert bg.HOOK_VERSION_LINE in content
    assert "2028-03" not in content
    assert "exit 1" not in content


# ===================== fail-closed 语义保持不变 =====================

@pytest.fixture(autouse=True)
def _isolate_guard_state():
    from src.kernel.session_manager import db
    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.execute("DELETE FROM branch_guard_state")
        db.commit()
    except Exception:
        pass
    yield
    try:
        db.execute("DELETE FROM distributed_locks WHERE lock_key LIKE 'branch_guard:%'")
        db.execute("DELETE FROM branch_guard_state")
        db.commit()
    except Exception:
        pass


def test_fail_closed_pr_merged_unchanged():
    r = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="aaa", pr_state="merged",
                              pr_number=42, base_branch="weekly-mys")
    assert r["allowed"] is False and r["reason"] == "pr_merged"


def test_fail_closed_remote_drift_unchanged():
    r = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="bbb",
                              expected_remote_sha="aaa")
    assert r["allowed"] is False and r["reason"] == "remote_drift"
    assert "fetch/rebase" in r["message"]


def test_fail_closed_lease_contention_unchanged(monkeypatch):
    import src.kernel.lock as lock
    monkeypatch.setattr(lock, "_holder", "host-task-A")
    assert bg.acquire_branch_lease(REPO_KEY, BRANCH, expected_remote_sha="sha-old")["ok"] is True

    monkeypatch.setattr(lock, "_holder", "host-task-B")
    second = bg.acquire_branch_lease(REPO_KEY, BRANCH)
    assert second["ok"] is False and second["reason"] == "branch_locked"
    assert "host-task-A" in second["message"]