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
from src.api import auth_token, openapi
from src.api.jwt_utils import create_access_token
from src.user import db as user_db

client = TestClient(app)

# 预期进契约的对外端点（M1.4/M3 显式 operation_id）
EXPECTED_PUBLIC = {
    "tasks_complete": ("post", "/api/tasks/complete"),
    "tasks_current": ("get", "/api/tasks/current"),
    "tasks_notify": ("post", "/api/tasks/notify"),
    "issues_awaiting-input": ("post", "/api/issues/{issue_id}/awaiting-input"),
    "issues_pending-review": ("post", "/api/issues/{issue_id}/pending-review"),
    "issues_review-pass": ("post", "/api/issues/{issue_id}/review-pass"),
    "issues_review-reject": ("post", "/api/issues/{issue_id}/review-reject"),
    # POST /api/tasks/create 已废弃移除（C2）→ 创建走 POST /api/issues/{owner}/{repo}
    "a2a_handoff": ("post", "/api/a2a"),
    # 合并引入的新公开操作（multi-vcs / knowledge detail / profile）
    "auth_profile": ("get", "/api/auth/profile"),
    "auth_logout": ("post", "/api/auth/logout"),
    "knowledge_stats": ("get", "/api/knowledge/stats"),
    "knowledge_detail": ("get", "/api/knowledge/{knowledge_id}"),
    "repos_member": ("get", "/api/repos/member"),
    "repos_managed": ("get", "/api/repos/managed"),
    "repos_deploy_url": ("get", "/api/repos/deploy-url/{owner}/{repo_name}"),
    "repos_git_credential": ("get", "/api/repos/{owner}/{repo}/git-credential"),
    "repos_workspace": ("get", "/api/repos/{owner}/{repo}/workspace"),
    "agents_detail": ("get", "/api/agents/{name}/{owner}/{repo}"),
    "chat_conversations": ("get", "/api/chat/conversations"),
    "chat_messages": ("get", "/api/chat/messages/{conv_id}"),
    "skills_list": ("get", "/api/skills"),
    "skills_categories": ("get", "/api/skills/categories"),
    "skills_detail": ("get", "/api/skills/by-id/{skill_id}"),
    "scenes_list": ("get", "/api/scenes"),
    "knowledge_list": ("get", "/api/knowledge"),
    "knowledge_detail": ("get", "/api/knowledge/{knowledge_id}"),
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
    "gitrepo_issues_close": ("post", "/api/git-repo/{owner}/{repo}/issues/{number}/close"),
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
    # ===== A 类列表归位（tasks-issues-api-rebound-design.md）：mine / repo 迁 /api/issues =====
    "issues_mine": ("get", "/api/issues/mine"),
    # ===== Issue #113: 任务中心仓库全部视图 =====
    # ===== A 批（openapi-full-coverage-design.md）：skills/scenes 全量进契约 =====
    "skills_upload": ("post", "/api/skills/upload"),
    "skills_like_toggle": ("post", "/api/skills/{skill_id}/like"),
    "skills_stats": ("get", "/api/skills/stats"),
    "skills_search": ("get", "/api/skills/search"),
    "skills_featured": ("get", "/api/skills/featured"),
    "skills_download": ("get", "/api/skills/by-id/{skill_id}/download"),
    "skills_delete": ("delete", "/api/skills/by-id/{skill_id}"),
    "skills_convert": ("post", "/api/skills/convert"),
    "skills_sub_list": ("get", "/api/skills/{skill_id}/sub"),
    "skills_sub_get": ("get", "/api/skills/{skill_id}/sub/{sub_name}"),
    "skills_package_sub_get": ("get", "/api/skills/package-sub/{pkg_id}/{sub_name}"),
    "scenes_create": ("post", "/api/scenes"),
    "scenes_with_pipeline": ("get", "/api/scenes/with-pipeline"),
    "scenes_market_create": ("post", "/api/scenes/market"),
    "scenes_market_list": ("get", "/api/scenes/market"),
    "scenes_market_categories": ("get", "/api/scenes/market/categories"),
    "scenes_market_detail": ("get", "/api/scenes/market/{market_id}"),
    "scenes_market_update": ("put", "/api/scenes/market/{market_id}"),
    "scenes_market_delete": ("delete", "/api/scenes/market/{market_id}"),
    "scenes_market_applied": ("get", "/api/scenes/market/{market_id}/applied"),
    "scenes_market_apply": ("post", "/api/scenes/market/{market_id}/apply"),
    "scenes_market_experience_check": ("get", "/api/scenes/market/{market_id}/experience-check"),
    "scenes_market_experience": ("post", "/api/scenes/market/{market_id}/experience"),
    "scenes_market_like_toggle": ("post", "/api/scenes/market/{market_id}/like"),
    "scenes_market_icon_upload": ("post", "/api/scenes/market/icon-upload"),
    "scenes_check_name": ("get", "/api/scenes/check-name"),
    "scenes_skills_for_agent": ("get", "/api/scenes/skills/{agent_type}"),
    "scenes_apply": ("post", "/api/scenes/apply/{owner}/{repo}"),
    "scenes_branches": ("get", "/api/scenes/branches/{owner}/{repo}"),
    "scenes_branch_create": ("post", "/api/scenes/branches/{owner}/{repo}"),
    "scenes_detail": ("get", "/api/scenes/{scene_id}"),
    "scenes_update": ("put", "/api/scenes/{scene_id}"),
    "scenes_delete": ("delete", "/api/scenes/{scene_id}"),
    "scenes_advanced_options_get": ("get", "/api/scenes/{scene_id}/advanced-options"),
    "scenes_advanced_options_set": ("put", "/api/scenes/{scene_id}/advanced-options"),
    # ===== B 批（openapi-full-coverage-design.md）：settings/repos/agents/knowledge 全量进契约 =====
    "settings_credentials_get": ("get", "/api/settings/credentials"),
    "settings_credentials_update": ("put", "/api/settings/credentials"),
    "settings_key_create": ("post", "/api/settings/api-keys"),
    "settings_keys": ("get", "/api/settings/api-keys"),
    "settings_key_delete": ("delete", "/api/settings/api-keys/{key_id}"),
    "settings_notifications_get": ("get", "/api/settings/notifications"),
    "settings_notifications_update": ("put", "/api/settings/notifications"),
    "settings_oauth_get": ("get", "/api/settings/oauth"),
    "settings_oauth_update": ("put", "/api/settings/oauth"),
    "settings_oauth_github_get": ("get", "/api/settings/oauth-github"),
    "settings_oauth_github_update": ("put", "/api/settings/oauth-github"),
    "settings_gitcode_repo_get": ("get", "/api/settings/gitcode-repo/{owner}/{repo}"),
    "settings_gitcode_repo_update": ("put", "/api/settings/gitcode-repo/{owner}/{repo}"),
    "settings_agent_runtimes": ("get", "/api/settings/agent-runtimes"),
    "settings_runtime_devices": ("get", "/api/settings/runtime-devices"),
    "settings_backend_agents": ("get", "/api/settings/backend-agents"),
    "settings_agent_runtimes_refresh": ("post", "/api/settings/agent-runtimes/refresh"),
    "settings_backend_agents_refresh": ("post", "/api/settings/backend-agents/refresh"),
    "settings_runtime_credential_create": ("post", "/api/settings/runtime-credentials"),
    "settings_runtime_credential_rename": ("put", "/api/settings/runtime-credentials/{cid}"),
    "settings_runtime_credential_scope": ("put", "/api/settings/runtime-credentials/{cid}/scope"),
    "settings_runtime_credentials": ("get", "/api/settings/runtime-credentials"),
    "settings_runtime_credential_revoke": ("post", "/api/settings/runtime-credentials/{cid}/revoke"),
    "settings_execution_mode_get": ("get", "/api/settings/execution-mode"),
    "settings_execution_mode_set": ("put", "/api/settings/execution-mode"),
    "settings_pipeline_mode_get": ("get", "/api/settings/pipeline-mode"),
    "settings_pipeline_mode_set": ("put", "/api/settings/pipeline-mode"),
    "settings_engine_tags": ("get", "/api/settings/agent-runtimes/{rid}/tags"),
    "settings_engine_tag_create": ("post", "/api/settings/agent-runtimes/{rid}/tags"),
    "settings_engine_tag_delete": ("delete", "/api/settings/agent-runtimes/{rid}/tags/{tag}"),
    "settings_engine_tag_update": ("put", "/api/settings/agent-runtimes/{rid}/tags/{tag}"),
    "settings_event_action_filters": ("get", "/api/settings/event-action-filters"),
    "settings_event_action_rule_create": ("post", "/api/settings/event-action-filters"),
    "settings_event_action_rule_delete": ("delete", "/api/settings/event-action-filters/{rule_id}"),
    "settings_event_action_rule_update": ("put", "/api/settings/event-action-filters/{rule_id}"),
    "repos_check_name": ("get", "/api/repos/check-name"),
    "repos_sync": ("post", "/api/repos/sync"),
    "repos_create": ("post", "/api/repos/create"),
    "repos_host": ("post", "/api/repos/host"),
    "repos_bot_token_get": ("get", "/api/repos/bot-token/{owner}/{repo_name}"),
    "repos_bot_token_set": ("put", "/api/repos/bot-token"),
    "repos_deploy_url_set": ("put", "/api/repos/deploy-url"),
    "repos_remove": ("post", "/api/repos/remove"),
    "repos_repair_host": ("post", "/api/repos/{owner}/{repo}/repair-host"),
    "repos_project_info": ("get", "/api/repos/{owner}/{repo}/project-info"),
    "repos_scene_recommend_config_get": ("get", "/api/repos/{owner}/{repo}/scene-recommend-config"),
    "repos_scene_recommend_config_set": ("put", "/api/repos/{owner}/{repo}/scene-recommend-config"),
    "agents_update": ("put", "/api/agents/{name}/{owner}/{repo}"),
    "agents_clean": ("post", "/api/agents/{name}/{owner}/{repo}/clean"),
    "knowledge_entry_create": ("post", "/api/knowledge/entry"),
    "knowledge_delete": ("delete", "/api/knowledge/{knowledge_id}"),
    "knowledge_update": ("put", "/api/knowledge/{knowledge_id}"),
    "knowledge_refs_list": ("get", "/api/knowledge/{knowledge_id}/refs"),
    "knowledge_ref_create": ("post", "/api/knowledge/{knowledge_id}/refs"),
    "knowledge_add_refs_batch": ("post", "/api/knowledge/{knowledge_id}/refs/batch"),
    "knowledge_ref_update": ("put", "/api/knowledge/ref/{ref_id}"),
    "knowledge_ref_delete": ("delete", "/api/knowledge/ref/{ref_id}"),
    # ===== C 批（openapi-full-coverage-design.md）：tasks/events 用户操作类进契约 =====
    "events_terminate": ("post", "/api/events/{seq}/terminate"),
    "events_redispatch": ("post", "/api/events/{seq}/redispatch"),
    "events_thread": ("get", "/api/events/{seq}/thread"),
    "events_chat_delete": ("delete", "/api/events/{seq}/chat"),
    "events_knowledge_delete": ("delete", "/api/events/{seq}/knowledge/{scope}"),
    "events_chat_send": ("post", "/api/events/{seq}/chat"),
    "events_chat_stream": ("post", "/api/events/{seq}/chat/stream"),
    "events_chat_submit": ("post", "/api/events/{seq}/chat/submit"),
    "events_chat_task_status": ("get", "/api/events/chat/task/{task_id}/status"),
    "events_chat_task_stream": ("get", "/api/events/chat/task/{task_id}/stream"),
    "events_list": ("get", "/api/events/{owner}/{repo}"),
    # ===== C 批续（openapi-full-coverage）：runtime/chat 用户操作类进契约 =====
    "runtime_event_bus": ("get", "/api/runtime/admin/event-bus"),
    "runtime_machine_health": ("get", "/api/runtime/admin/machine-health"),
    "runtime_machine_health_detail": ("get", "/api/runtime/admin/machine-health/{runtime_id}"),
    "runtime_batch_recover": ("post", "/api/runtime/admin/batch-recover"),
    "runtime_task_status": ("get", "/api/runtime/tasks/{task_id}/status"),
    "runtime_task_cancel": ("post", "/api/runtime/tasks/{task_id}/cancel"),
    "runtime_tasks_list": ("get", "/api/runtime/runtimes/{runtime_id}/tasks"),
    "runtime_sessions_list": ("get", "/api/runtime/runtimes/{runtime_id}/sessions"),
    "runtime_credential_tasks": ("get", "/api/runtime/credentials/{cid}/tasks"),
    "runtime_refresh_skills": ("post", "/api/runtime/runtimes/{runtime_id}/refresh-skills"),
    "runtime_role_tags": ("get", "/api/runtime/runtimes/{runtime_id}/role-tags"),
    "runtime_tags_set": ("put", "/api/runtime/runtimes/{runtime_id}/tags"),
    "runtime_alias_set": ("put", "/api/runtime/runtimes/{runtime_id}/alias"),
    "runtime_max_concurrent_set": ("put", "/api/runtime/runtimes/{runtime_id}/max-concurrent"),
    "runtime_scene_tags_list": ("get", "/api/runtime/scene-tags"),
    "runtime_scene_tags_get": ("get", "/api/runtime/runtimes/{runtime_id}/scene-tags"),
    "runtime_scene_tags_set": ("put", "/api/runtime/runtimes/{runtime_id}/scene-tags"),
    "chat_conversations_create": ("post", "/api/chat/conversations"),
    "chat_conversations_delete": ("delete", "/api/chat/conversations/{conv_id}"),
    "chat_conversations_ensure_issue": ("post", "/api/chat/conversations/ensure-issue"),
    "chat_send": ("post", "/api/chat/send"),
    "chat_send_stream": ("post", "/api/chat/send/stream"),
    "chat_send_async": ("post", "/api/chat/send/async"),
    "chat_feedback": ("post", "/api/chat/feedback"),
    "chat_config_get": ("get", "/api/chat/config"),
    "chat_config_update": ("put", "/api/chat/config"),
    "chat_submit": ("post", "/api/chat/submit"),
    "chat_task_status": ("get", "/api/chat/task/{task_id}/status"),
    "chat_task_stream": ("get", "/api/chat/task/{task_id}/stream"),
    # ===== D 批（openapi-full-coverage-design.md）：devstage/templates/dashboard/pipeline/feishu/task-logs 进契约 =====
    "devstage_config_get": ("get", "/api/devstage/config"),
    "devstage_config_update": ("put", "/api/devstage/config"),
    "devstage_templates": ("get", "/api/devstage/templates"),
    "devstage_specs": ("get", "/api/devstage/specs"),
    "devstage_environments": ("get", "/api/devstage/environments"),
    "devstage_environment_create": ("post", "/api/devstage/environments"),
    "devstage_environment_delete": ("delete", "/api/devstage/environments/{devstage_id}"),
    "devstage_environment_action": ("post", "/api/devstage/environments/{action}"),
    "project_template_create": ("post", "/api/project-templates/create"),
    "project_templates_list": ("get", "/api/project-templates"),
    "project_template_preview": ("get", "/api/project-templates/{template_id}/preview"),
    "project_template_readme": ("get", "/api/project-templates/{template_id}/readme"),
    "dashboard_ops_overview": ("get", "/api/dashboard/ops/overview"),
    "dashboard_repo": ("get", "/api/dashboard/{owner}/{repo}"),
    "pipeline_get": ("get", "/api/pipeline/{owner}/{repo}"),
    "search_global": ("get", "/api/search"),
    "feishu_onboard_start": ("post", "/api/feishu/onboard/start"),
    "feishu_onboard_status": ("get", "/api/feishu/onboard/status"),
    "feishu_onboard_cancel": ("post", "/api/feishu/onboard/cancel"),
    "task_logs_messages": ("get", "/api/task-logs/{task_id}/messages"),
    "task_logs_info": ("get", "/api/task-logs/{task_id}/info"),
    "task_logs_stream": ("get", "/api/task-logs/{task_id}/stream"),
    # ===== 合并 weekly-03-hc 新增（2026-09-15）=====
    "skills_agent_stats": ("get", "/api/skills/agent-stats"),
    # ===== B 类操作归位 /api/issues（tasks-issues-api-rebound-design.md §三）=====
    "issues_start": ("post", "/api/issues/{issue_id}/start"),
    "issues_status": ("put", "/api/issues/{issue_id}/status"),
    "issues_status_get": ("get", "/api/issues/{issue_id}/status"),
    "issues_confirm_done": ("post", "/api/issues/{issue_id}/confirm-done"),
    "issues_cancel": ("post", "/api/issues/{issue_id}/cancel"),
    "issues_resume": ("post", "/api/issues/{issue_id}/resume"),
    "issues_pipeline_detail": ("get", "/api/issues/{issue_id}/pipeline"),
    "issues_clarifications": ("get", "/api/issues/{issue_id}/clarifications"),
    "issues_clarify": ("post", "/api/issues/{issue_id}/clarify"),
    "issues_upload_attachment": ("post", "/api/issues/{issue_id}/attachments"),
    "issues_list_attachments": ("get", "/api/issues/{issue_id}/attachments"),
    "issues_delete_attachment": ("delete", "/api/issues/{issue_id}/attachments/{filename}"),
}

