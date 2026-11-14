# 跨域依赖矩阵看板（P0-4 自动生成）

> 生成时间：2026-11-15 02:59　口径：`scripts/foreign_table_refs.py`（只算 api/kernel/domains 运行代码）
> 汇总：跨域表 **39** 张；跨域引用 **166** 处（55 个「表×消费域」对）；含归属域在内的 SQL 引用 2189 处

## 1. 域 × 域 跨域引用热力

| 消费方 ↓ / 归属域 → | mgmt | runtime | insight | 合计 |
| --- | ---: | ---: | ---: | ---: |
| api | 20 | 47 | 3 | 70 |
| kernel | 7 | 3 | 0 | 10 |
| mgmt | 0 | 14 | 5 | 19 |
| runtime | 30 | 0 | 1 | 31 |
| insight | 3 | 32 | 0 | 35 |

## 2. 处置分类进度

| 分类 | 表数 | 剩余跨域引用 |
| --- | ---: | ---: |
| D | 11 | 47 |
| C1 | 8 | 57 |
| C2 | 17 | 59 |
| B | 2 | 2 |
| E | 1 | 1 |

## 3. 全量明细（按剩余跨域引用降序）

| 表 | 归属 | 跨域引用 | 消费方 | 处置 | 写通道 | 文件数 |
| --- | --- | ---: | --- | --- | --- | ---: |
| `agent_tasks` | runtime | 18 | insight 9 / api 7 / mgmt 2 | C2 outbox 跨域写 | W1 | 36 |
| `issue` | runtime | 15 | api 12 / insight 2 / mgmt 1 | D 模块归位（kernel 写/读域表） | — | 23 |
| `events` | runtime | 13 | api 7 / insight 6 | C1 归属重定（写路径挪进 owner 域） | — | 24 |
| `runtime_credentials` | runtime | 13 | insight 7 / api 6 | C1 归属重定（写路径挪进 owner 域） | — | 17 |
| `agent_runtimes` | runtime | 9 | insight 5 / api 4 | C1 归属重定（写路径挪进 owner 域） | — | 28 |
| `skill` | runtime | 8 | api 8 | D 模块归位（kernel 写/读域表） | — | 11 |
| `devops_pipeline_scene` | mgmt | 7 | api 4 / runtime 2 / insight 1 | C1 归属重定（写路径挪进 owner 域） | — | 12 |
| `grape_t_repo_credentials` | insight | 7 | mgmt 5 / contracts 1 / runtime 1 | C1 归属重定（写路径挪进 owner 域） | — | 6 |
| `managed_repos` | mgmt | 7 | runtime 5 / api 2 | D 模块归位（kernel 写/读域表） | — | 20 |
| `external_identities` | mgmt | 6 | api 4 / runtime 2 | C2 outbox 跨域写 | W2 | 7 |
| `user_feedback` | mgmt | 6 | api 6 | C2 outbox 跨域写 | W3 | 6 |
| `agent_shared_knowledge` | mgmt | 5 | runtime 5 | C1 归属重定（写路径挪进 owner 域） | — | 6 |
| `grape_accounts` | mgmt | 4 | runtime 3 / insight 1 | C2 outbox 跨域写 | W2 | 14 |
| `grape_t_channel_installation` | mgmt | 4 | runtime 4 | C2 outbox 跨域写 | W2 | 7 |
| `login_whitelist` | mgmt | 4 | kernel 4 | C2 outbox 跨域写 | W2 | 4 |
| `meta` | runtime | 4 | mgmt 4 | D 模块归位（kernel 写/读域表） | — | 4 |
| `unified_user_mapping` | mgmt | 4 | kernel 3 / runtime 1 | D 模块归位（kernel 写/读域表） | — | 8 |
| `event_action_filter` | mgmt | 3 | api 3 | C2 outbox 跨域写 | W2 | 4 |
| `grape_t_credential_usage_log` | runtime | 3 | kernel 3 | D 模块归位（kernel 写/读域表） | — | 2 |
| `api_latency_log` | insight | 2 | api 2 | C2 outbox 跨域写 | W3 | 5 |
| `chat_conversations` | runtime | 2 | mgmt 2 | D 模块归位（kernel 写/读域表） | — | 8 |
| `devops_agent_session` | runtime | 2 | mgmt 2 | C1 归属重定（写路径挪进 owner 域） | — | 11 |
| `grape_t_channel_lease` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W2 | 2 |
| `grape_t_channel_outbound_card` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W3 | 2 |
| `takeover_sessions` | runtime | 2 | api 1 / mgmt 1 | C2 outbox 跨域写 | W1 | 4 |
| `agent_type` | runtime | 1 | insight 1 | C2 outbox 跨域写 | W1 | 3 |
| `audit_logs` | mgmt | 1 | insight 1 | E 洞察事件/宽表 | — | 3 |
| `chat_agent_session` | runtime | 1 | mgmt 1 | D 模块归位（kernel 写/读域表） | — | 5 |
| `distributed_locks` | runtime | 1 | insight 1 | D 模块归位（kernel 写/读域表） | — | 3 |
| `event_skipped` | runtime | 1 | api 1 | C2 outbox 跨域写 | W3 | 5 |
| `grape_t_channel_chat_session_binding` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 2 |
| `grape_t_merge_gate_log` | insight | 1 | api 1 | C2 outbox 跨域写 | W3 | 2 |
| `issue_state` | runtime | 1 | api 1 | C2 outbox 跨域写 | W1 | 4 |
| `runtime_health_scores` | runtime | 1 | insight 1 | B API 聚合端点 | — | 5 |
| `scene_knowledge_ref` | mgmt | 1 | runtime 1 | D 模块归位（kernel 写/读域表） | — | 5 |
| `scene_market` | mgmt | 1 | api 1 | C1 归属重定（写路径挪进 owner 域） | — | 6 |
| `skill_category` | mgmt | 1 | runtime 1 | B API 聚合端点 | — | 4 |
| `user_accounts` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 5 |
| `vod_conversations` | runtime | 1 | mgmt 1 | D 模块归位（kernel 写/读域表） | — | 5 |

## 4. 用法

```bash
python3 scripts/gen_domain_dependency_board.py          # 刷新看板
python3 scripts/gen_domain_dependency_board.py --check  # 过期即失败（CI）
```

验收口径：G4「代码零外域表引用」= 本表 `跨域引用` 列全为 0 且 `docs/weekly-11/foreign-refs-allowlist.csv` 为空。
