# 跨域依赖矩阵看板（P0-4 自动生成）

> 生成时间：2026-11-15 04:15　口径：`scripts/foreign_table_refs.py`（只算 api/kernel/domains 运行代码）
> 汇总：跨域表 **37** 张；跨域引用 **128** 处（49 个「表×消费域」对）；含归属域在内的 SQL 引用 2126 处

## 1. 域 × 域 跨域引用热力

| 消费方 ↓ / 归属域 → | mgmt | runtime | insight | 合计 |
| --- | ---: | ---: | ---: | ---: |
| api | 12 | 23 | 2 | 37 |
| kernel | 7 | 3 | 0 | 10 |
| mgmt | 0 | 14 | 5 | 19 |
| runtime | 30 | 0 | 1 | 31 |
| insight | 1 | 29 | 0 | 30 |

## 2. 处置分类进度

| 分类 | 表数 | 剩余跨域引用 |
| --- | ---: | ---: |
| D | 11 | 28 |
| C1 | 7 | 49 |
| C2 | 16 | 48 |
| B | 2 | 2 |
| E | 1 | 1 |

## 3. 全量明细（按剩余跨域引用降序）

| 表 | 归属 | 跨域引用 | 消费方 | 处置 | 写通道 | 文件数 |
| --- | --- | ---: | --- | --- | --- | ---: |
| `agent_tasks` | runtime | 14 | insight 8 / api 4 / mgmt 2 | C2 outbox 跨域写 | W1 | 34 |
| `runtime_credentials` | runtime | 13 | insight 7 / api 6 | C1 归属重定（写路径挪进 owner 域） | — | 17 |
| `events` | runtime | 9 | insight 6 / api 3 | C1 归属重定（写路径挪进 owner 域） | — | 23 |
| `agent_runtimes` | runtime | 8 | insight 5 / api 3 | C1 归属重定（写路径挪进 owner 域） | — | 27 |
| `grape_t_repo_credentials` | insight | 7 | mgmt 5 / contracts 1 / runtime 1 | C1 归属重定（写路径挪进 owner 域） | — | 6 |
| `external_identities` | mgmt | 6 | api 4 / runtime 2 | C2 outbox 跨域写 | W2 | 7 |
| `agent_shared_knowledge` | mgmt | 5 | runtime 5 | C1 归属重定（写路径挪进 owner 域） | — | 6 |
| `devops_pipeline_scene` | mgmt | 5 | api 3 / runtime 2 | C1 归属重定（写路径挪进 owner 域） | — | 10 |
| `managed_repos` | mgmt | 5 | runtime 5 | D 模块归位（kernel 写/读域表） | — | 19 |
| `grape_t_channel_installation` | mgmt | 4 | runtime 4 | C2 outbox 跨域写 | W2 | 7 |
| `issue` | runtime | 4 | api 3 / mgmt 1 | D 模块归位（kernel 写/读域表） | — | 19 |
| `login_whitelist` | mgmt | 4 | kernel 4 | C2 outbox 跨域写 | W2 | 4 |
| `meta` | runtime | 4 | mgmt 4 | D 模块归位（kernel 写/读域表） | — | 4 |
| `unified_user_mapping` | mgmt | 4 | kernel 3 / runtime 1 | D 模块归位（kernel 写/读域表） | — | 8 |
| `event_action_filter` | mgmt | 3 | api 3 | C2 outbox 跨域写 | W2 | 4 |
| `grape_accounts` | mgmt | 3 | runtime 3 | C2 outbox 跨域写 | W2 | 13 |
| `grape_t_credential_usage_log` | runtime | 3 | kernel 3 | D 模块归位（kernel 写/读域表） | — | 2 |
| `chat_conversations` | runtime | 2 | mgmt 2 | D 模块归位（kernel 写/读域表） | — | 8 |
| `devops_agent_session` | runtime | 2 | mgmt 2 | C1 归属重定（写路径挪进 owner 域） | — | 11 |
| `grape_t_channel_lease` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W2 | 2 |
| `grape_t_channel_outbound_card` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W3 | 2 |
| `skill` | runtime | 2 | api 2 | D 模块归位（kernel 写/读域表） | — | 10 |
| `takeover_sessions` | runtime | 2 | api 1 / mgmt 1 | C2 outbox 跨域写 | W1 | 4 |
| `user_feedback` | mgmt | 2 | api 2 | C2 outbox 跨域写 | W3 | 5 |
| `agent_type` | runtime | 1 | insight 1 | C2 outbox 跨域写 | W1 | 3 |
| `api_latency_log` | insight | 1 | api 1 | C2 outbox 跨域写 | W3 | 4 |
| `audit_logs` | mgmt | 1 | insight 1 | E 洞察事件/宽表 | — | 3 |
| `chat_agent_session` | runtime | 1 | mgmt 1 | D 模块归位（kernel 写/读域表） | — | 5 |
| `distributed_locks` | runtime | 1 | insight 1 | D 模块归位（kernel 写/读域表） | — | 3 |
| `grape_t_channel_chat_session_binding` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 2 |
| `grape_t_merge_gate_log` | insight | 1 | api 1 | C2 outbox 跨域写 | W3 | 2 |
| `issue_state` | runtime | 1 | api 1 | C2 outbox 跨域写 | W1 | 4 |
| `runtime_health_scores` | runtime | 1 | insight 1 | B API 聚合端点 | — | 5 |
| `scene_knowledge_ref` | mgmt | 1 | runtime 1 | D 模块归位（kernel 写/读域表） | — | 5 |
| `skill_category` | mgmt | 1 | runtime 1 | B API 聚合端点 | — | 4 |
| `user_accounts` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 5 |
| `vod_conversations` | runtime | 1 | mgmt 1 | D 模块归位（kernel 写/读域表） | — | 5 |

## 4. 用法

```bash
python3 scripts/gen_domain_dependency_board.py          # 刷新看板
python3 scripts/gen_domain_dependency_board.py --check  # 过期即失败（CI）
```

验收口径：G4「代码零外域表引用」= 本表 `跨域引用` 列全为 0 且 `docs/weekly-11/foreign-refs-allowlist.csv` 为空。
