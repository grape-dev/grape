# GitCode API 端点参数参考

## 目录

1. [认证](#认证)
2. [仓库 Repositories](#仓库)
3. [Issue](#issue)
4. [Pull Request](#pull-request)
5. [Webhook](#webhook)
6. [分支 Branch](#分支)
7. [Commit](#commit)
8. [搜索 Search](#搜索)
9. [组织 Organization](#组织)
10. [Release](#release)
11. [成员 Member](#成员)
12. [标签 Label](#标签)
13. [里程碑 Milestone](#里程碑)
14. [用户 User](#用户)

---

## 认证

Token 获取：https://gitcode.com/setting/token-classic

三种认证方式等价，优先使用 `Authorization: Bearer {token}`。

---

## 仓库

### 获取仓库目录 Tree

```
GET /repos/:owner/:repo/git/trees/:sha
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| owner | path | 是 | 仓库所属用户/组织 |
| repo | path | 是 | 仓库名 |
| sha | path | 是 | 分支名或 commit SHA |

### 获取文件内容

```
GET /repos/:owner/:repo/contents/:path
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| owner | path | 是 | 仓库所属 |
| repo | path | 是 | 仓库名 |
| path | path | 是 | 文件路径 |
| ref | query | 否 | 分支名/tag/commit SHA |

响应 `content` 字段为 Base64 编码。

### 新建文件

```
POST /repos/:owner/:repo/contents/:path
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| path | path | 是 | 文件路径 |
| message | body | 是 | commit 消息 |
| content | body | 是 | 文件内容（Base64 编码） |
| branch | body | 否 | 目标分支，默认默认分支 |

### 更新文件

```
PUT /repos/:owner/:repo/contents/:path
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| path | path | 是 | 文件路径 |
| message | body | 是 | commit 消息 |
| content | body | 是 | 新内容（Base64） |
| sha | body | 是 | 原文件的 blob SHA |
| branch | body | 否 | 目标分支 |

### 删除文件

```
DELETE /repos/:owner/:repo/contents/:path
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| path | path | 是 | 文件路径 |
| message | body | 是 | commit 消息 |
| sha | body | 是 | 文件的 blob SHA |
| branch | body | 否 | 目标分支 |

### 获取仓库语言

```
GET /repos/:owner/:repo/languages
```

### 获取贡献者

```
GET /repos/:owner/:repo/contributors
```

### Fork 仓库

```
POST /repos/:owner/:repo/forks
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| organization | body | 否 | Fork 到指定组织 |

### 上传文件

```
POST /repos/:owner/:repo/file-upload
```

使用 `multipart/form-data` 上传。

### 获取 Raw 文件

```
GET /repos/:owner/:repo/raw/:path?ref=:branch
```

直接返回文件原始内容，非 JSON。

---

## Issue

### 创建 Issue

```
POST /repos/:owner/issues
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| owner | path | 是 | 仓库所属 |
| repo | body | 是 | 仓库名 |
| title | body | 是 | Issue 标题 |
| body | body | 否 | Issue 描述（Markdown） |
| assignees | body | 否 | 指派人（数组） |
| labels | body | 否 | 标签（数组） |
| milestone | body | 否 | 里程碑 ID |

### 更新 Issue

```
PATCH /repos/:owner/issues/:number
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| number | path | 是 | Issue 编号 |
| title | body | 否 | 新标题 |
| body | body | 否 | 新描述 |
| state | body | 否 | `open` 或 `closed` |
| assignees | body | 否 | 指派人 |
| labels | body | 否 | 标签 |

### 获取所有 Issues

```
GET /repos/:owner/:repo/issues
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| state | query | 否 | `open`, `closed`, `all` |
| labels | query | 否 | 逗号分隔的标签名 |
| sort | query | 否 | `created`, `updated` |
| direction | query | 否 | `asc`, `desc` |
| page | query | 否 | 页码 |
| per_page | query | 否 | 每页数量 |

### 创建 Issue 评论

```
POST /repos/:owner/:repo/issues/:number/comments
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| number | path | 是 | Issue 编号 |
| body | body | 是 | 评论内容（Markdown） |

---

## Pull Request

### 获取 PR 列表

```
GET /repos/:owner/:repo/pulls
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| state | query | 否 | `open`, `closed`, `all` |
| head | query | 否 | 源分支过滤（`owner:branch`） |
| base | query | 否 | 目标分支过滤 |
| sort | query | 否 | 排序字段 |
| direction | query | 否 | 排序方向 |
| page | query | 否 | 页码 |
| per_page | query | 否 | 每页数量 |

### 创建 PR

```
POST /repos/:owner/:repo/pulls
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| title | body | 是 | PR 标题 |
| body | body | 否 | PR 描述 |
| head | body | 是 | 源分支 |
| base | body | 是 | 目标分支 |

### 合并 PR

```
PUT /repos/:owner/:repo/pulls/:number/merge
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| number | path | 是 | PR 编号 |
| commit_message | body | 否 | 合并 commit 消息 |
| squash | body | 否 | 是否 squash 合并 |

### PR 评论

```
POST /repos/:owner/:repo/pulls/:number/comments
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| number | path | 是 | PR 编号 |
| body | body | 是 | 评论内容 |
| commit_id | body | 否 | 评论的 commit SHA |
| path | body | 否 | 评论的文件路径 |
| position | body | 否 | 文件中的行号 |

### PR 文件变更

```
GET /repos/:owner/:repo/pulls/:number/files
```

### PR 审查

```
POST /repos/:owner/:repo/pulls/:number/review
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| number | path | 是 | PR 编号 |
| body | body | 否 | 审查意见 |
| event | body | 否 | `APPROVE`, `REQUEST_CHANGES`, `COMMENT` |

---

## Webhook

### 列出 Webhooks

```
GET /repos/:owner/:repo/hooks
```

### 创建 Webhook

```
POST /repos/:owner/:repo/hooks
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| hook_url | body | 是 | 回调 URL |
| hook_events | body | 是 | 事件类型数组 |
| hook_active | body | 否 | 是否启用（默认 true） |
| hook_type | body | 否 | Hook 类型 |

**可用事件类型**：
- `push` — 代码推送
- `pull_request` — PR 事件
- `issues` — Issue 事件
- `issue_comment` — Issue 评论
- `pull_request_comment` — PR 评论
- `release` — Release 事件
- `tag_push` — Tag 推送
- `member` — 成员变更
- `note` — 评论事件

### 获取单个 Webhook

```
GET /repos/:owner/:repo/hooks/:id
```

### 更新 Webhook

```
PATCH /repos/:owner/:repo/hooks/:id
```

可更新 `hook_url`, `hook_events`, `hook_active` 等字段。

### 删除 Webhook

```
DELETE /repos/:owner/:repo/hooks/:id
```

### 测试 Webhook

```
POST /repos/:owner/:repo/hooks/:id/tests
```

触发一次测试推送，验证 Webhook 是否可达。

---

## 分支

### 获取分支列表

```
GET /repos/:owner/:repo/branches
```

### 创建分支

```
POST /repos/:owner/:repo/branches
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| branch_name | body | 是 | 新分支名 |
| ref | body | 是 | 基于的 commit SHA 或分支名 |

---

## Commit

### 获取 Commit 列表

```
GET /repos/:owner/:repo/commits
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| sha | query | 否 | 分支名或 SHA |
| path | query | 否 | 文件路径过滤 |
| since | query | 否 | 起始时间（ISO 8601） |
| until | query | 否 | 截止时间 |
| page | query | 否 | 页码 |
| per_page | query | 否 | 每页数量 |

### 获取单个 Commit

```
GET /repos/:owner/:repo/commits/:sha
```

---

## 搜索

### 搜索仓库

```
GET /search/repositories?q=:keyword
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| q | query | 是 | 搜索关键词 |
| sort | query | 否 | `stars`, `forks`, `updated` |
| order | query | 否 | `asc`, `desc` |
| page | query | 否 | 页码 |
| per_page | query | 否 | 每页数量 |

### 搜索代码

```
GET /search/code?q=:keyword
```

### 搜索用户

```
GET /search/users?q=:keyword
```

---

## 组织

### 创建组织仓库

```
POST /orgs/:org/repos
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| org | path | 是 | 组织名 |
| name | body | 是 | 仓库名 |
| description | body | 否 | 仓库描述 |
| visibility | body | 否 | `0` 私有, `1` 公开 |
| auto_init | body | 否 | 是否自动初始化 README |

### 获取组织成员

```
GET /orgs/:org/members
```

---

## Release

### 创建 Release

```
POST /repos/:owner/:repo/releases
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| tag_name | body | 是 | Tag 名 |
| target_commitish | body | 否 | 目标分支/SHA |
| name | body | 否 | Release 名称 |
| body | body | 否 | Release 描述 |
| draft | body | 否 | 是否为草稿 |
| prerelease | body | 否 | 是否为预发布 |

---

## 成员

### 添加仓库成员

```
PUT /repos/:owner/:repo/collaborators/:username
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| username | path | 是 | 用户名 |
| permission | body | 否 | `pull`, `push`, `admin` |

---

## 标签

### 获取项目标签

```
GET /repos/:owner/:repo/labels
```

### 创建项目标签

```
POST /repos/:owner/:repo/labels
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| name | body | 是 | 标签名 |
| color | body | 否 | 颜色（6位十六进制） |

---

## 里程碑

### 获取里程碑列表

```
GET /repos/:owner/:repo/milestones
```

### 创建里程碑

```
POST /repos/:owner/:repo/milestones
```

| 参数 | 位置 | 必填 | 说明 |
|------|------|------|------|
| title | body | 是 | 里程碑标题 |
| description | body | 否 | 描述 |
| due_on | body | 否 | 截止日期 |

---

## 用户

### 获取当前用户信息

```
GET /user
```

### 获取指定用户信息

```
GET /users/:username
```

### 获取用户仓库列表

```
GET /users/:username/repos
```
