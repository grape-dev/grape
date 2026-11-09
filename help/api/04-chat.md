# Chat 对话接口（/api/chat）

> 对外公开，认证：`get_current_user_or_api_key`。
> scope（x-token-scope）：全部 = **user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## GET /api/chat/conversations — 会话列表
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Query | `repo_key`（可选） |
| 出参 | `[{id, user_id, repo_key, title, conv_type, issue_number, created_at, last_active_at}]` |
| 返回 | 200 |

---

## POST /api/chat/conversations — 新建会话
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（NewConversationBody）：`{"repo_key": "owner/repo"}`

| 出参 | `{id, repo_key, title, ...}` |
| 返回 | 200 |

---

## DELETE /api/chat/conversations/{conv_id} — 删除会话
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{conv_id}` |
| 返回 | 200/204；404 |

---

## POST /api/chat/conversations/ensure-issue — 确保 Issue 会话
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（EnsureIssueConvBody）：`{"repo_key": "owner/repo", "issue_number": 5}`

| 出参 | `{id, ...}` |
| 返回 | 200 |

---

## GET /api/chat/messages/{conv_id} — 会话消息
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{conv_id}` |
| Query | `limit`（可选）、`before_ts`（可选） |
| 出参 | `[{role, content, created_at, ...}]` |
| 返回 | 200 |

---

## POST /api/chat/messages/{msg_id}/recall — 撤回消息
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{msg_id}`（消息 id，`chat_submit` 响应 `message_id` 或 `GET /messages` 列表 `id`） |
| 出参 | `{recalled: true, message_id, task_id, terminated}`（软删除 + 终止该轮 agent 执行） |
| 返回 | 200；400（非 user 消息）；404（消息不存在 / 非本人会话） |

说明（Issue #442）：仅限本人发送的消息；软删除保留原文供「重新编辑」；同轮 agent 回复一并软删除；direct 模式经 ChatTaskManager 终止，远端模式复用任务取消（#136）。

---

## POST /api/chat/send — 发送消息（普通）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（ChatMessage）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `message` | string | 消息内容（必填） |
| `repo_key` | string | 仓库 |
| `conversation_id` | string | 会话 ID |
| `image_data` | string | 图片 base64（可选） |

| 出参 | 完整 assistant 回复（text/结构化） |
| 返回 | 200 |

---

## POST /api/chat/send/stream — 发送消息（SSE 流式）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

同上请求体。返回 SSE 流：`data: {delta: "..."}` … `data: {done: true, ...}`。

---

## POST /api/chat/send/async — 异步发送
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

同上请求体。出参：`{"task_id": "...", "status": "accepted"}`，随后轮询 `GET /api/chat/task/{task_id}/status`。

---

## GET /api/chat/task/{task_id}/status — 异步任务状态
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{task_id}` |
| 出参 | `{"status": "processing/completed/failed", "result": "..."}` |
| 返回 | 200 |

---

## GET /api/chat/task/{task_id}/stream — 异步任务流式结果
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{task_id}` |
| 出参 | SSE 流 |
| 返回 | 200 |

---

## POST /api/chat/feedback — 提交反馈
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（FeedbackBody）：`{"seq": int, "feedback_type": "create_issue", "comment": "..."}`

| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## GET /api/chat/config — 获取 Chat 配置
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Query | `repo`（可选） |
| 出参 | `{backend, backend_name, settings, personal_engines, ...}` |
| 返回 | 200 |

---

## PUT /api/chat/config — 更新 Chat 配置
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## POST /api/chat/submit — 提交（任务场景）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

| 出参 | 处理结果 |
| 返回 | 200 |