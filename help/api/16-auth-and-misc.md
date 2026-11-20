# Auth / Dashboard / Pipeline / Search / 模板 / 飞书 Onboard / 组织审计接口

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
| 出参 | 该仓库 pipeline 路线图（阶段/节点/事件）。条目含 `statuses` 数组——与 nodes 一一对应的**节点状态数值码**：0=pending 1=running 2=completed 3=failed 4=cancelled 5=skipped（由 `compute_node_statuses` 统一计算，与流水线菜单页同源） |
| 返回 | 200；403 无仓库权限 |

> 与 `GET /api/issues/{owner}/{repo}/{number}/pipeline`（Issue 维度，dual）的差异：本接口为**仓库全局**流水线视图；Issue 维度接口的 `node_status` 同一套数值码（见 help/api/11-issues.md「节点状态」说明）。

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

## 组织审计

### GET /api/orgs/{org_id}/audit — 查询组织审计日志（Issue #2260）

> scope：user_only。权限：仅组织 owner/admin；member 与非成员 403。

| Path | `{org_id}` 组织 ID |
| Query | `event_type`（事件名或前缀，如 `org_repo_bind` / `org_repo_` / `org_dispatch_`；空=全部组织相关事件）、`limit`（默认 50，最大 200，越界夹取）、`offset`（默认 0，负数夹取为 0）、`since`（`YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`，含边界） |
| 出参 | `{items: [{id, seq, event_type, user_id, operator_id, ip, detail, created_at}], total, limit, offset}`：`items` 按 `id` 倒序，`detail` 递归脱敏（敏感键 → `[REDACTED]`，正文键剔除，超出深度上限的子树整棵丢弃），`total` 为全量匹配数 |
| 返回 | 200；400 `INVALID_SINCE`（格式非法或含时区）；404 `ORG_NOT_FOUND`；403 `PERMISSION_DENIED`（含 `tk_` 会话凭据） |

> 事件白名单固定为组织相关事件（`org_repo_*` / `org_context_*` / `org_knowledge_*`）与跨仓下派事件（`dispatch_*` / `target_*`）；`org_dispatch_` 是下派事件的语义别名。`org_provision_` 当前无对应审计写入，返回空页。日志本身不可变（哈希链），接口只读。

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

---

## 组织管理（/api/orgs）

> 组织上下文、关联仓库、仓库地图、跨仓下派、组织知识与审计。
> 认证与 scope 以 OpenAPI 的 `security` / `x-token-scope` 为准；组织成员/管理员校验由服务端执行。

### GET /api/orgs — orgs_list

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs — orgs_create

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/repo-namespaces — orgs_repo_namespaces

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/search/users — orgs_search_users

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### DELETE /api/orgs/{org_id} — orgs_delete

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id} — orgs_detail

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### PUT /api/orgs/{org_id} — orgs_update

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/context — org_context_list

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/context — org_context_create

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### DELETE /api/orgs/{org_id}/context/{ctx_id} — org_context_delete

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/context/{ctx_id} — org_context_get

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### PUT /api/orgs/{org_id}/context/{ctx_id} — org_context_update

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/context/{ctx_id}/deprecate — org_context_deprecate

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/context/{ctx_id}/publish — org_context_publish

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/dispatches — org_dispatches_list

> scope：`dual`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/dispatches — org_dispatches_create

> scope：`dual`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/dispatches/{dispatch_id} — org_dispatches_detail

> scope：`dual`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/dispatches/{dispatch_id}/cancel — org_dispatches_cancel

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/dispatches/{dispatch_id}/context — org_dispatches_context

