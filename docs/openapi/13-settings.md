# Settings 设置接口（/api/settings）

> 对外公开（openapi-full-coverage-design.md B/D 批收录），认证：`require_no_session_token`。
> scope（x-token-scope）：**全部 = user_only**（`grape_` API Key / JWT；`tk_` → 403）。
> 面向：当前登录用户的账号配置（凭据 / API Key / 通知 / OAuth / 引擎 / 规则 / mode）。

## 凭据 Credentials

### GET /api/settings/credentials — 获取第三方平台凭据状态

> scope：user_only

| Query | `repo_key`（可选，确定仓库前缀） |
| 出参 | `{gitcode: {api_token_exists, account, auto_filled}, github: {token_exists, login}, huawei_cloud: {ak_exists, sk_exists, ak, sk}, codearts: {ak_exists, sk_exists}}` |
| 返回 | 200 |

### PUT /api/settings/credentials — 更新第三方平台凭据

> scope：user_only

请求头：`Content-Type: application/json`

请求体（UpdateCredentials）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `huawei_cloud_ak` / `huawei_cloud_sk` | string | 否 | 华为云 AK/SK |
| `codearts_ak` / `codearts_sk` | string | 否 | CodeArts 凭据 |
| `github_token` | string | 否 | GitHub PAT（保存时联网校验并绑定 github_login） |
| `repo_key` | string | 否 | 仓库前缀 |

> 占位符/空值不写入（`AK-demo-*`/`SK-demo-*`/`xxxx` 一律拒绝，防覆盖真实凭据）。

| 出参 | `{"message": "..."}` |
| 返回 | 200 |

---

## API Key 管理（Issue #69）

### POST /api/settings/api-keys — 生成 API Key

> scope：user_only

请求头：`Content-Type: application/json`

请求体（CreateApiKeyBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `name` | string | ✅ | Key 名称（≤64 字符） |

| 出参 | `{"id", "key": "grape_xxx", "name", "message"}`（**明文仅此一次返回**，DB 存 SHA-256） |
| 返回 | 200；400 name 空/超长；500 生成失败（同名已存在） |

### GET /api/settings/api-keys — 列出我的 API Key

> scope：user_only

| 出参 | `{keys: [{id, name, prefix, created_at, last_used_at}]}`（不含明文） |
| 返回 | 200 |

### DELETE /api/settings/api-keys/{key_id} — 吊销 API Key

> scope：user_only

| Path | `{key_id}` int |
| 出参 | `{"message": "..."}`（软删除 revoked=1，立即失效，保审计） |
| 返回 | 200；404 不存在/已吊销 |

---

## 通知偏好

### GET /api/settings/notifications — 获取通知配置

> scope：user_only

| 出参 | `{issues, pr, build, release, vod, notifications_enabled}` |
| 返回 | 200 |

### PUT /api/settings/notifications — 更新通知配置

> scope：user_only

请求体（NotificationPrefs）：`{vod: bool, notifications_enabled: bool}`

| 出参 | `{"message": "..."}` |
| 返回 | 200 |

---

## OAuth 配置（平台级 meta）

### GET /api/settings/oauth — 获取 GitCode OAuth 配置

> scope：user_only

| 出参 | `{gitcode_oauth_client_id: bool, gitcode_oauth_redirect_uri: str}`（不泄露 secret） |
| 返回 | 200 |

### PUT /api/settings/oauth — 更新 GitCode OAuth 配置

> scope：user_only

请求体（OAuthConfig）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `gitcode_oauth_client_id` | string | 否 | 客户端 id |
| `gitcode_oauth_client_secret` | string | 否 | 客户端 secret |
| `gitcode_oauth_redirect_uri` | string | 否 | 回调地址 |

| 出参 | `{"message": "..."}` |
| 返回 | 200 |

### GET /api/settings/oauth-github — 获取 GitHub OAuth 配置

> scope：user_only

| 出参 | `{github_oauth_client_id: bool, github_oauth_redirect_uri: str}` |
| 返回 | 200 |

### PUT /api/settings/oauth-github — 更新 GitHub OAuth 配置

> scope：user_only

请求体（GitHubOAuthConfig，字段同 OAuthConfig：`github_oauth_client_id` / `github_oauth_client_secret` / `github_oauth_redirect_uri`）。

| 出参 | `{"message": "..."}` |
| 返回 | 200 |

---

## GitCode 仓库配置

### GET /api/settings/gitcode-repo/{owner}/{repo} — 获取仓库 GitCode 配置

> scope：user_only

| Path | `{owner}` `{repo}` |
| 出参 | 该仓库的 GitCode 相关配置 |
| 返回 | 200 |

### PUT /api/settings/gitcode-repo/{owner}/{repo} — 更新仓库 GitCode 配置

> scope：user_only

请求头：`Content-Type: application/json`

请求体（GitCodeRepoConfig）：仓库级配置对象。

| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## 引擎 Runtimes

### GET /api/settings/agent-runtimes — 引擎列表

> scope：user_only

> 从 DB 读取 Agent 执行引擎列表，含健康态派生与唯一引擎名。可见性：全局 + 个人（owner=当前用户）+ 我所在仓库的仓库级。

| 出参 | `[{id, name, instance_name, worker_name, worker_ip, scope, owner, status, ...}]` |
| 返回 | 200 |

### POST /api/settings/agent-runtimes/refresh — 手动刷新引擎

> scope：user_only

| 出参 | `{"runtimes": [...]}`（发现本地引擎 + 子进程检测后写 DB） |
| 返回 | 200；503 刷新失败 |

### GET /api/settings/backend-agents — 后端 Agent 列表（兼容别名）

> scope：user_only

| 出参 | 同 `agent-runtimes`（兼容旧路径） |
| 返回 | 200 |

