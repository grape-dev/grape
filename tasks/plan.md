# Implementation Plan: Issue #162 — 系统监控分页增加「每页条数」选择器

## Overview
在系统监控页面概览 Worker/Runtime 分页栏和任务 Tab timeline 列表，新增「每页条数」选择器（10/20/50）。

## Architecture Decisions
- **独立状态变量**：Worker/Runtime 各自持有 `_workerPageSize` / `_runtimePageSize`，互不影响
- **复用 `_paginationWithPageSizeHTML` 通用函数**：统一翻页栏 UI，含 prev/next 按钮 + 页码 + rows_per_page 下拉框
- **空列表不渲染分页栏**：`workers.length === 0` 或 `_filteredRuntimes.length === 0` 时 pager 渲染为空
- **任务 Tab 新增完整分页**：`_timelinePage` / `_timelinePageSize` + `_filterTimelineIssues` 内分页

## Task List

### Phase 1: 状态与通用函数
- [x] Task 1: 替换 `_OBS_PAGE_SIZE` 为 `_workerPageSize` / `_runtimePageSize` / `_timelinePage` / `_timelinePageSize`
- [x] Task 2: 新增 `_paginationWithPageSizeHTML()` 函数

### Phase 2: 概览 Worker/Runtime
- [x] Task 3: 更新 `_rerenderWorkerGrid()` 使用 `_workerPageSize`
- [x] Task 4: 更新 `_rerenderRuntimeTable()` 使用 `_runtimePageSize`
- [x] Task 5: 更新 `renderOverview()` 使用新页面尺寸 + 新分页栏
- [x] Task 6: 更新 `updateOverview()` 使用新页面尺寸 + 新分页栏

### Phase 3: 任务 Tab
- [x] Task 7: `_filterTimelineIssues()` 增加分页切片 + 分页栏
- [x] Task 8: 搜索 oninput 重置 `_timelinePage = 0`
- [x] Task 9: `#N` 定位搜索自动跳到目标页

### Phase 4: 测试与回归
- [x] Task 10: 更新 #117 测试断言（选择器可见行为变更）
- [x] Task 11: 新增 `test_observability_page_size_162.js` 回归测试（15 条）
- [x] Task 12: 全部 JS 测试通过

### Checkpoint: Complete
- [x] 3 个测试文件全部通过
- [ ] 自审查完成
- [ ] PR 创建完成
