# {skill-name} 汇总清单

> 生成日期：{YYYY-MM-DD}
> 数据来源：本地 case-run 测试报告 + tester 测试报告 + GitCode Issue API

---

## 一、测试统计

### case-run

| 序号 | Skill | 归属服务 | 报告生成时间 | 总用例数 | 通过 | 告警 | 失败 | 通过率 | 测试报告 |
|------|-------|---------|-------------|---------|------|------|------|--------|---------|
| 1 | {skill-name} | {归属服务} | {YYYY-MM-DD} | {total} | {pass} | {warn} | {fail} | {rate}% | [整合测试报告](../evaluation-results/{skill-name}/case-run/{YYYYMMDD}/整合测试报告.md) |
| 2 | {skill-name} | {归属服务} | {YYYY-MM-DD} | {total} | {pass} | {warn} | {fail} | {rate}% | [整合测试报告](../evaluation-results/{skill-name}/case-run/{YYYYMMDD}/整合测试报告.md) |
| … | | | | | | | | | |

> 每行对应一次测试记录，按报告生成时间倒序排列（最新在前）。

### tester

| 序号 | Skill | 归属服务 | 报告生成时间 | 总用例数 | 通过 | 告警 | 失败 | 通过率 | 测试报告 |
|------|-------|---------|-------------|---------|------|------|------|--------|---------|
| 1 | {skill-name} | {归属服务} | {YYYY-MM-DD} | {total} | {pass} | {warn} | {fail} | {rate}% | [test-report](../evaluation-results/{skill-name}/tester/{YYYYMMDD}/reports/test-report.md) |
| 2 | {skill-name} | {归属服务} | {YYYY-MM-DD} | {total} | {pass} | {warn} | {fail} | {rate}% | [test-report](../evaluation-results/{skill-name}/tester/{YYYYMMDD}/reports/test-report.md) |
| … | | | | | | | | | |

> 每行对应一次测试记录，按报告生成时间倒序排列（最新在前）。

> 通过率 = (通过 + 告警) / 总用例数 × 100%

---

## 二、GitCode 问题单清单

共 {N} 个 open Issue。

> 排序规则：按创建时间倒序排列（最新在前）。仅展示 open 状态的 Issue。

| Issue | 状态 | 创建时间 | 责任人 | 标签 | 标题 |
|-------|------|---------|--------|------|------|
| [#{num}]({url}) | 🟢 open | {YYYY-MM-DD HH:MM:SS} | {name} @{login} | {labels} | {title} |

---

