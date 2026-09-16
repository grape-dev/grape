# TaskLogs 任务日志接口（/api/task-logs）

> 对外公开，认证：`get_current_user_or_api_key`。worker agent 结果的实时日志/消息来源。
> scope（x-token-scope）：全部 = **user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## GET /api/task-logs/{task_id}/info — 任务运行信息
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{task_id}` |
| 出参 | `{id, status, exit_code, duration, started_at, completed_at, engine, ...}` |
| 返回 | 200；404 |

---

## GET /api/task-logs/{task_id}/messages — 任务消息列表
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{task_id}` |
| Query | `limit`、`since`、`ts`（可选） |
| 出参 | `[{time, type(step_start/step_finish/tool_use/text/reasoning/done), data}]` |
| 返回 | 200 |

---

## GET /api/task-logs/{task_id}/stream — 任务消息流（SSE）
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）

| Path | `{task_id}` |
| Query | `since`（可选，增量游标） |
| 出参 | SSE 流 `data: {time, type, data}`，新消息实时推送 |
| 返回 | 200（长连接） |