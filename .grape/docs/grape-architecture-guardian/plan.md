# grape-architecture-guardian 2.0 改造方案（Issue #467）

> 基线：spec.md（Issue agent Spec，评论 id=196008879）。本文件只描述实现顺序/依赖/风险，不重复规则内容。

## 实现顺序

1. **规则文件升级**（`references/grape-architecture-rules.md`）
   - 新增 §10 架构守护规则 2.0：4 级判定体系定义 + 16 条规则（A1-A9 / B1-B2 / C1-C3 / D1-D2），每条含【级别】标注、检查方式与证据要求。
   - §8 Gate 适用矩阵增补 2.0 规则类别适用说明；既有 §1-§7 七大类检查项**原样保留**（无规则丢失）。
   - 既有 7 Gate 检查项严重度与 2.0 四级体系对齐（BLOCKED/Critical/Medium/Informational）。
2. **SKILL.md 升级**：版本 1.0.1 → 2.0.0；处理流程/硬约束/任务完成流程改为按 4 级别归类统计（0 BLOCKED / N Critical / N Medium / N Informational）+ 合入建议映射；新增 Medium 级 pending-review 人工判断节点。
3. **runbook 升级**（`references/architecture-review-runbook.md`）：Step 3 判定改为「逐 Gate + 2.0 规则双轨判定」；Step 4 汇总按 4 级别统计；新增 Step 4b Medium 级不遵从 → `grape issues pending-review` 置审核点（flow_nodes 落地）。
4. **模板升级**（`templates/pr-review-report.md`）：总体结论/发现清单/结论与建议三处按 4 级别归类 + 合入建议；`templates/finding.md` 严重度指引与 2.0 四级对齐。
5. **脚本扩展**（`scripts/grape_constraint_check.py`）：
   - A6 表名检查：新增 `CREATE TABLE` 语句表名前缀校验（grape_t_xxx / grape_admin_t_xxx，现有遗留表名白名单）。
   - B1 SQL 双模式：新增 SQL 语句中 SQLite 专属 / MySQL 专属方言混用检测（基于 `src/mysql_compat.py` 翻译层边界）。
   - B2 魔鬼数字：不新写，复用既有 `magic_number_audit.py`（runbook Step 2 已有调用），规则文件中注明复用关系。
   - D1 i18n：新增前端 JS 新增用户可见文案 key 化检查（对照 `locales/zh|en` 双语言包 key 一致性，复用仓库 `scripts/check-i18n-keys.py` 的既有机制）。
6. **agents/grape-architecture-auditor.md**：执行步骤补 2.0 规则对照。

## 依赖

- grape CLI ≥0.3.2（既有）；`grape issues pending-review/review-pass/review-reject` 命令已验证存在（契约 v2.1.0）。
- B2 依赖既有 `scripts/magic_number_audit.py`（不动）；D1 依赖 `locales/` 语言包结构（只读对照）。

## 风险与对策

| 风险 | 对策 |
|------|------|
| A6 表名规则与存量表名（非 grape_t_ 前缀）冲突 | 检查只针对 PR 新增 `CREATE TABLE`；存量表名进白名单 |
| B1 方言检测误报 | 只报静态可确定项，其余标注「需人工复核」 |
| D1 key 化检查误报（动态拼接 key） | 输出为候选 + 证据，finding 需人工复核（沿用脚本输出即候选的原则） |
| 行数超限（SKILL.md ≤130 / reference ≤250） | 规则明细控制在 §10，必要时压缩既有背景描述 |
