# Plan: Issue #4340 — 停机汇总行计数口径

## 改动点（单文件 + 单测试文件）

| 位置 | 改动 |
|------|------|
| `src/domains/runtime/agents/executor.py` `terminate_active_agents` | 新增 `_failed_direct` 计数；DIRECT UPDATE 补 `status IN ('running','dispatched')` 守卫并按 `rowcount` 累加；汇总行改中性三字段 + 条件旧文案 |
| `tests/runtime/test_issue_4340_shutdown_sweep_counts.py`（新增） | REMOTE-only / DIRECT-only / 混合 / 竞态 0 行 / 无候选 五用例，`caplog` 断言日志字段 |

## 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 汇总文案形态 | 只留中性行 / 中性行 + 条件旧行 | 中性行 + 条件旧行 | 消除虚报，同时让旧语义检索在真失败时仍准确，巡检可平滑迁移 |
| 计数来源 | 循环计数 / UPDATE `rowcount` | `rowcount` | 对应 DB 实况，覆盖并发自报 0 行场景 |
| DIRECT UPDATE 守卫 | 不加 / 补 `status IN (...)` | 补守卫 | Issue 期望「0 行不计」；无守卫时 rowcount 恒为 1，且会把并发自报已终态的任务覆写成 failed（数据损坏） |
| 逐条明细行 | 保留 / 删除 | 保留 | 现状可用，删除会丢明细 |

## 与 Spec 的差异说明

Spec「Technical Approach 2」称「UPDATE 自带 `status IN ('running','dispatched')` 条件」——
基线代码实际无此条件（`WHERE id=?`）。为使「0 行不计」成立、并避免覆写并发自报终态，
本次补上该守卫（语义与既有 REMOTE 分支一致）。此差异在 PR body 中标注。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 旧巡检按 `as failed` 聚合，切换后历史/新增口径不一致 | Med | PR body 标注切换点；旧行仅在真失败时出现 |
| `rowcount` 在 MySQL/SQLite 语义差异 | Low | 守卫后命中即变更，两后端均以受影响行数计，不新增查询 |
| 与 #4303 同窗口同数字再混淆 | Low | 不同文件独立 PR，PR body 引用说明 9 个任务实为 `pending_self_report` |

## 同口径审计（结论随 PR，不扩大改动）

- `cleanup.py:852` / `cleanup.py:1016`：数字取 `UPDATE rowcount`、尾随列表却是候选集，两数可能不等 → 建议另开 Issue。
- `executor.py:293` `terminated {len(procs)}`：进程数语义正确，不改。