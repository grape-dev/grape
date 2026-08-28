# GitCode API 参考

## 使用规则（必须遵守）

1. **所有端点都必须以 Base URL 为前缀**：本文件下表的 Endpoint 均为相对路径，调用时必须拼上 `https://api.gitcode.com/api/v5`（例：`Create PR` → `POST https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls`）。
2. **严禁凭经验/记忆/猜测拼接 API**：只使用本文件列出的端点与域名。**不得**使用 GitLab 风格（`/api/v3`、`/api/v4`、`projects/:id/merge_requests`）或其他域名（如 `gitcode.com/api/...`）。
3. 遇到本文件未覆盖的 GitCode API 需求时，**不要臆造路径**：用 `GET /repos/{owner}/{repo}` 确认仓库信息，或在 Issue/PR 评论中说明"该操作无现成 API，需人工确认"。
4. 错误响应（404/405/500 等）说明**路径或方法有误**：先核对本文件表格，再重试；连续失败停止，不要绕行。

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

Token 已在 prompt 中给出：`$BOT_GITCODE_TOKEN`
（**不要**从代码或 env 文件中找其他值，那不是 API token。）

## 安全与边界（必须遵守）

1. **只使用系统注入的 token**：调用 GitCode API 只能用 prompt 中给出的 token
   （BOT_GITCODE_TOKEN / USER_GITCODE_TOKEN）。不得从数据库、配置文件、日志、
   历史记录、环境变量或任何文件中寻找、猜测、读取其他 token。
2. **禁止访问 Grape 内部数据**：不得读取 data/sessions.db、
   user_accounts 表、managed_repos 表、configs/ 目录（含 global-bot-token.json 等）、
   global-bot-token.json 或任何 Grape 运行目录下的数据。那是 Grape 内部基础设施。
3. **403/无权限处理**：若 API 返回 403 或权限不足，直接回复用户
   "该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试"，
   不要尝试通过其他 token 或绕行手段执行。
4. **不得写入 token 到任何文件**：不要将 token 写进日志、脚本、issue 评论或工具调用以外的地方。

## Common Endpoints

### Issues

| Action | Method | Endpoint |
|--------|--------|----------|
| Create issue | POST | `/repos/:owner/:repo/issues` (body: `title`, `body`) |
| List issues | GET | `/repos/:owner/:repo/issues` |
| Get issue | GET | `/repos/:owner/:repo/issues/:number` |
| Update issue | PATCH | `/repos/:owner/:repo/issues/:number` (JSON body: `labels` string, `assignee_ids` array, `milestone_id` int) |
| Create comment | POST | `/repos/:owner/:repo/issues/:number/comments` (body: `body`) |
| List comments | GET | `/repos/:owner/:repo/issues/:number/comments` |
| Get issue labels | GET | `/repos/:owner/:repo/issues/:number/labels` |
| Clear issue labels | DELETE | `/repos/:owner/:repo/issues/:number/labels` |

### Pull Requests

| Action | Method | Endpoint |
|--------|--------|----------|
| Create PR | POST | `/repos/:owner/:repo/pulls` |
| List PRs | GET | `/repos/:owner/:repo/pulls` |
| Get PR | GET | `/repos/:owner/:repo/pulls/:number` |
| Merge PR | PUT | `/repos/:owner/:repo/pulls/:number/merge` |
| PR comments | POST | `/repos/:owner/:repo/pulls/:number/comments` (body: `body`) |

**Create PR body 字段**（JSON）：
- `title`：PR 标题（如 `feat: #{issue} 标题`）
- `head`：源分支（如 `feat/issue-{issue}`，即你 push 的分支）
- `base`：目标分支（通常 `main` / `master`）
- `inner_issue_nums`：关联 Issue（格式 `{owner}/{repo}#{issue}`，用于 PR 关联 Issue）
- `close_related_issue`：`true` 表示 PR 合并后自动关闭关联 Issue

> 完整调用示例：
> ```bash
> curl -sL -X POST -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
>   -H "Content-Type: application/json" \
>   -d '{"title": "feat: #7 标题", "head": "feat/issue-7", "base": "main",
>        "inner_issue_nums": "Jason7602/gitcode-pr-assistant#7", "close_related_issue": true}' \
>   https://api.gitcode.com/api/v5/repos/Jason7602/gitcode-pr-assistant/pulls
> ```

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

### Labels

| Action | Method | Endpoint |
|--------|--------|----------|
| List labels | GET | `/repos/:owner/:repo/labels` |
| Create label | POST | `/repos/:owner/:repo/labels` (form-encoded: `name`, `color`) |
| Update label | PATCH | `/repos/:owner/:repo/labels/:name` (form-encoded: `name`, `color`) |
| Delete label | DELETE | `/repos/:owner/:repo/labels/:name` |

### Milestones

| Action | Method | Endpoint |
|--------|--------|----------|
| List milestones | GET | `/repos/:owner/:repo/milestones?state=all` |

### User

| Action | Method | Endpoint |
|--------|--------|----------|
| Get current user | GET | `/user` |
| Get user repos | GET | `/users/:username/repos` |
| Create user repo | POST | `/user/repos` (body: `name`, `description`) |
| List collaborators | GET | `/repos/:owner/:repo/collaborators` |

## Pagination

```
?page=1&per_page=20    (max per_page=100)
```

## Rate Limit

Default: 400 req/min, 4000 req/hour. Returns `429 Too Many Requests` when exceeded.
