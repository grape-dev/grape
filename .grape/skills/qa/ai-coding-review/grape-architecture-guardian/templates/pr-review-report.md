[QA Agent] PR #{N} grape架构守护审查报告（head sha: {sha8}｜模式 {X}（{基线描述}）｜执行位置 {worker/build}｜{重启状态}，被中断他人任务 {N} 个）

## 总体结论

> 4 级别归类统计 + 合入建议（规则 2.0）

**级别统计**：{N} BLOCKED / {N} Critical / {N} Medium / {N} Informational

{✅ APPROVED：0 BLOCKED / 0 Critical，N Medium 全部经人工判断通过或不适用 / M Informational}
{⚠️ APPROVED WITH CONDITIONS：N Critical（可修复，列出条件）；Critical 不建议合入}
{❌ BLOCKED：N 项 BLOCKED 或未整改 Critical（列出具体技术原因）；不建议合入}

**合入建议**：BLOCKED/Critical → 不建议合入（打回整改）；Medium → 人工判断是否合入（已置 pending-review，如适用）；Informational → 不影响合入。

## 审查范围

- 仓库：{owner/repo}
- PR：#{N} — {title}
- 变更：{X} 文件 +{Y} -{Z}
- 模式：{A/B/C/D}（{base 分支} — {基线类型}）
- 执行位置：{worker 名称 / build 编号}
- 重启状态：{未执行重启 / 已执行重启}
- 被中断他人任务：{N} 个
- 适用 Gate：{Gate 1,2,4,6,7 / Gate 1,2,3,4,5,6,7 / ...}
- 2.0 规则类别：{A1-A9 / B1-B2 / C1-C3 / D1-D2（按变更涉及勾选）}

## 逐 Gate 审查明细

### Gate 1：Grape 核心架构约束

| # | 检查项 | 结论 | 证据定位 | 备注 |
|---|--------|------|----------|------|
| 1-1 | uvicorn 单 worker | ✅ 符合 | 启动脚本未指定 --workers | — |
| 1-2 | 跨进程状态走 db_lock | ✅ 符合 | — | — |
| 1-5 | Worker MANIFEST 完整 | ✅ 符合 | worker_package.py:30-50 | — |
| 1-6 | conftest.py 强制覆盖 | ✅ 符合 | tests/conftest.py:15 | os.environ[]= |
| 1-7 | conftest.py 清除 MySQL 参数 | ✅ 符合 | tests/conftest.py:17-18 | pop 循环 |
| ... | ... | ... | ... | ... |

### Gate 2：组织隔离与安全

| # | 检查项 | 结论 | 证据定位 | 备注 |
|---|--------|------|----------|------|
| ... | ... | ... | ... | ... |

### Gate 3：前端规范（仅前端仓库）

| # | 检查项 | 结论 | 证据定位 | 备注 |
|---|--------|------|----------|------|
| 3-1 | JS 版本号 bump | ✅/❌ | index.html:{line} | — |
| ... | ... | ... | ... | ... |

### Gate 4：Git 与提交规范

| # | 检查项 | 结论 | 证据定位 | 备注 |
|---|--------|------|----------|------|
| 4-1 | commit 全英文 | ✅ 符合 | commit {sha8} | — |
| ... | ... | ... | ... | ... |

### Gate 5：测试规范

| # | 检查项 | 结论 | 证据定位 | 备注 |
|---|--------|------|----------|------|
| 5-1 | 新增功能附带测试 | ✅/❌ | {test_file} | — |
| ... | ... | ... | ... | ... |

### Gate 6：数值证据审查

**magic_number_audit.py 扫描结果**：
- 扫描文件数：{N}
- 候选数值：{N}（high: {N}, medium: {N}）
- 无来源数值：{N}
- 已知注册表数值偏离：{N}

| 数值 | 文件:行号 | 类别 | 严重度 | 来源 | 判定 |
|------|-----------|------|--------|------|------|
| ... | ... | ... | ... | ... | ✅ 已注册 / ⚠️ INSUFFICIENT DATA |

### Gate 7：架构适配性

|| # | 检查项 | 结论 | 证据定位 | 备注 |
|---|--------|------|----------|------|
|| ... | ... | ... | ... | ... |

## 规则 2.0 审查明细（A/B/C/D 四类 16 条）

### 设计架构约束（A 类）

|| 规则 | 级别 | 结论 | 证据定位 | 备注 |
|------|------|------|----------|------|
|| A1 API REST 风格 | Critical | ✅/❌/N.A. | {file:line} | — |
|| A2 grape cli 开放 | Medium | ... | ... | ... |
|| A3 性能 ≥50tps | Critical | ... | ... | measured / INSUFFICIENT DATA |
|| A4 时延 <200ms | Critical | ... | ... | 同 A3 |
|| A5 OBS 存储 | BLOCKED | ... | ... | — |
|| A6 表名规则 | Critical | ... | {CREATE TABLE 处} | grape_t_xxx / grape_admin_t_xxx |
|| A7 大表分区 | Medium | ... | ... | — |
|| A8 模块化设计 | Medium | ... | ... | — |
|| A9 鉴权机制 | BLOCKED | ... | ... | — |

### 代码架构约束（B 类）

|| 规则 | 级别 | 结论 | 证据定位 | 备注 |
|------|------|------|----------|------|
|| B1 SQL 双模式 | BLOCKED | ... | {file:line} | sqlite+mysql |
|| B2 禁魔鬼数字 | Critical | ... | {脚本输出} | 复用 magic_number_audit |

### 可信约束（C 类）

|| 规则 | 级别 | 结论 | 证据定位 | 备注 |
|------|------|------|----------|------|
|| C1 隐私数据加密 | Medium | ... | ... | PBKDF2 / AES-GCM-256 |
|| C2 接口日志 | Critical | ... | ... | — |
|| C3 审计日志 | Critical | ... | ... | — |

### UI 架构约束（D 类）

|| 规则 | 级别 | 结论 | 证据定位 | 备注 |
|------|------|------|----------|------|
|| D1 国际化双语 | BLOCKED | ... | {JS 行 + zh/en key} | — |
|| D2 双主题风格 | BLOCKED | ... | {CSS/HTML 行} | Grape Default + Liquid Glass |

## 发现清单

（仅列出不符合项，符合项不重复。按级别排序：BLOCKED → Critical → Medium → Informational。）

### BLOCKED

（无 / 逐条列出）

### Critical

（无 / 逐条列出）

### Medium

（无 / 逐条列出；存在时标注「待人工判断是否合入（已置 pending-review）」）

### Informational

（无 / 逐条列出，不影响合入）

## 独立验证

- 测试运行：{pytest 结果 / 未运行（原因）}
- magic_number_audit：{结果摘要}
- grape_constraint_check：{结果摘要}

## 结论与建议

{APPROVED / APPROVED WITH CONDITIONS / BLOCKED}

**级别统计**：{N} BLOCKED / {N} Critical / {N} Medium / {N} Informational

**合入建议**：
- BLOCKED/Critical → 不建议合入，需修复后复审
- Medium → 人工判断是否合入（{已置 pending-review，等待用户审阅 / 无}）
- Informational → 不影响合入，记录备查

{下一步建议：无需修改 / 需修复 N 项 Critical / 需修复 N 项 BLOCKED / N 项 Medium 待人工判断}
