# 跨流水线需求移交/接续机制 — 方案设计文档（Issue #329）

> 基于 #326 Spec v4 适配通用开发场景，目标分支 `weekly-ljt`。

## 1. 背景与目标

### 现状问题
- 跨场景流转只能新建/复制 Issue，同一需求出现多条并行记录，分析结论与开发实现脱节。
- 公共流水线（VOD/Issue 分析阶段）已完成的需求分析结论，无法被其他开发流水线直接接续开发。

### 目标
让「公共流水线分析完成的需求」能被其他开发流水线从 vod 节点开始接续开发，全程无需人工新建/复制重复需求。以 Issue 编号为锚点的场景重定向 + `carried_context` 传递。

## 2. 数据流

```
公共流水线 issue Agent
  ──A2A handoff(target_scene_id, target_node=vod, carried_context)──▶
目标场景 vod(快速复核 + 归属者变更)
  ──▶ issue(确认/微调 Spec + 引擎确认)
  ──▶ coding(按引擎配置开发)
  ──▶ qa
  ──▶ sre
```

事件链统一回挂原 Issue（`source_issue_number` 锚点）。

## 3. carried_context 完整字段

```json
{
  "source_issue_number": 329,
  "source_scene_id": "公共流水线场景ID",
  "source_scene_name": "公共流水线场景名",
  "spec_ref": "Spec v4 评论引用",
  "rating": "ready",
  "work_branch": "weekly-ljt",
  "target_assignee": "目标场景归属者",
  "engine_config": { "type": "runtime", "runtime_id": 1 } | null
}
```

## 4. 后端改动

### 4.1 webhook.py — `a2a_endpoint` 扩展

新增 `_cross_pipeline_handoff()` 函数，在 `a2a_endpoint` 中当 payload 含 `target_scene_id` 时触发：

1. **场景节点校验**：目标场景必须含 `target_node`（默认 vod），否则返回 `skipped: target_scene_missing_node`。
2. **幂等去重**：同 Issue + 同目标场景已有非终态 A2A 事件 → 返回 `skipped: duplicate_handoff`。复用 `issue_opened` 去重守卫（acd07be2）的原子抢占思路。
3. **issue_state 更新**：`set_issue_scene` + `set_issue_stage` 将 Issue 场景/阶段更新为目标场景/vod。
4. **carried_context 持久化**：写入 `issue.carried_context` 列（TEXT JSON）。
5. **assignee_changed 事件**：若 `target_assignee` 非空，更新 `issue.assignee` 并写入 `event_type=assignee_changed` 事件。
6. **cross_scene_handoff 审计事件**：写入 `event_type=cross_scene_handoff` 事件记录完整移交信息。
7. **A2A 分发**：创建 `a2a_event`（status=running，scene_id=target_scene_id），vod Agent 接收并执行快速复核。

### 4.2 events.py / issues.py — pipeline 视图扩展

`GET /api/issues/{issue_id}/pipeline` 返回新增字段：
- `carried_context`：携带的上下文 JSON
- `source_issue_number`：来源 Issue 编号
- `has_cross_scene_handoff`：是否有跨场景移交事件

### 4.3 pipeline.py — 事件类型识别

`_EVENT_TYPE_NAMES` 新增 `assignee_changed`、`cross_scene_handoff`，确保占位符识别逻辑覆盖新事件类型。

### 4.4 DB 迁移

`issue` 表新增 `carried_context TEXT DEFAULT ''` 列（`_ensure_column` 幂等迁移）。

## 5. 前端改动

### 5.1 issues.js — 移交入口 + 弹窗 + 接续标注

**移交按钮**：`_renderCrossPipelineButton(t)` — 在 Issue 详情页操作栏新增「⇄ 移交到其他流水线」按钮（completed/cancelled 不显示）。

**自定义弹窗** `openCrossPipelineModal(taskId)`：
- 目标场景选择器（`<select>`，仅含 vod 节点的场景）
- 归属者提示行（选择场景后显示）
- 引擎选择区（`<select>`，异步加载在线引擎，无引擎时灰显）
- 携带上下文预览（展示来源 Issue/评级/工作分支/目标场景）
- 确认/取消按钮

