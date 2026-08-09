"""Tests for agent_executor module."""
import pytest
import os
import sys
import json

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")  # 兼容旧测试；token 现经 prompt 注入

from src.runtime import (
    OpencodeRuntime, CodeArtsRuntime, get_runtime,
    AgentRuntime, stream_runtime, stream_opencode
)
from src.runtime.executor import _redact, stream_agent


class TestOpencodeRuntime:
    def setup_method(self):
        self.backend = OpencodeRuntime()

    def test_build_args_basic(self):
        args = self.backend.build_args("hello world")
        assert "run" in args
        assert "--format" in args
        assert "json" in args
        assert "--dangerously-skip-permissions" in args
        assert "hello world" in args

    def test_build_args_with_session(self):
        args = self.backend.build_args("test prompt", "sess_abc")
        assert "--session" in args
        idx = args.index("--session")
        assert args[idx + 1] == "sess_abc"

    def test_build_args_without_session(self):
        args = self.backend.build_args("test")
        assert "--session" not in args

    def test_name(self):
        assert self.backend.name == "opencode"


class TestCodeArtsRuntime:
    def setup_method(self):
        self.backend = CodeArtsRuntime()

    def test_build_args_basic(self):
        args = self.backend.build_args("hello world")
        assert "run" in args
        assert "--format" in args
        assert "json" in args
        assert "--dangerously-skip-permissions" not in args
        assert "hello world" not in args  # prompt piped via stdin

    def test_build_args_with_session(self):
        args = self.backend.build_args("test prompt", "sess_abc")
        assert "--session" in args
        assert "sess_abc" in args
        assert "--sessionID" not in args

    def test_build_args_no_dangerous_flag(self):
        """CodeArts should NOT have --dangerously-skip-permissions."""
        args = self.backend.build_args("test")
        assert all("dangerously" not in a for a in args)

    def test_name(self):
        assert self.backend.name == "codearts"


class TestGetBackend:
    def test_opencode(self):
        b = get_runtime("opencode")
        assert isinstance(b, OpencodeRuntime)

    def test_codearts(self):
        b = get_runtime("codearts")
        assert isinstance(b, CodeArtsRuntime)

    def test_unknown_fallback_to_opencode(self):
        b = get_runtime("nonexistent")
        assert isinstance(b, OpencodeRuntime)

    def test_case_sensitive(self):
        b = get_runtime("CodeArts")
        assert isinstance(b, OpencodeRuntime)  # case doesn't match, fallback


class TestBackendParseOutput:
    @pytest.fixture(params=[OpencodeRuntime(), CodeArtsRuntime()])
    def backend(self, request):
        return request.param

    def test_parse_text_event(self, backend):
        line = json.dumps({"type": "text", "part": {"text": "hello", "type": "text"}})
        result = backend.parse_output(line)
        assert result is not None
        assert result["type"] == "text"
        assert result["text"] == "hello"

    def test_parse_step_start(self, backend):
        line = json.dumps({"type": "step_start", "sessionID": "sess_1", "part": {"step": 1}})
        result = backend.parse_output(line)
        assert result["type"] == "step_start"
        assert result["sessionID"] == "sess_1"

    def test_parse_reasoning(self, backend):
        line = json.dumps({"type": "reasoning", "part": {"text": "thinking..."}})
        result = backend.parse_output(line)
        assert result["type"] == "reasoning"

    def test_parse_tool_use(self, backend):
        line = json.dumps({"type": "tool_use", "part": {"tool": "read", "state": {"input": {"path": "file.txt"}}}})
        result = backend.parse_output(line)
        assert result["type"] == "tool_use"
        assert result["tool"] == "read"

    def test_parse_step_finish(self, backend):
        line = json.dumps({"type": "step_finish", "part": {"tokens": {"total": 100}}})
        result = backend.parse_output(line)
        assert result["type"] == "step_finish"
        assert result["tokens"] == 100

    def test_parse_error(self, backend):
        line = json.dumps({"type": "error", "error": {"data": {"message": "something failed"}}})
        result = backend.parse_output(line)
        assert result["type"] == "error"

    def test_parse_invalid_json(self, backend):
        result = backend.parse_output("not json")
        assert result is None

    def test_parse_unknown_event(self, backend):
        line = json.dumps({"type": "ping"})
        result = backend.parse_output(line)
        assert result["type"] == "other"


