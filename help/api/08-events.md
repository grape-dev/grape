# Events 事件接口（/api/events）

> 对外公开，认证：`get_current_user_or_api_key`。worker agent 结果获取核心。
> scope（x-token-scope）：`GET /api/events/detail/{seq}` = **dual**；操作类
> （terminate/redispatch/thread/chat/knowledge/stream） = **user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## GET /api/events/{owner}/{repo} — 仓库事件列表
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{owner}` `{repo}` |
| Query | `page`、`limit`、`event_type`、`issue_id`、`status`（可选中） |
| 出参 | `{items: Event[], total}` |

Event 字段：`seq / repo_key / issue_id / event_type / source / agent_type / title / status / created_at / started_at / completed_at / duration / summary / result / actor / runtime / skill`

---

## GET /api/events/detail/{seq} — 事件详情
> scope（x-token-scope）：**dual**（`grape_` API Key / JWT / `tk_` 会话 token 均可）

| Path | `{seq}` int |
| 出参 | Event 完整对象（含 payload、agent_logs、result、archive） |
| archive | 结构化产出段 `{seq, agent, event_type, status, runtime, skill, output, logs}`；优先 `events.structured_output`，无则回退 event.json（weekly-05 起） |
| 返回 | 200；404 |

---

## GET /api/issues/{owner}/{repo}/{number}/events — Issue 事件摘要（平替 event.json）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）。
> 作用：按 Issue 获取该 issue 下全部事件 + 每个事件的结构化产出/执行日志（Agent 与前端平替 event.json 的读取入口）。

| Path | `{owner}` `{repo}` `{number}`（`5` 或 `#5` 均可，`#` 需 URL 编码 `%23`） |
| 出参 | `{events: [{seq, event_type, agent_type, title, actor, summary, status, created_at, runtime, skill, output?, logs?}], segments: 同 events, total}` |
| `output` | `{actions, conclusion, artifacts, next_step, issue_summary}`（来自 events.structured_output，无则省略） |
| `logs` | 执行消息列表（来自 events.agent_logs） |
| 返回 | 200（无事件 → segments=[]）；404（issue/仓库不存在） |

---

## GET /api/events/{seq}/thread — 事件对话线程
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{seq}` int |
| 出参 | `[{role, content, created_at}]` |
| 返回 | 200 |

---

## DELETE /api/events/{seq}/knowledge/{scope} — 删除 Issue 知识

> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）。

| Path | `{seq}` / `{scope}` |
| 出参 | `{"ok": true}` |
| 返回 | 200；403 非 bot；400 |

---

## POST /api/events/{seq}/flow/reset — 流程重置

> scope：worker/内部端点（不走 /api/issues 契约标注）。请求头：`Content-Type: application/json`

请求体（FlowResetBody）：`{"to_stage": "vod"}`

| 出参 | `{"ok": true, "to_stage": "vod"}` |
| 返回 | 200；400 非关联 Issue 事件 |

---

## POST /api/events/{seq}/terminate — 终止事件（重派）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{seq}` int |
| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## POST /api/events/{seq}/redispatch — 重派事件
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{seq}` int |
| 出参 | `{"status": "redispatched", "event_seq": n, ...}` |
| 返回 | 200；409 重派失败 |

---

## POST /api/events/{seq}/chat — 事件对话发送
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（ConversationMessage）：`{"message": "..."}`

| 出参 | agent 回复 |
| 返回 | 200 |

---

## POST /api/events/{seq}/chat/stream — 事件对话 SSE
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| 出参 | SSE 流 |
| 返回 | 200 |

---

## DELETE /api/events/{seq}/chat — 删除事件对话（清理线程）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

> Issue #100：清除该事件关联的对话消息（event_thread 记录删空）。

| Path | `{seq}` int |
| 出参 | `{"ok": true, "message": "对话已清理"}` |
| 返回 | 200 |

---

## POST /api/events/{seq}/chat/submit — 事件对话异步提交
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

请求头：`Content-Type: application/json`

请求体（ConversationMessage）：`{"message": "..."}`

> 提交对话查询到后台异步队列，返回 task_id；随后轮询 `GET /api/events/chat/task/{task_id}/status`。

| 出参 | `{"task_id", "seq", "status": "running"}` |
| 返回 | 200 |

---

## GET /api/events/chat/task/{task_id}/status — 事件对话异步状态
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{task_id}` |
| 出参 | `{"task_id", "status": "processing/completed/failed/not_found", "result": "..."}` |
| 返回 | 200 |

## GET /api/events/chat/task/{task_id}/stream — 事件对话异步流
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{task_id}` |
| 出参 | SSE 流（回放历史 + 实时追帧） |
| 返回 | 200 |