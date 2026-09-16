# Tasks 接口（/api/tasks）— Agent 执行实例动作

> `/api/tasks` 现仅承载「Agent 执行期动作」类接口；用户侧的**需求列表与操作接口都在 `/api/issues`**
> （见 [11-issues.md](11-issues.md)）。
>
> | 端点 | 语义 | 认证 |
> |------|------|------|
> | `POST /api/tasks/notify` | Agent 分配/换人/接管通知 | `tk_` 会话 token（仅 `GRAPE_SESSION_TOKEN`） |
> | `POST /api/tasks/complete` | Agent 完成一次执行实例 | dual（三通道） |

---

## POST /api/tasks/notify — 任务分配/通知

> 调用方：issue 系列技能（issue/issue、vod/repo-onboarding 等）在分配/换人/接管时调用。
> 认证：`Authorization: Bearer <tk_ 会话 token>`——**仅支持 `GRAPE_SESSION_TOKEN`**，仓库作用域必须与
> `repo_key` 匹配（Agent 执行期由系统注入 `$GRAPE_SESSION_TOKEN`，见 event_pipeline 签发）。
> ⚠️ **不再支持 `BOT_GITCODE_TOKEN`**：若使用 `BOT_GITCODE_TOKEN` 调用，返回 **403 token 无效**。
> 也**不接受** `grape_` API Key / WebUI JWT。

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

返回：200；400 责任人未注册平台；**403 非 `GRAPE_SESSION_TOKEN` 或仓库作用域不匹配（含 `BOT_GITCODE_TOKEN`）**；bot_takeover 场景返回 `{"ok":true,"task_id":0,"message":"任务已由 ... 接管"}`。

---

## POST /api/tasks/complete — 任务自报完成（grape CLI tasks complete，Event 执行实例）

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

> 迁移索引：原 `/api/tasks/{task_id}/*` 用户操作端点已迁至 `/api/issues/{issue_id}/...`（见 11-issues.md）；
> CLI 轮询状态改用 `GET /api/issues/{issue_id}/status`。
> `POST /api/tasks/close`、`POST/DELETE /api/tasks/attachments/pre-upload*` 已移除——
> 附件统一走 `/api/issues/{issue_id}/attachments`；Issue 关闭由平台 webhook 自动收尾。