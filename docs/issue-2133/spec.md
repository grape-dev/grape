# Spec: Issue #2133 — 引擎恢复加回闭环 + spawn_failed 派单缺口 + 任务绑定对账

> 权威 Spec：Issue 评论 id=200254599（Developability: ready）。
> 目标分支：`arch-weekly-11-zj`（场景开发基线）。

## Objective

闭环 #1982 第 6 项遗留（PR !1202 已落 1-5 项）：在 spawn_failed 三态语义之上补齐
「挡派单 → 时间窗告警/降级 → 恢复自动加回场景 + 前端一键兜底 → 加回时任务绑定对账」。

## Scope

### In Scope
1. 认领闸门：`_has_verified_executor` 显式拒绝 `spawn_failed`（不再被 `worker_version` 短路）。
2. 时间窗兜底：连续 spawn_failed 次数/时长追踪，达阈值强告警，超第二阈值标记 degraded（不摘除）。
3. 恢复加回：探活连续 M 次 ok + 心跳正常 → 按解绑审计自动重建场景绑定；前端一键加回兜底。
4. 加回对账：已取消任务回写终态；worker 从未收到的 dispatched 悬挂释放回 queued；
   busy_slots 收敛；全过程审计留痕（幂等）。
5. 按 #1982「复现」章节全链路回归 + 新增单测。

### Out of Scope
- 派单调度策略/路由优先级；凭据发放；监控告警规则平台化；PR !1202 CI/文档（#2134）。

## Acceptance Criteria
- [x] `spawn_failed` 期间引擎不被派单；探活恢复 ok 后自动恢复认领
- [x] 持续 spawn_failed 达阈值有强告警、超第二阈值标记 degraded，全程不静默
- [x] 引擎恢复后自动加回场景；前端一键入口可用
- [x] 加回触发绑定对账：已取消任务回写终态 + 审计 + 通知留痕；对账幂等
- [x] #1982 复现链路回归：无引擎误摘除、无任务误取消、无反复派单失败
- [x] 现有测试通过（与本改动相关的 runtime/mgmt 用例）

## Key Decisions
| 决策 | 选择 | 理由 |
|------|------|------|
| 时间窗动作 | 告警 + 超时标记 degraded，不自动摘除 | #1982 教训是误摘除；只挡不告警会静默不接单 |
| 阈值默认值 | 连败 10 次或 10 分钟告警；30 分钟 degraded；恢复 M=3 次 ok | 全部 env 可调（`RUNTIME_SPAWN_FAIL_WARN_*`/`RUNTIME_SPAWN_FAIL_DEGRADE_SECONDS`/`RUNTIME_RECOVER_OK_STREAK`） |
| 加回方式 | 自动为主 + 前端一键兜底 | 自动与手动复用同一实现，幂等 |
| 黑名单形态 | 状态驱动（不持久化） | 探活 ok 自动出黑名单，避免二次悬挂 |