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
    "tasks_status_get": ("get", "/api/tasks/{task_id}/status"),
    "tasks_complete": ("post", "/api/tasks/complete"),
    "a2a_handoff": ("post", "/api/a2a"),
    # 合并引入的新公开操作（multi-vcs / knowledge detail / profile）
    "get_profile_api_auth_profile_get": ("get", "/api/auth/profile"),
    "knowledge_stats": ("get", "/api/knowledge/stats"),
    "get_knowledge_detail_api_knowledge__knowledge_id__get": ("get", "/api/knowledge/{knowledge_id}"),
    "repos_member": ("get", "/api/repos/member"),
    "repos_managed": ("get", "/api/repos/managed"),
    "repos_deploy_url": ("get", "/api/repos/deploy-url/{owner}/{repo_name}"),
    "repos_git_credential": ("get", "/api/repos/{owner}/{repo}/git-credential"),
    "repos_workspace": ("get", "/api/repos/{owner}/{repo}/workspace"),
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
    # ===== Issue-45 M3：Git 仓库统一接口（/api/git-repo 平台透传；git-credential/workspace 归 /api/repos 平台）=====
    "gitrepo_repo": ("get", "/api/git-repo/{owner}/{repo}"),
    "gitrepo_branches": ("get", "/api/git-repo/{owner}/{repo}/branches"),
    "gitrepo_commits": ("get", "/api/git-repo/{owner}/{repo}/commits"),
    "gitrepo_contents": ("get", "/api/git-repo/{owner}/{repo}/contents/{path}"),
    "gitrepo_labels_list": ("get", "/api/git-repo/{owner}/{repo}/labels"),
    "gitrepo_labels_create": ("post", "/api/git-repo/{owner}/{repo}/labels"),
    "gitrepo_milestones": ("get", "/api/git-repo/{owner}/{repo}/milestones"),
    "gitrepo_issues_list": ("get", "/api/git-repo/{owner}/{repo}/issues"),
    "gitrepo_issues_create": ("post", "/api/git-repo/{owner}/{repo}/issues"),
    "gitrepo_issues_get": ("get", "/api/git-repo/{owner}/{repo}/issues/{number}"),
    "gitrepo_issues_update": ("patch", "/api/git-repo/{owner}/{repo}/issues/{number}"),
    "gitrepo_issues_comments_list": ("get", "/api/git-repo/{owner}/{repo}/issues/{number}/comments"),
    "gitrepo_issues_comments_create": ("post", "/api/git-repo/{owner}/{repo}/issues/{number}/comments"),
    "gitrepo_pulls_list": ("get", "/api/git-repo/{owner}/{repo}/pulls"),
    "gitrepo_pulls_create": ("post", "/api/git-repo/{owner}/{repo}/pulls"),
    "gitrepo_pulls_get": ("get", "/api/git-repo/{owner}/{repo}/pulls/{number}"),
    "gitrepo_pulls_update": ("patch", "/api/git-repo/{owner}/{repo}/pulls/{number}"),
    "gitrepo_pulls_merge": ("put", "/api/git-repo/{owner}/{repo}/pulls/{number}/merge"),
    "gitrepo_pulls_comments_list": ("get", "/api/git-repo/{owner}/{repo}/pulls/{number}/comments"),
    "gitrepo_pulls_comments_create": ("post", "/api/git-repo/{owner}/{repo}/pulls/{number}/comments"),
    "gitrepo_pulls_files": ("get", "/api/git-repo/{owner}/{repo}/pulls/{number}/files"),
    # ===== Issue #114: git-repo tree / contents root 端点 =====
    "gitrepo_tree": ("get", "/api/git-repo/{owner}/{repo}/tree"),
    "gitrepo_contents_root": ("get", "/api/git-repo/{owner}/{repo}/contents"),
    # ===== Issues 对外接口（/api/issues，issues-api-design.md）=====
    "issues_list": ("get", "/api/issues/{owner}/{repo}"),
    "issues_create": ("post", "/api/issues/{owner}/{repo}"),
    "issues_detail": ("get", "/api/issues/{owner}/{repo}/{number}"),
    "issues_events": ("get", "/api/issues/{owner}/{repo}/{number}/events"),
    "issues_pipeline": ("get", "/api/issues/{owner}/{repo}/{number}/pipeline"),
    # ===== Issue #113: 任务中心仓库全部视图 =====
    "tasks_repo_all": ("get", "/api/tasks/repo/{owner}/{repo}"),
}

# 内部（会话认证）或管理类端点，不应出现在契约
NOT_IN_CONTRACT = [
    "/api/agents/types",                 # 无认证
    "/api/scenes/with-pipeline",         # get_current_user
    "/api/scenes/{scene_id}",            # get_current_user（CRUD）
    "/api/tasks/{task_id}/clarifications",  # get_current_user
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
    assert client.get("/api/scenes",
                      headers={"PRIVATE-TOKEN": api_key}).status_code == 200


def test_public_api_401_without_or_invalid_token():
    assert client.get("/api/tasks/mine").status_code == 401
    assert client.get("/api/tasks/mine",
                      headers={"Authorization": "Bearer <bad-token>"}).status_code == 401
    assert client.get("/api/scenes",
                      headers={"PRIVATE-TOKEN": "grape_bad"}).status_code == 401