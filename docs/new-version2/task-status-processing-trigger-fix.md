# 任务状态"待处理→处理中"触发点修复方案（真实对话为准）

> 状态：设计方案（v0.1，待评审）｜ 分支：new-version2
> 现象：任务状态 `active→processing` 目前只在任务中心点"去对话"（`POST /tasks/{id}/start`）时触发；直接在 Chat Agent 页或飞书进入任务会话对话不会变更状态，不合理。

## 1. 现状

- **唯一 `active→processing` 触发点**：`POST /api/tasks/{id}/start`（`tasks.py:313-331`），即任务中心"去对话"按钮（`tasks.js:159-165`：先调 `/start` 再 `app.nav('chat', conversation_id)`）。
- **真实消息落库点**（`add_message(conv_id, "user", ...)`）**不触发**任何任务状态变更：
  | 入口 | 位置 |
  |---|---|
  | Web chat `/send` | `chat.py:175` |
  | Web chat `/send/stream` | `chat.py:287` |
  | 飞书 chat | `handler_async.py:663` |

## 2. 目标

**状态变更的标志 = 是否就这个任务开展了真实对话**，而非"去对话"的点击：
- 任务中心"去对话"后，在聊天里**发消息** → `processing`；
- Web Chat Agent 页**直接进任务会话**发消息 → `processing`；
- 飞书 `/task` **进入任务会话**发消息 → `processing`；
- 仅打开不发言 / 已 completed / bot_takeover → 状态不变（`active` 保持，终态不回翻）。

## 3. 修复方案

### 3.1 新增 `mark_task_processing_by_conversation(conv_id)`
- `get_conversation(conv_id)` → `conv_type=='task'` 时，将任务 `active → processing`：
  - 优先按 `conversation_id` 匹配任务；
  - 无则按 `repo_key + issue_number` 回退；
  - 幂等、仅 `status='active' → 'processing'`（不回翻 completed/bot_takeover）；
  - 限定 `tasks.user_id = conv.user_id`（只动本人任务）。

### 3.2 三处真实消息落库点调用
在 `add_message(conv_id, "user", ...)` 之后调用：
1. `chat.py` `/send`（web）
2. `chat.py` `/send/stream`（web 流式）
3. `handler_async.py` 飞书（`add_message(conv_id, "user", text)` 后）

### 3.3 `/start` 语义调整
"去对话"不再是状态标志：`/start` 改为**不翻转状态**（或前端不再调用，仅负责跳转）；状态只由真实消息触发。
- 同步 `tasks.js`：`openTaskConversation` 去掉 `/start` 调用（或保留但不再依赖返回值置状态）。
- 同步测试：`test_start_task_active_to_processing` 改为"点去对话后状态仍 active；任务会话收到真实消息后才 processing"。

## 4. 测试用例设计

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| T01 | web 去对话后发消息置 processing | 任务 active + 会话 | `/tasks/{id}/start` 后状态仍 active；`/send` 该会话后 → processing |
| T02 | web 直接进任务会话发消息置 processing | 任务 active | Chat Agent 页对任务会话 `/send` → processing |
| T03 | 飞书进任务会话发消息置 processing | 任务 active | 飞书消息写任务会话后 → processing |
| T04 | 幂等：已 processing 再发消息不变 | processing | 状态保持 processing |
| T05 | 终态不回翻 | completed/bot_takeover | 发消息后状态不变 |
| T06 | 非本人任务不翻转 | conv.user_id ≠ tasks.user_id | 状态不变 |
| T07 | 普通（非任务）会话不影响任务 | general 会话 | 无任务被翻为 processing |

## 5. 实施步骤

1. 新增 `mark_task_processing_by_conversation(conv_id)`（session_manager 或 tasks 模块）。
2. 三处 `add_message(conv_id, "user", ...)` 后调用。
3. `/start` 去状态翻转；`tasks.js` 去 `/start` 调用；更新测试 T01-T07 + 回归 → 部署。

## 6. 结论

以"真实任务对话"为 `processing` 触发点：Web 与飞书任何入口进入任务会话并发言，任务即进入"处理中"，统一、直观、符合业务语义。
