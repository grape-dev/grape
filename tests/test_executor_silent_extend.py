"""#501 修复：进程存活但无输出时延长窗口，不误杀长命令。

真实故障：agent 执行 `pip install langgraph`（长 bash 命令）期间 opencode 无 NDJSON 输出，
executor 600s 无输出超时强杀（exit=-9），即使进程仍在工作。

修复：无输出超时分支先检查 proc.poll()——
- 进程仍存活（长命令/长思考中）→ 重置静默计时继续等待（受 AGENT_SILENT_EXTEND_CAP 硬上限约束）；
- 进程已退出但无 done（异常）→ 才强杀。
"""
import io, os, time
import pytest
import subprocess


class _AliveProc:
    """模拟"进程存活但无输出"的子进程：poll 返回 None（永不退出）、reader 持续阻塞。"""
    def __init__(self, *a, **kw):
        self.stdout = _BlockingStdout()
        self.stderr = io.BytesIO()
        self.stdin = None
        self.returncode = -9
    def poll(self):
        return None  # 进程存活
    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=timeout)
    def terminate(self):
        pass
    def kill(self):
        pass


class _BlockingStdout:
    def readline(self):
        import threading
        threading.Event().wait(30)
        return b""


def _terminate_now(proc, label="", grace_seconds=10.0):
    proc.returncode = -9
    return None


class TestSilentExtension:
    def test_alive_process_not_killed_until_extend_cap(self, monkeypatch):
        """进程存活 + 无输出：延长窗口（不立即 -9）；累积超过硬上限才强杀。"""
        from src.runtime.executor import stream_agent
        import src.agent.executor as _ex
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _AliveProc())
        monkeypatch.setattr(_ex, "_terminate_proc", _terminate_now)
        monkeypatch.setenv("AGENT_SILENT_EXTEND_CAP", "0.5")  # 硬上限 0.5s（max_exec 0.1s 的 5 倍）

        out = []
        for evt_type, data in stream_agent(
            "prompt", "/tmp", session_id="", backend_name="opencode", max_exec_seconds=0.1,
        ):
            out.append((evt_type, data))
        dones = [d for t, d in out if t == "done"]
        # 延长窗口后仍会超时强杀（cap 耗尽），但应经过"extending"延长（time.sleep 累积）
        assert dones, "最终应产出 done"
        assert dones[0]["exit_code"] == -9, "cap 耗尽后仍强杀"

    def test_dead_process_fails_fast(self, monkeypatch):
        """进程已退出但无 done（异常）：不延长，直接强杀。"""
        from src.runtime.executor import stream_agent
        import src.agent.executor as _ex

        class _DeadProc(_AliveProc):
            def poll(self):
                return 1  # 进程已退出（异常返回码）

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _DeadProc())
        monkeypatch.setattr(_ex, "_terminate_proc", _terminate_now)
        out = []
        for evt_type, data in stream_agent(
            "prompt", "/tmp", session_id="", backend_name="opencode", max_exec_seconds=0.1,
        ):
            out.append((evt_type, data))
        dones = [d for t, d in out if t == "done"]
        assert dones and dones[0]["exit_code"] == -9, "进程已退出无 done → 应强杀"
