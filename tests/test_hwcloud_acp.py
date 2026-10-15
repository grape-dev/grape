"""hwcloud ACP 后端单测：事件解析 / 噪音过滤 / prompt 回调 / session 提取。

fixture 来自阶段 0 实测 JSON-RPC 行（docs/hwcloud-runtime-acp-design.md 实测行为），
不依赖真 hwcloud 二进制——回放原始行验证解析层。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.runtime.registry  # noqa: E402,F401 先触发（绕开 src.agent.__init__ 循环导入）
from src.agent.hwcloud_acp import (  # noqa: E402
    HwcloudACPBackend,
    _ACPSimpleClient,
    _build_task_settings,
    _normalize_update_type,
    _extract_session_id,
    _IGNORED_UPDATE_TYPES,
)


# ── 阶段 0 实测 fixture（截取真实响应行，脱敏）──

INIT_RESPONSE = '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,"sessionCapabilities":{"close":{},"delete":{},"list":{},"resume":{}}},"agentInfo":{"name":"hwcloud","version":"8b49d913"}}}'

SESSION_NEW_RESPONSE = '{"jsonrpc":"2.0","id":2,"result":{"_meta":{"created_at":"2026-10-12T08:20:11.683927538Z"},"sessionId":"acp_1791793211680273280_1","configOptions":[{"id":"mode","name":"Session Mode"}],"modes":{"currentModeId":"auto","availableModes":[{"id":"auto"}]}}}'

PROMPT_RESPONSE_END_TURN = '{"jsonrpc":"2.0","id":3,"result":{"_meta":{"mode":"auto","turn_count":1},"stopReason":"end_turn"}}'

PROMPT_RESPONSE_ERROR = '{"jsonrpc":"2.0","id":3,"error":{"code":-32603,"message":"session acp_nonexistent_999 not found"}}'

UPDATE_TEXT = '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"ok"}}}}'

UPDATE_THOUGHT = '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"thinking..."}}}}'

UPDATE_TOOL_CALL = '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"tool_call","toolCallId":"t1","title":"terminal","rawInput":{"command":"ls"}}}}'

UPDATE_TOOL_DONE = '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"tool_call_update","status":"completed","title":"terminal","rawOutput":"file.txt"}}}'

# 噪音事件（阶段 0 实测：新会话首轮必发，必须被忽略）
UPDATE_NOISES = [
    '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"available_commands_update","availableCommands":[{"name":"help"}]}}}',
    '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"available_skills_update","availableSkills":[{"name":"powerpoint"}]}}}',
    '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"mcp_servers_update"}}}',
    '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"session_info_update","title":"请求回复OK"}}}',
    '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"usage_update","cost":{"amount":0}}}}',
]

UPDATE_PLAN = '{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"plan_update","entries":[{"title":"分析代码"},{"title":"编写修复"}]}}}'


class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass


class _FakeStdout:
    pass


def _make_client(**kw):
    messages = []
    stdin = _FakeStdin()
    client = _ACPSimpleClient(stdin=stdin, stdout=_FakeStdout(), label="[test] ",
                              on_message=lambda t, c: messages.append((t, c)), **kw)
    return client, messages, stdin


# ══════════════════════════════════════════════
# 事件解析
# ══════════════════════════════════════════════

class TestNotificationParsing:
    def test_agent_message_chunk_maps_to_text(self):
        client, messages, _ = _make_client()
        client.handle_line(UPDATE_TEXT)
        assert messages == [("text", "ok")]

    def test_agent_thought_chunk_maps_to_thinking(self):
        client, messages, _ = _make_client()
        client.handle_line(UPDATE_THOUGHT)
        assert messages == [("thinking", "thinking...")]

    def test_tool_call_maps_to_tool_use(self):
        client, messages, _ = _make_client()
        client.handle_line(UPDATE_TOOL_CALL)
        assert messages == [("tool_use", {"tool": "terminal", "input": {"command": "ls"}})]

    def test_tool_call_update_completed_maps_to_finish_and_result(self):
        client, messages, _ = _make_client()
        client.handle_line(UPDATE_TOOL_DONE)
        assert ("tool_finish", "Tool terminal completed") in messages
        assert ("tool_result", "file.txt") in messages

    def test_noise_updates_are_ignored(self):
        """阶段 0 实测：available_*/mcp_servers/session_info/usage 不进事件流。"""
        client, messages, _ = _make_client()
        for line in UPDATE_NOISES:
            client.handle_line(line)
        assert messages == []

    def test_plan_update_maps_to_text(self):
        client, messages, _ = _make_client()
        client.handle_line(UPDATE_PLAN)
        assert len(messages) == 1
        et, data = messages[0]
        assert et == "text"
        assert "[plan]" in data and "分析代码" in data and "编写修复" in data

    def test_unknown_update_type_is_dropped(self):
        client, messages, _ = _make_client()
        client.handle_line('{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"mystery_update"}}}')
        assert messages == []

    def test_non_json_line_is_dropped(self):
        client, messages, _ = _make_client()
        client.handle_line("not json at all")
        assert messages == []

    def test_non_session_update_method_is_dropped(self):
        client, messages, _ = _make_client()
        client.handle_line('{"jsonrpc":"2.0","method":"log/entry","params":{"msg":"x"}}')
        assert messages == []


# ══════════════════════════════════════════════
# 响应路由 / prompt 回调
# ══════════════════════════════════════════════

class TestResponseRouting:
    def test_prompt_response_triggers_on_prompt_done(self):
        """PromptResponse(stopReason) 必须触发 on_prompt_done。

        回归守护：hermes_acp 的 _on_prompt_done 别名快照 bug（构造后再赋回调
        仍是 no-op，生产靠 60s 静默超时兜底）——本后端已改为实时读公开属性。
        """
        done = []
        client, _, _ = _make_client(on_prompt_done=lambda r: done.append(r))
        import threading
        client._pending[3] = threading.Event()  # 真实路径由 _send_prompt 注册
        client._prompt_req_id = 3
        client.handle_line(PROMPT_RESPONSE_END_TURN)
        assert done == [{"stopReason": "end_turn", "result": json.loads(PROMPT_RESPONSE_END_TURN)["result"]}]

    def test_late_bound_callback_is_honored(self):
        """构造后赋值 client.on_prompt_done 必须生效（hermes_acp 潜伏 bug 的反例）。"""
        done = []
        client, _, _ = _make_client()
        client.on_prompt_done = lambda r: done.append(r)   # 构造后再绑定
        import threading
        client._pending[3] = threading.Event()
        client._prompt_req_id = 3
        client.handle_line(PROMPT_RESPONSE_END_TURN)
        assert done and done[0]["stopReason"] == "end_turn"

    def test_non_prompt_response_does_not_trigger_callback(self):
        done = []
        client, _, _ = _make_client(on_prompt_done=lambda r: done.append(r))
        client._prompt_req_id = 99  # 响应 id=3 ≠ 99 → 非 prompt 响应
        client.handle_line(PROMPT_RESPONSE_END_TURN)
        assert done == []

    def test_prompt_error_detail_propagated_to_error_events(self):
        """seq#559 排障盲点回归守护：stopReason=error 时 PromptResponse 的
        error.message 必须进 error_events（否则 done.errors 空，服务端只见空壳）。

        此测试直接驱动 _ACPSimpleClient 回调 + 模拟收尾的收集逻辑。
        """
        done = []
        client, _, _ = _make_client(on_prompt_done=lambda r: done.append(r))
        import threading
        client._pending[3] = threading.Event()
        client._prompt_req_id = 3
        client.handle_line(PROMPT_RESPONSE_ERROR)
        assert done and done[0]["stopReason"] == "error"
        # 模拟 stream_execute 收尾的修复逻辑（stopReason=error + error_events 空 → 收集）
        prompt_result = done[0]
        error_events = []
        stop_reason = prompt_result.get("stopReason", "")
        if stop_reason == "error" and not error_events:
            _pr = prompt_result.get("result") or {}
            if isinstance(_pr, dict) and isinstance(_pr.get("error"), dict):
                _em = _pr["error"].get("message", str(_pr["error"]))[:300]
                error_events.append(f"prompt error: {_em}")
        assert error_events == ["prompt error: session acp_nonexistent_999 not found"]

    def test_prompt_error_response_triggers_error_stop(self):
        done = []
        client, _, _ = _make_client(on_prompt_done=lambda r: done.append(r))
        import threading
        client._pending[3] = threading.Event()
        client._prompt_req_id = 3
        client.handle_line(PROMPT_RESPONSE_ERROR)
        assert done and done[0]["stopReason"] == "error"

    def test_response_without_stopreason_treated_as_end_turn(self):
        done = []
        client, _, _ = _make_client(on_prompt_done=lambda r: done.append(r))
        import threading
        client._pending[3] = threading.Event()
        client._prompt_req_id = 3
        client.handle_line('{"jsonrpc":"2.0","id":3,"result":{"_meta":{"turn_count":2}}}')
        assert done and done[0]["stopReason"] == "end_turn"


# ══════════════════════════════════════════════
# request_permission 自动应答
# ══════════════════════════════════════════════

class TestPermissionAutoApprove:
    def _permission_request(self, options):
        return json.dumps({
            "jsonrpc": "2.0", "id": 7, "method": "session/request_permission",
            "params": {"sessionId": "s1", "options": options},
        })

    def test_allow_once_is_selected(self):
        client, _, stdin = _make_client()
        client.handle_line(self._permission_request([
            {"optionId": "allow_once", "kind": "allow_once"},
            {"optionId": "allow_always", "kind": "allow_always"},
            {"optionId": "reject_once", "kind": "reject_once"},
        ]))
        resp = json.loads(stdin.written[0])
        assert resp["id"] == 7
        assert resp["result"]["outcome"]["optionId"] == "allow_once"

    def test_fallback_first_allow_when_no_allow_once(self):
        client, _, stdin = _make_client()
        client.handle_line(self._permission_request([
            {"optionId": "allow_session", "kind": "allow_always"},
            {"optionId": "reject_once", "kind": "reject_once"},
        ]))
        resp = json.loads(stdin.written[0])
        assert resp["result"]["outcome"]["optionId"] == "allow_session"

    def test_fs_read_text_file_served_from_disk(self, tmp_path):
        """Agent→Client fs/read_text_file：initialize 声明能力后 agent 可读客户端文件。"""
        f = tmp_path / "hello.txt"
        f.write_text("hello grape", encoding="utf-8")
        client, _, _ = _make_client()
        client.handle_line(json.dumps({
            "jsonrpc": "2.0", "id": 9, "method": "fs/read_text_file",
            "params": {"sessionId": "s1", "path": str(f)},
        }))
        resp = json.loads(client.stdin.written[0])
        assert resp["id"] == 9
        assert resp["result"]["content"] == "hello grape"

    def test_fs_write_text_file_writes_disk(self, tmp_path):
        """Agent→Client fs/write_text_file：内容落盘且返回空 result。"""
        target = tmp_path / "sub" / "out.txt"
        client, _, _ = _make_client()
        client.handle_line(json.dumps({
            "jsonrpc": "2.0", "id": 10, "method": "fs/write_text_file",
            "params": {"sessionId": "s1", "path": str(target), "content": "written"},
        }))
        resp = json.loads(client.stdin.written[0])
        assert resp["id"] == 10
        assert resp["result"] == {}
        assert target.read_text(encoding="utf-8") == "written"

    def test_fs_read_missing_file_returns_error(self, tmp_path):
        client, _, _ = _make_client()
        client.handle_line(json.dumps({
            "jsonrpc": "2.0", "id": 11, "method": "fs/read_text_file",
            "params": {"sessionId": "s1", "path": str(tmp_path / "nope.txt")},
        }))
        resp = json.loads(client.stdin.written[0])
        assert resp["error"]["code"] == -32000

    def test_initialize_declares_fs_capabilities(self):
        """initialize 必须声明 fs read/write（否则 agent 侧工具被 gate 裁掉）。"""
        import inspect
        import src.runtime.registry  # noqa: F401
        from src.agent import hwcloud_acp as mod
        src_text = inspect.getsource(mod)
        assert '"fs": {"readTextFile": True, "writeTextFile": True}' in src_text

    def test_unknown_agent_request_gets_method_not_found(self):
        client, _, stdin = _make_client()
        client.handle_line('{"jsonrpc":"2.0","id":8,"method":"session/whatever"}')
        resp = json.loads(stdin.written[0])
        assert resp["error"]["code"] == -32601


# ══════════════════════════════════════════════
# request()（同步请求-响应）
# ══════════════════════════════════════════════

class TestRequest:
    def test_request_returns_result_when_response_arrives(self):
        client, _, _ = _make_client()

        def _respond():
            import threading
            client.handle_line(INIT_RESPONSE)

        import threading
        threading.Timer(0.05, _respond).start()
        result, err = client.request("initialize", {"protocolVersion": 1}, timeout=5)
        assert err is None
        assert result is not None and result["agentInfo"]["name"] == "hwcloud"

    def test_request_timeout_returns_error(self):
        client, _, _ = _make_client()
        result, err = client.request("initialize", {}, timeout=0.2)
        assert result is None
        assert err is not None and "timeout" in err


# ══════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════

class TestHelpers:
    def test_extract_session_id_top_level(self):
        assert _extract_session_id(json.loads(SESSION_NEW_RESPONSE)["result"]) == "acp_1791793211680273280_1"

    def test_extract_session_id_empty(self):
        assert _extract_session_id(None) == ""
        assert _extract_session_id({}) == ""
        assert _extract_session_id({"other": 1}) == ""

    @pytest.mark.parametrize("raw,expected", [
        ("agent_message_chunk", "agent_message_chunk"),
        ("AGENT_MESSAGE_CHUNK", "agent_message_chunk"),  # 大小写不敏感（先 lower）
        ("tool_call", "tool_call"),
        ("tool_call_update", "tool_call_update"),
        ("plan_update", "plan_update"),
        ("usage_update", "usage_update"),
        ("available_commands_update", ""),  # 不在 _UPDATE_TYPE_MAP（依赖 _IGNORED 集合前过滤）
        ("whatever", ""),
    ])
    def test_normalize_update_type(self, raw, expected):
        assert _normalize_update_type(raw) == expected

    def test_ignored_types_cover_stage0_noise(self):
        """噪音集合必须覆盖阶段 0 实测全部噪音事件。"""
        for noise in UPDATE_NOISES:
            update = json.loads(noise)["params"]["update"]
            assert update["sessionUpdate"] in _IGNORED_UPDATE_TYPES, \
                f"未覆盖噪音类型: {update['sessionUpdate']}"


# ══════════════════════════════════════════════
# per-task settings.json
# ══════════════════════════════════════════════

class TestBuildTaskSettings:
    def _clear_env(self):
        for k in ("HWCLOUD_PROVIDER_BASE_URL", "HWCLOUD_PROVIDER_API_KEY", "HWCLOUD_PROVIDER_MODELS"):
            os.environ.pop(k, None)

    def test_provider_env_written_and_mode_auto(self, tmp_path):
        self._clear_env()
        os.environ["HWCLOUD_PROVIDER_BASE_URL"] = "https://opengw.example.com/v1"
        os.environ["HWCLOUD_PROVIDER_API_KEY"] = "sk-test"
        os.environ["HWCLOUD_PROVIDER_MODELS"] = "model-a, model-b"
        try:
            path = _build_task_settings(str(tmp_path))
            cfg = json.load(open(path))
            assert cfg["default_mode"] == "auto"  # 免审（阶段 0 实测确认）
            p = cfg["provider"]["opengw"]
            assert p["base_url"] == "https://opengw.example.com/v1"
            assert p["api_key"] == "sk-test"
            assert p["models"] == ["model-a", "model-b"]
        finally:
            self._clear_env()

    def test_no_provider_env_keeps_settings_minimal(self, tmp_path):
        """语义随 seq#561 修复更新：无任何 provider → 空串（不写空配置文件）。"""
        self._clear_env()
        try:
            path = _build_task_settings(str(tmp_path))
            assert path == ""
        finally:
            self._clear_env()

    def test_no_provider_returns_empty_no_relocation(self, tmp_path, monkeypatch):
        """seq#561 修复守护：env 无注入 + 全局 settings.json 不存在 → 返回空串
        （调用方不设 OPENAGENT_CLI_CONFIG，hwcloud 用设备默认配置链）。"""
        self._clear_env()
        monkeypatch.setenv("HOME", str(tmp_path))  # 无 ~/.hwcloud/settings.json
        try:
            path = _build_task_settings(str(tmp_path / "iso"))
            assert path == ""
            # 且不产生任何文件（不建目录不写 settings）
            assert not (tmp_path / "iso").exists()
        finally:
            self._clear_env()

    def test_global_settings_fallback_still_relocates(self, tmp_path, monkeypatch):
        """全局 ~/.hwcloud/settings.json 存在（213 场景）→ 仍生成 per-task 配置。"""
        self._clear_env()
        fake_home = tmp_path / "home"
        (fake_home / ".hwcloud").mkdir(parents=True)
        (fake_home / ".hwcloud" / "settings.json").write_text(
            json.dumps({"provider": {"opengw": {"base_url": "https://x", "api_key": "k",
                                                "models": ["m"]}}}), encoding="utf-8")
        monkeypatch.setenv("HOME", str(fake_home))
        try:
            path = _build_task_settings(str(tmp_path / "iso"))
            assert path.endswith("settings.json")
            cfg = json.load(open(path))
            assert cfg["provider"]["opengw"]["api_key"] == "k"
            assert cfg["default_mode"] == "auto"
        finally:
            self._clear_env()

    def test_file_permissions_0600(self, tmp_path):
        """settings.json 含明文 api_key，必须 0600（验收标准）。"""
        self._clear_env()
        os.environ["HWCLOUD_PROVIDER_API_KEY"] = "sk-x"
        try:
            path = _build_task_settings(str(tmp_path))
            assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        finally:
            self._clear_env()


