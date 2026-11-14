# 跨域写通道覆盖度（P3 自动生成）

> 输入：`docs/weekly-11/cross-domain-write-channel.csv`；public 写函数扫描自 `src/domains/<owner>/public/__init__.py`；action 白名单扫描自 `src/domains/<d>/routes/internal.py`。

| 表 | 归属 | 通道 | 写引用 | 写方 | public 写函数 | 覆盖 |
| --- | --- | --- | ---: | --- | --- | --- |
| `admin_accounts` | mgmt | W2 | 1 | api:1 | — | ❌ |
| `agent_tasks` | runtime | W1 | 4 | api:2;insight:1;mgmt:1 | `stamp_task_traces` | ✅ |
| `agent_type` | runtime | W1 | 3 | api:3 | — | ❌ |
| `api_latency_log` | insight | W3 | 3 | api:3 | — | ❌ |
| `autopilot_executions` | runtime | W3 | 2 | mgmt:2 | `delete_autopilot_executions`, `set_autopilot_findings` | ✅ |
| `autopilot_tasks` | mgmt | W3 | 3 | runtime:3 | `set_autopilot_engine_ids`, `set_autopilot_run_info` | ✅ |
| `event_action_filter` | mgmt | W2 | 3 | runtime:3 | — | ❌ |
| `event_active_guard` | runtime | W2 | 3 | api:3 | — | ❌ |
| `event_skipped` | runtime | W3 | 1 | api:1 | — | ❌ |
| `external_identities` | mgmt | W2 | 1 | runtime:1 | `mark_external_identity_expired` | ✅ |
| `feishu_onboard_task` | mgmt | W3 | 1 | runtime:1 | `delete_feishu_onboard_task` | ✅ |
| `grape_accounts` | mgmt | W2 | 1 | api:1 | — | ❌ |
| `grape_t_channel_chat_session_binding` | mgmt | W2 | 1 | runtime:1 | — | ❌ |
| `grape_t_channel_inbound_dedup` | mgmt | W2 | 3 | runtime:3 | — | ❌ |
| `grape_t_channel_inbound_message` | mgmt | W3 | 1 | runtime:1 | — | ❌ |
| `grape_t_channel_installation` | mgmt | W2 | 5 | runtime:5 | — | ❌ |
| `grape_t_channel_lease` | mgmt | W2 | 9 | runtime:9 | — | ❌ |
| `grape_t_channel_outbound_card` | mgmt | W3 | 1 | runtime:1 | — | ❌ |
| `grape_t_channel_sender_identity` | mgmt | W2 | 1 | runtime:1 | — | ❌ |
| `grape_t_merge_gate_log` | insight | W3 | 1 | api:1 | — | ❌ |
| `grape_t_schema_migrations` | mgmt | X | 2 | api:2 | — | ❌ |
| `issue_agent_session` | runtime | W2 | 3 | api:3 | — | ❌ |
| `issue_state` | runtime | W1 | 1 | api:1 | — | ❌ |
| `login_whitelist` | mgmt | W2 | 1 | api:1 | — | ❌ |
| `organization` | mgmt | W2 | 2 | api:2 | — | ❌ |
| `scene_category` | runtime | W2 | 1 | api:1 | — | ❌ |
| `scene_engine_detach_log` | runtime | W3 | 1 | mgmt:1 | `mark_detach_rebound` | ✅ |
| `scene_skill_ref` | mgmt | W2 | 2 | api:1;runtime:1 | `replace_scene_skill_ref` | ✅ |
| `takeover_sessions` | runtime | W1 | 1 | mgmt:1 | — | ❌ |
| `user_accounts` | mgmt | W2 | 1 | api:1 | — | ❌ |
| `user_feedback` | mgmt | W3 | 3 | api:3 | — | ❌ |

合计：31 张表，7 张已有 owner public 写函数 （22%）。

W2 缺口（14）：`admin_accounts`、`event_action_filter`、`event_active_guard`、`grape_accounts`、`grape_t_channel_chat_session_binding`、`grape_t_channel_inbound_dedup`、`grape_t_channel_installation`、`grape_t_channel_lease`、`grape_t_channel_sender_identity`、`issue_agent_session`、`login_whitelist`、`organization`、`scene_category`、`user_accounts`
W3 缺口（6）：`api_latency_log`、`event_skipped`、`grape_t_channel_inbound_message`、`grape_t_channel_outbound_card`、`grape_t_merge_gate_log`、`user_feedback`（W3 可由 outbox action 承接，未实现前应保持同库写）

已登记 xwrite action：`feishu_onboard_task_delete`（mgmt）、`scene_deploy_url_set`（mgmt）、`scene_nodes_set`（mgmt）、`user_mapping_feishu_clear`（mgmt）、`user_mapping_feishu_upsert`（mgmt）
