# DevOps Bot Web API 接口文档

> 端口 80：Admin/User 管理面板 + OAuth  
> 端口 8080：Webhook / A2A / 辅助 API

---

## 1. 用户面板 (User Panel) — 端口 80

### 1.1 登录与会话

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 1 | GET | `/` | `token` (query, 可选) | 无 / Token | 有有效 token → 用户面板；否则→登录页 | HTML |
| 2 | GET | `/panel` | `token` (query) | Token | 直接通过 token 访问面板 | HTML / 登录页 |
| 3 | GET | `/logout` | `token` (query, 可选) | 无 | 清除 session token | HTML 登录页 |
| 4 | POST | `/login` | `username`, `password` (form) | 无 | 密码认证，创建 session token | HTML 面板 / 登录页 |

### 1.2 凭据管理

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 5 | POST | `/user/cred` | `token` (query) + `gitcode_api_token`, `gitcode_webhook_token`, `webhook_url`, `feishu_app_id`, `feishu_app_secret`, `huawei_cloud_ak`, `huawei_cloud_sk`, `CODEARTS_CLI_AK`, `CODEARTS_CLI_SK` (form) | Token | 保存用户凭据到 `user_accounts` 表 | HTML 面板 |

### 1.3 云主机管理

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 6 | POST | `/user/vm` | `token` (query) + `name`, `host`, `ssh_user`(默认 root), `ssh_password`, `port`(默认 22) (form) | Token | 新增云主机记录 | HTML |
| 7 | POST | `/user/vm-delete` | `token` (query) + `vm_id` (form) | Token | 删除云主机 | HTML |
| 8 | POST | `/user/vm-test` | `token` (query) + `vm_id` (form) | Token | 测试 SSH 连通性（paramiko 连接） | HTML |
| 9 | POST | `/user/vm-config` | `token` (query) + `vm_id`, `repos`(数组) (form) | Token | 配置云主机关联的托管仓库 | HTML |

### 1.4 自定义技能管理

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 10 | POST | `/user/skill` | `token` (query) + `zip_b64` (form, base64 zip) | Token | 上传自定义技能 zip 包，解压注册 | HTML |
| 11 | POST | `/user/skill-config` | `token` (query) + `skill_name`, `repos`(数组), `events`(数组) (form) | Token | 配置仓库→事件→技能映射 | HTML |
| 12 | POST | `/user/skill-delete` | `token` (query) + `skill_name` (form) | Token | 删除自定义技能 | HTML |

### 1.5 仓库托管管理

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 13 | POST | `/user/repo-sync` | `token` (query) | Token | 从 GitCode 加载用户仓库列表，标注管理员角色 | HTML |
| 14 | POST | `/user/repo-add` | `token` (query) + `full_name` (form, `owner/repo`) | Token | 创建 webhook 并托管仓库 | HTML |
| 15 | POST | `/user/repo-remove` | `token` (query) + `repo_owner`, `repo_name` (form) | Token | 移除托管仓库（可选删除 webhook） | HTML |

---

## 2. 管理员面板 (Admin Panel) — 端口 80

### 2.1 登录与会话

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 16 | GET | `/admin/` | `token`(可选), `error`(可选), `select`(可选) (query) | Token / 无 | 管理员面板或登录页 | HTML |
| 17 | GET | `/admin/logout` | `token` (query) | 无 | 清除 admin token | HTML 登录页 |
| 18 | POST | `/admin/login` | `username`, `password` (form) | 无 | 硬编码账号验证 (`developer`/`YOUR_SSH_PASSWORD`)，创建 24h token | HTML |

### 2.2 账户管理

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 19 | GET | `/admin/delete` | `token` (query) + `name` (query) | Token | 删除账户（`default` 不可删） | HTML |
| 20 | GET | `/admin/test` | `token` (query) + `name` (query) | Token | 测试 GitCode API 连通性 | HTML |
| 21 | POST | `/admin/save` | `token` (query) + ALL account fields (form) | Token | 创建/更新账户，同步密码 hash | HTML |

### 2.3 仓库托管管理（管理员视图）

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 22 | POST | `/admin/repo-sync` | `token` (query) + `account_name` (form) | Token | 加载指定账户的 GitCode 仓库 | HTML |
| 23 | POST | `/admin/repo-add` | `token` (query) + `account_name`, `full_name` (form) | Token | 为指定账户创建 webhook | HTML |
| 24 | POST | `/admin/repo-remove` | `token` (query) + `account_name`, `repo_owner`, `repo_name` (form) | Token | 移除指定账户的托管仓库 | HTML |

---

