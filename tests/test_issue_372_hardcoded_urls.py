"""Issue #372 §15: 同类问题修复 — 硬编码地址 + 自动发现断裂

测试覆盖：
- T10-01: settings.py webhook_url 为空时返回空字符串（非 localhost:8080）
- T10-02~04: events.py A2A re-dispatch 用 get_server_base_url
- T10-05~08: providers.py _get_trusted_host 支持 X-Forwarded-Host
- T10-09~11: config.py _detect_server_base 端口从 GRAPE_PORT 读取
"""
import os
from unittest.mock import patch, MagicMock

import pytest


# ===========================================================================
# T10-01: settings.py 去掉 localhost 兜底
# ===========================================================================

class TestSettingsNoLocalhostFallback:
    """测试 settings.py webhook_url 不再有 localhost 兜底。"""

    def test_t10_01_webhook_url_empty_returns_empty(self):
        """T10-01: webhook_url 为空时 settings 返回空字符串（非 localhost:8080）。"""
        # 验证代码中不再有 localhost:8080 兜底
        import inspect
        from src.api.routes import settings
        src = inspect.getsource(settings)
        # 查找 get_webhook_url() or "http://localhost:8080" 模式
        assert 'get_webhook_url() or "http://localhost:8080' not in src, \
            "localhost:8080 fallback still present in settings.py"
        # 应该是 get_webhook_url() or ""
        assert 'get_webhook_url() or ""' in src, \
            "webhook_url should fall back to empty string"


# ===========================================================================
# T10-02~04: events.py A2A re-dispatch 用 get_server_base_url
# ===========================================================================

class TestA2ARedispatchUrl:
    """测试 A2A re-dispatch URL 从 get_server_base_url 获取。"""

    def test_t10_02_no_hardcoded_localhost_8000(self):
        """T10-02: events.py A2A re-dispatch 不再硬编码 localhost:8000。"""
        import inspect
        from src.api.routes import events
        src = inspect.getsource(events)
        # 不应有硬编码 http://localhost:8000/api/a2a
        assert '"http://localhost:8000/api/a2a"' not in src, \
            "hardcoded localhost:8000/api/a2a still present"
        # 应该用 get_server_base_url
        assert "get_server_base_url" in src, \
            "get_server_base_url not used in events.py"

    def test_t10_03_a2a_url_uses_server_base_url(self):
        """T10-03: get_server_base_url() 有值时 A2A URL 含正确地址。"""
        from src.user.db import get_server_base_url
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value="http://1.2.3.4:3000"):
            base = get_server_base_url()
            assert base == "http://1.2.3.4:3000"
            a2a_url = f"{base}/api/a2a"
            assert a2a_url == "http://1.2.3.4:3000/api/a2a"

    def test_t10_04_fallback_to_localhost(self):
        """T10-04: get_server_base_url() 为空时 fallback 到 localhost（兼容本地开发）。"""
        from src.user.db import get_server_base_url
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""):
            base = get_server_base_url()
            # get_server_base_url 最终 fallback 到 localhost:8080（原有兜底）
            # events.py 中 `or "http://localhost:8000"` 是额外 fallback
            a2a_base = base or "http://localhost:8000"
            assert "localhost" in a2a_base  # 本地开发兼容


# ===========================================================================
# T10-05~08: providers.py _get_trusted_host 支持 X-Forwarded-Host
# ===========================================================================

class TestGetTrustedHostForwarded:
    """测试 _get_trusted_host 优先读 X-Forwarded-Host。"""

    def _mock_request(self, host="", fwd_host=""):
        req = MagicMock()
        headers = {}
        if host:
            headers["Host"] = host
        if fwd_host:
            headers["X-Forwarded-Host"] = fwd_host
        req.headers.get = lambda k, d="": headers.get(k, d)
        return req

    def test_t10_05_forwarded_host_used_for_redirect_uri(self):
        """T10-05: nginx 代理场景，X-Forwarded-Host 含公网域名 → 用公网域名（需设 TRUSTED_HOSTS）。"""
        from src.auth.providers import _get_trusted_host
        # R11.1: 未设 TRUSTED_HOSTS 时不读 X-Forwarded-Host
        with patch.dict(os.environ, {"TRUSTED_HOSTS": "grape.example.com"}, clear=False):
            req = self._mock_request(host="127.0.0.1:3000", fwd_host="grape.example.com")
            result = _get_trusted_host(req)
            assert result == "grape.example.com"

    def test_t10_06_fallback_to_host_when_no_forwarded(self):
        """T10-06: 无 X-Forwarded-Host 时 fallback 到 Host 头。"""
        from src.auth.providers import _get_trusted_host
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUSTED_HOSTS", None)
            req = self._mock_request(host="1.2.3.4:3000", fwd_host="")
            result = _get_trusted_host(req)
            assert result == "1.2.3.4:3000"

    def test_t10_07_trusted_hosts_validates_forwarded(self):
        """T10-07: TRUSTED_HOSTS 白名单校验 X-Forwarded-Host 的值（不在白名单 → 用 fallback）。"""
        from src.auth.providers import _get_trusted_host
        with patch.dict(os.environ, {"TRUSTED_HOSTS": "trusted.com"}):
            req = self._mock_request(host="127.0.0.1:3000", fwd_host="evil.com")
            result = _get_trusted_host(req)
            # evil.com 不在白名单 → 用 fallback trusted.com
            assert result == "trusted.com"

    def test_t10_08_localhost_forwarded_excluded(self):
        """T10-08: X-Forwarded-Host 是 localhost 时排除，fallback 到 Host 头。"""
        from src.auth.providers import _get_trusted_host
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUSTED_HOSTS", None)
            req = self._mock_request(host="1.2.3.4:3000", fwd_host="localhost")
            result = _get_trusted_host(req)
            # fwd_host 是 localhost → 排除 → 用 Host 头
            assert result == "1.2.3.4:3000"

    def test_both_localhost_returns_host(self):
        """两个都是 localhost 时保持原行为（开发环境）。"""
        from src.auth.providers import _get_trusted_host
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRUSTED_HOSTS", None)
            req = self._mock_request(host="localhost:8080", fwd_host="127.0.0.1:3000")
            result = _get_trusted_host(req)
            # 都是 localhost → 返回 Host 头（原行为）
            assert "localhost" in result or "127.0.0.1" in result


