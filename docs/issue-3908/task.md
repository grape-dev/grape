# Issue #3908 Tasks

> 目标合入分支：`arch-weekly-11-ljt`

- [x] T1 kernel：`AUTOPILOT_EVENT_SEQ_BASE` / `is_autopilot_event_seq()` / `is_autopilot_issue_ref()`
- [x] T2 scheduler：常量别名委托，保留既有符号与静态守护
- [x] T3 `_pr_subscription_state()`：events 查询前按命名空间短路 `False`
- [x] T4 `reconcile_task_scene_env()`：autopilot 回链键 / 无绑定合成 seq debug 跳过
- [x] T5 `_is_autopilot_task()`：seq 命名空间 + 真实 Issue 编号不算 autopilot
- [x] T6 新增 `tests/runtime/test_issue_3908_autopilot_scene_env.py`（4 情形 + kernel 判据）
- [x] T7 回归：`test_autopilot_event_seq_namespace.py`、`test_issue_1745_scene_env_truth.py`、
  `test_issue_3744_autopilot_scene_reconcile.py`、`test_3055_issue_ident_hardening.py`