---
name: blog-manager
version: 0.1.0
description: |
  封装 Blog System REST API 的管理 skill，提供文章/标签/用户/评论/留言/说说/上传/健康检查
  共 8 模块 28 个子命令的命令行能力。无认证（公开 API）。
triggers:
  - blog
  - article
  - blog api
  - 博客管理
  - blog system
tags:
  - blog
  - rest-api
  - content-management
  - blog-system
  - no-auth
tools:
  - curl
  - python3
---

# blog-manager-kit

封装 Blog System REST API（OpenAPI 3.1 文档：`{base_url}/openapi.json`）的管理 skill，提供 8 模块 28 个子命令。无认证。

## 安全硬约束（必须遵守）

**通用约束（所有 skill 必含）**：

1. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
2. API 返回 403/无权限时：立即停止该操作并回复用户，严禁尝试其他 token 或绕行手段。
3. 所有 curl / API 调用设 30s 超时——超时判定失败，不 hang。
4. 严禁 mock 模式/假数据：所有 API 调用必须真实访问目标系统。
5. 调用目标 API 一律以 `references/api-reference.md` 为准：严禁根据经验/记忆/猜测自行拼写 API 路径。

**动态约束**：

- 无认证（公开 API）：不需要凭据，省略凭据相关约束。

## 评论规范

- 所有 Issue/PR 评论 body 必须以 `[blog-manager-kit]` 开头（环境变量未注入 AGENT_NAME 时用 skill name 作为前缀）。
- 引用用户原文：`> {用户评论}\n\n[blog-manager-kit] {回复}`

## Configuration

目标系统 API 地址必须在使用前确定。所有命令中的 `{base_url}` 替换为实际地址（格式 `http://<host>:<port>`，示例）。

`{base_url}` 解析优先级：

1. **项目知识** — 检查 `.project-info/` 目录下 JSON 配置文件（`config.BLOG_MANAGER_KIT_BASE_URL`）
2. **环境变量** — `BLOG_MANAGER_KIT_BASE_URL`
3. **默认地址** — 以上都无时使用 skill 内置默认地址（`http://<host>:<port>`，示例）

### 配置指导

**方式一：环境变量（临时，推荐快速测试）**

```bash
export BLOG_MANAGER_KIT_BASE_URL="http://<host>:port"  # (示例)
```

**方式二：项目知识 JSON 文件（持久化，推荐生产使用）**

在项目根目录下创建 `.project-info/` 目录，放入任意名称的 `.json` 文件（脚本会递归扫描所有 JSON 文件）：

```json
{
  "config": {
    "BLOG_MANAGER_KIT_BASE_URL": "http://<host>:port"  // (示例)
  }
}
```

> ⚠️ `.project-info/` 含敏感配置，不提交到 git 仓库（加入 .gitignore）。

## 场景 / When to Use

- 用户要求查询/发布/更新/删除博客文章
- 用户要求管理博客标签、用户、评论、留言、说说
- 用户要求上传/列出/删除博客文件资源
- 用户要求检查博客系统 API 可达性
- 用户要求获取热门文章 Top N

Don't use for: 后台 Cookie 登录/登出/批量删除（Web UI 层 `/admin*`）、博客 HTML 页面浏览（`GET /`、`GET /article/{id}`）、非 Blog System 的 API 操作。

## 知识 / Knowledge

### API Base URL

```
{base_url}
```

All endpoints below are relative to this base. 无认证（不需要 header）。

### Endpoints

按资源分组列出端点表（从 API 文档解析，详见 `references/api-reference.md`）：

