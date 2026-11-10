"""Issue #1577：任务派发 env 语义拆分测试。

`GRAPE_SERVER_URL` 承载主控业务 API 基址（issues / git-repo 等全量端点），
worker runtime / A2A 回连基址由 `RUNTIME_SERVER_URL` 独立承载。
本机/内网 worker 覆盖只改 runtime 基址，不再覆盖业务基址；单地址部署回退等价旧行为。
"""
import os
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestBusinessApiUrlGetter:
    def test_from_server_config_explicit(self):
        from src.user.db import get_business_api_url
        with patch("src.config.read_server_config",
                   return_value={"task_business_api_url": "https://api.grape.example.com"}), \
             patch("src.user.db.get_server_base_url", return_value="https://grape.example.com"):
            assert get_business_api_url() == "https://api.grape.example.com"

    def test_from_meta(self):
        from src.user.db import get_business_api_url
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value",
                   side_effect=lambda k: {"task_business_api_url": "https://biz.internal"}.get(k, "")), \
             patch("src.user.db.get_server_base_url", return_value="https://grape.example.com"):
            assert get_business_api_url() == "https://biz.internal"

    def test_fallback_to_server_base_url_when_unconfigured(self):
        from src.user.db import get_business_api_url
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db.get_server_base_url", return_value="https://grape.example.com"):
            assert get_business_api_url() == "https://grape.example.com"

    def test_trailing_slash_stripped(self):
        from src.user.db import get_business_api_url
        with patch("src.config.read_server_config",
                   return_value={"task_business_api_url": "https://api.grape.example.com/"}):
            assert get_business_api_url() == "https://api.grape.example.com"


class TestResolveBusinessServerUrl:
    def test_remote_runtime_uses_business_base(self):
        from src import prompt_builder
        with patch("src.user.db.get_business_api_url_explicit", return_value=""), \
             patch("src.user.db.get_business_api_url",
                   return_value="https://api.grape.example.com"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=False):
            assert prompt_builder._resolve_business_server_url(7) == "https://api.grape.example.com"

    def test_local_runtime_uses_localhost(self):
        from src import prompt_builder
        with patch("src.user.db.get_business_api_url_explicit", return_value=""), \
             patch("src.user.db.get_business_api_url",
                   return_value="https://api.grape.example.com"), \
             patch("src.user.db.get_business_api_port", return_value=""), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=True), \
             patch.object(prompt_builder, "_local_port", return_value="3456"), \
             patch.object(prompt_builder, "_probe_business_api", return_value=True):
            assert prompt_builder._resolve_business_server_url(3) == "http://127.0.0.1:3456"

    def test_local_runtime_respects_explicit_business_base(self):
        """Spec 优先级：显式 task_business_api_url > server_base_url > 本机 127.0.0.1。"""
        from src import prompt_builder
        with patch("src.user.db.get_business_api_url_explicit",
                   return_value="https://api.grape.example.com"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=True), \
             patch.object(prompt_builder, "_local_port", return_value="3456"):
            assert prompt_builder._resolve_business_server_url(3) == "https://api.grape.example.com"

    def test_runtime_lookup_error_keeps_localhost_fallback(self):
        """DB 查询异常（未知）保留改造前 localhost 回退，不等同于「非本地」。"""
        from src import prompt_builder
        with patch.object(prompt_builder, "_runtime_is_local", return_value=None), \
             patch.object(prompt_builder, "_local_port", return_value="3456"), \
             patch("src.user.db.get_worker_connect_url",
                   return_value="https://worker.grape.example.com"):
            assert prompt_builder._resolve_server_url(5) == "http://localhost:3456"

    def test_resolve_task_server_env_returns_both_bases(self):
        from src import prompt_builder
        with patch.object(prompt_builder, "_resolve_business_server_url",
                          return_value="https://api.grape.example.com"), \
             patch.object(prompt_builder, "_resolve_server_url",
                          return_value="https://worker.grape.example.com"):
            env = prompt_builder.resolve_task_server_env(7)
        assert env == {"GRAPE_SERVER_URL": "https://api.grape.example.com",
                       "RUNTIME_SERVER_URL": "https://worker.grape.example.com"}

    def test_runtime_resolver_keeps_worker_connect_semantics(self):
        from src import prompt_builder
        with patch("src.user.db.get_worker_connect_url",
                   return_value="https://worker.grape.example.com"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=False):
            assert prompt_builder._resolve_server_url(7) == "https://worker.grape.example.com"

    def test_single_address_equivalence(self):
        """单地址部署（未配拆分项）：业务基址与 runtime 基址取值等价旧行为。"""
        from src import prompt_builder
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db.get_server_base_url", return_value="https://grape.example.com"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=False):
            assert prompt_builder._resolve_business_server_url(9) == "https://grape.example.com"
            assert prompt_builder._resolve_server_url(9) == "https://grape.example.com"


class TestBuildTaskEnv:
    def _build(self, runtime_id=0):
        from src.api.event_pipeline import build_task_env
        with patch("src.prompt_builder._resolve_business_server_url",
                   return_value="https://api.grape.example.com"), \
             patch("src.prompt_builder._resolve_server_url",
                   return_value="https://worker.grape.example.com"), \
             patch("src.api.event_pipeline._inject_scene_env"), \
             patch("src.api.event_pipeline._inject_bot_agent_env"), \
             patch("src.api.event_pipeline._read_scene_linked_repos", return_value=[]), \
             patch("src.runtime.repogit._read_scene_work_branch", return_value=""):
            return build_task_env("gitcode:grape-dev/grape", "coding", "123",
                                  runtime_id=runtime_id)

    def test_dual_address_env_injects_both_bases(self):
        env = self._build()
        assert env["GRAPE_SERVER_URL"] == "https://api.grape.example.com"
        assert env["RUNTIME_SERVER_URL"] == "https://worker.grape.example.com"


