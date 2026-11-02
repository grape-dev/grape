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
from src.auth import grape_account_db

client = TestClient(app)

# 预期进契约的对外端点（M1.4/M3 显式 operation_id）
EXPECTED_PUBLIC = {
    "a2a_handoff": ("post", "/api/a2a"),
    "a2a_status": ("get", "/api/a2a/status"),
    "agents_clean": ("post", "/api/agents/{name}/{owner}/{repo}/clean"),
    "agents_detail": ("get", "/api/agents/{name}/{owner}/{repo}"),
    "agents_update": ("put", "/api/agents/{name}/{owner}/{repo}"),
    "auth_logout": ("post", "/api/auth/logout"),
    "auth_profile": ("get", "/api/auth/profile"),
    "bind_provider_api_auth_bind__provider__get": ("get", "/api/auth/bind/{provider}"),
    "change_email_api_auth_change_email_post": ("post", "/api/auth/change-email"),
    "change_email_send_code_api_auth_change_email_send_code_post": ("post", "/api/auth/change-email/send-code"),
    "change_password_api_auth_change_password_post": ("post", "/api/auth/change-password"),
    "chat_config_get": ("get", "/api/chat/config"),
    "chat_config_update": ("put", "/api/chat/config"),
    "chat_conversations": ("get", "/api/chat/conversations"),
    "chat_conversations_create": ("post", "/api/chat/conversations"),
    "chat_conversations_delete": ("delete", "/api/chat/conversations/{conv_id}"),
    "chat_conversations_ensure_issue": ("post", "/api/chat/conversations/ensure-issue"),
    "chat_feedback": ("post", "/api/chat/feedback"),
    "chat_messages": ("get", "/api/chat/messages/{conv_id}"),
    "chat_recall_message": ("post", "/api/chat/messages/{msg_id}/recall"),
    "chat_send": ("post", "/api/chat/send"),
    "chat_send_async": ("post", "/api/chat/send/async"),
    "chat_send_stream": ("post", "/api/chat/send/stream"),
    "chat_submit": ("post", "/api/chat/submit"),
    "chat_task_status": ("get", "/api/chat/task/{task_id}/status"),
    "chat_task_stream": ("get", "/api/chat/task/{task_id}/stream"),
    "dashboard_ops_a2a_health": ("get", "/api/dashboard/ops/a2a-health"),
    "dashboard_ops_error_log_sources": ("get", "/api/dashboard/ops/error-logs/sources"),
    "dashboard_ops_error_logs": ("get", "/api/dashboard/ops/error-logs"),
    "dashboard_ops_engine_select_explain": ("get", "/api/dashboard/ops/engine-select-explain"),
    "dashboard_ops_overview": ("get", "/api/dashboard/ops/overview"),
    "dashboard_ops_probe_now": ("get", "/api/dashboard/ops/probe-now"),
    "dashboard_ops_timeline": ("get", "/api/dashboard/ops/timeline"),
    "dashboard_repo": ("get", "/api/dashboard/{owner}/{repo}"),
    "delete_account_api_auth_delete_account_post": ("post", "/api/auth/delete-account"),
    "delete_account_send_code_api_auth_delete_account_send_code_post": ("post", "/api/auth/delete-account/send-code"),
    "delete_account_via_oauth_api_auth_delete_account_oauth__provider__get": ("get", "/api/auth/delete-account/oauth/{provider}"),
    "devstage_config_get": ("get", "/api/devstage/config"),
    "devstage_config_update": ("put", "/api/devstage/config"),
    "devstage_environment_action": ("post", "/api/devstage/environments/{action}"),
    "devstage_environment_create": ("post", "/api/devstage/environments"),
    "devstage_environment_delete": ("delete", "/api/devstage/environments/{devstage_id}"),
    "devstage_environments": ("get", "/api/devstage/environments"),
    "devstage_specs": ("get", "/api/devstage/specs"),
    "devstage_templates": ("get", "/api/devstage/templates"),
    "events_a2a_redo": ("post", "/api/events/{seq}/a2a-redo"),
    "events_a2a_redo_scene": ("get", "/api/events/{seq}/a2a-redo/scene"),
    "events_chat_delete": ("delete", "/api/events/{seq}/chat"),
    "events_chat_send": ("post", "/api/events/{seq}/chat"),
    "events_chat_stream": ("post", "/api/events/{seq}/chat/stream"),
    "events_chat_submit": ("post", "/api/events/{seq}/chat/submit"),
    "events_chat_task_status": ("get", "/api/events/chat/task/{task_id}/status"),
    "events_chat_task_stream": ("get", "/api/events/chat/task/{task_id}/stream"),
    "events_detail": ("get", "/api/events/detail/{seq}"),
    "events_knowledge_delete": ("delete", "/api/events/{seq}/knowledge/{scope}"),
    "events_list": ("get", "/api/events/{owner}/{repo}"),
    "events_redispatch": ("post", "/api/events/{seq}/redispatch"),
    "events_terminate": ("post", "/api/events/{seq}/terminate"),
    "events_thread": ("get", "/api/events/{seq}/thread"),
    "feedback_context": ("get", "/api/feedback/context/{event_seq}"),
    "feedback_delete": ("delete", "/api/feedback/{fid}"),
    "feedback_mine": ("get", "/api/feedback/mine"),
    "feedback_submit": ("post", "/api/feedback"),
    "feedback_upload": ("post", "/api/feedback/upload"),
    "feishu_onboard_cancel": ("post", "/api/feishu/onboard/cancel"),
    "feishu_onboard_start": ("post", "/api/feishu/onboard/start"),
    "feishu_onboard_status": ("get", "/api/feishu/onboard/status"),
    "get_identities_api_auth_identities_get": ("get", "/api/auth/identities"),
    "gitrepo_branches": ("get", "/api/git-repo/{owner}/{repo}/branches"),
    "gitrepo_commits": ("get", "/api/git-repo/{owner}/{repo}/commits"),
    "gitrepo_contents": ("get", "/api/git-repo/{owner}/{repo}/contents/{path}"),
    "gitrepo_contents_root": ("get", "/api/git-repo/{owner}/{repo}/contents"),
    "gitrepo_issues_close": ("post", "/api/git-repo/{owner}/{repo}/issues/{number}/close"),
    "gitrepo_issues_comments_create": ("post", "/api/git-repo/{owner}/{repo}/issues/{number}/comments"),
    "gitrepo_issues_comments_list": ("get", "/api/git-repo/{owner}/{repo}/issues/{number}/comments"),
    "gitrepo_issues_create": ("post", "/api/git-repo/{owner}/{repo}/issues"),
    "gitrepo_issues_get": ("get", "/api/git-repo/{owner}/{repo}/issues/{number}"),
    "gitrepo_issues_list": ("get", "/api/git-repo/{owner}/{repo}/issues"),
    "gitrepo_issues_update": ("patch", "/api/git-repo/{owner}/{repo}/issues/{number}"),
    "gitrepo_labels_create": ("post", "/api/git-repo/{owner}/{repo}/labels"),
    "gitrepo_labels_list": ("get", "/api/git-repo/{owner}/{repo}/labels"),
    "gitrepo_milestones": ("get", "/api/git-repo/{owner}/{repo}/milestones"),
    "gitrepo_milestones_create": ("post", "/api/git-repo/{owner}/{repo}/milestones"),
    "gitrepo_milestones_update": ("patch", "/api/git-repo/{owner}/{repo}/milestones/{number}"),
    "gitrepo_pulls_comments_create": ("post", "/api/git-repo/{owner}/{repo}/pulls/{number}/comments"),
    "gitrepo_pulls_comments_delete": ("delete", "/api/git-repo/{owner}/{repo}/pulls/{number}/comments/{comment_id}"),
    "gitrepo_pulls_comments_list": ("get", "/api/git-repo/{owner}/{repo}/pulls/{number}/comments"),
    "gitrepo_pulls_comments_update": ("patch", "/api/git-repo/{owner}/{repo}/pulls/{number}/comments/{comment_id}"),
    "gitrepo_pulls_create": ("post", "/api/git-repo/{owner}/{repo}/pulls"),
    "gitrepo_pulls_files": ("get", "/api/git-repo/{owner}/{repo}/pulls/{number}/files"),
    "gitrepo_pulls_get": ("get", "/api/git-repo/{owner}/{repo}/pulls/{number}"),
    "gitrepo_pulls_list": ("get", "/api/git-repo/{owner}/{repo}/pulls"),
    "gitrepo_pulls_merge": ("put", "/api/git-repo/{owner}/{repo}/pulls/{number}/merge"),
    "gitrepo_pulls_update": ("patch", "/api/git-repo/{owner}/{repo}/pulls/{number}"),
    "gitrepo_repo": ("get", "/api/git-repo/{owner}/{repo}"),
    "gitrepo_tree": ("get", "/api/git-repo/{owner}/{repo}/tree"),
    "grape_auth_me": ("get", "/api/auth/me"),
    "grape_auth_transfer_context": ("get", "/api/auth/transfer-context"),
    "grape_auth_update_profile": ("put", "/api/auth/profile"),
    "issues_admin_confirm_done": ("post", "/api/issues/{issue_id}/admin-confirm-done"),
    "issues_awaiting-input": ("post", "/api/issues/{issue_id}/awaiting-input"),
    "issues_cancel": ("post", "/api/issues/{issue_id}/cancel"),
    "issues_clarifications": ("get", "/api/issues/{issue_id}/clarifications"),
    "issues_clarify": ("post", "/api/issues/{issue_id}/clarify"),
    "issues_confirm_done": ("post", "/api/issues/{issue_id}/confirm-done"),
    "issues_create": ("post", "/api/issues/{owner}/{repo}"),
    "issues_delete_attachment": ("delete", "/api/issues/{issue_id}/attachments/{filename}"),
    "issues_detail": ("get", "/api/issues/{owner}/{repo}/{number}"),
    "issues_events": ("get", "/api/issues/{owner}/{repo}/{number}/events"),
    "issues_handoff": ("post", "/api/issues/{issue_id}/handoff"),
    "issues_handoff_targets": ("get", "/api/issues/{issue_id}/handoff/targets"),
    "issues_list": ("get", "/api/issues/{owner}/{repo}"),
    "issues_list_attachments": ("get", "/api/issues/{issue_id}/attachments"),
    "issues_mine": ("get", "/api/issues/mine"),
    "issues_pending-review": ("post", "/api/issues/{issue_id}/pending-review"),
    "issues_pipeline": ("get", "/api/issues/{owner}/{repo}/{number}/pipeline"),
    "issues_pipeline_detail": ("get", "/api/issues/{issue_id}/pipeline"),
    "issues_resume": ("post", "/api/issues/{issue_id}/resume"),
    "issues_review-pass": ("post", "/api/issues/{issue_id}/review-pass"),
    "issues_review-reject": ("post", "/api/issues/{issue_id}/review-reject"),
    "issues_start": ("post", "/api/issues/{issue_id}/start"),
    "issues_status": ("put", "/api/issues/{issue_id}/status"),
    "issues_status_get": ("get", "/api/issues/{issue_id}/status"),
    "issues_upload_attachment": ("post", "/api/issues/{issue_id}/attachments"),
    "knowledge_add_refs_batch": ("post", "/api/knowledge/{knowledge_id}/refs/batch"),
    "knowledge_delete": ("delete", "/api/knowledge/{knowledge_id}"),
    "knowledge_detail": ("get", "/api/knowledge/{knowledge_id}"),
    "knowledge_entry_create": ("post", "/api/knowledge/entry"),
    "knowledge_list": ("get", "/api/knowledge"),
    "knowledge_ref_create": ("post", "/api/knowledge/{knowledge_id}/refs"),
    "knowledge_ref_delete": ("delete", "/api/knowledge/ref/{ref_id}"),
    "knowledge_ref_update": ("put", "/api/knowledge/ref/{ref_id}"),
    "knowledge_refs_list": ("get", "/api/knowledge/{knowledge_id}/refs"),
    "knowledge_stats": ("get", "/api/knowledge/stats"),
    "knowledge_update": ("put", "/api/knowledge/{knowledge_id}"),
    "obs_alert_rule_notify": ("post", "/api/obs/alerts/rules/{rule_id}/notify"),
    "obs_alert_rule_threshold": ("post", "/api/obs/alerts/rules/{rule_id}/threshold"),
    "obs_alert_rule_toggle": ("post", "/api/obs/alerts/rules/{rule_id}/toggle"),
    "obs_alert_silence": ("post", "/api/obs/alerts/{event_id}/silence"),
    "obs_alert_test_notify": ("post", "/api/obs/alerts/test-notify"),
    "obs_alerts": ("get", "/api/obs/alerts"),
    "obs_events": ("get", "/api/obs/events"),
    "obs_events_failed_result_gap": ("get", "/api/obs/events/failed-result-gap"),
    "obs_logs": ("get", "/api/obs/logs"),
    "obs_logs_sources": ("get", "/api/obs/logs/sources"),
    "obs_metrics_series": ("get", "/api/obs/metrics/series"),
    "obs_overview": ("get", "/api/obs/overview"),
    "obs_runtime_history": ("get", "/api/obs/runtimes/history"),
    "obs_slo_detail": ("get", "/api/obs/slo/{slo_id}"),
    "obs_slo_list": ("get", "/api/obs/slo"),
    "obs_tokens_engines": ("get", "/api/obs/tokens/engines"),
    "obs_tokens_issue_stages": ("get", "/api/obs/tokens/issues/{issue_id}/stages"),
    "obs_tokens_stages": ("get", "/api/obs/tokens/stages"),
    "obs_tokens_tasks": ("get", "/api/obs/tokens/tasks"),
    "obs_trace": ("get", "/api/obs/trace/{trace}"),
    "obs_trace_by_issue": ("get", "/api/obs/trace"),
    "obs_trace_stamp": ("post", "/api/obs/trace/stamp"),
    "orgs_create": ("post", "/api/orgs"),
    "orgs_delete": ("delete", "/api/orgs/{org_id}"),
    "orgs_detail": ("get", "/api/orgs/{org_id}"),
    "orgs_leave": ("post", "/api/orgs/{org_id}/leave"),
    "orgs_list": ("get", "/api/orgs"),
    "orgs_members_add": ("post", "/api/orgs/{org_id}/members"),
    "orgs_members_list": ("get", "/api/orgs/{org_id}/members"),
    "orgs_members_remove": ("delete", "/api/orgs/{org_id}/members/{user_id}"),
    "orgs_members_update": ("patch", "/api/orgs/{org_id}/members/{user_id}"),
    "orgs_search_users": ("get", "/api/orgs/search/users"),
    "orgs_update": ("put", "/api/orgs/{org_id}"),
    "pipeline_get": ("get", "/api/pipeline/{owner}/{repo}"),
    "project_template_create": ("post", "/api/project-templates/create"),
    "project_template_preview": ("get", "/api/project-templates/{template_id}/preview"),
    "project_template_readme": ("get", "/api/project-templates/{template_id}/readme"),
    "project_templates_list": ("get", "/api/project-templates"),
    "repos_bot_token_get": ("get", "/api/repos/bot-token/{owner}/{repo_name}"),
    "repos_bot_token_set": ("put", "/api/repos/bot-token"),
    "repos_check_name": ("get", "/api/repos/check-name"),
    "repos_create": ("post", "/api/repos/create"),
    "repos_deploy_url": ("get", "/api/repos/deploy-url/{owner}/{repo_name}"),
    "repos_deploy_url_set": ("put", "/api/repos/deploy-url"),
    "repos_git_credential": ("get", "/api/repos/{owner}/{repo}/git-credential"),
    "repos_host": ("post", "/api/repos/host"),
    "repos_managed": ("get", "/api/repos/managed"),
    "repos_member": ("get", "/api/repos/member"),
    "repos_migrate": ("post", "/api/repos/migrate"),
    "repos_migrate_orgs": ("get", "/api/repos/migrate-orgs"),
    "repos_project_info": ("get", "/api/repos/{owner}/{repo}/project-info"),
    "repos_remove": ("post", "/api/repos/remove"),
    "repos_repair_host": ("post", "/api/repos/{owner}/{repo}/repair-host"),
    "repos_scene_recommend_config_get": ("get", "/api/repos/{owner}/{repo}/scene-recommend-config"),
    "repos_scene_recommend_config_set": ("put", "/api/repos/{owner}/{repo}/scene-recommend-config"),
    "repos_sync": ("post", "/api/repos/sync"),
    "repos_workspace": ("get", "/api/repos/{owner}/{repo}/workspace"),
    "runtime_alias_set": ("put", "/api/runtime/runtimes/{runtime_id}/alias"),
    "runtime_batch_recover": ("post", "/api/runtime/admin/batch-recover"),
    "runtime_credential_tasks": ("get", "/api/runtime/credentials/{cid}/tasks"),
    "runtime_engine_onboarding": ("get", "/api/runtime/engine-onboarding"),
    "runtime_event_bus": ("get", "/api/runtime/admin/event-bus"),
    "runtime_machine_health": ("get", "/api/runtime/admin/machine-health"),
    "runtime_machine_health_detail": ("get", "/api/runtime/admin/machine-health/{runtime_id}"),
    "runtime_max_concurrent_set": ("put", "/api/runtime/runtimes/{runtime_id}/max-concurrent"),
    "runtime_refresh_skills": ("post", "/api/runtime/runtimes/{runtime_id}/refresh-skills"),
    "runtime_role_tags": ("get", "/api/runtime/runtimes/{runtime_id}/role-tags"),
    "runtime_scene_tags_get": ("get", "/api/runtime/runtimes/{runtime_id}/scene-tags"),
    "runtime_scene_tags_list": ("get", "/api/runtime/scene-tags"),
    "runtime_scene_tags_set": ("put", "/api/runtime/runtimes/{runtime_id}/scene-tags"),
    "runtime_sessions_list": ("get", "/api/runtime/runtimes/{runtime_id}/sessions"),
    "runtime_tags_set": ("put", "/api/runtime/runtimes/{runtime_id}/tags"),
    "runtime_task_cancel": ("post", "/api/runtime/tasks/{task_id}/cancel"),
    "runtime_task_converge_event": ("post", "/api/runtime/tasks/{task_id}/converge-event"),
    "runtime_task_status": ("get", "/api/runtime/tasks/{task_id}/status"),
    "runtime_tasks_list": ("get", "/api/runtime/runtimes/{runtime_id}/tasks"),
    "scenes_advanced_options_get": ("get", "/api/scenes/{scene_id}/advanced-options"),
    "scenes_advanced_options_set": ("put", "/api/scenes/{scene_id}/advanced-options"),
    "scenes_apply": ("post", "/api/scenes/apply/{owner}/{repo}"),
    "scenes_branch_create": ("post", "/api/scenes/branches/{owner}/{repo}"),
    "scenes_branches": ("get", "/api/scenes/branches/{owner}/{repo}"),
    "scenes_check_name": ("get", "/api/scenes/check-name"),
    "scenes_create": ("post", "/api/scenes"),
    "scenes_delete": ("delete", "/api/scenes/{scene_id}"),
    "scenes_deploy_url_get": ("get", "/api/scenes/{scene_id}/deploy-url"),
    "scenes_deploy_url_set": ("put", "/api/scenes/{scene_id}/deploy-url"),
    "scenes_detail": ("get", "/api/scenes/{scene_id}"),
    "scenes_engine_status": ("get", "/api/scenes/{scene_id}/engine-status"),
    "scenes_list": ("get", "/api/scenes"),
    "scenes_market_applied": ("get", "/api/scenes/market/{market_id}/applied"),
    "scenes_market_apply": ("post", "/api/scenes/market/{market_id}/apply"),
    "scenes_market_categories": ("get", "/api/scenes/market/categories"),
    "scenes_market_create": ("post", "/api/scenes/market"),
    "scenes_market_delete": ("delete", "/api/scenes/market/{market_id}"),
    "scenes_market_detail": ("get", "/api/scenes/market/{market_id}"),
    "scenes_market_experience": ("post", "/api/scenes/market/{market_id}/experience"),
    "scenes_market_experience_check": ("get", "/api/scenes/market/{market_id}/experience-check"),
    "scenes_market_icon_upload": ("post", "/api/scenes/market/icon-upload"),
    "scenes_market_like_toggle": ("post", "/api/scenes/market/{market_id}/like"),
    "scenes_market_list": ("get", "/api/scenes/market"),
    "scenes_market_update": ("put", "/api/scenes/market/{market_id}"),
    "scenes_skills_for_agent": ("get", "/api/scenes/skills/{agent_type}"),
    "scenes_update": ("put", "/api/scenes/{scene_id}"),
    "scenes_with_pipeline": ("get", "/api/scenes/with-pipeline"),
    "search_global": ("get", "/api/search"),
    "set_password_api_auth_set_password_post": ("post", "/api/auth/set-password"),
    "set_password_send_code_api_auth_set_password_send_code_post": ("post", "/api/auth/set-password/send-code"),
    "settings_agent_runtime_detail": ("get", "/api/settings/agent-runtimes/{rid}"),
    "settings_agent_runtime_health_reset": ("post", "/api/settings/agent-runtimes/{rid}/health-reset"),
    "settings_agent_runtimes": ("get", "/api/settings/agent-runtimes"),
    "settings_agent_runtimes_refresh": ("post", "/api/settings/agent-runtimes/refresh"),
    "settings_backend_agents": ("get", "/api/settings/backend-agents"),
    "settings_backend_agents_refresh": ("post", "/api/settings/backend-agents/refresh"),
    "settings_credentials_get": ("get", "/api/settings/credentials"),
    "settings_credentials_reveal": ("post", "/api/settings/credentials/reveal"),
    "settings_credentials_update": ("put", "/api/settings/credentials"),
    "settings_engine_tag_create": ("post", "/api/settings/agent-runtimes/{rid}/tags"),
    "settings_engine_tag_delete": ("delete", "/api/settings/agent-runtimes/{rid}/tags/{tag}"),
    "settings_engine_tag_update": ("put", "/api/settings/agent-runtimes/{rid}/tags/{tag}"),
    "settings_engine_tags": ("get", "/api/settings/agent-runtimes/{rid}/tags"),
    "settings_event_action_filters": ("get", "/api/settings/event-action-filters"),
    "settings_event_action_rule_create": ("post", "/api/settings/event-action-filters"),
    "settings_event_action_rule_delete": ("delete", "/api/settings/event-action-filters/{rule_id}"),
    "settings_event_action_rule_update": ("put", "/api/settings/event-action-filters/{rule_id}"),
    "settings_execution_mode_get": ("get", "/api/settings/execution-mode"),
    "settings_execution_mode_set": ("put", "/api/settings/execution-mode"),
    "settings_gitcode_repo_get": ("get", "/api/settings/gitcode-repo/{owner}/{repo}"),
    "settings_gitcode_repo_update": ("put", "/api/settings/gitcode-repo/{owner}/{repo}"),
    "settings_key_create": ("post", "/api/settings/api-keys"),
    "settings_key_delete": ("delete", "/api/settings/api-keys/{key_id}"),
    "settings_keys": ("get", "/api/settings/api-keys"),
    "settings_notifications_get": ("get", "/api/settings/notifications"),
    "settings_notifications_update": ("put", "/api/settings/notifications"),
    "settings_oauth_get": ("get", "/api/settings/oauth"),
    "settings_oauth_github_get": ("get", "/api/settings/oauth-github"),
    "settings_oauth_github_update": ("put", "/api/settings/oauth-github"),
    "settings_oauth_update": ("put", "/api/settings/oauth"),
    "settings_pipeline_mode_get": ("get", "/api/settings/pipeline-mode"),
    "settings_pipeline_mode_set": ("put", "/api/settings/pipeline-mode"),
    "settings_runtime_credential_create": ("post", "/api/settings/runtime-credentials"),
    "settings_runtime_credential_rename": ("put", "/api/settings/runtime-credentials/{cid}"),
    "settings_runtime_credential_revoke": ("post", "/api/settings/runtime-credentials/{cid}/revoke"),
    "settings_runtime_credential_scope": ("put", "/api/settings/runtime-credentials/{cid}/scope"),
    "settings_runtime_credentials": ("get", "/api/settings/runtime-credentials"),
    "settings_runtime_devices": ("get", "/api/settings/runtime-devices"),
    "skills_agent_stats": ("get", "/api/skills/agent-stats"),
    "skills_categories": ("get", "/api/skills/categories"),
    "skills_convert": ("post", "/api/skills/convert"),
    "skills_delete": ("delete", "/api/skills/by-id/{skill_id}"),
    "skills_detail": ("get", "/api/skills/by-id/{skill_id}"),
    "skills_download": ("get", "/api/skills/by-id/{skill_id}/download"),
    "skills_featured": ("get", "/api/skills/featured"),
    "skills_like_toggle": ("post", "/api/skills/{skill_id}/like"),
    "skills_list": ("get", "/api/skills"),
    "skills_package_sub_get": ("get", "/api/skills/package-sub/{pkg_id}/{sub_name}"),
    "skills_search": ("get", "/api/skills/search"),
    "skills_stats": ("get", "/api/skills/stats"),
    "skills_sub_get": ("get", "/api/skills/{skill_id}/sub/{sub_name}"),
    "skills_sub_list": ("get", "/api/skills/{skill_id}/sub"),
    "skills_update": ("post", "/api/skills/by-id/{skill_id}/update"),
    "skills_upload": ("post", "/api/skills/upload"),
    "task_logs_info": ("get", "/api/task-logs/{task_id}/info"),
    "task_logs_messages": ("get", "/api/task-logs/{task_id}/messages"),
    "task_logs_stream": ("get", "/api/task-logs/{task_id}/stream"),
    "tasks_complete": ("post", "/api/tasks/complete"),
    "tasks_current": ("get", "/api/tasks/current"),
    "tasks_notify": ("post", "/api/tasks/notify"),
    "transfer_bind_api_auth_transfer_bind_post": ("post", "/api/auth/transfer-bind"),
    "unbind_provider_api_auth_bind__provider__delete": ("delete", "/api/auth/bind/{provider}"),
}

