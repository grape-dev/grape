# Task: Issue #1740 — 需求中心视图持久化与返回看板修复

- [x] T1 视图模式持久化：新增 `tasks_view_mode` 读写函数并在切换时落盘
- [x] T2 初始化 `_tasksViewMode` 时读取持久化值（非法值回退 mine）
- [x] T3 统一详情返回链路 `_backToBoard()`，清理详情态/详情轮询
- [x] T4 返回时先用缓存 `_render()`，再后台 merge 校准，并恢复场景轮询
- [x] T5 仓库全部看板返回改走 `_boardLoadAll({merge:true})`，避免覆盖列游标缓存
- [x] T6 详情页与 PR 详情页返回按钮统一为 `_backToBoard()`
- [x] T7 统一 `_mapStatus` / `_mapStatusL` 映射，兼容 status/stage 口径
- [x] T8 新增回归测试 `tests/frontend/test_issue_1740_view_mode_back_board.js`
- [x] T9 刷新 `issues.js` 资源版本号（v=135）
- [x] T10 运行相关前端回归测试与自审查