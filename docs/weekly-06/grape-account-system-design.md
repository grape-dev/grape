# Grape 独立账号体系技术方案

## 1. 现状分析

### 1.1 当前账号架构

Grape 当前没有独立的账号体系，用户身份完全依赖第三方平台 OAuth 登录：

| 组件 | 现状 |
|------|------|
| `user_accounts` 表 | 以 OAuth 平台 ID 为主标识，无独立密码体系（password 字段虽然存在但仅用于兜底） |
| `unified_user_mapping` 表 | 跨平台 ID 映射表，但以 `user_id`（即 `user_accounts.id`）为枢纽 |
| `user_credentials` 表 | 存储各平台凭据（token 等），以 `(user_id, provider)` 为复合主键 |
| `oauth_states` 表 | OAuth state 防 CSRF，已有通用 provider 字段 |
| JWT | 双 token 机制（access 15min + refresh 7天滚动/30天绝对上限），已有 `login_provider` 字段 |
| 登录入口 | GitCode OAuth + GitHub OAuth + 密码登录（管理员创建） |

### 1.2 核心问题

1. **无独立身份** — 用户必须先有 GitCode/GitHub 账号才能使用 Grape，无法独立注册
2. **平台耦合** — `user_accounts` 表直接内嵌 `gitcode_id`、`github_id`、`gitcode_api_token` 等字段，新增平台需要改表结构
3. **映射分散** — `user_accounts` 内有平台 ID 列，`unified_user_mapping` 也有平台映射，两处冗余
4. **扩展困难** — 未来接入微信、华为账号等需要持续在 `user_accounts` 表加列

### 1.3 现有可复用资产

- JWT 双 token 机制已成熟（access + refresh + token_version 撤销）
- `oauth_states` 表已有通用 `provider` 字段
- `user_credentials` 表已有 `(user_id, provider)` 模型
- `unified_user_mapping` 已有多平台映射意识
- 密码哈希函数 `_hash_password`（SHA-256）已存在（需升级为 bcrypt/argon2）

---

## 2. 设计目标

1. **Grape 账号为唯一身份** — 用户通过 Grape 自身账号登录，第三方账号为可绑定的外部身份
2. **解耦** — Grape 账号表不包含任何第三方平台字段，第三方绑定独立存储
3. **可扩展** — 新增第三方平台（微信、华为等）只需配置，不改表结构
4. **向后兼容** — 现有用户平滑迁移，不中断服务
5. **OAuth 登录仍可用** — 第三方登录时自动关联到 Grape 账号（已绑定→直接登录；未绑定→引导注册/绑定）

---

## 3. 数据模型

### 3.1 新增表

#### `grape_accounts` — Grape 独立账号表

```sql
CREATE TABLE IF NOT EXISTS grape_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        VARCHAR(64) NOT NULL UNIQUE,      -- Grape 用户名（唯一）
    email           VARCHAR(255) DEFAULT '',           -- 邮箱（可选，用于找回密码）
    phone           VARCHAR(32) DEFAULT '',            -- 手机号（可选，未来短信验证）
    password_hash   TEXT NOT NULL DEFAULT '',          -- bcrypt 哈希（替代 SHA-256）
    nickname        VARCHAR(128) DEFAULT '',           -- 显示名称
    avatar_url      VARCHAR(512) DEFAULT '',           -- 头像 URL
    status          INTEGER DEFAULT 1,                 -- 1=正常 0=禁用 -1=待激活
    token_version   INTEGER DEFAULT 0,                 -- JWT 撤销版本号
    last_active_at TEXT DEFAULT NULL,                 -- 最后活跃时间
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
```

#### `external_identities` — 第三方身份绑定表

