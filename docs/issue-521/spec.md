# Spec: Issue #521 — 重派 queued 任务副本收敛与回执一致性（开发版）

> 复用 Issue #521 既有 Spec（Issue Agent 发布，评级 ready）。目标分支：weekly-08-mys。

## Objective
修复 `POST /api/events/{seq}/redispatch`（`src/api/routes/events.py` `redispatch_event`）：
1. queued 任务重派不再"新建任务+保留旧 queued 行"（会产生两个可领取副本）；改为**同事务原子复用原任务行**（更新 runtime_id 等派发目标字段）。
2. 回执 `task_id` 返回**实际有效任务 ID**（queued 复用=原 ID；终态重建=新任务 ID），与事件详情最新任务一致。

## Scope
- In：redispatch_event queued 分支原子收敛；回执 task_id 一致性；回归测试（queued→重派→仅一个有效任务 + 回执一致性）。
- Out：#520 claim 精度；dispatched/running requeue 分支行为（保持现状）；worker 接单/执行逻辑。

## Acceptance Criteria
- queued 重派后同一事件下仅一个 status='queued' 任务（复用原行，不新建）
- 回执 task_id 与事件详情最新任务一致
- 保留场景/scope/角色/引擎池/串行约束（复用行原快照 env/prompt/pool 字段不动）
- dispatched/running requeue 分支与终态重建分支不回归
- 重派瞬间被领取的竞态：单事务 `UPDATE ... WHERE status='queued'` 守卫，失败 409 无半状态
