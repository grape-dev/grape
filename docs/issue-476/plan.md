# Plan: Issue #476 — 失败事件强制携带可诊断 result

## 改动清单

| 文件 | 改动 |
|------|------|
| `src/api/event_pipeline.py` | `_fail_event(seq, reason, exit_code=1, diag=None)`：result 内嵌 `[code]` 前缀；diag 合并写入 events.payload `_fail_diag`（幂等：已存在不覆盖）；agent_tasks.errors 同步带 code 前缀 |
| `src/api/event_store.py` | `update_event_status(seq, status, result="", exit_code=None)`：终态 failed 且 result 为空时回填 fallback reason `[E-FAIL-NODIAG] 事件置 failed 但未携带诊断信息（#476）` + exit_code=1 + completed_at；调用方传 result 时优先使用 |
| `src/api/routes/webhook.py` | ① 新增 `_a2a_source_event_seq(session_info)` 助手（session_key→agent_tasks.event_seq）；② a2a_endpoint append_event payload 与 plugin 派发 handoff payload 注入 `ref_seq`；③ `_on_handoff_dispatch` 两处裸 update_event_status → `_fail_event(seq, reason, diag={code,target_stage,phase,ref_seq})` |
| `src/api/routes/events.py` | `list_events` SELECT 补 result / issue_id，LEFT JOIN agent_tasks 取最新 task_id；terminate/redispatch 标记 failed 处传明确 reason（用户手动终止 / 重新派发） |
| `src/observability/failed_gap.py`（新增） | 跨仓口径：failed 总数 / result 空数（legacy 单独统计）/ 占比 / 最近样本 |
| `src/api/routes/observability.py` | 新增 `GET /api/obs/events/failed-result-gap`（可见仓库范围） |
| `src/api/static/js/pages/events.js` | 详情页失败诊断渲染（payload._fail_diag → 目标阶段/失败环节/引用 seq/错误码） |
| `src/api/static/js/pages/pipeline.js` | 回退跳转消费 issue_id/task_id（后端已补字段，恢复正常） |
| `src/api/static/js/pages/observability.js` | 事件 Tab 顶部新增"失败空结果占比"摘要卡 |
| `scripts/backfill_legacy_failed_events.py`（新增） | 存量空 result failed 事件打 legacy 标记（--dry-run / --apply） |
| `docs/issue-476/record-main-prod.md`（新增） | main/生产同路径排查结论 |
| `tests/test_issue_476_failed_result.py`（新增） | listener 失败路径 + update_event_status 防御 + _fail_event diag + list_events 字段 + failed-result-gap 口径 |

## 测试策略

- **listener 路径（本 Issue 核心）**：构造 agent/handoff GrapeEvent → `patch(dispatch_node)` 返回 failed/抛异常 → 调用 `_on_handoff_dispatch` → 断言 events 行 result 非空且含错误与 target、payload._fail_diag 含 code/target_stage/phase/ref_seq。
- **update_event_status 防御**：running 事件裸置 failed → 断言 result 非空（fallback）、exit_code=1、completed_at 非空；传 result 时优先用传入值。
- **接口字段**：TestClient + JWT 调 `/api/events/{owner}/{repo}` → 断言 result/issue_id/task_id 返回；调 `/api/obs/events/failed-result-gap` → 断言总量/空数/legacy/占比。
- 回归：`test_issue_418/368`、`test_event_store`、`test_event_status_protection` 全量跑通。

## 风险与缓解

- `update_event_status` 语义变化：仅 failed 且 result 空时回填，running/completed/cancelled 行为不变；402+ 调用点已 grep 核查。
- `_fail_event` 形参扩展默认兼容，既有 30+ 调用点零改动。
- ref_seq 获取失败（无 session/task）→ 0，不阻断诊断写入。