```sql
CREATE TABLE IF NOT EXISTS external_identities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    grape_user_id   INTEGER NOT NULL,                 -- 关联 grape_accounts.id
    provider        VARCHAR(32) NOT NULL,             -- 'gitcode' | 'github' | 'wechat' | 'huawei' | ...
    external_id     VARCHAR(128) NOT NULL,            -- 第三方平台用户 ID
    external_login  VARCHAR(128) DEFAULT '',          -- 第三方用户名（如 gitcode login）
    external_name   VARCHAR(128) DEFAULT '',           -- 第三方昵称
    external_avatar VARCHAR(512) DEFAULT '',           -- 第三方头像
    access_token    TEXT DEFAULT '',                   -- OAuth access_token（可刷新）
    refresh_token   TEXT DEFAULT '',                   -- OAuth refresh_token
    token_expires_at TEXT DEFAULT '',                  -- token 过期时间
    metadata        TEXT DEFAULT '{}',                 -- JSON：平台特有数据（如 scope、open_id 等）
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    UNIQUE (provider, external_id),                    -- 同一第三方账号只能绑定一个 Grape 账号
    UNIQUE (grape_user_id, provider)                   -- 同一 Grape 账号同一平台只能绑定一个
);
```

### 3.2 改造现有表

#### `user_accounts` → 迁移至 `grape_accounts`

迁移完成后，`user_accounts` 表保留但不再写入，所有读写切换到 `grape_accounts`。

原有散落在 `user_accounts` 的平台字段（`gitcode_id`、`github_id`、`gitcode_api_token`、`github_api_token` 等）迁移到 `external_identities`。

#### `unified_user_mapping` → 废弃

跨平台映射由 `external_identities` 统一承担。迁移完成后 `unified_user_mapping` 只读，不再写入。

#### `user_credentials` → 保留

`user_credentials (user_id, provider, config_json)` 结构不变，继续存储各平台凭据（AK/SK 等），`user_id` 改为引用 `grape_accounts.id`。

### 3.3 ER 关系

```
grape_accounts (1) ──< external_identities (N)     -- 一个 Grape 账号可绑定多个第三方
grape_accounts (1) ──< user_credentials (N)         -- 一个 Grape 账号有多组凭据
grape_accounts (1) ──< organization.owner_id (1)    -- 个人组织归属
grape_accounts (1) ──< org_member.user_id (N)       -- 组织成员关系
grape_accounts (1) ──< agent_tasks.user_id (N)      -- 任务归属
```

---

## 4. 认证流程

### 4.1 Grape 账号注册

```
用户填写 username + password (+ email 可选)
    ↓
校验 username 唯一性
    ↓
bcrypt 哈希密码 → 写入 grape_accounts
    ↓
自动创建个人组织（is_personal=1, name="默认工作空间"）
    ↓
签发 JWT (login_provider="grape")
    ↓
返回 access_token + set refresh cookie
```

### 4.2 Grape 账号登录

```
用户输入 username + password
    ↓
grape_accounts 查询 + bcrypt.verify
    ↓
校验 status=1（未禁用）
    ↓
签发 JWT (login_provider="grape", token_version=N)
    ↓
返回 access_token + set refresh cookie
```

### 4.3 第三方 OAuth 登录（已绑定）

```
用户点击「GitCode 登录」/「GitHub 登录」
    ↓
OAuth redirect → callback → 获取 external_id
    ↓
external_identities 查询 (provider='gitcode', external_id=xxx)
    ↓
找到 → 取 grape_user_id → 查 grape_accounts
    ↓
签发 JWT (login_provider="gitcode", token_version=N)
    ↓
返回 access_token + set refresh cookie → 登录成功
```

### 4.4 第三方 OAuth 登录（未绑定）

```
用户点击第三方登录 → callback → 获取 external_id
    ↓
external_identities 查询无记录
    ↓
方案 A：用户已登录（有 Grape session）
    → 直接绑定到当前 Grape 账号 → 写入 external_identities → 完成
    ↓
方案 B：用户未登录
    → 跳转「关联账号」页面：
      ├─ 选择「绑定已有 Grape 账号」→ 输入 username/password → 绑定
      └─ 选择「注册新 Grape 账号」→ 填写 username/password → 创建 → 绑定
    → 绑定完成后签发 JWT → 登录成功
```

### 4.5 账号绑定/解绑

```
绑定流程（需已登录 Grape）：
    GET /api/auth/bind/{provider} → OAuth redirect → callback
    → external_identities 写入 (grape_user_id, provider, external_id, ...)
    → 同时刷新 user_credentials 中该 provider 的 token

解绑流程：
    DELETE /api/auth/bind/{provider}
    → 检查：至少保留一种登录方式（Grape 密码 或 ≥1 个第三方绑定）
    → 删除 external_identities 记录
    → 清除 user_credentials 中对应 provider
```

