# Task: Issue #623

- [x] 定位 422 根因：update 端点 `comment_id: int` 校验拒绝十六进制串
- [x] 实现 `_resolve_pull_comment_note_id` 解析层（数字直传 / hex 映射 / 非法 422）
- [x] update 端点接入解析层，OpenAPI description 补契约说明
- [x] help/api/10-git-repos.md 补 `--comment_id` 契约文档
- [x] 新增 5 个回归测试（三类入参全覆盖）并跑通
- [x] 自审查（5 轴 + pre-pr 增量检查）→ 建分支提交 → PR → 通知 QA
