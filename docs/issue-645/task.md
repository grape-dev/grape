# Task: Issue #645

- [x] T1 编写回归测试 `tests/js/test_issue_645_skill_update_fallback.js`（RED）
- [x] T2 `skills.js` 6 处 I18N.t 改第三参 fallback（行 506/876/877/888/916/920）
- [x] T3 `showSkillUpdate` 传 `s.display_name || s.name`（含单引号转义）
- [x] T4 `zh/en skills.json` 补 6 个更新相关键（GREEN：测试全绿）
- [x] T5 `node --check skills.js`、locale JSON 解析、相关既有回归（issue-316 skills.json 测试）通过
- [x] T6 自审查（5 轴 + pre-pr 增量检查）→ 提交 → push → 建 PR（base=weekly-08-mys）→ Issue 评论 → A2A QA → tasks complete
