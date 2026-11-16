"""Provider 配置下发链路 E2E 测试。

覆盖审查报告建议的测试链路：
  mock RuntimeClient.get_provider_configs
  → _fetch_provider_configs 设置 _provider_configs 全局
  → SandboxManager.set_provider_configs 注入
  → _select_provider_from_context 从 _provider_configs 取 config
  → Provider factory 收到 config

同时覆盖：
  - _fetch_provider_configs 降级路径（fetch 失败 → 内存缓存 → 磁盘缓存 → 空配置）
  - _refresh_provider_configs 配置变更检测 + invalidate_provider_cache
  - runtime /provider-configs 端点 BOLA（system scope 放行，非 system 403）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider_configs():
    """v3: provider configs indexed by provider_id."""
    return {
        1: {
            "platform": "cubesandbox",
            "name": "cubesandbox",
            "api_url": "https://api.cube.app",
            "api_key": "cs-key-123",
            "ssl_cert_content": "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----",
        },
        2: {
            "platform": "devbox",
            "name": "devbox",
            "api_url": "https://devbox-test.example.com",
            "api_key": "db-key-456",
            "gateway_url": "https://{tunnel_id}-{port}.devbox-s2.hwtunnel.com",
            "gateway_ip": "10.0.0.1",
            "gateway_skip_tls_verify": True,
        },
    }


def _make_runtime_token(scope="global"):
    """创建 runtime credential token（直接插 DB）。source 列已移除——凭据隔离由
    runtime_credential_id 承担（provider-configs 收敛），scope 仅控制任务派发范围。"""
    import hashlib, secrets
    from src.kernel.session_manager import db, db_lock
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, scope, status) "
            "VALUES (?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(),
             f"test-{scope}-{secrets.token_hex(4)}", scope, "active"))
        db.commit()
        cred_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return token, cred_id


# ---------------------------------------------------------------------------
# A. _fetch_provider_configs 链路：mock client → _provider_configs 全局
# ============================================================================

class TestFetchProviderConfigs:
    """_fetch_provider_configs：拉取 → 缓存 → 降级。"""

    def setup_method(self):
        """每个测试前重置 cloud_worker 模块级 _provider_configs。"""
        from src.domains.runtime.engine import cloud_worker
        cloud_worker._provider_configs = {}

    @pytest.mark.asyncio
    async def test_fetch_sets_provider_configs_global(self):
        """mock client.get_provider_configs → _provider_configs 被正确设置。"""
        from src.domains.runtime.engine import cloud_worker

        configs = _make_provider_configs()
        client = MagicMock()
        client.get_provider_configs = AsyncMock(return_value=configs)

        # patch 缓存路径到临时目录（避免写 /var/lib）
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "provider-configs.json")
            ssl_path = os.path.join(tmpdir, "cube-rootCA.pem")
            with patch.object(cloud_worker, "_provider_configs_cache_path", lambda: cache_path), \
                 patch.object(cloud_worker, "_ssl_cert_dir", lambda: ssl_path):
                result = await cloud_worker._fetch_provider_configs(client)

            assert result == configs
            assert cloud_worker._provider_configs == configs
            # api_key 明文存在
            assert result[1]["api_key"] == "cs-key-123"
            assert result[2]["api_key"] == "db-key-456"
            # extra_config 展开到顶层
            assert result[2]["gateway_ip"] == "10.0.0.1"
            assert result[2]["gateway_skip_tls_verify"] is True

    @pytest.mark.asyncio
    async def test_fetch_materializes_ssl_cert(self):
        """拉取后 ssl_cert_content 落盘到 _SSL_CERT_DIR。"""
        from src.domains.runtime.engine import cloud_worker

        configs = _make_provider_configs()
        client = MagicMock()
        client.get_provider_configs = AsyncMock(return_value=configs)

        with tempfile.TemporaryDirectory() as tmpdir:
            ssl_path = os.path.join(tmpdir, "cube-rootCA.pem")
            cache_path = os.path.join(tmpdir, "provider-configs.json")
            with patch.object(cloud_worker, "_ssl_cert_dir", lambda: ssl_path), \
                 patch.object(cloud_worker, "_provider_configs_cache_path", lambda: cache_path):
                await cloud_worker._fetch_provider_configs(client)

            assert os.path.isfile(os.path.join(ssl_path, "cube-rootCA-1.pem"))
            with open(os.path.join(ssl_path, "cube-rootCA-1.pem")) as f:
                content = f.read()
            assert "BEGIN CERTIFICATE" in content
            assert content == configs[1]["ssl_cert_content"]

    @pytest.mark.asyncio
    async def test_fetch_failure_falls_back_to_memory_cache(self):
        """client 抛异常 → 使用内存缓存（之前成功拉取的 _provider_configs）。"""
        from src.domains.runtime.engine import cloud_worker

        # 预设内存缓存
        cached = {2: {"api_url": "http://cached", "api_key": "cached-key"}}
        cloud_worker._provider_configs = cached

        client = MagicMock()
        client.get_provider_configs = AsyncMock(side_effect=RuntimeError("network down"))

        result = await cloud_worker._fetch_provider_configs(client)
        assert result == cached
        assert result[2]["api_key"] == "cached-key"

    @pytest.mark.asyncio
    async def test_fetch_failure_falls_back_to_disk_cache(self):
        """client 抛异常 + 无内存缓存 → 使用磁盘缓存（加密/解密链路完整）。"""
        from src.domains.runtime.engine import cloud_worker

        cloud_worker._provider_configs = {}  # 无内存缓存

        disk_cached = {1: {"api_url": "http://disk", "api_key": "disk-key"}}
        client = MagicMock()
        client.get_provider_configs = AsyncMock(side_effect=RuntimeError("network down"))

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "provider-configs.json")
            # 用真实的 _write_provider_configs_cache 写入到临时路径
            with patch.object(cloud_worker, "_provider_configs_cache_path", lambda: cache_path):
                cloud_worker._write_provider_configs_cache(disk_cached)
                # 再读——_read 会解密 api_key
                result = await cloud_worker._fetch_provider_configs(client)

            assert result == disk_cached
            assert cloud_worker._provider_configs == disk_cached

    @pytest.mark.asyncio
    async def test_fetch_failure_no_cache_returns_empty(self):
        """client 抛异常 + 无内存 + 无磁盘 → 空配置（Provider fallback 到本地文件）。"""
        from src.domains.runtime.engine import cloud_worker

        cloud_worker._provider_configs = {}

        client = MagicMock()
        client.get_provider_configs = AsyncMock(side_effect=RuntimeError("network down"))

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "nonexistent.json")
            with patch.object(cloud_worker, "_provider_configs_cache_path", lambda: cache_path):
                result = await cloud_worker._fetch_provider_configs(client)

            assert result == {}

    @pytest.mark.asyncio
    async def test_fetch_empty_configs_uses_cache(self):
        """服务端返回空 dict → 不覆盖缓存，使用已有内存缓存。"""
        from src.domains.runtime.engine import cloud_worker

        cached = {"devbox": {"api_url": "http://cached", "api_key": "k"}}
        cloud_worker._provider_configs = cached

        client = MagicMock()
        client.get_provider_configs = AsyncMock(return_value={})  # 空

        result = await cloud_worker._fetch_provider_configs(client)
        # 空配置不覆盖 → 使用内存缓存
        assert result == cached


# ---------------------------------------------------------------------------
# B. _refresh_provider_configs：心跳刷新 + invalidate_provider_cache
# ============================================================================

class TestRefreshProviderConfigs:
    """_refresh_provider_configs：检测配置变更 + 通知 sandbox_manager。"""

    def setup_method(self):
        from src.domains.runtime.engine import cloud_worker
        cloud_worker._provider_configs = {}

    @pytest.mark.asyncio
    async def test_refresh_updates_when_config_changed(self):
        """配置变更 → _provider_configs 更新 + invalidate_provider_cache 被调。"""
        from src.domains.runtime.engine import cloud_worker

        old_configs = {"devbox": {"api_url": "http://old", "api_key": "old-key"}}
        new_configs = {"devbox": {"api_url": "http://new", "api_key": "new-key"}}
        cloud_worker._provider_configs = old_configs

        client = MagicMock()
        client.get_provider_configs = AsyncMock(return_value=new_configs)

        mock_manager = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "pc.json")
            ssl_path = os.path.join(tmpdir, "cert.pem")
            with patch.object(cloud_worker, "_provider_configs_cache_path", lambda: cache_path), \
                 patch.object(cloud_worker, "_ssl_cert_dir", lambda: ssl_path), \
                 patch("src.domains.runtime.agents.sandbox.sandbox_manager", mock_manager):
                await cloud_worker._refresh_provider_configs(client)

        assert cloud_worker._provider_configs == new_configs
        mock_manager.invalidate_provider_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_no_change_skips_invalidate(self):
        """配置未变 → 不更新、不调 invalidate。"""
        from src.domains.runtime.engine import cloud_worker

        configs = {"devbox": {"api_url": "http://same", "api_key": "same-key"}}
        cloud_worker._provider_configs = configs

        client = MagicMock()
        client.get_provider_configs = AsyncMock(return_value=configs)  # 相同

        mock_manager = MagicMock()
        with patch("src.domains.runtime.agents.sandbox.sandbox_manager", mock_manager):
            await cloud_worker._refresh_provider_configs(client)

        assert cloud_worker._provider_configs == configs  # 不变
        mock_manager.invalidate_provider_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_failure_keeps_cached(self):
        """刷新失败 → 保持当前缓存，不抛异常。"""
        from src.domains.runtime.engine import cloud_worker

        cached = {"devbox": {"api_url": "http://cached", "api_key": "k"}}
        cloud_worker._provider_configs = cached

        client = MagicMock()
        client.get_provider_configs = AsyncMock(side_effect=RuntimeError("timeout"))

        # 不应抛异常
        await cloud_worker._refresh_provider_configs(client)
        assert cloud_worker._provider_configs == cached


# ---------------------------------------------------------------------------
# C. SandboxManager.set_provider_configs → _select_provider_from_context
# ============================================================================

class TestSandboxManagerProviderConfigInjection:
    """SandboxManager 配置注入 → _select_provider_from_context 取 config → factory 收到。"""

    def test_set_provider_configs_stores_config(self):
        """set_provider_configs 后 _provider_configs 被正确设置。"""
        from src.domains.runtime.agents.sandbox.manager import SandboxManager
        mgr = SandboxManager()
        configs = _make_provider_configs()
        mgr.set_provider_configs(configs)
        assert mgr._provider_configs == configs
        assert mgr._provider_configs[1]["api_key"] == "cs-key-123"

    def test_invalidate_provider_cache_clears_cache(self):
        """invalidate_provider_cache 清空 _provider_cache。"""
        from src.domains.runtime.agents.sandbox.manager import SandboxManager
        mgr = SandboxManager()
        # 填充缓存
        mgr._provider_cache["cubesandbox"] = (MagicMock(), 1234567890.0)
        assert len(mgr._provider_cache) == 1
        mgr.invalidate_provider_cache()
        assert len(mgr._provider_cache) == 0

    def test_select_provider_from_context_passes_config_to_factory(self):
        """_select_provider_from_context 从 _provider_configs 取 config 传给 factory。"""
        from src.domains.runtime.agents.sandbox.manager import SandboxManager
        from src.domains.runtime.agents.sandbox.sandbox_context import SandboxContext

        # 构造 mock factory + mock provider
        mock_provider = MagicMock()
        mock_provider.is_configured.return_value = True
        mock_health = MagicMock()
        mock_health.healthy = True
        mock_provider.health.return_value = mock_health

        received_config = {}

        def mock_factory(audit, credential, seccomp, alert_engine, config=None):
            received_config.update(config or {})
            return mock_provider

        configs = _make_provider_configs()
        mgr = SandboxManager(registry={"devbox": mock_factory})
        mgr.set_provider_configs(configs)

        ctx = SandboxContext(provider_id=2, sandbox_provider="devbox", template_id="tpl-001")
        provider, template_id = mgr._select_provider_from_context(ctx, "devbox")

        # factory 收到了 devbox 的 config
        assert provider is mock_provider
        assert template_id == "tpl-001"
        assert received_config["api_url"] == "https://devbox-test.example.com"
        assert received_config["api_key"] == "db-key-456"
        assert received_config["gateway_ip"] == "10.0.0.1"
        assert received_config["gateway_skip_tls_verify"] is True

    def test_select_provider_from_context_empty_config_when_not_set(self):
        """provider_config 未注入时 → factory 收到空 dict（fallback 到本地文件）。"""
        from src.domains.runtime.agents.sandbox.manager import SandboxManager
        from src.domains.runtime.agents.sandbox.sandbox_context import SandboxContext

        mock_provider = MagicMock()
        mock_provider.is_configured.return_value = True
        mock_health = MagicMock()
        mock_health.healthy = True
        mock_provider.health.return_value = mock_health

        received_config = {"sentinel": "exists"}

        def mock_factory(audit, credential, seccomp, alert_engine, config=None):
            received_config.clear()
            received_config.update(config or {})
            return mock_provider

        mgr = SandboxManager(registry={"devbox": mock_factory})
        # 不调 set_provider_configs → _provider_configs 为空

        ctx = SandboxContext(sandbox_provider="devbox")
        provider, _ = mgr._select_provider_from_context(ctx, "devbox")

        assert provider is mock_provider
        assert received_config == {}  # 空配置

    def test_select_provider_from_context_uses_cached_provider(self):
        """同 provider 60s 内复用缓存实例，不重复调 factory。"""
        import time
        from src.domains.runtime.agents.sandbox.manager import SandboxManager
        from src.domains.runtime.agents.sandbox.sandbox_context import SandboxContext

        call_count = [0]
        mock_provider = MagicMock()
        mock_provider.is_configured.return_value = True
        mock_health = MagicMock()
        mock_health.healthy = True
        mock_provider.health.return_value = mock_health

        def mock_factory(audit, credential, seccomp, alert_engine, config=None):
            call_count[0] += 1
            return mock_provider

        mgr = SandboxManager(registry={"devbox": mock_factory})
        mgr.set_provider_configs(_make_provider_configs())

        ctx = SandboxContext(sandbox_provider="devbox")
        # 第一次调用 → 创建
        p1, _ = mgr._select_provider_from_context(ctx, "devbox")
        assert call_count[0] == 1
        # 第二次调用 → 缓存命中，不调 factory
        p2, _ = mgr._select_provider_from_context(ctx, "devbox")
        assert call_count[0] == 1  # 仍然只调了 1 次
        assert p1 is p2  # 同一实例

    def test_select_provider_from_context_unknown_provider_logs_error(self):
        """provider 不在 registry → 打 error 日志 → fallback bwrap 或 None。"""
        from src.domains.runtime.agents.sandbox.manager import SandboxManager
        from src.domains.runtime.agents.sandbox.sandbox_context import SandboxContext

        # 空 registry → provider 不在 → bwrap 也不在 → None
        mgr = SandboxManager(registry={})
        mgr.set_provider_configs({"unknown_provider": {}})

        ctx = SandboxContext(sandbox_provider="unknown_provider")
        # mock detect_capabilities 返回 bwrap 不可用
        with patch("src.domains.runtime.agents.sandbox.detect.detect_capabilities", return_value={"bwrap_available": False}):
            provider, template_id = mgr._select_provider_from_context(ctx, "unknown")
        # bwrap 不可用 → (None, "")
        assert provider is None
        assert template_id == ""


# ---------------------------------------------------------------------------
# D. runtime /provider-configs 端点 BOLA（E2E HTTP）
# ============================================================================

class TestProviderConfigsEndpoint:
    """GET /api/runtime/provider-configs 端点——system scope 放行 / 非 system 403。"""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            db.cursor().execute("DELETE FROM runtime_credentials")
            db.cursor().execute("DELETE FROM sandbox_provider_config")
            db.commit()
        yield
        with db_lock:
            db.cursor().execute("DELETE FROM runtime_credentials")
            db.cursor().execute("DELETE FROM sandbox_provider_config")
            db.commit()

    def _seed_provider_config(self, cred_id=None):
        """插入一条 provider 配置到 DB（cred_id 非空时关联到该凭据——
        provider-configs 已按 [安全修正] 收敛为 rta_ 只查自己关联的 Provider）。"""
        from src.kernel.session_manager import db, db_lock
        from src.utils.crypto import encrypt
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO sandbox_provider_config (platform, endpoint, api_key_enc, extra_config, enabled, runtime_credential_id) "
                "VALUES (?,?,?,?,1,?)",
                ("devbox", "https://devbox-test.example.com",
                 encrypt("db-key-456"),
                 json.dumps({"gateway_url": "https://{tunnel}-{port}.devbox-s2.hwtunnel.com"}),
                 cred_id or 0))
            db.commit()

    def _client(self):
        return TestClient(__import__("src.api.main", fromlist=["app"]).app)

    def test_system_scope_can_access_provider_configs(self):
        """system scope 凭据 → 200 + 配置下发（api_key 解密为明文）。"""
        token, _cid = _make_runtime_token(scope="system")
        self._seed_provider_config(cred_id=_cid)
        c = self._client()
        r = c.get("/api/runtime/provider-configs", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        providers = r.json()["providers"]
        assert len(providers) == 1
        p = providers[0]
        assert p["endpoint"] == "https://devbox-test.example.com"
        assert p["api_key"] == "db-key-456"
        assert "gateway_url" in p["extra_config"]

    def test_system_scope_cannot_see_others_providers(self):
        """[安全修正回归] Provider 关联他人凭据 → 不下发（rta_ 只查自己，与 scope/source 无关）。"""
        token, _cid = _make_runtime_token(scope="system")
        # Provider 关联到另一个凭据（非自己）
        self._seed_provider_config(cred_id=_cid + 999)
        c = self._client()
        r = c.get("/api/runtime/provider-configs", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["providers"] == []

    def test_user_scope_unbound_gets_empty(self):
        """user scope 凭据（未关联 Provider）→ 200 + 空（source 门禁已移除，
        隔离由 runtime_credential_id 承担——未关联即拿不到任何配置）。"""
        self._seed_provider_config()
        token, _cid = _make_runtime_token(scope="user")
        c = self._client()
        r = c.get("/api/runtime/provider-configs", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["providers"] == []

    def test_repo_scope_unbound_gets_empty(self):
        """repo scope 凭据（未关联 Provider）→ 200 + 空。"""
        self._seed_provider_config()
        token, _cid = _make_runtime_token(scope="repo")
        c = self._client()
        r = c.get("/api/runtime/provider-configs", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["providers"] == []

    def test_no_token_401(self):
        """无 token → 401。"""
        c = self._client()
        r = c.get("/api/runtime/provider-configs")
        assert r.status_code == 401

    def test_empty_db_returns_empty_configs(self):
        """DB 无 provider 配置 → 200 + 空 configs（不报错）。"""
        token, _cid = _make_runtime_token(scope="system")
        c = self._client()
        r = c.get("/api/runtime/provider-configs", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["providers"] == []
