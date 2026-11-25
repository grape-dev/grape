# Plan: Issue #4349 — 限频告警 + uid 比对 + 心跳丢失可观测

## 方案

新增进程内 stale-uid 观测模块 `src/domains/runtime/engine/stale_uid.py`（叶子模块，无环）：

1. **限频 ERROR**：`record_and_log()` 按 `(uid, credential_id)` 维度在日志窗口
   （`GRAPE_STALE_UID_LOG_WINDOW`，默认 3600s）内只输出一条 ERROR；窗口内后续命中只计数。
   - `_resolve_runtime_by_uid()`（runtime.py:1768）：stale 命中改走该函数，消息保留
     `engine_uid not found (stale?)` 口径；回退命中时把报告关联到回退行 id。
   - `runtime_heartbeat_v2()` stale 段：同款限频，报告关联同请求 `body.runtime_ids`。
2. **指标**（接入现有 insight prometheus registry，同 `_relay_total` 模式）：
   - `grape_runtime_v2_stale_uid_total{source=resolve|heartbeat}`：按次累加（不受日志限频影响）。
   - `grape_runtime_v2_heartbeat_uid_lost_total`：`stale_uids` 非空且 `runtime_ids` 为空
     （心跳丢失）时累加，并按凭据限频输出 ERROR。
3. **报告注册表**：进程内 TTL 表（`GRAPE_STALE_UID_RETENTION`，默认 86400s），
   `reports_for_credential()` 供 `/settings/agent-runtimes`（列表+详情共用
   `get_agent_runtimes`）带出：
   - `rt["engine_uid"]`（DB 行 uid，已存在）
   - `rt["engine_uid_stale_reports"]`：该凭据下的 stale 上报（uid/count/last_seen/sources/runtime_ids）
   - `rt["engine_uid_status"]`：报告关联到本行 id → `stale`，否则 `matched`
4. **前端**：RuntimesView.vue 引擎详情弹窗加「引擎 UID」行，stale 时黄色标记 +
   tooltip（上报失效 uid 列表）；locales zh/en 补 key。
5. **测试**（`tests/api/test_issue_4349_stale_uid_observability.py`）：
   - resolve stale 限频（首条 ERROR、窗口静默、窗口翻转恢复）
   - heartbeat stale 回退关联 + 心跳丢失分支（指标 + 限频 ERROR）
   - 双失效 404 不关联
   - 详情端点比对字段（TestClient + 全局测试库，同 test_3396 模式）
   - 前端/locales 接线源级守卫（同 test_vue_shell_and_engine_detail.py 模式）

## 拓扑注记
注册表为进程内状态（与 `_relay_warn_ts` 等既有缓存同模式）；runtime 域 lib 当前为
「进程内 import」，拆分三节点时需迁 DB 持久化（届时随迁移补，不在本 Issue 范围）。

## 风险
- 限频引入进程内状态：TTL + 容量上限剪枝，单测覆盖窗口边界。
- 心跳丢失告警误报（引擎短暂重注册窗口）：按凭据限频，窗口可调。