# 内部（会话认证）或管理类端点，不应出现在契约
NOT_IN_CONTRACT = [
    "/api/agents/types",                 # 无认证
    "/api/auth/login",
    "/api/runtime/start",                # 管理类模块
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


# ===================== x-token-scope 强制（openapi-x-token-scope-design.md §3.2/§五） =====================
def test_public_ops_have_x_token_scope():
    """L2：每个公开 op 必须有合法 x-token-scope（user_only/session_only/dual），不允许遗漏。"""
    s = _schema()
    for path, ops in s["paths"].items():
        for method, op in ops.items():
            scope = op.get("x-token-scope")
            assert scope in {"user_only", "session_only", "dual"}, \
                f"缺 x-token-scope: {method} {path} -> {scope!r}"


def test_x_token_scope_consistency():
    """L3：session_only 覆盖表与前缀规则 ↔ 契约双相一致；抽查 user_only/dual 命中。"""
    s = _schema()

    # 契约中实际被标 session_only 的 (method, path)
    actual_session_only = {
        (m, p)
        for p, ops in s["paths"].items()
        for m, op in ops.items()
        if op.get("x-token-scope") == "session_only"
    }

    # 1) 覆盖表 / 前缀规则命中的 operation 必须在契约中且被标 session_only
    for path, ops in s["paths"].items():
        for method, op in ops.items():
            op_id = op["operationId"]
            is_session_rule = (
                op_id in openapi._SESSION_ONLY_OPERATIONS
                or path.startswith(openapi._SESSION_ONLY_PATH_PREFIXES)
            )
            if is_session_rule:
                assert (method, path) in actual_session_only, \
                    f"session 规则命中但未标 session_only: {op_id} {method} {path}"

    # 2) 所有标为 session_only 的必须命中规则（防漏登记/误标）
    for method, path in actual_session_only:
        op_id = s["paths"][path][method]["operationId"]
        assert (op_id in openapi._SESSION_ONLY_OPERATIONS
                or path.startswith(openapi._SESSION_ONLY_PATH_PREFIXES)), \
            f"标了 session_only 但未命中规则: {op_id} {method} {path}"

    # 3) 抽样：user_only（D 类管理 / 用户操作类）/ dual（开放读）实例
    user_only_samples = {"/api/skills": "get",
                         "/api/auth/logout": "post", "/api/dashboard/ops/overview": "get",
                         "/api/issues/mine": "get", "/api/issues/{owner}/{repo}": "post",
                         # 用户操作类：状态写/会话预检禁止 tk_（避免 Agent 干扰 pipeline）
                         "/api/issues/{issue_id}/status": "put",
                         "/api/issues/{issue_id}/start": "post",
                         # skills agent-stats：仅 Web UI/用户级消费，禁 session
                         "/api/skills/agent-stats": "get", "/api/skills/stats": "get"}
    for path, method in user_only_samples.items():
        assert s["paths"][path][method]["x-token-scope"] == "user_only", \
            f"期望 user_only: {method} {path}"

    dual_samples = {"/api/issues/{owner}/{repo}": "get", "/api/a2a": "post",
                    # skills upload（repo 技能上传会话可用）、详情与子技能（agent 运行时按需加载）为 dual
                    "/api/skills/upload": "post",
                    "/api/skills/by-id/{skill_id}": "get",
                    "/api/skills/{skill_id}/sub": "get",
                    # Issue 状态只读轮询（CLI wait 用）保持 dual
                    "/api/issues/{issue_id}/status": "get"}
    for path, method in dual_samples.items():
        assert s["paths"][path][method]["x-token-scope"] == "dual", \
            f"期望 dual: {method} {path}"

    # 4) session_only 样例（需求 §四验收）
    session_samples = {"/api/git-repo/{owner}/{repo}/branches": "get",
                       "/api/tasks/current": "get",
                       "/api/tasks/notify": "post",
                       "/api/issues/{issue_id}/awaiting-input": "post",
                       "/api/repos/{owner}/{repo}/git-credential": "get",
                       "/api/repos/{owner}/{repo}/workspace": "get"}
    for path, method in session_samples.items():
        assert (method, path) in actual_session_only, \
            f"期望 session_only: {method} {path}"


def _user():
    import uuid
    u = user_db.create_user(f"openapi_{uuid.uuid4().hex[:6]}", "pw")
    return u["id"]


def test_three_channels_can_call_public_api():
    from src.session_manager import db, db_lock
    with db_lock:  # 清空 token 表，避免残留
        db.cursor().execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
        db.cursor().execute("DELETE FROM api_tokens WHERE token LIKE 'test_%' OR token LIKE 'tk_test%' OR user_id IN (SELECT id FROM user_accounts WHERE username LIKE 'test_%' OR username LIKE 'tc_%')")
        db.commit()
    uid = _user()

    jwt = create_access_token(uid, f"u{uid}")
    api_key = auth_token.generate_api_token(uid, name="t")["token"]
    session_tk = auth_token.issue_session_token(
        session_key=f"conv-{uid}", repo_key="o/r", agent_type="chat", user_id=uid)

    # JWT
    assert client.get("/api/issues/mine",
                      headers={"Authorization": f"Bearer {jwt}"}).status_code == 200
    # api_key 三通道
    assert client.get("/api/issues/mine",
                      headers={"Authorization": f"Bearer {api_key}"}).status_code == 200
    assert client.get("/api/issues/mine",
                      headers={"PRIVATE-TOKEN": api_key}).status_code == 200
    assert client.get("/api/issues/mine?access_token=" + api_key).status_code == 200
    # session token（chat 会话携带 user_id）→ mine 为 user_only，session 应 403
    assert client.get("/api/issues/mine",
                      headers={"Authorization": f"Bearer {session_tk}"}).status_code == 403
    # Issue 状态写（PUT /status）/ start 为 user_only：session token → 403；api_key 通过守卫（无该 issue → 404）
    assert client.put("/api/issues/999999/status", json={"status": "processing"},
                      headers={"Authorization": f"Bearer {session_tk}"}).status_code == 403
    assert client.post("/api/issues/999999/start",
                       headers={"Authorization": f"Bearer {session_tk}"}).status_code == 403
    assert client.put("/api/issues/999999/status", json={"status": "processing"},
                      headers={"Authorization": f"Bearer {api_key}"}).status_code == 404
    # GET /status 为 dual（读）：session token 可达（无该 issue → 404，而非 403）
    assert client.get("/api/issues/999999/status",
                      headers={"Authorization": f"Bearer {session_tk}"}).status_code == 404
    # 场景/技能/事件详情读类接口也走 token
    assert client.get("/api/scenes",
                      headers={"Authorization": f"Bearer {api_key}"}).status_code == 200
    assert client.get("/api/scenes",
                      headers={"PRIVATE-TOKEN": api_key}).status_code == 200


def test_public_api_401_without_or_invalid_token():
    assert client.get("/api/issues/mine").status_code == 401
    assert client.get("/api/issues/mine",
                      headers={"Authorization": "Bearer <bad-token>"}).status_code == 401
    assert client.get("/api/scenes",
                      headers={"PRIVATE-TOKEN": "grape_bad"}).status_code == 401