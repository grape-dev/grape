# Task: openspec-pr-reviewer 任务拆解清单

- [x] T1 查重：skills list + .grape/skills/qa 无同名 → 通过
- [x] T2 读取三件套（grape-cli-reference / spec-reference / command-matrix）
- [x] T3 下载并通读规范文档附件（ef25dd8c894541aa98070cc12cee94cc.md，§0-§7 全文）
- [x] T4 生成 SKILL.md（frontmatter/安全硬约束/Prerequisites/env 表/runbook 引用/操作边界/收尾）——验收：行数 ≤130、tools 含 bash、含 CLI 优先+任务上下文先行
- [x] T5 生成 references/openspec-review-runbook.md——验收：Step 0 必须最先、编号步骤+命令+错误排查+明确禁止、≤250 行
- [x] T6 生成 references/openspec-checklist.md——验收：覆盖 §1.1 全部 7 类敏感类型、Conventional Commits 基线、三态判定、报告模板、≤250 行
- [x] T7 生成 references/grape-cli-reference.md——验收：只列用到的命令、含命令遮蔽/无 api 逃生舱/不存在能力声明
- [x] T8 生成 .grape/docs/openspec-pr-reviewer/（spec/plan/task）
- [x] T9 质量自检（spec §8 清单逐项）
- [x] T10 建分支提交 + 创建 PR（base=$GRAPE_WORK_BRANCH=weekly-06-lyz）+ Issue 评论告知
- [x] T11 A2A 交棒 qa agent 审查 + tasks complete 收尾

## 验证方式

- `wc -l` 校验行数约束；grep 校验关键规范项（Prerequisites/环境变量表/a2a agent 名/base 用 WORK_BRANCH）。
- 后续由 QA agent 对 PR 做技能规范合规性审查闭环验证。
