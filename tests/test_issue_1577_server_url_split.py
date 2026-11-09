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
        with patch("src.user.db.get_business_api_url",
                   return_value="https://api.grape.example.com"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=False):
            assert prompt_builder._resolve_business_server_url(7) == "https://api.grape.example.com"

    def test_local_runtime_uses_localhost(self):
        from src import prompt_builder
        with patch("src.user.db.get_business_api_url",
                   return_value="https://api.grape.example.com"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=True), \
             patch.object(prompt_builder, "_local_port", return_value="3456"):
            assert prompt_builder._resolve_business_server_url(3) == "http://127.0.0.1:3456"

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

    def test_build_task_env_source_injects_both(self):
        import inspect
        from src.api.event_pipeline import build_task_env
        src = inspect.getsource(build_task_env)
        assert "_resolve_business_server_url" in src
        assert 'env["RUNTIME_SERVER_URL"]' in src


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

    def test_no_unconditional_business_override_in_source(self):
        import inspect
        from src.runtime import agent as agent_mod
        src = inspect.getsource(agent_mod)
        assert 'env["GRAPE_SERVER_URL"] = _local_base' not in src