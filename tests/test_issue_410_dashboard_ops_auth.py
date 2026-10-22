"""Issue #410：/api/dashboard/ops/* 全族端点鉴权测试。

QA 回归 AT-SEC-14：匿名请求 5 个 ops 端点应返回 401；
修复前这些端点未挂鉴权依赖，匿名即返回 200 泄露业务数据，
且 probe-now 可被匿名触发真实引擎探测（副作用）。
修复后统一挂接 Depends(require_org_member)，与 ops/overview 一致。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

OPS_ENDPOINTS = [
    "/api/dashboard/ops/probe-now",
    "/api/dashboard/ops/error-logs/sources",
    "/api/dashboard/ops/error-logs",
    "/api/dashboard/ops/timeline",
    "/api/dashboard/ops/a2a-health",
]


def test_anonymous_ops_endpoints_return_401():
    """匿名（无 Authorization）请求 5 个 ops 端点全部返回 401。"""
    from fastapi.testclient import TestClient
    from src.api.main import app

    with TestClient(app) as cli:
        for path in OPS_ENDPOINTS:
            r = cli.get(path)
            assert r.status_code == 401, \
                f"{path}: expected 401, got {r.status_code}: {r.text[:200]}"


def test_anonymous_probe_now_does_not_trigger_probe():
    """匿名 probe-now 在鉴权闸门外直接 401，不触发探针（probe_all_now 不被调用）。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient
    from src.api.main import app

    with patch("src.observability.engine_probe.probe_all_now") as mock_probe:
        with TestClient(app) as cli:
            r = cli.get("/api/dashboard/ops/probe-now")
            assert r.status_code == 401
            mock_probe.assert_not_called()


def test_authed_ops_endpoints_behavior_unchanged():
    """登录（组织成员）请求 5 个端点行为不变：200 + 响应结构完整。"""
    import time as _t
    from unittest.mock import patch

    from fastapi.testclient import TestClient
    from src.api.deps import require_org_member
    from src.api.main import app
    import src.observability.engine_probe as _ep

    user = {"id": 41001, "username": "ops-user", "org_id": 0, "org_role": "owner"}
    app.dependency_overrides[require_org_member] = lambda: user

    try:
        with TestClient(app) as cli:
            # probe-now：置于防抖窗口内返回 throttled，避免测试触发真实探测线程
            with patch.object(_ep, "_last_probe_time", _t.time()):
                r = cli.get("/api/dashboard/ops/probe-now")
                assert r.status_code == 200, r.text
                assert r.json()["status"] == "throttled"

            # error-logs/sources
            r = cli.get("/api/dashboard/ops/error-logs/sources")
            assert r.status_code == 200, r.text
            assert "sources" in r.json()

            # error-logs
            r = cli.get("/api/dashboard/ops/error-logs", params={"limit": 10})
            assert r.status_code == 200, r.text
            body = r.json()
            assert "entries" in body and "count" in body

            # timeline
            r = cli.get("/api/dashboard/ops/timeline")
            assert r.status_code == 200, r.text
            assert "issues" in r.json()

            # a2a-health（server_base_url 置为不可达地址，handler 内捕获异常仍返回 200）
            with patch("src.user.db.get_server_base_url", return_value="http://127.0.0.1:1"):
                r = cli.get("/api/dashboard/ops/a2a-health")
                assert r.status_code == 200, r.text
                body = r.json()
                assert "reachable" in body and "recent_a2a" in body
    finally:
        app.dependency_overrides.pop(require_org_member, None)