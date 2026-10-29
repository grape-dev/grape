# Spec: Issue #495 — 调度侧 healthy 门控 + 连续失败熔断

> 来源：Issue #495（评论 196643584 Spec + 196675863 修订确认）| 目标分支：weekly-08-mys

## 需求

1. **healthy 门控**：引擎池调度侧 claim 候选过滤增加 `healthy=1` 门控——`agent_runtimes.healthy != 1` 的 runtime 不派发/认领新任务。
2. **连续失败熔断**：同一 runtime 连续任务失败（含 rc=127 / 引擎启动失败）达到阈值 N（默认 3，可配置）自动置 `offline` 并写 WARNING 日志；置 offline 后可人工恢复。
3. **成功清零**：任务成功（exit_code=0）后该 runtime 的连续失败计数重置。

## 范围

- In：`src/runtime/service.py` 的 `claim` / `claim_by_tag` 认领路径；`fail_task` / `complete_task` 终态钩子；新增熔断模块与配置项；单测。
- Out：线上 6280 worker 重装 CLI / 置 offline（SRE 运维动作）；健康探测机制重构；事件 32824 跟踪。

## 验收标准

- [ ] healthy=0 的 runtime 不被派发新任务（单测覆盖）
- [ ] 同一 runtime 连续失败达阈值自动置 offline 并记录日志（单测覆盖）
- [ ] 任务成功后连续失败计数重置（单测覆盖）
- [ ] 所有现有测试通过
- [ ] 新增测试覆盖门控与熔断逻辑
