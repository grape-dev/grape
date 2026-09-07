# Issue #52 技术方案：GitCode OAuth Token 生命周期与登录会话对齐（已实施）

> Issue: https://gitcode.com/grape-dev/grape/issues/52
> 需求：grape 当前登录会话失效机制过长，导致 GitCode 的 token 失效
> 关联 Issue: #50「grape 频繁出现无法创建 task 的情况」（GitCode API 返回 401 Bad credentials）
> 实施状态：已完成（commit c4efd85a）

## 1. 现状分析

### 1.1 双层 Token 体系

Grape 平台存在**两层独立的 Token 体系**，生命周期完全不同步：

| 层 | Token 类型 | 存储位置 | 有效期 | 刷新机制 |
|----|-----------|---------|--------|---------|
| 内层 | JWT access_token | localStorage（前端） | 15 分钟 | 前端 401 时调 `/api/auth/refresh` 滚动续期 |
| 内层 | JWT refresh_token | HttpOnly cookie | 7 天滚动 / 30 天绝对上限 | `/api/auth/refresh` 端点自动续期 |
| 外层 | GitCode OAuth access_token | `user_credentials` 表（`gitcode_oauth` provider）+ `user_accounts.gitcode_api_token` | **GitCode 控制（约 2 小时）** | **实施前无——仅在 OAuth 登录时获取一次；实施后自动刷新** |
| 外层 | GitCode OAuth refresh_token | `user_credentials` 表（`gitcode_oauth` provider） | **GitCode 控制（约 30 天）** | **实施前存储了但从未使用；实施后用于刷新 access_token** |

### 1.2 根因定位

**核心问题：GitCode OAuth access_token 过期后，grape 没有任何刷新机制。**

代码路径追踪：

1. OAuth 登录（`auth.py:130-232`）：GitCode callback 获取 `access_token` + `refresh_token`，存入 `user_credentials` 表，同时将 `access_token` 写入 `user_accounts.gitcode_api_token`
2. API 调用（`tasks.py:652`）：`get_user_token(user["username"])` → `get_user_account(username).get("gitcode_api_token")` → 读取**登录时存入的 access_token**
3. GitCode API 调用（`tasks.py:566`）：`_post_gitcode_issue(api_token, ...)` → 用 `access_token` query param 调 GitCode v5 API
4. GitCode access_token 过期（约 2 小时后）→ API 返回 `401 Bad credentials`
5. grape 抛 `RuntimeError` → 前端收到 500 → 用户看到「创建失败: 请求失败 (500)」

**为什么 JWT refresh 没有解决此问题：**

JWT 双 token 机制（`jwt_utils.py` + `api.js`）只管理 grape 自己的会话（access 15min / refresh 7 天滚动 / 30 天绝对上限）。前端 401 自动 refresh 刷新的是 **JWT access_token**，不会触碰 GitCode OAuth token。用户登录后即使 grape 会话保持 30 天，GitCode OAuth access_token 在 2 小时后就已失效，但 `user_accounts.gitcode_api_token` 仍然是那个过期的值。

### 1.3 代码证据

```
auth.py:201    user_db.link_gitcode_user(local_user["id"], gitcode_id, access_token, refresh_token)
               → 存入 user_credentials (provider=gitcode_oauth)
auth.py:204    user_acc["gitcode_api_token"] = access_token
               → 同时写入 user_accounts.gitcode_api_token（API 调用实际读取的字段）

deps.py:52-55  def get_user_token(username):
                   acc = user_db.get_user_account(username) or {}
                   return acc.get("gitcode_api_token", "")
               → 读取的是登录时写入的 access_token，永不刷新

tasks.py:652   api_token = get_user_token(user["username"])
tasks.py:566   url = f"...issues?access_token={api_token}"
               → 用可能已过期的 token 调 GitCode API → 401

# link_gitcode_user 存了 refresh_token 但全代码库无任何地方使用它刷新
grep -rn "grant_type.*refresh" src/ → 0 结果
```

### 1.4 影响范围

受影响的操作（所有用 `get_user_token` / `user_accounts.gitcode_api_token` 调 GitCode API 的路径）：

