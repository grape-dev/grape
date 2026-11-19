# 数值证据门（Numerical Evidence Gate）

> 硬规则 1 的执行细则：**无出处不得给数字**。脚本 `scripts/magic_number_audit.py` 只负责取证（找候选、看附近有无来源），**判定由检查项 H-4 完成**。
> 规则依据：`B2 代码不允许有魔鬼数字`（见 `references/architecture-rules-baseline.md` §3）。

## §1 适用范围

任何要进入配置、限额、告警、SLO 或技术建议的数字，包括但不限于：超时、TTL、连接池、worker/并发数、重试次数与退避、限流阈值、内存与 CPU 限额、健康检查间隔、熔断阈值、批大小、分页大小、数据保留期、RPO/RTO、时延与吞吐目标。

## §2 出处五源（缺一不可）

| # | 出处 | 举证形态 | 可否用于 `whitelist` 例外 |
|---|------|----------|---------------------------|
| 1 | 本项目已记录该数值（含 rationale） | 文档/注释/ADR 的 `file:line` | ✅ `primary_source` |
| 2 | 实际部署厂商的文档默认值/限额 | 厂商文档链接 + 版本 + 条目 | ✅ `industry_standard` |
| 3 | 已发布标准或 RFC | 标准编号/RFC 编号 + 条款 | ✅ `industry_standard` |
| 4 | 本项目实测（生产指标/基准/压测） | 指标看板/压测报告 + 时间 + 范围 | ✅ `measured` |
| 5 | 明确业务要求 | 需求/Issue/评审记录原文 + 定位 | ✅ `business_requirement` |

五源都不成立 → **不写最终值**，写占位符并登记待决策：

```
CACHE_TTL_SECONDS=<undecided: 需要命中率与陈旧容忍度测量>
```

并标注 `INSUFFICIENT DATA TO VERIFY`，写明缺哪种测量，列入报告「待决策清单」。**看起来合理的数字不能替代缺失的数字**——它会在评审中蒙混过关，比留空更危险。

> 本项目两条既定业务要求：A3 吞吐目标 `50tps`、A4 时延目标 `<200ms`，来源一律写「明确业务要求（Issue #3166 正文）」；**不得**表述为实测值、厂商默认值或行业标准。

## §3 取证命令

```bash
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 技能目录相对解析，禁止绝对路径
REPO_PATH="${REPO_PATH:-.}"

# 1) 机器可校验映射（结构化 JSON）
python3 "$SKILL_DIR/scripts/magic_number_audit.py" "$REPO_PATH" --json --min-severity medium

# 2) 带例外白名单（登记表见 templates/numerical-register.md）
EXCEPTIONS="${NUMERICAL_EXCEPTIONS:-$REPO_PATH/numerical-exceptions.json}"
python3 "$SKILL_DIR/scripts/magic_number_audit.py" "$REPO_PATH" --json --whitelist "$EXCEPTIONS"

# 3) 人读报告（文本）
python3 "$SKILL_DIR/scripts/magic_number_audit.py" "$REPO_PATH" --min-severity high

# 4) 脚本自检（改动脚本后必跑）
python3 "$SKILL_DIR/scripts/magic_number_audit.py" --self-check
```

## §4 `--json` 输出 → 「未举证数字清单」字段映射

```json
{
  "schema": "grape.magic-number-audit/v1",
  "rule": "B2",
  "summary": {"candidates": 0, "unbacked": 0, "claimed_backed": 0, "whitelisted": 0},
  "unbacked_numbers": [
    {"number_value": "300", "key": "CACHE_TTL", "location": "src/config.py:42",
     "context": "CACHE_TTL = 300", "evidence_status": "unbacked",
     "exception_basis": "", "category": "cache_ttl", "severity": "high", "occurrences": 1}
  ],
  "findings": ["（原始命中明细，含 risk 与 other_locations）"]
}
```

| 报告列（`templates/audit-report.md` §3） | JSON 字段 | 说明 |
|------------------------------------------|-----------|------|
| 数字值 | `number_value` | 原样保留写法（含单位/乘法链） |
| 出现位置 | `location` | `file:line`，可直接引用为证据 |
| 上下文 | `context` | 该行源码，评审时免二次跳转 |
| 举证状态 | `evidence_status` | `unbacked`（未举证）/ `claimed_backed`（附近有来源声明，**须核对声明是否真的支持该值**）/ `whitelisted`（已登记例外） |
| 例外依据 | `exception_basis` | 白名单登记内容（`basis_type: basis`），未登记为空 |

**结案规则**：`unbacked` 必须二选一——补举证（落到五源之一）或转 `<undecided>` + 待决策清单；`claimed_backed` 必须人工核对声明与数值一致，声明不成立时回退为 `unbacked`；`whitelisted` 仅接受 §2 四类出处。

## §5 例外白名单登记（`numerical-exceptions.json`）

```json
{
  "entries": [
    {"key": "CACHE_TTL", "value": "300", "file": "src/*.py",
     "basis_type": "business_requirement", "basis": "需求单 #3166 指定缓存 5 分钟刷新"}
  ]
}
```

- `basis_type` 只接受 `primary_source` / `measured` / `business_requirement` / `industry_standard`；**口径外类型（如「经验值」「看起来合理」）脚本会拒收并告警**。
- `basis` 为空 → 拒收；`value`/`file` 可选，用于收敛匹配范围（`file` 支持 glob）。
- 登记表同时落到审计报告（`templates/audit-report.md` §3），**报告与 JSON 白名单必须一致**，不得只在脚本里登记。

## §6 判定（检查项 H-4）

1. 跑 §3 第 1 条命令，取 `unbacked_numbers[]`。
2. 逐条给出结案：举证（附 `file:line`/链接/实测时间）｜例外登记（四类出处）｜转 `<undecided>` + 待决策清单。
3. `summary.unbacked > 0` 且未结案 → H-4 判「不符合」，级别 `Critical`（规则 B2）。
4. 数值证据面（Gate §2 第 17 面）证据不足 → Gate 裁决 `BLOCKED`（见 `references/gate-review.md` §3 第 3 条）。
5. 自检反幻觉：任何数字能否被仓库代码、项目文档、标准或实测支持？不能则不作事实陈述。
