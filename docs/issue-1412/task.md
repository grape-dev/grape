# Issue #1412 — 任务拆解（Task）

- [x] T1 renumber issue865 迁移 version 至 202611070001
- [x] T2 `_applied_versions()` 增读 name，返回 `{version: (name, checksum)}`
- [x] T3 checksum 漂移循环前新增「同号不同名」ERROR 守卫
- [x] T4 `register_migration` docstring 自查清单补第 5 条
- [x] T5 tests/atomic/test_issue_536_migrations.py 新增撞号运行时用例
- [x] T6 tests/atomic/test_migration_track_guard.py docstring 补充说明
- [x] T7 `pytest tests/atomic/` 全绿