class TestLocalWorkerOverride:
    def test_keeps_business_base_and_overrides_runtime(self):
        from src.runtime.agent import _apply_local_worker_base_override
        env = {"GRAPE_SERVER_URL": "https://api.grape.example.com"}
        task = {"server_url": "https://old.example.com"}
        _apply_local_worker_base_override(env, task, "http://127.0.0.1:3000")
        assert env["GRAPE_SERVER_URL"] == "https://api.grape.example.com"
        assert env["RUNTIME_SERVER_URL"] == "http://127.0.0.1:3000"
        assert task["server_url"] == "http://127.0.0.1:3000"

    def test_fallback_when_business_base_missing(self):
        from src.runtime.agent import _apply_local_worker_base_override
        env = {}
        task = {}
        _apply_local_worker_base_override(env, task, "http://127.0.0.1:3000")
        assert env["GRAPE_SERVER_URL"] == "http://127.0.0.1:3000"
        assert env["RUNTIME_SERVER_URL"] == "http://127.0.0.1:3000"
        assert task["server_url"] == "http://127.0.0.1:3000"

    def test_single_address_overrides_business_base(self):
        """单地址部署（业务基址与回连基址同主机）：业务基址仍覆盖为本机直连。"""
        from src.runtime.agent import _apply_local_worker_base_override
        env = {"GRAPE_SERVER_URL": "https://grape.example.com", "RUNTIME_SERVER_URL": "https://grape.example.com"}
        task = {"server_url": "https://grape.example.com"}
        _apply_local_worker_base_override(env, task, "http://127.0.0.1:3000")
        assert env["GRAPE_SERVER_URL"] == "http://127.0.0.1:3000"
        assert env["RUNTIME_SERVER_URL"] == "http://127.0.0.1:3000"

    def test_dual_address_keeps_business_base_when_hosts_differ(self):
        from src.runtime.agent import _apply_local_worker_base_override
        env = {"GRAPE_SERVER_URL": "https://grape.example.com", "RUNTIME_SERVER_URL": "https://worker.grape.example.com"}
        task = {"server_url": "https://worker.grape.example.com"}
        _apply_local_worker_base_override(env, task, "http://127.0.0.1:3000")
        assert env["GRAPE_SERVER_URL"] == "https://grape.example.com"
        assert env["RUNTIME_SERVER_URL"] == "http://127.0.0.1:3000"

    def test_same_host_different_path_keeps_business_base(self):
        """同 host:port、不同 path 前缀的双地址部署不得误判为单地址。"""
        from src.runtime.agent import _apply_local_worker_base_override
        env = {"GRAPE_SERVER_URL": "https://grape.example.com/api",
               "RUNTIME_SERVER_URL": "https://grape.example.com/runtime"}
        task = {"server_url": "https://grape.example.com/runtime"}
        _apply_local_worker_base_override(env, task, "http://127.0.0.1:3000")
        assert env["GRAPE_SERVER_URL"] == "https://grape.example.com/api"
        assert env["RUNTIME_SERVER_URL"] == "http://127.0.0.1:3000"


class TestSameUrlHost:
    def test_same_scheme_host_port_path_ignores_trailing_slash(self):
        from src.runtime.agent import _same_url_host
        assert _same_url_host("https://grape.example.com/api", "https://grape.example.com/api/")

    def test_different_path_is_not_same_endpoint(self):
        from src.runtime.agent import _same_url_host
        assert not _same_url_host("https://grape.example.com/api",
                                   "https://grape.example.com/runtime")

    def test_different_scheme_is_not_same_endpoint(self):
        from src.runtime.agent import _same_url_host
        assert not _same_url_host("https://grape.example.com", "http://grape.example.com")

    def test_different_port_is_not_same_endpoint(self):
        from src.runtime.agent import _same_url_host
        assert not _same_url_host("https://grape.example.com:8443",
                                   "https://grape.example.com:443")

class TestBusinessApiWarningAndHealth:
    def test_loopback_business_api_warns(self):
        from src.user.db import get_url_split_warning
        with patch("src.config.read_server_config",
                   return_value={"task_business_api_url": "http://127.0.0.1:8000"}):
            w = get_url_split_warning()
        assert "task_business_api_url" in w

    def test_health_exposes_business_api_url(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.api.routes.health import router
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        with patch("src.config.read_server_config",
                   return_value={"task_business_api_url": "https://api.grape.example.com"}), \
             patch("src.user.db.get_meta_value", return_value=""):
            data = client.get("/api/health").json()
        assert data["task_business_api_url"] == "https://api.grape.example.com"


class TestReservedRuntimeEnv:
    def test_runtime_server_url_reserved_in_scene_custom_env(self):
        from src.scene_custom_env import is_reserved_name
        assert is_reserved_name("RUNTIME_SERVER_URL")

    def test_config_example_documents_business_api_url(self):
        import json
        path = os.path.join(ROOT, "configs", "server-config.example.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg.get("task_business_api_url") == "https://grape.example.com"
        assert "task_business_api_url" in cfg.get("_comment_issue_1577", "")
