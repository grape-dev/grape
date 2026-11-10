# Issue #1588 任务拆解

| # | 任务 | 批次 | 提交 |
|---|------|------|------|
| 1 | 删除 A 批 6 函数（0 引用）+ engine_select 过时 docstring | A+E | M1 `refactor(#1588)` |
| 2 | 删除 B 批 12 函数 + 装饰器/字段表/docstring 映射/孤儿 _hash_password | B | M2 `refactor(#1588)` |
| 3 | test_user_db.py 等价改写为 grape_account_db 用例 | B | M2 |
| 4 | test_auth_routes / test_issue_367 / test_tasks / ui_test_playwright 夹具迁移 | B | M2 |
| 5 | 验收 grep ×2 + pytest 全量 | — | — |
| 6 | 自审查（5 轴 + 前置增量检查）→ 建 PR → Issue 评论 → A2A QA | — | — |

C 批（init_db 建表与启动迁移段）单独立项，D 批保留勿动。
