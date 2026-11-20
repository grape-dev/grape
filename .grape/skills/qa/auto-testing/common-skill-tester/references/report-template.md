# Skill 综合测试报告

> 本报告由 common-skill-tester 自动生成，检查点/测试点/审计规则均动态加载自在线地址。

---

## 一、综合概览

| 项目 | 值 |
|------|-----|
| 报告生成时间 | {TIMESTAMP} |
| 目标 Skill | {SKILL_NAME} |
| Skill 路径 | {SKILL_PATH} |
| 执行模式 | {MODE}（all = lint + test + audit） |
| lint 检查点来源 | {CHECKS_URL} |
| lint 检查点版本 | {CHECKS_DOC} |
| test 测试点来源 | {TESTS_URL} |
| test 测试点版本 | {TESTS_DOC} |
| audit 规则来源 | {RULES_URL} |
| audit 规则版本 | {RULES_DOC} |

### 总体统计摘要

| 套件 | 总数 | ✅ pass | ❌ fail | ⚠️ warn | ⏭️ skip | 评分 |
|------|------|--------|---------|---------|---------|------|
| Lint 规范检查 | {LINT_TOTAL} | {LINT_PASS} | {LINT_FAIL} | {LINT_WARN} | {LINT_SKIP} | {LINT_SCORE} |
| Test 功能测试 | {TEST_TOTAL} | {TEST_PASS} | {TEST_FAIL} | {TEST_WARN} | {TEST_SKIP} | {TEST_PASS_RATE}% |
| Audit 安全审计 | {AUDIT_TOTAL} | {AUDIT_PASS} | {AUDIT_FAIL} | {AUDIT_WARN} | {AUDIT_SKIP} | {AUDIT_SCORE} |

---

## 二、综合判定与评分

### 综合评分

| 项目 | 值 |
|------|-----|
| 综合分 | **{OVERALL_SCORE}** / 100 |
| 星级 | {STARS} |
| 等级 | {GRADE} |
| Lint 总分 | {LINT_SCORE} / 100 |
| Test 通过率 | {TEST_PASS_RATE}% |
| Audit 安全分 | {AUDIT_SCORE} / 100 |
| Critical fail（lint） | {CRITICAL_FAIL} |
| P0 必测项 fail（test） | {P0_FAIL} |
| CRITICAL 严重发现（audit） | {SEC_CRITICAL} |

**综合分公式：** `综合分 = round((Lint总分 + Test通过率 + Audit安全分) / 3)`

**星级标准：**
- 90-100: ⭐⭐⭐⭐⭐ 优秀
- 75-89: ⭐⭐⭐⭐ 良好
- 60-74: ⭐⭐⭐ 合格
- 40-59: ⭐⭐ 需改进
- 0-39: ⭐ 不合格

### 综合判定

**判定结果：{VERDICT}**

| 判定 | 条件 |
|------|------|
| ✅ 通过 | 三套评分均 ≥ 95，且无 lint Critical fail、无 test P0 fail、无 audit CRITICAL 发现 |
| ⚠️ 有条件通过 | 无上述三类 fail，但存在 warn 或任一评分在 75-94 之间（条件项——建议随报告转交下游，不阻断合并） |
| ❌ 不通过 | 存在上述任一 fail 类型，或任一评分 < 75（列出未达标项及原因） |

> 门禁语义：lint Critical / test P0 / audit CRITICAL 均为**一票否决**项，对应 grape-skills 等质量门槛中"三套评分全 ≥95 才合格"的要求。

---

## 三、Lint 检查结果

### 3.1 检查概览

| 项目 | 值 |
|------|-----|
| 检查时间 | {TIMESTAMP} |
| 检查点版本 | {CHECKS_DOC} |
| 检查项总数 | {LINT_TOTAL} |

### 3.2 评分

| 项目 | 值 |
|------|-----|
| 总分 | **{LINT_SCORE}** / 100 |
| Critical fail | {CRITICAL_FAIL} |
| High fail | {HIGH_FAIL} |
| Medium fail | {MEDIUM_FAIL} |
| Low fail | {LOW_FAIL} |
| warn | {LINT_WARN} |

**评分公式：** `总分 = 100 - (Critical_fail × 15) - (High_fail × 8) - (Medium_fail × 4) - (Low_fail × 2) - (warn × 1)`

### 3.3 必改项说明（Critical fail）

{LINT_CRITICAL_SECTION}

### 3.4 建议修改说明（非 Critical fail + warn）

{LINT_SUGGEST_SECTION}

### 3.5 通过项清单

