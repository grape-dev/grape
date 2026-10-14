# Plan: Issue #337 — 实现方案设计

> 依据 spec.md；改动最小化，全部复用既有 #329 机制。

## 1. 总体设计

**后端代理模式**（Spec 关键设计决策：移交执行方式 = 后端代理，统一鉴权与审计）：

```
前端（列表/看板详情入口）
  → GET /api/issues/{issue_id}/handoff/targets      # 目标流水线列表（权限+排除当前+vod 过滤）
  → POST /api/issues/{issue_id}/handoff              # 移交提交（二次确认后调用）
        鉴权: require_no_session_token + check_repo_access
        校验: 目标场景存在 / 含 vod / 非当前场景 / 30s 幂等防连点
        执行: 服务内直调 a2a_endpoint（复用 a2a-redo 同模式）
              → a2a_endpoint → _cross_pipeline_handoff（不改）
```

两个新端点放在 `routes/issues.py`（`/api/issues` 路由天然按 issue.id 定位需求，与 pipeline/clarifications 等详情端点同族）；领域逻辑（目标列表计算）放 `issues_center.py`（Spec 指定模块，叶子模块防环形依赖）。

## 2. 后端改动

### 2.1 `src/api/issues_center.py` — `get_handoff_targets(repo_key, issue_number, current_scene_id)`

- 数据源：`get_scenes(repo_key=repo_key)`（与 `/scenes/with-pipeline` 同源，仓库作用域=权限过滤）。
- 过滤：
  1. 排除 `scene_id == current_scene_id`（当前流水线）；`current_scene_id` 由 `get_issue_scene` 解析（无记录 → "default"，default 也排除）。
  2. 仅保留 nodes 含 `id`（或 `tag`）== `vod` 的场景（与 `_cross_pipeline_handoff` 的目标节点约定一致）。
- 返回 `[{id, name, display_name, description, work_branch, scene_type}]`（前端弹窗所需最小字段）。
- 返回 `current_scene_id`，供前端提示/测试断言。

### 2.2 `src/api/routes/issues.py` — 两个端点

- `GET /{issue_id}/handoff/targets`：
  - `issue` 表查 `repo_key/issue_number` → `check_repo_access`（403 无权限显式报错）；
  - 调 `get_handoff_targets`；返回 `{targets: [...], current_scene_id, issue_number, repo_key}`。
  - 空列表照常返回（前端展示「无可用目标」），不算失败。
- `POST /{issue_id}/handoff`（body: `{target_scene_id}`）：
  - 同鉴权；校验：需求存在、目标非空、目标 ≠ 当前场景（409）、目标在 `get_handoff_targets` 结果内（404/400，防绕过权限直接点名其他仓库场景）；
  - 30s 幂等防连点（`a2a-handoff:{issue_id}` meta 键，复用 a2a-redo 模式）；
  - 组装 `A2AHandoffBody(agent='vod', payload={repository, account, issue_number, context, target_scene_id, target_node:'vod', carried_context, engine_config: None})`，`carried_context` 从 `get_carried_context` 读取（历史接续信息保留）+ 补 `source_scene_id/source_scene_name`；
  - `_redo_user = {**user, auth_type:'jwt'}` 服务内直调 `a2a_endpoint`（jwt 分支内部再校验 managed_repos，双保险，与 `a2a_redo_event` 完全同模式）；
  - 透传返回（dict 或 JSONResponse 解析）；`skipped` 原样透传给前端。

### 2.3 不改动

`webhook.py`（`a2a_endpoint` / `_cross_pipeline_handoff`）、`events.py`、`event_pipeline.py`、事件存储——全部原样复用。

## 3. 前端改动（`src/api/static/js/pages/issues.js`）

1. **列表模式详情操作区**（`_render` L992 按钮 row）：`${_renderCrossPipelineButton(_taskSel)}`——复用既有按钮渲染函数（看板入口同款），打开同一弹窗。
2. **弹窗 `openCrossPipelineModal`**：
   - 目标列表改为 `API.get('/issues/{id}/handoff/targets')`（后端过滤：权限 + 排除当前 + vod）；保留加载中/无可用场景/选择交互；
   - 空列表时禁用提交按钮（明确提示）。
3. **提交 `crossPipelineSubmit`**：
   - 改为 `API.post('/issues/{id}/handoff', {target_scene_id})`（后端代理）；
   - 提交前 `_grapeConfirm` 二次确认（展示目标流水线名称 + 需求号，复用既有防误触弹窗组件）；
   - 成功/skipped/失败分支提示保留；成功后刷新 pipeline 段 + `loadTasks()` 刷新列表状态。

## 4. i18n 与缓存

- `locales/zh/issues.json` + `locales/en/issues.json` `cross_pipeline` 段新增：`confirm_title`、`confirm_message`（带 `{scene}`/`{issue}` 占位）、`no_permission`、`no_target`、`err_invalid_target`、`duplicate_current`。
- `index.html` issues.js 版本号 `v=113 → v=114`（缓存刷新，tests/js/test_issue_329 断言的是 `pipeline.js?v=44` 与 issues.js 存在性，需同步更新该测试对 v=113 的依赖——检查确认该测试只断言 `issues.js?v=109`，实际是旧值，改为不破坏现有断言的 v=114 并更新测试）。

## 5. 测试计划（TDD）

- pytest `tests/test_issue_337_list_handoff.py`（sqlite 落数，仿 test_a2a_redo.py）：
  - targets：排除当前场景 / 仅含 vod / 空列表 / 无权限 403；
  - handoff：成功（事件落库 cross_scene_handoff + a2a_event，issue.scene_id 回写目标）/ 目标=当前场景 409 / 目标不在列表 400 / 30s 幂等 409 / cancelled 拒绝（复用 a2a_endpoint 行为）。
- JS `tests/js/test_issue_337_list_handoff.js`（源码级断言，仿 test_issue_290）：
  - 列表模式操作区包含 `_renderCrossPipelineButton(_taskSel)`；
  - 弹窗调用 `/handoff/targets`；提交调用 `POST /handoff`；提交前有 `_grapeConfirm` 二次确认；
  - i18n 词条存在。

## 6. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 目标列表权限泄漏（跨组织） | 服务端 `check_repo_access` + `get_scenes(repo_key)` 仓库作用域双保险；提交端点校验目标必须在列表内 |
| 误触移交 | 二次确认（展示目标名）+ 30s 幂等防连点 |
| 双流水线状态不一致 | 完全复用 `_cross_pipeline_handoff` 的 issue_state 回写与审计事件，不新写状态逻辑 |
