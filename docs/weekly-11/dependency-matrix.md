# 跨域依赖矩阵看板（P0-4 自动生成）

> 生成时间：2026-11-14 16:20　口径：`scripts/foreign_table_refs.py`（只算 api/kernel/domains 运行代码）
> 汇总：跨域表 **68** 张；跨域引用 **1232** 处（135 个「表×消费域」对）；含归属域在内的 SQL 引用 2711 处

## 1. 域 × 域 跨域引用热力

| 消费方 ↓ / 归属域 → | mgmt | runtime | insight | 合计 |
| --- | ---: | ---: | ---: | ---: |
| api | 224 | 342 | 5 | 571 |
| kernel | 88 | 89 | 0 | 177 |
| mgmt | 0 | 193 | 18 | 211 |
| runtime | 160 | 0 | 11 | 171 |
| insight | 9 | 92 | 0 | 101 |

## 2. 处置分类进度

| 分类 | 表数 | 剩余跨域引用 |
| --- | ---: | ---: |
| D | 21 | 490 |
| C1 | 10 | 419 |
| C2 | 30 | 310 |
| B | 6 | 12 |
| E | 1 | 1 |

## 3. 全量明细（按剩余跨域引用降序）

| 表 | 归属 | 跨域引用 | 消费方 | 处置 | 写通道 | 文件数 |
| --- | --- | ---: | --- | --- | --- | ---: |
| `skill` | runtime | 90 | mgmt 59 / api 26 / kernel 5 | D 模块归位（kernel 写/读域表） | — | 19 |
| `issue` | runtime | 87 | api 79 / insight 4 / mgmt 2 / kernel 2 | D 模块归位（kernel 写/读域表） | — | 27 |
| `agent_runtimes` | runtime | 81 | api 37 / mgmt 31 / insight 10 / kernel 3 | C1 归属重定（写路径挪进 owner 域） | — | 42 |
| `agent_tasks` | runtime | 70 | insight 36 / api 28 / mgmt 5 / kernel 1 | C2 outbox 跨域写 | W1 | 45 |
| `events` | runtime | 57 | api 36 / insight 17 / mgmt 4 | C1 归属重定（写路径挪进 owner 域） | — | 30 |
| `runtime_credentials` | runtime | 57 | mgmt 22 / insight 20 / api 14 / kernel 1 | C1 归属重定（写路径挪进 owner 域） | — | 29 |
| `meta` | runtime | 56 | api 24 / mgmt 24 / kernel 8 | D 模块归位（kernel 写/读域表） | — | 9 |
| `devops_pipeline_scene` | mgmt | 51 | api 34 / runtime 15 / insight 2 | C1 归属重定（写路径挪进 owner 域） | — | 17 |
| `agent_shared_knowledge` | mgmt | 48 | api 37 / runtime 8 / kernel 3 | C1 归属重定（写路径挪进 owner 域） | — | 9 |
| `unified_user_mapping` | mgmt | 45 | kernel 24 / runtime 14 / api 6 / insight 1 | D 模块归位（kernel 写/读域表） | — | 16 |
| `managed_repos` | mgmt | 40 | api 16 / runtime 13 / kernel 10 / insight 1 | D 模块归位（kernel 写/读域表） | — | 29 |
| `devops_agent_session` | runtime | 39 | api 25 / mgmt 12 / insight 1 / kernel 1 | C1 归属重定（写路径挪进 owner 域） | — | 20 |
| `chat_agent_session` | runtime | 33 | kernel 19 / api 13 / mgmt 1 | D 模块归位（kernel 写/读域表） | — | 5 |
| `scene_market` | mgmt | 32 | api 24 / runtime 8 | C1 归属重定（写路径挪进 owner 域） | — | 7 |
| `grape_t_repo_credentials` | insight | 29 | mgmt 16 / runtime 11 / api 1 / contracts 1 | C1 归属重定（写路径挪进 owner 域） | — | 7 |
| `grape_t_channel_installation` | mgmt | 26 | runtime 26 | C2 outbox 跨域写 | W2 | 12 |
| `chat_conversations` | runtime | 24 | kernel 14 / api 8 / mgmt 2 | D 模块归位（kernel 写/读域表） | — | 10 |
| `grape_accounts` | mgmt | 24 | runtime 12 / api 6 / kernel 4 / insight 2 | C2 outbox 跨域写 | W2 | 21 |
| `user_feedback` | mgmt | 22 | api 22 | C2 outbox 跨域写 | W3 | 6 |
| `issue_state` | runtime | 18 | api 18 | C2 outbox 跨域写 | W1 | 4 |
| `external_identities` | mgmt | 17 | runtime 7 / api 5 / kernel 5 | C2 outbox 跨域写 | W2 | 12 |
| `repo_im_group_binding` | runtime | 16 | kernel 11 / api 3 / mgmt 2 | D 模块归位（kernel 写/读域表） | — | 6 |
| `scene_knowledge_ref` | mgmt | 16 | api 11 / kernel 3 / runtime 2 | D 模块归位（kernel 写/读域表） | — | 8 |
| `grape_t_channel_lease` | mgmt | 15 | runtime 15 | C2 outbox 跨域写 | W2 | 4 |
| `user_repos` | mgmt | 14 | kernel 5 / api 4 / runtime 3 / insight 2 | D 模块归位（kernel 写/读域表） | — | 13 |
| `agent_type` | runtime | 13 | api 10 / mgmt 2 / insight 1 | C2 outbox 跨域写 | W1 | 5 |
| `grape_t_patrol_findings` | mgmt | 13 | api 13 | C1 归属重定（写路径挪进 owner 域） | — | 4 |
| `autopilot_tasks` | mgmt | 12 | runtime 12 | C2 outbox 跨域写 | W3 | 6 |
| `engine_capabilities` | runtime | 12 | mgmt 12 | C1 归属重定（写路径挪进 owner 域） | — | 9 |
| `chat_messages` | runtime | 10 | kernel 10 | D 模块归位（kernel 写/读域表） | — | 4 |
| `distributed_locks` | runtime | 10 | kernel 9 / insight 1 | D 模块归位（kernel 写/读域表） | — | 4 |
| `event_action_filter` | mgmt | 10 | api 6 / runtime 4 | C2 outbox 跨域写 | W2 | 5 |
| `session_api_tokens` | mgmt | 10 | kernel 10 | D 模块归位（kernel 写/读域表） | — | 2 |
| `user_accounts` | mgmt | 10 | api 9 / runtime 1 | C2 outbox 跨域写 | W2 | 5 |
| `gsb_service_registry` | mgmt | 9 | kernel 9 | D 模块归位（kernel 写/读域表） | — | 1 |
| `login_whitelist` | mgmt | 9 | api 5 / kernel 4 | C2 outbox 跨域写 | W2 | 4 |
| `vod_conversations` | runtime | 9 | api 4 / mgmt 4 / kernel 1 | D 模块归位（kernel 写/读域表） | — | 7 |
| `autopilot_executions` | runtime | 7 | mgmt 7 | C2 outbox 跨域写 | W3 | 4 |
| `organization` | mgmt | 7 | api 7 | C2 outbox 跨域写 | W2 | 5 |
| `issue_agent_session` | runtime | 6 | api 6 | C2 outbox 跨域写 | W2 | 5 |
| `outbound_content_ledger` | mgmt | 6 | kernel 6 | D 模块归位（kernel 写/读域表） | — | 1 |
| `scene_skill_ref` | mgmt | 6 | api 4 / runtime 2 | C2 outbox 跨域写 | W2 | 6 |
| `api_tokens` | mgmt | 5 | kernel 5 | D 模块归位（kernel 写/读域表） | — | 2 |
| `grape_t_channel_inbound_dedup` | mgmt | 5 | runtime 5 | C2 outbox 跨域写 | W2 | 2 |
| `grape_t_schema_migrations` | mgmt | 5 | api 5 | C2 outbox 跨域写 | X | 1 |
| `scene_category` | runtime | 5 | api 3 / mgmt 2 | C2 outbox 跨域写 | W2 | 4 |
| `grape_t_channel_chat_session_binding` | mgmt | 4 | runtime 4 | C2 outbox 跨域写 | W2 | 3 |
| `grape_t_credential_usage_log` | runtime | 4 | kernel 3 / api 1 | D 模块归位（kernel 写/读域表） | — | 2 |
| `api_latency_log` | insight | 3 | api 3 | C2 outbox 跨域写 | W3 | 5 |
| `grape_t_channel_sender_identity` | mgmt | 3 | runtime 3 | C2 outbox 跨域写 | W2 | 3 |
| `runtime_health_scores` | runtime | 3 | api 2 / insight 1 | B API 聚合端点 | — | 5 |
| `skill_category` | mgmt | 3 | api 2 / runtime 1 | B API 聚合端点 | — | 3 |
| `takeover_sessions` | runtime | 3 | api 2 / mgmt 1 | C2 outbox 跨域写 | W1 | 4 |
| `task_messages` | runtime | 3 | api 1 / insight 1 / kernel 1 | D 模块归位（kernel 写/读域表） | — | 12 |
| `user_credentials` | mgmt | 3 | api 3 | B API 聚合端点 | — | 5 |
| `admin_accounts` | mgmt | 2 | api 2 | C2 outbox 跨域写 | W2 | 3 |
| `event_route_config` | mgmt | 2 | api 2 | D 模块归位（kernel 写/读域表） | — | 1 |
| `event_skipped` | runtime | 2 | api 2 | C2 outbox 跨域写 | W3 | 5 |
| `grape_t_channel_outbound_card` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W3 | 2 |
| `audit_logs` | mgmt | 1 | insight 1 | E 洞察事件/宽表 | — | 3 |
| `autopilot_subscriptions` | mgmt | 1 | runtime 1 | B API 聚合端点 | — | 3 |
| `feishu_onboard_task` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W3 | 4 |
| `grape_t_channel_inbound_message` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W3 | 2 |
| `grape_t_merge_gate_log` | insight | 1 | api 1 | C2 outbox 跨域写 | W3 | 2 |
| `obs_llm_usage` | insight | 1 | mgmt 1 | B API 聚合端点 | — | 7 |
| `obs_metric_rollup` | insight | 1 | mgmt 1 | B API 聚合端点 | — | 5 |
| `org_member` | mgmt | 1 | api 1 | D 模块归位（kernel 写/读域表） | — | 6 |
| `scene_engine_detach_log` | runtime | 1 | mgmt 1 | C2 outbox 跨域写 | W3 | 4 |

## 4. 用法

```bash
python3 scripts/gen_domain_dependency_board.py          # 刷新看板
python3 scripts/gen_domain_dependency_board.py --check  # 过期即失败（CI）
```

验收口径：G4「代码零外域表引用」= 本表 `跨域引用` 列全为 0 且 `docs/weekly-11/foreign-refs-allowlist.csv` 为空。
