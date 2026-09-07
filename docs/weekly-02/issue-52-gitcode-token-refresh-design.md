# Issue #52 技术方案：GitCode OAuth Token 生命周期与登录会话对齐

> Issue: https://gitcode.com/grape-dev/grape/issues/52
> 需求：grape 当前登录会话失效机制过长，导致 GitCode 的 token 失效
> 关联 Issue: #50「grape 频繁出现无法创建 task 的情况」（GitCode API 返回 401 Bad credentials）

## 1. 现状分析

### 1.1 双层 Token 体系

Grape 平台存在**两层独立的 Token 体系**，生命周期完全不同步：

| 层 | Token 类型 | 存储位置 | 有效期 | 刷新机制 |
|----|-----------|---------|--------|---------|
| 内层 | JWT access_token | localStorage（前端） | 15 分钟 | 前端 401 时调 `/api/auth/refresh` 滚动续期 |
| 内层 | JWT refresh_token | HttpOnly cookie | 7 天滚动 / 30 天绝对上限 | `/api/auth/refresh` 端点自动续期 |
| 外层 | GitCode OAuth access_token | `user_credentials` 表（`gitcode_oauth` provider） | **GitCode 控制（约 2 小时）** | **无——仅在 OAuth 登录时获取一次** |
| 外层 | GitCode OAuth refresh_token | `user_credentials` 表（`gitcode_oauth` provider） | **GitCode 控制（约 30 天）** | **无——存储了但从未使用** |

### 1.2 根因定位

**核心问题：GitCode OAuth access_token 过期后，grape 没有任何刷新机制。**

代码路径追踪：

1. OAuth 登录（`auth.py:130-232`）：GitCode callback 获取 `access_token` + `refresh_token`，存入 `user_credentials` 表，同时将 `access_token` 写入 `user_accounts.gitcode_api_token`
2. API 调用（`tasks.py:652`）：`get_user_token(user["username"])` → `get_user_account(username).get("gitcode_api_token")` → 读取**登录时存入的 access_token**
3. GitCode API 调用（`tasks.py:566`）：`_post_gitcode_issue(api_token, ...)` → 用 `access_token` query param 调 GitCode v5 API
4. GitCode access_token 过期（约 2 小时后）→ API 返回 `401 Bad credentials`
5. grape 抛 `RuntimeError` → 前端收到 500 → 用户看到「创建失败: 请求失败 (500)`

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
- `tasks.py:862` — POST Issue comment
- `chat.py:452/576/1264/1610` — Chat Agent 获取 user_token
- `event_pipeline.py` — webhook 事件处理中的 GitCode API 调用

不受影响的操作（使用 bot token，不依赖用户 OAuth token）：

- `get_gitcode_token(repo_key=...)` 路径：devops_agent_session.repo_gitcode_token → managed_repos.bot_gitcode_token → 全局 bot token
- bot token 是 PAT（Personal Access Token），有效期由 GitCode 账号设置控制（通常更长），不走 OAuth 刷新流程

## 2. 设计目标

1. **GitCode OAuth access_token 过期前自动刷新**——用户无感知，无需重新登录
2. **刷新失败时优雅降级**——提示用户重新授权，而非 500 错误
3. **刷新后的 token 持久化**——全实例可用，不限于当前请求
4. **不影响现有 JWT 会话机制**——两层 Token 体系各自独立
5. **并发安全**——多请求同时遇到 401 时只刷新一次

## 3. 技术方案

### 3.1 总体架构

```
用户请求（tasks.py / chat.py / event_pipeline.py）
    ↓
get_user_token(username)  →  读取 user_accounts.gitcode_api_token
    ↓
调 GitCode API
    ↓
┌── 200 → 正常返回
└── 401 Bad credentials → 触发 GitCode OAuth token 刷新
                            ↓
                    _refresh_gitcode_oauth_token(user_id)
                            ↓
                    读取 user_credentials.refresh_token（gitcode_oauth provider）
                            ↓
                    POST https://gitcode.com/oauth/token
                      grant_type=refresh_token&refresh_token=xxx
                            ↓
                    ┌── 成功 → 更新 user_accounts.gitcode_api_token + user_credentials
                    │         → 重试原请求（一次）
                    └── 失败 → 返回 401「GitCode 授权已过期，请重新登录」
                               前端跳 OAuth 授权页
