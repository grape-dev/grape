"""Issue #3766：注入的 GRAPE_SERVER_URL 指向 admin 前端导致业务 API 404。

覆盖注入链路守护：
- `/api/issues/mine` 探测：鉴权挑战（401/403 JSON）视为业务 API 存在，301/404/HTML 视为被前端接管；
- 业务 API 探测三级兜底：health → openapi → issues；
- 解析器对显式配置与远程 runtime 一并校验，命中不可用候选时回退到可用候选并告警；
- 无任何可用候选：warn 模式保留运维声明值并 error 告警，strict 模式阻断注入；
- 远程 runtime 绝不回退到本机回环地址；
- `GRAPE_BUSINESS_API_GUARD=off` 保留 Issue #1731 旧行为。
"""
import logging
import os
from unittest.mock import patch

import pytest

ADMIN_FRONT = "https://admin.grape.example.com"
BIZ = "https://api.grape.example.com"


class TestBusinessIssuesProbe:
    def test_accepts_auth_challenge_from_business_api(self):
        from src import prompt_builder
        with patch.object(prompt_builder, "_http_json_status",
                          return_value=(401, {"detail": {"auth_code": "MISSING_TOKEN"}})):
            assert prompt_builder._probe_business_issues(BIZ, 0.2) is True
        with patch.object(prompt_builder, "_http_json_status",
                          return_value=(403, {"error_code": "FORBIDDEN"})):
            assert prompt_builder._probe_business_issues(BIZ, 0.2) is True

    def test_accepts_2xx_json(self):
        from src import prompt_builder
        with patch.object(prompt_builder, "_http_json_status",
                          return_value=(200, {"data": []})):
            assert prompt_builder._probe_business_issues(BIZ, 0.2) is True

    def test_rejects_admin_frontend_redirect_and_404(self):
        from src import prompt_builder
        for status, payload in ((301, None), (302, None), (404, {"detail": "Not Found"}),
                                (405, None), (500, None), (200, None)):
            with patch.object(prompt_builder, "_http_json_status",
                              return_value=(status, payload)):
                assert prompt_builder._probe_business_issues(ADMIN_FRONT, 0.2) is False, status

    def test_probe_business_api_uses_issues_as_last_resort(self):
        from src import prompt_builder
        prompt_builder._clear_business_probe_cache()
        try:
            with patch.object(prompt_builder, "_probe_health", return_value=False), \
                 patch.object(prompt_builder, "_probe_openapi_family", return_value=False), \
                 patch.object(prompt_builder, "_probe_business_issues",
                              return_value=True) as issues:
                assert prompt_builder._probe_business_api(BIZ) is True
            issues.assert_called_once()
        finally:
            prompt_builder._clear_business_probe_cache()

    def test_probes_registered_mine_endpoint(self):
        """实际探测路径必须是业务 router 注册的 `/api/issues/mine`（非裸 /api/issues）。"""
        from src import prompt_builder
        with patch.object(prompt_builder, "_http_json_status",
                          return_value=(401, {"detail": "missing token"})) as status:
            assert prompt_builder._probe_business_issues(BIZ, 0.2) is True
        assert status.call_args.args[0] == BIZ + "/api/issues/mine"

    def test_probe_path_is_registered_by_business_router(self):
        """路由级校验：`/api/issues/mine` 真实存在，裸 `/api/issues` 未注册。"""
        from src import prompt_builder
        from src.domains.runtime.routes.issues import router
        paths = {getattr(r, "path", "") for r in router.routes}
        assert prompt_builder._BUSINESS_ISSUES_PROBE_PATH == "/api/issues/mine"
        assert prompt_builder._BUSINESS_ISSUES_PROBE_PATH in paths
        assert "/api/issues" not in paths


def _resolve(probe, explicit=ADMIN_FRONT, public=BIZ, local=False,
             server=BIZ, worker=BIZ, runtime_id=7, guard=""):
    from src import prompt_builder
    prompt_builder._clear_business_probe_cache()
    old = os.environ.pop(prompt_builder._BUSINESS_API_GUARD_ENV, None)
    if guard:
        os.environ[prompt_builder._BUSINESS_API_GUARD_ENV] = guard
    try:
        with patch("src.domains.mgmt.user.db.get_business_api_url_explicit",
                   return_value=explicit), \
             patch("src.domains.mgmt.user.db.get_business_api_url",
                   return_value=public), \
             patch("src.domains.mgmt.user.db.get_server_base_url",
                   return_value=server), \
             patch("src.domains.mgmt.user.db.get_worker_connect_url",
                   return_value=worker), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=local), \
             patch.object(prompt_builder, "_local_business_base",
                          return_value="http://127.0.0.1:8081"), \
             patch.object(prompt_builder, "_probe_business_api", side_effect=probe) as pr:
            result = prompt_builder._resolve_business_base(runtime_id)
        return result, pr
    finally:
        prompt_builder._clear_business_probe_cache()
        if guard:
            os.environ.pop(prompt_builder._BUSINESS_API_GUARD_ENV, None)
        if old is not None:
            os.environ[prompt_builder._BUSINESS_API_GUARD_ENV] = old


