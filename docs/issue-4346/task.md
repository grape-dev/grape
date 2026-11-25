# Task：Issue #4346 任务拆解

| # | 任务 | 产出 | 验证 |
|---|------|------|------|
| T1 | session 失败分类 + 观测 payload | `engine/session_guard.py`（classify / failure_payload） | 单测：三类错误码、字段恒存在 |
| T2 | session 层熔断/半开状态列 | `migrations/session_circuit_columns.py` + `schema_ddl` + `db_setup` 注册 | 迁移幂等、post_check、测试库建表带列 |
| T3 | 熔断计数与半开恢复 | `session_guard`（note_session_failure/success/probe/claim_allowed） | 单测：连续 3 次熔断、冷却半开、成功恢复 |
| T4 | 换引擎重试与回落 | `session_guard.handle_task_failure` + `service.fail_task` 接入 | 单测：requeue 换引擎、attempt 超限判 failed、无可用引擎显式失败 |
| T5 | 派发/认领过滤 | `backend_health.claim_allowed` + `engine_select._backend_claimable_map` | 单测：熔断引擎不被选中/认领 |
| T6 | 健康分透出 session 指标 | `health_scorer`（读/算/空档三路径） | 单测：字段可见，既有 ENGINE_OFFLINE 语义不变 |
| T7 | LLM provider 注册/启用校验 | `engine/provider_check.py` + `agent._discover_local_runtimes` + `routes/runtime.py` | 单测：缺失拒绝、存量 `llm_provider_ok=0` 不进池 |
| T8 | worker ACP 失败文案分类标记 | `agents/{hermes,aishell,hwcloud}_acp.py` | 单测：分类可解析 |
| T9 | #423 解析侧回归守护 | `tests/runtime/test_issue_4346_session_guard.py` | `_ACP_READ_MAX_LINES > 10` 断言 + 既有 #423 用例保持绿 |
| T10 | 文档 + 全量门禁 | docs/issue-4346、pytest、lint、arch | `make lint`、`scripts/check-arch.py` |