---

## 5. API 设计

### 5.1 Grape 账号管理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册 Grape 账号（username + password + email?） |
| POST | `/api/auth/login` | Grape 账号密码登录（已有，改造为查 grape_accounts） |
| POST | `/api/auth/refresh` | 刷新 token（已有，不变） |
| POST | `/api/auth/logout` | 登出（token_version+1，已有逻辑） |
| GET | `/api/auth/me` | 获取当前用户信息 + 已绑定第三方列表 |
| PUT | `/api/auth/profile` | 修改昵称、头像 |
| PUT | `/api/auth/password` | 修改密码（token_version+1） |

### 5.2 第三方绑定

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/bind/{provider}` | 发起第三方绑定 OAuth（需登录态） |
| GET | `/api/auth/bind/{provider}/callback` | 绑定 OAuth 回调 |
| DELETE | `/api/auth/bind/{provider}` | 解绑第三方 |
| GET | `/api/auth/identities` | 获取已绑定的第三方列表 |

### 5.3 OAuth 登录（已有，改造）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/login/{provider}` | 发起第三方 OAuth 登录（provider 可枚举） |
| GET | `/api/auth/callback/{provider}` | OAuth 回调（统一入口，按 provider 分流） |

### 5.4 管理员 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/users` | 用户列表（分页、搜索） |
| PUT | `/api/admin/users/{id}/status` | 启用/禁用用户 |

---

## 6. 密码安全升级

### 6.1 哈希算法

当前使用 SHA-256（无盐），升级为 bcrypt：

```python
import bcrypt

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())
```

### 6.2 旧密码迁移

`grape_accounts.password_hash` 加前缀标识算法版本：

- `bcrypt$...` — bcrypt 哈希
- `sha256$...` — 旧 SHA-256 哈希（兼容期）

用户用旧算法登录时，验证通过后自动用 bcrypt 重新哈希并更新（透明升级）。

---

## 7. 第三方平台扩展机制

### 7.1 Provider 注册表

```python
# src/auth/providers.py

PROVIDERS = {
    "gitcode": {
        "name": "GitCode",
        "auth_url": "https://gitcode.com/oauth/authorize",
        "token_url": "https://gitcode.com/oauth/token",
        "user_url": "https://gitcode.com/api/v5/user",
        "scope": "all",
        "id_field": "id",
        "login_field": "login",
        "name_field": "nickname",
    },
    "github": {
        "name": "GitHub",
        "auth_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "user_url": "https://api.github.com/user",
        "scope": "user:email",
        "id_field": "id",
        "login_field": "login",
        "name_field": "name",
    },
    # 未来扩展：
    # "wechat": { ... },
    # "huawei": { ... },
}
```

### 7.2 新增平台流程

1. 在 `PROVIDERS` 注册表中添加配置
2. 在 `meta` 表配置 `oauth_{provider}_client_id` / `oauth_{provider}_client_secret`
3. 前端登录页自动展示新平台按钮
4. 无需改表结构、无需改业务代码

---

## 8. 数据迁移方案

### 8.1 迁移步骤

```
Phase 1: 建表（不停服）
    → CREATE TABLE grape_accounts
    → CREATE TABLE external_identities

Phase 2: 数据迁移（不停服，脚本执行）
    → 遍历 user_accounts
    → 为每条记录在 grape_accounts 创建对应行
      - id 保持一致
      - username 直接复制
      - password_hash 加 "sha256$" 前缀
      - nickname 复制
    → 遍历 user_accounts 的平台字段
      - 有 gitcode_id → external_identities 写入 (provider='gitcode', external_id=xxx, ...)
      - 有 github_id → external_identities 写入 (provider='github', external_id=xxx, ...)
    → unified_user_mapping 中的 feishu/dingtalk 映射也迁入 external_identities

Phase 3: 读写切换（灰度发布）
    → 所有读写从 user_accounts 切换到 grape_accounts
    → OAuth 登录查询从 user_accounts.gitcode_id 改为 external_identities 查询
    → user_credentials 的 user_id 引用不变（id 一致）

Phase 4: 清理（确认稳定后）
    → user_accounts 表改名为 user_accounts_deprecated
    → unified_user_mapping 表废弃
```

