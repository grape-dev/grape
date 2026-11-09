# Spec: Issue #460 — 新建需求时支持描述傍边一个小图标同步标题内容

> 目标分支: weekly-08-ljt | 来源: Issue #460 评论（Issue Agent 已发布完整 Spec）

## Objective
新建需求弹窗「描述」字段旁新增小图标，点击将「标题」内容同步追加到描述 textarea（已有内容时空行分隔追加不覆盖，标题为空无副作用，幂等去重）。

## Scope (In)
- `src/api/static/js/pages/issues.js` 的 `openTaskCreateModal`：描述标签旁渲染同步图标 + 点击事件
- `src/api/static/locales/zh/tasks.json`：新增 `sync_title_to_desc` 文案
- `src/api/static/locales/en/tasks.json`：新增 `sync_title_to_desc` 文案

## Out of Scope
- 复制需求弹窗 `openTaskCopyModal`（复制场景标题/描述已预填）
- PR 干预创建弹窗
- 后端接口变更（`POST /api/issues/{owner}/{repo}` 提交行为不变）

## Acceptance Criteria
- [ ] 描述旁出现同步图标，hover 显示双语提示
- [ ] 点击后标题出现在描述中；已有内容追加不覆盖（空行分隔）
- [ ] 标题为空时点击无副作用
- [ ] 重复点击幂等：描述已包含标题内容时不重复追加
- [ ] 创建流程不受影响（POST /api/issues/{owner}/{repo} 提交行为不变）
- [ ] 现有测试通过 + 新增测试覆盖同步逻辑
