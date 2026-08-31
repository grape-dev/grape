# 评测结果总览

> 生成日期：{YYYY-MM-DD}
> 数据来源：本地 case-run / tester 测试报告 + GitCode Issue API
> 覆盖范围：evaluation-results/ 下 {N} 个 Skill 目录

---

## 一、测试统计总览

### case-run

| 序号 | Skill | 归属服务 | 报告生成时间 | 总用例数 | 通过 | 告警 | 失败 | 通过率 | 测试报告 |
|------|-------|---------|-------------|---------|------|------|------|--------|---------|
| 1 | {skill} | {归属服务} | {YYYY-MM-DD} | {total} | {pass} | {warn} | {fail} | {rate}% | [整合测试报告](../evaluation-results/{skill}/case-run/{YYYYMMDD}/整合测试报告.md) |
| 2 | {skill} | {归属服务} | {YYYY-MM-DD} | {total} | {pass} | {warn} | {fail} | {rate}% | [整合测试报告](../evaluation-results/{skill}/case-run/{YYYYMMDD}/整合测试报告.md) |
| … | | | | | | | | | |
| | **合计** | | | **{total}** | **{pass}** | **{warn}** | **{fail}** | **{rate}%** | |

> 每个 Skill 的全部历史测试记录均展示，按报告生成时间倒序排列。

### tester

| 序号 | Skill | 归属服务 | 报告生成时间 | 总用例数 | 通过 | 告警 | 失败 | 通过率 | 测试报告 |
|------|-------|---------|-------------|---------|------|------|------|--------|---------|
| 1 | {skill} | {归属服务} | {YYYY-MM-DD} | {total} | {pass} | {warn} | {fail} | {rate}% | [test-report](../evaluation-results/{skill}/tester/{YYYYMMDD}/reports/test-report.md) |
| 2 | {skill} | {归属服务} | {YYYY-MM-DD} | {total} | {pass} | {warn} | {fail} | {rate}% | [test-report](../evaluation-results/{skill}/tester/{YYYYMMDD}/reports/test-report.md) |
| … | | | | | | | | | |
| | **合计** | | | **{total}** | **{pass}** | **{warn}** | **{fail}** | **{rate}%** | |

> 每个 Skill 的全部历史测试记录均展示，按报告生成时间倒序排列。

> 通过率 = (通过 + 告警) / 总用例数 × 100%

---

## 二、GitCode 问题单清单

共 {N} 个 open Issue。

> 排序规则：按创建时间倒序排列（最新在前）。仅展示 open 状态的 Issue。

| Issue | 状态 | 创建时间 | 责任人 | 标签 | 标题 |
|-------|------|---------|--------|------|------|
| [#{num}]({url}) | 🟢 open | {YYYY-MM-DD HH:MM:SS} | {name} @{login} | {labels} | {title} |

---

## 三、各 Skill 汇总文件

| Skill | 汇总文件 | Issue 数 | 有分析报告 | case-run 通过率 | tester 通过率 |
|-------|---------|---------|-----------|----------------|--------------|
| {skill} | [summary]({skill}_summary.md) | {count} | {count}（#{nums}） | {rate}% | {rate}% |
| | **合计** | **{total}** | **{total}** | **{rate}%** | **{rate}%** |

---
