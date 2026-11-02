# Task: Issue #614

1. [x] 基线确认：feat/issue-614 重置到 origin/weekly-09（Spec 声明的目标合入分支）。
2. [x] 代码线索考察：app.js showLogin/logout/closeModal/closeConfirm、runtimes.js 凭据面板（addDeviceCredArea 等）、同类敏感弹窗（credModal_*、sreAddDeviceOverlay）。
3. [ ] 红：tests/js/test_issue_614_session_logout_cred_clear.js（会话超时登出 → 敏感弹窗关闭/秘钥清空断言）。
4. [ ] 绿：app.js 新增 `_clearSensitiveModals()` + `showLogin()` 调用 + 导出。
5. [ ] 回归：现有 tests/js 相关用例。
6. [ ] 自审查（5 轴 + pre-pr 增量检查）→ 建 PR（base=weekly-09）→ Issue 评论 → A2A QA。
