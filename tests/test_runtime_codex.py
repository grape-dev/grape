"""Codex Runtime 测试：注册 / build_args / parse_output / get_env / discover_backends / is_known_engine_name。

覆盖 Issue #65 验收标准 1~3 + 事件解析映射。
JSONL 事件格式参照 codex-rs/exec/src/exec_events.rs（ThreadEvent / ThreadItemDetails）。
"""
import json
import os
import sys

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

os.environ.setdefault("GITCODE_API_TOKEN", "test_token")

from src.runtime.codex import CodexRuntime
from src.runtime import get_runtime
from src.runtime import CodexRuntime as ExportedCodexRuntime
from src.agent.codex import CodexBackend
from src.agent.executor import _BACKENDS, discover_backends


class TestCodexRuntimeRegistration:
    def test_backends_contains_codex(self):
        assert "codex" in _BACKENDS
        assert isinstance(_BACKENDS["codex"], CodexRuntime)

    def test_get_runtime_codex(self):
        b = get_runtime("codex")
        assert isinstance(b, CodexRuntime)

    def test_agent_compat_layer(self):
        assert CodexBackend is CodexRuntime

    def test_exported_from_runtime_init(self):
        assert ExportedCodexRuntime is CodexRuntime

    def test_name_and_binary(self):
        b = CodexRuntime()
        assert b.name == "codex"
        assert b.binary == "codex"

    def test_prompt_via_stdin_false(self):
        b = CodexRuntime()
        assert b.prompt_via_stdin is False


class TestDiscoverBackends:
    def test_codex_in_discover_list(self):
        names = [b["name"] for b in discover_backends()]
        assert "codex" in names

    def test_codex_discover_fields(self):
        codex_info = next(b for b in discover_backends() if b["name"] == "codex")
        assert codex_info["pip_package"] == "@openai/codex"
        assert "installed" in codex_info
        assert "cli_path" in codex_info


class TestIsKnownEngineName:
    def test_codex_known(self):
        from src.user.db import is_known_engine_name
        assert is_known_engine_name("codex") is True

    def test_codex_case_insensitive(self):
        from src.user.db import is_known_engine_name
        assert is_known_engine_name("CODEX") is True
        assert is_known_engine_name("Codex") is True

    def test_unknown_still_false(self):
        from src.user.db import is_known_engine_name
        assert is_known_engine_name("default") is False
        assert is_known_engine_name("") is False


class TestBuildArgs:
    def test_basic_args(self):
        b = CodexRuntime()
        args = b.build_args("hello world")
        assert args == ["exec", "--json", "--sandbox", "danger-full-access", "--skip-git-repo-check", "hello world"]

    def test_args_contain_required_flags(self):
        b = CodexRuntime()
        args = b.build_args("test prompt")
        assert "exec" in args
        assert "--json" in args
        assert "--sandbox" in args
        idx = args.index("--sandbox")
        assert args[idx + 1] == "danger-full-access"
        assert "test prompt" in args

    def test_empty_prompt_no_positional(self):
        b = CodexRuntime()
        args = b.build_args("")
        assert args == ["exec", "--json", "--sandbox", "danger-full-access", "--skip-git-repo-check"]

    def test_session_id_ignored(self):
        """codex exec 不支持 --session（resume 走 exec resume，首期不做）。"""
        b = CodexRuntime()
        args = b.build_args("test", "sess_abc")
        assert "--session" not in args
        assert "sess_abc" not in args


