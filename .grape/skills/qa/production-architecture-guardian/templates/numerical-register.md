# 数值登记表（例外白名单 + 待决策）

> 用途：给 `scripts/magic_number_audit.py --whitelist` 提供登记输入，同时作为报告「未举证数字清单 / 待决策清单」的人读载体。
> **口径**：例外只接受四类出处（`primary_source` / `measured` / `business_requirement` / `industry_standard`）；**「无出处不得给数字」硬规则不因登记表存在而放宽**——登记表只登记真实成立的出处。

## §1 例外白名单登记表（人读）

| # | key | 数字值 | 位置（file:line 或 glob） | 出处类型 | 出处依据（链接/文档/Issue 定位/实测时间） | 登记人 | 复核日期 |
|---|-----|--------|---------------------------|----------|--------------------------------------------|--------|----------|
| 1 | {CACHE_TTL} | {300} | {src/config.py:42} | {business_requirement} | {Issue #3166 正文/需求单定位} | | |

**出处类型与举证要求**

| 出处类型 | 含义 | 必须给到 |
|----------|------|----------|
| `primary_source` | 本项目已记录该数值（含 rationale） | 文档/ADR/注释 `file:line` |
| `measured` | 本项目实测（生产指标/基准/压测） | 指标出处 + 测量时间 + 测量范围 |
| `business_requirement` | 明确业务要求 | 需求/Issue/评审原文 + 定位 |
| `industry_standard` | 标准/RFC/实际部署厂商文档默认值 | 标准编号或厂商文档链接 + 版本 + 条目 |

## §2 机器可读登记（`numerical-exceptions.json`）

```json
{
  "entries": [
    {
      "key": "CACHE_TTL",
      "value": "300",
      "file": "src/config.py",
      "basis_type": "business_requirement",
      "basis": "需求单 #3166：缓存 5 分钟刷新，原文定位见报告 §3"
    }
  ]
}
```

- `basis_type` 只接受上表四类；其它类型（「经验值」「看起来合理」「行业惯例」）脚本会拒收并告警。
- `basis` 为空 → 拒收。`value` / `file` 可选：省略则按 key 全量匹配（谨慎使用）。
- 用法：`python3 scripts/magic_number_audit.py "$REPO_PATH" --json --whitelist numerical-exceptions.json`。

## §3 待决策清单（未举证数字，不得填空）

| # | key / 检查项 | 待决策内容 | 缺哪种测量或文档 | 建议方案 | 状态（open/closed） |
|---|--------------|------------|------------------|----------|---------------------|
| 1 | {H-2 告警阈值} | {该阈值取值} | {缺指标口径与观测窗口} | {先接指标，跑 2 周再定阈值} | open |

占位符写法（进代码/配置/报告正文时）：

```
ALERT_LATENCY_P99_MS=<undecided: 需要 p99 延迟观测窗口与业务可接受阈值测量>
```

## §4 报告一致性核对

- 报告 §3「未举证数字清单」条数 = `magic_number_audit.py --json` 的 `summary.unbacked`；
- 报告登记的例外行 = `numerical-exceptions.json` 中 `entries` 命中的行（不得只在脚本登记而不进报告）；
- 报告 §4「待决策清单」= §3 表中状态为 `open` 的行。
