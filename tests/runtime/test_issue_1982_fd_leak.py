"""Issue #1982：worker 任务路径 pipe fd 泄漏回归测试。

覆盖：
1. `_close_proc_pipes` 幂等关闭 stdin/stdout/stderr；
2. spawn/终止 N 次后 `/proc/self/fd` 回到基线（正常结束 / SIGTERM 终止分支）；
3. `stream_agent` 消费方提前中断（cancel/requeue/stream 提前 break）时，
   作用域兜底回收进程并关闭管道；
4. 安装器 systemd unit 与升级 drop-in 均写入 LimitNOFILE=524288。
"""
import gc
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.agents.executor import (  # noqa: E402
    _close_proc_pipes, _terminate_proc, _track_proc, _untrack_proc,
    register_backend, stream_agent,
)

pytestmark = pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"), reason="fd regression test requires /proc")


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _settle() -> None:
    gc.collect()
    time.sleep(0.2)


def _spawn_reading_stdin() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0,
    )


def test_close_proc_pipes_releases_all_fds():
    proc = _spawn_reading_stdin()
    _close_proc_pipes(proc)
    proc.wait(timeout=5)
    # 幂等：重复关闭不抛异常
    _close_proc_pipes(proc)
    _settle()
    assert proc.poll() is not None


def test_spawn_and_close_cycles_return_to_fd_baseline():
    """模拟任务结束路径：spawn → close → 40 轮后 fd 不线性增长。"""
    _settle()
    baseline = _fd_count()
    for _ in range(40):
        proc = _spawn_reading_stdin()
        _close_proc_pipes(proc)
        proc.wait(timeout=5)
    _settle()
    assert _fd_count() <= baseline + 3, (
        f"fd grew after 40 spawn/close cycles: base={baseline} now={_fd_count()}")


def test_spawn_and_terminate_cycles_return_to_fd_baseline():
    """模拟 kill/超时分支：_track_proc → _terminate_proc → _untrack_proc。"""
    _settle()
    baseline = _fd_count()
    for _ in range(20):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, start_new_session=True,
        )
        _track_proc(proc)
        _terminate_proc(proc, "fd-regression", grace_seconds=0.2)
        _untrack_proc(proc)
    _settle()
    assert _fd_count() <= baseline + 3, (
        f"fd grew after 20 spawn/terminate cycles: base={baseline} now={_fd_count()}")


class _ChatterBackend:
    name = "fd-chatter-1982"
    binary = "python3"
    prompt_via_stdin = False

    def build_args(self, prompt, session_id=""):
        code = (
            "import time,json\n"
            "for i in range(50):\n"
            "    print(json.dumps({'type':'text','part':{'type':'text','text':'t'+str(i)}}), flush=True)\n"
            "    time.sleep(0.05)\n"
        )
        return ["-c", code]

    def get_env(self, account="", user_id=0):
        return {}

    def parse_output(self, line):
        import json
        try:
            msg = json.loads(line)
        except Exception:
            return None
        if msg.get("type") == "text":
            return {"type": "text", "text": (msg.get("part") or {}).get("text", "")}
        return None


def test_stream_agent_early_close_reclaims_pipes():
    """消费方在流中提前关闭生成器（cancel/requeue/break）→ 进程被杀、fd 回基线。"""
    register_backend(_ChatterBackend.name, _ChatterBackend())
    _settle()
    baseline = _fd_count()
    gen = stream_agent("p", os.getcwd(), "", backend_name=_ChatterBackend.name,
                       max_exec_seconds=30)
    first = next(gen)
    assert first[0] == "text"
    gen.close()
    _settle()
    assert _fd_count() <= baseline + 3, (
        f"fd leaked after early generator close: base={baseline} now={_fd_count()}")


def test_abort_cancel_rounds_return_threads_and_fds_to_baseline():
    """Issue #1982 复审：N 轮 abort/cancel（消费方提前 close 生成器）后，
    线程数与 fd 同时回基线——reader 线程经作用域兜底 join 回收，不持续累积。"""
    import threading
    register_backend(_ChatterBackend.name, _ChatterBackend())
    _settle()
    fd_baseline = _fd_count()
    thread_baseline = threading.active_count()
    rounds = 5
    for _ in range(rounds):
        gen = stream_agent("p", os.getcwd(), "", backend_name=_ChatterBackend.name,
                           max_exec_seconds=30)
        first = next(gen)
        assert first[0] == "text"
        gen.close()  # 模拟 abort/cancel：消费方提前中断，走 _close_exec_scope 兜底
        _settle()
    _settle()
    now_threads = threading.active_count()
    now_fds = _fd_count()
    assert now_threads <= thread_baseline + 1, (
        f"reader threads accumulated after {rounds} abort/cancel rounds: "
        f"base={thread_baseline} now={now_threads}")
    assert now_fds <= fd_baseline + 3, (
        f"fd leaked after {rounds} abort/cancel rounds: base={fd_baseline} now={now_fds}")


def test_install_worker_unit_sets_limit_nofile_and_upgrade_dropin():
    script = os.path.join(ROOT, "scripts", "install-worker.sh")
    content = open(script, encoding="utf-8").read()
    assert "LimitNOFILE=524288" in content
    assert "runtime-agent.service.d" in content
    assert "limit-nofile.conf" in content
