# Plan: grape-architecture-guardian 开发方案

## 实现顺序

1. 查重：`grape skills list --q architecture` + 仓库 `.grape/skills/qa/` 目录检查 → 平台同名 skill id=3180 为用户上传的同名技能包（user 附件副本，scope=repo），仓库侧 `.grape/skills/qa/` 下无同名目录（仅 openspec-pr-reviewer）→ 按 Spec 在仓库侧生成注册，与 qa-cli / openspec-pr-reviewer / production-architecture-guardian 职责互补。
2. 下载并通读附件技能包（Issue 附件 1668bf1d29104c598878b36b182bfdb8.zip，9 文件）→ 敏感信息扫描（内网 IP/绝对路径/真实凭据/个人名）。
3. 按 Spec 落位 `.grape/skills/qa/ai-coding-review/grape-architecture-guardian/`：SKILL.md 按 grape-cli skill 撰写规范重写（安全硬约束/Prerequisites/环境变量表/runbook 引用/操作边界/收尾），两条不可协商规则与 7 Gate 全部保留。
4. 适配 references：新增 architecture-review-runbook.md（走查表）+ grape-cli-reference.md（qa 审查命令子集）；附件 grape-architecture-rules.md（7 Gate 检查项+历史事故背景）与 known-config-values.md（数值注册表）保留并做 QA 合规适配。
5. 保留 templates/×2、scripts/×3、agents/×1 原样（只读、标准库、零依赖）。
6. 自检（spec §8 清单 + 验收标准逐项）→ 建 PR（base=$GRAPE_WORK_BRANCH=weekly-08）→ Issue 评论告知 → A2A 交棒 qa agent。

## 关键适配决策

| 项 | 附件原状 | 适配后 | 理由 |
|----|---------|--------|------|
| SKILL.md 结构 | 256 行自由结构 | 130 行规范结构（安全硬约束/Prerequisites/env 表/runbook 引用/收尾） | grape-cli skill 撰写规范 §2.2 段落顺序 + ≤130 行 |
| 7 Gate 检查项 | 在 SKILL.md 正文 | 迁至 references/grape-architecture-rules.md（§1-§6）+ 新增 §8.1 适用矩阵 | SKILL.md 行数约束；检查项本就属领域规则 |
| 评论首行格式 | 在 SKILL.md 输出规范段 | 迁至 rules §8.2 | 同上 |
| 数值注册表 | rules §7 与 known-config-values.md 重复 | rules §7 改为指向引用 | 去重 |
| 分支名硬编码 | rules §5.2 写死带个人名后缀的主工作分支 | `$GRAPE_WORK_BRANCH` 动态引用 | 剔除个人名（QA 合规 6.1）+ 与当前迭代基线保持一致 |
| 内部 DB ID | rules §2.2 事故背景含具体 org_id 数值 | 泛化为「org_id 非 0」 | 剔除内部 DB ID |
| 审查流程步骤 | SKILL.md「审查流程」段 | 新增 references/architecture-review-runbook.md 六步走查表 | 规范要求处理流程 = runbook 引用 |
| agents 子审计 | `agents/grape-architecture-auditor.md` | 原样保留，runbook Step 1 挂接（>100 文件或 >1000 行触发） | Spec 验收标准 |

## 依赖

- grape CLI ≥0.3.2（已预装 0.3.3）。
- 附件技能包全文（已下载解包，9 文件 2086 行）。
- Spec：Issue #440 评论 id=195439434（issue agent 交付）。

## 风险

- BLOCKED 误伤阻塞 PR 合入 → 坚守「无证据不判 BLOCKED」，证据不足标「需人工复核」（SKILL.md 安全硬约束第 5 条）。
- 8 项目中 7 个非 grape 主仓时脚本路径不可达 → runbook Step 2 给出从主仓 `git-repo contents --raw` 拉取脚本后本地执行的降级路径。
- SKILL.md ≤130 / references ≤250 行约束 → 检查项细节全部拆入 references/templates。