# 内部（会话认证）或管理类端点，不应出现在契约
NOT_IN_CONTRACT = [
    "/api/agents/types",                 # 无认证
    "/api/auth/login",
    "/api/runtime/start",                # 管理类模块
    "/api/feedback/list",                # 管理类（require_admin）
    "/api/feedback/stats",               # 管理类（require_admin）
    "/api/feedback/{fid}/status",        # 管理类（require_admin）
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
    assert s["info"]["version"] == "2.2.0"
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
            assert scope in {"user_only", "session_only", "dual"},                 f"缺 x-token-scope: {method} {path} -> {scope!r}"


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
                assert (method, path) in actual_session_only,                     f"session 规则命中但未标 session_only: {op_id} {method} {path}"

    # 2) 所有标为 session_only 的必须命中规则（防漏登记/误标）
    for method, path in actual_session_only:
        op_id = s["paths"][path][method]["operationId"]
        assert (op_id in openapi._SESSION_ONLY_OPERATIONS
                or path.startswith(openapi._SESSION_ONLY_PATH_PREFIXES)),             f"标了 session_only 但未命中规则: {op_id} {method} {path}"

    # 3) 抽样：user_only（D 类管理 / 用户操作类）/ dual（开放读）实例
    # /api/skills get：组织隔离 P1-P5 改用 require_org_member（get_current_user_or_api_key）
    # → dual（tk_ 可读技能列表），从 user_only 样本迁至 dual 样本
    # 组织隔离（0c8b6b5df/d7caf6201）把 ops/overview 与 issues/mine 改为
    # require_org_member（get_current_user_or_api_key 链）→ dual（tk_ 可访问），迁至 dual 样本
    user_only_samples = {"/api/auth/logout": "post",
                         # 用户操作类：状态写/会话预检禁止 tk_（避免 Agent 干扰 pipeline）
                         "/api/issues/{issue_id}/status": "put",
                         "/api/issues/{issue_id}/start": "post",
                         # skills agent-stats：仅 Web UI/用户级消费，禁 session
                         "/api/skills/agent-stats": "get", "/api/skills/stats": "get",
                         # 意见反馈 user_only（Agent tk_ submit 为 dual）
                         "/api/feedback/mine": "get", "/api/feedback/{fid}": "delete"}
    for path, method in user_only_samples.items():
        assert s["paths"][path][method]["x-token-scope"] == "user_only",             f"期望 user_only: {method} {path}"

    dual_samples = {"/api/issues/{owner}/{repo}": "get", "/api/a2a": "post",
                    "/api/skills": "get",
                    "/api/dashboard/ops/overview": "get", "/api/issues/mine": "get",
                    # issues create 亦为 require_org_member（组织隔离统一入口鉴权）→ dual
                    "/api/issues/{owner}/{repo}": "post",
                    # skills upload（repo 技能上传会话可用）、详情与子技能（agent 运行时按需加载）为 dual
                    "/api/skills/upload": "post",
                    "/api/skills/by-id/{skill_id}": "get",
                    "/api/skills/{skill_id}/sub": "get",
                    # Issue 状态只读轮询（CLI wait 用）保持 dual
                    "/api/issues/{issue_id}/status": "get",
                    # 意见反馈提交：Agent（tk_）自动反馈场景 → dual
                    "/api/feedback": "post"}
    for path, method in dual_samples.items():
        assert s["paths"][path][method]["x-token-scope"] == "dual",             f"期望 dual: {method} {path}"

    # 4) session_only 样例（需求 §四验收）
    session_samples = {"/api/git-repo/{owner}/{repo}/branches": "get",
                       "/api/tasks/current": "get",
                       "/api/tasks/notify": "post",
                       "/api/issues/{issue_id}/awaiting-input": "post",
                       "/api/repos/{owner}/{repo}/git-credential": "get",
                       "/api/repos/{owner}/{repo}/workspace": "get"}
    for path, method in session_samples.items():
        assert (method, path) in actual_session_only,             f"期望 session_only: {method} {path}"


def _user():
    """创建临时测试账号，返回 (id, username)。
    username 必须原样用于签 JWT——deps.py 的 #343 一致性校验要求
    payload.username 与库内 sub 对应用户名一致，否则 SESSION_INVALID。"""
    import uuid
    u = grape_account_db.create_grape_account(f"openapi_{uuid.uuid4().hex[:6]}", email=None, password="pw")
    return u["id"], u["username"]


def test_three_channels_can_call_public_api():
    from src.session_manager import db, db_lock
    with db_lock:  # 清空 token 表，避免残留
        db.cursor().execute("DELETE FROM session_api_tokens WHERE id LIKE 'test_%' OR id LIKE 'sess_%' OR session_key LIKE 'test_%'")
        db.cursor().execute("DELETE FROM api_tokens WHERE user_id IN (SELECT id FROM grape_accounts WHERE username LIKE 'test_%' OR username LIKE 'tc_%')")
        db.commit()
    uid, _uname = _user()

    jwt = create_access_token(uid, _uname)
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
    # session token（chat 会话携带 user_id）→ mine 已 dual 化（组织隔离 require_org_member），
    # tk_ 携带 user_id 可读个人维度，应 200 而非 403
    assert client.get("/api/issues/mine",
                      headers={"Authorization": f"Bearer {session_tk}"}).status_code == 200
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