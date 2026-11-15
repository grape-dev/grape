# 跨域写通道覆盖度（P3 自动生成）

> 输入：`docs/weekly-11/cross-domain-write-channel.csv`；public 写函数扫描自 `src/domains/<owner>/public/__init__.py`；action 白名单扫描自 `src/domains/<d>/routes/internal.py`。

| 表 | 归属 | 通道 | 写引用 | 写方 | public 写函数 | 覆盖 |
| --- | --- | --- | ---: | --- | --- | --- |
| `admin_accounts` | mgmt | W2 | 1 | api:1 | — | ◻︎ 同进程 |
| `agent_tasks` | runtime | W1 | 4 | api:2;insight:1;mgmt:1 | `stamp_task_traces` | ✅ |
| `agent_type` | runtime | W1 | 3 | api:3 | — | ❌ |
| `api_latency_log` | insight | W3 | 3 | api:3 | `record_api_latency` | ✅ |
| `autopilot_executions` | runtime | W3 | 2 | mgmt:2 | `delete_autopilot_executions`, `set_autopilot_findings` | ✅ |
| `autopilot_tasks` | mgmt | W3 | 3 | runtime:3 | `set_autopilot_engine_ids`, `set_autopilot_run_info` | ✅ |
| `event_action_filter` | mgmt | W2 | 3 | runtime:3 | `add_event_action_filter`, `add_event_action_filter_row`, `delete_event_action_filter`, `delete_event_action_filter_row`, `update_event_action_filter`, `update_event_action_filter_row` | ✅ |
| `event_active_guard` | runtime | W2 | 3 | api:3 | `upsert_event_active_guard` | ✅ |
| `event_skipped` | runtime | W3 | 1 | api:1 | `record_event_skipped` | ✅ |
| `external_identities` | mgmt | W2 | 1 | runtime:1 | `mark_external_identity_expired` | ✅ |
| `feishu_onboard_task` | mgmt | W3 | 1 | runtime:1 | `delete_feishu_onboard_task` | ✅ |
| `grape_accounts` | mgmt | W2 | 1 | api:1 | — | ◻︎ 同进程 |
| `grape_t_channel_chat_session_binding` | mgmt | W2 | 1 | runtime:1 | `chat_binding_upsert`, `upsert_chat_session_binding` | ✅ |
| `grape_t_channel_inbound_dedup` | mgmt | W2 | 3 | runtime:3 | `claim_inbound_dedup`, `inbound_dedup_release`, `inbound_dedup_set_state`, `inbound_message_save_and_mark` | ✅ |
| `grape_t_channel_inbound_message` | mgmt | W3 | 1 | runtime:1 | `save_inbound_message` | ✅ |
| `grape_t_channel_installation` | mgmt | W2 | 5 | runtime:5 | `installation_activate`, `installation_revoke`, `installation_set_config`, `installation_set_config_any`, `upsert_channel_installation` | ✅ |
| `grape_t_channel_lease` | mgmt | W2 | 9 | runtime:9 | `acquire_channel_lease`, `lease_acquire_row`, `lease_renew_all_by_owner`, `lease_renew_expiry`, `release_channel_lease`, `renew_channel_lease` | ✅ |
| `grape_t_channel_outbound_card` | mgmt | W3 | 1 | runtime:1 | `upsert_outbound_card` | ✅ |
| `grape_t_channel_sender_identity` | mgmt | W2 | 1 | runtime:1 | `sender_identity_bind`, `upsert_sender_identity` | ✅ |
| `grape_t_merge_gate_log` | insight | W3 | 1 | api:1 | `merge_gate_log_insert`, `record_merge_gate_log` | ✅ |
| `grape_t_schema_migrations` | mgmt | X | 2 | api:2 | — | ◻︎ 同进程 |
| `issue_agent_session` | runtime | W2 | 3 | api:3 | `update_issue_agent_session_fields`, `upsert_issue_agent_session` | ✅ |
| `issue_state` | runtime | W1 | 1 | api:1 | — | ❌ |
| `login_whitelist` | mgmt | W2 | 1 | api:1 | — | ◻︎ 同进程 |
| `organization` | mgmt | W2 | 2 | api:2 | — | ◻︎ 同进程 |
| `scene_category` | runtime | W2 | 1 | api:1 | `upsert_scene_category` | ✅ |
| `scene_engine_detach_log` | runtime | W3 | 1 | mgmt:1 | `mark_detach_rebound`, `scene_detach_log_rebound` | ✅ |
| `scene_skill_ref` | mgmt | W2 | 2 | api:1;runtime:1 | `replace_scene_skill_ref`, `replace_scene_skill_ref_any` | ✅ |
| `takeover_sessions` | runtime | W1 | 1 | mgmt:1 | — | ❌ |
| `user_accounts` | mgmt | W2 | 1 | api:1 | — | ◻︎ 同进程 |
| `user_feedback` | mgmt | W3 | 3 | api:3 | `add_user_feedback` | ✅ |

合计：31 张表，22 张已有 owner public 写函数 （70%）。

同进程免通道（6）：`admin_accounts`、`grape_accounts`、`grape_t_schema_migrations`、`login_whitelist`、`organization`、`user_accounts`——写方 `api` 与 owner `mgmt` 同在 mgmt 进程/同库，不产生跨实例写

已登记 xwrite action：`api_latency_record`（insight）、`feishu_onboard_task_delete`（mgmt）、`inbound_message_save`（mgmt）、`merge_gate_log_record`（insight）、`outbound_card_upsert`（mgmt）、`scene_deploy_url_set`（mgmt）、`scene_nodes_set`（mgmt）、`user_feedback_add`（mgmt）、`user_mapping_feishu_clear`（mgmt）、`user_mapping_feishu_upsert`（mgmt）
