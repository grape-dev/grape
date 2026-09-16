# Spec: Issue #162 — 系统监控分页增加「每页条数」选择器

## Objective
系统监控页面中概览的两处分页（Worker 卡片 / Runtime 引擎表格）增加「每页条数」可选（10/20/50）；任务 Tab（timeline 任务列表）新增分页（含相同的「每页条数」选择器），避免数据量大时一次性渲染过多 DOM 元素。

## 背景
- 概览 Worker / Runtime 分页已在 Issue #117 实现，固定 `_OBS_PAGE_SIZE = 10`，仅有翻页按钮
- 异常任务 Tab 已在 Issue #123 实现 `anomalyPageSize` + `rows_per_page` 选择器（10/20/50）
- 任务 Tab（timeline issue 列表）目前无分页，所有 issue 一次性渲染

## 技术方案

### 概览：Worker 卡片 & Runtime 引擎
1. 将 `const _OBS_PAGE_SIZE = 10` 替换为独立状态：`_workerPageSize = 10`、`_runtimePageSize = 10`
2. 新增 `_paginationWithPageSizeHTML(page, totalPages, prevExpr, nextExpr, pageSize, pageSizeVar, rerenderExpr)` 通用分页栏，包含翻页按钮 + 页码 + `rows_per_page` 选择器（10/20/50）
3. `_rerenderWorkerGrid()` / `_rerenderRuntimeTable()` / `renderOverview()` / `updateOverview()` 均改为使用对应 `_*PageSize` 变量
4. 空列表（`workers.length === 0` / `_filteredRuntimes.length === 0`）时不渲染分页栏

### 任务 Tab
5. 新增状态：`_timelinePage = 0`、`_timelinePageSize = 10`
6. `_filterTimelineIssues()` 增加分页逻辑：先按搜索条件筛选，再按 `_timelinePage/_timelinePageSize` 切片渲染
7. 渲染后追加 `_paginationWithPageSizeHTML` 分页栏
8. `#N` 定位搜索：当 `locateIssueId` 存在时，将 `_timelinePage` 调整到包含目标 issue 的页
9. 搜索输入 `oninput` 重置 `_timelinePage = 0`

### 状态重置
10. `switchObsTab()` 切换 tab 时重置 `_timelinePage = 0`

## 验收标准
- [ ] 概览 Worker 分页：10/20/50 选择器存在，切换后正确渲染对应数量卡片，页码正确
- [ ] 概览 Runtime 分页：10/20/50 选择器存在，切换后正确渲染对应行数，页码正确
- [ ] 任务 Tab 分页：10/20/50 选择器存在，切换后正确渲染，页码正确
- [ ] 空列表/少量数据时，选择器仍可见，无翻页按钮，页码显示 "1 / 1"
- [ ] `switchObsTab` 切换 tab 后分页状态正确重置
- [ ] 搜索筛选后分页正确
- [ ] `#N` 定位搜索自动跳到目标页
- [ ] 现有 Issue #117 / #123 测试全部通过
- [ ] Issue #162 新增回归测试全部通过

## 代码风格
- JS（浏览器脚本），无模块化
- 全局变量/函数风格（遵循现有代码库风格）
- HTML 字符串模板（`innerHTML`）

## 测试策略
- 框架：Node.js 单文件测试（`node tests/js/*.js`）
- 现有：`test_observability_pagination.js`（#117，已更新）、`test_anomaly_page_size.js`（#123）
- 新增：`test_observability_page_size_162.js`（#162 回归测试）

## 边界
- Always: 测试通过后提交
- Never: 修改非 Issue 相关代码，修改后端逻辑，引入外部依赖
