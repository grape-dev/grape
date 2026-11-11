# Plan: Issue #286

1. JS：chat.js 7 处渲染点互换 `msg l` / `msg r`（用户→r，agent→l）
2. CSS：app.css 气泡样式互换归属 + #233 链接规则迁移到 `.msg.r a`；apple-theme.css 同步
3. 测试：新增 test_issue_286_chat_bubble_layout.js；更新 test_issue_233_link_contrast.js 选择器
4. 验证：node tests/js/ 全量 + pytest 全量
5. 提交 feat/issue-286，建 PR base=weekly-06-lyz，A2A QA