```

### 3.2 后端改造

#### 3.2.1 新增 GitCode OAuth token 刷新函数

在 `src/user/db.py` 新增：

```python
def refresh_gitcode_oauth_token(user_id: int) -> str | None:
    """用 refresh_token 刷新 GitCode OAuth access_token。

    读取 user_credentials (provider=gitcode_oauth) 中存储的 refresh_token，
    调用 GitCode OAuth token endpoint 刷新 access_token。

    成功时更新两处存储：
    - user_credentials.config_json.access_token（OAuth 凭据原始存储）
    - user_accounts.gitcode_api_token（API 调用实际读取的字段）

    返回新的 access_token，失败返回 None。
    """
    import urllib.request, urllib.parse, json as _json

    # 1. 读取存储的 refresh_token
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

    # 2. 调 GitCode OAuth refresh endpoint
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

    # 3. 更新存储（access_token + refresh_token 如果返回了新的）
    new_cred = dict(cred)
    new_cred["access_token"] = new_access
    if new_refresh:
        new_cred["refresh_token"] = new_refresh
    set_credential(user_id, "gitcode_oauth", new_cred)

    # 同步更新 user_accounts.gitcode_api_token（API 调用实际读取的字段）
    username = _get_username_by_id(user_id)
    if username:
        acc = get_user_account(username) or {}
        acc["gitcode_api_token"] = new_access
        save_user_account(username, acc)

    log.info(f"refresh_gitcode_oauth: success for user={user_id}")
    return new_access
```

#### 3.2.2 新增带自动刷新的 token 获取函数

在 `src/api/deps.py` 新增：

```python
import threading

# 用户级刷新锁——同一用户并发 401 时只刷新一次
_gitcode_refresh_locks: dict[int, threading.Lock] = {}
_gitcode_refresh_locks_guard = threading.Lock()


def _get_user_refresh_lock(user_id: int) -> threading.Lock:
    """获取用户级别的刷新锁（懒创建）。"""
    with _gitcode_refresh_locks_guard:
        if user_id not in _gitcode_refresh_locks:
            _gitcode_refresh_locks[user_id] = threading.Lock()
        return _gitcode_refresh_locks[user_id]


def get_user_token_with_refresh(username: str, user_id: int = 0) -> str:
    """获取用户 GitCode token，过期时自动刷新。

    与 get_user_token 的区别：当 user_id > 0 时，如果 token 为空
    或已知过期（由调用方判断），尝试用 refresh_token 刷新。

    并发安全：同一 user_id 的刷新操作加锁串行化，
    避免多请求同时触发重复刷新。
    """
    acc = user_db.get_user_account(username) or {}
    token = acc.get("gitcode_api_token", "")

    if token and user_id > 0:
        # 乐观策略：先返回 token，让调用方试一次。
        # 401 时由调用方调 try_refresh_gitcode_token 再重试。
        return token

    if user_id > 0 and not token:
        # token 为空——尝试刷新
        return try_refresh_gitcode_token(user_id) or ""

    return token


def try_refresh_gitcode_token(user_id: int) -> str | None:
    """尝试刷新 GitCode OAuth token（加锁，防并发重复刷新）。

    返回新 token 或 None（刷新失败/无 refresh_token）。
    """
    lock = _get_user_refresh_lock(user_id)
    with lock:
        # 双重检查：拿到锁后可能其他线程已经刷新过了
        # 再读一次 user_accounts.gitcode_api_token
        # （但无法判断是否仍然有效——只能靠调用方 401 反馈）
        return user_db.refresh_gitcode_oauth_token(user_id)
