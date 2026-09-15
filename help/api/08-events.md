# Events 事件接口（/api/events）

> 对外公开，认证：`get_current_user_or_api_key`。worker agent 结果获取核心。
> scope（x-token-scope）：`GET /api/events/detail/{seq}` = **dual**；操作类
> （terminate/redispatch/thread/chat/knowledge/stream） = **user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## GET /api/events/{owner}/{repo} — 仓库事件列表

| Path | `{owner}` `{repo}` |
| Query | `page`、`limit`、`event_type`、`issue_id`、`status`（可选中） |
| 出参 | `{items: Event[], total}` |

Event 字段：`seq / repo_key / issue_id / event_type / source / agent_type / title / status / created_at / started_at / completed_at / duration / summary / result / actor / runtime / skill`

---

## GET /api/events/detail/{seq} — 事件详情

| Path | `{seq}` int |
| 出参 | Event 完整对象（含 payload、agent_logs、result） |
| 返回 | 200；404 |

---

## GET /api/events/{seq}/thread — 事件对话线程

| Path | `{seq}` int |
| 出参 | `[{role, content, created_at}]` |
| 返回 | 200 |

---

## POST /api/events/{seq}/knowledge — 写入 Issue 知识

请求头：`Content-Type: application/json`

请求体（KnowledgeBody）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `scope` | string | project / issue / issue_agent |
| `content` | string | 知识正文 |
| `status` | string | accepted / rejected |

| 出参 | `{"ok": true}` |
| 返回 | 200；403 非 bot；400 |

---

## POST /api/events/{seq}/flow/reset — 流程重置

请求头：`Content-Type: application/json`

请求体（FlowResetBody）：`{"to_stage": "vod"}`

| 出参 | `{"ok": true, "to_stage": "vod"}` |
| 返回 | 200；400 非关联 Issue 事件 |

---

## POST /api/events/{seq}/terminate — 终止事件（重派）

| Path | `{seq}` int |
| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## POST /api/events/{seq}/redispatch — 重派事件

| Path | `{seq}` int |
| 出参 | `{"status": "redispatched", "event_seq": n, ...}` |
| 返回 | 200；409 重派失败 |

---

## POST /api/events/{seq}/chat — 事件对话发送

请求头：`Content-Type: application/json`

请求体（ConversationMessage）：`{"message": "..."}`

| 出参 | agent 回复 |
| 返回 | 200 |

---

## POST /api/events/{seq}/chat/stream — 事件对话 SSE

| 出参 | SSE 流 |
| 返回 | 200 |

---

## GET /api/events/chat/task/{task_id}/status — 事件对话异步状态

## GET /api/events/chat/task/{task_id}/stream — 事件对话异步流