# Task: Issue #2072 — issue_number 类型归一（int）

| # | 任务 | 状态 | 提交 |
|---|------|------|------|
| 1 | 新增类型单测（mine/列表/任务卡/详情），注入文本型编号行复现 str 泄漏 → 红 | 待做 | test(#2072) |
| 2 | `_query_tasks` 响应构造处 `int(r[3])` 归一 | 待做 | fix(#2072) |
| 3 | `get_issue_status`（任务卡）`int(row[3])` 归一 | 待做 | fix(#2072) |
| 4 | 详情/PR 伪卡/`number` 别名巡检确认一致 | 待做 | （随 2） |
| 5 | 全量相关测试回归（tests/ 跑本单用例 + issues 相关既有测试） | 待做 | — |
| 6 | 自审查（5 轴 + pre-pr 检查）→ 建 PR（base=local-liufei）→ 评论 → A2A QA | 待做 | — |
