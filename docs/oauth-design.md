# DevOps Bot GitCode OAuth 联邦登录 — 设计文档

## 概述

允许用户通过 GitCode 账号一键登录 DevOps Bot，无需管理员预先创建账号。复用 GitCode 的账户体系，同时绑定到 DevOps Bot 本地用户。

## 前提条件

管理员需在 GitCode 创建一个 OAuth App（[设置页面](https://gitcode.com/setting/oauth)），然后在 admin 面板配置：

| 字段 | 位置 | 说明 |
|------|------|------|
| `gitcode_oauth_client_id` | admin 面板 → GitCode 分组 | GitCode OAuth App 的 client_id |
| `gitcode_oauth_client_secret` | 同上 | GitCode OAuth App 的 client_secret |
| `gitcode_oauth_redirect_uri` | 同上 | 回调 URL，如 `http://203.0.113.2:80/auth/gitcode/callback` |

> `gitcode_oauth_redirect_uri` 必须存在于 `USER_ACCOUNT_FIELDS` 中，否则保存时会被忽略。  
> GitCode OAuth App 注册的回调 URL 必须与此值**完全一致**（包括端口号）。

## OAuth 流程

```
┌─ 浏览器 ─┐      ┌─ DevOps Bot ─┐      ┌─ GitCode ─┐
│          │      │              │      │           │
│ 点击登录  │ ──→ │ 生成 state   │      │           │
│          │      │ 存内存 store │      │           │
│          │ ←── │ 302 跳转     │      │           │
│          │      │ (no-cache)   │      │           │
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

### oauth_states 表（保留但不再用于 OAuth state）

`oauth_states` SQLite 表作为兼容性保留，OAuth state 实际使用**进程内内存存储**（`_oauth_store` dict），原因是 SQLite 在多线程场景下存在跨线程事务隔离问题，state 写入后可能在另一线程不可见。

```python
# src/user/db.py
_oauth_store = {}               # 内存 dict
_oauth_lock = threading.Lock()  # 专用锁
```

- 写入（`save_oauth_state`）：加锁 → dict 赋值 → 解锁
- 读取并消耗（`validate_oauth_state`）：加锁 → `pop(state, None)` → 解锁
- 10 分钟过期（调用方控制，未单独清理线程）

## API 端点

### GET /auth/gitcode/login

生成随机 state → 存入 `_oauth_store` → 302 跳转到 GitCode 授权页（**带 no-cache 头，防浏览器缓存 302**）：

```
https://gitcode.com/oauth/authorize
  ?client_id={client_id}
  &redirect_uri={callback_url}
  &response_type=code
  &scope=read_user
  &state={state}
```

- `client_id` 从 `user_accounts.gitcode_oauth_client_id` 读取
- `redirect_uri` 优先用配置的 `gitcode_oauth_redirect_uri`，未配置则自动从请求头拼接（含端口号）

### GET /auth/gitcode/callback

接收 GitCode 回调：

```
1. 验证 state → 从 _oauth_store 查找并删除（一次性使用，防 replay）
   → 返回 dict {}（空字典），不是 None
   → 回调中判断：if state_data is None:  # 而不是 if not state_data:
                                  ↑ 空字典 {} 在 Python 中是 falsy
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
    """保存 OAuth state（防 CSRF，10 分钟过期，内存存储）"""

def validate_oauth_state(state, provider) -> dict | None:
    """验证并消耗 OAuth state（pop + 校验），返回 data 或 None。
    注意：返回空 dict {} 表示成功，回调必须用 is None 判断。"""

def get_user_by_gitcode_id(gitcode_id: str) -> dict | None:
    """通过 GitCode ID 查找本地用户"""

def link_gitcode_user(user_id, gitcode_id, access_token, refresh_token=""):
    """绑定本地用户与 GitCode ID，存储 access_token 到 user_credentials"""
```

所有 DDL 统一在 `init_db()` 中处理，不在模块级别执行额外 SQL。

## 陷坑记录

| 问题 | 症状 | 根因 | 修复 |
|------|------|------|------|
| state 存不入 DB | state 从未找到 | SQLite 跨线程事务隔离：线程 A 写入后线程 B 读取不到 | 改为内存 dict 存储 |
| state 验证返回空字典导致失败 | 日志显示 FOUND 但仍报错 | `validate_oauth_state` 返回 `{}`，`if not state_data:` 认为 `{}` 为 falsy | 改为 `if state_data is None:` |
| state 始终返回 None | 每次验证都失败 | `return entry["data"]` 缩进错误，位于 `if` 块内成为死代码 | 修正缩进 |
| redirect_uri 不保存 | admin 填入后刷新就没了 | `gitcode_oauth_redirect_uri` 未加入 `USER_ACCOUNT_FIELDS` | 加入字段列表 |
| 浏览器缓存 302 | 再次点击登录时直接跳到 GitCode，state 未更新 | 302 响应无 cache-control 头 | 添加 no-cache 头 |
| `log("warning", ...)` 调用错误 | Logger 对象不可调用 | 误用 `log("warning", msg)` 而非 `log.warning(msg)` | 修正调用方式 |

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
| `src/user/db.py` | `init_db()` 增加 `oauth_states` 表 DDL + ALTER TABLE + `_cleanup_expired_oauth_states()`；新增 `save_oauth_state()`、`validate_oauth_state()`、`get_user_by_gitcode_id()`、`link_gitcode_user()`；`USER_ACCOUNT_FIELDS` 增加 `gitcode_oauth_redirect_uri` |
| `src/admin/server.py` | `do_GET` 新增 `/auth/gitcode/login` 和 `/auth/gitcode/callback` 路由；`_render_user_login()` 增加 OAuth 登录按钮；`_render_user_panel()` 增加 GitCode 绑定状态；302 跳转添加 no-cache 头 |

## 安全措施

| 措施 | 说明 |
|------|------|
| state 防 CSRF | 随机 state 存内存 dict（`_oauth_store`），一次性使用（pop），10 分钟过期 |
| access_token 存储 | 存于 `user_credentials` 表 + `user_accounts.gitcode_api_token`（OAuth 登录后自动填充） |
| HTTPS 回调 | redirect_uri 自动检测请求协议 |
| 302 防缓存 | Cache-Control: no-cache, no-store, must-revalidate + Pragma: no-cache + Expires: 0 |
| 错误处理 | 每一步错误返回具体提示给用户，不泄漏敏感信息 |
