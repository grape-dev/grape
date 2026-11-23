# Issue #4082 任务拆解（Task）

按 TDD 红/绿分提交执行，一竖切片一次红一次绿。

| # | 切片 | 红（测试先行） | 绿（实现） |
|---|------|---------------|-----------|
| 1 | 服务端非法 id → 400 | `tests/runtime/test_issue_4082_runtime_id_guard.py::test_invalid_runtime_id_400`（0/abc/-1） | `routes/runtime_id_guard.py` + 两 router 挂 `route_class` |
| 2 | worker 探活守卫（不发请求） | `test_probe_tags_id_missing_no_http`（mock aiohttp 断言零请求） | `runtime_id_guard.py` + `client._guard_runtime_id` + `probe_tags` |
| 3 | 注册回填去 0 兜底 | `test_register_drops_invalid_ids` | `client.register()` 过滤 + 失败返回 |
| 4 | 心跳过滤非法 id | `test_heartbeat_skips_invalid_ids` | `client.heartbeat()` 过滤/空不发 |
| 5 | 探活 reason 归类 | `test_probe_tags_reasons`（200/200空/401/404） | `probe_tags` 状态码映射 |
| 6 | 指标承接 | `test_probe_failure_signal_metric` | `observability.record_runtime_tags_probe_failure` + `POST /runtime-tags-probe/signal` |
| 7 | 连续失败告警 | `test_alerts_rule_present` | `deploy/alerts.yml` `GrapeRuntimeTagsProbeFailing` |
| 8 | 文档 | — | `docs/issue-4082/{spec,plan,task}.md` |
| 9 | 多引擎 reporter 隔离（PR #2066 审查 Required） | `test_probe_reporters_are_isolated_per_rid` / `test_two_engines_with_different_reasons_both_alert` / `test_prune_reporters_drops_stale_rids_and_keeps_live_counts` | `agent.tags_probe_reporter_for` / `prune_tags_probe_reporters`，主循环改按 rid 建 reporter |
| 10 | 注册部分回填（审查 Optional ①） | `test_register_keeps_valid_ids_when_some_are_invalid` / `test_reregister_cycle_schedules_retry_on_partial_ids` | `client.register` 逐条过滤 + `_PARTIAL_ID_REREGISTER_BACKOFF` |
| 11 | 契约外 reason（审查 Optional ②） | `test_non_contract_reason_counted_as_other_and_logged_once` / `test_network_reason_logged_but_not_counted` | `ProbeFailureReporter` 归 `other` 桶 + 每 reason 一次日志 |
| 12 | 信号端点 reason 口径统一（审查 Optional ③） | `test_probe_failure_signal_endpoint_records_metric`（未知 reason → 200 + unknown 计数） | `record_runtime_tags_probe_failure` 兜底口径，去掉 400 |

## 验证命令

```bash
pytest tests/runtime/test_issue_4082_runtime_id_guard.py -q
pytest tests/runtime tests/api -q      # 相关面回归
make test                             # 全量
```

## 存量主机（运维，不在代码范围）

15 台主机核对 `GRAPE_RUNTIME_ID` 并补回真实 id；观察
`grape_runtime_tags_probe_failure_total{reason="id_missing"}` 与 `/runtimes/0/tags` 401 归零。