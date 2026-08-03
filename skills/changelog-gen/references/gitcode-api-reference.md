# GitCode API 参考

## Base URL

```
https://api.gitcode.com/api/v5
```

## Authentication

```
Authorization: Bearer {token}
# or
PRIVATE-TOKEN: {token}
```

## 安全与边界（必须遵守）

1. **只使用系统注入的 token**：调用 GitCode API 只能用 prompt 中给出的 token
   （BOT_GITCODE_TOKEN / USER_GITCODE_TOKEN）。不得从数据库、配置文件、日志、
   历史记录、环境变量或任何文件中寻找、猜测、读取其他 token。
2. **禁止访问 Bot 内部数据**：不得读取 /root/devops-bot/data/sessions.db、
   user_accounts 表、managed_repos 表、configs/ 目录（含 global-bot-token.json 等）、
   global-bot-token.json 或任何 Bot 运行目录下的数据。那是 Bot 内部基础设施。
3. **403/无权限处理**：若 API 返回 403 或权限不足，直接回复用户
   "该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试"，
   不要尝试通过其他 token 或绕行手段执行。
4. **不得写入 token 到任何文件**：不要将 token 写进日志、脚本、issue 评论或工具调用以外的地方。

## Common Endpoints

### Issues

| Action | Method | Endpoint |
|--------|--------|----------|
| Create issue | POST | `/repos/:owner/issues` (body: `repo`, `title`, `body`) |
| List issues | GET | `/repos/:owner/:repo/issues` |
| Get issue | GET | `/repos/:owner/:repo/issues/:number` |
| Create comment | POST | `/repos/:owner/:repo/issues/:number/comments` (body: `body`) |
| List comments | GET | `/repos/:owner/:repo/issues/:number/comments` |

### Pull Requests

| Action | Method | Endpoint |
|--------|--------|----------|
| Create PR | POST | `/repos/:owner/:repo/pulls` (body: `title`, `head`, `base`) |
| List PRs | GET | `/repos/:owner/:repo/pulls` |
| Get PR | GET | `/repos/:owner/:repo/pulls/:number` |
| Merge PR | PUT | `/repos/:owner/:repo/pulls/:number/merge` |
| PR comments | POST | `/repos/:owner/:repo/pulls/:number/comments` (body: `body`) |

### Repository

| Action | Method | Endpoint |
|--------|--------|----------|
| Get repo | GET | `/repos/:owner/:repo` |
| Update repo | PATCH | `/repos/:owner/:repo` (body: `name`, `description`) |
| Get file content | GET | `/repos/:owner/:repo/contents/:path?ref=:branch` |
| Create file | POST | `/repos/:owner/:repo/contents/:path` (body: `message`, `content`(base64), `branch`) |
| Update file | PUT | `/repos/:owner/:repo/contents/:path` (body: `message`, `content`(base64), `sha`, `branch`) |
| List branches | GET | `/repos/:owner/:repo/branches` |
| Create branch | POST | `/repos/:owner/:repo/branches` (body: `branch_name`, `ref`) |
| List commits | GET | `/repos/:owner/:repo/commits` |

### Webhooks

| Action | Method | Endpoint |
|--------|--------|----------|
| List hooks | GET | `/repos/:owner/:repo/hooks` |
| Create hook | POST | `/repos/:owner/:repo/hooks` (body: `url`, `push_events`, `issues_events`, etc.) |
| Update hook | PATCH | `/repos/:owner/:repo/hooks/:id` |
| Test hook | POST | `/repos/:owner/:repo/hooks/:id/tests` |

### Search

| Action | Method | Endpoint |
|--------|--------|----------|
| Search repos | GET | `/search/repositories?q=:keyword` |
| Search code | GET | `/search/code?q=:keyword` |

### User

| Action | Method | Endpoint |
|--------|--------|----------|
| Get current user | GET | `/user` |
| Get user repos | GET | `/users/:username/repos` |
| Create user repo | POST | `/user/repos` (body: `name`, `description`) |

## Pagination

```
?page=1&per_page=20    (max per_page=100)
```

## Rate Limit

Default: 400 req/min, 4000 req/hour. Returns `429 Too Many Requests` when exceeded.