{LINT_PASS_SECTION}

### 3.6 跳过项清单

{LINT_SKIP_SECTION}

### 3.7 详细检查记录

| ID | 检查项 | 严重级别 | 自动化 | 检查结果 | 证据/备注 |
|----|--------|----------|--------|----------|-----------|
{LINT_DETAIL_ROWS}

---

## 四、功能测试结果

### 4.1 测试概览

| 项目 | 值 |
|------|-----|
| 测试时间 | {TIMESTAMP} |
| 测试点版本 | {TESTS_DOC} |
| 测试用例总数 | {TEST_TOTAL} |

### 4.2 通过率与评级

| 项目 | 值 |
|------|-----|
| 通过率 | **{TEST_PASS_RATE}**% |
| pass / 有效用例 | {TEST_PASS} / {TEST_EFFECTIVE} |
| P0 必测项结果 | {P0_RESULT} |

**通过率公式：** `通过率 = pass / (总用例数 - skip) × 100%`

### 4.3 P0 必测项详情

{TEST_P0_SECTION}

### 4.4 失败项分析

{TEST_FAIL_SECTION}

### 4.5 警告项分析

{TEST_WARN_SECTION}

### 4.6 通过项清单

{TEST_PASS_SECTION}

### 4.7 跳过项清单

{TEST_SKIP_SECTION}

### 4.8 详细测试记录

| ID | 用例 | 方向 | 优先级 | 自动化 | 输入/场景 | 预期 | 实际 | 状态 | 证据/备注 |
|----|------|------|--------|--------|-----------|------|------|------|-----------|
{TEST_DETAIL_ROWS}

### 4.9 测试分组汇总

| 分组 | 用例数 | pass | fail | warn | skip | 通过率 |
|------|--------|------|------|------|------|--------|
{TEST_GROUP_SUMMARY_ROWS}

---

## 五、安全审计结果

### 5.1 审计概览

| 项目 | 值 |
|------|-----|
| 审计时间 | {TIMESTAMP} |
| 规则版本 | {RULES_DOC} |
| 审计规则总数 | {AUDIT_TOTAL} |
| CWE 模式数 | {CWE_COUNT} |

### 5.2 安全评分

| 项目 | 值 |
|------|-----|
| 安全评分 | **{AUDIT_SCORE}** / 100 |
| CRITICAL fail | {SEC_CRITICAL} |
| WARNING fail | {SEC_WARNING} |
| INFO fail | {SEC_INFO} |
| warn | {AUDIT_WARN} |
| CWE 覆盖率 | {CWE_COVERAGE}% |

**评分公式：** `安全评分 = 100 - (CRITICAL_fail × 20) - (WARNING_fail × 8) - (INFO_fail × 2) - (warn × 1)`

### 5.3 严重发现（CRITICAL fail）

{AUDIT_CRITICAL_SECTION}

### 5.4 警告发现（WARNING fail/warn）

{AUDIT_WARNING_SECTION}

### 5.5 提示发现（INFO fail/warn）

{AUDIT_INFO_SECTION}

### 5.6 CWE 漏洞模式检测结果

{AUDIT_CWE_SECTION}

### 5.7 通过项清单

{AUDIT_PASS_SECTION}

### 5.8 详细审计记录

| ID | 检测项 | 严重级别 | 关联 CWE | 检测结果 | 证据/备注 |
|----|--------|----------|----------|----------|-----------|
{AUDIT_DETAIL_ROWS}

---

## 六、覆盖矩阵（脚本 × 三套检查）

> 保证被测 skill 每个可执行入口/脚本都被至少一个套件检查到——无死角。

| 脚本/入口 | 路径 | Lint 语法/规范 | Test 功能 | Audit 安全 | 覆盖状态 |
|-----------|------|---------------|-----------|------------|----------|
| 主执行脚本 | scripts/xxx.sh | ✅ bash -n 通过 | ✅ 冒烟实测 | ✅ CWE 扫描 | 全覆盖 |
| 辅助脚本 | scripts/init.sh | ✅ | ⏭️ 无独立入口（随主脚本）| ✅ | 全覆盖 |
| — | — | — | — | — | — |

**覆盖规则**：每个脚本至少被 Audit 覆盖（语法可执行或静态扫描）；Test 覆盖其可实测入口；**任何脚本无任何覆盖 → 覆盖缺失（记 warn 并在结论标注）**。无脚本的 skill → 本表一行「无可执行入口——静态文档检查」注明。

*报告由 common-skill-tester 自动生成 · 检查点/测试点/审计规则动态加载自在线地址*