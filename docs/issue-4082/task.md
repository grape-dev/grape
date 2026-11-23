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

## 验证命令

```bash
pytest tests/runtime/test_issue_4082_runtime_id_guard.py -q
pytest tests/runtime tests/api -q      # 相关面回归
make test                             # 全量
```

## 存量主机（运维，不在代码范围）

15 台主机核对 `GRAPE_RUNTIME_ID` 并补回真实 id；观察
`grape_runtime_tags_probe_failure_total{reason="id_missing"}` 与 `/runtimes/0/tags` 401 归零。