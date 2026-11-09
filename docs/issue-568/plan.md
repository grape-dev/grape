# Plan: Issue #568 实现方案

## 根因
- `scenes detail`（#533/#534 修复后）只按「显式 --repo > 会话 token 绑定 repo_key > repo_var」的 **repo 作用域**解析场景；当会话 token 绑定仓库与目标场景归属不一致（跨流水线/关联仓库信任域会话）时 404。而 a2a handoff 的 `_scene_node_check` 以 **issue 绑定场景**（`get_issue_scene`）为权威解析并直接读 `devops_pipeline_scene.nodes_json`——两路查询路径不同、节点来源实现不同（detail 走 `get_pipeline_config` 含市场/官方兜底，a2a 走裸 SQL + pipelines.json），产生 404 vs 409-nodes 口径分歧。
- cross_scene_handoff 已写 `set_issue_scene`，但缺少「目标场景存在性显式校验 + 有效 ID 归一」，且场景解析双实现导致注入 GRAPE_SCENE_ID 可能指向解析不一致的场景。

## 改动
1. `src/api/routes/webhook.py`
   - `_scene_node_check`：节点来源统一改走 `get_pipeline_config(scene_id, repo_key)`（与 scenes detail 同源同缓存），保留 issue 绑定场景解析优先级与 in_scene 语义。
   - `_cross_pipeline_handoff`：新增目标场景存在性显式校验（`target_scene_not_found`）；提取 `_refresh_issue_scene_after_handoff(repo_key, issue_number, target_scene_id)` —— 校验 DB 行有效并刷新 issue 绑定场景为有效 scene_id（软失败仅告警，不阻断移交）。
2. `src/api/routes/scenes.py`
   - `get_scene_detail` 新增第二级「会话绑定」解析 `_resolve_session_bound_scene(scene_id, user)`：session token（未显式 --repo）→ `agent_tasks(session_key)` → `issue_id` → `get_issue_scene` → 命中请求 scene_id 时返回 `get_pipeline_config` 结果。仅补齐 a2a 同口径查询路径，不放宽权限（显式 --repo 优先、miss 仍 404）。
3. `tests/test_issue_568_scene_visibility_unification.py`
   - 两路一致：同一 scene_id 下 `scenes detail` 与 `_scene_node_check` 节点集合一致（含 DB / 市场兜底两来源）。
   - 会话绑定兜底：repo 作用域 miss（会话绑定仓库 ≠ 场景归属仓库）时按 issue 绑定命中；显式 --repo 误指仍 404。
   - 移交刷新：`_refresh_issue_scene_after_handoff` 将 issue 场景刷新为目标有效 ID；`_resolve_scene_id`（GRAPE_SCENE_ID 注入源）读到新场景；目标场景不存在 → `target_scene_not_found`。
4. `docs/issue-568/` spec/plan/task + runbook 降级路径指引（见下）。

## runbook Step 5 降级路径（技能资产不在本仓库，交付为指引文本）
issue-cli runbook Step 5（场景节点预校验）建议补充（#129 口径）：

```bash
SCENE_NODES="$(grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out 2>/dev/null)"
if [ -z "$SCENE_NODES" ] || echo "$SCENE_NODES" | grep -q '"code": *40[34]'; then
  # 降级：scenes detail 不可用（404/403，如 scenes list 对 tk_ 维持 user_only）时，
  # 以 a2a handoff 409 响应携带的 nodes 为权威兜底（Issue #568/#129 口径）
  SCENE_NODES='{"nodes": []}'
fi
# …后续 nodes 校验逻辑不变；a2a 409 skipped=not_in_scene 响应中的 message.nodes 即权威节点清单
```

## 权限不放松
- 会话绑定兜底仅在「未显式传 repo + session token」时启用，且命中的场景必须归属 agent_tasks 绑定仓库的 issue；显式 --repo 行为不变（#534 口径回归锚点保持）。

## 不改动
- `scenes list` 对 tk_ 维持 user_only 403（#534 口径，降级由 runbook 兜底覆盖）。
- 场景编排 UI / #125（缺 --repo 参数）不在本 Issue。
