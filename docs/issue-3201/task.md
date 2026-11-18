# Issue #3201 任务拆解

- [x] T1 接管 active 判定自愈 + 忽略计数（`takeover.py`）
- [x] T2 建 task 出口守门：`_create_direct_task` / `_enqueue_pipeline_task`（`event_pipeline.py`）
- [x] T3 A2A 入口 409 `already_taken_over`（`webhook.py`）
- [x] T4 状态聚合锁定 running + 过滤 skipped_takeover（`pipeline_status.py`）
- [x] T5 列表/详情/finish 暴露已忽略 N 条（`mgmt/routes/takeover.py`、`takeover.py`）
- [x] T6 测试：webhook/direct/A2A/过期自愈/跨 Issue/聚合/finish 顺序与计数

验证命令：`pytest tests/test_takeover_dispatch_guard_3201.py tests/test_takeover_flow.py tests/test_takeover_event_guard.py -q`
