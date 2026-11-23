# Issue #3848 任务拆解（竖切片，红/绿分提交）

| # | 切片 | 测试（先红） | 实现（后绿） |
|---|------|--------------|--------------|
| 1 | usage_log 保留期清理：异常分类 + bind 后补跑一次 | `test_issue_3848_startup_order.py::test_cleanup_returns_zero_and_debug_when_port_unbound`、`test_bind_tokens_runs_startup_prune_exactly_once`、`test_replace_rebind_does_not_prune_again`、`test_startup_prune_prunes_rows_once_port_bound` | `src/kernel/persistence/credential_usage_log.py`（`_PortNotBound` 分类、`_on_port_bound`、`_run_startup_prune_once`、`defer_startup_retention_cleanup`、`reset_startup_prune_state`） |
| 2 | 启动调用点改为「布防」 | `test_startup_call_site_does_not_prune_before_bind`、`test_startup_call_site_prunes_when_already_bound` | `src/domains/mgmt/user/db.py::_cleanup_usage_log_retention` 改调 `defer_startup_retention_cleanup` |
| 3 | card_sources 端口未就绪 vs 真读失败分流 | `test_card_source_port_not_bound_is_debug_and_counted`、`test_card_source_real_failure_keeps_warning` | `src/domains/runtime/vcs/card_sources.py`（`except PortNotBound` 分支 + `record_startup_degraded`） |
| 4 | `startup_degraded` 指标 | `test_startup_metrics_install_and_count`、`test_startup_metrics_noop_before_install`（随切片 3 同测） | `src/kernel/startup_metrics.py` + `src/domains/insight/observability/__init__.py` 安装 |

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