# Grape OpenAPI 接口参考

> 基于 Issue #45 设计（`docs/weekly-02/issue-45-openapi-api-design.md`、`issue-45-cli-design.md`、`issue-45-git-repos-api-design.md`）与现有代码路由整理。
> 覆盖：对外公开接口（Token/API Key 认证）+ Git 仓库统一接口 + A2A；Token 管理为内部（Web UI，不对外，不提供 OpenAPI 文档）。
> 认证方式对齐 GitCode 官方：`Authorization: Bearer` / `PRIVATE-TOKEN` / `?access_token=` 三通道。

## 全局约定（各端点文档引用，不重复展开）

| 约定 | 内容 |
|------|------|
| **Base URL** | `{{SERVER_URL}}` = 环境变量 `GRAPE_SERVER_URL`，默认 `https://<host>`；所有对外路径前缀 `/api/...` |
| **版本** | 当前无版本号（`/api/...`）；未来破坏性变更引入 `/api/v1/...`，旧版本返回 410 Gone |
| **分页** | 统一 `page`（默认 1）+ `page_size`（默认 20，最大 100）；兼容别名 `per_page`（git-repo 系列）；`limit` 仅用于 stream/游标类 |
| **时间格式** | 一律 ISO 8601 UTC（如 `2026-09-16T08:00:00Z`） |
| **ID 前缀口径** | `t_` = Agent 执行实例 / task_id（agent_tasks.id）；`tk_` = 会话 token；`grape_` = 用户级 API Key；`rta_` = 运行时凭据；`seq` = 事件序号；**`issue_id` / `issue.id` = 需求卡片 id**（`/api/issues` 创建与 notify 返回的 `task_id` 即此值，主字段为 `issue_id`）；`{number}` = 平台 issue 编号 |
| **repo_key 口径** | 统一表达式 `[gitcode:|github:]owner/repo`；对外入参写 `owner/repo`（无前缀）时由服务端按托管记录判定前缀 |
| **CLI 命令映射** | CLI 命令名由契约 `operation_id` 派生（`<tag> <verb>`，如 `scenes_list` → `grape scenes list`），**并非 HTTP 路径字面量**；`grape <cmd> --help` 可查实际对应路径 |
| **SSE** | 事件格式 `data: {…}\n\n`；流终止固定 `data: {"done": true}`；异常 `data: {"error": {"code", "message"}}`；请求头 `Accept: text/event-stream` |
| **限流/重试** | 429 时先 1s 退避，最多重试 3 次；幂等操作可无惧重放 |

## Token 作用域（x-token-scope）

契约（`/openapi.json`）每个公开 operation 含 `x-token-scope` 扩展字段（自动推导，`src/api/openapi.py`），
标识该接口可用的 token 类型，供 CLI/SDK 按凭证过滤命令：

| 值 | 含义 | 认证守卫（源码） | 可用 Token |
|----|------|------------------|------------|
| `user_only` | 仅用户级凭证 | `require_no_session_token` | `grape_` API Key / Web JWT ✅；**`tk_` 会话 token ❌（403）** |
| `session_only` | 仅会话级临时 token | 认证 `get_current_user_or_api_key` + 体内 `_repo_token`/`_repo_platform_token` 强制 | `tk_` ✅；**`grape_` API Key / JWT ❌（403）** |
| `dual` | 会话 / 用户均可 | `get_current_user_or_api_key` | 三通道全放行 |

> 设计：`docs/weekly-03/openapi-x-token-scope-design.md` + `docs/weekly-03/token-scope-classification-design.md`

### 各文档 scope 一览

