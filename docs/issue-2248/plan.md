# Plan: Issue #2248

## 方案
1. 服务端 `src/api/routes/webhook.py`
   - 新增 `_A2A_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "timeout")`。
   - 新增 `_force_round_idempotency_key(repo_key, base_key, existing_seq)`：
     派生 `<base>-fr-<seq>`（占用则 `-fr-<seq>-<n>`），基键截断保证 ≤128。
   - 新增 `_a2a_idempotent_hit_response(existing, agent_name, source_seq, message)`：
     统一命中响应，输出 `idempotent` / `existing_status` / `replayable` / `event`。
   - 在 `_idem_key` 格式校验后、鉴权/命中查询前解析 `_force` / `_queue` /
     `_a2a_ctx_key`；命中时：
     - `force=true` 且旧事件终态 → 换派生键继续走落库（现有 `force` 分支直写事件）；
     - 否则返回 `_a2a_idempotent_hit_response`（保持原事件幂等语义）。
   - 删除原先 2058 行附近的重复解析，避免二次覆盖。
   - 并发撞键回查分支复用同一响应 helper。
2. 契约/help
   - `A2AHandoffBody.force` description 补充「终态命中可越过幂等短路」与轮次键建议。
   - `contracts/a2a_handoff.schema.json`、`contracts/a2a_handoff_response.schema.json`
     增加 `existing_status` / `replayable` 说明。
   - `help/api/12-a2a.md` 与 `docs/openapi/12-a2a.md`：字段表、命中响应示例、
     判定表（running / 终态 × force）与修复轮键建议同步。
   - 各 Agent runbook（grape-skills 仓）判定表按同一分支（`idempotent` / `existing_status` /
     `replayable` → 等待 / force 重派 / 新轮次键）联动，不在本仓改，PR 备注联动清单。
3. 回归测试 `tests/test_issue_2248_a2a_idempotency_force_replay.py`
   - `test_force_true_replays_terminal_key_hit`：同键 + force → 新 seq、事件数 +1。
   - `test_no_force_returns_existing_event`：同键无 force → 原 seq、idempotent、
     existing_status=completed、replayable=true、事件数不变。
   - `test_new_round_key_dispatches_new_event`：轮次/sha 新键 → 新事件。
   - `test_force_true_active_hit_still_returns_existing`：活跃命中即使 force 也不重派。

## 影响面
- 仅 A2A 幂等命中路径与响应字段；不改派发门禁策略、不改事件表唯一索引。
- 兼容存量固定键调用方：无 force 时幂等语义与响应（新增字段）不变。