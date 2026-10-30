# Plan: Issue #521 实现方案

## 缺陷定位（src/api/routes/events.py redispatch_event）
- 分支条件 `if task_id and task_status in ("dispatched","running")` 之外全部落入"终态/缺失"else 分支——**queued 也落入该分支**：
  - 旧行保持 queued（可领取），`enqueue_task` 又新建一行 queued → 双副本；
  - 末尾回执 `task_id or "(new)"` 返回**旧任务 ID**（终态重建时同样指旧）。
- `local_only` 分支同样无条件 `enqueue_task` 新建，queued 旧行残留。

## 修复方案（Spec 决策 A：复用原任务行）
1. 新增独立 queued 分支（`elif task_id and task_status == "queued"`）：
   - 引擎重解析：plugin+pool_tag → tag+scope 在线引擎重选（与 dispatched/running 分支一致，选不到置 0 贴证广播）；否则 task.runtime_id → session 主引擎 → pick_engine fallback 链（与终态重建分支一致）。
   - **原子收敛**：单事务 `UPDATE agent_tasks SET runtime_id=?, started_at=NULL, completed_at=NULL, exit_code=NULL WHERE id=? AND status='queued'`；rowcount=0（窗口期被领取）→ 409，无半状态。
   - 本机 direct 引擎（cid=0 且 rid>0）→ start_direct_agent 复用原行触发直跑。
   - env/prompt/skill/pool 快照不动（queued 行即首次派发原快照，无漂移）。
2. local_only 分支：旧任务 status=queued 时同事务复用原行（UPDATE runtime_id=原机 rid），不再 enqueue 新行。
3. 回执一致性：引入 `_effective_task_id`（初始=task_id）；终态重建分支 enqueue 后置为新任务 ID；末尾回执/log 一律用 `_effective_task_id`。

## 测试计划（tests/test_issue_521_redispatch_queued.py，内存库模式参照 test_issue_253_engine_binding.py）
- UT-1 plugin 模式 queued→重派：复用原行（task_id 不变、runtime_id 重选）、仅一个 queued、enqueue_task 不被调用
- UT-2 终态重建回执：返回新任务 ID（非旧 ID）
- UT-3 local_only queued：复用原行、仅一个 queued、回执=原 ID
- UT-4 dispatched/running 回归：仍走 requeue、回执=原 ID
- UT-5 竞态守卫：DB 行已被领取（running）而快照读为 queued → 409
