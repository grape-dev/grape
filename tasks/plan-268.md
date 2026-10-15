# Plan: Issue #268

## 任务拆解

| # | 任务 | 文件 | 依赖 |
|---|------|------|------|
| 1 | 修复 _maybe_cleanup 后端分支适配 | src/api/routes/latency.py | - |
| 2 | 修复 _cleanup_old_latency_logs 同一 bug（启动时执行） | src/api/db_setup.py | - |
| 3 | 新增测试：SQLite 路径集成 + MySQL SQL 方言断言 | tests/test_issue_268_latency_cleanup.py | #1,#2 |
| 4 | 运行测试 + lint | - | #3 |

## 风险
- `get_db_backend()` 在测试环境被 conftest 强制 sqlite → MySQL 分支需通过 monkeypatch 注入测试
- `_last_cleanup` 模块级变量节流 → 测试前需重置为 0
