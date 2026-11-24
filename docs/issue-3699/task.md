# Issue #3699 任务拆解

按「一竖切片一测试」TDD 推进，红/绿分提交：

| # | 切片 | 红（先提交失败测试） | 绿（最小实现） | 验收 |
|---|------|---------------------|----------------|------|
| 1 | 中继禁用响应标记 + 一次性 WARNING | `tests/runtime/test_issue_3699_trace_relay_noise.py::test_disabled_relay_response_carries_marker` | `runtime_traces_relay` 加 `X-Grape-Trace-Relay: disabled` + 进程内一次告警 | 503 + 标记，body 契约不变 |
| 2 | 访问日志分级（C 进程 + legacy） | `::test_relay_disabled_503_logs_debug` / `::test_relay_upstream_503_still_error` | `server_common._attach_request_log`、`middleware.install_request_middleware` | 带标记 → 无 ERROR；无标记 → 仍 ERROR |
| 3 | worker 禁用识别 + 退避策略 | `::test_disabled_kind_classification` / `::test_backoff_grows_and_caps` / `::test_backoff_resets_on_success` | `src/kernel/otel.py` `_RelayBackoffPolicy` + `relay_failure_kind` | 仅 503+`trace relay disabled` 判禁用，退避 30s→60s→…上限 900s |
| 4 | worker exporter 分支行为 | `::test_disabled_export_warns_once_and_suppresses` / `::test_retryable_export_retries_then_errors` / `::test_success_export_resets_policy` | `_RelaySpanExporter` + `init_otel` 接线（失败回退 SDK） | 一次 WARNING、窗口内不发请求、其他 5xx 仍重试并 ERROR |

## 回归门禁

```bash
python3 -m pytest tests/runtime/test_issue_3699_trace_relay_noise.py \
                 tests/insight/test_pr1077_observability_endpoints.py -q
make lint
```

基线对比：新增用例全绿，`test_pr1077_observability_endpoints.py` 既有中继/OTel 用例不回归。

## 交付

- 分支 `feat/issue-3699` → PR base `arch-weekly-11-ljt`；
- PR 说明列出改动文件、日志分级口径与测试结果；
- 建 PR 后 Issue 评论告知 + A2A 通知 QA 审查。