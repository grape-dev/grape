# Task Breakdown: Issue #357

## P1（结构性修复）
- [x] P1-A: src/mysql_compat.py — DBUtils PooledDB 连接池 (commit ce191c22e)
- [x] P1-B: src/api/pipeline_config.py + issues_center.py — 场景缓存 + 循环外批量 (commit bc0e2f75a)
- [x] P1-C: src/api/routes/scenes.py — experience 超时保护 (commit 3efa37116)
- [x] P1-D: src/runtime/task_lock.py + 4 个 worker — 分布式锁 (commit 3efa37116)
- [x] P1-E: src/api/routes/issues.py — 响应瘦身 (commit 3efa37116)

## P2（两周内）
- [x] P2-A: src/user/db.py — 分批 DELETE (commit f29700112)
- [x] P2-B: src/api/db_setup.py — events 复合索引 (commit f29700112)
- [x] P2-C: src/runtime/table_maintenance.py + main.py — 大表维护 (commit f29700112)
- [x] P2-D: src/api/routes/runtime.py — nginx X-Accel-Redirect (commit f29700112)
- [x] P2-E: issues.js — 轮询 30s (commit f29700112)

## 测试
- [x] tests/test_issue357_health.py — 14 个测试 (commit 89714d432)
- [x] tests/test_mysql_compat.py — 适配连接池 (commit ce191c22e)

## 验收对照
| Spec V 用例 | 实现 | 测试 |
|-------------|------|------|
| V2-3 N+1 修复 | P1-B 缓存 + 批量 | test_cache_put_and_get, test_cache_expiry |
| V2-5 experience 超时 | P1-C 10s/30s timeout | (route-level, mock) |
| V2-6/V3-1 连接池化 | P1-A PooledDB | test_pool_disabled_falls_back, test_pool_env_var |
| V3-2 agent_tasks 治理 | P2-C table_maintenance | test_cleanup_old_agent_tasks_fields |
| V3-3 events 索引 | P2-B 复合索引 | (DB-level, EXPLAIN) |
| V3-4 死锁修复 | P2-A 分批 DELETE | (DB-level, batch loop) |
| V2-2 定时任务不重复 | P1-D 分布式锁 | test_lock_acquired_runs_body, test_lock_not_acquired_skips_body |
