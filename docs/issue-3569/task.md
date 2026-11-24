# Issue #3569 任务拆解

- [x] T1 后端 `src/api/oauth_token.py` 三处 401 detail 补 `provider`
- [x] T2 前端 `api.js` 新增 `_redirectToOAuth(provider)` + provider 归一化（缺省回退 GitCode）
- [x] T3 两个 401 分支改用 `_redirectToOAuth(detail.provider)`，删除 `_redirectToGitcodeAuth`
- [x] T4 sessionStorage 防循环标记（provider + 时间戳，60 秒窗口，跨整页重载生效）
- [x] T5 命中标记后渲染「<平台> 授权已过期，请到 账号设置 → 第三方绑定 重新授权」+ `#account/profile` 入口
- [x] T6 `bind_success=<provider>` 页面加载时清除该平台标记（重新授权后正向回归）
- [x] T7 400 / 429 / 其他异常只提示，不新增整页导航
- [x] T8 i18n zh/en 各补 `error.reauth_*` 文案（集合一致）
- [x] T9 新增 node 回归测试 `tests/js/test_issue_3569_reauth_loop_guard.js`
- [x] T10 新增 pytest `tests/api/test_issue_3569_reauth_provider.py`
- [x] T11 自审查（5 轴 + 增量敏感扫描）+ 跑通 JS/pytest
- [ ] T12 建 PR（base `arch-weekly-11-ljt`）+ Issue 评论 + A2A QA
