# Task: Issue #514 — 任务拆解

- [x] T1 红：新增原子测试 `webhook_entry_rejects_non_post_methods`（tests/atomic/），基线复现 GET=200 失败
- [x] T2 绿：webhook.py 添加非 POST 方法 api_route 处理器 → 405 JSON
- [x] T3 回归：webhook 相关测试套基线对照，无新增失败
- [x] T4 自审查（5 轴 + 敏感增量扫描）→ 提交推送
- [x] T5 建 PR（base=weekly-09）+ Issue 评论 + A2A 通知 QA
