"""Grape Service Bus — Provider 路由配置表（Issue #2479：remote/lib 按接口并存）.

设计依据：docs/issue-2479/plan.md §2.1；docs/lib-3db-3host-design.md §11（按方法路由）。

把 provider 选择从 ``GRAPE_PROVIDER_MODE`` 全局一刀切改为按两级 key 路由：

- 契约方法 key：``service.method``（如 ``mgmt.get_bot_config``，23 个）；
- public 原语 key：``domain.primitive``（如 ``mgmt.get_repo_field``，
  mgmt 39 个 + runtime 66 个数据原语；``available``/``health`` 探活口不参与路由）。

配置落 DB 表 ``gsb_provider_route``（与 ``gsb_service_registry`` 同库），
进程内按 version+TTL 缓存：改表后 ≤TTL（默认 30s，``GRAPE_GSB_ROUTE_TTL``）
不重启生效；**读取失败退回上一次已知配置**（启动期兜底，非调用期回落）。

kernel 层禁止 import domains（check-arch R1 HARD），因此路由 key 清单以字面量
镜像 contracts/public 实际面；``tests/kernel/test_gsb_route.py`` 的同步用例把两者
锁定——契约/原语增删不同步登记会在 CI 变红。
"""
from __future__ import annotations

import os
import threading
import time

from src.kernel import session_manager

# gsb_provider_route 归 mgmt 域：任一进程读路由配置都必须打到 owner 库，
# 否则三库模式下非 mgmt 进程会得到 1146（表不在本域库里）。
from src.kernel.owner_db import owner_db as _owner_db
_owner = _owner_db("mgmt")
_owner_lock = session_manager.db_lock
from src.kernel.config import log

# ---------------- 路由 key registry（静态镜像，同步测试防漂移） ----------------

#: 三域契约方法（来源：src/domains/contracts/{mgmt,runtime,insight}_service.py）
CONTRACT_METHODS: dict[str, tuple[str, ...]] = {
    "mgmt": (
        "get_repo_webhook_config", "get_agent_session", "get_scene_config",
        "get_scene_pool_engine_ids", "verify_session_token", "get_bot_config",
        "get_skill",
        # P2 服务接口补齐（N6/N8/N9/N10）
        "get_repo_bundle", "get_account", "get_knowledge", "get_user_mapping_batch",
        # Issue #3632：平台卡跨域只读（worker/engine 取卡契约的数据源）
        "get_repo_platform_cards",
    ),
    "runtime": (
        "get_events", "get_event_detail", "get_tasks", "get_task_messages",
        "get_runtimes", "get_issue_state", "update_issue_state",
        "get_chat_conversations", "get_chat_messages",
        # P2 服务接口补齐（N1–N5/N11/N12）
        "list_skills", "get_skill_detail", "list_engine_capabilities",
        "get_runtime_credential_meta", "get_meta_namespace", "get_tasks_batch",
        "get_task_stats",
        # 本轮新增（mgmt → runtime 跨域读迁移）
        "get_issue_card", "get_credential_health_rows", "get_inflight_task_count",
        "get_agent_session_row", "get_event_status_counts", "search_events",
        "list_credential_rows", "list_engine_tags",
        # Issue #3054（用户业务数据移交）：引擎引用对账 / 业务表计数 / 数据移交
        "reconcile_engine_references", "transfer_user_business_data",
        "user_business_table_counts",
    ),
    "insight": (
        "log_obs", "log_llm_usage", "log_api_latency", "query_metrics",
        "query_slo", "query_alerts", "aggregate_runtime_stats",
        # 本轮新增（mgmt → insight 跨域读迁移）
        "aggregate_llm_usage",
    ),
}