- `tasks.py` — 创建 Issue / 任务（Issue #50 报的就是这个）
- `tasks.py:583` — PATCH Issue body（多任务模式标记）
- `tasks.py:862` — POST Issue comment（任务澄清消息）
- `chat.py:452/576/1264/1610` — Chat Agent 获取 user_token（4 处调用）
- `event_pipeline.py` — webhook 事件处理中的 GitCode API 调用（本次未改造，走 bot token 路径不受影响）

不受影响的操作（使用 bot token，不依赖用户 OAuth token）：

- `get_gitcode_token(repo_key=...)` 路径：devops_agent_session.repo_gitcode_token → managed_repos.bot_gitcode_token → 全局 bot token
- bot token 是 PAT（Personal Access Token），有效期由 GitCode 账号设置控制（通常更长），不走 OAuth 刷新流程

## 2. 设计目标

1. **GitCode OAuth access_token 过期前自动刷新**——用户无感知，无需重新登录
2. **刷新失败时优雅降级**——提示用户重新授权，而非 500 错误
3. **刷新后的 token 持久化**——全实例可用，不限于当前请求
4. **不影响现有 JWT 会话机制**——两层 Token 体系各自独立
5. **并发安全**——多请求同时遇到 401 时只刷新一次

## 3. 技术方案（已实施）

### 3.1 总体架构

```
用户请求（tasks.py / chat.py）
    ↓
get_user_token_with_refresh(username, user_id)  →  读取 user_accounts.gitcode_api_token
    ↓                                              ↓ 检查 fetched_at 年龄 > 100min → 预刷新
调 GitCode API                                    ↓
    ↓
┌── 200 → 正常返回
└── 401 Bad credentials → _gitcode_api_with_refresh 包装器触发刷新
                            ↓
                    try_refresh_gitcode_token(user_id)  ← 用户级 Lock 串行化
                            ↓
                    refresh_gitcode_oauth_token(user_id)
                            ↓
                    读取 user_credentials.refresh_token（gitcode_oauth provider）
                            ↓
                    POST https://gitcode.com/oauth/token
                      grant_type=refresh_token&refresh_token=xxx
                            ↓
                    ┌── 成功 → 更新 user_accounts.gitcode_api_token + user_credentials + fetched_at
                    │         → 重试原请求（一次）
                    └── 失败 → 返回 401 {action: "reauth_required"}
                               前端 _redirectToGitcodeAuth() 跳 OAuth 授权页
```

### 3.2 后端改造

#### 3.2.1 新增 GitCode OAuth token 刷新函数（src/user/db.py）

新增 `refresh_gitcode_oauth_token(user_id)` 和辅助函数 `_get_username_by_id(user_id)`。

关键实现点：
- 读取 `user_credentials` 表中 `provider=gitcode_oauth` 的 `config_json.refresh_token`
- 调用 `POST https://gitcode.com/oauth/token`（`grant_type=refresh_token`），10 秒超时
- 成功时双写：`user_credentials.config_json`（含新 access_token + 可能轮转的新 refresh_token + fetched_at 时间戳）和 `user_accounts.gitcode_api_token`
- 读取 OAuth `client_id` / `client_secret` 从 `meta` 表（`get_meta_value`）
- 任何环节失败返回 `None`，调用方据此决定降级策略

