"""terminate_active_agents 测试：服务停机时终止运行中 agent 子进程（grape-main timeout 重启修复 A）。"""
import os
import sys
import subprocess

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.runtime.executor import (
    _track_proc, _untrack_proc, _active_procs, _active_procs_lock, terminate_active_agents,
)


def _spawn():
    return subprocess.Popen(["python", "-c", "import time; time.sleep(60)"])


def _cleanup(procs):
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            pass


def _reset_registry():
    with _active_procs_lock:
        _active_procs.clear()


def test_terminate_kills_tracked_proc():
    """被跟踪的子进程在停机时被终止，且注册表清空"""
    _reset_registry()
    p = _spawn()
    _track_proc(p)
    assert p.poll() is None
    terminate_active_agents(grace_seconds=3)
    assert p.poll() is not None, "被跟踪子进程应被终止"
    assert not _active_procs, "终止后注册表应清空"
    _cleanup([p])


def test_untracked_proc_not_terminated():
    """untrack 后停机不应终止该进程"""
    _reset_registry()
    p = _spawn()
    _track_proc(p)
    _untrack_proc(p)
    terminate_active_agents(grace_seconds=2)
    assert p.poll() is None, "已 untrack 的子进程不应被终止"
    _cleanup([p])
    _reset_registry()


def test_terminate_empty_registry_ok():
    _reset_registry()
    terminate_active_agents(grace_seconds=2)   # 不应抛异常
    assert not _active_procs
