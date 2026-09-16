# Issue #175 — 需求卡片页面反馈迟缓

> 设计方案 v4 | 2026-09-16 | 分支：feat/issue-175-task-card-feedback

## 1. 问题分析

### 1.1 原始问题

文件 `src/api/static/js/pages/issues.js` 中三个按钮处理函数均缺少即时反馈：

| 函数 | 问题 |
|------|------|
| `confirmTaskDone` | API 成功后仅 showToast，未调 loadTasks() 刷新列表，无 loading 状态 |
| `cancelTask` | 同上 |
| `resumeTask` | 同上，且无确认弹窗直接调 API |

对比 `changeTaskStatus` 在 API 成功后正确调用了 `loadTasks()` 刷新列表。

### 1.2 用户感知

点击按钮后页面无任何视觉变化（卡片状态不变、按钮不变），需等待 15 秒轮询（`_startScenePoll`）才可能刷新——且轮询条件只覆盖 `active`/`processing` 状态任务，已取消任务恢复后不在轮询范围内。

### 1.3 实现过程中发现的问题

| # | 问题 | 根因 | 影响 |
|---|------|------|------|
| A | 遮罩只盖右侧详情面板，列表仍可操作 | v2 遮罩目标为 .tsk-detail | 用户等待期间点其他任务导致 _taskSel 变化，loadTasks 回来后渲染的任务和操作的不一致 |
| B | 成功后遮罩一直不消失 | _withTaskLoading 的 .then 先调 loadTasks()（异步），遮罩等 loadTasks API 返回才随 DOM 替换消失 | 用户以为卡死 |
| C | 列表页点取消后跳到详情页 | _pendingTaskDetailId 无条件设置 | 列表视图用户被强制跳转详情页 |
| D | 状态变了但按钮不更新 | loadTasks 刷新 _tasksAll 后 _taskSel 仍指向旧对象引用（旧 status） | _render 用旧 _taskSel 渲染按钮，confirm/cancel/resume 按钮不跟着 status 变化 |
| E | API 超时后遮罩永远不消失 | catch 用 `e.name === 'AbortError'` 静默所有 abort，包括超时 abort | 超时 abort 无 e.silent 标记但被静默，遮罩卡死页面不可用 |
| F | "去对话"按钮无 loading | openTaskConversation 调 API.post('/tasks/{id}/start') 无反馈 | 接口慢时用户以为没点到反复点击 |
| G | 删除附件用裸 fetch 无超时保护 | _deleteTaskAttach 直接 fetch 不走 API.js | 无 30s 超时，无统一错误处理，用 alert 而非 toast |

## 2. 设计方案

### 2.1 交互流程

**confirmTaskDone / cancelTask（有确认弹窗）：**

1. 用户点按钮 → _grapeConfirm 弹窗
2. 用户点确认 → 弹窗关闭
3. 整个 pageContent 立即覆盖半透明遮罩，居中显示「处理中…」
4. 遮罩期间整个需求中心不可操作（天然防抖——遮罩挡住列表+详情所有交互）
5. API 调用
6. 成功 → 移除遮罩 → showToast 成功提示 → 若当前在详情页则设 _pendingTaskDetailId → loadTasks() 刷新列表+详情
7. 失败/超时 → 移除遮罩 → showToast 错误提示 → loadTasks() 刷新页面保证可用
8. 导航离开（非超时 abort） → 移除遮罩，静默忽略，不弹 toast 不刷新

**resumeTask（无确认弹窗）：**

1. 用户点按钮 → 直接覆盖遮罩 → 同上步骤 4-8

### 2.2 遮罩范围

遮罩整个 `#pageContent`（含搜索栏 + 列表 + 详情 + pipeline）：

| 视图 | 遮罩目标 | 范围 |
|------|---------|------|
| 列表视图（双栏） | `#pageContent` | 搜索栏 + 左侧列表 + 右侧详情（全部遮住） |
| 详情视图（单屏） | `#pageContent` | 返回按钮 + 按钮行 + 详情 + pipeline（全部遮住） |
| 看板视图 | `#pageContent` | 搜索栏 + 看板列 + 卡片（全部遮住） |

#pageContent 默认 position:static，追加遮罩前设 position:relative 确保 absolute 遮罩正确定位。

### 2.3 成功后保持视图 + 刷新数据

**列表视图**：成功后不设 _pendingTaskDetailId，loadTasks() 刷新 _tasksAll + _taskSel + _render()，留在列表页。

