# Plan: Issue #1740 — 需求中心视图持久化与返回看板链路

## 现状定位

- `_tasksViewMode` 为内存变量、默认 `mine`（`issues.js:41`），刷新即重置，导致「仓库全部」无法保持。
- 详情页「返回看板」直接 `loadTasks()`，而详情页会停止场景轮询；返回后不恢复轮询。
- 「仓库全部 + 看板」使用按列分页（`_boardFetch` / `_boardLoadAll` 合并到 `_tasksAll`）。返回时直接 `loadTasks()` 会以全局单页响应覆盖 `_tasksAll`，导致其余列无数据。
- `_mapStatusL` 仅覆盖部分 `stage` 口径（如 `stage=awaiting_input/cancelled/completed`），两套接口字段差异下分组可能不一致。

## 改动

### 1. 视图模式持久化

- 新增 `_TASKS_VIEW_MODE_KEY = 'tasks_view_mode'`。
- `_loadTasksViewMode()` 读取并校验合法值（`mine` / `repo_all`），异常/污染回退 `mine`。
- `_tasksViewMode` 初始化时读取；`_switchTasksViewMode` 切换时写入。
- 保证 tab 选中态、请求数据源（`/issues/mine` vs `/issues/{owner}/{repo}`）与看板数据同源。

### 2. 统一详情返回链路

- `_clearTaskDetailState()`：清理 `window._currentDetailTask` 与 `_taskPipeTimer`。
- `_resumeTasksAfterDetail()`：
  - 恢复 `_startScenePoll()`；
  - 先用 `_tasksAll` 缓存立即 `_render()`（各列先有数据）；
  - 仓库全部看板：`_boardLoadAll({merge:true})` 按列校准，避免全局分页覆盖列游标；
  - 其它模式：`loadTasks({merge:true})` 后台校准确认。
- `window._backToBoard()`：场景来源回退历史栈，其余回到看板；详情页及 PR 详情页返回按钮统一调用。

### 3. 分组映射统一

- 新增 `_mapStatus(t)` 作为单一事实源，`_mapStatusL` 委托之。
- 兼容 `status`：`active/processing/awaiting_input/pending_review/completed/cancelled`。
- 兼容 `stage`：`vod/issue/coding/qa/sre/processing/done/completed/pending_review/awaiting_input/cancelled`。
- 优先级：休息态/终态 > 待审/待澄清 > 进行中 > 待处理。

### 4. 测试与缓存版本

- 新增 `tests/frontend/test_issue_1740_view_mode_back_board.js`（8 组断言）。
- `index.html` 中 `issues.js` 版本号 v=134 → v=135。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 缓存先渲染与服务端短暂不一致 | 后台 merge 校准并补渲染 |
| localStorage 值异常/污染 | 读取时校验合法值，回退 mine |
| 后台全局分页覆盖列缓存 | 仓库全部看板走按列 merge，不走全局 loadTasks |
| 重复轮询/详情轮询残留 | 返回统一清理 `_taskPipeTimer`，`_startScenePoll` 自身去重 |