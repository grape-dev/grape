# Plan: Issue #341 — 技术方案

## 现状链路分析
- `app.js nav()` 已支持 `#view/param` hash：`_pushHistory` 写入 `#tasks/341`，`_restoreNavAfterLogin` / `popstate` 均把 `parts[1]` 作为 param 传给 `nav('tasks', param)`。
- `view === 'tasks'` 分支当前忽略 param，直接 `renderTasks()`。
- `issues.js loadTasks()` 已有 Issue #166 的 `window._pendingIssueNumber` 机制：列表加载后按 `issue_number` 匹配并 `_openBoardDetail(id)` 打开详情；无匹配渲染列表。该机制目前由场景详情页跳转（dashboard.js）设置。

## 改动方案（纯前端，最小复用）
1. **app.js**：`view === 'tasks'` 分支改为 `renderTasks(param)` 透传深链编号。
2. **issues.js `renderTasks(param)`**：
   - param 为纯数字（`/^\d+$/`）→ 设置 `window._pendingIssueNumber`（复用 #166 匹配链路）+ `window._deeplinkIssueNumber`（标记来源为 URL 深链）。
   - param 非数字/无 param → 视为原 `#tasks` 行为，不动 `_pendingIssueNumber`（保留场景跳转设置时机），清 `_deeplinkIssueNumber`。
3. **issues.js `loadTasks()` 容错**：
   - 深链命中 → 打开详情，清 `_deeplinkIssueNumber`。
   - 深链无匹配 → `_render()` 回退列表 + Toast `tasks.deeplink_not_found`（区分于场景跳转的静默回退/seq 跳转分支）。
   - `loadTasks` 失败 catch 分支同步清理两个标志，避免残留。
4. **i18n**：`locales/{zh,en}/tasks.json` 新增 `deeplink_not_found` 键。
5. **刷新恢复**：`_pushHistory('tasks', '341')` 保持 hash 不被改写，F5 后 `_restoreNavAfterLogin` 重放 `nav('tasks','341')`，详情态自动恢复。

## 风险与对策
- 与场景跳转（#166）共用 `_pendingIssueNumber`：深链仅在其后设置/清理 `_deeplinkIssueNumber`，互不覆盖。
- 详情初始化时序：沿用 loadTasks 数据就绪后打开，无额外时序风险。
- 畸形 hash `#tasks/abc`：非数字 param 直接走原列表逻辑，不报错。

## 测试方案
新增 `tests/frontend/test_issue_341_tasks_deeplink.js`（node:test + vm 沙箱，mock DOM/API）：
1. `renderTasks('341')` 设置 `_pendingIssueNumber`/`_deeplinkIssueNumber`；
2. `renderTasks()` / `renderTasks('abc')` 不设置且清理深链标志（#tasks 零回归）；
3. loadTasks 深链命中 → `_openBoardDetail` 被调用、深链标志清除；
4. loadTasks 深链无匹配 → Toast 提示 + 回退列表；
5. 场景跳转链路（`_taskDetailFromScene.latestSeq`）不受深链影响。
