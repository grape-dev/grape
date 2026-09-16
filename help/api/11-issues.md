# Issues 对外接口（/api/issues）

> 对外公开，认证：`get_current_user_or_api_key`（JWT 会话 / 会话 token（`tk_`）/ API Key 任一）。
> scope（x-token-scope）：读列表 = **dual**；操作类（confirm-done/cancel/resume/pipeline/clarify/attachments 写）= **user_only**。
> 以 **Issue 为中心**聚合：pipeline 阶段（`stage`）+ 需求中心状态（`status`）+ 事件流（`events`）+ 路线图（`pipeline`）。
> 设计：`docs/weekly-03/issues-api-design.md`。

## 概念速览

> 参考：`docs/weekly-03/issues-api-design.md` §一（含两个同名 Task 的澄清）。

| 概念 | 本质 | 载体 | 判别关键 |
|------|------|------|----------|
| **Issue（需求）** | **grape 的需求单元（`#N`）**，pipeline 的**主对象**；需求中心每条记录即一个 Issue，映射到 GitCode/GitHub 等平台的 issue | `issue` 表（`issue_number` 关联平台） | **`stage`**（pipeline 视角）+ **`status`**（需求中心视角） |
| **Event** | 一次触发动作（webhook/A2A），pipeline 的**执行单元** | `events` 表（`seq` 唯一） | **`seq`** |
| **Task（执行实例）** | 一次 **Agent 引擎执行**，Event 的具体落地 | `agent_tasks` 表（`event_seq` 关联） | **`task_id`**（`t_` 前缀） |

> - **Issue 双状态**：`stage`（机器驱动，vod/issue/coding/qa/sre/pending_review/done）由 pipeline 推进；
>   `status`（用户可读，active/processing/awaiting_input/pending_review/completed/cancelled）由 `stage` 映射同步，
>   但 `awaiting_input / completed / cancelled` 可由用户操作直接设置（更高优先级）。两者同存 `issue` 表。
> - **task complete ≠ Issue 完结**：`POST /api/tasks/complete` 完成的是某次引擎执行（对应 Event 的 `event_seq`），
>   不代表需求完结；需求完结以「最后节点 complete → pending_review → 审核 → done」判断（`confirm-done` / `status` 操作）。
> - 接口中的 `task_status` 字段即 `issue.status`（需求中心状态）。

---

## GET /api/issues/mine — 我的需求列表（需求中心·个人维度）

| 项 | 值 |
|----|-----|
| scope（x-token-scope） | **dual**（`grape_` API Key / JWT / `tk_` 会话 token 均可） |
| Query | `status`（active/processing/awaiting_input/pending_review/completed）、`repo_key`（可选，按仓库过滤）、`sort_order`（可选，desc/asc）、`mode`（可选，default=状态优先/time=纯时间序）、`created_by_me`（可选）、`assignees`（可选，逗号分隔创建人） |
| 出参 | `{issues: Issue[], sort_order, mode}`（跨仓库，user_id=当前用户） |
| 返回 | 200；401 未认证 |

> 富化字段：`scene_id/scene_name/stage/is_running/agent_statuses`。

---

## GET /api/issues/repo/{owner}/{repo} — 仓库下全部需求列表（需求中心·仓库全部视图）

| 项 | 值 |
|----|-----|
| scope（x-token-scope） | **dual**（三通道） |
| Path | `{owner}` / `{repo}` 仓库 |
| Query | `status`（.../cancelled）、`sort_order`、`mode`、`assignees`（同上） |
| 出参 | `{issues: Issue[], sort_order, mode, repo_key}`（不限 user_id） |
| 返回 | 200；403 无权访问该仓库；401 未认证 |

> 防 IDOR：校验调用方可访问该仓库，无权返回 403。

---

## GET /api/issues/{owner}/{repo} — 仓库 Issue 列表（含阶段/状态聚合）

| 项 | 值 |
|----|-----|
| scope（x-token-scope） | **dual**（三通道） |
| Path | `{owner}` / `{repo}` 仓库 |
| 出参 | `{issues: Issue[], total: int}` |
| 返回 | 200 成功；401 未认证 |

Issue 对象字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `number` | int | Issue 编号 |
| `title` | string | 标题（事件首条带 title 的事件；无则 `Issue #N`） |
| `stage` | string | pipeline 阶段（vod/issue/coding/qa/sre/pending_review/done） |
| `task_status` | string | 需求中心状态（active/processing/awaiting_input/pending_review/completed/cancelled） |
| `owner_agent` | string | 当前归属 agent（默认 vod） |
| `scene_id` | string | Issue 级开发场景（空=回退仓库默认） |
| `assignee` | string | Issue 责任人（GitCode 用户名） |
| `is_subtask_mode` | int | 0/1 多任务模式 |
| `parent_issue_id` | string | 父任务 Issue ID（如 `#20`，空=普通任务） |
| `events_count` | int | 该 Issue 事件数 |
| `latest_event` | object? | 最新事件（seq 升序末条） |

> 空仓库返回 `{"issues": [], "total": 0}`。

---

## POST /api/issues/{owner}/{repo} — 创建 Issue（对外统一创建入口）

> scope（x-token-scope）：**dual**（三通道）。
> 创建 GitCode Issue + 建档 + 触发 pipeline。

请求头：`Content-Type: application/json`

