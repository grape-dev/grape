"""Issue #9427 回归：四进程拆分后 /api/a2a 的契约不能丢。

背景：#699 Phase 4 把 webhook 路由（含 POST /api/a2a）只挂在 Event 进程，
而 CLI 按 Mgmt 的 /openapi.json 生成命令 —— 契约少了 /api/a2a，
`grape a2a handoff` 就不存在，agent 无法交办下游节点，流水线停在 issue 阶段
（flask-blog Issue #18 / event #9427 实测）。

覆盖：
- Mgmt 契约里能查到 a2a_handoff / a2a_status，token scope 为 dual
- a2a 条目是「契约合并」而非在 Mgmt 进程真的注册了路由（否则会与 Event 双跑流水线）
"""
import os

os.environ.setdefault("GRAPE_MODE", "cluster")

from fastapi.routing import APIRoute

from src.api.grape_server import app
from src.api.openapi import _iter_routes


def test_mgmt_contract_includes_a2a_handoff():
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/a2a" in paths, "Mgmt 契约缺 /api/a2a，CLI 将无法生成 a2a handoff 命令"
    post = paths["/api/a2a"]["post"]
    assert post["operationId"] == "a2a_handoff"
    assert post.get("x-token-scope") == "dual"
    assert "requestBody" in post

    assert "/api/a2a/status" in paths
    get_op = paths["/api/a2a/status"]["get"]
    assert get_op["operationId"] == "a2a_status"
    assert get_op.get("x-token-scope") == "dual"


def test_a2a_is_contract_only_not_a_mgmt_route():
    """契约补条目 ≠ 在 Mgmt 进程注册路由：真实处理函数只应存在于 Event 进程。"""
    a2a_routes = [
        r.path
        for route in app.routes
        for r in _iter_routes(route)
        if isinstance(r, APIRoute) and r.path.startswith("/api/a2a")
    ]
    assert a2a_routes == []