#: 跨域 public 数据原语（来源：src/domains/{mgmt,runtime}/public/__init__.py；
#: available/health 为探活口，不参与路由）。insight 无 public 模块。
PUBLIC_PRIMITIVES: dict[str, tuple[str, ...]] = {
    "mgmt": (
        "fetch_rows", "fetch_one", "get_repo_field", "set_repo_fields",
        "ensure_repo_row", "get_repo_org_id", "list_user_ids_by_open_id",
        "get_feishu_open_id", "list_all_user_ids", "list_baseline_knowledge_files",
        "list_knowledge_obs_keys", "get_autopilot_task_brief", "mark_default_featured",
        "set_scene_deploy_url", "set_scene_nodes", "upsert_user_mapping_feishu",
        "clear_user_mapping_feishu", "set_user_mapping_nickname",
        "delete_feishu_onboard_task", "mark_external_identity_expired",
        "set_autopilot_run_info", "set_autopilot_engine_ids",
        "upsert_baseline_knowledge", "ensure_scene_knowledge_ref",
        "delete_event_knowledge", "replace_scene_skill_ref", "upsert_scene_content",
        "set_scene_icon", "get_official_scene_id", "count_rows",
        "count_official_scenes", "list_enabled_autopilot_tasks",
        "get_account_id_by_username", "get_account_username_by_id",
        "find_repo_key_in", "list_alert_user_ids", "get_user_id_by_mapping_column",
        # P2 服务接口补齐（N6/N8/N9/N10）
        "repo_bundle", "get_account_public", "get_knowledge_entry", "user_mapping_batch",
        # W2 渠道写原语（runtime IM → mgmt 归属表）
        "acquire_channel_lease", "renew_channel_lease", "release_channel_lease",
        # 渠道租约只读（runtime/im/outbox 归属过滤；任意拓扑）
        "lease_owner_by_key", "lease_owned_installations_by_owner",
        "release_all_leases", "claim_inbound_dedup", "upsert_channel_installation",
        "upsert_chat_session_binding", "upsert_sender_identity",
        "add_event_action_filter", "update_event_action_filter", "delete_event_action_filter",
        # 事件动作过滤器启用规则读口（api/event_rule.py L1 准入判定；任意拓扑）
        "event_action_filter_enabled_rows",
        # 流水线责任人展示（runtime/routes/pipeline.py；任意拓扑）
        "feishu_name_by_platform_column", "repo_bot_usernames_any",
        # VOD Bot 导航：仓库轨安装反查（runtime/im/vod_handler_async.py；任意拓扑）
        "installation_repo_app_ids_any", "installation_repo_keys_by_app_any",
        # IM 通知扇出目标解析（runtime/im/notify.py；任意拓扑）
        "installation_user_active_rows_any", "installation_p2p_chat_ids_any",
        # 互动卡片台账（runtime/im/engine/outbound_cards.py；任意拓扑）
        "outbound_card_put_any", "outbound_card_chat_row_any",
        "add_user_feedback", "save_inbound_message", "upsert_outbound_card",
        "read_one", "read_all",
        # ===== 镜像专项同步（2026-11-21）：按 test_public_primitives_registry_sync 口径
        # 补齐历史欠账（docs/weekly-11/rds-upgrade-execution-status.md 记录的既有红：
        # public 面逐批增长而镜像未跟随）。语义效应单向：provider.decide_mode 对 public
        # 原语恒取 lib（本期无 remote 传输通道），补齐后不再有原语落进默认 remote 分支。
        "account_auth_row", "account_id_by_platform_login", "account_id_by_username",
        "account_id_by_username_ci_any", "account_id_by_username_first_any",
        "account_id_by_username_valid_any", "account_name_rows_by_ids",
        "account_names_by_ids", "account_username_by_id_any",
        "active_account_id_by_username", "add_event_action_filter_row",
        "all_bot_usernames", "any_engine_sandbox_provider",
        "any_repo_credential_for_repos", "api_token_by_sha", "api_token_insert",
        "api_token_list", "api_token_revoke", "api_token_touch", "audit_recent_rows",
        "autopilot_subscribers_any", "autopilot_task_brief_any",
        "autopilot_task_last_run_any", "autopilot_task_row_any",
        "backfill_org_id_by_repo", "baseline_knowledge_files_any",
        "bind_feishu_mapping_row", "category_dict_active_map", "category_dict_rows",
        "channel_access_token", "chat_binding_get", "chat_binding_upsert",
        "check_repo_member", "clear_feishu_by_app", "clear_feishu_by_user",
        "clear_user_mapping_feishu_any", "content_limit_config_hint",
        "count_active_scenes_any", "count_engine_sandbox_whitelist",
        "count_featured_official_scenes_any", "count_manual_sandbox_bindings",
        "credential_list", "decrypt", "delete_event_action_filter_row",
        "delete_event_knowledge_any", "delete_feishu_onboard_task_any",
        "disable_sandbox_template_bindings", "egress_admin_exceptions",
        "egress_policy_for_skill", "egress_policy_global", "encrypt",
        "engine_ref_snapshot", "engine_sandbox_mode", "engine_sandbox_paths_of",
        "engine_sandbox_provider_of", "engine_sandbox_toggles",
        "engine_sandbox_whitelist_domains", "engine_sandbox_whitelist_rows",
        "ensure_baseline_knowledge_ref_any", "ensure_gsb_registry_schema",
        "ensure_outbound_ledger_table", "ensure_repo_row_any",
        "ensure_user_mapping_schema", "exec_any", "external_user_id",
        "external_user_id_by_login", "feedback_apply_statuses", "feedback_claim_row",
        "feedback_external_sync_page", "feedback_internal_sync_page",
        "feedback_list_claims", "feedback_mark_converted",
        "feedback_mark_converted_from_claim", "feedback_mark_status_if_live",
        "feedback_pending_conversions", "feedback_release_claim",
        "feishu_open_id_by_platform_column", "feishu_open_id_by_user", "filter_repos",
        "first_join_user_id_by_repo", "format_content_limit", "get_all_managed_repos",
        "get_browser_callback_url", "get_business_api_url", "get_credential",
        "get_dispatch_aggregate_comment", "get_global_bot_usernames_for",
        "get_grape_account_by_id", "get_grape_account_by_username", "get_host_detail",
        "get_host_user_id", "get_host_vcs_username", "get_managed_repos",
        "get_org_dispatch_snapshot", "get_org_id_by_repo_key", "get_org_is_personal",
        "get_repo_admin_identity", "get_runtime_id_by_engine_uid",
        "get_scene_org_metadata", "get_server_base_url_warning",
        "get_skill_content_hash", "get_skill_hash_by_id", "get_skill_hash_by_key",
        "get_skill_hashes_by_name_scope", "get_skill_object_key",
        "get_url_split_warning", "get_user_identities", "get_user_repos",
        "get_worker_connect_url", "gitcode_req", "gitcode_req_form", "gitcode_token",
        "github_req", "github_token", "global_bot_token_for",
        "global_gitcode_bot_token", "global_github_bot_token",
        "grape_account_by_username", "gsb_all_services", "gsb_discover_row",
        "gsb_heartbeat", "gsb_instances", "gsb_mark_offline", "gsb_online_services",
        "gsb_purge_stale", "gsb_register", "gsb_registry_ddl",
        "hard_delete_uploaded_file", "has_manual_knowledge_ref", "has_refresh_token",
        "inbound_dedup_claim", "inbound_dedup_release", "inbound_dedup_seen",
        "inbound_dedup_set_state", "inbound_message_save_and_mark",
        "insert_sandbox_provider", "insert_sandbox_template", "installation_activate",
        "installation_active_configs", "installation_active_configs_any",
        "installation_active_configs_by_type_any", "installation_active_installs",
        "installation_active_installs_any", "installation_active_row",
        "installation_active_rows", "installation_config_by_app",
        "installation_detail_by_id_any", "installation_holders_any",
        "installation_list_any", "installation_other_refs_any",
        "installation_owner_by_app_any", "installation_owner_by_id_any",
        "installation_provider_rows_any", "installation_revoke",
        "installation_set_config", "installation_set_config_and_fingerprint_any",
        "installation_set_config_any", "installation_set_status_any",
        "installation_status", "installation_status_counts_any",
        "installation_user_channel_rows_any", "invalidate_global_bot_cache",
        "invalidate_provider_cache", "invalidate_user_repos", "is_known_engine_name",
        "is_org_personal", "is_server_base_url_configured", "knowledge_ids_scoped",
        "knowledge_obs_keys_scoped", "lease_acquire_row", "lease_keys_valid",
        "lease_owner_info", "lease_renew_all_by_owner", "lease_renew_expiry",
        "lease_tokens_by_keys", "list_active_scene_bindings",
        "list_active_scene_nodes", "list_agent_knowledge_refs",
        "list_autopilot_engine_ids_nonempty", "list_enabled_autopilot_engine_ids",
        "list_enabled_autopilot_tasks_any", "list_feishu_name_open_id_pairs",
        "list_official_scene_ids_names_any", "list_platform_links",
        "list_pr_subscription_scenes_any", "list_providers", "list_scene_market",
        "list_temp_uploaded_files_before", "list_user_mappings",
        "load_org_snapshot_files", "log_audit", "login_whitelist_count",
        "login_whitelist_legacy_column", "login_whitelist_legacy_column_match",
        "login_whitelist_row_match", "managed_repo_host_user",
        "managed_repos_for_user", "managed_webhook_id", "mapped_feishu_user_ids",
        "mapped_user_by_channel", "mapped_user_id", "mapping_user_id_by_column",
        "mapping_user_id_preferring_account", "mark_default_featured_any",
        "mark_dispatch_aggregate_comment", "mark_external_identity_expired_any",
        "mark_repo_platform_bot_status", "max_content_bytes", "member_role",
        "meta_value", "migrate_org_scene_type", "migrate_repo_key_mgmt",
        "oauth_token_status_any", "official_scene_id_any", "official_scene_row_any",
        "org_by_id", "org_by_slug", "org_dispatch_terminal_statuses", "org_member",
        "outbound_ledger_ddl", "outbound_ledger_has_edit", "outbound_ledger_has_id",
        "outbound_ledger_has_within", "outbound_ledger_row_by_hash",
        "patrol_finding_claim", "patrol_finding_mark", "patrol_finding_row",
        "patrol_findings_delete_terminal", "patrol_findings_insert_many",
        "patrol_findings_insert_one", "patrol_findings_known",
        "patrol_open_finding_ids", "patrol_reclaim_stale_claimings", "personal_org_id",
        "pipeline_config", "pipeline_scene_advanced_options",
        "platform_login_by_user_id_any", "purge_outbound_ledger_before",
        "purge_repo_rows", "purge_runtime_binding", "purge_task_proxy_tokens",
        "read_all_any", "read_one_any", "reassign_runtime_binding",
        "record_outbound_ledger", "refresh_gitcode_oauth_token", "refresh_oauth_token",
        "remove_managed_repo", "replace_scene_skill_ref_any", "repo_admin_user_id",
        "repo_bot_token", "repo_bot_token_from_legacy_column_any",
        "repo_bot_token_no_global", "repo_bot_token_status_any",
        "repo_bot_username_any", "repo_bound_scene_rows_any", "repo_credential_value",
        "repo_dev_scene", "repo_key_exists_any", "repo_key_in_candidates",
        "repo_keys_existing_any", "repo_llm_credential_row", "repo_member_user_ids",
        "repo_org_id_any", "repo_org_map", "repo_owner_name_any",
        "repo_platform_cards", "repo_registered_any", "repo_scene",
        "repo_secret_decrypt", "repo_token_for", "repo_token_with_refresh",
        "repo_user_id_by_platform_login", "resolve_feishu_open_id",
        "resolve_official_checker_skill", "resolve_repo_key", "resolve_skill_dispatch",
        "resolve_skill_id", "resolve_user_id_by_platform_login",
        "sandbox_provider_by_credential", "sandbox_provider_credential_id",
        "sandbox_provider_credential_ids", "sandbox_provider_endpoint",
        "sandbox_provider_ids_by_credential", "sandbox_provider_max_sandboxes",
        "sandbox_provider_owned_by", "sandbox_provider_platform",
        "sandbox_providers_by_credential", "sandbox_template_candidates",
        "sandbox_template_id", "sandbox_template_provider_of", "sandbox_template_row",
        "scene_active_flags_any", "scene_active_nodes_any",
        "scene_active_official_id_by_name_any", "scene_by_id",
        "scene_copy_id_by_market_any", "scene_display_name_rows",
        "scene_id_exists_any", "scene_linked_repos_any", "scene_linked_repos_rows_any",
        "scene_list", "scene_names_by_repo_any", "scene_nodes_any",
        "scene_nodes_row_by_id_any", "scene_nodes_write_by_id",
        "seed_official_scene_market", "sender_identity_bind", "sender_identity_user",
        "sender_identity_user_by_app_any", "server_base_url", "session_token_by_sha",
        "session_token_delete_by_session", "session_token_delete_expired",
        "session_token_insert", "session_token_renew", "session_token_rotate",
        "session_token_set_expiry", "session_token_shrink_active",
        "set_autopilot_engine_ids_any", "set_autopilot_run_info_any",
        "set_gitcode_nickname_any", "set_host_status", "set_meta_value",
        "set_repo_bot_token_status_any", "set_repo_deploy_url_any",
        "set_repo_dev_scene_cas_any", "set_scene_deploy_url_any", "set_scene_icon_any",
        "skill_by_id", "skill_category_active_map", "skill_category_map", "skill_path",
        "stored_access_token_any", "sync_agent_tags", "sync_user_mapping",
        "task_proxy_token_row", "task_token", "token_expires_at_any",
        "unified_user_mapping_ddl", "update_event_action_filter_row",
        "update_sandbox_provider", "update_skill_content_hash",
        "upsert_baseline_knowledge_any", "upsert_sandbox_template_binding",
        "upsert_scene_content_any", "upsert_task_proxy_token",
        "user_account_username_by_id_any", "user_llm_credential_row",
        "user_mapping_row_by_open_id", "user_repo_keys_by_username",
        "usernames_by_ids", "write_back_feishu_identity"
    ),
    "runtime": (
        "list_runtimes", "list_runtimes_rows", "get_runtime", "task_stats",
        "append_task_message", "list_credential_rows", "latest_task_for_event",
        "task_brief", "get_skill_by_id", "get_skill_content_hash", "count_skills",
        "aggregate_counts", "list_repo_credentials", "has_engine_capability",
        "list_engine_capabilities", "list_engine_tags", "query_events",
        "group_events", "count_events", "get_autopilot_execution",
        "upsert_engine_capability", "update_engine_capability",
        "delete_engine_capability", "acquire_channel_lease", "release_channel_lease",
        "heartbeat_instance", "list_active_instances", "release_all_leases",
        "renew_channel_lease", "purge_user_rows", "delete_chat_conversations",
        "delete_vod_conversations", "detach_agent_sessions",
        "clear_agent_session_runtime", "ensure_agent_session", "stamp_task_traces",
        "mark_detach_rebound", "delete_autopilot_executions",
        "set_autopilot_findings", "set_repo_im_notifications",
        "add_event_action_filter", "update_event_action_filter",
        "delete_event_action_filter", "update_skill_content", "update_skill_obs",
        "insert_skill", "create_skill_row", "delete_skill_row", "delete_skill_by_id",
        "adjust_skill_like_count", "insert_runtime_credential",
        "rename_runtime_credential", "set_runtime_credential_scope",
        "delete_runtime_credential", "upsert_repo_credential", "clear_repo_credential",
        "ensure_device_credential", "touch_credential_metrics",
        # 本机指标上报任意拓扑版（api/server_tasks.py insight 进程 15s 心跳）
        "device_credential_id_any", "register_device_credential_any",
        "touch_device_credential_any",
        # 流水线业务状态 / 阶段任务引用（api/pipeline_status.py 任意拓扑读口）
        "issue_business_stage_status_any", "issue_state_stage_any",
        "segment_task_rows_any",
        "upsert_local_runtimes", "ensure_default_runtime", "set_agent_tags",
        "purge_runtime_credential", "change_credential_scope",
        "reassign_repo_org_id",
        # P2 服务接口补齐（N1/N2/N3/N4/N5/N11/N12）
        "list_skills", "get_skill_detail", "list_all_engine_capabilities",
        "get_runtime_credential_meta", "get_meta_namespace", "tasks_brief_many",
        "task_stats_extended", "xwrite_apply",
        # W2 写原语
        "upsert_event_active_guard", "upsert_issue_agent_session", "upsert_scene_category",
        "record_event_skipped",
        "list_user_conversations", "get_scene_category", "list_scene_categories",
        "list_featured_skills", "list_active_credential_ids", "list_runtime_credentials",
        "list_autopilot_executions", "get_meta", "set_meta_value",
        "read_one", "read_all",
        # ===== 镜像专项同步（2026-11-21）：按 test_public_primitives_registry_sync 口径
        # 补齐历史欠账（docs/weekly-11/rds-upgrade-execution-status.md 记录的既有红：
        # public 面逐批增长而镜像未跟随）。语义效应单向：provider.decide_mode 对 public
        # 原语恒取 lib（本期无 remote 传输通道），补齐后不再有原语落进默认 remote 分支。
        "a2a_active_dup_rows", "a2a_active_event_rows", "a2a_storm_event_times",
        "a2a_storm_skip_rows", "a2a_window_dedup_rows", "acquire_distributed_lock",
        "agent_runtime_health_snapshot_any", "agent_session_row",
        "agent_sessions_with_runtime", "agent_task_activity_rows_any",
        "agent_types_search_global", "aggregate_skill_consistency",
        "alerts_credential_heartbeats", "alerts_task_inflight", "alerts_task_mutation",
        "alerts_task_rows_total", "alerts_task_terminal", "alerts_task_timeout",
        "append_chat_message", "apply_issue_vcs_existence_any", "apply_runtime_state",
        "autopilot_execution_detail_any", "autopilot_execution_findings_result_any",
        "backend_registered", "backfill_org_id_by_repo", "backfill_skill_category_id",
        "bot_context", "build_platform_card", "cancel_direct_task",
        "cancel_inflight_tasks", "cancel_issue_core", "cancel_task",
        "canonical_engine", "chat_conversations_delete_by_im_channel_any",
        "chat_session_ddl", "claim_meta_lease", "cleanup_expired_distributed_locks",
        "cleanup_scene_engine_bindings", "clear_event_active_guard",
        "clone_auth_scheme_for", "cloud_sandboxes_active_count", "columns_exist",
        "credential_brief", "credential_health_rows_any", "credential_id_by_name_any",
        "credential_id_uid_pairs", "credential_ids_visible", "credential_scope_owner",
        "credential_uid_by_id", "credential_usage_log_insert_many",
        "credential_usage_log_prune", "dashboard_avg_durations",
        "dashboard_credential_name_device", "dashboard_credential_scope_owner",
        "dashboard_failed_tasks", "dashboard_issue_contrib",
        "dashboard_issue_latest_status_counts", "dashboard_issue_status_counts",
        "dashboard_local_runtimes", "dashboard_running_counts",
        "dashboard_runtime_rows", "dashboard_runtimes_with_credential",
        "dashboard_stuck_tasks", "dashboard_task_by_source",
        "dashboard_task_issue_rows", "dashboard_task_status_counts",
        "dashboard_today_status_counts", "dashboard_worker_rows", "dashboard_workers",
        "dedup_auto_merge_allowed", "dedup_level_suspected", "default_registry",
        "default_vcs_provider", "delete_agent_sessions",
        "delete_autopilot_executions_by_task_any", "derive_local_runtime_health",
        "derive_runtime_health", "detect_engine_model", "devops_session_runtime_id",
        "dingtalk_card_client", "discover_backends", "efficiency_issue_contrib",
        "efficiency_issue_detail_rows", "efficiency_issue_rows",
        "efficiency_task_rows", "engine_mode", "engine_mounts_to_credential",
        "engine_plugins_info", "engine_types", "engines_by_ids",
        "ensure_chat_session_schema", "ensure_credential_uid", "ensure_issue_card",
        "ensure_repo_im_binding_schema", "event_cascade_counts_by_repo_any",
        "event_created_ats_by_types", "event_dashboard_metrics",
        "event_issue_id_by_seq", "event_payload", "event_status_counts",
        "events_search", "events_search_global", "exec_agent_task", "exec_any",
        "explain_engine_selection", "failed_event_gap_counts_any",
        "failed_event_gap_samples_any", "feishu_api", "feishu_client",
        "first_online_runtime", "get_active_installation", "get_adapter",
        "get_all_adapters", "get_cloud_platforms", "get_cloud_registry",
        "get_detached_engine_hints", "get_execution_mode", "get_health_score",
        "get_meta_value_any", "get_platform_card", "get_scene_engine_selector",
        "get_scene_pool_engine_ids", "get_skill_storage", "get_skill_zip_bytes",
        "get_storage", "has_engine_type", "has_inflight", "has_inflight_repo",
        "health_runtime_rows", "health_task_inflight_count", "health_task_rows_total",
        "health_task_status_counts", "hosting_full_flow_providers", "im_db_connection",
        "inflight_task_count_any", "inflight_tasks", "installation_fingerprint",
        "instance_name", "invalidate_cache", "invalidate_platform_card_cache",
        "invalidate_skill_caches", "is_plugin_mode", "issue_bound_scene_rows_any",
        "issue_brief", "issue_stage_status_rows", "issue_vcs_backfill_repo_keys_any",
        "issue_vcs_checked_count_any", "issue_vcs_index_rows_any", "join_feishu_chat",
        "known_agent_roles", "latest_event_brief", "latest_task_status",
        "list_featured_skills_any", "list_runtime_id_uid_rows",
        "list_runtime_name_rows", "list_runtime_uid_rows", "list_vcs_platforms",
        "list_zip_sub_skills", "load_engine_selection", "load_project_info",
        "lock_lease_status_any", "mark_cloud_sandboxes_orphaned_by_provider",
        "mark_issue_processing", "mem_cache_invalidate", "merge_meta_json_any",
        "meta_delete_keys", "meta_keys_like", "meta_set", "migrate_repo_key_runtime",
        "module_for", "native_issue_number", "new_out_track_id", "node_agent_role",
        "node_matches_agent_role", "norm_repo", "normalize_dedup_level",
        "notify_stats", "now_iso", "obs_overview_credential_status_rows_any",
        "obs_overview_task_rows_any", "online_credential_count_any", "outbox_counts",
        "outbox_get_row", "outbox_list_rows", "outbox_recent_undelivered",
        "outbox_requeue_for_retry", "parse_card_action", "parse_dt",
        "patrol_dispatch_stall_threshold_seconds", "pr_web_path",
        "probe_aishell_dsh_acp", "probe_hermes_acp", "probe_local_engines",
        "purge_env_credential_any", "purge_repo_rows", "qa_gate_latest_event_any",
        "qa_gate_latest_pr_payload_any", "query_engines", "query_events_any",
        "read_all_any", "read_one_any", "read_zip_sub_skill",
        "rebase_engine_uid_references", "rebuild_scene_skill_ref", "recent_issues",
        "reconcile_engine_references", "reconcile_for_caller", "recover_engine",
        "refresh_whitelist_cache", "release_distributed_lock",
        "rename_runtime_engine_uid", "renew_distributed_lock", "repo_im_binding_ddl",
        "require_credential", "reset_health", "resolve_default_engine",
        "resolve_engine", "resolve_instance_id", "revoke_installation", "run_git",
        "run_pipeline", "runtime_capability_match", "runtime_cred_rows_by_engine_uids",
        "runtime_credential_display_names", "runtime_credential_id",
        "runtime_credential_sample_rows_any", "runtime_credentials_gap_rows_any",
        "runtime_credentials_silent_rows_any", "runtime_credentials_visible_rows",
        "runtime_device_map", "runtime_engine_name", "runtime_heartbeat_alive_any",
        "runtime_ids_by_engine_uids", "runtime_locality", "runtime_model_name_any",
        "runtime_rebase_row", "runtime_status_healthy", "runtime_uid_of",
        "runtime_with_credential", "runtimes_credential_ids_any", "same_user",
        "sanitize_dedup_confirmed_by", "sanitize_dedup_note", "scan_local_skills",
        "scan_local_skills_with_detail", "scene_detach_log_rebound",
        "scene_skill_search", "schedule_notify", "scope_of_runtime",
        "seed_official_skill_if_missing", "selector_candidate_engines",
        "session_uid_mirror_sql", "set_autopilot_findings_any", "set_card_mode_file",
        "set_credential_uid", "set_execution_mode", "set_gitcode_nickname",
        "set_issue_scene_cas_any", "set_meta_if_absent", "set_meta_value_any",
        "set_meta_values_any", "set_runtime_engine_uid",
        "skill_active_display_rows_any", "skill_agent_counts",
        "skill_agent_type_by_priority_any", "skill_delete_by_id",
        "skill_display_name_by_id_any", "skill_display_name_by_scope_any",
        "skill_display_name_official_any", "skill_download_row", "skill_exists_active",
        "skill_id_by_name_scope", "skill_ids_active", "skill_key", "skill_md_content",
        "skill_rebind_candidate_rows_any", "skill_repo_count_by_name",
        "skill_repo_id_by_name", "skill_search", "skills_by_ids",
        "stamp_task_traces_any", "storage_delete_object", "storage_save_bytes",
        "strip_task_diagnostics", "summarize_task_errors", "sync_installation",
        "takeover_active_status", "takeover_session_status_any",
        "takeover_touch_updated_at_any", "task_attribution_row_any", "task_event_map",
        "task_issue_attribution", "task_issue_id_rows", "task_issue_repo",
        "task_recheck_row", "task_sampler_snapshot_any", "task_status_by_id",
        "task_status_counts_by_completed_any", "task_status_counts_by_created_any",
        "task_timeout_counts_by_repo_any", "task_timing_rows_any",
        "tasks_completed_page", "tasks_result_texts", "terminal_task_statuses",
        "timeout_fail_signature", "to_display", "trace_events_by_seq",
        "trace_runtime_label", "trace_task_steps", "trace_tasks_for_task",
        "trace_tasks_for_trace", "trace_unstamped_tasks",
        "update_agent_session_fields", "update_conversation_fields",
        "update_issue_agent_session_fields", "update_issue_card_fields",
        "update_skill_category_tags", "upsert_chat_config_row",
        "upsert_repo_im_binding", "usage_payload", "usage_retry_engine",
        "validate_selector_tags", "vod_agent_session_ensure_any",
        "vod_conversation_delete_by_session_any", "vod_conversations_delete",
        "vod_conversations_delete_by_repo", "vod_session_id_by_repo_any",
        "zip_cache_get", "zip_cache_get_with_hash", "zip_cache_put"
    ),
    "insight": (
        # W3 派生写（指标/日志落库）
        "record_api_latency", "record_merge_gate_log",
    ),
}

