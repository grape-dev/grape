# grape-architecture-guardian 2.0 任务拆解（Issue #467）

> 逐项验收/验证清单，对照 spec.md Acceptance Criteria。

## T1 规则文件 2.0 升级（grape-architecture-rules.md）
- [ ] 4 级判定体系定义落位（BLOCKED/Critical/Medium/Informational + 合入建议映射）
- [ ] 16 条规则完整：A1-A9 / B1-B2 / C1-C3 / D1-D2，每条含【级别】+ 检查方式 + 证据要求
- [ ] 既有 7 Gate 检查项无丢失（§1-§7 原样保留）
- [ ] A3/A4/A5 证据类规则沿用数值证据门禁标签（verified/measured/INSUFFICIENT DATA）
- 验证：grep 逐条 ID 核对 16 条 + 7 Gate 章节标题齐全

## T2 SKILL.md 升级
- [ ] frontmatter version 1.0.1 → 2.0.0
- [ ] 处理流程/硬约束：结论按 4 级别归类统计 + 合入建议（BLOCKED/Critical 不建议合入、Medium 人工判断、Informational 不影响）
- [ ] 行数 ≤130
- 验证：行数统计 + grep 四级别关键字

## T3 runbook 升级（architecture-review-runbook.md）
- [ ] Step 3 双轨判定（7 Gate + 2.0 规则 A-D）
- [ ] Step 4 按 4 级别统计汇总
- [ ] Step 4b Medium 级不遵从 → pending-review 流程节点落地（flow_nodes）
- [ ] 行数 ≤250
- 验证：行数统计 + grep pending-review 命令

## T4 模板升级（pr-review-report.md / finding.md）
- [ ] 总体结论按 4 级别统计（0 BLOCKED / N Critical / N Medium / N Informational）+ 合入建议
- [ ] finding.md 严重度指引与 2.0 四级对齐
- 验证：目视核对模板字段

## T5 脚本扩展（grape_constraint_check.py）
- [ ] A6 表名检查（grape_t_xxx / grape_admin_t_xxx，存量白名单）
- [ ] B1 SQL 双模式（sqlite/mysql 方言混用检测）
- [ ] D1 i18n key 化检查（zh/en 语言包对照）
- [ ] B2 复用 magic_number_audit.py（不新写，规则文件注明）
- [ ] 冒烟：3 脚本在本仓库可执行、输出 JSON 合法
- 验证：构造正反用例（合规表名/违规表名、双语 key/缺 key）跑脚本

## T6 agents 子审计指令同步
- [ ] grape-architecture-auditor.md 执行步骤含 2.0 规则对照
- 验证：目视核对

## T7 提交与交付
- [ ] commit（feat(skills): … Issue #467）+ push feat/issue-467
- [ ] 建 PR（base=$GRAPE_WORK_BRANCH=weekly-08）+ Issue 评论告知
- [ ] A2A 交棒 qa + grape tasks complete
- 验证：PR html_url + comment id + a2a seq