class TestRedact:
    """_redact 函数：日志/事件输出中脱敏 token"""

    def test_redact_replaces_token(self):
        r = _redact("curl -H 'Authorization: Bearer SECRET123'", ["SECRET123"])
        assert r == "curl -H 'Authorization: Bearer [REDACTED]'"
        assert "SECRET123" not in r

    def test_redact_multiple_tokens(self):
        r = _redact("mix SECRET1 and SECRET2", ["SECRET1", "SECRET2"])
        assert r == "mix [REDACTED] and [REDACTED]"

    def test_redact_no_token_unchanged(self):
        r = _redact("no secret here", ["SECRET"])
        assert r == "no secret here"

    def test_redact_empty(self):
        assert _redact("", ["S"]) == ""
        assert _redact("text", []) == "text"

    def test_stream_agent_redacts_tool_use_token(self):
        """stream_agent 输出（tool_use 等）不应含注入的 token，应为 [REDACTED]"""
        from unittest.mock import patch, MagicMock

        fake_lines = [
            json.dumps({"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": "curl -H 'Authorization: Bearer BOTTOK' https://api.gitcode.com/x"}}}}),
            json.dumps({"type": "text", "part": {"text": "reply mentions USERTOK", "type": "text"}}),
        ]

        class FakeStream:
            returncode = 0
            def __init__(self):
                self._lines = list(fake_lines)
                self._sidx = 0
                self.stdout = self
                self.stderr = self
            def readline(self):
                if self._sidx < len(self._lines):
                    line = self._lines[self._sidx]
                    self._sidx += 1
                    return line.encode() + b"\n"
                return b""
            def wait(self, timeout=None):
                return 0
            def read(self):
                return b""

        fake_proc = FakeStream()
        with patch("src.agent.executor.subprocess.Popen", return_value=fake_proc), \
             patch("src.agent.executor.shutil.which", return_value="/fake/opencode"):
            events = list(stream_runtime("prompt", os.getcwd(), "", "issue",
                                       bot_token="BOTTOK", user_token="USERTOK"))
        combined = " ".join(str(d) for _, d in events)
        assert "BOTTOK" not in combined
        assert "USERTOK" not in combined
        assert "[REDACTED]" in combined


class TestStreamAgentBackwardCompat:
    """stream_opencode() should still work (backward compat wrapper)."""
    def test_stream_opencode_yields_done_on_no_binary(self):
        gen = stream_opencode("prompt", os.getcwd(), "", "test")
        results = list(gen)
        assert any(r[0] == "done" for r in results)
        done = [r[1] for r in results if r[0] == "done"][0]
        assert "errors" in done or "stderr" in done


class TestConfigIntegration:
    """Test that config.py's get_agent_backend works with the routing file."""
    def test_config_routing(self):
        from src.config import get_agent_backend
        assert get_agent_backend("issue") == "opencode"
        assert get_agent_backend("implementation") == "opencode"
        assert get_agent_backend("build-debug") == "opencode"
        assert get_agent_backend("unknown_skill") == "opencode"


class _SleepBackend(AgentRuntime):
    """无输出的假 backend：spawn `python -c time.sleep(60)`，用于测超时终止。"""
    name = "sleepbackend"
    binary = "python"
    prompt_via_stdin = True

    def build_args(self, prompt, session_id=""):
        return ["-c", "import time; time.sleep(60)"]

    def get_env(self, account="", user_id=0):
        return {}


