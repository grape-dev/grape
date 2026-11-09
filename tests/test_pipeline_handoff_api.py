"""跨流水线移交开放接口（POST /api/pipeline/handoff）——契约 + 守卫 + 委托。

覆盖三件事：
1. 契约：路径/operationId/security/x-token-scope 按 tags=["pipeline"] 正确渲染
   （CLI 侧才会出现 `grape pipeline handoff`）；
2. 守卫：缺 payload.target_scene_id 必须同步 400（不是静默退化成场内派发）；
3. 委托：带 target_scene_id 时原样交给 a2a_endpoint，不改写 body。

不启动服务、不碰网络：守卫/委托直接调函数（打桩 a2a_endpoint），契约走 in-process TestClient。
"""
import asyncio
import os
import sys

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from src.api.main import app
from src.api.routes import webhook
from src.api.routes.webhook import A2AHandoffBody, pipeline_handoff_endpoint

client = TestClient(app)


def _call(body, user=None):
    return asyncio.run(pipeline_handoff_endpoint(None, body, user))


def test_contract_exposes_pipeline_handoff():
    s = client.get("/openapi.json").json()
    op = s["paths"]["/api/pipeline/handoff"]["post"]
    assert op["operationId"] == "pipeline_handoff"
    assert op["summary"]
    # 路由级 tags=["webhook"] 会与端点 tags 合并（与 /api/a2a 的 ['webhook','a2a'] 同形），
    # CLI 分组按 operation_id 前缀而非 tags[0]，所以 pipeline_handoff 仍渲染为
    # `grape pipeline handoff`。
    assert "pipeline" in op["tags"]
    assert {"apiKey": []} in op["security"]
    assert op["x-token-scope"] == "dual"


def test_missing_target_scene_id_is_400():
    with pytest.raises(HTTPException) as ei:
        _call(A2AHandoffBody(agent="coding", payload={"repository": "o/r", "issue_number": "1"}))
    assert ei.value.status_code == 400
    assert "target_scene_id" in str(ei.value.detail)


def test_blank_target_scene_id_is_400():
    with pytest.raises(HTTPException) as ei:
        _call(A2AHandoffBody(agent="coding", payload={"repository": "o/r",
                                                     "issue_number": "1",
                                                     "target_scene_id": "   "}))
    assert ei.value.status_code == 400


def test_missing_payload_is_400_not_crash():
    with pytest.raises(HTTPException) as ei:
        _call(A2AHandoffBody(agent="coding", payload={}))
    assert ei.value.status_code == 400


def test_http_route_is_wired_and_guards():
    """真实走一次 HTTP：路径注册 + body 解析 + 守卫都在 FastAPI 层生效（不只是直调函数）。"""
    r = client.post("/api/pipeline/handoff", json={
        "agent": "coding",
        "payload": {"repository": "o/r", "issue_number": "1"},
    })
    assert r.status_code == 400
    assert "target_scene_id" in r.text


def test_delegates_to_a2a_with_target_scene(monkeypatch):
    seen = {}

    async def fake_a2a(request, body, user):
        seen["request"] = request
        seen["body"] = body
        seen["user"] = user
        return {"status": "ok", "seq": 7, "agent": body.agent}

    monkeypatch.setattr(webhook, "a2a_endpoint", fake_a2a)

    body = A2AHandoffBody(agent="coding", payload={
        "repository": "o/r", "issue_number": "1",
        "target_scene_id": "custom-abc", "target_node": "coding",
    })
    out = _call(body, user={"id": "u1"})

    assert out == {"status": "ok", "seq": 7, "agent": "coding"}
    assert seen["body"] is body          # 不改写请求体
    assert seen["user"] == {"id": "u1"}  # 认证结果原样透传