_MODES = ("lib", "remote")

# ---------------- DDL（与 gsb_service_registry 同库；不走 TABLE_REGISTRY） ----------------

ROUTE_DDL = """
CREATE TABLE IF NOT EXISTS gsb_provider_route (
    route_key  TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    timeout_ms INTEGER DEFAULT 1000,
    version    INTEGER DEFAULT 1,
    updated_at REAL DEFAULT 0,
    updated_by TEXT DEFAULT ''
)
"""

# ---------------- 进程内缓存（version + TTL 单飞刷新） ----------------

_cache_lock = threading.Lock()
_cache: dict[str, dict] = {}
_cache_version: int = 0
_cache_at: float = 0.0

_TTL_ENV = "GRAPE_GSB_ROUTE_TTL"
_TTL_DEFAULT = 30.0


def _ttl() -> float:
    """缓存 TTL 秒数（默认 30s；非法值回退默认并告警一次）。"""
    raw = (os.environ.get(_TTL_ENV) or "").strip()
    if not raw:
        return _TTL_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        log.warning(f"invalid {_TTL_ENV}={raw!r}, fallback to {_TTL_DEFAULT}")
        return _TTL_DEFAULT
    return value if value >= 0 else _TTL_DEFAULT


def reset_cache() -> None:
    """清空路由缓存（测试 / 配置写入后本进程立即生效用）。"""
    global _cache, _cache_version, _cache_at
    with _cache_lock:
        _cache = {}
        _cache_version = 0
        _cache_at = 0.0


