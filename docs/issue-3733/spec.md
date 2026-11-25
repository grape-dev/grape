# Issue #3733 需求文档（编码侧收敛）

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-22
> 权威 Spec：Issue #3733 评论 202816767（Issue Agent，评级 ready）。本文只做编码口径收敛，不新增需求。

## 目标

让「已启用但指令为空」的 Autopilot 任务从「每 tick 刷 WARNING 且永不派发」变为：

1. 显式不可派发状态落库（状态 + 原因 + 标记时间），任务页可见原因与补齐入口；
2. 状态变更只留痕一次（多 tick / 多副本都不再线性增长日志）；
3. 手动触发空指令任务改为写状态 + 明确原因，**不建 execution、不写 last_run_at**；
4. 补齐非空指令后自动恢复 cron 派发；
5. 存量 enabled 且空指令行一次性幂等自检标记（**不改 `enabled`**）。

## 现状（已在基线核对）

- `src/domains/runtime/engine/scheduler.py`
  - `_get_enabled_tasks()`（L1110-1138）：空指令行 `continue` 跳过，循环结束后**每次调用**打一条 WARNING；
  - `_execute_task()`（L1395-1399）：手动触发命中空指令只 `log.warning` 后 `return`；
  - 调度扫描间隔常量 `_SCHEDULER_INTERVAL = 60`（秒）。
- `autopilot_tasks` 现有列（`src/domains/mgmt/schema_ddl.py` L334）：无任何「不可派发原因 / 健康状态」列。
- 线上该 WARNING 1 小时 240 条 ≈ 每 15s 一条，与 60s 扫描间隔不符 → 存在多个持调度器副本，
  仅进程内去重无法让稳态归零。

## 范围

### In

- `autopilot_tasks` 新增状态三列 + 补列迁移 + 存量一次性幂等自检；
- mgmt 域列白名单 / 可移植查询白名单纳入新列；
- 调度器跳过路径：状态落库 + 状态变更留痕一次；
- 手动触发路径：写状态 + 明确原因，零 execution、零 `last_run_at`；
- `_row_to_task` 出参暴露不可派发原因；保存非空指令清状态；
- 任务页（`AutopilotView.vue`）列表 / 详情展示原因与补齐入口；
- 单测覆盖跳过 / 状态标记 / 手动触发拒绝 / 迁移幂等。

### Out

- 为 `id=6` 编写巡检指令内容（需业务方提供，不得编造）；
- 停用 / 删除任务决策、历史 execution 与 agent_task 清理；
- 该 WARNING 之外其它 Autopilot 告警项重构。

## 验收标准

- [ ] 补齐指令后按 cron 正常派发；未补齐时为「显式不可派发」，任务页可见原因
- [ ] 存量自检幂等：重复执行不重复写入；指令非空任务不受影响
- [ ] 同一进程多 tick 只留痕一次；多副本下 WARNING 总量不随 tick / 副本线性增长（稳态 0 条）
- [ ] 手动触发空指令任务：不建 execution、不写 `last_run_at`、返回明确原因
- [ ] `id=6` 的指令内容不在本 Issue 内编造（界面给补齐入口即可）