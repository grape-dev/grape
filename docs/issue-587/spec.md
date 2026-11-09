# Spec: Issue #587 — 任务终态后事件不收敛 & A2A 去重无限期阻断

> 本文档为 Issue 分诊阶段已发布 Spec 的开发落地副本（来源：Issue #587 Spec 评论，评级 ready）。
> 开发基线分支：`weekly-09-ljt`（场景注入值，权威）。关联 #260 / #537（状态收敛主题）。

## Objective

- 任务进入终态（completed/failed/cancelled）后，关联事件在有限时间内自动收敛为对应终态，无需人工干预。
- A2A 去重仅针对真正活跃（running/dispatched）事件；已终态事件不阻断同角色新交接。
- 状态回报失败有可见记录（task_id/event_seq/原因/重试次数）与补偿重试入口；重复回报幂等。
- 遗留 running 事件可通过可审计操作（终止/重派）修复，操作强制留痕（actor/time/target/动作）。
- 取消不被当作业务通过；保留 Agent 实际业务结论。

## Scope

### In Scope
- 任务终态 → 关联事件终态的级联收敛（completed/failed/cancelled 三态映射）
- A2A 去重判定键收敛为「事件活跃(running/dispatched)」并加回归测试锁定
- 状态回报失败的可观测记录 + 补偿重试入口
- 管理端终止/重派操作强制审计留痕

### Out of Scope
- Worker 侧业务逻辑（退出码语义）改造
- 全量历史 running 事件批量回溯修复（仅提供修复路径）
- 跨仓转交

## Acceptance Criteria

- [ ] 任务进入 completed/failed/cancelled 后，关联事件在有限时间内收敛为对应终态
- [ ] 指向已终态事件的同角色新交接不再被 duplicate_a2a 阻断
- [ ] 状态回报失败有可见记录与重试/补偿入口；重复回报幂等
- [ ] 遗留 running 事件可通过可审计操作修复，操作留痕（actor/time/target/动作）
- [ ] 取消不被当作业务通过；保留 Agent 实际业务结论
- [ ] 现有测试通过 + 新增测试覆盖上述场景
