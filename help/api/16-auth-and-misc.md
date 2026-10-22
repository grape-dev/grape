# Auth / Dashboard / Pipeline / Search / 模板 / 飞书 Onboard 接口

> 对外公开（openapi-full-coverage-design.md A/D 批收录），认证：`require_no_session_token`。
> scope（x-token-scope）：**全部 = user_only**（`grape_` API Key / JWT；`tk_` → 403）。

## 认证

### GET /api/auth/profile — 用户信息

> scope：user_only。支持 JWT 会话 / `grape_` API Key / `tk_` 会话 token（Issue #69）。

| 出参 | `{username, nickname, status: "online", managed_repos: [{key, full_name, is_admin}], gitcode_bound, github_bound}` |
| 返回 | 200；401 未认证 |

### POST /api/auth/logout — 登出

> scope：user_only。`token_version+1` 撤销所有 token + 清 cookie。

| 出参 | `{"message": "..."}` |
| 返回 | 200 |

> 注：`/api/auth/login`、`/api/auth/refresh`、OAuth 登录/回调为**无 Token 公共入口**，不收录契约（`help/api/openapi.json` 之外）。

---

## Dashboard 运维看板

### GET /api/dashboard/ops/overview — 运维概览

> scope：user_only。

| Query | `repo`（可选） |
| 出参 | 实时运维指标（任务总量/成功/失败、引擎运行状态等聚合） |
| 返回 | 200 |

### GET /api/dashboard/ops/probe-now — 手动触发引擎探测

> scope：dual。需登录：组织成员可见。

| 出参 | `throttled`（防抖窗口内返回上次结果）/ `accepted`（后台线程异步探测） |
| 返回 | 200；401 未登录 |

### GET /api/dashboard/ops/error-logs/sources — 日志来源列表

> scope：dual。需登录：组织成员可见。

| 出参 | 主控 + 各 worker 日志来源列表 |
| 返回 | 200；401 未登录 |

### GET /api/dashboard/ops/error-logs — 错误/警告日志

> scope：dual。需登录：组织成员可见。

| Query | `limit` `level` `repo` `user_id` `source`（可选） |
| 出参 | 最近错误/警告日志条目 |
| 返回 | 200；401 未登录 |

### GET /api/dashboard/ops/timeline — 任务时序图

> scope：dual。需登录：组织成员可见。

| Query | `repo`（可选） |
| 出参 | 按 issue 分组的事件链 |
| 返回 | 200；401 未登录 |

### GET /api/dashboard/ops/a2a-health — A2A 健康检查

> scope：dual。需登录：组织成员可见。

| 出参 | server_base_url 可达性 + 最近 A2A 调用记录 |
| 返回 | 200；401 未登录 |

### GET /api/dashboard/{owner}/{repo} — 仓库看板

> scope：user_only。权限：`check_repo_access` 当前用户可访问该仓库。

| Path | `{owner}` `{repo}` |
| 出参 | 仓库维度看板数据 |
| 返回 | 200；403 无仓库权限 |

---

## Pipeline 流水线

### GET /api/pipeline/{owner}/{repo} — 仓库流水线路线图

> scope：user_only。权限：`check_repo_access`。

| Path | `{owner}` `{repo}` |
| 出参 | 该仓库 pipeline 路线图（阶段/节点/事件） |
| 返回 | 200；403 无仓库权限 |

> 与 `GET /api/issues/{owner}/{repo}/{number}/pipeline`（Issue 维度，dual）的差异：本接口为**仓库全局**流水线视图。

---

## Search 全局搜索

### GET /api/search — 全局搜索

> scope：user_only。

| Query | 参数 | 类型 | 必填 | 说明 |
|------|------|------|:---:|------|
| `q` | string | ✅ | 搜索关键字（≥1 字符） |
| `type` | string | 否 | `all`（默认）/ `event` / `repo` / `agent` |

| 出参 | `{events: [], repos: [], agents: []}`（仅当前用户有权限访问的仓库事件） |
| 返回 | 200 |

---

## Project Templates 项目模板

### GET /api/project-templates — 模板分类列表

> scope：user_only。

| 出参 | `{categories: [...]}`（模板分类） |
| 返回 | 200 |

### GET /api/project-templates/{template_id}/readme — 模板 README

> scope：user_only。

| Path | `{template_id}` string |
| 出参 | `{content, source: local|git|gitcode|github}`（git 源按模板配置从 GitCode/GitHub API 拉取） |
| 返回 | 200；404 模板不存在 |

### GET /api/project-templates/{template_id}/preview — 模板预览 HTML

> scope：user_only。

| Path | `{template_id}` string |
| 出参 | `{content, source}`（local 模板读 `examples/{id}/preview.html`） |
| 返回 | 200；404 模板不存在 |

### POST /api/project-templates/create — 一键创建项目

> scope：user_only。

请求头：`Content-Type: application/json`

请求体（CreateTemplateRequest）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `template_id` | string | ✅ | 模板 id |
| `repo_name` | string | 否 | 目标仓库名 |

> 流程：建仓库 → 写模板文件 → 自动托管 → 应用开发场景。

| 出参 | 创建结果（仓库 full_name / scene 应用结果） |
| 返回 | 200/201；400 未配平台 token；404 模板不存在 |

---

## Feishu Onboard 飞书扫码建应用（RFC 8628）

> 流程：`start` → 前端轮询 `status`（拿 `qr_url` 展示二维码）→ 用户扫码确认 → `connected`。

### POST /api/feishu/onboard/start — 创建扫码任务

> scope：user_only。

请求头：`Content-Type: application/json`

请求体（StartBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `bot_type` | string | ✅ | `chat_bot`（默认）/ `vod_bot` |
| `repo_key` | string | vod_bot 时必填 | 绑定仓库（`owner/repo`） |

| 出参 | `{"task_id", "status": "pending"}` |
| 返回 | 200；400 bot_type 非法 / vod_bot 缺 repo_key；409 已有进行中任务 |

### GET /api/feishu/onboard/status — 查询扫码任务状态

> scope：user_only。

| Query | `task_id`（必填） |
| 出参 | `{task_id, status (pending|waiting_confirm|connected|failed), qr_url, expire_in, app_id, app_secret_exists, error}` |
| 返回 | 200；403 非本人任务；404 任务不存在 |

### POST /api/feishu/onboard/cancel — 取消扫码任务

> scope：user_only。

请求头：`Content-Type: application/json`

请求体（CancelBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `task_id` | string | ✅ | 待取消任务 |

| 出参 | `{"message", "status"}`（connected/failed 任务幂等返回「无需取消」） |
| 返回 | 200；404 任务不存在 |

---

## 错误返回（全端点通用）

| 码 | 场景 |
|----|------|
| 400 | 参数缺失/校验失败 |
| 401 | 未认证 / token 无效 |
| 403 | `tk_` 不适用（user_only）+ 仓库无权限 |
| 404 | 资源不存在 |
| 409 | 进行中任务冲突 |

```json
{"detail": {"code": 404, "message": "Not Found"}}
```