# OAuth 链路安全加固技术设计文档

> **分支**：`feat/oauth-security-hardening-330`（基于 `weekly-07`）
> **Issue**：[#330](https://gitcode.com/grape-dev/grape/issues/330)
> **需求文档**：`docs/weekly-07/oauth-security-hardening-requirements.md`（已冻结）
> **关联文档**：`docs/weekly-06/grape-account-system-tech-design-v2.md`（现有账号体系设计）
> **状态**：待评审

---

## 1. 设计目标

将需求文档中 11 个需求转化为可执行的代码级设计方案，覆盖：

- 每个需求的改动文件、函数签名变更、SQL 语句、事务边界
- 函数间的调用关系与 cursor 传递方案
- 升级脚本与回滚方案
- 测试策略

### 设计决策（已确认）

| # | 决策 | 理由 |
|---|------|------|
| D1 | 旧统一回调路由直接删除，不保留 302 | 系统未上线 |
| D2 | 需求 6 采用方案 A（merge_placeholder_account 去内部 commit，不拆分） | 仅一处调用方，拆分属提前抽象 |
| D3 | 旧 state 不兼容，升级时清空 oauth_states 表 | 系统未上线，加兼容逻辑等于开永久后门 |
| D4 | 需求 2 不用 FOR UPDATE，统一 SELECT+DELETE+rowcount | DELETE InnoDB 行锁 + rowcount 已保证原子性 |
| D5 | 直接用 security_config.is_production()，不走 email_service 间接层 | 同一函数，减少间接层 |
| D6 | 生产环境 OAUTH_REDIRECT_URI 未配则拒绝启动，不做软警告过渡 | 先配再发 |
| D7 | 批次：P0(1/2/4/9) → P1(5/6/8/11) → P2(3/7/10)，P0 不触碰合并逻辑 | 已核实 |

---

## 2. 改动范围总览

### 2.1 文件清单

| 批次 | 需求 | 改动文件 | 改动类型 |
|:---:|------|---------|---------|
| P0 | 需求 1 | `src/api/routes/grape_auth.py` | oauth_login + oauth_callback + _handle_oauth_login |
| P0 | 需求 1 | `src/user/db.py` | save_oauth_state 增加 session_nonce |
| P0 | 需求 2 | `src/user/db.py` | validate_oauth_state 原子消费 |
| P0 | 需求 4 | `src/user/db.py` | refresh_oauth_token 改读取源 |
| P0 | 需求 9 | `src/admin/cooldown.py` | _anonymize_account datetime 修复 |
| P1 | 需求 5 | `src/user/db.py` | refresh_oauth_token 删 provider 硬编码 |
| P1 | 需求 6 | `src/api/routes/grape_auth.py` | _handle_oauth_bind 事务改造 |
| P1 | 需求 6 | `src/auth/grape_account_db.py` | merge_placeholder_account 去 commit |
| P1 | 需求 6 | `src/auth/grape_account_db.py` | create_external_identity 支持 _in_tx |
| P1 | 需求 8 | `src/admin/cooldown.py` | _anonymize_account 单事务 |
| P1 | 需求 11 | `src/auth/providers.py` | get_redirect_uri 从 provider 配置读 redirect_uri |
| P1 | 需求 11 | `src/api/main.py` | 启动校验每个 provider 有 redirect_uri |
| P2 | 需求 3 | `src/api/routes/grape_auth.py` | 回调路由分路径 |
| P2 | 需求 3 | `src/auth/providers.py` | get_redirect_uri 返回分路径 |
| P2 | 需求 7 | `src/api/routes/grape_auth.py` | transfer_bind 邮箱清理 |
| P2 | 需求 7 | `src/auth/grape_account_db.py` | merge_placeholder_account 邮箱清理 |
| P2 | 需求 10 | `src/auth/grape_account_db.py` | 新增 clear_placeholder_email() |
| P2 | 需求 10 | `src/api/routes/grape_auth.py` | unbind_provider 改调数据访问层 |

### 2.2 不改动的文件

- `src/session_manager.py`（db/db_lock 全局连接，不变）
- `src/auth/credential_crypto.py`（加密/解密接口，不变）
- `src/api/db_setup.py`（表结构不变，通过 _ensure_column 补列除外）

---

## 3. P0 详细设计

### 3.1 需求 1：OAuth login state 绑定浏览器 session

#### 3.1.1 改动点

**文件 1：`src/api/routes/grape_auth.py` — `oauth_login()` 函数（L1228）**

当前代码：
```python
def oauth_login(provider: str, popup: bool = False, request: Request = None):
    ...
    extra_state = {"login_flow": True}
    ...
    state = _h.sha256(_os.urandom(32)).hexdigest()
    result = build_auth_url(provider, state, redirect_uri, extra_state_data=extra_state)
    ...
    return {"redirect_url": result["redirect_url"]}
```

改造后：
```python
def oauth_login(provider: str, popup: bool = False, request: Request = None):
    ...
    extra_state = {"login_flow": True}
    ...
    state = _h.sha256(_os.urandom(32)).hexdigest()

    # 需求 1：login flow 生成 nonce，绑定浏览器 session
    nonce = _h.sha256(_os.urandom(32)).hexdigest()
    extra_state["session_nonce"] = nonce

    result = build_auth_url(provider, state, redirect_uri, extra_state_data=extra_state)
    if not result:
        raise HTTPException(...)

    # 通过 Set-Cookie 返回 nonce（仅 login flow，绑定/注销不设置）
    from src.auth.security_config import is_production
    secure_flag = "; Secure" if is_production() else ""
    response = JSONResponse({"redirect_url": result["redirect_url"]})
    response.set_cookie(
        key="oauth_nonce",
        value=nonce,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=is_production(),
        path="/",
    )
    return response
```

**关键设计点**：
- nonce 用 `secrets.token_hex(32)`（32 字节随机值的 hex），不用 hashlib.sha256——secrets 是密码学安全随机数生成器
- cookie 属性：HttpOnly + SameSite=Lax + Max-Age=600 + Path=/
- Secure 属性：生产环境 True，开发环境 False（否则 HTTP 下浏览器不接受）
- 只在 `login_flow` 分支设置 nonce——`binding_user_id` 和 `delete_account_user_id` 分支不设置

**文件 2：`src/api/routes/grape_auth.py` — `oauth_callback()` 函数（L1577）**

当前代码在回调入口直接调 `validate_oauth_state(state)`，不校验 cookie。

改造后——在 `state_data` 恢复后、分流前插入 nonce 校验：
```python
@router.get("/callback/{provider}")
def oauth_callback(request: Request, provider: str, code: str = "", state: str = "", error: str = ""):
    ...
    state_data = user_db.validate_oauth_state(state)
    if state_data is None:
        ...  # 现有 state_invalid 处理

    # 需求 1：login flow 校验 nonce cookie
    is_login_flow = bool(state_data.get("login_flow"))
    if is_login_flow:
        cookie_nonce = request.cookies.get("oauth_nonce", "")
        state_nonce = state_data.get("session_nonce", "")
        if not cookie_nonce or not state_nonce or cookie_nonce != state_nonce:
            # 清除 cookie 并拒绝
            resp = _oauth_error_redirect(request, "state_mismatch", popup=popup)
            resp.delete_cookie("oauth_nonce", path="/")
            return resp

    # 分流（现有逻辑）
    ...

    # 回调完成后清除 nonce cookie（无论成功失败）
    # 在所有 redirect 返回前包装：
    # 方式：在每个返回点加 resp.delete_cookie，或用一个 helper 包装
```

**关键设计点**：
- 校验时机：validate_oauth_state 成功后、分流前。如果 state 已被消费（返回 None），走现有 state_invalid 流程，不额外校验 cookie
- 校验条件：仅 `login_flow=True` 时校验。`binding_user_id` / `delete_account_user_id` 不校验
- cookie 清除：回调完成后（成功和失败）都清除，在 redirect response 上调 `delete_cookie`
- popup 模式：不特殊处理——cookie 在同域 302 链中自动传递，中间页不读 cookie

**实现注意——cookie 清除的代码组织**：

oauth_callback 有多个返回点（error/state_invalid/state_mismatch/各分流结果）。在每个返回点加 delete_cookie 会很散。建议用一个 helper 函数包装：

```python
def _oauth_callback_response(request, redirect_url: str, popup: bool = False,
                              clear_nonce: bool = False) -> RedirectResponse:
    """构造回调重定向响应，可选清除 nonce cookie。"""
    resp = RedirectResponse(url=redirect_url, status_code=302)
    if clear_nonce:
        resp.delete_cookie("oauth_nonce", path="/")
    return resp
```

但现有代码直接 `return RedirectResponse(...)` 在多处。更实际的方式是：在 oauth_callback 函数末尾不提前返回，统一到一个出口。或用 try/finally 包裹——但 FastAPI 路由函数的 redirect 是返回值不是异常。

**推荐方案**：保持现有多返回点结构，但在每个返回的 RedirectResponse 上调 `delete_cookie`。login flow 的返回点需要清除，bind/delete flow 不需要。用条件判断：

```python
# 在 oauth_callback 中，拿到 state_data 后定义一个清理标志
need_clear_nonce = is_login_flow

# 每个返回点：
resp = _oauth_error_redirect(request, "xxx", popup=popup)
if need_clear_nonce:
    resp.delete_cookie("oauth_nonce", path="/")
return resp
```

#### 3.1.2 nonce 生成与存储

nonce 通过 `build_auth_url` → `save_oauth_state` 存入 oauth_states.data_json。不需要改 save_oauth_state——它已经把 extra_state_data 整体 JSON 序列化存入 data_json。nonce 作为 data_json 中的 `session_nonce` 字段自动持久化。

#### 3.1.3 不改动的部分

- `bind_provider()`（L872）：不设置 nonce，extra_state 只有 `binding_user_id`
- `delete_account_via_oauth()`（L656）：不设置 nonce，extra_state 只有 `delete_account_user_id`
- `build_auth_url()`：不改，extra_state_data 透传到 save_oauth_state
- `save_oauth_state()`：不改，已支持任意 extra_state_data

---

### 3.2 需求 2：OAuth state 原子消费

#### 3.2.1 改动点

**文件：`src/user/db.py` — `validate_oauth_state()` 函数（L347）**

当前代码：
```python
def validate_oauth_state(state: str, provider: str = "") -> dict | None:
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
            return None
        # ... provider 校验、过期校验、返回 data
    except Exception as e:
        return None
```

问题：
1. SELECT 和 DELETE 之间没有检查 DELETE rowcount
2. 过期校验在 Python 侧做（`time.time() > row[2]`），DELETE 不带 expires 条件——已过期的 state 被 SELECT 到后，DELETE 仍然删除它，但后续 Python 校验返回 None。虽然结果正确，但 DELETE 没有利用 expires 条件防止过期 state 被消费

改造后：
```python
def validate_oauth_state(state: str, provider: str = "") -> dict | None:
    """验证并原子消费 OAuth state（SELECT + DELETE + rowcount 检查）。

    原子性保证：
    - SQLite/单实例：db_lock 保证线程安全
    - MySQL 多实例：db_lock 跨实例无效，原子性靠 DELETE rowcount 保证
    - 不使用 FOR UPDATE，避免方言判断
    """
    import json as _json
    import time as _time
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT provider, data_json, expires FROM oauth_states WHERE state=?",
                (state,),
            )
            row = c.fetchone()
            if row:
                # 原子消费：DELETE 带 state + expires 条件
                # rowcount=0 说明已被其他请求消费或已过期
                c.execute(
                    "DELETE FROM oauth_states WHERE state=? AND expires > ?",
                    (state, _time.time()),
                )
                if c.rowcount == 0:
                    # 已被消费或已过期，不能使用 SELECT 到的数据
                    db.commit()
                    return None
            db.commit()
        if not row:
            log.warning(f"validate_oauth_state: state={state[:20]}... NOT FOUND")
            return None
        db_provider = row[0]
        if provider and provider != db_provider:
            log.warning(f"validate_oauth_state: provider mismatch (expected={provider}, db={db_provider})")
            return None
        data = _json.loads(row[1] or "{}")
        data["_provider"] = db_provider
        return data
    except Exception as e:
        log.warning(f"validate_oauth_state failed: {e}")
        return None
```

**关键设计点**：
- DELETE 条件包含 `expires > time.time()`，过期 state 不能被消费
- rowcount=0 时立即返回 None，不使用 SELECT 到的数据
- 移除 Python 侧的 `time.time() > row[2]` 校验——DELETE 条件已覆盖
- `db.commit()` 在 with db_lock 块内执行，确保事务提交
- 异常时返回 None（fail-closed）

#### 3.2.2 不改动的部分

- `save_oauth_state()`：不改
- oauth_callback 调用方：不改（返回 None 时已有正确的 state_invalid 处理）
- `validate_exchange_code()`：不在此需求范围内，但它有相同的 SELECT+DELETE 模式。如需统一修复可作为后续优化，本次不改

---

### 3.3 需求 4：refresh_token 存储链路统一

#### 3.3.1 改动点

**文件：`src/user/db.py` — `refresh_oauth_token()` 函数（L2117）**

当前代码读取逻辑：
```python
def refresh_oauth_token(user_id: int, provider: str) -> str | None:
    # 1. 读取存储的 refresh_token
    provider_key = f"{provider}_oauth"
    cred = get_credential(user_id, provider_key)  # ← 从 user_credentials 读
    if not cred or not cred.get("refresh_token"):
        return None
    rt = cred["refresh_token"]
    ...
    # 4. 更新存储
    set_credential(user_id, provider_key, new_cred)  # ← 写回 user_credentials
    if provider == "gitcode":  # ← 硬编码
        update_external_identity_tokens(user_id, "gitcode", access_token=new_access)
    ...
```

改造后——读取逻辑改为从 external_identities 读 + fallback：
```python
def refresh_oauth_token(user_id: int, provider: str) -> str | None:
    """用 refresh_token 刷新 OAuth access_token。

    需求 4：refresh_token 权威存储改为 external_identities（加密）。
    需求 5：删除 provider 硬编码，统一更新 external_identities。
    """
    import urllib.request, urllib.parse, json as _json

    # 1. 从 external_identities 读 refresh_token（权威存储）
    rt = _read_refresh_token_from_identity(user_id, provider)
    if not rt:
        # fallback：从 user_credentials 读（迁移期兼容）
        rt = _read_refresh_token_from_credentials(user_id, provider)
    if not rt:
        log.warning(f"refresh_oauth: no refresh_token for user={user_id}, provider={provider}")
        return None

    # 2. 获取 token_url
    from src.auth.providers import get_provider
    cfg = get_provider(provider)
    if not cfg:
        return None
    token_url = cfg["token_url"]

    # 3. 调 OAuth refresh endpoint
    try:
        req = urllib.request.Request(
            token_url,
            data=urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": get_meta_value(f"oauth_{provider}_client_id"),
                "client_secret": get_meta_value(f"oauth_{provider}_client_secret"),
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        token_data = _json.loads(resp.read())
    except Exception as e:
        log.warning(f"refresh_oauth: refresh failed for user={user_id}, provider={provider}: {e}")
        return None

    new_access = token_data.get("access_token", "")
    new_refresh = token_data.get("refresh_token", "")  # 可能为空（平台不返回新 refresh_token）
    if not new_access:
        return None

    # 4. 更新 external_identities（权威存储）—— 不限制 provider
    from src.auth.grape_account_db import update_external_identity_tokens
    update_external_identity_tokens(
        user_id, provider,
        access_token=new_access,
        refresh_token=new_refresh,  # 空字符串时不覆盖旧值（函数内部判断）
    )

    # 5. 同步更新 user_credentials（迁移期保留，上层 deps.py 读 fetched_at 判断过期）
    provider_key = f"{provider}_oauth"
    cred = get_credential(user_id, provider_key)
    if cred:
        new_cred = dict(cred)
        new_cred["access_token"] = new_access
        if new_refresh:
            new_cred["refresh_token"] = new_refresh
        new_cred["fetched_at"] = int(time.time())
        set_credential(user_id, provider_key, new_cred)

    log.info(f"refresh_oauth: success for user={user_id}, provider={provider}")
    return new_access
```

#### 3.3.2 新增辅助函数

```python
def _read_refresh_token_from_identity(user_id: int, provider: str) -> str:
    """从 external_identities 读 refresh_token（权威存储，解密）。

    判定链：
    - 无记录 → 返回空
    - refresh_token 字段为空 → 返回空
    - 解密失败 → 返回空（safe_decrypt）
    - 解密成功 → 返回明文
    """
    try:
        from src.session_manager import db, db_lock
        from src.auth.credential_crypto import decrypt
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT refresh_token FROM external_identities "
                "WHERE grape_user_id=? AND provider=? AND status=1",
                (user_id, provider),
            )
            row = c.fetchone()
        if not row or not row[0]:
            return ""
        # 解密，失败时返回空（不抛异常）
        try:
            return decrypt(row[0])
        except (ValueError, Exception) as e:
            log.warning(f"refresh_oauth: decrypt refresh_token failed for "
                       f"user={user_id}, provider={provider}: {e}")
            return ""
    except Exception as e:
        log.warning(f"_read_refresh_token_from_identity failed: {e}")
        return ""


def _read_refresh_token_from_credentials(user_id: int, provider: str) -> str:
    """从 user_credentials 读 refresh_token（迁移期 fallback）。

    使用同一密钥解密——如果 external_identities 解密失败，
    user_credentials 大概率也解密失败。
    """
    provider_key = f"{provider}_oauth"
    cred = get_credential(user_id, provider_key)
    if not cred or not cred.get("refresh_token"):
        return ""
    return cred["refresh_token"]
```

**关键设计点**：
- `_read_refresh_token_from_identity` 在解密失败时返回空字符串（safe 模式），不抛异常
- fallback 后 `_read_refresh_token_from_credentials` 也用同一密钥——如果密钥轮换后旧密钥丢失，两边都读不到
- `update_external_identity_tokens` 的 `refresh_token` 参数为空字符串时不覆盖旧值（现有函数已正确实现：`if refresh_token:` 判断，空字符串不 append 到 SET）

#### 3.3.3 不改动的部分

- `deps.py` 的 `get_user_token_with_refresh()`：不改。它调 `try_refresh_gitcode_token` → `refresh_gitcode_oauth_token` → `refresh_oauth_token`，底层改了读取源即可
- `deps.py` 的 `_get_oauth_token_from_identity()`：不改。它读 access_token（不是 refresh_token），逻辑正确
- `refresh_gitcode_oauth_token()`：不改。它是 `refresh_oauth_token(user_id, "gitcode")` 的包装，底层改了即可

#### 3.3.4 需求 5 同步实现

需求 5（消除 provider 硬编码）在改造后的 `refresh_oauth_token` 中已自动实现：
- 删除了 `if provider == "gitcode":` 分支
- 统一调用 `update_external_identity_tokens(user_id, provider, ...)`
- 对所有 provider 生效

---

### 3.4 需求 9：cooldown.py 消除 datetime('now')

#### 3.4.1 改动点

**文件：`src/admin/cooldown.py` — `_anonymize_account()` 函数**

当前代码 Step 1（L62）和 Step 4（L126）使用 `datetime('now')`：
```python
# Step 1
c.execute(
    "UPDATE grape_accounts SET status=2, updated_at=datetime('now') "
    "WHERE id=? AND status=3",
    (user_id,),
)

# Step 4
c.execute(
    "UPDATE grape_accounts SET username=?, email=NULL, "
    "password_hash='', nickname='', avatar_url='', "
    "updated_at=datetime('now') WHERE id=?",
    (f"deleted_{user_id}", user_id),
)
```

改造后——用 Python `time.strftime` 替代：
```python
# Step 1
import time as _t
now = _t.strftime("%Y-%m-%d %H:%M:%S")
c.execute(
    "UPDATE grape_accounts SET status=2, updated_at=? "
    "WHERE id=? AND status=3",
    (now, user_id),
)

# Step 4
now = _t.strftime("%Y-%m-%d %H:%M:%S")
c.execute(
    "UPDATE grape_accounts SET username=?, email=NULL, "
    "password_hash='', nickname='', avatar_url='', "
    "updated_at=? WHERE id=?",
    (f"deleted_{user_id}", now, user_id),
)
```

**注意**：需求 8（单事务改造）在 P1 批次实施时，Step 1-4 合并为单事务，datetime 修复同步完成。但 P0 批次先单独修 datetime（不改事务结构），确保 MySQL 兼容性立即可用。

---

## 4. P1 详细设计

### 4.1 需求 6：占位账号合并与新绑定创建改为同事务

#### 4.1.1 核心问题

当前调用链（`_handle_oauth_bind` L1303-1390）：
1. `merge_placeholder_account(conflict_user_id, ...)` — 内部 `with db_lock: commit()`，独立事务
2. `create_external_identity(binding_user_id, ...)` — 内部 `with db_lock: commit()`，独立事务

问题：步骤 1 成功后占位账号已 status=4（不可逆），步骤 2 失败则数据不一致。

#### 4.1.2 external_identities UNIQUE 约束

```
UNIQUE (provider, external_id)     -- 同一平台同一外部身份只能一条记录
UNIQUE (grape_user_id, provider)   -- 同一用户同一平台只能一条活跃记录
```

`create_external_identity` 的逻辑：
1. 查 `status=0` 的记录 → 有则恢复（UPDATE status=0 → status=1）
2. 无 status=0 记录 → DELETE status=0 旧记录 → INSERT 新记录

如果占位账号的旧绑定还是 status=1，create 时查不到 status=0 记录 → 走 INSERT → `UNIQUE(provider, external_id)` 冲突。

#### 4.1.3 正确执行顺序

```
Step 1: unbind 旧绑定 — UPDATE external_identities SET status=0 WHERE grape_user_id=占位账号 AND provider AND status=1
Step 2: create 新绑定 — 此时 create_external_identity 查到 status=0 记录，走"恢复"逻辑（UPDATE grape_user_id + status=1）
Step 3: mark 占位账号 status=4 — UPDATE grape_accounts SET status=4, username='merged_'||id, email=NULL
```

#### 4.1.4 改造方案（方案 A）

**文件 1：`src/auth/grape_account_db.py` — `merge_placeholder_account()`**

去掉内部 `with db_lock:` / `commit()` / `rollback()`，改为接收 cursor 参数：

```python
def merge_placeholder_account(placeholder_user_id: int, target_user_id: int,
                               provider: str, external_id: str,
                               cursor) -> bool:
    """将占位账号的绑定转移到目标账号，原账号标记为 merged（status=4）。

    ⚠️ 必须在外部 with db_lock 事务内调用，函数内部不 commit/rollback。
    调用方控制事务生命周期。

    前提：调用方已通过 is_placeholder_account_mergeable 检查。
    执行步骤（在调用方事务内）：
    1. unbind 旧绑定：UPDATE external_identities SET status=0, 清空 token
    2. （create_external_identity 由调用方在事务内调用）
    3. mark 占位账号 status=4：UPDATE grape_accounts SET status=4, email=NULL

    本函数执行步骤 1 和 3，步骤 2 由调用方在中间调用 restore_binding_in_tx。
    """
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    try:
        c = cursor
        # Step 1: unbind 旧绑定
        c.execute(
            "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
            "unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1",
            (now, placeholder_user_id, provider),
        )
        # Step 3: 标记占位账号 status=4 + 清邮箱（需求 7）
        c.execute(
            "UPDATE grape_accounts SET status=4, username='merged_' || CAST(id AS TEXT), "
            "email=NULL, email_source=NULL, email_is_placeholder=0, "
            "updated_at=? WHERE id=?",
            (now, placeholder_user_id),
        )
        log.info(f"Placeholder account {placeholder_user_id} merged to {target_user_id} "
                 f"(provider={provider}, external_id={external_id})")
        return True
    except Exception as e:
        log.warning(f"merge_placeholder_account failed: {e}")
        return False
```

**关键设计点**：
- 函数签名新增 `cursor` 参数，去掉 `with db_lock:` / `commit()`
- 不在函数内调 `create_external_identity`——调用方在 Step 1 和 Step 3 之间插入 Step 2
- 需求 7（邮箱清理）同步实现：status=4 时同时清空 email/email_source/email_is_placeholder
- 函数名保留 `merge_placeholder_account`，加注释明确"必须在外部 with db_lock 事务内调用"

**注意**：原 merge_placeholder_account 把 Step 1 和 Step 3 放一个函数内。改造后调用方需要在 Step 1 和 Step 3 之间插入 create_external_identity。所以函数需要拆成两步调用，或者调用方手动执行 Step 1 → create → Step 3。

**修正设计**——merge_placeholder_account 只执行 Step 1（unbind）和 Step 3（mark），但 Step 2（create）在中间。有两种方式：

方式 1：拆成两次调用
```python
# 调用方
with db_lock:
    merge_placeholder_unbind(cursor, placeholder_user_id, provider)
    restore_binding_in_tx(cursor, binding_user_id, provider, external_id, ...)
    merge_placeholder_mark(cursor, placeholder_user_id)
    db.commit()
```

方式 2：merge_placeholder_account 分两段，调用方在中间插入
```python
# 调用方
with db_lock:
    c = db.cursor()
    merge_placeholder_account(placeholder_user_id, binding_user_id, provider, external_id, cursor=c, phase="unbind")
    restore_binding_in_tx(c, binding_user_id, provider, external_id, ...)
    merge_placeholder_account(placeholder_user_id, binding_user_id, provider, external_id, cursor=c, phase="mark")
    db.commit()
```

**决策**：选方式 1——拆成两个清晰函数，不用 phase 参数。理由：phase 参数是隐式状态机，不如两个独立函数清晰。

```python
def unbind_placeholder_binding(cursor, placeholder_user_id: int, provider: str) -> bool:
    """Step 1: unbind 占位账号的旧绑定（在调用方事务内执行）。"""
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute(
            "UPDATE external_identities SET status=0, access_token='', refresh_token='', "
            "unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1",
            (now, placeholder_user_id, provider),
        )
        return True
    except Exception as e:
        log.warning(f"unbind_placeholder_binding failed: {e}")
        return False


def mark_account_merged(cursor, placeholder_user_id: int) -> bool:
    """Step 3: 标记占位账号 status=4 + 清邮箱（在调用方事务内执行）。"""
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute(
            "UPDATE grape_accounts SET status=4, username='merged_' || CAST(id AS TEXT), "
            "email=NULL, email_source=NULL, email_is_placeholder=0, "
            "updated_at=? WHERE id=?",
            (now, placeholder_user_id),
        )
        return True
    except Exception as e:
        log.warning(f"mark_account_merged failed: {e}")
        return False
```

保留 `merge_placeholder_account` 作为兼容包装（内部调两个新函数 + 自己 commit），但标注 deprecated。如果有其他地方调用旧接口，不会 break。

**文件 2：`src/auth/grape_account_db.py` — `restore_binding_in_tx()`**

新增事务内版本（与 `transfer_external_identity_in_tx` 模式一致）：

```python
def restore_binding_in_tx(
    cursor, grape_user_id: int, provider: str, external_id: str,
    external_login: str = "", external_name: str = "", external_avatar: str = "",
    access_token: str = "", refresh_token: str = "",
) -> bool:
    """在调用方事务内创建或恢复第三方绑定（不加锁，不 commit）。

    逻辑与 create_external_identity 相同，但由调用方持有 db_lock 和事务。
    """
    from src.auth.credential_crypto import encrypt
    try:
        c = cursor
        # 查 status=0 的记录（可能被 unbind_placeholder_binding 刚标记为 status=0）
        c.execute(
            "SELECT id FROM external_identities "
            "WHERE provider=? AND external_id=? AND status=0",
            (provider, external_id),
        )
        existing = c.fetchone()
        if existing:
            import time as _time
            now = _time.strftime("%Y-%m-%d %H:%M:%S")
            c.execute(
                "UPDATE external_identities SET "
                "grape_user_id=?, status=1, external_login=?, external_name=?, "
                "external_avatar=?, access_token=?, refresh_token=?, "
                "unbound_at=NULL, updated_at=? WHERE id=?",
                (grape_user_id, external_login, external_name,
                 external_avatar, encrypt(access_token), encrypt(refresh_token),
                 now, existing[0]),
            )
            return True
        # 无 status=0 记录，清理后 INSERT
        c.execute(
            "DELETE FROM external_identities WHERE grape_user_id=? AND provider=? AND status=0",
            (grape_user_id, provider),
        )
        c.execute(
            "INSERT INTO external_identities "
            "(grape_user_id, provider, external_id, external_login, "
            "external_name, external_avatar, access_token, refresh_token) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (grape_user_id, provider, external_id, external_login,
             external_name, external_avatar,
             encrypt(access_token), encrypt(refresh_token)),
        )
        return True
    except Exception as e:
        log.warning(f"restore_binding_in_tx failed: {e}")
        return False
```

**文件 3：`src/api/routes/grape_auth.py` — `_handle_oauth_bind()` 函数**

当前代码（L1313-1390）调用 merge_placeholder_account 后直接调 create_external_identity。

改造后——合并冲突分支改为单事务：

```python
def _handle_oauth_bind(request, provider, external_id, user_info,
                        access_token, refresh_token, binding_user_id, popup=False):
    from src.auth.grape_account_db import (
        get_external_identity, get_external_identity_any_status, save_binding_ticket,
        is_placeholder_account_mergeable,
        unbind_placeholder_binding, mark_account_merged,
        restore_binding_in_tx,
    )
    from src.session_manager import db, db_lock

    existing = get_external_identity(provider, external_id)
    if existing and existing["grape_user_id"] != binding_user_id:
        conflict_user_id = existing["grape_user_id"]
        if is_placeholder_account_mergeable(conflict_user_id):
            # 占位账号可自动合并 → 单事务执行 unbind → create → mark
            import time as _t
            try:
                with db_lock:
                    c = db.cursor()
                    # Step 1: unbind 旧绑定
                    if not unbind_placeholder_binding(c, conflict_user_id, provider):
                        db.rollback()
                        return _bind_error_redirect(request, "bind_failed", popup=popup)
                    # Step 2: create 新绑定（走"恢复"逻辑）
                    if not restore_binding_in_tx(
                        c, grape_user_id=binding_user_id, provider=provider,
                        external_id=external_id,
                        external_login=user_info.get("external_login", ""),
                        external_name=user_info.get("external_name", ""),
                        external_avatar=user_info.get("external_avatar", ""),
                        access_token=access_token,
                        refresh_token=refresh_token,
                    ):
                        db.rollback()
                        return _bind_error_redirect(request, "bind_failed", popup=popup)
                    # Step 3: 标记占位账号 status=4 + 清邮箱
                    if not mark_account_merged(c, conflict_user_id):
                        db.rollback()
                        return _bind_error_redirect(request, "bind_failed", popup=popup)
                    db.commit()
                # 审计日志（commit 后）
                auth_audit.log_audit("placeholder_merge", user_id=binding_user_id,
                                     ip=_get_client_ip(request),
                                     detail={"provider": provider, "external_id": external_id,
                                             "merged_account": conflict_user_id})
                log.info(f"Auto-merged placeholder account {conflict_user_id} → {binding_user_id} "
                         f"(provider={provider}, external_id={external_id})")
                # 绑定成功 → 重定向
                auth_audit.log_audit("bind", user_id=binding_user_id, ...)
                _clear_user_repos_cache(binding_user_id)
                from fastapi.responses import RedirectResponse
                url = _build_redirect_url(request, "/", {"bind_success": provider},
                                          hash_frag="repos", popup=popup)
                return RedirectResponse(url=url, status_code=302)
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                log.warning(f"_handle_oauth_bind merge transaction failed: {e}")
                return _bind_error_redirect(request, "bind_failed", popup=popup)
        else:
            # 不可自动合并 → 走转移确认流程（现有逻辑不变）
            ...

    # 无冲突 → 正常绑定（现有逻辑不变）
    bind_ok = create_external_identity(...)
    ...
```

**关键设计点**：
- unbind → create → mark 三步在同一 `with db_lock:` 事务内
- 任一步失败 → rollback，返回 bind_failed
- 审计日志在 commit 后执行（不影响事务）
- 额外检查：`is_placeholder_account_mergeable` 已确认占位账号只有一条活跃绑定（`ident_count != 1` 时返回 False），所以 unbind 只会 unbind 一条记录。当前绑定用户如果有该 provider 的活跃记录，`is_placeholder_account_mergeable` 不会检查这个——需要在进入合并分支前额外检查

**额外检查 UNIQUE(grape_user_id, provider)**：

在 `_handle_oauth_bind` 开头，进入合并分支前，检查当前用户是否已有该 provider 的活跃绑定：

```python
# 在 existing 冲突检查后、合并分支内，执行 unbind 前加检查
from src.auth.grape_account_db import get_user_identities
current_identities = get_user_identities(binding_user_id)
bound_providers = {ident["provider"] for ident in current_identities}
if provider in bound_providers:
    # 当前用户已绑定该平台 → 冲突，不能合并
    return _bind_error_redirect(request, "bind_conflict", popup=popup)
```

---

### 4.2 需求 8：冷静期匿名化改为单事务

#### 4.2.1 改动点

**文件：`src/admin/cooldown.py` — `_anonymize_account()` 函数**

当前 Step 1-4 各自独立 `with db_lock: commit()`。

改造后——Step 1-4 合并为单事务，datetime 修复同步完成：

```python
def _anonymize_account(user_id: int):
    """匿名化账号（单事务，幂等设计）。

    Step 1-4 在同一个 with db_lock 事务内，任一失败整体 rollback。
    Step 5 审计日志在 commit 后独立执行。
    """
    from src.auth import audit as auth_audit
    import time as _t

    try:
        with db_lock:
            c = db.cursor()
            now = _t.strftime("%Y-%m-%d %H:%M:%S")

            # Step 1: 原子标记已处理（幂等：WHERE status=3）
            c.execute(
                "UPDATE grape_accounts SET status=2, updated_at=? "
                "WHERE id=? AND status=3",
                (now, user_id),
            )
            if c.rowcount == 0:
                return  # 已被处理（幂等跳过）

            # Step 2: 清除第三方绑定
            c.execute(
                "DELETE FROM external_identities WHERE grape_user_id=?",
                (user_id,),
            )

            # Step 3: 删除个人工作空间 + 业务数据
            c.execute(
                "SELECT id FROM organization WHERE owner_id=? AND is_personal=1",
                (user_id,),
            )
            org_row = c.fetchone()
            if org_row:
                org_id = org_row[0]
                c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
                c.execute("DELETE FROM organization WHERE id=?", (org_id,))
            c.execute("DELETE FROM user_repos WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM agent_tasks WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM chat_agent_session WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM chat_conversations WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM issue WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM user_feedback WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM user_credentials WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM api_tokens WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM session_api_tokens WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM scene_likes WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM skill_likes WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM org_member WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM feishu_onboard_task WHERE user_id=?", (user_id,))

            # Step 4: 匿名化字段
            c.execute(
                "UPDATE grape_accounts SET username=?, email=NULL, "
                "password_hash='', nickname='', avatar_url='', "
                "updated_at=? WHERE id=?",
                (f"deleted_{user_id}", now, user_id),
            )

            db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        log.warning(f"Cooldown: anonymize failed for user {user_id}: {e}")
        return

    # Step 5: 记录审计日志（独立，失败不影响匿名化结果）
    try:
        auth_audit.log_audit("account_anonymized", user_id=user_id,
                             detail={"username": f"deleted_{user_id}"})
    except Exception as e:
        log.warning(f"Cooldown: Step 5 failed for user {user_id}: {e}")

    log.info(f"Cooldown: user {user_id} anonymized")
```

**关键设计点**：
- Step 1 的 `WHERE status=3` 保证幂等——已处理（status=2）的账号 rowcount=0，直接 return
- Step 2-4 对 status=2 的账号执行是安全的（DELETE 已删除数据是 no-op，清空已空字段是 no-op）
- datetime('now') 改为 Python `time.strftime`
- Step 5 审计日志在 commit 后独立执行
- 异常时 rollback + return（不标记已处理，下次定时任务重试）

---

### 4.3 需求 11：redirect_uri 从 provider 配置读取

#### 4.3.1 设计变更说明

原设计使用 `OAUTH_REDIRECT_URI` 环境变量 + `security_config.get_oauth_redirect_uri()`。
改为：redirect_uri 作为 provider 配置字段，配置在 `configs/oauth-providers.json` 中每个 provider 的 `redirect_uri` 字段（完整地址，含路径）。

理由：
- 每个 provider 的回调地址可能不同（不同域名/路径），按 provider 配置更合理
- provider 配置已有完善的同步机制（oauth-providers.json → meta 表 → 内存缓存）
- 减少环境变量数量，配置统一在 provider 配置文件管理

#### 4.3.2 不新增 security_config.py 函数

**不新增 `get_oauth_redirect_uri()`**。redirect_uri 从 provider 配置读取，通过 `get_provider(provider)["redirect_uri"]` 获取。

#### 4.3.3 改造 get_redirect_uri

**文件：`src/auth/providers.py` — `get_redirect_uri()` 函数**

```python
def get_redirect_uri(request, provider: str = "") -> str:
    """获取 OAuth 回调 URI。

    需求 11：从 provider 配置的 redirect_uri 字段读取（完整地址）。
    需求 3：路径为 /api/auth/callback/{provider}。
    开发环境 fallback：provider 未配 redirect_uri 时从 Host header 构造。
    """
    # 优先从 provider 配置读取
    cfg = get_provider(provider) if provider else None
    if cfg and cfg.get("redirect_uri"):
        return cfg["redirect_uri"]

    # fallback：从 Host header 构造（开发环境）
    # fallback：从 Host header 构造（开发环境）
    if not provider:
        raise ValueError("get_redirect_uri requires a non-empty provider")
    path = f"/api/auth/callback/{provider}"
    scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else "http"
    host = _get_trusted_host(request)
    return f"{scheme}://{host}{path}"
```

**关键设计点**：
- provider 配置有 `redirect_uri` → 直接返回（生产 + 开发均适用）
- provider 配置无 `redirect_uri` → fallback 从 Host header 构造（开发环境兼容）
- 生产环境启动校验确保所有 provider 都配了 `redirect_uri`，fallback 不会在生产触发
- 不再使用 `OAUTH_REDIRECT_URI` 环境变量
- 不再使用 `security_config.get_oauth_redirect_uri()`

#### 4.3.4 _sync_providers_from_config 不剥离 redirect_uri

`_sync_providers_from_config` 中剥离凭据时只排除 `client_id` 和 `client_secret`，`redirect_uri` 作为协议参数保留在 `oauth_providers_config` 中：

```python
prov_cfg = {k: v for k, v in cfg.items()
            if k not in ("client_id", "client_secret")}
```

无需改动——现有代码已正确保留 `redirect_uri`。

#### 4.3.5 启动校验

**文件：`src/api/main.py` — `_startup_security_check()` 函数**

```python
# 需求 11：每个 provider 必须配置 redirect_uri
from src.auth.providers import get_provider, list_providers
missing = [p for p in list_providers() if not get_provider(p).get("redirect_uri")]
if missing:
    errors.append(f"生产环境必须配置 redirect_uri，缺少: {', '.join(missing)}")
```

---

## 5. P2 详细设计

### 5.1 需求 3：回调恢复分路径

#### 5.1.1 改动点

**文件 1：`src/api/routes/grape_auth.py` — 路由定义**

当前：
```python
@router.get("/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
```

改造后：
```python
@router.get("/callback/{provider}")
def oauth_callback(request: Request, provider: str, code: str = "", state: str = "", error: str = ""):
```

- provider 从 URL 路径参数获取
- 删除旧路由 `@router.get("/oauth/callback")`
- 回调入口校验 URL 路径中的 provider 与 state 中的 provider 一致：

```python
# 在 state_data 恢复后
db_provider = state_data.get("_provider", "")
if provider != db_provider:
    return _oauth_error_redirect(request, "provider_mismatch", popup=popup)
```

**文件 2：`src/auth/providers.py` — `get_redirect_uri()`**

已在需求 11 中改造，返回分路径 `/api/auth/callback/{provider}`。

**文件 3：调用方传 provider**

所有调用 `get_redirect_uri(request)` 的地方改为 `get_redirect_uri(request, provider)`：

| 函数 | 行号 | 当前调用 |
|------|------|---------|
| `oauth_login()` | L1248 | `get_redirect_uri(request)` → `get_redirect_uri(request, provider)` |
| `bind_provider()` | L883 | `get_redirect_uri(request)` → `get_redirect_uri(request, provider)` |
| `delete_account_via_oauth()` | L677 | `get_redirect_uri(request)` → `get_redirect_uri(request, provider)` |
| `oauth_callback()` | L1610 | `get_redirect_uri(request)` → `get_redirect_uri(request, provider)` |

**文件 4：`oauth_callback()` 中 exchange_token 的 redirect_uri**

回调时 `exchange_token(provider, code, redirect_uri, pkce_verifier)` 中的 redirect_uri 必须与发起时的 redirect_uri 一致。改造后回调时传 `get_redirect_uri(request, provider)`，与发起时一致。

### 5.2 需求 7：合并账号邮箱清理

已在需求 6 的 `mark_account_merged()` 中同步实现（清空 email/email_source/email_is_placeholder）。

`transfer_bind()` 中也需要同步修改——当前代码（grape_auth.py L2117）：
```python
c.execute(
    "UPDATE grape_accounts SET status=4, username='merged_' || CAST(id AS TEXT), "
    "updated_at=? WHERE id=?",
    (now, original_user_id),
)
```

改为：
```python
c.execute(
    "UPDATE grape_accounts SET status=4, username='merged_' || CAST(id AS TEXT), "
    "email=NULL, email_source=NULL, email_is_placeholder=0, "
    "updated_at=? WHERE id=?",
    (now, original_user_id),
)
```

### 5.3 需求 10：解绑时 placeholder 邮箱清理走数据访问层

#### 5.3.1 新增函数

**文件：`src/auth/grape_account_db.py`**

```python
def clear_placeholder_email(user_id: int, provider: str) -> bool:
    """清理 placeholder 邮箱（解绑时调用）。

    如果 email 是 placeholder 且 email_source == provider → 清空。
    否则不动，返回 True。

    ⚠️ 必须在外部 with db_lock 事务内调用，或在独立调用时自带事务。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT email, email_source, email_is_placeholder FROM grape_accounts WHERE id=?",
                (user_id,),
            )
            row = c.fetchone()
            if not row:
                return True
            email, email_source, is_placeholder = row
            if email and is_placeholder and email_source == provider:
                import time as _t
                now = _t.strftime("%Y-%m-%d %H:%M:%S")
                c.execute(
                    "UPDATE grape_accounts SET email=NULL, email_source=NULL, "
                    "email_is_placeholder=0, updated_at=? WHERE id=?",
                    (now, user_id),
                )
                db.commit()
                log.info(f"Cleared placeholder email for user={user_id}, provider={provider}")
            return True
    except Exception as e:
        log.warning(f"clear_placeholder_email failed: {e}")
        return False
```

#### 5.3.2 改造 unbind_provider

**文件：`src/api/routes/grape_auth.py` — `unbind_provider()` 函数（L950-966）**

当前代码直接写 SQL 操作 grape_accounts 表。

改造后：
```python
# 删除绑定关系
deleted = delete_external_identity(user["id"], provider)
if not deleted:
    raise HTTPException(status_code=404, detail={"code": 404, "message": "未绑定该平台"})

# 解绑后清理 placeholder 邮箱（走数据访问层）
from src.auth.grape_account_db import clear_placeholder_email
clear_placeholder_email(user["id"], provider)

# R30: 先计算受影响仓库数量
...
```

---

## 6. 升级脚本

### 6.1 清空 oauth_states 表

```sql
-- 需求 1：旧 state 无 session_nonce，不做兼容，直接清空
DELETE FROM oauth_states;
```

### 6.2 确认 provider 配置 redirect_uri

发布前确认 `configs/oauth-providers.json` 中每个 provider 配置了 `redirect_uri` 字段：

```json
{
  "gitcode": {
    "redirect_uri": "https://grape.example.com/api/auth/callback/gitcode",
    ...
  },
  "github": {
    "redirect_uri": "https://grape.example.com/api/auth/callback/github",
    ...
  }
}
```

### 6.3 无 DDL 变更

本次改造不新增表、不改列定义。所有字段已存在（external_identities.refresh_token、grape_accounts.email_source 等）。

---

## 7. 回滚方案

### 7.1 代码回滚

直接 `git revert` 对应批次的 commit。每批次独立 commit，可单独回滚。

### 7.2 数据回滚

- oauth_states 表清空：无需回滚（开发/测试数据，无业务价值）
- external_identities：如果 refresh_oauth_token 改造后执行过 token 刷新，access_token 已更新到 external_identities。回滚后 refresh_oauth_token 回到从 user_credentials 读，但 user_credentials 中的 refresh_token 在改造后仍然同步更新（设计上保留了 set_credential 同步写入），所以回滚不影响
- grape_accounts：如果 merge_placeholder_account 改造后执行过合并，status=4 + email=NULL 不可逆。但合并操作只在绑定冲突时触发，频率极低

### 7.3 配置回滚

provider 配置中的 `redirect_uri` 字段回滚后不被读取（旧代码不读此字段），无副作用。

---

## 8. 测试策略

### 8.1 单元测试

| 需求 | 测试文件 | 测试用例 |
|------|---------|---------|
| 需求 1 | tests/test_oauth_login.py（新增） | 正常登录 nonce 匹配 / 攻击者无 cookie 被拒 / cookie 篡改被拒 / 绑定流程不校验 / 注销流程不校验 / popup 模式 |
| 需求 2 | tests/test_user_db.py（扩展） | 单实例双重消费 / rowcount=0 / 过期 state |
| 需求 4 | tests/test_user_db.py（扩展） | 从 external_identities 读 / fallback 读 user_credentials / 解密失败 / 空 refresh_token 不覆盖 |
| 需求 5 | tests/test_user_db.py（扩展） | gitcode/github 刷新后 external_identities 更新 / 无硬编码分支 |
| 需求 6 | tests/test_transfer_bind.py（扩展） | unbind→create→mark 全成功 / 各步失败回滚 / UNIQUE 冲突防护 |
| 需求 8 | tests/test_cooldown.py（新增） | 全步骤成功 / 中间失败回滚 / 幂等 / 审计日志独立 |
| 需求 9 | tests/test_cooldown.py | 无 datetime('now') / MySQL 兼容 |
| 需求 11 | tests/test_providers.py（扩展） | 生产环境未配置拒绝启动 / 从配置读取 / 开发环境从 Host header |
| 需求 3 | tests/test_oauth_callback.py（新增） | 分路径路由 / provider 路径 vs state 不一致 / 旧路由已删除 |
| 需求 7 | tests/test_transfer_bind.py | status=4 邮箱为 NULL / 新用户可注册 |
| 需求 10 | tests/test_unbind.py（扩展） | 走数据访问层 / placeholder 邮箱清理 / 非 placeholder 不受影响 |

### 8.2 E2E 验证

| 场景 | 验证步骤 |
|------|---------|
| GitCode OAuth 登录 | 发起登录 → 收到 nonce cookie → 回调校验通过 → 签发 JWT → 前端拿到 exchange_code → 换 JWT 成功 |
| GitCode OAuth 登录（攻击模拟） | 攻击者拿到 state+code → 受害者无 cookie 访问回调 → 被拒绝 → 重定向登录页 |
| GitHub OAuth 绑定 | 已登录用户发起绑定 → 回调走绑定分支 → 绑定成功 |
| 占位账号自动合并 | 占位账号有活跃绑定 → 新用户绑定同一 external_id → unbind→create→mark 单事务成功 |
| token 自动刷新 | 新 OAuth 用户 token 过期 → refresh_oauth_token 从 external_identities 读 → 刷新成功 |
| 冷静期匿名化 | status=3 账号 → 匿名化单事务执行 → status=2 + 数据清除 → 审计日志 |
| 分路径回调 | GitCode 平台回调 /api/auth/callback/gitcode → 路由存在 → 登录成功 |

---

## 9. 函数调用关系图

### 9.1 OAuth 登录流程（改造后）

```
GET /api/auth/login/{provider}
  → oauth_login()
    → 生成 nonce
    → build_auth_url(provider, state, redirect_uri, extra_state={login_flow, session_nonce})
      → save_oauth_state(state, provider, data)  ← nonce 存入 data_json
    → Set-Cookie: oauth_nonce=nonce
    → 返回 redirect_url

浏览器跳转到 OAuth 平台 → 平台回调

GET /api/auth/callback/{provider}?code=xxx&state=xxx
  → oauth_callback(request, provider, code, state)
    → validate_oauth_state(state)  ← 原子消费（SELECT+DELETE+rowcount）
    → 校验 URL provider == state provider
    → 校验 nonce cookie（仅 login_flow）
    → exchange_token(provider, code, redirect_uri)
    → get_user_info(provider, access_token)
    → 分流：login_flow → _handle_oauth_login()
    → Set-Cookie: oauth_nonce=; Max-Age=0  ← 清除 cookie
    → 返回 exchange_code
```

### 9.2 占位账号合并流程（改造后）

```
_handle_oauth_bind()
  → get_external_identity(provider, external_id)
  → 冲突 + is_placeholder_account_mergeable → True
  → 检查当前用户已有该 provider 绑定 → 无
  → with db_lock:
      Step 1: unbind_placeholder_binding(c, conflict_user_id, provider)
      Step 2: restore_binding_in_tx(c, binding_user_id, provider, ...)
      Step 3: mark_account_merged(c, conflict_user_id)  ← 含邮箱清理
      db.commit()
  → 审计日志
  → 重定向
```

### 9.3 token 刷新流程（改造后）

```
deps.py get_user_token_with_refresh(username, user_id)
  → _get_oauth_token_from_identity(username, "gitcode")  ← 读 access_token
  → token 过期 → try_refresh_gitcode_token(user_id)
    → refresh_gitcode_oauth_token(user_id)
      → refresh_oauth_token(user_id, "gitcode")
        → _read_refresh_token_from_identity(user_id, provider)  ← 权威存储
        → fallback: _read_refresh_token_from_credentials(user_id, provider)
        → 调 OAuth refresh endpoint
        → update_external_identity_tokens(user_id, provider, ...)  ← 写回权威存储
        → set_credential(user_id, provider_key, ...)  ← 同步 user_credentials（迁移期）
```

---

## 10. 风险与注意事项

### 10.1 cookie 在反向代理下的行为

nginx 反代可能影响 cookie 传递：
- 确保 nginx 不修改 Set-Cookie 头
- 确保 nginx 传递 X-Forwarded-Proto（生产环境 HTTPS 判断）
- Secure cookie 在 HTTPS 下正常传递

### 10.2 oauth_callback 多返回点的 cookie 清除

oauth_callback 有多个返回点（error/state_invalid/各分流结果）。login flow 的每个返回点都需要清除 nonce cookie。实现时用条件判断 `if need_clear_nonce: resp.delete_cookie(...)`，不能遗漏任何一个返回点。

### 10.3 restore_binding_in_tx 替代 create_external_identity_in_tx

原设计的 `create_external_identity_in_tx` 已删除——它内部含 SELECT 查询，在 MySQL 下会触发 auto-commit 架空事务。替代函数 `restore_binding_in_tx` 用条件 UPDATE + rowcount 代替 SELECT，全程只写不读，MySQL 安全。`create_external_identity`（非事务版）仍保留，用于独立事务场景。

### 10.4 transfer_bind 中的邮箱清理

需求 7 要求 transfer_bind 中 status=4 时清邮箱。当前 transfer_bind 的代码（grape_auth.py L2117）在同一个 `with _ga_dl:` 事务内，直接在 cursor 上执行 SQL。改造时只需在现有 SQL 中加 `email=NULL, email_source=NULL, email_is_placeholder=0`，不需要改事务结构。

### 10.5 deps.py 的 token 预刷新逻辑

`get_user_token_with_refresh()` 中检查 token 年龄用的是 `cred.get("fetched_at")`，从 user_credentials 读。改造后 refresh_oauth_token 仍然同步写入 user_credentials（包括 fetched_at），所以 deps.py 的预刷新逻辑不受影响。

### 10.6 加固项：redirect_uri 从 state 恢复（P2）

**问题**：`oauth_callback` 中 `exchange_token` 需要的 `redirect_uri` 是重新调 `get_redirect_uri(request, provider)` 构造的，而非从 state 恢复。如果 admin 在用户发起 OAuth 和回调之间修改了 provider 配置的 `redirect_uri`，两次调用结果不同，导致 token 交换失败（OAuth 平台校验 redirect_uri 不一致）。

**风险**：极低——OAuth 流程通常几秒内完成，admin 恰好在这几秒改配置的概率很小。但一旦发生，表现为随机、偶发的 token 交换失败，日志中两个 redirect_uri 值看起来都"正确"，排查成本高。

**修复方案**：发起时将 `redirect_uri` 存入 state_data（与 `pkce_verifier` 同理），回调时从 state 恢复而非重新构造。

**发起阶段**（`build_auth_url`）：

```python
state_data = dict(extra_state_data or {})
if code_verifier:
    state_data["pkce_verifier"] = code_verifier
state_data["redirect_uri"] = redirect_uri  # 新增
user_db.save_oauth_state(state, provider, state_data)
```

**回调阶段**（`oauth_callback`）：

```python
state_data = user_db.validate_oauth_state(state)
# 从 state 恢复 redirect_uri（不再重新构造）
redirect_uri = state_data.get("redirect_uri", "")
if not redirect_uri:
    # 旧 state 兼容（升级时已清空 oauth_states，正常不会走到）
    redirect_uri = get_redirect_uri(request, provider)
```

**设计要点**：

- `redirect_uri` 存在 `oauth_states` 表的 `data_json` 中，服务端存储，攻击者无法篡改
- 系统未上线，升级时 `DELETE FROM oauth_states`，旧 state 无 `redirect_uri` 字段不会存在
- fallback 重新构造仅作为防御性兜底，正常流程不触发
- `exchange_token` 的签名已显式接收 `redirect_uri` 参数，不需要内部再调 `get_redirect_uri`

**不改动需求文档**：此加固项属于实现层最佳实践，不影响需求层面的验收标准。

---

## 附录 A：需求与设计章节对照表

| 需求 | 优先级 | 设计章节 |
|------|:---:|---------|
| 需求 1：state 绑定 session | P0 | §3.1 |
| 需求 2：state 原子消费 | P0 | §3.2 |
| 需求 3：回调分路径 | P2 | §5.1 |
| 需求 4：refresh_token 链路统一 | P0 | §3.3 |
| 需求 5：消除 provider 硬编码 | P1 | §3.3.4（随需求 4 同步实现） |
| 需求 6：merge 同事务 | P1 | §4.1 |
| 需求 7：status=4 邮箱清理 | P2 | §5.2 |
| 需求 8：cooldown 单事务 | P1 | §4.2 |
| 需求 9：cooldown datetime 修复 | P0 | §3.4 |
| 需求 10：解绑走数据访问层 | P2 | §5.3 |
| 需求 11：redirect_uri 固定 | P1 | §4.3 |

## 附录 B：函数签名变更清单

| 函数 | 文件 | 变更 |
|------|------|------|
| `oauth_login()` | grape_auth.py | 返回 JSONResponse → set_cookie(oauth_nonce) |
| `oauth_callback()` | grape_auth.py | 路由改 /callback/{provider}，加 nonce 校验 |
| `validate_oauth_state()` | user/db.py | SELECT+DELETE+rowcount 原子消费 |
| `refresh_oauth_token()` | user/db.py | 读取源改 external_identities + fallback |
| `_read_refresh_token_from_identity()` | user/db.py | 新增 |
| `_read_refresh_token_from_credentials()` | user/db.py | 新增 |
| `merge_placeholder_account()` | grape_account_db.py | 废弃（保留兼容包装），拆为 unbind_placeholder_binding + mark_account_merged |
| `unbind_placeholder_binding()` | grape_account_db.py | 新增 |
| `mark_account_merged()` | grape_account_db.py | 新增 |
| `restore_binding_in_tx()` | grape_account_db.py | 新增 |
| `_anonymize_account()` | cooldown.py | Step 1-4 单事务 + datetime 修复 |
| `get_redirect_uri()` | providers.py | 从 provider 配置读 redirect_uri，fallback Host header |
| `_check_security_config()` | main.py | 新增 OAUTH_REDIRECT_URI 校验 |
| `clear_placeholder_email()` | grape_account_db.py | 新增 |
| `unbind_provider()` | grape_auth.py | 邮箱清理改调 clear_placeholder_email |
| `transfer_bind()` | grape_auth.py | status=4 SQL 加 email 清理 |
