# Issue #520 Spec：Runtime 按 task/event 原子 claim + 许可对号 lookup（Spec v2 实施版）

## 目标
Runtime 新增「按 task/event 原子 claim」（bound claim）能力与「许可对号 lookup 端点」，补全端到端许可链闭环：
外部执行器凭一次性许可按 `expected_event_seq` 查到 `task_id`，再携带 `expected_task_id + expected_event_seq`
原子领取指定任务，避免一次性 SRE 许可被队列中更旧任务消费。

不扩大凭据/仓库/角色权限，不授予生产部署批准。零 schema 变更，claim/claim_by_tag 主路径（未传 expected）零改动。

## 开发基准
- 目标分支：`weekly-08-mys`（`$GRAPE_WORK_BRANCH` 注入，权威）
- 开发分支：`feat/issue-520`

## 现状（代码基线，weekly-08-mys）
- `RuntimeClaimBody`（src/api/routes/runtime.py:428-431）仅 `supports_obs_url` / `prefer_fallback`。
- `POST /api/runtime/runtimes/{runtime_id}/tasks/claim`（runtime.py:491）→ `service.claim(runtime_id)`（src/runtime/service.py:1054），
  FIFO（`ORDER BY created_at ASC LIMIT 1`），db_lock 内领取并置 running。
- `POST .../claim-by-tag/{tag}`（runtime.py:565）→ `service.claim_by_tag(tag, runtime_id)`（service.py:1429），同样 FIFO + scope 隔离。
- rta_ 凭据无任务查询通道（`GET /runtimes/{id}/tasks` 为账号级鉴权），许可链「对号」缺口属实。

## 实施变更（四件套，同 PR 同批部署）

### 1. RuntimeClaimBody 扩展
- 新增可选成对字段 `expected_task_id: str = ""`、`expected_event_seq: int = 0`。
- pydantic `model_validator` 强制成对：只传其一 → 422（明确错误信息：must be provided together）。

### 2. bound claim 入口（独立新路径，fail-closed）
- `POST /api/runtime/runtimes/{runtime_id}/tasks/claim-bound`
- `POST /api/runtime/runtimes/{runtime_id}/tasks/claim-by-tag-bound/{tag}`
- 鉴权：`_require_credential`（rta_）+ 归属校验（`agent_runtimes.credential_id` 必须等于当前凭据，否则 403）。
- body 必须携带成对 expected 字段，缺失 → 400。
- 服务层 `claim()/claim_by_tag()` 增加 `expected_task_id/expected_event_seq` 参数（默认 None 保持原 FIFO 路径）：
  - 同一 db_lock/事务内，将 expected 条件与原有 runtime/池/状态/串行槽（及 tag 版的 tag/scope）条件一并校验（SQL WHERE 增加过滤）。
  - `pool_engine_ids` 广播范围不含本引擎 → 返回 None（fail-closed）。
  - 未匹配 → 返回「无任务」（`{"task": null}`），不领取、不修改任何其他任务；**绝不回退 FIFO**。
- 旧 claim/claim-by-tag 路由在 expected 成对提供时同样透传给服务层（in-place 原子守卫）；未传 expected 保持 FIFO 零改动。

### 3. 能力协商端点（只读）
- `GET /api/runtime/runtimes/{runtime_id}/tasks/claim-capabilities`
- 鉴权同上（rta_ + 归属校验）；返回 `{runtime_id, capabilities: {bound_claim: true, task_lookup: true}}`。
- 未部署节点 404 → 客户端保持许可关闭，不回退通用 claim。

### 4. 许可对号 lookup 端点（XOcto 补充设计）
- `GET /api/runtime/runtimes/{runtime_id}/tasks/lookup?expected_event_seq=N`
- 鉴权同 `/runtimes/{id}/tags`（`_require_credential` + 归属校验，他人 runtime → 403，无凭据 → 401）。
- 查询条件：完整照抄 claim 的候选 WHERE 骨架（runtime_id 精确匹配 + 共享池 `runtime_id=0 AND pool_agent_type != ''`
  + 串行槽 NOT EXISTS + `status='queued'`）+ `event_seq=?` 精确匹配。
- 返回：命中仅身份字段 `{task_id, issue_id, event_seq, agent_type, created_at}`；未命中 `{"task": null}`。
- `event_seq` 缺失或 <=0 → 400 明确拒绝；纯 SELECT 无副作用（不翻状态、不返回 payload/env/凭据）。

## 完整许可链
授权方签发许可（绑定 runtime+event_seq）→ lookup 对号取 task_id → bound claim 原子领取 → 领取成功且回执匹配才消费许可；
空结果保留许可；网络结果未知进入关闭的 uncertain 态，不自动重开/重派。

## 验收用例（12 条，tests/test_issue_520_bound_claim.py 全覆盖）
1. 队列有旧任务 A、许可目标 B：bound claim 只领 B，A 状态不变。
2. task/event 任一不匹配、目标不在作用域、串行槽不可用：不修改其他任务，返回无任务。
3. expected 只传一项：请求被明确拒绝（4xx）。
4. 未传 expected（旧路径）：现有 FIFO 行为保持。
5. 能力协商端点返回 200 且含 bound_claim / task_lookup 两个能力位（能力未知/404 时客户端保持关闭，不回退）。
6. 空队列 bound claim 返回 null 的回归覆盖。
7. lookup 对号成功 + bound claim 闭环。
8. lookup 未命中（未入队/已被领走）返回 `{"task": null}`。
9. 他人 runtime_id 调 lookup/claim-bound：403（凭据归属校验）。
10. event_seq=0 或缺参：明确 4xx 拒绝。
11. claim-capabilities 含 task_lookup 能力位。
12. FIFO 回归：现有 claim/claim-by-tag 用例全部保持通过（主路径零改动）。

## 影响面
- 服务端：runtime.py 新增 4 路由 + RuntimeClaimBody 扩展；service.py claim/claim_by_tag 加 expected 分支 + lookup 函数（约 120 行）。
- 零 schema 变更、无迁移；现有 worker（FIFO）零影响；前端零影响。
- 新端点为 rta_ 凭据级路由，不进对外 OpenAPI 契约（与现有 claim 端点一致）。
