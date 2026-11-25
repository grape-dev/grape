# Task Breakdown: Issue #3790

| # | 任务 | 状态 | 验收 |
|---|------|------|------|
| T1 | `engine_select`：`POOL_SOURCE_SCENE_MISSING` 常量 + `get_scene_pool_engine_ids(source_out=)` | TODO | 未登记场景写出 scene_missing，返回语义不变 |
| T2 | `engine_select`：`_alert_scene_missing` 窗口收敛告警 | TODO | 同键窗口内仅 1 行 + suppressed 计数 |
| T3 | `event_pipeline._scene_pool_gate` 回传来源 | TODO | payload["pool_engine_source"] |
| T4 | `event_pipeline` 主派发 / 订阅 / 重派 / 推荐重派显式透传来源 | TODO | 不被推导覆盖 |
| T5 | `_enqueue_pipeline_task` 内层 `enqueue_task` 落 `pool_engine_source` | TODO | agent_tasks 行值正确 |
| T6 | scene_missing 事件 payload 回填 + 失败文案原因码 | TODO | payload 四字段；文案可区分未注册/离线 |
| T7 | `event_store.merge_event_payload` | TODO | 幂等合并、非法 JSON 重建 |
| T8 | `mgmt` 侧 `scene_pool_binding_warning` + `set_repo_scene` 提示 + 模板建仓回执 | TODO | 提示不阻断保存 |
| T9 | 新增测试 `tests/runtime/test_issue_3790_scene_missing_source.py` | TODO | 覆盖上述路径 |
| T10 | 回归：`tests/runtime/test_issue_541_template_scene_id.py` 等 + `make lint` + `scripts/check-arch.py` | TODO | 通过（基线既有失败项除外并记录） |