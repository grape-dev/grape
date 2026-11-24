# Task: Issue #3738

| # | 任务 | 提交 |
|---|------|------|
| 1 | 红：新增 `tests/runtime/test_3738_meta_upsert_idempotent.py`（同值幂等 + 并发首写 + rowcount 护栏），对当前实现变红 | `test(#3738)` |
| 2 | 绿：`set_meta_value_any()` 改为 `_any_cursor(write=True)` 内单语句 `meta_upsert`（`ON CONFLICT(key) DO UPDATE`，MySQL 方言翻译为 `ON DUPLICATE KEY UPDATE`） | `fix(#3738)` |
| 3 | 回归：sqlite 实跑用例 + 批量版 `set_meta_values_any` 行为不变断言，全量相关测试通过 | 同上（用例并入 1） |
| 4 | 合并基线 `arch-weekly-11-ljt`（fe1d51a33）：冲突按「目标分支已独立修复同缺陷（ac92dc382 单语句 upsert）」取目标实现，回归用例改锁行为契约 | `merge(#3738)` |
| 5 | 复审修复：再次同步基线 `arch-weekly-11-ljt`（819f68e）恢复 `mergeable`；spec/task 口径改为单语句 `meta_upsert` 并更新行号；测试文件末行补换行 | `chore(merge)` / `docs(#3738)` |
