# Spec: Issue #4334 — watchdog 重派前强制校验 Issue 流水线终态与外部阻塞标记

> Target Branch: weekly-mys

## Objective
watchdog 在自动重派 coding 前，强制回读目标 Issue 的最新 pipeline 终态与
external-block 标记；命中「已完成（completed / qa-passed）」即跳过；同 Issue
自动重派受频次上限约束；重派请求必须携带非空原 context；跳过/拦截行为写入
结构化审计与事件流（含判据来源）。

## Scope
### In Scope
- 重派前查询目标 Issue 最新 pipeline 段与 external-block 标记，命中即跳过并留痕。
- 同 Issue watchdog 自动重派的时间窗/次数上限拦截与留痕。
- 重派 payload 原 context 非空校验，空 context 先回填原结论，仍为空则拦截。
- 跳过/拦截 reason + detail（判据来源）写入 `event_skipped` 与日志。

### Out of Scope
- #3861 / #2707 / #2912 等相邻重派路径的治理。
- external-block 标记本身的写入/维护逻辑。

## Technical Approach
1. 新增 `src/api/watchdog_redispatch_guard.py`：终态判定、external-block 识别、
   重派频次统计、原 context 回填与综合守卫。
2. 接入 `a2a_terminal_guard.guard_terminal_dispatch`：coding 派发在既有
   needs-human 终态判定之后复查 pipeline 终态/上限/context。
3. 接入 `patrol_compensate.RecheckHandler`：补偿链终检新增 pipeline 终态预检；
   `DispatchHandler` 显式声明 watchdog 重派标记并保证 context 非空。

## Acceptance Criteria
- [ ] qa 双轮通过 / 已完成 / external-block 的 Issue 不再被重派 coding。
- [ ] 同 Issue 重派超上限被拦截并留痕。
- [ ] 重派携带非空原 context；空 context 不覆盖既有产物。
- [ ] 跳过/拦截在 `event_skipped` 与日志中可追溯（含判据来源）。
- [ ] 现有测试通过，新增测试覆盖终态预检、上限拦截、context 校验、留痕。