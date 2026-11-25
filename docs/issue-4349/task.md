# Tasks: Issue #4349

- [x] T1 新增 `src/domains/runtime/engine/stale_uid.py`：限频窗口 + 报告注册表 + prometheus 计数器
- [x] T2 `runtime.py` `_resolve_runtime_by_uid()`：stale 命中 → 限频 ERROR + 指标 + 回退行关联
- [x] T3 `runtime.py` `runtime_heartbeat_v2()`：stale 段限频 ERROR + 指标 + runtime_ids 关联；心跳丢失分支（stale_uids 非空且 runtime_ids 为空）计数告警
- [x] T4 `settings.py` `get_agent_runtimes()`：engine_uid 比对字段（engine_uid_stale_reports / engine_uid_status）
- [x] T5 前端 RuntimesView.vue 详情弹窗展示 engine_uid + stale 标记；locales zh/en
- [x] T6 回归用例 `tests/api/test_issue_4349_stale_uid_observability.py`
- [x] T7 门禁：新用例 + 既有 v2 用例（test_issue_1011_claim_v2）+ AGENTS §6 三件套 + make lint + check-arch
