# Spec: Issue #337 — 需求中心列表模式需求详情「移交到其他流水线」入口

> 目标基线分支：weekly-ljt（$GRAPE_WORK_BRANCH）

## 1. 背景与现状

- 需求中心（`src/api/static/js/pages/issues.js`）有「看板 / 列表」两种视图：
  - 看板模式卡片详情 `_openBoardDetail`（L356-373）操作区**已有**「移交到其他流水线」按钮（`_renderCrossPipelineButton`，Issue #329）。
  - 列表模式右侧详情面板 `_render`（L992）操作区只有「查看 Issue / 去对话 / 复制需求 / 确认完成 / 取消 / 恢复」——**没有移交入口**，用户必须切回看板视图才能移交。
- 既有跨流水线移交链路（Issue #329，全部复用不改核心）：
  - 前端直调 `POST /api/a2a`，payload 携带 `target_scene_id / target_node='vod' / carried_context / engine_config`；
  - `webhook.py a2a_endpoint` → `_cross_pipeline_handoff`（场景校验 / 重复拦截 / issue_state 回写 / `cross_scene_handoff` + `a2a_event` 审计事件 / 事件总线派发）。
- 现有弹窗的问题（本 Issue 范围内的体验缺口）：
  - 未排除「当前流水线」自身；
  - 无二次确认（Spec 明确要求防误触）；
  - 目标列表由前端拿 `/scenes/with-pipeline` 后自行过滤 vod 节点，权限语义散落在前端。

## 2. 需求目标（In Scope）

1. 列表模式详情操作区新增「移交到其他流水线」入口（按钮，与既有操作区样式一致）。
2. 点击弹窗：列出**有权限的**目标流水线（复用 `get_scenes` 的仓库作用域数据源，与既有 `/scenes/with-pipeline` 同一权限模型），**排除当前需求所在流水线**，仅含 vod 节点场景。
3. 提交复用既有 handoff 链路：后端代理调用 `a2a_endpoint`（与 `a2a-redo` 相同的服务内直调模式），统一鉴权（JWT/api_key + `check_repo_access`）与审计。
4. 成功/失败反馈：成功 toast + 刷新详情；skipped（重复移交/目标无 vod）显式提示，不静默失败；无权限 403 明确报错。
5. 二次确认防误触：提交前用既有 `_grapeConfirm` 弹确认框，展示目标流水线名称。

## 3. Out of Scope

- 不改 `_cross_pipeline_handoff` / A2A / 事件总线底层机制。
- 不做批量移交。
- 不改历史记录（events 表 `cross_scene_handoff` / `a2a_event` 审计）存储与展示。
- 看板模式既有入口行为保持（仅把其弹窗提交收敛到同一后端代理端点 + 同样加二次确认与排除当前流水线，交互一致性）。

## 4. 验收标准（对齐 Issue Spec）

- [ ] 列表模式 → 需求详情操作区可见「移交到其他流水线」入口。
- [ ] 目标列表按权限过滤（服务端 `check_repo_access` + `get_scenes(repo_key)` 仓库作用域）、排除当前流水线、仅含 vod 节点。
- [ ] 完成移交：目标流水线接续流转（a2a_event 派发）、原流水线状态正确更新（issue_state scene/stage 回写）、历史保留（审计事件）。
- [ ] 无权限 / 无可用目标：明确提示，不静默失败（403 透传、空列表文案、目标缺失 4xx）。
- [ ] 二次确认防误触。
- [ ] 现有测试通过 + 新增测试覆盖目标列表过滤与移交提交链路。
