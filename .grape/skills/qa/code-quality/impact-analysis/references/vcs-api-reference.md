# VCS API 参考

## Base URL

```
$VCS_API_BASE
```

## Authentication

```
Authorization: Bearer {token}
# or
PRIVATE-TOKEN: {token}
```

Token 已在 prompt 中给出：`$BOT_TOKEN`
（**不要**从代码或 env 文件中找其他值，那不是 API token。）

## 安全与边界（必须遵守）

1. **只使用系统注入的 token**：调用 VCS API 只能用 prompt 中给出的 token
   （BOT_TOKEN / USER_TOKEN）。不得从数据库、配置文件、日志、
   历史记录、环境变量或任何文件中寻找、猜测、读取其他 token。
2. **禁止访问 Grape 内部数据**：不得读取 data/sessions.db、
   user_accounts 表、managed_repos 表、configs/ 目录（含 global-bot-token.json 等）、
   global-bot-token.json 或任何 Grape 运行目录下的数据。那是 Grape 内部基础设施。
3. **403/无权限处理**：若 API 返回 403 或权限不足，直接回复用户
   "该操作需要管理员/维护者权限，请在 仓库设置中手动配置后重试"，
   不要尝试通过其他 token 或绕行手段执行。
4. **不得写入 token 到任何文件**：不要将 token 写进日志、脚本、issue 评论或工具调用以外的地方。

## 影响范围评估常用端点

### Pull Requests

| Action | Method | Endpoint |
|--------|--------|----------|
| Get PR | GET | `/repos/:owner/:repo/pulls/:number` |
| PR files (含 diff) | GET | `/repos/:owner/:repo/pulls/:number/files` |
| PR comments (行内) | POST | `/repos/:owner/:repo/pulls/:number/comments` (body: `body`, `commit_id`, `path`, `position`) |
| List PR comments | GET | `/repos/:owner/:repo/pulls/:number/comments` |
| Resolve comment | PUT | `/repos/:owner/:repo/pulls/:number/comments/:discussion_id` (body: `{"resolved": true}`) |
| PR review comment (总结) | POST | `/repos/:owner/:repo/pulls/:number/comments` (body: `body`) |

### Repository

| Action | Method | Endpoint |
|--------|--------|----------|
| Get file content | GET | `/repos/:owner/:repo/contents/:path?ref=:branch` |
| List commits | GET | `/repos/:owner/:repo/commits` |

### Search

| Action | Method | Endpoint |
|--------|--------|----------|
| Search code (找符号引用) | GET | `/search/code?q=:keyword` |

### Issues（关联 Issue 评论）

| Action | Method | Endpoint |
|--------|--------|----------|
| Create comment | POST | `/repos/:owner/:repo/issues/:number/comments` (body: `body`) |

## PR files 响应结构

`GET /repos/:owner/:repo/pulls/:number/files` 返回数组，每个元素：

```json
{
  "filename": "src/api/event_pipeline.py",
  "status": "modified",
  "additions": 12,
  "deletions": 3,
  "changes": 15,
  "sha": "abc123",
  "patch": "@@ -239,7 +239,9 @@ def run_pipeline(...):\n def run_pipeline(...):\n+    strict_mode=False\n..."
}
```

- `patch` 为 unified diff 文本，解析 hunk 头 `@@ -old,count +new,count @@` 定位行号
- `status`：added / modified / removed / renamed
- 大 PR 可能分页，用 `?page=1&per_page=100` 翻页

## 行内评论 position 计算

发布行内评论需 `position`（diff 中的行号）：
1. 从 PR files API 的 `patch` 解析 hunk
2. `position` = 问题行在 hunk 中的相对位置（从 hunk 新侧 `+` 行起算）
3. `commit_id` = PR head SHA
4. `path` = 问题文件路径

## Pagination

```
?page=1&per_page=20    (max per_page=100)
```

## Rate Limit

Default: 400 req/min, 4000 req/hour. Returns `429 Too Many Requests` when exceeded.

## GitHub 与 GitCode 差异对照（重要！）

调用 VCS API 前先确认仓库 provider（由 prompt 的 VCS API Access 段指定），按下表选择正确的参数格式：

| 功能 | GitCode | GitHub |
|------|---------|--------|
| Issue 关闭 | `state="close"` | `state="closed"` |
| labels 格式 | 逗号分隔字符串 `"bug,feature"` | 数组 `["bug","feature"]` |
| assignees | `assignee` 单值字符串 | `assignees` 数组 `["user1"]` |
| Create Issue body | 需带 `repo` 字段 | 不需要（repo 在 URL 里） |
| PR 普通评论 | `POST /pulls/:n/comments` | `POST /issues/:n/comments`（GitHub 把 PR 视为 Issue） |
| Create file | `POST /contents/:path` | `PUT /contents/:path` |
| Update file | `PUT /contents/:path`（需 `sha`） | `PUT /contents/:path`（需 `sha`） |
| Create branch | `POST /branches`（body: `branch_name`, `ref`） | `POST /git/refs`（body: `ref`, `sha`） |
| Delete file | 不支持 | `DELETE /contents/:path` |
| Accept header | 不需要 | `application/vnd.github+json`（必须带！） |
| Create label | form-encoded | JSON body |
| Add labels to issue | 通过 `PATCH /issues/:n` 的 `labels` 字段 | `POST /issues/:n/labels`（专用端点） |
| Rate limit | 400 req/min | 5000 req/hour（认证用户） |
| 关联 Issue | `related_issue_nums`（Issue 号数组，如 `[12]`）/ `auto_close_issues` | PR create 的 `related_issue_nums` 字段关联（勿用 `inner_issue_nums`/`close_related_issue` 旧字段） |
