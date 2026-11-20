"""Issue #2395：运行时四变量注入契约统一（各引擎路径）。

覆盖：
1. 各引擎 get_env() 不再预置 DEVOPS_EVENT_TYPE=""（空串会被误当事件类型）；
2. executor 统一装配函数以真实 event_type 覆盖，并补齐 ACCOUNT/GRAPE_ISSUE_ID/
   GRAPE_WORK_BRANCH（后者区分内部卡片 id 与仓库 Issue 编号）；
3. 缺失时输出可诊断告警（仅变量名，不含敏感值）且不阻断；
4. hermes/hwcloud/aishell ACP 与通用 CLI 路径均在 env_extra 合并后走同一装配。
"""
import os
import sys
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.agents import executor as ex  # noqa: E402

CONTRACT_KEYS = ("DEVOPS_EVENT_TYPE", "ACCOUNT", "GRAPE_ISSUE_ID", "GRAPE_WORK_BRANCH")


class TestAdaptersDoNotPresetEmptyEventType:
    def test_all_engine_env_adapters_do_not_preset_empty_event_type(self):
        from src.domains.runtime.engine.codex import CodexRuntime
        from src.domains.runtime.engine.opencode import OpencodeRuntime
        from src.domains.runtime.engine.dsh import DshRuntime
        from src.domains.runtime.engine.hermes import HermesRuntime
        from src.domains.runtime.engine.codearts import CodeArtsRuntime
        from src.domains.runtime.engine.atomcode import AtomCodeRuntime
        from src.domains.runtime.agents.aishell_acp import AishellDshACPBackend
        from src.domains.runtime.agents.hermes_acp import HermesACPBackend
        from src.domains.runtime.agents.hwcloud_acp import HwcloudACPBackend

        adapters = [
            CodexRuntime(), OpencodeRuntime(), DshRuntime(), HermesRuntime(),
            CodeArtsRuntime(), AtomCodeRuntime(), AishellDshACPBackend(),
            HermesACPBackend(), HwcloudACPBackend(),
        ]
        _saved = os.environ.pop("DEVOPS_EVENT_TYPE", None)
        try:
            for adapter in adapters:
                env = adapter.get_env()
                assert "DEVOPS_EVENT_TYPE" not in env, type(adapter).__name__
        finally:
            if _saved is not None:
                os.environ["DEVOPS_EVENT_TYPE"] = _saved


class TestApplyRuntimeEnvContract:
    def test_real_event_type_overrides_empty_value_from_env_extra(self):
        env = {"DEVOPS_EVENT_TYPE": ""}
        ex._apply_runtime_env_contract(env, event_type="a2a_event", account="XOcto",
                                       repo_key="gitcode:o/r", label="[Agent:coding] ")
        assert env["DEVOPS_EVENT_TYPE"] == "a2a_event"

    def test_explicit_account_wins_and_repo_shaped_account_is_rejected(self):
        env = {"ACCOUNT": "ExplicitUser"}
        ex._apply_runtime_env_contract(env, event_type="issue_opened", account="repoOwner",
                                       repo_key="o/r")
        assert env["ACCOUNT"] == "ExplicitUser"

        env2 = {}
        ex._apply_runtime_env_contract(env2, event_type="issue_opened",
                                       account="gitcode:owner/repo", repo_key="gitcode:owner/repo")
        assert env2.get("ACCOUNT", "") == ""

    def test_grape_issue_id_is_internal_card_id_not_repo_issue_number(self):
        env = {"ISSUE_NUMBER": "2395"}
        with mock.patch("src.domains.runtime.pipeline.event_pipeline.resolve_issue_card_id",
                        return_value="13465"):
            ex._apply_runtime_env_contract(env, event_type="issue_opened",
                                           repo_key="gitcode:o/r")
        assert env["ISSUE_NUMBER"] == "2395"
        assert env["GRAPE_ISSUE_ID"] == "13465"

    def test_work_branch_falls_back_to_scene(self):
        env = {"GRAPE_ISSUE_ID": "13465", "ISSUE_NUMBER": "2395"}
        with mock.patch("src.domains.runtime.engine.repogit._read_scene_work_branch",
                        return_value="arch-weekly-11-zj") as m_wb:
            ex._apply_runtime_env_contract(env, event_type="issue_opened",
                                           repo_key="gitcode:o/r")
        assert env["GRAPE_WORK_BRANCH"] == "arch-weekly-11-zj"
        # R1（PR #1460 审查）：场景回退按仓库 Issue 编号（issue_number）索引 issue 表，
        # 不得传内部卡片 id（GRAPE_ISSUE_ID）——那是 issue 表主键，命中恒错键。
        m_wb.assert_called_once_with("gitcode:o/r", "2395")

    def test_work_branch_fallback_never_passes_internal_card_id(self):
        """只有内部卡片 id、没有仓库 Issue 编号时，回退查询也不得把卡片 id 当 issue_number。"""
        env = {"GRAPE_ISSUE_ID": "13465"}
        with mock.patch("src.domains.runtime.engine.repogit._read_scene_work_branch",
                        return_value="") as m_wb:
            ex._apply_runtime_env_contract(env, event_type="issue_opened",
                                           repo_key="gitcode:o/r")
        assert "GRAPE_WORK_BRANCH" not in env
        for call in m_wb.call_args_list:
            assert "13465" not in repr(call)

    def test_missing_vars_warn_without_values_and_do_not_raise(self):
        env = {"SECRET_TOKEN": "super-secret-value"}
        with mock.patch.object(ex.log, "warning") as warn:
            ex._apply_runtime_env_contract(env, event_type="", account="",
                                           repo_key="", label="[Agent:qa] ",
                                           backend_name="opencode")
        assert warn.called
        text = " ".join(str(c.args[0]) for c in warn.call_args_list if c.args)
        for name in CONTRACT_KEYS:
            assert name in text
        assert "super-secret-value" not in text


