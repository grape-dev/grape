# Issue #3811 任务拆解

- [x] 红：新增 sqlite 双库 fixture 单测（域路由 / 回落告警 / fail-open 计数 / 409 回归）。
- [x] 绿：`src/kernel/db_registry.py` 补 `warn_if_cross_domain_fallback(domain)`（对齐同族分支）。
- [x] 绿：`release_guard._read_task_issue_id()` 改按域取连接，回落告警，异常路径计数。
- [x] 回归：`tests/runtime/test_issue_2225_release_guard.py` 全绿。
- [x] 自审查 + 敏感增量扫描 → 建 PR → 评论 → A2A QA。
- [x] CI 门禁：`linux-backend` 纳入 #3811 域路由用例。
- [x] 复审回归：`tests/kernel/test_db_registry_cross_domain_warning.py` fixture 复位进程级 `_warned_cross_domains`，并将该文件纳入门禁。