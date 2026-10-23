# Tasks: Issue #437

- [x] T1 metric_rollup：新增 engine_avail_online/total SUM 指标 + compute_bucket ⑥ 聚合采样表
- [x] T2 slo.py：_availability_buckets 改读 rollup + 当前桶有界现算 + 回退
- [x] T3 slo.py：eval_interval_sec()（meta > env > 默认 300，下限 60）
- [x] T4 slo.py：evaluate_all cleanup 按小时桶变更降频
- [x] T5 main.py：SLO 评估从 30s 告警循环解耦为独立周期
- [x] T6 测试：rollup availability 口径 / slo 可用率读 rollup / 周期配置源
- [x] T7 全量回归：pytest tests/test_obs_slo.py tests/test_obs_metric_rollup.py tests/test_obs_rollup.py
