"""Issue #1858：任务环境按服务注入 VCS / Issue / A2A 基址的回归测试。

覆盖：
- 显式 `task_<service>_api_url` 优先，来源可追溯；
- 可选聚合网关按 `/vcs` `/issue` `/a2a` 前缀路由；
- 未配置时 vcs/issue 回退业务基址、a2a 回退回连基址（不互冒充）；
- `resolve_task_server_env` 注入三面基址 + `GRAPE_BASE_URL_SOURCES`；
- `build_task_env` / worker env 重拉 / direct 执行同源；
- 本机 worker 覆盖策略（单地址跟随、显式分服务保留）；
- 404/405 候选 base_url 诊断文案。
"""
import json
import os
from contextlib import nullcontext
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BIZ = "https://api.grape.example.com"
RT = "https://worker.grape.example.com"
GW = "https://gw.grape.example.com"


def _patch_bases():
    return patch.multiple(
        "src.prompt_builder",
        _resolve_business_base=lambda runtime_id=0: (BIZ, "server_base_url"),
        _resolve_server_url=lambda runtime_id=0: RT,
    )


class TestExplicitServiceBases:
    def test_explicit_service_urls_win_and_sources_traceable(self):
        from src import prompt_builder
        mapping = {
            "vcs": "https://vcs.example.com:8081",
            "issue": "https://issue.example.com:8081",
            "a2a": "https://a2a.example.com:8082",
        }
        with _patch_bases(), \
             patch("src.user.db.get_task_service_api_url_explicit",
                   side_effect=lambda svc: mapping.get(svc, "")), \
             patch("src.user.db.get_task_gateway_base_url", return_value=""):
            env = prompt_builder.resolve_service_base_env(7)
        assert env["GRAPE_VCS_BASE_URL"] == mapping["vcs"]
        assert env["GRAPE_ISSUE_BASE_URL"] == mapping["issue"]
        assert env["GRAPE_A2A_BASE_URL"] == mapping["a2a"]
        sources = json.loads(env["GRAPE_BASE_URL_SOURCES"])
        assert sources["vcs"] == "explicit_vcs_api_url"
        assert sources["issue"] == "explicit_issue_api_url"
        assert sources["a2a"] == "explicit_a2a_api_url"

    def test_gateway_prefix_routing(self):
        from src import prompt_builder
        with _patch_bases(), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=GW):
            env = prompt_builder.resolve_service_base_env(7)
        assert env["GRAPE_VCS_BASE_URL"] == GW + "/vcs"
        assert env["GRAPE_ISSUE_BASE_URL"] == GW + "/issue"
        assert env["GRAPE_A2A_BASE_URL"] == GW + "/a2a"
        assert env["GRAPE_GATEWAY_BASE_URL"] == GW
        sources = json.loads(env["GRAPE_BASE_URL_SOURCES"])
        assert sources["vcs"] == "gateway_prefix_vcs"
        assert sources["a2a"] == "gateway_prefix_a2a"

    def test_fallback_keeps_business_and_control_plane_separate(self):
        from src import prompt_builder
        with _patch_bases(), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=""):
            env = prompt_builder.resolve_service_base_env(7)
        assert env["GRAPE_VCS_BASE_URL"] == BIZ
        assert env["GRAPE_ISSUE_BASE_URL"] == BIZ
        # A2A 回退回连基址（控制面），不是业务基址
        assert env["GRAPE_A2A_BASE_URL"] == RT
        sources = json.loads(env["GRAPE_BASE_URL_SOURCES"])
        assert sources["vcs"].startswith("business_api_fallback")
        assert sources["a2a"] == "runtime_server_url_fallback"


