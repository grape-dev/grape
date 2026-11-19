# audit 报告模板（architecture audit）

> 复制本模板填写；发布时 body 以 `[QA Agent]` 开头。**首屏顺序不可调整**：总体结论 → 四级统计 → Medium 清单 → 待决策条数。
> 检查项编号与依据引用 `references/audit-checklists.md`；级别只取 `BLOCKED`/`Critical`/`Medium`/`Informational`。

## 首屏（评论第一屏）

```
[QA Agent] 架构守护审计报告（{仓库/系统}｜模式 audit｜范围 {目录/模块}｜head sha {sha8}）

总体结论：{一句话：可维护性/生产就绪证据是否成立 + 最关键的 1 项风险}
四级统计：{N} BLOCKED / {N} Critical / {N} Medium / {N} Informational（共 43 条检查项，证据不足 {K} 条）
需人工判断（Medium）：{编号 + 一句话影响；无则写「无」}
待决策清单：{Z} 条（缺测量的数字项）
证据分级：{verified: code x 项 / verified: config x 项 / measured x 项 / plausible x 项 / INSUFFICIENT DATA TO VERIFY x 项}
```

## §1 phase 覆盖汇总（43 条检查项）

| Phase | 项数 | 符合 | 不符合 | 不适用 | 证据不足 | 结论级别分布 |
|-------|------|------|--------|--------|----------|--------------|
| A Discovery | 4 | | | | | |
| B Architecture Mapping | 6 | | | | | |
| C Scalability | 4 | | | | | |
| D Security | 5 | | | | | |
| E Performance | 3 | | | | | |
| F Reliability | 4 | | | | | |
| G Data | 6 | | | | | |
| H Operations | 5 | | | | | |
| I Supply Chain | 3 | | | | | |
| J Verification Plan | 3 | | | | | |
| **合计** | **43** | | | | | |

> 未覆盖的 phase 必须写「本次范围未涉及」+ 理由，不得留空。

## §2 不符合项（按级别降序）

| 检查项 | 级别 | 规则 | 证据（file:line / commit sha） | 问题 | 故障场景 | 修复顺序与依赖 | 验证方式 |
|--------|------|------|-------------------------------|------|----------|----------------|----------|
| {B-2} | {BLOCKED} | {A14} | {path:line} | | | | |

> 无证据的项不得写进本表（落「证据不足」）。逐条格式见 `templates/finding.md`。

## §3 未举证数字清单（数值证据门，检查项 H-4）

来源：`scripts/magic_number_audit.py --json` 的 `unbacked_numbers[]`；条数须与 `summary.unbacked` 对齐。

| # | 数字值 | 出现位置 | 上下文 | 举证状态 | 例外依据 | 结案方式 |
|---|--------|----------|--------|----------|----------|----------|
| 1 | {300} | {src/config.py:42} | {CACHE_TTL = 300} | {unbacked} | — | {补源 / 登记例外 / → `<undecided: 需要命中率测量>`} |

- 举证状态取值：`unbacked`（未举证）/ `claimed_backed`（附近有声明，须核对是否真支持）/ `whitelisted`（已登记例外）。
- 例外只接受四类出处：`primary_source` / `measured` / `business_requirement` / `industry_standard`；登记表见 `templates/numerical-register.md`。

## §4 待决策清单（`INSUFFICIENT DATA TO VERIFY` 汇总）

| # | 检查项/规则 | 待决策内容 | 缺哪种测量或文档 | 责任人/建议来源 |
|---|-------------|------------|------------------|------------------|
| 1 | {C-1 / A3} | {吞吐目标 50tps 是否可达} | {缺压测报告：需 {场景} 下 {QPS} 与 p99 口径} | |

> 占位符写法：`<undecided: 需要何种测量>`；不得用「看起来合理」的数字填空。

## §5 与规则基线的交叉核对

- 23 条规则（A1…D2）是否全部有检查项结论（符合/不符合/不适用/证据不足）？漏项须补齐后才可发布。
- 检查项反向索引是否成立（无无依据检查项、无未索引规则）？核对表见 `references/architecture-rules-baseline.md` §5。

## §6 后续动作

- `BLOCKED`/`Critical` 需整改 → A2A 交办 `coding`（附本报告 comment id + 修复顺序）。
- 需放行/合并决策 → 交办 `sre`（仅 `APPROVED` 场景）。
- 证据缺失需补充 → 评论追问（本版不置审核点，flow_nodes=无）。
- 收尾：`grape tasks complete`（结构化字段见 SKILL.md）。
