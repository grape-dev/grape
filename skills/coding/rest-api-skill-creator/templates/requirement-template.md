# REST API Skill 需求模板

> 填写以下模板后提交 Issue，rest-api-skill-creator 将基于此需求自动生成 skill。
> 带 * 的字段必填，其余可选。

---

## 基本信息

- **Skill 名称** *：（小写连字符，如 blog-manager / cms-toolkit / wiki-admin）
- **一句话描述** *：（如"管理博客系统文章的发布/编辑/删除/查询"）
- **目标系统名称** *：（如"我的博客" / "Hexo 博客" / "内部 CMS"）

## API 地址

- **API base_url** *：（如 https://blog.example.com/api）
- **API 路径前缀**：（如 /api / /v1 / /rest，如果不填默认 /api）

## 认证方式（四选一）*

- [ ] **Basic Auth**（用户名 + 密码）
  - 用户名环境变量：`{PREFIX}_USERNAME`
  - 密码环境变量：`{PREFIX}_PASSWORD`
- [ ] **Bearer Token**（JWT / API Token）
  - Token 环境变量：`{PREFIX}_TOKEN`
- [ ] **API Key**（Header 或 Query 参数）
  - API Key 环境变量：`{PREFIX}_API_KEY`
  - 传递方式：Header（如 `X-API-Key`）/ Query（如 `?api_key=xxx`）
  - Header/参数名：（如 X-API-Key / api_key）
- [ ] **无认证**（公开 API，无需凭据）

> {PREFIX} 由 skill name 自动推导（如 blog-manager → BLOG_MANAGER）

## API 文档（三选一）*

- [ ] **OpenAPI/Swagger 文档**（JSON/YAML 文件，粘贴或附链接）
  ```
  粘贴 OpenAPI 文档内容，或提供文档 URL
  ```
- [ ] **Markdown 文档**（手写端点描述）
  ```
  GET    /api/articles          - 获取文章列表（支持 page/size 分页）
  POST   /api/articles          - 创建文章（body: title/content/category/tags）
  GET    /api/articles/{id}     - 获取文章详情
  PUT    /api/articles/{id}     - 更新文章（body: title/content）
  DELETE /api/articles/{id}     - 删除文章
  GET    /api/categories        - 获取分类列表
  POST   /api/categories        - 创建分类（body: name）
  ```
- [ ] **基础信息描述**（口头描述，agent 会用 curl 探测确认）
  ```
  我的博客系统有文章管理（增删改查）和分类管理功能。
  端点大概在 /api/articles 和 /api/categories 下。
  具体参数我不确定，请你帮我探测。
  ```

## 需要的操作能力（可选，不填则由 API 文档决定）

- [ ] 查询列表（List）
- [ ] 查询详情（Get）
- [ ] 创建（Create）
- [ ] 更新（Update）
- [ ] 删除（Delete）
- [ ] 分类/标签管理
- [ ] 其他：（自定义描述）

## 输出格式（可选）

- [ ] JSON（默认）
- [ ] Markdown 表格
- [ ] 两者都支持

---

## 填写示例

以下是一个完整填写的示例，供参考：

```markdown
## 基本信息
- Skill 名称：blog-manager
- 一句话描述：管理博客系统文章的发布/编辑/删除/查询和分类管理
- 目标系统名称：我的个人博客

## API 地址
- API base_url：https://blog.example.com/api
- API 路径前缀：/api

## 认证方式
- [x] Basic Auth（用户名 + 密码）

## API 文档
- [x] Markdown 文档
  GET    /api/articles          - 获取文章列表（支持 page/size 分页，返回 {data: [...], total: N}）
  POST   /api/articles          - 创建文章（body: title, content, category_id, tags[]）
  GET    /api/articles/{id}     - 获取文章详情（返回 {id, title, content, category, tags, created_at, updated_at}）
  PUT    /api/articles/{id}     - 更新文章（body: title, content, category_id, tags[]）
  DELETE /api/articles/{id}     - 删除文章
  GET    /api/categories        - 获取分类列表（返回 [{id, name, count}]）
  POST   /api/categories        - 创建分类（body: name）
  DELETE /api/categories/{id}   - 删除分类

## 需要的操作能力
- [x] 查询列表
- [x] 查询详情
- [x] 创建
- [x] 更新
- [x] 删除
- [x] 分类/标签管理

## 输出格式
- [x] JSON
- [x] Markdown 表格
```

基于以上示例，rest-api-skill-creator 将生成：
- 8 个子命令 + 1 个 capability-list = 9 个子命令
- 凭据变量：BLOG_MANAGER_USERNAME / BLOG_MANAGER_PASSWORD / BLOG_MANAGER_BASE_URL
- 认证方式：Basic Auth
- 输出格式：JSON + Markdown

---

## 凭据提供方式

生成 skill 后，使用时需要提供目标系统的凭据。有两种方式（优先环境变量，缺失时回退 JSON）。

**无认证模式不需要提供凭据**，只需设置 `{PREFIX}_BASE_URL`。

### 方式一：环境变量（运行时注入）

```bash
# Basic Auth
export BLOG_MANAGER_BASE_URL="https://blog.example.com/api"
export BLOG_MANAGER_USERNAME="your-username"
export BLOG_MANAGER_PASSWORD="your-password"

# Bearer Token
export BLOG_MANAGER_BASE_URL="https://blog.example.com/api"
export BLOG_MANAGER_TOKEN="your-jwt-token"

# API Key
export BLOG_MANAGER_BASE_URL="https://blog.example.com/api"
export BLOG_MANAGER_API_KEY="your-api-key"
```

### 方式二：JSON 配置文件（持久化）

在 .project-info/ 目录下放 JSON 配置文件（文件名不限，脚本会递归扫描所有 .json 文件）：

**Basic Auth：**
```json
{
  "version": 1,
  "secrets": {
    "BLOG_MANAGER_USERNAME": "your-username",
    "BLOG_MANAGER_PASSWORD": "your-password"
  },
  "config": {
    "BLOG_MANAGER_BASE_URL": "https://blog.example.com/api"
  }
}
```

**Bearer Token：**
```json
{
  "version": 1,
  "secrets": {
    "BLOG_MANAGER_TOKEN": "your-jwt-token"
  },
  "config": {
    "BLOG_MANAGER_BASE_URL": "https://blog.example.com/api"
  }
}
```

**API Key：**
```json
{
  "version": 1,
  "secrets": {
    "BLOG_MANAGER_API_KEY": "your-api-key"
  },
  "config": {
    "BLOG_MANAGER_BASE_URL": "https://blog.example.com/api"
  }
}
```

> {PREFIX} 由 skill name 推导（如 blog-manager → BLOG_MANAGER）
> ⚠️ 变量值存在但为空也算缺失——必须检查值非空
> ⚠️ JSON 配置文件含敏感凭据，不提交到 git 仓库（.gitignore 已排除 .project-info/）
