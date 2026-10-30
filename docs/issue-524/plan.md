# Plan: Issue #524 — apply_scene 按节点结构化回执

## 改动点

### 1. `src/api/routes/scenes.py`
- `ApplySceneRequest` 增加可选字段 `strict: bool = False`。
- 重写 `apply_scene` 节点循环（原 1752-1808 行）：
  - 每个节点产出回执 `{agent_type, skill, status, effective_runtime_id, declared_runtime_ids, reason}`。
  - 声明引擎（`engine_runtime_ids` 非空）：
    - 首个 `online + healthy=1` 命中 → upsert，status=applied，计入 updated；
    - 全部不健康/离线 → status=skipped，不改写 session 绑定（保持当前生效引擎），reason 说明首个声明引擎状态与当前生效 runtime_id。
  - 未声明引擎：维持旧回退链（session 旧绑定 → 默认引擎）→ upsert，status=applied，计入 updated。
  - 节点处理异常 → status=failed，reason=异常信息，不计入 updated。
  - `strict=true` 且存在 skipped（声明引擎不健康）→ 回滚本次事务，抛 HTTPException 409（返回按节点原因）。
  - 返回体增加 `nodes`（回执数组）与 `applied/skipped/failed` 计数；`updated` 仅统计 applied。

### 2. `src/api/routes/agents.py` `get_agent_detail`
- session 查询已有 `runtime_id` 列，回填 `session_data["runtime_id"]`。
- 返回体新增 `declared_runtime_ids`（场景 node 声明）与 `effective_runtime_id`（session 实际绑定），前端可据此展示两者差异。

### 3. 测试 `tests/test_issue_524_scene_apply_receipt.py`
- 对齐 `test_health_scorer.py` / `test_scene_apply_enable.py` 风格：直调 `apply_scene`，`data/test_sessions.db` 测试库，unique repo_key + finally 清理。
- 三类回归：目标引擎 online/healthy=false（skipped + strict 409）、恢复 healthy 后重新 apply（applied 生效 B）、部分节点成功（混合回执 + updated 只计 applied）。
- agents detail 声明/生效字段断言。

## 风险与兼容
- 返回体向后兼容：保留 `message` + `updated`，新增字段为增量。
- 默认 `strict=false` 维持"部分应用"旧行为语义（不健康节点不再被静默改绑）。
- skipped 节点不再执行 upsert：声明引擎不健康时不改写 session（skill/绑定保持原样），这是 Spec 要求的"keep runtime A"。