### 8.2 迁移脚本

```python
# scripts/migrate_grape_accounts.py

def migrate():
    """Phase 2 数据迁移"""
    # 1. user_accounts → grape_accounts
    users = db.execute("SELECT * FROM user_accounts")
    for u in users:
        db.execute("""
            INSERT OR IGNORE INTO grape_accounts
                (id, username, email, password_hash, nickname, avatar_url,
                 status, token_version, last_active_at, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (u["id"], u["username"], "", f"sha256${u['password_hash']}",
              u.get("nickname", ""), "", 1, u.get("token_version", 0),
              u.get("last_active_at"), u["created_at"], u["created_at"]))

    # 2. 平台字段 → external_identities
    for u in users:
        if u.get("gitcode_id"):
            upsert_external_identity(u["id"], "gitcode", u["gitcode_id"],
                                     u.get("gitcode_api_token", ""), ...)
        if u.get("github_id"):
            upsert_external_identity(u["id"], "github", u["github_id"],
                                     u.get("github_api_token", ""), ...)

    # 3. unified_user_mapping 的飞书/钉钉映射
    mappings = db.execute("SELECT * FROM unified_user_mapping")
    for m in mappings:
        if m.get("feishu_open_id"):
            upsert_external_identity(m["user_id"], "feishu", m["feishu_open_id"], ...)
        if m.get("dingtalk_user_id"):
            upsert_external_identity(m["user_id"], "dingtalk", m["dingtalk_user_id"], ...)
```

---

## 9. 前端改造

### 9.1 登录页

```
┌─────────────────────────────────┐
│         Grape 登录              │
│                                 │
│    [用户名]                     │
│    [密码]                       │
│    [登录]  [注册]               │
│                                 │
│    ── 或使用第三方登录 ──        │
│                                 │
│    [GitCode]  [GitHub]          │
│    [微信]     [华为]  (未来)     │
│                                 │
└─────────────────────────────────┘
```

### 9.2 账号设置页 — 第三方绑定

```
┌─────────────────────────────────┐
│       账号设置                   │
│                                 │
│  基本信息                        │
│    用户名: songspace             │
│    昵称: [输入框] [保存]          │
│    邮箱: [输入框]                │
│    修改密码 →                    │
│                                 │
│  第三方账号绑定                   │
│    GitCode   ✓ 已绑定  [解绑]    │
│    GitHub    ✗ 未绑定  [绑定]    │
│    微信      ✗ 未绑定  [绑定]    │
│    华为      ✗ 未绑定  [绑定]    │
│                                 │
└─────────────────────────────────┘
```

### 9.3 OAuth 未绑定跳转页

```
┌─────────────────────────────────┐
│    关联 Grape 账号               │
│                                 │
│  您的 GitCode 账号尚未关联       │
│  Grape 账号，请选择：            │
│                                 │
│  [绑定已有 Grape 账号]           │
│   用户名 [____] 密码 [____]      │
│   [确认绑定]                    │
│                                 │
│  [注册新 Grape 账号]             │
│   用户名 [____] 密码 [____]      │
│   确认密码 [____]                │
│   [注册并绑定]                   │
│                                 │
└─────────────────────────────────┘
```

---

## 10. 代码改造清单

### 10.1 新增文件

| 文件 | 说明 |
|------|------|
| `src/auth/providers.py` | 第三方平台 Provider 注册表 + 统一 OAuth 流程 |
| `src/auth/grape_account_db.py` | grape_accounts + external_identities CRUD |
| `scripts/migrate_grape_accounts.py` | 数据迁移脚本 |

### 10.2 改造文件

| 文件 | 改动 |
|------|------|
| `src/api/routes/auth.py` | 登录/注册/OAuth 回调改为查 grape_accounts + external_identities |
| `src/api/jwt_utils.py` | login_provider 增加 "grape" 值 |
| `src/user/db.py` | verify_user / create_user 改为操作 grape_accounts |
| `src/api/db_setup.py` | 新增 grape_accounts + external_identities DDL + 迁移 |
| `src/api/deps.py` | get_current_user 改为查 grape_accounts |
| `src/api/routes/orgs.py` | owner_id 引用不变（id 一致） |
| `src/api/static/js/pages/login.js` | 新增注册表单 + 第三方绑定 UI |
| `src/api/static/js/app.js` | 用户信息展示改为读 grape_accounts |
| `src/api/static/locales/*/auth.json` | 新增注册/绑定相关 i18n 文案 |

