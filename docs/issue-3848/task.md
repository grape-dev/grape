# Issue #3848 任务拆解（竖切片，红/绿分提交）

| # | 切片 | 测试（先红） | 实现（后绿） |
|---|------|--------------|--------------|
| 1 | usage_log 保留期清理：异常分类 + bind 后补跑一次 | `test_issue_3848_startup_order.py::test_cleanup_returns_zero_and_logs_debug_when_port_unbound`、`::test_deferred_cleanup_counts_startup_degraded`、`::test_bind_tokens_runs_startup_prune_exactly_once`、`::test_other_port_bind_does_not_trigger_startup_prune`、`::test_startup_prune_reports_deleted_rows` | `src/kernel/persistence/credential_usage_log.py`（`_PortNotBound` 分类、`_on_port_bound`、`_run_startup_prune_once`、`defer_startup_retention_cleanup`、`reset_startup_prune_state`） |
| 2 | 启动调用点改为「布防」 | `::test_startup_call_site_arms_cleanup_instead_of_running_it`、`::test_defer_runs_immediately_when_tokens_already_bound` | `src/domains/mgmt/user/db.py::_cleanup_usage_log_retention` 改调 `defer_startup_retention_cleanup` |
| 3 | card_sources 端口未就绪 vs 真读失败分流 | `::test_card_source_port_not_bound_is_debug_and_counted`、`::test_card_source_real_failure_keeps_warning` | `src/domains/runtime/vcs/card_sources.py`（`except PortNotBound` 分支 + `record_startup_degraded`） |
| 4 | `startup_degraded` 指标 | `::test_startup_metrics_counts_by_component_and_reason`、`::test_startup_metrics_noop_before_install`（随切片 1/3 同测） | `src/kernel/startup_metrics.py` + `src/domains/insight/observability/__init__.py` 安装 |

> 用例文件统一为 `tests/runtime/test_issue_3848_startup_order.py`（表中第二列省略了文件名前缀）。
> 两条启动时序各有独立用例：`test_bind_tokens_runs_startup_prune_exactly_once`（未绑→bind 补跑）、
> `test_defer_runs_immediately_when_tokens_already_bound`（已绑→布防立即补跑）。

## 不在本分支交付

- item 4（anyio 线程上限 24 / `db-pool-guard`）：基线 `arch-weekly-11-ljt` 无
  `src/kernel/db_pool_guard.py`（该文件只在 `origin/arch-weekly-11` 的 #501 提交
  `2e4c9465d` 里），本分支新建即与 #501 冲突；由 #501 线负责，PR 中明示。

## 验收命令

```bash
python -m pytest tests/runtime/test_issue_3848_startup_order.py -q
python -m pytest tests/mgmt/test_n4_audit_coverage.py \
    tests/runtime/test_platform_card_contract_failure.py \
    tests/runtime/test_issue_3632_platform_card_contract.py -q
python -m pytest tests/arch -q
python scripts/check-arch.py
```

CI 门禁：`.github/workflows/windows-smoke.yml` 的 `linux-backend` job 已接入
`Startup port-binding order regression tests (issue #3848)` step，回退可阻断合并。