class _FakeACP:
    name = "fake-acp"
    binary = "true"
    last_instance = None

    def __init__(self):
        type(self).last_instance = self
        self.captured = {}

    def get_env(self, account="", user_id=0):
        return {}

    def stream_execute(self, prompt, cwd, session_id="", env=None,
                       max_exec_seconds=600, agent_label=""):
        self.captured = dict(env or {})
        yield ("done", {"exit_code": 0, "text": "", "oc_session_id": "",
                        "errors": [], "stderr": ""})


class TestEnginePathsUseUnifiedContract:
    def _run_acp(self, module_name, fn_name, patch_target):
        import importlib
        mod = importlib.import_module(module_name)
        with mock.patch.object(mod, patch_target, _FakeACP):
            list(getattr(ex, fn_name)(
                "p", "/tmp", "", "a2a_event", None,
                "XOcto", 0, {"ACCOUNT": "XOcto"}, "", "",
                "", "", 60, repo_key="gitcode:o/r",
            ))
        return _FakeACP.last_instance.captured

    def test_hermes_acp_path_injects_real_event_type(self):
        env = self._run_acp("src.domains.runtime.agents.hermes_acp",
                            "_stream_hermes_acp", "HermesACPBackend")
        assert env["DEVOPS_EVENT_TYPE"] == "a2a_event"
        assert env["ACCOUNT"] == "XOcto"

    def test_hwcloud_acp_path_injects_real_event_type(self):
        env = self._run_acp("src.domains.runtime.agents.hwcloud_acp",
                            "_stream_hwcloud_acp", "HwcloudACPBackend")
        assert env["DEVOPS_EVENT_TYPE"] == "a2a_event"

    def test_aishell_acp_path_injects_real_event_type(self):
        env = self._run_acp("src.domains.runtime.agents.aishell_acp",
                            "_stream_aishell_dsh_acp", "AishellDshACPBackend")
        assert env["DEVOPS_EVENT_TYPE"] == "a2a_event"

    def test_generic_cli_path_injects_real_event_type(self):
        class _FakeStd:
            def readline(self):
                return b""

            def read(self):
                return b""

            def settimeout(self, *a):
                pass

        class _FakeProc:
            returncode = 0

            def __init__(self, captured):
                self._captured = captured
                self.stdout = _FakeStd()
                self.stderr = _FakeStd()
                self.stdin = None

            def poll(self):
                return 0

            def wait(self, *a, **kw):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

        captured = {}

        def _fake_popen(*args, **kwargs):
            captured.update(kwargs.get("env") or {})
            return _FakeProc(captured)

        class _FakeBackend:
            name = "fake"
            binary = "true"
            prompt_via_stdin = False

            def get_env(self, account="", user_id=0):
                return {"DEVOPS_EVENT_TYPE": ""}

            def build_args(self, prompt, session_id):
                return []

            def parse_output(self, line):
                return None

        with mock.patch.object(ex, "get_backend", return_value=_FakeBackend()), \
             mock.patch.object(ex.subprocess, "Popen", _fake_popen), \
             mock.patch("src.domains.runtime.engine.os_adapter.find_cli", return_value="/bin/true"), \
             mock.patch.object(ex, "_track_proc", lambda *a, **kw: None), \
             mock.patch.object(ex, "_untrack_proc", lambda *a, **kw: None), \
             mock.patch("src.domains.mgmt.user.db.get_credentials", return_value=[]):
            list(ex.stream_agent(
                "p", "/tmp", session_id="", event_type="a2a_event",
                backend_name="fake", account="XOcto", user_id=0,
                env_extra={"ACCOUNT": "XOcto"}, repo_key="gitcode:o/r",
                max_exec_seconds=60,
            ))
        assert captured["DEVOPS_EVENT_TYPE"] == "a2a_event"
        assert captured["ACCOUNT"] == "XOcto"


class TestConsumeStreamAsyncForwardsRepoKey:
    """R2（PR #1460 审查）：async 桥接层不得丢弃 repo_key。

    GRAPE_ISSUE_ID / GRAPE_WORK_BRANCH 的补齐都以 repo_key 为前置条件，桥接层丢参会让
    VOD direct 路径的整体解析静默跳过（与 vod_handler_async 注释承诺不符）。
    """

    def test_consume_stream_async_forwards_repo_key_to_stream_agent(self):
        import asyncio
        captured = {}

        def _fake_stream_agent(*args, **kwargs):
            captured.update(kwargs)
            yield ("done", {"exit_code": 0, "text": "", "oc_session_id": "s1"})

        async def _drive():
            events = []
            async for evt in ex.consume_stream_async(
                "p", "/tmp", "sid", "vod_signal",
                repo_key="gitcode:o/r", env_extra={"ACCOUNT": "XOcto"},
            ):
                events.append(evt)
            return events

        with mock.patch.object(ex, "stream_agent", _fake_stream_agent):
            events = asyncio.run(_drive())

        assert events[-1][0] == "done"
        assert captured["repo_key"] == "gitcode:o/r"
        assert captured["event_type"] == "vod_signal"
        assert captured["env_extra"] == {"ACCOUNT": "XOcto"}