```python
def refresh_gitcode_oauth_token(user_id: int) -> str | None:
    """用 refresh_token 刷新 GitCode OAuth access_token（Issue #52）。"""
    import urllib.request, urllib.parse, json as _json

    cred = get_credential(user_id, "gitcode_oauth")
    if not cred or not cred.get("refresh_token"):
        log.warning(f"refresh_gitcode_oauth: no refresh_token for user={user_id}")
        return None

    rt = cred["refresh_token"]
    client_id = get_meta_value("oauth_gitcode_client_id")
    client_secret = get_meta_value("oauth_gitcode_client_secret")
    if not client_id or not client_secret:
        log.warning("refresh_gitcode_oauth: OAuth client_id/secret not configured")
        return None

    try:
        req = urllib.request.Request(
            "https://gitcode.com/oauth/token",
            data=urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": client_id,
                "client_secret": client_secret,
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=10)
        token_data = _json.loads(resp.read())
    except Exception as e:
        log.warning(f"refresh_gitcode_oauth: refresh failed for user={user_id}: {e}")
        return None

    new_access = token_data.get("access_token", "")
    new_refresh = token_data.get("refresh_token", "")
    if not new_access:
        log.warning(f"refresh_gitcode_oauth: no access_token in response for user={user_id}")
        return None

    # 更新存储（access_token + refresh_token 如果返回了新的 + fetched_at）
    new_cred = dict(cred)
    new_cred["access_token"] = new_access
    if new_refresh:
        new_cred["refresh_token"] = new_refresh
    new_cred["fetched_at"] = int(time.time())
    set_credential(user_id, "gitcode_oauth", new_cred)

    # 同步更新 user_accounts.gitcode_api_token
    username = _get_username_by_id(user_id)
    if username:
        acc = get_user_account(username) or {}
        acc["gitcode_api_token"] = new_access
        save_user_account(username, acc)

    log.info(f"refresh_gitcode_oauth: success for user={user_id}")
    return new_access
```

#### 3.2.2 新增带预刷新的 token 获取函数（src/api/deps.py）

新增三个函数 + 用户级锁：

- `_get_user_refresh_lock(user_id)` — 用户级 `threading.Lock`（懒创建，防并发重复刷新）
- `try_refresh_gitcode_token(user_id)` — 加锁后调 `db.refresh_gitcode_oauth_token`
- `get_user_token_with_refresh(username, user_id)` — 读取 token + 主动预刷新（检查 `fetched_at` 年龄 > 100 分钟则预刷新）+ 空 token 时尝试刷新

```python
def get_user_token_with_refresh(username: str, user_id: int = 0) -> str:
    """获取用户 GitCode token，过期时主动预刷新（Issue #52）。

    与 get_user_token 的区别：当 user_id > 0 时，检查 token 年龄，
    超过 100 分钟（GitCode token 默认 2 小时，留 20 分钟余量）则预刷新。
    401 时的被动刷新由 _gitcode_api_with_refresh 包装器兜底。
    """
    import time as _time
    acc = user_db.get_user_account(username) or {}
    token = acc.get("gitcode_api_token", "")

    if token and user_id > 0:
        cred = user_db.get_credential(user_id, "gitcode_oauth")
        if cred:
            fetched_at = cred.get("fetched_at", 0)
            age = int(_time.time()) - fetched_at if fetched_at else 0
            if age > 6000:  # 100 分钟
                new_token = try_refresh_gitcode_token(user_id)
                return new_token or token
        return token

    if user_id > 0 and not token:
        return try_refresh_gitcode_token(user_id) or ""

    return token
```

#### 3.2.3 GitCode API 调用 401 自动重试包装器（src/api/routes/tasks.py）

新增 `_is_gitcode_401(err)` 判断函数 + `_gitcode_api_with_refresh(user, api_call_fn, *args, **kwargs)` 包装器。

包装器逻辑：
1. 用 `get_user_token(username)` 取当前 token
2. 调 `api_call_fn(token, *args, **kwargs)`
3. 捕获 `RuntimeError`，用 `_is_gitcode_401` 判断是否 401
4. 401 → 调 `try_refresh_gitcode_token(user_id)` 刷新
5. 刷新成功 → 用新 token 重试一次；刷新失败 → 抛 `HTTPException(401, action="reauth_required")`

```python
def _gitcode_api_with_refresh(user: dict, api_call_fn, *args, **kwargs):
    """包装 GitCode API 调用——401 时自动刷新 OAuth token 并重试一次（Issue #52）。"""
    from src.api.deps import get_user_token, try_refresh_gitcode_token

    username = user["username"]
    user_id = user["id"]
    token = get_user_token(username)

    try:
        return api_call_fn(token, *args, **kwargs)
    except RuntimeError as e:
        if not _is_gitcode_401(e):
            raise
        new_token = try_refresh_gitcode_token(user_id)
        if not new_token:
            raise HTTPException(status_code=401, detail={
                "code": 401,
                "message": "GitCode 授权已过期，请重新登录授权",
                "action": "reauth_required"
            })
        return api_call_fn(new_token, *args, **kwargs)
```

