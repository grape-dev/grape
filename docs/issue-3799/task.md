# Issue #3799 任务拆解（竖切片，红/绿分提交）

| # | 切片 | 测试（先红） | 实现（后绿） |
|---|------|--------------|--------------|
| 1 | 健康状态机 + 阈值告警 | `test_issue_3799_channel_health.py::test_record_pause_persists_state`、`test_second_pause_within_window_fires_alert`、`test_all_instances_paused_within_window_alerts`、`test_record_resume_clears_pause_and_resolves_alert` | `src/domains/runtime/im/channel_health.py`（`ChannelHealthState/ChannelHealthStore/ChannelHealthTracker`）、`channel_health_ddl` + `_NEW_TABLES` + `table_ownership.json` |
| 2 | 失败日志小时聚合 | `test_hourly_log_aggregator_one_summary_per_window`、`test_notify_failure_logs_aggregated_per_window`、`test_sender_notify_failure_logs_aggregated` | `HourlyLogAggregator` + `channel.py`/`outbound.py` 4 个失败点接入 |
| 3 | 渠道侧暂停/恢复状态机 | `test_channel_pause_records_health_and_resume_clears_it`、`test_channel_pause_then_successful_poll_resumes`、`test_request_retry_wakes_pause_sleep_and_reissues_notifystart` | `WechatChannel._on_session_paused/_record_pause/_record_resume/request_retry` + 可唤醒 sleep |
| 4 | 通知降级路由 | `test_notify_user_skips_paused_channel_and_records_degradation`、`test_notify_user_paused_without_fallback_is_recorded` | `notify.notify_user` 分桶 + `record_degraded_delivery` + `_deliver` 返回 `channel_paused` + outbox 消费者 `release_sending` |
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

R2 复审（R3 修复）回归 —— 用例总数 `test_issue_3799_channel_health.py` 40 例（R1 24 + R2 11 + R3 5）：

- 首停状态对象不被 peer 重读替换（Critical）：`test_first_pause_keeps_local_state_when_store_has_history_row`
- 暂停行释放退避 / deferral 日志窗口（Required）：`test_released_paused_row_defers_claim_by_channel_retry_window`、`test_released_paused_row_keeps_min_60s_backoff`、`test_paused_deferral_logs_once_per_window`、`test_recover_action_wakes_deferred_paused_rows`
- 降级记录按 target 判定（Optional）：`test_notify_user_degraded_to_same_type_other_installation`（补 `inst-wx2 degraded_count==0` 与 `skipped["wechat"]` 断言）
- 过期暂停对外口径（Nit）：`test_stale_pause_row_not_treated_as_paused`（补 `paused=False / stale=True` + 健康面板不计 paused 断言）
