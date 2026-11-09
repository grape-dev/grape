# Feedback 意见反馈接口（/api/feedback）

> 对外公开（feedback-openapi-expose-design.md weekly-08 收录）。
> 认证分级：
> - `POST /api/feedback` Submit：**dual**（`grape_` API Key / Web JWT / `tk_` 会话 token 均可）
> - `mine` / `context` / `delete` / `upload`：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> - `list` / `stats` / `status`：**admin-only**（JWT role=admin），**不进公开契约**。

> **CLI 命令**：由契约 `operation_id` 派生（`<tag> <verb>`，issue-45-cli-design.md §3.1）。

| operation_id | CLI 命令 |
|---|---|
| `feedback_submit` | `grape feedback submit` |
| `feedback_mine` | `grape feedback mine` |
| `feedback_context` | `grape feedback context <event_seq>` |
| `feedback_delete` | `grape feedback delete <fid>` |
| `feedback_upload` | `grape feedback upload` |

## POST /api/feedback — 提交反馈
> scope（x-token-scope）：**dual**（JWT / API Key / `tk_` Agent 会话均可用）
> CLI：`grape feedback submit --json '{...}'`

请求头：`Content-Type: application/json`

请求体（FeedbackBody）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `category` | string | 必填：bug / suggestion / question / other |
| `title` | string | 必填：标题 |
| `description` | string | 必填：描述 |
| `page_url` | string | 否：来源页面 URL |
| `repo_key` | string | 否：仓库（仅 JWT/API Key 且无 event_seq 时生效） |
| `attachment_url` / `attachment_name` | string | 否：附件（配合 `/upload`） |
| `event_seq` | int | 否：失败事件 seq（服务端现查任务上下文落库） |
| `feedback_kind` | string | 否：Agent 元类别（cli_ux / skill_quality / token_waste / other） |

Agent（`tk_`）提交说明：
- 服务端从 token **权威解析**仓库/任务/agent/技能，忽略 body 的 `repo_key`/`event_seq`；
- 落库 `source='agent'`；每任务（session_key）最多 2 条，超出返回 **429**；
- 反馈随后由后台 reconcile worker 按技能归属自动转 Issue（见「反馈 → Issue 闭环」）。

```bash
# 用户提交（API Key 三通道自动带 Authorization）
grape feedback submit --json '{"category":"bug","title":"x","description":"y"}'
# Agent 自动反馈（tk_ 经 GRAPE_SESSION_TOKEN 注入 → token 权威解析仓库/任务/技能）
grape feedback submit --json '{"category":"suggestion","title":"CLI 不友好","description":"...","feedback_kind":"cli_ux"}'
```

| 出参 | `{id, status: "open", message}` |
| 返回 | 200；401 未认证；429 超限 |

## GET /api/feedback/mine — 我的反馈列表
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape feedback mine [--page 0] [--size 10]`

| Query | `page`（默认 0）`size`（默认 10，≤100） |
| 出参 | `{items:[{id,category,title,description,attachment_url,attachment_name,status,priority,handler_name,handler_remark,created_at,updated_at,source,skill_name,issue_url,issue_number}], total, page, total_pages}` |
| 返回 | 200；401 未认证 |

## GET /api/feedback/context/{event_seq} — 反馈上下文
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape feedback context 12345`

| Path | `{event_seq}` |
| 出参 | `{event_seq, repo_key, issue_id, issue_number, agent_type, event_status, task, engine, skill, fail_summary}` |
| 返回 | 200；404 事件未找到或无权限 |

## DELETE /api/feedback/{fid} — 删除反馈
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape feedback delete <fid>`

| Path | `{fid}` |
| 出参 | `{success, message}` |
| 返回 | 200；403 非本人；404 不存在 |

## POST /api/feedback/upload — 上传附件
> scope（x-token-scope）：**user_only**（`grape_` API Key / JWT；`tk_` → 403）
> CLI：`grape feedback upload --file <path>`（multipart）

| Body | multipart `file`（jpg/png/webp/gif/svg/zip，≤5MB） |
| 出参 | `{url, file_key, is_zip, original_name}` |
| 返回 | 200；400 类型/大小不支持；401 未认证 |

## 管理端点（admin-only，不进公开契约）

| 端点 | 说明 |
|------|------|
| `GET /api/feedback/list?status=&category=&search=&source=user|agent` | 管理列表（含 source 筛选、skill/issue 列） |
| `PUT /api/feedback/{fid}/status` | 状态流转（handler_name 取 admin token username） |
| `GET /api/feedback/stats` | 统计（byStatus / byCategory / bySource） |

## 反馈 → Issue 闭环（Agent 自动反馈）

- `tk_` Agent 提交落 `source='agent'` 后，后台 reconcile worker 周期扫描未转换行；
- 路由：`skill.scope='official'` → 官方技能仓库（`GRAPE_SKILLS_REPO` 配置，全局 bot 身份）；
  `repo`/`user` → 反馈所属仓库（仓库 bot token 身份）；
- 幂等：同 `(task_id, skill_name, title)` 只建一个 Issue，组内全部回写；
- 成功回写 `issue_url`/`issue_number`/`converted_at`；失败/仓库不可达/配置缺失 → 下轮重试，admin 列表可见。