| Method | Path | 子命令 | 说明 |
|--------|------|--------|------|
| GET | `/health` | health-check | 检查 API 可达性 |
| GET | `/api/articles` | list-articles | 分页查询文章列表 |
| POST | `/api/articles` | create-article | 发布新文章 |
| GET | `/api/articles/{id}` | get-article | 查询文章详情（含评论） |
| PUT | `/api/articles/{id}` | update-article | 更新文章 |
| DELETE | `/api/articles/{id}?soft=true` | delete-article | 软删除文章（可恢复） |
| DELETE | `/api/articles/{id}?soft=false` | hard-delete-article | 硬删除文章（不可逆） |
| POST | `/api/articles/{id}/restore` | restore-article | 恢复软删除文章 |
| GET | `/api/articles/heat/top` | top-articles | 热门文章 Top N |
| GET | `/api/lables` | list-labels | 获取所有标签 |
| POST | `/api/lables` | create-label | 创建标签 |
| GET | `/api/users` | list-users | 获取用户列表 |
| POST | `/api/users` | create-user | 创建用户 |
| POST | `/api/comments` | create-comment | 发表评论 |
| GET | `/api/comments/{aid}` | list-comments | 获取文章评论列表 |
| DELETE | `/api/comments/{id}` | delete-comment | 删除评论（软删除） |
| GET | `/api/messages` | list-messages | 获取留言列表 |
| POST | `/api/messages` | create-message | 发表留言 |
| POST | `/api/messages/reply` | reply-message | 回复留言 |
| DELETE | `/api/messages/{id}` | delete-message | 删除留言（软删除） |
| GET | `/api/moods` | list-moods | 获取说说列表 |
| POST | `/api/moods` | create-mood | 发布说说 |
| DELETE | `/api/moods/{id}` | delete-mood | 删除说说 |
| POST | `/api/upload` | upload-single | 上传单个文件 |
| POST | `/api/upload/multiple` | upload-batch | 批量上传文件 |
| GET | `/api/uploads/list` | list-uploads | 列出已上传文件 |
| DELETE | `/api/uploads/{filename}` | delete-upload | 删除已上传文件 |

### 认证方式

- 无认证：不需要凭据
- API 地址：`BLOG_MANAGER_KIT_BASE_URL`
- 凭据前缀：由 skill name 推导（`blog-manager-kit` → `BLOG_MANAGER_KIT`），无认证模式仅需 BASE_URL
- 读取优先级（4 级）：项目知识（`.project-info/` 下 JSON 的 `config.BLOG_MANAGER_KIT_BASE_URL`）> 环境变量 `BLOG_MANAGER_KIT_BASE_URL` > 默认地址

### Common Pitfalls

1. **API 路径拼写 `lables`。** API 实际路径为 `/api/lables`（原文拼写错误），子命令用正确拼写 `labels`（list-labels / create-label），脚本内部请求时用 `lables`。详见 api-reference.md。
2. **硬删除不可逆。** `hard-delete-article` 永久删除文章且无法 restore 恢复。子命令默认二次确认（输入 `yes`），自动化场景用 `--yes` 跳过。软删除用 `delete-article`（可 `restore-article` 恢复）。
3. **DELETE 软/硬删除同一端点。** `DELETE /api/articles/{id}` 通过 query 参数 `soft` 区分：`soft=true`（默认，软删除）/ `soft=false`（硬删除）。本 skill 拆为两个子命令避免误操作。
4. **文件上传字段名不同。** 单文件上传字段 `file`（`files={'file': f}`），批量上传字段 `files`（`files=[('files', f)...]`）。批量上传不能用 dict value 为列表（requests 会报 unpack 错误），必须用元组列表。
5. **响应结构包裹在 `code`/`data`。** 列表/详情返回 `{"code":200,"data":[...]}`，不是直接返回数组。解析时取 `data` 字段。
6. **分页参数。** `list-articles` 用 `page`（从 1 开始）/`size`（最大 100），不是 offset/limit。`lid=0` 表示不按标签过滤。
7. **Web 页面端点不属于 API。** `GET /` 和 `GET /article/{id}` 返回 HTML 页面，`/admin*` 是后台 Cookie 管理层，均不在本 skill 范围。

### 字段说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| article_id | integer | 是 | — | 文章 ID（路径参数） |
| title | string | create 是 | — | 文章/说说标题 |
| content | string | 是 | — | 文章/评论/留言/说说内容 |
| uid | integer | 是 | 1 | 用户 ID |
| lid | integer | 否 | 1 | 标签 ID |
| aid | integer | 是 | — | 文章 ID（评论关联） |
| mid | integer | 是 | — | 留言 ID（回复关联） |
| comment_id | integer | 是 | — | 评论 ID |
| message_id | integer | 是 | — | 留言 ID |
| mood_id | integer | 是 | — | 说说 ID |
| filename | string | 是 | — | 上传文件名 |
| lname | string | 是 | — | 标签名 |
| uname | string | 是 | — | 用户名 |

## 步骤 / Steps

主流程按序号排列，每个操作是子步骤。所有命令在 `skills/blog-manager-kit/` 目录下执行。

### 1. 检查 API 可达性

#### health-check
```bash
python3 scripts/blog-manager-kit.py health-check
```
Expected: `{"status":"ok"}`

#### capability-list
```bash
python3 scripts/blog-manager-kit.py capability-list
```
Expected: `{"capability":"capability-list","skill":"blog-manager-kit","subcommand_count":28,...}`

