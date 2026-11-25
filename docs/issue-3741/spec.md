# Spec: Issue #3741 — Autopilot 任务无进展轮询死循环：exec 判 failed 后仍跑 36 分钟

## Objective

消除「autopilot exec 已判 failed、底层 agent 任务仍在空转」的状态机不自洽：

- 无实质进展的任务在预算内收口为 `failed(reason=no_progress)`，error 带重复命令指纹与起止步号；
- exec 超时分支主动终止底层 agent 任务并释放独占引擎槽位；
- periodic sweep 覆盖「exec 已终态 ∧ agent_tasks 仍活跃」的反向残留。

## In Scope

- `task_activity.py`：新增「连续 N 步 tool+input 指纹相同」的进展判据（纯内存 + 一次索引查询）。
- `task_timeout.py`：新增 `no_progress_repeat_steps` 配置链（任务 env > per_skill > per_repo > 顶层配置 > 进程 env > 默认 8）。
- `scheduler.py`：
  - `_sweep_no_progress_tasks`：在既有 #2793 入口上扩展进展判据，命中走既有重派语义；
    重派上限耗尽时终止任务（`failed`）、释放槽位、回写事件与 autopilot execution。
  - `_poll_dispatched_completion`：超时先把仍活跃的 agent_task 置终态 + 释放槽位，再写 exec failed。
  - 新增反向 zombie sweep：exec 已终态（failed/cancelled）而 agent_task 仍活跃 → 终止任务并计数。

## Out of Scope

- 不改 `max_retry=0` 语义，不改 3600s 总时长阈值默认值。
- 不新增 sweeper 进程/线程，不重写 #2793 既有开关与重派上限。
- 不引入「长时间无消息即判死」判据（避免 #1495 长命令静默误杀）。
- 不改 worker 侧执行循环；远端进程终止复用既有 sid watchdog（服务端任务终态 → ≤60s 杀引擎）。

## Technical Approach

1. **进展判据**：`repeated_tool_steps(cursor, task_id, window_seconds, min_repeats, now_epoch)`
   取最近 50 条 `type='tool_use'` 消息，按 `sha1(tool + normalized input_json)` 指纹从新到旧
   统计连续相同条数；最新一条必须在窗口内，窗口外的行不参与。命中返回
   `{tool, fingerprint, repeats, first_seq, last_seq}`。
2. **巡检落点**：`_sweep_no_progress_tasks` 原判据为「窗口内无 task_messages」（活跃度）。
   扩展为「无活跃 或 连续重复指纹」，二者共用 #2793 的重派计数（`NO_PROGRESS_REQUEUE`）。
   重派上限耗尽时不再只留诊断，而由 `_finalize_no_progress` 收口：
   `agent_tasks.status=failed` + `errors` 追加 `NO_PROGRESS_FAILED`（带
   `repeats/tool/fingerprint/first_seq/last_seq`）+ `release_busy_slot` + 事件 failed +
   关联 autopilot execution failed。
3. **超时收口**：`_poll_dispatched_completion` 超时前解析对应 agent_task，若状态仍为
   `queued/dispatched/running/pending_self_report`，调 `_terminate_stuck_agent_task`
   置 failed（`WORKER_TIMEOUT_TERMINATED` 诊断）+ 释放槽位 + 事件 failed，然后写 exec failed。
4. **反向残留**：`_reap_terminal_exec_running_tasks` 扫描最近 100 条 `failed/cancelled` exec
   且 `agent_task_id` 非空的行，若任务仍活跃（且 exec 终态时间已过 60s 宽限），按 exec 终态
   的语义把任务置 failed/cancelled、释放槽位，并以 `record_task_failed` 计数。

## Key Decisions

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 判据落点 | 新增 sweeper / 扩展 #2793 | 扩展 #2793 | 复用开关与重派语义，避免双套巡检 |
| 进展判据 | 长静默 / 指纹重复 | 指纹重复（可配 N） | 不按时长判死，避免 #1495 误杀 |
| 收口顺序 | 先写 exec failed / 先终止任务 | 先终止任务并释放槽位 | 终态幂等不阻断资源释放 |
| 远端进程终止 | 新增推送通道 / 复用 sid watchdog | 复用 sid watchdog | 服务端任务终态即触发 ≤60s 杀引擎 |

## Acceptance Criteria

- [ ] 连续 K 步同命令的 running 任务在下一轮巡检以 `failed(reason=no_progress)` 收口，`errors` 含步号与指纹；
- [ ] exec 超时后 agent_task 立即变 `failed`，`busy_slots` 回落；
- [ ] 反向 sweep 能发现并收口「exec 已终态 ∧ agent_tasks running」残留；
- [ ] `no_progress_enabled=false` 仍只留诊断不重派；#2793 既有用例全绿；
- [ ] 长静默但无重复指纹的任务不被判死。
