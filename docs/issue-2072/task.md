# Task: Issue #2072 — issue_number 类型归一（int）

| # | 任务 | 状态 | 提交 |
|---|------|------|------|
| 1 | 新增类型单测（mine/列表/任务卡/详情），注入文本型编号行复现 str 泄漏 → 红 | 已完成 | test(#2072) |
| 2 | `_query_tasks` 响应构造处 `_native_issue_number(r[3])` 归一 | 已完成 | fix(#2072) |
| 3 | `get_issue_status`（任务卡）`_native_issue_number(row[3])` 归一 | 已完成 | fix(#2072) |
| 4 | 详情/PR 伪卡/`number` 别名巡检确认一致 | 已完成 | （随 2） |
| 5 | 全量相关测试回归（tests/ 跑本单用例 + issues 相关既有测试） | 已完成 | — |
| 6 | 自审查（5 轴 + pre-pr 检查）→ 建 PR（base=arch-weekly-11）→ 评论 → A2A QA | 已完成 | — |
| 7 | 非数字 ident 面（gitee `IKSQ4O`）：列表/任务卡原样透传不 500 + 单元/HTTP 两条回归用例 | 已完成 | fix(#2072) |
