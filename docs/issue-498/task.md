# Task: Issue #498 拆解

## M1 判定侧（core）
- [x] health_scorer：新增 HEALTH_FAILURE_DECAY_HOURS 配置 + _decay_start
- [x] _fetch_task_stats：failed_effective / last_failure_at + cf 衰减清零
- [x] _compute_and_persist：健康分用 failed_effective；数据带 health_abnormal / health_reason / failed_tasks_effective / last_failure_at
- [x] _derive_health_flag 统一异常口径（心跳 > 连败 > 低分，最小样本门槛）
- [x] get_health_score 读路径 / _empty_score 同步派生
- [x] db_setup：runtime_health_scores 增列迁移（failed_tasks_effective / last_failure_at）

## M2 透出与呈现
- [x] settings.py agent-runtimes 列表（详情复用列表）：透出 health_abnormal / health_reason / health_last_failure_at / health_failed_tasks_effective
- [x] runtime.py machine-health 端点描述更新（字段随 scorer 返回自动携带）
- [x] runtimes.js：_healthHelpHint / _isAbnormalEngine / _abnormalBorderColor 优先后端口径（兼容回退）
- [x] runtimes.js：worker_version_stale 独立琥珀标签（卡片 + 详情弹窗）
- [x] locales zh/en：runtimes.label.worker_version_stale

## M3 测试与验证
- [x] test_health_scorer.py：衰减清零 / 自愈回升 / 异常口径 / env 可配 / DB 留痕用例
- [x] tests/js：前端口径一致性用例（health_abnormal 优先、worker_version_stale 独立）
- [x] 全量回归：python -m pytest tests/test_health_scorer.py + 相关既有套件 + node js 测试
