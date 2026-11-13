# 方案/计划：grape-architecture-guardian-v3 技能生成（Issue #2403）

> 来源：`.grape/docs/grape-architecture-guardian-v3/spec.md`（<upstream-agent> 交付 Spec，comment id=<redacted>）。
> 生成方：<coding-agent>。范围：仅新增 `.grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/` 与本文档目录。

## 1. 实现顺序

| 步 | 动作 | 产出 | 依赖 |
|----|------|------|------|
| 1 | 查重（`grape skills list --q architecture-guardian`、`git-repo contents .grape/skills/qa`） | 结论：3.0 目录不存在，2.0 存在（只读基线） | — |
| 2 | 规则包落盘：24 条 + 四级 + 合入映射 + 7 Gate 映射 + 两条硬规则细则 | `references/grape-architecture-rules-v3.md` | Issue #2403 正文（唯一权威） |
| 3 | 资产移植：10 个领域 references / 5 个 templates / 3 个只读 scripts | `references/*`、`templates/*`、`scripts/*` | 仓库 2.0 `grape-architecture-guardian` 内容基线（VOD §三.1） |
| 4 | 模板适配：`templates/finding.md` 改为 3.0 四级 + `file:line`/commit sha + 验证方式 + 修复顺序与依赖 | `templates/finding.md` | 步 2 四级定义 |
| 5 | 审查 runbook：7 模式执行矩阵 + 8 步流程 + 平台交互 + 错误排查 | `references/architecture-guardian-3-runbook.md` | 步 2/3 |
| 6 | 主文件：Prerequisites / env 表 / 定位 / 评论规范 / 处理流程 / 领域规则 / 边界 / 收尾 | `SKILL.md`（≤130 行） | 步 2/5 |
| 7 | CLI 基线：只列本技能用到命令 + 限制/遮蔽/无逃生舱/退出码 | `references/grape-cli-reference.md` | 步 5 |
| 8 | 自检 + 提交 + PR + A2A qa | PR、Issue 评论、A2A event | 步 1-7 |

## 2. 关键实现决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 规则权威 | Issue #2403 正文 24 条（A1-A14/B1-B5/C1-C3/D1-D2） | 用户正文是唯一权威；2.0 的 16 条是其子集，不作权威 |
| 7 Gate 定位 | 保留为审查维度，附 Gate→规则组映射 | 不丢失 2.0 审查视角，又不让旧规则清单替代新规则 |
| B5 | 占位「内容待补充」，判定固定为「不适用」，禁止编造 | 原文无内容；编造条款会污染规则集 |
| 资产基线 | references/templates/scripts 与交互范式均取仓库 2.0 `grape-architecture-guardian`（VOD §三.1） | 与仓库实际资产对齐；本地 2.0 已完成平台化 |
| 领域 references 语言 | 保持英文原文 | 标准/厂商引用原文更可核验，避免翻译引入偏差；Grape 特有内容（规则包/runbook/SKILL）用中文 |
| finding 模板 | 由 2.0 的 Blocker/Critical/High/Medium/Low/Informational 改为 3.0 四级 | 与规则包级别一一对应，避免级别口径不一致 |
| flow_nodes | pending-review | 与仓库 2.0 基线一致；存在 Medium 时按 runbook Step 6b 置审核点，用户回复后 review-pass/review-reject 续接 |
| 目录 | `.grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/` | 与 2.0 隔离，物理上不可能覆盖 |

## 3. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| 平台化改造丢失两条硬规则（首要防退化） | High | 硬规则同时写入 SKILL.md 专章 + 规则包 §9；验收逐条核对「五源 + 占位符 + 待决策清单」「8 个证据标签」 |
| Medium 审核点未续接被忽略 | Med | 报告首屏固定「需人工判断是否合入」清单 + runbook Step 6b 置 pending-review 并续接 |
| BLOCKED 误判阻断合入 | Med | 无 `file:line`/`commit sha` 不判 BLOCKED，标「需人工复核」 |
| 50tps/200ms 被当成实测 | Low | 规则包与 SKILL.md 双处固定来源=明确业务要求（Issue #2403 原文） |
| 误改 2.0 | High | 不触碰 `grape-architecture-guardian/` 与 2.0 平台资产；提交前 diff 核对 |
| 脚本跨目录调用失败 | Low | bash 用 `SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"`，python 用 `Path(__file__).resolve().parent` |

## 4. 验证方式

- 行数：SKILL.md ≤130、references ≤250（自检命令见 task.md）。
- 路径：全文 grep `C:\`、`/Users/`、`/home/`、`.claude` 应为 0 命中。
- 规则：24 条编号齐备、每条带级别；B5 标注「内容待补充」。
- 硬规则：`<undecided:` + `INSUFFICIENT DATA TO VERIFY` + 待决策清单；8 个证据标签齐备。
- 平台交互：Prerequisites（版本/refresh/env 校验）、env 表、`[QA Agent]` 前缀、`tasks complete` 结构化字段。
- 2.0 零改动：`grape git status` / PR diff 不出现 2.0 路径。
- 脚本可运行：3 只脚本在仓库根上 `--json` 跑通。