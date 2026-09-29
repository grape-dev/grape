# Spec: Issue #250 — revoke 物理删除 agent_runtimes 导致场景配置 engine_runtime_ids 失效

> Bug Fix | Priority: P1 | 2026-09-25

## 根因

`src/api/routes/settings.py:982` `revoke_runtime_credential` 执行 `DELETE FROM agent_runtimes WHERE credential_id=?`（物理删除），未清理场景配置 `devops_pipeline_scene.nodes_json` 中 node 的 `engine_runtime_ids` → 悬空引用 → `scenes.py:1442` `_validate_scene_engine_ids` 校验报 400。

## 修复方案（双重保障）

1. **revoke 时主动清理**（settings.py）：物理删除 agent_runtimes 前，扫描所有 `devops_pipeline_scene` 场景配置的 `engine_runtime_ids`，移除即将被删除的 runtime_id。
2. **场景校验优雅降级**（scenes.py）：`_validate_scene_engine_ids` 遇到失效 runtime_id 时跳过而非抛 400（兜底）。
3. **修正过时注释**（runtime.py:220）：注释「revoke 只置 offline，不删行」与实际不符，修正为物理删除。

## 影响模块

| 模块 | 改动 |
|------|------|
| `src/api/routes/settings.py` | `revoke_runtime_credential` 增加 `_cleanup_scene_engine_runtime_ids` 调用 |
| `src/api/routes/scenes.py` | `_validate_scene_engine_ids` 失效 ID 优雅降级 |
| `src/api/routes/runtime.py` | 修正 line 220 过时注释 |
| `tests/test_issue_250_revoke_scene_cleanup.py` | 新增全链路测试 |

## 验收标准

- [x] 吊销凭据后引用被删除 runtime_id 的场景配置不再报错
- [x] 场景配置 engine_runtime_ids 中的失效 ID 被自动清理
- [x] 场景校验遇到失效 runtime_id 时优雅降级（跳过而非 400 报错）
- [x] 所有现有测试通过 + 新增测试覆盖全链路