def cache_state() -> dict:
    """缓存观测口（运维/测试）：快照大小、最大 version、加载时间。"""
    with _cache_lock:
        return {
            "routes": len(_cache),
            "version": _cache_version,
            "loaded_at": _cache_at,
            "age_seconds": round(max(0.0, time.time() - _cache_at), 1) if _cache_at else 0.0,
            "ttl": _ttl(),
        }


def ensure_table() -> None:
    """幂等建表（运行时惰性调用；迁移脚本同款 DDL）。"""
    with _owner_lock:
        c = _owner.cursor()
        c.execute(ROUTE_DDL)
        _owner.commit()


def _load_all() -> list[dict]:
    """全表读出为 dict 列表（SQLite Row / MySQL DictCursor 双兼容）。

    首次读前幂等建表：mgmt 域库由 provisioning 建，其它拓扑（单库/新环境）
    可能缺表，缺表时 snapshot() 只会退化成内置基线（表现是路由配置不生效）。
    """
    try:
        ensure_table()
    except Exception as _e:  # noqa: BLE001 - 建表失败不阻断读（读失败仍会退回基线）
        log.debug(f"gsb route config ensure_table skipped: {_e}")
    with _owner_lock:
        c = _owner.cursor()
        c.execute(
            "SELECT route_key, mode, timeout_ms, version, updated_at, updated_by "
            "FROM gsb_provider_route"
        )
        rows = c.fetchall()
    if not rows:
        return []
    if hasattr(rows[0], "keys"):
        return [dict(r) for r in rows]
    cols = [d[0] for d in c.description] if c.description else []
    return [dict(zip(cols, r)) for r in rows] if cols else []


