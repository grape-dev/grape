# Spec: Issue #524 — apply_scene 按节点结构化回执，不健康目标引擎标记 skipped

> Spec 已由 VOD/Issue Agent 发布于 Issue #524 评论（2026-10-29/30），本文件为开发侧存档副本。目标分支：`weekly-08-mys`。

## Objective
`src/api/routes/scenes.py` `apply_scene` 应用场景时按节点返回结构化回执，目标引擎不健康时显式标记 skipped 并提示当前仍生效引擎，仅实际生效节点计入 `updated`；agents detail 区分「场景声明配置」与「实际生效配置」。

## 根因
1. 引擎选择仅命中 `status=='online' 且 healthy==1` 的声明引擎，不命中时 `runtime_id` 保持 0。
2. `runtime_id=0` 时静默回退 `devops_agent_session` 旧绑定，无任何提示。
3. `updated += 1` 在 upsert 后无条件累加，5 个不健康回退节点全部计为成功。
4. 返回体只有 `message + updated`，无按节点状态、无跳过原因、未区分声明/生效配置。

## 验收标准
1. 回执结构：`nodes: [{agent_type, skill, status: applied|skipped|failed, effective_runtime_id, declared_runtime_ids, reason}]`。
2. 仅实际生效（applied）节点计入 `updated`；skipped/failed 不计入。
3. 目标引擎 online/healthy=false → 该节点 skipped，reason 说明不健康并提示当前仍生效 runtime；新增可选 `strict: bool`（默认 false 部分应用；true 时任一节点 skipped 则整单拒绝，不落库）。
4. agents detail 增加声明配置（declared_runtime_ids）与生效配置（effective_runtime_id）区分字段。
5. 回归测试（对齐 `tests/test_health_scorer.py` 风格）覆盖三类场景：目标引擎不健康、恢复 healthy 后重新 apply、部分节点成功。

详细内容以 Issue #524 评论中的修复 Spec 为准。