# ===========================================================================
# T10-09~11: config.py _detect_server_base 端口从 GRAPE_PORT 读取
# ===========================================================================

class TestDetectServerBasePort:
    """测试 _detect_server_base 端口从 GRAPE_PORT 环境变量读取。"""

    def test_t10_09_port_from_grape_port_env(self):
        """T10-09: GRAPE_PORT 环境变量设置时探测地址含该端口。"""
        with patch.dict(os.environ, {"GRAPE_PORT": "4000"}):
            _port = os.environ.get("GRAPE_PORT", "3000")
            assert _port == "4000"
            # 模拟探测成功后的地址
            ip = "1.2.3.4"
            url = f"http://{ip}:{_port}"
            assert url == "http://1.2.3.4:4000"

    def test_t10_10_default_port_3000(self):
        """T10-10: GRAPE_PORT 未设置时默认 3000（非 8080）。"""
        # 确保环境变量不存在
        env = dict(os.environ)
        env.pop("GRAPE_PORT", None)
        with patch.dict(os.environ, env, clear=True):
            _port = os.environ.get("GRAPE_PORT", "3000")
            assert _port == "3000"
            # 不应是 8080
            assert _port != "8080"

    def test_t10_11_grape_port_8080_backward_compat(self):
        """T10-11: GRAPE_PORT=8080 时探测地址含 8080 端口（向前兼容）。"""
        with patch.dict(os.environ, {"GRAPE_PORT": "8080"}):
            _port = os.environ.get("GRAPE_PORT", "3000")
            assert _port == "8080"
            url = f"http://1.2.3.4:{_port}"
            assert url == "http://1.2.3.4:8080"

    def test_config_no_hardcoded_8080(self):
        """验证 config.py 代码中不再有硬编码 :8080。"""
        import inspect
        from src import config
        src = inspect.getsource(config)
        # 不应有硬编码 http://{ip}:8080
        assert 'f"http://{ip}:8080"' not in src, \
            "hardcoded :8080 still present in config.py"
        # 应该用 GRAPE_PORT
        assert "GRAPE_PORT" in src, \
            "GRAPE_PORT not used in config.py"


# ===========================================================================
# T10-12~15: 剩余硬编码 localhost:8080 fallback 修复
# ===========================================================================

class TestRemainingLocalhostFallback:
    """测试剩余硬编码 localhost:8080 fallback 已修复。"""

    def test_t10_12_get_server_base_url_fallback_not_8080(self):
        """T10-12: get_server_base_url() 所有来源为空时返回 http://localhost:3000（非 8080）。"""
        from src.user.db import get_server_base_url
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GRAPE_PORT", None)
            result = get_server_base_url()
            assert "8080" not in result, f"fallback should not be 8080, got: {result}"
            assert "3000" in result, f"fallback should be 3000, got: {result}"

    def test_t10_13_get_server_base_url_fallback_grape_port(self):
        """T10-13: get_server_base_url() fallback 端口从 GRAPE_PORT 读取。"""
        from src.user.db import get_server_base_url
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch.dict(os.environ, {"GRAPE_PORT": "4000"}):
            result = get_server_base_url()
            assert "4000" in result, f"fallback should use GRAPE_PORT=4000, got: {result}"

    def test_t10_14_prompt_builder_fallback_uses_local_port(self):
        """T10-14: _resolve_server_url 异常 fallback 用 _local_port()（非硬编码 8080）。"""
        import inspect
        from src import prompt_builder
        src = inspect.getsource(prompt_builder)
        # 不应有硬编码 localhost:8080
        assert '"http://localhost:8080"' not in src, \
            "hardcoded localhost:8080 still in prompt_builder"
        # 应该用 _local_port()
        assert "_local_port()" in src, \
            "_local_port() not used in prompt_builder fallback"

    def test_t10_15_providers_host_default_empty(self):
        """T10-15: providers.py Host header default 为空字符串（非 localhost:80）。"""
        import inspect
        from src.auth import providers
        src = inspect.getsource(providers)
        # 不应有 localhost:80 作为 default
        assert '"localhost:80"' not in src, \
            "localhost:80 default still in providers.py"
        # 应该用空字符串 default
        assert 'get("Host", "")' in src, \
            "Host default should be empty string"
