# Addendum: Issue #407 附件保活 — ChatAgent 图片/视频附件闲置返回不丢

> 用户追加反馈（2026-10-23）：PR #598 已修复 ChatAgent 输入框文本草稿（commit ecade319c），但 ChatAgent 的图片/视频附件在页签闲置 >60s 返回后仍丢失。QA 已在 weekly-08 源码复核确认根因。本次在同一改动面（PR #598 / feat/issue-407）上扩展覆盖附件。

## 根因（附件缺口）

1. `app.js:1153` visibilitychange 守卫条件为 `!window._dashboardEditing`，附件 pending 时无任何置位/守卫，60s 后 `app.nav('chat')` 整页重渲染。
2. `chat.js:575` `let _pendingImages = []`、`chat.js:648` `let _pendingVideo = null` 仅存内存 dataURL，`renderChat` 重建后不回填预览，且无任何持久化。

## 方案

在既有 chat 草稿修复（chatInput 文本，commit ecade319c）同一改动面上扩展，分三层：

### 1. 编辑态守卫（图片 + 视频）

- 新增 `window._chatEditing` 标志：`_pendingImages.length > 0 || !!_pendingVideo` 时置 `true`。
- `app.js` visibilitychange 守卫由 `!window._dashboardEditing` 扩展为 `!(window._dashboardEditing || window._chatEditing)`（对齐 dashboard 模式，dashboard 行为不回归）。
- `_updateChatEditingGuard()` 在每次附件增删/清空时调用，保持标志与内存状态一致。
- 闲置 >60s 返回时守卫命中 → 跳过 `app.nav()` 重渲染 → 图片/视频 dataURL 留在内存不丢。

### 2. 图片附件按会话粒度持久化（IndexedDB）

- IndexedDB 库名 `grapeChatDB`，object store `chatImages`，`keyPath=convId`，值 `{convId, images, ts}`。
- `_saveChatImages(convId, images)`：覆盖式写入；空数组 = 删除键。
- `_loadChatImages(convId)`：读取并校验 1 小时过期（过期清除返回空）；IndexedDB 不可用/无记录返回空（降级为仅守卫保活）。
- `_clearChatImages(convId)`：删除键。
- 全部 try/catch 降级，无抛错（隐身模式/配额满等场景不影响主流程）。

### 3. 视频仅守卫保活（不持久化）

- 视频附件体积大（≤50MB），强持久化成本高且易触发配额；按需求「视频仅守卫保活不强持久化」处理。
- 视频仅靠守卫防止闲置重渲染丢失；切换会话/硬刷新后丢失（接受）。

### 清除时机（发送/移除/切换会话/删除会话）

| 时机 | 行为 |
|------|------|
| 发送（doSend） | `_clearAllImages()` 清内存 + 删 IndexedDB 键；`_clearVideo()` 清内存；守卫复位 |
| 移除单图（_removeChatImage） | splice 后 `_saveChatImages`（空则删键）+ 守卫复位 |
| 移除视频（_removeChatVideo） | 置 null + 守卫复位 |
| 切换会话（switchConversation） | 持久化旧会话图片到 IndexedDB → 清空内存 `_pendingImages`/`_pendingVideo`（防串扰）→ 新会话消息渲染后异步回填新会话图片（含 `_currentConvId` 守卫防竞态） |
| 新建会话（newConversation） | 持久化旧会话图片 → 清空内存（新会话无附件） |
| 删除会话（_doDeleteConv） | 清除该会话 IndexedDB 图片键 |

### 覆盖场景对照（验收）

| 场景 | 行为 |
|------|------|
| 选图/视频 → 切走 >60s 返回 | 守卫命中跳过重渲染 → 附件留在内存不丢 |
| 选图 → 切换会话 A→B → 切回 A | A 图片持久化到 IndexedDB；切回时异步回填预览 |
| 选视频 → 切换会话 | 视频不持久化，切换后丢失（仅守卫保活） |
| 发送消息（含附件） | 清空内存 + 删 IndexedDB 键 + 守卫复位 |
| 移除单张图片 | 更新 IndexedDB + 守卫复位 |
| 删除会话 | 清除该会话图片记录 |
| F5 硬刷新 | 图片从 IndexedDB 恢复（视频丢失） |
| IndexedDB 不可用 | 降级为仅守卫保活（闲置返回不丢，切换/刷新不恢复图片），不抛错 |
| dashboard 编辑态 | `_dashboardEditing` 守卫原样保留，行为不回归 |

## 改动文件

| 文件 | 改动 |
|------|------|
| `src/api/static/js/app.js` | visibilitychange 守卫扩展为 `!(window._dashboardEditing \|\| window._chatEditing)`（+1 处） |
| `src/api/static/js/pages/chat.js` | 附件保活 helper（`_updateChatEditingGuard`/IndexedDB 图片存取 `_saveChatImages`/`_loadChatImages`/`_clearChatImages`）、各附件增删/发送/切换/删除会话集成点接入 |
| `tests/js/test_issue_407_chat_attachment.js` | 附件保活回归测试（守卫逻辑 + IndexedDB 存取/过期/隔离/降级 + 集成点静态断言 + PR #598 不回归） |
| `docs/issue-407/chat-attachment-addendum.md` | 本附录 |

## 不回归声明

- `app.js` 守卫保留 `window._dashboardEditing`（PR #598 dashboard 行为原样），仅扩展 `_chatEditing`。
- `dashboard.js` 草稿逻辑、`chat_task_<convId>`/`chatDraft_<convId>` 键空间均不触碰。
- IndexedDB 失败全部 try/catch 降级，最坏退化为「仅守卫保活」（与修复前等价于闲置不丢），不引入新崩溃路径。
