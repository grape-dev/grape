# Plan: Issue #402 — 复制场景实现方案

> **2026-10-21 Rework（用户评论 #402）**：应用户要求「仅在前端实现，不修改后端接口」，
> 撤销原后端 copy 端点方案（`POST /{scene_id}/copy` 及 `pipeline_config.py` 改动全部回退），
> 改为纯前端实现：复用既有 `GET /dashboard/{owner}/{repo}`（`_scenes` 含源场景完整配置 +
> `agent_engines` 当前用户可选引擎）与既有 `POST /scenes` 创建接口，后端代码零改动。

## 前端（纯前端实现，后端接口零改动）
1. `dashboard.js` `_renderLocalSceneDetailPage` 头部操作区新增「复制场景」按钮；
   新增 `window._copySceneFromDetail`：
   - 源配置：优先 `dashData._scenes`（含 nodes/技能绑定/engine_runtime_ids/描述/图标/PR 订阅/关联仓库），
     页面 `nodes` 兜底。
   - 引擎清洗：按 `agentEngines[tag]`（当前用户该角色可选引擎）过滤 `engine_runtime_ids`——
     失效/他人个人机器/他仓库引擎自动剔除；SRE 节点无剩余可用引擎时副本置 `enabled=false`
     （既有创建接口拒绝「SRE 启用 + 空引擎」，停用后可编辑副本时再选引擎）。
   - 名称：弹窗预填 `<源场景名>-copy`（maxlength=40 可编辑）；提交时基于已有场景名
     冲突自动追加序号（2..99）、超 40 字符截断。
   - 提交：`API.post('/scenes?repo=…', {name, description, scope:'repo', nodes, icon,
     pr_subscription_enabled, linked_repos})` → 成功 toast（引擎被剔除时附提示）+
     `app.nav('scene-detail-local', 新id)`。
2. i18n：`locales/{zh,en}/dashboard.json` 新增 `copy_scene`/`confirm_copy_scene`/
   `copy_scene_name_label`/`copy_scene_hint`/`scene_copied`/`copy_engines_dropped`/`copy_failed`。

## 测试
3. `tests/js/test_issue_402_scene_copy.js`：前端源码验证（按钮/弹窗/复用既有 POST /scenes/
   引擎清洗/SRE 停用/冲突序号/跳转/i18n key/后端零改动断言）。
   相关回归：`test_scene_apply_detail_refresh.js`、`test_issue_316_scene_repo_tab_removed.js` 通过；
   Python 场景相关测试与基线 功能分支 结果完全一致（无新增失败）。

## 已知边界（纯前端方案的取舍）
- 图标依赖 `dashData._scenes.icon`（既有接口已返回）；页面快照兜底路径下图标可能为空（回退默认图标）。
- official 市场模板不经项目场景详情页展示，本入口不覆盖（与原 Issue 范围一致）。

## 风险
- 引擎绑定跨用户导致创建接口 400 → 前端按 agentEngines 预清洗剔除
- SRE 空引擎被创建接口拒绝 → 副本该节点置停用并提示
- 名称超长/冲突 → 截断 + 序号追加；服务端 409 时 toast 报错可重试

