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

# 预期进契约的对外端点（M1.4/M3 显式 operation_id）
EXPECTED_PUBLIC = {
    "tasks_mine": ("get", "/api/tasks/mine"),
    "tasks_create": ("post", "/api/tasks/create"),
    "tasks_start": ("post", "/api/tasks/{task_id}/start"),
    "tasks_status": ("put", "/api/tasks/{task_id}/status"),
    "repos_member": ("get", "/api/repos/member"),
    "repos_managed": ("get", "/api/repos/managed"),
    "repos_deploy_url": ("get", "/api/repos/deploy-url/{owner}/{repo_name}"),
    "agents_detail": ("get", "/api/agents/{name}/{owner}/{repo}"),
    "chat_conversations": ("get", "/api/chat/conversations"),
    "chat_messages": ("get", "/api/chat/messages/{conv_id}"),
    "skills_list": ("get", "/api/skills"),
    "skills_detail": ("get", "/api/skills/by-id/{skill_id}"),
    "scenes_list": ("get", "/api/scenes"),
    "knowledge_list": ("get", "/api/knowledge"),
    "knowledge_content": ("get", "/api/knowledge/content"),
    "knowledge_context": ("get", "/api/knowledge/context"),
    "events_detail": ("get", "/api/events/detail/{seq}"),
    # ===== Issue-45 M3：Git 仓库统一接口 =====
    "gitrepo_repo": ("get", "/api/repos/{owner}/{repo}"),
    "gitrepo_workspace": ("get", "/api/repos/{owner}/{repo}/workspace"),
    "gitrepo_git_credential": ("get", "/api/repos/{owner}/{repo}/git-credential"),
    "gitrepo_branches": ("get", "/api/repos/{owner}/{repo}/branches"),
    "gitrepo_commits": ("get", "/api/repos/{owner}/{repo}/commits"),
    "gitrepo_contents": ("get", "/api/repos/{owner}/{repo}/contents/{path}"),
    "gitrepo_labels": ("get", "/api/repos/{owner}/{repo}/labels"),
    "gitrepo_labels_create": ("post", "/api/repos/{owner}/{repo}/labels"),
    "gitrepo_milestones": ("get", "/api/repos/{owner}/{repo}/milestones"),
    "gitrepo_issues": ("get", "/api/repos/{owner}/{repo}/issues"),
    "gitrepo_issues_create": ("post", "/api/repos/{owner}/{repo}/issues"),
    "gitrepo_issues_get": ("get", "/api/repos/{owner}/{repo}/issues/{number}"),
    "gitrepo_issues_update": ("patch", "/api/repos/{owner}/{repo}/issues/{number}"),
    "gitrepo_issues_comments": ("get", "/api/repos/{owner}/{repo}/issues/{number}/comments"),
    "gitrepo_issues_comments_create": ("post", "/api/repos/{owner}/{repo}/issues/{number}/comments"),
    "gitrepo_pulls": ("get", "/api/repos/{owner}/{repo}/pulls"),
    "gitrepo_pulls_create": ("post", "/api/repos/{owner}/{repo}/pulls"),
    "gitrepo_pulls_get": ("get", "/api/repos/{owner}/{repo}/pulls/{number}"),
    "gitrepo_pulls_update": ("patch", "/api/repos/{owner}/{repo}/pulls/{number}"),
    "gitrepo_pulls_merge": ("put", "/api/repos/{owner}/{repo}/pulls/{number}/merge"),
    "gitrepo_pulls_comments": ("get", "/api/repos/{owner}/{repo}/pulls/{number}/comments"),
    "gitrepo_pulls_comments_create": ("post", "/api/repos/{owner}/{repo}/pulls/{number}/comments"),
    "gitrepo_pulls_files": ("get", "/api/repos/{owner}/{repo}/pulls/{number}/files"),
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
    assert {p for _, p in EXPECTED_PUBLIC.values()} <= paths
    for p in NOT_IN_CONTRACT:
        assert p not in paths, f"内部路由不应进契约: {p}"


def test_public_ops_have_operation_id_summary_security():
    s = _schema()
    op_ids = set()
    seen = set()
    for path, ops in s["paths"].items():
        for method, op in ops.items():
            assert op.get("operationId"), f"缺 operation_id: {path} {method}"
            assert op.get("summary"), f"缺 summary: {path} {method}"
            assert op.get("security"), f"缺 security: {path} {method}"
            assert {"apiKey": []} in op["security"]
            op_ids.add(op["operationId"])
            seen.add((method, path))
    assert op_ids == set(EXPECTED_PUBLIC)
    assert seen == set(EXPECTED_PUBLIC.values())


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