### 2. 文章管理（articles）

#### list-articles
```bash
python3 scripts/blog-manager-kit.py list-articles --page 1 --size 10 --lid 0 --keyword ""
```
Expected: `{"code":200,"data":[...]}`。参数：`--page`页码 / `--size`每页数(≤100) / `--lid`标签过滤(0=不过滤) / `--keyword`关键词

#### create-article
```bash
python3 scripts/blog-manager-kit.py create-article --title "标题" --content "内容" --uid 1 --lid 1
```
Expected: `{"code":200,"data":{"id":...}}`。参数：`--title`/`--content`必填；`--uid`(默认1)/`--lid`(默认1)/`--img`/`--heat`(默认0)可选

#### get-article
```bash
python3 scripts/blog-manager-kit.py get-article --article-id 1
```
Expected: `{"code":200,"data":{...}}`。参数：`--article-id`必填

#### update-article
```bash
python3 scripts/blog-manager-kit.py update-article --article-id 1 --title "新标题"
```
Expected: `{"code":200,"data":{...}}`。参数：`--article-id`必填；`--title`/`--content`/`--lid`/`--img`/`--heat`可选

#### delete-article（软删除，可恢复）
```bash
python3 scripts/blog-manager-kit.py delete-article --article-id 1
```
Expected: `{"code":200,"data":{...}}`。参数：`--article-id`必填。软删除，可用 restore-article 恢复

#### hard-delete-article（硬删除，不可逆，二次确认）
```bash
python3 scripts/blog-manager-kit.py hard-delete-article --article-id 1
```
Expected: 提示确认 → 输入 `yes` → `{"code":200,"data":{...}}`。参数：`--article-id`必填；`--yes`跳过确认（自动化）

#### restore-article
```bash
python3 scripts/blog-manager-kit.py restore-article --article-id 1
```
Expected: `{"code":200,"data":{...}}`。参数：`--article-id`必填

#### top-articles
```bash
python3 scripts/blog-manager-kit.py top-articles --limit 5
```
Expected: `{"code":200,"data":[...]}`。参数：`--limit`(默认5)

### 3. 标签管理（labels）

#### list-labels
```bash
python3 scripts/blog-manager-kit.py list-labels
```
Expected: `{"code":200,"data":[{"id":1,"lname":"..."}]}`

#### create-label
```bash
python3 scripts/blog-manager-kit.py create-label --lname "新标签"
```
Expected: `{"code":200,"data":{...}}`。参数：`--lname`必填

### 4. 用户管理（users）

#### list-users
```bash
python3 scripts/blog-manager-kit.py list-users
```
Expected: `{"code":200,"data":[...]}`

#### create-user
```bash
python3 scripts/blog-manager-kit.py create-user --uname "用户名" --phone "13800000000"
```
Expected: `{"code":200,"data":{...}}`。参数：`--uname`必填；`--phone`/`--pwd`/`--email`/`--img`(默认img/moren.jpg)可选

### 5. 评论管理（comments）

#### create-comment
```bash
python3 scripts/blog-manager-kit.py create-comment --uid 1 --aid 1 --content "评论内容"
```
Expected: `{"code":200,"data":{...}}`。参数：`--uid`/`--aid`/`--content`必填

#### list-comments
```bash
python3 scripts/blog-manager-kit.py list-comments --aid 1
```
Expected: `{"code":200,"data":[...]}`。参数：`--aid`必填

#### delete-comment
```bash
python3 scripts/blog-manager-kit.py delete-comment --comment-id 1
```
Expected: `{"code":200,"data":{...}}`。参数：`--comment-id`必填（软删除）

### 6. 留言管理（messages）

#### list-messages
```bash
python3 scripts/blog-manager-kit.py list-messages
```
Expected: `{"code":200,"data":[...]}`

#### create-message
```bash
python3 scripts/blog-manager-kit.py create-message --uid 1 --content "留言内容"
```
Expected: `{"code":200,"data":{...}}`。参数：`--uid`/`--content`必填

#### reply-message
```bash
python3 scripts/blog-manager-kit.py reply-message --uid 1 --mid 1 --content "回复内容"
```
Expected: `{"code":200,"data":{...}}`。参数：`--uid`/`--mid`/`--content`必填

#### delete-message
```bash
python3 scripts/blog-manager-kit.py delete-message --message-id 1
```
Expected: `{"code":200,"data":{...}}`。参数：`--message-id`必填（软删除）

### 7. 说说管理（moods）

