# Spec: Issue #4349 — v2 engine_uid 失效刷屏治理

> 来源：Issue #4349 Spec Document（issue agent 评论 id 203688397，评级 ready）。
> 本文件为开发侧落地摘要，验收口径以 Issue Spec 为准。

## 目标
- 消除 v2 `engine_uid` 失效导致的 WARNING 刷屏（vod-dev-002 三引擎 1 小时 2,733 条，占该小时 worker WARNING 36%）。
- 认领/心跳在 uid 失效时不再静默降级：stale uid 有限频 ERROR + 指标；「stale-only 且无 runtime_ids 回退」的心跳丢失分支有显式告警。
- `/runtimes` 详情暴露 DB `engine_uid` 与引擎上报 uid 的比对结果（stale/matched）。

## 范围
- In：`runtime.py` `_resolve_runtime_by_uid()`（限频 ERROR + 指标）、`runtime_heartbeat_v2()._resolve_uids()` stale 段（限频 ERROR + 指标 + 心跳丢失告警）、`/settings/agent-runtimes`（列表+详情，引擎详情弹窗消费）比对字段、前端详情展示、回归用例。
- Out：v1 `body.runtime_id` 回退字段下线（阶段 5）；引擎宿主侧 uid 对齐（重注册/回填，运维动作，合入后由 sre 在部署阶段执行）。

## 验收（Issue 口径）
- [ ] Loki 1h 窗口 `engine_uid not found` 计数归零（或仅限频后一次性 ERROR）。
- [ ] 三引擎 uid 对齐由 sre 部署阶段执行（重注册/回填）。
- [ ] `stale_uids` 非空且 `runtime_ids` 为空场景有指标/告警 + 回归用例。
- [ ] `/runtimes` 详情可见 engine_uid 比对结果。
- [ ] 相关单测/回归通过，`make lint` 通过。
