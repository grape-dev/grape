# Plan: Issue #463

## 测试 seam 声明
- P0/P1 在 `cleanup_old_agent_tasks_fields()` / `delete_old_agent_tasks()` 公开函数 seam 上测试（复用 tests/test_issue357_health.py 先例：直接向测试库插旧行→调用→断言字段/行状态）。
- P2 在 `enqueue_task()` seam 上测试：mock storage 返回 URL，断言内联字段仅保留无 URL 键。
- P3 在 `seed_rules()` / `_platform_metrics()` seam 上测试（复用 tests/test_obs_alerts.py 先例）。

## 实现切片（TDD 红/绿分提交）
1. **P0**：`_AGENT_TASKS_RETENTION_DAYS 30→7`；`_AGENT_TASKS_CLEANUP_FIELDS`（10 字段）驱动 SELECT/UPDATE。红：test_cleanup 扩展断言新字段置空+7 天边界。绿：实现。
2. **P1**：`delete_old_agent_tasks()`（90 天终态、_BATCH_SIZE 分批、连带 task_messages）；`src/api/main.py` 维护 worker 挂载调用。红/绿同上。
3. **P2**：`enqueue_task` 中 `_file_urls` 命中的键内联内容置 `""`。红：enqueue 测试断言单写。绿：实现。
4. **P3**：`_platform_metrics()` 增 `agent_tasks_rows`/`agent_tasks_mb`（MySQL: information_schema；SQLite: page_count×page_size / COUNT）；`SEED_RULES` 增两条平台级规则（rows>500k、mb>2048）。红/绿。

## 风险与对策
- 误删排障数据：仅删 90 天前终态行；行审计由 event.json/events 表承担（Issue Spec 决策）。
- DELETE 锁表：_BATCH_SIZE=500 分批 + 6h 周期分布式锁。
- OBS URL 失效：worker 三级降级（URL→content→storage_key），content 置空后仍可 storage_key 直连兜底。
