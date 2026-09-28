"""GET /api/ping 健康探针测试（Issue #206 前端 heartbeat/恢复预检用）。

- 无鉴权可访问（返回 200 + {"ok": true}）
- 不依赖 DB/会话状态
"""
import os
import sys

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from fastapi.testclient import TestClient


def test_ping_no_auth_ok():
    from src.api.main import app
    c = TestClient(app)
    r = c.get("/api/ping")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data.get("ts")


def test_ping_is_not_loopback_only():
    """/api/ping 必须对任意来源可达（浏览器经 nginx），区别于 /health（仅 loopback）。"""
    from src.api.main import app
    c = TestClient(app)
    # TestClient 模拟远端请求（非 loopback 语义），/health 应 403 而 /api/ping 应 200
    health = c.get("/health")
    assert health.status_code == 403           # /health 仅 loopback
    r = c.get("/api/ping")
    assert r.status_code == 200                # /api/ping 任意来源可探