# Blog System API 端点文档

> 本文档由 OpenAPI 3.1 规范（`{base_url}/openapi.json`）解析生成。
> 所有端点均相对 `{base_url}`（默认 `http://<host>:<port>`，通过 `BLOG_MANAGER_KIT_BASE_URL` 环境变量配置）。
> **认证方式：无**（公开 API，无需 token/凭据）。

## base_url

```
{base_url}
```

- 优先级：项目知识（`.project-info/` 下 JSON 的 `config.BLOG_MANAGER_KIT_BASE_URL`）> 环境变量 `BLOG_MANAGER_KIT_BASE_URL` > 默认地址。
- 未配置环境变量时使用 skill 内置默认地址。

## 端点总览

共 27 个 API 端点（排除 Web 页面 `GET /`、`GET /article/{id}` 与后台 Cookie 管理端点 `/admin*`），对应 28 个子命令（`delete-article` 软删除 + `hard-delete-article` 硬删除由同一端点拆分）。

## articles（文章）

| Method | Path | 子命令 | 参数 / Body | 说明 |
|--------|------|--------|-------------|------|
| GET | `/api/articles` | `list-articles` | query: page(1), size(10), lid(0), keyword("") | 分页查询文章列表 |
| POST | `/api/articles` | `create-article` | body: ArticleCreate | 发布新文章 |
| GET | `/api/articles/{article_id}` | `get-article` | path: article_id | 查询单篇文章详情（含评论） |
| PUT | `/api/articles/{article_id}` | `update-article` | path: article_id; body: ArticleUpdate | 更新文章 |
| DELETE | `/api/articles/{article_id}` | `delete-article` | path: article_id; query: soft=true | 删除文章（默认软删除，可恢复） |
| DELETE | `/api/articles/{article_id}` | `hard-delete-article` | path: article_id; query: soft=false | 硬删除文章（不可逆，需二次确认） |
| POST | `/api/articles/{article_id}/restore` | `restore-article` | path: article_id | 恢复软删除的文章 |
| GET | `/api/articles/heat/top` | `top-articles` | query: limit(5) | 获取热门文章 Top N |

## labels（标签）

> ⚠️ API 路径拼写为 `/api/lables`（原文拼写错误），子命令使用正确拼写 `labels`，脚本内部请求时用 API 实际路径 `lables`。

| Method | Path | 子命令 | 参数 / Body | 说明 |
|--------|------|--------|-------------|------|
| GET | `/api/lables` | `list-labels` | — | 获取所有标签 |
| POST | `/api/lables` | `create-label` | body: LableCreate | 创建标签 |

## users（用户）

| Method | Path | 子命令 | 参数 / Body | 说明 |
|--------|------|--------|-------------|------|
| GET | `/api/users` | `list-users` | — | 获取用户列表 |
| POST | `/api/users` | `create-user` | body: UserCreate | 创建用户 |

## comments（评论）

| Method | Path | 子命令 | 参数 / Body | 说明 |
|--------|------|--------|-------------|------|
| POST | `/api/comments` | `create-comment` | body: CommentCreate | 发表评论 |
| GET | `/api/comments/{aid}` | `list-comments` | path: aid | 获取文章的评论列表 |
| DELETE | `/api/comments/{comment_id}` | `delete-comment` | path: comment_id | 删除评论（软删除） |

## messages（留言）

| Method | Path | 子命令 | 参数 / Body | 说明 |
|--------|------|--------|-------------|------|
| GET | `/api/messages` | `list-messages` | — | 获取留言列表（含回复） |
| POST | `/api/messages` | `create-message` | body: MessageCreate | 发表留言 |
| POST | `/api/messages/reply` | `reply-message` | body: Message2Create | 回复留言 |
| DELETE | `/api/messages/{message_id}` | `delete-message` | path: message_id | 删除留言（软删除） |

## moods（说说）

| Method | Path | 子命令 | 参数 / Body | 说明 |
|--------|------|--------|-------------|------|
| GET | `/api/moods` | `list-moods` | — | 获取说说列表 |
| POST | `/api/moods` | `create-mood` | body: MoodCreate | 发布说说 |
| DELETE | `/api/moods/{mood_id}` | `delete-mood` | path: mood_id | 删除说说 |

## uploads（文件上传）

| Method | Path | 子命令 | 参数 / Body | 说明 |
|--------|------|--------|-------------|------|
| POST | `/api/upload` | `upload-single` | multipart: file | 上传单个文件 |
| POST | `/api/upload/multiple` | `upload-batch` | multipart: files[] | 批量上传文件 |
| GET | `/api/uploads/list` | `list-uploads` | — | 列出所有已上传文件 |
| DELETE | `/api/uploads/{filename}` | `delete-upload` | path: filename | 删除已上传文件 |

## health（健康检查）

| Method | Path | 子命令 | 参数 / Body | 说明 |
|--------|------|--------|-------------|------|
| GET | `/health` | `health-check` | — | 检查 API 可达性 |

## 请求体 Schema

### ArticleCreate
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| title | string | 是 | — | 标题 |
| content | string | 是 | — | 内容 |
| uid | integer | 否 | 1 | 作者用户 ID |
| lid | integer | 否 | 1 | 标签 ID |
| img | string\|null | 否 | — | 封面图 URL |
| heat | integer | 否 | 0 | 热度 |

### ArticleUpdate
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| title | string\|null | 否 | — | 标题 |
| content | string\|null | 否 | — | 内容 |
| lid | integer\|null | 否 | — | 标签 ID |
| img | string\|null | 否 | — | 封面图 URL |
| heat | integer\|null | 否 | — | 热度 |

### LableCreate
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| lname | string | 是 | — | 标签名 |

### UserCreate
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| uname | string | 是 | — | 用户名 |
| phone | string | 否 | — | 手机号 |
| pwd | string | 否 | — | 密码 |
| email | string | 否 | — | 邮箱 |
| img | string | 否 | img/moren.jpg | 头像 |

### CommentCreate
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| uid | integer | 是 | — | 评论者用户 ID |
| aid | integer | 是 | — | 文章 ID |
| content | string | 是 | — | 评论内容 |

### MessageCreate
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| uid | integer | 是 | — | 留言者用户 ID |
| content | string | 是 | — | 留言内容 |

### Message2Create（回复留言）
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| uid | integer | 是 | — | 回复者用户 ID |
| mid | integer | 是 | — | 被回复的留言 ID |
| content | string | 是 | — | 回复内容 |

### MoodCreate
| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| title | string | 否 | — | 标题 |
| content | string | 是 | — | 内容 |
| src | string | 否 | — | 来源/链接 |

## 响应结构

- 列表类端点：`{"code":200,"data":[...]}`
- 详情/创建类端点：`{"code":200,"data":{...}}`
- 健康检查：`{"status":"ok"}`（或类似）
- 参数校验失败：HTTP 422 `{"detail":[{...}]}`

## 排除端点（Web UI / 后台 Cookie 层，不在本 skill 范围内）

- `GET /` — 博客首页（HTML）
- `GET /article/{article_id}` — 文章详情页（HTML）
- `GET /admin`、`POST /admin/login`、`GET /admin/logout`、`POST /admin/api/delete` — 后台 Cookie 登录/批量删除（Web UI 层）
