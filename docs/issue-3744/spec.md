# Issue #3744 Spec — autopilot 合成 seq 无 events 行：场景校正按来源短路

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-22

## 目标

autopilot 任务使用合成 `event_seq`（`_AUTOPILOT_EVENT_SEQ_BASE + exec_id`）且设计上不写
`events` 表（`skip_event_config=True`），导致执行前场景校正每次必然查空并产生
`events row missing for seq=` WARNING（实测 939 条/日），同时被
`_pr_subscription_state` 的 `None → 保守跳过` 语义永久跳过校正。

在场景校正入口按任务来源识别 autopilot 并短路：不查 events、不打 WARNING、打 debug 并保留
任务快照场景值；非 autopilot 缺行告警保留并增补可定位字段。

## 范围

In Scope：
- `src/domains/runtime/pipeline/event_pipeline.py`：新增 `_is_autopilot_task()` 统一来源判定；
  `reconcile_task_scene_env()` 入口短路；非 autopilot 缺行 WARNING 增补
  `task_id` / `event_seq` / `repo_key`。
- `src/domains/runtime/engine/scheduler.py`：将 autopilot issue_id 前缀提为共享常量
  `_AUTOPILOT_ISSUE_ID_PREFIX`，复用既有 `_AUTOPILOT_EVENT_SEQ_BASE`（不硬编码 900023）。
- 单测覆盖 autopilot 短路 / 非 autopilot 缺行告警 / PR 订阅回归。

Out of Scope：
- 不修改 `_pr_subscription_state()` 的 `None → 保守视为 PR 订阅` 语义（防 #427 回退）。
- 不为 autopilot 写 events 占位行。
- 不改 scheduler 派发逻辑与 `_AUTOPILOT_EVENT_SEQ_BASE` 基值。

## 来源识别口径

1. 主判据：`agent_tasks.issue_id` 前缀 `autopilot-`（任务级权威标记，由
   `scheduler._autopilot_issue_ref` 产出）。
2. 辅助判据：`event_seq >= _AUTOPILOT_EVENT_SEQ_BASE`（高位命名空间兜底，兼容历史
   无 issue_id 的 autopilot 任务）。
3. 两个常量均从 scheduler 导入，禁止硬编码观测值。

## 验收

- autopilot 任务不产生 `events row missing for seq=`，日志可见
  `scene_reconciled=false reason=autopilot_source` 的 debug 依据，快照场景值保留。
- 非 autopilot 真实缺行仍产生 1 条含 `task_id`/`event_seq`/`repo_key` 的 WARNING。
- 回归：PR 订阅任务不校正（#427），普通 Issue 任务校正行为不变；现有测试全部通过。