class _ChatterBackend(_SleepBackend):
    """持续输出的假 backend：每 0.05s 输出一行 text，测"持续输出不超时"。"""
    name = "chatterbackend"

    def build_args(self, prompt, session_id=""):
        code = "import time,json,sys\nfor i in range(50):\n    print(json.dumps({'type':'text','part':{'type':'text','text':'t'+str(i)}}), flush=True)\n    time.sleep(0.05)\nprint(json.dumps({'type':'text','part':{'type':'text','text':'final'}}), flush=True)"
        return ["-c", code]


class TestStreamAgentTimeout:
    def _register(self, backend):
        from src.agent.executor import register_backend, _BACKENDS
        _BACKENDS[backend.name] = backend
        return backend.name

    def test_kills_silent_process_on_no_output(self):
        """完全无输出的 agent，超过 max_exec_seconds 后被优雅终止，返回 done(exit_code=-9)"""
        name = self._register(_SleepBackend())
        from src.agent.executor import stream_agent
        events = list(stream_agent("p", os.getcwd(), "", backend_name=name, max_exec_seconds=1))
        done = [d for et, d in events if et == "done"][0]
        assert done["exit_code"] == -9
        assert any("no output" in e for e in done["errors"])
        # POSIX：验证无遗留子进程；Windows 无 pgrep，跳过
        if sys.platform.startswith("win"):
            return
        import subprocess
        leftovers = subprocess.run(
            ["pgrep", "-f", "import time; time.sleep(60)"],
            capture_output=True, text=True).stdout.strip()
        assert leftovers == "", f"leftover agent process: {leftovers}"

    def test_continuous_output_no_timeout(self):
        """持续有输出的 agent 不被超时杀掉，正常走完并返回 done(exit_code=0)"""
        name = self._register(_ChatterBackend())
        from src.agent.executor import stream_agent
        events = list(stream_agent("p", os.getcwd(), "", backend_name=name, max_exec_seconds=0.4))
        done = [d for et, d in events if et == "done"][0]
        assert done["exit_code"] == 0, done
        # 应产出多条 text 事件
        texts = [d for et, d in events if et == "text"]
        assert len(texts) >= 10


class TestTerminateProc:
    def test_graceful_then_kill(self):
        """SIGTERM 优雅失败后，SIGKILL 兜底并回收进程。仅 POSIX。"""
        import signal as _signal
        from unittest.mock import patch
        from subprocess import TimeoutExpired
        if not hasattr(os, "killpg"):
            import pytest
            pytest.skip("POSIX process groups not available on this platform")
        from src.agent.executor import _terminate_proc

        class FakeProc:
            pid = 12345
            def __init__(self):
                self._wait_count = 0
            def poll(self):
                return None
            def terminate(self):
                pass
            def kill(self):
                pass
            def wait(self, timeout=None):
                self._wait_count += 1
                if self._wait_count == 1:
                    raise TimeoutExpired  # SIGTERM 后 10s 未退出
                return 0

        fp = FakeProc()
        with patch("src.agent.executor.os.killpg") as mock_killpg:
            _terminate_proc(fp, "test")
        # 第一次 SIGTERM 进程组，第二次 SIGKILL 进程组
        sigs = [c.args[1] for c in mock_killpg.call_args_list]
        assert sigs == [_signal.SIGTERM, _signal.SIGKILL]

    def test_already_exited_returns_early(self):
        from src.agent.executor import _terminate_proc
        class ExitedProc:
            pid = 1
            def poll(self):
                return 0
        _terminate_proc(ExitedProc(), "test")  # 不应抛异常


class TestConsumeStreamAsync:
    def test_bridges_events(self):
        """consume_stream_async 在线程池消费 stream_agent，逐事件回传。"""
        import asyncio
        from src.agent.executor import consume_stream_async
        name = _register_tmp(_ChatterBackend())

        async def main():
            events = []
            async for et, d in consume_stream_async("p", os.getcwd(), "", backend_name=name):
                events.append((et, d))
            return events

        events = asyncio.run(main())
        done = [d for et, d in events if et == "done"][0]
        assert done["exit_code"] == 0
        assert len([1 for et, _ in events if et == "text"]) >= 10


