---
name: gitcode-api-guidance
version: 1.0.0
description: |
  GitCode API 交互指南。当需要与 GitCode 平台进行任何 API 交互时使用此技能，包括：
  创建/更新仓库、管理 Issue、操作 Pull Request、配置 Webhook、管理分支、
  查看提交记录、搜索代码/用户/仓库、管理组织、处理 Release 等。
  只要涉及 GitCode 平台操作、GitCode API 调用、gitcode.com 交互，
  或用户提到 GitCode/AtomGit 的任何功能，都应使用此技能。
triggers:
  - event_type: push
  - event_type: pull_request_opened
  - event_type: issue_opened
tools:
  - curl
  - git
---

# GitCode API 交互指南

## 场景

当 Agent 需要与 GitCode 平台进行 API 交互时，此技能提供完整的 API 端点参考、认证方式、请求/响应格式和常见操作模式，使 Agent 能够快速准确地调用 GitCode API 完成任务。

## 知识

### 基础信息

- **API 基础路径**: `https://api.gitcode.com/api/v5`
- **API 版本**: v5（当前最新）
- **文档地址**: https://docs.gitcode.com/docs/apis/

### 认证方式（三选一）

所有需要认证的 API 请求必须携带 Token。Token 获取地址：https://gitcode.com/setting/token-classic

**方式一：Authorization Header（推荐）**
```
Authorization: Bearer {token}
```

**方式二：PRIVATE-TOKEN Header**
```
PRIVATE-TOKEN: {token}
```

**方式三：Query Parameter**
```
?access_token={token}
```

### 速率限制

- 默认限制：400 次/分钟，4000 次/小时
- 超限返回 `429 Too Many Requests`

### 通用请求模式

```bash
# GET 请求
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.gitcode.com/api/v5/{endpoint}"

# POST 请求
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key": "value"}' \
  "https://api.gitcode.com/api/v5/{endpoint}"
```

## 步骤

### 1. 确定操作目标

根据任务确定需要调用的 API 端点。参考下方 API 速查表或 `references/endpoints.md` 获取完整端点列表。

### 2. 构造请求

按照端点要求构造请求，确保：
- 携带正确的认证信息
- 路径参数（`:owner`, `:repo`, `:number` 等）已替换
- 必填字段已包含在请求体中
- Content-Type 设置为 `application/json`

### 3. 执行并处理响应

执行请求后根据状态码处理：
- `200` / `201`：成功，解析响应体
- `401`：认证失败，检查 Token
- `404`：资源不存在或无权限
- `422`：请求参数错误
- `429`：速率限制，等待后重试

### 4. 分页处理

列表接口支持分页参数：
- `page`：页码（从 1 开始）
- `per_page`：每页数量（默认 20，最大 100）

## API 速查表

### 仓库操作

| 操作 | 方法 | 端点 |
|------|------|------|
| 获取仓库目录 Tree | GET | `/repos/:owner/:repo/git/trees/:sha` |
| 获取文件内容 | GET | `/repos/:owner/:repo/contents/:path` |
| 新建文件 | POST | `/repos/:owner/:repo/contents/:path` |
| 更新文件 | PUT | `/repos/:owner/:repo/contents/:path` |
| 删除文件 | DELETE | `/repos/:owner/:repo/contents/:path` |
| 获取文件 Blob | GET | `/repos/:owner/:repo/git/blobs/:sha` |
| 获取仓库语言 | GET | `/repos/:owner/:repo/languages` |
| 获取贡献者 | GET | `/repos/:owner/:repo/contributors` |
| 更新仓库设置 | PATCH | `/repos/:owner/:repo` |
| 删除仓库 | DELETE | `/repos/:owner/:repo` |
| Fork 仓库 | POST | `/repos/:owner/:repo/forks` |
| 上传文件 | POST | `/repos/:owner/:repo/file-upload` |
| 获取 Raw 文件 | GET | `/repos/:owner/:repo/raw/:path` |

### Issue 操作

| 操作 | 方法 | 端点 |
|------|------|------|
| 创建 Issue | POST | `/repos/:owner/issues` |
| 更新 Issue | PATCH | `/repos/:owner/issues/:number` |
| 获取单个 Issue | GET | `/repos/:owner/:repo/issues/:number` |
| 获取所有 Issues | GET | `/repos/:owner/:repo/issues` |
| 创建 Issue 评论 | POST | `/repos/:owner/:repo/issues/:number/comments` |
| 获取 Issue 评论 | GET | `/repos/:owner/:repo/issues/:number/comments` |
| 创建 Issue 标签 | POST | `/repos/:owner/:repo/issues/:number/labels` |

### Pull Request 操作

