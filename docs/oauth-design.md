# DevOps Bot GitCode OAuth 联邦登录 — 设计文档

## 概述

允许用户通过 GitCode 账号一键登录 DevOps Bot，无需管理员预先创建账号。复用 GitCode 的账户体系，同时绑定到 DevOps Bot 本地用户。

## 前提条件

管理员需在 GitCode 创建一个 OAuth App，然后在 admin 面板配置：

| 字段 | 位置 | 说明 |
|------|------|------|
| `gitcode_oauth_client_id` | admin 面板 → GitCode 分组 | GitCode OAuth App 的 client_id |
| `gitcode_oauth_client_secret` | 同上 | GitCode OAuth App 的 client_secret |
| `gitcode_oauth_redirect_uri` | 同上 | 回调 URL，如 `http://203.0.113.2:80/auth/gitcode/callback` |

以上三个字段在 master 分支中已存在（`BOT_ACCOUNT_FIELDS` 包含，admin 面板已渲染）。

## OAuth 流程

```
┌─ 浏览器 ─┐      ┌─ DevOps Bot ─┐      ┌─ GitCode ─┐
│          │      │              │      │           │
│ 点击登录  │ ──→ │ 生成 state   │      │           │
│          │      │ 存 oauth_states     │           │
│          │ ←── │ 302 跳转     │      │           │
│          │ ──→ │              │ ──→ │ 授权页面   │
│          │      │              │      │ 用户确认   │
│          │ ←── │              │ ←── │ code+state│
│          │      │ 验证 state   │      │           │
│          │      │ code 换 token│ ──→ │ /oauth/token
│          │      │ token 换用户 │ ──→ │ /api/v5/user
│          │      │ 创建/绑定    │      │           │
│          │      │ 生成 session │      │           │
│          │ ←── │ 用户面板     │      │           │
└──────────┘      └──────────────┘      └───────────┘
```

## 数据库变更

### users 表新增字段

```sql
ALTER TABLE users ADD COLUMN gitcode_id TEXT DEFAULT '';
```

`gitcode_id` 存储 GitCode 的用户数字 ID（对象 ID，如 `"11652785"`），用于去重绑定。

### 新增 oauth_states 表

```sql
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    data_json TEXT DEFAULT '{}',
    expires REAL NOT NULL
)
```

- `state`：随机字符串，CSRF 防伪
- `provider`：固定 `"gitcode"`
- `data_json`：回调后需要恢复的数据（如登录前所在页面）
- `expires`：过期时间戳（10 分钟后）

## API 端点

### GET /auth/gitcode/login

生成随机 state → 存入 `oauth_states` 表 → 302 跳转到 GitCode 授权页：

```
https://gitcode.com/oauth/authorize
  ?client_id={client_id}
  &redirect_uri={callback_url}
  &response_type=code
  &scope=read_user
  &state={state}
```

- `client_id` 从 `bot_accounts.gitcode_oauth_client_id` 读取
- `redirect_uri` 优先用配置的 `gitcode_oauth_redirect_uri`，未配置则自动从请求头拼接

### GET /auth/gitcode/callback

接收 GitCode 回调：

```
1. 验证 state → 从 oauth_states 表查找并删除（一次性使用，防 replay）
2. POST https://gitcode.com/oauth/token → 获取 access_token
   grant_type=authorization_code&code={code}&client_id={client_id}
   &client_secret={client_secret}&redirect_uri={redirect_uri}
3. GET https://api.gitcode.com/api/v5/user → 获取用户信息
   Authorization: Bearer {access_token}
4. 查找/创建本地用户：
   a. users.gitcode_id == gitcode_id → 已绑定，直接登录
   b. users.username == gitcode_login → 绑定 gitcode_id 后登录
   c. 都不存在 → 创建新用户 + 绑定 gitcode_id
5. 生成 session token → 渲染用户面板
```

## 新增函数（src/user/db.py）

```python
def save_oauth_state(state, provider, data=None):
    """保存 OAuth state（防 CSRF，10 分钟过期）"""

def validate_oauth_state(state, provider) -> dict | None:
    """验证并消耗 OAuth state（查 + 删），返回 data 或 None"""

def get_user_by_gitcode_id(gitcode_id: str) -> dict | None:
    """通过 GitCode ID 查找本地用户"""

def link_gitcode_user(user_id, gitcode_id, access_token, refresh_token=""):
    """绑定本地用户与 GitCode ID，存储 access_token 到 user_credentials"""
```

所有 DDL 统一在 `init_db()` 中处理，不在模块级别执行额外 SQL。

## UI 变更

### 登录页（_render_user_login）

在密码表单下方增加 OAuth 入口。仅当配置了 `gitcode_oauth_client_id` 时显示：

```
┌──────────────────────┐
│  用户名 [________]    │
│  密码   [________]    │
│    [登录]            │
│  ─────── 或 ────────  │
│  [使用 GitCode 登录]  │
│   ← 管理员登录       │
└──────────────────────┘
```

### 用户面板基本信息（_render_user_panel）

在基本信息区增加 GitCode 绑定状态：

```
用户名: xxx
用户 ID: xxx
GitCode 绑定: ✅ 已绑定 / ❌ 未绑定
```

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/user/db.py` | `init_db()` 增加 `oauth_states` 表 DDL + ALTER TABLE + `_cleanup_expired_oauth_states()`；新增 `save_oauth_state()`、`validate_oauth_state()`、`get_user_by_gitcode_id()`、`link_gitcode_user()` |
| `src/admin/server.py` | `do_GET` 新增 `/auth/gitcode/login` 和 `/auth/gitcode/callback` 路由；`_render_user_login()` 增加 OAuth 登录按钮；`_render_user_panel()` 增加 GitCode 绑定状态 |

## 与之前 Agent 实现的关键区别

| 问题 | Agent 实现的问题 | 本方案 |
|------|----------------|--------|
| DDL 执行时机 | `_ensure_gitcode_column()` 在模块级定义两次，第二次覆盖第一次，导致死锁 | 所有 DDL 统一在 `init_db()` 中处理，不在模块级执行额外 SQL |
| `db.commit()` 位置 | 在 `init_db()` 的 `with db_lock` 内外各调一次 | 统一在 `with db_lock` 内调一次 |
| `list_users()` 中的列 | 每次调用都 `_ensure_gitcode_column()`，增加锁竞争 | DDL 在 `init_db()` 时已处理，查询直接取字段 |
| 模块级代码 | `_ensure_gitcode_column()` 在模块级调用 | 无模块级多余代码（仅调用 `init_db()`） |

## 安全措施

| 措施 | 说明 |
|------|------|
| state 防 CSRF | 随机 state 存 DB，一次性使用，10 分钟过期 |
| access_token 存储 | 存于 `user_credentials` 表（已有凭据存储机制） |
| HTTPS 回调 | redirect_uri 自动检测请求协议 |
| 错误处理 | 每一步错误返回具体提示给用户，不泄漏敏感信息 |