```

#### 3.2.3 GitCode API 调用增加 401 自动重试

在 `src/api/routes/tasks.py` 的 `_post_gitcode_issue` 和其他 GitCode API 调用函数中，增加 401 自动刷新 + 重试逻辑。

提取通用包装函数：

```python
def _gitcode_api_with_refresh(user: dict, api_call_fn, *args, **kwargs):
    """包装 GitCode API 调用——401 时自动刷新 OAuth token 并重试一次。

    user: 当前登录用户 dict（含 id, username）
    api_call_fn: 实际调 GitCode API 的函数，第一个参数是 token
    *args, **kwargs: 传给 api_call_fn 的其余参数

    返回 api_call_fn 的结果，或抛 HTTPException（刷新失败时）。
    """
    from src.api.deps import get_user_token, try_refresh_gitcode_token
    from src.user import db as user_db

    username = user["username"]
    user_id = user["id"]
    token = get_user_token(username)

    try:
        return api_call_fn(token, *args, **kwargs)
    except RuntimeError as e:
        # 检查是否 401
        if "401" not in str(e) and "Bad credentials" not in str(e):
            raise  # 非 401 错误不处理
        # 401 → 尝试刷新 token
        new_token = try_refresh_gitcode_token(user_id)
        if not new_token:
            raise HTTPException(status_code=401, detail={
                "code": 401,
                "message": "GitCode 授权已过期，请重新登录授权",
                "action": "reauth_required"
            })
        # 刷新成功 → 重试一次
        return api_call_fn(new_token, *args, **kwargs)
```

改造 `create_task_via_issue` 中的调用（`tasks.py:664`）：

```python
# 改造前
api_token = get_user_token(user["username"])
resp = _post_gitcode_issue(api_token, owner, rname, ...)

# 改造后
resp = _gitcode_api_with_refresh(
    user, _post_gitcode_issue, owner, rname,
    body.title.strip(), (body.description or "").strip(), assignee=assignee
)
```

同样模式应用到：
- `_patch_issue_body` 调用处
- `_post_issue_comment` 调用处（`tasks.py:862`）
- `chat.py` 中的 `user_token` 获取路径

#### 3.2.4 前端 401 reauth 处理

在 `api.js` 的 `handleResp` 中，对返回的 `action: "reauth_required"` 特殊处理：

```javascript
function handleResp(r, retryFn) {
  if (!r.ok) {
    if (r.status === 401 && retryFn) {
      return handle401(retryFn);
    }
    if (r.status === 401) {
      // 检查是否是 GitCode OAuth 过期（需要重新授权）
      return r.json().then(err => {
        if (err?.detail?.action === 'reauth_required') {
          // GitCode OAuth token 过期——跳授权页
          _redirectToGitcodeAuth();
          throw new Error(err.detail.message);
        }
        // 普通 JWT 过期——跳登录
        localStorage.removeItem('token');
        localStorage.removeItem('user');
        if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
        throw new Error(err?.detail?.message || I18N.t('common.session_expired'));
      });
    }
    return r.json().then(err => {
      const e = new Error(err?.detail?.message || I18N.t('common.request_failed', {status: r.status}));
      e.status = r.status;
      e.errors = err?.detail?.errors || null;
      throw e;
    });
  }
  return r.json();
}

function _redirectToGitcodeAuth() {
  // 调后端获取 GitCode OAuth 授权 URL，然后跳转
  API.get('/auth/login/gitcode').then(d => {
    if (d.redirect_url) {
      location.href = d.redirect_url;
    } else {
      // 后端未配置 OAuth——跳登录页
      location.href = '/login';
    }
  }).catch(() => {
    location.href = '/login';
  });
}
```

### 3.3 主动刷新策略（可选增强）

除了被动的 401 重试，可以增加主动刷新——在 token 临近过期时预刷新：

#### 3.3.1 记录 token 获取时间

在 `link_gitcode_user` 和 `refresh_gitcode_oauth_token` 中，记录 token 获取时间戳：

```python
# user_credentials.config_json 增加 "fetched_at" 字段
new_cred["access_token"] = new_access
new_cred["fetched_at"] = int(time.time())  # 新增
```

#### 3.3.2 读取时判断是否需要预刷新

在 `get_user_token_with_refresh` 中：

```python
def get_user_token_with_refresh(username: str, user_id: int = 0) -> str:
    acc = user_db.get_user_account(username) or {}
    token = acc.get("gitcode_api_token", "")

    if token and user_id > 0:
        # 检查是否临近过期（GitCode token 默认 2 小时 = 7200 秒）
        cred = user_db.get_credential(user_id, "gitcode_oauth")
        if cred:
            fetched_at = cred.get("fetched_at", 0)
            age = int(time.time()) - fetched_at if fetched_at else 0
            # 超过 100 分钟（6000 秒）就预刷新（留 20 分钟余量）
            if age > 6000:
                new_token = try_refresh_gitcode_token(user_id)
                return new_token or token
        return token

    if user_id > 0 and not token:
        return try_refresh_gitcode_token(user_id) or ""

    return token