def snapshot() -> dict[str, dict]:
    """取路由快照：到期才刷新（单飞），失败退回上次已知配置。

    - TTL 内直接回缓存（热路径 O(1)，无 env/DB 开销之外的开销）；
    - 刷新失败（表不存在/DB 抖动）：保留旧快照并把计时后移一个 TTL，
      避免每次调用都撞库；启动期首刷失败 → 空快照 → 调用方走内置基线。
    """
    global _cache, _cache_version, _cache_at
    now = time.time()
    with _cache_lock:
        if _cache_at and now - _cache_at < _ttl():
            return _cache
    try:
        rows = _load_all()
    except Exception as exc:  # noqa: BLE001 - 配置读失败退回上次已知配置
        log.warning(f"gsb route config load failed, keep last known: {exc}")
        with _cache_lock:
            _cache_at = now
        return _cache
    with _cache_lock:
        _cache = {r["route_key"]: r for r in rows}
        _cache_version = max((int(r.get("version") or 0) for r in rows), default=0)
        _cache_at = now
    return _cache


def get_cached_route(route_key: str) -> dict | None:
    """查单条路由配置（未配置返回 None，调用方按内置基线取值）。"""
    return snapshot().get(route_key)


def get_cached_timeout_ms(route_key: str) -> int | None:
    """路由配置的 per-method remote 超时（ms）。

    未配置/未填 → None（调用方维持客户端默认超时；显式配置才收紧/放宽，
    建议：热查询 1s、管理类查询 ≤3s，见 docs/issue-2479/spec.md）。
    """
    row = get_cached_route(route_key)
    if not row:
        return None
    try:
        value = row.get("timeout_ms")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------- 写接口（改表即热切换：其它进程 ≤TTL 生效，本进程立即生效） ----------------