### POST /api/settings/backend-agents/refresh — 刷新后端 Agent（兼容别名）

> scope：user_only

| 出参 | 同 `agent-runtimes/refresh` |
| 返回 | 200；503 |

### GET /api/settings/agent-runtimes/{rid}/tags — 引擎标签列表

> scope：user_only

| Path | `{rid}` int |
| 出参 | 标签数组 |
| 返回 | 200 |

### POST /api/settings/agent-runtimes/{rid}/tags — 添加引擎标签

> scope：user_only

请求头：`Content-Type: application/json`

请求体（EngineTagBody）：标签对象（幂等：已有则更新）。

| 出参 | `{"ok": true}` |
| 返回 | 200 |

### PUT /api/settings/agent-runtimes/{rid}/tags/{tag} — 修改引擎标签

> scope：user_only

| Path | `{rid}` int、`{tag}` |
| 请求体（EngineTagUpdateBody） | 新标签配置 |
| 出参 | `{"ok": true}` |
| 返回 | 200 |

### DELETE /api/settings/agent-runtimes/{rid}/tags/{tag} — 删除引擎标签

> scope：user_only

| Path | `{rid}` int、`{tag}` |
| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## Runtime 凭据（rta_ token）

### GET /api/settings/runtime-credentials — 凭据列表

> scope：user_only

| 出参 | `[{id, name, scope, owner, repo_key, created_at}]`（不含 token_hash / 明文） |
| 返回 | 200 |

### POST /api/settings/runtime-credentials — 创建运行时凭据

> scope：user_only

请求头：`Content-Type: application/json`

请求体（RuntimeCredBody）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `name` | string | ✅ | worker 名（全局唯一） |
| `scope` | string | 否 | `user`（默认）/ `repo` / `global` |
| `repo_key` | string | repo 时必填 | `owner/repo` 归属仓库 |

| 出参 | `{"token": "rta_xxx", "id", "name", "scope", "owner", "message"}`（**明文仅一次**，DB 存 sha256；owner=当前登录用户不可选） |
| 返回 | 200；400 缺 name / scope 非法 / 重名；500 生成失败 |

### PUT /api/settings/runtime-credentials/{cid} — 改名凭据

> scope：user_only

| Path | `{cid}` int |
| 请求体（RuntimeCredBody） | `name`（owner/scope 不可改） |
| 出参 | `{"message": "..."}` |
| 返回 | 200；400 重名/非 active；404 不存在 |

### PUT /api/settings/runtime-credentials/{cid}/scope — 切换凭据 scope

> scope：user_only

| Path | `{cid}` int |
| 请求体（CredScopeBody） | 目标 scope（不得为 global） |
| 出参 | `{"message": "..."}` |
| 返回 | 200；400 非法/非 active；403 非 owner；404 |

### POST /api/settings/runtime-credentials/{cid}/revoke — 吊销凭据

> scope：user_only

| Path | `{cid}` int |

> 吊销=删除：删 runtime_credentials 行 + 级联删其 agent_runtimes 行；devops_agent_session.runtime_id 置 0；历史任务恢复走 `/api/events/{seq}/redispatch`。

| 出参 | `{"ok": true}` |
| 返回 | 200；404 |

---

## 执行模式 / 流水线模式

### GET /api/settings/execution-mode — 获取执行模式

> scope：user_only

| 出参 | 当前执行模式配置 |
| 返回 | 200 |

### PUT /api/settings/execution-mode — 设置执行模式

> scope：user_only

请求体（ExecModeBody）：执行模式配置。

| 出参 | `{"ok": true}` |
| 返回 | 200 |

### GET /api/settings/pipeline-mode — 获取流水线模式

> scope：user_only

| 出参 | 当前流水线模式 |
| 返回 | 200 |

### PUT /api/settings/pipeline-mode — 设置流水线模式

> scope：user_only

请求头：`Content-Type: application/json`

请求体：流水线模式配置。

| 出参 | `{"ok": true}` |
| 返回 | 200 |

---

## 事件 Action 过滤规则（热生效 ≤30s）

### GET /api/settings/event-action-filters — 全部过滤规则

> scope：user_only

| 出参 | `[{rule_id, event_type, action, allow, actor, changes, side_effect, priority, enabled}]` |
| 返回 | 200 |

### POST /api/settings/event-action-filters — 新增过滤规则

> scope：user_only

请求头：`Content-Type: application/json`

请求体（EventActionFilterBody）：`event_type` × `action` 规则（幂等：同组合已存在则跳过）。

| 出参 | `{"ok": true}` |
| 返回 | 200 |

### PUT /api/settings/event-action-filters/{rule_id} — 修改过滤规则

> scope：user_only

| Path | `{rule_id}` int |
| 请求体（EventActionFilterUpdateBody） | 修改 allow/actor/changes/副作用/优先级/启用（热生效 ≤30s） |
| 出参 | `{"ok": true}` |
| 返回 | 200 |

### DELETE /api/settings/event-action-filters/{rule_id} — 删除过滤规则

> scope：user_only

| Path | `{rule_id}` int |
| 出参 | `{"ok": true}`（删除后无规则=放行） |
| 返回 | 200 |

---

## 错误返回（全端点通用）

| 码 | message（英文） | 场景 |
|----|--------------|------|
| 400 | Bad Request / Invalid parameter | name 空、scope 非法、凭据重名（全局唯一） |
| 401 | Unauthorized | 未认证 / token 无效 |
| 403 | Forbidden | `tk_` 会话 token 不适用（user_only） |
| 404 | Not Found | key/cid/rule 不存在 |
| 500 | Internal Server Error | 生成/刷新失败 |
| 503 | Service Unavailable | 引擎刷新失败 |

```json
{"detail": {"code": 404, "message": "Not Found"}}
```