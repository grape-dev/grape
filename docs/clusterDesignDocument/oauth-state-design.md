# OAuth State 共享存储改造方案

## 背景

集群部署后 OAuth 登录报错 `state 无效或已过期`。

根因：OAuth state 存在进程内存里（`_oauth_store` dict + `_oauth_lock`），nginx 负载均衡把回调打到另一台后端实例 → 那台没有这个 state → 报错。

这是 JWT 改造时遗漏的问题——JWT 把 `_user_tokens`（进程内会话）改成了无状态 JWT token，但同一文件里的 `_oauth_store`（进程内 OAuth state）没有一起改。

## 问题分析

### 当前代码（src/user/db.py）

```python
# 进程内存存储——集群不共享
_oauth_store = {}
_oauth_lock = threading.Lock()

def save_oauth_state(state, provider, data=None):
    expires = time.time() + 600
    with _oauth_lock:
        _oauth_store[state] = {"provider": provider, "data": data or {}, "expires": expires}

def validate_oauth_state(state, provider):
    with _oauth_lock:
        entry = _oauth_store.pop(state, None)
    # 验证 provider + expires
```

### 问题链条

```
1. 用户点 GitCode 登录 → 请求打到实例 A → 实例 A 内存里存 state
2. GitCode 回调 /api/auth/callback/gitcode?state=xxx → nginx 轮询打到实例 B
3. 实例 B 内存里没有这个 state → "state 无效或已过期"
```

### 已有但未使用的 DB 基础设施

代码里已经有：
- `oauth_states` 表 DDL（user/db.py 第 472 行）：`state TEXT PRIMARY KEY, provider TEXT, data_json MEDIUMTEXT, expires REAL`
- `_cleanup_expired_oauth_states()` 清理函数（第 167 行）：`DELETE FROM oauth_states WHERE expires < ?`
- 启动时调用清理（第 825 行）

但 `save_oauth_state` / `validate_oauth_state` 没用这张表，还在用内存 dict。

## 修复方案

把 `save_oauth_state` / `validate_oauth_state` 从内存 dict 改成 MySQL `oauth_states` 表读写。

### save_oauth_state 改造

```python
def save_oauth_state(state: str, provider: str, data: dict | None = None):
    """保存 OAuth state（防 CSRF，10 分钟过期，DB 共享存储——集群多实例可用）。"""
    import json as _json
    expires = time.time() + 600
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO oauth_states (state, provider, data_json, expires) VALUES (?,?,?,?)",
                (state, provider, _json.dumps(data or {}), expires),
            )
            db.commit()
        log.info(f"save_oauth_state: state={state[:20]}... saved OK")
    except Exception as e:
        log.warning(f"save_oauth_state failed: {e}")
```

### validate_oauth_state 改造

```python
def validate_oauth_state(state: str, provider: str) -> dict | None:
    """验证并消耗 OAuth state（查 + 删），返回 data 或 None。"""
    import json as _json
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT provider, data_json, expires FROM oauth_states WHERE state=?", (state,))
            row = c.fetchone()
            if row:
                c.execute("DELETE FROM oauth_states WHERE state=?", (state,))
                db.commit()
        if not row:
            log.warning(f"validate_oauth_state: state={state[:20]}... NOT FOUND")
            return None
        if row[0] != provider:
            log.warning(f"validate_oauth_state: provider mismatch")
            return None
        if time.time() > row[2]:
            log.warning(f"validate_oauth_state: state expired")
            return None
        return _json.loads(row[1] or "{}")
    except Exception as e:
        log.warning(f"validate_oauth_state failed: {e}")
        return None
```

### 清理旧代码

- 删除 `_oauth_store = {}`
- 删除 `_oauth_lock = threading.Lock()`
- `_cleanup_expired_oauth_states` 保留（已经有，用 DB）

## OAuth 完整流程

```
1. 前端点击"GitCode 登录" → GET /api/auth/login/gitcode

2. 后端 gitcode_oauth_login()
   → 生成随机 state（sha256(urandom(32))）
   → save_oauth_state(state) 存到 DB（改造后）
   → 返回 {"redirect_url": "https://gitcode.com/oauth/authorize?...&state=xxx"}

3. 前端跳转到 GitCode 授权页 → 用户点"授权"

4. GitCode 回调
   → 浏览器跳转到 /api/auth/callback/gitcode?code=xxx&state=xxx
   → nginx 负载均衡到某台后端实例

5. 后端 gitcode_oauth_callback()
   → validate_oauth_state(state) 验证 state（改造后从 DB 查 + 删）
   → state 无效 → 400 "state 无效或已过期"
   → state 有效 → 继续

6. 后端用 code 换 access_token → POST https://gitcode.com/oauth/token

7. 后端用 access_token 查 GitCode 用户信息 → GET https://api.gitcode.com/api/v5/user

8. 后端查找/创建本地用户 → get_user_by_gitcode_id / create_user

9. 后端签发 JWT 双 token
   → create_access_token（15 分钟）
   → create_refresh_token（7 天滚动续期，HttpOnly cookie）
   → 302 重定向到 /?token=xxx

10. 前端收到 302 → access_token 存 localStorage → refresh_token 在 HttpOnly cookie
```

## state 的作用

state 是 OAuth 2.0 的 CSRF 防护（RFC 6749 要求）：
- 第 2 步生成、第 5 步验证
- 防止攻击者构造假回调 URL 骗用户用攻击者账号登录
- 不能去掉——去掉意味着没有 CSRF 防护

## 集群兼容性

改造后：
- state 存在共享 MySQL → 实例 A 生成 state，实例 B 回调验证 → 跨实例可用 ✅
- 和 JWT token_version 同模式——共享 DB 存储
- 不需要 Redis——oauth_states 表足够

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | src/user/db.py | save_oauth_state / validate_oauth_state 从内存 dict 改成 DB 读写 + 删 _oauth_store / _oauth_lock |

## 验证

- test_auth_routes.py（含 OAuth 登录流程）全部通过 ✅
- 集群多实例 OAuth 登录跨实例可用 ✅
