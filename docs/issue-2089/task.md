# Tasks: Issue #2089

- [ ] T1 红：新增 `tests/js/test_issue_2089_help_drawer.js` 契约测试（应红）
- [ ] T2 绿：index.html 抽屉头部按钮组（全屏/还原、新标签页）
- [ ] T3 绿：app.css `.help-drawer.fullscreen` 全屏态 + 按钮组样式
- [ ] T4 绿：app.js `toggleHelpFullscreen`/`openHelpNewTab` + 导出
- [ ] T5 绿：i18n.js zh/en 新 key（fullscreen/exit_fullscreen/open_new_tab）
- [ ] T6 红：`tests/test_help_page_generation.py` TestIssue2089（应红）
- [ ] T7 绿：help_page_gen.py `_CSS` 自适应 + 横向滚动；重跑生成器更新 help.html
- [ ] T8 验证：node 测试 + pytest 全绿；一次性静态服务 curl 校验
- [ ] T9 自审查（5 轴 + pre-pr 增量检查）
- [ ] T10 建 PR（base=local-liufei）+ Issue 评论 + A2A QA + tasks complete