请求体：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `title` | string | ✅ | 任务/Issue 标题 |
| `description` | string | 否 | 任务描述（将成为 Issue body） |
| `repo_key` | string | 否 | 仓库（缺省按 `{owner}/{repo}` 拼） |
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
  "message": "任务已创建（Issue 已同步）"
}
```

返回：200 成功；400 仓库/标题/描述缺失；409 同标题 Issue 已存在；502 建 GitCode Issue 失败。

---

## GET /api/issues/{owner}/{repo}/{number} — 单个 Issue 详情

| 项 | 值 |
|----|-----|
| scope（x-token-scope） | **dual**（三通道） |
| Path | `{owner}` / `{repo}` / `{number}` 仓库 + Issue 编号 |
| 出参 | `{...Issue 字段, events: Event[], pipeline: Pipeline}` |
| 返回 | 200 成功；404 Issue 不存在；401 未认证 |

`events` 结构（seq 升序）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `seq` | int | 事件唯一序号 |
| `event_type` | string | issue_opened / issue_comment / pull_request_updated / ... |
| `agent_type` | string | vod / issue / coding / qa / sre |
| `title` | string | 事件标题（缺省取 event_type） |
| `actor` | string | 触发人 |
| `summary` | string | 摘要 |
| `status` | string | completed / running / failed / skipped |
| `created_at` | string | 事件时间（已格式化） |

`pipeline` 结构：

| 字段 | 类型 | 说明 |
|------|------|------|
| `issue` | object | 关联 Issue（number） |
| `summary` | string | pipeline 汇总 |
| `status` | string | 终态 |
| `segments` | Segment[] | 分段链（seq/agent/event_type/status/runtime/skill/output） |

> 无 event.json 档案时降级由 `events` 表重构（summary/status 降级）。

---

## GET /api/issues/{owner}/{repo}/{number}/events — 该 Issue 的事件列表

| 项 | 值 |
|----|-----|
| scope（x-token-scope） | **dual**（三通道） |
| Path | `{owner}` / `{repo}` / `{number}` |
| 出参 | `{events: Event[], total: int}` |
| 返回 | 200 成功（含空列表）；404 Issue 不存在；401 未认证 |

> 仅返回该 Issue 的事件，seq 升序；只读聚合，不影响 issue_state/events。

---

## GET /api/issues/{owner}/{repo}/{number}/pipeline — Issue pipeline 路线图

| 项 | 值 |
|----|-----|
| scope（x-token-scope） | **dual**（三通道） |
| Path | `{owner}` / `{repo}` / `{number}` |
| 出参 | `{issue: object, summary: string, status: string, segments: Segment[]}` |
| 返回 | 200 成功；404 Issue 不存在；401 未认证 |

`segments[].output`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `conclusion` | string | 结论 |
| `next_step` | string | 下一步 |
| `actions` | string[] | 动作清单 |
| `artifacts` | string[] | 产出/标识 |
| `issue_summary` | string | 一句话摘要 |

> 优先读 event.json 档案；无档案时降级由 `events` 重构。

---

## Issue 操作（按 `{issue_id}` 定位）

> `{issue_id}` = `issue.id`（需求中心列表返回的 `id` 字段）。scope：`start/status/attachments 读` = **dual**；
> `confirm-done/cancel/resume/pipeline/clarifications/clarify/attachments 写` = **user_only**（`tk_` → 403）。

| 方法 | 路径 | 说明 | 返回 |
|:---:|------|------|------|
| POST | `/api/issues/{issue_id}/start` | 打开 Issue 会话 | `{status}` |
| GET | `/api/issues/{issue_id}/status` | 查询状态（轮询 wait） | `{id,status,repo_key,issue_number,title,...}` |
| PUT | `/api/issues/{issue_id}/status` | 状态迁移（active/processing/awaiting_input/pending_review/completed/cancelled） | `{ok,status}` |
| POST | `/api/issues/{issue_id}/confirm-done` | 开发者确认完成（stage→done） | `{ok,status:completed,stage:done}` |
| POST | `/api/issues/{issue_id}/cancel` | 取消（停流水线 + 终止 agent） | `{ok,status:cancelled}` |
| POST | `/api/issues/{issue_id}/resume` | 恢复已取消 | `{ok,status:processing}` |
| GET | `/api/issues/{issue_id}/pipeline` | 流水线路线图（scene/stage/nodes/events） | `{scene_id,current_stage,nodes,events}` |
| GET | `/api/issues/{issue_id}/clarifications` | agent 澄清消息 | `{messages,conversation_id,issue_url}` |
| POST | `/api/issues/{issue_id}/clarify` | 回复澄清（发 GitCode 评论 + 写会话） | `{ok,message}` |
| POST | `/api/issues/{issue_id}/attachments` | 上传附件（multipart `file`） | `{url,filename,size}` |
| GET | `/api/issues/{issue_id}/attachments` | 列出附件 | `{attachments:[...]}` |
| DELETE | `/api/issues/{issue_id}/attachments/{filename}` | 删除附件（filename 为 UUID 名） | `{ok,message}` |

> 通用错误码见下节（404 需求不存在 / 403 无权 / 401 未认证）。

---

## 错误返回（全端点通用）

> 格式与全量状态码契约见 `README.md`「错误响应统一格式」；`detail.message` 英文。

| 码 | message（英文） | 场景 |
|----|--------------|------|
| 400 | Bad Request / Invalid parameter | 创建时仓库/标题/描述缺失等参数错误 |
| 401 | Unauthorized | 未认证 / token 失效 |
| 403 | Forbidden | 无权限（含 session token 不适用于账户类端点） |
| 404 | Not Found | Issue #N 不存在 |
| 409 | Conflict | 创建时同标题 Issue 已存在 |
| 502 | Bad Gateway | GitCode 创建 Issue 失败 |

```json
{"detail": {"code": 404, "message": "Not Found"}}
```