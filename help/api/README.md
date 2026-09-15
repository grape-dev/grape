# Grape OpenAPI 接口参考

> 基于 Issue #45 设计（`docs/weekly-02/issue-45-openapi-api-design.md`、`issue-45-cli-design.md`、`issue-45-git-repos-api-design.md`）与现有代码路由整理。
> 覆盖：对外公开接口（Token/API Key 认证）+ Git 仓库统一接口 + A2A；Token 管理为内部（Web UI，不对外，不提供 OpenAPI 文档）。
> 认证方式对齐 GitCode 官方：`Authorization: Bearer` / `PRIVATE-TOKEN` / `?access_token=` 三通道。

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
| [01 Tasks](01-tasks.md) | `mine/start/status/complete/repo/{owner}/{repo}` | dual |
| | `confirm-done/cancel/resume/clarifications/clarify/pipeline` | user_only |
| [02 Repos](02-repos.md) | `managed/member/sync/check-name/create/host/repair/bot-token/deploy-url/remove/project-info` | user_only |
| | `{owner}/{repo}/git-credential`、`{owner}/{repo}/workspace` | session_only |
| [03 Agents](03-agents.md) | 全部 | user_only |
| [04 Chat](04-chat.md) | 全部 | user_only |
| [05 Skills](05-skills.md) | 全部 | user_only |
| [06 Scenes](06-scenes.md) | `GET /api/scenes`（VOD 技能用 session，有意例外） | dual |
| | 其余 | user_only |
| [07 Knowledge](07-knowledge.md) | `list/stats/detail/content/context` | dual |
| | 写操作（entry/archive/refs/delete/update） | user_only |
| [08 Events](08-events.md) | `GET /api/events/detail/{seq}` | dual |
| | 操作类（terminate/redispatch/thread/chat/knowledge） | user_only |
| [09 TaskLogs](09-task-logs.md) | 全部 | user_only |
| [10 Git 仓库](10-git-repos.md) | **全部** | session_only |
| [11 Issues](11-issues.md) | 全部 | dual |
| [12 A2A](12-a2a.md) | `POST /api/a2a` | dual |

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