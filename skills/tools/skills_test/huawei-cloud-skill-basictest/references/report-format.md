# 报告格式规范

## 1. 单个 Skill 汇总报告

文件名: `{skill-name}_summary.md`

### 结构

```markdown
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
```

### 字段说明

#### 测试统计表

| 字段 | 说明 |
|------|------|
| 序号 | 从 1 递增，每行对应一次历史测试记录 |
| Skill | skill 名称 |
| 归属服务 | 来自 skill-service-map.md 映射表 |
| 报告生成时间 | 测试报告的生成日期，按此字段倒序排列 |
| 总用例数 | 通过 + 告警 + 失败 |
| 通过 | ✅ 用例数 |
| 告警 | ⚠️ 用例数 |
| 失败 | ❌ 用例数 |
| 通过率 | (通过 + 告警) / 总用例数 × 100%，保留 1 位小数 |
| 测试报告 | case-run 链接到 `整合测试报告.md`，tester 链接到 `test-report.md`；无报告文件时为 — |

#### GitCode 问题单表

| 字段 | 说明 |
|------|------|
| Issue | Issue 编号，链接到 GitCode Issue 页面 |
| 状态 | 仅显示 🟢 open（closed 状态的 Issue 不展示） |
| 创建时间 | Issue 创建时间，按此字段倒序排列 |
| 责任人 | Issue 指派人 |
| 标签 | Issue 标签 |
| 标题 | Issue 标题 |

---

## 2. 总览汇总报告

文件名: `总览_summary.md`

### 结构

```markdown
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
```

### 合计行说明

- 测试统计总览的合计行：序号列为空，Skill 列为 `**合计**`，数值列加粗，测试报告列为空
- 合计行汇总全部 Skill 的全部历史测试记录（非仅最新一次）
- 合计通过率 = (合计通过 + 合计告警) / 合计总用例数 × 100%
- 各 Skill 汇总文件表的合计行：汇总各列数值，通过率取加权平均
- "case-run 通过率" / "tester 通过率" 列显示该 Skill 最新一次测试的通过率

### 问题单筛选与排序规则

- **仅展示 open 状态的 Issue**，closed 状态的 Issue 不出现在问题单清单中
- **按创建时间倒序排列**（最新创建的在前）
- 适用于单个 Skill 报告和总览报告的问题单清单

### 无数据 Skill 处理

- 序号显示为 `—`
- 数值列全部显示为 `—`
- 在通过率公式注释后追加说明，如 `> {skill} 暂无本地测试目录，数据待补充。`

---

## 3. 评测数据来源

### case-run 测试报告

位置: `evaluation-results/{skill-name}/case-run/{YYYYMMDD}/`

解析方式:
1. 优先解析 `整合测试报告.md` 中的用例结果表格
2. 回退: 按目录名后缀统计（通过/告警/失败/跳过）

提取字段:
- 总用例数 = 通过 + 告警 + 失败
- 通过 = ✅
- 告警 = ⚠️
- 失败 = ❌
- 通过率 = (通过 + 告警) / 总用例数 × 100%
- 测试报告链接 = `../evaluation-results/{skill-name}/case-run/{YYYYMMDD}/整合测试报告.md`

### tester 测试报告

位置: `evaluation-results/{skill-name}/tester/{YYYYMMDD}/`

解析方式:
1. 优先解析 `test-report.json` 的 `overall_statistics`
2. 回退: 解析 `test-report.md` 的统计表格

测试报告链接:
- 优先链接 `../evaluation-results/{skill-name}/tester/{YYYYMMDD}/reports/test-report.md`
- 回退链接 `../evaluation-results/{skill-name}/tester/{YYYYMMDD}/test-report.md`

### 问题单分析报告

位置: `evaluation-results/{skill-name}/问题单/Issue-{num}-分析与实测报告.md`

存在检查: 在"各 Skill 汇总文件"表的"有分析报告"列中统计

### GitCode Issue

API: `https://gitcode.com/api/v5/repos/{owner}/{repo}/issues`

认证: `Authorization: Bearer {token}`

Token 获取顺序:
1. 命令行参数 `--issue-token`
2. 环境变量 `GITCODE_TOKEN`
3. git remote URL 中的凭据

筛选: 仅展示 `state=open` 的 Issue，closed 状态的 Issue 不纳入问题单清单
