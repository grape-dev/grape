# Plan: Issue #1564

## 方案
1. **校验器改造（R3）**：将 `_validate_scene_skill_scope` 拆为
   `_collect_scene_skill_scope_violations`（收集式、不抛异常）+ 校验包装。
   新增参数 `allow_own_user`（默认 True 保持 #657 口径）与 `expected_scope`（默认 repo）。
   400 detail 携带 `violations[]` 及顶层兼容字段。
2. **市场模板保存（R1）**：`create_market_scene` 使用请求 repo_key；`update_market_scene`
   使用市场行自身 `repo_key`；两者均以 `allow_own_user=False` 校验。
3. **市场模板应用（R2）**：`apply_market_scene` 解析 nodes 后、写入前以应用者
   `username/id` 复核（默认 `allow_own_user=True`），保留技能已删除校验。
4. **存量体检（R5）**：新增 `GET /scenes/skill-scope-audit`（置于 `/{scene_id}`
   动态路由之前），只读查询 `devops_pipeline_scene`，输出 violations + 汇总计数。
5. **前端标红（R4）**：`_renderEditFlowCards` 读取 `window._sceneNodeViolations`，
   对 `data-node-id` 卡片加红框与提示；编辑/创建保存 catch 解析 `e.detail.violations`
   后重渲染；`selectAgentSkill` 修正后清除对应节点。
6. **运行期兜底（R6）**：`webhook.py` / `event_pipeline.py` 的
   `_skill_scope_mismatch` 分支保持原样。

## 影响面
- `src/api/routes/scenes.py`（校验器、market create/update/apply、audit 路由）
- `src/api/static/js/pages/dashboard.js`（节点卡片标红）
- `tests/test_issue_1564_skill_scope_market_paths.py`（新增）
- `docs/issue-1564/`（文档）

## 兼容性
- 既有 #657 调用沿用默认参数，行为不变。
- 400 detail 只新增字段，不删除既有 `code/message`。
- 体检接口只读，无数据库写入。