```

> 注意：GitCode OAuth access_token 的实际有效期由 GitCode 平台控制，文档未明确标注。根据 Gitee/AtomGit 系惯例通常为 2 小时。`fetched_at` 机制提供预刷新能力，但被动 401 重试是兜底保障——两者组合使用。

### 3.4 辅助函数

在 `src/user/db.py` 新增 `_get_username_by_id`（如果不存在）：

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

### 4.1 正常流程（token 有效）

```
前端 → POST /api/tasks/create
  → get_current_user（JWT 验证）
  → get_user_token(username) → user_accounts.gitcode_api_token
  → _post_gitcode_issue(token, ...)
  → GitCode API 200
  → 任务创建成功
```

### 4.2 token 过期流程（被动刷新）

```
前端 → POST /api/tasks/create
  → get_current_user（JWT 验证）
  → get_user_token(username) → user_accounts.gitcode_api_token（已过期）
  → _post_gitcode_issue(token, ...)
  → GitCode API 401 Bad credentials
  → try_refresh_gitcode_token(user_id)
    → refresh_gitcode_oauth_token(user_id)
      → 读取 user_credentials.refresh_token
      → POST gitcode.com/oauth/token (grant_type=refresh_token)
      → 更新 user_accounts.gitcode_api_token + user_credentials
  → _post_gitcode_issue(new_token, ...)
  → GitCode API 200
  → 任务创建成功（用户无感知）
```

### 4.3 refresh_token 也过期流程（降级）

```
前端 → POST /api/tasks/create
  → get_user_token(username) → 过期 token
  → _post_gitcode_issue(token, ...) → 401
  → try_refresh_gitcode_token(user_id)
    → refresh_gitcode_oauth_token(user_id)
      → POST gitcode.com/oauth/token → 400/401（refresh_token 也过期了）
      → 返回 None
  → HTTPException 401 {action: "reauth_required"}
  → 前端 _redirectToGitcodeAuth()
  → 用户重新 OAuth 授权
  → callback 更新 token
  → 继续 操作
```

## 5. 边界情况

| 场景 | 处理 |
|------|------|
| user_credentials 无 gitcode_oauth 记录 | `refresh_gitcode_oauth_token` 返回 None → 401 reauth |
| refresh_token 为空（OAuth 登录时没返回） | 同上 → 401 reauth |
| OAuth client_id/secret 未配置 | 返回 None → 401 reauth（提示管理员配置 OAuth） |
| 并发多请求同时 401 | 用户级 threading.Lock 串行化，只刷新一次 |
| bot token 路径（非 user token） | 不受影响——bot token 是 PAT，不走 OAuth 刷新 |
| 多实例部署 | 刷新后写入数据库（user_credentials + user_accounts），全实例可见 |
| refresh_token 轮转（GitCode 返回新 refresh_token） | 更新存储的 refresh_token，下次用新值刷新 |
| GitCode OAuth 平台维护/不可达 | urllib 超时 10s → None → 401 reauth |
| 用户在另一 Tab 重新登录刷新了 token | 数据库已更新，当前请求重试时读到新 token |

## 6. 实施计划

### Phase 1: 核心刷新机制（MVP）

1. `src/user/db.py`：新增 `refresh_gitcode_oauth_token(user_id)` + `_get_username_by_id`
2. `src/api/deps.py`：新增 `try_refresh_gitcode_token(user_id)` + 用户级锁
3. `src/api/routes/tasks.py`：新增 `_gitcode_api_with_refresh` 包装函数，改造 `_post_gitcode_issue` 调用处
4. 前端 `api.js`：新增 `action: "reauth_required"` 处理 + `_redirectToGitcodeAuth`
5. 测试：手动验证 token 过期后创建 task 能自动恢复

### Phase 2: 全路径覆盖

6. 改造 `tasks.py` 中 `_patch_issue_body`、`_post_issue_comment` 的调用处
7. 改造 `chat.py` 中 `user_token` 获取路径（`_resolve_gitcode_token`）
8. 改造 `event_pipeline.py` 中 webhook 事件处理的 GitCode API 调用

### Phase 3: 主动预刷新（增强）

9. `link_gitcode_user` + `refresh_gitcode_oauth_token` 增加 `fetched_at` 时间戳
10. `get_user_token_with_refresh` 增加预刷新逻辑
11. `deps.py` 中 `get_user_token` 替换为 `get_user_token_with_refresh`

### Phase 4: i18n + 版本号

12. `zh/common.json` + `en/common.json`：新增 `gitcode_reauth_required` 文案
13. `api.js?v=N` bump + `index.html` 版本号同步

## 7. 涉及文件

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/user/db.py` | 新增函数 | `refresh_gitcode_oauth_token`, `_get_username_by_id` |
| `src/api/deps.py` | 新增函数 | `try_refresh_gitcode_token`, `get_user_token_with_refresh`, 用户级锁 |
| `src/api/routes/tasks.py` | 改造调用 | `_gitcode_api_with_refresh` 包装 + 改造 3 处 GitCode API 调用 |
| `src/api/routes/chat.py` | 改造调用 | `_resolve_gitcode_token` 增加刷新能力 |
| `src/api/event_pipeline.py` | 改造调用 | webhook 事件 GitCode API 调用增加刷新 |
| `src/api/static/js/api.js` | 前端改造 | 401 reauth 处理 + 跳转授权 |
| `src/api/routes/auth.py` | 可选增强 | `link_gitcode_user` 增加 `fetched_at` |

