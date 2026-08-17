# GitCode API Guidance

> 基于 [GitCode 官方 API 文档](https://docs.gitcode.com/docs/apis/)（OpenAPI 2.0.0 / API v5、v8）整理。

---

## 一、概述

- **正式环境 API 基地址**: `https://api.gitcode.com`
- **API 版本前缀**: 大多数接口为 `/api/v5`；企业（Enterprise）与流水线（Actions）相关接口使用 `/api/v8`。
- **内容类型**: 请求与响应均为 `application/json`（上传类接口为 `multipart/form-data`）。
- **令牌**: 可通过[个人访问令牌](https://gitcode.com/setting/token-classic)或 OAuth2.0 获取（详见下文）。

## 二、认证

大多数接口需要认证（或未认证时只返回公共数据），三种传令牌方式任选其一：

### 1. Authorization 请求头（推荐）

```bash
curl --location 'https://api.gitcode.com/api/v5/user' \
  --header 'Authorization: Bearer {your-token}'
```

### 2. PRIVATE-TOKEN 请求头

```bash
curl --location 'https://api.gitcode.com/api/v5/user' \
  --header 'PRIVATE-TOKEN: {your-token}'
```

### 3. access_token 查询参数

```bash
curl "https://api.gitcode.com/api/v5/users/{username}?access_token={your-token}"
```

> 认证失败时返回 `401`：
> ```json
> { "message": "401 Unauthorized" }
> ```

## 三、状态码

| 状态码 | 说明 |
|--------|------|
| `200 OK` | GET、PUT 或 DELETE 请求成功，资源以 JSON 返回 |
| `201 Created` | POST 请求成功，新资源以 JSON 返回 |
| `202 Accepted` | 请求成功，资源计划进行异步处理 |
| `204 No Content` | 请求成功，响应无内容（如删除） |
| `301 Moved Permanently` | 资源已被移动到 Location 头给出的 URL |
| `304 Not Modified` | 资源自上次请求以来未修改 |
| `400 Bad Request` | 请求缺少必需属性 |
| `401 Unauthorized` | 未认证，需要有效用户令牌 |
| `403 Forbidden` | 请求不被允许（如无权限删除项目） |
| `404 Not Found` | 资源不存在或无访问权限 |
| `405 Method Not Allowed` | 请求方法不支持 |
| `409 Conflict` | 资源冲突已存在（如创建重名项目） |
| `412 Precondition Failed` | 前置条件失败（配合 If-Unmodified-Since 使用） |
| `418 I'm a teapot` | 请求被拒绝，请求疑似不安全 |
| `422 Unprocessable` | 无法处理实体 |
| `429 Too Many Requests` | 超过速率限制（默认 400 次/分，4000 次/小时） |
| `500 Server Error` | 服务器内部错误 |
| `503 Service Unavailable` | 服务器暂时过载 |
| `504 Time Out` | 响应超时 |

## 四、分页与通用参数

- **分页**: 列表接口通用 `page`（页码，默认 1）与 `per_page`（每页条数，默认 20，最大 100）两个查询参数。
- **通用参数**: `access_token`（用户授权码）几乎存在于所有需认证接口；列表类接口常用 `sort` / `direction` 控制排序。
- **公开数据**: 部分只读接口在未认证时可访问，但通常需要 `access_token` 才能看到完整数据。

## 五、接口总览

| 分类 | 接口数 | 说明 |
|------|--------|------|
| [仓库管理](#仓库管理) | 46 | 仓库的创建、文件读写、设置、Fork、统计等 |
| [分支管理](#分支管理) | 8 | 分支的增删改查与保护分支规则 |
| [Issue 管理](#issue-管理) | 28 | Issue 全生命周期、评论、标签、关联 PR 等 |
| [搜索](#搜索) | 3 | 用户、仓库、Issue 搜索 |
| [Pull Request 管理](#pull-request-管理) | 45 | PR 列表、审查、合并、标签、测试与评审人指派 |
| [Commit 管理](#commit-管理) | 12 | 提交列表、详情、diff、评论与代码量统计 |
| [Tag 管理](#tag-管理) | 8 | 标签与保护标签 |
| [标签管理](#标签管理) | 9 | 仓库/企业标签、Issue 标签 |
| [里程碑](#里程碑) | 5 | 仓库里程碑的增删改查 |
| [用户管理](#用户管理) | 22 | 用户资料、仓库、Star/Watch、公钥、Namespace |
| [组织管理](#组织管理) | 22 | 组织信息、成员、讨论、自定义角色 |
| [Webhooks](#webhooks) | 6 | 仓库 WebHook 的增删改查与测试 |
| [项目成员](#项目成员) | 6 | 协作者与成员权限管理 |
| [Release 管理](#release-管理) | 9 | 发行版、附件上传下载 |
| [企业管理](#企业管理) | 18 | 企业成员、里程碑、自定义字段、标签（v8） |
| [看板](#看板) | 7 | 组织看板与看板条目管理 |
| [流水线 / Actions](#流水线-actions) | 22 | Workflow、运行记录、日志、Artifact、Runner（v8） |
| [OAuth 2.0](#oauth-20) | 1 | 授权码/刷新令牌获取 |
| [AI hub 模型服务](#ai-hub-模型服务) | 7 | 文本生成、语音识别、目标检测、视频生成等 |

共 **284** 个接口。

---

## 六、项目实践（devops-bot 视角）

本项目（DevOps Bot）通过 GitCode Webhook 接收事件，再调用 GitCode API 完成后续操作。以下是本项目常用接口与技能模块的对应关系，可作为快速入口：

| 场景 | 常用接口 | 说明 |
|------|----------|------|
| **Webhook 配置** | [Webhooks](#webhooks) | 创建/更新/删除仓库 WebHook，测试推送 |
| **Issue 分诊** | [Issue 管理](#issue-管理) | 查询/更新 Issue、打标签、关联 PR、操作日志 |
| **PR 审查** | [Pull Request 管理](#pull-request-管理) | 获取 PR 列表/详情、评论、审查、合并判断、评审人指派 |
| **自动代码实现** | [仓库管理](#仓库管理) + [Commit 管理](#commit-管理) | 读写仓库文件（contents）、Tree/Blob、提交记录与 diff |
| **成员与权限** | [项目成员](#项目成员) + [组织管理](#组织管理) | 添加协作者、查询权限 |
| **通知与反馈** | [仓库动态/用户动态](#仓库管理) | 获取仓库 events、用户 events |
| **Release 发布** | [Release 管理](#release-管理) | 创建 Release、附件上传下载 |
| **流水线对接** | [流水线 / Actions](#流水线--actions) | Workflow 校验、运行记录、日志（v8） |

> **调用约定提醒**：
> - Webhook 事件中携带的用户信息需要再次调用 `/api/v5/users/{username}` 或 `/api/v5/user` 获取最新资料。
> - 创建/更新文件走 contents 系列接口，需提供 SHA（先 GET 再 PUT），与 Git 仓库操作一致。
> - 企业版能力（自定义字段、Issue 类型/优先级、企业标签、看板）通过 `/api/v8` 提供，调用前需确认目标空间为企业空间。
> - 令牌建议放在 Authorization: Bearer 请求头中传递，避免将 access_token 写入 URL 日志。

---

## 七、接口详细参考

### 仓库管理

> 仓库的创建、文件读写、设置、Fork、统计等

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v5/orgs/{org}/repos` | [创建仓库](#创建仓库) |
| `GET` | `/api/v5/repos/{owner}/{repo}/git/trees/{sha}` | [获取仓库目录Tree](#获取仓库目录tree) |
| `GET` | `/api/v5/repos/{owner}/{repo}/contents/{path}` | [获取仓库具体路径下的内容](#获取仓库具体路径下的内容) |
| `POST` | `/api/v5/repos/{owner}/{repo}/contents/{path}` | [新建文件](#新建文件) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/contents/{path}` | [更新文件](#更新文件) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/contents/{path}` | [删除文件](#删除文件) |
| `GET` | `/api/v5/repos/{owner}/{repo}/file_list` | [获取文件列表](#获取文件列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/git/blobs/{sha}` | [获取文件Blob](#获取文件blob) |
| `GET` | `/api/v5/repos/{owner}/{repo}/languages` | [获取仓库的语言](#获取仓库的语言) |
| `GET` | `/api/v5/repos/{owner}/{repo}/contributors` | [获取仓库贡献者](#获取仓库贡献者) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/module/setting` | [设置项目模块](#设置项目模块) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}` | [更新仓库设置](#更新仓库设置) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}` | [删除一个仓库](#删除一个仓库) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/reviewer` | [修改项目代码审查设置](#修改项目代码审查设置) |
| `PUT` | `/api/v5/org/{org}/repo/{repo}/status` | [仓库归档](#仓库归档) |
| `POST` | `/api/v5/org/{org}/projects/{repo}/transfer` | [转移仓](#转移仓) |
| `GET` | `/api/v5/repos/{owner}/{repo}/transition` | [获取项目的权限模式](#获取项目的权限模式) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/transition` | [更新仓库的权限模式](#更新仓库的权限模式) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/push_config` | [设置项目推送规则](#设置项目推送规则) |
| `GET` | `/api/v5/repos/{owner}/{repo}/push_config` | [获取项目推送规则](#获取项目推送规则) |
| `POST` | `/api/v5/repos/{owner}/{repo}/forks` | [Fork一个仓库](#fork一个仓库) |
| `GET` | `/api/v5/repos/{owner}/{repo}/forks` | [查看仓库的Forks](#查看仓库的forks) |
| `POST` | `/api/v5/repos/{owner}/{repo}/img/upload` | [上传图片](#上传图片) |
| `POST` | `/api/v5/repos/{owner}/{repo}/file/upload` | [上传文件](#上传文件) |
| `GET` | `/api/v5/repos/{owner}/{repo}/subscribers` | [列出 watch 了仓库的用户](#列出-watch-了仓库的用户) |
| `GET` | `/api/v5/repos/{owner}/{repo}/stargazers` | [列出 star 了仓库的用户](#列出-star-了仓库的用户) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/repo_settings` | [更新仓库设置](#更新仓库设置-1) |
| `GET` | `/api/v5/repos/{owner}/{repo}/repo_settings` | [获取仓库设置](#获取仓库设置) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pull_request_settings` | [获取 Pull Request设置](#获取-pull-request设置) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/pull_request_settings` | [更新 Pull Request设置](#更新-pull-request设置) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/members/{username}` | [更新项目成员角色](#更新项目成员角色) |
| `POST` | `/api/v5/repos/{owner}/{repo}/transfer` | [仓库转移](#仓库转移) |
| `GET` | `/api/v5/repos/{owner}/{repo}/customized_roles` | [获取项目自定义角色](#获取项目自定义角色) |
| `GET` | `/api/v5/repos/{owner}/{repo}/download_statistics` | [下载次数统计](#下载次数统计) |
| `GET` | `/api/v5/repos/{owner}/{repo}/raw/{path}` | [获取 raw 文件](#获取-raw-文件) |
| `GET` | `/api/v5/repos/{owner}/{repo}/contributors/statistic` | [获取仓库贡献者统计信息](#获取仓库贡献者统计信息) |
| `GET` | `/api/v5/repos/{owner}/{repo}/events` | [获取仓库动态](#获取仓库动态) |
| `GET` | `/api/v5/repos/{owner}/{repo}/sync_repo` | [查询Fork仓同步状态](#查询fork仓同步状态) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/sync_repo` | [Fork仓同步源仓库](#fork仓同步源仓库) |
| `GET` | `/api/v5/repos/{owner}/{repo}/discuss` | [获取项目讨论列表](#获取项目讨论列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/discuss/{number}` | [获取项目讨论详情](#获取项目讨论详情) |
| `GET` | `/api/v5/repos/{owner}/{repo}/discuss/{number}/comment` | [获取项目讨论评论列表](#获取项目讨论评论列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/discuss/{number}/comment/{comment_id}/reply` | [获取项目讨论评论回复列表](#获取项目讨论评论回复列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/repo_remote_mirror` | [获取仓库的远程镜像配置信息](#获取仓库的远程镜像配置信息) |
| `GET` | `/api/v5/repos/{owner}/{repo}/push_remote_mirrors` | [查询仓库推送远程镜像列表](#查询仓库推送远程镜像列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/license` | [获取仓库许可协议](#获取仓库许可协议) |

---

### 创建仓库

<a id="创建仓库"></a>

`POST /api/v5/orgs/{org}/repos`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 仓库名称 |
| `description` | string |  | 仓库描述 |
| `homepage` | string |  | 主页 |
| `has_issues` | boolean |  | 允许提Issue与否。默认: 允许(true) |
| `has_wiki` | boolean |  | 提供Wiki与否。默认: 提供(true) |
| `can_comment` | boolean |  | 允许用户对仓库进行评论。默认： 允许(true) |
| `public` | integer |  | 仓库开源类型。0(私有), 1(公开) |
| `auto_init` | boolean |  | 值为true时则会用README初始化仓库。默认: 不初始化(false) |
| `gitignore_template` | string |  | Git Ignore模版 |
| `license_template` | string |  | License模版 |
| `path` | string |  | 仓库路径 |
| `default_branch` | string |  | 初始化项目时的默认分支名称。默认: main |
| `import_url` | string |  | 导入项目的git地址，import_url和project_template参数只能传入一个 |
| `project_template` | string |  | 模板项目的path，import_url和project_template参数只能传入一个 |
| `repository_type` | string |  | 仓库类型。code:代码仓，model:模型仓，dataset:数据集仓，space:space，默认code |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `description` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |
| `namespace` | object(13字段) |  |  |
| `empty_repo` | null |  |  |
| `starred` | null |  |  |
| `visibility` | string |  |  |
| `owner` | null |  |  |
| `creator` | null |  |  |
| `forked_from_project` | null |  |  |
| `item_type` | null |  |  |
| `main_repository_language` | null |  |  |
| `homepage` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/orgs/{org}/repos?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","description":"string","homepage":"string","has_issues":true,"has_wiki":true,"can_comment":true,"public":0,"auto_init":true,"gitignore_template":"string","license_template":"string","path":"string","default_branch":"string","import_url":"string","project_template":"string","repository_type":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-orgs-org-repos

---

### 获取仓库目录Tree

<a id="获取仓库目录tree"></a>

`GET /api/v5/repos/{owner}/{repo}/git/trees/{sha}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `sha` | **是** | string | 可以是分支名(如master)、Commit或者目录Tree的SHA |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `recursive` | 否 | integer | 赋值为1递归获取目录 |
| `file_path` | 否 | string | 文件树路径 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tree` | array&lt;object(6字段)&gt; |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/git/trees/{sha}?access_token={your-token}&page={page}&per_page={per_page}&recursive={recursive}&file_path={file_path}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-git-trees-sha

---

### 获取仓库具体路径下的内容

<a id="获取仓库具体路径下的内容"></a>

`GET /api/v5/repos/{owner}/{repo}/contents/{path}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `path` | **是** | string | 文件的路径 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `ref` | 否 | string | 分支、tag或commit。默认: 仓库的默认分支(main) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string |  |  |
| `encoding` | string |  |  |
| `size` | integer |  |  |
| `name` | string |  |  |
| `path` | string |  |  |
| `content` | string |  |  |
| `sha` | string |  |  |
| `url` | string |  |  |
| `html_url` | string |  |  |
| `download_url` | string |  |  |
| `_links` | object(2字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/contents/{path}?access_token={your-token}&ref={ref}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-contents-path

---

### 新建文件

<a id="新建文件"></a>

`POST /api/v5/repos/{owner}/{repo}/contents/{path}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `path` | **是** | string | 文件路径 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 文件内容, 要用 base64 编码 |
| `message` | string | 是 | 提交的 commit 信息 |
| `branch` | string |  | 提交的分支 |
| `author[name]` | string |  | Author的名字，默认为当前用户的名字 |
| `author[email]` | string |  | Author的邮箱，默认为当前用户的邮箱 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `commit` | object(5字段) |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/contents/{path}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"content":"string","message":"string","branch":"string","author[name]":"string","author[email]":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-contents-path

---

### 更新文件

<a id="更新文件"></a>

`PUT /api/v5/repos/{owner}/{repo}/contents/{path}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `path` | **是** | string | 文件的路径 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 文件内容, 要用 base64 编码 |
| `sha` | string | 是 | 文件的 Blob SHA |
| `branch` | string |  | 分支名称。默认为仓库对默认分支 |
| `message` | string | 是 | 提交信息 |
| `author[name]` | string |  | Author的名字，默认为当前用户的名字 |
| `author[email]` | string |  | Author的邮箱，默认为当前用户的邮箱 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `commit` | object(6字段) | 是 |  |
| `content` | object(5字段) | 是 |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/contents/{path}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"content":"string","sha":"string","branch":"string","message":"string","author[name]":"string","author[email]":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-contents-path

---

### 删除文件

<a id="删除文件"></a>

`DELETE /api/v5/repos/{owner}/{repo}/contents/{path}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `path` | **是** | string | 文件的路径 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sha` | string | 是 | 文件的 Blob SHA |
| `branch` | string |  | 分支名称。默认为仓库对默认分支 |
| `message` | string | 是 | 提交信息 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `commit` | object(6字段) |  |  |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/contents/{path}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"sha":"string","branch":"string","message":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-contents-path

---

### 获取文件列表

<a id="获取文件列表"></a>

`GET /api/v5/repos/{owner}/{repo}/file_list`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `ref_name` | 否 | string | ref(分支、tag、commit) |
| `file_name` | 否 | string | 要查找的文件名称 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/file_list?access_token={your-token}&ref_name={ref_name}&file_name={file_name}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-file-list

---

### 获取文件Blob

<a id="获取文件blob"></a>

`GET /api/v5/repos/{owner}/{repo}/git/blobs/{sha}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `sha` | **是** | string | 文件的 Blob SHA，可通过 [获取仓库具体路径下的内容] API 获取 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sha` | string |  |  |
| `size` | integer |  |  |
| `url` | string |  |  |
| `content` | string |  |  |
| `encoding` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/git/blobs/{sha}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-git-blobs-sha

---

### 获取仓库的语言

<a id="获取仓库的语言"></a>

`GET /api/v5/repos/{owner}/{repo}/languages`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `Shell` | number |  |  |
| `Python` | number |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/languages?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-languages

---

### 获取仓库贡献者

<a id="获取仓库贡献者"></a>

`GET /api/v5/repos/{owner}/{repo}/contributors`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `type` | 否 | string | 贡献者类型(committers/authors) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  |  |
| `contributions` | integer |  |  |
| `email` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/contributors?access_token={your-token}&type={type}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-contributors

---

### 设置项目模块

<a id="设置项目模块"></a>

`PUT /api/v5/repos/{owner}/{repo}/module/setting`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `has_wiki` | boolean |  | 是否设置wiki |
| `has_issue` | boolean |  | 是否设置issue |
| `has_security` | boolean |  | 是否设置安全配置 |
| `has_merge_request` | boolean |  | 是否设置merge request |
| `has_fork` | boolean |  | 是否设置fork |
| `has_analysis` | boolean |  | 是否设置分析 |
| `has_discussion` | boolean |  | 是否设置讨论 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `msg` | string |  |  |
| `code` | integer |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/module/setting?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"has_wiki":true,"has_issue":true,"has_security":true,"has_merge_request":true,"has_fork":true,"has_analysis":true,"has_discussion":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-module-setting

---

### 更新仓库设置

<a id="更新仓库设置"></a>

`PATCH /api/v5/repos/{owner}/{repo}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 仓库名称 |
| `description` | string |  | 仓库描述 |
| `homepage` | string |  | 项目地址(eg: https://gitcode.com) |
| `path` | string |  | 更新仓库路径 |
| `private` | boolean |  | 仓库公开或私有。（true/false） |
| `default_branch` | string |  | 默认分支 |
| `lfs_enabled` | boolean |  | 是否启动 lfs |
| `tags` | array&lt;string&gt; |  | 项目标签 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `description` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |
| `namespace` | object(13字段) |  |  |
| `empty_repo` | null |  |  |
| `starred` | null |  |  |
| `visibility` | string |  |  |
| `owner` | null |  |  |
| `creator` | null |  |  |
| `forked_from_project` | null |  |  |
| `item_type` | null |  |  |
| `main_repository_language` | null |  |  |
| `homepage` | string |  |  |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","description":"string","homepage":"string","path":"string","private":true,"default_branch":"string","lfs_enabled":true,"tags":["string"]}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo

---

### 删除一个仓库

<a id="删除一个仓库"></a>

`DELETE /api/v5/repos/{owner}/{repo}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo

---

### 修改项目代码审查设置

<a id="修改项目代码审查设置"></a>

`PUT /api/v5/repos/{owner}/{repo}/reviewer`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `assignees` | string |  | 审查人员username，可多个，半角逗号分隔，如：(username1,username2) |
| `testers` | string |  | 测试人员username，可多个，半角逗号分隔，如：(username1,username2) |
| `assignees_number` | integer |  | 最少审查人数 |
| `testers_number` | integer |  | 最少测试人数 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/reviewer?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"assignees":"string","testers":"string","assignees_number":0,"testers_number":0}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-reviewer

---

### 仓库归档

<a id="仓库归档"></a>

`PUT /api/v5/org/{org}/repo/{repo}/status`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 仓库所属组织 |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `status` | integer | 是 | 仓库状态，0：开始，2：关闭 |
| `password` | string |  | 用户密码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `code` | integer |  |  |
| `msg` | string |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/org/{org}/repo/{repo}/status?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"status":0,"password":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-org-org-repo-repo-status

---

### 转移仓

<a id="转移仓"></a>

`POST /api/v5/org/{org}/projects/{repo}/transfer`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 仓库所属组织 |
| `repo` | **是** | string | 仓库路径 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `transfer_to` | string | 是 | 要转移到的目标组织 |
| `password` | string | 是 | 用户密码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `code` | integer |  |  |
| `msg` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/org/{org}/projects/{repo}/transfer?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"transfer_to":"string","password":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-org-org-projects-repo-transfer

---

### 获取项目的权限模式

<a id="获取项目的权限模式"></a>

`GET /api/v5/repos/{owner}/{repo}/transition`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `memberMgntMode` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/transition?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-transition

---

### 更新仓库的权限模式

<a id="更新仓库的权限模式"></a>

`PUT /api/v5/repos/{owner}/{repo}/transition`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `mode` | integer | 是 | 成员权限管理模式: 1 (继承模式)，2 (是独立模式) 不接受设置混合模式 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `msg` | string |  |  |
| `code` | integer |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/transition?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"mode":0}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-transition

---

### 设置项目推送规则

<a id="设置项目推送规则"></a>

`PUT /api/v5/repos/{owner}/{repo}/push_config`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reject_not_signed_by_gpg` | boolean |  | 只允许带有验证的签名提交 |
| `commit_message_regex` | string |  | 提交信息校验 |
| `max_file_size` | integer |  | 提交文件限制 （单位 MB） |
| `skip_rule_for_owner` | boolean |  | 项目管理员的提交不受上述规则限制 |
| `deny_force_push` | boolean |  | 禁止强推（包括管理员） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reject_not_signed_by_gpg` | integer |  |  |
| `deny_force_push` | integer |  |  |
| `max_file_size` | integer |  |  |
| `skip_rule_for_owner` | integer |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/push_config?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"reject_not_signed_by_gpg":true,"commit_message_regex":"string","max_file_size":0,"skip_rule_for_owner":true,"deny_force_push":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-push-config

---

### 获取项目推送规则

<a id="获取项目推送规则"></a>

`GET /api/v5/repos/{owner}/{repo}/push_config`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reject_not_signed_by_gpg` | integer |  |  |
| `deny_force_push` | integer |  |  |
| `max_file_size` | integer |  |  |
| `skip_rule_for_owner` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/push_config?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-push-config

---

### Fork一个仓库

<a id="fork一个仓库"></a>

`POST /api/v5/repos/{owner}/{repo}/forks`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `organization` | string |  | 组织空间完整地址，不填写默认Fork到用户个人空间地址 |
| `name` | string |  | fork 后仓库名称。默认: 源仓库名称 |
| `path` | string |  | fork 后仓库地址。默认: 源仓库地址 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(10字段) |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/forks?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"organization":"string","name":"string","path":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-forks

---

### 查看仓库的Forks

<a id="查看仓库的forks"></a>

`GET /api/v5/repos/{owner}/{repo}/forks`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `sort` | 否 | string | 排序方式: fork的时间(newest, oldest)，star的人数(stargazers) |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `created_after` | 否 | string | 在此之后创建的 |
| `created_before` | 否 | string | 在此之前创建的 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(5字段) |  |  |
| `description` | string |  |  |
| `status` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `owner` | object(3字段) |  |  |
| `pushed_at` | string |  |  |
| `parent` | object(5字段) |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/forks?access_token={your-token}&sort={sort}&page={page}&per_page={per_page}&created_after={created_after}&created_before={created_before}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-forks

---

### 上传图片

<a id="上传图片"></a>

`POST /api/v5/repos/{owner}/{repo}/img/upload`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string | 是 | 文件内容 base64（去掉Data URL）形式（限制20M） |
| `file_name` | string | 是 | 文件名 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `success` | integer |  |  |
| `path` | string |  |  |
| `full_path` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/img/upload?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"body":"string","file_name":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-img-upload

---

### 上传文件

<a id="上传文件"></a>

`POST /api/v5/repos/{owner}/{repo}/file/upload`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file | 是 | 文件（限制20M） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `success` | integer |  |  |
| `path` | string |  |  |
| `full_path` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/file/upload?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-file-upload

---

### 列出 watch 了仓库的用户

<a id="列出-watch-了仓库的用户"></a>

`GET /api/v5/repos/{owner}/{repo}/subscribers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `watched_after` | 否 | string | 在此之后watch的 |
| `watched_before` | 否 | string | 在此之前watch的 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `avatar_url` | string |  |  |
| `watch_at` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/subscribers?access_token={your-token}&page={page}&per_page={per_page}&watched_after={watched_after}&watched_before={watched_before}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-subscribers

---

### 列出 star 了仓库的用户

<a id="列出-star-了仓库的用户"></a>

`GET /api/v5/repos/{owner}/{repo}/stargazers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `starred_after` | 否 | string | 在此之后star的 |
| `starred_before` | 否 | string | 在此之前star的 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `avatar_url` | string |  |  |
| `starred_at` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/stargazers?access_token={your-token}&page={page}&per_page={per_page}&starred_after={starred_after}&starred_before={starred_before}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-stargazers

---

### 更新仓库设置

<a id="更新仓库设置-1"></a>

`PUT /api/v5/repos/{owner}/{repo}/repo_settings`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `disable_fork` | boolean |  | 禁止派生项目（禁止Fork项目） |
| `forbidden_developer_create_branch` | boolean |  | 禁止开发者创建分支 |
| `forbidden_developer_create_tag` | boolean |  | 禁止开发者创建标签 |
| `forbidden_committer_create_branch` | boolean |  | 禁止提交者创建分支 |
| `forbidden_developer_create_branch_user_ids` | string |  | 禁止开发者创建分支的白名单用户ID |
| `branch_name_regex` | string |  | 分支名称正则表达式 |
| `tag_name_regex` | string |  | 标签名称正则表达式 |
| `generate_pre_merge_ref` | boolean |  | 生成MergeRequest的Pre-Merge引用 |
| `rebase_disable_trigger_webhook` | boolean |  | MR rebase不触发Webhook事件 |
| `open_gpg_verified` | boolean |  | 公开GPG公钥验证 |
| `include_lfs_objects` | boolean |  | 压缩包下载包含LFS对象 |
| `show_branch_creator` | boolean |  | 是否显示分支创建者信息，与页面上「显示分支创建者信息」复选框对应 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `forbidden_developer_create_branch` | boolean | 是 |  |
| `forbidden_developer_create_tag` | boolean | 是 |  |
| `forbidden_committer_create_branch` | boolean | 是 |  |
| `generate_pre_merge_ref` | boolean | 是 |  |
| `forbidden_gitlab_access` | boolean | 是 |  |
| `rebase_disable_trigger_webhook` | boolean | 是 |  |
| `action_pipeline_enabled` | boolean | 是 |  |
| `block_cross_repo_pr_trigger` | boolean | 是 |  |
| `show_branch_creator` | boolean | 是 | 是否显示分支创建者信息，与页面上「显示分支创建者信息」复选框对应 |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/repo_settings?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"disable_fork":true,"forbidden_developer_create_branch":true,"forbidden_developer_create_tag":true,"forbidden_committer_create_branch":true,"forbidden_developer_create_branch_user_ids":"string","branch_name_regex":"string","tag_name_regex":"string","generate_pre_merge_ref":true,"rebase_disable_trigger_webhook":true,"open_gpg_verified":true,"include_lfs_objects":true,"show_branch_creator":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-repo-settings

---

### 获取仓库设置

<a id="获取仓库设置"></a>

`GET /api/v5/repos/{owner}/{repo}/repo_settings`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `forbidden_developer_create_branch` | boolean | 是 |  |
| `forbidden_developer_create_tag` | boolean | 是 |  |
| `forbidden_committer_create_branch` | boolean | 是 |  |
| `generate_pre_merge_ref` | boolean | 是 |  |
| `forbidden_gitlab_access` | boolean | 是 |  |
| `rebase_disable_trigger_webhook` | boolean | 是 |  |
| `action_pipeline_enabled` | boolean | 是 |  |
| `block_cross_repo_pr_trigger` | boolean | 是 |  |
| `show_branch_creator` | boolean | 是 | 是否显示分支创建者信息，与页面上「显示分支创建者信息」复选框对应 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/repo_settings?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-repo-settings

---

### 获取 Pull Request设置

<a id="获取-pull-request设置"></a>

`GET /api/v5/repos/{owner}/{repo}/pull_request_settings`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `merge_request_setting` | object(24字段) | 是 |  |
| `only_allow_merge_if_all_discussions_are_resolved` | boolean | 是 |  |
| `only_allow_merge_if_pipeline_succeeds` | boolean | 是 |  |
| `merge_method` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pull_request_settings?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pull-request-settings

---

### 更新 Pull Request设置

<a id="更新-pull-request设置"></a>

`PUT /api/v5/repos/{owner}/{repo}/pull_request_settings`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `approval_required_reviewers_enable` | boolean |  | 是否启用审批必需的评审者功能 |
| `approval_required_reviewers` | integer |  | 需要的审批者数量(最小评审人数【选择的数字：1~5, 如果取消评审人功能传入0】) |
| `only_allow_merge_if_all_discussions_are_resolved` | boolean |  | 评审问题全部解决才能合入 |
| `only_allow_merge_if_pipeline_succeeds` | boolean |  | 是否仅在流水线成功后才允许合并 |
| `disable_merge_by_self` | boolean |  | 禁止合入自己创建的合并请求 |
| `can_force_merge` | boolean |  | 允许管理员强制合入 |
| `add_notes_after_merged` | boolean |  | 允许合并请求合并后继续做代码检视和评论 |
| `mark_auto_merged_mr_as_closed` | boolean |  | 是否将自动合并的MR状态标记为关闭状态 |
| `can_reopen` | boolean |  | 是否可以重新打开一个已经关闭的合并请求 |
| `delete_source_branch_when_merged` | boolean |  | 合并时是否删除源分支，默认删除原分支 |
| `disable_squash_merge` | boolean |  | 禁止 Squash 合并 |
| `auto_squash_merge` | boolean |  | 新建合并请求，默认开启 Squash 合并 |
| `merge_method` | string |  | 合并模式三选一(通过 merge commit 合并：merge；通过 merge commit 合并 （记录半线性历史）：rebase_merge；fast - forward 合并：ff) |
| `squash_merge_with_no_merge_commit` | boolean |  | Squash 合并不产生 Merge 节点 |
| `merged_commit_author` | string |  | 使用 MR (合入/创建) 者生成 Merge Commit（使用 PR 合入者生成 Merge Commit：传 merged_by; 使用 PR 创建者生成 Merge Commit：传 created_by） |
| `approval_required_approvers` | integer |  | 需要审批的批准者数量 |
| `approval_approver_ids` | string |  | 项目审查人, user_id 以逗号分隔 |
| `approval_tester_ids` | string |  | 项目测试人，user_id以逗号分隔 |
| `approval_required_testers` | integer |  | 测试最小通过人数 |
| `is_check_cla` | boolean |  | 是否校验CLA |
| `is_allow_lite_merge_request` | boolean |  | 是否启用轻量级 Pull Request |
| `lite_merge_request_prefix_title` | string |  | 轻量级 pr 的标题前缀 |
| `close_issue_when_mr_merged` | boolean |  | 创建 Pull Request时,默认选中 “合并后关闭已关联的 Issue”；当禁用 “合并后关闭已关联的Issue”选项时该参数无效 |
| `forbidden_pr_related_issue_closed` | boolean |  | 禁用 “合并后关闭已关联的Issue”选项 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `merge_request_setting` | object(23字段) |  |  |
| `only_allow_merge_if_all_discussions_are_resolved` | integer |  |  |
| `only_allow_merge_if_pipeline_succeeds` | integer |  |  |
| `merge_method` | string |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pull_request_settings?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"approval_required_reviewers_enable":true,"approval_required_reviewers":0,"only_allow_merge_if_all_discussions_are_resolved":true,"only_allow_merge_if_pipeline_succeeds":true,"disable_merge_by_self":true,"can_force_merge":true,"add_notes_after_merged":true,"mark_auto_merged_mr_as_closed":true,"can_reopen":true,"delete_source_branch_when_merged":true,"disable_squash_merge":true,"auto_squash_merge":true,"merge_method":"string","squash_merge_with_no_merge_commit":true,"merged_commit_author":"string","approval_required_approvers":0,"approval_approver_ids":"string","approval_tester_ids":"string","approval_required_testers":0,"is_check_cla":true,"is_allow_lite_merge_request":true,"lite_merge_request_prefix_title":"string","close_issue_when_mr_merged":true,"forbidden_pr_related_issue_closed":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-pull-request-settings

---

### 更新项目成员角色

<a id="更新项目成员角色"></a>

`PUT /api/v5/repos/{owner}/{repo}/members/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `username` | **是** | string | 项目成员用户名 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `permission` | string |  | 成员权限: 参与者(pull)，开发者(push)，维护者(admin)。默认: push, customized(自定义角色) |
| `role_id` | string |  | 角色ID, 如果permission传入了customized则需要传入角色id |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `followers_url` | string |  |  |
| `html_url` | string |  |  |
| `id` | string |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `type` | string |  |  |
| `url` | string |  |  |
| `permissions` | object(4字段) |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/members/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"permission":"string","role_id":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-members-username

---

### 仓库转移

<a id="仓库转移"></a>

`POST /api/v5/repos/{owner}/{repo}/transfer`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `new_owner` | string | 是 | 要转移到的目标组织 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `new_owner` | string |  |  |
| `new_name` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/transfer?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"new_owner":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-transfer

---

### 获取项目自定义角色

<a id="获取项目自定义角色"></a>

`GET /api/v5/repos/{owner}/{repo}/customized_roles`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `role_id` | string |  |  |
| `access_level` | integer |  |  |
| `role_name` | string |  |  |
| `role_chinese_name` | string |  |  |
| `role_description` | string |  |  |
| `role_type` | string |  |  |
| `member_count` | integer |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/customized_roles?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-customized-roles

---

### 下载次数统计

<a id="下载次数统计"></a>

`GET /api/v5/repos/{owner}/{repo}/download_statistics`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `start_date` | 否 | string | 统计起始日期包含当前日期（eg:2024-01-06) |
| `end_date` | 否 | string | 统计截止日期包含当前日期 (eg: 2024-12-06) |
| `direction` | 否 | string | 排序方式: 升序(asc)，降序(desc)。默认: desc |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `download_statistics_detail` | array&lt;object(4字段)&gt; |  |  |
| `download_statistics_total` | integer |  |  |
| `download_statistics_history_total` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/download_statistics?access_token={your-token}&start_date={start_date}&end_date={end_date}&direction={direction}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-download-statistics

---

### 获取 raw 文件

<a id="获取-raw-文件"></a>

`GET /api/v5/repos/{owner}/{repo}/raw/{path}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `path` | **是** | string | 文件的路径，特殊字符需转码 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `ref` | 否 | string | 分支、tag 或 commit(默认: 仓库的默认分支) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/raw/{path}?access_token={your-token}&ref={ref}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-raw-path

---

### 获取仓库贡献者统计信息

<a id="获取仓库贡献者统计信息"></a>

`GET /api/v5/repos/{owner}/{repo}/contributors/statistic`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `author` | 否 | string | 过滤贡献者的用户名。支持指定用户名以获取该用户的贡献数据。默认情况下，返回所有用户的贡献数据。 |
| `current_user` | 否 | boolean | 是否只返回当前用户的数据。`true` 表示只返回当前用户的贡献数据，`false` 或者省略则返回所有用户的贡献数据，当为`true`时，优先级高于`author`参数。 |
| `since` | 否 | string | 起始日期，格式为 `YYYY-MM-DD`或者`YYYY-MM-DD HH:mm:ss`。用来限定返回的贡献数据的起始时间。 |
| `until` | 否 | string | 结束日期，格式为 `YYYY-MM-DD`或者`YYYY-MM-DD HH:mm:ss`。用来限定返回的贡献数据的结束时间。 |
| `ref_name` | 否 | string | 指定要获取贡献数据的 ref_name(分支名称、commit id、tag 名称)，不传或者为空时使用`默认分支`。 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  |  |
| `email` | string |  |  |
| `overview` | object(4字段) |  |  |
| `contributions` | array&lt;object(5字段)&gt; |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/contributors/statistic?access_token={your-token}&author={author}&current_user={current_user}&since={since}&until={until}&ref_name={ref_name}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-contributors-statistic

---

### 获取仓库动态

<a id="获取仓库动态"></a>

`GET /api/v5/repos/{owner}/{repo}/events`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string |  |
| `filter` | 否 | string | 筛选条件，传入all（所有的）, push（push事件）, merged（merged事件）, issue（issue事件）, comments（评论事件）, team（团队事件）, project（项目事件） |
| `author` | 否 | string | 事件触发人，传入用户名 |
| `before` | 否 | string | 起始日期，格式为 `YYYY-MM-DD`。用来限定返回的事件的起始时间。 |
| `after` | 否 | string | 结束日期，格式为 `YYYY-MM-DD`。用来限定返回的事件的结束时间。 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `events` | array&lt;object(9字段)&gt; |  |  |
| `has_next_page` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/events?access_token={your-token}&filter={filter}&author={author}&before={before}&after={after}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-events-access-token-your-token

---

### 查询Fork仓同步状态

<a id="查询fork仓同步状态"></a>

`GET /api/v5/repos/{owner}/{repo}/sync_repo`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |
| `branch` | 否 | string | 分支名称 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `repo_sync_result` | boolean | 是 |  |
| `repo_sync_message` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/sync_repo?access_token={your-token}&branch={branch}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-sync-repo

---

### Fork仓同步源仓库

<a id="fork仓同步源仓库"></a>

`PUT /api/v5/repos/{owner}/{repo}/sync_repo`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `branch` | string |  | 拉取更新的分支名称，不传更新所有分支 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `repo_sync_result` | boolean | 是 |  |
| `repo_sync_message` | string | 是 |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/sync_repo?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"branch":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-sync-repo

---

### 获取项目讨论列表

<a id="获取项目讨论列表"></a>

`GET /api/v5/repos/{owner}/{repo}/discuss`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |
| `sort` | 否 | string | 可选。排序字段 created（创建时间）, comment_size（评论数量），默认创建时间 |
| `direction` | 否 | string | 可选。升序/降序(asc/desc)，默认降序 |
| `search` | 否 | string | 根据标题和描述搜索 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `number` | integer | 是 |  |
| `title` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `author` | object(5字段) | 是 |  |
| `is_lock` | integer | 是 | 是否锁定 |
| `is_pin` | integer | 是 | 是否置顶 |
| `is_category_pin` | integer | 是 | 是否类型下置顶 |
| `is_closed` | integer | 是 | 是否关闭 |
| `is_answered` | integer | 是 | 是否采纳答案 |
| `comment_total` | integer | 是 | 评论数量 |
| `category` | object(5字段) | 是 |  |
| `namespace` | object(3字段) | 是 |  |
| `labels` | array&lt;object(5字段)&gt; | 是 | 标签 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/discuss?access_token={your-token}&page={page}&per_page={per_page}&sort={sort}&direction={direction}&search={search}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-discuss

---

### 获取项目讨论详情

<a id="获取项目讨论详情"></a>

`GET /api/v5/repos/{owner}/{repo}/discuss/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 讨论的编号 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `number` | integer | 是 |  |
| `title` | string | 是 |  |
| `md_content` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `author` | object(5字段) | 是 |  |
| `is_lock` | integer | 是 | 是否锁定 |
| `is_pin` | integer | 是 | 是否置顶 |
| `is_category_pin` | integer | 是 | 是否类型下置顶 |
| `is_closed` | integer | 是 | 是否关闭 |
| `is_answered` | integer | 是 | 是否采纳答案 |
| `comment_total` | integer | 是 | 评论总数 |
| `category` | object(5字段) | 是 |  |
| `vote` | object(12字段) | 是 | 投票类型返回，投票信息 |
| `vote_options` | array&lt;object(9字段)&gt; | 是 | 投票类型返回 选项信息 |
| `labels` | array&lt;object(5字段)&gt; |  | 标签 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/discuss/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-discuss-number

---

### 获取项目讨论评论列表

<a id="获取项目讨论评论列表"></a>

`GET /api/v5/repos/{owner}/{repo}/discuss/{number}/comment`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |
| `order` | 否 | string | 排序，按创建时间升序（time_asc）；按创建时间降序（time_desc），按热度值降序（hot_desc） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string |  |  |
| `created_at` | string |  |  |
| `author` | object(5字段) |  |  |
| `content` | string |  |  |
| `md_content` | string |  |  |
| `is_hide` | integer |  |  |
| `reply_total` | integer |  |  |
| `like_total` | integer |  |  |
| `is_like` | boolean |  |  |
| `is_deleted` | integer |  |  |
| `is_remark` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/discuss/{number}/comment?access_token={your-token}&page={page}&per_page={per_page}&order={order}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-discuss-number-comment

---

### 获取项目讨论评论回复列表

<a id="获取项目讨论评论回复列表"></a>

`GET /api/v5/repos/{owner}/{repo}/discuss/{number}/comment/{comment_id}/reply`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 讨论的编号 |
| `comment_id` | **是** | string | 评论id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `author` | object(5字段) | 是 |  |
| `content` | string | 是 |  |
| `md_content` | string | 是 |  |
| `is_hide` | integer | 是 |  |
| `reply_total` | integer | 是 |  |
| `like_total` | integer | 是 |  |
| `is_like` | boolean | 是 |  |
| `is_deleted` | integer | 是 |  |
| `is_remark` | integer | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/discuss/{number}/comment/{comment_id}/reply?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-discuss-number-comment-comment-id-reply

---

### 获取仓库的远程镜像配置信息

<a id="获取仓库的远程镜像配置信息"></a>

`GET /api/v5/repos/{owner}/{repo}/repo_remote_mirror`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `project_id` | integer | 是 |  |
| `update_status` | string | 是 |  |
| `last_update_at` | string | 是 |  |
| `url` | string | 是 |  |
| `last_successful_update_at` | string | 是 |  |
| `number_of_failures` | integer | 是 |  |
| `mirroring_enabled` | boolean | 是 |  |
| `is_private` | boolean | 是 |  |
| `last_error` | string | 是 |  |
| `force` | boolean | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/repo_remote_mirror?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-repo-remote-mirror

---

### 查询仓库推送远程镜像列表

<a id="查询仓库推送远程镜像列表"></a>

`GET /api/v5/repos/{owner}/{repo}/push_remote_mirrors`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `project_id` | integer |  |  |
| `update_status` | string |  |  |
| `last_update_at` | string |  |  |
| `url` | string |  |  |
| `last_successful_update_at` | string |  |  |
| `number_of_failures` | integer |  |  |
| `is_private` | boolean |  |  |
| `message` | string |  |  |
| `force` | boolean |  |  |
| `created_at` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/push_remote_mirrors?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-push-remote-mirrors

---

### 获取仓库许可协议

<a id="获取仓库许可协议"></a>

`GET /api/v5/repos/{owner}/{repo}/license`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 |  |
| `path` | string | 是 |  |
| `size` | integer | 是 |  |
| `url` | string | 是 |  |
| `html_url` | string | 是 |  |
| `content` | string | 是 |  |
| `encoding` | string | 是 |  |
| `license` | object(1字段) | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/license?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-license

---

### 分支管理

> 分支的增删改查与保护分支规则

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/repos/{owner}/{repo}/branches` | [获取项目所有分支](#获取项目所有分支) |
| `POST` | `/api/v5/repos/{owner}/{repo}/branches` | [创建分支](#创建分支) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/branches/{name}` | [删除分支](#删除分支) |
| `GET` | `/api/v5/repos/{owner}/{repo}/branches/{branch}` | [获取单个分支](#获取单个分支) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/branches/setting/new` | [新建保护分支规则](#新建保护分支规则) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/branches/{wildcard}/setting` | [删除保护分支规则](#删除保护分支规则) |
| `GET` | `/api/v5/repos/{owner}/{repo}/protect_branches` | [获取保护分支规则列表](#获取保护分支规则列表) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/branches/{wildcard}/setting` | [更新保护分支规则](#更新保护分支规则) |

---

### 获取项目所有分支

<a id="获取项目所有分支"></a>

`GET /api/v5/repos/{owner}/{repo}/branches`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `sort` | 否 | string | 排序字段 name/updated |
| `direction` | 否 | string | 排序方向 asc/desc |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 |  |
| `description` | string | 是 |  |
| `commit` | object(3字段) | 是 |  |
| `default_branch` | boolean | 是 | 是否默认分支 |
| `created_at` | string | 是 | 创建时间 |
| `creator` | object(4字段) | 是 | 创建人 |
| `protected` | boolean | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches?access_token={your-token}&sort={sort}&direction={direction}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-branches

---

### 创建分支

<a id="创建分支"></a>

`POST /api/v5/repos/{owner}/{repo}/branches`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `refs` | string | 是 | 起点名称，默认：master |
| `branch_name` | string | 是 | 新创建的分支名称 |
| `description` | string |  | 分支描述信息 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 |  |
| `description` | string | 是 |  |
| `commit` | object(3字段) | 是 |  |
| `default_branch` | boolean | 是 |  |
| `protected` | boolean | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"refs":"string","branch_name":"string","description":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-branches

---

### 删除分支

<a id="删除分支"></a>

`DELETE /api/v5/repos/{owner}/{repo}/branches/{name}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `name` | **是** | string | 分支名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches/{name}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-branches-name

---

### 获取单个分支

<a id="获取单个分支"></a>

`GET /api/v5/repos/{owner}/{repo}/branches/{branch}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `branch` | **是** | string | 分支名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 |  |
| `description` | string | 是 |  |
| `commit` | object(16字段) | 是 |  |
| `merged` | boolean | 是 |  |
| `protected` | boolean | 是 |  |
| `developers_can_push` | boolean | 是 |  |
| `developers_can_merge` | boolean | 是 |  |
| `can_push` | boolean | 是 |  |
| `default` | boolean | 是 |  |
| `created_at` | string | 是 | 创建时间 |
| `creator` | object(4字段) | 是 | 创建人 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches/{branch}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-branches-branch

---

### 新建保护分支规则

<a id="新建保护分支规则"></a>

`PUT /api/v5/repos/{owner}/{repo}/branches/setting/new`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `wildcard` | string | 是 | 分支/通配符 |
| `pusher` | string | 是 | 可推送代码成员。develop：仓库管理员和开发者；admin：仓库管理员；maintainer：维护者；多个角色使用英文分号相隔。指定用户传入用户名，多个用户使用英文分号隔开。空字符串：不允许任何人推送 |
| `merger` | string | 是 | 可合并 Pull Request成员。develop：仓库管理员和开发者；admin：仓库管理员；maintainer：维护者；多个角色使用英文分号相隔。指定用户传入用户名，多个用户使用英文分号隔开。空字符串：不允许任何人合并 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches/setting/new?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"wildcard":"string","pusher":"string","merger":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-branches-setting-new

---

### 删除保护分支规则

<a id="删除保护分支规则"></a>

`DELETE /api/v5/repos/{owner}/{repo}/branches/{wildcard}/setting`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `wildcard` | **是** | string | 分支名 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches/{wildcard}/setting?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-branches-wildcard-setting

---

### 获取保护分支规则列表

<a id="获取保护分支规则列表"></a>

`GET /api/v5/repos/{owner}/{repo}/protect_branches`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  |  |
| `updated_at` | string |  |  |
| `push_users` | array&lt;object&gt; |  |  |
| `merge_users` | array&lt;object&gt; |  |  |
| `merged` | integer |  |  |
| `developers_can_push` | integer |  |  |
| `developers_can_merge` | integer |  |  |
| `committer_can_push` | integer |  |  |
| `committer_can_merge` | integer |  |  |
| `master_can_push` | integer |  |  |
| `master_can_merge` | integer |  |  |
| `maintainer_can_push` | integer |  |  |
| `maintainer_can_merge` | integer |  |  |
| `owner_can_push` | integer |  |  |
| `owner_can_merge` | integer |  |  |
| `no_one_can_push` | integer |  |  |
| `no_one_can_merge` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/protect_branches?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-protect-branches

---

### 更新保护分支规则

<a id="更新保护分支规则"></a>

`PUT /api/v5/repos/{owner}/{repo}/branches/{wildcard}/setting`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `wildcard` | **是** | string | 保护分支名 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `pusher` | string | 是 | 可推送代码成员。develop：仓库管理员和开发者；admin：仓库管理员；maintainer：维护者；多个角色使用英文分号相隔。指定用户传入用户名，多个用户使用英文分号隔开。空字符串：不允许任何人推送 |
| `merger` | string | 是 | 可合并 Pull Request成员。develop：仓库管理员和开发者；admin：仓库管理员；maintainer：维护者；多个角色使用英文分号相隔。指定用户传入用户名，多个用户使用英文分号隔开。空字符串：不允许任何人合并 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/branches/{wildcard}/setting?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"pusher":"string","merger":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-branches-wildcard-setting

---

### Issue 管理

> Issue 全生命周期、评论、标签、关联 PR 等

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v5/repos/{owner}/issues` | [创建Issue](#创建issue) |
| `PATCH` | `/api/v5/repos/{owner}/issues/{number}` | [更新Issue](#更新issue) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/{number}` | [获取仓库的某个Issue](#获取仓库的某个issue) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues` | [获取仓库所有 issues](#获取仓库所有-issues) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/{number}/comments` | [获取仓库某个Issue所有的评论](#获取仓库某个issue所有的评论) |
| `POST` | `/api/v5/repos/{owner}/{repo}/issues/{number}/comments` | [创建Issue评论](#创建issue评论) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/comments` | [获取仓库所有 Issue 评论](#获取仓库所有-issue-评论) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/{number}/pull_requests` | [获取 issue 关联的 pull requests](#获取-issue-关联的-pull-requests) |
| `GET` | `/api/v5/enterprises/{enterprise}/issues/{issue_id}/labels` | [获取企业某个Issue所有标签](#获取企业某个issue所有标签) |
| `POST` | `/api/v5/repos/{owner}/{repo}/issues/{number}/labels` | [创建Issue标签](#创建issue标签) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/issues/{number}/labels/{name}` | [删除Issue标签](#删除issue标签) |
| `GET` | `/api/v5/repos/{owner}/issues/{number}/operate_logs` | [获取某个issue下的操作日志](#获取某个issue下的操作日志) |
| `GET` | `/api/v5/enterprises/{enterprise}/issues` | [获取某个企业的所有Issues](#获取某个企业的所有issues) |
| `GET` | `/api/v5/user/issues` | [获取授权用户的所有Issues](#获取授权用户的所有issues) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/issues/comments/{id}` | [更新Issue某条评论](#更新issue某条评论) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/issues/comments/{id}` | [删除Issue某条评论](#删除issue某条评论) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/comments/{id}` | [获取仓库Issue某条评论](#获取仓库issue某条评论) |
| `GET` | `/api/v5/orgs/{org}/issues` | [获取当前用户某个组织的Issues](#获取当前用户某个组织的issues) |
| `GET` | `/api/v5/enterprises/{enterprise}/issues/{number}/comments` | [获取企业某个Issue所有评论](#获取企业某个issue所有评论) |
| `GET` | `/api/v5/enterprises/{enterprise}/issues/{number}` | [获取企业的某个Issue](#获取企业的某个issue) |
| `GET` | `/api/v5/enterprises/{enterprise}/issue/statuses` | [获取企业issue状态](#获取企业issue状态) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/{number}/related_branches` | [获取issue关联的分支列表](#获取issue关联的分支列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/{number}/user_reactions` | [获取issue的表态列表](#获取issue的表态列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/comment/{comment_id}/user_reactions` | [获取issue评论的表态列表](#获取issue评论的表态列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/{number}/modify_history` | [获取issue的修改历史](#获取issue的修改历史) |
| `GET` | `/api/v5/repos/{owner}/{repo}/issues/comment/{comment_id}/modify_history` | [获取issue评论的修改历史](#获取issue评论的修改历史) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/issues/{number}/related_branches` | [设置Issue关联的分支](#设置issue关联的分支) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/issues/{number}/kanban_values` | [更新issue关联的看板字段](#更新issue关联的看板字段) |

---

### 创建Issue

<a id="创建issue"></a>

`POST /api/v5/repos/{owner}/issues`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `repo` | string | 是 | 仓库路径 |
| `title` | string | 是 | Issue标题 |
| `body` | string | 是 | Issue描述 |
| `assignee` | string |  | Issue负责人的username，多个用英文逗号隔开 |
| `milestone` | integer |  | 里程碑序号 |
| `labels` | string |  | 用逗号分开的标签，名称要求长度在 2-20 之间且非特殊字符。如: bug,performance |
| `security_hole` | string |  | 是否是私有issue(默认为false) |
| `template_path` | string |  | issue模板路径，项目模板支持.gitcode，.github，.gitee目录下的文件，组织模板仅支持.gitcode项目下的.gitcode目录下的文件 |
| `issue_type` | string |  | issue类型（企业版支持），设置值需与企业issue高级功能中的设置保持一致 |
| `issue_severity` | string |  | issue优先级（企业版支持），设置值需与企业issue高级功能中的设置保持一致 |
| `custom_fields` | array&lt;object(2字段)&gt; |  | 自定义字段信息（企业版支持） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `html_url` | string |  |  |
| `number` | integer |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `user` | object(18字段) |  |  |
| `assignee` | null |  |  |
| `repository` | object(7字段) |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `finished_at` | null |  |  |
| `labels` | array&lt;object(5字段)&gt; |  |  |
| `stage` | string |  |  |
| `severity` | string |  |  |
| `custom_fields` | array&lt;object(4字段)&gt; |  | 自定义字段信息 |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/issues?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"repo":"string","title":"string","body":"string","assignee":"string","milestone":0,"labels":"string","security_hole":"string","template_path":"string","issue_type":"string","issue_severity":"string","custom_fields":[{"field_name":"string","field_values":["string"]}]}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-issues

---

### 更新Issue

<a id="更新issue"></a>

`PATCH /api/v5/repos/{owner}/issues/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `number` | **是** | string | 第几个 issue，即本仓库 issue 的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `repo` | string | 是 | 仓库路径 |
| `title` | string | 是 | Issue标题 |
| `body` | string |  | Issue描述 |
| `state` | string |  | Issue 状态，reopen（开启的）、close（关闭的） |
| `assignee` | string |  | Issue负责人的username，多个用英文逗号隔开 |
| `milestone` | integer |  | 里程碑序号 |
| `labels` | string |  | 用逗号分开的标签，名称要求长度在 2-20 之间且非特殊字符。如: bug,performance |
| `security_hole` | string |  | 是否是私有issue(默认为false) |
| `status` | string |  | issue状态（企业版支持），与类型不能同时设置 |
| `issue_severity` | string |  | issue优先级（企业版支持），与类型不能同时设置 |
| `custom_fields` | array&lt;object(2字段)&gt; |  | 自定义字段信息（企业版支持） |
| `issue_type` | string |  | issue类型（企业版支持），与状态和优先级不能同时设置 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `html_url` | string |  |  |
| `number` | integer |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `user` | object(18字段) |  |  |
| `assignee` | object(18字段) |  |  |
| `repository` | object(7字段) |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `finished_at` | string |  |  |
| `labels` | array&lt;object(3字段)&gt; |  |  |
| `stage` | string |  |  |
| `severity` | string |  |  |
| `custom_fields` | array&lt;object(4字段)&gt; |  | 自定义字段信息（企业版支持） |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/issues/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"repo":"string","title":"string","body":"string","state":"string","assignee":"string","milestone":0,"labels":"string","security_hole":"string","status":"string","issue_severity":"string","custom_fields":[{"field_name":"string","field_values":["string"]}],"issue_type":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-issues-number

---

### 获取仓库的某个Issue

<a id="获取仓库的某个issue"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Issue 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `html_url` | string | 是 |  |
| `number` | string | 是 |  |
| `state` | string | 是 |  |
| `title` | string | 是 |  |
| `body` | string | 是 |  |
| `user` | object(5字段) | 是 |  |
| `repository` | object(9字段) | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `finished_at` | string | 是 |  |
| `labels` | array&lt;object(5字段)&gt; | 是 |  |
| `issue_state` | string | 是 |  |
| `comments` | integer | 是 |  |
| `priority` | integer | 是 |  |
| `issue_type` | string | 是 |  |
| `issue_state_detail` | object(3字段) |  |  |
| `issue_type_detail` | object(3字段) |  |  |
| `issue_priority_detail` | object(2字段) |  | issue优先级 |
| `milestone` | object(9字段) | 是 |  |
| `custom_fields` | array&lt;object(4字段)&gt; |  | 自定义字段信息 |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-number

---

### 获取仓库所有 issues

<a id="获取仓库所有-issues"></a>

`GET /api/v5/repos/{owner}/{repo}/issues`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `state` | 否 | string | Issue的状态: open（开启的）, closed（关闭的）， all (所有)。 默认: all |
| `labels` | 否 | string | 用逗号分开的标签。如: bug,performance |
| `sort` | 否 | string | 排序依据: 创建时间(created)，更新时间(updated)。默认: created |
| `direction` | 否 | string | 排序方式: 升序(asc)，降序(desc)。默认: desc |
| `since` | 否 | string | 起始的更新时间，要求时间格式为 2024-11-10T08:10:30.000+08:00（注意+号要url编码为%2B） |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `created_at` | 否 | string | 任务创建时间，例如：2024-11-20T13:00:21+08:00 |
| `milestone` | 否 | string | 根据里程碑标题。none为没里程碑的 |
| `assignee` | 否 | string | Issue指派人ID |
| `creator` | 否 | string | 创建Issues的用户username |
| `created_after` | 否 | string | 返回在指定时间之后创建的问题，例如：2024-11-20T13:00:21+08:00 |
| `created_before` | 否 | string | 返回在指定时间之前创建的问题，例如：2024-11-20T13:00:21+08:00 |
| `updated_after` | 否 | string | 返回在指定时间之后更新的问题，例如：2024-11-20T13:00:21+08:00 |
| `updated_before` | 否 | string | 返回在指定时间之前更新的问题，例如：2024-11-20T13:00:21+08:00 |
| `search` | 否 | string | 通过关键字搜索issue标题或者内容 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `html_url` | string | 是 |  |
| `number` | string | 是 |  |
| `state` | string | 是 |  |
| `title` | string | 是 |  |
| `body` | string | 是 |  |
| `user` | object(5字段) | 是 |  |
| `repository` | object(9字段) | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `finished_at` | string | 是 |  |
| `labels` | array&lt;object(5字段)&gt; | 是 |  |
| `issue_state` | string | 是 |  |
| `comments` | integer | 是 |  |
| `priority` | integer | 是 |  |
| `issue_type` | string | 是 |  |
| `issue_state_detail` | object(3字段) | 是 |  |
| `issue_type_detail` | object(3字段) | 是 |  |
| `issue_priority_detail` | object(2字段) |  | issue优先级 |
| `milestone` | object(9字段) | 是 |  |
| `assignee` | object(5字段) | 是 |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues?access_token={your-token}&state={state}&labels={labels}&sort={sort}&direction={direction}&since={since}&page={page}&per_page={per_page}&created_at={created_at}&milestone={milestone}&assignee={assignee}&creator={creator}&created_after={created_after}&created_before={created_before}&updated_after={updated_after}&updated_before={updated_before}&search={search}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues

---

### 获取仓库某个Issue所有的评论

<a id="获取仓库某个issue所有的评论"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/{number}/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Issue 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `order` | 否 | string | 排序顺序: asc(default),desc |
| `since` | 否 | string | 起始的更新时间，要求时间格式为 2024-11-10T08:10:30.000+08:00（注意+号要url编码为%2B） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `body` | string |  |  |
| `user` | object(18字段) |  |  |
| `target` | object(1字段) |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/comments?access_token={your-token}&page={page}&per_page={per_page}&order={order}&since={since}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-number-comments

---

### 创建Issue评论

<a id="创建issue评论"></a>

`POST /api/v5/repos/{owner}/{repo}/issues/{number}/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | issue编号 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string | 是 | The contents of the comment. |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `body` | string |  |  |
| `user` | object(18字段) |  |  |
| `target` | object(1字段) |  |  |
| `created_at` | null |  |  |
| `updated_at` | null |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/comments?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"body":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-issues-number-comments

---

### 获取仓库所有 Issue 评论

<a id="获取仓库所有-issue-评论"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `sort` | 否 | string | 按照字段排序，支持created或者updated。默认: created |
| `direction` | 否 | string | 排序方向，支持asc/desc |
| `since` | 否 | string | 只会返回在此时间或之后更新的评论。例如：2024-11-20T13:00:21+08:00（注意+号要url编码为%2B） |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `body` | string |  |  |
| `user` | object(18字段) |  |  |
| `target` | object(1字段) |  |  |
| `created_at` | string |  |  |
| `updated_at` | null |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/comments?access_token={your-token}&sort={sort}&direction={direction}&since={since}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-comments

---

### 获取 issue 关联的 pull requests

<a id="获取-issue-关联的-pull-requests"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/{number}/pull_requests`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Issue 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `mode` | 否 | integer | 1 (增强模式，传上述参数，返回 pr 的 mergeable 状态）; 0(默认，不返回mergeable 状态) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `html_url` | string |  |  |
| `diff_url` | string |  |  |
| `number` | integer |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `merged_at` | null |  |  |
| `closed_at` | null |  |  |
| `head` | object(4字段) |  |  |
| `base` | object(4字段) |  |  |
| `assignees` | array&lt;object(5字段)&gt; |  |  |
| `testers` | array&lt;object(5字段)&gt; |  |  |
| `labels` | array&lt;object(8字段)&gt; |  |  |
| `can_merge_check` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/pull_requests?access_token={your-token}&mode={mode}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-number-pull-requests

---

### 获取企业某个Issue所有标签

<a id="获取企业某个issue所有标签"></a>

`GET /api/v5/enterprises/{enterprise}/issues/{issue_id}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业名(path) |
| `issue_id` | **是** | string | issue 全局唯一id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `color` | string |  |  |
| `name` | string |  |  |
| `id` | integer |  |  |
| `url` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/issues/{issue_id}/labels?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-issues-issue-id-labels

---

### 创建Issue标签

<a id="创建issue标签"></a>

`POST /api/v5/repos/{owner}/{repo}/issues/{number}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | issue编号 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**


**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `color` | string |  |  |
| `name` | string |  |  |
| `id` | integer |  |  |
| `title` | string |  |  |
| `type` | null |  |  |
| `textColor` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/labels?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '["string"]'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-issues-number-labels

---

### 删除Issue标签

<a id="删除issue标签"></a>

`DELETE /api/v5/repos/{owner}/{repo}/issues/{number}/labels/{name}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 第几个Issue，即本仓库Issue的序数 |
| `name` | **是** | string | 标签名称(批量删除用英文逗号分隔，如: bug,feature) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/labels/{name}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-issues-number-labels-name

---

### 获取某个issue下的操作日志

<a id="获取某个issue下的操作日志"></a>

`GET /api/v5/repos/{owner}/issues/{number}/operate_logs`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `number` | **是** | string | issue编号 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `repo` | **是** | string | 仓库路径(path) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `user` | object(18字段) |  |  |
| `content` | string |  |  |
| `created_at` | string |  |  |
| `action_type` | string |  |  |
| `update_at` | string |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `head` | object(4字段) |  |  |
| `base` | object(4字段) |  |  |
| `issue_id` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/issues/{number}/operate_logs?access_token={your-token}&repo={repo}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-issues-number-operate-logs

---

### 获取某个企业的所有Issues

<a id="获取某个企业的所有issues"></a>

`GET /api/v5/enterprises/{enterprise}/issues`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `state` | 否 | string | Issue的状态: open（开启的）, closed（关闭的）, all（所有） 默认: open |
| `labels` | 否 | string | 用逗号分开的标签。如: bug,performance |
| `sort` | 否 | string | 排序依据: 创建时间(created)，更新时间(updated_at)。默认: created_at |
| `direction` | 否 | string | 排序方式: 升序(asc)，降序(desc)。默认: desc |
| `since` | 否 | string | 起始的更新时间，要求时间格式为 ISO 8601 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `milestone` | 否 | string | 根据里程碑标题。none为没里程碑的，\*为所有带里程碑的 |
| `assignee` | 否 | string | 用户的username。 none为没指派者, \*为所有带有指派者的 |
| `creator` | 否 | string | 创建Issues的用户username |
| `program` | 否 | string | 所属项目名称。none为没所属有项目的，\*为所有带所属项目的 |
| `created_at` | 否 | string | 任务创建日期，格式2024-11-09 |
| `created_before` | 否 | string | 任务创建截止时间，格式2024-11-09 |
| `search` | 否 | string | 通过关键字搜索issue标题或者内容 |
| `approximate` | 否 | boolean | 是否精确查询数量，默认false |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `html_url` | string | 是 |  |
| `number` | string | 是 |  |
| `state` | string | 是 |  |
| `title` | string | 是 |  |
| `body` | string | 是 |  |
| `user` | object(3字段) | 是 |  |
| `repository` | object(8字段) | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `finished_at` | string | 是 |  |
| `deadline` | string | 是 |  |
| `labels` | array&lt;object(5字段)&gt; | 是 |  |
| `issue_state` | string | 是 |  |
| `comments` | integer | 是 |  |
| `priority` | integer | 是 |  |
| `issue_type` | string | 是 |  |
| `issue_state_detail` | object(3字段) | 是 |  |
| `issue_type_detail` | object(3字段) | 是 |  |
| `issue_priority_detail` | object(2字段) |  | issue优先级 |
| `parent_id` | integer | 是 |  |
| `milestone` | object(10字段) |  |  |
| `url` | string | 是 |  |
| `repository_url` | string | 是 |  |
| `assignee` | object(6字段) | 是 |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/issues?access_token={your-token}&state={state}&labels={labels}&sort={sort}&direction={direction}&since={since}&page={page}&per_page={per_page}&milestone={milestone}&assignee={assignee}&creator={creator}&program={program}&created_at={created_at}&created_before={created_before}&search={search}&approximate={approximate}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-issues

---

### 获取授权用户的所有Issues

<a id="获取授权用户的所有issues"></a>

`GET /api/v5/user/issues`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `filter` | 否 | string | 筛选参数: 授权用户负责的(assigned)，授权用户创建的(created)，包含前两者的(all)。默认: assigned |
| `state` | 否 | string | Issue的状态: open（开启的）, progressing(进行中), closed（关闭的）, rejected（拒绝的）。默认: open |
| `labels` | 否 | string | 用逗号分开的标签。如: bug,performance |
| `sort` | 否 | string | 排序依据: 创建时间(created)，更新时间(updated_at)。默认: created_at |
| `direction` | 否 | string | 排序方式: 升序(asc)，降序(desc)。默认: desc |
| `since` | 否 | string | 起始的更新时间，要求时间格式为 ISO 8601 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `schedule` | 否 | string | 计划开始日期，格式：20181006T173008+80-20181007T173008+80（区间），或者 -20181007T173008+80（小于20181007T173008+80），或者 20181006T173008+80-（大于20181006T173008+80），要求时间格式为20181006T173008+80 |
| `deadline` | 否 | string | 计划截止日期，格式同上 |
| `created_at` | 否 | string | 任务创建时间，格式同上 |
| `finished_at` | 否 | string | 任务完成时间，即任务最后一次转为已完成状态的时间点。格式同上 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `html_url` | string | 是 |  |
| `number` | string | 是 |  |
| `state` | string | 是 |  |
| `title` | string | 是 |  |
| `body` | string | 是 |  |
| `assignee` | object(6字段) | 是 |  |
| `repository` | object(10字段) | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `labels` | array&lt;object(5字段)&gt; | 是 |  |
| `comments` | integer | 是 |  |
| `priority` | integer | 是 |  |
| `depth` | integer | 是 |  |
| `parent_id` | integer | 是 |  |
| `milestone` | object(10字段) | 是 |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/issues?access_token={your-token}&filter={filter}&state={state}&labels={labels}&sort={sort}&direction={direction}&since={since}&page={page}&per_page={per_page}&schedule={schedule}&deadline={deadline}&created_at={created_at}&finished_at={finished_at}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-issues

---

### 更新Issue某条评论

<a id="更新issue某条评论"></a>

`PATCH /api/v5/repos/{owner}/{repo}/issues/comments/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | 评论 ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string | 是 | 评论内容 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/comments/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"body":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-issues-comments-id

---

### 删除Issue某条评论

<a id="删除issue某条评论"></a>

`DELETE /api/v5/repos/{owner}/{repo}/issues/comments/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | 评论 ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/comments/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-issues-comments-id

---

### 获取仓库Issue某条评论

<a id="获取仓库issue某条评论"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/comments/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | 评论 ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `body` | string |  |  |
| `comment_type` | string |  |  |
| `user` | object(4字段) |  |  |
| `target` | object(1字段) |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/comments/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-comments-id

---

### 获取当前用户某个组织的Issues

<a id="获取当前用户某个组织的issues"></a>

`GET /api/v5/orgs/{org}/issues`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `filter` | 否 | string | 筛选参数：授权用户负责的(assigned)，授权用户创建的(created)，包含前两者的(all)。默认：assigned |
| `state` | 否 | string | Issue的状态：open（开启的）, closed（关闭的）,默认：open |
| `labels` | 否 | string | 用逗号分开的标签。如：bug,performance |
| `sort` | 否 | string | 排序依据：创建时间(created)，更新时间(updated_at)。默认：created_at |
| `direction` | 否 | string | 排序方式：升序(asc)，降序(desc)。默认：desc |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `created_at` | 否 | string | 任务创建时间，格式同上 |
| `search` | 否 | string | 通过关键字搜索issue标题或者内容 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `html_url` | string | 是 |  |
| `number` | string | 是 |  |
| `state` | string | 是 |  |
| `title` | string | 是 |  |
| `body` | string | 是 |  |
| `user` | object(3字段) | 是 |  |
| `assignee` | object(6字段) | 是 |  |
| `repository` | object(8字段) | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `finished_at` | string | 是 |  |
| `deadline` | string | 是 |  |
| `labels` | array&lt;object(3字段)&gt; | 是 |  |
| `issue_state` | string | 是 |  |
| `comments` | integer | 是 |  |
| `priority` | integer | 是 |  |
| `issue_type` | string | 是 |  |
| `issue_state_detail` | object(3字段) | 是 |  |
| `issue_type_detail` | object(3字段) | 是 |  |
| `issue_priority_detail` | object(2字段) |  | issue优先级 |
| `parent_id` | integer | 是 |  |
| `url` | string | 是 |  |
| `repository_url` | string | 是 |  |
| `milestone` | object(10字段) |  |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/issues?access_token={your-token}&filter={filter}&state={state}&labels={labels}&sort={sort}&direction={direction}&page={page}&per_page={per_page}&created_at={created_at}&search={search}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org-issues

---

### 获取企业某个Issue所有评论

<a id="获取企业某个issue所有评论"></a>

`GET /api/v5/enterprises/{enterprise}/issues/{number}/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `number` | **是** | integer | issue 全局唯一 id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量：最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string |  |  |
| `created_at` | string |  |  |
| `id` | integer |  |  |
| `target` | object(1字段) |  |  |
| `updated_at` | string |  |  |
| `user` | object(4字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/issues/{number}/comments?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-issues-number-comments

---

### 获取企业的某个Issue

<a id="获取企业的某个issue"></a>

`GET /api/v5/enterprises/{enterprise}/issues/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `number` | **是** | integer | issue 全局唯一 id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `html_url` | string | 是 |  |
| `number` | string | 是 |  |
| `state` | string | 是 |  |
| `title` | string | 是 |  |
| `body` | string | 是 |  |
| `user` | object(5字段) | 是 |  |
| `repository` | object(9字段) | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `finished_at` | string | 是 |  |
| `labels` | array&lt;object(5字段)&gt; | 是 |  |
| `issue_state` | string | 是 |  |
| `comments` | integer | 是 |  |
| `priority` | integer | 是 |  |
| `issue_type` | string | 是 |  |
| `issue_state_detail` | object(3字段) | 是 |  |
| `issue_type_detail` | object(3字段) | 是 |  |
| `issue_priority_detail` | object(2字段) |  | issue优先级 |
| `custom_fields` | array&lt;object(4字段)&gt; |  | 自定义字段信息 |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/issues/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-issues-number

---

### 获取企业issue状态

<a id="获取企业issue状态"></a>

`GET /api/v5/enterprises/{enterprise}/issue/statuses`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 |  |
| `desc` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/issue/statuses?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-issue-statuses

---

### 获取issue关联的分支列表

<a id="获取issue关联的分支列表"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/{number}/related_branches`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Issue 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/related_branches?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-number-related-branches

---

### 获取issue的表态列表

<a id="获取issue的表态列表"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/{number}/user_reactions`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Issue 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前页码 |
| `per_page` | 否 | string | 每页数量 |
| `emoji_name` | 否 | string | emoji表情，可选：like，dislike，smile，confused，love，rocket，eyes，party |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `emoji` | string | 是 |  |
| `emoji_name` | string | 是 |  |
| `user` | object(4字段) | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/user_reactions?access_token={your-token}&page={page}&per_page={per_page}&emoji_name={emoji_name}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-number-user-reactions

---

### 获取issue评论的表态列表

<a id="获取issue评论的表态列表"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/comment/{comment_id}/user_reactions`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `comment_id` | **是** | string | 评论的id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前页码 |
| `per_page` | 否 | string | 每页数量 |
| `emoji_name` | 否 | string | emoji表情，可选：like，dislike，smile，confused，love，rocket，eyes，party |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `emoji` | string | 是 |  |
| `emoji_name` | string | 是 |  |
| `user` | object(4字段) | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/comment/{comment_id}/user_reactions?access_token={your-token}&page={page}&per_page={per_page}&emoji_name={emoji_name}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-comment-comment-id-user-reactions

---

### 获取issue的修改历史

<a id="获取issue的修改历史"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/{number}/modify_history`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Issue 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `deleted` | boolean | 是 |  |
| `created` | boolean | 是 |  |
| `content` | string | 是 |  |
| `user` | object(4字段) | 是 |  |
| `updated_user` | object(4字段) | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/modify_history?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-number-modify-history

---

### 获取issue评论的修改历史

<a id="获取issue评论的修改历史"></a>

`GET /api/v5/repos/{owner}/{repo}/issues/comment/{comment_id}/modify_history`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `comment_id` | **是** | string | 评论的id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `deleted` | boolean | 是 |  |
| `created` | boolean | 是 |  |
| `content` | string | 是 |  |
| `user` | object(4字段) | 是 |  |
| `updated_user` | object(4字段) | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/comment/{comment_id}/modify_history?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-issues-comment-comment-id-modify-history

---

### 设置Issue关联的分支

<a id="设置issue关联的分支"></a>

`PUT /api/v5/repos/{owner}/{repo}/issues/{number}/related_branches`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | issue编号 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `branch_names` | array&lt;string&gt; | 是 | 分支名列表 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/related_branches?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"branch_names":["string"]}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-issues-number-related-branches

---

### 更新issue关联的看板字段

<a id="更新issue关联的看板字段"></a>

`PUT /api/v5/repos/{owner}/{repo}/issues/{number}/kanban_values`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | issue编号 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `kanban_id` | integer | 是 | 看板id |
| `field_values` | array&lt;object(2字段)&gt; | 是 |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `kanban_id` | integer | 是 |  |
| `name` | string | 是 |  |
| `description` | string | 是 |  |
| `values` | array&lt;object(3字段)&gt; | 是 |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/kanban_values?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"kanban_id":0,"field_values":[{"field_name":"string","value":"string"}]}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-issues-number-kanban-values

---

### 搜索

> 用户、仓库、Issue 搜索

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/search/users` | [搜索用户](#搜索用户) |
| `GET` | `/api/v5/search/issues` | [搜索 Issues](#搜索-issues) |
| `GET` | `/api/v5/search/repositories` | [搜索仓库](#搜索仓库) |

---

### 搜索用户

<a id="搜索用户"></a>

`GET /api/v5/search/users`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 最大为 100 |
| `per_page` | 否 | integer | 每页的数量，最大为 50 |
| `q` | **是** | string | 搜索关键字 |
| `sort` | 否 | string | 排序字段，joined_at(注册时间)，默认为最佳匹配 |
| `order` | 否 | string | 排序顺序 (默认:desc) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `avatar_url` | string |  |  |
| `created_at` | string |  |  |
| `html_url` | string |  |  |
| `id` | string |  |  |
| `login` | string |  |  |
| `name` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/search/users?access_token={your-token}&page={page}&per_page={per_page}&q={q}&sort={sort}&order={order}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-search-users

---

### 搜索 Issues

<a id="搜索-issues"></a>

`GET /api/v5/search/issues`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 最大为 100 |
| `per_page` | 否 | integer | 每页的数量，最大为 50 |
| `q` | **是** | string | 搜索关键字 |
| `sort` | 否 | string | 排序字段，created_at(创建时间)、last_push_at(更新时间)，默认为最佳匹配 |
| `order` | 否 | string | 排序顺序 (默认:desc) |
| `repo` | 否 | string | 仓库路径 |
| `state` | 否 | string | 筛选指定状态的 issues, open(开启)、closed(完成) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `html_url` | string |  |  |
| `number` | string |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `repository` | object(7字段) |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `labels` | array&lt;object&gt; |  |  |
| `priority` | integer |  |  |
| `comments` | integer |  |  |
| `parent_id` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/search/issues?access_token={your-token}&page={page}&per_page={per_page}&q={q}&sort={sort}&order={order}&repo={repo}&state={state}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-search-issues

---

### 搜索仓库

<a id="搜索仓库"></a>

`GET /api/v5/search/repositories`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 最大为 100 |
| `per_page` | 否 | integer | 每页的数量，最大为 20 |
| `q` | **是** | string | 搜索关键字 |
| `sort` | 否 | string | 排序字段，last_push_at(更新时间)、stars_count(收藏数)、forks_count(Fork 数)，默认为最佳匹配 |
| `order` | 否 | string | 排序顺序 (默认:desc) |
| `owner` | 否 | string | 仓库所属空间地址(组织或个人的地址path) |
| `fork` | 否 | string | 是否搜索含 fork 的仓库，如果显示,受白名单限制，默认：否 |
| `language` | 否 | string | 筛选指定语言的仓库 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(5字段) |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `description` | string |  |  |
| `status` | string |  |  |
| `ssh_url_to_repo` | string |  |  |
| `http_url_to_repo` | string |  |  |
| `web_url` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `homepage` | string |  |  |
| `members` | array&lt;string&gt; |  |  |
| `forks_count` | integer |  |  |
| `stargazers_count` | integer |  |  |
| `relation` | string |  |  |
| `permission` | object(1字段) |  |  |
| `internal` | integer |  |  |
| `open_issues_count` | integer |  |  |
| `has_issue` | integer |  |  |
| `watchers_count` | integer |  |  |
| `enterprise` | object(4字段) |  |  |
| `default_branch` | string |  |  |
| `fork` | integer |  |  |
| `pushed_at` | string |  |  |
| `owner` | object(4字段) |  |  |
| `issue_template_source` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/search/repositories?access_token={your-token}&page={page}&per_page={per_page}&q={q}&sort={sort}&order={order}&owner={owner}&fork={fork}&language={language}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-search-repositories

---

### Pull Request 管理

> PR 列表、审查、合并、标签、测试与评审人指派

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls` | [获取项目Pull Request列表](#获取项目pull-request列表) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls` | [创建Pull Request](#创建pull-request) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/merge` | [合并Pull Request](#合并pull-request) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/merge` | [判断Pull Request是否合并](#判断pull-request是否合并) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/issues` | [获取pr关联的issue](#获取pr关联的issue) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/comments` | [提交pull request 评论](#提交pull-request-评论) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/comments` | [获取某个Pull Request的所有评论](#获取某个pull-request的所有评论) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/files` | [Pull Request Commit文件列表](#pull-request-commit文件列表) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/pulls/{number}` | [更新Pull Request信息](#更新pull-request信息) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}` | [获取单个Pull Request](#获取单个pull-request) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/commits` | [获取某Pull Request的所有Commit信息](#获取某pull-request的所有commit信息) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/labels` | [创建 Pull Request标签](#创建-pull-request标签) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/labels` | [获取某个 Pull Request的所有标签](#获取某个-pull-request的所有标签) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/labels` | [替换 Pull Request所有标签](#替换-pull-request所有标签) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/labels/{name}` | [删除 Pull Request标签](#删除-pull-request标签) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/test` | [处理 Pull Request测试](#处理-pull-request测试) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/review` | [处理 Pull Request审查](#处理-pull-request审查) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/operate_logs` | [获取某个Pull Request的操作日志](#获取某个pull-request的操作日志) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/testers` | [重置 Pull Request测试 的状态](#重置-pull-request测试-的状态) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/testers` | [指派用户测试 Pull Request](#指派用户测试-pull-request) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/testers` | [取消用户测试Pull Request](#取消用户测试pull-request) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/option_testers` | [获取可作为Pull Request测试人列表](#获取可作为pull-request测试人列表) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/assignees` | [重置 Pull Request审查 的状态](#重置-pull-request审查-的状态) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/assignees` | [指派用户审查 Pull Request](#指派用户审查-pull-request) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/assignees` | [取消用户审查 Pull Request](#取消用户审查-pull-request) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/files.json` | [pr提交的文件变更信息](#pr提交的文件变更信息) |
| `GET` | `/{owner}/{repo}/raw/{head_sha}/{name}` | [获取文件内容](#获取文件内容) |
| `GET` | `/api/v5/enterprises/{enterprise}/pull_requests` | [企业 Pull Request列表](#企业-pull-request列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/comments/{id}` | [获取Pull Request某条评论](#获取pull-request某条评论) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/pulls/comments/{id}` | [编辑评论](#编辑评论) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/pulls/comments/{id}` | [删除评论](#删除评论) |
| `GET` | `/api/v5/org/{org}/pull_requests` | [组织 Pull Request列表](#组织-pull-request列表) |
| `GET` | `/api/v5/enterprises/{enterprise}/issues/{number}/pull_requests` | [获取企业 issue 关联的 Pull Requests](#获取企业-issue-关联的-pull-requests) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/issues` | [Pull Request关联issue](#pull-request关联issue) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/issues` | [Pull Request删除关联的issue](#pull-request删除关联的issue) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/discussions/{discussion_id}/comments` | [回复Pull Request评论](#回复pull-request评论) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/comments/{discussion_id}` | [修改检视意见解决状态](#修改检视意见解决状态) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/reviewers` | [指派用户评审Pull Request](#指派用户评审pull-request) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/reviewers` | [取消用户评审Pull Request](#取消用户评审pull-request) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/option_reviewers` | [获取可作为Pull Request评审人列表](#获取可作为pull-request评审人列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/user_reactions` | [获取Pull Request的表态列表](#获取pull-request的表态列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/comment/{comment_id}/user_reactions` | [获取Pull Request评论的表态列表](#获取pull-request评论的表态列表) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/modify_history` | [获取Pull Request的修改历史](#获取pull-request的修改历史) |
| `GET` | `/api/v5/repos/{owner}/{repo}/pulls/comment/{comment_id}/modify_history` | [获取Pull Request评论的修改历史](#获取pull-request评论的修改历史) |
| `POST` | `/api/v5/repos/{owner}/{repo}/pulls/{number}/refresh_position` | [刷新Pull Request评论位置/过期状态](#刷新pull-request评论位置过期状态) |

---

### 获取项目Pull Request列表

<a id="获取项目pull-request列表"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `state` | 否 | string | 可选。Pull Request状态: all、open、closed、locked、merged，默认：all |
| `base` | 否 | string | 可选。Pull Request提交目标分支的名称 |
| `since` | 否 | string | 可选。起始的更新时间，要求时间格式为 ISO 8601 例如：`2024-11-20T13:00:21+08:00` |
| `direction` | 否 | string | 可选。升序/降序 默认：desc(asc 或者 desc) |
| `sort` | 否 | string | 可选。排序字段: created、updated 默认：created |
| `milestone_number` | 否 | integer | 可选。里程碑序号(id) |
| `labels` | 否 | string | 以逗号分隔的标签名称列表 |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `author` | 否 | string | 可选。PR 创建者用户名 |
| `assignee` | 否 | string | 可选。PR 负责人用户名 |
| `reviewer` | 否 | string | 可选。PR 评审人用户名 |
| `merged_after` | 否 | string | 返回在指定时间之后合并的合并请求,要求时间格式为 ISO 8601 例如：`2024-11-20T13:00:21+08:00` |
| `merged_before` | 否 | string | 返回在指定时间之前合并的合并请求,要求时间格式为 ISO 8601 例如：`2024-11-20T13:00:21+08:00` |
| `only_count` | 否 | boolean | 如果为true，则仅返回合并请求的计数，默认为 false |
| `created_after` | 否 | string | 返回在指定时间之后创建的合并请求,要求时间格式为 ISO 8601 例如：`2024-11-20T13:00:21+08:00` |
| `created_before` | 否 | string | 返回在指定时间之前创建的合并请求,要求时间格式为 ISO 8601 例如：`2024-11-20T13:00:21+08:00` |
| `updated_before` | 否 | string | 返回在指定时间之前更新的合并请求,要求时间格式为 ISO 8601 例如：`2024-11-20T13:00:21+08:00` |
| `updated_after` | 否 | string | 返回在指定时间之后更新的合并请求,要求时间格式为 ISO 8601 例如：`2024-11-20T13:00:21+08:00` |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `number` | integer | 是 |  |
| `html_url` | string | 是 |  |
| `url` | string | 是 |  |
| `close_related_issue` | integer | 是 |  |
| `prune_branch` | boolean | 是 |  |
| `draft` | boolean | 是 |  |
| `labels` | array&lt;object(5字段)&gt; | 是 |  |
| `user` | object(10字段) | 是 |  |
| `assignees` | array&lt;string&gt; | 是 |  |
| `testers` | array&lt;string&gt; | 是 |  |
| `head` | object(5字段) | 是 |  |
| `base` | object(5字段) | 是 |  |
| `id` | integer | 是 |  |
| `iid` | integer | 是 |  |
| `project_id` | integer | 是 |  |
| `title` | string | 是 |  |
| `body` | string | 是 |  |
| `state` | string | 是 |  |
| `assignees_number` | integer | 是 |  |
| `testers_number` | integer | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `merged_at` | string | 是 |  |
| `closed_at` | string | 是 |  |
| `target_branch` | string | 是 |  |
| `source_branch` | string | 是 |  |
| `source_project_id` | integer | 是 |  |
| `force_remove_source_branch` | boolean | 是 |  |
| `web_url` | string | 是 |  |
| `merge_request_type` | string | 是 |  |
| `review_mode` | string | 是 |  |
| `added_lines` | integer | 是 |  |
| `removed_lines` | integer | 是 |  |
| `diff_refs` | object(3字段) | 是 |  |
| `notes` | integer | 是 |  |
| `pipeline_status` | string | 是 |  |
| `pipeline_status_with_code_quality` | string | 是 |  |
| `source_git_url` | string | 是 |  |
| `can_merge_check` | boolean | 是 | 该字段已废弃，如果需要请调用pull request详情接口 |
| `mergeable` | boolean | 是 |  |
| `locked` | boolean | 是 |  |
| `closed_by` | object(10字段) | 是 |  |
| `visibility_reason` | string | 是 | 可见性，public：公开可见，other：仅项目成员可见 |
| `merged_by` | object(5字段) |  | 合并人 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls?access_token={your-token}&state={state}&base={base}&since={since}&direction={direction}&sort={sort}&milestone_number={milestone_number}&labels={labels}&page={page}&per_page={per_page}&author={author}&assignee={assignee}&reviewer={reviewer}&merged_after={merged_after}&merged_before={merged_before}&only_count={only_count}&created_after={created_after}&created_before={created_before}&updated_before={updated_before}&updated_after={updated_after}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls

---

### 创建Pull Request

<a id="创建pull-request"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 必填。Pull Request标题 |
| `head` | string | 是 | 必填。Pull Request提交的源分支。格式：branch 如果跨仓PR传：username:branch |
| `base` | string | 是 | 必填。Pull Request提交目标分支的名称 |
| `body` | string |  | 可选。Pull Request内容 |
| `milestone_number` | integer |  | 可选。里程碑序号(id) |
| `labels` | string |  | 用逗号分开的标签，名称要求长度在 2-20 之间且非特殊字符。如: bug,performance |
| `assignees` | string |  | 可选。审查人员username，可多个，半角逗号分隔，如：(username1,username2), 注意: 当仓库代码审查设置中已设置【指派审查人员】则此选项无效 |
| `testers` | string |  | 可选。测试人员username，可多个，半角逗号分隔，如：(username1,username2), 注意: 当仓库代码审查设置中已设置【指派测试人员】则此选项无效 |
| `prune_source_branch` | boolean |  | 可选。合并PR后是否删除源分支，默认false（不删除） |
| `draft` | boolean |  | 是否设置为草稿 默认false |
| `squash` | boolean |  | 接受 Pull Request时使用扁平化（Squash）合并, 默认false |
| `squash_commit_message` | string |  | squash提交信息 |
| `fork_path` | string |  | fork项目路径【owner/repo】，跨仓PR 必填。 |
| `close_related_issue` | boolean |  | 可选，合并后是否关闭关联的 Issue，默认根据仓库配置设置 |
| `inner_issue_nums` | string | 是 | 分号分隔的issue_iid |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `url` | string |  |  |
| `html_url` | string |  |  |
| `diff_url` | string |  |  |
| `patch_url` | string |  |  |
| `issue_url` | string |  |  |
| `commits_url` | string |  |  |
| `review_comments_url` | string |  |  |
| `review_comment_url` | string |  |  |
| `comments_url` | string |  |  |
| `number` | integer |  |  |
| `title` | string |  |  |
| `description` | string |  |  |
| `state` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `merged_by` | null |  |  |
| `merged_at` | null |  |  |
| `closed_by` | null |  |  |
| `closed_at` | null |  |  |
| `title_html` | null |  |  |
| `description_html` | null |  |  |
| `target_branch` | string |  |  |
| `source_branch` | string |  |  |
| `squash_commit_message` | null |  |  |
| `user_notes_count` | integer |  |  |
| `upvotes` | integer |  |  |
| `downvotes` | integer |  |  |
| `author` | object(13字段) |  |  |
| `assignee` | null |  |  |
| `source_project_id` | integer |  |  |
| `target_project_id` | integer |  |  |
| `labels` | array&lt;object(5字段)&gt; |  |  |
| `work_in_progress` | integer |  |  |
| `milestone` | null |  |  |
| `merge_when_pipeline_succeeds` | integer |  |  |
| `merge_status` | string |  |  |
| `sha` | string |  |  |
| `merge_commit_sha` | null |  |  |
| `discussion_locked` | null |  |  |
| `should_remove_source_branch` | integer |  |  |
| `force_remove_source_branch` | integer |  |  |
| `allow_collaboration` | null |  |  |
| `allow_maintainer_to_push` | null |  |  |
| `web_url` | string |  |  |
| `time_stats` | object(4字段) |  |  |
| `squash` | integer |  |  |
| `merge_request_type` | string |  |  |
| `has_pre_merge_ref` | integer |  |  |
| `review_mode` | string |  |  |
| `is_source_branch_exist` | integer |  |  |
| `approval_merge_request_reviewers` | array&lt;object(10字段)&gt; |  |  |
| `approval_merge_request_approvers` | array&lt;object(10字段)&gt; |  |  |
| `approval_merge_request_testers` | array&lt;object(10字段)&gt; |  |  |
| `source_project` | object(51字段) |  |  |
| `target_project` | object(51字段) |  |  |
| `added_lines` | integer |  |  |
| `removed_lines` | integer |  |  |
| `subscribed` | integer |  |  |
| `changes_count` | string |  |  |
| `latest_build_started_at` | null |  |  |
| `latest_build_finished_at` | null |  |  |
| `first_deployed_to_production_at` | null |  |  |
| `pipeline` | null |  |  |
| `diff_refs` | object(3字段) |  |  |
| `merge_error` | null |  |  |
| `json_merge_error` | null |  |  |
| `rebase_in_progress` | null |  |  |
| `diverged_commits_count` | null |  |  |
| `merge_request_assignee_list` | array&lt;object&gt; |  |  |
| `merge_request_reviewer_list` | array&lt;object&gt; |  |  |
| `user` | object(1字段) |  |  |
| `merge_request_review_count` | integer |  |  |
| `merge_request_reviewers_count` | integer |  |  |
| `notes` | integer |  |  |
| `unresolved_discussions_count` | integer |  |  |
| `e2e_issues` | array&lt;object(18字段)&gt; |  |  |
| `gate_check` | integer |  |  |
| `head_pipeline_id` | null |  |  |
| `pipeline_status` | string |  |  |
| `codequality_status` | string |  |  |
| `pipeline_status_with_code_quality` | string |  |  |
| `from_forked_project` | integer |  |  |
| `forked_project_name` | null |  |  |
| `can_delete_source_branch` | integer |  |  |
| `required_reviewers` | array&lt;object&gt; |  |  |
| `omega_mode` | integer |  |  |
| `root_mr_locked_detail` | null |  |  |
| `source_git_url` | string |  |  |
| `auto_merge` | null |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"title":"string","head":"string","base":"string","body":"string","milestone_number":0,"labels":"string","assignees":"string","testers":"string","prune_source_branch":true,"draft":true,"squash":true,"squash_commit_message":"string","fork_path":"string","close_related_issue":true,"inner_issue_nums":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls

---

### 合并Pull Request

<a id="合并pull-request"></a>

`PUT /api/v5/repos/{owner}/{repo}/pulls/{number}/merge`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `merge_method` | string |  | 可选。合并PR的方法，merge（合并所有提交）、rebase（变基并合入）。默认为merge。 |
| `title` | string |  | 可选。合并标题 |
| `description` | string |  | 可选。合并描述 |
| `force_merge` | boolean |  | 是否强制合并（需开启“允许管理员强制合入”设置，并且需管理员权限） |
| `squash` | boolean |  | 是否squash合并，默认false |
| `squash_commit_message` | string |  | squash合并的相关备注信息 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sha` | string |  |  |
| `merged` | integer |  |  |
| `message` | string |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/merge?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"merge_method":"string","title":"string","description":"string","force_merge":true,"squash":true,"squash_commit_message":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-pulls-number-merge

---

### 判断Pull Request是否合并

<a id="判断pull-request是否合并"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/merge`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `message` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/merge?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-merge

---

### 获取pr关联的issue

<a id="获取pr关联的issue"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/issues`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `number` | string |  |  |
| `title` | string |  |  |
| `state` | string |  |  |
| `url` | string |  |  |
| `body` | string |  |  |
| `user` | object(3字段) |  |  |
| `labels` | array&lt;object(5字段)&gt; |  |  |
| `issue_created_at` | string |  | issue创建时间 |
| `issue_updated_at` | string | 是 | issue更新时间 |
| `issue_closed_at` | string | 是 | issue关闭时间 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/issues?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-issues

---

### 提交pull request 评论

<a id="提交pull-request-评论"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string | 是 | 评论内容 |
| `path` | string |  | 文件的相对路径 |
| `position` | integer |  | 代码所在行数 |
| `position_type` | string |  | "binary":文件级评论，"text":代码行评论。默认为"text"，传了"binary"时position不再生效。 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string |  |  |
| `body` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/comments?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"body":"string","path":"string","position":0,"position_type":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-comments

---

### 获取某个Pull Request的所有评论

<a id="获取某个pull-request的所有评论"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `direction` | 否 | string | 可选。升序/降序(asc/desc) |
| `comment_type` | 否 | string | 可选。筛选评论类型。代码行评论/pr普通评论:diff_comment/pr_comment |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `discussion_id` | string | 是 | 讨论id |
| `body` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `user` | object(5字段) | 是 |  |
| `comment_type` | string | 是 |  |
| `resolved` | boolean | 是 |  |
| `diff_file` | string | 是 |  |
| `diff_position` | object(5字段) | 是 |  |
| `reply` | array&lt;object(5字段)&gt; |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/comments?access_token={your-token}&page={page}&per_page={per_page}&direction={direction}&comment_type={comment_type}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-comments

---

### Pull Request Commit文件列表

<a id="pull-request-commit文件列表"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/files`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sha` | string |  |  |
| `filename` | string |  |  |
| `status` | string |  |  |
| `additions` | string |  |  |
| `deletions` | string |  |  |
| `blob_url` | string |  |  |
| `raw_url` | string |  |  |
| `patch` | object(11字段) |  |  |
| `target_branch` | string |  | 目标分支 |
| `source_branch` | string |  | 源分支 |
| `target_project` | object(4字段) |  | 目标仓库 |
| `source_project` | object(4字段) |  | 源仓库 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/files?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-files

---

### 更新Pull Request信息

<a id="更新pull-request信息"></a>

`PATCH /api/v5/repos/{owner}/{repo}/pulls/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string |  | 可选。Pull Request标题 |
| `body` | string |  | 可选。Pull Request内容 |
| `state` | string |  | 可选。Pull Request状态 |
| `milestone_number` | integer |  | 可选。里程碑序号(id) |
| `labels` | string |  | 用逗号分开的标签，名称要求长度在 2-20 之间且非特殊字符。如: bug,performance |
| `draft` | boolean |  | 是否设置为草稿 |
| `close_related_issue` | boolean |  | 可选，合并后是否关闭关联的 Issue，默认根据仓库配置设置 |
| `prune_branch` | boolean |  | 合并后删除源分支 |
| `squash_merge` | boolean |  | squash合并 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string |  |  |
| `body` | string |  |  |
| `state` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"title":"string","body":"string","state":"string","milestone_number":0,"labels":"string","draft":true,"close_related_issue":true,"prune_branch":true,"squash_merge":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-pulls-number

---

### 获取单个Pull Request

<a id="获取单个pull-request"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `html_url` | string | 是 |  |
| `number` | integer | 是 | 序号 |
| `state` | string | 是 | 状态，open/closed/merged |
| `title` | string | 是 | 标题 |
| `url` | string | 是 | api url |
| `issue_url` | string | 是 | 关联的issue api url |
| `body` | string | 是 | 描述 |
| `assignees_number` | integer | 是 | 审核人人数 |
| `assignees` | array&lt;object(8字段)&gt; | 是 | 审核人信息 |
| `testers` | array&lt;object(7字段)&gt; | 是 | 测试人信息 |
| `approval_reviewers` | array&lt;object(9字段)&gt; | 是 | 评审人信息 |
| `labels` | array&lt;object(6字段)&gt; | 是 | 标签信息 |
| `created_at` | string | 是 | 创建时间 |
| `updated_at` | string | 是 | 更新时间 |
| `closed_at` | string | 是 | 关闭时间 |
| `merged_at` | string | 是 | 合并时间 |
| `draft` | boolean | 是 | 是否为草稿 |
| `can_merge_check` | boolean | 是 | 是否通过合并检查 |
| `prune_branch` | boolean | 是 | 合并后删除源分支 |
| `mergeable` | boolean | 是 | 是否无冲突 |
| `user` | object(5字段) | 是 | 创建人信息 |
| `head` | object(5字段) | 是 | 提交信息 |
| `base` | object(4字段) | 是 | 目标分支信息 |
| `mergeable_state` | object(22字段) | 是 | 门禁信息 |
| `milestone` | object(6字段) | 是 |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，other：仅项目成员可见 |
| `merged_by` | object(5字段) | 是 | 合并人 |
| `close_related_issue` | boolean |  | 合并时是否关闭关联的issue |
| `squash_merge` | boolean | 是 | Squash 合并 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number

---

### 获取某Pull Request的所有Commit信息

<a id="获取某pull-request的所有commit信息"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/commits`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前页码 |
| `per_page` | 否 | string | 每页数量 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sha` | string | 是 |  |
| `html_url` | string | 是 |  |
| `commit` | object(3字段) | 是 |  |
| `author` | object(6字段) | 是 |  |
| `committer` | object(6字段) | 是 |  |
| `parents` | object(2字段) | 是 |  |
| `co_authors` | array&lt;object(6字段)&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/commits?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-commits

---

### 创建 Pull Request标签

<a id="创建-pull-request标签"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**


**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `color` | string |  |  |
| `name` | string |  |  |
| `id` | integer |  |  |
| `title` | string |  |  |
| `type` | null |  |  |
| `textColor` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/labels?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '["string"]'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-labels

---

### 获取某个 Pull Request的所有标签

<a id="获取某个-pull-request的所有标签"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `color` | string |  |  |
| `name` | string |  |  |
| `repository_id` | integer |  |  |
| `url` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `text_color` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/labels?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-labels

---

### 替换 Pull Request所有标签

<a id="替换-pull-request所有标签"></a>

`PUT /api/v5/repos/{owner}/{repo}/pulls/{number}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**


**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/labels?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '["string"]'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-pulls-number-labels

---

### 删除 Pull Request标签

<a id="删除-pull-request标签"></a>

`DELETE /api/v5/repos/{owner}/{repo}/pulls/{number}/labels/{name}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |
| `name` | **是** | string | 标签名称(批量删除用英文逗号分隔，如: bug,feature) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/labels/{name}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-pulls-number-labels-name

---

### 处理 Pull Request测试

<a id="处理-pull-request测试"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/test`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `force` | boolean |  | 是否强制测试通过（默认否），只对管理员生效 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/test?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"force":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-test

---

### 处理 Pull Request审查

<a id="处理-pull-request审查"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/review`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `force` | boolean |  | 是否强制审查通过（默认否），只对管理员生效 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/review?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"force":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-review

---

### 获取某个Pull Request的操作日志

<a id="获取某个pull-request的操作日志"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/operate_logs`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `sort` | 否 | string | 按递减(desc)排序，默认：递减 |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string |  |  |
| `id` | integer |  |  |
| `action` | string |  |  |
| `merge_request_id` | integer |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `discussion_id` | string |  |  |
| `project` | string |  |  |
| `assignee` | null |  |  |
| `proposer` | null |  |  |
| `user` | object(13字段) |  |  |
| `action_type` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/operate_logs?access_token={your-token}&sort={sort}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-operate-logs

---

### 重置 Pull Request测试 的状态

<a id="重置-pull-request测试-的状态"></a>

`PATCH /api/v5/repos/{owner}/{repo}/pulls/{number}/testers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reset_all` | boolean |  | 是否重置所有测试人，默认：false，只对管理员生效 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/testers?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"reset_all":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-pulls-number-testers

---

### 指派用户测试 Pull Request

<a id="指派用户测试-pull-request"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/testers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `testers` | string | 是 | 用户的个人空间地址, 以逗号分隔 |
| `add` | boolean |  | 是否新增测试人，为true会新增测试人，false则为覆盖更新测试人，默认false |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `avatar_url` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/testers?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"testers":"string","add":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-testers

---

### 取消用户测试Pull Request

<a id="取消用户测试pull-request"></a>

`DELETE /api/v5/repos/{owner}/{repo}/pulls/{number}/testers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 第几个PR，即本仓库PR的序数。对应iid |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `testers` | string | 是 | 用户的个人空间地址, 以逗号分隔 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `login` | string | 是 |  |
| `name` | string | 是 |  |
| `avatar_url` | string |  |  |
| `object_id` | string | 是 |  |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/testers?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"testers":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-pulls-number-testers

---

### 获取可作为Pull Request测试人列表

<a id="获取可作为pull-request测试人列表"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/option_testers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `login` | string | 是 |  |
| `name` | string | 是 |  |
| `avatar_url` | string |  |  |
| `object_id` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/option_testers?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-option-approval-testers

---

### 重置 Pull Request审查 的状态

<a id="重置-pull-request审查-的状态"></a>

`PATCH /api/v5/repos/{owner}/{repo}/pulls/{number}/assignees`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reset_all` | boolean |  | 是否重置所有审查人，默认：false，只对管理员生效 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/assignees?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"reset_all":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-pulls-number-assignees

---

### 指派用户审查 Pull Request

<a id="指派用户审查-pull-request"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/assignees`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `assignees` | string | 是 | 用户的个人空间地址, 以 , 分隔 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `assignees_number` | integer |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/assignees?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"assignees":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-assignees

---

### 取消用户审查 Pull Request

<a id="取消用户审查-pull-request"></a>

`DELETE /api/v5/repos/{owner}/{repo}/pulls/{number}/assignees`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `assignees` | 否 | string | 用户的个人空间地址, 以 , 分隔 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/assignees?access_token={your-token}&assignees={assignees}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-pulls-number-assignees

---

### pr提交的文件变更信息

<a id="pr提交的文件变更信息"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/files.json`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `code` | integer |  |  |
| `added_lines` | integer |  |  |
| `remove_lines` | integer |  |  |
| `count` | integer |  |  |
| `diff_refs` | object(3字段) |  |  |
| `diffs` | array&lt;object(6字段)&gt; |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/files.json?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-files-json

---

### 获取文件内容

<a id="获取文件内容"></a>

`GET /{owner}/{repo}/raw/{head_sha}/{name}`

- **认证**: 可选/公共

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string |  |
| `repo` | **是** | string |  |
| `head_sha` | **是** | string |  |
| `name` | **是** | string |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl "https://raw.gitcode.com/{owner}/{repo}/raw/{head_sha}/{name}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-owner-repo-raw-head-sha-name

---

### 企业 Pull Request列表

<a id="企业-pull-request列表"></a>

`GET /api/v5/enterprises/{enterprise}/pull_requests`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `state` | 否 | string | 可选。Pull Request状态，all，所有，open：开启，closed：关闭，merged：合并 |
| `issue_number` | 否 | integer | issue全局id |
| `sort` | 否 | string | 可选。排序字段，创建时间：created，更新时间：updated。默认按创建时间 |
| `direction` | 否 | string | 可选。升序：asc，降序：desc |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `base` | 否 | string | 目标分支 |
| `author` | 否 | string | pull request作者 |
| `search` | 否 | string | 根据 title、description 模糊查询 |
| `created_after` | 否 | string | 返回在指定时间之后创建的合并请求,要求时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `created_before` | 否 | string | 返回在指定时间之前创建的合并请求,要求时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `updated_before` | 否 | string | 返回在指定时间之前更新的合并请求,要求时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `updated_after` | 否 | string | 返回在指定时间之后更新的合并请求,要求时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `labels` | 否 | string | 根据指定的label名称进行筛选，多个使用英文逗号相隔 |
| `scope` | 否 | string | 可选。all，所有；created_by_me，我创建的；assigned_to_me，分配给我的；need_my_approve，需要我审查的；need_my_review，需要我评审的。 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `title` | string | 是 |  |
| `url` | string | 是 |  |
| `html_url` | string | 是 |  |
| `number` | integer | 是 |  |
| `state` | string | 是 |  |
| `assignees_number` | integer | 是 |  |
| `testers_number` | integer | 是 |  |
| `assignees` | array&lt;string&gt; | 是 |  |
| `head` | object(3字段) | 是 |  |
| `base` | object(3字段) | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `labels` | array&lt;object(5字段)&gt; | 是 |  |
| `merged_at` | string | 是 |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，other：仅项目成员可见 |
| `merged_by` | object(5字段) |  | 合并人 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/pull_requests?access_token={your-token}&state={state}&issue_number={issue_number}&sort={sort}&direction={direction}&page={page}&per_page={per_page}&base={base}&author={author}&search={search}&created_after={created_after}&created_before={created_before}&updated_before={updated_before}&updated_after={updated_after}&labels={labels}&scope={scope}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-pull-requests

---

### 获取Pull Request某条评论

<a id="获取pull-request某条评论"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/comments/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | 评论 ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `discussion_id` | string |  | 讨论id |
| `body` | string |  |  |
| `comment_type` | string |  |  |
| `user` | object(4字段) |  |  |
| `target` | object(1字段) |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `position` | object(10字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/comments/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-comments-id

---

### 编辑评论

<a id="编辑评论"></a>

`PATCH /api/v5/repos/{owner}/{repo}/pulls/comments/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | integer | 评论的ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string | 是 | 必填。评论内容 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/comments/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"body":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-pulls-comments-id

---

### 删除评论

<a id="删除评论"></a>

`DELETE /api/v5/repos/{owner}/{repo}/pulls/comments/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | integer | 评论的ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/comments/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-pulls-comments-id

---

### 组织 Pull Request列表

<a id="组织-pull-request列表"></a>

`GET /api/v5/org/{org}/pull_requests`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | org(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `state` | 否 | string | 可选。Pull Request状态，all，所有，open：开启，closed：关闭，merged：合并 |
| `sort` | 否 | string | 可选。排序字段，创建时间：created，更新时间：updated。默认按创建时间 |
| `direction` | 否 | string | 可选。升序：asc，降序：desc |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | string | 每页数量:默认20 |
| `base` | 否 | string | 目标分支 |
| `author` | 否 | string | pull request作者 |
| `search` | 否 | string | 根据 title、description 模糊查询 |
| `created_after` | 否 | string | 返回在指定时间之后创建的合并请求,要求时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `created_before` | 否 | string | 返回在指定时间之前创建的合并请求,要求时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `updated_before` | 否 | string | 返回在指定时间之前更新的合并请求,要求时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `updated_after` | 否 | string | 返回在指定时间之后更新的合并请求,要求时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `scope` | 否 | string | 可选。all，所有；created_by_me，我创建的；assigned_to_me，分配给我的；need_my_approve，需要我审查的；need_my_review，需要我评审的。 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `url` | string |  |  |
| `html_url` | string |  |  |
| `number` | integer |  |  |
| `state` | string |  |  |
| `assignees_number` | integer |  |  |
| `testers_number` | integer |  |  |
| `assignees` | array&lt;object&gt; |  |  |
| `testers` | array&lt;object&gt; |  |  |
| `mergeable` | null |  |  |
| `can_merge_check` | integer |  |  |
| `head` | object(3字段) |  |  |
| `base` | object(3字段) |  |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，other：仅项目成员可见 |
| `merged_by` | object(5字段) |  | 合并人 |
| `labels` | array&lt;object(5字段)&gt; |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/org/{org}/pull_requests?access_token={your-token}&state={state}&sort={sort}&direction={direction}&page={page}&per_page={per_page}&base={base}&author={author}&search={search}&created_after={created_after}&created_before={created_before}&updated_before={updated_before}&updated_after={updated_after}&scope={scope}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-org-org-pull-requests

---

### 获取企业 issue 关联的 Pull Requests

<a id="获取企业-issue-关联的-pull-requests"></a>

`GET /api/v5/enterprises/{enterprise}/issues/{number}/pull_requests`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | org(path/login) |
| `number` | **是** | integer | issue 全局 id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `number` | integer |  |  |
| `html_url` | string |  |  |
| `url` | string |  |  |
| `close_related_issue` | integer |  |  |
| `prune_branch` | integer |  |  |
| `draft` | integer |  |  |
| `labels` | array&lt;object&gt; |  |  |
| `user` | object(7字段) |  |  |
| `assignees` | array&lt;object&gt; |  |  |
| `testers` | array&lt;object&gt; |  |  |
| `head` | object(5字段) |  |  |
| `base` | object(5字段) |  |  |
| `id` | integer |  |  |
| `iid` | integer |  |  |
| `project_id` | integer |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `state` | string |  |  |
| `assignees_number` | integer |  |  |
| `testers_number` | integer |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `merged_at` | string |  |  |
| `closed_at` | string |  |  |
| `target_branch` | string |  |  |
| `source_branch` | string |  |  |
| `source_project_id` | integer |  |  |
| `force_remove_source_branch` | integer |  |  |
| `web_url` | string |  |  |
| `merge_request_type` | string |  |  |
| `review_mode` | string |  |  |
| `added_lines` | integer |  |  |
| `removed_lines` | integer |  |  |
| `diff_refs` | object(3字段) |  |  |
| `notes` | integer |  |  |
| `pipeline_status` | string |  |  |
| `pipeline_status_with_code_quality` | string |  |  |
| `source_git_url` | string |  |  |
| `can_merge_check` | integer |  |  |
| `mergeable` | integer |  |  |
| `locked` | integer |  |  |
| `diff_url` | string |  |  |
| `patch_url` | string |  |  |
| `commits_url` | string |  |  |
| `comments_url` | string |  |  |
| `issue_url` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/issues/{number}/pull_requests?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-issues-number-pull-requests

---

### Pull Request关联issue

<a id="pull-request关联issue"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/issues`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**


**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `number` | string | 是 |  |
| `title` | string | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/issues?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '[0]'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-linked-issues

---

### Pull Request删除关联的issue

<a id="pull-request删除关联的issue"></a>

`DELETE /api/v5/repos/{owner}/{repo}/pulls/{number}/issues`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 第几个PR，即本仓库PR的序数 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**


**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `number` | string | 是 |  |
| `title` | string | 是 |  |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/issues?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '[0]'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-pulls-number-issues

---

### 回复Pull Request评论

<a id="回复pull-request评论"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/discussions/{discussion_id}/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path)<br/><br/> |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 第几个PR，即本仓库PR的序数 |
| `discussion_id` | **是** | string | 讨论id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string | 是 |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 讨论id |
| `noteId` | integer | 是 | 评论id |
| `body` | string | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/discussions/{discussion_id}/comments?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"body":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-discussions-discussions-id-comments

---

### 修改检视意见解决状态

<a id="修改检视意见解决状态"></a>

`PUT /api/v5/repos/{owner}/{repo}/pulls/{number}/comments/{discussion_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 第几个PR，即本仓库PR的序数。对应iid |
| `discussion_id` | **是** | string | 讨论的ID（字符串类型 id）  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `resolved` | boolean | 是 | 是否已解决 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/comments/{discussion_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"resolved":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-pulls-number-comments-discussions-id

---

### 指派用户评审Pull Request

<a id="指派用户评审pull-request"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/reviewers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 第几个PR，即本仓库PR的序数。对应iid |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reviewers` | string | 是 | 用户的个人空间地址, 以逗号分隔 |
| `add` | boolean |  | 是否新增评审人，为true会新增评审人，false会覆盖更新评审人，默认false |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `login` | string | 是 |  |
| `name` | string | 是 |  |
| `avatar_url` | string |  |  |
| `object_id` | string | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/reviewers?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"reviewers":"string","add":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-approval-reviewers

---

### 取消用户评审Pull Request

<a id="取消用户评审pull-request"></a>

`DELETE /api/v5/repos/{owner}/{repo}/pulls/{number}/reviewers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 第几个PR，即本仓库PR的序数。对应iid |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reviewers` | string | 是 | 用户的个人空间地址, 以逗号分隔 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `login` | string | 是 |  |
| `name` | string | 是 |  |
| `avatar_url` | string |  |  |
| `object_id` | string | 是 |  |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/reviewers?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"reviewers":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-pulls-number-approval-reviewers

---

### 获取可作为Pull Request评审人列表

<a id="获取可作为pull-request评审人列表"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/option_reviewers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | 第几个PR，即本仓库PR的序数。对应iid |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `login` | string | 是 |  |
| `name` | string | 是 |  |
| `avatar_url` | string |  |  |
| `object_id` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/option_reviewers?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-option-approval-reviewers

---

### 获取Pull Request的表态列表

<a id="获取pull-request的表态列表"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/user_reactions`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Pull Request 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前页码 |
| `per_page` | 否 | string | 每页数量 |
| `emoji_name` | 否 | string | emoji表情，可选：like，dislike，smile，confused，love，rocket，eyes，party |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `emoji` | string | 是 |  |
| `emoji_name` | string | 是 |  |
| `user` | object(4字段) | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/user_reactions?access_token={your-token}&page={page}&per_page={per_page}&emoji_name={emoji_name}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-user-reactions

---

### 获取Pull Request评论的表态列表

<a id="获取pull-request评论的表态列表"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/comment/{comment_id}/user_reactions`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `comment_id` | **是** | string | 评论的id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前页码 |
| `per_page` | 否 | string | 每页数量 |
| `emoji_name` | 否 | string | emoji表情，可选：like，dislike，smile，confused，love，rocket，eyes，party |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `emoji` | string | 是 |  |
| `emoji_name` | string | 是 |  |
| `user` | object(4字段) | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/comment/{comment_id}/user_reactions?access_token={your-token}&page={page}&per_page={per_page}&emoji_name={emoji_name}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-comment-comment-id-user-reactions

---

### 获取Pull Request的修改历史

<a id="获取pull-request的修改历史"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/{number}/modify_history`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Pull Request 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `deleted` | boolean | 是 |  |
| `created` | boolean | 是 |  |
| `content` | string | 是 |  |
| `user` | object(4字段) | 是 |  |
| `updated_user` | object(4字段) | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/modify_history?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-modify-history

---

### 获取Pull Request评论的修改历史

<a id="获取pull-request评论的修改历史"></a>

`GET /api/v5/repos/{owner}/{repo}/pulls/comment/{comment_id}/modify_history`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `comment_id` | **是** | string | 评论的id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `deleted` | boolean | 是 |  |
| `created` | boolean | 是 |  |
| `content` | string | 是 |  |
| `user` | object(4字段) | 是 |  |
| `updated_user` | object(4字段) | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/comment/{comment_id}/modify_history?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-comment-comment-id-modify-history

---

### 刷新Pull Request评论位置/过期状态

<a id="刷新pull-request评论位置过期状态"></a>

`POST /api/v5/repos/{owner}/{repo}/pulls/{number}/refresh_position`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path)<br/><br/> |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | Pull Request编号 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `refresh_by_paths` | array&lt;string&gt; |  | 按文件路径刷新，文件路径和评论id均不传则刷新所有 |
| `refresh_by_note_id` | string |  | 按评论id刷新，文件路径和评论id均不传则刷新所有 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `note_ids` | array&lt;integer&gt; | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/pulls/{number}/refresh_position?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"refresh_by_paths":["string"],"refresh_by_note_id":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-pulls-number-refresh-position

---

### Commit 管理

> 提交列表、详情、diff、评论与代码量统计

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/repos/{owner}/{repo}/commits` | [获取仓库所有提交](#获取仓库所有提交) |
| `GET` | `/api/v5/repos/{owner}/{repo}/commits/{sha}` | [仓库的某个提交](#仓库的某个提交) |
| `GET` | `/api/v5/repos/{owner}/{repo}/compare/{base}...{head}` | [Commits对比](#commits对比) |
| `POST` | `/api/v5/repos/{owner}/{repo}/commits/{sha}/comments` | [创建commit评论](#创建commit评论) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/comments/{id}` | [删除commit评论](#删除commit评论) |
| `GET` | `/api/v5/repos/{owner}/{repo}/comments/{id}` | [获取仓库的某条Commit评论](#获取仓库的某条commit评论) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/comments/{id}` | [更新Commit评论](#更新commit评论) |
| `GET` | `/api/v5/repos/{owner}/{repo}/comments` | [获取仓库的Commit评论](#获取仓库的commit评论) |
| `GET` | `/api/v5/{owner}/{repo}/repository/commit_statistics` | [获取代码量贡献](#获取代码量贡献) |
| `GET` | `/api/v5/repos/{owner}/{repo}/commits/{ref}/comments` | [获取单个commit评论](#获取单个commit评论) |
| `GET` | `/api/v5/repos/{owner}/{repo}/commit/{sha}/diff` | [获取commit的diff](#获取commit的diff) |
| `GET` | `/api/v5/repos/{owner}/{repo}/commit/{sha}/patch` | [获取commit的patch](#获取commit的patch) |

---

### 获取仓库所有提交

<a id="获取仓库所有提交"></a>

`GET /api/v5/repos/{owner}/{repo}/commits`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `sha` | 否 | string | 提交起始的SHA值 |
| `path` | 否 | string | 包含该文件的提交 |
| `author` | 否 | string | 提交作者的邮箱或个人空间地址(username/login) |
| `since` | 否 | string | 提交的起始时间，时间格式（2024-11-08T16:25:44Z） |
| `until` | 否 | string | 提交的最后时间，时间格式为（2024-11-08T16:25:44Z） |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 |  |
| `sha` | string | 是 |  |
| `html_url` | string | 是 |  |
| `comments_url` | string | 是 |  |
| `commit` | object(4字段) | 是 |  |
| `author` | object(6字段) | 是 |  |
| `committer` | object(7字段) | 是 |  |
| `parents` | array&lt;object(2字段)&gt; | 是 |  |
| `co_authors` | array&lt;object(6字段)&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/commits?access_token={your-token}&sha={sha}&path={path}&author={author}&since={since}&until={until}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-commits

---

### 仓库的某个提交

<a id="仓库的某个提交"></a>

`GET /api/v5/repos/{owner}/{repo}/commits/{sha}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `sha` | **是** | string | 提交的SHA值或者分支名 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `show_diff` | 否 | boolean | 默认为 false; 为 true 时，返回files字段, files字段中包含本地提交中的至多100个变更文件名 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 |  |
| `sha` | string | 是 |  |
| `html_url` | string | 是 |  |
| `comments_url` | string | 是 |  |
| `commit` | object(3字段) | 是 |  |
| `stats` | object(3字段) | 是 |  |
| `files` | array&lt;object(8字段)&gt; | 是 |  |
| `parents` | array&lt;object(2字段)&gt; | 是 |  |
| `co_authors` | array&lt;object(6字段)&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/commits/{sha}?access_token={your-token}&show_diff={show_diff}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-commits-sha

---

### Commits对比

<a id="commits对比"></a>

`GET /api/v5/repos/{owner}/{repo}/compare/{base}...{head}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `base` | **是** | string | 对比的起点。Commit SHA、分支名或标签名 |
| `head` | **是** | string | 对比的终点。Commit SHA、分支名或标签名 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `straight` | 否 | boolean | 是否直对比。默认 false |
| `suffix` | 否 | string | 按照文件后缀过滤文件，如 `.txt`。只影响 `files` |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `base_commit` | object(8字段) |  |  |
| `merge_base_commit` | object(8字段) |  |  |
| `commits` | array&lt;object(4字段)&gt; |  |  |
| `files` | array&lt;object(10字段)&gt; |  |  |
| `truncated` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/compare/{base}...{head}?access_token={your-token}&straight={straight}&suffix={suffix}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-compare-base-head

---

### 创建commit评论

<a id="创建commit评论"></a>

`POST /api/v5/repos/{owner}/{repo}/commits/{sha}/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `sha` | **是** | string | commit的id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string | 是 | 评论内容 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string |  |  |
| `body` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/commits/{sha}/comments?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"body":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-commits-sha-comments

---

### 删除commit评论

<a id="删除commit评论"></a>

`DELETE /api/v5/repos/{owner}/{repo}/comments/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | 评论id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/comments/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-comments-id

---

### 获取仓库的某条Commit评论

<a id="获取仓库的某条commit评论"></a>

`GET /api/v5/repos/{owner}/{repo}/comments/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | 评论id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string |  |  |
| `created_at` | string |  |  |
| `id` | integer |  |  |
| `target` | object |  |  |
| `updated_at` | string |  |  |
| `user` | object(4字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/comments/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-comments-id

---

### 更新Commit评论

<a id="更新commit评论"></a>

`PATCH /api/v5/repos/{owner}/{repo}/comments/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | 评论id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string | 是 | 评论内容 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string |  |  |
| `created_at` | string |  |  |
| `id` | integer |  |  |
| `target` | object |  |  |
| `updated_at` | string |  |  |
| `user` | object(4字段) |  |  |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/comments/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"body":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-comments-id

---

### 获取仓库的Commit评论

<a id="获取仓库的commit评论"></a>

`GET /api/v5/repos/{owner}/{repo}/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `order` | 否 | string | 排序顺序: asc(default),desc |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string |  |  |
| `created_at` | string |  |  |
| `id` | integer |  |  |
| `target` | object |  |  |
| `updated_at` | string |  |  |
| `user` | object(4字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/comments?access_token={your-token}&page={page}&per_page={per_page}&order={order}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-comments

---

### 获取代码量贡献

<a id="获取代码量贡献"></a>

`GET /api/v5/{owner}/{repo}/repository/commit_statistics`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `branch_name` | **是** | string | 分支名 |
| `author` | 否 | string | 作者用户名 |
| `only_self` | 否 | boolean | 查询个人 |
| `since` | 否 | string | 仅返回此日期及以后的提交 |
| `until` | 否 | string | 仅返回此日期及之前的提交 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `commits` | array&lt;object(2字段)&gt; |  |  |
| `statistics` | array&lt;object(7字段)&gt; |  |  |
| `total` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/{owner}/{repo}/repository/commit_statistics?access_token={your-token}&branch_name={branch_name}&author={author}&only_self={only_self}&since={since}&until={until}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-owner-repo-repository-commit-statistics

---

### 获取单个commit评论

<a id="获取单个commit评论"></a>

`GET /api/v5/repos/{owner}/{repo}/commits/{ref}/comments`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `ref` | **是** | string | commit的sha |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string |  |  |
| `created_at` | string |  |  |
| `id` | integer |  |  |
| `updated_at` | string |  |  |
| `user` | object(4字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/commits/{ref}/comments?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-commits-ref-comments

---

### 获取commit的diff

<a id="获取commit的diff"></a>

`GET /api/v5/repos/{owner}/{repo}/commit/{sha}/diff`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `sha` | **是** | string | commit的sha |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/commit/{sha}/diff?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-commits-sha-diff

---

### 获取commit的patch

<a id="获取commit的patch"></a>

`GET /api/v5/repos/{owner}/{repo}/commit/{sha}/patch`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `sha` | **是** | string | commit的sha |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/commit/{sha}/patch?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-commits-sha-patch

---

### Tag 管理

> 标签与保护标签

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/repos/{owner}/{repo}/tags` | [列出项目所有的tags](#列出项目所有的tags) |
| `POST` | `/api/v5/repos/{owner}/{repo}/tags` | [创建一个仓库的Tag](#创建一个仓库的tag) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/tags/{tag_name}` | [删除仓库的一个Tag](#删除仓库的一个tag) |
| `GET` | `/api/v5/repos/{owner}/{repo}/protected_tags` | [列出项目保护 tags](#列出项目保护-tags) |
| `POST` | `/api/v5/repos/{owner}/{repo}/protected_tags` | [创建项目保护 tag](#创建项目保护-tag) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/protected_tags` | [修改项目保护 tag](#修改项目保护-tag) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/protected_tags/{tag_name}` | [删除项目保护 tag](#删除项目保护-tag) |
| `GET` | `/api/v5/repos/{owner}/{repo}/protected_tags/{tag_name}` | [获取项目保护 tag 详情](#获取项目保护-tag-详情) |

---

### 列出项目所有的tags

<a id="列出项目所有的tags"></a>

`GET /api/v5/repos/{owner}/{repo}/tags`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码:默认为 1,默认： 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  |  |
| `message` | string |  |  |
| `commit` | object(2字段) |  |  |
| `tagger` | object(3字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/tags?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-tags

---

### 创建一个仓库的Tag

<a id="创建一个仓库的tag"></a>

`POST /api/v5/repos/{owner}/{repo}/tags`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `refs` | string | 是 | 起点名称，默认main |
| `tag_name` | string | 是 | 新创建的标签名称 |
| `tag_message` | string |  | Tag 描述,默认为空 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  |  |
| `message` | string |  |  |
| `commit` | object(2字段) |  |  |
| `tagger` | null |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/tags?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"refs":"string","tag_name":"string","tag_message":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-tags

---

### 删除仓库的一个Tag

<a id="删除仓库的一个tag"></a>

`DELETE /api/v5/repos/{owner}/{repo}/tags/{tag_name}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `tag_name` | **是** | string | tag名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/tags/{tag_name}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-tags-tag-name

---

### 列出项目保护 tags

<a id="列出项目保护-tags"></a>

`GET /api/v5/repos/{owner}/{repo}/protected_tags`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码:默认为 1:默认为 1 |
| `per_page` | 否 | integer | 每页的数量：最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  |  |
| `create_access_level` | integer |  |  |
| `create_access_level_desc` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/protected_tags?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-protected-tags

---

### 创建项目保护 tag

<a id="创建项目保护-tag"></a>

`POST /api/v5/repos/{owner}/{repo}/protected_tags`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 标签名称 |
| `create_access_level` | integer |  | 允许创建的访问级别(0:不允许任何人;30:开发者、维护者、管理员;40:维护者、管理员),默认:40 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  |  |
| `create_access_level` | integer |  |  |
| `create_access_level_desc` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/protected_tags?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","create_access_level":0}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-protected-tags

---

### 修改项目保护 tag

<a id="修改项目保护-tag"></a>

`PUT /api/v5/repos/{owner}/{repo}/protected_tags`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 标签名称 |
| `create_access_level` | integer | 是 | 允许创建的访问级别(0:不允许任何人;30:开发者、维护者、管理员;40:维护者、管理员) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  |  |
| `create_access_level` | integer |  |  |
| `create_access_level_desc` | string |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/protected_tags?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","create_access_level":0}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-protected-tags

---

### 删除项目保护 tag

<a id="删除项目保护-tag"></a>

`DELETE /api/v5/repos/{owner}/{repo}/protected_tags/{tag_name}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `tag_name` | **是** | string | 标签名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/protected_tags/{tag_name}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-protected-tags-tag-name

---

### 获取项目保护 tag 详情

<a id="获取项目保护-tag-详情"></a>

`GET /api/v5/repos/{owner}/{repo}/protected_tags/{tag_name}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `tag_name` | **是** | string | 标签名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  |  |
| `create_access_level` | integer |  |  |
| `create_access_level_desc` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/protected_tags/{tag_name}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-protected-tags-tag-name

---

### 标签管理

> 仓库/企业标签、Issue 标签

| 方法 | 路径 | 说明 |
|------|------|------|
| `PUT` | `/api/v5/repos/{owner}/{repo}/project_labels` | [替换所有仓库标签](#替换所有仓库标签) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/issues/{number}/labels` | [删除Issue所有标签](#删除issue所有标签) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/issues/{number}/labels` | [替换Issue所有标签](#替换issue所有标签) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/labels/{original_name}` | [更新一个仓库的任务标签](#更新一个仓库的任务标签) |
| `GET` | `/api/v5/repos/{owner}/{repo}/labels` | [获取仓库所有任务标签](#获取仓库所有任务标签) |
| `POST` | `/api/v5/repos/{owner}/{repo}/labels` | [创建仓库任务标签](#创建仓库任务标签) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/labels/{name}` | [删除一个仓库任务标签](#删除一个仓库任务标签) |
| `GET` | `/api/v5/enterprises/{enterprise}/labels` | [获取企业所有的标签](#获取企业所有的标签) |
| `GET` | `/api/v8/enterprises/{enterprise}/labels` | [获取标签列表](#获取标签列表) |

---

### 替换所有仓库标签

<a id="替换所有仓库标签"></a>

`PUT /api/v5/repos/{owner}/{repo}/project_labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**


**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/project_labels?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '["string"]'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-project-labels

---

### 删除Issue所有标签

<a id="删除issue所有标签"></a>

`DELETE /api/v5/repos/{owner}/{repo}/issues/{number}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Issue 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/labels?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-issues-number-labels

---

### 替换Issue所有标签

<a id="替换issue所有标签"></a>

`PUT /api/v5/repos/{owner}/{repo}/issues/{number}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | string | Issue 编号(区分大小写，无需添加 # 号) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**


**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{number}/labels?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '["string"]'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-issues-number-labels

---

### 更新一个仓库的任务标签

<a id="更新一个仓库的任务标签"></a>

`PATCH /api/v5/repos/{owner}/{repo}/labels/{original_name}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `original_name` | **是** | string | 标签原有名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `name` | 否 | string | 标签新名称 |
| `color` | 否 | string | 标签新颜色 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `name` | string |  |  |
| `color` | string |  |  |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/labels/{original_name}?access_token={your-token}&name={name}&color={color}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-labels-original-name

---

### 获取仓库所有任务标签

<a id="获取仓库所有任务标签"></a>

`GET /api/v5/repos/{owner}/{repo}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 100 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `name` | string |  |  |
| `color` | string |  |  |
| `repository_id` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/labels?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-labels

---

### 创建仓库任务标签

<a id="创建仓库任务标签"></a>

`POST /api/v5/repos/{owner}/{repo}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/x-www-form-urlencoded)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  | 标签名称 |
| `color` | string |  | 标签新颜色.如#fff |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `name` | string |  |  |
| `color` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/labels?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-labels

---

### 删除一个仓库任务标签

<a id="删除一个仓库任务标签"></a>

`DELETE /api/v5/repos/{owner}/{repo}/labels/{name}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `name` | **是** | string | 标签名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/labels/{name}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-labels-name

---

### 获取企业所有的标签

<a id="获取企业所有的标签"></a>

`GET /api/v5/enterprises/{enterprise}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 100 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `name` | string |  |  |
| `color` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/labels?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-labels

---

### 获取标签列表

<a id="获取标签列表"></a>

`GET /api/v8/enterprises/{enterprise}/labels`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `search` | 否 | string | 关键词查询 |
| `direction` | 否 | string | 排序方向 asc/desc |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `name` | string |  |  |
| `color` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/enterprises/{enterprise}/labels?access_token={your-token}&search={search}&direction={direction}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-enterprises-enterprise-labels

---

### 里程碑

> 仓库里程碑的增删改查

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/repos/{owner}/{repo}/milestones` | [获取仓库所有里程碑](#获取仓库所有里程碑) |
| `POST` | `/api/v5/repos/{owner}/{repo}/milestones` | [创建仓库里程碑](#创建仓库里程碑) |
| `GET` | `/api/v5/repos/{owner}/{repo}/milestones/{number}` | [获取仓库单个里程碑](#获取仓库单个里程碑) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/milestones/{number}` | [删除仓库单个里程碑](#删除仓库单个里程碑) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/milestones/{number}` | [更新仓库里程碑](#更新仓库里程碑) |

---

### 获取仓库所有里程碑

<a id="获取仓库所有里程碑"></a>

`GET /api/v5/repos/{owner}/{repo}/milestones`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `state` | 否 | string | 里程碑状态: open, closed, all。默认: open |
| `sort` | 否 | string | 排序方式: due_on |
| `direction` | 否 | string | 升序(asc)或是降序(desc)。默认: asc |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `closed_issues` | integer |  |  |
| `created_at` | string |  |  |
| `description` | string |  |  |
| `due_on` | string |  |  |
| `number` | integer |  |  |
| `open_issues` | integer |  |  |
| `repository_id` | integer |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `updated_at` | string |  |  |
| `url` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/milestones?access_token={your-token}&state={state}&sort={sort}&direction={direction}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-milestones

---

### 创建仓库里程碑

<a id="创建仓库里程碑"></a>

`POST /api/v5/repos/{owner}/{repo}/milestones`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 里程碑标题 |
| `description` | string |  | 里程碑具体描述 |
| `due_on` | string | 是 | 里程碑的截止日期 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `closed_issues` | integer |  |  |
| `created_at` | string |  |  |
| `description` | string |  |  |
| `due_on` | string |  |  |
| `number` | integer |  |  |
| `open_issues` | integer |  |  |
| `repository_id` | integer |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `updated_at` | string |  |  |
| `url` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/milestones?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"title":"string","description":"string","due_on":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-milestones

---

### 获取仓库单个里程碑

<a id="获取仓库单个里程碑"></a>

`GET /api/v5/repos/{owner}/{repo}/milestones/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 里程碑序列号（number） |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `closed_issues` | integer |  |  |
| `created_at` | string |  |  |
| `description` | string |  |  |
| `due_on` | string |  |  |
| `number` | integer |  |  |
| `open_issues` | integer |  |  |
| `repository_id` | integer |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `updated_at` | string |  |  |
| `url` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/milestones/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-milestones-number

---

### 删除仓库单个里程碑

<a id="删除仓库单个里程碑"></a>

`DELETE /api/v5/repos/{owner}/{repo}/milestones/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 里程碑序列号（number） |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/milestones/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-milestones-number

---

### 更新仓库里程碑

<a id="更新仓库里程碑"></a>

`PATCH /api/v5/repos/{owner}/{repo}/milestones/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `number` | **是** | integer | 里程碑序列号（number） |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 里程碑标题 |
| `state` | string |  | 里程碑状态: open, closed, all。默认: open |
| `description` | string |  | 里程碑具体描述 |
| `due_on` | string | 是 | 里程碑的截止日期 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `closed_issues` | integer |  |  |
| `created_at` | string |  |  |
| `description` | string |  |  |
| `due_on` | string |  |  |
| `number` | integer |  |  |
| `open_issues` | integer |  |  |
| `repository_id` | integer |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `updated_at` | string |  |  |
| `url` | string |  |  |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/milestones/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"title":"string","state":"string","description":"string","due_on":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-milestones-number

---

### 用户管理

> 用户资料、仓库、Star/Watch、公钥、Namespace

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/users/{username}/starred` | [列出用户 star 了的仓库](#列出用户-star-了的仓库) |
| `GET` | `/api/v5/users/{username}/subscriptions` | [列出用户 watch 了的仓库](#列出用户-watch-了的仓库) |
| `GET` | `/api/v5/user/subscriptions` | [列出授权用户 watch 了的仓库](#列出授权用户-watch-了的仓库) |
| `GET` | `/api/v5/user/namespaces` | [列出授权用户所有的 Namespace](#列出授权用户所有的-namespace) |
| `GET` | `/api/v5/repos/{owner}/{repo}/notifications` | [列出一个仓库里的通知](#列出一个仓库里的通知) |
| `PUT` | `/api/v5/repos/{owner}/{repo}/notifications` | [标记一个仓库里的通知为已读](#标记一个仓库里的通知为已读) |
| `POST` | `/api/v5/user/repos` | [创建个人项目仓库](#创建个人项目仓库) |
| `GET` | `/api/v5/user/repos` | [列出授权用户所有仓库](#列出授权用户所有仓库) |
| `GET` | `/api/v5/repos/{owner}/{repo}` | [获取用户的某个仓库](#获取用户的某个仓库) |
| `GET` | `/api/v5/users/{username}` | [获取一个用户](#获取一个用户) |
| `PATCH` | `/api/v5/user` | [更新授权用户的资料](#更新授权用户的资料) |
| `GET` | `/api/v5/user` | [获取授权用户的资料](#获取授权用户的资料) |
| `GET` | `/api/v5/emails` | [获取授权用户的全部邮箱](#获取授权用户的全部邮箱) |
| `GET` | `/api/v5/users/{username}/events` | [获取用户个人动态](#获取用户个人动态) |
| `GET` | `/api/v5/users/{username}/repos` | [获取某个用户的公开仓库](#获取某个用户的公开仓库) |
| `POST` | `/api/v5/user/keys` | [添加一个公钥](#添加一个公钥) |
| `GET` | `/api/v5/user/keys` | [列出授权用户的所有公钥](#列出授权用户的所有公钥) |
| `DELETE` | `/api/v5/user/keys/{id}` | [删除一个公钥](#删除一个公钥) |
| `GET` | `/api/v5/user/keys/{id}` | [获取一个公钥](#获取一个公钥) |
| `GET` | `/api/v5/user/namespace` | [获取授权用户的一个 Namespace](#获取授权用户的一个-namespace) |
| `GET` | `/api/v5/user/starred` | [列出授权用户 star 了的仓库](#列出授权用户-star-了的仓库) |
| `GET` | `/api/v5/user/pulls` | [列出授权用户的Pull Request列表](#列出授权用户的pull-request列表) |

---

### 列出用户 star 了的仓库

<a id="列出用户-star-了的仓库"></a>

`GET /api/v5/users/{username}/starred`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `username` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `sort` | 否 | string | created/last_push 根据仓库创建时间(created)或最后推送时间(updated)进行排序，默认：创建时间 |
| `direction` | 否 | string | asc/desc 排序方向，默认：降序 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(5字段) |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `parentfull_name` | string |  |  |
| `description` | string |  |  |
| `status` | string |  |  |
| `ssh_url_to_repo` | string |  |  |
| `http_url_to_repo` | string |  |  |
| `web_url` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `homepage` | string |  |  |
| `members` | array&lt;string&gt; |  |  |
| `parent` | object(2字段) |  |  |
| `forks_count` | integer |  |  |
| `stargazers_count` | integer |  |  |
| `relation` | string |  |  |
| `permission` | object(3字段) |  |  |
| `internal` | integer |  |  |
| `open_issues_count` | integer |  |  |
| `has_issue` | integer |  |  |
| `has_issues` | integer |  |  |
| `watchers_count` | integer |  |  |
| `enterprise` | object(4字段) |  |  |
| `default_branch` | string |  |  |
| `fork` | integer |  |  |
| `pushed_at` | string |  |  |
| `owner` | object(4字段) |  |  |
| `issue_template_source` | string |  |  |
| `project_creator` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/users/{username}/starred?access_token={your-token}&sort={sort}&direction={direction}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-username-starred

---

### 列出用户 watch 了的仓库

<a id="列出用户-watch-了的仓库"></a>

`GET /api/v5/users/{username}/subscriptions`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `username` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `sort` | 否 | string | created/last_push 根据仓库创建时间(created)或最后推送时间(updated)进行排序，默认：创建时间 |
| `direction` | 否 | string | asc/desc 排序方向，默认：降序 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(5字段) |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `parentfull_name` | string |  |  |
| `description` | string |  |  |
| `status` | string |  |  |
| `ssh_url_to_repo` | string |  |  |
| `http_url_to_repo` | string |  |  |
| `web_url` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `homepage` | string |  |  |
| `members` | array&lt;string&gt; |  |  |
| `parent` | object(2字段) |  |  |
| `forks_count` | integer |  |  |
| `stargazers_count` | integer |  |  |
| `relation` | string |  |  |
| `permission` | object(3字段) |  |  |
| `internal` | integer |  |  |
| `open_issues_count` | integer |  |  |
| `has_issue` | integer |  |  |
| `has_issues` | integer |  |  |
| `watchers_count` | integer |  |  |
| `enterprise` | object(4字段) |  |  |
| `default_branch` | string |  |  |
| `fork` | integer |  |  |
| `pushed_at` | string |  |  |
| `owner` | object(4字段) |  |  |
| `issue_template_source` | string |  |  |
| `project_creator` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/users/{username}/subscriptions?access_token={your-token}&sort={sort}&direction={direction}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-username-subscriptions

---

### 列出授权用户 watch 了的仓库

<a id="列出授权用户-watch-了的仓库"></a>

`GET /api/v5/user/subscriptions`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `sort` | 否 | string | created/last_push 根据仓库创建时间(created)或最后推送时间(updated)进行排序，默认：创建时间 |
| `direction` | 否 | string | asc/desc 排序方向，默认：降序 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(5字段) |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `parentfull_name` | string |  |  |
| `description` | string |  |  |
| `status` | string |  |  |
| `ssh_url_to_repo` | string |  |  |
| `http_url_to_repo` | string |  |  |
| `web_url` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `homepage` | string |  |  |
| `members` | array&lt;string&gt; |  |  |
| `parent` | object(2字段) |  |  |
| `forks_count` | integer |  |  |
| `stargazers_count` | integer |  |  |
| `relation` | string |  |  |
| `permission` | object(3字段) |  |  |
| `internal` | integer |  |  |
| `open_issues_count` | integer |  |  |
| `has_issue` | integer |  |  |
| `has_issues` | integer |  |  |
| `watchers_count` | integer |  |  |
| `enterprise` | object(4字段) |  |  |
| `default_branch` | string |  |  |
| `fork` | integer |  |  |
| `pushed_at` | string |  |  |
| `owner` | object(4字段) |  |  |
| `issue_template_source` | string |  |  |
| `project_creator` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/subscriptions?access_token={your-token}&sort={sort}&direction={direction}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-subscriptions

---

### 列出授权用户所有的 Namespace

<a id="列出授权用户所有的-namespace"></a>

`GET /api/v5/user/namespaces`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `mode` | 否 | string | 参与方式: project(所有参与仓库的namepsce)、intrant(所加入的namespace)、all(包含前两者)，默认(intrant) |
| `page` | 否 | integer | 当前页码 |
| `perPage` | 否 | integer | 每页的项目数 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `path` | string | 是 |  |
| `name` | string | 是 |  |
| `html_url` | string | 是 |  |
| `type` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/namespaces?access_token={your-token}&mode={mode}&page={page}&perPage={perPage}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-namespaces

---

### 列出一个仓库里的通知

<a id="列出一个仓库里的通知"></a>

`GET /api/v5/repos/{owner}/{repo}/notifications`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string |  |
| `repo` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `unread` | 否 | boolean | 是否未读，默认true |
| `type` | 否 | string | 筛选指定类型的通知，all：所有，event：事件通知，referer：@ 通知 |
| `since` | 否 | string | 只获取在给定时间前更新后的消息，要求时间格式为 ISO 860 |
| `before` | 否 | string | 只获取在给定时间前更新的消息，要求时间格式为 ISO 860 |
| `ids` | 否 | string | 指定一组通知 ID，以 , 分隔 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `list` | array&lt;object(8字段)&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/notifications?access_token={your-token}&unread={unread}&type={type}&since={since}&before={before}&ids={ids}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-notifications

---

### 标记一个仓库里的通知为已读

<a id="标记一个仓库里的通知为已读"></a>

`PUT /api/v5/repos/{owner}/{repo}/notifications`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string |  |
| `repo` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `ids` | 否 | string | 指定一组通知 ID，以 , 分隔 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/notifications?access_token={your-token}&ids={ids}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-notifications

---

### 创建个人项目仓库

<a id="创建个人项目仓库"></a>

`POST /api/v5/user/repos`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 仓库名称 |
| `description` | string |  | 仓库描述 |
| `has_issues` | boolean |  | 允许提Issue与否。默认: 允许(true) |
| `has_wiki` | boolean |  | 提供Wiki与否。默认: 提供(true) |
| `can_comment` | boolean |  | 允许用户对仓库进行评论。默认：允许(true) |
| `auto_init` | boolean |  | 值为true时则会用README初始化仓库。默认: 不初始化(false) |
| `gitignore_template` | string |  | Git Ignore模版 |
| `license_template` | string |  | License模版 |
| `path` | string |  | 仓库路径 |
| `private` | boolean |  | 是否私有 |
| `default_branch` | string |  | 初始化项目时的默认分支名称。默认: main |
| `import_url` | string |  | 导入项目的git地址，import_url和project_template参数职能传入一个 |
| `project_template` | string |  | 模板项目的path，import_url和project_template参数职能传入一个 |
| `repository_type` | string |  | 仓库类型。code:代码仓，model:模型仓，dataset:数据集仓，space:space，默认code |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(10字段) |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `description` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |
| `visibility` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/user/repos?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","description":"string","has_issues":true,"has_wiki":true,"can_comment":true,"auto_init":true,"gitignore_template":"string","license_template":"string","path":"string","private":true,"default_branch":"string","import_url":"string","project_template":"string","repository_type":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-user-repos

---

### 列出授权用户所有仓库

<a id="列出授权用户所有仓库"></a>

`GET /api/v5/user/repos`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `visibility` | 否 | string | 公开(public)、私有(private)或者所有(all)，默认: 所有(all) |
| `affiliation` | 否 | string | owner(授权用户拥有的仓库)、collaborator(授权用户为仓库成员)、organization_member(授权用户为仓库所在组织并有访问仓库权限)、enterprise_member(授权用户所在企业并有访问仓库权限)、admin(所有有权限的，包括所管理的组织中所有仓库、所管理的企业的所有仓库)。可以用逗号分隔符组合。如: owner, organization_member 或 owner, collaborator, organization_member |
| `type` | 否 | string | 筛选用户仓库: 其创建(owner)、个人(personal)、其为成员(member)、公开(public)、私有(private)，不能与 affiliation 参数一并使用，否则会报 422 错误，与visibility参数一起使用，visibility参数拥有更高的优先级 |
| `sort` | 否 | string | 排序方式: 创建时间(created)，更新时间(updated)，最后推送时间(pushed)，仓库所属与名称(full_name)。默认: full_name |
| `direction` | 否 | string | 如果sort参数为full_name，用升序(asc)。否则降序(desc) |
| `q` | 否 | string | 搜索关键字 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `repo_type` | 否 | string | 筛选仓库类型，可以是 code：代码仓，model：模型仓，dataset：数据集仓，space ：space，all：所有仓库类型。默认: code |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(5字段) |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `description` | string |  |  |
| `status` | string |  |  |
| `ssh_url_to_repo` | string |  |  |
| `http_url_to_repo` | string |  |  |
| `web_url` | string |  |  |
| `homepage` | string |  |  |
| `members` | array&lt;string&gt; |  |  |
| `assignee` | array&lt;object(6字段)&gt; |  |  |
| `forks_count` | integer |  |  |
| `stargazers_count` | integer |  |  |
| `project_labels` | array&lt;object&gt; |  |  |
| `relation` | string |  |  |
| `permission` | object(3字段) |  |  |
| `internal` | integer |  |  |
| `open_issues_count` | integer |  |  |
| `has_issue` | integer |  |  |
| `watched` | integer |  |  |
| `watchers_count` | integer |  |  |
| `assignees_number` | integer |  |  |
| `enterprise` | object(4字段) |  |  |
| `default_branch` | string |  |  |
| `fork` | integer |  |  |
| `owner` | object(4字段) |  |  |
| `assigner` | object(4字段) |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |
| `repo_type` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/repos?access_token={your-token}&visibility={visibility}&affiliation={affiliation}&type={type}&sort={sort}&direction={direction}&q={q}&page={page}&per_page={per_page}&repo_type={repo_type}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-repos

---

### 获取用户的某个仓库

<a id="获取用户的某个仓库"></a>

`GET /api/v5/repos/{owner}/{repo}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(4字段) |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `description` | string |  |  |
| `status` | string |  |  |
| `ssh_url_to_repo` | string |  |  |
| `http_url_to_repo` | string |  |  |
| `web_url` | string |  |  |
| `readme_url` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `creator` | object(6字段) |  |  |
| `members` | array&lt;string&gt; |  |  |
| `forks_count` | integer |  |  |
| `stargazers_count` | integer |  |  |
| `project_labels` | array&lt;object&gt; |  |  |
| `license` | string |  |  |
| `internal` | integer |  |  |
| `open_issues_count` | integer |  |  |
| `watchers_count` | integer |  |  |
| `assignees_number` | integer |  |  |
| `enterprise` | object(4字段) |  |  |
| `default_branch` | string |  |  |
| `fork` | integer |  |  |
| `owner` | object(4字段) |  |  |
| `assigner` | object(4字段) |  |  |
| `issue_template_source` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo

---

### 获取一个用户

<a id="获取一个用户"></a>

`GET /api/v5/users/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `username` | **是** | string | 用户名(username) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `avatar_url` | string |  |  |
| `followers_url` | string |  |  |
| `html_url` | string |  |  |
| `id` | string |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `type` | string |  |  |
| `url` | string |  |  |
| `bio` | string |  |  |
| `blog` | string |  |  |
| `company` | string |  |  |
| `email` | string |  |  |
| `followers` | integer |  |  |
| `following` | integer |  |  |
| `top_languages` | array&lt;string&gt; |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/users/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-users-username

---

### 更新授权用户的资料

<a id="更新授权用户的资料"></a>

`PATCH /api/v5/user`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**Header 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `Content-Type` | 否 | string |  （示例: `application/json`） |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `avatar` | string |  | 头像 |
| `nickname` | string |  | 用户昵称 |
| `company` | string |  | 公司 |
| `description` | string |  | 个人简介 |
| `email` | string |  | 个人邮箱（需要通过认证处理） |
| `github_account` | string |  | Github账号 |
| `website` | string |  | 个人网站 |
| `location` | string |  | 所在地 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `avatar` | string | 是 |  |
| `nickname` | string | 是 |  |
| `company` | string | 是 |  |
| `description` | string | 是 |  |
| `email` | string | 是 |  |
| `github_account` | string | 是 |  |
| `website` | string | 是 |  |
| `location` | string | 是 |  |
| `id` | string | 是 |  |
| `login` | string | 是 |  |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/user?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"avatar":"string","nickname":"string","company":"string","description":"string","email":"string","github_account":"string","website":"string","location":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-user

---

### 获取授权用户的资料

<a id="获取授权用户的资料"></a>

`GET /api/v5/user`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `avatar_url` | string |  |  |
| `followers_url` | string |  |  |
| `html_url` | string |  |  |
| `id` | string |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `type` | string |  |  |
| `url` | string |  |  |
| `bio` | string |  |  |
| `blog` | string |  |  |
| `company` | string |  |  |
| `email` | string |  |  |
| `followers` | integer |  |  |
| `following` | integer |  |  |
| `top_languages` | array&lt;string&gt; |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user

---

### 获取授权用户的全部邮箱

<a id="获取授权用户的全部邮箱"></a>

`GET /api/v5/emails`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `email` | string |  |  |
| `state` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/emails?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-emails

---

### 获取用户个人动态

<a id="获取用户个人动态"></a>

`GET /api/v5/users/{username}/events`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `username` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `year` | 否 | string | 开始年（2024） |
| `next` | 否 | string | 结束日期 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `events` | object(1字段) |  |  |
| `next` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/users/{username}/events?access_token={your-token}&year={year}&next={next}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-users-username-events

---

### 获取某个用户的公开仓库

<a id="获取某个用户的公开仓库"></a>

`GET /api/v5/users/{username}/repos`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `type` | 否 | string | 用户创建的仓库(owner)，用户个人仓库(personal)，用户为仓库成员(member)，所有(all)。默认: 所有(all) |
| `sort` | 否 | string | 排序方式: 创建时间(created)，更新时间(updated)，最后推送时间(pushed)，仓库所属与名称(full_name)。默认: full_name |
| `direction` | 否 | string | 如果sort参数为full_name，用升序(asc)。否则降序(desc) |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(5字段) |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `description` | string |  |  |
| `status` | string |  |  |
| `ssh_url_to_repo` | string |  |  |
| `http_url_to_repo` | string |  |  |
| `web_url` | string |  |  |
| `homepage` | string |  |  |
| `members` | array&lt;string&gt; |  |  |
| `assignee` | array&lt;object(6字段)&gt; |  |  |
| `forks_count` | integer |  |  |
| `stargazers_count` | integer |  |  |
| `project_labels` | array&lt;object&gt; |  |  |
| `relation` | string |  |  |
| `permission` | object(3字段) |  |  |
| `internal` | integer |  |  |
| `open_issues_count` | integer |  |  |
| `has_issue` | integer |  |  |
| `watched` | integer |  |  |
| `watchers_count` | integer |  |  |
| `assignees_number` | integer |  |  |
| `enterprise` | object(4字段) |  |  |
| `default_branch` | string |  |  |
| `fork` | integer |  |  |
| `owner` | object(4字段) |  |  |
| `assigner` | object(4字段) |  |  |
| `issue_template_source` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |
| `gitee` | object(3字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/users/{username}/repos?access_token={your-token}&type={type}&sort={sort}&direction={direction}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-users-username-repos

---

### 添加一个公钥

<a id="添加一个公钥"></a>

`POST /api/v5/user/keys`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `key` | string | 是 | 公钥内容 |
| `title` | string | 是 | 公钥名称 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `title` | string |  |  |
| `key` | string |  |  |
| `created_at` | string |  |  |
| `url` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/user/keys?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"key":"string","title":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-user-keys

---

### 列出授权用户的所有公钥

<a id="列出授权用户的所有公钥"></a>

`GET /api/v5/user/keys`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `title` | string |  |  |
| `key` | string |  |  |
| `created_at` | string |  |  |
| `url` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/keys?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-keys

---

### 删除一个公钥

<a id="删除一个公钥"></a>

`DELETE /api/v5/user/keys/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `id` | **是** | string | 公钥 ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/user/keys/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-user-keys-id

---

### 获取一个公钥

<a id="获取一个公钥"></a>

`GET /api/v5/user/keys/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `id` | **是** | string | 公钥 ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `title` | string |  |  |
| `key` | string |  |  |
| `created_at` | string |  |  |
| `url` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/keys/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-keys-id

---

### 获取授权用户的一个 Namespace

<a id="获取授权用户的一个-namespace"></a>

`GET /api/v5/user/namespace`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `path` | **是** | string | Namespace path |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `html_url` | string |  |  |
| `type` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/namespace?access_token={your-token}&path={path}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-namespace

---

### 列出授权用户 star 了的仓库

<a id="列出授权用户-star-了的仓库"></a>

`GET /api/v5/user/starred`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `sort` | 否 | string | created/last_push 根据仓库创建时间(created)或最后推送时间(updated)进行排序，默认：创建时间 |
| `direction` | 否 | string | asc/desc 排序方向，默认：降序 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `full_name` | string |  |  |
| `human_name` | string |  |  |
| `url` | string |  |  |
| `namespace` | object(5字段) |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `parentfull_name` | string |  |  |
| `description` | string |  |  |
| `status` | string |  |  |
| `ssh_url_to_repo` | string |  |  |
| `http_url_to_repo` | string |  |  |
| `web_url` | string |  |  |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `homepage` | string |  |  |
| `members` | array&lt;string&gt; |  |  |
| `parent` | object(2字段) |  |  |
| `forks_count` | integer |  |  |
| `stargazers_count` | integer |  |  |
| `relation` | string |  |  |
| `permission` | object(3字段) |  |  |
| `internal` | integer |  |  |
| `open_issues_count` | integer |  |  |
| `has_issue` | integer |  |  |
| `has_issues` | integer |  |  |
| `watchers_count` | integer |  |  |
| `enterprise` | object(4字段) |  |  |
| `default_branch` | string |  |  |
| `fork` | integer |  |  |
| `pushed_at` | string |  |  |
| `owner` | object(4字段) |  |  |
| `issue_template_source` | string |  |  |
| `project_creator` | string |  |  |
| `private` | integer |  |  |
| `public` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/starred?access_token={your-token}&sort={sort}&direction={direction}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-starred

---

### 列出授权用户的Pull Request列表

<a id="列出授权用户的pull-request列表"></a>

`GET /api/v5/user/pulls`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `state` | 否 | string | 返回已打开、已关闭、已锁定、已合并或所有合并请求， open：已打开；closed：已关闭；locked：已锁定；merged：已合并；all：所有。默认返回所有的 |
| `sort` | 否 | string | 返回按字段排序的合并请求，创建时间：created；更新时间：updated；合并时间：merged_at。默认按照创建时间排序 |
| `direction` | 否 | string | 返回按照排序字段升序或者降序的合并请求，desc：降序；asc：升序 |
| `labels` | 否 | string | 逗号分隔的标签名称 |
| `created_after` | 否 | string | 返回在指定时间之后创建的合并请求，时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `created_before` | 否 | string | 返回在指定时间之前创建的合并请求，时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `updated_after` | 否 | string | 返回在指定时间之后更新的合并请求，时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `updated_before` | 否 | string | 返回在指定时间之前更新的合并请求，时间格式为 ISO 8601 例如：2024-11-20T13:00:21+08:00 |
| `scope` | 否 | string | 返回给定范围的合并请求，need_my_approve：需要我审查的；created_by_me：由我创建的；assigned_to_me：分配给我的；need_my_review：需要我评审的。默认返回我创建的 |
| `source_branch` | 否 | string | 返回具有给定源分支的合并请求 |
| `target_branch` | 否 | string | 返回具有给定目标分支的合并请求 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |
| `page` | 否 | string | 当前的页码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `number` | integer | 是 | 序号 |
| `html_url` | string | 是 | 页面地址 |
| `url` | string | 是 |  |
| `close_related_issue` | integer | 是 | 合并后关闭关联的issue |
| `draft` | boolean | 是 | 是否草稿状态 |
| `labels` | array&lt;object(5字段)&gt; | 是 |  |
| `user` | object(10字段) | 是 | 作者 |
| `assignees` | array&lt;object(13字段)&gt; | 是 | 评审人列表 |
| `testers` | array&lt;object(12字段)&gt; | 是 | 测试人列表 |
| `head` | object(4字段) | 是 | 提交信息 |
| `base` | object(4字段) | 是 | 目标分支信息 |
| `id` | integer | 是 |  |
| `iid` | integer | 是 |  |
| `project_id` | integer | 是 | 归属项目id |
| `title` | string | 是 | 标题 |
| `body` | string | 是 | 描述 |
| `state` | string | 是 | 状态，open/closed/merged |
| `assignees_number` | integer | 是 | 评审人数量 |
| `testers_number` | integer | 是 | 测试人数量 |
| `created_at` | string | 是 | 创建时间 |
| `updated_at` | string | 是 | 更新时间 |
| `merged_at` | string | 是 | 合并时间 |
| `closed_at` | string | 是 | 关闭时间 |
| `target_branch` | string | 是 | 目标分支 |
| `source_branch` | string | 是 | 源分支 |
| `source_project_id` | integer | 是 | 源项目id |
| `web_url` | string | 是 |  |
| `merge_request_type` | string | 是 | 合并请求类型 |
| `added_lines` | integer | 是 | 新增的行数 |
| `removed_lines` | integer | 是 | 删除的行数 |
| `diff_refs` | object(3字段) | 是 | 差异引用 |
| `notes` | integer | 是 | 评论数量 |
| `source_git_url` | string | 是 |  |
| `mergeable` | boolean | 是 | 是否可合并 |
| `locked` | boolean | 是 | 是否锁定 |
| `closed_by` | object(10字段) | 是 | 关闭人信息 |
| `visibility_reason` | string |  | 可见性，public：公开可见，other：仅项目成员可见 |
| `merged_by` | object(5字段) |  | 合并人 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/pulls?access_token={your-token}&state={state}&sort={sort}&direction={direction}&labels={labels}&created_after={created_after}&created_before={created_before}&updated_after={updated_after}&updated_before={updated_before}&scope={scope}&source_branch={source_branch}&target_branch={target_branch}&per_page={per_page}&page={page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-users-merge-requests

---

### 组织管理

> 组织信息、成员、讨论、自定义角色

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/orgs/{org}/repos` | [获取组织项目列表](#获取组织项目列表) |
| `GET` | `/api/v5/users/{username}/orgs` | [列出用户所属的组织](#列出用户所属的组织) |
| `GET` | `/api/v5/users/orgs` | [列出授权用户所属的组织](#列出授权用户所属的组织) |
| `GET` | `/api/v5/orgs/{org}/members/{username}` | [获取组织成员详情](#获取组织成员详情) |
| `GET` | `/api/v5/orgs/{org}` | [获取一个组织信息](#获取一个组织信息) |
| `PATCH` | `/api/v5/orgs/{org}` | [更新授权用户所管理的组织资料](#更新授权用户所管理的组织资料) |
| `GET` | `/api/v5/enterprises/{enterprise}/members/{username}` | [获取企业的一个成员](#获取企业的一个成员) |
| `PUT` | `/api/v5/enterprises/{enterprise}/members/{username}` | [修改企业成员权限](#修改企业成员权限) |
| `GET` | `/api/v5/user/memberships/orgs/{org}` | [获取授权用户在一个组织的成员资料](#获取授权用户在一个组织的成员资料) |
| `DELETE` | `/api/v5/user/memberships/orgs/{org}` | [退出一个组织](#退出一个组织) |
| `GET` | `/api/v5/orgs/{org}/members` | [列出一个组织的所有成员](#列出一个组织的所有成员) |
| `GET` | `/api/v5/enterprises/{enterprise}/members` | [列出企业的所有成员](#列出企业的所有成员) |
| `DELETE` | `/api/v5/orgs/{org}/memberships/{username}` | [移除授权用户所管理组织中的成员](#移除授权用户所管理组织中的成员) |
| `POST` | `/api/v5/orgs/{org}/memberships/{username}` | [邀请组织成员](#邀请组织成员) |
| `GET` | `/api/v5/orgs/{owner}/followers` | [列出指定组织的所有关注者](#列出指定组织的所有关注者) |
| `GET` | `/api/v5/orgs/{org}/issue/extend/settings` | [获取 issue 扩展配置](#获取-issue-扩展配置) |
| `GET` | `/api/v5/orgs/{org}/customized_roles` | [获取组织自定义角色](#获取组织自定义角色) |
| `POST` | `/api/v5/orgs` | [创建组织](#创建组织) |
| `GET` | `/api/v5/orgs/{org}/discuss` | [获取组织讨论列表](#获取组织讨论列表) |
| `GET` | `/api/v5/orgs/{org}/discuss/{number}` | [获取组织讨论详情](#获取组织讨论详情) |
| `GET` | `/api/v5/orgs/{org}/discuss/{number}/comment` | [获取组织讨论评论列表](#获取组织讨论评论列表) |
| `GET` | `/api/v5/orgs/{org}/discuss/{number}/comment/{comment_id}/reply` | [获取组织讨论评论回复列表](#获取组织讨论评论回复列表) |

---

### 获取组织项目列表

<a id="获取组织项目列表"></a>

`GET /api/v5/orgs/{org}/repos`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `type` | 否 | string | 筛选公开/私有仓库的类型，可以是 all, public, private。默认: all |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `repo_type` | 否 | string | 筛选仓库类型，可以是 code：代码仓，model：模型仓，dataset：数据集仓，space ：space，all：所有仓库类型。默认: code |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `full_name` | string | 是 |  |
| `namespace` | object(5字段) | 是 |  |
| `path` | string | 是 |  |
| `name` | string | 是 |  |
| `internal` | boolean | 是 |  |
| `fork` | boolean | 是 |  |
| `html_url` | string | 是 |  |
| `forks_count` | integer | 是 |  |
| `stargazers_count` | integer | 是 |  |
| `watchers_count` | integer | 是 |  |
| `default_branch` | string | 是 |  |
| `open_issues_count` | integer | 是 |  |
| `project_creator` | string | 是 |  |
| `pushed_at` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `status` | string | 是 |  |
| `language` | string | 是 |  |
| `repo_type` | string | 是 |  |
| `parent` | object(15字段) | 是 |  |
| `private` | boolean | 是 |  |
| `public` | boolean | 是 |  |
| `description` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/repos?access_token={your-token}&type={type}&page={page}&per_page={per_page}&repo_type={repo_type}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org-repos

---

### 列出用户所属的组织

<a id="列出用户所属的组织"></a>

`GET /api/v5/users/{username}/orgs`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | string | 当前的页码:默认为 1 |
| `per_page` | 否 | string | 每页的数量，默认为20，最大为100 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `avatar_url` | null |  |  |
| `repos_url` | null |  |  |
| `events_url` | null |  |  |
| `members_url` | null |  |  |
| `description` | string |  |  |
| `follow_count` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/users/{username}/orgs?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-users-username-orgs

---

### 列出授权用户所属的组织

<a id="列出授权用户所属的组织"></a>

`GET /api/v5/users/orgs`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | string | 当前的页码:默认为 1 |
| `per_page` | 否 | string | 每页的数量，默认为20，最大为100 |
| `admin` | 否 | boolean | 筛选有管理员权限 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `login` | string |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `avatar_url` | null |  |  |
| `repos_url` | null |  |  |
| `events_url` | null |  |  |
| `members_url` | null |  |  |
| `description` | string |  |  |
| `follow_count` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/users/orgs?access_token={your-token}&page={page}&per_page={per_page}&admin={admin}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-users-orgs

---

### 获取组织成员详情

<a id="获取组织成员详情"></a>

`GET /api/v5/orgs/{org}/members/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径(path/login) |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `url` | string |  |  |
| `avatar_url` | null |  |  |
| `user` | object(7字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/members/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org-members-username

---

### 获取一个组织信息

<a id="获取一个组织信息"></a>

`GET /api/v5/orgs/{org}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `avatar_url` | string |  |  |
| `repos_url` | string |  |  |
| `events_url` | string |  |  |
| `members_url` | string |  |  |
| `description` | string |  |  |
| `enterprise` | string |  |  |
| `follow_count` | integer |  |  |
| `gitee` | object(1字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org

---

### 更新授权用户所管理的组织资料

<a id="更新授权用户所管理的组织资料"></a>

`PATCH /api/v5/orgs/{org}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  | 组织名称 |
| `email` | string |  | 组织公开的邮箱地址 |
| `location` | string |  | 组织所在地 |
| `description` | string |  | 组织简介 |
| `html_url` | string |  | 组织站点 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `email` | string |  |  |
| `name` | string |  |  |
| `description` | string |  |  |
| `html_url` | string |  |  |
| `id` | integer |  |  |
| `path` | string |  |  |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/orgs/{org}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","email":"string","location":"string","description":"string","html_url":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-orgs-org

---

### 获取企业的一个成员

<a id="获取企业的一个成员"></a>

`GET /api/v5/enterprises/{enterprise}/members/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业的路径(path/login) |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user` | object(7字段) |  |  |
| `url` | string |  |  |
| `active` | integer |  |  |
| `role` | string |  |  |
| `enterprise` | object(2字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/members/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-members-username

---

### 修改企业成员权限

<a id="修改企业成员权限"></a>

`PUT /api/v5/enterprises/{enterprise}/members/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `role` | string | 是 | 企业角色(viewer:浏览者、tester:测试人、developer:研发、maintainer:维护者、admin:管理员、customized:自定义角色) |
| `role_id` | string |  | 角色ID, 如果role传入了customized则需要传入角色id |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `active` | integer |  |  |
| `role` | string |  |  |
| `url` | string |  |  |
| `user` | object(4字段) |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/enterprises/{enterprise}/members/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"role":"string","role_id":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-enterprises-enterprise-members-username

---

### 获取授权用户在一个组织的成员资料

<a id="获取授权用户在一个组织的成员资料"></a>

`GET /api/v5/user/memberships/orgs/{org}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 企业的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `path` | string |  |  |
| `name` | string |  |  |
| `url` | string |  |  |
| `avatar_url` | string |  |  |
| `user` | object(5字段) |  |  |
| `active` | integer |  |  |
| `role` | string |  |  |
| `organization` | object(3字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/user/memberships/orgs/{org}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-user-memberships-orgs-org

---

### 退出一个组织

<a id="退出一个组织"></a>

`DELETE /api/v5/user/memberships/orgs/{org}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/user/memberships/orgs/{org}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-user-memberships-orgs-org

---

### 列出一个组织的所有成员

<a id="列出一个组织的所有成员"></a>

`GET /api/v5/orgs/{org}/members`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 企业的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `role` | 否 | string | 根据角色筛选成员(all/admin/member) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `avatar_url` | string |  |  |
| `followers_url` | string |  |  |
| `html_url` | string |  |  |
| `id` | string |  |  |
| `login` | string |  |  |
| `member_role` | string |  |  |
| `name` | string |  |  |
| `type` | string |  |  |
| `url` | string |  |  |
| `user_id` | string |  |  |
| `object_id` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/members?access_token={your-token}&page={page}&per_page={per_page}&role={role}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org-members

---

### 列出企业的所有成员

<a id="列出企业的所有成员"></a>

`GET /api/v5/enterprises/{enterprise}/members`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 企业的路径(path/login) |
| `enterprise` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `role` | 否 | string | 根据角色筛选成员(all/admin/member) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user` | object(8字段) | 是 |  |
| `url` | string | 是 |  |
| `active` | boolean | 是 |  |
| `role` | string | 是 |  |
| `role_name` | string | 是 |  |
| `role_id` | string | 是 |  |
| `role_name_cn` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/enterprises/{enterprise}/members?access_token={your-token}&page={page}&per_page={per_page}&role={role}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-enterprises-enterprise-members

---

### 移除授权用户所管理组织中的成员

<a id="移除授权用户所管理组织中的成员"></a>

`DELETE /api/v5/orgs/{org}/memberships/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径(path/login) |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/orgs/{org}/memberships/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-orgs-org-memberships-username

---

### 邀请组织成员

<a id="邀请组织成员"></a>

`POST /api/v5/orgs/{org}/memberships/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `permission` | string |  | 成员权限: 参与者(pull)，开发者(push)，维护者(admin)，customized(自定义角色)。默认: push |
| `role_id` | string |  | 角色ID, 如果permission传入了customized则需要传入角色id |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `followers_url` | string |  |  |
| `html_url` | string |  |  |
| `id` | string |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `type` | string |  |  |
| `url` | string |  |  |
| `permissions` | object(4字段) |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/orgs/{org}/memberships/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"permission":"string","role_id":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-orgs-org-memberships-username

---

### 列出指定组织的所有关注者

<a id="列出指定组织的所有关注者"></a>

`GET /api/v5/orgs/{owner}/followers`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `avatar_url` | string |  |  |
| `watch_at` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{owner}/followers?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-owner-followers

---

### 获取 issue 扩展配置

<a id="获取-issue-扩展配置"></a>

`GET /api/v5/orgs/{org}/issue/extend/settings`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 仓库所属空间地址(企业、组织 path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type_name` | string |  |  |
| `type_id` | integer |  |  |
| `type_desc` | string |  |  |
| `status` | array&lt;object(4字段)&gt; |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/issue/extend/settings?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org-issue-extend-settings

---

### 获取组织自定义角色

<a id="获取组织自定义角色"></a>

`GET /api/v5/orgs/{org}/customized_roles`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径(path/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `name_cn` | string | 是 |  |
| `product_id` | string | 是 |  |
| `access_level` | integer | 是 |  |
| `created_at` | string | 是 |  |
| `role_type` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `description` | string | 是 |  |
| `member_count` | integer | 是 |  |
| `role_from` | string | 是 |  |
| `mgnt_mode` | integer | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/customized_roles?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-org-org-customized-roles

---

### 创建组织

<a id="创建组织"></a>

`POST /api/v5/orgs`

- **认证**: 需要

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string |  | 组织名称，默认等于path |
| `path` | string | 是 | 组织路径 |
| `visibility` | string |  | 可见性，public：公开；private：私有，默认public |
| `description` | string |  | 组织描述 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `web_url` | string | 是 |  |
| `name` | string | 是 |  |
| `path` | string | 是 |  |
| `description` | string | 是 |  |
| `visibility` | string | 是 |  |
| `full_name` | string | 是 |  |
| `full_path` | string | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/orgs?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","path":"string","visibility":"string","description":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-org-org

---

### 获取组织讨论列表

<a id="获取组织讨论列表"></a>

`GET /api/v5/orgs/{org}/discuss`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织path |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |
| `sort` | 否 | string | 可选。排序字段 created（创建时间）, comment_size（评论数量），默认创建时间 |
| `direction` | 否 | string | 可选。升序/降序(asc/desc)，默认降序 |
| `search` | 否 | string | 根据标题和描述搜索 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `number` | integer | 是 |  |
| `title` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `author` | object(5字段) | 是 |  |
| `is_lock` | integer | 是 | 是否锁定 |
| `is_pin` | integer | 是 | 是否置顶 |
| `is_category_pin` | integer | 是 | 是否类型下置顶 |
| `is_closed` | integer | 是 | 是否关闭 |
| `is_answered` | integer | 是 | 是否采纳答案 |
| `comment_total` | integer | 是 | 评论数量 |
| `category` | object(5字段) | 是 |  |
| `namespace` | object(3字段) | 是 |  |
| `labels` | array&lt;object(5字段)&gt; |  | 标签 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/discuss?access_token={your-token}&page={page}&per_page={per_page}&sort={sort}&direction={direction}&search={search}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org-discuss

---

### 获取组织讨论详情

<a id="获取组织讨论详情"></a>

`GET /api/v5/orgs/{org}/discuss/{number}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织path |
| `number` | **是** | string | 讨论的编号 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `number` | integer | 是 |  |
| `title` | string | 是 |  |
| `md_content` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `author` | object(5字段) | 是 |  |
| `is_lock` | integer | 是 | 是否锁定 |
| `is_pin` | integer | 是 | 是否置顶 |
| `is_category_pin` | integer | 是 | 是否类型下置顶 |
| `is_closed` | integer | 是 | 是否关闭 |
| `is_answered` | integer | 是 | 是否采纳答案 |
| `comment_total` | integer | 是 | 评论总数 |
| `category` | object(5字段) | 是 |  |
| `vote` | object(12字段) | 是 | 投票类型返回，投票信息 |
| `vote_options` | array&lt;object(9字段)&gt; | 是 | 投票类型返回 选项信息 |
| `labels` | array&lt;object(5字段)&gt; |  | 标签 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/discuss/{number}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org-discuss-number

---

### 获取组织讨论评论列表

<a id="获取组织讨论评论列表"></a>

`GET /api/v5/orgs/{org}/discuss/{number}/comment`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的path |
| `number` | **是** | string | 讨论的编号 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |
| `order` | 否 | string | 排序，按创建时间升序（time_asc）；按创建时间降序（time_desc），按热度值降序（hot_desc） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string |  |  |
| `created_at` | string |  |  |
| `author` | object(5字段) |  |  |
| `content` | string |  |  |
| `md_content` | string |  |  |
| `is_hide` | integer |  |  |
| `reply_total` | integer |  |  |
| `like_total` | integer |  |  |
| `is_like` | boolean |  |  |
| `is_deleted` | integer |  |  |
| `is_remark` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/discuss/{number}/comment?access_token={your-token}&page={page}&per_page={per_page}&order={order}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org-discuss-number-comment

---

### 获取组织讨论评论回复列表

<a id="获取组织讨论评论回复列表"></a>

`GET /api/v5/orgs/{org}/discuss/{number}/comment/{comment_id}/reply`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织path |
| `number` | **是** | string | 讨论的编号 |
| `comment_id` | **是** | string | 评论id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `page` | 否 | string | 当前的页码 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `created_at` | string | 是 |  |
| `author` | object(5字段) | 是 |  |
| `content` | string | 是 |  |
| `md_content` | string | 是 |  |
| `is_hide` | integer | 是 |  |
| `reply_total` | integer | 是 |  |
| `like_total` | integer | 是 |  |
| `is_like` | boolean | 是 |  |
| `is_deleted` | integer | 是 |  |
| `is_remark` | integer | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/orgs/{org}/discuss/{number}/comment/{comment_id}/reply?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-orgs-org-discuss-number-comment-comment-id-reply

---

### Webhooks

> 仓库 WebHook 的增删改查与测试

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/repos/{owner}/{repo}/hooks` | [列出仓库的WebHooks](#列出仓库的webhooks) |
| `POST` | `/api/v5/repos/{owner}/{repo}/hooks` | [创建一个仓库的WebHooks](#创建一个仓库的webhooks) |
| `GET` | `/api/v5/repos/{owner}/{repo}/hooks/{id}` | [获取仓库单个WebHook](#获取仓库单个webhook) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/hooks/{id}` | [更新一个仓库WebHook](#更新一个仓库webhook) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/hooks/{id}` | [删除一个仓库WebHook](#删除一个仓库webhook) |
| `POST` | `/api/v5/repos/{owner}/{repo}/hooks/{id}/tests` | [测试WebHook是否发送成功](#测试webhook是否发送成功) |

---

### 列出仓库的WebHooks

<a id="列出仓库的webhooks"></a>

`GET /api/v5/repos/{owner}/{repo}/hooks`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `url` | string |  |  |
| `password` | string |  |  |
| `result` | string |  |  |
| `project_id` | integer |  |  |
| `result_code` | integer |  |  |
| `push_events` | integer |  |  |
| `tag_push_events` | integer |  |  |
| `issues_events` | integer |  |  |
| `note_events` | integer |  |  |
| `merge_requests_events` | integer |  |  |
| `created_at` | string |  |  |
| `active` | boolean |  | 是否激活 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/hooks?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-hooks

---

### 创建一个仓库的WebHooks

<a id="创建一个仓库的webhooks"></a>

`POST /api/v5/repos/{owner}/{repo}/hooks`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 远程HTTP URL |
| `encryption_type` | integer |  | 加密类型: 0: 密码, 1: 签名密钥 |
| `password` | string |  | 请求URL时会带上该密码，防止URL被恶意请求 |
| `push_events` | boolean |  | Push代码到仓库 |
| `tag_push_events` | boolean |  | 提交Tag到仓库 |
| `issues_events` | boolean |  | 创建/关闭Issue |
| `note_events` | boolean |  | 评论了Issue/代码等等 |
| `merge_requests_events` | boolean |  | 合并请求和合并后 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `url` | string |  |  |
| `password` | string |  |  |
| `result` | null |  |  |
| `project_id` | integer |  |  |
| `result_code` | integer |  |  |
| `push_events` | integer |  |  |
| `tag_push_events` | integer |  |  |
| `issues_events` | integer |  |  |
| `note_events` | integer |  |  |
| `merge_requests_events` | integer |  |  |
| `created_at` | string |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/hooks?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"url":"string","encryption_type":0,"password":"string","push_events":true,"tag_push_events":true,"issues_events":true,"note_events":true,"merge_requests_events":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-hooks

---

### 获取仓库单个WebHook

<a id="获取仓库单个webhook"></a>

`GET /api/v5/repos/{owner}/{repo}/hooks/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | Webhook的ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `url` | string |  |  |
| `password` | string |  |  |
| `result` | null |  |  |
| `project_id` | integer |  |  |
| `result_code` | integer |  |  |
| `push_events` | integer |  |  |
| `tag_push_events` | integer |  |  |
| `issues_events` | integer |  |  |
| `note_events` | integer |  |  |
| `merge_requests_events` | integer |  |  |
| `created_at` | string |  |  |
| `active` | boolean |  | 是否激活 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/hooks/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-hooks-id

---

### 更新一个仓库WebHook

<a id="更新一个仓库webhook"></a>

`PATCH /api/v5/repos/{owner}/{repo}/hooks/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | Webhook的ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 远程HTTP URL |
| `encryption_type` | integer |  | 加密类型: 0: 密码, 1: 签名密钥 |
| `password` | string |  | 请求URL时会带上该密码，防止URL被恶意请求 |
| `push_events` | boolean |  | Push代码到仓库 |
| `tag_push_events` | boolean |  | 提交Tag到仓库 |
| `issues_events` | boolean |  | 创建/关闭Issue |
| `note_events` | boolean |  | 评论了Issue/代码等等 |
| `merge_requests_events` | boolean |  | 合并请求和合并后 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string |  |  |
| `password` | string |  |  |
| `result` | null |  |  |
| `project_id` | integer |  |  |
| `result_code` | integer |  |  |
| `push_events` | integer |  |  |
| `tag_push_events` | integer |  |  |
| `issues_events` | integer |  |  |
| `note_events` | integer |  |  |
| `merge_requests_events` | integer |  |  |
| `created_at` | string |  |  |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/hooks/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"url":"string","encryption_type":0,"password":"string","push_events":true,"tag_push_events":true,"issues_events":true,"note_events":true,"merge_requests_events":true}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-hooks-id

---

### 删除一个仓库WebHook

<a id="删除一个仓库webhook"></a>

`DELETE /api/v5/repos/{owner}/{repo}/hooks/{id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | Webhook的ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/hooks/{id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-hooks-id

---

### 测试WebHook是否发送成功

<a id="测试webhook是否发送成功"></a>

`POST /api/v5/repos/{owner}/{repo}/hooks/{id}/tests`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `id` | **是** | string | Webhook的ID |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/hooks/{id}/tests?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-hooks-id-tests

---

### 项目成员

> 协作者与成员权限管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `PUT` | `/api/v5/repos/{owner}/{repo}/collaborators/{username}` | [添加项目成员或更新项目成员权限](#添加项目成员或更新项目成员权限) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/collaborators/{username}` | [移除项目成员](#移除项目成员) |
| `GET` | `/api/v5/repos/{owner}/{repo}/collaborators/{username}` | [判断用户是否为仓库成员](#判断用户是否为仓库成员) |
| `GET` | `/api/v5/repos/{owner}/{repo}/collaborators` | [获取仓库的所有成员](#获取仓库的所有成员) |
| `GET` | `/api/v5/repos/{owner}/{repo}/collaborators/{username}/permission` | [查看仓库成员的权限](#查看仓库成员的权限) |
| `GET` | `/api/v5/repos/{owner}/{repo}/collaborators/self-permission` | [查看当前成员仓库的权限点](#查看当前成员仓库的权限点) |

---

### 添加项目成员或更新项目成员权限

<a id="添加项目成员或更新项目成员权限"></a>

`PUT /api/v5/repos/{owner}/{repo}/collaborators/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `permission` | string |  | 成员权限: 参与者(pull)，开发者(push)，仓库维护者(admin)， 自定义角色传入角色名。默认: push |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `avatar_url` | null |  |  |
| `html_url` | string |  |  |
| `remark` | string |  |  |
| `type` | string |  |  |
| `permissions` | object(3字段) |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"permission":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-repos-owner-repo-collaborators-username

---

### 移除项目成员

<a id="移除项目成员"></a>

`DELETE /api/v5/repos/{owner}/{repo}/collaborators/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-collaborators-username

---

### 判断用户是否为仓库成员

<a id="判断用户是否为仓库成员"></a>

`GET /api/v5/repos/{owner}/{repo}/collaborators/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `message` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-collaborators-username

---

### 获取仓库的所有成员

<a id="获取仓库的所有成员"></a>

`GET /api/v5/repos/{owner}/{repo}/collaborators`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string |  |  |
| `name` | string |  |  |
| `username` | string |  |  |
| `nick_name` | null |  |  |
| `state` | null |  |  |
| `avatar` | null |  |  |
| `avatar_url` | null |  |  |
| `email` | null |  |  |
| `name_cn` | null |  |  |
| `web_url` | string |  |  |
| `access_level` | null |  |  |
| `expires_at` | null |  |  |
| `limited` | null |  |  |
| `type` | string |  |  |
| `last_owner` | null |  |  |
| `is_current_source_member` | null |  |  |
| `last_source_owner` | null |  |  |
| `join_way` | null |  |  |
| `source_name` | null |  |  |
| `member_roles` | null |  |  |
| `iam_id` | null |  |  |
| `committer_system_from` | null |  |  |
| `permissions` | object(3字段) |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-collaborators

---

### 查看仓库成员的权限

<a id="查看仓库成员的权限"></a>

`GET /api/v5/repos/{owner}/{repo}/collaborators/{username}/permission`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `username` | string | 是 |  |
| `nick_name` | string | 是 |  |
| `state` | string | 是 |  |
| `email` | string | 是 |  |
| `web_url` | string | 是 |  |
| `access_level` | integer | 是 |  |
| `type` | string | 是 |  |
| `join_way` | string | 是 |  |
| `source_name` | string | 是 |  |
| `role_name` | string | 是 |  |
| `role_name_cn` | string | 是 |  |
| `permissions` | object(1字段) | 是 |  |
| `object_id` | string | 是 |  |
| `permission` | string | 是 |  |
| `login` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators/{username}/permission?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-collaborators-username-permission

---

### 查看当前成员仓库的权限点

<a id="查看当前成员仓库的权限点"></a>

`GET /api/v5/repos/{owner}/{repo}/collaborators/self-permission`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `role_info` | object(5字段) | 是 | 用户角色 |
| `resource_trees` | array&lt;object(8字段)&gt; | 是 | 用户权限矩阵树 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators/self-permission?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-collaborators-self-permission

---

### Release 管理

> 发行版、附件上传下载

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v5/repos/{owner}/{repo}/releases` | [创建仓库Release](#创建仓库release) |
| `PATCH` | `/api/v5/repos/{owner}/{repo}/releases/{tag}` | [更新仓库Release](#更新仓库release) |
| `GET` | `/api/v5/repos/{owner}/{repo}/releases/{tag}/upload_url` | [获取Release附件上传地址](#获取release附件上传地址) |
| `GET` | `/api/v5/repos/{owner}/{repo}/releases/{tag}` | [获取仓库的单个Releases](#获取仓库的单个releases) |
| `GET` | `/api/v5/repos/{owner}/{repo}/releases/latest` | [获取仓库的最新的Release](#获取仓库的最新的release) |
| `GET` | `/api/v5/repos/{owner}/{repo}/releases/tags/{tag}` | [根据Tag名称获取仓库的Release](#根据tag名称获取仓库的release) |
| `GET` | `/api/v5/repos/{owner}/{repo}/releases` | [获取仓库的所有Releases](#获取仓库的所有releases) |
| `GET` | `/api/v5/repos/{owner}/{repo}/releases/{tag}/attach_files/{file_name}/download` | [下载仓库release附件](#下载仓库release附件) |
| `DELETE` | `/api/v5/repos/{owner}/{repo}/releases/{tag}/attach_files/{attach_file_id}` | [删除Release附件](#删除release附件) |

---

### 创建仓库Release

<a id="创建仓库release"></a>

`POST /api/v5/repos/{owner}/{repo}/releases`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tag_name` | string | 是 | tag名称 |
| `name` | string | 是 | release名称 |
| `body` | string | 是 | release描述 |
| `target_commitish` | string |  | 分支名称或者commit SHA，如果tag不存在，需要新建tag则传入该参数，如果不传入该参数，则为默认分支的最新提交 |
| `release_status` | string |  | Release状态。pre（预发布版本），latest（最新版本） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tag_name` | string | 是 |  |
| `target_commitish` | string | 是 |  |
| `prerelease` | boolean | 是 |  |
| `name` | string | 是 |  |
| `body` | string | 是 |  |
| `author` | object(7字段) | 是 |  |
| `created_at` | string | 是 |  |
| `assets` | array&lt;object(3字段)&gt; | 是 |  |
| `release_status` | string | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"tag_name":"string","name":"string","body":"string","target_commitish":"string","release_status":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-repos-owner-repo-releases

---

### 更新仓库Release

<a id="更新仓库release"></a>

`PATCH /api/v5/repos/{owner}/{repo}/releases/{tag}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `tag` | **是** | string | Tag 名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**Header 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `Content-Type` | 否 | string |  （示例: `application/json`） |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Release 名称 |
| `body` | string | 是 | Release 描述	 |
| `release_status` | string |  | Release状态。pre（预发布版本），latest（最新版本） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tag_name` | string | 是 |  |
| `target_commitish` | string | 是 |  |
| `prerelease` | boolean | 是 |  |
| `name` | string | 是 |  |
| `body` | string | 是 |  |
| `author` | object(7字段) | 是 |  |
| `created_at` | string | 是 |  |
| `assets` | array&lt;object(3字段)&gt; | 是 |  |
| `release_status` | string | 是 |  |

**请求示例**

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases/{tag}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","body":"string","release_status":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/patch-api-v-5-repos-owner-repo-releases-tag

---

### 获取Release附件上传地址

<a id="获取release附件上传地址"></a>

`GET /api/v5/repos/{owner}/{repo}/releases/{tag}/upload_url`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径 |
| `tag` | **是** | string | tag名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `file_name` | **是** | string | 要上传的文件名称 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 上传的地址，使用put请求 |
| `headers` | object(4字段) | 是 | 上传的请求需要携带的请求头 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases/{tag}/upload_url?access_token={your-token}&file_name={file_name}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-releases-tag-upload-url

---

### 获取仓库的单个Releases

<a id="获取仓库的单个releases"></a>

`GET /api/v5/repos/{owner}/{repo}/releases/{tag}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `tag` | **是** | string | Tag 名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `temp_download_url` | 否 | string | 是否返回源码包和附件临时下载地址，默认false |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tag_name` | string | 是 |  |
| `target_commitish` | string | 是 |  |
| `prerelease` | boolean | 是 |  |
| `name` | string | 是 |  |
| `body` | string | 是 |  |
| `author` | object(7字段) | 是 |  |
| `created_at` | string | 是 |  |
| `assets` | array&lt;object(4字段)&gt; | 是 |  |
| `release_status` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases/{tag}?access_token={your-token}&temp_download_url={temp_download_url}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-releases-tag

---

### 获取仓库的最新的Release

<a id="获取仓库的最新的release"></a>

`GET /api/v5/repos/{owner}/{repo}/releases/latest`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `type` | 否 | string | 类型。updated（最后更新的），latest（最新的） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tag_name` | string | 是 |  |
| `target_commitish` | string | 是 |  |
| `prerelease` | boolean | 是 |  |
| `name` | string | 是 |  |
| `body` | string | 是 |  |
| `author` | object(7字段) | 是 |  |
| `created_at` | string | 是 |  |
| `assets` | array&lt;object(4字段)&gt; | 是 |  |
| `release_status` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases/latest?access_token={your-token}&type={type}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-releases-latest

---

### 根据Tag名称获取仓库的Release

<a id="根据tag名称获取仓库的release"></a>

`GET /api/v5/repos/{owner}/{repo}/releases/tags/{tag}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |
| `tag` | **是** | string | Tag 名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tag_name` | string | 是 |  |
| `target_commitish` | string | 是 |  |
| `prerelease` | boolean | 是 |  |
| `name` | string | 是 |  |
| `body` | string | 是 |  |
| `author` | object(7字段) | 是 |  |
| `created_at` | string | 是 |  |
| `assets` | array&lt;object(4字段)&gt; | 是 |  |
| `release_status` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases/tags/{tag}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-releases-tags-tag

---

### 获取仓库的所有Releases

<a id="获取仓库的所有releases"></a>

`GET /api/v5/repos/{owner}/{repo}/releases`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `direction` | 否 | string | 可选。升序/降序。不填为升序 |
| `page` | 否 | integer | 当前的页码 |
| `per_page` | 否 | integer | 每页的数量，最大为100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tag_name` | string | 是 |  |
| `target_commitish` | string | 是 |  |
| `prerelease` | boolean | 是 |  |
| `name` | string | 是 |  |
| `body` | string | 是 |  |
| `author` | object(7字段) | 是 |  |
| `created_at` | string | 是 |  |
| `assets` | array&lt;object(4字段)&gt; | 是 |  |
| `release_status` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases?access_token={your-token}&direction={direction}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-releases

---

### 下载仓库release附件

<a id="下载仓库release附件"></a>

`GET /api/v5/repos/{owner}/{repo}/releases/{tag}/attach_files/{file_name}/download`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(企业、组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `tag` | **是** | string | 标签名称 |
| `file_name` | **是** | string | 附件名称 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases/{tag}/attach_files/{file_name}/download?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-repos-owner-repo-releases-attach-files-file-name-download

---

### 删除Release附件

<a id="删除release附件"></a>

`DELETE /api/v5/repos/{owner}/{repo}/releases/{tag}/attach_files/{attach_file_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址（企业、组织或个人的地址path） |
| `repo` | **是** | string | 仓库路径 |
| `tag` | **是** | string | tag名称 |
| `attach_file_id` | **是** | string | 附件的id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `204` |  |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/releases/{tag}/attach_files/{attach_file_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-repos-owner-repo-releases-tag-attach-files-attach-file-id

---

### 企业管理

> 企业成员、里程碑、自定义字段、标签（v8）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v8/enterprises/{enterprise}/members/{username}` | [获取企业的一个成员](#获取企业的一个成员-1) |
| `GET` | `/api/v8/enterprises/{enterprise}/members` | [列出企业的所有成员](#列出企业的所有成员-1) |
| `POST` | `/api/v8/enterprises/{enterprise}/memberships/{username}` | [邀请企业成员](#邀请企业成员) |
| `DELETE` | `/api/v8/enterprises/{enterprise}/members/{username}` | [删除企业成员](#删除企业成员) |
| `PUT` | `/api/v8/enterprises/{enterprise}/members/{username}` | [修改企业成员权限](#修改企业成员权限-1) |
| `GET` | `/api/v8/org/{org}/enterprise` | [获取组织关联的企业](#获取组织关联的企业) |
| `GET` | `/api/v8/enterprise/{enterprise_id}/customized_roles` | [获取企业自定义角色](#获取企业自定义角色) |
| `POST` | `/api/v8/enterprises/{enterprise}/milestones` | [创建企业里程碑](#创建企业里程碑) |
| `PUT` | `/api/v8/enterprises/{enterprise}/milestones/{milestone_id}` | [修改企业里程碑](#修改企业里程碑) |
| `GET` | `/api/v8/enterprises/{enterprise}/milestones/{milestone_id}` | [获取企业里程碑详情](#获取企业里程碑详情) |
| `DELETE` | `/api/v8/enterprises/{enterprise}/milestones/{milestone_id}` | [删除企业里程碑](#删除企业里程碑) |
| `GET` | `/api/v8/enterprises/{enterprise}/milestones` | [获取企业里程碑列表](#获取企业里程碑列表) |
| `GET` | `/api/v8/enterprises/{enterprise}/groups/projects` | [获取企业里程碑可以关联的项目列表](#获取企业里程碑可以关联的项目列表) |
| `GET` | `/api/v8/enterprises/{enterprise_id}/issue_extend_field` | [获取企业Issue自定义字段列表](#获取企业issue自定义字段列表) |
| `POST` | `/api/v8/enterprises/{enterprise_id}/issues` | [获取企业Issue列表](#获取企业issue列表) |
| `POST` | `/api/v8/enterprises/{enterprise_id}/label` | [创建企业标签](#创建企业标签) |
| `PUT` | `/api/v8/enterprises/{enterprise_id}/label/{label_id}` | [更新企业标签](#更新企业标签) |
| `DELETE` | `/api/v8/enterprises/{enterprise_id}/label/{label_id}` | [删除企业标签](#删除企业标签) |

---

### 获取企业的一个成员

<a id="获取企业的一个成员-1"></a>

`GET /api/v8/enterprises/{enterprise}/members/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | integer | 企业id |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user` | object(8字段) | 是 |  |
| `active` | boolean | 是 |  |
| `role` | string | 是 |  |
| `role_name` | string | 是 |  |
| `role_id` | string | 是 |  |
| `role_name_cn` | string | 是 |  |
| `enterprise_email` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/enterprises/{enterprise}/members/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-enterprises-enterprise-members-username

---

### 列出企业的所有成员

<a id="列出企业的所有成员-1"></a>

`GET /api/v8/enterprises/{enterprise}/members`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | integer | 企业id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | integer | 当前的页码:默认为 1 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `role` | 否 | string | 根据角色筛选成员(all/admin/member) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user` | object(8字段) | 是 |  |
| `active` | boolean | 是 |  |
| `role` | string | 是 |  |
| `role_name` | string | 是 |  |
| `role_id` | string | 是 |  |
| `role_name_cn` | string | 是 |  |
| `enterprise_email` | string |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/enterprises/{enterprise}/members?access_token={your-token}&page={page}&per_page={per_page}&role={role}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-enterprises-enterprise-members

---

### 邀请企业成员

<a id="邀请企业成员"></a>

`POST /api/v8/enterprises/{enterprise}/memberships/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | integer | 企业id |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `permission` | string |  | 成员权限: 参与者(pull)，开发者(push)，维护者(admin)，customized(自定义角色)。默认: push |
| `role_id` | string |  | 角色ID, 如果permission传入了customized则需要传入角色id |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `followers_url` | string |  |  |
| `html_url` | string |  |  |
| `id` | string |  |  |
| `login` | string |  |  |
| `name` | string |  |  |
| `type` | string |  |  |
| `url` | string |  |  |
| `permissions` | object(4字段) |  |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v8/enterprises/{enterprise}/memberships/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"permission":"string","role_id":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-8-enterprises-enterprise-memberships-username

---

### 删除企业成员

<a id="删除企业成员"></a>

`DELETE /api/v8/enterprises/{enterprise}/members/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | integer | 企业id |
| `username` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v8/enterprises/{enterprise}/members/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-8-enterprises-enterprise-members-usernames

---

### 修改企业成员权限

<a id="修改企业成员权限-1"></a>

`PUT /api/v8/enterprises/{enterprise}/members/{username}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | integer | 企业id |
| `username` | **是** | string | 用户名(username/login) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `role` | string | 是 | 企业角色(viewer:浏览者、tester:测试人、developer:研发、maintainer:维护者、admin:管理员、customized:自定义角色) |
| `role_id` | string |  | 角色ID, 如果role传入了customized则需要传入角色id |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `active` | integer |  |  |
| `role` | string |  |  |
| `url` | string |  |  |
| `user` | object(5字段) |  |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v8/enterprises/{enterprise}/members/{username}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"role":"string","role_id":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-8-enterprises-enterprise-members-username

---

### 获取组织关联的企业

<a id="获取组织关联的企业"></a>

`GET /api/v8/org/{org}/enterprise`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织的路径 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `name` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/org/{org}/enterprise?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-org-org-enterprise

---

### 获取企业自定义角色

<a id="获取企业自定义角色"></a>

`GET /api/v8/enterprise/{enterprise_id}/customized_roles`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise_id` | **是** | string | 企业id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string |  |  |
| `name` | string |  |  |
| `name_cn` | string |  |  |
| `product_id` | string |  |  |
| `access_level` | integer |  |  |
| `created_at` | string |  |  |
| `role_type` | string |  |  |
| `updated_at` | string |  |  |
| `description` | string |  |  |
| `mgnt_mode` | integer |  |  |
| `member_count` | integer |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/enterprise/{enterprise_id}/customized_roles?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-enterprise-enterprise-id-customized-roles

---

### 创建企业里程碑

<a id="创建企业里程碑"></a>

`POST /api/v8/enterprises/{enterprise}/milestones`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 名称 |
| `description` | string |  | 描述 |
| `start_date` | string |  | 开始时间, 格式%Y-%m-%d |
| `due_date` | string |  | 截止时间, 格式%Y-%m-%d |
| `projects` | array&lt;integer&gt; |  | 关联的项目 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `title` | string | 是 |  |
| `description` | string | 是 |  |
| `state` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `due_date` | string |  |  |
| `start_date` | string |  |  |
| `projects` | array&lt;string&gt; | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v8/enterprises/{enterprise}/milestones?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"title":"string","description":"string","start_date":"string","due_date":"string","projects":[0]}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-8-enterprise-enterprise-id-milestones

---

### 修改企业里程碑

<a id="修改企业里程碑"></a>

`PUT /api/v8/enterprises/{enterprise}/milestones/{milestone_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业id |
| `milestone_id` | **是** | string | 里程碑id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `title` | string | 是 | 名称 |
| `description` | string |  | 描述 |
| `start_date` | string |  | 开始时间, 格式%Y-%m-%d |
| `due_date` | string |  | 截止时间, 格式%Y-%m-%d |
| `state_event` | string |  | 里程碑状态。开启：active；关闭：closed |
| `projects` | array&lt;integer&gt; |  | 关联的项目 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `title` | string | 是 |  |
| `description` | string | 是 |  |
| `state` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `due_date` | string |  |  |
| `start_date` | string |  |  |
| `projects` | array&lt;string&gt; | 是 |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v8/enterprises/{enterprise}/milestones/{milestone_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"title":"string","description":"string","start_date":"string","due_date":"string","state_event":"string","projects":[0]}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-8-enterprise-enterprise-id-milestones-milestone-id

---

### 获取企业里程碑详情

<a id="获取企业里程碑详情"></a>

`GET /api/v8/enterprises/{enterprise}/milestones/{milestone_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业id |
| `milestone_id` | **是** | string | 里程碑id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `title` | string | 是 |  |
| `description` | string | 是 |  |
| `state` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `due_date` | string |  |  |
| `start_date` | string |  |  |
| `projects` | array&lt;string&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/enterprises/{enterprise}/milestones/{milestone_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-enterprise-enterprise-id-milestones-milestone-id

---

### 删除企业里程碑

<a id="删除企业里程碑"></a>

`DELETE /api/v8/enterprises/{enterprise}/milestones/{milestone_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业id |
| `milestone_id` | **是** | string | 里程碑id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v8/enterprises/{enterprise}/milestones/{milestone_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-8-enterprise-enterprise-id-milestones-milestone-id

---

### 获取企业里程碑列表

<a id="获取企业里程碑列表"></a>

`GET /api/v8/enterprises/{enterprise}/milestones`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `name` | 否 | string | 名称 |
| `state` | 否 | string | 状态 |
| `order_by` | 否 | string | 排序字段。可选：updated_at |
| `sort` | 否 | string | 排序方式。降序：desc；升序：asc |
| `page` | 否 | string | 当前的页码:默认为 1 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `title` | string | 是 |  |
| `description` | string | 是 |  |
| `state` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |
| `due_date` | string |  |  |
| `start_date` | string |  |  |
| `projects` | array&lt;object(9字段)&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/enterprises/{enterprise}/milestones?access_token={your-token}&name={name}&state={state}&order_by={order_by}&sort={sort}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-enterprise-enterprise-id-milestones

---

### 获取企业里程碑可以关联的项目列表

<a id="获取企业里程碑可以关联的项目列表"></a>

`GET /api/v8/enterprises/{enterprise}/groups/projects`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise` | **是** | string | 企业id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `group_name` | 否 | string | 组织名称 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `full_name` | string | 是 |  |
| `human_name` | string | 是 |  |
| `path` | string | 是 |  |
| `name` | string | 是 |  |
| `created_at` | string | 是 |  |
| `updated_at` | string | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/enterprises/{enterprise}/groups/projects?access_token={your-token}&group_name={group_name}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-enterprise-enterprise-id-groups-projects

---

### 获取企业Issue自定义字段列表

<a id="获取企业issue自定义字段列表"></a>

`GET /api/v8/enterprises/{enterprise_id}/issue_extend_field`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise_id` | **是** | string | 企业id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |
| `page` | 否 | string | 当前的页码:默认为 1 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 | id |
| `name` | string | 是 | 字段名称 |
| `description` | string | 是 | 字段描述 |
| `field_type` | string | 是 | 自定义字段的类型。text_single：单行文本，text_multi：多行文本，integer：整数，decimal：小数，date：日期，select_single：单选下拉，select_multi：多选下拉，member_single：单选成员，member_multi：多选成员 |
| `field_visibility` | integer | 是 | 可见性。0：仅管理员可见，1：所有人可见 |
| `created_time` | string | 是 | 创建时间 |
| `updated_time` | string | 是 | 更新时间 |
| `options` | array&lt;object(6字段)&gt; | 是 | 选项明细，仅选项类型返回 |
| `types` | array&lt;object(6字段)&gt; | 是 | 该字段关联的issue类型列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/enterprises/{enterprise_id}/issue_extend_field?access_token={your-token}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-enterprises-enterprises-id-issue-extend-field

---

### 获取企业Issue列表

<a id="获取企业issue列表"></a>

`POST /api/v8/enterprises/{enterprise_id}/issues`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise_id` | **是** | string | 企业id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `page` | integer |  | 当前的页码:默认为 1 |
| `per_page` | integer |  | 每页的数量，最大为 100，默认 20 |
| `labels` | string |  | 标签，多个按英文逗号隔开 |
| `sort` | string |  | 排序依据: 创建时间(created)，更新时间(updated_at)。默认: created_at |
| `direction` | string |  | 排序方式: 升序(asc)，降序(desc)。默认: desc |
| `since` | string |  | 起始的更新时间，要求时间格式为 ISO 8601 |
| `assignees` | array&lt;string&gt; |  | 负责人的用户名 |
| `milestone_ids` | array&lt;integer&gt; |  | 企业里程碑id |
| `project_ids` | array&lt;integer&gt; |  | 项目id |
| `create_at` | string |  | 任务创建日期，格式2024-11-09 |
| `created_before` | string |  | 任务创建截止时间，格式2024-11-09 |
| `search` | string |  | 通过关键字搜索issue标题 |
| `issue_types` | array&lt;string&gt; |  | issue自定义类型 |
| `issue_states` | array&lt;string&gt; |  | issue自定义状态 |
| `custom_fields` | array&lt;object(3字段)&gt; |  |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  | id |
| `html_url` | string |  |  |
| `number` | string |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `user` | object(4字段) |  | 作者 |
| `repository` | object(8字段) |  | 仓库信息 |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `finished_at` | string |  |  |
| `deadline` | string |  |  |
| `labels` | array&lt;object(3字段)&gt; |  |  |
| `issue_state` | string |  | issue自定义状态 |
| `comments` | integer |  |  |
| `custom_fields` | array&lt;object(2字段)&gt; |  | issue自定义字段 |
| `issue_type` | string |  | issue类型 |
| `issue_priority` | string |  | issue优先级 |
| `url` | string |  |  |
| `repository_url` | string |  |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |
| `related_branches` | array&lt;string&gt; |  | 关联的分支列表 |
| `linked_pulls` | array&lt;object(17字段)&gt; |  | 关联的pull_request列表 |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v8/enterprises/{enterprise_id}/issues?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"page":0,"per_page":0,"labels":"string","sort":"string","direction":"string","since":"string","assignees":["string"],"milestone_ids":[0],"project_ids":[0],"create_at":"string","created_before":"string","search":"string","issue_types":["string"],"issue_states":["string"],"custom_fields":[{"field_name":"string","values":["string"],"operation":"string"}]}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-8-enterprises-enterprises-id-issues

---

### 创建企业标签

<a id="创建企业标签"></a>

`POST /api/v8/enterprises/{enterprise_id}/label`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise_id` | **是** | integer | 企业id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 标签名称 |
| `color` | string |  | 标签的颜色，以6位16进制表示，前面带有'#'号 |
| `description` | string | 是 | 标签描述 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  | id |
| `html_url` | string |  |  |
| `number` | string |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `user` | object(4字段) |  | 作者 |
| `repository` | object(8字段) |  | 仓库信息 |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `finished_at` | string |  |  |
| `deadline` | string |  |  |
| `labels` | array&lt;object(3字段)&gt; |  |  |
| `issue_state` | string |  | issue自定义状态 |
| `comments` | integer |  |  |
| `custom_fields` | array&lt;object(2字段)&gt; |  | issue自定义字段 |
| `issue_type` | string |  | issue类型 |
| `issue_priority` | string |  | issue优先级 |
| `url` | string |  |  |
| `repository_url` | string |  |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |
| `related_branches` | array&lt;string&gt; |  | 关联的分支列表 |
| `linked_pulls` | array&lt;object(17字段)&gt; |  | 关联的pull_request列表 |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v8/enterprises/{enterprise_id}/label?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","color":"string","description":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-8-enterprises-enterprises-id-label

---

### 更新企业标签

<a id="更新企业标签"></a>

`PUT /api/v8/enterprises/{enterprise_id}/label/{label_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise_id` | **是** | integer | 企业id |
| `label_id` | **是** | string | 标签id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 标签名称 |
| `color` | string |  | 标签的颜色，以6位16进制表示，前面带有'#'号 |
| `description` | string | 是 | 标签描述 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  | id |
| `html_url` | string |  |  |
| `number` | string |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `user` | object(4字段) |  | 作者 |
| `repository` | object(8字段) |  | 仓库信息 |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `finished_at` | string |  |  |
| `deadline` | string |  |  |
| `labels` | array&lt;object(3字段)&gt; |  |  |
| `issue_state` | string |  | issue自定义状态 |
| `comments` | integer |  |  |
| `custom_fields` | array&lt;object(2字段)&gt; |  | issue自定义字段 |
| `issue_type` | string |  | issue类型 |
| `issue_priority` | string |  | issue优先级 |
| `url` | string |  |  |
| `repository_url` | string |  |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |
| `related_branches` | array&lt;string&gt; |  | 关联的分支列表 |
| `linked_pulls` | array&lt;object(17字段)&gt; |  | 关联的pull_request列表 |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v8/enterprises/{enterprise_id}/label/{label_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"name":"string","color":"string","description":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-8-enterprises-enterprises-id-label-label-id

---

### 删除企业标签

<a id="删除企业标签"></a>

`DELETE /api/v8/enterprises/{enterprise_id}/label/{label_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `enterprise_id` | **是** | integer | 企业id |
| `label_id` | **是** | string | 标签id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer |  | id |
| `html_url` | string |  |  |
| `number` | string |  |  |
| `state` | string |  |  |
| `title` | string |  |  |
| `body` | string |  |  |
| `user` | object(4字段) |  | 作者 |
| `repository` | object(8字段) |  | 仓库信息 |
| `created_at` | string |  |  |
| `updated_at` | string |  |  |
| `finished_at` | string |  |  |
| `deadline` | string |  |  |
| `labels` | array&lt;object(3字段)&gt; |  |  |
| `issue_state` | string |  | issue自定义状态 |
| `comments` | integer |  |  |
| `custom_fields` | array&lt;object(2字段)&gt; |  | issue自定义字段 |
| `issue_type` | string |  | issue类型 |
| `issue_priority` | string |  | issue优先级 |
| `url` | string |  |  |
| `repository_url` | string |  |  |
| `visibility_reason` | string |  | 可见性，public：公开可见，confidential：私密，项目成员可见，other：其他原因导致的仅项目成员可见 |
| `related_branches` | array&lt;string&gt; |  | 关联的分支列表 |
| `linked_pulls` | array&lt;object(17字段)&gt; |  | 关联的pull_request列表 |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v8/enterprises/{enterprise_id}/label/{label_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-8-enterprises-enterprises-id-label-label-id

---

### 看板

> 组织看板与看板条目管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/v5/org/{owner}/kanban/list` | [获取组织看板列表](#获取组织看板列表) |
| `GET` | `/api/v5/org/{owner}/kanban/{kanban_id}/detail` | [获取单个看板详情](#获取单个看板详情) |
| `POST` | `/api/v5/org/{owner}/kanban/{kanban_id}/add_item` | [添加Issue或者Pull Request到看板](#添加issue或者pull-request到看板) |
| `PUT` | `/api/v5/org/{owner}/kanban/repo/{repo}/{type}/{iid}` | [更新Issue或者Pull Request关联的看板](#更新issue或者pull-request关联的看板) |
| `DELETE` | `/api/v5/org/{owner}/kanban/{kanban_id}/remove_item` | [删除看板关联的Issue或者Pull Request](#删除看板关联的issue或者pull-request) |
| `GET` | `/api/v5/org/{owner}/kanban/{kanban_id}/item_list` | [获取看板内容列表](#获取看板内容列表) |
| `PUT` | `/api/v5/org/{owner}/kanban/{kanban_id}/state` | [修改看板状态](#修改看板状态) |

---

### 获取组织看板列表

<a id="获取组织看板列表"></a>

`GET /api/v5/org/{owner}/kanban/list`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 组织的路径 |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `status` | 否 | integer | 状态 0: 正常 1:关闭，默认不传为全部 |
| `sort` | 否 | string | 最新: newest,  最老: oldest  |
| `visibility` | 否 | integer | 可见性 1:公开的 2: 私密的 |
| `search` | 否 | string | 按照名称搜索 |
| `page` | 否 | string | 当前页码 |
| `per_page` | 否 | string | 每页数量 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `close_count` | integer | 是 |  |
| `open_count` | integer | 是 |  |
| `all_count` | integer | 是 |  |
| `content` | array&lt;object(7字段)&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/org/{owner}/kanban/list?access_token={your-token}&status={status}&sort={sort}&visibility={visibility}&search={search}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-org-owner-kanban-list

---

### 获取单个看板详情

<a id="获取单个看板详情"></a>

`GET /api/v5/org/{owner}/kanban/{kanban_id}/detail`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 组织的路径 |
| `kanban_id` | **是** | string | 看板id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 |  |
| `name` | string | 是 |  |
| `description` | string | 是 |  |
| `status` | integer | 是 |  |
| `visibility` | integer | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/org/{owner}/kanban/{kanban_id}/detail?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-org-owner-kanban-id-detail

---

### 添加Issue或者Pull Request到看板

<a id="添加issue或者pull-request到看板"></a>

`POST /api/v5/org/{owner}/kanban/{kanban_id}/add_item`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 组织的路径 |
| `kanban_id` | **是** | string | 看板id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `repo` | string | 是 | 仓库的路径 |
| `issue_iids` | array&lt;integer&gt; | 是 | 仓库内issue的iid |
| `pr_iids` | array&lt;integer&gt; | 是 | 仓库内pull request的iid |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/org/{owner}/kanban/{kanban_id}/add_item?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"repo":"string","issue_iids":[0],"pr_iids":[0]}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-org-owner-kanban-id-add-item

---

### 更新Issue或者Pull Request关联的看板

<a id="更新issue或者pull-request关联的看板"></a>

`PUT /api/v5/org/{owner}/kanban/repo/{repo}/{type}/{iid}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 组织的路径 |
| `repo` | **是** | string | 仓库的路径 |
| `type` | **是** | string | 类型，issue/merge_request |
| `iid` | **是** | string | issue或者pull request的iid |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `kanban_id` | string | 是 | 看板id |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/org/{owner}/kanban/repo/{repo}/{type}/{iid}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"kanban_id":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-org-owner-kanban-repo-repo-type-iid-new

---

### 删除看板关联的Issue或者Pull Request

<a id="删除看板关联的issue或者pull-request"></a>

`DELETE /api/v5/org/{owner}/kanban/{kanban_id}/remove_item`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 组织的路径 |
| `kanban_id` | **是** | string | 看板id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `repo` | string | 是 | 仓库的路径 |
| `issue_iids` | array&lt;integer&gt; | 是 | 仓库内issue的iid |
| `pr_iids` | array&lt;integer&gt; | 是 | 仓库内pull request的iid |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|


**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v5/org/{owner}/kanban/{kanban_id}/remove_item?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '[{"repo":"string","issue_iids":[0],"pr_iids":[0]}]'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-5-org-owner-kanban-kanban-id-remove-item-new

---

### 获取看板内容列表

<a id="获取看板内容列表"></a>

`GET /api/v5/org/{owner}/kanban/{kanban_id}/item_list`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 组织的路径 |
| `kanban_id` | **是** | string | 看板id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `repo` | 否 | string | 仓库路径 |
| `source_type` | 否 | string | 来源类型：issue/merge_request |
| `source_status` | 否 | string | 来源状态：opened/closed/merged |
| `title` | 否 | string | 标题 |
| `source_iids` | 否 | string | 使用pr或者issue的iid进行查询, 使用该参数必须填写repo参数，否则不生效 |
| `page` | 否 | string | 分页参数，第几页 |
| `per_page` | 否 | string | 分页参数，每页数量 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | integer | 是 |  |
| `number` | integer | 是 |  |
| `title` | string | 是 |  |
| `source_type` | string | 是 |  |
| `status` | string | 是 |  |
| `html_url` | string | 是 |  |
| `url` | string | 是 |  |
| `repo` | object(5字段) | 是 |  |
| `labels` | array&lt;object(3字段)&gt; | 是 |  |
| `assignees` | array&lt;object(6字段)&gt; | 是 |  |
| `milestone_resp` | object(4字段) |  |  |
| `linked_pull_requests` | array&lt;object(6字段)&gt; |  |  |
| `values` | array&lt;object(3字段)&gt; | 是 |  |
| `reviewers` | array&lt;object(6字段)&gt; |  |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v5/org/{owner}/kanban/{kanban_id}/item_list?access_token={your-token}&repo={repo}&source_type={source_type}&source_status={source_status}&title={title}&source_iids={source_iids}&page={page}&per_page={per_page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-5-org-owner-kanban-kanban-id-item-list

---

### 修改看板状态

<a id="修改看板状态"></a>

`PUT /api/v5/org/{owner}/kanban/{kanban_id}/state`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 组织的路径 |
| `kanban_id` | **是** | string | 看板id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `state` | string | 是 | 看板状态，关闭：closed；开启：open |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**请求示例**

```bash
curl -X PUT "https://api.gitcode.com/api/v5/org/{owner}/kanban/{kanban_id}/state?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"state":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/put-api-v-5-org-owner-kanban-kanban-id-state

---

### 流水线 / Actions

> Workflow、运行记录、日志、Artifact、Runner（v8）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v8/repos/{owner}/{repo}/actions/workflows/validate` | [校验 Workflow YAML](#校验-workflow-yaml) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/workflows` | [获取仓库的流水线列表](#获取仓库的流水线列表) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/runs` | [获取仓库所有的流水线的运行记录](#获取仓库所有的流水线的运行记录) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}` | [获取流水线的运行详情](#获取流水线的运行详情) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs` | [获取工作流运行的jobs列表](#获取工作流运行的jobs列表) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}` | [获取工作流运行的job详情](#获取工作流运行的job详情) |
| `POST` | `/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/logs` | [查询工作流Job的Step级日志](#查询工作流job的step级日志) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/download_log` | [下载工作流运行的job日志](#下载工作流运行的job日志) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/artifacts` | [列出仓库的 Artifacts](#列出仓库的-artifacts) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts` | [列出特定 Run 的 Artifacts](#列出特定-run-的-artifacts) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/artifacts/{artifact_id}` | [获取 Artifact 详情](#获取-artifact-详情) |
| `DELETE` | `/api/v8/repos/{owner}/{repo}/actions/artifacts/{artifact_id}` | [删除指定 Artifact](#删除指定-artifact) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/{archive_format}` | [下载指定 Artifact](#下载指定-artifact) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/runners` | [查询指定仓库下的所有主机 Runner](#查询指定仓库下的所有主机-runner) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/runners/shared-runners` | [查询分享给仓库的所有主机 Runner](#查询分享给仓库的所有主机-runner) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/runner-sets` | [查询指定仓库下的所有 K8S Runner](#查询指定仓库下的所有-k8s-runner) |
| `GET` | `/api/v8/repos/{owner}/{repo}/actions/shared-runner-sets` | [查询分享给仓库的所有 K8S Runner](#查询分享给仓库的所有-k8s-runner) |
| `GET` | `/api/v8/orgs/{org}/actions/runner-groups` | [查询指定组织下的所有 Runner Group](#查询指定组织下的所有-runner-group) |
| `GET` | `/api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}` | [获取指定 Runner Group 的详细信息](#获取指定-runner-group-的详细信息) |
| `GET` | `/api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}/runners` | [查询指定 Runner Group 下的所有主机 Runner](#查询指定-runner-group-下的所有主机-runner) |
| `GET` | `/api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}/runner-sets` | [查询指定 Runner Group 下的所有 K8S Runner](#查询指定-runner-group-下的所有-k8s-runner) |
| `GET` | `/api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}/shared-namespaces` | [查询有权访问指定 Runner Group 的仓库列表](#查询有权访问指定-runner-group-的仓库列表) |

---

### 校验 Workflow YAML

<a id="校验-workflow-yaml"></a>

`POST /api/v8/repos/{owner}/{repo}/actions/workflows/validate`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `base64_content` | string | 是 | yaml文件内容，需要base64编码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `valid` | boolean | 是 | 校验结果 |
| `diagnostics` | array&lt;object(3字段)&gt; | 是 | 失败原因 |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/workflows/validate?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"base64_content":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-8-repos-owner-repo-actions-workflows-validate

---

### 获取仓库的流水线列表

<a id="获取仓库的流水线列表"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/workflows`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `per_page` | 否 | integer | 每页的数量，最大为 100，默认 20 |
| `page` | 否 | integer | 当前的页码:默认为 1 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer | 是 | 总数量 |
| `workflows` | array&lt;object(4字段)&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/workflows?access_token={your-token}&per_page={per_page}&page={page}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-workflows

---

### 获取仓库所有的流水线的运行记录

<a id="获取仓库所有的流水线的运行记录"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/runs`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `event` | 否 | string | 触发事件类型，MR（mr事件），Push（推送事件），Manual（手动触发） |
| `status` | 否 | string | 运行状态，COMPLETED（已完成），RUNNING（运行中），FAILED（失败），CANCELED（取消），IGNORED（忽略），PAUSED（暂停），SUSPEND（挂起） |
| `branch` | 否 | string | 分支过滤 |
| `executor` | 否 | string | 触发人用户名 |
| `pull_request_id` | 否 | string | PR的编号 |
| `workflow_id` | 否 | string | 流水线id |
| `workflow_name` | 否 | string | 流水线名称 |
| `per_page` | 否 | string | 每页的数量，最大为 100，默认 20 |
| `page` | 否 | string | 当前的页码:默认为 1 |
| `startTime` | 否 | number | 开始时间，格式时间戳 |
| `endTime` | 否 | number | 结束时间，格式时间戳 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer | 是 | 总数量 |
| `workflow_runs` | array&lt;object(14字段)&gt; | 是 | 流水线运行记录 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runs?access_token={your-token}&event={event}&status={status}&branch={branch}&executor={executor}&pull_request_id={pull_request_id}&workflow_id={workflow_id}&workflow_name={workflow_name}&per_page={per_page}&page={page}&startTime={startTime}&endTime={endTime}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-runs

---

### 获取流水线的运行详情

<a id="获取流水线的运行详情"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/runs/{run_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `run_id` | **是** | string | 流水线运行id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `workflow_run_id` | string | 是 | 流水线运行id |
| `workflow_id` | string | 是 | 流水线id |
| `workflow_name` | string | 是 | 流水线名称 |
| `file_path` | string | 是 |  |
| `title` | string | 是 |  |
| `status` | string | 是 | 运行状态，COMPLETED（已完成），RUNNING（运行中），FAILED（失败），CANCELED（取消），IGNORED（忽略），PAUSED（暂停），SUSPEND（挂起） |
| `event` | string | 是 | 触发事件类型，MR（mr事件），Push（推送事件），Manual（手动触发） |
| `run_number` | integer | 是 |  |
| `head_branch` | string | 是 |  |
| `head_sha` | string | 是 |  |
| `actor` | object(4字段) | 是 | 触发人 |
| `start_time` | integer | 是 | 开始时间戳 |
| `end_time` | integer | 是 | 结束时间戳 |
| `pause_time` | integer | 是 |  |
| `exist_in_default_branch` | boolean | 是 |  |
| `stages` | array&lt;object(18字段)&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-runs-run-id

---

### 获取工作流运行的jobs列表

<a id="获取工作流运行的jobs列表"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `run_id` | **是** | string | 流水线运行id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer | 是 |  |
| `jobs` | array&lt;object(29字段)&gt; | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-runs-run-id-jobs

---

### 获取工作流运行的job详情

<a id="获取工作流运行的job详情"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `run_id` | **是** | string | 流水线运行id |
| `job_id` | **是** | string | 任务id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 任务id |
| `category` | null | 是 |  |
| `sequence` | integer | 是 |  |
| `async` | null | 是 |  |
| `name` | string | 是 | 任务名称 |
| `identifier` | string | 是 |  |
| `depends_on` | array&lt;string&gt; | 是 |  |
| `condition` | string | 是 |  |
| `resource` | string | 是 |  |
| `is_select` | boolean | 是 |  |
| `timeout` | null | 是 |  |
| `last_dispatch_id` | string | 是 |  |
| `execute_cost_time` | integer | 是 |  |
| `status` | string | 是 | 任务状态。COMPLETED（已完成），RUNNING（运行中），FAILED（失败），CANCELED（取消），IGNORED（忽略），PAUSED（暂停），SUSPEND（挂起） |
| `message` | null | 是 |  |
| `start_time` | integer | 是 |  |
| `end_time` | integer | 是 |  |
| `exec_id` | string | 是 |  |
| `steps` | array&lt;object(22字段)&gt; | 是 |  |
| `job_type` | string | 是 |  |
| `max_parallel` | null | 是 |  |
| `fail_fast` | null | 是 |  |
| `from` | null | 是 |  |
| `with` | null | 是 |  |
| `secrets` | null | 是 |  |
| `outputs_define` | null | 是 |  |
| `concurrency` | null | 是 |  |
| `timeout_minutes` | null | 是 |  |
| `continue_on_error` | null | 是 |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-runs-run-id-jobs-job-id

---

### 查询工作流Job的Step级日志

<a id="查询工作流job的step级日志"></a>

`POST /api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/logs`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `run_id` | **是** | string | 流水线运行id |
| `job_id` | **是** | string | 任务id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `step_id` | string | 是 | Step运行id |
| `offset` | integer | 是 | 日志起始偏移量，用于增量分页，第一页传0，正序传入上一次返回的end_offset，倒序传入上一次返回的start_offset |
| `limit` | integer | 是 | 本次返回的日志条数上限 |
| `sort` | string | 是 | 排序方式（ asc/desc） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `has_more` | boolean | 是 |  |
| `start_offset` | integer | 是 |  |
| `end_offset` | integer | 是 |  |
| `log` | string | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/logs?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"step_id":"string","offset":0,"limit":0,"sort":"string"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-8-repos-owner-repo-actions-runs-run-id-jobs-job-id-logs

---

### 下载工作流运行的job日志

<a id="下载工作流运行的job日志"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/download_log`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `run_id` | **是** | string | 流水线运行id |
| `job_id` | **是** | string | 任务id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string | 用户授权码 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/download_log?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-runs-run-id-jobs-job-id-download-log

---

### 列出仓库的 Artifacts

<a id="列出仓库的-artifacts"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/artifacts`

- **认证**: 需要
- **说明**: 分页查询仓库下的制品，支持按名称过滤及排序。

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `name` | 否 | string | 制品名称过滤（模糊匹配） |
| `sort` | 否 | string | 排序字段，当前仅支持created |
| `direction` | 否 | string | 排序方向，asc 或 desc |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | 制品总数 |
| `artifacts` | array&lt;object(9字段)&gt; |  | 制品列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/artifacts?access_token={your-token}&name={name}&sort={sort}&direction={direction}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-artifacts

---

### 列出特定 Run 的 Artifacts

<a id="列出特定-run-的-artifacts"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts`

- **认证**: 需要
- **说明**: 分页查询指定 Workflow Run 产生的所有制品，支持按创建时间排序。

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `run_id` | **是** | string | 流水线运行id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string | 用户授权码 |
| `name` | 否 | string | 制品名称过滤（模糊匹配） |
| `sort` | 否 | string | 排序字段，当前仅支持created |
| `direction` | 否 | string | 排序方向，asc 或 desc |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | 制品总数 |
| `artifacts` | array&lt;object(9字段)&gt; |  | 制品列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts?access_token={your-token}&name={name}&sort={sort}&direction={direction}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-runs-run-id-artifacts

---

### 获取 Artifact 详情

<a id="获取-artifact-详情"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/artifacts/{artifact_id}`

- **认证**: 需要
- **说明**: 查询仓库下指定制品的详情。

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `artifact_id` | **是** | string | 制品id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string |  | 制品ID |
| `name` | string |  | 制品名称 |
| `size_bytes` | integer |  | 制品大小（字节） |
| `workflow_id` | string |  | 工作流ID |
| `workflow_run_id` | string |  | 工作流运行ID |
| `digest` | string |  | SHA256 摘要 |
| `expires_at` | string |  | 过期时间（毫秒时间戳） |
| `created_at` | string |  | 创建时间（毫秒时间戳） |
| `updated_at` | string |  | 更新时间（毫秒时间戳） |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/artifacts/{artifact_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-artifacts-artifact-id

---

### 删除指定 Artifact

<a id="删除指定-artifact"></a>

`DELETE /api/v8/repos/{owner}/{repo}/actions/artifacts/{artifact_id}`

- **认证**: 需要
- **说明**: 删除仓库下指定制品。

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `artifact_id` | **是** | string | 制品id |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `204` |  |

**请求示例**

```bash
curl -X DELETE "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/artifacts/{artifact_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/delete-api-v-8-repos-owner-repo-actions-artifacts-artifact-id

---

### 下载指定 Artifact

<a id="下载指定-artifact"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/{archive_format}`

- **认证**: 需要
- **说明**: 获取制品 zip 下载地址并返回 302 跳转。

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |
| `artifact_id` | **是** | string | 制品id |
| `archive_format` | **是** | string | 仅支持zip |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | 否 | string |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `302` |  |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/{archive_format}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-artifacts-artifact-id-archive-format

---

### 查询指定仓库下的所有主机 Runner

<a id="查询指定仓库下的所有主机-runner"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/runners`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |
| `keyword` | 否 | string | 关键字过滤 |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | Runner 总数 |
| `runners` | array&lt;object(11字段)&gt; |  | Runner 列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runners?access_token={your-token}&keyword={keyword}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-runners

---

### 查询分享给仓库的所有主机 Runner

<a id="查询分享给仓库的所有主机-runner"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/runners/shared-runners`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |
| `keyword` | 否 | string | 关键字过滤 |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | Runner 总数 |
| `runners` | array&lt;object(11字段)&gt; |  | Runner 列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runners/shared-runners?access_token={your-token}&keyword={keyword}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-runners-shared-runners

---

### 查询指定仓库下的所有 K8S Runner

<a id="查询指定仓库下的所有-k8s-runner"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/runner-sets`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |
| `keyword` | 否 | string | 关键字过滤 |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | RunnerSet 总数 |
| `runner_sets` | array&lt;object(20字段)&gt; |  | RunnerSet 列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/runner-sets?access_token={your-token}&keyword={keyword}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-runner-sets

---

### 查询分享给仓库的所有 K8S Runner

<a id="查询分享给仓库的所有-k8s-runner"></a>

`GET /api/v8/repos/{owner}/{repo}/actions/shared-runner-sets`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `owner` | **是** | string | 仓库所属空间地址(组织或个人的地址path) |
| `repo` | **是** | string | 仓库路径(path) |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |
| `keyword` | 否 | string | 关键字过滤 |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | RunnerSet 总数 |
| `runner_sets` | array&lt;object(20字段)&gt; |  | RunnerSet 列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/repos/{owner}/{repo}/actions/shared-runner-sets?access_token={your-token}&keyword={keyword}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-repos-owner-repo-actions-shared-runner-sets

---

### 查询指定组织下的所有 Runner Group

<a id="查询指定组织下的所有-runner-group"></a>

`GET /api/v8/orgs/{org}/actions/runner-groups`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织path |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |
| `keyword` | 否 | string | 关键字过滤 |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | Runner Group 总数 |
| `runner_groups` | array&lt;object(9字段)&gt; |  | Runner Group 列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/orgs/{org}/actions/runner-groups?access_token={your-token}&keyword={keyword}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-orgs-org-actions-runner-groups

---

### 获取指定 Runner Group 的详细信息

<a id="获取指定-runner-group-的详细信息"></a>

`GET /api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织path |
| `runner_group_id` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `runner_group_id` | string |  | Runner Group ID |
| `runner_group_name` | string |  | Runner Group 名称 |
| `share_all` | boolean |  | 是否分享给全部仓库 |
| `share_all_public_repos` | boolean |  | 是否分享给全部公开仓库 |
| `explicit_shared_repo_count` | integer |  | 显式分享仓库数量 |
| `created_at` | integer |  | 创建时间 |
| `updated_at` | integer |  | 更新时间 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}?access_token={your-token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-orgs-org-actions-runner-groups-runner-group-id

---

### 查询指定 Runner Group 下的所有主机 Runner

<a id="查询指定-runner-group-下的所有主机-runner"></a>

`GET /api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}/runners`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织path |
| `runner_group_id` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |
| `keyword` | 否 | string | 关键字过滤 |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | Runner 总数 |
| `runners` | array&lt;object(11字段)&gt; |  | Runner 列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}/runners?access_token={your-token}&keyword={keyword}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-orgs-org-actions-runner-groups-runner-group-id-runners

---

### 查询指定 Runner Group 下的所有 K8S Runner

<a id="查询指定-runner-group-下的所有-k8s-runner"></a>

`GET /api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}/runner-sets`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织path |
| `runner_group_id` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |
| `keyword` | 否 | string | 关键字过滤 |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | RunnerSet 总数 |
| `runner_sets` | array&lt;object(20字段)&gt; |  | RunnerSet 列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}/runner-sets?access_token={your-token}&keyword={keyword}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-orgs-org-actions-runner-groups-runner-group-id-runners-sets

---

### 查询有权访问指定 Runner Group 的仓库列表

<a id="查询有权访问指定-runner-group-的仓库列表"></a>

`GET /api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}/shared-namespaces`

- **认证**: 需要

**路径参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `org` | **是** | string | 组织path |
| `runner_group_id` | **是** | string |  |

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `access_token` | **是** | string |  |
| `keyword` | 否 | string | 关键字过滤 |
| `page` | 否 | integer | 当前页码 （示例: `1`） |
| `per_page` | 否 | integer | 每页的项目数 （示例: `10`） |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `total_count` | integer |  | 有权访问的仓库总数 |
| `shared_namespaces` | array&lt;object(12字段)&gt; |  | 有权访问的仓库列表 |

**请求示例**

```bash
curl "https://api.gitcode.com/api/v8/orgs/{org}/actions/runner-groups/{runner_group_id}/shared-namespaces?access_token={your-token}&keyword={keyword}&page=1&per_page=10" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/get-api-v-8-orgs-org-actions-runner-groups-runner-group-id-shared-namespaces

---

### OAuth 2.0

> 授权码/刷新令牌获取

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/oauth/token` | [获取或刷新授权 Token 接口](#获取或刷新授权-token-接口) |

---

### 获取或刷新授权 Token 接口

<a id="获取或刷新授权-token-接口"></a>

`POST /oauth/token`

- **认证**: 可选/公共
- **说明**: ## 基本信息<br/>- 请求路径: `/oauth/token`<br/>- 请求方法: POST<br/>- 接口说明: 用于获取或刷新访问令牌(access_token)的统一接口<br/><br/>## 请求参数<br/><br/>### Query 参数<br/><br/>\| 参数名 \| 必填 \| 类型 \| 说明 \|<br/>\|--------\|------\|------\|------\|<br/>\| grant_type \| 是 \| string \| 授权类型。可选值:<br/>- `authorization_code`: 授权码模式，用于获取新token<br/>- `refresh_token`: 刷新token模式，用于刷新已有token \|<br/>\| code \| 条件必填 \| string \| 授权码。当 grant_type=authorization_code 时必填 \|<br/>\| client_id \| 条件必填 \| string \| 注册的客户端ID。当 grant_type=authorization_code 时必填 \|<br/>\| refresh_token \| 条件必填 \| string \| 刷新令牌。当 grant_type=refresh_token 时必填 \|<br/><br/>### form-data 参数<br/><br/>\| 参数名 \| 必填 \| 类型 \| 说明 \|<br/>\|--------\|------\|------\|------\|<br/>\| client_secret \| 条件必填 \| string \| 注册的客户端密钥。当 grant_type=authorization_code 时必填 \|<br/><br/>## 使用场景<br/><br/>1. **获取新的访问令牌**<br/>   - 设置 grant_type=authorization_code<br/>   - 提供 code、client_id 和 client_secret<br/>   <br/>2. **刷新访问令牌**<br/>   - 设置 grant_type=refresh_token<br/>   - 提供 refresh_token<br/><br/>## 示例<br/><br/>### 获取新token<br/>```<br/>POST '/oauth/token?grant_type=&code=&client_id=&client_secret=&refresh_token=' \<br/>--form 'client_secret=""'<br/>```<br/><br/>### 刷新token<br/>```<br/>POST /oauth/token?grant_type=refresh_token&refresh_token={refresh_token}<br/>```

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `grant_type` | **是** | string | 授权码模式 |
| `code` | 否 | string | 授权码 |
| `client_id` | 否 | string | 注册的客户端 ID |
| `refresh_token` | 否 | string | 刷新令牌，仅在 grant_type 为 refresh_token 时必传 |

**请求体 (multipart/form-data)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `client_secret` | string |  | 刷新令牌。当 grant_type=refresh_token 时必填 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` | 成功响应 |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `access_token` | string |  |  |
| `expires_in` | integer |  |  |
| `refresh_token` | string |  |  |
| `scope` | string |  |  |
| `created_at` | string |  |  |

**请求示例**

```bash
curl -X POST "https://gitcode.com/oauth/token?grant_type={grant_type}&code={code}&client_id={client_id}&refresh_token={refresh_token}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-oauth-token-grant-type-authorization-code-code-code-client-id-client-id-client-secret-client-secret

---

### AI hub 模型服务

> 文本生成、语音识别、目标检测、视频生成等

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v5/chat/completions` | [文本生成](#文本生成) |
| `POST` | `/api/v5/similarity/` | [句子相似度](#句子相似度) |
| `POST` | `/api/v5/audio/transcriptions` | [自动语音识别](#自动语音识别) |
| `POST` | `/api/v5/detect/yolo` | [目标检测](#目标检测) |
| `POST` | `/api/v5/video/generate` | [图像生成视频（创建任务）](#图像生成视频创建任务) |
| `POST` | `/api/v5/video/status` | [图像生成视频（查询状态）](#图像生成视频查询状态) |
| `POST` | `/api/v5/audio/classification` | [音频分类](#音频分类) |

---

### 文本生成

<a id="文本生成"></a>

`POST /api/v5/chat/completions`

- **认证**: 可选/公共

**Header 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `Authorization` | **是** | string | 用户个人令牌 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `temperature` | number |  | 默认 0.7 |
| `top_k` | integer |  | 默认 50 |
| `top_p` | number |  | 默认 0.7 |
| `frequency_penalty` | number |  | 默认 1.0 |
| `messages` | array&lt;string&gt; | 是 |  |
| `model` | string | 是 |  |
| `maxTokens` | integer |  |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `trace_id` | string | 是 | 请求的唯一标识 |
| `choices` | array&lt;string&gt; | 是 |  |
| `ext` | string | 是 | 扩展字段，通常为null |
| `usage` | object(4字段) | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/chat/completions" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"temperature":0,"top_k":0,"top_p":0,"frequency_penalty":0,"messages":["string"],"model":"string","maxTokens":0}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-chat-completions

---

### 句子相似度

<a id="句子相似度"></a>

`POST /api/v5/similarity/`

- **认证**: 可选/公共

**Header 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `Authorization` | **是** | string | 用户个人令牌 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | 是 |  |
| `inputs` | object(2字段) | 是 |  |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/similarity/" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"model":"string","inputs":{"source_sentence":"string","sentences":["string"]}}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-similarity

---

### 自动语音识别

<a id="自动语音识别"></a>

`POST /api/v5/audio/transcriptions`

- **认证**: 可选/公共

**Header 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `Authorization` | **是** | string | 用户个人令牌 |

**请求体 (multipart/form-data)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | string | 是 | 音频文件 (支持 mp3, wav, m4a, flac, webm) |
| `model` | string | 是 | 模型名称，默认为 openai/whisper-large-v3 |
| `temperature` | string |  | 采样温度（0.0-1.0），默认为0.6 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | 是 |  |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/audio/transcriptions" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-1-audio-transcriptions

---

### 目标检测

<a id="目标检测"></a>

`POST /api/v5/detect/yolo`

- **认证**: 可选/公共
- **说明**: 使用YOLO模型对图片进行目标检测，识别图片中的物体

**Header 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `Authorization` | **是** | string | 用户个人令牌 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model` | string | 是 | yolo11模型权重文件名称 (yolo11n.pt, yolo11s.pt, yolo11m.pt, yolo11l.pt, yolo11x.pt) |
| `source` | string | 是 | 待检测图片base64编码字符串 |
| `imgsz` | integer | 是 | 输入图像的尺寸，有效范围是 32 - 1280 像素 |
| `conf` | number | 是 | 预测的置信度阈值，有效范围 0.01 - 1.0 |
| `iou` | number | 是 | 交并比iou阈值，有效范围 0.0 - 0.95 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `images` | array&lt;object(3字段)&gt; | 是 | 检测结果图片列表 |
| `metadata` | object(5字段) | 是 | 元数据信息 |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/detect/yolo" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"model":"yolo11n.pt","source":"data:image/jpeg;base64,/9j/4AAQSkZJRgABAQXXXXXXX","imgsz":640,"conf":0.5,"iou":0.45}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-detect-yolo

---

### 图像生成视频（创建任务）

<a id="图像生成视频创建任务"></a>

`POST /api/v5/video/generate`

- **认证**: 可选/公共
- **说明**: 调用此接口创建视频生成任务，如果没有异常返回，可根据 requestId 进行异步视频结果查询。

**Header 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `Authorization` | **是** | string | 用户个人令牌 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `img_url` | string | 是 | 支持输入 Base64 或图片 URL |
| `model` | string | 是 | 模型名称 |
| `prompt` | string | 是 | 提示词 |
| `seed` | integer |  | 随机种子,范围 0-2147483648 |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `request_id` | string |  | 请求ID |
| `status` | string |  | 处理状态 |
| `error_message` | string |  | 错误信息（仅异常时出现） |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/video/generate" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"img_url":"https://cdn-static.gitcode.com/_nuxtaihub/chatexample_67.png","model":"Wan2.2-I2V-A14B","prompt":"小猫在散步","seed":0}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-video-generate

---

### 图像生成视频（查询状态）

<a id="图像生成视频查询状态"></a>

`POST /api/v5/video/status`

- **认证**: 可选/公共
- **说明**: 根据 requestId查询视频生成状态。

**Header 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `Authorization` | **是** | string | 用户个人令牌 |

**请求体 (application/json)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `request_id` | string | 是 | 请求ID |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `request_id` | string |  | 请求ID（有效时长为 24h，请尽快获取结果） |
| `status` | string |  | 处理状态（running,failed,succeeded） |
| `video_url` | string |  | 视频URL（仅成功返回，且有效时长为 10 分钟） |
| `error_message` | string |  | 错误信息（仅异常时出现） |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/video/status" \
  --header 'Authorization: Bearer {your-token}' --data-raw '{"request_id":"009522a671394f3f5ac02c63b1a62662"}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-video-status

---

### 音频分类

<a id="音频分类"></a>

`POST /api/v5/audio/classification`

- **认证**: 可选/公共
- **说明**: 对音频文件进行情感分类识别

**Query 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `model` | **是** | string | 模型名称，默认为 audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim |

**Header 参数**

| 参数 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `Authorization` | **是** | string | 用户个人令牌 |

**请求体 (multipart/form-data)**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | string | 是 | 音频文件 (支持 mp3, wav) |

**响应**

| 状态码 | 说明 |
|--------|------|
| `200` |  |

**200 响应体字段**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `vector` | number |  | 音频文件的向量表示 |
| `emotion_class` | object(3字段) |  |  |
| `emotionItems` | array&lt;object(5字段)&gt; |  | 情感分类结果列表（格式化后） |

**请求示例**

```bash
curl -X POST "https://api.gitcode.com/api/v5/audio/classification?model={model}" \
  --header 'Authorization: Bearer {your-token}'
```

> 官方文档: https://docs.gitcode.com/docs/apis/post-api-v-5-audio-classification

---