class TestResolveBusinessBaseGuard:
    def test_admin_frontend_explicit_falls_back_to_reachable_business_base(self):
        """显式配置指向 admin 前端（301→404）→ 回退到可用的 server_base_url。"""
        (url, source), probe = _resolve(lambda u: u == BIZ)
        assert url == BIZ
        assert source == "server_base_url"
        assert probe.call_count == 2

    def test_explicit_admin_frontend_falls_back_to_real_server_base_url_contract(self):
        """真实 getter 契约（QA 复审 High）：显式存在时 `get_business_api_url` 返回显式值。

        此时独立的 `server_base_url` 仍须进入候选链并可回退；旧实现复用
        `get_business_api_url` 会把 server_base_url 去重丢弃，导致永不回退。
        """
        (url, source), probe = _resolve(
            lambda u: u == BIZ, explicit=ADMIN_FRONT, public=ADMIN_FRONT,
            server=BIZ, worker=BIZ, local=False)
        assert url == BIZ
        assert source == "server_base_url"
        probed = [c.args[0] for c in probe.call_args_list]
        assert probed[0] == ADMIN_FRONT
        assert BIZ in probed

    def test_candidate_chain_keeps_server_base_url_when_public_equals_explicit(self):
        """候选链独立收 server_base_url，即便 get_business_api_url 与显式值相同。"""
        from src import prompt_builder
        with patch("src.domains.mgmt.user.db.get_worker_connect_url", return_value=BIZ), \
             patch.object(prompt_builder, "_runtime_is_local", return_value=False), \
             patch.object(prompt_builder, "_local_business_base",
                          return_value="http://127.0.0.1:8081"):
            chain, local = prompt_builder._business_candidate_chain(7, ADMIN_FRONT, BIZ)
        assert local is False
        assert (ADMIN_FRONT, "explicit_config") in chain
        assert (BIZ, "server_base_url") in chain

    def test_all_candidates_unreachable_warns_and_keeps_explicit(self, caplog):
        (url, source), _ = _resolve(lambda u: False, guard="warn")
        assert url == ADMIN_FRONT
        assert source == "unreachable_fallback"

    def test_all_candidates_unreachable_logs_error_alert(self, caplog):
        from src import prompt_builder
        with caplog.at_level(logging.ERROR, logger="devops-bot"):
            (url, source), _ = _resolve(lambda u: False, guard="warn")
        assert source == "unreachable_fallback"
        assert "business API injection guard failed" in caplog.text
        assert "Issue #3766" in caplog.text

    def test_strict_mode_blocks_injection_without_reachable_candidate(self):
        from src import prompt_builder
        with pytest.raises(prompt_builder.BusinessApiGuardError):
            _resolve(lambda u: False, guard="strict")

    def test_guard_off_restores_legacy_explicit_priority(self):
        """off 模式：显式配置不探测，保持 Issue #1731 旧语义。"""
        (url, source), probe = _resolve(lambda u: False, guard="off")
        assert url == ADMIN_FRONT
        assert source == "explicit_config"
        probe.assert_not_called()

    def test_remote_runtime_never_falls_back_to_loopback(self):
        """远程 runtime 只探测对外候选，绝不把本机回环注入远端任务。"""
        def only_loopback(url):
            return url.startswith("http://127.0.0.1")

        (url, source), probe = _resolve(only_loopback, public=ADMIN_FRONT,
                                        server=ADMIN_FRONT, worker=ADMIN_FRONT,
                                        local=False)
        assert url == ADMIN_FRONT
        assert source == "unreachable_fallback"
        probed = [c.args[0] for c in probe.call_args_list]
        assert all(not u.startswith("http://127.0.0.1") for u in probed)

    def test_local_runtime_can_use_loopback_business_port(self):
        (url, source), probe = _resolve(
            lambda u: u == "http://127.0.0.1:8081", local=True)
        assert url == "http://127.0.0.1:8081"
        assert source == "local_business_port"