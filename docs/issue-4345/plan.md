# Issue #4345 方案设计

## 1. 决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 基址解析归属 | `src/api/a2a_dispatch.py` 新增共享解析器，takeover 与重派两侧共用 | 避免重派侧复制探活实现再次漂移 |
| 探测原语归属 | 保留在 `takeover`（唯一实现） | 既有回归用例直接覆盖其行为；共享解析器只做编排与结果分类 |
| 上游 4xx 映射 | 422（通道不可用） | 409 保留给入口幂等/接管/自循环闸，语义分离 |
| 上游 5xx / 传输异常 | 502（本端 5xx） | 明确是网关/传输层问题，不与上游 4xx 混淆 |
| 幂等键 | 仅 event seq + 30s TTL | 重派语义按事件唯一 |
| 回执持久 | 进程内缓存 + 既有 `redispatch:{seq}` meta 时间戳作窗口闸 | meta 无值/读取失败时不回放，行为与旧版一致 |
| 失败审计 | `redispatch_failed` + `dispatch=failed` + reason + candidate_base_url | 保留可追溯性且不产生假成功 |

## 2. 实现路径

1. `src/api/a2a_dispatch.py`
   - `resolve_a2a_dispatch_target(runtime_id, exclude)`：候选顺序 = 显式 a2a >
     网关 /a2a > server_url 回退；前两者可信直接返回；回退先双重校验，
     失败在同机候选集内重选；无可用候选返回 `verified=False`（调用方必须短路）。
   - `dispatch_a2a(base, body, headers, timeout)`：POST 并把失败分类为
     `A2AUpstreamError`（有上游状态码）/ `A2ATransportError`（传输层）。
   - `classify_dispatch_http_status`：4xx/不可用 → 422；5xx/传输 → 502。
2. `src/domains/runtime/engine/takeover.py`
   - `_a2a_dispatch_base` 改为调用共享解析器返回 `base`（探测原语与缓存仍在本模块）。
3. `src/domains/runtime/routes/events.py`
   - 抽 `_redispatch_a2a_event(seq, evt)`：候选解析 → 未验证短路 → 派发 →
     错误逐档映射（422 / 502）；A2A 返回非 ok 仍 409（既有语义）。
   - `_run_redispatch`：回放闸（30s meta 窗口 + 进程内回执）→ 执行 →
     单点审计 + 单点 error 行；非 HTTP 异常统一转 HTTPException，杜绝 ASGI
     再落一条无 seq 的幽灵 error 行。
   - `_audit_admin_action` 增加 `extra` 字段（dispatch/reason/candidate_base_url）。
4. `src/kernel/prompt_builder.py`：`resolve_a2a_base_with_source` docstring 同步
   「重派与 takeover 共用共享解析器」约束，回退顺序与 source 标签不变。

## 3. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 共享解析器与 takeover 行为漂移 | 解析器只是编排，探活/候选/缓存的唯一实现在 takeover；takeover 用例全绿 |
| 进程内回执缓存跨请求残留 | 只作窗口内快速回放，窗口由 meta 时间戳兜底；meta 无值时不回放 |
| 422 与 409 语义混淆 | 422 只用于通道不可用；入口闸 409 原样保留 |
| 日志双写 | 失败 error 行只在 `_run_redispatch` 单点 emit，且 seq 为空直接跳过 |