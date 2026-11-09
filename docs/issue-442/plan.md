# Plan: Issue #442 — 实现方案设计

## 总体架构
端到端新增「撤回」链路：前端 hover 撤回入口 → `app.showConfirm` 二次确认 → `POST /api/chat/messages/{msg_id}/recall` → 端点校验（本人 + role=user）→ 置 `deleted=1`（先置状态再终止，防竞态）→ 同轮 agent 消息一并软删除 → 终止该轮执行 → 前端原位置渲染撤回提示 +「重新编辑」追加输入框。

## 分层设计

### 1. 数据层（db_setup.py / session_manager.py）
- `chat_messages` 新增 `deleted INTEGER DEFAULT 0`（DDL + `_ensure_column` 幂等迁移，旧行默认 0，行为不变）
- `session_manager`：`get_messages` 返回 `deleted` 字段；新增 `get_message(msg_id)`、`recall_message(msg_id)`、`recall_agent_replies_by_task(task_id)`、`is_task_recalled(task_id)`

### 2. 任务管理（chat_task_manager.py）— direct 模式终止
- `ChatTask` 新增 `cancelled` 标志（Lock 保护）：`cancel()` 置位 + 推送一条 `cancelled` 事件 + `notify_done()`；`push_event` 在 cancelled 后丢弃后续事件（满足"不再输出状态更新"）
- `ChatTaskManager._run`：迭代前检查 `task.cancelled` → break；break 后显式 `gen.close()` 触发 agent_fn 的 finally（其内部按 `is_task_recalled` 跳过落库/推送）
- `publish`（外部驱动）：cancelled 后丢弃
- 新增 `ChatTaskManager.cancel(task_id)` 类方法

### 3. 后端端点（chat.py）
- `POST /messages/{msg_id}/recall`：404（不存在/非本人会话）→ 400（role!=user / 已撤回幂等返回 200）→ 置 deleted → 软删同轮 agent 行 → `ChatTaskManager.cancel(task_id)` → 远端模式经模块级 `_CHAT_ENQUEUE_MAP[chat_task_id]=enqueue_tid` 映射调用 `service.cancel_task`（复用 #136，worker 心跳感知杀引擎）
- `chat_submit`：`add_message` 返回值 `message_id` 加入响应，供前端 live 气泡绑定 `data-msg-id`
- `agent_fn` / `remote_agent_fn` 落库前检查 `is_task_recalled(task_id)`，已撤回则跳过 agent 回复写入与飞书推送

### 4. 前端（chat.js + locales + app.css）
- 历史渲染（switchConversation/renderChatMessages）与 live 渲染（doSend）统一：user 气泡带 `data-msg-id` + hover 显示撤回按钮（`.msg-recall-btn`）
- `_recallMessage(msgId)`：`app.showConfirm`（danger）确认后调端点；成功后：气泡替换为撤回提示（含「重新编辑」）、终止当前 SSE（abort + 清占位 + `_clearTask`）
- `_recallReEdit(msgId)`：`input.value = input.value ? input.value + '\n' + 原文 : 原文`（追加不覆盖），focus + 自适应高度；原文取自消息内容剥离 `[chat_img:]` 标记
- i18n：`chat.recall.action/confirm/hint/re_edit/failed`（zh/en）

### 5. API 文档（help/api/04-chat.md）
新增 `POST /api/chat/messages/{msg_id}/recall` 端点块（满足 test_api_docs_sync 契约一致性）。

## 竞态与风险
- 撤回时仍在流式推送：端点**先置 deleted 再终止**；`push_event` cancelled 后丢弃 → 双保险
- 存量数据：`deleted` 默认 0，无迁移脚本
- 前端冲突：改动收敛 chat 页面作用域，追加逻辑独立函数