class TestResolveTaskServerEnv:
    def test_injects_three_service_bases_with_source_map(self):
        from src import prompt_builder
        with _patch_bases(), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=""):
            env = prompt_builder.resolve_task_server_env(7)
        assert env["GRAPE_SERVER_URL"] == BIZ
        assert env["RUNTIME_SERVER_URL"] == RT
        assert env["GRAPE_VCS_BASE_URL"] == BIZ
        assert env["GRAPE_ISSUE_BASE_URL"] == BIZ
        assert env["GRAPE_A2A_BASE_URL"] == RT
        assert "vcs" in json.loads(env["GRAPE_BASE_URL_SOURCES"])

    def test_build_task_env_carries_three_bases(self):
        from src.api.event_pipeline import build_task_env
        with _patch_bases(), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=""), \
             patch("src.api.event_pipeline._inject_scene_env"), \
             patch("src.api.event_pipeline._inject_bot_agent_env"), \
             patch("src.api.event_pipeline._read_scene_linked_repos", return_value=[]), \
             patch("src.runtime.repogit._read_scene_work_branch", return_value=""):
            env = build_task_env("gitcode:grape-dev/grape", "coding", "1858", runtime_id=7)
        assert env["GRAPE_VCS_BASE_URL"] == BIZ
        assert env["GRAPE_ISSUE_BASE_URL"] == BIZ
        assert env["GRAPE_A2A_BASE_URL"] == RT

    def test_worker_env_refresh_injects_three_bases(self):
        """worker 执行前重拉 env：按任务 runtime 下发三面基址（覆盖旧快照）。"""
        from src.api.routes.runtime import runtime_task_env

        class _Cur:
            def execute(self, *_a, **_k):
                return None

            def fetchone(self):
                return (0, "", 7)

        class _Db:
            def cursor(self):
                return _Cur()

        with patch("src.session_manager.db", _Db()), \
             patch("src.session_manager.db_lock", nullcontext()), \
             patch("src.runtime.service._collect_server_env", return_value={}), \
             patch("src.runtime.service.sync_task_session_token", return_value=""), \
             patch("src.api.event_pipeline.reconcile_task_scene_env"), \
             patch.multiple("src.prompt_builder",
                            _resolve_business_base=lambda runtime_id=0: (BIZ, "server_base_url"),
                            _resolve_server_url=lambda runtime_id=0: RT), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=""):
            got = runtime_task_env("task-1858", credential_id=1)
        assert got["env"]["GRAPE_VCS_BASE_URL"] == BIZ
        assert got["env"]["GRAPE_A2A_BASE_URL"] == RT

    def test_direct_executor_uses_unified_task_env(self):
        """direct 执行路径复用 resolve_task_server_env（与 enqueue/重派同源）。"""
        import inspect
        from src.runtime import direct_executor
        src = inspect.getsource(direct_executor)
        assert "resolve_task_server_env(runtime_id)" in src

    def test_legacy_snapshot_env_backfill(self):
        """旧快照缺失三面基址时，executor 仅补缺失键（A2A 用回连基址）。"""
        from src.agent.executor import _assemble_env
        env = {"GRAPE_SERVER_URL": BIZ, "RUNTIME_SERVER_URL": RT}
        _assemble_env(env, kind="devops", label="t")
        assert env["GRAPE_VCS_BASE_URL"] == BIZ
        assert env["GRAPE_ISSUE_BASE_URL"] == BIZ
        assert env["GRAPE_A2A_BASE_URL"] == RT


class TestLocalWorkerOverride:
    def test_single_address_overrides_service_bases(self):
        from src.runtime.agent import _apply_local_worker_base_override
        env = {
            "GRAPE_SERVER_URL": "https://same.example.com",
            "RUNTIME_SERVER_URL": "https://same.example.com",
            "GRAPE_VCS_BASE_URL": "https://same.example.com",
            "GRAPE_ISSUE_BASE_URL": "https://same.example.com",
            "GRAPE_A2A_BASE_URL": "https://same.example.com",
            "GRAPE_BASE_URL_SOURCES": "{}",
        }
        _apply_local_worker_base_override(
            env, {"server_url": "https://same.example.com"}, "http://127.0.0.1:8083")
        assert env["GRAPE_VCS_BASE_URL"] == "http://127.0.0.1:8083"
        assert env["GRAPE_ISSUE_BASE_URL"] == "http://127.0.0.1:8083"
        assert env["GRAPE_A2A_BASE_URL"] == "http://127.0.0.1:8083"
        sources = json.loads(env["GRAPE_BASE_URL_SOURCES"])
        assert sources["a2a"] == "local_worker_override"

    def test_split_service_bases_are_preserved(self):
        from src.runtime.agent import _apply_local_worker_base_override
        env = {
            "GRAPE_SERVER_URL": "https://biz.example.com:8081",
            "RUNTIME_SERVER_URL": "https://rt.example.com:8083",
            "GRAPE_VCS_BASE_URL": "https://vcs.example.com:8081",
            "GRAPE_ISSUE_BASE_URL": "https://issue.example.com:8081",
            "GRAPE_A2A_BASE_URL": "https://a2a.example.com:8082",
        }
        task = {"server_url": "https://rt.example.com:8083"}
        _apply_local_worker_base_override(env, task, "http://127.0.0.1:8083")
        assert env["GRAPE_VCS_BASE_URL"] == "https://vcs.example.com:8081"
        assert env["GRAPE_ISSUE_BASE_URL"] == "https://issue.example.com:8081"
        assert env["GRAPE_A2A_BASE_URL"] == "https://a2a.example.com:8082"
        assert env["RUNTIME_SERVER_URL"] == "http://127.0.0.1:8083"

    def test_same_host_different_port_vcs_issue_preserved(self):
        """分端口部署（业务 8081 / runtime 8083，同 host）不得把 VCS/Issue 改成 8083。"""
        from src.runtime.agent import _apply_local_worker_base_override
        env = {
            "GRAPE_SERVER_URL": "http://127.0.0.1:8081",
            "RUNTIME_SERVER_URL": "http://127.0.0.1:8083",
            "GRAPE_VCS_BASE_URL": "http://127.0.0.1:8081",
            "GRAPE_ISSUE_BASE_URL": "http://127.0.0.1:8081",
            "GRAPE_A2A_BASE_URL": "http://127.0.0.1:8083",
        }
        task = {"server_url": "http://127.0.0.1:8083"}
        _apply_local_worker_base_override(env, task, "http://127.0.0.1:8083")
        assert env["GRAPE_SERVER_URL"] == "http://127.0.0.1:8081"
        assert env["GRAPE_VCS_BASE_URL"] == "http://127.0.0.1:8081"
        assert env["GRAPE_ISSUE_BASE_URL"] == "http://127.0.0.1:8081"
        # A2A 仍跟随 runtime 直连
        assert env["GRAPE_A2A_BASE_URL"] == "http://127.0.0.1:8083"
        assert env["RUNTIME_SERVER_URL"] == "http://127.0.0.1:8083"

    def test_missing_service_bases_follow_effective_business_base(self):
        """缺失三面基址时，VCS/Issue 回退生效业务基址，A2A 回退 runtime 直连。"""
        from src.runtime.agent import _apply_local_worker_base_override
        env = {"GRAPE_SERVER_URL": "https://biz.example.com:8081"}
        _apply_local_worker_base_override(
            env, {"server_url": "https://rt.example.com:8083"}, "http://127.0.0.1:8083")
        assert env["GRAPE_VCS_BASE_URL"] == "https://biz.example.com:8081"
        assert env["GRAPE_ISSUE_BASE_URL"] == "https://biz.example.com:8081"
        assert env["GRAPE_A2A_BASE_URL"] == "http://127.0.0.1:8083"