def _register_tmp(backend):
    from src.agent.executor import _BACKENDS
    _BACKENDS[backend.name] = backend
    return backend.name


class TestStreamAgentSessionAutoRetry:
    """会话失效自动重建：opencode 返回 'Session not found' 时，executor 应降级重跑（不带 session）。"""

    def _make_popen(self, calls):
        """构造一个每次调用返回不同行为的 FakePopen（模拟进程）。calls: [dict(...), ...]。"""
        class FakePipe:
            def __init__(self, data=b""):
                self._data = data
            def readline(self, chunk=b""):
                if self._data:
                    line, self._data = self._data.split(b"\n", 1)
                    return line + b"\n"
                return b""
            def read(self, *a, **k):
                return self._data
            def close(self):
                pass

        class FakePopen:
            def __init__(self, *args, **kwargs):
                idx = self.__class__.call_count
                self.__class__.call_count += 1
                spec = calls[min(idx, len(calls) - 1)]
                self.returncode = spec.get("returncode", 0)
                self._stdout = spec.get("stdout", "")
                self._stderr = spec.get("stderr", "")
                self.__class__.last_args = args  # 记录最近一次调用参数（类级，便于断言）
            @property
            def stdout(self):
                return FakePipe(self._stdout.encode("utf-8"))
            @property
            def stderr(self):
                return FakePipe(self._stderr.encode("utf-8"))
            def wait(self, timeout=None):
                return self.returncode

        FakePopen.call_count = 0
        return FakePopen

    def test_retries_without_session_on_session_not_found(self):
        """首次带死 session 失败（Session not found），应自动重跑并成功"""
        from unittest.mock import patch
        from src.agent import stream_agent

        success_stdout = (
            '{"type":"step_start","sessionID":"ses_new_abc","part":{"type":"step-start"}}\n'
            '{"type":"text","part":{"type":"text","text":"RECOVERED"}}\n'
        )
        calls = [
            {"returncode": 1, "stdout": "", "stderr": "Error: Session not found\n"},
            {"returncode": 0, "stdout": success_stdout, "stderr": ""},
        ]
        FakePopen = self._make_popen(calls)
        with patch("src.agent.executor.subprocess.Popen", FakePopen), \
             patch("src.agent.executor.shutil.which", return_value="/usr/bin/opencode"):
            events = list(stream_runtime("p", os.getcwd(), session_id="ses_dead_old", backend_name="opencode"))

        done = [d for et, d in events if et == "done"][0]
        assert done["exit_code"] == 0, done
        assert "RECOVERED" in done["text"]
        assert done["oc_session_id"] == "ses_new_abc"
        # 确认重跑：两次 Popen 调用，第二次不带 --session
        assert FakePopen.call_count == 2, f"expected 2 Popen calls, got {FakePopen.call_count}"
        second_args = FakePopen.last_args[0]
        assert "--session" not in second_args, f"second run should drop --session, got {second_args}"

    def test_single_done_event_when_retry(self):
        """重跑场景下，调用方只应收到一个 done 事件（避免重复 done 破坏上层逻辑）"""
        from unittest.mock import patch
        from src.agent import stream_agent

        success_stdout = (
            '{"type":"text","part":{"type":"text","text":"OK"}}\n'
        )
        calls = [
            {"returncode": 1, "stdout": "", "stderr": "Error: Session not found\n"},
            {"returncode": 0, "stdout": success_stdout, "stderr": ""},
        ]
        FakePopen = self._make_popen(calls)
        with patch("src.agent.executor.subprocess.Popen", FakePopen), \
             patch("src.agent.executor.shutil.which", return_value="/usr/bin/opencode"):
            events = list(stream_runtime("p", os.getcwd(), session_id="ses_dead", backend_name="opencode"))

        done_events = [et for et, d in events if et == "done"]
        assert len(done_events) == 1, f"expected exactly 1 done event, got {len(done_events)}"

    def test_no_retry_when_session_valid(self):
        """会话有效（正常成功）时不触发重跑，仅一次 Popen"""
        from unittest.mock import patch
        from src.agent import stream_agent

        success_stdout = (
            '{"type":"text","part":{"type":"text","text":"NORMAL"}}\n'
        )
        calls = [
            {"returncode": 0, "stdout": success_stdout, "stderr": ""},
        ]
        FakePopen = self._make_popen(calls)
        with patch("src.agent.executor.subprocess.Popen", FakePopen), \
             patch("src.agent.executor.shutil.which", return_value="/usr/bin/opencode"):
            events = list(stream_runtime("p", os.getcwd(), session_id="ses_good", backend_name="opencode"))

        done = [d for et, d in events if et == "done"][0]
        assert done["exit_code"] == 0
        assert "NORMAL" in done["text"]
        assert FakePopen.call_count == 1, f"expected 1 Popen call, got {FakePopen.call_count}"

    def test_no_retry_on_unrelated_error(self):
        """非会话类错误（如权限失败）不重跑，保持单次 done 且透传错误"""
        from unittest.mock import patch
        from src.agent import stream_agent

        calls = [
            {"returncode": 1, "stdout": "", "stderr": "Error: something else failed\n"},
        ]
        FakePopen = self._make_popen(calls)
        with patch("src.agent.executor.subprocess.Popen", FakePopen), \
             patch("src.agent.executor.shutil.which", return_value="/usr/bin/opencode"):
            events = list(stream_runtime("p", os.getcwd(), session_id="ses_any", backend_name="opencode"))

        done = [d for et, d in events if et == "done"][0]
        assert done["exit_code"] == 1
        assert "something else" in (done.get("stderr") or "")
        assert FakePopen.call_count == 1, f"expected 1 Popen call (no retry), got {FakePopen.call_count}"


