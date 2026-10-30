# Task: Issue #521 拆解

| # | 任务 | 状态 |
|---|------|------|
| 1 | docs/issue-521 spec/plan/task 文档 | done |
| 2 | events.py：新增 queued 重派复用分支（原子 UPDATE 守卫） | done |
| 3 | events.py：local_only 分支 queued 复用 | done |
| 4 | events.py：回执 `_effective_task_id` 一致性 | done |
| 5 | 回归测试 tests/test_issue_521_redispatch_queued.py（UT-1~UT-5） | done |
| 6 | 既有测试回归（redispatch/253/453 相关） | done |
| 7 | 自审查 + pre-pr 检查 + PR | done |
