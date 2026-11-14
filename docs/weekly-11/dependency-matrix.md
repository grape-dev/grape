# 跨域依赖矩阵看板（P0-4 自动生成）

> 生成时间：2026-11-14 22:58　口径：`scripts/foreign_table_refs.py`（只算 api/kernel/domains 运行代码）
> 汇总：跨域表 **58** 张；跨域引用 **507** 处（99 个「表×消费域」对）；含归属域在内的 SQL 引用 2511 处

## 1. 域 × 域 跨域引用热力

| 消费方 ↓ / 归属域 → | mgmt | runtime | insight | 合计 |
| --- | ---: | ---: | ---: | ---: |
| api | 29 | 48 | 3 | 80 |
| kernel | 59 | 34 | 0 | 93 |
| mgmt | 0 | 96 | 6 | 102 |
| runtime | 145 | 0 | 1 | 146 |
| insight | 9 | 76 | 0 | 85 |

## 2. 处置分类进度

| 分类 | 表数 | 剩余跨域引用 |
| --- | ---: | ---: |
| D | 18 | 200 |
| C1 | 10 | 142 |
| C2 | 25 | 160 |
| B | 4 | 4 |
| E | 1 | 1 |

## 3. 全量明细（按剩余跨域引用降序）

| 表 | 归属 | 跨域引用 | 消费方 | 处置 | 写通道 | 文件数 |
| --- | --- | ---: | --- | --- | --- | ---: |
| `agent_tasks` | runtime | 49 | insight 35 / api 8 / mgmt 5 / kernel 1 | C2 outbox 跨域写 | W1 | 44 |
| `skill` | runtime | 42 | mgmt 29 / api 8 / kernel 5 | D 模块归位（kernel 写/读域表） | — | 18 |
| `runtime_credentials` | runtime | 32 | insight 17 / mgmt 8 / api 6 / kernel 1 | C1 归属重定（写路径挪进 owner 域） | — | 26 |
| `managed_repos` | mgmt | 29 | runtime 15 / kernel 10 / api 3 / insight 1 | D 模块归位（kernel 写/读域表） | — | 28 |
| `agent_runtimes` | runtime | 24 | mgmt 10 / insight 8 / api 4 / kernel 2 | C1 归属重定（写路径挪进 owner 域） | — | 36 |
| `devops_pipeline_scene` | mgmt | 21 | runtime 15 / api 4 / insight 2 | C1 归属重定（写路径挪进 owner 域） | — | 17 |
| `unified_user_mapping` | mgmt | 21 | runtime 17 / kernel 3 / insight 1 | D 模块归位（kernel 写/读域表） | — | 14 |
| `events` | runtime | 19 | insight 8 / api 7 / mgmt 4 | C1 归属重定（写路径挪进 owner 域） | — | 28 |
| `grape_t_channel_installation` | mgmt | 19 | runtime 19 | C2 outbox 跨域写 | W2 | 11 |
| `meta` | runtime | 19 | mgmt 11 / kernel 8 | D 模块归位（kernel 写/读域表） | — | 7 |
| `issue` | runtime | 18 | api 12 / insight 4 / mgmt 2 | D 模块归位（kernel 写/读域表） | — | 25 |
| `grape_accounts` | mgmt | 15 | runtime 13 / insight 2 | C2 outbox 跨域写 | W2 | 18 |
| `agent_shared_knowledge` | mgmt | 11 | runtime 8 / kernel 3 | C1 归属重定（写路径挪进 owner 域） | — | 9 |
| `devops_agent_session` | runtime | 11 | mgmt 10 / kernel 1 | C1 归属重定（写路径挪进 owner 域） | — | 16 |
| `user_repos` | mgmt | 11 | kernel 5 / runtime 3 / insight 2 / api 1 | D 模块归位（kernel 写/读域表） | — | 12 |
| `autopilot_tasks` | mgmt | 10 | runtime 10 | C2 outbox 跨域写 | W3 | 5 |
| `distributed_locks` | runtime | 10 | kernel 9 / insight 1 | D 模块归位（kernel 写/读域表） | — | 4 |
| `grape_t_channel_lease` | mgmt | 10 | runtime 10 | C2 outbox 跨域写 | W2 | 3 |
| `session_api_tokens` | mgmt | 10 | kernel 10 | D 模块归位（kernel 写/读域表） | — | 2 |
| `external_identities` | mgmt | 9 | api 4 / runtime 4 / kernel 1 | C2 outbox 跨域写 | W2 | 9 |
| `gsb_service_registry` | mgmt | 9 | kernel 9 | D 模块归位（kernel 写/读域表） | — | 1 |
| `scene_market` | mgmt | 9 | runtime 8 / api 1 | C1 归属重定（写路径挪进 owner 域） | — | 7 |
| `grape_t_patrol_findings` | mgmt | 7 | api 7 | C1 归属重定（写路径挪进 owner 域） | — | 4 |
| `grape_t_repo_credentials` | insight | 7 | mgmt 5 / contracts 1 / runtime 1 | C1 归属重定（写路径挪进 owner 域） | — | 6 |
| `outbound_content_ledger` | mgmt | 6 | kernel 6 | D 模块归位（kernel 写/读域表） | — | 1 |
| `user_feedback` | mgmt | 6 | api 6 | C2 outbox 跨域写 | W3 | 6 |
| `api_tokens` | mgmt | 5 | kernel 5 | D 模块归位（kernel 写/读域表） | — | 2 |
| `autopilot_executions` | runtime | 5 | mgmt 5 | C2 outbox 跨域写 | W3 | 4 |
| `grape_t_channel_inbound_dedup` | mgmt | 5 | runtime 5 | C2 outbox 跨域写 | W2 | 2 |
| `scene_knowledge_ref` | mgmt | 5 | kernel 3 / runtime 2 | D 模块归位（kernel 写/读域表） | — | 8 |
| `vod_conversations` | runtime | 5 | mgmt 4 / kernel 1 | D 模块归位（kernel 写/读域表） | — | 7 |
| `grape_t_channel_chat_session_binding` | mgmt | 4 | runtime 4 | C2 outbox 跨域写 | W2 | 3 |
| `login_whitelist` | mgmt | 4 | kernel 4 | C2 outbox 跨域写 | W2 | 4 |
| `agent_type` | runtime | 3 | mgmt 2 / insight 1 | C2 outbox 跨域写 | W1 | 4 |
| `chat_conversations` | runtime | 3 | mgmt 2 / kernel 1 | D 模块归位（kernel 写/读域表） | — | 9 |
| `event_action_filter` | mgmt | 3 | api 3 | C2 outbox 跨域写 | W2 | 4 |
| `grape_t_channel_sender_identity` | mgmt | 3 | runtime 3 | C2 outbox 跨域写 | W2 | 3 |
| `grape_t_credential_usage_log` | runtime | 3 | kernel 3 | D 模块归位（kernel 写/读域表） | — | 2 |
| `api_latency_log` | insight | 2 | api 2 | C2 outbox 跨域写 | W3 | 5 |
| `grape_t_channel_outbound_card` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W3 | 2 |
| `scene_skill_ref` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W2 | 6 |
| `takeover_sessions` | runtime | 2 | api 1 / mgmt 1 | C2 outbox 跨域写 | W1 | 4 |
| `task_messages` | runtime | 2 | insight 1 / kernel 1 | D 模块归位（kernel 写/读域表） | — | 12 |
| `audit_logs` | mgmt | 1 | insight 1 | E 洞察事件/宽表 | — | 3 |
| `autopilot_subscriptions` | mgmt | 1 | runtime 1 | B API 聚合端点 | — | 3 |
| `chat_agent_session` | runtime | 1 | mgmt 1 | D 模块归位（kernel 写/读域表） | — | 5 |
| `chat_messages` | runtime | 1 | kernel 1 | D 模块归位（kernel 写/读域表） | — | 4 |
| `engine_capabilities` | runtime | 1 | mgmt 1 | C1 归属重定（写路径挪进 owner 域） | — | 7 |
| `event_skipped` | runtime | 1 | api 1 | C2 outbox 跨域写 | W3 | 5 |
| `feishu_onboard_task` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W3 | 4 |
| `grape_t_channel_inbound_message` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W3 | 2 |
| `grape_t_merge_gate_log` | insight | 1 | api 1 | C2 outbox 跨域写 | W3 | 2 |
| `issue_state` | runtime | 1 | api 1 | C2 outbox 跨域写 | W1 | 4 |
| `obs_metric_rollup` | insight | 1 | mgmt 1 | B API 聚合端点 | — | 5 |
| `runtime_health_scores` | runtime | 1 | insight 1 | B API 聚合端点 | — | 5 |
| `scene_engine_detach_log` | runtime | 1 | mgmt 1 | C2 outbox 跨域写 | W3 | 4 |
| `skill_category` | mgmt | 1 | runtime 1 | B API 聚合端点 | — | 4 |
| `user_accounts` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 5 |

## 4. 用法

```bash
python3 scripts/gen_domain_dependency_board.py          # 刷新看板
python3 scripts/gen_domain_dependency_board.py --check  # 过期即失败（CI）
```

验收口径：G4「代码零外域表引用」= 本表 `跨域引用` 列全为 0 且 `docs/weekly-11/foreign-refs-allowlist.csv` 为空。
