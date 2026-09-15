# Issues 对外接口（/api/issues）

> 对外公开，认证：`get_current_user_or_api_key`（JWT 会话 / 会话 token（`tk_`）/ API Key 任一）。
> scope（x-token-scope）：全部 = **dual**（三通道全放行）。
> 以 **Issue 为中心**聚合：pipeline 阶段（`stage`）+ 任务中心状态（`task_status`）+ 事件流（`events`）+ 路线图（`pipeline`）。
> 设计：`docs/weekly-03/issues-api-design.md`（含 Issue / Event / Task 概念澄清）。

## 概念速览

| 概念 | 本质 | 判别关键 |
|------|------|----------|
| **Issue** | GitCode 上的需求/任务单元（`#N`），pipeline 主对象 | `stage`（vod/issue/coding/qa/sre/pending_review/done） |
| **Event** | 一次触发动作（webhook/A2A），pipeline 执行单元 | `seq` |
| **Task（任务卡片）** | 任务中心展示对象，每 Issue 一行（`issue` 表） | `status`（active/processing/awaiting_input/pending_review/completed/cancelled） |

> `stage` 由 pipeline 驱动（机器视角），`task_status` 由 `stage` 映射联动，但 `awaiting_input / completed / cancelled` 可由用户操作直接设置（更高优先级）。

---

## GET /api/issues/{owner}/{repo} — 仓库 Issue 列表（含阶段/状态聚合）

| 项 | 值 |
|----|-----|
| 认证 | 必填 |
| Path | `{owner}` / `{repo}` 仓库 |
| 出参 | `{issues: Issue[], total: int}` |
| 返回 | 200 成功；401 未认证 |

Issue 对象字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `number` | int | Issue 编号 |
| `title` | string | 标题（事件首条带 title 的事件；无则 `Issue #N`） |
| `stage` | string | pipeline 阶段（vod/issue/coding/qa/sre/pending_review/done） |
| `task_status` | string | 任务中心状态（active/processing/awaiting_input/pending_review/completed/cancelled） |
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

> 迁移自 `POST /api/tasks/create`（已废弃）。创建 GitCode Issue + 建档 + 触发 pipeline。

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
| 认证 | 必填 |
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
| 认证 | 必填 |
| Path | `{owner}` / `{repo}` / `{number}` |
| 出参 | `{events: Event[], total: int}` |
| 返回 | 200 成功（含空列表）；404 Issue 不存在；401 未认证 |

> 仅返回该 Issue 的事件，seq 升序；只读聚合，不影响 issue_state/events。

---

## GET /api/issues/{owner}/{repo}/{number}/pipeline — Issue pipeline 路线图

| 项 | 值 |
|----|-----|
| 认证 | 必填 |
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