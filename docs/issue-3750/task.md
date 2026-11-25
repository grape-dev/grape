# Issue #3750 任务拆解（Task）

| # | 任务 | 文件 | 验证 |
|---|------|------|------|
| 1 | RED：拒绝台账用例（指纹/理由/落库字段/队满丢弃/窗口计数/跨仓库不聚合/留存清理） | `tests/insight/test_issue_3750_webhook_rejects.py` | 未实现前用例按预期红（ImportError / AttributeError） |
| 2 | RED：路由用例（403 落一行 + 日志 `event`/`reason`；404 补 `event`/`ip`；落库异常不影响 403） | `tests/runtime/test_issue_3750_webhook_reject_ledger.py` | 同上 |
| 3 | RED：告警用例（指标值 / 种子规则 / firing→resolved / 跨仓库不聚合） | `tests/insight/test_issue_3750_webhook_mismatch_alert.py` | 同上 |
| 4 | RED：`deploy/alerts.yml` 规则与指标名一致性 | `tests/deploy/test_issue_3750_alerts_yml.py` | 同上 |
| 5 | GREEN：`grape_t_obs_webhook_rejects` DDL + 建表登记 | `src/domains/insight/schema_ddl.py`、`src/api/db_setup.py` | 用例 1 |
| 6 | GREEN：拒绝台账模块（有界队列 writer / 计数 / 清理） | `src/domains/insight/observability/webhook_rejects.py` | 用例 1 |
| 7 | GREEN：insight public 暴露指纹与落库原语 | `src/domains/insight/public/__init__.py` | 用例 1/2 |
| 8 | GREEN：路由 403/404 日志补字段 + 403 落库 | `src/domains/runtime/routes/webhook.py` | 用例 2 + #3897 回归 |
| 9 | GREEN：指标 `webhook_token_mismatch` + 种子规则 + Prometheus 计数器 | `src/domains/insight/observability/alerts.py`、`observability/__init__.py` | 用例 3 |
| 10 | GREEN：`deploy/alerts.yml` 规则 + 留存清理挂点 | `deploy/alerts.yml`、`src/api/main.py`、`src/api/server_tasks.py` | 用例 4 + `promtool check rules` |
| 11 | 回归：新用例 + `test_obs_alerts` / `test_obs_retention` / `test_issue_3897_webhook_fingerprint` / `make lint` / `scripts/check-arch.py` | — | 无新增失败 |
| 12 | 自审查（5 轴 + 开源前置增量检查）→ commit/push → 建 PR → Issue 评论 → A2A QA → `tasks complete` | — | runbook Step 5-8 |