# Tasks 任务接口（/api/tasks）

> 对外公开，认证：`get_current_user_or_api_key`（JWT 会话或 API Key 任一）。

## GET /api/tasks/mine — 当前用户任务列表

| 项 | 值 |
|----|-----|
| 认证 | 必填 |
| Query | `status`（可选，active/processing/completed/cancelled）、`repo_key`（可选）、`sort_order`（可选，asc/desc）、`mode`（可选，default 等） |
| 出参 | `{tasks: Task[], total: int}` 或直接任务数组（取决于实现） |
| 返回 | 200 成功；401 未认证 |

Task 对象字段：`id / user_id / assignee / repo_key / issue_number / title / task_description / conversation_id / status / completed_reason / created_at / completed_at / is_subtask_mode / parent_issue_number`

---

## POST /api/tasks/create — 创建任务（同时建 GitCode Issue）

请求头：`Content-Type: application/json`

请求体（TaskCreateBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `repo_key` | string | ✅ | 仓库（如 `owner/repo` 或带前缀） |
| `title` | string | ✅ | 任务/Issue 标题 |
| `description` | string | ✅ | 任务描述（将成为 Issue body） |
| `assignee` | string | 否 | 责任人 GitCode 用户名，默认当前用户 |
| `scene_id` | string | 否 | 关联场景 ID |
| `is_subtask_mode` | int | 否 | 0/1 多任务模式 |

出参：

```json
{
  "ok": true,
  "task_id": 101,
  "issue_number": 5,
  "created": true,
  "assignee": "Jason7602",
  "is_subtask_mode": 0,
  "message": "任务已创建，Issue 已同步"
}
```

返回：200 成功；400 参数缺失（title/description/repo_key 缺）；409 同标题 Issue 已存在；502 建 GitCode Issue 失败。

---

## POST /api/tasks/notify — 任务分配/通知

请求头：`Content-Type: application/json`

请求体（TaskNotifyBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `assignee` | string | ✅ | 责任人（人或葡萄机器人） |
| `repo_key` | string | ✅ | 仓库 |
| `issue_number` | int | ✅ | Issue 编号 |
| `task_description` | string | 否 | 任务描述 |

出参：

```json
{"ok": true, "task_id": 5, "conversation_id": "uuid", "message": "任务已创建并通知责任人"}
```

返回：200；400 责任人未注册平台；403 token 无效；bot_takeover 场景返回 `{"ok":true,"task_id":0,"message":"任务已由 ... 接管"}`。

---

## POST /api/tasks/close — 关闭任务

请求头：`Content-Type: application/json`

请求体（TaskCloseBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `repo_key` | string | ✅ | 仓库 |
| `issue_number` | int | ✅ | Issue 编号 |

出参：`{"ok": true}`

返回：200。

---

## POST /api/tasks/{task_id}/start — 打开任务会话（不翻转状态）

| 项 | 值 |
|----|-----|
| Path | `{task_id}` int |
| 出参 | `{"id": ..., "status": "active"}` |
| 返回 | 200；403 无权；404 任务不存在 |

---

## PUT /api/tasks/{task_id}/status — 状态迁移

请求头：`Content-Type: application/json`

请求体（TaskStatusBody）：`{"status": "processing"}`（active/processing/completed/cancelled 按流转规则）

| 项 | 值 |
|----|-----|
| Path | `{task_id}` int |
| 出参 | `{"ok": true, "task_id": ..., "status": "processing"}` |
| 返回 | 200；400 非法迁移；403 无权；404 不存在 |

---

## GET /api/tasks/{task_id}/clarifications — 澄清历史

| Path | `{task_id}` int |
| 出参 | 澄清消息数组 |
| 返回 | 200 |

---

## POST /api/tasks/{task_id}/clarify — 回复澄清

请求头：`Content-Type: application/json`

请求体（TaskClarifyBody）：`{"message": "..."}`

| 项 | 值 |
|----|-----|
| Path | `{task_id}` int |
| 出参 | `{"ok": true, ...}` |
| 返回 | 200；400 message 缺 |