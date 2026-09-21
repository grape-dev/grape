# TODO: Issue #162

- [x] Task 1: 替换 `_OBS_PAGE_SIZE` 为 `_workerPageSize` / `_runtimePageSize` / `_timelinePage` / `_timelinePageSize`
- [x] Task 2: 新增 `_paginationWithPageSizeHTML()` 函数
- [x] Task 3: 更新 `_rerenderWorkerGrid()` 使用 `_workerPageSize`
- [x] Task 4: 更新 `_rerenderRuntimeTable()` 使用 `_runtimePageSize`
- [x] Task 5: 更新 `renderOverview()` 使用新页面尺寸 + 新分页栏
- [x] Task 6: 更新 `updateOverview()` 使用新页面尺寸 + 新分页栏
- [x] Task 7: `_filterTimelineIssues()` 增加分页切片 + 分页栏
- [x] Task 8: 搜索 oninput 重置 `_timelinePage = 0`
- [x] Task 9: `#N` 定位搜索自动跳到目标页
- [x] Task 10: 更新 #117 测试断言（选择器可见行为变更）
- [x] Task 11: 新增 `test_observability_page_size_162.js` 回归测试（15 条）
- [x] Task 12: 全部 JS 测试通过
- [ ] 自审查完成
- [ ] PR 创建完成