"""Issue #1731：本机 runtime 场景业务 API 基址探测与回退测试。

背景：`GRAPE_SERVER_URL` 在本机 runtime 分支曾被直接解析为
`http://127.0.0.1:{GRAPE_PORT}`，当 GRAPE_PORT 指向 runtime 网关（如 8083）
而非业务 API（如 8081）时，issues / knowledge / repos 等业务端点全量 404。

覆盖：
- 业务端口显式配置（task_business_api_port / GRAPE_BUSINESS_PORT）
- 本机 runtime + 非业务端口：探测通过则用业务端口，失败回退 server_base_url
- 显式 task_business_api_url 仍最高优先且不探测
- 远程 runtime 行为不变、单地址部署等价
- 探测 health 优先 / openapi 兜底 / 进程内缓存
- resolve_task_server_env 审计日志（地址 + 来源，不含 token）
"""
import logging
import os
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestBusinessApiPortConfig:
    def test_server_config_has_highest_priority(self):
        from src.user.db import get_business_api_port
        with patch("src.config.read_server_config",
                   return_value={"task_business_api_port": 8081}), \
             patch("src.user.db.get_meta_value", return_value="8091"), \
             patch.dict(os.environ, {"GRAPE_BUSINESS_PORT": "8092"}):
            assert get_business_api_port() == "8081"

    def test_meta_value_used_when_config_missing(self):
        from src.user.db import get_business_api_port
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value="8091"), \
             patch.dict(os.environ, {"GRAPE_BUSINESS_PORT": "8092"}):
            assert get_business_api_port() == "8091"

    def test_server_config_int_value_accepted(self):
        from src.user.db import get_business_api_port
        with patch("src.config.read_server_config",
                   return_value={"task_business_api_port": 8081}), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GRAPE_BUSINESS_PORT", None)
            assert get_business_api_port() == "8081"

    def test_env_used_when_unconfigured(self):
        from src.user.db import get_business_api_port
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch.dict(os.environ, {"GRAPE_BUSINESS_PORT": "8092"}):
            assert get_business_api_port() == "8092"

    def test_empty_when_unconfigured(self):
        from src.user.db import get_business_api_port
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GRAPE_BUSINESS_PORT", None)
            assert get_business_api_port() == ""


class TestLocalRuntimeProbeAndFallback:
    def _resolve(self, probe, business_port="", local_port="8083",
                 public_url="https://api.grape.example.com"):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        with patch("src.user.db.get_business_api_url_explicit", return_value=""), \
             patch("src.user.db.get_business_api_url", return_value=public_url), \
             patch("src.user.db.get_business_api_port", return_value=business_port), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=True), \
             patch.object(prompt_builder, "_local_port", return_value=local_port), \
             patch.object(prompt_builder, "_probe_business_api", side_effect=probe) as _probe:
            url = prompt_builder._resolve_business_server_url(3)
        return url, _probe

    def test_uses_explicit_business_port_when_probe_passes(self):
        """本机 runtime 8083 + 业务 API 8081：探测通过 → 返回业务端口。"""
        url, probe = self._resolve(lambda u: u == "http://127.0.0.1:8081",
                                   business_port="8081", local_port="8083")
        assert url == "http://127.0.0.1:8081"
        probe.assert_called_once_with("http://127.0.0.1:8081")

    def test_falls_back_to_server_base_url_when_local_probe_fails(self):
        """未配业务端口（GRAPE_PORT=8083 为 runtime）：本机候选探测失败 → 回退 server_base_url。"""
        url, probe = self._resolve(
            lambda u: u == "https://api.grape.example.com",
            business_port="", local_port="8083")
        assert url == "https://api.grape.example.com"
        assert probe.call_count == 2

    def test_tail_fallback_keeps_server_base_url_when_all_probes_fail(self):
        """本机与 server_base_url 均不可用：末尾兜底 server_base_url（有 warning 日志）。"""
        url, probe = self._resolve(lambda u: False,
                                   business_port="8081", local_port="8083")
        assert url == "https://api.grape.example.com"
        assert probe.call_count == 2

    def test_explicit_business_api_url_wins_without_probe(self):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        with patch("src.user.db.get_business_api_url_explicit",
                   return_value="https://biz.explicit.example.com"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=True), \
             patch.object(prompt_builder, "_probe_business_api") as probe:
            url = prompt_builder._resolve_business_server_url(3)
        assert url == "https://biz.explicit.example.com"
        probe.assert_not_called()

    def test_remote_runtime_keeps_business_base_without_probe(self):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        with patch("src.user.db.get_business_api_url_explicit", return_value=""), \
             patch("src.user.db.get_business_api_url",
                   return_value="https://api.grape.example.com"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=False), \
             patch.object(prompt_builder, "_probe_business_api") as probe:
            url = prompt_builder._resolve_business_server_url(7)
        assert url == "https://api.grape.example.com"
        probe.assert_not_called()

    def test_single_address_deployment_equivalent(self):
        """单地址部署：业务基址与 runtime 基址取值等价改造前行为。"""
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db.get_server_base_url",
                   return_value="https://grape.example.com"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=False):
            assert prompt_builder._resolve_business_server_url(9) == "https://grape.example.com"
            assert prompt_builder._resolve_server_url(9) == "https://grape.example.com"


    def test_local_runtime_single_address_uses_only_configured_address(self):
        """本机 runtime + 单地址部署：本机候选探测失败时回退唯一配置地址。"""
        url, probe = self._resolve(
            lambda u: u == "https://grape.example.com",
            business_port="", local_port="8083",
            public_url="https://grape.example.com")
        assert url == "https://grape.example.com"
        assert probe.call_count == 2

    def test_unknown_locality_probes_local_before_gateway_fallback(self):
        """locality 查询异常（未知）也先探测本机业务端口，不直接落 runtime 网关。"""
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        with patch("src.user.db.get_business_api_url_explicit", return_value=""), \
             patch("src.user.db.get_business_api_url", return_value=""), \
             patch("src.user.db.get_business_api_port", return_value="8081"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=None), \
             patch.object(prompt_builder, "_probe_business_api", return_value=True) as probe:
            assert prompt_builder._resolve_business_server_url(3) == "http://127.0.0.1:8081"
        probe.assert_called_once_with("http://127.0.0.1:8081")


