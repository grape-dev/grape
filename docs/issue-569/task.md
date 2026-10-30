# Task: Issue #569

- [x] 定位 vod-runbook 实际资产（grape-skills 仓库 skills/vod/feedback-loop/vod-cli/references/vod-runbook.md）
- [x] grape 仓：`issues detail` 出参增量字段 `issue_id`（内部卡片 id）+ 注释与帮助文档说明
- [x] grape 仓：新增测试 `test_detail_issue_id_is_internal_card_id`，全量跑 tests/test_issues_api.py（基线既有 2 个失败与本次无关）
- [ ] grape-skills 仓：修正 vod-runbook Step 4b（删 `.id` 兜底、`$GRAPE_ISSUE_ID` 唯一来源、两套 id 不可混用说明）+ SKILL.md 环境变量表
- [ ] 两仓分别提交、建 PR（grape base=weekly-08-mys；grape-skills base=main）
- [ ] Issue 评论告知 + A2A 通知 QA + tasks complete 收尾