| 文档 | 接口 | scope |
|------|------|:---:|
| [01 Tasks](01-tasks.md) | `notify`（tk_）/ `current`（tk_ 自省） | session_only |
| | `complete`（Event 执行） | dual |
| [02 Repos](02-repos.md) | `managed/member/sync/check-name/create/host/repair/bot-token/deploy-url/remove/project-info` | user_only |
| | `{owner}/{repo}/git-credential`、`{owner}/{repo}/workspace` | session_only |
| [03 Agents](03-agents.md) | 全部 | user_only |
| [04 Chat](04-chat.md) | 全部 | user_only |
| [05 Skills](05-skills.md) | 全部 | user_only |
| [06 Scenes](06-scenes.md) | `GET /api/scenes`（VOD 技能用 session，有意例外） | dual |
| | 其余 | user_only |
| [07 Knowledge](07-knowledge.md) | `list/stats/detail/content/context` | dual |
| | 写操作（entry/refs/delete/update） | user_only |
| [08 Events](08-events.md) | `GET /api/events/detail/{seq}` | dual |
| | 操作类（terminate/redispatch/thread/chat/knowledge） | user_only |
| [09 TaskLogs](09-task-logs.md) | 全部 | user_only |
| [10 Git 仓库](10-git-repos.md) | **全部** | session_only |
| [11 Issues](11-issues.md) | 仓库列表 + 详情/事件/pipeline + `GET status`（读） | dual |
| | `mine`（个人维度）、`create`（创建人=调用者） | user_only |
| | 操作/写类（start/`PUT status`/confirm-done/cancel/resume/pipeline/clarify/attachments 写） | user_only |
| [12 A2A](12-a2a.md) | `POST /api/a2a` | dual |
| [13 Settings](13-settings.md) | 全部（凭据/API Key/通知/OAuth/引擎/rta 凭据/mode/规则） | user_only |
| [14 Runtime](14-runtime.md) | 全部（健康度/任务/会话/标签/凭据聚合） | user_only |
| [15 DevStage](15-devstage.md) | 全部（环境 CRUD/开机/关机） | user_only |
| [16 Auth+Misc](16-auth-and-misc.md) | 全部（profile/logout/dashboard/pipeline/search/templates/feishu-onboard） | user_only |

## 目录

1. [认证与通用约定](00-auth.md)
2. [Tasks 任务接口（/api/tasks）](01-tasks.md)
3. [Repos 仓库接口（/api/repos）](02-repos.md)
4. [Agents Agent 配置接口（/api/agents）](03-agents.md)
5. [Chat 对话接口（/api/chat）](04-chat.md)
6. [Skills 技能接口（/api/skills）](05-skills.md)
7. [Scenes 场景接口（/api/scenes）](06-scenes.md)
8. [Knowledge 知识接口（/api/knowledge）](07-knowledge.md)
9. [Events 事件接口（/api/events）](08-events.md)
10. [TaskLogs 任务日志接口（/api/task-logs）](09-task-logs.md)
11. [Git 仓库统一接口（/api/git-repo）](10-git-repos.md)
12. [Issues 对外接口（/api/issues）](11-issues.md)
13. [A2A 协作接口（/api/a2a）](12-a2a.md)
14. [Settings 设置接口（/api/settings）](13-settings.md)
15. [Runtime 运行时接口（/api/runtime）](14-runtime.md)
16. [DevStage 开发环境（/api/devstage）](15-devstage.md)
17. [Auth/Misc 账号与全局（/api/auth · dashboard · pipeline · search · project-templates · feishu）](16-auth-and-misc.md)

## 状态码约定

| 码 | 含义（message 英文，Agent 可读） |
|----|------|
| 200 | Success |
| 201 | Created |
| 204 | No Content |
| 302 | Found（OAuth 重定向回调） |
| 400 | Bad Request（参数缺失/校验失败） |
| 401 | Unauthorized |
| 403 | Forbidden |
| 404 | Not Found（资源不存在） |
| 409 | Conflict（资源冲突） |
| 410 | Gone（端点已移除） |
| 429 | Too Many Requests |
| 500 | Internal Server Error |
| 502 | Bad Gateway（上游平台失败） |
| 503 | Service Unavailable |

## 错误响应统一格式

```json
{"detail": {"code": 404, "message": "Not Found"}}
```

> **约定**：
> - `detail.code` 默认等于 HTTP 状态码；业务细分用 4 位子码（如 `40401`）。
> - `detail.message` **一律英文**（主要供 Agent 读取判断）；前端展示语言由 i18n 映射，不依赖 message 原文。
> - HTTP 错误用 `HTTPException(detail={"code","message"})`；CLI/上传类业务失败（如 `tasks/notify`）用
>   `{"ok": false, "code": N, "message": "..."}`（HTTP 200 但业务失败），两者边界 =「HTTP 层错误 vs 业务层返回」。
> - 命令语义端点（grape CLI）按 `ok`/`code` 判失败；其余接口按 HTTP 状态码判。
>
> ⚠️ 旧文档中的 `{"message":"401 Unauthorized"}` 写法已废弃，统一为 `{"detail":{"code","message"}}`。