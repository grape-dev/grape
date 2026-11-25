# Issue #3908 Spec — autopilot 场景环境校正恒跳过（合成 seq 被判 events 缺失）

> 目标合入分支：`arch-weekly-11-ljt`

## 目标

消除 autopilot 执行前场景校正（`reconcile_task_scene_env`）对所有 autopilot 任务的恒跳过
与误告警：不对合成 seq（`AUTOPILOT_EVENT_SEQ_BASE + exec_id`）查询 events 表、不产生
`events row missing` WARNING；autopilot 合成回链键静默跳过校正；高位 seq + 真实 Issue
编号的任务恢复 #1745 的正常场景校正。

## 范围

In Scope：
- `src/kernel/issue_ident.py`：新增 `AUTOPILOT_EVENT_SEQ_BASE`、`is_autopilot_event_seq()`、
  `is_autopilot_issue_ref()`（唯一判据入口，避免 event_pipeline → scheduler 反向依赖）。
- `src/domains/runtime/engine/scheduler.py`：常量改为 kernel 别名委托，保留
  `_AUTOPILOT_EVENT_SEQ_BASE` / `_AUTOPILOT_ISSUE_ID_PREFIX` 符号以维持 #1864 静态守护。
- `src/domains/runtime/pipeline/event_pipeline.py`：
  - `_pr_subscription_state()` 在 events 查询前按命名空间短路返回 `False`；
  - `reconcile_task_scene_env()` 对 `autopilot-<字母>` 回链键 debug 级跳过；
  - `_is_autopilot_task()` 口径对齐：seq 命名空间仅在 issue_id 非真实编号时视为 autopilot。
- `tests/runtime/test_issue_3908_autopilot_scene_env.py`：4 情形回归。

Out of Scope：
- 不改 autopilot event_seq 命名空间基值（#1864 已锁定）。
- 不改 PR 订阅三态语义与「未判定即保守跳过」口径（#427）。
- 不改 #1745「Issue 未绑定 → 回退 dev_scene」口径。

## 验收

- [x] 合成 seq 时 `_pr_subscription_state()` 返回 `False`，不查 events、无 WARNING。
- [x] `autopilot-abc` + 合成 seq：返回快照原值，`GRAPE_SCENE_ID` 不改写，仅 debug。
- [x] 合成 seq + 真实 `#N`：`GRAPE_SCENE_ID` 校正为目标场景。
- [x] 普通任务真实 events 行缺失：仍打 WARNING 并保留快照。
- [x] `test_autopilot_event_seq_namespace.py`、`test_issue_1745_scene_env_truth.py`、
  `test_3055_issue_ident_hardening.py` 回归全绿。