## 8. 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| GitCode OAuth refresh endpoint 行为未知（是否支持 refresh_token grant） | **中** | Phase 1 先用 curl 手动验证 `POST gitcode.com/oauth/token` + `grant_type=refresh_token`；如不支持则降级为方案 B（401 时直接提示重新授权，不做自动刷新） |
| refresh_token 可能也被 GitCode 设置了较短有效期 | **低** | 被动 401 重试是兜底——即使预刷新失败，用户操作时仍会尝试一次刷新 |
| 多实例并发刷新竞争 | **低** | 数据库是单一写入点（SQLite/MySQL），`INSERT OR REPLACE` / `UPDATE` 原子操作；锁是进程内防重复刷新 |
| `user_accounts.gitcode_api_token` 与 `user_credentials` 双写一致性 | **低** | 两处写操作在同一函数内顺序执行，无事务需求——最坏情况是读到旧 token 触发一次额外 401 重试 |
| 前端跳转 OAuth 授权页打断用户操作 | **中** | 仅在 refresh_token 也过期时才跳转（预计 30 天一次）；跳转前提示「GitCode 授权已过期，即将重新授权」 |

## 9. 验证方案

### 9.1 单元测试

```python
# tests/test_gitcode_token_refresh.py
def test_refresh_gitcode_oauth_token_success(mocker):
    """刷新成功时更新两处存储"""
    ...

def test_refresh_gitcode_oauth_token_no_refresh_token():
    """无 refresh_token 时返回 None"""
    ...

def test_refresh_gitcode_oauth_token_api_failure(mocker):
    """GitCode refresh endpoint 失败时返回 None"""
    ...

def test_gitcode_api_with_refresh_401_retry(mocker):
    """401 时自动刷新并重试"""
    ...
```

### 9.2 手动验证

1. OAuth 登录后记录 `user_accounts.gitcode_api_token` 的值
2. 等待 2 小时（或手动将 token 改为无效值模拟过期）
3. 创建 task → 应自动刷新 token 并成功创建
4. 检查 `user_accounts.gitcode_api_token` 已更新为新值
5. 将 `user_credentials` 中的 `refresh_token` 也改为无效值
6. 创建 task → 应返回 401 + `action: reauth_required`
7. 前端应跳转 GitCode 授权页

### 9.3 日志验证

```python
# 成功刷新时日志
log.info(f"refresh_gitcode_oauth: success for user={user_id}")

# 刷新失败时日志
log.warning(f"refresh_gitcode_oauth: refresh failed for user={user_id}: {e}")

# 无 refresh_token
log.warning(f"refresh_gitcode_oauth: no refresh_token for user={user_id}")
```

## 10. 与 Issue #50 的关系

Issue #50「grape 频繁出现无法创建 task 的情况」的根因就是 Issue #52 描述的 token 失效：

- #50 报错：`GitCode API POST issues failed (401): Bad credentials`
- #52 描述：`登录会话失效机制过长，导致 GitCode 的 token 失效`

本方案解决 #52 后，#50 的 401 问题同步解决——token 过期后自动刷新，用户无感知。

但 #50 可能还有其他触发因素（如 bot token 也过期、网络抖动），需要在本方案实施后观察是否完全消除。
