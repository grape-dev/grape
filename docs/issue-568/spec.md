# Spec: Issue #568 — scenes detail 与 a2a handoff 场景可见性不一致：404 vs 409 nodes

> Developability: ready（Spec 评论 id=197160262）| 2026-10-30 | 开发基线：weekly-08-mys（跨流水线移交后以场景注入工作分支为准）

## Objective
- 统一 `scenes detail` 与 `a2a handoff` 对同一场景（GRAPE_SCENE_ID）的查询/可见性逻辑，修复跨流水线移交（cross_scene_handoff）后注入 scene_id 失效的问题，使 issue-cli runbook Step 5 的场景节点预校验链路可用。
- 用户价值：所有依赖场景预校验的流水线（issue→coding 交办守门）不再断裂；同簇反馈 #127/#129/#133/#139/#140 一并解决。

## Scope
### In Scope
- `scenes detail` API：支持会话级 token 查询会话绑定场景；`--repo` 归属定位逻辑与 a2a handoff 的 409 响应口径对齐。
- cross_scene_handoff：移交后刷新注入的 GRAPE_SCENE_ID 为目标场景有效 ID。
- runbook 降级路径：scenes detail 404/403 时，以 a2a handoff 响应携带的 nodes 作为权威来源（#129 建议）。
- `scenes list` WORKER 模式 403（user_only scope）导致预校验断裂的兜底。

### Out of Scope
- #125（scenes detail 缺 --repo 参数导致 404）——根因不同，不合并。
- 场景节点配置/编排 UI 改动。

## Technical Approach（实现口径）
1. **两路可见性统一（同一节点集合来源）**：
   - a2a 侧 `_scene_node_check`（src/api/routes/webhook.py）改为与 `scenes detail` 同源——统一走 `get_pipeline_config(scene_id, repo_key)`（DB-first + 市场/官方/pipelines 兜底 + 进程级缓存），两路节点集合按构造一致。
   - `scenes detail`（src/api/routes/scenes.py）补第二级解析：会话绑定（session token → agent_tasks → issue → `get_issue_scene`）；repo 作用域解析 miss 且未显式传 `--repo` 时，按 a2a 同一 issue 绑定口径命中场景。权限不放松：显式 `--repo` 指向无该场景的仓库仍 404。
2. **cross_scene_handoff 移交刷新**：`_cross_pipeline_handoff` 校验目标场景存在后，将 issue 绑定场景刷新为目标场景有效 ID（DB 行 scene_id 归一），后续 agent 任务注入的 GRAPE_SCENE_ID 即为目标场景有效 ID。
3. **runbook 降级路径（技能资产不在本仓库，交付为文档指引）**：issue-cli runbook Step 5 补充——`scenes detail` 404/403 时以 `a2a handoff` 409 响应携带的 nodes 作为权威兜底。

## Acceptance Criteria
- [x] 相同会话 token 下，scenes detail 与 a2a handoff 对同一 scene_id 返回一致节点集合
- [x] cross_scene_handoff 后注入的 scene_id 可通过 scenes detail 查询且节点有效
- [x] runbook Step 5 在 scenes detail 404/403 时有明确降级路径（a2a 409 nodes 兜底，文档交付）
- [x] 所有现有测试通过；新增测试覆盖上述场景

## 优先级
P1（阻塞 runbook Step 5 预校验，影响全部依赖场景预校验的流水线）。
