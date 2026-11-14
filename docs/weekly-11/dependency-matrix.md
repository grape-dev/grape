# 跨域依赖矩阵看板（P0-4 自动生成）

> 生成时间：2026-11-14 22:15　口径：`scripts/foreign_table_refs.py`（只算 api/kernel/domains 运行代码）
> 汇总：跨域表 **60** 张；跨域引用 **597** 处（110 个「表×消费域」对）；含归属域在内的 SQL 引用 2572 处

## 1. 域 × 域 跨域引用热力

| 消费方 ↓ / 归属域 → | mgmt | runtime | insight | 合计 |
| --- | ---: | ---: | ---: | ---: |
| api | 53 | 81 | 4 | 138 |
| kernel | 59 | 34 | 0 | 93 |
| mgmt | 0 | 98 | 7 | 105 |
| runtime | 164 | 0 | 11 | 175 |
| insight | 9 | 76 | 0 | 85 |

## 2. 处置分类进度

| 分类 | 表数 | 剩余跨域引用 |
| --- | ---: | ---: |
| D | 18 | 222 |
| C1 | 10 | 174 |
| C2 | 27 | 195 |
| B | 4 | 5 |
| E | 1 | 1 |

## 3. 全量明细（按剩余跨域引用降序）

| 表 | 归属 | 跨域引用 | 消费方 | 处置 | 写通道 | 文件数 |
| --- | --- | ---: | --- | --- | --- | ---: |
| `agent_tasks` | runtime | 58 | insight 35 / api 17 / mgmt 5 / kernel 1 | C2 outbox 跨域写 | W1 | 45 |
| `skill` | runtime | 43 | mgmt 29 / api 9 / kernel 5 | D 模块归位（kernel 写/读域表） | — | 19 |
| `managed_repos` | mgmt | 34 | runtime 15 / kernel 10 / api 8 / insight 1 | D 模块归位（kernel 写/读域表） | — | 30 |
| `runtime_credentials` | runtime | 32 | insight 17 / mgmt 8 / api 6 / kernel 1 | C1 归属重定（写路径挪进 owner 域） | — | 27 |
| `agent_runtimes` | runtime | 29 | mgmt 10 / api 9 / insight 8 / kernel 2 | C1 归属重定（写路径挪进 owner 域） | — | 39 |
| `events` | runtime | 26 | api 14 / insight 8 / mgmt 4 | C1 归属重定（写路径挪进 owner 域） | — | 30 |
| `unified_user_mapping` | mgmt | 24 | runtime 19 / kernel 3 / api 1 / insight 1 | D 模块归位（kernel 写/读域表） | — | 16 |
| `meta` | runtime | 23 | mgmt 11 / kernel 8 / api 4 | D 模块归位（kernel 写/读域表） | — | 8 |
| `issue` | runtime | 22 | api 16 / insight 4 / mgmt 2 | D 模块归位（kernel 写/读域表） | — | 27 |
| `devops_pipeline_scene` | mgmt | 21 | runtime 15 / api 4 / insight 2 | C1 归属重定（写路径挪进 owner 域） | — | 17 |
| `grape_t_channel_installation` | mgmt | 19 | runtime 19 | C2 outbox 跨域写 | W2 | 11 |
| `grape_accounts` | mgmt | 18 | runtime 16 / insight 2 | C2 outbox 跨域写 | W2 | 19 |
| `grape_t_repo_credentials` | insight | 18 | runtime 11 / mgmt 6 / contracts 1 | C1 归属重定（写路径挪进 owner 域） | — | 7 |
| `grape_t_channel_lease` | mgmt | 15 | runtime 15 | C2 outbox 跨域写 | W2 | 4 |
| `agent_shared_knowledge` | mgmt | 13 | runtime 8 / kernel 3 / api 2 | C1 归属重定（写路径挪进 owner 域） | — | 10 |
| `external_identities` | mgmt | 13 | runtime 7 / api 5 / kernel 1 | C2 outbox 跨域写 | W2 | 11 |
| `autopilot_tasks` | mgmt | 12 | runtime 12 | C2 outbox 跨域写 | W3 | 6 |
| `scene_market` | mgmt | 12 | runtime 8 / api 4 | C1 归属重定（写路径挪进 owner 域） | — | 8 |
| `user_repos` | mgmt | 12 | kernel 5 / runtime 3 / api 2 / insight 2 | D 模块归位（kernel 写/读域表） | — | 13 |
| `devops_agent_session` | runtime | 11 | mgmt 10 / kernel 1 | C1 归属重定（写路径挪进 owner 域） | — | 16 |
| `grape_t_patrol_findings` | mgmt | 11 | api 11 | C1 归属重定（写路径挪进 owner 域） | — | 4 |
| `distributed_locks` | runtime | 10 | kernel 9 / insight 1 | D 模块归位（kernel 写/读域表） | — | 4 |
| `session_api_tokens` | mgmt | 10 | kernel 10 | D 模块归位（kernel 写/读域表） | — | 2 |
| `gsb_service_registry` | mgmt | 9 | kernel 9 | D 模块归位（kernel 写/读域表） | — | 1 |
| `autopilot_executions` | runtime | 7 | mgmt 7 | C2 outbox 跨域写 | W3 | 4 |
| `event_action_filter` | mgmt | 7 | runtime 4 / api 3 | C2 outbox 跨域写 | W2 | 5 |
| `scene_knowledge_ref` | mgmt | 7 | kernel 3 / api 2 / runtime 2 | D 模块归位（kernel 写/读域表） | — | 9 |
| `outbound_content_ledger` | mgmt | 6 | kernel 6 | D 模块归位（kernel 写/读域表） | — | 1 |
| `user_feedback` | mgmt | 6 | api 6 | C2 outbox 跨域写 | W3 | 6 |
| `api_tokens` | mgmt | 5 | kernel 5 | D 模块归位（kernel 写/读域表） | — | 2 |
| `grape_t_channel_inbound_dedup` | mgmt | 5 | runtime 5 | C2 outbox 跨域写 | W2 | 2 |
| `login_whitelist` | mgmt | 5 | kernel 4 / api 1 | C2 outbox 跨域写 | W2 | 5 |
| `vod_conversations` | runtime | 5 | mgmt 4 / kernel 1 | D 模块归位（kernel 写/读域表） | — | 7 |
| `chat_conversations` | runtime | 4 | mgmt 2 / api 1 / kernel 1 | D 模块归位（kernel 写/读域表） | — | 10 |
| `grape_t_channel_chat_session_binding` | mgmt | 4 | runtime 4 | C2 outbox 跨域写 | W2 | 3 |
| `agent_type` | runtime | 3 | mgmt 2 / insight 1 | C2 outbox 跨域写 | W1 | 4 |
| `api_latency_log` | insight | 3 | api 3 | C2 outbox 跨域写 | W3 | 5 |
| `grape_t_channel_sender_identity` | mgmt | 3 | runtime 3 | C2 outbox 跨域写 | W2 | 3 |
| `grape_t_credential_usage_log` | runtime | 3 | kernel 3 | D 模块归位（kernel 写/读域表） | — | 2 |
| `takeover_sessions` | runtime | 3 | api 2 / mgmt 1 | C2 outbox 跨域写 | W1 | 4 |
| `chat_agent_session` | runtime | 2 | api 1 / mgmt 1 | D 模块归位（kernel 写/读域表） | — | 6 |
| `grape_t_channel_outbound_card` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W3 | 2 |
| `grape_t_schema_migrations` | mgmt | 2 | api 2 | C2 outbox 跨域写 | X | 2 |
| `scene_skill_ref` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W2 | 6 |
| `skill_category` | mgmt | 2 | api 1 / runtime 1 | B API 聚合端点 | — | 4 |
| `task_messages` | runtime | 2 | insight 1 / kernel 1 | D 模块归位（kernel 写/读域表） | — | 12 |
| `audit_logs` | mgmt | 1 | insight 1 | E 洞察事件/宽表 | — | 3 |
| `autopilot_subscriptions` | mgmt | 1 | runtime 1 | B API 聚合端点 | — | 3 |
| `chat_messages` | runtime | 1 | kernel 1 | D 模块归位（kernel 写/读域表） | — | 4 |
| `engine_capabilities` | runtime | 1 | mgmt 1 | C1 归属重定（写路径挪进 owner 域） | — | 7 |
| `event_skipped` | runtime | 1 | api 1 | C2 outbox 跨域写 | W3 | 5 |
| `feishu_onboard_task` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W3 | 4 |
| `grape_t_channel_inbound_message` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W3 | 2 |
| `grape_t_merge_gate_log` | insight | 1 | api 1 | C2 outbox 跨域写 | W3 | 2 |
| `issue_state` | runtime | 1 | api 1 | C2 outbox 跨域写 | W1 | 4 |
| `obs_metric_rollup` | insight | 1 | mgmt 1 | B API 聚合端点 | — | 5 |
| `organization` | mgmt | 1 | api 1 | C2 outbox 跨域写 | W2 | 6 |
| `runtime_health_scores` | runtime | 1 | insight 1 | B API 聚合端点 | — | 5 |
| `scene_engine_detach_log` | runtime | 1 | mgmt 1 | C2 outbox 跨域写 | W3 | 4 |
| `user_accounts` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 5 |

## 4. 用法

```bash
python3 scripts/gen_domain_dependency_board.py          # 刷新看板
python3 scripts/gen_domain_dependency_board.py --check  # 过期即失败（CI）
```

验收口径：G4「代码零外域表引用」= 本表 `跨域引用` 列全为 0 且 `docs/weekly-11/foreign-refs-allowlist.csv` 为空。