> scope：`dual`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/dispatches/{dispatch_id}/targets/{repo_key}/retry — org_dispatches_target_retry

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/knowledge — org_knowledge_list

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/knowledge — org_knowledge_create

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### DELETE /api/orgs/{org_id}/knowledge/{knowledge_id} — org_knowledge_delete

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/knowledge/{knowledge_id} — org_knowledge_get

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### PUT /api/orgs/{org_id}/knowledge/{knowledge_id} — org_knowledge_update

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/knowledge/{knowledge_id}/archive — org_knowledge_archive

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/knowledge/{knowledge_id}/deprecate — org_knowledge_deprecate

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/knowledge/{knowledge_id}/publish — org_knowledge_publish

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/leave — orgs_leave

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/members — orgs_members_list

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/members — orgs_members_add

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### DELETE /api/orgs/{org_id}/members/{user_id} — orgs_members_remove

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### PATCH /api/orgs/{org_id}/members/{user_id} — orgs_members_update

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/provision — orgs_provision_get

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### PUT /api/orgs/{org_id}/provision/repo — orgs_provision_manual_bind

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/provision/retry — orgs_provision_retry

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/repo-map — org_repo_map_list

> scope：`dual`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/repo-map/analyze — org_repo_map_analyze_all

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/repo-map/recall — org_repo_map_recall

> scope：`dual`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/repo-map/{repo_key} — org_repo_map_detail

> scope：`dual`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/repo-map/{repo_key}/analyze — org_repo_map_analyze_one

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/repos — org_repos_list

> scope：`dual`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/repos — org_repos_bind

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/repos/available — org_repos_available

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/repos/batch — org_repos_bind_batch

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### DELETE /api/orgs/{org_id}/repos/{repo_key} — org_repos_unbind

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/repos/{repo_key}/transfer — org_repos_transfer

> scope：`user_only`

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

## 组织 OV 凭据格子（/api/orgs/{org_id}/ov-credentials）

### GET /api/orgs/{org_id}/ov-credentials — orgs_ov_credentials_list

> scope：`user_only`

列出本组织全部 OV 凭据格子（user_id×org）。只回存在性 / base_url / 时间戳等元数据，凭据明文与 vault 句柄永不返回。仅组织管理员可见。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### PUT /api/orgs/{org_id}/ov-credentials/{user_id} — orgs_ov_credential_put

> scope：`user_only`

为成员（含本人）配置/重置 OpenViking 凭据。明文经统一凭据库入库（全系统唯一持明文处，不落明文文件），格子行只存 vault 句柄；重置同格时句柄引用稳定。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### DELETE /api/orgs/{org_id}/ov-credentials/{user_id} — orgs_ov_credential_delete

> scope：`user_only`

吊销一格（user_id×org）：格子行与 vault 明文行同事务删除，吊销台账落一笔；执行期兑换每次实时解析、无缓存副本——吊销即刻生效、不可复活。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

## 组织仓库分析（/api/orgs/{org_id}/repo-analysis）

### GET /api/orgs/{org_id}/repo-analysis/targets — org_repo_analysis_targets

> scope：`dual`

列出组织内可用于 AI 分析的仓库目标（含可分析性判定），组织看板与 CLI 共用。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/repo-analysis/context/{repo_key} — org_repo_analysis_context

> scope：`dual`

取某仓库的组织上下文快照（成员/项目/仓库关系），供组织分析流水线拼提示词。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/repo-analysis/run — org_repo_analysis_run

> scope：`user_only`

发起一次组织分析（异步）：落分析任务并派发，返回 task_id；进度用 status 端点查。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/repo-analysis/status — org_repo_analysis_status

> scope：`dual`

查组织分析任务进度（排队/执行/完成/失败），失败回原因，不吞异常。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/repo-analysis/automation/ensure — org_repo_analysis_automation_ensure

> scope：`user_only`

确保组织分析自动化已配置（定时任务/流水线挂点幂等创建）。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### GET /api/orgs/{org_id}/repo-analysis/docs — org_repo_analysis_docs

> scope：`dual`

列出组织分析产出文档（设计说明/报告）及索引。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。

### POST /api/orgs/{org_id}/repo-map/{repo_key}/ai-result — org_repo_map_ai_result

> scope：`dual`

接收某仓库的 AI 仓库地图生成结果（写入组织仓库地图，供看板可视化）。

参数与响应字段以 `/openapi.json` 为准；组织类错误返回 `detail.error_code`。