**提交逻辑** `crossPipelineSubmit(taskId)`：
- 构造 `carried_context` JSON
- `POST /api/a2a`（agent=vod, payload 含 target_scene_id/target_node/carried_context/engine_config）
- 成功 → `app.showToast()` + 刷新流水线区域
- skipped → 根据 skip 原因显示对应提示

**接续来源标注**：`_renderPipelineSection` 中当 `source_issue_number` 非空时，在流水线卡片标题下方新增蓝色标注行「⇄ 接续自 #{source_issue_number}（{source_scene_name}）」。

**跨场景事件链标注**：事件历史列表中 `cross_scene_handoff` / `assignee_changed` 事件用「⇄」图标 + 场景名 badge 标注。

### 5.2 pipeline.js — 跨场景标签 + 事件链标注

**跨场景标签**：`_renderCrossSceneBadge(pipe)` — 流程图标题栏新增场景切换标签，当 stages 中含 `cross_scene_handoff` 事件时显示「⇄ 跨场景移交 → {scene_name}」badge。

## 6. 边界场景处理

| 场景 | 处理 |
|------|------|
| 目标场景缺失 vod 节点 | `skipped: target_scene_missing_node`，前端提示 |
| 重复移交同一 Issue 到同一场景 | `skipped: duplicate_handoff`，前端提示已拦截 |
| 原 Issue 已关闭 | A2A 层 cancelled 检查拦截（已有逻辑） |
| 无在线引擎 | 引擎选择器灰显，使用默认引擎 |
| carried_context 列不存在 | DB 迁移自动补列；写入失败静默跳过 |

## 7. 验收标准映射

| 验收项 | 实现位置 |
|--------|----------|
| 公共流水线 Issue 移交目标场景后 vod 接收上下文 | webhook.py `_cross_pipeline_handoff` |
| 无新建重复 Issue | 复用 issue_opened 去重守卫 + `duplicate_handoff` 检查 |
| 原 Issue pipeline 可见跨场景事件链 | issues.py pipeline 返回 `has_cross_scene_handoff` + 事件列表 |
| 重复移交被拦截 | `_cross_pipeline_handoff` 步骤 2 |
| 归属者自动变更 + 事件记录 | 步骤 5 `assignee_changed` 事件 |
| 移交弹窗支持引擎选择 | issues.js `openCrossPipelineModal` |
| 边界场景有明确错误提示 | skipped 原因映射 + 前端 toast |
| UI 遵循 Apple 设计规范 | 0.5px border / rounded-lg / SF Pro / 自定义 DOM |
| 新增文案走 I18N | zh/en issues.json + pipeline.json |
| 方案设计文档合入仓库 | 本文档 |

## 8. 受影响模块

| 模块 | 改动类型 | 说明 |
|------|---------|------|
| `src/api/routes/webhook.py` | 修改 | `a2a_endpoint` 扩展 + `_cross_pipeline_handoff` 新函数 |
| `src/api/routes/issues.py` | 修改 | pipeline 端点返回 carried_context + source_issue_number |
| `src/api/routes/pipeline.py` | 修改 | `_EVENT_TYPE_NAMES` 新增事件类型 |
| `src/api/db_setup.py` | 修改 | issue 表新增 `carried_context` 列 |
| `src/api/static/js/pages/issues.js` | 修改 | 移交按钮+弹窗+引擎选择+接续标注 |
| `src/api/static/js/pages/pipeline.js` | 修改 | 跨场景标签+事件链标注 |
| `src/api/static/index.html` | 修改 | JS 版本号 v108→v109 / v43→v44 |
| `src/api/static/locales/zh/issues.json` | 修改 | 新增 cross_pipeline 词条 |
| `src/api/static/locales/en/issues.json` | 修改 | 新增 cross_pipeline 词条 |
| `src/api/static/locales/zh/pipeline.json` | 修改 | 新增 cross_scene_handoff 词条 |
| `src/api/static/locales/en/pipeline.json` | 修改 | 新增 cross_scene_handoff 词条 |
| `docs/weekly-ljt/cross-pipeline-handoff-design.md` | 新增 | 本文档 |
