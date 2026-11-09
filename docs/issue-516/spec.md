# Spec: Issue #516 — agent 任务 3600s 硬超时导致长耗时 coding 子任务强制失败

> 承接 Issue Agent 已发布 Spec（跨流水线 VOD 复核 ready）。目标分支：weekly-08-mys。

## Objective
- 任务超时上限可配置（全局/per-repo/per-skill，coding 默认 ≥7200s）+ 超时失败有限次自动重试 + 进度 checkpoint + 超时失败聚合观测。
- 根因：worker 侧 `_wrap` 中 `_env_float("RUNTIME_TASK_TIMEOUT", 3600)` 为全局唯一硬编码超时，长耗时 coding 任务跑满 1h 即被强制失败（2026-10-23 起 36 个 agent_task 触顶）。

## Scope
- M1：超时上限配置化——配置读取链（显式任务 env > per-skill > per-repo > agent 默认 > 全局 env > 3600），coding 默认 7200s；其余 agent 默认值不回退（保持 3600s）。
- M2：超时失败有限次自动重试（默认 1 次，可配置）；执行期周期性进度快照（checkpoint，原子落盘），重试注入快照上下文续跑。
- M3：`timed out after` 失败按仓库维度聚合指标（`task_timeout`）+ 告警规则。

### Out of Scope
- 任务规模预估动态超时；任务自动分片；#470/#282/#378/#266 各自独立范围。

## Acceptance Criteria
- [ ] 超时上限可配置（全局/per-repo/per-skill），coding 默认 ≥7200s，未配置路径行为不回退
- [ ] 超时失败自动重试 ≥1 次
- [ ] 超时前周期输出进度快照，重试可从快照续跑而非从零开始
- [ ] 超时失败按仓库聚合可观测（统计+告警）
- [ ] 现有测试通过；新增测试覆盖配置优先级、重试、快照续跑、聚合统计
