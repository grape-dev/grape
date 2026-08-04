"""Tests for agent_executor module."""
import pytest
import os
import sys
import json

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")  # 兼容旧测试；token 现经 prompt 注入

from src.agent import (
    OpencodeBackend, CodeArtsBackend, get_backend,
    AgentBackend, stream_agent, stream_opencode
)
from src.agent.executor import _redact


class TestOpencodeBackend:
    def setup_method(self):
        self.backend = OpencodeBackend()

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


class TestCodeArtsBackend:
    def setup_method(self):
        self.backend = CodeArtsBackend()

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
        b = get_backend("opencode")
        assert isinstance(b, OpencodeBackend)

    def test_codearts(self):
        b = get_backend("codearts")
        assert isinstance(b, CodeArtsBackend)

    def test_unknown_fallback_to_opencode(self):
        b = get_backend("nonexistent")
        assert isinstance(b, OpencodeBackend)

    def test_case_sensitive(self):
        b = get_backend("CodeArts")
        assert isinstance(b, OpencodeBackend)  # case doesn't match, fallback


class TestBackendParseOutput:
    @pytest.fixture(params=[OpencodeBackend(), CodeArtsBackend()])
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
            events = list(stream_agent("prompt", os.getcwd(), "", "issue",
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
        from src.config import get_agent_backend, _load_agent_routing_config
        _load_agent_routing_config()
        assert get_agent_backend("issue") == "opencode"
        assert get_agent_backend("implementation") == "opencode"
        assert get_agent_backend("build-debug") == "opencode"
        assert get_agent_backend("unknown_skill") == "opencode"
