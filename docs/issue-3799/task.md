# Issue #3799 任务拆解（竖切片，红/绿分提交）

| # | 切片 | 测试（先红） | 实现（后绿） |
|---|------|--------------|--------------|
| 1 | 健康状态机 + 阈值告警 | `test_issue_3799_channel_health.py::test_record_pause_persists_state`、`test_second_pause_within_window_fires_alert`、`test_all_instances_paused_within_window_alerts`、`test_record_resume_clears_pause_and_resolves_alert` | `src/domains/runtime/im/channel_health.py`（`ChannelHealthState/ChannelHealthStore/ChannelHealthTracker`）、`channel_health_ddl` + `_NEW_TABLES` + `table_ownership.json` |
| 2 | 失败日志小时聚合 | `test_hourly_log_aggregator_one_line_per_window`、`test_wechat_channel_notify_failures_aggregated` | `HourlyLogAggregator` + `channel.py`/`outbound.py` 4 个失败点接入 |
| 3 | 渠道侧暂停/恢复状态机 | `test_channel_pause_records_health_and_alerts`、`test_channel_resume_clears_pause` | `WechatChannel._on_session_paused/_health_resume/request_retry` + 可唤醒 sleep |
| 4 | 通知降级路由 | `test_notify_user_skips_paused_channel_and_records_degradation`、`test_notify_user_paused_without_fallback_deferred` | `notify.notify_user` 分桶 + `record_degraded_delivery` + `_deliver` 返回 `channel_paused` + outbox 消费者 `release_sending` |
| 5 | 管理台健康卡片/恢复入口 | `test_im_channel_health_endpoint`、`test_im_installation_recover_endpoint` | `mgmt/routes/im.py` 新增 `GET /api/im/channels/health`、`POST /api/im/installations/{id}/recover`，`installations` 行带健康字段；runtime public 契约 `channel_health_rows/channel_recover_channel` |
| 6 | 指标 | `test_engine_metrics_counts_channel_paused` | `EngineMetrics.record_channel_paused` + `channel_paused_by_instance` + `health()` 快照 |

## 验收命令

```bash
python -m pytest tests/runtime/im/test_issue_3799_channel_health.py tests/runtime/im/test_wechat_channel.py \
    tests/runtime/im/test_wechat_outbound.py tests/runtime/im/test_notify_outbox.py \
    tests/runtime/im/test_im_routes.py tests/runtime/im/test_health_metrics.py -q
python -m pytest tests/arch tests/api/test_channel_ddl_ownership.py -q
make lint
```


PR #1983 审查回归（第 7/8 节用例，与上表切片一一对应）：
- 健康面板/outbound 行级裁剪：`test_im_channel_health_scoped_to_visible_installations`、`test_im_outbound_scoped_to_visible_installations`
- peer 状态 TTL 重读：`test_alert_all_instances_rule_refreshes_peer_state_from_store`
- flush 丢单次失败 / sender flush：`test_hourly_log_aggregator_flush_outputs_single_failure`、`test_wechat_sender_flush_outputs_pending_window`
- 消费分支 / 409 / 403：`test_outbox_consumer_defers_paused_channel_row`、`test_recover_route_409_when_channel_not_running`、`test_recover_route_409_when_engine_unavailable`、`test_recover_route_403_for_other_users_installation`
- Optional：`test_notify_user_degraded_to_same_type_other_installation`、`test_request_retry_on_healthy_channel_does_not_arm_wake_gate`
- 前端消费点：`src/api/frontend/app/src/views/ImChannel.vue`（paused 卡片 + 三恢复入口，locale zh/en）
