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
    # POST /api/tasks/create 已废弃移除（C2）→ 创建走 POST /api/issues/{owner}/{repo}
    "a2a_handoff": ("post", "/api/a2a"),
    # 合并引入的新公开操作（multi-vcs / knowledge detail / profile）
    "get_profile_api_auth_profile_get": ("get", "/api/auth/profile"),
    "logout_api_auth_logout_post": ("post", "/api/auth/logout"),
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
    # ===== A 类列表归位（tasks-issues-api-rebound-design.md）：mine / repo 迁 /api/issues =====
    "issues_mine": ("get", "/api/issues/mine"),
    # ===== Issue #113: 任务中心仓库全部视图 =====
    # ===== A 批（openapi-full-coverage-design.md）：skills/scenes 全量进契约 =====
    "upload_skill_api_skills_upload_post": ("post", "/api/skills/upload"),
    "toggle_skill_like_api_skills__skill_id__like_post": ("post", "/api/skills/{skill_id}/like"),
    "get_skill_stats_api_skills_stats_get": ("get", "/api/skills/stats"),
    "search_skills_api_skills_search_get": ("get", "/api/skills/search"),
    "list_featured_skills_api_skills_featured_get": ("get", "/api/skills/featured"),
    "download_skill_zip_by_id_api_skills_by_id__skill_id__download_get": ("get", "/api/skills/by-id/{skill_id}/download"),
    "delete_skill_by_id_api_skills_by_id__skill_id__delete": ("delete", "/api/skills/by-id/{skill_id}"),
    "convert_skill_api_skills_convert_post": ("post", "/api/skills/convert"),
    "list_sub_skills_api_skills__skill_id__sub_get": ("get", "/api/skills/{skill_id}/sub"),
    "read_sub_skill_api_skills__skill_id__sub__sub_name__get": ("get", "/api/skills/{skill_id}/sub/{sub_name}"),
    "package_sub_skill_api_skills_package_sub__pkg_id___sub_name__get": ("get", "/api/skills/package-sub/{pkg_id}/{sub_name}"),
    "create_scene_api_scenes_post": ("post", "/api/scenes"),
    "list_scenes_with_pipeline_api_scenes_with_pipeline_get": ("get", "/api/scenes/with-pipeline"),
    "create_market_scene_api_scenes_market_post": ("post", "/api/scenes/market"),
    "list_scenes_market_api_scenes_market_get": ("get", "/api/scenes/market"),
    "list_scene_categories_api_scenes_market_categories_get": ("get", "/api/scenes/market/categories"),
    "get_scene_market_detail_api_scenes_market__market_id__get": ("get", "/api/scenes/market/{market_id}"),
    "update_market_scene_api_scenes_market__market_id__put": ("put", "/api/scenes/market/{market_id}"),
    "delete_market_scene_api_scenes_market__market_id__delete": ("delete", "/api/scenes/market/{market_id}"),
    "check_market_scene_applied_api_scenes_market__market_id__applied_get": ("get", "/api/scenes/market/{market_id}/applied"),
    "apply_market_scene_api_scenes_market__market_id__apply_post": ("post", "/api/scenes/market/{market_id}/apply"),
    "check_market_scene_experience_api_scenes_market__market_id__experience_check_get": ("get", "/api/scenes/market/{market_id}/experience-check"),
    "experience_market_scene_api_scenes_market__market_id__experience_post": ("post", "/api/scenes/market/{market_id}/experience"),
    "toggle_scene_like_api_scenes_market__market_id__like_post": ("post", "/api/scenes/market/{market_id}/like"),
    "upload_scene_icon_api_scenes_market_icon_upload_post": ("post", "/api/scenes/market/icon-upload"),
    "check_scene_name_api_scenes_check_name_get": ("get", "/api/scenes/check-name"),
    "list_skills_for_agent_api_scenes_skills__agent_type__get": ("get", "/api/scenes/skills/{agent_type}"),
    "apply_scene_api_scenes_apply__owner___repo__post": ("post", "/api/scenes/apply/{owner}/{repo}"),
    "list_repo_branches_api_scenes_branches__owner___repo__get": ("get", "/api/scenes/branches/{owner}/{repo}"),
    "create_repo_branch_api_scenes_branches__owner___repo__post": ("post", "/api/scenes/branches/{owner}/{repo}"),
    "get_scene_detail_api_scenes__scene_id__get": ("get", "/api/scenes/{scene_id}"),
    "update_scene_api_scenes__scene_id__put": ("put", "/api/scenes/{scene_id}"),
    "delete_scene_api_scenes__scene_id__delete": ("delete", "/api/scenes/{scene_id}"),
    "get_advanced_options_api_scenes__scene_id__advanced_options_get": ("get", "/api/scenes/{scene_id}/advanced-options"),
    "save_advanced_options_api_scenes__scene_id__advanced_options_put": ("put", "/api/scenes/{scene_id}/advanced-options"),
    # ===== B 批（openapi-full-coverage-design.md）：settings/repos/agents/knowledge 全量进契约 =====
    "get_credentials_api_settings_credentials_get": ("get", "/api/settings/credentials"),
    "update_credentials_api_settings_credentials_put": ("put", "/api/settings/credentials"),
    "create_api_key_api_settings_api_keys_post": ("post", "/api/settings/api-keys"),
    "list_api_keys_api_settings_api_keys_get": ("get", "/api/settings/api-keys"),
    "revoke_api_key_api_settings_api_keys__key_id__delete": ("delete", "/api/settings/api-keys/{key_id}"),
    "get_notifications_api_settings_notifications_get": ("get", "/api/settings/notifications"),
    "update_notifications_api_settings_notifications_put": ("put", "/api/settings/notifications"),
    "get_oauth_config_api_settings_oauth_get": ("get", "/api/settings/oauth"),
    "update_oauth_config_api_settings_oauth_put": ("put", "/api/settings/oauth"),
    "get_github_oauth_config_api_settings_oauth_github_get": ("get", "/api/settings/oauth-github"),
    "update_github_oauth_config_api_settings_oauth_github_put": ("put", "/api/settings/oauth-github"),
    "get_gitcode_repo_config_api_settings_gitcode_repo__owner___repo__get": ("get", "/api/settings/gitcode-repo/{owner}/{repo}"),
    "update_gitcode_repo_config_api_settings_gitcode_repo__owner___repo__put": ("put", "/api/settings/gitcode-repo/{owner}/{repo}"),
    "get_agent_runtimes_api_settings_agent_runtimes_get": ("get", "/api/settings/agent-runtimes"),
    "get_backend_agents_api_settings_backend_agents_get": ("get", "/api/settings/backend-agents"),
    "refresh_agent_runtimes_api_settings_agent_runtimes_refresh_post": ("post", "/api/settings/agent-runtimes/refresh"),
    "refresh_backend_agents_api_settings_backend_agents_refresh_post": ("post", "/api/settings/backend-agents/refresh"),
    "create_runtime_credential_api_settings_runtime_credentials_post": ("post", "/api/settings/runtime-credentials"),
    "rename_runtime_credential_api_settings_runtime_credentials__cid__put": ("put", "/api/settings/runtime-credentials/{cid}"),
    "change_credential_scope_api_settings_runtime_credentials__cid__scope_put": ("put", "/api/settings/runtime-credentials/{cid}/scope"),
    "list_runtime_credentials_api_settings_runtime_credentials_get": ("get", "/api/settings/runtime-credentials"),
    "revoke_runtime_credential_api_settings_runtime_credentials__cid__revoke_post": ("post", "/api/settings/runtime-credentials/{cid}/revoke"),
    "get_execution_mode_api_api_settings_execution_mode_get": ("get", "/api/settings/execution-mode"),
    "set_execution_mode_api_api_settings_execution_mode_put": ("put", "/api/settings/execution-mode"),
    "get_pipeline_mode_api_api_settings_pipeline_mode_get": ("get", "/api/settings/pipeline-mode"),
    "set_pipeline_mode_api_api_settings_pipeline_mode_put": ("put", "/api/settings/pipeline-mode"),
    "get_engine_tags_api_settings_agent_runtimes__rid__tags_get": ("get", "/api/settings/agent-runtimes/{rid}/tags"),
    "add_engine_tag_api_settings_agent_runtimes__rid__tags_post": ("post", "/api/settings/agent-runtimes/{rid}/tags"),
    "delete_engine_tag_api_settings_agent_runtimes__rid__tags__tag__delete": ("delete", "/api/settings/agent-runtimes/{rid}/tags/{tag}"),
    "update_engine_tag_api_settings_agent_runtimes__rid__tags__tag__put": ("put", "/api/settings/agent-runtimes/{rid}/tags/{tag}"),
    "list_event_action_filters_api_settings_event_action_filters_get": ("get", "/api/settings/event-action-filters"),
    "add_event_action_rule_api_settings_event_action_filters_post": ("post", "/api/settings/event-action-filters"),
    "delete_event_action_rule_api_settings_event_action_filters__rule_id__delete": ("delete", "/api/settings/event-action-filters/{rule_id}"),
    "update_event_action_rule_api_settings_event_action_filters__rule_id__put": ("put", "/api/settings/event-action-filters/{rule_id}"),
    "check_repo_name_api_repos_check_name_get": ("get", "/api/repos/check-name"),
    "sync_repos_api_repos_sync_post": ("post", "/api/repos/sync"),
    "create_new_repo_api_repos_create_post": ("post", "/api/repos/create"),
    "host_repo_api_repos_host_post": ("post", "/api/repos/host"),
    "get_repo_bot_token_api_repos_bot_token__owner___repo_name__get": ("get", "/api/repos/bot-token/{owner}/{repo_name}"),
    "set_repo_bot_token_api_repos_bot_token_put": ("put", "/api/repos/bot-token"),
    "set_deploy_url_api_repos_deploy_url_put": ("put", "/api/repos/deploy-url"),
    "remove_managed_repo_api_repos_remove_post": ("post", "/api/repos/remove"),
    "repair_host_api_repos__owner___repo__repair_host_post": ("post", "/api/repos/{owner}/{repo}/repair-host"),
    "get_repo_project_info_api_repos__owner___repo__project_info_get": ("get", "/api/repos/{owner}/{repo}/project-info"),
    "get_scene_recommend_config_api_repos__owner___repo__scene_recommend_config_get": ("get", "/api/repos/{owner}/{repo}/scene-recommend-config"),
    "save_scene_recommend_config_api_repos__owner___repo__scene_recommend_config_put": ("put", "/api/repos/{owner}/{repo}/scene-recommend-config"),
    "update_agent_config_api_agents__name___owner___repo__put": ("put", "/api/agents/{name}/{owner}/{repo}"),
    "clean_agent_session_api_agents__name___owner___repo__clean_post": ("post", "/api/agents/{name}/{owner}/{repo}/clean"),
    "create_knowledge_entry_api_knowledge_entry_post": ("post", "/api/knowledge/entry"),
    "delete_knowledge_api_knowledge_delete": ("delete", "/api/knowledge"),
    "delete_knowledge_by_id_api_knowledge__knowledge_id__delete": ("delete", "/api/knowledge/{knowledge_id}"),
    "update_knowledge_api_knowledge__knowledge_id__put": ("put", "/api/knowledge/{knowledge_id}"),
    "archive_knowledge_api_knowledge__knowledge_id__archive_put": ("put", "/api/knowledge/{knowledge_id}/archive"),
    "get_knowledge_refs_api_knowledge__knowledge_id__refs_get": ("get", "/api/knowledge/{knowledge_id}/refs"),
    "add_knowledge_ref_api_knowledge__knowledge_id__refs_post": ("post", "/api/knowledge/{knowledge_id}/refs"),
    "knowledge_add_refs_batch": ("post", "/api/knowledge/{knowledge_id}/refs/batch"),
    "update_knowledge_ref_api_knowledge_ref__ref_id__put": ("put", "/api/knowledge/ref/{ref_id}"),
    "delete_knowledge_ref_api_knowledge_ref__ref_id__delete": ("delete", "/api/knowledge/ref/{ref_id}"),
    # ===== C 批（openapi-full-coverage-design.md）：tasks/events 用户操作类进契约 =====
    "terminate_event_api_events__seq__terminate_post": ("post", "/api/events/{seq}/terminate"),
    "redispatch_event_api_events__seq__redispatch_post": ("post", "/api/events/{seq}/redispatch"),
    "get_event_thread_api_events__seq__thread_get": ("get", "/api/events/{seq}/thread"),
    "delete_event_thread_api_events__seq__chat_delete": ("delete", "/api/events/{seq}/chat"),
    "delete_event_knowledge_api_events__seq__knowledge__scope__delete": ("delete", "/api/events/{seq}/knowledge/{scope}"),
    "send_conversation_api_events__seq__chat_post": ("post", "/api/events/{seq}/chat"),
    "stream_conversation_api_events__seq__chat_stream_post": ("post", "/api/events/{seq}/chat/stream"),
    "submit_conversation_api_events__seq__chat_submit_post": ("post", "/api/events/{seq}/chat/submit"),
    "conv_task_status_api_events_chat_task__task_id__status_get": ("get", "/api/events/chat/task/{task_id}/status"),
    "conv_task_stream_api_events_chat_task__task_id__stream_get": ("get", "/api/events/chat/task/{task_id}/stream"),
    "list_events_api_events__owner___repo__get": ("get", "/api/events/{owner}/{repo}"),
    # ===== C 批续（openapi-full-coverage）：runtime/chat 用户操作类进契约 =====
    "event_bus_debug_api_runtime_admin_event_bus_get": ("get", "/api/runtime/admin/event-bus"),
    "machine_health_overview_api_runtime_admin_machine_health_get": ("get", "/api/runtime/admin/machine-health"),
    "machine_health_detail_api_runtime_admin_machine_health__runtime_id__get": ("get", "/api/runtime/admin/machine-health/{runtime_id}"),
    "batch_recover_stale_tasks_api_api_runtime_admin_batch_recover_post": ("post", "/api/runtime/admin/batch-recover"),
    "get_task_status_api_runtime_tasks__task_id__status_get": ("get", "/api/runtime/tasks/{task_id}/status"),
    "cancel_task_api_runtime_tasks__task_id__cancel_post": ("post", "/api/runtime/tasks/{task_id}/cancel"),
    "list_runtime_tasks_api_runtime_runtimes__runtime_id__tasks_get": ("get", "/api/runtime/runtimes/{runtime_id}/tasks"),
    "list_runtime_sessions_api_runtime_runtimes__runtime_id__sessions_get": ("get", "/api/runtime/runtimes/{runtime_id}/sessions"),
    "list_credential_tasks_api_runtime_credentials__cid__tasks_get": ("get", "/api/runtime/credentials/{cid}/tasks"),
    "runtime_refresh_skills_api_runtime_runtimes__runtime_id__refresh_skills_post": ("post", "/api/runtime/runtimes/{runtime_id}/refresh-skills"),
    "get_runtime_tags_api_runtime_runtimes__runtime_id__role_tags_get": ("get", "/api/runtime/runtimes/{runtime_id}/role-tags"),
    "set_runtime_tags_api_runtime_runtimes__runtime_id__tags_put": ("put", "/api/runtime/runtimes/{runtime_id}/tags"),
    "set_runtime_alias_api_runtime_runtimes__runtime_id__alias_put": ("put", "/api/runtime/runtimes/{runtime_id}/alias"),
    "set_runtime_max_concurrent_api_runtime_runtimes__runtime_id__max_concurrent_put": ("put", "/api/runtime/runtimes/{runtime_id}/max-concurrent"),
    "list_scene_tags_api_runtime_scene_tags_get": ("get", "/api/runtime/scene-tags"),
    "get_runtime_scene_tags_api_runtime_runtimes__runtime_id__scene_tags_get": ("get", "/api/runtime/runtimes/{runtime_id}/scene-tags"),
    "set_runtime_scene_tags_api_runtime_runtimes__runtime_id__scene_tags_put": ("put", "/api/runtime/runtimes/{runtime_id}/scene-tags"),
    "new_conversation_api_chat_conversations_post": ("post", "/api/chat/conversations"),
    "remove_conversation_api_chat_conversations__conv_id__delete": ("delete", "/api/chat/conversations/{conv_id}"),
    "ensure_issue_conversation_api_chat_conversations_ensure_issue_post": ("post", "/api/chat/conversations/ensure-issue"),
    "send_message_api_chat_send_post": ("post", "/api/chat/send"),
    "send_message_stream_api_chat_send_stream_post": ("post", "/api/chat/send/stream"),
    "send_message_async_api_chat_send_async_post": ("post", "/api/chat/send/async"),
    "submit_feedback_api_chat_feedback_post": ("post", "/api/chat/feedback"),
    "get_chat_config_api_chat_config_get": ("get", "/api/chat/config"),
    "update_chat_config_api_chat_config_put": ("put", "/api/chat/config"),
    "chat_submit_api_chat_submit_post": ("post", "/api/chat/submit"),
    "chat_task_status_api_chat_task__task_id__status_get": ("get", "/api/chat/task/{task_id}/status"),
    "chat_task_stream_api_chat_task__task_id__stream_get": ("get", "/api/chat/task/{task_id}/stream"),
    # ===== D 批（openapi-full-coverage-design.md）：devstage/templates/dashboard/pipeline/feishu/task-logs 进契约 =====
    "get_devstage_config_api_devstage_config_get": ("get", "/api/devstage/config"),
    "update_devstage_config_api_devstage_config_put": ("put", "/api/devstage/config"),
    "list_templates_api_devstage_templates_get": ("get", "/api/devstage/templates"),
    "list_specs_api_devstage_specs_get": ("get", "/api/devstage/specs"),
    "list_environments_api_devstage_environments_get": ("get", "/api/devstage/environments"),
    "create_environment_api_devstage_environments_post": ("post", "/api/devstage/environments"),
    "delete_environment_api_devstage_environments__devstage_id__delete": ("delete", "/api/devstage/environments/{devstage_id}"),
    "environment_action_api_devstage_environments__action__post": ("post", "/api/devstage/environments/{action}"),
    "create_from_template_api_project_templates_create_post": ("post", "/api/project-templates/create"),
    "list_templates_api_project_templates_get": ("get", "/api/project-templates"),
    "get_template_preview_api_project_templates__template_id__preview_get": ("get", "/api/project-templates/{template_id}/preview"),
    "get_template_readme_api_project_templates__template_id__readme_get": ("get", "/api/project-templates/{template_id}/readme"),
    "get_ops_overview_api_dashboard_ops_overview_get": ("get", "/api/dashboard/ops/overview"),
    "get_dashboard_api_dashboard__owner___repo__get": ("get", "/api/dashboard/{owner}/{repo}"),
    "get_pipeline_api_pipeline__owner___repo__get": ("get", "/api/pipeline/{owner}/{repo}"),
    "global_search_api_search_get": ("get", "/api/search"),
    "start_onboard_api_feishu_onboard_start_post": ("post", "/api/feishu/onboard/start"),
    "onboard_status_api_feishu_onboard_status_get": ("get", "/api/feishu/onboard/status"),
    "cancel_onboard_api_feishu_onboard_cancel_post": ("post", "/api/feishu/onboard/cancel"),
    "get_task_messages_api_task_logs__task_id__messages_get": ("get", "/api/task-logs/{task_id}/messages"),
    "get_task_runtime_info_api_task_logs__task_id__info_get": ("get", "/api/task-logs/{task_id}/info"),
    "stream_task_messages_api_task_logs__task_id__stream_get": ("get", "/api/task-logs/{task_id}/stream"),
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
    "gitrepo_git_credential": ("get", "/api/git-repo/{owner}/{repo}/git-credential"),
    "gitrepo_workspace": ("get", "/api/git-repo/{owner}/{repo}/workspace"),
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

    # 3) 抽样：user_only（D 类管理）/ dual（开放读）实例
    user_only_samples = {"/api/skills": "get", "/api/skills/upload": "post",
                         "/api/auth/logout": "post", "/api/dashboard/ops/overview": "get",
                         "/api/issues/mine": "get", "/api/issues/{owner}/{repo}": "post"}
    for path, method in user_only_samples.items():
        assert s["paths"][path][method]["x-token-scope"] == "user_only", \
            f"期望 user_only: {method} {path}"

    dual_samples = {"/api/issues/{owner}/{repo}": "get", "/api/a2a": "post"}
    for path, method in dual_samples.items():
        assert s["paths"][path][method]["x-token-scope"] == "dual", \
            f"期望 dual: {method} {path}"

    # 4) session_only 样例（需求 §四验收）
    session_samples = {"/api/git-repo/{owner}/{repo}/branches": "get",
                       "/api/tasks/current": "get",
                       "/api/git-repo/{owner}/{repo}/git-credential": "get",
                       "/api/git-repo/{owner}/{repo}/workspace": "get",
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
        db.cursor().execute("DELETE FROM session_api_tokens")
        db.cursor().execute("DELETE FROM api_tokens")
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