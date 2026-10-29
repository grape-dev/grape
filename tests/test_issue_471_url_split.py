"""Issue #471 E1：server_base_url 双语义拆分。

browser_callback_url（浏览器回调：OAuth/附件/前端展示）与
worker_connect_url（worker 回连：devstage RUNTIME_SERVER_URL、任务 server_url、
远程 worker 地址）拆分为两个配置项；未配新项时回退旧 server_base_url（向前兼容）。
"""
import os
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestUrlSplitGetters:
    def test_worker_connect_url_from_server_config(self):
        from src.user.db import get_worker_connect_url
        with patch("src.config.read_server_config",
                   return_value={"worker_connect_url": "https://worker.grapedev.cn"}), \
             patch("src.user.db.get_server_base_url", return_value="https://www.grapedev.cn"):
            assert get_worker_connect_url() == "https://worker.grapedev.cn"

    def test_worker_connect_url_from_meta(self):
        from src.user.db import get_worker_connect_url
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value",
                   side_effect=lambda k: {"worker_connect_url": "https://w.internal"}.get(k, "")), \
             patch("src.user.db.get_server_base_url", return_value="https://www.grapedev.cn"):
            assert get_worker_connect_url() == "https://w.internal"

    def test_browser_callback_url_from_server_config(self):
        from src.user.db import get_browser_callback_url
        with patch("src.config.read_server_config",
                   return_value={"browser_callback_url": "https://www.grapedev.cn"}), \
             patch("src.user.db.get_server_base_url", return_value="https://worker.grapedev.cn"):
            assert get_browser_callback_url() == "https://www.grapedev.cn"

    def test_fallback_to_server_base_url_when_unconfigured(self):
        """向前兼容：未配新项时两个 getter 均回退 server_base_url。"""
        from src.user.db import get_browser_callback_url, get_worker_connect_url
        with patch("src.config.read_server_config", return_value={}), \
             patch("src.user.db.get_meta_value", return_value=""), \
             patch("src.user.db.get_server_base_url", return_value="https://grape.example.com"):
            assert get_worker_connect_url() == "https://grape.example.com"
            assert get_browser_callback_url() == "https://grape.example.com"

    def test_trailing_slash_stripped(self):
        from src.user.db import get_worker_connect_url
        with patch("src.config.read_server_config",
                   return_value={"worker_connect_url": "https://w.internal/"}):
            assert get_worker_connect_url() == "https://w.internal"


class TestUrlSplitWarning:
    def test_loopback_worker_connect_url_warns(self):
        from src.user.db import get_url_split_warning
        with patch("src.config.read_server_config",
                   return_value={"worker_connect_url": "http://localhost:3000"}):
            w = get_url_split_warning()
            assert "worker_connect_url" in w

    def test_loopback_browser_callback_url_warns(self):
        from src.user.db import get_url_split_warning
        with patch("src.config.read_server_config",
                   return_value={"browser_callback_url": "http://127.0.0.1:3000"}):
            w = get_url_split_warning()
            assert "browser_callback_url" in w

    def test_no_warning_when_both_public(self):
        from src.user.db import get_url_split_warning
        with patch("src.config.read_server_config",
                   return_value={"browser_callback_url": "https://www.grapedev.cn",
                                 "worker_connect_url": "https://worker.grapedev.cn"}):
            assert get_url_split_warning() == ""


class TestHealthEndpoint:
    def test_health_exposes_split_urls(self):
        from fastapi.testclient import TestClient
        from src.api.routes.health import router
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        with patch("src.config.read_server_config",
                   return_value={"browser_callback_url": "https://www.grapedev.cn",
                                 "worker_connect_url": "https://worker.grapedev.cn"}), \
             patch("src.user.db.get_server_base_url", return_value="https://www.grapedev.cn"), \
             patch("src.user.db.is_server_base_url_configured", return_value=True):
            data = client.get("/api/health").json()
        assert data["browser_callback_url"] == "https://www.grapedev.cn"
        assert data["worker_connect_url"] == "https://worker.grapedev.cn"
        assert data["url_split_warning"] == ""


class TestConsumers:
    """消费端切换：worker 面走 get_worker_connect_url，浏览器面走 get_browser_callback_url。"""

    def test_devstage_uses_worker_connect_url(self):
        src = _src("src/api/routes/devstage.py")
        assert "get_worker_connect_url" in src

    def test_service_uses_worker_connect_url(self):
        src = _src("src/runtime/service.py")
        assert "get_worker_connect_url" in src

    def test_prompt_builder_uses_worker_connect_url(self):
        src = _src("src/prompt_builder.py")
        assert "get_worker_connect_url" in src

    def test_issues_center_attachments_use_browser_callback_url(self):
        src = _src("src/api/issues_center.py")
        assert 'get_meta_value("server_base_url")' not in src
        assert "get_browser_callback_url" in src

    def test_health_route_exposes_split_fields(self):
        src = _src("src/api/routes/health.py")
        assert "browser_callback_url" in src and "worker_connect_url" in src

    def test_config_double_writes_meta(self):
        src = _src("src/config.py")
        assert 'set_meta_value("worker_connect_url"' in src
        assert 'set_meta_value("browser_callback_url"' in src
