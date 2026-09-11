# Tasks 任务接口（/api/tasks）

> 对外公开，认证：`get_current_user_or_api_key`（JWT 会话 / 会话 token（`tk_`）/ API Key 任一）。

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

## POST /api/tasks/complete — 任务自报完成（grape tasks complete）

请求头：`Content-Type: application/json`

请求体（TaskSelfCompleteBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `task_id` | string | 会话 token 时可省 | 会话 token（`tk_`）下由 token 自动定位（`session_key`）；JWT/API Key 必填 |
| `result_text` | string | 非结构化时为 ✅ | 结论/动作/产出摘要（写入事件 result），未结构化时兜底为「任务自报完成…」 |
| `next_step` | string | 否 | `a2a_issue` / `reply_comment` / `a2a_need_clarify` / `done` |
| `actions` | string[] | 否 | 动作清单（如 `["修改代码修复审查意见", "grape git push 分支", "在 PR #N 评论告知重新审查"]） |
| `conclusion` | string | 否 | 结论（如「已按审查意见完成哪些修复，测试结果，待 QA 重新审查」） |
| `artifacts` | string[] | 否 | 产出/标识（如 `["PR #N 评论", "commit xxxxxx"]`） |
| `issue_summary` | string | 否 | 一句话摘要（如「已按审查意见修复 \| 修改摘要 \| 待重新审查」） |

> 结构化示例（agent 收尾 `grape tasks complete --json`）：
> ```json
> {
>   "actions": ["修改代码修复审查意见", "grape git push 分支", "在 PR #N 评论告知重新审查"],
>   "conclusion": "已按审查意见完成哪些修复，测试结果，待 QA 重新审查",
>   "artifacts": ["PR #N 评论", "commit xxxxxx"],
>   "next_step": "QA Agent 重新审查 PR #N",
>   "issue_summary": "已按审查意见修复 | 修改摘要 | 待重新审查"
> }
> ```
> 传入结构化字段后，服务端组装为结构化 `result_text`（含 `actions`/`conclusion`…），供事件对话与结构化解析识别；不传则用 `result_text` 原文。

| 项 | 值 |
|----|-----|
| 出参 | `{"ok": true, "task_id": "t_xxx", "status": "completed"}`；终态幂等返回 `already: true` |
| 返回 | 200；401 未认证；400 缺 task_id；403 作用域不匹配/非本人托管仓库；404 任务不存在 |

> 会话 token：任务 id 由 `session_key` 自动定位（M2 以 task_id 签发），agent 收尾直接
> `grape tasks complete --json '{"result_text":"...","next_step":"a2a_issue"}'`，无需 `--task-id`。

---

## GET /api/tasks/{task_id}/status — 查询任务状态（轮询 wait 用）

| 项 | 值 |
|----|-----|
| Path | `{task_id}` int |
| 出参 | `{"id": ..., "status": "running", "repo_key": ..., "issue_number": ..., "title": ..., "completed_reason": ..., "completed_at": ...}` |
| 返回 | 200；401；404 |

> `grape tasks wait --json-out` 即轮询本端点直至终态（completed/failed/cancelled）。

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