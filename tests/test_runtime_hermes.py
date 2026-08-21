"""Tests for Hermes runtime backend（hermes 引擎支持）。"""
import os
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")

from unittest.mock import patch  # noqa: E402

from src.runtime.hermes import HermesRuntime  # noqa: E402
from src.runtime.agent import _backend_for_engine  # noqa: E402
from src.agent.executor import _BACKENDS, get_backend, discover_backends  # noqa: E402


class TestHermesRuntime:
    def setup_method(self):
        self.backend = HermesRuntime()

    def test_build_args_basic(self):
        # 新版本 hermes 支持 --cli（探测结果 mock 注入，不依赖真实二进制）
        with patch.object(self.backend, "_probe_cli_flag", return_value=True):
            args = self.backend.build_args("hello world")
        assert "-z" in args
        assert "hello world" in args
        assert "--yolo" in args          # 自动批准工具调用
        assert "--cli" in args           # 非交互纯文本输出
        assert "--continue" not in args  # 无会话时不继续

    def test_build_args_old_cli_without_flag(self):
        # 老版本无 --cli（如 v0.11.0）：不应注入 --cli，避免 unrecognized arguments
        with patch.object(self.backend, "_probe_cli_flag", return_value=False):
            args = self.backend.build_args("hello")
        assert "--cli" not in args
        assert "-z" in args

    def test_build_args_with_session(self):
        args = self.backend.build_args("test prompt", "sess_abc")
        assert "--continue" in args      # hermes 用最近会话语义继续

    def test_parse_output_text(self):
        evt = self.backend.parse_output("reply line one\n")
        assert evt == {"type": "text", "text": "reply line one", "sessionID": ""}

    def test_parse_output_blank(self):
        assert self.backend.parse_output("\n") is None
        assert self.backend.parse_output("   ") is None

    def test_parse_output_preserves_json_like_text(self):
        # hermes 纯文本输出中可能含 JSON 样式的文本，必须原样作为 text 保留（不按 NDJSON 解析）
        evt = self.backend.parse_output('{"type": "text"}')
        assert evt is not None
        assert evt["type"] == "text"
        assert evt["text"] == '{"type": "text"}'

    def test_get_env(self):
        env = self.backend.get_env()
        assert env.get("DEVOPS_SKILLS_DIR")
        assert env.get("DEVOPS_EVENT_TYPE") == ""

    def test_registered_in_backends(self):
        assert "hermes" in _BACKENDS
        assert get_backend("hermes").__class__.__name__ == "HermesRuntime"

    def test_discover_backends_includes_hermes(self):
        names = [b["name"] for b in discover_backends()]
        assert "hermes" in names


class TestBackendForEngine:
    def test_hermes_engine(self):
        assert _backend_for_engine("hermes-local-dev") == "hermes"
        assert _backend_for_engine("hermes") == "hermes"

    def test_opencode_engine(self):
        assert _backend_for_engine("opencode-local-dev") == "opencode"
        assert _backend_for_engine("opencode-worker-123") == "opencode"

    def test_codearts_engine(self):
        assert _backend_for_engine("codearts-x") == "codearts"

    def test_unknown_defaults_to_opencode(self):
        assert _backend_for_engine("") == "opencode"
        assert _backend_for_engine("some-engine") == "opencode"
        assert _backend_for_engine(None) == "opencode"
