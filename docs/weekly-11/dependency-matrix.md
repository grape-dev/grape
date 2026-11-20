# 跨域依赖矩阵看板（P0-4 自动生成）

> 生成时间：2026-11-19 11:37　口径：`scripts/foreign_table_refs.py`（只算 api/kernel/domains 运行代码）
> 汇总：跨域表 **20** 张；跨域引用 **62** 处（23 个「表×消费域」对）；含归属域在内的 SQL 引用 1208 处

## 1. 域 × 域 跨域引用热力

| 消费方 ↓ / 归属域 → | mgmt | runtime | insight | 合计 |
| --- | ---: | ---: | ---: | ---: |
| api | 13 | 35 | 0 | 48 |
| kernel | 4 | 0 | 0 | 4 |
| runtime | 4 | 0 | 0 | 4 |

## 2. 处置分类进度

| 分类 | 表数 | 剩余跨域引用 |
| --- | ---: | ---: |
| D | 3 | 9 |
| C1 | 5 | 21 |
| C2 | 3 | 7 |
| B | 1 | 1 |
| 未分类 | 8 | 24 |

## 3. 全量明细（按剩余跨域引用降序）

| 表 | 归属 | 跨域引用 | 消费方 | 处置 | 写通道 | 文件数 |
| --- | --- | ---: | --- | --- | --- | ---: |
| `agent_runtimes` | runtime | 10 | api 10 | C1 归属重定（写路径挪进 owner 域） | — | 26 |
| `backend_agents` | runtime | 6 | api 6 |  | — | 1 |
| `meta` | runtime | 6 | api 5 / platform 1 | D 模块归位（kernel 写/读域表） | — | 5 |
| `runtime_credentials` | runtime | 6 | api 6 | C1 归属重定（写路径挪进 owner 域） | — | 10 |
| `grape_t_repo_platforms` | mgmt | 5 | api 5 |  | — | 4 |
| `agent_tasks` | runtime | 4 | api 4 | C2 outbox 跨域写 | W1 | 30 |
| `gsb_provider_route` | mgmt | 4 | kernel 4 |  | — | 1 |
| `grape_t_engine_sandbox_whitelist` | mgmt | 3 | runtime 2 / api 1 |  | — | 5 |
| `devops_pipeline_scene` | mgmt | 2 | api 2 | C1 归属重定（写路径挪进 owner 域） | — | 9 |
| `external_identities` | mgmt | 2 | api 1 / platform 1 | C2 outbox 跨域写 | W2 | 8 |
| `grape_t_category_dict` | mgmt | 2 | api 2 |  | — | 3 |
| `grape_t_external_identity_tombstones` | mgmt | 2 | api 2 |  | — | 1 |
| `grape_t_repo_credentials` | insight | 2 | platform 2 | C1 归属重定（写路径挪进 owner 域） | — | 3 |
| `task_messages` | runtime | 2 | api 2 | D 模块归位（kernel 写/读域表） | — | 12 |
| `chat_conversations` | runtime | 1 | api 1 | D 模块归位（kernel 写/读域表） | — | 6 |
| `engine_capabilities` | runtime | 1 | api 1 | C1 归属重定（写路径挪进 owner 域） | — | 8 |
| `grape_t_engine_template_binding` | mgmt | 1 | runtime 1 |  | — | 3 |
| `grape_t_user_repo_platform_link` | mgmt | 1 | platform 1 |  | — | 2 |
| `user_accounts` | mgmt | 1 | runtime 1 | C2 outbox 跨域写 | W2 | 6 |
| `user_credentials` | mgmt | 1 | platform 1 | B API 聚合端点 | — | 7 |

## 4. 用法

```bash
python3 scripts/gen_domain_dependency_board.py          # 刷新看板
python3 scripts/gen_domain_dependency_board.py --check  # 过期即失败（CI）
```

验收口径：G4「代码零外域表引用」= 本表 `跨域引用` 列全为 0 且 `docs/weekly-11/foreign-refs-allowlist.csv` 为空。
