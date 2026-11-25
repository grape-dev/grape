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
    assert "probe-log-line" in r.stderr, "文件 handler 失效后 log.info 必须仍输出到 stderr"


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
    assert "日志文件 handler 构造失败" in r.stderr
    assert "日志目录不可写" not in r.stderr
    assert "probe-warning" in r.stderr, "文件 handler 失效后 log.warning 必须仍输出到 stderr"


def test_log_file_not_degraded_in_normal_env():
    import src.kernel.config as config
    assert config.LOG_FILE_DEGRADED is False
    assert hasattr(config, "LOG_FILE_DEGRADED_REASON")


# ===================== 钩子 shell 层 stdin 暂存 fail-open =====================

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
    assert bg.HOOK_TEMPLATE_VERSION == "2028-09"
    assert bg.HOOK_VERSION_LINE == "# hook-template-version: " + bg.HOOK_TEMPLATE_VERSION


# ===================== 只读 hooks 目录：安装失败可观测 =====================

def _record_guard_log(monkeypatch):
    warnings = []

    class _Recorder:
        def warning(self, message):
            warnings.append(message)

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(bg, "log", _Recorder())
    return warnings


def test_install_failure_is_not_silent(tmp_path, monkeypatch):
    """hooks 目录只读导致 makedirs 抛 OSError 时，必须留下真实原因告警。"""
    repo = _init_repo(tmp_path / "repo")
    warnings = _record_guard_log(monkeypatch)

    def _boom(*a, **k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(bg.os, "makedirs", _boom)
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is False
    assert warnings, "安装失败不得静默"
    assert "not upgraded" in warnings[0]
    assert "Read-only file system" in warnings[0]
    assert "read-only hooks dir" not in warnings[0]


def test_install_failure_when_hook_write_permission_denied(tmp_path, monkeypatch):
    """hooks 目录存在但写 pre-push 权限不足：走真实 open() PermissionError 路径。"""
    repo = _init_repo(tmp_path / "repo")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    warnings = _record_guard_log(monkeypatch)

    import builtins
    real_open = builtins.open

    def _denied(path, mode="r", *a, **k):
        if str(path) == str(hooks / "pre-push") and "w" in mode:
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", _denied)
    assert bg.install_pre_push_hook(str(repo), REPO_KEY) is False
    assert warnings, "写 pre-push 权限不足不得静默"
    assert "not upgraded" in warnings[0]
    assert "Permission denied" in warnings[0]


def test_install_missing_context_reports_real_reason(tmp_path, monkeypatch):
    """安装上下文缺失（wt_dir/repo_key 为空）同样不得静默，且带真实 reason。"""
    warnings = _record_guard_log(monkeypatch)
    assert bg.install_pre_push_hook("", REPO_KEY) is False
    assert warnings and "missing wt_dir/repo_key" in warnings[0]
    assert "read-only hooks dir" not in warnings[0]

    warnings.clear()
    assert bg.install_pre_push_hook(str(tmp_path), "") is False
    assert warnings and "missing wt_dir/repo_key" in warnings[0]


def test_repogit_does_not_double_report_install_failure(tmp_path, monkeypatch, capsys):
    """安装失败告警由安装侧统一输出，repogit 不得重复上报覆盖真实原因。"""
    from src.domains.runtime.engine import repogit

    monkeypatch.setattr(bg, "install_pre_push_hook", lambda *a, **k: False)
    repogit._install_branch_guard_hook(str(tmp_path), REPO_KEY, BRANCH)
    err = capsys.readouterr().err
    assert "not upgraded" not in err


def test_hook_not_upgraded_falls_back_to_stderr(tmp_path, monkeypatch, capsys):
    """logger 自身失败时，_hook_not_upgraded 必须兜底写 stderr。"""
    class _BoomLog:
        def warning(self, message):
            raise RuntimeError("logger down")

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(bg, "log", _BoomLog())
    bg._hook_not_upgraded(REPO_KEY, str(tmp_path), "hooks dir not resolvable")
    err = capsys.readouterr().err
    assert "not upgraded" in err
    assert "hooks dir not resolvable" in err
    assert "logger down" in err or "RuntimeError" in err


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
    import logging
    from src.kernel.session_manager import db
    lock_key = bg.lease_key(REPO_KEY, BRANCH)

    def _cleanup():
        for sql, params in (
            ("DELETE FROM distributed_locks WHERE lock_key=?", (lock_key,)),
            ("DELETE FROM branch_guard_state WHERE repo_key=? AND head_branch=?",
             (REPO_KEY, BRANCH)),
        ):
            try:
                db.execute(sql, params)
                db.commit()
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "guard state cleanup failed (may be unavailable): %s", e)

    _cleanup()
    yield
    _cleanup()


def test_fail_closed_pr_merged_unchanged():
    r = bg.check_push_allowed(REPO_KEY, BRANCH, remote_sha="aaa", pr_state="merged",
                              pr_number=42, base_branch="arch-weekly-11")
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


# ===================== hook_main 真实退出码 =====================

_PUSH_INPUT = (f"refs/heads/{BRANCH} {'a' * 40} refs/heads/{BRANCH} {'b' * 40}\n")


def _run_hook_main(monkeypatch, *, lease=None, pr=None, expected=""):
    import io
    monkeypatch.setattr(bg, "_missing_hook_deps", lambda: [])
    monkeypatch.setattr(bg, "acquire_branch_lease",
                        lambda *a, **k: lease or {"ok": True})
    monkeypatch.setattr(bg, "release_branch_lease", lambda *a, **k: None)
    monkeypatch.setattr(bg, "reconcile_pending", lambda *a, **k: expected)
    monkeypatch.setattr(bg, "_fetch_pr_state_via_grape",
                        lambda *a, **k: pr or {"state": ""})
    monkeypatch.setattr(bg, "record_pending_push", lambda *a, **k: {})
    monkeypatch.setattr(bg.sys, "stdin", io.StringIO(_PUSH_INPUT))
    return bg.hook_main([REPO_KEY])


def test_hook_main_exits_1_on_pr_merged(monkeypatch):
    rc = _run_hook_main(monkeypatch,
                        pr={"state": "merged", "number": 42, "base": "arch-weekly-11"})
    assert rc == 1


def test_hook_main_exits_1_on_lease_contention(monkeypatch):
    rc = _run_hook_main(monkeypatch, lease={"ok": False, "message": "branch locked"})
    assert rc == 1


def test_hook_main_exits_1_on_remote_drift(monkeypatch):
    rc = _run_hook_main(monkeypatch, pr={"state": "open"}, expected="aaa")
    assert rc == 1
