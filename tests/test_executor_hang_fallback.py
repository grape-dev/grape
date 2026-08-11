"""方案 C：executor 超时兜底防误判（见 vod-agent-hang-rootcause-fix.md 方案 C）。

核心：完成判定仅以「进程退出 + done」为准；无输出超时收尾先 SIGTERM→收尾窗口→SIGKILL；
长任务静默不误判完成（不提前 done）；真卡死最终 exit_code=-9（failed 语义）。

用 monkeypatch 替换 subprocess.Popen，模拟「启动后 stdout 无任何输出且永不退出」的子进程，
验证 stream_agent 走「无输出超时」分支：产出 done，exit_code=-9，errors 含 killed。
"""
import io, subprocess

from src.runtime.executor import stream_agent

import src.agent.executor as _ex_mod  # 用于 patch 模块级 _terminate_proc（stream_agent 内部引用它）


class _BlockingStdout:
    """阻塞的 stdout：readline 永不返回（模拟子进程无输出且不退出）。"""
    def readline(self):
        import threading
        threading.Event().wait(30)  # 阻塞 30s，超时后返回空（EOF）
        return b""


class _FakeProc:
    """模拟子进程：stdout 阻塞、poll 永不退出、wait 抛 TimeoutExpired。"""
    returncode = -9
    def __init__(self, *a, **kw):
        self.stdout = _BlockingStdout()
        self.stderr = io.BytesIO()
        self.stdin = None
    def poll(self):
        return None  # 永不自行退出 → 走「无输出超时」分支
    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=timeout)
    def terminate(self):
        pass
    def kill(self):
        pass


def _fake_popen(*args, **kwargs):
    return _FakeProc()


def _terminate_now(proc, label="", grace_seconds=10.0):
    proc.returncode = -9
    return None


class TestHangFallback:
    def test_silent_hang_times_out_with_neg9(self, monkeypatch):
        """模拟启动后零输出、永不退出 → 无输出超时分支产出 done(-9)（failed 语义）。"""
        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(_ex_mod, "_terminate_proc", _terminate_now)
        out = []
        for evt_type, data in stream_agent(
            "prompt", "/tmp", session_id="", backend_name="opencode", max_exec_seconds=0.2,
        ):
            out.append((evt_type, data))
        dones = [d for t, d in out if t == "done"]
        assert dones, "应产出 done（无输出超时兜底）"
        assert dones[0]["exit_code"] == -9, "零输出卡死应标 -9（failed），不得误判成功"
        assert any("killed" in e for e in dones[0].get("errors", [])), "errors 应含 killed 说明"

    def test_no_premature_done_on_long_silence(self, monkeypatch):
        """模拟「有 tool_use 但随后长静默（如 git clone）」，未正常收尾不得标成功。"""
        class _SlowProc(_FakeProc):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                # 第一行是 tool_use NDJSON，之后永静默（stdout 阻塞）
                self.stdout = _YieldOnceThenBlock(
                    b'{"type":"tool_use","part":{"tool":"bash","state":{"input":{}}}}\n'
                )
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _SlowProc())
        monkeypatch.setattr(_ex_mod, "_terminate_proc", _terminate_now)
        out = []
        for evt_type, data in stream_agent(
            "prompt", "/tmp", session_id="", backend_name="opencode", max_exec_seconds=0.2,
        ):
            out.append((evt_type, data))
        tool_uses = [d for t, d in out if t == "tool_use"]
        dones = [d for t, d in out if t == "done"]
        assert tool_uses, "应记录到 tool_use 事件（工作确实开始）"
        assert dones and dones[0]["exit_code"] == -9, "虽产出 tool_use 但未正常收尾，应 failed(-9)"


class _YieldOnceThenBlock:
    """先返回一行，之后 readline 阻塞。"""
    def __init__(self, first_line):
        self._buf = first_line
    def readline(self):
        if self._buf:
            line, self._buf = self._buf, b""
            return line
        import threading
        threading.Event().wait(30)
        return b""
