# Task Breakdown: Issue #827

| # | 任务 | 文件 | 提交 |
|---|------|------|------|
| 1 | 红：ensure_issue_task assignee 变更未注册时 user_id 不清零 | tests/runtime/test_issue_827_user_id_preserve.py | test(#827) |
| 2 | 绿：保留 user_id + 写入 created_by | src/domains/runtime/issues_center.py | feat(#827) |
| 3 | 红：my_issues user_id=0 但 created_by 命中时可见 | tests/runtime/test_issue_827_mine_filter.py | test(#827) |
| 4 | 绿：扩展 mine 过滤口径 | src/domains/runtime/routes/issues.py | feat(#827) |
| 5 | 红：回填脚本 created_by 回退 | tests/runtime/test_issue_827_backfill.py | test(#827) |
| 6 | 绿：增强回填脚本 | scripts/backfill_issue_user_id.py | feat(#827) |
| 7 | 自审查 + 建 PR + A2A | — | — |