class TestServiceDiagnosticsModule:
    def test_a2a_base_skips_business_probe(self):
        """公开 resolve_a2a_base 不得为取 A2A 基址触发业务 API 探测。"""
        from src import prompt_builder
        with patch("src.prompt_builder._resolve_business_base",
                   side_effect=AssertionError("business probe must not run")), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=""), \
             patch("src.prompt_builder._resolve_server_url", return_value=RT):
            assert prompt_builder.resolve_a2a_base(7) == RT

    def test_a2a_base_prefers_gateway_prefix(self):
        from src import prompt_builder
        with patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=GW):
            assert prompt_builder.resolve_a2a_base(7) == GW + "/a2a"

    def test_gateway_config_read_once_per_env_build(self):
        """_build_service_base_env 只解析一次聚合网关基址。"""
        from src import prompt_builder
        with _patch_bases(), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=GW) as gw:
            prompt_builder.resolve_service_base_env(7)
        assert gw.call_count == 1

    def test_candidates_live_in_shared_diagnostics_module(self):
        from src.service_diagnostics import (
            format_base_url_candidates,
            service_for_endpoint,
        )
        assert callable(format_base_url_candidates)
        assert service_for_endpoint("/api/a2a/status") == "a2a"


class TestFailureDiagnostics:
    def test_a2a_endpoint_candidates(self):
        from src import prompt_builder
        with _patch_bases(), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=""):
            text = prompt_builder.format_base_url_candidates("/api/a2a/status")
        assert "候选 base_url" in text
        assert "GRAPE_A2A_BASE_URL=" + RT in text
        assert "非 0 退出码" in text
        assert "GRAPE_VCS_BASE_URL" not in text

    def test_vcs_endpoint_candidates(self):
        from src import prompt_builder
        with _patch_bases(), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=""):
            text = prompt_builder.format_base_url_candidates("/api/git-repo/pulls")
        assert "GRAPE_VCS_BASE_URL=" + BIZ in text

    def test_unknown_endpoint_lists_all_candidates(self):
        from src import prompt_builder
        with _patch_bases(), \
             patch("src.user.db.get_task_service_api_url_explicit", return_value=""), \
             patch("src.user.db.get_task_gateway_base_url", return_value=""):
            text = prompt_builder.format_base_url_candidates("/api/unknown")
        assert "GRAPE_VCS_BASE_URL" in text
        assert "GRAPE_ISSUE_BASE_URL" in text
        assert "GRAPE_A2A_BASE_URL" in text

    def test_service_endpoint_mapping(self):
        from src.prompt_builder import service_for_endpoint
        assert service_for_endpoint("/api/a2a") == "a2a"
        assert service_for_endpoint("/api/a2a/status") == "a2a"
        assert service_for_endpoint("/api/git-repo/issues/get") == "vcs"
        assert service_for_endpoint("/api/issues/1858") == "issue"
        assert service_for_endpoint("/api/runtime/tasks") == ""
