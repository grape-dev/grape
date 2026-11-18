# Spec：Issue #3166 — 架构守护技能 v3（production-architecture-guardian 检查项完善）

> 来源：Issue #3166 评论 Spec v2（comment id=201738707，issue agent 交付，原样落盘口径）+ VOD 技能需求单（comment id=201735982）。
> 目标技能：`production-architecture-guardian`（平台 skill_id=3161，agent_type=qa，user-uploaded，scope=user）；本次为 v3 迭代。
> platform_skill=true；flow_nodes=无；agent_type 保持 qa。

## Objective

把叙述式审计清单升级为**逐项编号、可判定、可举证**的检查项体系：43 条检查项（phases A–J）+ 17 检查面 Gate + 数值证据门机器校验 + 路径去硬编码 + 交叉引用一致；并把 Issue 正文「架构守护规则3.1」（A/B/C/D 四类 23 条）收纳为规则基线。

## In Scope（逐项，对应 Spec v2 P0–P7）

| 项 | 内容 | 落点 |
|----|------|------|
| P0 | 四级判定体系 BLOCKED/Critical/Medium/Informational 作为检查项结论级别唯一取值域 | SKILL.md「四级判定体系」；rules-baseline §1 |
| P1 | phases A–J 检查项逐项编号 + 四段式（检查内容/判定标准/所需证据/结论级别） | SKILL.md 索引表；`references/audit-checklists.md`（43 条） |
| P2 | 生产就绪 5 项 N1–N5 归位对应 phase | B-2 / D-3 / G-3 / H-2 / F-3；rules-baseline §6 |
| P3 | Gate：BLOCKED 阻断条件清单 + 17 检查面最低证据 + APPROVED WITH CONDITIONS 条件模板（命中 BLOCKED 级规则不通过 → Gate BLOCKED） | `references/gate-review.md`；`templates/gate-decision.md` |
| P4 | `magic_number_audit.py` 结构化 JSON 映射未举证数字清单 + 例外白名单登记；保留「无出处不得给数字」 | `scripts/magic_number_audit.py`（`--json`/`--whitelist`/`--self-check`）；`references/numerical-evidence-gate.md`；`templates/numerical-register.md` |
| P5 | 去硬编码 Windows 绝对路径，技能包相对解析；三支脚本 grape 环境可直接执行 | 全包路径写法；`scripts/*.py`（shebang + python3，可执行位） |
| P6 | 检查项与 references/templates/scripts 交叉引用一致，无悬空 | 每条检查项「依据」列；双向索引 rules-baseline §5 |
| P7 | 「架构守护规则3.1」A/B/C/D 四类 23 条收纳为规则基线（含级别），与检查项双向索引 | `references/architecture-rules-baseline.md` |

## Out of Scope

- 不改两条硬规则：数值证据门「无出处不得给数字」、不给虚假保证。
- 不改 audit / review 两模式与 APPROVED / APPROVED WITH CONDITIONS / BLOCKED 三态结论语义（向后兼容）。
- agent_type 仍为 qa；不新增流程审批节点；不新增自定义环境变量。
- 不涉及 grape 平台代码与生产资源改动。

## Acceptance Criteria（含本次实现结果）

- [x] 四级判定体系落地为检查项结论级别唯一取值域
- [x] 43 条检查项按 phases A–J 编号落地，每条含检查内容/判定标准/所需证据/结论级别
- [x] 23 条规则基线收录（A1–A14、B1–B4、C1–C3、D1–D2，含级别）并与检查项双向索引
- [x] N1–N5 归位（B-2/D-3/G-3/H-2/F-3）且判定标准与证据形态齐备
- [x] Gate 含 BLOCKED 阻断条件清单（3 条）+ 17 检查面最低证据 + 条件模板；命中阻断条件 → 裁决 BLOCKED
- [x] `magic_number_audit.py --json` 输出 `unbacked_numbers[]`（数字值/出现位置/上下文/举证状态/例外依据）+ 白名单四类出处校验
- [x] 正文无本机绝对路径；三支脚本可直接执行（`--self-check` 通过）
- [x] 交叉引用一致、无悬空（引用扫描 0 missing）
- [x] agent_type=qa、两模式与三态结论向后兼容

## Risks & Mitigations

| 风险 | 影响 | 缓解 |
|------|------|------|
| user-uploaded 更新后未重传不生效 | 中 | sre 节点经 `grape-skill-uploader` 重新上传注册并确认版本覆盖 |
| 检查项膨胀导致正文超限 | 低 | 正文只放索引与体系，四段式细则下沉 references（SKILL.md 130 行、reference ≤250 行） |
| 阻断条件与「无证据不判不符合」互相抵消 | 中 | gate-review §3「口径澄清」：不冤枉代码（指控需证据）、不放行无据变更（关键面证据缺失即 BLOCKED） |
| 平台 skill 3161 存量正文不可读（403） | 中 | 以 Spec v2 结构 + 仓库内同类资产 + Issue 正文规则 3.1 为基线重建；注册名沿用，版本号 3.0.0 由 sre 上传时确认 |
