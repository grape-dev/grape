# Task: Issue #2132 任务拆解（旧布局生产 hotfix twin）

- [x] A1 `runtime_history` 时间谓词下推（方言类错误回退 + 行数上限）
- [x] A2 `db_lock` 收窄到取数、聚合锁外（数据层 + 路由）
- [x] A3 观测页设备分区轮询按页面可见性启停
- [x] B1 知识对象存储读取异步化（`asyncio.to_thread` / 建单前置预热 + 时延预算）
- [x] B2 知识条目上限 + 并发上限（按仓可配置，截断可观测；agent + member 双路径）
- [x] B3 repo 级进程内读缓存（hash 版本 + TTL + 容量上限）
- [x] 运维可观测：`/health/realtime` 暴露 `knowledge_cache` 统计
- [x] CI 门禁：`windows-smoke.yml` 接入本 issue 回归测试
- [x] 新增测试：谓词/锁外聚合/IO 线程化/上限截断/缓存 TTL/前端可见性
- [x] 自审查 + 敏感信息增量扫描
- [x] 建 PR（base `the legacy-layout branch`）+ Issue 评论 + A2A 通知 QA

## 与 PR #1271 的等价性映射

| 项 | PR #1271（新布局 the reviewed ljt line） | 本 twin（旧布局 the legacy-layout branch） |
|---|---|---|
| A1/A2 数据层 | `src/domains/insight/observability/rollup.py` | `src/observability/rollup.py` |
| A2 路由 | `src/domains/insight/routes/observability.py` | `src/api/routes/observability.py` |
| A3 前端 | `src/api/static/js/pages/observability.js` | 同路径 |
| B1/B2/B3 | `src/api/knowledge.py` | `src/api/knowledge.py` |
| B1 入口/防御 | `src/domains/runtime/engine/service.py` | `src/runtime/service.py` |
| 建单预热 | `src/api/issues_center.py` | `src/api/issues_center.py` |
| 运维暴露 | `src/api/health_endpoints.py` + `server_common.py` | `src/api/health_endpoints.py`（旧布局无 server_common） |
| 测试 | `tests/insight/test_issue_2132_obs_history.py` 等 | `tests/test_issue_2132_obs_history.py`（旧布局测试树扁平） |
| CI | `windows-smoke.yml` | 同文件（本基线） |

## 验收对照

| 验收项 | 落地证据 |
|---|---|
| A1 命中索引 | `tests/test_issue_2132_obs_history.py::test_history_predicate_hits_composite_index` |
| A1 p95 | 现网压测指标（需部署后观测），代码侧已消除全表回拉 |
| A2 锁外聚合 | `test_history_aggregation_runs_outside_lock`（持锁探针在行迭代期断言 depth==0） |
| A3 可见性 | `tests/js/test_issue_2132_obs_visibility.js`（4 项） |
| B1 IO 线程化 | `test_collect_knowledge_files_async_runs_off_loop` / `test_prefetch_reads_obs_off_loop_with_bounded_concurrency` |
| B2 上限截断 | `test_prefetch_truncation_is_observable` / `test_agent_knowledge_files_applies_sql_limit` / `test_member_files_pi_applies_sql_limit` |
| B3 缓存 | `test_read_with_cache_ttl_and_lru` |
| 预热预算 | `test_warm_cache_respects_time_budget` / `test_warm_budget_rejects_invalid_env` |
| 瞬时异常不回退 | `test_history_does_not_full_scan_on_transient_error` |
| CI 门禁 | `windows-smoke.yml`「Observability + knowledge perf regression tests (issue #2132)」 |
