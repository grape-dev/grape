# Issue #1412 — 方案设计（Plan）

## 改动点

| 位置 | 改动 |
|------|------|
| `src/api/db_setup.py:3030` | `version="202611030004"` → `"202611070001"`，name 不变 |
| `_applied_versions()` | `SELECT version, name, checksum` → `{version: (name, checksum)}` |
| `_run_migrations_with_conn` checksum 循环前 | 新增「同号不同名」守卫：ERROR + 跳过 |
| `register_migration` docstring | 自查清单补第 5 条 renumber 人工核对 |
| `tests/atomic/test_issue_536_migrations.py` | 新增同号不同名运行时用例 |
| `tests/atomic/test_migration_track_guard.py` | docstring 补运行时撞号守卫说明 |

## 关键决策

- 版本号选择 `202611070001`：0005/0006 已被 #868 占用，0007 与 0004 同段易再撞。
- 同号不同名处理：ERROR + 跳过（fail-safe，与既有语义一致，不阻断启动）。
- 守卫实现位置：checksum 漂移循环之前，复用 `_run_migrations_with_conn` 内部闭包数据。

## 风险

- 目标库 `202611070001` 已被其它分支占用（本仓不可见）→ 部署前人工核对；
  即便占用，新 ERROR 会直接报出同号不同名。
- `_applied_versions` 返回结构变更 → 调用点仅在该闭包内 4 处，改完跑 tests/atomic。