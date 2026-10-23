# Addendum: Issue #407 遗留缺口 — ChatAgent 输入草稿持久化

> 用户澄清反馈（2026-10-23）：PR #598 已修复大部分子页面（编辑态守卫 + dashboard 草稿），但 ChatAgent 界面（chat 页面）输入框未发送文本在页签闲置 >60s 返回后仍丢失。QA 已在 PR head（059d76e）源码复核确认根因。

## 根因（遗留缺口）

1. `app.js:1153` visibilitychange 守卫条件为 `!window._dashboardEditing`，该标志仅由 `dashboard.js` 场景编辑/创建模式置位；chat 页无置位/守卫，60s 后 `app.nav('chat')` 整页重渲染。
2. `chat.js` 的 `chatInput` textarea 无任何草稿持久化/恢复逻辑，重渲染后为空。

## 方案（A：草稿持久化，QA 推荐方向）

对齐既有 `chat_task_<convId>`（chat.js:9-17）与 dashboard `dashDraft_*` 模式：

1. **草稿存储**：`chatDraft_<convId>` sessionStorage 键，值 `{text, ts}`，1 小时过期（过期即清除返回 null，老格式无 ts 不误清）。
2. **写入**：chatInput `input` 事件按会话粒度保存；文本清空时移除键。
3. **最后会话记忆**：`chatLastConv` 键（`{convId, ts}`，1 小时过期）记录用户所在会话；闲置返回/F5 重渲染后 `loadConversations` 自动选中**同一会话**（优先级：显式跳转 > 最后会话 > 第一个），否则草稿存对了键却显示不出来。
4. **回填**：`switchConversation` 消息渲染完成后回填当前会话草稿并自适应高度。
5. **清除**：发送（doSend 清空输入后）、手动清空输入（input 事件空文本）、删除会话时清除；切换会话时保存旧会话草稿 + 清空输入框（新会话只回填自己的草稿，杜绝串扰）。
6. **会话粒度隔离**：无 `_currentConvId` 时（尚未选中会话）不保存；发送失败（submit catch）把文本回填输入框并重建草稿（未上服务器的文本不能丢）。

### 覆盖场景对照（验收）

| 场景 | 行为 |
|------|------|
| 输入未发送文本 → 切走 >60s 返回 | nav 重渲染 → chatLastConv 自动选中原会话 → switchConversation 回填草稿 |
| 发送消息 | doSend 清空输入后 `_clearChatDraft`；失败则回填重建草稿 |
| 手动清空输入框 | input 事件检测空值 → removeItem |
| 切换会话 A→B | A 草稿实时保存留待切回；输入框清空，B 只回填 B 自己的草稿 |
| 删除会话 | `_doDeleteConv` 清除草稿键；删除当前会话作废 chatLastConv |
| 草稿超 1h | 恢复时清除返回 null |
| F5 刷新 | 同闲置返回路径（chatLastConv 自动选中 → 回填） |

## 改动文件

| 文件 | 改动 |
|------|------|
| `src/api/static/js/pages/chat.js` | 草稿 helper（`_chatDraftKey`/`_saveChatDraft`/`_restoreChatDraft`/`_clearChatDraft`）、chatLastConv 记忆 helper、textarea oninput 保存、loadConversations 自动选中优先级、switchConversation 保存/清空/回填/记忆、doSend 发送清除+失败恢复、newConversation/首发建会话记忆、_doDeleteConv 清除 |
| `tests/js/test_issue_407_chat_draft.js` | 回归测试 23 用例（存/取/清/过期/会话隔离/会话记忆/集成点静态断言/PR #598 不回归） |
| `docs/issue-407/chat-draft-addendum.md` | 本附录 |

## 不回归声明

- 不触碰 `app.js` visibilitychange 守卫与 `dashboard.js` 既有草稿逻辑（PR #598 已实现行为保持原样）。
- 方案 A 与 `chat_task_<convId>` 键空间正交（`chatDraft_` 前缀独立），互不干扰。