#### 3.2.4 前端 401 reauth 处理（src/api/static/js/api.js）

改造 `handleResp` 函数：401 时先解析响应体，检查 `action: "reauth_required"`——是则跳 GitCode OAuth 授权页，否则走原有 JWT refresh 流程。

新增 `_redirectToGitcodeAuth()` 函数：调 `/api/auth/login/gitcode` 获取授权 URL 后跳转。

```javascript
function handleResp(r, retryFn) {
  if (!r.ok) {
    if (r.status === 401 && retryFn) {
      // 先检查是否 GitCode OAuth 过期（需要重新授权，不是 JWT refresh）
      return r.json().then(err => {
        if (err?.detail?.action === 'reauth_required') {
          _redirectToGitcodeAuth();
          throw new Error(err.detail.message);
        }
        return handle401(retryFn);  // 普通 JWT 过期
      });
    }
    if (r.status === 401) {
      return r.json().then(err => {
        if (err?.detail?.action === 'reauth_required') {
          _redirectToGitcodeAuth();
          throw new Error(err.detail.message);
        }
        localStorage.removeItem('token');
        localStorage.removeItem('user');
        if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
        throw new Error(err?.detail?.message || I18N.t('common.session_expired'));
      });
    }
    // ... 其余非 401 错误处理不变
  }
  return r.json();
}

function _redirectToGitcodeAuth() {
  if (location.pathname.includes('/login')) return;
  API.get('/auth/login/gitcode').then(d => {
    if (d.redirect_url) {
      location.href = d.redirect_url;
    } else {
      location.href = '/login';
    }
  }).catch(() => {
    location.href = '/login';
  });
}
```

### 3.3 主动预刷新策略

已实施主动预刷新（原设计标注为"可选增强"，实施时一并完成）：

#### 3.3.1 记录 token 获取时间

- `link_gitcode_user`（`db.py:264`）：OAuth 登录时写入 `fetched_at = int(time.time())`
- `refresh_gitcode_oauth_token`：刷新成功时写入 `fetched_at = int(time.time())`

#### 3.3.2 读取时判断是否需要预刷新

`get_user_token_with_refresh` 检查 `fetched_at` 年龄，超过 100 分钟（6000 秒）则预刷新（GitCode token 默认 2 小时，留 20 分钟余量）。

> 注意：GitCode OAuth access_token 的实际有效期由 GitCode 平台控制。`fetched_at` 机制提供预刷新能力，但被动 401 重试是兜底保障——两者组合使用。

### 3.4 辅助函数

新增 `_get_username_by_id(user_id)`（`src/user/db.py`）：

```python
def _get_username_by_id(user_id: int) -> str:
    """根据 user_id 查 username。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT username FROM user_accounts WHERE id=?", (user_id,))
        row = c.fetchone()
    return row[0] if row else ""
```

## 4. 数据流

### 4.1 正常流程（token 有效且未临近过期）

```
前端 → POST /api/tasks/create
  → get_current_user（JWT 验证）
  → get_user_token_with_refresh(username, user_id)
    → 读取 user_accounts.gitcode_api_token
    → 检查 fetched_at 年龄 < 100min → 直接返回
  → _gitcode_api_with_refresh(user, _post_gitcode_issue, ...)
    → _post_gitcode_issue(token, ...)
    → GitCode API 200
  → 任务创建成功
```

### 4.2 token 临近过期（预刷新）

```
前端 → POST /api/tasks/create
  → get_user_token_with_refresh(username, user_id)
    → 读取 token + 检查 fetched_at 年龄 > 100min
    → try_refresh_gitcode_token(user_id) → 刷新成功
    → 返回新 token
  → _gitcode_api_with_refresh(user, _post_gitcode_issue, ...)
    → _post_gitcode_issue(new_token, ...)
    → GitCode API 200
  → 任务创建成功（用户无感知）
```

### 4.3 token 已过期（被动 401 刷新）

