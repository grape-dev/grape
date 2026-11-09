# Issue #508 任务拆解（Task）

> 每步一原子提交（TDD 红/绿拆分，见 plan.md）。

| # | 任务 | 状态 |
|---|------|------|
| 1 | 失败测试 test_chat_config_backend_validation.py（对象形态 400 断言 + 全分支 + 回归） | 待做 → 提交 test(#508) |
| 2 | chat.py update_chat_config backend 显式校验（非 str 400）+ skill 防御 | 待做 → 提交 feat(#508) |
| 3 | 回归验证：新测试全绿 + test_chat_config_im_preserve / row_selection / engine_selection / test_chat_routes | 待做 |
| 4 | 自审查 5 轴 + pre-pr 增量检查（黑名单拦截 + 敏感扫描） | 待做 |
| 5 | 建 PR（base=weekly-09）→ Issue 评论 → A2A QA → tasks complete | 待做 |