# ══════════════════════════════════════════════
# 后端元数据 / 注册
# ══════════════════════════════════════════════

class TestAIShellDisplayRename:
    """hwcloud → AIShell 展示名改名守护（2026-10-13）：
    内部标识（name/binary/路由键）不动，仅 instance_name 派生前缀与 display_name 变。"""

    def test_instance_name_uses_display_name(self):
        from src.runtime.instances import instance_name
        assert instance_name("hwcloud", "worker-dev-0006", 6) == "aishell-worker-dev-0006"
        # 其他引擎不受影响
        assert instance_name("opencode", "dev-0005", 0) == "opencode-dev-0005"
        assert instance_name("hermes", "dev-0005", 0) == "hermes-dev-0005"

    def test_engine_display_name_mapping(self):
        from src.runtime.instances import engine_display_name
        assert engine_display_name("hwcloud") == "aishell"
        assert engine_display_name("HWCLOUD") == "aishell"  # 大小写不敏感
        assert engine_display_name("opencode") == "opencode"  # 未映射原样

    def test_resolve_accepts_old_and_new_names(self):
        """双名兼容：旧绑定 hwcloud-worker-* 与新 aishell-worker-* 都能解析到同一行。

        用 monkeypatch 模拟 DB 行（name=hwcloud）验证 resolve_instance_id 两个名字都命中。
        """
        import src.runtime.instances as inst
        rows = [(271, "hwcloud", 31, "worker-2b8e99ce470a455597b5b1dafb0bcf7b", "active")]

        class _FakeCursor:
            def execute(self, *a, **kw):
                pass
            def fetchall(self):
                return rows

        class _FakeDB:
            def cursor(self):
                return _FakeCursor()

        class _FakeLock:
            def __enter__(self):
                return None
            def __exit__(self, *a):
                return False

        _orig_lock, _orig_db = inst._lock, inst._db
        inst._lock = lambda: _FakeLock()
        inst._db = lambda: _FakeDB()
        try:
            # 新名（aishell-worker-2b8e99ce470a4-f7b）
            assert inst.resolve_instance_id("aishell-worker-2b8e99ce470a4-f7b") == 271
            # 旧名（hwcloud-worker-2b8e99ce470a4-f7b）——历史绑定不断链
            assert inst.resolve_instance_id("hwcloud-worker-2b8e99ce470a4-f7b") == 271
        finally:
            inst._lock, inst._db = _orig_lock, _orig_db

    def test_backend_for_engine_accepts_aishell_prefix(self):
        """seq#552 同款坑守护：instance_name 变 aishell-worker-* 后，
        _backend_for_engine 仍路由到 hwcloud 后端（不能掉默认 opencode）。"""
        from src.runtime.agent import _backend_for_engine
        assert _backend_for_engine("aishell-worker-ecs-grape-de-0006") == "hwcloud"
        assert _backend_for_engine("hwcloud-worker-ecs-grape-de-0006") == "hwcloud"  # 旧名也认
        assert _backend_for_engine("opencode-worker-x") == "opencode"


class TestBackendRegistration:
    def test_backend_metadata(self):
        b = HwcloudACPBackend()
        assert b.name == "hwcloud"
        assert b.binary == "hwcloud"
        assert b.prompt_via_stdin is False

    def test_registered_in_backend_dict(self):
        import src.runtime.registry as r
        assert "hwcloud" in r._RUNTIMES
        assert r.get_runtime("hwcloud").name == "hwcloud"

    def test_discover_reports_installed_state(self):
        from src.agent.executor import discover_backends
        hw = [x for x in discover_backends() if x["name"] == "hwcloud"]
        assert len(hw) == 1
        # installed 状态与 PATH 中是否有 hwcloud 一致（不硬编码 True）
        import shutil
        assert hw[0]["installed"] == bool(shutil.which("hwcloud"))

    def test_cli_not_found_yields_done_error(self):
        b = HwcloudACPBackend()
        b.binary = "hwcloud-definitely-not-exist-xyz"
        events = list(b.stream_execute("hi", cwd="/tmp", env={}, max_exec_seconds=5))
        done = [d for et, d in events if et == "done"][0]
        assert done["exit_code"] == -1
        assert "not found" in done["errors"][0]