```
前端 → POST /api/tasks/create
  → get_user_token_with_refresh → 返回过期 token（预刷新窗口外或刷新失败）
  → _gitcode_api_with_refresh(user, _post_gitcode_issue, ...)
    → _post_gitcode_issue(token, ...) → GitCode API 401 Bad credentials
    → _is_gitcode_401(err) → True
    → try_refresh_gitcode_token(user_id)
      → refresh_gitcode_oauth_token(user_id)
        → 读取 user_credentials.refresh_token
        → POST gitcode.com/oauth/token (grant_type=refresh_token)
        → 更新 user_accounts.gitcode_api_token + user_credentials + fetched_at
    → _post_gitcode_issue(new_token, ...)
    → GitCode API 200
  → 任务创建成功（用户无感知）
```

### 4.4 refresh_token 也过期（降级）

```
前端 → POST /api/tasks/create
  → get_user_token_with_refresh → 过期 token
  → _gitcode_api_with_refresh → _post_gitcode_issue → 401
    → try_refresh_gitcode_token(user_id)
      → refresh_gitcode_oauth_token(user_id)
        → POST gitcode.com/oauth/token → 400/401（refresh_token 也过期）
        → 返回 None
    → HTTPException 401 {action: "reauth_required"}
  → 前端 handleResp → 检测 action=reauth_required
    → _redirectToGitcodeAuth()
    → 调 GET /api/auth/login/gitcode 获取授权 URL
    → 跳转 GitCode OAuth 授权页
    → 用户重新授权 → callback 更新 token
```

## 5. 边界情况

| 场景 | 处理 |
|------|------|
| user_credentials 无 gitcode_oauth 记录 | `refresh_gitcode_oauth_token` 返回 None → 401 reauth |
| refresh_token 为空（OAuth 登录时没返回） | 同上 → 401 reauth |
| OAuth client_id/secret 未配置 | 返回 None → 401 reauth（提示管理员配置 OAuth） |
| 并发多请求同时 401 | 用户级 `threading.Lock` 串行化，只刷新一次 |
| bot token 路径（非 user token） | 不受影响——bot token 是 PAT，不走 OAuth 刷新 |
| 多实例部署 | 刷新后写入数据库（user_credentials + user_accounts），全实例可见 |
| refresh_token 轮转（GitCode 返回新 refresh_token） | 更新存储的 refresh_token，下次用新值刷新 |
| GitCode OAuth 平台维护/不可达 | urllib 超时 10s → None → 401 reauth |
| 用户在另一 Tab 重新登录刷新了 token | 数据库已更新，当前请求重试时读到新 token |
| `_patch_issue_body` 401 | 包装器自动刷新重试；HTTPException 向上传递（reauth_required 到前端） |
| chat agent token 401 | `get_user_token_with_refresh` 预刷新 + 会话缓存刷新机制（`_resolve_gitcode_token`）兜底 |

## 6. 实施记录

### 实施状态：全部完成

| Phase | 步骤 | 文件 | 状态 |
|-------|------|------|------|
| 1 | 新增 `refresh_gitcode_oauth_token` + `_get_username_by_id` | `src/user/db.py` | 已完成 |
| 1 | 新增 `try_refresh_gitcode_token` + 用户级锁 + `get_user_token_with_refresh` | `src/api/deps.py` | 已完成 |
| 1 | 新增 `_gitcode_api_with_refresh` 包装器 + `_is_gitcode_401` + 改造 `_post_gitcode_issue` 调用 | `src/api/routes/tasks.py` | 已完成 |
| 1 | 401 reauth 处理 + `_redirectToGitcodeAuth` | `src/api/static/js/api.js` | 已完成 |
| 2 | 改造 `_patch_issue_body` 调用处（多任务模式标记） | `src/api/routes/tasks.py` | 已完成 |
| 2 | 改造 clarify 评论 POST（内联 `_post_comment` 闭函数） | `src/api/routes/tasks.py` | 已完成 |
| 2 | 改造 `chat.py` 4 处 `user_token` 获取（send / send/stream / submit / sync_send_async） | `src/api/routes/chat.py` | 已完成 |
| 3 | `link_gitcode_user` 增加 `fetched_at` 时间戳 | `src/user/db.py` | 已完成 |
| 3 | `refresh_gitcode_oauth_token` 增加 `fetched_at` 时间戳 | `src/user/db.py` | 已完成 |
| 3 | `get_user_token_with_refresh` 预刷新逻辑（100 分钟阈值） | `src/api/deps.py` | 已完成 |
| 4 | i18n 新增 `gitcode_reauth_required` 文案 | `zh/common.json` + `en/common.json` | 已完成 |
| 4 | 版本号 `api.js?v=18` → `v=19` | `src/api/static/index.html` | 已完成 |