class TestAuditSourceAccuracy:
    def test_unreachable_candidates_do_not_claim_local_source(self):
        """本机与 server_base_url 均探测失败：来源不得标成 local_business_port。"""
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        with patch("src.user.db.get_business_api_url_explicit", return_value=""), \
             patch("src.user.db.get_business_api_url", return_value=""), \
             patch("src.user.db.get_business_api_port", return_value="8081"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=True), \
             patch.object(prompt_builder, "_probe_business_api", return_value=False):
            url, source = prompt_builder._resolve_business_base(3)
        assert url == "http://127.0.0.1:8081"
        assert source == "unreachable_fallback"

    def test_unreachable_candidates_with_server_base_url_source(self):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        with patch("src.user.db.get_business_api_url_explicit", return_value=""), \
             patch("src.user.db.get_business_api_url",
                   return_value="https://api.grape.example.com"), \
             patch("src.user.db.get_business_api_port", return_value="8081"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=True), \
             patch.object(prompt_builder, "_probe_business_api", return_value=False):
            url, source = prompt_builder._resolve_business_base(3)
        assert url == "https://api.grape.example.com"
        assert source == "unreachable_fallback"

    def test_audit_log_reports_unreachable_fallback(self, caplog):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        with patch("src.user.db.get_business_api_url_explicit", return_value=""), \
             patch("src.user.db.get_business_api_url", return_value=""), \
             patch("src.user.db.get_business_api_port", return_value="8081"), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=True), \
             patch.object(prompt_builder, "_resolve_server_url",
                          return_value="https://worker.grape.example.com"), \
             patch.object(prompt_builder, "_probe_business_api", return_value=False):
            with caplog.at_level(logging.INFO, logger="devops-bot"):
                prompt_builder.resolve_task_server_env(3)
        assert "source=unreachable_fallback" in caplog.text


class TestProbeCacheBound:
    def test_probe_cache_evicts_beyond_max(self):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        limit = prompt_builder._BUSINESS_PROBE_CACHE_MAX
        try:
            with patch.object(prompt_builder, "_probe_health", return_value=False), \
                 patch.object(prompt_builder, "_probe_openapi_family", return_value=False), \
                 patch.object(prompt_builder, "_business_probe_ttl", return_value=60.0):
                for i in range(limit + 10):
                    prompt_builder._probe_business_api(f"http://127.0.0.1:{9000 + i}")
            assert len(prompt_builder._business_probe_cache) <= limit
        finally:
            prompt_builder._clear_business_probe_cache()


