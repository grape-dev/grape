# Task: Issue #648

- [x] T1 issues_center：`_attachments_markdown_block` + 建单路径复用 + `sync_issue_attachment_comment`
- [x] T2 routes/issues：`_issue_attachments` + detail/list 响应 attachments 字段
- [x] T3 routes/issues：上传端点触发 GitCode「## 附件」同步评论
- [x] T4 前端 issues.js：附件区内联渲染（图片预览/下载/空态/回退）
- [x] T5 测试：tests/test_issue_648_attachments_display.py + tests/js/test_issue_648_attachments_inline.js
- [x] T6 回归：pytest 相关用例 + node JS 用例