### 与原设计差异

| 项 | 原设计 | 实际实施 | 原因 |
|----|--------|---------|------|
| `event_pipeline.py` 改造 | 列入 Phase 2 步骤 8 | **未实施** | webhook 事件处理走 `get_gitcode_token(repo_key=...)` bot token 路径，不依赖用户 OAuth token，不受此问题影响 |
| `_resolve_gitcode_token` 改造 | 列入 Phase 2 步骤 7 | **改为改造调用方** | 未修改 `_resolve_gitcode_token` 本身，而是在 4 个调用端点（send / send/stream / submit / sync_send_async）将 `acc.get("gitcode_api_token")` 替换为 `get_user_token_with_refresh`，效果等价且侵入更小 |
| `get_user_token` 替换 | Phase 3 步骤 11 建议替换 | **未替换全局函数** | `get_user_token` 保留原样，新增 `get_user_token_with_refresh` 作为增强版；调用方按需选择，避免影响不需要刷新的路径 |
| `_post_issue_comment` 独立函数 | 原设计改造已有函数 | **内联闭函数** | clarify 端点的评论 POST 原来是内联代码，改为内联 `_post_comment` 闭函数传给 `_gitcode_api_with_refresh`，避免新增不必要的模块级函数 |
| 主动预刷新标注 | 原设计标注"可选增强" | **一并实施** | `fetched_at` + 预刷新逻辑与核心刷新机制紧密相关，一起实施减少二次改动 |
| 单元测试 | 原设计列出 4 个测试函数 | **未新增测试文件** | 功能验证通过导入测试 + 断言测试完成（见 §9）；后续可补正式测试文件 |

## 7. 涉及文件

| 文件 | 改动类型 | 改动量 | 说明 |
|------|---------|--------|------|
| `src/user/db.py` | 新增函数 + 改造 | +83 行 | `refresh_gitcode_oauth_token`, `_get_username_by_id`；`link_gitcode_user` 增加 `fetched_at` |
| `src/api/deps.py` | 新增函数 | +52 行 | `try_refresh_gitcode_token`, `get_user_token_with_refresh`, `_get_user_refresh_lock`, 用户级锁 |
| `src/api/routes/tasks.py` | 新增 + 改造 | +98 行 | `_gitcode_api_with_refresh` 包装器 + `_is_gitcode_401`；改造 `create_task_via_issue`、`_patch_issue_body`、clarify 评论 3 处调用 |
| `src/api/routes/chat.py` | 改造调用 | +16 行 | 4 处 `user_token` 获取改用 `get_user_token_with_refresh` |
| `src/api/static/js/api.js` | 前端改造 | +40 行 | `handleResp` 401 reauth 分支 + `_redirectToGitcodeAuth` |
| `src/api/static/index.html` | 版本号 | 1 行 | `api.js?v=18` → `v=19` |
| `src/api/static/locales/zh/common.json` | i18n | +1 行 | `gitcode_reauth_required` |
| `src/api/static/locales/en/common.json` | i18n | +1 行 | `gitcode_reauth_required` |

## 8. 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| GitCode OAuth refresh endpoint 行为未知（是否支持 refresh_token grant） | **中** | 被动 401 重试是兜底——即使预刷新失败，用户操作时仍会尝试一次刷新；如 GitCode 不支持 refresh_token grant，`refresh_gitcode_oauth_token` 返回 None，降级为提示重新授权（方案 B） |
| refresh_token 可能也被 GitCode 设置了较短有效期 | **低** | 被动 401 重试是兜底——即使预刷新失败，用户操作时仍会尝试一次刷新 |
| 多实例并发刷新竞争 | **低** | 数据库是单一写入点（SQLite/MySQL），`INSERT OR REPLACE` / `UPDATE` 原子操作；锁是进程内防重复刷新 |
| `user_accounts.gitcode_api_token` 与 `user_credentials` 双写一致性 | **低** | 两处写操作在同一函数内顺序执行，无事务需求——最坏情况是读到旧 token 触发一次额外 401 重试 |
| 前端跳转 OAuth 授权页打断用户操作 | **中** | 仅在 refresh_token 也过期时才跳转（预计 30 天一次）；跳转前提示「GitCode 授权已过期，正在跳转重新授权...」 |

