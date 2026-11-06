# Spec: Issue #1105 — 终态事件缺 completed_at（终态⇔completed_at 不变量被打破）

> 来源：Issue Agent Spec（Issue 评论 2026-11-05）| Coding 整理

## Objective
- 修复事件写入路径：所有终态事件（completed/failed/cancelled）必须落 `completed_at`，恢复「终态 ⇔ completed_at 非空」不变量。
- 现状：dev 库 136 条终态事件缺 completed_at，近 7 天 433 条中 51 条缺失（≈12%），写入路径持续漏写，属 P1 缺陷。

## Scope
### In Scope
- 终态事件写入路径统一补写 `completed_at`（服务端时间）。
- 存量数据幂等回填（取 created_at，缺省当前时间）。
- 防御性校验：终态写入时强制 completed_at 非空。

### Out of Scope
- 事件模型/表结构重构、其他字段语义变更、查询/展示层优化。

## 实现路径（Coding 考察结论）
梳理全部 `UPDATE events SET status=...` 终态写入点后，漏写 completed_at 的路径共 3 处：
1. `src/api/event_store.py update_event_status()`：completed/cancelled 终态走 else 分支，仅 `SET status=?`，不写 completed_at（failed 分支已在 #476 修复）。
2. `src/api/event_store.py update_event_complete()`：`completed_at: str = ""`——调用方传空串时直接落空值。
3. `src/api/routes/issues.py admin_confirm_issue_done` 收敛分支：`UPDATE events SET status='completed'` 不带 completed_at。

其余直接写终态的点（`src/runtime/service.py:2098-2106/3052`、`src/runtime/cleanup.py:371-502`）均已携带 completed_at。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 写入点 | 在 3 处漏写点收口 + 保留各处现有写法 | 改动最小；统一出口重构超出本 Issue 范围 |
| 回填时间 | created_at（events 表无 updated_at 列；created_at 为空则用当前时间） | 保留统计可用性，幂等可重跑 |
| 兜底策略 | 强制赋值（COALESCE/CASE 兜底） | 不变量优先 |

## Acceptance Criteria
- [ ] 事件进入 completed/failed/cancelled 任一终态时，completed_at 均非空
- [ ] 回填迁移幂等：重跑无副作用
- [ ] 终态 ∧ completed_at 为空的存量记录全部补齐
- [ ] 新增测试覆盖三类终态路径与回填幂等；所有现有测试通过