class TestParseOutput:
    def setup_method(self):
        self.rt = CodexRuntime()

    def test_invalid_json_returns_none(self):
        assert self.rt.parse_output("not json") is None

    def test_thread_started_maps_step_start(self):
        line = json.dumps({"type": "thread.started", "thread_id": "thread-abc"})
        r = self.rt.parse_output(line)
        assert r["type"] == "step_start"
        assert r["sessionID"] == "thread-abc"

    def test_turn_started_maps_step_start(self):
        line = json.dumps({"type": "turn.started"})
        r = self.rt.parse_output(line)
        assert r["type"] == "step_start"

    def test_turn_completed_maps_step_finish_with_tokens(self):
        line = json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "reasoning_output_tokens": 20, "cached_input_tokens": 0,
                      "cache_write_input_tokens": 0}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "step_finish"
        assert r["tokens"] == 170

    def test_turn_failed_maps_error(self):
        line = json.dumps({"type": "turn.failed", "error": {"message": "rate limited"}})
        r = self.rt.parse_output(line)
        assert r["type"] == "error"
        assert "rate limited" in r["message"]

    def test_top_level_error_maps_error(self):
        line = json.dumps({"type": "error", "message": "auth failed"})
        r = self.rt.parse_output(line)
        assert r["type"] == "error"
        assert "auth failed" in r["message"]

    def test_item_completed_agent_message_maps_text(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i1", "type": "agent_message", "text": "Hello from codex"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "text"
        assert r["text"] == "Hello from codex"

    def test_item_started_agent_message_maps_text(self):
        line = json.dumps({
            "type": "item.started",
            "item": {"id": "i1", "type": "agent_message", "text": "partial..."}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "text"
        assert r["text"] == "partial..."

    def test_item_reasoning_maps_reasoning(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i2", "type": "reasoning", "text": "thinking about approach"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "reasoning"
        assert r["text"] == "thinking about approach"

    def test_item_started_command_execution_maps_tool_use(self):
        line = json.dumps({
            "type": "item.started",
            "item": {"id": "i3", "type": "command_execution",
                     "command": "ls -la", "aggregated_output": "",
                     "exit_code": None, "status": "in_progress"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "tool_use"
        assert r["tool"] == "shell"
        assert r["input"]["command"] == "ls -la"

    def test_item_completed_command_execution_maps_tool_result(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i3", "type": "command_execution",
                     "command": "ls -la", "aggregated_output": "file1\nfile2",
                     "exit_code": 0, "status": "completed"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "tool_result"
        assert "file1" in r["text"]
        assert "exit_code=0" in r["text"]

    def test_item_completed_command_execution_failed(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i3b", "type": "command_execution",
                     "command": "false", "aggregated_output": "",
                     "exit_code": 1, "status": "failed"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "tool_result"
        assert "exit_code=1" in r["text"]

    def test_item_started_mcp_tool_call_maps_tool_use(self):
        line = json.dumps({
            "type": "item.started",
            "item": {"id": "i4", "type": "mcp_tool_call",
                     "server": "fs", "tool": "read_file",
                     "arguments": {"path": "foo.txt"},
                     "result": None, "error": None, "status": "in_progress"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "tool_use"
        assert r["tool"] == "fs.read_file"
        assert r["input"]["path"] == "foo.txt"

    def test_item_completed_mcp_tool_call_maps_tool_result(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i4", "type": "mcp_tool_call",
                     "server": "fs", "tool": "read_file",
                     "arguments": {"path": "foo.txt"},
                     "result": {"content": [{"type": "text", "text": "file content here"}]},
                     "error": None, "status": "completed"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "tool_result"
        assert "file content here" in r["text"]

    def test_item_completed_mcp_tool_call_error(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i4e", "type": "mcp_tool_call",
                     "server": "fs", "tool": "read_file",
                     "arguments": {}, "result": None,
                     "error": {"message": "file not found"}, "status": "failed"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "tool_result"
        assert "file not found" in r["text"]

    def test_item_completed_file_change_maps_tool_use(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i5", "type": "file_change",
                     "changes": [{"path": "src/main.py", "kind": "update"},
                                 {"path": "README.md", "kind": "add"}],
                     "status": "completed"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "tool_use"
        assert r["tool"] == "apply_patch"
        assert len(r["input"]["changes"]) == 2

    def test_item_completed_web_search_maps_text(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i6", "type": "web_search",
                     "query": "python asyncio docs"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "text"
        assert "python asyncio docs" in r["text"]

    def test_item_todo_list_skipped(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i7", "type": "todo_list", "items": []}
        })
        r = self.rt.parse_output(line)
        assert r is None

    def test_item_error_maps_error(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i8", "type": "error", "message": "tool crashed"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "error"
        assert "tool crashed" in r["message"]

    def test_unknown_event_maps_other(self):
        line = json.dumps({"type": "some_future_event"})
        r = self.rt.parse_output(line)
        assert r["type"] == "other"
        assert r["raw"] == "some_future_event"

    def test_unknown_item_type_maps_other(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i9", "type": "new_item_type"}
        })
        r = self.rt.parse_output(line)
        assert r["type"] == "other"

    def test_empty_agent_message_returns_none(self):
        line = json.dumps({
            "type": "item.completed",
            "item": {"id": "i10", "type": "agent_message", "text": ""}
        })
        r = self.rt.parse_output(line)
        assert r is None

    def test_non_dict_event_returns_none(self):
        assert self.rt.parse_output(json.dumps([1, 2, 3])) is None


class TestGetEnv:
    def test_env_contains_skills_dir(self):
        b = CodexRuntime()
        env = b.get_env()
        assert "DEVOPS_SKILLS_DIR" in env
        assert env["DEVOPS_EVENT_TYPE"] == ""

    def test_env_copies_os_environ(self):
        b = CodexRuntime()
        os.environ["TEST_CODEX_ENV_KEY"] = "testval"
        env = b.get_env()
        assert env.get("TEST_CODEX_ENV_KEY") == "testval"
        del os.environ["TEST_CODEX_ENV_KEY"]


class TestFullEventStream:
    """模拟完整 codex exec --json 事件流，验证端到端解析链路。"""

    def test_full_stream_parses_all_events(self):
        rt = CodexRuntime()
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "thread-xyz"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.started", "item": {"id": "i1", "type": "reasoning", "text": "planning..."}}),
            json.dumps({"type": "item.started", "item": {"id": "i2", "type": "command_execution",
                        "command": "echo hi", "aggregated_output": "", "exit_code": None, "status": "in_progress"}}),
            json.dumps({"type": "item.completed", "item": {"id": "i2", "type": "command_execution",
                        "command": "echo hi", "aggregated_output": "hi", "exit_code": 0, "status": "completed"}}),
            json.dumps({"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "Done!"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5,
                        "reasoning_output_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0}}),
        ]
        events = []
        for line in lines:
            parsed = rt.parse_output(line)
            if parsed:
                events.append(parsed)
        types = [e["type"] for e in events]
        assert "step_start" in types
        assert "reasoning" in types
        assert "tool_use" in types
        assert "tool_result" in types
        assert "text" in types
        assert "step_finish" in types
        finish_evt = next(e for e in events if e["type"] == "step_finish")
        assert finish_evt["tokens"] == 15
