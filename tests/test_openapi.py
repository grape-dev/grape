"""Issue-45 M1：OpenAPI 对外暴露 + 对外安全路由契约测试.

覆盖：
- /docs /redoc /openapi.json 可访问
- 契约版本 2.1.0 + 三通道 securitySchemes
- 仅对外安全路由进契约（16 个端点），管理类/会话认证路由被过滤
- 每 operation 有 operation_id / summary / security
- 三通道认证调实际对外端点（JWT / api_key / session_token）
"""
import os
import sys

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from fastapi.testclient import TestClient
from src.api.main import app
from src.api import auth_token
from src.api.jwt_utils import create_access_token
from src.user import db as user_db

client = TestClient(app)

# 预期进契约的对外端点（M1.4 显式 operation_id）
EXPECTED_PUBLIC = {
    "/api/tasks/mine": "tasks_mine",
    "/api/tasks/create": "tasks_create",
    "/api/tasks/{task_id}/start": "tasks_start",
    "/api/tasks/{task_id}/status": "tasks_status",
    "/api/repos/member": "repos_member",
    "/api/repos/managed": "repos_managed",
    "/api/repos/deploy-url/{owner}/{repo_name}": "repos_deploy_url",
    "/api/agents/{name}/{owner}/{repo}": "agents_detail",
    "/api/chat/conversations": "chat_conversations",
    "/api/chat/messages/{conv_id}": "chat_messages",
    "/api/skills": "skills_list",
    "/api/skills/by-id/{skill_id}": "skills_detail",
    "/api/scenes": "scenes_list",
    "/api/knowledge": "knowledge_list",
    "/api/knowledge/content": "knowledge_content",
    "/api/knowledge/context": "knowledge_context",
    "/api/events/detail/{seq}": "events_detail",
}

# 内部（会话认证）或管理类端点，不应出现在契约
NOT_IN_CONTRACT = [
    "/api/agents/types",                 # 无认证
    "/api/scenes/with-pipeline",         # get_current_user
    "/api/scenes/{scene_id}",            # get_current_user（CRUD）
    "/api/tasks/{task_id}/clarifications",  # get_current_user
    "/api/auth/profile",                 # 管理类模块
    "/api/auth/login",
    "/api/settings",                     # 管理类模块
    "/api/dashboard/ops/overview",       # 管理类模块
    "/api/runtime/start",                # 管理类模块
    "/api/repos/host",                   # get_current_user（托管）
    "/api/skills/upload",                # get_current_user（上传）
]


def _schema():
    r = client.get("/openapi.json")
    assert r.status_code == 200
    return r.json()


def test_docs_endpoints_accessible():
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_contract_version_and_security_schemes():
    s = _schema()
    assert s["info"]["version"] == "2.1.0"
    schemes = s["components"]["securitySchemes"]
    assert set(schemes) == {"apiKey", "privateKey", "accessToken"}
    assert schemes["apiKey"]["scheme"] == "bearer"
    assert schemes["privateKey"]["in"] == "header"
    assert schemes["accessToken"]["in"] == "query"


def test_only_public_security_routes_in_contract():
    s = _schema()
    paths = set(s["paths"])
    assert EXPECTED_PUBLIC.keys() <= paths
    for p in NOT_IN_CONTRACT:
        assert p not in paths, f"内部路由不应进契约: {p}"


def test_public_ops_have_operation_id_summary_security():
    s = _schema()
    op_ids = []
    for path, ops in s["paths"].items():
        for method, op in ops.items():
            assert op.get("operationId"), f"缺 operation_id: {path} {method}"
            assert op.get("summary"), f"缺 summary: {path} {method}"
            assert op.get("security"), f"缺 security: {path} {method}"
            assert {"apiKey": []} in op["security"]
            op_ids.append(op["operationId"])
    assert set(op_ids) == set(EXPECTED_PUBLIC.values())


def _user():
    import uuid
    u = user_db.create_user(f"openapi_{uuid.uuid4().hex[:6]}", "pw")
    return u["id"]


def test_three_channels_can_call_public_api():
    from src.session_manager import db, db_lock
    with db_lock:  # 清空 token 表，避免残留
        db.cursor().execute("DELETE FROM session_api_tokens")
        db.cursor().execute("DELETE FROM api_tokens")
        db.commit()
    uid = _user()

    jwt = create_access_token(uid, f"u{uid}")
    api_key = auth_token.generate_api_token(uid, name="t")["token"]
    session_tk = auth_token.issue_session_token(
        session_key=f"conv-{uid}", repo_key="o/r", agent_type="chat", user_id=uid)

    # JWT
    assert client.get("/api/tasks/mine",
                      headers={"Authorization": f"Bearer {jwt}"}).status_code == 200
    # api_key 三通道
    assert client.get("/api/tasks/mine",
                      headers={"Authorization": f"Bearer {api_key}"}).status_code == 200
    assert client.get("/api/tasks/mine",
                      headers={"PRIVATE-TOKEN": api_key}).status_code == 200
    assert client.get("/api/tasks/mine?access_token=" + api_key).status_code == 200
    # session token（chat 会话携带 user_id）
    assert client.get("/api/tasks/mine",
                      headers={"Authorization": f"Bearer {session_tk}"}).status_code == 200
    # 场景/技能/事件详情读类接口也走 token
    assert client.get("/api/scenes",
                      headers={"Authorization": f"Bearer {api_key}"}).status_code == 200
    assert client.get("/api/knowledge?repo=o/r",
                      headers={"PRIVATE-TOKEN": api_key}).status_code == 200


def test_public_api_401_without_or_invalid_token():
    assert client.get("/api/tasks/mine").status_code == 401
    assert client.get("/api/tasks/mine",
                      headers={"Authorization": "Bearer <bad-token>"}).status_code == 401
    assert client.get("/api/scenes",
                      headers={"PRIVATE-TOKEN": "grape_bad"}).status_code == 401