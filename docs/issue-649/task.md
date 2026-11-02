# Task Breakdown: Issue #649

| # | 任务 | 状态 |
|---|------|------|
| 1 | 定位 create 链路 assignee 默认值与 403 包装点 | ✅ |
| 2 | 移除 assignee 默认填创建者，`_creator_login` 解耦内部创建人字段 | ✅ |
| 3 | 新增 `_gitcode_assignee_member_check` 三态成员预校验 + 前置阻断 | ✅ |
| 4 | 上游 403 归类「非成员」语义，附 trace_id + upstream_detail | ✅ |
| 5 | 新增 #649 测试（4 验收场景 + 空值跳过预校验） | ✅ |
| 6 | 更新 #523 回归测试适配新语义 | ✅ |
| 7 | 回归验证（523/404/415/onboarding/tasks/issues_api 等） | ✅ |
| 8 | 提交 + 敏感增量扫描 + 建 PR + A2A 交办 QA | 进行中 |