### 10.3 废弃文件/表

| 对象 | 处理 |
|------|------|
| `user_accounts` 表 | 迁移后改名 `_deprecated_user_accounts` |
| `unified_user_mapping` 表 | 迁移后废弃，不再写入 |
| `user_accounts` 中的平台字段 | 迁移后删除（deprecated 表保留） |

---

## 11. 实施计划

### Phase 1 — 基础设施（1-2 天）

- [ ] 建表 `grape_accounts` + `external_identities`
- [ ] 实现 `src/auth/grape_account_db.py`（CRUD）
- [ ] 实现 `src/auth/providers.py`（Provider 注册表 + 统一 OAuth 流程）
- [ ] 密码哈希升级 bcrypt + 兼容 SHA-256

### Phase 2 — 数据迁移（0.5 天）

- [ ] 编写迁移脚本 `scripts/migrate_grape_accounts.py`
- [ ] 执行迁移，验证数据一致性

### Phase 3 — 后端改造（2-3 天）

- [ ] 改造 `auth.py` 路由（注册/登录/OAuth/绑定/解绑）
- [ ] 改造 `deps.py`（get_current_user）
- [ ] 改造 `user/db.py`（verify_user / create_user）
- [ ] 新增 API：`/api/auth/register`、`/api/auth/bind/{provider}`、`/api/auth/identities`

### Phase 4 — 前端改造（1-2 天）

- [ ] 登录页增加注册表单
- [ ] 账号设置页增加第三方绑定/解绑 UI
- [ ] OAuth 未绑定跳转页
- [ ] i18n 文案

### Phase 5 — 测试 & 上线（1 天）

- [ ] 单元测试：注册/登录/绑定/解绑/OAuth 全流程
- [ ] 迁移验证：数据一致性、登录功能正常
- [ ] 灰度发布 → 全量

---

## 12. 安全设计

| 威胁 | 防御 |
|------|------|
| 密码泄露 | bcrypt(rounds=12) + 透明升级旧 SHA-256 |
| 暴力破解 | 登录失败 5 次锁定 15 分钟（status=0） |
| Token 劫持 | refresh_token 存 HttpOnly cookie + token_version 撤销 |
| OAuth CSRF | state 参数 + oauth_states 表校验 |
| 账号劫持 | 解绑前检查至少保留一种登录方式 |
| 重复绑定 | `(provider, external_id)` UNIQUE 约束 |
| 平台 ID 欺骗 | OAuth 回调只信任 token 换取的 user info，不信任前端传入 |

---

## 13. 未来扩展

### 13.1 微信登录

```python
PROVIDERS["wechat"] = {
    "name": "微信",
    "auth_url": "https://open.weixin.qq.com/connect/qrconnect",
    "token_url": "https://api.weixin.qq.com/sns/oauth2/access_token",
    "user_url": "https://api.weixin.qq.com/sns/userinfo",
    "scope": "snsapi_login",
    "id_field": "openid",
    "login_field": "openid",
    "name_field": "nickname",
}
```

### 13.2 华为账号登录

```python
PROVIDERS["huawei"] = {
    "name": "华为",
    "auth_url": "https://oauth-login.cloud.huawei.com/oauth2/v3/authorize",
    "token_url": "https://oauth-login.cloud.huawei.com/oauth2/v3/token",
    "user_url": "https://api.vmall.com/rest.php?nsp_svc=OpenID.User.getInfo",
    "scope": "https://www.huawei.com/auth/account/base.profile",
    "id_field": "userID",
    "login_field": "userID",
    "name_field": "displayName",
}
```

### 13.3 其他扩展方向

- **邮箱验证** — 注册时发送验证邮件，激活后 status=1
- **短信验证** — 手机号注册/登录（接入短信网关）
- **SSO/OIDC** — 企业内部 SSO 对接（OIDC 协议）
- **MFA** — 双因素认证（TOTP / 短信验证码）
- **账号合并** — 多个 Grape 账号合并（数据迁移 + 保留主账号）
