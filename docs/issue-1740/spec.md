# Spec: Issue #1740 — 工作区-需求中心页面问题

> 目标基线：weekly-mys（Spec 记录为 weekly-10，目标流水线复核为 weekly-mys，同源演进线）。

## 目标

修复需求中心两处状态/交互缺陷，使看板数据与筛选状态在页面跳转、刷新后保持一致。

1. 从需求详情点击「返回看板」后，各看板列立即按当前筛选正确渲染，无需手动刷新。
2. 「我的需求 / 仓库全部」视图模式持久化，刷新页面后保持用户上次选择。

## 范围

### In Scope

- `src/api/static/js/pages/issues.js`
  - 视图模式 localStorage 持久化（`tasks_view_mode`）。
  - 统一详情返回链路 `_backToBoard()`：清理详情态与详情轮询、缓存先渲染、后台校准、恢复场景轮询。
  - 统一看板分组映射 `_mapStatus` / `_mapStatusL`，兼容 `status` / `stage` 字段口径。
- `src/api/static/index.html`：`issues.js` 资源版本号刷新（v=134 → v=135）。
- `tests/frontend/test_issue_1740_view_mode_back_board.js`：回归测试。

### Out of Scope

- 后端 issues API 契约与数据模型。
- 其它页面与无关文案改版。

## 验收标准

- [ ] 进入任一需求详情后点击「返回看板」，六个看板列均正确展示当前筛选下的需求列表，无需刷新页面。
- [ ] 切换至「仓库全部」后刷新页面仍停留在「仓库全部」；切回「我的需求」刷新后保持「我的需求」。
- [ ] tab 选中态、请求数据源与看板数据一致，切换筛选不残留上一视图数据。
- [ ] 所有现有相关测试通过，新增回归测试覆盖以上场景。