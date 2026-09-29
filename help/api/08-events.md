# Events 事件接口（/api/events）

> 对外公开，认证：`get_current_user_or_api_key`。worker agent 结果获取核心。
> scope（x-token-scope）：`GET /api/events/detail/{seq}` = **dual**；操作类
> （terminate/redispatch/thread/chat/knowledge/stream） = **user_only**（`grape_` API Key / JWT；`tk_` → 403）。
> **CLI 说明**：grape CLI 命令与 API 的 **`operation_id` 精确关联**——`operation_id` 按
> `{根组}_{动词}` 拆分：`_` 前为 CLI 根命令组（`events`/`issues`/`tasks`/`scenes`...），`_` 后为组内子命令。
> 例：`events_list`→`grape events list`、`events_detail`→`grape events detail`、`events_chat_task_status`→`grape events chat task status`。
> 查命令：`grape <组> -h` / `grape <组> <动词> -h`；认证用 `GRAPE_API_TOKEN`/Web JWT（`tk_` 会话 → 403 的记 user_only）。
> ℹ️ **按 Issue 获取事件**（`GET /api/issues/{owner}/{repo}/{number}/events`，operation_id=`issues_events`）属
> Issues 接口，文档见 `11-issues.md`——CLI：`grape issues events --owner --repo --number`。

## GET /api/events/{owner}/{repo} — 仓库事件列表（operation_id=events_list）
> scope（x-token-scope）：**dual**（`grape_` API Key / JWT / `tk_` 会话 token 均可）；`tk_` 限定其绑定仓库（跨仓库 → 403）。

| Path | `{owner}` `{repo}` |
| Query | `page`、`limit`、`event_type`、`issue_id`、`status`（可选中） |
| 出参 | `{items: Event[], total}` |
| CLI | `grape events list --owner {owner} --repo {repo} [--page N --page_size N --status ...] --json-out` |

Event 字段：`seq / repo_key / issue_id / event_type / source / agent_type / title / status / created_at / started_at / completed_at / duration / summary / result / actor / runtime / skill`

---

## GET /api/events/detail/{seq} — 事件详情（operation_id=events_detail）
> scope（x-token-scope）：**dual**（`grape_` API Key / JWT / `tk_` 会话 token 均可）

| Path | `{seq}` int |
| 出参 | Event 完整对象（含 payload、agent_logs、result、archive） |
| archive | 结构化产出段 `{seq, agent, event_type, status, runtime, skill, output, logs}`；优先 `events.structured_output`，无则回退 event.json（weekly-05 起） |
| 返回 | 200；404 |
| CLI | `grape events detail --seq {seq} --json-out` |

---

## GET /api/events/{seq}/thread（operation_id=events_thread） — 事件对话线程
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{seq}` int |
| 出参 | `[{role, content, created_at}]` |
| 返回 | 200 |
| CLI | `grape events thread --seq {seq} --json-out` |

---

## DELETE /api/events/{seq}/knowledge/{scope}（operation_id=events_knowledge_delete） — 删除 Issue 知识

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）。

| Path | `{seq}` / `{scope}` |
| 出参 | `{"ok": true}` |
| 返回 | 200；403 非 bot；400 |
| CLI | `grape events knowledge delete --seq {seq} --scope {scope} --json-out` |

---

## POST /api/events/{seq}/flow/reset（内部端点，无 operation_id 契约） — 流程重置

> scope：worker/内部端点（不走 /api/issues 契约标注）。请求头：`Content-Type: application/json`

请求体（FlowResetBody）：`{"to_stage": "vod"}`

| 出参 | `{"ok": true, "to_stage": "vod"}` |
| 返回 | 200；400 非关联 Issue 事件 |
| CLI | 无（内部/worker 端点，未在 CLI 暴露） |

---

## POST /api/events/{seq}/terminate（operation_id=events_terminate） — 终止事件（重派）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{seq}` int |
| 出参 | `{"ok": true}` |
| 返回 | 200 |
| CLI | `grape events terminate --seq {seq} --json-out` |

---

## POST /api/events/{seq}/redispatch（operation_id=events_redispatch） — 重派事件
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{seq}` int |
| 出参 | `{"status": "redispatched", "event_seq": n, ...}` |
| 返回 | 200；409 重派失败 |
| CLI | `grape events redispatch --seq {seq} [--local_only 0/1] --json-out` |

---

## POST /api/events/{seq}/chat（operation_id=events_chat_send） — 事件对话发送
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（ConversationMessage）：`{"message": "..."}`

| 出参 | agent 回复 |
| 返回 | 200 |
| CLI | `grape events chat send --seq {seq} --json '{"message":"..."}' --json-out` |

---

## POST /api/events/{seq}/chat/stream（operation_id=events_chat_stream） — 事件对话 SSE
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| 出参 | SSE 流 |
| 返回 | 200 |
| CLI | `grape events chat stream --seq {seq} --json '{"message":"..."}'`（SSE，配合 --json-out 消费帧） |

---

## DELETE /api/events/{seq}/chat（operation_id=events_chat_delete） — 删除事件对话（清理线程）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

> Issue #100：清除该事件关联的对话消息（event_thread 记录删空）。

| Path | `{seq}` int |
| 出参 | `{"ok": true, "message": "对话已清理"}` |
| 返回 | 200 |
| CLI | `grape events chat delete --seq {seq} --json-out` |

---

## POST /api/events/{seq}/chat/submit（operation_id=events_chat_submit） — 事件对话异步提交
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（ConversationMessage）：`{"message": "..."}`

> 提交对话查询到后台异步队列，返回 task_id；随后轮询 `GET /api/events/chat/task/{task_id}/status`。

| 出参 | `{"task_id", "seq", "status": "running"}` |
| 返回 | 200 |
| CLI | `grape events chat submit --seq {seq} --json '{"message":"..."}' --json-out` |

---

## GET /api/events/chat/task/{task_id}/status（operation_id=events_chat_task_status） — 事件对话异步状态
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{task_id}` |
| 出参 | `{"task_id", "status": "processing/completed/failed/not_found", "result": "..."}` |
| 返回 | 200 |
| CLI | `grape events chat task status --task_id {task_id} --json-out` |

## GET /api/events/chat/task/{task_id}/stream（operation_id=events_chat_task_stream） — 事件对话异步流
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{task_id}` |
| 出参 | SSE 流（回放历史 + 实时追帧） |
| 返回 | 200 |
| CLI | `grape events chat task stream --task_id {task_id}`（SSE） |