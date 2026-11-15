# 跨域依赖矩阵看板（P0-4 自动生成）

> 生成时间：2026-11-15 08:15　口径：`scripts/foreign_table_refs.py`（只算 api/kernel/domains 运行代码）
> 汇总：跨域表 **19** 张；跨域引用 **31** 处（20 个「表×消费域」对）；含归属域在内的 SQL 引用 1430 处

## 1. 域 × 域 跨域引用热力

| 消费方 ↓ / 归属域 → | mgmt | runtime | insight | 合计 |
| --- | ---: | ---: | ---: | ---: |
| api | 2 | 7 | 2 | 11 |
| kernel | 3 | 3 | 0 | 6 |
| mgmt | 0 | 2 | 0 | 2 |
| runtime | 10 | 0 | 0 | 10 |
| insight | 0 | 1 | 0 | 1 |

## 2. 处置分类进度

| 分类 | 表数 | 剩余跨域引用 |
| --- | ---: | ---: |
| D | 5 | 10 |
| C1 | 4 | 8 |
| C2 | 9 | 12 |
| B | 1 | 1 |

## 3. 全量明细（按剩余跨域引用降序）

| 表 | 归属 | 跨域引用 | 消费方 | 处置 | 写通道 | 文件数 |
| --- | --- | ---: | --- | --- | --- | ---: |
| `events` | runtime | 3 | api 3 | C1 归属重定（写路径挪进 owner 域） | — | 20 |
| `grape_t_credential_usage_log` | runtime | 3 | kernel 3 | D 模块归位（kernel 写/读域表） | — | 2 |
| `unified_user_mapping` | mgmt | 3 | kernel 3 | D 模块归位（kernel 写/读域表） | — | 7 |
| `agent_runtimes` | runtime | 2 | api 2 | C1 归属重定（写路径挪进 owner 域） | — | 23 |
| `devops_pipeline_scene` | mgmt | 2 | runtime 2 | C1 归属重定（写路径挪进 owner 域） | — | 9 |
| `external_identities` | mgmt | 2 | runtime 2 | C2 outbox 跨域写 | W2 | 6 |
| `managed_repos` | mgmt | 2 | runtime 2 | D 模块归位（kernel 写/读域表） | — | 17 |
| `takeover_sessions` | runtime | 2 | api 1 / mgmt 1 | C2 outbox 跨域写 | W1 | 4 |
| `user_feedback` | mgmt | 2 | api 2 | C2 outbox 跨域写 | W3 | 5 |
| `agent_tasks` | runtime | 1 | mgmt 1 | C2 outbox 跨域写 | W1 | 26 |
| `api_latency_log` | insight | 1 | api 1 | C2 outbox 跨域写 | W3 | 4 |
| `distributed_locks` | runtime | 1 | insight 1 | D 模块归位（kernel 写/读域表） | — | 3 |
| `grape_accounts` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 12 |
| `grape_t_channel_installation` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 5 |
| `grape_t_merge_gate_log` | insight | 1 | api 1 | C2 outbox 跨域写 | W3 | 2 |
| `grape_t_repo_credentials` | insight | 1 | contracts 1 | C1 归属重定（写路径挪进 owner 域） | — | 3 |
| `issue` | runtime | 1 | api 1 | D 模块归位（kernel 写/读域表） | — | 16 |
| `skill_category` | mgmt | 1 | runtime 1 | B API 聚合端点 | — | 4 |
| `user_accounts` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 5 |

## 4. 用法

```bash
python3 scripts/gen_domain_dependency_board.py          # 刷新看板
python3 scripts/gen_domain_dependency_board.py --check  # 过期即失败（CI）
```

验收口径：G4「代码零外域表引用」= 本表 `跨域引用` 列全为 0 且 `docs/weekly-11/foreign-refs-allowlist.csv` 为空。
