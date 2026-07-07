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