## 3. OAuth 联邦登录 — 端口 80

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 25 | GET | `/auth/gitcode/login` | 无 | 无 | 生成 state → 存内存 → 302 跳 GitCode 授权页 | 302 Redirect |
| 26 | GET | `/auth/gitcode/callback` | `code`, `state`, `error` (query) | state 验证 | 验证 state → code 换 token → 获取用户信息 → 创建/绑定用户 → session | HTML 面板 / 错误页 |

**OAuth 流程概要**
```
/login → 生成 state → 存 _oauth_store → 302 GitCode
├─ 用户授权 → GitCode 302 回 /callback?code=xxx&state=yyy
│  ├─ state 验证（内存 dict pop）→ 失败则报错
│  ├─ POST /oauth/token → access_token
│  ├─ GET /api/v5/user → gitcode_id + login
│  ├─ 查找/创建 user_accounts 条目
│  ├─ link_gitcode_user() → 存 user_credentials
│  ├─ save_user_account() → 自动填充 gitcode_api_token
│  └─ create_session_token() → 渲染面板
```

---

## 4. JSON API 端点 — 端口 80

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 27 | GET | `/api/admin/accounts` | `Authorization: Bearer <token>` (header) | Admin Token | 列出所有账户摘要 | JSON 数组 |
| 28 | GET | `/api/admin/accounts/{name}` | `Authorization: Bearer <token>` (header) | Admin Token | 获取单个账户详情 | JSON |
| 29 | GET | `/api/vm-test` | `token` (query) + `vm_id` (query) | User Token | TCP 端口连通性测试（非 SSH 认证） | `{"status":"ok"}` / `{"status":"fail","error":"..."}` |
| 30 | OPTIONS | `*` (任意路径) | 无 | 无 | CORS 预检 | 200 + CORS headers |

---

## 5. Webhook / A2A / 辅助端点 — 端口 8080

| # | 方法 | 路径 | 参数 | 认证 | 说明 | 响应 |
|---|------|------|------|------|------|------|
| 31 | POST | `/webhook/gitcode` | Body: raw bytes; Header: `X-GitCode-Token` | Webhook Token | 接收 GitCode 事件推送，提交到事件处理器 | `200 ok` / `403 invalid webhook token` |
| 32 | POST | `/api/a2a` | Body: JSON | 无 | Agent-to-Agent 协议，触发另一 Agent | `{"status":"ok"}` |
| 33 | GET | `/health` | 无 | 无 | 健康检查 | `{"status":"ok","agents":[...],"skills":[...]}` |
| 34 | GET | `/api/sessions` | 无 | 无 | 列出所有 Session | JSON 数组 |
| 35 | GET | `/api/user-mapping` | `login` (query), `repo_key` (query) | 无 | 查询 GitCode 用户→飞书映射 | JSON |
| 36 | GET | `/admin*` | 无 | 无 | 302 重定向到端口 80 的 admin 面板 | 302 → `http://host:80/admin/` |

---

## 6. 认证方式汇总

| 方式 | 传输方式 | 适用端点 | 存储位置 |
|------|---------|---------|---------|
| **User Token** | `?token=` query param | 用户面板所有端点 | 内存 dict（24h 过期） |
| **Admin Token** | `?token=` query param | 管理员面板所有端点 | 内存 dict（24h 过期） |
| **API Bearer** | `Authorization: Bearer <token>` header | `/api/admin/accounts*` | 同 Admin Token（`_valid_token`） |
| **Webhook Token** | `X-GitCode-Token` header | `/webhook/gitcode` | 全局配置 `GITCODE_WEBHOOK_TOKEN` |
| **无认证** | — | OAuth, Health, Sessions, A2A, User-Mapping | — |

---

## 7. 响应格式

| 类型 | 说明 | Content-Type |
|------|------|-------------|
| **HTML** | 服务端渲染页面（内嵌 CSS） | `text/html; charset=utf-8` |
| **JSON** | AJAX API 响应 | `application/json` |
| **纯文本** | Webhook 响应 | 无（默认 `text/html`） |
| **302** | 重定向 | 无 |

---

## 8. 后续重构建议

1. **认证统一**：User 和 Admin 用不同的 token 机制，前端传递方式也不一致（query vs header），可统一为 `Authorization: Bearer`
2. **响应标准化**：HTML 与 JSON 混用，前后端分离后应全部改为 JSON + RESTful
3. **参数格式**：POST 表单混合（form 参数名以 `snake_case` 为主）
4. **错误处理**：HTML 错误嵌入页面文本中，API 无统一错误格式
5. **端口拆分**：80（面板）和 8080（Webhook）分开，前端 SPA 只需关注 80 端口的 JSON API