def set_route(route_key: str, mode: str, timeout_ms: int | None = None,
              updated_by: str = "") -> None:
    """插入/更新一行路由配置（version 自增）。

    Args:
        route_key: ``service.method`` 或 ``domain.primitive``
        mode: ``lib`` / ``remote``（其它值 ValueError，不静默落库）
        timeout_ms: 该方法 remote 调用超时（缺省不修改既有值/默认 1000）
        updated_by: 变更人（审计）
    """
    key = (route_key or "").strip()
    if not key:
        raise ValueError("route_key must not be empty")
    value = (mode or "").strip().lower()
    if value not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}, got {mode!r}")
    if timeout_ms is not None:
        timeout_ms = int(timeout_ms)
        if timeout_ms <= 0:
            raise ValueError(f"timeout_ms must be positive, got {timeout_ms}")
    ensure_table()
    with _owner_lock:
        c = _owner.cursor()
        c.execute("SELECT version, timeout_ms FROM gsb_provider_route WHERE route_key=?", (key,))
        row = c.fetchone()
        next_version = (int(row[0]) + 1) if row else 1
        # 未显式给 timeout_ms：沿用既有行值；新行落 NULL（= 客户端默认超时，
        # 不因配置 mode 而意外改变既有 remote 时延语义）
        effective_timeout = timeout_ms if timeout_ms is not None else (
            row[1] if row else None
        )
        c.execute(
            "INSERT OR REPLACE INTO gsb_provider_route "
            "(route_key, mode, timeout_ms, version, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, value, effective_timeout, next_version, time.time(), updated_by),
        )
        _owner.commit()
    reset_cache()