class TestExecutorPipeInvocation:
    """守护：executor 用纯管道直接启动 agent 命令，不得引入 `script` 包装。

    2026-08-09 复盘：曾误判「opencode 需控制终端」而加 script 包装，实为多余修复
    （飞书一直走纯管道正常），且远端 worker 无 util-linux script 会退化。
    见 docs/new-version2/web-chat-postmortem.md §四。
    """

    def test_popen_uses_direct_command_and_pipe(self):
        from unittest.mock import patch
        import subprocess as _subprocess

        fake_lines = [
            json.dumps({"type": "step_start", "sessionID": "s1", "part": {"step": 1}}),
            json.dumps({"type": "text", "part": {"text": "ok", "type": "text"}}),
            json.dumps({"type": "step_finish", "part": {"tokens": {"total": 1}}}),
        ]

        class FakeProc:
            returncode = 0
            def __init__(self, *a, **k):
                self._lines = list(fake_lines)
                self._i = 0
                self.stdout = self
                self.stderr = self
            def readline(self):
                if self._i < len(self._lines):
                    line = self._lines[self._i]
                    self._i += 1
                    return line.encode() + b"\n"
                return b""
            def wait(self, timeout=None):
                return 0
            def read(self):
                return b""

        captured = {}
        def fake_popen(cmd, *a, **k):
            captured["cmd"] = cmd
            captured["stdout"] = k.get("stdout")
            return FakeProc(cmd, *a, **k)

        with patch("src.agent.executor.subprocess.Popen", side_effect=fake_popen), \
             patch("src.agent.executor.shutil.which", return_value="/usr/bin/opencode"):
            list(stream_runtime("p", os.getcwd(), "", "issue"))

        assert captured["cmd"][0] == "/usr/bin/opencode", "命令应为直接 agent CLI，不得是 script 包装"
        assert "script" not in captured["cmd"]
        assert captured["cmd"][1] == "run"
        assert captured["stdout"] == _subprocess.PIPE, "应为纯管道 stdout"