| 操作 | 方法 | 端点 |
|------|------|------|
| 获取 PR 列表 | GET | `/repos/:owner/:repo/pulls` |
| 创建 PR | POST | `/repos/:owner/:repo/pulls` |
| 获取单个 PR | GET | `/repos/:owner/:repo/pulls/:number` |
| 合并 PR | PUT | `/repos/:owner/:repo/pulls/:number/merge` |
| 更新 PR | PATCH | `/repos/:owner/:repo/pulls/:number` |
| PR 评论 | POST | `/repos/:owner/:repo/pulls/:number/comments` |
| PR 文件变更 | GET | `/repos/:owner/:repo/pulls/:number/files` |
| PR Commits | GET | `/repos/:owner/:repo/pulls/:number/commits` |
| PR 审查 | POST | `/repos/:owner/:repo/pulls/:number/review` |
| 指派审查人 | POST | `/repos/:owner/:repo/pulls/:number/assignees` |

### Webhook 操作

| 操作 | 方法 | 端点 |
|------|------|------|
| 列出 Webhooks | GET | `/repos/:owner/:repo/hooks` |
| 创建 Webhook | POST | `/repos/:owner/:repo/hooks` |
| 获取单个 Webhook | GET | `/repos/:owner/:repo/hooks/:id` |
| 更新 Webhook | PATCH | `/repos/:owner/:repo/hooks/:id` |
| 删除 Webhook | DELETE | `/repos/:owner/:repo/hooks/:id` |
| 测试 Webhook | POST | `/repos/:owner/:repo/hooks/:id/tests` |

### 分支操作

| 操作 | 方法 | 端点 |
|------|------|------|
| 获取分支列表 | GET | `/repos/:owner/:repo/branches` |
| 获取单个分支 | GET | `/repos/:owner/:repo/branches/:branch` |
| 创建分支 | POST | `/repos/:owner/:repo/branches` |
| 删除分支 | DELETE | `/repos/:owner/:repo/branches/:branch` |

### Commit 操作

| 操作 | 方法 | 端点 |
|------|------|------|
| 获取 Commit 列表 | GET | `/repos/:owner/:repo/commits` |
| 获取单个 Commit | GET | `/repos/:owner/:repo/commits/:sha` |
| 比较 Commit | GET | `/repos/:owner/:repo/compare/:base...:head` |

### 搜索

| 操作 | 方法 | 端点 |
|------|------|------|
| 搜索仓库 | GET | `/search/repositories` |
| 搜索用户 | GET | `/search/users` |
| 搜索代码 | GET | `/search/code` |
| 搜索 Issue | GET | `/search/issues` |

### 组织操作

| 操作 | 方法 | 端点 |
|------|------|------|
| 创建组织仓库 | POST | `/orgs/:org/repos` |
| 获取组织仓库列表 | GET | `/orgs/:org/repos` |
| 获取组织成员 | GET | `/orgs/:org/members` |
| 列出用户组织 | GET | `/users/:username/orgs` |

### Release 操作

| 操作 | 方法 | 端点 |
|------|------|------|
| 获取 Release 列表 | GET | `/repos/:owner/:repo/releases` |
| 创建 Release | POST | `/repos/:owner/:repo/releases` |
| 获取单个 Release | GET | `/repos/:owner/:repo/releases/:id` |
| 更新 Release | PATCH | `/repos/:owner/:repo/releases/:id` |
| 删除 Release | DELETE | `/repos/:owner/:repo/releases/:id` |

### 成员操作

| 操作 | 方法 | 端点 |
|------|------|------|
| 添加仓库成员 | PUT | `/repos/:owner/:repo/collaborators/:username` |
| 删除仓库成员 | DELETE | `/repos/:owner/:repo/collaborators/:username` |

## 常见操作示例

### 创建 Webhook

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/:owner/:repo/hooks" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "hook_url": "https://your-server.com/webhook/gitcode",
    "hook_events": ["push", "pull_request", "issue"],
    "hook_active": true
  }'
```

### 创建 Issue

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/:owner/issues" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo": ":repo",
    "title": "Bug report title",
    "body": "Issue description in markdown"
  }'
```

### 创建 Pull Request

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/:owner/:repo/pulls" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "PR title",
    "body": "PR description",
    "head": "feature-branch",
    "base": "main"
  }'
```

### 获取仓库文件内容

```bash
curl "https://api.gitcode.com/api/v5/repos/:owner/:repo/contents/:path?ref=main" \
  -H "Authorization: Bearer $TOKEN"
```

响应中 `content` 字段为 Base64 编码的文件内容。

### 在 PR 上发表评论

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/:owner/:repo/pulls/:number/comments" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "body": "Review comment content",
    "position": 1,
    "commit_id": "abc123",
    "path": "src/main.go"
  }'
```

## 判断标准

- API 调用返回 2xx 状态码
- 请求体符合 API 要求的 JSON 格式
- 认证信息正确携带
- 分页参数合理设置

## 输出规范

API 调用结果以 JSON 格式返回。对于需要向用户展示的操作结果，提取关键字段并格式化输出：

```markdown
## 操作结果

**操作**: [创建 Issue / 合并 PR / ...]
**状态**: 成功 / 失败
**详情**: [关键字段：number, html_url, state 等]
```

## 进阶参考

完整的端点参数详情和更多 API 示例，请参阅：
- `references/endpoints.md` — 全量 API 端点参数参考
- `references/webhook-events.md` — Webhook 事件类型与 Payload 格式
