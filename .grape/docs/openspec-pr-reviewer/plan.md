# Plan: openspec-pr-reviewer 开发方案

## 实现顺序

1. 查重：`grape skills list --q openspec` + `.grape/skills/qa/` 目录检查 → 确认无同名技能。
2. 生成 `.grape/skills/qa/openspec-pr-reviewer/SKILL.md`：frontmatter（name/version/description/tools=[bash]）+ 安全硬约束（CLI 优先/任务上下文先行/纯审查零写库/token 不落盘/报告不泄密）+ Prerequisites + 环境变量表 + 定位 + 评论规范 + runbook 引用式处理流程 + 操作边界 + 硬约束 + tasks complete 收尾。
3. 生成 `references/openspec-review-runbook.md`：Step 0 上下文 → 1 拉取 PR 材料（pulls get/files、commits、contents --ref）→ 2 逐章审查 → 3 汇总报告 → 4 PR 评论发布 → 5 收尾；含错误排查与明确禁止。
4. 生成 `references/openspec-checklist.md`：将附件规范 §0-§7 展开为逐条检查项（0-x/1-x/2-x/3-x 编号），§1.1 七类敏感模式全清单 + Conventional Commits 基线 + 报告模板 + 判定红线。
5. 生成 `references/grape-cli-reference.md`：qa 审查命令子集（pulls get/files/comments、commits、contents、tasks complete、a2a）+ 限制声明（命令遮蔽/无 api 逃生舱/不存在能力/本 skill 不用 pulls merge）。
6. 自检（spec §8 清单）→ 建 PR（base=$GRAPE_WORK_BRANCH）→ Issue 评论告知 → A2A 交棒 qa agent。

## 依赖

- grape CLI ≥0.3.2（已预装 0.3.3）。
- 规范文档全文：Issue #311 附件 ef25dd8c894541aa98070cc12cee94cc.md（已获取，246 行）。

## 风险

- 附件 OBS 直链曾失效 → 已通过任务附件机制成功下载，内容完整。
- SKILL.md ≤130 行、references ≤250 行约束 → 检查清单细节全部拆入 references。
