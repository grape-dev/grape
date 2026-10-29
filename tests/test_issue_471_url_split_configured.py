"""Issue #471 E1（QA Required 修复）：配置检查与告警纳入拆分项。

双地址部署按推荐只配 browser_callback_url + worker_connect_url（不配
server_base_url）时，is_server_base_url_configured / get_server_base_url_warning
不得误报「server_base_url 未配置」——浏览器面告警认 browser_callback_url、
worker 面认 worker_connect_url，任一已配置即消除对应告警。
"""
import os
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 双地址部署推荐配置：只有拆分项，无 server_base_url
_SPLIT_ONLY = {
    "browser_callback_url": "https://www.grapedev.cn",
    "worker_connect_url": "https://worker.grapedev.cn",
}


class TestConfiguredSplit:
    """is_server_base_url_configured：任一拆分项已配置（且非回环）即视为已配置。"""

    def test_split_only_counts_as_configured(self):
        """QA Required 场景：只配双拆分项 → 不得再报 server_base_url 未配置。"""
        from src.user.db import is_server_base_url_configured
        with patch("src.config.read_server_config", return_value=dict(_SPLIT_ONLY)), \
             patch("src.user.db.get_meta_value", return_value=""):
            assert is_server_base_url_configured() is True

    def test_warning_empty_when_split_only(self):
        from src.user.db import get_server_base_url_warning
        with patch("src.config.read_server_config", return_value=dict(_SPLIT_ONLY)), \
             patch("src.user.db.get_meta_value", return_value=""):
            assert get_server_base_url_warning() == ""

    def test_warning_empty_via_meta_split_only(self):
        """meta 双写来源同样生效（server-config.json 缺失时）。"""
        from src.user.db import get_server_base_url_warning
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value",
                   side_effect=lambda k: _SPLIT_ONLY.get(k, "")):
            assert get_server_base_url_warning() == ""

    def test_browser_face_loopback_still_warns(self):
        """浏览器面拆分项指向回环 → 告警（含 browser_callback_url 字样与回环提示）。"""
        from src.user.db import get_server_base_url_warning
        with patch("src.config.read_server_config",
                   return_value={"browser_callback_url": "http://localhost:3000",
                                 "worker_connect_url": "https://worker.grapedev.cn"}), \
             patch("src.user.db.get_meta_value", return_value=""):
            w = get_server_base_url_warning()
            assert "browser_callback_url" in w
            assert "回环" in w or "localhost" in w

    def test_worker_face_loopback_still_warns(self):
        """worker 面拆分项指向回环 → 告警（含 worker_connect_url 字样）。"""
        from src.user.db import get_server_base_url_warning
        with patch("src.config.read_server_config",
                   return_value={"browser_callback_url": "https://www.grapedev.cn",
                                 "worker_connect_url": "http://127.0.0.1:3000"}), \
             patch("src.user.db.get_meta_value", return_value=""):
            w = get_server_base_url_warning()
            assert "worker_connect_url" in w

    def test_all_unconfigured_still_warns(self):
        """全未配置（含 server_base_url）→ 保留原「未配置」告警（回归看护）。"""
        from src.user.db import is_server_base_url_configured, get_server_base_url_warning
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""):
            assert is_server_base_url_configured() is False
            w = get_server_base_url_warning()
            assert "未配置" in w or "server_base_url" in w

    def test_legacy_loopback_without_split_still_warns(self):
        """只配回环 server_base_url、无拆分项 → 保留回环告警（Issue #455 回归）。"""
        from src.user.db import is_server_base_url_configured, get_server_base_url_warning
        with patch("src.config.read_server_config",
                   return_value={"server_base_url": "http://localhost:3000"}), \
             patch("src.user.db.get_meta_value", return_value=""):
            assert is_server_base_url_configured() is False
            w = get_server_base_url_warning()
            assert "回环" in w or "localhost" in w


class TestHealthSplitConfigured:
    """健康端点：双地址部署不误报（server_base_url_configured=true、无误导告警）。"""

    def test_health_no_false_warning_when_split_only(self):
        from fastapi.testclient import TestClient
        from src.api.routes.health import router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        with patch("src.config.read_server_config", return_value=dict(_SPLIT_ONLY)), \
             patch("src.user.db.get_meta_value", return_value=""):
            data = client.get("/api/health").json()
        assert data["server_base_url_configured"] is True
        assert data["server_base_url_warning"] == ""
        assert data["browser_callback_url"] == "https://www.grapedev.cn"
        assert data["worker_connect_url"] == "https://worker.grapedev.cn"

    def test_health_warns_when_all_unconfigured(self):
        from fastapi.testclient import TestClient
        from src.api.routes.health import router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""):
            data = client.get("/api/health").json()
        assert data["server_base_url_configured"] is False
        assert data["server_base_url_warning"]


class TestRuntimeServerUrlWorkerFace:
    """Suggested：register 响应的 server_url 对齐 worker 回连地址。"""

    def test_runtime_server_url_uses_worker_connect(self):
        src = _src_text("src/api/routes/runtime.py")
        assert "get_worker_connect_url" in src
        assert "def _server_url" in src

    def test_runtime_server_url_value(self):
        from src.api.routes.runtime import _server_url
        with patch("src.config.read_server_config",
                   return_value={"worker_connect_url": "https://worker.grapedev.cn",
                                 "server_base_url": "https://www.grapedev.cn"}):
            assert _server_url() == "https://worker.grapedev.cn"


def _src_text(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()
