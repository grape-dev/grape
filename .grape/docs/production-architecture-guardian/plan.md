# 方案与实施计划：production-architecture-guardian v3

## 实施顺序与依赖

| # | 步骤 | 产出 | 依赖 |
|---|------|------|------|
| 1 | 规则基线收纳（Issue 正文 23 条 + 四级定义 + 双向索引） | `references/architecture-rules-baseline.md` | Issue #3166 正文原文 |
| 2 | 检查项编号与四段式（phases A–J，43 条）+ N1–N5 归位 + 依据列 | `references/audit-checklists.md` | 步骤 1（规则编号） |
| 3 | SKILL.md：四级体系 + phase 索引 + Gate 摘要 + 数值证据门 + 平台交互 | `SKILL.md`（130 行） | 步骤 1–2 |
| 4 | Gate 文档与条件模板（17 检查面最低证据 + 阻断清单 + 三态） | `references/gate-review.md`、`templates/gate-decision.md` | 步骤 2（检查面 ↔ 检查项映射） |
| 5 | 数值证据门（脚本结构化输出 + 白名单 + 文档 + 登记表） | `scripts/magic_number_audit.py`、`references/numerical-evidence-gate.md`、`templates/numerical-register.md` | 规则 B2 |
| 6 | 走查表（audit/review 两模式，命令级） | `references/architecture-review-runbook.md`、`references/grape-cli-reference.md` | 步骤 3–5 |
| 7 | 自检（行数/引用/路径/脚本执行/级别取值域） | 自检记录（见 task.md） | 全部 |

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 检查项承载位置 | 正文全量内联 / 正文索引 + references 细则 | 正文索引 + references 四段式 | SKILL.md ≤130 行硬约束；四段式可逐条执行且可举证 |
| 「17 检查面」口径 | 仅列需求单 16 个面名 / 补足第 17 面 | 16 个既有面 + 第 17 面「数值证据」 | 数值证据门需独立成面才可门控；面名不臆造，增补面明确标注为新增 |
| 阻断条件与「不判不符合」的关系 | 合并为一条 / 拆成「指控」与「放行」两条口径 | 拆成两条并写明澄清 | 避免「无据指控」与「无据放行」互相抵消 |
| 例外白名单 | 禁止例外 / 白名单登记 | 白名单登记（四类出处，脚本强校验） | 保留「无出处不得给数字」硬规则，同时让合法例外可机器校验 |
| 脚本改造方式 | 重写 / 增量扩展 | 增量扩展 `--json` 字段 + `--whitelist` + 自检断言 | 保持既有取证能力与文本报告不变（向后兼容） |
| 路径解析 | 绝对路径 / 技能目录相对 | `SKILL_DIR`（bash）+ `Path(__file__).resolve().parent`（python） | 跨运行环境可执行，符合平台技能要求 |
| 注册名 | 更名 production-architecture-guardian-v3 / 沿用 | 沿用 `production-architecture-guardian`，version 3.0.0 | 技能为 user-uploaded（skill_id=3161），沿用名可原地升级；最终以 sre 上传节点确认 |

## 风险与回退

- **兼容性**：两模式与三态结论语义未改，仅新增检查项编号与阻断清单 → 既有调用方无感。
- **脚本回退**：`magic_number_audit.py` 仅新增可选参数与 JSON 字段，未改既有文本输出与命中规则；`--self-check` 覆盖新旧断言。
- **上传风险**：平台侧为 user-uploaded 技能，需 sre 重新上传覆盖；本 PR 只交付技能包，不直接改平台注册数据。