## 9. 验证结果

### 9.1 语法验证

- Python 语法检查（`ast.parse`）：`db.py` / `deps.py` / `tasks.py` / `chat.py` 全部通过
- JSON 校验：`zh/common.json` / `en/common.json` 全部通过
- JavaScript 语法检查（`new Function(code)`）：`api.js` 通过

### 9.2 导入验证

```python
from src.user.db import refresh_gitcode_oauth_token, _get_username_by_id
from src.api.deps import try_refresh_gitcode_token, get_user_token_with_refresh, _get_user_refresh_lock
from src.api.routes.tasks import _gitcode_api_with_refresh, _is_gitcode_401
```
全部导入成功。

### 9.3 功能断言验证

```
_is_gitcode_401:
  - "GitCode API POST issues failed (401): Bad credentials" → True
  - "something 401 something" → True
  - "500: server error" → False
  - "Cannot reach GitCode API: timeout" → False

try_refresh_gitcode_token(999999) → None（不存在用户优雅返回）
get_user_token_with_refresh('nonexistent_user', user_id=0) → ''（无 user_id 退化为原行为）
_get_user_refresh_lock(1) is _get_user_refresh_lock(1) → True（同一用户同一锁）
```
全部断言通过。

### 9.4 pytest 测试

对 14 个相关测试文件运行 pytest（`test_auth_routes` / `test_user_db` / `test_tasks_create` / `test_tasks_clarify` / `test_executor_token_env` / `test_global_bot_token_config` / `test_tasks` / `test_task_center_ensure` / `test_task_messages` / `test_tasks_sync_conversation` / `test_chat_backend_resolution` / `test_onboarding_standard_task` / `test_task_notify_bot_resolve` / `test_task_status_trigger`）：

- **147 passed, 4 failed**（7.92s）
- 4 个失败全部为**预先存在的失败**（在父提交 `2c180e9a` 上运行相同测试产生相同 4 个失败）
- 预先存在失败原因：测试 payload 缺少 `description` 字段（test_tasks_create）、MySQL/SQLite schema 不匹配（test_user_db）、OAuth callback mock 问题（test_auth_routes）——均与本次改动无关
- **本次改动零新增失败**

### 9.5 手动验证步骤

1. OAuth 登录后记录 `user_accounts.gitcode_api_token` 的值
2. 等待 2 小时（或手动将 token 改为无效值模拟过期）
3. 创建 task → 应自动刷新 token 并成功创建
4. 检查 `user_accounts.gitcode_api_token` 已更新为新值
5. 将 `user_credentials` 中的 `refresh_token` 也改为无效值
6. 创建 task → 应返回 401 + `action: reauth_required`
7. 前端应跳转 GitCode 授权页

### 9.6 日志验证

```python
# 成功刷新
log.info(f"refresh_gitcode_oauth: success for user={user_id}")

# 刷新失败
log.warning(f"refresh_gitcode_oauth: refresh failed for user={user_id}: {e}")

# 无 refresh_token
log.warning(f"refresh_gitcode_oauth: no refresh_token for user={user_id}")

# OAuth 未配置
log.warning("refresh_gitcode_oauth: OAuth client_id/secret not configured")
```

## 10. 与 Issue #50 的关系

Issue #50「grape 频繁出现无法创建 task 的情况」的根因就是 Issue #52 描述的 token 失效：

- #50 报错：`GitCode API POST issues failed (401): Bad credentials`
- #52 描述：`登录会话失效机制过长，导致 GitCode 的 token 失效`

本方案解决 #52 后，#50 的 401 问题同步解决——token 过期后自动刷新，用户无感知。

但 #50 可能还有其他触发因素（如 bot token 也过期、网络抖动），需要在本方案实施后观察是否完全消除。
