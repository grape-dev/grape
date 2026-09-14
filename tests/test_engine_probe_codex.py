"""Tests for codex engine probe coverage (Issue #81).

验证 codex 引擎在服务端和 worker 端探活命令模板中均有条目，
探活逻辑能正确覆盖 codex 后端。
"""
import os
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")


class TestServerSideProbeCommands:
    """服务端探活（src/observability/engine_probe.py）。"""

    def test_codex_in_probe_commands(self):
        from src.observability.engine_probe import _PROBE_COMMANDS
        assert "codex" in _PROBE_COMMANDS

    def test_codex_probe_command_format(self):
        from src.observability.engine_probe import _PROBE_COMMANDS
        cmd = _PROBE_COMMANDS["codex"]
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--json" in cmd
        assert "--sandbox" in cmd
        assert "workspace-write" in cmd
        assert "hello" in cmd

    def test_codex_probe_returns_result_not_skip(self):
        """_probe_engine 对 codex 不应返回 skip（有命令模板）。"""
        from src.observability.engine_probe import _probe_engine
        result = _probe_engine("codex", "")
        assert result["status"] != "skip"

    def test_unknown_engine_returns_skip(self):
        from src.observability.engine_probe import _probe_engine
        result = _probe_engine("unknown-engine", "")
        assert result["status"] == "skip"

    def test_all_known_engines_covered(self):
        """确保四个已注册引擎都有探活命令。"""
        from src.observability.engine_probe import _PROBE_COMMANDS
        for eng in ("opencode", "hermes", "codearts", "codex"):
            assert eng in _PROBE_COMMANDS, f"{eng} missing from _PROBE_COMMANDS"


class TestWorkerSideProbeCommands:
    """Worker 端探活（src/runtime/agent.py）。"""

    def test_codex_in_worker_probe_commands(self):
        from src.runtime.agent import _PROBE_COMMANDS
        assert "codex" in _PROBE_COMMANDS

    def test_codex_worker_probe_command_format(self):
        from src.runtime.agent import _PROBE_COMMANDS
        cmd = _PROBE_COMMANDS["codex"]
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--json" in cmd
        assert "--sandbox" in cmd
        assert "workspace-write" in cmd
        assert "hello" in cmd

    def test_codex_worker_probe_returns_result_not_skip(self):
        from src.runtime.agent import _probe_engine_worker
        result = _probe_engine_worker("codex", "")
        assert result["status"] != "skip"

    def test_all_known_engines_covered_worker(self):
        from src.runtime.agent import _PROBE_COMMANDS
        for eng in ("opencode", "hermes", "codearts", "codex"):
            assert eng in _PROBE_COMMANDS, f"{eng} missing from worker _PROBE_COMMANDS"
