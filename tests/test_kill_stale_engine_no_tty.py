"""Issue #522 回归测试：无 TTY（setsid）Worker 上 `_kill_stale_engine` 仍能执行 pgrep 并终止进程树。

根因：`os.environ.get("USER", os.getlogin() ...)` 默认参数提前求值，无控制终端时
`os.getlogin()` 抛 OSError errno 6 (ENXIO)，pgrep 终止分支被跳过。

覆盖：
- `_current_username()` 逐级降级（USER 设置 / 未设置走 pwd / pwd 失败走 UID）
- setsid 无 TTY 子进程内调用 `_kill_stale_engine`（USER 已设置 / 未设置两场景）：
  不抛 ENXIO、pgrep 分支执行、模拟引擎进程（独立会话 + cwd 匹配 + "opencode run" 命令行）被终止
"""
import os
import pwd
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from src.runtime import agent as ra

_is_win = sys.platform == "win32"

# 在无 TTY 子进程内执行的验证脚本：spawn victim → 调 _kill_stale_engine → 报告结果
_CHILD_CODE = textwrap.dedent(
    """
    import os, subprocess, sys, time
    sys.path.insert(0, {repo_root!r})
    from src.runtime import agent as ra

    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "opencode", "run"],
        cwd={victim_cwd!r}, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    task = {{"cwd": {victim_cwd!r}}}
    try:
        ra._kill_stale_engine(task, "opencode")
    except Exception as e:
        print("EXC:", repr(e), flush=True)
        sys.exit(2)
    deadline = time.time() + 10
    while time.time() < deadline:
        if victim.poll() is not None:
            print("KILLED", flush=True)
            sys.exit(0)
        time.sleep(0.2)
    print("ALIVE", flush=True)
    sys.exit(1)
    """
)


def _run_no_tty_child(env_extra: dict, tmp_path):
    """start_new_session=True 模拟 setsid：子进程脱离控制终端（tty_nr=0）。"""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(ra.__file__), "..", ".."))
    victim_cwd = str(tmp_path / "task")
    os.makedirs(victim_cwd, exist_ok=True)
    code = _CHILD_CODE.format(repo_root=repo_root, victim_cwd=victim_cwd)
    env = {k: v for k, v in os.environ.items() if k != "USER"}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=repo_root, start_new_session=True,
    )


@pytest.mark.skipif(_is_win, reason="POSIX-only: pgrep/setsid/killpg")
class TestCurrentUsername:
    def test_user_env_set(self, monkeypatch):
        monkeypatch.setenv("USER", "someuser")
        assert ra._current_username() == "someuser"

    def test_user_env_unset_falls_back_to_pwd(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        assert ra._current_username() == pwd.getpwuid(os.getuid()).pw_name

    def test_pwd_failure_falls_back_to_uid(self, monkeypatch):
        monkeypatch.delenv("USER", raising=False)
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "pwd":
                raise ImportError("no pwd")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert ra._current_username() == str(os.getuid())


@pytest.mark.skipif(_is_win, reason="POSIX-only: pgrep/setsid/killpg")
class TestKillStaleEngineNoTty:
    def test_kill_without_tty_user_set(self, tmp_path):
        r = _run_no_tty_child({"USER": pwd.getpwuid(os.getuid()).pw_name}, tmp_path)
        assert r.returncode == 0, f"stdout={r.stdout} stderr={r.stderr}"
        assert "KILLED" in r.stdout
        assert "Errno 6" not in r.stderr and "Errno 6" not in r.stdout

    def test_kill_without_tty_user_unset(self, tmp_path):
        r = _run_no_tty_child({}, tmp_path)
        assert r.returncode == 0, f"stdout={r.stdout} stderr={r.stderr}"
        assert "KILLED" in r.stdout
        assert "Errno 6" not in r.stderr and "Errno 6" not in r.stdout
