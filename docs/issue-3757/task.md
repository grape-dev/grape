# Issue #3757 — 任务拆解与执行记录

| # | 任务 | 状态 | 产物 |
|---|------|------|------|
| 1 | 复现：真实 app 的重复注册计数与 operationId 告警（HEAD 基线） | 完成 | mgmt：545 个重复 id / 544 条重复 (method,path)，契约面 983 ops 折叠到 546 |
| 2 | 定位三条重复源（第二套注册循环 / runtime 视图交集 / 共享装配不幂等） | 完成 | docs/issue-3757/plan.md |
| 3 | 合并 mgmt 路由注册为单一清单（并集，含 observability + runtime_view） | 完成 | `src/api/grape_server.py` |
| 4 | runtime GET 子集与 view_router 交集去重（`exclude_ops`） | 完成 | `src/api/server_common.py`、`grape_server.py` |
| 5 | 清理尾块残留（重复 import / 重复模块 docstring / 第二套循环） | 完成 | `src/api/grape_server.py` |
| 6 | 启动自检 `assert_route_registry_unique`（fail-fast） | 完成 | `src/api/server_common.py`、`grape_server.py` |
| 7 | 共享装配幂等化（SPA 页面路由 / health 端点 / main `/metrics`） | 完成 | `src/api/spa.py`、`health_endpoints.py`、`main.py` |
| 8 | CI 门禁：展开 `_IncludedRouter` + 覆盖五个进程入口 + fail-closed | 完成 | `scripts/check-openapi-endpoints.py` |
| 9 | 测试：唯一性正例 / 注入重复反例 / 端点零丢失 / 门禁用例 | 完成 | `tests/api/test_issue_3757_route_registry_unique.py`（19 passed） |
| 10 | 回归：既有用例失败集合与基线比对 | 完成 | 30 failed / 125 passed，与 HEAD 基线逐条一致 |
| 11 | 文档：spec / plan / task | 完成 | `docs/issue-3757/*.md` |
| 12 | 建 PR（base=`arch-weekly-11-ljt`）+ Issue 评论 + A2A 通知 QA | 进行中 | — |

## 关联仓库

`$GRAPE_LINKED_REPOS` 配置了 `grape-dev/grape-admin`，但本次缺陷在 mgmt 进程的
FastAPI 路由装配层，`grape-admin`（Java 后端 + 前端）不含 FastAPI 路由注册
（`grep include_router` 零命中），本需求无需改动该仓，故未在其上开发/建 PR。

## 遗留（独立单跟进，不在本单处理）

1. `/openapi.json` 恒 500（`contracts/deprecated_endpoints.json` 未落盘）→ #3756。
2. 重复**中间件**装配（CORS / metrics / 异常处理器在 `build_app` 与入口各装一次）：
   不产生重复路由，不改契约唯一性；`grape_server.py` 中重复的 `metrics_middleware(app)`
   与双 CORS 属同一份尾块残留，建议随中间件装配统一收口时清理。