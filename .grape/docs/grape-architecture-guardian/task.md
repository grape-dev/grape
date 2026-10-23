# Task: grape-architecture-guardian 任务拆解清单

- [x] T1 查重：skills list（平台同名 id=3180 为用户上传附件副本）+ 仓库 `.grape/skills/qa/` 无同名目录 → 仓库侧生成通过
- [x] T2 读取三件套（grape-cli-reference / spec-reference / command-matrix）
- [x] T3 下载并通读附件技能包（9 文件）+ 敏感信息扫描（无内网 IP/绝对路径/真实凭据；剔除个人名分支、泛化内部 org_id）
- [x] T4 生成 SKILL.md（130 行：frontmatter bash+grape / 安全硬约束 5 条 / 两条不可协商规则 / Prerequisites / env 表 / 定位 / 评论规范 / runbook 引用 / 操作边界 / 硬约束 / tasks complete 收尾）——验收：≤130 行、tools 含 bash、含 CLI 优先+任务上下文先行、数值门禁+反虚假保证保留
- [x] T5 生成 references/architecture-review-runbook.md（117 行：Step 0-6 走查表 + 错误排查 + 明确禁止）
- [x] T6 适配 references/grape-architecture-rules.md（250 行：7 Gate 检查项+历史事故背景保留，新增 §8.1 适用矩阵 + §8.2 评论首行格式，剔除个人名/内部 ID）
- [x] T7 保留 references/known-config-values.md（数值注册表）+ 新增 references/grape-cli-reference.md（qa 审查命令子集）
- [x] T8 保留 templates/（pr-review-report.md + finding.md）、scripts/×3、agents/grape-architecture-auditor.md 原样
- [x] T9 验证 3 脚本可独立运行（只读、标准库、零依赖）——本仓与空目录冒烟
- [x] T10 生成 .grape/docs/grape-architecture-guardian/（spec 原样落盘 / plan / task）
- [x] T11 质量自检（spec §8 清单 + 需求单验收标准逐项）
- [x] T12 建分支提交 + 创建 PR（base=$GRAPE_WORK_BRANCH=weekly-08）+ Issue 评论告知
- [x] T13 A2A 交棒 qa agent 审查 + tasks complete 收尾

## 验证方式

- `wc -l`：SKILL.md 130 ≤130；references 最大 250 ≤250；frontmatter description ≤500 字符。
- 脚本冒烟：`python3 scripts/repo_inventory.py . --json` / `magic_number_audit.py . --min-severity medium` / `grape_constraint_check.py . --json` 均正常退出（exit 0）。
- 敏感扫描：无 `weekly-08-ljt`、无内网 IP 字面量（`192.168.x.x` 为掩码示例）、无绝对路径、无真实凭据。
- 后续由 QA agent 对 PR 做技能规范合规性审查闭环验证。