class TestBusinessApiProbe:
    def test_health_probe_success_skips_openapi(self):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        try:
            with patch.object(prompt_builder, "_probe_health", return_value=True) as health, \
                 patch.object(prompt_builder, "_probe_openapi_family") as openapi:
                assert prompt_builder._probe_business_api("http://127.0.0.1:8081")
            health.assert_called_once()
            openapi.assert_not_called()
        finally:
            prompt_builder._clear_business_probe_cache()

    def test_health_failure_falls_back_to_openapi_family(self):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        try:
            with patch.object(prompt_builder, "_probe_health", return_value=False), \
                 patch.object(prompt_builder, "_probe_openapi_family",
                              return_value=True) as openapi:
                assert prompt_builder._probe_business_api("http://127.0.0.1:8081")
            openapi.assert_called_once()
        finally:
            prompt_builder._clear_business_probe_cache()

    def test_probe_result_cached_in_process(self):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        try:
            with patch.object(prompt_builder, "_probe_health", return_value=True) as health:
                assert prompt_builder._probe_business_api("http://127.0.0.1:8081")
                assert prompt_builder._probe_business_api("http://127.0.0.1:8081")
            assert health.call_count == 1
        finally:
            prompt_builder._clear_business_probe_cache()

    def test_health_payload_requires_business_marker(self):
        from src import prompt_builder
        with patch.object(prompt_builder, "_http_get_json",
                          return_value={"detail": "Not Found"}):
            assert prompt_builder._probe_health("http://127.0.0.1:8083", 0.2) is False
        with patch.object(prompt_builder, "_http_get_json",
                          return_value={"server": "ok"}):
            assert prompt_builder._probe_health("http://127.0.0.1:8081", 0.2) is True

    def test_openapi_family_detects_issues_and_git_repo(self):
        from src import prompt_builder
        with patch.object(prompt_builder, "_http_get_json",
                          return_value={"paths": {"/api/runtime/nodes": {}}}):
            assert prompt_builder._probe_openapi_family("http://127.0.0.1:8083", 0.2) is False
        with patch.object(prompt_builder, "_http_get_json",
                          return_value={"paths": {"/api/git-repo/{owner}/{repo}/issues": {},
                                                  "/api/issues/{number}": {}}}):
            assert prompt_builder._probe_openapi_family("http://127.0.0.1:8081", 0.2) is True


class TestTaskServerEnvAuditLog:
    def test_env_resolution_logs_address_and_source_without_token(self, caplog):
        from src import prompt_builder
        with patch.object(prompt_builder, "_resolve_business_base",
                          return_value=("https://api.grape.example.com",
                                        "server_base_url")), \
             patch.object(prompt_builder, "_resolve_server_url",
                          return_value="https://worker.grape.example.com"):
            with caplog.at_level(logging.INFO, logger="devops-bot"):
                env = prompt_builder.resolve_task_server_env(7)
        assert env == {"GRAPE_SERVER_URL": "https://api.grape.example.com",
                       "RUNTIME_SERVER_URL": "https://worker.grape.example.com"}
        text = caplog.text
        assert "GRAPE_SERVER_URL=https://api.grape.example.com" in text
        assert "RUNTIME_SERVER_URL=https://worker.grape.example.com" in text
        assert "source=server_base_url" in text
        assert "tk_" not in text

    def test_audit_log_redacts_userinfo_and_query(self, caplog):
        """显式基址含 userinfo/query token 时，审计日志必须脱敏。"""
        from src import prompt_builder
        explicit = "https://user:secret@biz.example.com/api?token=tk_abc"
        with patch("src.user.db.get_business_api_url_explicit", return_value=explicit), \
             patch.object(prompt_builder, "_resolve_server_url",
                          return_value="https://worker.grape.example.com"):
            with caplog.at_level(logging.INFO, logger="devops-bot"):
                env = prompt_builder.resolve_task_server_env(3)
        text = caplog.text
        assert env["GRAPE_SERVER_URL"] == explicit
        assert "secret" not in text
        assert "tk_abc" not in text
        assert "user@" not in text
        assert "https://biz.example.com/api" in text

    def test_config_example_documents_business_api_port(self):
        import json
        path = os.path.join(ROOT, "configs", "server-config.example.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg.get("task_business_api_port") == 8081
        assert "task_business_api_port" in cfg.get("_comment_issue_1731", "")