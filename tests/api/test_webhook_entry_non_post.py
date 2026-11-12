"""Issue #514 原子测试：/webhook/gitcode 非 POST 方法显式拒绝（405）。

背景：路由仅声明 POST，非 POST 请求（GET/PUT/DELETE/PATCH 等）未命中注册路由，
落入 SPA catch-all（main.py spa_catch_all）兜底返回 200 text/html，导致健康探针
把入口误判为成功。期望：非 POST → 405，body 非 SPA HTML；POST 行为不变。
"""
import os
import sys

# 强制使用独立测试库，防止 TestClient 误连生产库（与 tests/conftest.py 双保险）
_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(_ROOT, "data", "test_sessions.db"))
sys.path.insert(0, _ROOT)

import pytest
from fastapi.testclient import TestClient

from src.api.main import app

client = TestClient(app)

# 覆盖的 HTTP 方法（不含 POST——POST 是唯一合法的 webhook 入口方法）
_NON_POST_METHODS = ("get", "put", "delete", "patch", "options")


@pytest.mark.atomic
def test_webhook_entry_rejects_non_post_methods():
    """非 POST 方法访问 /webhook/gitcode 必须返回 405，而非 SPA 兜底 200 HTML。"""
    for method in _NON_POST_METHODS:
        resp = getattr(client, method)("/webhook/gitcode")
        assert resp.status_code == 405, (
            f"{method.upper()} /webhook/gitcode 应返回 405，实际 {resp.status_code}"
        )
        content_type = resp.headers.get("content-type", "")
        assert "text/html" not in content_type.lower(), (
            f"{method.upper()} 响应不得是 SPA HTML，实际 content-type={content_type}"
        )
        assert "<!DOCTYPE html" not in resp.text[:200].upper(), (
            f"{method.upper()} 响应 body 不得为 SPA HTML"
        )

    # HEAD：FastAPI 不自动派生 HEAD，需显式覆盖（405，无 body）
    head_resp = client.head("/webhook/gitcode")
    assert head_resp.status_code == 405, (
        f"HEAD /webhook/gitcode 应返回 405，实际 {head_resp.status_code}"
    )

    # POST 行为不变：空 body 非法 JSON → 400（原有逻辑），绝不 405/200 HTML
    post_resp = client.post("/webhook/gitcode", content=b"")
    assert post_resp.status_code == 400, (
        f"POST /webhook/gitcode 空 body 应保持原有 400，实际 {post_resp.status_code}"
    )
    assert "text/html" not in post_resp.headers.get("content-type", "").lower()
