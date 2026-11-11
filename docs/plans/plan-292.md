# Plan: Issue #292

1. 定位与数据源确认：dashboard.js 两处编辑态技能 `<select>`；后端 `/scenes/skills/{agent_type}` 已返回 scope。
2. 新增共享 helper：`_skillFilterByQuery` / `_skillGroupByScope`（纯函数，可测）+ `_openSkillDropdown` / `_closeSkillDropdown` / `_skillPickCache`。
3. 替换 `_renderEditFlowCards` 与 `renderAgentCards` 的技能下拉为分组下拉按钮。
4. `_saveSceneEdit` 显示名映射改为 cache 展平 + option 兜底。
5. 新增 `tests/js/test_issue_292_skill_dropdown.js`（分组归属/顺序/空组、过滤大小写与描述匹配、清空恢复、空态）。
6. 自审查（5 轴）→ 原子提交 → PR base=weekly-06-lyz → Issue 评论 → A2A QA。
