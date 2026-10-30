# Plan: Issue #587 实现方案

## 根因定位（基于 weekly-09-ljt 基线代码走查）

1. **取消路径无事件级联**：`src/runtime/service.py::cancel_task` → `update_task_status(task_id,"cancelled")`
   只写 agent_tasks，不回写 events → 任务 cancelled 后事件仍 running。
   `routes/runtime.py /api/runtime/tasks/{id}/cancel` 即管理端取消入口，完整复现 Issue 描述。
2. **直接判死路径同样绕过事件**：`_fail_task_pool_unreachable/_unhealthy`、
   `cleanup.sweep_stuck_agent_tasks_once` 等直接 `UPDATE agent_tasks SET status='failed'`，事件不收敛。
3. **A2A 去重键已正确**（`webhook.py` 两处均 `status IN ('running','dispatched')`），
   但缺乏回归测试锁定——事件 running 被卡时仍会无限期阻断，收敛修复（1/2）落地后即解除阻断。
4. **回报失败不可见**：`service._ingestion_loop` 落库失败仅打日志，无 task 级可观测记录；
   complete/fail 补报失败后无服务端补偿入口。
5. **终止/重派无审计留痕**：`routes/events.py terminate/redispatch` 无审计事件（actor/time/target/动作）。

## 设计

### 1. 终态级联收敛（service.py）

`update_task_status` 写终态时同步级联事件（事务内，幂等）：

```
task terminal status → event status（仅当事件仍处于 pending/running/dispatched）
  cancelled → cancelled  result="任务已取消：级联收敛（cancel 不作为业务通过）"
  failed    → failed     result="任务失败：级联收敛（update_task_status 补偿）"
  completed → completed  result="任务已完成：级联收敛（回报丢失补偿）"
```

终态保护与 `update_event_status` 一致：已终态事件不被另一终态覆盖。
`complete_task/fail_task` 的主路径（update_event_complete 详写）不受影响——级联在事件已终态时 no-op。

### 2. sweeper 超时探活收敛（cleanup.py）

`sweep_stuck_events_once` 对「无活跃任务支撑」的超时事件，先查 agent_tasks 最近任务：
- 最近任务为终态 → 级联到该终态（cancelled→cancelled / completed→completed），不再一律 failed；
- 无任务记录 → 维持既有 failed + 根因文案。

### 3. 补偿重试入口（routes/runtime.py）

新增 `POST /api/runtime/tasks/{task_id}/converge-event`（管理端）：
调 `service.converge_event_to_task(task_id)`——按任务当前终态强制收敛事件并返回前后状态，
作为「回报丢失」的显式补偿入口（幂等，可重复调用）。

### 4. 回报失败可观测（service.py）

`_ingestion_loop` 落库失败 → 写 agent_tasks.errors 结构化记录：
`{"error_code":"REPORT_INGESTION_FAILED","reason":...,"retries":N,"ts":...}`（追加合并，[-5:] 截断）。

### 5. 终止/重派审计留痕（routes/events.py）

`terminate_event/redispatch_event` 末尾 `append_event(event_type="admin_action", status="completed")`，
payload 携带 actor/time/target_seq/action，summary 人读。

### 6. A2A 去重回归锁定

抽取 `webhook.py::_has_active_a2a_event(repo_key, issue_id, agent_type, scene_id=None)`，
两处去重点（同 issue 同 agent / 跨流水线）统一调用；测试锁定「终态事件不阻断、活跃事件阻断」。

## 测试策略

`tests/test_issue_587_event_convergence.py`（sqlite 内存基线，模式复用 test_terminate_event.py）：
- cancel_task/update_task_status(failed) → 事件级联
- complete_task 幂等（二次上报不覆盖终态）
- converge-event 补偿端点
- _has_active_a2a_event 去重键
- terminate/redispatch 审计事件
- sweeper 终态映射