#### list-moods
```bash
python3 scripts/blog-manager-kit.py list-moods
```
Expected: `{"code":200,"data":[...]}`

#### create-mood
```bash
python3 scripts/blog-manager-kit.py create-mood --content "说说内容" --title "标题"
```
Expected: `{"code":200,"data":{...}}`。参数：`--content`必填；`--title`/`--src`可选

#### delete-mood
```bash
python3 scripts/blog-manager-kit.py delete-mood --mood-id 1
```
Expected: `{"code":200,"data":{...}}`。参数：`--mood-id`必填

### 8. 文件上传（uploads）

#### upload-single
```bash
python3 scripts/blog-manager-kit.py upload-single --file /path/to/file.png
```
Expected: `{"code":200,"data":{"url":"/uploads/..."}}`。参数：`--file`必填（本地文件路径）

#### upload-batch
```bash
python3 scripts/blog-manager-kit.py upload-batch --files /path/a.png /path/b.png
```
Expected: `{"code":200,"data":[...]}`。参数：`--files`必填（≥1 个本地文件路径）

#### list-uploads
```bash
python3 scripts/blog-manager-kit.py list-uploads
```
Expected: `{"code":200,"data":[{"filename":"...","url":"...","type":"image"}]}`

#### delete-upload
```bash
python3 scripts/blog-manager-kit.py delete-upload --filename abc123.png
```
Expected: `{"code":200,"data":{...}}`。参数：`--filename`必填

## 判断标准 / Verification

- [ ] API 可达（`health-check` 返回 200 / `{"status":"ok"}`）
- [ ] 只读操作返回有效数据（list-* 命令返回非空 data 数组/字段完整）
- [ ] 脚本 `--help` 无语法错误
- [ ] 退出码正确（0=成功 / 2=参数错误或取消 / 3=缺少配置 / 4=API 调用失败）
- [ ] `hard-delete-article` 默认二次确认（未加 `--yes` 时提示）
- [ ] JSON 结构化输出，`--format md` 可切换 Markdown 表格

## 输出规范 / Output Format

### 脚本输出

- 默认输出：JSON（`json.dumps(..., ensure_ascii=False, indent=2)`）
- 可选输出：Markdown 表格（`--format md`，仅 capability-list 渲染为表格，其他保持 JSON）
- 使用示例：`cd skills/blog-manager-kit/ && python3 scripts/blog-manager-kit.py list-articles --page 1 --size 5`

### Issue 评论格式

执行完操作后，Issue 评论 body 格式：

```
[blog-manager-kit] ✅ {操作名称}完成

## 操作结果

| 操作 | 状态 | 资源 ID | 详情 |
|------|------|---------|------|
| {操作名} | ✅ 成功 / 🔴 失败 | {id} | {简要说明} |

**API 地址**：{base_url}
**执行时间**：{UTC ISO}
```

失败时：

```
[blog-manager-kit] 🔴 {操作名称}失败

## 错误分析

- **操作**：{操作名}
- **错误**：{API 返回的错误信息}
- **HTTP 状态码**：{code}
- **原因**：{分析原因}

### 修复建议
{修复步骤}
```

## 参考文档 / References

- [API 端点文档](references/api-reference.md)
- [测试用例](templates/test-vars.json)

## 分析结论示例

成功时：

```
分析结论：
- 场景：Blog System API 操作（无认证）
- Skill：blog-manager-kit v0.1.0
- 操作：{subcommand} {params}
- 结果：✅ 成功（{API 返回的关键数据摘要}）
- 必须执行：Issue 评论汇总结果 → 任务结束
```

失败时：

```
分析结论：
- 场景：Blog System API 操作（无认证）
- Skill：blog-manager-kit v0.1.0
- 操作：{subcommand} {params}
- 结果：🔴 失败（{错误原因}）
- 必须执行：Issue 评论报告错误 → 任务结束
```

## 任务结束

完成所有业务动作后，在最终答复末尾输出以下 JSON 块并结束任务：

```json
{
  "actions": ["{具体执行的操作，如 list-articles: 返回 5 篇文章}"],
  "conclusion": "{处理结论，如 查询成功，返回 5 篇文章}",
  "artifacts": ["{产生的产物：写操作返回资源 ID 或操作结果 / 只读查询无产物填空数组}"],
  "next_step": "{下一步建议，如 无 / 等待用户下一步指令}",
  "issue_summary": "{Issue 最新聚合结论，紧凑文本，用 | 分隔}"
}
```

注意：只输出一个 JSON 块，放在答复末尾。