**详情视图**：成功后设 `window._pendingTaskDetailId = taskId`，loadTasks() 内部已有机制（L216-220）自动调 `_openBoardDetail(pid)` 重新渲染详情页，_currentDetailTask 获得新对象，按钮跟着 status 变化。

### 2.4 _taskSel stale ref 修复

loadTasks() 刷新 _tasksAll 后，_taskSel 仍指向旧对象引用（旧 status），_render 用旧 _taskSel 渲染按钮导致按钮不更新。

修复（loadTasks L211-216）：
```
if (_taskSel) {
  var updated = _tasksAll.find(t => t.id === _taskSel.id);
  _taskSel = updated || null;  // 刷新指向新对象
}
```

### 2.5 超时与异常恢复

API.js 已有 30 秒默认超时（`_DEFAULT_TIMEOUT = 30000`）。超时后 abort 触发 AbortError，但 `controller._timedOut = true` 时该 AbortError 不带 `e.silent` 标记（区别于导航离开的 abort）。

_withTaskLoading 的 catch 区分两种 abort：
- `e.silent = true`（导航离开）→ 移除遮罩，静默返回，不弹 toast 不刷新
- 无 `e.silent`（超时或网络错误）→ 移除遮罩 + 错误提示 + loadTasks() 刷新页面保证可用

catch 里调 loadTasks() 保证页面恢复可用——即使 API 失败，列表也会刷新到最新状态，不会卡在遮罩里。

loadTasks 自身的 catch 也清理 _pendingTaskDetailId，防止失败后残留导致下次 loadTasks 误跳详情。

### 2.6 详情页其他交互优化

**"去对话"按钮（openTaskConversation）**：active 状态任务点击时调 `API.post('/tasks/{id}/start')`，加按钮 disabled + 半透明防止重复点击。

**删除附件（_deleteTaskAttach）**：裸 fetch 改走 `API.del`（获得 30s 超时保护 + handleResp 统一错误处理），alert 改 showToast。

## 3. 改动清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `issues.js` | 新增 `_withTaskLoading` 函数 | 遮罩 #pageContent；taskId 参数设 _pendingTaskDetailId；成功后立即移除遮罩；catch 区分超时/导航 abort；catch 刷新页面保证可用 |
| `issues.js` | 重构 `confirmTaskDone` | 用 _withTaskLoading 包装，传 taskId |
| `issues.js` | 重构 `cancelTask` | 用 _withTaskLoading 包装，传 taskId |
| `issues.js` | 重构 `resumeTask` | 用 _withTaskLoading 包装，传 taskId |
| `issues.js` | 修改 `loadTasks` | 刷新 _taskSel 指向新对象；catch 清理 _pendingTaskDetailId |
| `issues.js` | 修改 `openTaskConversation` | "去对话"按钮加 disabled+半透明 loading |
| `issues.js` | 修改 `_deleteTaskAttach` | 裸 fetch 改 API.del；alert 改 showToast |
| `index.html` | issues.js 版本号 94→97 | 缓存刷新（rebase 后 base 已是 v=94，bump 到 v=97） |

## 4. 不做的事

- 不做乐观更新——Issue 标记可选，本期不实现
- 不改轮询逻辑——不碰 _startScenePoll
- 不改 _grapeConfirm 确认弹窗机制——100ms 竞态窗口概率极低不修
- changeTaskStatus 是死代码（UI 无调用），不处理

## 5. 验收标准

- [ ] 列表视图：点击「确认需求完成」→ 确认弹窗 → 整个需求中心遮罩「处理中…」→ API 返回后列表刷新，按钮跟随 status 变化
- [ ] 列表视图：点击「取消需求」→ 确认弹窗 → 同上
- [ ] 列表视图：点击「恢复需求」→ 整个需求中心遮罩「处理中…」→ API 返回后列表刷新，resume 消失 + confirm/cancel 出现
- [ ] 详情视图：点击三个操作按钮 → 遮罩 → API 返回后详情页保持，按钮跟随 status 变化
- [ ] 遮罩期间列表+详情均不可操作（防抖）
- [ ] API 失败时遮罩移除，提示错误，页面自动刷新恢复可用
- [ ] API 超时（30s）后遮罩移除，提示错误，页面自动刷新恢复可用
- [ ] API 请求期间导航离开时静默移除遮罩，不在其他页面弹 toast
- [ ] "去对话"按钮点击后 disabled+半透明，防止重复点击
- [ ] 删除附件走 API.del 有超时保护，错误用 toast 不用 alert
- [ ] 所有现有测试通过（基线已有的失败除外）
