# Task: Issue #1495

- [ ] T1 红：tests/test_issue_1495_task_activity_format.py（recover_orphans × {空格格式, 无微秒 ISO} + claim 兜底 × 同矩阵 + 不可解析行保守取舍 + 真死任务仍回收）
- [ ] T2 绿：src/runtime/task_activity.py（CLAIM=600s / RECOVER_ORPHANS=1200s 单点定义 + has_recent_activity）
- [ ] T3 绿：src/api/routes/runtime.py recover_orphans 改走 helper
- [ ] T4 绿：src/runtime/service.py claim 兜底改走 helper + 「最近 60s」注释修正为 600s
- [ ] T5 回归：新增用例 + test_recover_orphans_*.py + test_runtime_pool.py + 相关 claim 回归全绿
- [ ] T6 自审查（5 轴 + pre-pr 增量检查）
- [ ] T7 push + 建 PR（base=weekly-10）+ Issue 评论 + A2A QA
