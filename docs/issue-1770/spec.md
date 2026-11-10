# Spec: Issue #1770 — a2a_event 派发去重/幂等门禁

## Objective
编排侧 `a2a_event` 派发增加三段门禁，避免同一 Issue 在下游（qa）审查 running 期间重复派发上游（coding）导致会话空跑、浪费执行槽位（#2955 实证：qa seq 62528 running 期间两次派发 coding seq 62535 / 62540）。

## Scope
### In Scope
- 派发入口「同 Issue 同场景下游 running」判定：目标 agent 为上游且存在 running/dispatched 下游事件 → 跳过并留痕。
- `a2a_event` 时间窗键（issue + scene + target agent + 时间窗）去重：窗口内已有 completed 派发则丢弃并留痕；failed/cancelled/timeout 允许立即重试。
- 短时间窗内同 Issue 同目标 agent 多次派发 → 告警日志（可观测）。
- `force=true` 保留人工强制派发语义，可越过门禁。

### Out of Scope
- handoff 侧排队能力（#1667 已实现）。
- 技能文档侧去重口径（#1752 / #1672 / #1584）。
- 执行引擎与 worker 改造。

## Technical Approach
- 新增 `src/api/a2a_dispatch_guard.py`：场景/节点顺序解析、下游 running 查询、时间窗去重、活跃重复判定、窗口键派生、原子「复查+落库」、短窗告警、跳过留痕。
- `src/api/routes/webhook.py` 在既有活跃事件去重之后、事件落库之前调用门禁：
  1. 下游 running：按 Issue 当前场景 pipeline 节点顺序，若运行中事件 agent 位于目标 agent 之后 → 跳过；
  2. 时间窗去重：同 Issue + 同场景 + 同目标 agent 在窗口内已有 completed 派发 → 丢弃；failed/cancelled/timeout 允许立即重试；活跃事件由下一步拦截；
  3. 活跃重复：同 Issue + 同场景 + 同目标 agent 在短窗内已有 running/dispatched 事件且 context 不同 → 丢弃（force 可越过，queue 走 #1667 排队）。
  4. 短窗告警：同 Issue + 同目标 agent 窗口内派发次数（含 `event_skipped` 跳过次数）达到阈值 → `A2A_DISPATCH_ALERT` 告警日志。
- 门禁查询与 `append_event` 落库在同一 `db_lock` 内原子完成（并发同键只落一行）；跳过统一写 `event_skipped`（reason 可读，如 `downstream_running:<seq>` / `duplicate_key:<seq>` / `active_dispatch:<seq>`），响应返回 `skipped` / `reason` / `window_key` 等关联信息。
- 时间窗可配置：`A2A_IDEMPOTENCY_WINDOW_SECONDS`（默认 120）、`A2A_DISPATCH_STORM_WINDOW_SECONDS`（默认 600）、`A2A_DISPATCH_STORM_THRESHOLD`（默认 2）。

## Acceptance Criteria
- [x] qa running 时派发 coding 被跳过，`event_skipped` 原因可读；qa 结束后再次派发可正常执行。
- [x] 同一时间窗键（issue + scene + target agent + 时间窗）内 completed 派发重复被丢弃，`event_skipped` 可查；失败后可立即重试。
- [x] 短时间窗内同 Issue 同目标 agent 多次派发/跳过产生告警日志。
- [x] 无回归：既有 A2A 活跃去重/排队/force 语义（#587 / #1667 / #729）保持；跨流水线交交流程不变。
- [x] 新增测试覆盖三项门禁；相关既有测试通过。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 下游 running 卡死阻塞上游 | Med | 沿用现有 running 超时/生命周期机制；仅 running/dispatched 视为活跃 |
| 门禁误伤合法并发 | Med | 同 Issue + 同场景 + 同目标 agent；跨流水线不拦；短窗内活跃重复拦截、同上下文沿用 #1667 语义；`force=true` 可越过 |
| 时间窗去重拦截合法返工 | Low | 窗口默认 120s 可配置，超窗或 force 可重新派发；failed/cancelled/timeout 不拦截 |
| 跳过记录增长 | Low | 仅写 reason + 引用 seq，不重复 payload |