def remove_route(route_key: str) -> bool:
    """删除一行配置（回落内置基线）。返回是否确实删除了行。"""
    ensure_table()
    with _owner_lock:
        c = _owner.cursor()
        c.execute("DELETE FROM gsb_provider_route WHERE route_key=?", (route_key,))
        removed = bool(c.rowcount)
        _owner.commit()
    reset_cache()
    return removed


def list_routes() -> list[dict]:
    """列出全部路由配置行（运维视角，绕过缓存直读）。"""
    return _load_all()


def known_route_keys() -> list[str]:
    """全部已知路由 key（契约方法 + public 数据原语），一键全量的目标集。

    去重：同一能力可能既是契约方法（HTTP）又是 public 原语（lib），
    两者的路由 key 形如 `<service>.<method>` / `<domain>.<prim>`，可能同名
    （如 runtime.list_skills）——对配置表而言是**同一个 key**，只登记一次。
    """
    keys: list[str] = []
    for service, methods in CONTRACT_METHODS.items():
        keys.extend(f"{service}.{m}" for m in methods)
    for domain, prims in PUBLIC_PRIMITIVES.items():
        keys.extend(f"{domain}.{p}" for p in prims)
    return list(dict.fromkeys(keys))


def set_all_known(mode: str, updated_by: str = "") -> int:
    """一键全量登记：把所有已知 key 配成同一 mode（回滚 = 全量 lib）。

    Returns:
        登记行数（23 契约方法 + mgmt/runtime public 原语）。
    """
    for key in known_route_keys():
        set_route(key, mode, updated_by=updated_by)
    return len(known_route_keys())
