# Grape 账号体系技术设计文档（v2）

> 需求文档：docs/weekly-06/grape-account-system-requirements.md（需求 1-25）
> 引导页需求：docs/weekly-06/onboarding-experience-requirements.md（需求 31）
> 引导页技术设计：docs/weekly-06/onboarding-experience-tech-design.md
> 平台体验需求：docs/weekly-06/platform-experience-requirements.md（需求 26-30）
> 分支：feat/grape-account-system，基于 grape-dev/multitask

---

## 1. 现状架构分析

### 1.1 认证链路

```
请求 → Authorization: Bearer ***
     → token 前缀分流（tk_ → session_token / grape_ → api_key / 其他 → JWT）
     → JWT: verify_access_token → get_status_and_token_version(uid) → 比对 payload.ver + 校验 status
     → 通过 → 返回 {id, username, token, token_type, login_provider}
```

### 1.2 数据模型现状

| 表 | 位置 | 职责 | 状态 |
|---|---|---|---|
| grape_accounts | src/auth/grape_account_db.py | Grape 独立账号 | 已实现，6 张新表之一 |
| external_identities | src/auth/grape_account_db.py | 第三方身份绑定 | 已实现 |
| verification_codes | src/auth/verification.py | 验证码（含 IP 维度限流） | 已实现 |
| audit_logs | src/auth/audit.py | 审计日志（哈希链） | 已实现 |
| signup_whitelist | src/auth/signup_policy.py | 注册白名单 | 已实现 |
| login_failures | src/auth/login_throttle.py | 登录失败记录 | 已实现 |
| exchange_codes | src/api/db_setup.py | OAuth exchange code（一次性交换码） | 附录A.6 P0-3 新增 |
| user_accounts | src/user/db.py | 旧用户账号（含 OAuth 平台字段） | 迁移后死表，@deprecated（附录A.6问题 1） |
| user_credentials | src/user/db.py | AK/SK 凭据 | 保留不变 |
| oauth_states | src/user/db.py | OAuth state 防 CSRF | 保留，复用 |
| admin_accounts | src/admin/db.py | 管理员独立账号体系 | 不改动 |

### 1.3 已实现可复用资产

- JWT 双 token 机制（access 15min + refresh 7天滚动/30天绝对上限 + token_version 撤销 + kid 密钥轮换）
- 鉴权中间件 status 校验 + 内存缓存（TTL 5s）
- oauth_states 表（通用 provider 字段）
- bcrypt + SHA-256 预哈希 + 旧算法兼容
- 验证码系统（6 场景 + IP/邮箱双维度限流 + 常量时间比较）
- 审计日志（append-only + 哈希链）
- 邮件服务（SMTP/Resend/DEV 三模式）
- Cookie 机制（HttpOnly refresh cookie + CSRF X-Requested-With）
- 前端登录/注册/忘记密码独立页面（复用主站 .ln/.lbox + app.css/apple-theme.css）
- OAuth 动态按钮渲染 + 品牌色 + Provider API
- Provider 缓存（附录A.6 P2-2 升级：TTL 5 分钟兜底 + invalidate_provider_cache 主动失效，admin save_config 后热生效；集群多实例最多 5 分钟延迟）

### 1.4 现有设计缺陷（需修复）

| # | 缺陷 | 影响 | 对应需求 |
|---|------|------|---------|
| D1 | auth.py 和 grape_auth.py 路由冲突（POST /login + 4 个 OAuth 路由） | 已修复：5 个冲突路由全部删除 | 需求 2/4 |
| D2 | 旧 OAuth 回调不查 external_identities 表 | 随 D1 一并解决（旧路由删除） | 需求 4/6/18 |
| D3 | refresh_token 续期不校验账号禁用状态 | 已修复：get_user_status_cached + status 校验 | 需求 20 |
| D4 | 注册接口 IP 限流阈值 10 与需求要求的 5 不一致 | 已修复：MAX_IP_PER_WINDOW 改为 5 | 需求 19 |
| D5 | 旧 OAuth 回调不创建个人组织 | 随 D1 一并解决（统一路由已实现） | 需求 18 |
| D6 | 管理员用户管理界面缺失 | 已实施：admin/db.py 查询 + admin/routes.py 7 个路由 + admin.html 前端 | 需求 21 |
| D7 | 冷静期定时任务缺失 | 已实施：cooldown.py 分步匿名化 + 幂等保护 + main.py 注册 | 需求 22 |
| D8 | 并发注册竞态返回 500 而非业务提示 | 已修复：email + username 冲突分支 | 需求 23 |
| D9 | 未绑定平台用户进入时报 400 空白页 | 已修复：showApp 串行 + bindings 检查 + 绑定引导卡片 | 需求 17 |
| D10 | 第三方凭据明文存储 | 已实施：credential_crypto.py AES-256-GCM + grape_account_db.py 集成 | 需求 5/6 |
| D11 | grape_accounts DDL 缺 last_login_at 列 | 已修复：DDL + _ensure_column + 登录写入 | 需求 21 |
| D12 | MySQL 部分唯一索引不完整 + 空 email 冲突 | 已修复：统一用 NULL + 普通唯一索引 | 需求 14/22 |
| D13 | _set_refresh_cookie 等辅助函数在 auth.py 和 grape_auth.py 重复定义 | 已实施：提取 src/auth/cookie_utils.py 统一管理（含 _app_base + admin/routes.py 复用 _cookie_secure） | 技术债 |
| D14 | soft_delete_account 不更新 updated_at，冷静期计时失效 | 已修复：UPDATE 增加 updated_at=datetime('now') | 需求 14/22 |
| D15 | change_password 不检查 update_password 返回值，迁移期旧表用户密码静默失败 | 已修复：检查返回值，False 时 fallback 旧表 user_db.update_password | 需求 11 |
| D16 | oauth_register 缺 D8 冲突检查，并发注册返回 500 而非 409 | 已修复：补充 is_email_registered + is_username_taken 检查 | 需求 23 |
| D17 | login_failures 表无定期清理，长期运行后无限增长 | 已修复：cooldown.py 新增 _cleanup_login_failures()，每天清理 24h 前记录 | 运维 |
| D18 | 注册流程非原子性——create_org / create_external_identity 失败不检查，产生半注册孤儿账号 | 已修复：register_verify + oauth_register 检查返回值 + WARNING + 失败时 soft_delete 回滚 | 需求 1/4 |
| D19 | signup_policy 读 "true" vs admin 写 "1" 值不匹配，开放注册开关失效 | 已修复：val in ("true", "1", "yes") 兼容多格式 | 需求 7/21 |
| D20 | audit EVENT_TYPES 缺 "account_anonymized"，冷静期匿名化审计日志静默失败 | 已修复：EVENT_TYPES 新增事件类型 | 需求 16/22 |
| D21 | get_grape_account_by_id 等 SELECT 缺 last_login_at 列 | 已修复：3 个 SELECT 统一补列 + _row_to_account 适配 | 需求 21 |
| D22 | 生产环境未校验 GRAPE_CREDENTIAL_KEYS，凭据加密密钥可能降级为开发默认值 | 已修复：_startup_security_check 增加密钥校验 | 需求 5/6/9 |
| D23 | 固定验证码使用时无审计日志 | 已修复：verify_code 中使用固定验证码时记录 dev_verification_code_used 审计日志 | 需求 8/16 |
| D24 | 审计日志无保留期限管理，表无限增长 | 已修复：cooldown.py 新增 _cleanup_audit_logs，每天清理 180 天前记录 | 需求 16 |
| D25 | 账号注销缺少管理员权限转移检查 | 已修复：delete_account 前检查 admin_accounts，管理员需先转移权限 | 需求 14 |
| D26 | OAuth 回调地址按平台分路径（/api/auth/login/{provider}/callback），每个平台需要配不同的回调地址，新增平台需额外配置 | 已修复（D31）：统一回调地址 /api/auth/oauth/callback，所有平台共用，回调后从 state 恢复 provider 和业务上下文（登录/绑定） | 需求 4/5 |
| D27 | meta 表残留旧 OAuth redirect_uri 配置 | 已修复：删除 meta 表 oauth_gitcode_redirect_uri 旧值，redirect_uri 由代码从 Host header 自动构造，不存 meta |
| D28 | 绑定回调重定向 URL hash/query 顺序错误 | 已修复：/#account?bind_success=xx → /?bind_success=xx#account，query 在 hash 前面浏览器才能通过 window.location.search 取到 | 需求 5/25 |
| D29 | OAuth 配置与 Bot 配置职责混淆——global-bot-token.json 同时承载 Bot 运行时配置和账号体系 OAuth 配置，故障域耦合 | 已修复：OAuth 配置分离到独立 configs/oauth-providers.json + migrate 统一入口 + admin 配置分流。旧文件不调整向前兼容。详见 oauth-provider-plugin-design.md v5 | 需求 6 |
| D30 | 业务代码读 OAuth token 只查 user_accounts 老表（get_user_token/get_user_github_token/get_user_token_with_refresh），登录走 grape_accounts 新表但 token 读取走老表，两套表 username 不一致导致 token 读不到，/api/repos/member 返回 400 | 已修复：deps.py 新增 _get_oauth_token_from_identity(username, provider)——从 grape_accounts 查 user_id → external_identities 查 OAuth access_token（解密）。get_user_token/get_user_github_token/get_user_token_with_refresh 优先读 external_identities 新表，不 fallback 老表（D32 已删除老表 fallback）。所有调用方（repos.py/scenes.py/chat.py/settings.py/issues.py/issues_center.py/devstage.py）自动生效 | 需求 6 |
| D31 | OAuth 绑定回调和登录回调分叉为两个路由路径（/bind/{provider}/callback + /login/{provider}/callback），每新增平台需在平台侧配多个回调地址 | 已修复：统一回调地址 /api/auth/oauth/callback，所有平台共用，回调后从 state 恢复 provider 和业务上下文（登录/绑定）。validate_oauth_state 改为 provider 可选参数，从 DB 记录恢复 provider | 需求 4/5 |
| D32 | grape_accounts 和 user_accounts 各自使用 MySQL 独立 AUTO_INCREMENT 主键，id 值范围重叠。业务代码用 user["id"] / user["username"] 查 user_accounts 老表，查到错误用户或查不到 | 已修复：迁移脚本 + 业务代码只查 grape_accounts 不 fallback + 删除 _sync_legacy_binding + 凭据改用 user_credentials 表。深度扫描 19 处遗漏全部修复。93 单元测试通过 + 34 E2E 测试通过。迁移脚本已执行验证，幂等。详见第 10.1.1 节 | 需求 6 |
| D33 | 迁移后个人组织缺失 + managed_repos.org_id 指向旧组织 → 登录后"不在组织下" + 仓库列表空。根因：① D32 迁移后 grape_accounts.id 复用了旧表 id，个人组织 slug=personal-{id} 冲突，创建失败但无补建逻辑；② managed_repos.org_id 指向旧组织 id，新个人组织 id 不同，/repos/managed 按 org_id 过滤返回空 | 已修复：① 注册/登录/OAuth 登录三处增加个人组织补建逻辑（幂等，slug 冲突时加 username 后缀）；② /repos/managed 个人组织时不按 org_id 过滤（与 /repos/member 行为对齐）。验证：yangaiwu 账号登录后 /repos/managed 返回 1 个仓库 | 需求 6 |
| D34 | admin 管理后台用户列表/注册白名单页面样式错乱。根因：① #usersView 和 #whitelistView 放在 .mn 容器外面，作为 #mainView flex 子元素被挤到侧边栏旁剩余空间（宽度仅 380px）；② .ipt class 样式为登录页深色背景设计（半透明背景+白色文字），在浅色背景的用户列表/白名单页输入框不可见；③ .tbl 表格 class 无 CSS 定义 | 已修复：① 将 #usersView 和 #whitelistView 移入 .mn 容器内（宽度填满 1060px 主区域）；② 新增 #usersView/.whitelistView 内 .ipt 浅色背景覆盖样式；③ 新增 .tbl 表格样式（th/td/hover）+ .tag-s/.tag-d/.tag-w 状态标签 + .f-row select 下拉框样式。Playwright DOM 验证全部通过 | — |
| D35 | OAuth 未绑定用户登录时跳转到注册/关联选择页，与业界通用做法不一致（GitHub/GitLab/Vercel 首次 OAuth 直接自动创建账号）。根因：需求 4 原设计为"跳关联页让用户手动选注册或关联"，体验差且多一步中间页面。同时 `_oauth_associate_redirect` 的 state 没存 email，前端拿不到 OAuth 邮箱信息 | 已修复：① OAuth 回调未绑定分支改为自动创建（`create_grape_account_oauth` + `create_external_identity` + 签发 JWT + 直接登录）；② 新增 `_generate_username` 从 external_login 自动生成用户名（清理非法字符、冲突加后缀）；③ 邮箱冲突时回退到关联页（仅密码关联）；④ state 补充 email 字段；⑤ 删除注册策略校验（OAuth 不受邮箱注册策略限制）。test_auth_routes 适配 D35 | 需求 4/18 |
| D36 | 邮箱注册/修改密码/设置密码/忘记密码缺少确认密码字段。用户输错密码无法发现，直到登录失败 | 已修复：注册表单（register.html）、忘记密码（forgot.html）、设置密码（account.js）新增确认密码输入框 + JS 校验两次一致。修改密码已有确认密码字段（无需改）。后端 API 不接收该字段，纯前端校验。Playwright DOM 验证通过 | 需求 1/3/11/12 |

---

## 2. 数据模型设计

### 2.1 grape_accounts — 新增 last_login_at 列（D11，已实施）

```sql
CREATE TABLE IF NOT EXISTS grape_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        VARCHAR(64) NOT NULL UNIQUE,
    email           VARCHAR(255) DEFAULT NULL,   -- NULL 表示无 email，不参与唯一约束（D12）
    password_hash   TEXT NOT NULL DEFAULT '',
    nickname        VARCHAR(128) DEFAULT '',
    avatar_url      VARCHAR(512) DEFAULT '',
    status          INTEGER DEFAULT 1,         -- 1=正常 0=禁用 2=已注销 3=冷静期
    token_version   INTEGER DEFAULT 0,
    email_verified  INTEGER DEFAULT 0,         -- 附录A.6P0-4：0=未验证 1=已验证（邮箱验证码注册=1，OAuth注册按第三方返回值）
    last_active_at  TEXT DEFAULT NULL,
    last_login_at   TEXT DEFAULT NULL,         -- 新增：最近登录时间（需求 21 用户列表展示）
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
```

**变更说明：**
- `last_login_at`：新增列，需求 21 管理员用户列表展示最近登录时间。已有表通过 `_ensure_column` 幂等补列。
- `email`：`DEFAULT NULL`（D12 修复，统一用 NULL 表示无 email，不参与唯一约束）。新表 DDL 直接定义为 DEFAULT NULL，不存在 '' 数据问题。附录A.6补充：MySQL ALTER MODIFY 修正线上 Default 为 NULL + 幂等迁移。
- `email_verified`：附录A.6 P0-4 新增列。邮箱验证码注册的账号 email_verified=1（邮箱已通过验证码验证），OAuth 注册按第三方返回的 email_verified 值。已有表通过 `_ensure_column` 幂等补列。

**当前状态：** DDL（db_setup.py `_GRAPE_ACCOUNTS_DDL`）已包含 `last_login_at` 列且 email 为 `DEFAULT NULL`，`_ensure_column` 补列逻辑和登录写入均已实现。D11/D12 修复已完成。

**status 状态机：**

```
  1 (正常) ──管理员禁用──→ 0 (禁用)
  0 (禁用) ──管理员恢复──→ 1 (正常)
  1 (正常) ──用户注销──→ 3 (冷静期)
  3 (冷静期) ──管理员恢复──→ 1 (正常)
  3 (冷静期) ──30天到期──→ 2 (已注销/匿名化) [终态，不可恢复]
```

不存在 -1（待激活）：注册流程在验证码通过后直接创建 status=1 的账号，无中间态。status=2 为终态，匿名化后数据已删除，不可恢复。

**phone 字段设计决策：** v1 设计文档中 grape_accounts 包含 `phone VARCHAR(32)` 字段。v2 移除此字段——23 条需求无短信验证场景，本期不引入。未来如需手机号注册/登录，通过 `_ensure_column` 幂等补列（与 D11 last_login_at 同模式），不影响已有数据。

### 2.2 email 唯一性约束方案（D12）

**设计背景：** 如果用空字符串 `''` 表示"无 email"，SQLite 需要条件索引 `WHERE status = 1 AND email != ''` 保证唯一性，MySQL 不支持条件索引则会导致：
1. 空 email 冲突：多个 OAuth 用户 email 为空字符串 `''`，插入失败
2. 已注销用户邮箱不释放：冷静期结束后 email 被清空为 `''`，多个 `''` 冲突

grape_accounts 新表直接采用 NULL 方案，避免上述问题：

**方案：统一用 NULL 表示无 email，靠 SQL 标准行为保证唯一性**

SQL 标准中 NULL 不参与唯一约束（多行 NULL 不冲突），SQLite 和 MySQL 行为一致。grape_accounts 新表直接使用 `DEFAULT NULL` 存储"无 email"，无需任何数据库特定语法（条件索引/函数索引/生成列/版本检测）。

**DDL 变更：**

grape_accounts 表 email 列为 `DEFAULT NULL`（新表 DDL 直接定义，不存在 '' 到 NULL 的迁移问题）：

```sql
CREATE TABLE IF NOT EXISTS grape_accounts (
    ...
    email           VARCHAR(255) DEFAULT NULL,   -- NULL 表示无 email，不参与唯一约束
    ...
);
```

唯一索引统一为普通索引，不带任何条件：

```sql
-- SQLite + MySQL 统一语法
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_email ON grape_accounts(email);
```

多行 NULL 不冲突（SQL 标准），非 NULL email 参与唯一约束。已注销用户 email 清空为 NULL 后不占唯一索引，邮箱释放可重新注册。

**注意：** grape_accounts 新表不使用 SQLite 条件索引，统一用 NULL + 普通唯一索引。

**代码变更：**

| 位置 | 实现 |
|------|------|
| `create_grape_account()` | `email = email or None`（NULL 存储） |
| `create_grape_account_oauth()` | `email = email or None`（NULL 存储） |
| `get_grape_account_by_email()` | `WHERE email=? AND status=1`（NULL 查不到，行为正确） |
| `is_email_registered()` | `WHERE email=? AND status=1`（NULL 查不到，行为正确） |
| `update_email()` | `new_email = new_email or None`（NULL 存储） |
| `soft_delete_account()` | status=3 不改 email（冷静期保留 email） |
| 冷静期匿名化（D7） | email 清空为 `NULL` |
| `_row_to_account()` | `email: row[2] or ""`（NULL 转空字符串给上层兼容） |
| `get_grape_account_by_id/username()` | SELECT email（NULL 原样返回） |

**db_setup.py 修改：**

1. DDL 中 `email VARCHAR(255) DEFAULT NULL`（新表直接定义，无需从 '' 改为 NULL）
2. email 唯一索引：SQLite + MySQL 统一为普通唯一索引 `ON grape_accounts(email)`，不带任何条件（新表从未用过条件索引）
3. 两个分支统一为同一条 SQL，消除 `_is_mysql` 分支判断

**数据迁移：** 新表 DDL 直接定义 `DEFAULT NULL`，不存在 `''` 数据需要迁移。迁移脚本 `scripts/migrate_grape_accounts.py` 从旧表 user_accounts 回填数据时直接写入 NULL（无 email 的用户）。

**向后兼容：** 上层代码收到的 email 始终是空字符串或非空字符串（`_row_to_account` 做 `row[2] or ""` 转换），不感知 NULL 变化。

### 2.3 external_identities — 不变

```sql
CREATE TABLE IF NOT EXISTS external_identities (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    grape_user_id     INTEGER NOT NULL,
    provider          VARCHAR(32) NOT NULL,
    external_id       VARCHAR(128) NOT NULL,
    external_login    VARCHAR(128) DEFAULT '',
    external_name     VARCHAR(128) DEFAULT '',
    external_avatar   VARCHAR(512) DEFAULT '',
    access_token      TEXT DEFAULT '',    -- AES-256-GCM 加密存储，读写时自动加解密（credential_crypto.py）
    refresh_token     TEXT DEFAULT '',    -- AES-256-GCM 加密存储
    token_expires_at  TEXT DEFAULT '',
    metadata          TEXT DEFAULT '{}',
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (provider, external_id),
    UNIQUE (grape_user_id, provider)
);
```

### 2.4 其他表

verification_codes、audit_logs、signup_whitelist、login_failures 表结构不变。

### 2.4a exchange_codes — 新增（附录A.6 P0-3）

```sql
CREATE TABLE IF NOT EXISTS exchange_codes (
    code         TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    username     VARCHAR(64) NOT NULL,
    provider     VARCHAR(32) NOT NULL,
    token_version INTEGER DEFAULT 0,
    is_new       INTEGER DEFAULT 0,      -- 1=首次登录（前端显示绑定引导）
    created_at   REAL NOT NULL,
    expires      REAL NOT NULL            -- created_at + 10 秒
);
```

**用途：** OAuth 回调成功后生成一次性 exchange code，替代 URL 直接传递 access_token。前端 POST /auth/exchange 用 code 换 JWT。10 秒过期，一次性消费（查+删）。懒清理：save 时顺带删除过期记录。

**存储位置：** 复用 session_manager.db 实例（与 oauth_states 同库），加 db_lock 保证并发安全。不引入 Redis。

### 2.4b oauth_states — 保留不变（DDL 位置迁移）

oauth_states 表结构不变。DDL 原定义在 user/db.py，附录A.6 P2-1 补充懒清理机制（save_oauth_state 1/10 概率清理过期 state）。

### 2.5 老表处理

**根因分析（D32）：**

grape_accounts（新表）和 user_accounts（老表）各自使用 MySQL 独立的 AUTO_INCREMENT 主键。两表独立自增，但 id 值范围重叠——grape_accounts 从 1 自增到 31，user_accounts 从 1 自增到 227。同一个 id=3 在 grape_accounts 是 yangaiwu（邮箱注册创建），在 user_accounts 是 gcw_3FZApeLT（OAuth 登录自动创建）——两个完全不同的用户。

业务代码用 `user["id"]`（来自 grape_accounts JWT）去查 user_accounts 老表，查到的是另一个用户的数据。用 `user["username"]`（来自 grape_accounts）去查 user_accounts，username 不一致直接查不到。

这不是某一处代码写错了，是架构层面的设计缺陷：**新建了 grape_accounts 独立账号表，但没有把业务代码的读写从 user_accounts 切换到 grape_accounts。** 登录走新表，业务读写走老表，两套表 id 和 username 都不对齐。

**修复策略：一次性迁移 + 不 fallback**

不采用 fallback 模式——fallback 导致每个读取点都要写"先查新表→查不到→查老表"的分支逻辑，39 个调用点写 39 次 fallback，代码复杂、容易遗漏、容易写错。而且 fallback 存在意味着老表永远删不掉，系统永远不干净。

改为：
1. **写迁移脚本**——把 user_accounts 里的数据一次性迁移到 grape_accounts + external_identities（幂等，可重复执行，支持 dry-run）
2. **业务代码只查新表**——所有业务代码只查 grape_accounts + external_identities，不查 user_accounts，不写 fallback 分支
3. **迁移完成后 user_accounts 变成死表**——不再被任何业务代码读取，可安全退役

**架构原则（防止再犯）：**

1. **单一权威源原则**：用户身份的唯一权威源是 grape_accounts。所有业务代码读写用户数据只查 grape_accounts + external_identities，不查 user_accounts。
2. **id 不跨表传递原则**：grape_accounts.id 不等于 user_accounts.id。业务代码不得用 grape_accounts 的 id 直接查 user_accounts。
3. **不 fallback 原则**：业务代码不写 fallback 到老表的分支。老表数据通过迁移脚本一次性导入新表，迁移后老表是死表。
4. **迁移脚本幂等原则**：迁移脚本可重复执行，已迁移的跳过。支持 dry-run 预览。迁移前备份 DB。
5. **同名函数归一原则（D32-补丁新增）**：user/db.py 和 deps.py 不得各自维护同名函数（get_user_github_token 等）读不同表。D32 第一轮只改了 deps.py 的调用方，遗漏了 user/db.py 内部同名函数仍读老表。修复后 user/db.py 的 token/凭据读取函数统一委托给 deps.py 或直接从 external_identities/user_credentials 读，消除两套实现。

| 表 | 处理方式 |
|---|---|
| user_accounts | 一次性迁移数据到 grape_accounts + external_identities，迁移后变为死表，不再被业务代码读取 |
| unified_user_mapping | 数据已迁入 external_identities，保持不动 |
| user_credentials | 迁移脚本同步 OAuth token 到 external_identities，迁移后 user_credentials 仍保留（非 OAuth 凭据如 AK/SK 仍在用） |
| oauth_states | 保留不变，复用 |
| admin_accounts | 不改动 |

### 2.6 ER 关系

```
grape_accounts (1) ──< external_identities (N)
grape_accounts (1) ──< user_credentials (N)
grape_accounts (1) ──< organization.owner_id (1)
grape_accounts (1) ──< org_member.user_id (N)
grape_accounts (1) ──< audit_logs.user_id (N)
verification_codes（独立，按 email + scene 查询）
login_failures（独立，按 identifier + ip 查询）
```

---

## 3. 模块设计

### 3.1 新增/改造模块

```
src/auth/
├── __init__.py
├── providers.py          # 第三方平台 Provider 注册表 + 统一 OAuth 流程 + _DEFAULT_PROVIDERS 内置默认 + _PROVIDERS_CACHE 从 DB 加载 + 配置驱动消除 if-else
├── grape_account_db.py   # grape_accounts + external_identities CRUD + last_login_at 更新
├── verification.py       # 验证码生成/发送/校验 + IP 维度限流（需求 19）
├── email_service.py      # 邮件发送服务（SMTP/Resend/DEV）
├── password.py           # 密码哈希（bcrypt + SHA-256 预哈希 + 旧算法兼容）
├── audit.py              # 审计日志（append-only + 哈希链）
├── signup_policy.py      # 注册策略判定 + 白名单管理 API（需求 21）
├── login_throttle.py     # 登录失败限流（账号 + IP 维度）
├── credential_crypto.py  # 新增：第三方凭据加密/解密（AES-256-GCM + 密钥轮换）
└── cookie_utils.py       # 新增：统一 cookie 常量 + _set_refresh_cookie + _clear_refresh_cookie（D13）

src/api/
├── routes/
│   ├── grape_auth.py     # 统一账号路由（注册/登录/OAuth/绑定/引导）+ 旧路由清理
│   ├── auth.py           # 精简：保留 refresh/logout/profile，移除重复 login + 旧 OAuth 回调
│   └── admin_users.py    # 新增：管理员用户管理路由（需求 21）
├── deps.py               # 鉴权中间件 + get_user_status_cached 公共函数 + refresh 续期 status 校验（需求 20）
├── jwt_utils.py          # JWT 签发/验签 + kid 密钥轮换
└── db_setup.py           # DDL + last_login_at 幂等补列 + email NULL 唯一索引统一（D12）

src/admin/
├── db.py                 # 新增 grape_accounts 查询函数 + 白名单 CRUD
└── cooldown.py           # 新增：冷静期定时任务（需求 22）

src/api/static/
├── index.html            # OAuth 按钮动态容器
├── register.html         # 注册页 + OAuth 关联模式
├── forgot.html           # 忘记密码页
├── css/app.css           # 移动端 .lbox 响应式
├── js/
│   ├── app.js            # showApp 串行加载 + 新手引导页 + 平台切换栏 + nav 未绑定引导
│   ├── auth.js           # 认证页面共享逻辑（showToast/togglePwd/密码强度/blur验证/i18n委托/倒计时）
│   ├── account.js        # 用户中心逻辑（资料/安全/凭据/外观/API Keys/危险区域，内嵌 SPA view）
│   └── pages/settings.js # 仓库设置（Bot Token + 解除托管，原通用设置已移除）
```

### 3.2 关键设计决策

**D1 修复 — auth.py 与 grape_auth.py 路由冲突全面清理：**

冲突范围比 v1 描述更大，不仅是 `POST /login`：

| 冲突路由 | auth.py | grape_auth.py | 谁生效 |
|----------|---------|--------------|--------|
| POST /api/auth/login | password_login (PasswordLogin) | login (LoginBody) | grape_auth（先 include） |
| GET /api/auth/login/gitcode | gitcode_oauth_login（具体路由） | oauth_login（/login/{provider} 参数化路由） | grape_auth（参数化路由拦截） |
| GET /api/auth/login/github | github_oauth_login | oauth_login | grape_auth |
| GET /api/auth/callback/gitcode | gitcode_oauth_callback | oauth_login_callback（/login/{provider}/callback） | grape_auth |
| GET /api/auth/callback/github | github_oauth_callback | oauth_login_callback | grape_auth |

grape_auth.router 先 include，其参数化路由 `/login/{provider}` 和 `/login/{provider}/callback` 拦截所有 `/login/xxx` 请求，导致 auth.py 的具体路由**永远不会被命中**。

方案：

- auth.py 移除全部 5 个冲突路由（POST /login + 2 个 /login/xxx + 2 个 /callback/xxx）
- auth.py 仅保留 `POST /refresh`、`POST /logout`、`GET /profile`（这些 grape_auth.py 没有重复）
- grape_auth.py 的 `LoginBody` 增加 `username: str = ""` 可选字段，兼容旧客户端发送 username 而非 identifier 的请求
- grape_auth.py 的统一 OAuth 路由完全替代 auth.py 旧路由（查 external_identities + 双写迁移 + 创建组织，逻辑已完整）
- 不再保留旧 OAuth 路由"兼容期"——它们已是死代码，保留只会造成混淆

**D2/D5 修复 — 随 D1 一并解决：**

D2（旧 OAuth 回调不查 external_identities）和 D5（旧 OAuth 回调不创建组织）在 v1 中描述为"修改 auth.py 旧路由"。但旧路由在 D1 修复后已被删除，这两个缺陷自动消解——grape_auth.py 的统一 OAuth 回调已实现：
1. 查 external_identities 表
2. 查旧表 fallback + 双写迁移
3. 创建 grape_accounts + external_identities + 个人组织

**D3 修复 — refresh_token 续期校验 status（已实施）：**

`POST /api/auth/refresh`（auth.py）复用 deps.py 的 `get_user_status_cached` 同时校验 token_version 和 status。

**关键设计：复用 deps.py 的 `_user_status_cache`，而非独立查库。**

deps.py 已有 `_user_status_cache: dict[int, tuple[int, int, float]]`（uid → token_version, status, expire_ts），TTL 5 秒。`get_current_user` 已实现缓存读写。refresh 路由已复用同一缓存，避免每次 refresh 都查库。

实现方案：

1. 在 deps.py 中提取公共函数：

```python
def get_user_status_cached(uid: int) -> tuple[int, int]:
    """获取用户 token_version + status，优先读缓存。"""
    cached = _user_status_cache.get(uid)
    now = time.time()
    if cached and cached[2] > now:
        return cached[0], cached[1]
    # 缓存未命中，查库
    from src.auth.grape_account_db import get_status_and_token_version
    info = get_status_and_token_version(uid)
    if info is not None:
        status, db_ver = info
    else:
        db_ver = user_db.get_token_version(uid)
        status = 1  # 旧表无 status
    _user_status_cache[uid] = (db_ver, status, now + 5)
    return db_ver, status
```

2. `get_current_user` 已调用 `get_user_status_cached(uid)` 替代内联缓存逻辑。

3. auth.py 的 refresh 路由中，已将 `db_ver = user_db.get_token_version(uid)` 替换为：

```python
from src.api.deps import get_user_status_cached
db_ver, status = get_user_status_cached(uid)
if token_ver != db_ver:
    raise HTTPException(401, "会话已失效")
if status != 1:
    from src.api.deps import invalidate_user_cache
    invalidate_user_cache(uid)
    raise HTTPException(401, "账号已被禁用")
```

**注意：** status 校验失败时需 `invalidate_user_cache(uid)`，确保后续请求立即查库而非读旧缓存。

**D4 修复 — 注册接口 IP 维度限流：**

在 `register_send_code` 中调用 `verification.send_code()` 前增加 IP 维度检查。复用 `verification.py` 中已有的 IP 限流计数（`verification_codes.ip` 列），注册验证码和密码重置验证码共享 IP 维度计数。

IP 限流阈值统一为 **5 次/10 分钟**（需求 19 明确要求）。`verification.py` 中 `MAX_IP_PER_WINDOW` 已修正为 5。

**D5 修复 — 随 D1 一并解决：**

旧 OAuth 回调不创建个人组织的问题在 D1 修复后自动消解——grape_auth.py 的统一 OAuth 回调已包含 `create_org()` 调用。

**D6 修复 — 管理员用户管理：**

新增 `src/api/routes/admin_users.py` 中的用户管理路由（挂载到 admin_router）：
- `GET /api/admin/users` — 分页列表 + 搜索
- `POST /api/admin/users/{id}/disable` — 禁用用户
- `POST /api/admin/users/{id}/restore` — 恢复用户
- `GET /api/admin/signup-whitelist` — 白名单列表
- `POST /api/admin/signup-whitelist` — 添加白名单
- `DELETE /api/admin/signup-whitelist/{id}` — 删除白名单
- `PUT /api/admin/allow-signup` — 开放注册开关

查询函数放在 `src/admin/db.py`（grape_accounts 查询 + 白名单 CRUD），定时任务放在 `src/admin/cooldown.py`。

前端 admin.html 新增"用户管理"模块。

**D7 修复 — 冷静期定时任务（已实施）：**

新增 `src/admin/cooldown.py`，在应用启动时注册定时任务（复用现有 scheduler 机制）：
- 每天执行一次
- 查询 `status=3 AND updated_at < datetime('now', '-30 days')` 的账号
- 对每个到期账号分步执行匿名化（幂等设计）

**分步执行 + 幂等保护：**

```
对每个到期账号（status=3 且超 30 天）：
  Step 1: 改 status=2（标记为已处理，防止重试时重复处理）
  Step 2: 清除 external_identities 绑定关系
  Step 3: 删除个人工作空间
  Step 4: 匿名化 username 改为 deleted_{id}，email 设为 NULL（D12），password_hash/nickname/avatar_url 清空
  Step 5: 记录审计日志
```

**幂等保证：** Step 1 先改 status=2，即使后续步骤中断，重启后重新查询 `status=3` 不会再命中该账号。Step 2-4 对 status=2 的账号执行是安全的（删除已删除的绑定是 no-op，清空已空的字段是 no-op）。

**失败处理：** 任一步骤失败记 WARNING 日志并跳过该账号，继续处理下一个。管理员可手动处理失败账号。不因单个账号失败阻断整个定时任务。

**MySQL 时间函数兼容：** `datetime('now', '-30 days')` 是 SQLite 语法，MySQL 需用 `DATE_SUB(NOW(), INTERVAL 30 DAY)`。通过 `src/mysql_compat.py` 的 `get_db_backend()` 判断后端，选择对应 SQL。

**D8 修复 — 并发注册竞态（部分实施，缺 username 冲突分支）：**

`create_grape_account()` 捕获数据库唯一约束冲突，返回 None。调用方检查返回值：

```python
account = create_grape_account(username, email, password)
if not account:
    if is_email_registered(email):
        raise HTTPException(409, "该邮箱已注册，请直接登录或重置密码")
    if is_username_taken(username):
        raise HTTPException(409, "用户名已被占用")
    raise HTTPException(500, "创建账号失败")
```

**当前状态：** D8 已修复——register_verify 和 oauth_register 均已实现 email + username 冲突分支检查，并发注册返回 409 业务提示。

**注意：** `create_grape_account` 在正常流程中已先校验 username 唯一（register_send_code 阶段），并发冲突是极端场景。但仍需正确处理，因为两个请求可能同时通过 send-code 阶段的 username 校验，在 verify 阶段同时插入。

**D9 修复 — 未绑定平台用户引导（已实施，需求 17 重写后需重构）：**

已实施部分：
- `showApp()` 改为串行：先 `loadProfile()` 获取 bindings，再决定加载路径
- `/auth/profile` 返回 `bindings` 列表
- `nav()` 无仓库守卫

需求 17 重写后需重构部分（Phase 4 待实施）：
- 未绑定时从"绑定引导卡片嵌入项目列表"改为"新手引导页替代三 tab"
- 已绑定时从"混合仓库列表"改为"按平台切换栏，独立展示"
- nav 守卫从"loadRepos 回项目页"改为"renderOnboarding 回新手引导页"
- 详见第 4.5 节和第 6.4 节

**去硬编码设计（扩展性）：**

`/auth/profile` 已改为返回 `bindings: ["gitcode"]` 列表（已绑定的 provider 数组），同时保留 `gitcode_bound` / `github_bound` 旧字段做向后兼容。前端引导逻辑改为检查 `bindings.length === 0`，不再检查具体平台字段。

`/auth/profile` 返回值变更：
```json
// 旧
{ "gitcode_bound": true, "github_bound": false, ... }
// 新（向后兼容期间同时返回）
{ "bindings": ["gitcode"], "gitcode_bound": true, "github_bound": false, ... }
```

前端先切换到 `bindings` 列表，旧字段保留兼容期，后续废弃。

**providers.py redirect_uri 构造清理（D27 + D26 待修复）：**

`get_redirect_uri()` 不再从 meta 表读取 redirect_uri，由代码从请求 Host header 自动构造统一回调路径，避免配置残留过期路径（D27）。

D26 进一步统一：所有平台共用同一个回调地址 `/api/auth/oauth/callback`，不再按 provider 分路径。redirect_uri 不含 provider——provider 从 state 恢复。

```python
def get_redirect_uri(request) -> str:
    """获取 OAuth 回调 URI。所有平台统一地址，不含 provider（D26）。"""
    env_uri = os.environ.get("OAUTH_REDIRECT_URI")
    if env_uri:
        return env_uri
    scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else "http"
    host = request.headers.get("Host", "localhost:80")
    return f"{scheme}://{host}/api/auth/oauth/callback"
```

绑定流程（bind_provider）使用和登录相同的回调地址 `/api/auth/oauth/callback`，通过 state 携带的 `binding_user_id` 区分绑定和登录上下文（D26 修复）。回调处理统一在 `oauth_callback` 中，从 state 恢复 provider + 检查 `binding_user_id` 走绑定逻辑、`login_flow` 走登录逻辑。

migrate_accounts_from_json 不再迁移 redirect_uri 到 meta 表（D27 根治）。
global-bot-token.json 已删除 `gitcode_oauth_redirect_uri` 字段。

**D29 — OAuth 配置与 Bot 配置职责分离（已修复，详见 oauth-provider-plugin-design.md v5）：**

OAuth 平台配置（协议参数 + 凭据）从 global-bot-token.json 分离到独立的 configs/oauth-providers.json。migrate_oauth_providers 作为统一入口，替代 user/db.py migrate_accounts_from_json OAuth 部分和 db_setup.py _migrate_tables 3096-3145 OAuth 部分——优先读 oauth-providers.json，fallback 读 global-bot-token.json 旧字段（向前兼容）。db_setup.py 删除 redirect_uri 迁移（D27 已废弃），保留 default 账户清理。global-bot-token.json 删除 oauth_providers 字段，旧 client_id/secret 保留。admin/config_store.py save_config 分流：bot 字段写旧文件 + 扁平→嵌套映射写新文件。providers.py 加 _loaded 标志位消除 lazy loading 竞态。

**D30 — 业务代码读 OAuth token 走老表（已修复）：**

`get_user_token` / `get_user_github_token` / `get_user_token_with_refresh` 只读 user_accounts 老表的 gitcode_api_token / github_api_token 字段。但用户通过 grape_accounts 新表登录（username=yangaiwu），user_accounts 老表里同一 id 的 username 是 gcw_3FZApeLT（OAuth 自动生成），两表 username 不一致导致 `get_user_account("yangaiwu")` 查不到 → token 读不到 → /api/repos/member 返回 400 "API Token 未配置"。

修复：deps.py 新增 `_get_oauth_token_from_identity(username, provider)` 函数——从 grape_accounts 查 user_id，再到 external_identities 查 OAuth access_token（解密）。`get_user_token` / `get_user_github_token` / `get_user_token_with_refresh` 优先读 external_identities 新表，不 fallback 老表（D32 已删除老表 fallback）。所有调用方（repos.py / scenes.py / chat.py / settings.py / issues.py / issues_center.py / devstage.py）通过这两个函数获取 token，改了入口全部自动生效。

**D31 — OAuth 统一回调地址（已修复）：**

删除独立的 bind_callback 路由，oauth_login_callback 改为统一 oauth_callback（GET /api/auth/oauth/callback）。回调进来后从 state 恢复 provider（validate_oauth_state provider 参数改为可选，从 DB 记录取 provider 返回 `_provider` 字段），检查 state 里的 binding_user_id 分流：有 binding_user_id 走绑定流程，有 login_flow 走登录流程。providers.py get_redirect_uri 去掉 provider 参数，返回固定 /api/auth/oauth/callback。所有平台共用一个回调地址，新增平台不需要在平台侧配新地址。

**D32 — 新表老表 id 碰撞 + username 不一致（已修复，详见第 10.1.1 节）：**

根因：grape_accounts（新表）和 user_accounts（老表）各自使用 MySQL 独立的 AUTO_INCREMENT 主键。两表独立自增，但 id 值范围重叠——grape_accounts 从 1 到 31，user_accounts 从 1 到 227。同一个 id=3 在 grape_accounts 是 yangaiwu（邮箱注册），在 user_accounts 是 gcw_3FZApeLT（OAuth 自动生成）——两个完全不同的用户。业务代码用 grape_accounts 的 id/username 查 user_accounts 老表，查到错误用户或查不到。

影响范围：14 处 get_user_account(username) + 19 处 get_managed_repos/get_user/get_credential(user_id) + 6 处 verify_user/update_password/increment_token_version。详见第 10.1.1 节完整影响范围表。

修复策略：一次性迁移 + 不 fallback（详见第 2.5 节架构原则）：

1. 迁移脚本（scripts/migrate_user_accounts_to_grape.py）把 user_accounts 数据导入 grape_accounts + external_identities + user_credentials（幂等，支持 dry-run）——已编写，dry-run 验证通过（90 个老用户待迁移），待执行 --execute
2. 业务代码只查 grape_accounts + external_identities，不写 fallback 分支——已实施（14 文件修改）
3. 删除 login/change_password/oauth_associate 的老表 fallback 分支——已删除（3 处）
4. 删除 _sync_legacy_binding 函数及 3 处调用——已删除
5. settings.py/devstage.py 凭据读写从 user_accounts 列改为 user_credentials 表——已实施
6. chat.py 5 处死代码 acc = get_user_account() 赋值后未使用——已删除
7. token 刷新后更新 external_identities 而非写 user_accounts 老表——已实施
8. 迁移完成后 user_accounts 变为死表

**深度扫描遗漏（D32-补丁，待实施）：**

第一轮修复覆盖了 14 个文件中的 get_user_account/get_user(username)/fallback 分支。但深度扫描发现 user/db.py 内部函数和其他文件的直接 SQL 仍有 17 处引用 user_accounts 老表，分三类：

A. user/db.py 内部函数仍查老表（P0，直接影响 Agent 执行和 IM 通信）：
- get_credentials(user_id) — SELECT ... FROM user_accounts，被 runtime/service.py:464（凭据注入）和 executor.py:278（凭据注入）调用
- get_user_gitcode_token(user_id) — 内部调 get_credentials，被 handler_async.py:1109（IM chat token）调用
- get_user_github_token(user_id) — 内部调 get_credentials，被 issues_center.py:1128/1250（Issue token）调用
- get_github_token(repo_key) — SELECT github_api_token FROM user_accounts WHERE username=owner，被 feishu_async.py:717 和 vcs/api_client.py:230 调用
- _get_username_by_id(user_id) — SELECT username FROM user_accounts WHERE id=?，用老表 id 查 username

B. 业务代码直接 SQL 查 user_accounts（P1，影响用户管理/引擎分配/UI 展示）：
- orgs.py:142 — 搜索用户 SELECT id, username, nickname FROM user_accounts（LIKE 模糊搜索）
- orgs.py:173 — get_user_by_username 查老表（添加组织成员）
- orgs.py:184 — SELECT id, username FROM user_accounts WHERE id=?（检查用户存在）
- runtime.py:152 — SELECT id FROM user_accounts WHERE username=?（runtime register owner_id 对齐）
- runtime/service.py:1347 — SELECT id FROM user_accounts WHERE username=?（任务 claim user scope）
- engine_select.py:175 — SELECT id FROM user_accounts WHERE username=? OR gitcode_id=?（引擎选择 user_id 解析）
- engine_select.py:184 — JOIN user_accounts（user_repos 回退）
- events.py:581,784 — SELECT username FROM user_accounts WHERE id=?（事件 scope 子查询）
- settings.py:399,759 — SELECT username FROM user_accounts（有效用户集合，过滤已删除用户引擎）
- session_manager.py:228-235 — INSERT INTO unified_user_mapping SELECT FROM user_accounts（backfill IM 映射）

C. config.py 账户加载（P1，影响 ACCOUNTS 字典）：
- config.py:208 — _load_accounts() 调 get_all_user_accounts() 从老表加载
- config.py:269 — save_accounts() 调 save_user_account/delete_user_account 写老表
- 调用方：api/main.py:795 和 run_webhook.py:14 启动时执行

修复方案详见第 10.1.1 节第四类。

**D13 修复 — 辅助函数提取（已实施）：**

auth.py 和 grape_auth.py 重复定义以下辅助函数和常量：

| 重复项 | auth.py | grape_auth.py | 差异 |
|--------|---------|--------------|------|
| `_COOKIE_NAME` / `_COOKIE_PATH` / `_COOKIE_MAX_AGE` | 模块级常量 | 模块级常量 | 无差异 |
| `_set_refresh_cookie()` | 完全相同 | 完全相同 | 无差异 |
| `_clear_refresh_cookie()` | 完全相同 | 完全相同 | 无差异 |
| `_app_base()` | 完全相同 | 完全相同 | 无差异 |
| `_oauth_error_redirect()` | 跳 `/login?oauth_error=` | 跳 `/?oauth_error=` | **重定向目标不同** |

方案：

1. 新增 `src/auth/cookie_utils.py`，提取 cookie 常量和函数：
   ```python
   # src/auth/cookie_utils.py
   COOKIE_NAME = "refresh_token"
   COOKIE_PATH = "/api/auth"
   COOKIE_MAX_AGE = 7 * 24 * 3600

   def set_refresh_cookie(response: Response, refresh_token: str): ...
   def clear_refresh_cookie(response: Response): ...
   def app_base() -> str: ...
   ```

2. auth.py 和 grape_auth.py 移除重复定义，改为 `from src.auth.cookie_utils import ...`

3. `_oauth_error_redirect` 统一为跳 `/?oauth_error=`（grape_auth.py 的版本，更符合 SPA 路由），auth.py 的 `/login?oauth_error=` 版本删除（旧路由已移除，此函数只在 auth.py 内部使用，删除后无影响）

4. D13 已实施：cookie_utils.py 已提取，auth.py 和 grape_auth.py 已改为导入

---

## 4. 核心流程设计

### 4.1 邮箱注册流程（需求 1/19/23）

```
POST /api/auth/register/send-code
  请求: {username, email, password}
  → 用户名校验（唯一 + 规则 + 保留字）
  → 邮箱格式 + 密码强度 + 注册策略
  → IP 维度限流检查（同一 IP 10 分钟最多 5 次验证码请求，含所有场景）  ← 新增
  → 邮箱已注册：直接返回 409 + "该邮箱已注册 Grape 账号，请直接登录或重置密码"
  → 未注册邮箱：生成验证码 → 存储（code_hash + scene=register + ip）→ 发邮件
  → 返回 {message: "验证码已发送"}

POST /api/auth/register/verify
  请求: {username, email, password, code}
  → 再次校验用户名唯一 + 密码强度
  → 校验验证码（constant-time 比较 + 原子标记 used）
  → 创建 grape_accounts（数据库 UNIQUE 兜底并发冲突）  ← D8 修复
  → 创建个人组织
  → 更新 last_login_at  ← 新增
  → 签发 JWT（login_provider="grape"）
  → 记录审计日志
  → 返回 {token, user}
```

### 4.2 统一密码登录流程（需求 2）

```
POST /api/auth/login（仅 grape_auth.py 注册，auth.py 移除重复路由）  ← D1 修复
  请求: {identifier, password}
  → 限流检查（账号 + IP 双维度）
  → 验证账号密码（只查 grape_accounts，D32 已删除旧表 fallback）
  → 密码正确但 status != 1 → 返回禁用/注销提示
  → 密码错误 → 记录失败 → 统一返回"账号或密码错误"
  → 登录成功 → 更新 last_login_at → 签发 JWT
  → 登录后去向：已有平台绑定 → 上次工作页面；无平台绑定 → 新手引导页（需求 17）
  → 返回 {token, user}
```

### 4.3 OAuth 登录流程（需求 4/18）

```
GET /api/auth/login/{provider}
  → providers.get_provider(provider) 读取配置
  → 生成 state（存 oauth_states，携带 provider + login_flow=True）
  → PKCE（如启用）
  → redirect_uri = /api/auth/oauth/callback（统一地址，不含 provider）
  → 返回 {redirect_url}

GET /api/auth/oauth/callback  ← 所有平台统一回调
  → 校验 state（按 state 查 oauth_states，恢复 provider）
  → code → token → 用户信息
  → 检查 state 上下文分流：
    → state 有 binding_user_id → 走绑定流程（见 4.3a）
    → state 有 login_flow → 走登录流程（下方）
  → 查 external_identities（provider + external_id）
  → 已绑定：查 grape_accounts → 禁用检查 → 更新绑定 token → 签发 JWT
  → 未绑定：OAuth 白名单校验 → 自动创建 Grape 账号 + 绑定 + 个人组织 → 签发 JWT（D35）
  → 未绑定且邮箱冲突：回退到关联页面，引导用户绑定已有账号（仅密码关联）

POST /api/auth/login/{provider}/associate
  → 用户输入已有 Grape 账号密码 → 验证通过 → 写入 external_identities → 签发 JWT

POST /api/auth/login/{provider}/register
  → 用户注册新 Grape 账号 → 创建 grape_accounts + external_identities + 个人组织 → 签发 JWT
```

### 4.3a OAuth 绑定流程（需求 5，统一回调分流）

```
GET /api/auth/bind/{provider}  ← 已登录用户发起绑定
  → 生成 state（存 oauth_states，携带 provider + binding_user_id=当前用户ID）
  → redirect_uri = /api/auth/oauth/callback（和登录相同）
  → 返回 {redirect_url}

GET /api/auth/oauth/callback  ← 统一回调
  → 校验 state（恢复 provider + binding_user_id）
  → code → token → 用户信息
  → 检查 state 有 binding_user_id → 走绑定流程：
    → 检查该第三方账号是否已被其他 Grape 账号绑定
    → 写入 external_identities
    → 302 重定向回用户中心 /?bind_success={provider}#account
```

**旧路由处理（D1/D2/D5 修复）：**

auth.py 的旧 OAuth 路由（/login/gitcode、/login/github、/callback/gitcode、/callback/github）已删除。grape_auth.py 的统一路由完全替代，已实现查 external_identities + 旧表 fallback + 双写迁移 + 创建组织全流程。如需兼容旧书签，可在 auth.py 中加 301 重定向到新路径（可选，非必须）。

### 4.4 Token 续期流程（需求 20，D3 修复）

```
POST /api/auth/refresh
  → CSRF 校验（X-Requested-With）
  → 验证 refresh_token 签名
  → get_user_status_cached(uid) → 校验 token_version + status  ← D3 修复，复用 deps.py 缓存
  → token_version 不匹配 → 拒绝续期，返回"会话已失效"
  → status != 1 → invalidate_user_cache(uid) → 拒绝续期，返回"账号已被禁用"
  → 通过 → 滚动续期 → 返回 {token}
```

**Token 绝对过期机制：**

- refresh_token 有滚动过期（7 天）和绝对过期（30 天）双重限制
- `create_refresh_token` 接受 `max_exp` 参数（Unix 时间戳），首次签发时设置为当前时间 + 30 天
- refresh 时读取旧 token 的 `max_exp` 并传递给新 token，保持绝对过期时间不变
- 超过 `max_exp` 后 refresh_token 验证失败，用户需重新登录
- 滚动续期保证活跃用户 7 天内无需重新登录，绝对过期保证 30 天后必须重新认证

### 4.5 未绑定平台用户引导流程（需求 17/24，重写）

**首页三种状态：**

| 状态 | 判定 | 显示 |
|------|------|------|
| 未绑定任何平台 | bindings 为空 | 新手引导页（欢迎语 + 平台绑定入口） |
| 已绑定无仓库 | bindings 非空 + 无托管仓库 | 项目页（空态引导"去该平台托管仓库"） |
| 已绑定有仓库 | bindings 非空 + 有托管仓库 | 项目页（仓库列表正常展示） |

**未绑定状态 — 新手引导页（renderOnboarding）：**

替代当前 lobby 的"项目/项目模板/工作台"三 tab，改为单屏绑定页面（详见 onboarding-experience-tech-design.md）：

```
showApp()
  → loadProfile() 获取 bindings 列表
  → bindings 为空 → renderOnboarding()
    → 隐藏 sidebar + tb
    → 单屏引导页：
      - 顶栏（Logo + 组织下拉 + 用户菜单）
      - 欢迎语 + 产品定位语
      - 平台绑定卡片（动态获取 /api/auth/providers，仅显示 configured=true 的平台）
      - 底部小提示
    → 不显示：项目列表、项目模板、工作台 tab、仓库搜索框、场景·技能市场、知识库
    → 不显示：工作流演示、Agent 架构、基础设施特性、底部 CTA、引流入口
    → 不显示：进度条、浮动按钮、滚动动画
```

**已绑定状态 — 项目页混合展示 + 平台筛选：**

```
showApp()
  → loadProfile() 获取 bindings 列表
  → bindings 非空 → 进入项目页
    → /repos/member 一次获取所有平台仓库（混合）
    → 顶部平台切换栏（"全部" + 各绑定平台），默认"全部"
    → 仓库卡片显示平台图标 + 平台名称
    → 切换到某平台 → 前端筛选只展示该平台仓库
    → 侧边栏仓库下拉框按平台分组（optgroup）
```

**后端适配 — /repos/member 支持按平台查询：**

当前 `_get_user_provider_and_token()` 自动选一个 token。改为支持前端传 `?provider=gitcode` 参数，按指定平台的 token 查询仓库。不传 provider 时保持现有 fallback 逻辑（兼容）。

**nav() 未绑定守卫：**

```
nav(view, param)
  → 未绑定 + 依赖仓库的功能（项目场景/流水线/自动化/Agent/知识库/场景·技能市场/Chat/IM）
    → 引导返回新手引导页
  → 未绑定 + 不依赖仓库的功能（Agent 运行时/设备监控/仓库设置/组织管理）
    → 正常进入
```

**返回引导提示条（需求 24）：**

未绑定用户进入不依赖仓库的功能后，页面顶部显示轻量提示条"还未绑定代码托管平台，点击此处完成绑定"，点击返回新手引导页。已绑定后提示条消失。

**多平台场景：**

- 绑定第二个平台 → 用户设置操作 → 项目页平台切换栏自动出现新平台 tab
- 解绑平台 → 解绑前检查该平台下有已托管仓库（已建立 webhook 的仓库），有则弹窗警告受影响的仓库列表
- 解绑后如果无任何绑定 → 首页回到新手引导页

### 4.6 OAuth 用户账号补全引导（需求 18）

```
OAuth 首次登录后
  → 创建 grape_accounts + external_identities + 个人组织
  → 签发 JWT → 跳转首页
  → 前端检查 has_password + email
    → 不完整 → 首页顶部轻量提示条"建议设置密码和绑定邮箱"
    → 用户跳过 → localStorage 记录，后续不再弹
  → 账号设置页始终展示完整度状态
```

**OAuth 邮箱处理：**

- 平台返回 `email_verified=true` → 自动填入 grape_accounts.email
- 平台返回未验证邮箱或无邮箱 → email 留空，引导补填

### 4.7 管理员用户管理流程（需求 21）

```
GET /api/admin/users?page=1&q=keyword
  → 分页查询 grape_accounts（排除 status=2 已注销）
  → 返回用户列表（id, username, email, status, has_password, identities, last_login_at）

POST /api/admin/users/{id}/disable  {reason: "..."}
  → 更新 status=0
  → 清除鉴权缓存
  → 记录审计日志

POST /api/admin/users/{id}/restore
  → 校验 status=0（禁用）或 status=3（冷静期）
  → 更新 status=1
  → token_version +1
  → 清除鉴权缓存
  → 记录审计日志

GET /api/admin/signup-whitelist
  → 返回白名单列表

POST /api/admin/signup-whitelist  {type, value}
  → 添加白名单

DELETE /api/admin/signup-whitelist/{id}
  → 删除白名单

PUT /api/admin/allow-signup  {enabled: true/false}
  → 更新 meta 表 allow_signup
```

### 4.8 冷静期管理流程（需求 22）

**定时任务（每天执行，分步匿名化 + 幂等保护）：**
```
src/admin/cooldown.py
  → SELECT id FROM grape_accounts WHERE status=3 AND updated_at < {30天前}
  → 对每个到期账号分步执行：
    Step 1: UPDATE status=2 WHERE id=? AND status=3  ← 原子标记已处理（幂等保证）
    Step 2: DELETE FROM external_identities WHERE grape_user_id=?  ← 清除绑定
    Step 3: 删除个人工作空间
    Step 4: UPDATE username='deleted_{id}', email=NULL, password_hash='', nickname='', avatar_url='' WHERE id=?
    Step 5: 记录审计日志
  → 任一步骤失败记 WARNING 日志，跳过该账号继续下一个
```

**MySQL 时间函数兼容：** `datetime('now', '-30 days')` 是 SQLite 语法，MySQL 需用 `DATE_SUB(NOW(), INTERVAL 30 DAY)`。通过 `src/mysql_compat.py` 的 `get_db_backend()` 判断后端，选择对应 SQL。

**管理员恢复（冷静期账号，接 4.7 restore 路由）：**
```
POST /api/admin/users/{id}/restore  (status=3 的冷静期账号)
  → 校验 status=3（冷静期）
  → 更新 status=1
  → token_version +1
  → 清除鉴权缓存
  → 记录审计日志
  → 返回成功
```

### 4.9 解绑检查与注销认证（需求 14/17）

**解绑平台前仓库使用检查（需求 17）：**

DELETE /api/auth/bind/{provider} 解绑前，后端查询该用户在该平台下的已托管仓库（managed_repos 表中 repo_key 以 `gitcode:` 或 `github:` 前缀匹配的记录，且 webhook_id 非空）。如果有，返回受影响仓库列表，前端弹窗警告，用户确认后才执行解绑。

```
DELETE /api/auth/bind/{provider}
  → 查询 managed_repos WHERE repo_key LIKE '{provider}:%' AND webhook_id != ''
  → 有记录 → 返回 409 + {warehouses: [...]}，前端弹窗警告
  → 用户确认后再次调用（带 confirm=true 参数）→ 执行解绑
  → 无记录 → 直接解绑
```

**注销认证方式动态选择（需求 14 修正）：**

POST /api/auth/delete-account 的认证方式根据用户状态动态选择：
- 有密码 → 要求输入密码（body.password）
- 有邮箱 → 发送验证码到邮箱（delete-account/send-code）→ 输入验证码（body.code）
- 无密码且无邮箱 → 返回提示"需先绑定邮箱或设置密码后才能注销"，或引导用户通过第三方平台重新 OAuth 授权完成认证（复用 OAuth login 流程 + state 携带 delete_account 标记）

前端危险区域页根据用户状态展示可用的认证方式，不展示不可用的方式。

### 4.10 鉴权中间件改造

现有 `deps.py` 已实现 status 校验 + 内存缓存。需确保：

1. `get_current_user_or_api_key` 校验 status（已实现）
2. `POST /api/auth/refresh` 增加 status 校验（D3 修复）
3. 禁用/恢复用户时主动失效缓存（已实现 `invalidate_user_cache`）
4. 提取 `get_user_status_cached(uid)` 公共函数，供 `get_current_user` 和 refresh 路由共享缓存（D3 修复）

**鉴权依赖层级（D45 P1-6）：**

```
require_no_session_token  — 允许 JWT + api_key，禁止 session_token（只读操作：get_me/get_identities/update_profile）
require_jwt_only          — 仅允许 JWT，禁止 session_token + api_key（高敏感操作：change_password/set_password/change_email/delete_account/bind/unbind）
```

`require_jwt_only` 是新增依赖（D45 P1-6），确保 API key 不能用于账号管理操作。API key 用于自动化场景（CI/CD、Agent），不应能修改密码或注销账号。10 个高敏感路由从 `require_no_session_token` 改为 `require_jwt_only`。

### 4.11 图形验证码范围界定（需求 2）

需求 2 要求"连续失败 5 次触发人机验证（图形验证码）"。

**本期范围：**

- 后端：`check_throttle` 返回 `captcha: True` 标志位（已实现）
- 前端：收到 `captcha: True` 时临时锁定 15 分钟，提供邮箱验证码自助解锁
- 本期不实现图形验证码生成、校验接口和前端组件
- 图形验证码列为 P1 后续迭代

**理由：** 当前系统已有 IP 限流（同一 IP 10 分钟内失败 20 次锁定）和账号限流（5 次锁定），已能阻挡绝大多数暴力攻击。图形验证码作为补充手段，P1 迭代不影响本期安全基线。

**后续迭代方案：**
- 新增 `GET /api/auth/captcha` — 生成图形验证码（SVG），返回 `{captcha_id, svg}`
- 新增 `POST /api/auth/login` 增加 `captcha_id` + `captcha_code` 可选字段
- 前端 `captcha: True` 时渲染图形验证码组件

### 4.12 验证码场景汇总

6 种验证码场景统一管理，按场景隔离，互不可混用：

| 场景 | 用途 | 有效期 | 限流 | 消费方式 |
|------|------|--------|------|---------|
| register | 邮箱注册 | 10 分钟 | 60s/3次(邮箱) 5次/10min(IP) | 验证通过后创建账号 |
| reset_password | 忘记密码 | 10 分钟 | 同上 | 验证通过后允许设置新密码 |
| set_password | 设置密码（无密码用户） | 10 分钟 | 同上 | 验证通过后允许设置密码 |
| change_email | 邮箱变更 / 首次设置邮箱 | 10 分钟 | 同上 | 验证通过后更换/绑定邮箱 |
| unlock_account | 账号解锁 | 10 分钟 | 同上 | 验证通过后清除锁定状态 |
| delete_account | 账号注销重认证 | 10 分钟 | 同上 | 验证通过后允许注销账号 |

所有场景验证码均遵循统一安全规则：6 位数字、使用后立即失效、按场景隔离、存储层不可用时拒绝验证（不降级）。IP 维度限流跨所有场景共享计数（同一 IP 的验证码请求统一计数，包含注册、密码重置等所有场景）。

---

## 5. Provider 注册表设计

### 5.1 配置文件分离（需求 6）

OAuth 平台配置独立存放于 `configs/oauth-providers.json`，与 Bot 运行时配置 `configs/global-bot-token.json` 物理分离。详见 `docs/weekly-06/oauth-provider-plugin-design.md`（v5）。

**配置驱动链路：**

```
configs/oauth-providers.json（协议参数 + 凭据，真相源）
  → migrate_oauth_providers() 启动时同步
  → meta 表（oauth_providers_config 覆盖同步 + oauth_{provider}_client_id 幂等同步）
  → _PROVIDERS_CACHE 运行时内存缓存
  → get_provider / list_providers 从缓存读取
```

**向前兼容链路（旧环境无 oauth-providers.json 时）：**

```
configs/global-bot-token.json（旧文件，不调整）
  → migrate_oauth_providers() fallback 读取旧 gitcode_oauth_client_id/secret
  → meta 表（幂等同步凭据）
  → _DEFAULT_PROVIDERS 代码内置默认协议参数兜底
```

### 5.2 代码内置默认（_DEFAULT_PROVIDERS）

```python
_DEFAULT_PROVIDERS = {
    "gitcode": {
        "name": "GitCode",
        "auth_url": "https://gitcode.com/oauth/authorize",
        "token_url": "https://gitcode.com/oauth/token",
        "user_url": "https://gitcode.com/api/v5/user",
        "scope": "all",
        "id_field": "id",
        "login_field": "login",
        "name_field": "nickname",
        "pkce_enabled": False,
        "brand_color": "#e7330f",
        "whitelist_column": "gitcode_username",
        "accept_header": "application/json",
    },
    "github": { ... },
}
```

注意：_DEFAULT_PROVIDERS 不含 client_id/secret——凭据只从配置文件或 meta 表读取。

### 5.3 消除 if-else 分叉

所有 provider 相关逻辑通过配置驱动，代码中不硬编码平台名做条件分叉：

| 位置 | 旧代码 | 新代码 |
|------|--------|--------|
| get_user_info | if provider == "github": headers["Accept"] = ... | headers["Accept"] = cfg.get("accept_header") |
| _get_client_id | if provider == "gitcode": get_meta_value("oauth_gitcode_client_id") | get_meta_value(f"oauth_{provider}_client_id") |
| 白名单检查 | if provider == "gitcode": is_gitcode_user_allowed(...) | is_user_allowed(cfg["whitelist_column"], ...) |
| providers API | for key, cfg in PROVIDERS.items(): if key == "gitcode": ... | for key in list_providers(): get_meta_value(f"oauth_{key}_client_id") |
| 旧表同步 | if provider == "gitcode": link_gitcode_user(...) | link_user_provider(user_id, provider, ...) |
| token 刷新 | refresh_gitcode_oauth_token(user_id) | refresh_oauth_token(user_id, provider) |

### 5.4 新增平台流程

1. 标准 OAuth 2.0 平台：在 oauth-providers.json 添加 provider 条目（含协议参数 + client_id/secret）→ 重启生效 → 前端自动展示 → 无需改代码
2. 非标准平台：实现适配器逻辑 → 在 oauth-providers.json 添加配置 → 注册到 _DEFAULT_PROVIDERS → 前端自动展示

**非标准平台适配器接口契约：**

标准平台通过配置驱动，providers.py 的 `get_user_info(token, provider_config)` 统一处理 OAuth 2.0 标准响应。非标准平台（如微信 openid/unionid 分离、企业 SSO 自定义字段）需扩展 `get_user_info` 或实现适配器函数：

```python
# 适配器需返回的标准化用户信息结构
def get_user_info(token: str, cfg: dict) -> dict:
    """获取第三方平台用户信息，返回标准化字段。"""
    return {
        "external_id": str,       # 平台唯一用户标识（如 GitHub user.id、微信 unionid）
        "external_login": str,    # 平台用户名（用于自动生成 Grape 用户名）
        "external_name": str,     # 显示名称
        "external_avatar": str,   # 头像 URL
        "email": str | None,      # 邮箱（可能为空）
    }

# 非标准平台通过 cfg 字段映射配置适配：
# id_field: "openid" → 从 user_url 响应中取 openid 字段
# login_field: "login" → 从 user_url 响应中取 login 字段
# name_field: "nickname" → 从 user_url 响应中取 nickname 字段
# 特殊处理（如微信 unionid）在 get_user_info 中按 provider 分支处理
```

---

## 6. 前端设计

### 6.1 登录页（index.html）

- 复用主站 `.ln` / `.lbox` + app.css + apple-theme.css
- 密码表单在上方，OAuth 按钮在下方，注册引导在最底部
- OAuth 按钮动态渲染（`GET /api/auth/providers`），品牌色区分
- 移动端 `.lbox` 响应式（padding/font-size/logo 缩小）

### 6.2 注册页（register.html）

- 复用主站 `.ln` / `.lbox` + app.css + apple-theme.css
- 两步式验证码流程（发送验证码 → 输入验证码 → 注册）
- `<form onsubmit>` 支持回车提交
- 密码可见性切换
- OAuth 关联模式（Tab 切换注册新账号 / 关联已有账号）
- i18n data-i18n 标记 + locale 文件加载
- 移动端响应式

### 6.3 忘记密码页（forgot.html）

- 复用主站 `.ln` / `.lbox` + app.css + apple-theme.css
- 两步式验证码流程（发送验证码 → 输入验证码 + 新密码 → 重置）
- 倒计时 + 邮箱只读展示 + 密码可见性切换
- i18n + 移动端响应式

### 6.4 新手引导页（需求 17/24/31）

未绑定时 `renderOnboarding()` 渲染单屏绑定页面（详见 onboarding-experience-tech-design.md）：

- 复用 lobby-head（Logo + 组织下拉 + 用户菜单）
- 不显示"项目/项目模板/工作台"三 tab
- 单屏居中内容区：
  - 欢迎语 + 产品定位语
  - 平台绑定卡片（动态获取 `/api/auth/providers`，每个平台一张卡片：图标 + 名称 + 绑定按钮）
  - 底部小提示（绑定后自动进入工作台，可在用户设置管理绑定）
- 用户头像下拉菜单提供"用户设置"和"登出"入口
- 不依赖仓库的功能页面中，未绑定时顶部显示"返回完成绑定"提示条
- 不展示产品介绍内容（工作流演示、Agent 架构、基础设施特性、底部 CTA、引流入口）
- 不展示滚动动画（进度条、浮动按钮、IO 揭示动画）

### 6.5 OAuth 补全提示条（需求 18）

OAuth 首次登录后，项目页顶部轻量提示条：
- "为了账号安全，建议设置 Grape 密码和绑定邮箱"
- "去设置"按钮 → 跳转用户中心
- "跳过"按钮 → 记录状态，后续不再显示
- 非首次登录不弹
- 用户中心安全子页面持续显示完整度状态

### 6.6 用户中心（需求 15/25）—— 内嵌 SPA view

**页面架构：**

用户中心为主应用内的独立 SPA view（app.nav('account')），不再是独立 HTML 页面。隐藏工作台侧边栏，保留主应用顶栏（logo + 面包屑 + 帮助 + 用户菜单），内容区全宽展示在 pageContent 内。使用主应用 CSS 类体系（.cd/.stabs/.bt/.srow）。不整页跳转，不丢失工作上下文。

account.js 从 IIFE 独立模块改造为全局函数（renderAccount(tab)），和 renderSettings(tab) 平级。渲染目标从 accContent 改为 pageContent。弹窗/Toast 用主应用的 app.showToast / app.showConfirm / .modal class。

**路由（main.py）：**

```python
@app.get("/account", include_in_schema=False)
@app.get("/account/{path:path}", include_in_schema=False)
async def serve_account_redirect():
    """旧用户中心 URL 302 重定向到内嵌 view。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/#account", status_code=302)
```

**页面结构：**

- 在主应用 pageContent 内渲染，复用侧边栏和顶栏
- 内容区用 .stabs tab 栏切换子页面
- tab：个人资料 / 安全 / 凭据 / 外观 / API Keys / 危险区域

**tab 结构：**

| tab | 内容 | 说明 |
|-----|------|------|
| 个人资料 | 用户名(只读) + 邮箱(只读) + 昵称(可编辑) + 保存 | 默认首页 |
| 安全 | 密码管理 + 邮箱管理 + 第三方账号绑定/解绑（3 个 .cd 卡片） | 认证方式管理 |
| 凭据 | 华为云 AK/SK + 未来扩展其他云平台凭据 | 授权访问管理，独立于安全 tab |
| 外观 | 主题选择 + 语言切换 | 从通用设置合并 |
| API Keys | API Key 列表 + 创建/撤销 | 从 settings.js 迁移，settings 不再保留 |
| 危险区域 | 注销账号入口 | 始终展示 |

**个人资料页（默认首页）：**

- 用户名（只读）+ 邮箱（只读）+ 昵称（可编辑）
- 保存 → `PUT /api/auth/profile`（nickname）

**安全页（密码 + 邮箱 + 第三方账号）：**

卡片1 — 密码管理：
- 有密码用户：显示"修改密码"入口 → 弹窗（当前密码 + 新密码）→ `POST /api/auth/change-password`
- 无密码用户：显示"设置密码"入口 → 弹窗（验证码发送 + 验证码输入 + 新密码）→ `POST /api/auth/set-password/send-code` + `POST /api/auth/set-password`

卡片2 — 邮箱管理：
- 无邮箱用户：显示"首次设置邮箱"入口
- 有邮箱用户：显示"更换邮箱"入口
- 流程：弹窗（输入新邮箱 + 验证码）→ `POST /api/auth/change-email/send-code` + `POST /api/auth/change-email`

卡片3 — 第三方账号：
- 平台列表（`GET /api/auth/providers`），已绑定的显示"解绑"按钮，未绑定的显示"绑定"按钮
- 绑定 → `GET /api/auth/bind/{provider}` → 跳转 OAuth 授权页
- 解绑 → 确认弹窗 → `DELETE /api/auth/bind/{provider}`
- 解绑前检查至少保留一种登录方式

**凭据页（云平台凭据，从通用设置迁移）：**

卡片1 — 华为云 AK/SK：
- Access Key + Secret Key 输入（掩码 ********，眼睛按钮切换明文/密文）
- 保存 → `PUT /api/settings/credentials`（huawei_cloud_ak + huawei_cloud_sk）
- 从 `/settings/credentials` API 加载现有凭据状态
- 设计理由：认证（security tab）≠ 授权（credentials tab），分开职责清晰，扩展性好

**外观页（从通用设置合并）：**

- 主题选择（Apple Liquid Glass / Grape Default）
- 主题模式（Light / Dark / Auto）
- 语言切换（中文 / English）

**API Keys 页：**

- API Key 列表 + 创建/撤销
- 从 settings.js 迁移，settings view 不再保留 API Keys tab

**危险区域页：**

- 注销账号入口 → 弹窗选择验证方式（密码 / 邮箱验证码）→ `POST /api/auth/delete-account/send-code` + `POST /api/auth/delete-account`
- 注销后显示成功提示 + "进入 30 天冷静期"

**敏感操作反馈（需求 25）：**

每个敏感操作完成后：
- 用主应用 app.showToast() 显示绿色 toast（操作结果摘要 + 下一步引导文案）
- 自动刷新当前子页面数据

| 操作 | 页面反馈 |
|------|---------|
| 修改密码 | 成功提示 + "其他设备已下线" |
| 邮箱变更 | 成功提示 + "旧邮箱已不可登录" |
| 绑定第三方 | 成功提示 + "可用该平台快捷登录" |
| 解绑第三方 | 成功提示 + "该平台登录已失效" |
| 设置密码 | 成功提示 + "可用密码登录" |
| 注销账号 | 成功提示 + "进入 30 天冷静期" |

**入口改造：**

| 入口 | 改造 |
|------|------|
| 侧边栏用户头像 | 点击弹出下拉菜单：个人资料 / 账号安全 / 登出，通过 app.nav('account') 切换 |
| 新手引导页"前往用户中心" | app.nav('account','profile') |
| OAuth 补全提示条"去设置" | app.nav('account','security') |
| 工作台设置 | 移除 account 子页签 + API Keys 子页签，保留凭据/通知/外观 |
| OAuth 绑定回调 | 统一回调到 /api/auth/oauth/callback，从 state 恢复 provider 并分流走绑定逻辑，成功后重定向到 /?bind_success={provider}#account（D26/D28 修复） |
| 旧 URL /account | 302 重定向到 /#account |

**app.js nav() 新增分支：**

```javascript
} else if (view === 'account') {
    title.textContent = '用户中心';
    // 只隐藏侧边栏，保留顶栏
    var sb = $('sidebar'); if (sb) sb.style.display = 'none';
    renderAccount(param || 'profile');
    _checkBindSuccess();
}
```

_nonRepoViews 数组加入 'account'。
_showBindBannerIfNeeded 排除 account view（不在用户中心显示绑定提示条）。
_accountGoBack() 恢复侧边栏并返回工作台。

### 6.7 管理后台用户管理（需求 21）

admin.html 新增"用户管理"模块：
- 用户列表（表格 + 分页 + 搜索）
- 禁用/恢复用户操作
- 白名单管理
- 开放注册开关

### 6.8 路由注册顺序

```python
# src/api/main.py
app.include_router(grape_auth.router)  # 统一账号路由（注册/登录/OAuth/绑定/引导）
app.include_router(auth.router)        # 仅保留 refresh/logout/profile（D1 修复后冲突路由已移除）
app.include_router(admin_router)       # /api/admin/*
```

**D1 修复后 auth.py 仅保留 3 个路由：**
- `POST /api/auth/refresh` — token 续期（D3 修复增加 status 校验）
- `GET /api/auth/profile` — 用户信息
- `POST /api/auth/logout` — 登出

**grape_auth.py OAuth 路由（D26 修复后）：**

OAuth 回调地址（固定，所有平台共用，需在平台侧注册）：
- `GET /api/auth/oauth/callback` — 统一回调（所有平台 + 登录/绑定共用，从 state 恢复 provider 和业务上下文）

前端 API 路由（provider 作为路径参数，前端已知 provider，不需要在平台侧注册）：
- `GET /api/auth/login/{provider}` — 发起 OAuth 登录（redirect_uri 指向统一回调）
- `GET /api/auth/bind/{provider}` — 发起 OAuth 绑定（redirect_uri 指向统一回调）
- `POST /api/auth/login/{provider}/associate` — 关联已有账号
- `POST /api/auth/login/{provider}/register` — 注册新账号
- `DELETE /api/auth/bind/{provider}` — 解绑
- `GET /api/auth/providers` — 平台列表

### 6.9 关键页面布局说明

**登录页（index.html）—— 从上到下：**

| 区域 | 内容 | 说明 |
|------|------|------|
| Logo | Grape 图标 | 居中 |
| 密码表单 | identifier 输入框 + password 输入框 + 登录按钮 | 上方，单输入框自动识别邮箱/用户名 |
| OAuth 按钮 | 动态渲染（GET /api/auth/providers） | 下方水平并排，品牌色区分，毛玻璃效果 |
| 底部入口 | 注册账号 · 忘记密码 · 申请内测 · 管理后台 | 最底部链接 |

**用户中心（account view，pageContent 内渲染）—— .stabs tab 栏 + 内容区：**

| 子页面 | 内容 | 显示条件 |
|--------|------|---------|
| 个人资料 | 用户名(只读) + 邮箱(只读) + 昵称(可编辑) | 默认首页 |
| 安全 | 密码管理 + 邮箱管理 + 第三方账号绑定/解绑（3 个 .cd 卡片） | 始终展示 |
| API Keys | API Key 列表 + 创建/撤销 | 始终展示 |
| 危险区域 | 注销账号入口 | 始终展示 |

**OAuth 关联页（register.html 关联模式）—— Tab 切换：**

| 区域 | Tab 1：绑定已有账号 | Tab 2：注册新账号 |
|------|---------------------|------------------|
| 表单字段 | identifier + password | username + email(可选) + password(可选) |
| 提交端点 | POST /api/auth/login/{provider}/associate | POST /api/auth/login/{provider}/register |
| 提交后 | 绑定关系写入 + 签发 JWT → 登录 | 创建账号 + 绑定 + 签发 JWT → 登录 |

---

## 7. 安全设计

| 威胁 | 防御 |
|------|------|
| 密码泄露 | bcrypt(rounds=12) + SHA-256 预哈希 + 旧算法透明升级 |
| 暴力破解 | 账号维度（5次→验证码 / 10次→锁定15min）+ IP 维度（20次→锁定） |
| 账号枚举 | 发送验证码统一响应 + 登录统一错误 + 忘记密码统一响应 |
| Token 劫持 | refresh_token HttpOnly cookie + token_version 撤销 |
| Token 续期绕过禁用 | refresh 时校验 status（需求 20，D3 修复） |
| OAuth CSRF | state 参数 + oauth_states 一次性消费 |
| OAuth Code 拦截 | PKCE（S256）按平台配置决定（`pkce_enabled` 字段控制） |
| 账号劫持 | 绑定已有账号需重新认证 + 不自动关联 OAuth 邮箱 |
| 重复绑定 | (provider, external_id) + (grape_user_id, provider) 双 UNIQUE |
| 验证码时序攻击 | constant-time 比较 |
| 验证码暴力破解 | 5 次失败作废 + IP 维度限流 |
| 注册接口批量调用 | IP 维度限流 5 次/10 分钟（需求 19，D4 修复） |
| 审计日志篡改 | append-only + 哈希链 |
| JWT 密钥泄露 | 生产环境黑名单校验 + 密钥轮换（kid） |
| 固定验证码滥用 | 仅非生产环境 + 常量时间比较 + 审计日志 |
| 并发注册竞态 | 数据库 UNIQUE 约束兜底 + 明确业务提示（需求 23，D8 修复） |
| 第三方凭据泄露 | 已实施：AES-256-GCM 加密存储（credential_crypto.py），密钥轮换支持（需求 5/6） |
| OAuth 开放重定向 | 已实施：_get_trusted_host TRUSTED_HOSTS 白名单校验（D37）。D46 P0-1：OAUTH_REDIRECT_URI 也校验 host 白名单（已修复） |
| OAuth 回调 HTTP 降级 | D46 P1-4：X-Forwarded-Proto 可伪造 → 生产环境强制 https（已修复） |
| SMTP 中间人攻击 | D46 P0-2：SMTPS 465 端口 use_tls=false 时禁用 SSL 证书校验 → 始终启用（已修复） |
| JWT 密钥过短 | D46 P0-4：validate_jwt_secret 只做黑名单检查无最小长度 → len >= 32（已修复） |
| JWT 关联 token 越权 | D46 P0-3：OAuth 关联流程以 user_id=0 签发 JWT → get_current_user 已有 uid=0 拒绝（已防御） |
| 登录锁定绕过 | D46 P1-13：_get_lock_until 缺窗口过滤 → 加 AND created_at>=?（已修复） |
| datetime('now') MySQL 不兼容 | D46 P1-14：改用 Python time.strftime 生成时间戳（已修复） |
| 限流 fail-open | D46 P1-7：验证码限流异常返回 0 → fail-closed 返回大值（已修复） |
| 白名单 fail-open | D46 P1-6：signup_policy 白名单异常返回空 set → 返回 None 拒绝注册（已修复） |
| 凭据解密静默失败 | D46 P1-10：decrypt 失败返回密文 → 抛 ValueError + _safe_decrypt 包装（已修复） |
| SMTP 邮件头注入 | D46 P1-8：to/subject 未过滤换行符 → 过滤 \r\n（已修复） |
| access_token URL 泄露 | D46 P1-2：OAuth 登录 access_token 在 URL → 前端已有 history.replaceState（已防御） |

**第三方凭据加密存储方案（已实施）：**

需求 5/6 要求第三方平台凭据加密存储。external_identities 表的 access_token 和 refresh_token 已通过 credential_crypto.py（AES-256-GCM）加密存储。方案：

**加密算法：** AES-256-GCM（认证加密，防篡改+防解密伪造）

**密钥管理：**
- 密钥从环境变量 `GRAPE_CREDENTIAL_KEY` 读取（base64 编码的 32 字节），与 JWT 密钥管理方式一致
- 密钥轮换：加密值存储格式为 `{key_id}:{nonce_b64}:{ciphertext_b64}`，key_id 标识使用哪个密钥版本
- 多密钥通过环境变量 `GRAPE_CREDENTIAL_KEYS` 配置（格式同 `JWT_SECRET_KEYS`：`kid1:key1,kid2:key2`），第一把为当前签发密钥
- 旧密钥保留用于解密，新密钥用于加密（同 JWT kid 机制）
- 生产环境启动校验密钥（同 JWT 密钥门禁机制，复用 `validate_jwt_secret` 模式）

**模块封装：**
- 加解密逻辑封装在 `src/auth/credential_crypto.py`
- `encrypt(plaintext: str) -> str` — 返回 `{key_id}:{nonce_b64}:{ciphertext_b64}`
- `decrypt(encrypted: str) -> str` — 按 key_id 查找密钥，解密
- 明文空字符串 `''` 不加密，直接存储 `''`（无凭据的 OAuth 用户）

**集成点：**
- `create_external_identity` / `update_external_identity_tokens` — 写入时调 `encrypt()`
- `get_external_identity` / `get_user_identities` — 读取时调 `decrypt()`
- 上层路由无感知，加解密在 grape_account_db.py 内部完成

**迁移策略：**
- 已有明文数据通过迁移脚本批量加密（幂等，已加密的跳过）
- 读取时兼容明文：decrypt 失败则当明文返回（迁移期）
- 迁移完成后移除明文兼容逻辑

### 7.2 错误码规范

账号体系所有 API 统一使用以下错误码规范，响应体格式为 `{"code": <int>, "message": <str>}`：

| HTTP 状态码 | 业务码 | 场景 | 示例消息 |
|-------------|--------|------|---------|
| 400 | 400 | 参数校验失败 | 用户名已被占用 / 密码至少 8 个字符 |
| 401 | 401 | 未认证/认证过期/密码错误 | 账号或密码错误 / 缺少 refresh_token |
| 403 | 403 | 权限不足/账号禁用/注册策略拒绝 | 账号已被禁用 / 该邮箱不在允许注册范围内 |
| 404 | 404 | 资源不存在 | 账号不存在 |
| 409 | 409 | 并发冲突 | 该邮箱已注册，请直接登录或重置密码 |
| 429 | 429 | 限流 | 请求过于频繁，请稍后再试 |
| 500 | 500 | 服务端错误 | 创建账号失败 |

**错误码规则：**
- 注册发送验证码接口（register/send-code）：邮箱已注册时直接返回 409 + "该邮箱已注册 Grape 账号，请直接登录或重置密码"，不发送验证码
- 登录接口：密码错误和账号不存在统一返回 401 + "账号或密码错误"，不暴露账号是否存在
- 仅当密码正确但账号被禁用时，才返回 403 + "账号已被禁用"

---

## 8. 数据迁移设计

### 8.1 迁移阶段

```
Phase 1: 建表 ✓ 已完成
Phase 2: 回填 ✓ 已完成（脚本已就绪，dry-run 验证通过）
Phase 3: 双写 ✓ 已完成 — grape_auth.py 统一 OAuth 回调实现双写 external_identities
Phase 4: 切读 ✓ 已完成（D32） — 业务代码只查 grape_accounts/external_identities，不 fallback 旧表；_sync_legacy_binding 已删除；深度扫描 19 处遗漏已修复
Phase 5: 废弃 ✓ 已完成（D32） — 业务代码不再读写 user_accounts，老表变为死表；服务启动时自动异步执行迁移脚本
```

**双写退役：** D32 修复后 _sync_legacy_binding 已删除，双写完全停止。深度扫描 19 处遗漏已全部修复（含 batch_update_last_active 定时任务 + link_user_provider 老表写入），业务代码不再读写 user_accounts 老表。

**Phase 5 废弃策略选择——保持不动（非改名）：**

v1 设计文档建议"将 user_accounts 表改名为 user_accounts_deprecated"，v2 选择保持不动。理由：

1. 迁移期需 fallback 读取旧表，改名会导致 fallback 查询失败
2. 双写退役条件（连续 30 天无 fallback 命中）达成后，可考虑改名作为 Phase 6（可选）
3. 保持不动策略更保守安全——即使有代码遗漏仍继续读写旧表，不会因改名导致 500 错误
4. 旧 OAuth 路由已随 D1 修复删除，无新数据写入旧表，保持不动无副作用

### 8.2 DDL 变更

- `grape_accounts.email`：`DEFAULT NULL`（D12，新表直接定义）
- `grape_accounts` 新增 `last_login_at TEXT DEFAULT NULL` 列（D11），通过 `_ensure_column` 幂等补列
- email 唯一索引：SQLite + MySQL 统一为普通唯一索引 `ON grape_accounts(email)`（D12，NULL 不参与唯一约束）

### 8.3 迁移脚本

统一迁移脚本 `scripts/migrate_user_accounts_to_grape.py`，一个方法保证逻辑正确性：

**设计原则：**
1. 依赖 `user_accounts.migrated` 字段跳过已迁移行（migrated=1）
2. 每行的全部数据（grape_accounts + external_identities + user_credentials）一次性全量迁完才标记 migrated=1，保证不遗漏
3. 三个幂等原语，每个用 NOT EXISTS 检查，重跑不产生重复数据：
   - `_ensure_grape_account` — 按 username 查已有→返回 id；不存在→INSERT
   - `_ensure_external_identity` — 按 (provider, external_id) 查已有→跳过
   - `_ensure_user_credentials` — 按 (user_id, provider) 查已有→跳过
4. `_migrate_one_user` 单行全量迁移：grape_accounts + external_identities + user_credentials 一次做对
5. 服务启动时异步执行（main.py lifespan），不阻塞启动

**两阶段：**
- 阶段 1：遍历 migrated=0 的行，每行全量迁移后标记 migrated=1
- 阶段 2：关联表 user_id 更新（user_repos/chat_conversations/organization/org_member），只更新不在 grape_accounts 中的旧表残留 id

**CLI：** `--dry-run`（预览）/ `--execute`（执行）/ `--force`（重置 migrated=0 后重跑）

**历史问题修复记录：**
- 早期脚本只迁用户数据就标记 migrated=1，未迁 external_identities，导致 xiaowu2119 OAuth 登录查不到绑定关系
- 重写后保证每行全量迁完才标记，--force 重跑验证：133/15/19 数据不变，0 重复

**已删除的重复脚本：**
- `scripts/migrate_grape_accounts.py` — 早期迁移脚本，功能已被统一脚本覆盖
- `scripts/fix_missing_identities.py` — 补丁脚本，功能已被 _migrate_one_user 覆盖

---

## 9. 实施计划

### Phase 1 — 基础设施 ✓ 已完成

- [x] src/auth/ 8 模块
- [x] 6 张新表 DDL
- [x] 密码哈希（bcrypt + SHA-256 兼容）
- [x] 验证码系统（6 场景 + IP/邮箱限流）
- [x] 审计日志（append-only + 哈希链）
- [x] 邮件服务（SMTP/Resend/DEV 三模式）
- [x] Provider 注册表 + PKCE
- [x] JWT kid 密钥轮换

### Phase 2 — 数据迁移 ✓ 已完成

- [x] 迁移脚本（幂等 + dry-run）
- [x] 执行生产迁移（服务启动时自动异步执行，幂等，不阻塞启动）— main.py lifespan 中 asyncio.create_task + asyncio.to_thread 调用 run_migration

### Phase 3 — 后端改造

- [x] grape_account_db.py CRUD
- [x] grape_auth.py 路由（注册/登录/忘记密码/修改密码/设置密码/邮箱变更/注销/绑定/解绑/解锁）
- [x] deps.py status 校验 + 内存缓存
- [x] 统一 OAuth 回调路由 + 未绑定引导 + providers API
- [x] D1 修复：auth.py 移除全部 5 个冲突路由（POST /login + 2 OAuth login + 2 OAuth callback）
- [x] D2/D5 修复：随 D1 一并解决（grape_auth.py 统一路由已实现查 external_identities + 双写 + 创建组织）
- [x] D8 修复：并发注册竞态返回业务提示（email + username 冲突分支）
- [x] LoginBody 兼容旧客户端 username 字段
- [x] D3 修复：refresh_token 续期校验 status（提取 get_user_status_cached 公共函数）
- [x] D4 修复：IP 限流阈值 MAX_IP_PER_WINDOW 从 10 改为 5（需求 19 要求）
- [x] D11 修复：grape_accounts.last_login_at（DDL + _ensure_column + 登录时写入）
- [x] D12 修复：统一用 NULL 表示无 email + 普通唯一索引（消除数据库特定语法）
- [x] providers.py redirect_uri 清理（get_redirect_uri 返回新路径）
- [x] D9 修复（后端）：profile/me 返回 bindings 列表
- [x] 新增：admin 用户管理路由（admin/db.py + admin/routes.py 7 个路由）
- [x] 新增：冷静期定时任务（cooldown.py + 幂等分步执行 + main.py 注册）
- [x] D10 修复：credential_crypto.py + 凭据加密存储（AES-256-GCM）
- [x] /repos/member 支持 ?provider 参数按平台查询仓库（需求 17 项目页按平台切换）— list_member_repos 支持 provider 参数，缓存按 repo_key 前缀过滤
- [x] 注销认证方式动态选择（需求 14 修正：无密码无邮箱用户走 OAuth 重新授权）— delete_account 支持 oauth_state 认证 + delete_account_via_oauth 发起端点 + OAuth callback delete_account_user_id 分流
- [x] 首次设置邮箱流程（需求 12 修正：无邮箱用户直接向新邮箱发验证码，不验证旧邮箱）— change_email_send_code 已处理 old_email 为空的情况

### Phase 4 — 前端改造

- [x] 登录/注册/忘记密码独立页面
- [x] OAuth 按钮动态渲染 + 品牌色
- [x] i18n 文案（register/forgot/settings.js）
- [x] 移动端响应式
- [x] D9 修复（前端）：showApp 串行加载 + bindings 列表
- [x] 需求 18：OAuth 补全提示条
- [x] 需求 21：管理后台用户管理界面
- [x] admin.js IIFE 闭合修复 + app.js 导出补全
- [x] 需求 15：用户中心内嵌 SPA view（account.js renderAccount 全局函数，pageContent 内渲染，与 settings 平级）
- [x] 需求 17 重写：新手引导页替代三 tab + 项目页按平台切换 + nav 未绑定守卫 — app.js 引导页逻辑 + nav 拦截跳转引导页
- [x] 需求 24：未绑定状态返回引导提示条 — app.js 提示条逻辑（引导页不显示，其他页面显示）
- [x] 需求 25：敏感操作 toast 反馈 — app.js bind_success toast 已实现
- [x] 工作台设置移除 account 子页签 — settings.js 只保留仓库设置（gitcode tab），通用设置页面已移除
- [x] 侧边栏用户头像下拉菜单改造 — app.js avatar dropdown（需求 15）

### Phase 5 — 重构 & 测试 & 上线

- [x] 单元测试 91 passed（账号体系相关全过，test_runtime_protocol 的 35 个失败为既有基线问题与账号体系无关）
- [x] Playwright DOM 验证
- [x] Playwright 移动端验证
- [x] D1 修复后 test_auth_routes.py OAuth 测试适配统一路由（路径 + redirect_uri 格式 + callback 302 行为）
- [x] D13 修复：辅助函数提取（cookie_utils.py）— 已实施，消除 auth.py 和 grape_auth.py 重复定义
- [x] D14 修复：soft_delete_account 补 updated_at
- [x] D15 修复：change_password 检查 update_password 返回值 + fallback 旧表
- [x] D16 修复：oauth_register 补 D8 冲突检查
- [x] D17 修复：login_failures 定期清理（cooldown.py）
- [x] D18 修复：注册流程非原子性补偿逻辑（create_org WARNING + create_external_identity 回滚）
- [x] D19 修复：signup_policy 值格式兼容
- [x] D20 修复：audit EVENT_TYPES 补 account_anonymized
- [x] D21 修复：get_grape_account_by_id 等 SELECT 补 last_login_at
- [x] D22 修复：生产环境 GRAPE_CREDENTIAL_KEYS 启动门禁
- [x] D23 修复：固定验证码使用时记录审计日志（需求 8/16）
- [x] D24 修复：审计日志保留期限管理——cooldown.py 清理 180 天前记录（需求 16）
- [x] D25 修复：账号注销管理员权限转移检查（需求 14）
- [x] D26 修复：统一回调地址 /api/auth/oauth/callback，所有平台共用，从 state 恢复 provider 和业务上下文（需求 4/5，D31）
- [x] D27 修复：meta 表删除旧 oauth_gitcode_redirect_uri，redirect_uri 由代码自动构造（需求 4/5）
- [x] D28 修复：绑定回调重定向 URL 改为 /?bind_success=xx#account，query 在 hash 前（需求 5/25）
- [x] D29 修复：OAuth 配置分离到独立 configs/oauth-providers.json + migrate 统一入口 + admin 配置分流（需求 6，详见 oauth-provider-plugin-design.md v5）
- [x] D30 修复：业务代码读 OAuth token 改为优先读 external_identities 新表，老表 fallback（需求 6）
- [x] D31 修复：统一回调地址 /api/auth/oauth/callback + validate_oauth_state provider 可选 + 删除 bind_callback 路由（需求 4/5）
- [x] D32 第一轮：迁移脚本 + 业务代码只查 grape_accounts 不 fallback + 删除 _sync_legacy_binding + 凭据改用 user_credentials 表。91 测试通过
- [x] D32 补丁：user/db.py get_credentials/get_user_gitcode_token/get_user_github_token/get_github_token/_get_username_by_id/batch_update_last_active/link_user_provider 改为查 grape_accounts/external_identities/user_credentials；orgs.py/runtime.py/events.py/settings.py/engine_select.py/session_manager.py/config.py 直接 SQL 改为查 grape_accounts（19 处遗漏全部修复，93 单元测试通过）
- [x] D32 测试适配：test_login_provider 4 个测试 + test_unified_user_info 9 个测试适配 D32（create_user → create_grape_account，get_token_version → grape_account_db.get_token_version，delete_user → soft_delete_account）
- [x] D33 修复：个人组织 slug 冲突补建 + /repos/managed 个人组织不按 org_id 过滤。注册/登录/OAuth 登录三处增加个人组织补建逻辑（幂等，slug 冲突时加 username 后缀）
- [x] D34 修复：admin 管理后台用户列表/注册白名单页面样式错乱。#usersView/#whitelistView 移入 .mn 容器 + .ipt 浅色背景覆盖 + .tbl 表格样式 + Playwright DOM 验证通过
- [x] D35：OAuth 自动创建账号（需求 4 变更）。回调未绑定分支改为自动创建 + `_generate_username` + 邮箱冲突回退 + state 补充 email + 删除注册策略校验
- [x] D36：确认密码字段（需求 1/3/11/12 变更）。注册/修改密码/设置密码/忘记密码四个表单加确认密码 + JS 校验
- [x] E2E 测试：注册→登录→修改密码→忘记密码重置→Token 刷新→注销→绑定查询→注销账号（34 项全通过）
- [x] 迁移验证：MySQL 后端数据完整性校验（grape_accounts 133 行，external_identities 15 行，user_credentials 19 行，旧表 90 个用户全部迁移，--force 重跑 0 重复）
- [x] D37 深度审查修复：BUG-1 Host Header 白名单校验 / BUG-3 onclick JS 转义 / OMIT-1 client_secret 配置文件留空 / OMIT-2 cryptography 库启动检查 / OMIT-3 PKCE 默认启用 / OMIT-4 test_auth_token schema 适配 / OMIT-5 test_auth_routes 测试隔离 / UX-1 用户中心动态渲染 / UX-2 OAuth 图标首字母降级
- [x] D38 迁移脚本重写：统一为一个方法，每行全量迁完才标记 migrated=1，三个幂等原语 NOT EXISTS 检查，删除 migrate_grape_accounts.py + fix_missing_identities.py 两个重复脚本
- [x] D39 审查报告修复（P0+P1）：P0-1 OAuth 注销 state 重新生成 / P1-1 get_grape_account_by_email 去掉 status=1 过滤 / P1-2 登录锁定时长独立于统计窗口 _get_lock_until / P1-3 注册开关默认关闭 / P1-5 send_code 先发邮件后写 DB / P1-8 danger tab 先加载 profile
- [x] D40 多平台仓库 + 创建项目平台选择：list_member_repos 不传 provider 时拉取全部绑定平台仓库合并返回 / create_from_template 支持 body.provider 参数 / 前端创建弹窗加平台选择下拉框 _buildPlatformSelect / _fetch_and_cache 每平台独立 try/catch 一个失败不阻断其他 / save_user_repos 合并后一次性写入避免覆盖 / D35 OAuth 自动创建前先检查已有同名账号直接绑定避免创建 _2 重复账号
- [x] D41 chat.py acc 未定义修复 + 6 个测试文件 user_accounts→grape_accounts 适配：D32 迁移时删除了 user_db.get_user_account 但保留了 _build_web_user_info(user, acc) 引用导致 NameError / session_manager update_unified_user_info 改为直接查 grape_accounts 避免模块级 db 引用缓存 / test_engine_select + test_session_manager + test_api_routes + test_issue_178 + test_p2p_bind + test_project_templates 全部适配新表
- [x] D42 仓库列表三个问题修复：缓存 TTL 时区错误（synced_at 存 MySQL 本地时间 CST 但 Python 按 UTC 解析导致 age 为负数缓存永不过期）/ 解绑不清理 user_repos（unbind_provider 只删 external_identities 不删 user_repos 缓存）/ 前端 bindings 不刷新（解绑后 localStorage.user.bindings 不更新仍调 /repos/member token 已删导致 400/403）/ 已删除组织仓库被过滤（_filter_repos 中 is_org_personal 返回 False 过滤了托管到已删除组织的仓库）/ 绑定不清理 user_repos（oauth_callback 绑定分支不清理旧缓存）
- [x] D43 全面深度检测修复（P0×4 + P1×1 + P2×1 + 基线测试修复）：set_password 未递增 token_version / 5 条绑定路径未清理 user_repos（auto_bind + auto_create + oauth_associate + oauth_register）/ 前端绑定后不清理 repo_member_cache / delete_account 未清理 user_repos+unified_user_mapping+org_member / session_manager sqlite_master MySQL 不兼容 / templates.py 返回值缺 scene_id / push 直接调 subprocess 不走 _run_git
- [x] D44 创建项目时 host_repo 不接收 provider 导致平台选择失效：create_from_template 确定了 provider 但调 host_repo 时不传，host_repo 自己重新调 _get_user_provider_and_token 自动选择——密码登录时 github 优先，导致 GitCode 仓库被托管为 github: 前缀 / 修复：host_repo 增加 provider 参数，create_from_template 传入
- [x] D45 认证页面 UX 全面优化（登录/注册/重置密码）：参照 Apple HIG + 业界标准（Apple ID / Google Sign-in / GitHub Sign-up）重新设计三个认证页面，统一设计语言，提升用户体验和无障碍访问性
  - 新建 auth.css 共享样式表：Apple pill 按钮（980px）、字段焦点环（4px blue glow）、密码强度条（弱/中/强三色）、密码要求清单（实时打勾）、步骤指示器、面板过渡动画、成功状态面板、toast 通知（毛玻璃背景）、reduced-motion 降级、coarse-pointer 适配
  - 登录页面（index.html）：从 .ln/.lbox + .obtn + inline style 迁移到 .auth-page/.auth-card + .field-input + .auth-btn；新增密码可见性 SVG 切换（eye/eye-off）；新增 blur 实时验证（空字段即时提示）；错误消息加 role="alert" + aria-live="polite" + aria-invalid；按钮 loading 态用 spinner 替代 opacity；输入框 font-size 15px（防 iOS 自动放大）；链接分层（主：注册/忘记密码，次：内测/管理后台）；OAuth 按钮改 pill 样式 + 44pt 触控目标
  - 注册页面（register.html）：两步流程加步骤指示器（填写信息 → 验证码）；密码强度实时计算（长度+复杂度+特殊字符评分）+ 要求清单实时打勾（≥8字符 / 含字母和数字 / 不与用户名邮箱相同）；blur 验证（用户名格式 / 邮箱格式 / 密码长度 / 确认密码一致性）；验证码输入 inputmode="numeric" + autocomplete="one-time-code" + enterkeyhint；成功状态面板（绿色勾选 icon + 自动跳转）；OAuth 关联模式 Tab 改 Apple 分段控件样式
  - 重置密码页面（forgot.html）：三步流程加步骤指示器（邮箱 → 验证码+新密码 → 成功）；步骤间可返回（← 返回修改邮箱）；新密码同样有强度条和要求清单；成功状态面板（密码重置成功 + 2s 后跳转登录）；验证码输入同注册页面无障碍属性
  - apple-theme.css 兼容：.ln/.lbox 选择器增加 .auth-page/.auth-card 并列，原有页面不受影响
  - Playwright DOM 验证全通过：按钮 borderRadius=980px / background=#0071e3；输入框 fontSize=15px；blur 验证 display=block + aria-invalid=true；密码强度 weak→strong 正确切换；密码切换 type password→text；无 JS 错误
  - 架构审查优化（P0×4 + P1×4 + P2×2）：
    - P0-1 注册页加"← 返回修改信息"按钮（步骤2可返回步骤1修改邮箱/用户名，同 forgot.html 的 backToStep1）
    - P0-2 登录页 loginUser 去掉 inputmode="email"（用户名可能不是邮箱，强制 email 键盘对纯用户名不友好）
    - P0-3 密码强度在长度<8时强制显示"弱"（不论其他维度得分，避免7字符密码显示"中"误导用户）
    - P0-4 注册成功文案改为"正在登录..."（实际自动登录跳转，文案和实际行为一致）
    - P1-5 提取共享JS到 /static/js/auth.js（showToast/togglePwd/showFieldError/clearFieldError/setBtnLoading/密码强度计算/i18n初始化/startCountdown/escHtml — 消除三个页面间的代码重复，Auth.updatePwdStrength 统一密码强度算法接受配置参数）
    - P1-6 pwdLogin 调用 _validateLoginField 而非重复验证逻辑（submit 时复用 blur 验证函数）
    - P1-7 OAuth 按钮用 auth.css .oauth-btn 类替代 inline style（renderOAuthButtons 不再拼长串 inline style）
    - P1-8 auth.css 颜色改用 CSS 变量（--auth-accent/--auth-text/--auth-text-secondary 等，支持主题切换；apple-theme.css 覆盖时用固定值）
    - P2-9 删除 .field-error[role="alert"] 冗余 CSS 规则（显隐由 JS style.display 控制，role 选择器不起作用）
    - P2-10 apple-theme.css 中 .auth-page/.auth-card 自包含块独立于 .ln/.lbox（两组选择器不再并列，各自独立，legacy .ln/.lbox 保留兼容）
  - UX 验证体验优化（对标业界最佳实践）：
    - blur 验证：空字段不报错（用户可能还没填到），仅验证非空字段的格式错误 — 对标 Apple ID
    - oninput：清除该字段错误状态（用户在修改，给正反馈）— 对标 Stripe
    - submit：统一验证所有必填字段（空字段此时才报错）— 对标 Google
    - 删除预置 field-hint（规则说明）— 错误时才显示，减少视觉噪音 — 对标 GitHub
    - 密码强度/要求清单：聚焦密码字段时展开，失焦后收起（CSS :focus-within）— 对标 GitHub
    - auth.js 新增 blurValidate/submitValidate/clearErrorOnInput 三个策略函数
    - app.js 登录页 _blurValidateLogin（空字段不报错）+ _submitValidateLogin + _clearLoginError
    - Playwright 验证全通过：空字段 blur 无错误 / submit 空字段报错 / oninput 清除错误 / 密码强度聚焦展开 / field-hint 已删除 / 无 JS 错误
  - 剩余 P1/P2 最终修复（P1-6 + P2-5 + P2-16）：
    - P1-6 API key 可用于账号管理：新建 require_jwt_only 依赖（仅允许 JWT，禁止 session_token 和 api_key）；10 个高敏感路由（change_password/set_password/change_email/delete_account/bind/unbind）从 require_no_session_token 改为 require_jwt_only；只读路由（get_me/get_identities/update_profile）保持 require_no_session_token
    - P2-5 SHA-256 == 非常量时间：verify_password 旧算法（sha256$ 前缀 + 无前缀）的比较从 == 改为 hmac.compare_digest（常量时间比较，防时序攻击）
    - P2-16 set_password 递增 token_version vs 需求12：遵循需求12"设置密码后不触发其他设备下线"，移除 increment_token_version（仅保留 invalidate_user_cache 刷新缓存）；set_password 是无密码用户主动增加登录方式，不是密码变更，不应让已登录设备 token 失效
  - 邮件配置改为 configs/email.json 配置文件模式（对齐 Grape 配置体系）：
    - 新增 configs/email.example.json 模板（提交到 git）+ configs/email.json（gitignored，用户自行创建）
    - 配置结构：mode/from_email/smtp{host,port,username,password,use_tls}/resend{api_key}/dev_verification_code
    - 配置优先级：configs/email.json > 环境变量 fallback（向后兼容现有部署）
    - 改配置后无需重启（动态读取，首次读后缓存）
    - email_service.py 新增 _load_email_config/_cfg/_cfg_nested 三个配置读取函数
- [x] 单元测试 122 passed（test_grape_auth 41 + test_auth_routes 16 + test_auth_token 14 + test_engine_select 9 + test_session_manager 26 + test_api_routes TestAuth 9 + TestSettings 4 + test_issue_178 4 + test_p2p_bind 8 + test_project_templates 13，全通过）
- [x] D46 深度审计修复（4 P0 + 14 P1 + 12 P2 = 30 项）
  - 灰度发布前必须修复（6 项）：
    - [x] P0-1: providers.py OAUTH_REDIRECT_URI 绕过 TRUSTED_HOSTS → 校验 host 白名单
    - [x] P0-2: email_service.py SMTPS 465 端口 SSL 证书校验可被禁用 → 始终启用
    - [x] P0-3: grape_auth.py OAuth 关联 token user_id=0 → get_current_user 已有 uid=0 拒绝（已防御）
    - [x] P0-4: jwt_utils.py JWT secret 无最小长度 → len >= 32
    - [x] P1-13: login_throttle.py _get_lock_until 缺窗口过滤 → 加 AND created_at>=?
    - [x] P1-14: grape_account_db.py datetime('now') MySQL 不兼容 → Python 时间戳
  - 灰度发布后优先修复（5 项）：
    - [x] P1-2: access_token 在 URL → 前端已有 history.replaceState（已防御）
    - [x] P1-6: signup_policy fail-open → fail-closed（异常返回 None 拒绝注册）
    - [x] P1-7: verification 限流 fail-open → fail-closed（异常返回大值）
    - [x] P1-10: credential_crypto decrypt 静默返回密文 → 抛异常 + _safe_decrypt 包装
    - [x] P1-1: 邮箱变更 TOCTOU → update_email 区分 UNIQUE 冲突返回 409
  - 其余 P1（4 项）：
    - [x] P1-3: unbind 竞态 → delete_external_identity 已有原子检查（已防御）
    - [x] P1-4: X-Forwarded-Proto 伪造 → 生产环境强制 https（随 P0-1 一并修复）
    - [x] P1-5: 密钥池 fallback → 有 kid 但未知返回 None 拒绝验签
    - [x] P1-8: SMTP 邮件头 CRLF 注入 → 过滤换行符（随 P0-2 一并修复）
    - [x] P1-9: get_unverified_claims 异常 → except (JWTError, ValueError, TypeError)（随 P0-4 一并修复）
    - [x] P1-11: 邮件配置缓存无失效 → mtime 检查自动重新加载
    - [x] P1-12: GitHub OAuth scope 过大 → 缩减为 read:user user:email
  - 测试改造：
    - [x] 邮件服务全程 mock（autouse fixture mock send_verification_code/send_notification/_send_smtp/_send_resend）
    - [x] dev verification code mock（autouse fixture mock is_dev_verification_code，固定 888888）
    - [x] 去除所有 os.environ 环境变量操作（不再依赖 GRAPE_DEV_VERIFICATION_CODE）
    - [x] 70 passed 全通过（test_grape_auth 40 + test_auth_routes 16 + test_auth_token 14）
  - 可逐步修复（P2 12 项）：
    - [x] P2-1: _get_client_ip 仅信任可信代理 XFF
    - [x] P2-2: is_username_taken 改 LOWER() 大小写不敏感
    - [x] P2-3: login_failures 清理（cooldown.py 已有）
    - [—] P2-4: JWT 默认 secret（评估后保留不改，影响开发环境稳定性）
    - [—] P2-5: credential_crypto 默认密钥（评估后保留不改，同上）
    - [—] P2-6: 明文降级（评估后保留不改，开发环境不强制 cryptography）
    - [x] P2-7: expires_at 改整数存储
    - [x] P2-8: 验证码哈希改 HMAC-SHA256
    - [x] P2-9: _cleanup_expired 改 10% 概率
    - [x] P2-10: _load_providers 加 threading.Lock
    - [x] P2-11: LOG_ONLY 验证码日志 mask
    - [x] P2-12: Host header 日志换行符过滤
- [ ] 灰度发布 → 全量

**需求 14 资源检查范围界定：**

需求 14 业务流程提到"检查用户是否有未完成的资源（如进行中的任务、负责的项目）"。本期范围界定：
- 当前系统的任务和项目归属组织而非个人，用户注销后组织保留进入冷静期，任务不受影响
- 个人工作空间在冷静期到期后由 cooldown.py 分步删除（Step 3）
- 本期不实现"未完成资源检查"（任务/项目归属组织，注销不影响组织运行）
- 管理员权限转移检查已实现（D25：注销前检查 admin_accounts，管理员需先转移权限）

---

## 10. 与 v1 设计文档差异

| 项目 | v1 | v2 |
|------|----|----|
| 需求覆盖 | 16 条需求 | 25 条需求（新增需求 17-25） |
| 路由冲突 | 未处理 | D1 修复：auth.py 移除全部 5 个冲突路由（POST /login + 4 个 OAuth 路由），grape_auth.py 统一路由替代 |
| OAuth 旧路由 | 保留兼容 | D1 修复：旧路由删除（死代码），统一路由已完整实现查 external_identities + 双写 + 创建组织 |
| refresh 续期 | 仅校验 token_version | D3 修复：增加 status 校验 + 提取 get_user_status_cached 公共函数复用缓存（已实施） |
| 注册限流 | 仅邮箱维度 | D4 修复：IP 限流阈值从 10 改为 5（需求 19 要求，已实施） |
| OAuth 创建组织 | 新路由有，旧路由无 | D5 修复：随 D1 一并解决（统一路由已实现） |
| 管理员管理 | 预留不实现 | 需求 21：完整用户管理模块（已实施） |
| 冷静期到期 | 描述但无定时任务 | 需求 22：定时任务 + 分步匿名化 + 幂等保护（已实施） |
| 并发注册 | 未考虑 | D8 修复：UNIQUE 兜底 + email + username 冲突提示（已实施） |
| 未绑定引导 | 绑定引导卡片嵌入项目列表 | 需求 17 重写：新手引导页替代三 tab + 项目页混合展示+平台筛选 + 侧边栏仓库下拉按平台分组 |
| OAuth 补全 | 未设计 | 需求 18：非阻断引导 + 首次/非首次区分（已实施） |
| 用户中心 | 账号设置嵌入工作台设置 tab | 需求 15：独立 SPA view（app.nav('account')），隐藏侧边栏保留顶栏，与 settings 平级不混合职责 |
| 操作反馈 | 仅 API 返回成功 | 需求 25：toast 成功提示 + 下一步引导 |
| 返回引导提示条 | 无 | 需求 24：未绑定状态返回引导提示条 |
| 首次设置邮箱 | 无（死锁：无邮箱用户无法设置邮箱） | 需求 12 修正：首次设置邮箱直接向新邮箱发验证码，不验证旧邮箱 |
| 注销认证 | 仅密码或邮箱验证码 | 需求 14 修正：动态选择认证方式，无密码无邮箱走 OAuth 重新授权 |
| 图形验证码 | 需求 2 要求 | 本期范围界定：临时锁定 + 邮箱验证码解锁，图形验证码 P1 后续迭代 |
| last_login_at | 无 | D11：新增列 + DDL + _ensure_column + 登录写入（已实施） |
| 第三方凭据加密 | 未考虑 | D10：AES-256-GCM + 迁移策略（已实施） |
| MySQL 部分唯一索引 | 未考虑 | D12：统一用 NULL 表示无 email，普通唯一索引，消除数据库特定语法（已实施） |
| 辅助函数重复 | 未考虑 | D13：提取 cookie_utils.py 统一管理（已实施） |
| 绑定回调路径 | 回调地址按平台分路径，每个平台需配不同回调地址 | D26/D31：统一回调地址 /api/auth/oauth/callback，所有平台共用，从 state 恢复 provider（已修复） |
| meta 表 redirect_uri | 残留旧路径配置 | D27：删除 meta 表旧值，redirect_uri 由代码自动构造（已修复） |
| 绑定回调 URL 格式 | hash 在 query 前导致前端取不到参数 | D28：改为 /?bind_success=xx#account，query 在 hash 前（已修复） |
| 配置文件 redirect_uri | global-bot-token.json 残留旧路径 | D27：删除配置文件 redirect_uri 字段 + migrate 不再迁移此字段（已修复） |
| redirect_uri 冗余 | 未考虑 | providers.py get_redirect_uri 返回新路径，消除覆盖逻辑（已实施） |
| 注销冷静期计时 | 未考虑 | D14：soft_delete_account 补 updated_at（已修复） |
| 修改密码 fallback | 未考虑 | D15：change_password 检查返回值 + fallback 旧表（已修复） |
| OAuth 注册冲突检查 | 未考虑 | D16：oauth_register 补 D8 冲突检查（已修复） |
| login_failures 清理 | 未考虑 | D17：cooldown.py 定期清理 24h 前记录（已修复） |
| 注册原子性 | 未考虑 | D18：create_org/external_identity 失败补偿 + 回滚（已修复） |
| 注册开关值格式 | 未考虑 | D19：signup_policy 兼容 "true"/"1"/"yes"（已修复） |
| 匿名化审计事件 | 未考虑 | D20：EVENT_TYPES 补 account_anonymized（已修复） |
| SELECT 字段完整性 | 未考虑 | D21：3 个 SELECT 补 last_login_at（已修复） |
| 凭据密钥门禁 | 未考虑 | D22：生产环境校验 GRAPE_CREDENTIAL_KEYS（已修复） |
| 固定验证码审计 | 未考虑 | D23：verify_code 使用固定验证码时记录审计日志（已修复） |
| 审计日志保留期限 | 未考虑 | D24：cooldown.py 清理 180 天前 audit_logs（已修复） |
| 注销管理员检查 | 未考虑 | D25：delete_account 检查 admin_accounts，管理员需先转移权限（已修复） |
| OAuth 配置职责分离 | OAuth 配置混入 global-bot-token.json，与 Bot 配置耦合 | D29：OAuth 配置分离到独立 configs/oauth-providers.json + migrate 统一入口 + admin 配置分流（已修复，详见 oauth-provider-plugin-design.md v5） |
| OAuth token 读取走老表 | 业务代码 get_user_token 只查 user_accounts 老表，登录走 grape_accounts 新表，两表 username 不一致导致 token 读不到 | D30：deps.py 新增 _get_oauth_token_from_identity 从 external_identities 读 token，优先新表 fallback 老表（已修复） |
| OAuth 回调地址分路径 | 每个平台需配不同回调地址，新增平台需额外配置 | D31：统一回调地址 /api/auth/oauth/callback，从 state 恢复 provider（已修复） |
| 新表老表 id 碰撞 | grape_accounts 和 user_accounts 各自独立 AUTO_INCREMENT，id 值范围重叠，同 id 不同用户，业务代码查老表查到错误用户 | D32：迁移脚本 + 业务代码只查新表不 fallback + 删除 _sync_legacy_binding + 凭据改用 user_credentials 表 + 深度扫描 19 处遗漏全部修复（user/db.py 内部函数 + 直接 SQL + batch_update_last_active + link_user_provider）（已修复，详见第 10.1.1 节） |
| Host Header 开放重定向 | OAuth 回调从 request.headers.get("Host") 构造重定向 URL，无白名单校验 | D37：_get_trusted_host() 支持 TRUSTED_HOSTS 环境变量白名单校验，grape_auth.py 5 处统一调用（已修复） |
| onclick 字符串转义 | account.js onclick 属性中用 HTML 实体编码，HTML 解析器解码后导致 JS 字符串断裂 | D37：_accountJsStr() JS 层面转义，替代 onclick 内的 HTML 实体编码（已修复） |
| PKCE 默认关闭 | 需求 4 要求标准 OAuth 2.0 平台强制启用 PKCE，但两个平台都关闭 | D37：gitcode/github pkce_enabled 改为 true，_DEFAULT_PROVIDERS + oauth-providers.json 同步（已修复） |
| 迁移脚本重复 | migrate_grape_accounts.py + migrate_user_accounts_to_grape.py 两个脚本职责重叠，逻辑碎片化 | D38：重写为统一方法，每行全量迁完才标记 migrated=1，三个幂等原语 NOT EXISTS 检查，删除重复脚本（已修复） |
| OAuth 注销 state 消费两次 | callback validate_oauth_state 消费 state 后重定向传原 state，delete_account 再查时已删除 → 纯 OAuth 用户无法注销 | D39 P0-1：认证通过后生成新一次性 state 写入 DB，重定向传新 state（已修复） |
| 禁用用户邮箱登录提示错误 | get_grape_account_by_email AND status=1，禁用用户查不到 → 提示"账号或密码错误"而非"账号已被禁用" | D39 P1-1：去掉 AND status=1，由调用方检查 status 给出正确提示（已修复） |
| 登录锁定时长不正确 | ACCOUNT_WINDOW=300s 统计窗口，达到阈值后依赖窗口滑动自动解锁，实际锁定最多 5 分钟而非 15 分钟 | D39 P1-2：_get_lock_until 取第 N 条失败记录时间 + LOCK_DURATION，独立于统计窗口（已修复） |
| 注册开关默认开放 | 需求要求默认关闭，但 _is_allow_signup 未配置时返回 True | D39 P1-3：未配置时返回 False（已修复） |
| 验证码发送顺序 | send_code 先写 DB 后发邮件，邮件失败时验证码已落库 | D39 P1-5：先发邮件成功后再写 DB（已修复） |
| danger tab profile 未加载 | 直接通过 hash 路由进入 danger tab 时 _accountProfile 为 null | D39 P1-8：_accountRenderDanger 先加载 /auth/me 再渲染（已修复） |
| 多平台仓库只显示一个 | _get_user_provider_and_token fallback 逻辑 GitHub 优先，不传 provider 时只拉一个平台 | D40：list_member_repos 不传 provider 时拉取全部绑定平台仓库合并返回（按 repo_key 去重）（已修复） |
| 创建项目总是去 GitHub | create_from_template 不支持前端传 provider，走 GitHub 优先逻辑 | D40：CreateTemplateRequest 新增 provider 字段 / 前端 _buildPlatformSelect 平台选择下拉框 / doCreateFromTemplate 传 provider（已修复） |
| 多平台拉取互相覆盖 | save_user_repos 每次先 DELETE 再 INSERT，多平台各调一次导致后者覆盖前者 | D40：_fetch_and_cache 只拉取不写缓存，合并去重后一次性 save_user_repos（已修复） |
| 一个平台失败阻断全部 | 服务器连不上 github.com 时 _fetch_and_cache 抛异常，整个 try 块跳到 except，gitcode 结果也丢失 | D40：每平台独立 try/catch，一个失败只 warning 跳过，全部失败才报 502（已修复） |
| D35 创建 _2 重复账号 | OAuth 登录时 external_login 已是 grape_accounts 用户名，_generate_username 冲突加后缀创建新账号 | D40：自动创建前先查 external_login 是否已有同名活跃账号，有则直接绑定不创建（已修复） |
| chat.py acc 未定义 | D32 删除了 acc = user_db.get_user_account() 但保留 _build_web_user_info(user, acc) 引用，5 处 NameError | D41：改为 acc = _get_acc(user["username"]) or {} 从 grape_accounts 读取（已修复） |
| session_manager db 引用缓存 | grape_account_db 模块级 from session_manager import db 在 importlib 测试重载后指向临时 DB | D41：update_unified_user_info 改为直接用本模块 db 查询 + _restore 保存/恢复 db 引用（已修复） |
| 缓存 TTL 永不过期 | synced_at 存 MySQL 本地时间(CST)，Python 按 UTC 解析，age 为负数 | D42：去掉 timezone.utc 用本地时间比较（已修复） |
| 解绑后仓库列表残留 | unbind_provider 只删 external_identities 不删 user_repos | D42：加 DELETE FROM user_repos WHERE repo_key LIKE 'provider:%'（已修复） |
| 解绑后前端 bindings 不刷新 | localStorage.user.bindings 不更新，仍调 /repos/member token 已删 | D42：后端返回 bindings，前端刷新 localStorage + 清除仓库缓存（已修复） |
| 已删除组织仓库被过滤 | _filter_repos 中 is_org_personal(已删除org) 返回 False，仓库被隐藏 | D42：组织不存在时不过滤（降级为未托管）（已修复） |
| 绑定后 user_repos 不清理 | oauth_callback 绑定分支不清理旧缓存，新平台仓库缺失 | D42+D43：5 条绑定路径统一调 _clear_user_repos_cache（已修复） |
| set_password 未撤销 token | 设置密码后不调 increment_token_version，旧 OAuth token 仍有效 | D43：加 increment_token_version + invalidate_user_cache（已修复） |
| 注销后关联数据残留 | delete_account 只软删除，不清理 user_repos/unified_user_mapping/org_member | D43：加清理 _clear_user_repos_cache + DELETE unified_user_mapping + DELETE org_member（已修复） |
| sqlite_master MySQL 不兼容 | SELECT name FROM sqlite_master WHERE type='table' AND name='X' 不被 MySQLCompatDB 转换 | D43：改为 SELECT 1 FROM grape_accounts LIMIT 1 try/except（已修复） |
| 创建项目平台选择失效 | create_from_template 确定了 provider 但 host_repo 不接收，自己重新选择（密码登录时 github 优先） | D44：host_repo 增加 provider 参数，create_from_template 传入（已修复） |
| 认证页面 UX 不一致 | 登录/注册/重置密码三个页面各自内联样式，与 apple-theme 不对齐；无密码强度、无 blur 验证、无步骤指示器、无密码切换、无障碍缺失（无 aria-live/aria-invalid/role=alert）、输入框 font-size 14px 触发 iOS 放大、按钮非 pill 形状、错误仅 toast 无 inline、OAuth 按钮用 .obtn 语义混乱 | D45：新建 auth.css 共享样式表统一设计语言；三个页面全面重写——Apple pill 按钮 980px + field-input 焦点环 + 密码强度条 + 要求清单 + 步骤指示器 + blur 验证 + aria-live/aria-invalid + SVG 密码切换 + spinner loading + 成功状态面板 + reduced-motion 降级 + coarse-pointer 适配（已修复） |
| API key 可用于账号管理 | require_no_session_token 允许 api_key 访问修改密码/注销/变更邮箱等高敏感操作 | P1-6：新建 require_jwt_only（仅 JWT），10 个高敏感路由改用此依赖；只读路由保持 require_no_session_token（已修复） |
| SHA-256 == 非常量时间 | verify_password 旧算法用 == 比较 SHA-256 hex，存在时序攻击理论可能 | P2-5：改为 hmac.compare_digest 常量时间比较（已修复） |
| set_password 递增 token_version | D43 加了 increment_token_version 但需求12 明确"不触发其他设备下线" | P2-16：遵循需求12，移除 increment_token_version，仅保留 invalidate_user_cache（已修复） |
| OAUTH_REDIRECT_URI 绕过白名单 | get_redirect_uri 中 OAUTH_REDIRECT_URI 环境变量直接 return 不经过 _get_trusted_host 校验 | **D46 P0-1**：对 OAUTH_REDIRECT_URI 的 host 也做 TRUSTED_HOSTS 白名单校验（已修复） |
| SMTPS SSL 证书校验可禁用 | email_service.py 465 端口 use_tls=false 时禁用 check_hostname + verify_mode | **D46 P0-2**：465 端口（SMTPS）始终启用证书校验，use_tls 仅控制 STARTTLS（已修复） |
| OAuth 关联 token user_id=0 | _oauth_associate_redirect 以 user_id=0 签发通用 JWT access_token | **D46 P0-3**：get_current_user 已有 `if not uid` 拒绝 uid=0（已防御） |
| JWT secret 无最小长度校验 | validate_jwt_secret 只做黑名单检查，HS256 要求 secret >= 32 字节 | **D46 P0-4**：添加 len(JWT_SECRET) >= 32 校验（已修复） |
| 登录锁定可被绕过 | _get_lock_until 缺窗口过滤，历史失败记录导致锁定时间计算为过期值 | **D46 P1-13**：SQL 加 AND created_at>=? 窗口过滤（已修复） |
| datetime('now') MySQL 不兼容 | grape_account_db.py 两处使用 SQLite 内建函数 datetime('now')，MySQL 后端报错 | **D46 P1-14**：改用 Python time.strftime 生成时间戳（已修复） |
| 验证码限流 fail-open | _count_recent_sends/_count_recent_ip_requests 异常返回 0，数据库故障时限流失效 | **D46 P1-7**：异常返回超过上限的大值 fail-closed（已修复） |
| 注册白名单 fail-open | _get_whitelist_emails/_get_whitelist_domains 异常返回空 set，白名单形同虚设 | **D46 P1-6**：异常返回 None 拒绝注册（已修复） |
| 凭据解密静默失败 | credential_crypto.decrypt 失败返回密文原值，调用方无法区分明文和密文 | **D46 P1-10**：decrypt 失败抛 ValueError + _safe_decrypt 安全包装（已修复） |
| SMTP 邮件头 CRLF 注入 | msg["To"]/msg["Subject"] 未过滤换行符 | **D46 P1-8**：对 to/subject 做 \r\n 过滤（已修复） |
| access_token 在 URL 参数 | OAuth 登录重定向 access_token 放在 URL 查询参数 | **D46 P1-2**：前端已有 history.replaceState 清除 URL 参数（已防御） |
| JWT get_unverified_claims 异常 | 畸形 token 可能抛非 JWTError 异常导致 500 | **D46 P1-9**：except 改为 (JWTError, ValueError, TypeError)（已修复） |
| 邮箱变更 TOCTOU | change-email 的 is_email_registered 检查与 update_email 之间有窗口 | **D46 P1-1**：update_email 区分 UNIQUE 冲突返回 conflict，路由层报 409（已修复） |
| unbind 竞态 | count_user_identities + has_password + delete 不在同一 db_lock 块 | **D46 P1-3**：delete_external_identity 已有 require_keep_one 原子检查（已防御） |
| 密钥池 fallback 不精确 | _get_verify_secret 未知 kid 也 fallback 到 JWT_SECRET | **D46 P1-5**：有 kid 但未知时返回 None 拒绝验签，无 kid 才 fallback（已修复） |
| 邮件配置缓存无失效 | _email_config_cache 首次加载后永久缓存，改 email.json 不生效 | **D46 P1-11**：加文件 mtime 检查自动重新加载（已修复） |
| GitHub OAuth scope 过大 | scope 含 admin:repo_hook，违反最小权限原则 | **D46 P1-12**：缩减为 read:user user:email（已修复） |
| _get_client_ip 信任 X-Forwarded-For | 直接取 XFF 第一个值，未校验是否经过可信代理 | **D46 P2-1**：仅信任 TRUSTED_PROXY_IPS 白名单内代理的 XFF（已修复） |
| 用户名大小写不一致 | is_username_taken 用 WHERE username=? 区分大小写，保留字检查用 .lower() | **D46 P2-2**：改为 WHERE LOWER(username)=LOWER(?) 大小写不敏感（已修复） |
| login_failures 表无自动清理 | 失败记录只在登录成功/解锁时删除，过期记录不清理 | **D46 P2-3**：cooldown.py 已有 _cleanup_login_failures 定时清理 24h 前记录（已有） |
| 开发环境默认 JWT secret 硬编码 | _DEFAULT_SECRET 固定值，知道源码可伪造开发环境 token | **D46 P2-4**：评估后保留——随机 secret 每次重启变化导致开发环境 token 全失效，影响开发便利性。生产环境门禁已保留（不改） |
| 开发环境凭据密钥全零字节 | credential_crypto 默认密钥为 32 字节 0x00，可解密全部开发环境凭据 | **D46 P2-5**：评估后保留——随机密钥每次重启变化导致已加密凭据无法解密。生产环境门禁已保留（不改） |
| 开发环境明文降级存储凭据 | cryptography 未安装时开发环境直接返回明文 | **D46 P2-6**：评估后保留——开发环境不应强制安装 cryptography。生产环境门禁已保留（不改） |
| expires_at 存储为字符串 | str(_now()+ttl) 存为文本，MySQL 中类型转换可能索引失效 | **D46 P2-7**：改为直接存整数（已修复） |
| 验证码哈希无 salt | SHA-256 直接哈希 6 位数字，可预计算彩虹表 | **D46 P2-8**：改为 HMAC-SHA256 用 JWT_SECRET 做 key（已修复） |
| _cleanup_expired 每次执行 | 每次 send_code 后全表 DELETE 过期行 | **D46 P2-9**：改为 10% 概率执行（已修复） |
| _load_providers 无锁保护 | check-then-act 模式，多线程首次调用可能并发加载 | **D46 P2-10**：加 threading.Lock + double-check（已修复） |
| LOG_ONLY 日志输出验证码明文 | log.info 输出完整 6 位验证码 | **D46 P2-11**：mask 为前 2 位+****（已修复） |
| 日志泄露 Host header 原始值 | raw_host 可含换行符导致日志注入 | **D46 P2-12**：过滤 \r\n（已修复） |

### 10.1 架构优化建议

本节记录架构审查中发现的优化点，本期不阻塞交付，建议在迁移完成后或后续迭代中实施。

#### 10.1.1 迁移期旧表依赖清理（D32，已修复）

**问题根因：** grape_accounts 和 user_accounts 各自使用 MySQL 独立的 AUTO_INCREMENT 主键，id 值范围重叠。业务代码用 grape_accounts 的 id/username 查 user_accounts 老表，查到错误用户或查不到。详见第 2.5 节根因分析。

**修复策略：一次性迁移 + 不 fallback（详见第 2.5 节架构原则）——已实施**

不采用 fallback 模式。改为：先执行迁移脚本把老表数据导入新表，然后业务代码只查新表，不写 fallback 分支。

**第一步：迁移脚本（scripts/migrate_user_accounts_to_grape.py）——已编写，dry-run 验证通过**

幂等迁移脚本，把 user_accounts 数据导入 grape_accounts + external_identities + user_credentials：

```
迁移顺序：先迁移用户数据，再更新关联表 user_id。

=== 阶段 1：迁移用户数据 ===

对 user_accounts 每一行（跳过 username='default'）：
  1. 检查 grape_accounts 是否已有同 username → 有则跳过（幂等）
  2. 在 grape_accounts 创建记录：
     - username: user_accounts.username
     - email: user_accounts 无 email 字段，设为 NULL
     - password_hash: 直接迁移（SHA-256 格式直接搬，verify_password 已有旧算法兼容，
       用户下次登录时自动升级为 bcrypt）
     - nickname: user_accounts.nickname（可能为空）
     - status: 1（正常。已注销/禁用的旧用户按实际情况标记）
  3. 如果 user_accounts.gitcode_id 有值 → 检查 external_identities 是否已有 → 无则创建：
     - grape_user_id: 新创建的 grape_accounts.id
     - provider=gitcode, external_id=gitcode_id
     - external_login: 从 user_accounts.gitcode_login 读取（如有）
     - access_token/refresh_token: 从 user_credentials(user_id, "gitcode_oauth") 读取
  4. 如果 user_accounts.github_id 有值 → 同上
  5. 迁移非 OAuth 业务凭据到 user_credentials 表：
     - huawei_cloud: {ak, sk} 从 user_accounts.huawei_cloud_ak/sk 读取
     - codearts: {ak, sk} 从 user_accounts.CODEARTS_CLI_AK/SK 读取
     - feishu: {app_id, app_secret} 从 user_accounts.feishu_app_id/secret 读取
     - 幂等：已存在则跳过
  6. 记录 id 映射：旧 user_accounts.id → 新 grape_accounts.id

=== 阶段 2：更新关联表 user_id ===

用阶段 1 产生的 id 映射，更新以下表（managed_repos 不需要——已重构为 repo_key 关联，无 user_id 列）：
  6. user_repos: UPDATE user_repos SET user_id=新id WHERE user_id=旧id
  7. chat_conversations: UPDATE chat_conversations SET user_id=新id WHERE user_id=旧id
  8. organization: UPDATE organization SET owner_id=新id WHERE owner_id=旧id
  9. org_member: UPDATE org_member SET user_id=新id WHERE user_id=旧id
```

脚本支持 `--dry-run` 预览迁移内容和 id 映射，`--execute` 实际执行。执行前自动备份 DB。

**字段差异处理（已实施）：**

`get_user_account(username)` 返回 user_accounts 的字段（包含 gitcode_api_token / gitcode_id / nickname / feishu_app_id 等旧字段）。改为 `get_grape_account_by_username(username)` 后返回字段不同（只有 username/email/nickname/avatar_url/status/token_version）。

调用 `get_user_account` 的代码已逐个检查依赖的字段并切换：
- nickname → grape_accounts.nickname 有（auth.py/profile、handler_async.py、session_manager.py 已切换）
- gitcode_api_token → 改为通过 get_user_token(username) 读 external_identities（D30 已修复，D32 删除老表 fallback）
- github_api_token → 改为通过 get_user_github_token(username) 读 external_identities（D32 删除老表 fallback）
- gitcode_id / github_id → 改为通过 get_user_identities(user_id) 读 external_identities
- huawei_cloud_ak/sk → 改为通过 get_credential(user_id, "huawei_cloud") 读 user_credentials 表（settings.py、devstage.py 已切换）
- CODEARTS_CLI_AK/SK → 改为通过 get_credential(user_id, "codearts") 读 user_credentials 表（settings.py 已切换）
- feishu_app_id/secret → 改为通过 get_credential(user_id, "feishu") 读 user_credentials 表（迁移脚本同步）

**第二步：业务代码只查新表（不写 fallback）——已实施（14 文件修改）**

**第一类：用 username 查 user_accounts 老表（14 处）——已改为查 grape_accounts**

| 位置 | 文件 | 修复（已实施） |
|------|------|------|
| get_profile | auth.py:74 | `get_grape_account_by_username(username)` 替代 `get_user_account(username)` |
| 凭据读取 | settings.py:37 | 改为从 external_identities + user_credentials 表读，不查 user_accounts |
| 凭据写入 | settings.py:91 | 改为写 user_credentials 表 + external_identities，不写 user_accounts |
| Chat 配置 | chat.py:533,697,1033,1399,1763 | 5 处死代码（acc 赋值后未使用）已删除 |
| DevStage AK/SK 读 | devstage.py:26 | `get_credential(user["id"], "huawei_cloud")` 替代 `get_user_account(username)` |
| DevStage 配置读 | devstage.py:82 | 同上 |
| DevStage AK/SK 写 | devstage.py:102 | `set_credential(user["id"], "huawei_cloud", cred)` 替代 `save_user_account(username, acc)` |
| get_user_token | deps.py:274 | 只从 external_identities 读，删除老表 fallback |
| get_user_github_token | deps.py:279 | 只从 external_identities 读，删除老表 fallback |
| get_user_token_with_refresh | deps.py:323 | 删除老表 fallback，直接尝试 token 刷新 |
| handler_async | handler_async.py:859 | `get_grape_account_by_id(uid)` 替代 `get_user(uid)` + `get_user_account(username)` |
| session_manager | session_manager.py:446 | `get_grape_account_by_username(gitcode_user)` 替代 `get_user_account(gitcode_user)` |
| executor | executor.py:247 | 删除 github_api_token 老表 fallback（get_user_github_token 已从 external_identities 读） |
| token 刷新 | user/db.py:1635,2120 | `update_external_identity_tokens()` 替代写 user_accounts.gitcode_api_token |

**第二类：用 user_id 查 user_accounts 老表（19 处）——已改为查 grape_accounts/external_identities**

| 函数 | 调用位置 | 修复（已实施） |
|------|---------|------|
| get_managed_repos(user["id"]) | 9 处 | managed_repos 已无 user_id 列（重构为 repo_key + org_id），代码不变——迁移脚本不需要更新此表 |
| get_user(user["id"]) | deps.py:153, repos.py:159/1290/1309, grape_auth.py:1023, git_repo_api.py:111 | `get_grape_account_by_id(uid)` 替代 `get_user(uid)` |
| get_credential(user["id"], ...) | 4 处 | 改为查 external_identities（D30 的 _get_oauth_token_from_identity 已覆盖 token 读取）。非 OAuth 凭据（AK/SK 等）改为查 user_credentials 表 |
| increment_token_version(user["id"]) | auth.py:101 | `grape_inc(user["id"])` 替代 `user_db.increment_token_version()`，删除双写 |
| get_token_version(uid) | deps.py:90 | `get_status_and_token_version(uid)` 替代 `user_db.get_token_version()`，无用户时返回 status=0（禁用） |
| organization.owner_id | 迁移脚本更新 | 阶段 2 第 8 步：UPDATE organization SET owner_id=新id WHERE owner_id=旧id |
| org_member.user_id | 迁移脚本更新 | 阶段 2 第 9 步：UPDATE org_member SET user_id=新id WHERE user_id=旧id |
| user_repos.user_id | 迁移脚本更新 | 阶段 2 第 6 步：UPDATE user_repos SET user_id=新id WHERE user_id=旧id |
| chat_conversations.user_id | 迁移脚本更新 | 阶段 2 第 7 步：UPDATE chat_conversations SET user_id=新id WHERE user_id=旧id |

**第三类：登录/改密码 fallback 到老表（3 处）——已删除 fallback 分支**

| 位置 | 文件 | 修复（已实施） |
|------|------|------|
| login fallback | grape_auth.py | 删除 `user_db.verify_user()` fallback 分支，只走 `verify_grape_account()` |
| change_password fallback | grape_auth.py | 删除老表 update_password/increment_token_version/get_token_version fallback，只走 grape_accounts |
| oauth_associate fallback | grape_auth.py | 删除 `old_db.verify_user()` fallback 分支，只走 `verify_grape_account()` |
| oauth_callback fallback | grape_auth.py | 删除 `old_db.get_user()` + `old_db.get_token_version()` fallback，grape_accounts 无用户直接报错 |
| get_me fallback | grape_auth.py | 删除 `user_db.get_user()` fallback，grape_accounts 无用户直接 404 |
| update_profile fallback | grape_auth.py | 删除迁移期静默成功逻辑 |

**第三步：删除 _sync_legacy_binding——已删除**

`_sync_legacy_binding` 函数和其 3 处调用已全部删除——external_identities 已有完整绑定数据，不需要同步到老表。

**第四类：深度扫描遗漏（D32-补丁，已修复）**

第一轮修复覆盖了业务代码中通过函数调用间接查老表的路径（get_user_account/get_user/fallback）。但深度扫描发现以下两类遗漏：

**A. user/db.py 内部函数仍查老表（P0）**

第一轮只改了"调用方"——deps.py 的 get_user_token/get_user_github_token 已改为读 external_identities。但 user/db.py 内部还有一套同名/类似函数仍在查老表，被其他模块直接调用：

| 函数 | user/db.py 行 | 查询 | 调用方 | 修复方案 |
|------|------|------|--------|---------|
| get_credentials(user_id) | 363 | SELECT USER_ACCOUNT_FIELDS FROM user_accounts WHERE id=? | runtime/service.py:464（凭据注入）、executor.py:278（凭据注入）、user/db.py:1545/1559 | 改为从 user_credentials 表 + external_identities 读（复用 get_credential 按 provider 遍历） |
| get_user_gitcode_token(user_id) | 1538 | 内部调 get_credentials | handler_async.py:1109（IM chat token） | 改为调 deps.py 的 get_user_token(username)，或直接从 external_identities 读 |
| get_user_github_token(user_id) | 1553 | 内部调 get_credentials | issues_center.py:1128/1250（Issue token） | 改为调 deps.py 的 get_user_github_token(username)，或直接从 external_identities 读 |
| get_github_token(repo_key) | 1487 | SELECT github_api_token FROM user_accounts WHERE username=owner | feishu_async.py:717、vcs/api_client.py:230 | 按 repo owner username 查 grape_accounts → external_identities 读 GitHub token |
| _get_username_by_id(user_id) | 1572 | SELECT username FROM user_accounts WHERE id=? | user/db.py 内部（token 刷新日志） | 改为 get_grape_account_by_id(user_id).username |

**关键问题：deps.py 和 user/db.py 有两套同名函数**

deps.py 的 get_user_github_token(username) 已改为从 external_identities 读（D30+D32）。但 user/db.py 的 get_user_github_token(user_id) 仍从老表读。调用方按 import 路径区分：
- `from src.api.deps import get_user_github_token` → 走 external_identities ✓
- `from src.user.db import get_user_github_token` → 走老表 ✗

issues_center.py:1128 和 handler_async.py:1109 import 的是 user/db.py 版本，所以仍在读老表。

**修复方案：**
1. user/db.py 的 get_credentials(user_id) 改为从 user_credentials 表遍历 + external_identities 读 token（不再 SELECT FROM user_accounts）
2. user/db.py 的 get_user_gitcode_token/get_user_github_token 改为从 external_identities 读（或直接委托给 deps.py 同名函数）
3. user/db.py 的 get_github_token(repo_key) 中查 owner 的 github_api_token 改为按 owner username 查 grape_accounts → external_identities
4. user/db.py 的 _get_username_by_id 改为查 grape_accounts

**B. 业务代码直接 SQL 查 user_accounts（P1）**

这些是直接在 SQL 语句中引用 user_accounts 表的代码，不走 user/db.py 函数封装：

| 文件 | 行 | SQL 用途 | 修复方案 |
|------|------|---------|---------|
| orgs.py | 142 | 搜索用户 SELECT id, username, nickname FROM user_accounts LIKE | 改为 SELECT FROM grape_accounts |
| orgs.py | 173 | get_user_by_username 查老表（添加组织成员） | 改为 get_grape_account_by_username |
| orgs.py | 184 | SELECT id, username FROM user_accounts WHERE id=?（检查用户存在） | 改为 SELECT FROM grape_accounts |
| runtime.py | 152 | SELECT id FROM user_accounts WHERE username=?（owner_id 对齐） | 改为 SELECT id FROM grape_accounts |
| runtime/service.py | 1347 | SELECT id FROM user_accounts WHERE username=?（任务 claim） | 改为 SELECT id FROM grape_accounts |
| engine_select.py | 175 | SELECT id FROM user_accounts WHERE username=? OR gitcode_id=? | 改为 SELECT id FROM grape_accounts + JOIN external_identities |
| engine_select.py | 184 | JOIN user_accounts（user_repos 回退） | JOIN grape_accounts |
| events.py | 581,784 | SELECT username FROM user_accounts WHERE id=?（scope 子查询） | 改为 SELECT username FROM grape_accounts |
| settings.py | 399,759 | SELECT username FROM user_accounts（有效用户集合） | 改为 SELECT username FROM grape_accounts |
| session_manager.py | 228-235 | INSERT INTO unified_user_mapping SELECT FROM user_accounts（backfill） | 改为 SELECT FROM grape_accounts |

**C. config.py 账户加载（P1）**

| 文件 | 行 | 用途 | 修复方案 |
|------|------|------|---------|
| config.py | 208 | _load_accounts() 调 get_all_user_accounts() 从老表加载到 ACCOUNTS 字典 | 改为从 grape_accounts 加载（或评估 ACCOUNTS 字典是否仍有必要——可能已被 DB 查询取代） |
| config.py | 269 | save_accounts() 调 save_user_account/delete_user_account 写老表 | 改为写 grape_accounts（或废弃 save_accounts 如果无调用方） |
| api/main.py | 795 | 启动时调 _load_accounts() | 跟随 config.py 改动 |
| run_webhook.py | 14 | 启动时调 _load_accounts() | 跟随 config.py 改动 |

**修复优先级：**

| 优先级 | 修复项 | 状态 |
|--------|--------|------|
| P0 | 迁移脚本阶段 1（用户数据 + external_identities + 业务凭据） | ✓ 已编写，dry-run 验证通过 |
| P0 | 迁移脚本阶段 2（关联表 user_id 更新） | ✓ 已编写，dry-run 验证通过 |
| P0 | get_token_version / increment_token_version | ✓ 已切换到 grape_accounts |
| P0 | 删除 login/change_password fallback | ✓ 已删除 |
| P0 | get_user_account(username) 14 处 → get_grape_account_by_username | ✓ 已实施 |
| P0 | get_user(user_id) 4 处 → get_grape_account_by_id | ✓ 已实施 |
| P0 | get_user_token_with_refresh 删除 user_accounts fallback 分支 | ✓ 已删除 |
| P0 | 删除 _sync_legacy_binding + 3 处调用 | ✓ 已删除 |
| P0 | user/db.py get_credentials 改为读 user_credentials + external_identities | ✓ 已实施 |
| P0 | user/db.py get_user_gitcode_token/get_user_github_token 改为读 external_identities | ✓ 已实施（通过 get_credentials 自动生效） |
| P0 | user/db.py get_github_token(repo_key) 查 owner token 改为查 external_identities | ✓ 已实施 |
| P0 | user/db.py _get_username_by_id 改为查 grape_accounts | ✓ 已实施 |
| P1 | orgs.py 3 处直接 SQL 改为查 grape_accounts | ✓ 已实施 |
| P1 | runtime.py/runtime/service.py/engine_select.py 4 处 SQL 改为查 grape_accounts | ✓ 已实施 |
| P1 | events.py 2 处 scope 子查询改为查 grape_accounts | ✓ 已实施 |
| P1 | settings.py 2 处有效用户集合改为查 grape_accounts | ✓ 已实施 |
| P1 | session_manager.py backfill 改为从 grape_accounts 读 | ✓ 已实施 |
| P1 | config.py _load_accounts/save_accounts 改为查 grape_accounts 或评估废弃 | ✓ 已实施（ACCOUNTS 死代码废弃，保留 webhook/server 配置） |
| P0 | user/db.py batch_update_last_active 改为 UPDATE grape_accounts | ✓ 已实施 |
| P1 | user/db.py link_user_provider 删除 UPDATE user_accounts 老表写入 | ✓ 已实施（死函数，无外部调用方） |

#### 10.1.2 get_redirect_uri meta 表查询无缓存

**现状：** providers.py get_redirect_uri() 每次调用都通过 get_meta_value() 查询 meta 表读取 `oauth_{provider}_redirect_uri`，无内存缓存。

**影响：** 每次 OAuth 登录请求多一次 DB 查询。OAuth 登录非高频操作，影响可接受。

**优化方案：** get_meta_value 增加 TTL 60s 内存缓存，或 get_redirect_uri 结果缓存到模块级变量（provider + host 维度缓存）。

#### 10.1.3 get_grape_user_detail 多事务查询

**现状：** admin/db.py get_grape_user_detail() 分 3 个独立 db_lock 事务查询：
1. SELECT grape_accounts（事务 1）
2. get_user_identities()（事务 2）
3. has_password()（事务 3）

**影响：** 用户详情页可能出现短暂数据不一致（查询过程中用户解绑了平台）。但详情页是只读展示，不一致窗口极短，可接受。

**优化方案：** 如需严格一致，可在单个 db_lock 内完成 3 个查询。但会延长锁持有时间，需权衡。

#### 10.1.4 验证码并发控制设计确认

**现状：** verification.py verify_code() 在 db_lock 外执行 hmac 比较 + _mark_used/_increment_attempts。_mark_used 用原子 `UPDATE ... WHERE used=0` 保证并发安全。

**确认：** 设计正确。两个请求同时验证同一验证码时，都查到 used=0，都通过 hmac 比较，但 _mark_used 的原子 UPDATE 只有一个成功，另一个返回 False 被拒绝。无需修改。

#### 10.1.5 audit_logs 哈希链与 180 天清理的兼容性

**现状：** D24 修复中 cooldown.py 清理 180 天前 audit_logs 记录。审计日志使用哈希链（每条记录 prev_hash 指向前一条 curr_hash）。

**确认：** 删除旧日志不影响新日志的链式完整性。新日志的 prev_hash 指向删除前最后一条的 curr_hash，删除后该 curr_hash 仍在 prev_hash 字段中保留——链式验证时遇到 prev_hash 指向不存在的记录即视为链起点（与空表首条记录行为一致）。无需修改。

#### 10.1.6 _sync_legacy_binding 安全性确认（已删除）

**历史现状：** _sync_legacy_binding 曾写入旧表 user_accounts 的 gitcode_id/github_id 字段和 user_credentials。

**D32 修复：** _sync_legacy_binding 函数和 3 处调用已全部删除。external_identities 已有完整绑定数据，不需要同步到老表。双写完全停止。

---

## 11. 未来扩展方向

### 11.1 微信登录

在 configs/oauth-providers.json 添加微信平台配置：

```json
"wechat": {
    "name": "微信",
    "auth_url": "https://open.weixin.qq.com/connect/qrconnect",
    "token_url": "https://api.weixin.qq.com/sns/oauth2/access_token",
    "user_url": "https://api.weixin.qq.com/sns/userinfo",
    "scope": "snsapi_login",
    "id_field": "openid",
    "login_field": "openid",
    "name_field": "nickname",
    "pkce_enabled": false,
    "brand_color": "#07c160",
    "whitelist_column": "wechat_username",
    "accept_header": "application/json",
    "client_id": "...",
    "client_secret": "..."
}
```

**微信 openid vs unionid 设计差异：**

微信平台的 id_field 是 `openid`，但 openid 是按应用隔离的——同一用户在不同微信开放平台应用下 openid 不同。如果 Grape 需要支持多个微信应用（如网站扫码登录 + 小程序），必须使用 `unionid` 而非 `openid` 作为 external_identities 的 external_id，否则同一用户在不同应用下会被识别为不同身份。

接入微信时需扩展 providers.py 的 user_url 解析逻辑：
- 微信 user_url 返回 openid + unionid（如果应用绑定到开放平台）
- external_id 应取 unionid（跨应用唯一），openid 存入 metadata
- 若应用未绑定开放平台（无 unionid），降级使用 openid，但需在 metadata 中标注

### 11.2 华为账号登录

在 configs/oauth-providers.json 添加华为平台配置：

```json
"huawei": {
    "name": "华为",
    "auth_url": "https://oauth-login.cloud.huawei.com/oauth2/v3/authorize",
    "token_url": "https://oauth-login.cloud.huawei.com/oauth2/v3/token",
    "user_url": "https://api.vmall.com/rest.php?nsp_svc=OpenID.User.getInfo",
    "scope": "https://www.huawei.com/auth/account/base.profile",
    "id_field": "userID",
    "login_field": "userID",
    "name_field": "displayName",
    "pkce_enabled": false,
    "brand_color": "#cf0a2c",
    "whitelist_column": "huawei_username",
    "accept_header": "application/json",
    "client_id": "...",
    "client_secret": "..."
}
```

华为账号的 userID 是全局唯一的，无需特殊处理。scope 需配置 base.profile 权限。

### 11.3 SSO / OIDC 企业对接

企业内部 SSO 对接（OIDC 协议）：

- 标准 OIDC 平台通过 oauth-providers.json 配置接入，auth_url / token_url 指向企业 IdP
- id_field 统一使用 `sub`（OIDC 标准 subject claim）
- user_url 替换为 UserInfo Endpoint
- PKCE 强制启用（企业安全要求）
- 需扩展 providers.py 支持 OIDC Discovery（从 `.well-known/openid-configuration` 自动发现端点）

### 11.4 MFA 双因素认证

- **TOTP（时间一次性密码）**：接入 Google Authenticator / Authy，用户绑定 TOTP 密钥后登录需输入 6 位动态码
- **短信验证码**：手机号 + 短信网关（接入阿里云/腾讯云短信服务），复用现有 verification.py 验证码系统，新增 `mfa_sms` 场景
- **实现思路**：grape_accounts 新增 `mfa_enabled` 和 `mfa_secret` 列（_ensure_column 幂等补列），登录流程在密码校验通过后增加 MFA 验证步骤

### 11.5 账号合并

多个 Grape 账号合并场景（用户用不同邮箱注册了两个账号，需合并为一个）：

- 管理员发起合并操作：指定主账号 + 从账号
- 从账号的 external_identities 绑定关系迁移到主账号
- 从账号的个人组织、任务、凭据等数据迁移到主账号
- 从账号标记为 status=2（已注销），username 改为 `merged_{id}`
- 合并操作记录审计日志
- 需新增管理员 API：`POST /api/admin/users/merge`（主账号 ID + 从账号 ID + 确认）

### 11.6 短信验证

手机号注册/登录扩展方向：

- grape_accounts 新增 `phone` 列（_ensure_column 幂等补列）
- verification.py 新增 `sms` 发送通道（复用 verification_codes 表，email 字段存手机号）
- 新增 SMS 服务模块（类似 email_service.py，接入阿里云/腾讯云短信 API）
- 注册流程增加手机号 + 短信验证码模式
- 手机号唯一性约束（同 email 模式，NULL 不参与唯一约束）

---

## 12. 集群部署场景设计

### 12.1 问题分析

Grape 账号体系涉及 5 个进程内状态，在多实例部署下存在不同步风险：

| # | 进程内状态 | 文件 | 多实例风险 | 影响 | 当前是否可接受 |
|---|-----------|------|-----------|------|---------------|
| 1 | _user_status_cache | deps.py | 禁用/恢复操作只清当前实例缓存，其他实例 5 秒 TTL 后自然过期 | 被禁用用户在其他实例最多 5 秒内仍可访问 | 可接受（需求 10 允许 5 秒延迟） |
| 2 | cooldown 定时任务 | cooldown.py | 多实例同时执行，Step 1 原子 UPDATE 保证幂等 | 重复查询浪费资源，功能正确 | 可接受（幂等设计） |
| 3 | audit_logs _next_seq | audit.py | SELECT MAX(seq)+1 多实例并发可能得到相同 seq，哈希链断裂 | **审计日志哈希链完整性破坏** | 不可接受 |
| 4 | _pending_active | deps.py | 每实例独立记录活跃用户，last_active_at 不精确 | last_active_at 可能不准确 | 可接受（辅助字段） |
| 5 | credential_crypto _keys | credential_crypto.py | 每实例独立从环境变量加载密钥 | 无影响（多实例共享环境变量） | 可接受 |

### 12.2 修复方案

#### 12.2.1 audit_logs seq 并发安全（P0，必须修复）

**问题根因：** _next_seq() 用 SELECT MAX(seq)+1，多实例并发时可能得到相同 seq 值。seq 无 UNIQUE 约束，重复 seq 不会报错但导致哈希链断裂（两条记录 prev_hash 指向同一条）。

**方案：seq UNIQUE 约束兜底 + INSERT 重试**

1. db_setup.py audit_logs DDL 中 seq 改为 `UNIQUE`
2. audit.py log_audit 增加 INSERT 重试逻辑（最多 3 次）：
   - 计算 seq（SELECT MAX(seq)+1）+ prev_hash（SELECT curr_hash ORDER BY seq DESC LIMIT 1）
   - INSERT
   - 如果 UNIQUE 冲突（IntegrityError），重新获取 seq + prev_hash，重试

**为什么不直接用 DB AUTOINCREMENT 替代 seq：**
seq 是业务序号，用于哈希链排序和 180 天清理后保持连续。id 是物理自增主键，D24 清理 180 天前记录后 id 会断续。seq UNIQUE + 重试方案在无冲突时零开销，冲突时才重试。

**为什么不加 distributed_lock：**
审计日志是高频操作（每次登录/注册/改密码都写），加分布式锁会显著增加延迟。SELECT-then-INSERT 重试方案在无冲突时零开销。

#### 12.2.2 cooldown 定时任务分布式锁互斥（P1，优化）

**方案：复用已有 src/lock.py 分布式锁**

run_cooldown_cleanup() 开头 acquire_lock("cooldown_cleanup", ttl=600)，执行完 release_lock。

项目已有 distributed_locks 表 + src/lock.py 实现（INSERT 冲突 + 过期抢占），已用于 upload_cleanup.py 定时任务互斥。直接复用，无需新建基础设施。

效果：只有一个实例执行清理，避免多实例重复查询和重复执行。

#### 12.2.3 _user_status_cache 跨实例失效（不实施）

当前：invalidate_user_cache 只清当前实例，其他实例 5 秒 TTL 后自然过期。需求 10 明确允许 5 秒延迟，当前设计满足需求。

约束：需求文档第 1043 行明确"不引入 Redis 等新基础设施"。跨实例缓存失效需要 pub/sub 机制（Redis/消息队列），不在本期范围。

后续优化方向（不实施）：
- 方案 A：Redis pub/sub 通知（需引入 Redis）
- 方案 B：缩短 TTL 到 1 秒（增加 DB 查询频率）
- 方案 C：DB 事件通知（MySQL trigger 或 application-level event）

#### 12.2.4 _pending_active 跨实例（不修改）

每实例独立记录活跃用户，独立更新 last_active_at。功能正确，last_active_at 可能不是最新值但可接受（需求 21 辅助字段，不要求精确到秒）。

#### 12.2.5 credential_crypto 密钥（不修改）

多实例共享相同环境变量 GRAPE_CREDENTIAL_KEYS，加解密行为一致。设计正确。

### 12.3 实施计划

| 优先级 | 修改项 | 文件 | 状态 |
|--------|--------|------|------|
| P0 | audit_logs seq UNIQUE + INSERT 重试 | db_setup.py + audit.py | 本期实施 |
| P1 | cooldown 分布式锁 | cooldown.py | 本期实施 |
| P2 | _user_status_cache 跨实例 | 不实施（约束不引入 Redis） | 后续迭代 |
| P2 | _pending_active 跨实例 | 不修改（可接受） | — |
| P2 | credential_crypto | 不修改（正确） | — |

---

## 13. OAuth 授权弹窗模式（2026-10-13）

### 13.1 背景

OAuth 绑定/登录原流程为整页跳转（`window.location.href = redirect_url`），用户被跳到第三方平台授权页，授权后 302 重定向回前端。问题：用户离开当前页面，丢失操作上下文；登录后整页加载较慢。

改为弹窗模式：`window.open()` 打开弹窗完成授权，弹窗通过 `postMessage` 通知父窗口结果后关闭，父窗口调后端接口确认最终状态。

### 13.2 安全设计（核心）

| 安全要点 | 实现方式 |
|----------|---------|
| 父窗口不信任 postMessage 的 success | 收到消息后调后端接口确认最终状态（绑定调 GET /auth/me，登录调 POST /auth/exchange） |
| binding_ticket 不走 postMessage | 票据留在服务端 binding_tickets 表，与用户 JWT 绑定；父窗口收到 need_transfer 信号后调 GET /auth/transfer-context 获取 |
| postMessage targetOrigin 显式指定 | `window.opener.postMessage(result, window.location.origin)`，不用 `*` |
| 父窗口 origin 校验 | `event.origin !== window.location.origin` 时忽略 |
| exchange_code 安全 | 一次性、10s 过期，通过 postMessage 传递可接受（即使泄露也很快失效） |
| 弹窗和父窗口同源 | 回调页是 Grape 站点自身（/oauth-popup-callback.html），cookie 共享 |
| message handler 幂等 | `handled` 标志位，只处理一次；处理完立即 removeEventListener |

### 13.3 流程

```
1. 前端调 /auth/bind/{provider}?popup=true → 后端生成 state（含 popup:true + binding_user_id）
2. window.open(redirect_url) 打开弹窗
   - 返回 null（被拦截）→ 重新请求非 popup state → window.location.href 降级
3. 弹窗走 OAuth → 第三方回调到后端 /api/auth/oauth/callback
4. 后端检测 state 中 popup 标记 → 302 重定向到 /oauth-popup-callback.html（带原参数）
5. 中间页检测 window.opener：
   - 有 opener → postMessage 结果 → window.close()
   - 无 opener → 降级跳转首页（带原参数，走原有流程）
6. 父窗口 message handler：
   - 校验 event.origin
   - 绑定成功 → 调 GET /auth/me 确认 → 刷新 + toast
   - 需要转移 → 调 GET /auth/transfer-context 获取票据 → 展示转移确认
   - 登录成功 → 调 POST /auth/exchange 换 JWT
   - 登录未绑定 → 跳转注册/关联页
   - 失败 → toast
7. 超时 120s → 提示用户重试
```

### 13.4 后端改动

**`_build_redirect_url` 新增 popup 参数：**
- `popup=True` 时 path 改为 `/oauth-popup-callback.html`
- `popup=False`（默认）保持原有路径，向后兼容

**三个发起接口新增 popup query 参数：**
- `GET /auth/bind/{provider}?popup=true` → state 携带 `popup: True`
- `GET /auth/login/{provider}?popup=true` → state 携带 `popup: True`
- `GET /auth/delete-account/oauth/{provider}?popup=true` → state 携带 `popup: True`

**`oauth_callback` 主函数：**
- 从 `state_data` 读取 `popup` 标记
- 传给所有分支函数（`_handle_oauth_delete` / `_handle_oauth_bind` / `_handle_oauth_login`）
- 各分支函数传给 `_build_redirect_url` / `_oauth_error_redirect` / `_bind_error_redirect` / `_oauth_login_redirect` / `_oauth_associate_redirect`

**`_handle_oauth_bind` popup 模式特殊处理：**
- 转移场景：popup 模式只传 `need_transfer` + `provider`，不传 `binding_ticket`（非 popup 模式仍传 `binding_ticket` 在 URL，向后兼容）

**新增 `GET /auth/transfer-context` 接口：**
- JWT 认证（`require_jwt_only`）
- 调 `get_pending_transfer_context(user_id)` 查当前用户最近的待转移票据（只读，不消费）
- 返回 provider / external_login / external_name / external_avatar / binding_ticket
- 票据与 JWT session 绑定，不暴露到 URL / Referer / 浏览器历史

**新增 `get_pending_transfer_context` 函数（grape_account_db.py）：**
- 按 `initiating_user_id` 查最近的未过期票据
- 只读查询，不消费（消费仍由 `validate_binding_ticket` 在 `POST /auth/transfer-bind` 时完成）

### 13.5 前端改动

**新增 `oauth-popup-callback.html` 中间页：**
- 极简 HTML，只有一段 JS
- 从 URL 参数解析结果（bind_success / bind_error / need_transfer / exchange_code / oauth_error / oauth_associate / delete_oauth_state）
- 构造 `{type: 'oauth_result', action, success, ...}` 对象（不含 access_token / binding_ticket）
- `history.replaceState` 清理 URL 参数
- 检测 `window.opener`：有 → postMessage + close；无 → 降级跳转首页
- 响应头 `Cache-Control: no-store` + `Referrer-Policy: no-referrer`

**`account.js` `_accountDoOauthBind` 改为 popup 模式：**
- 调 `API.get('/auth/bind/' + provider + '?popup=true')`
- `window.open()` 打开弹窗
- 注册一次性 message handler（幂等 + cleanup + 超时 120s）
- 绑定成功 → `_accountConfirmBindSuccess` 调 GET /auth/me 确认
- 需要转移 → `_accountFetchTransferContext` 调 GET /auth/transfer-context 获取票据
- 弹窗被拦截 → 回退到非 popup state + `window.location.href`

**`app.js` `oauthLogin` 改为 popup 模式：**
- 调 `API.get('/auth/login/' + provider + '?popup=true')`
- `window.open()` 打开弹窗
- 登录成功 → `_doOAuthExchange` 调 POST /auth/exchange 换 JWT
- 未绑定 → 跳转注册/关联页
- 已登录态转绑定成功 → `_doOAuthBindConfirm` 调 GET /auth/profile 确认
- 弹窗被拦截 → 回退到非 popup state + `window.location.href`

**`main.py` 新增 `/oauth-popup-callback.html` 路由：**
- 在 SPA catch-all 之前注册
- 返回中间页 HTML，带 `Cache-Control: no-store` + `Referrer-Policy: no-referrer`

### 13.6 降级兼容

| 场景 | 降级方式 |
|------|---------|
| 弹窗被浏览器拦截 | 重新请求非 popup state → window.location.href 整页跳转 |
| 弹窗中 window.opener 为 null | 中间页降级跳转首页，带原 URL 参数，走原有流程 |
| 非 popup 模式（旧流程） | 完全保留，state 不含 popup 标记，回调 302 重定向到首页 |

### 13.7 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/routes/grape_auth.py | `_build_redirect_url` 新增 popup 参数；所有回调分支函数加 popup 参数；三个发起接口加 popup query 参数；新增 GET /auth/transfer-context 接口 |
| src/auth/grape_account_db.py | 新增 `get_pending_transfer_context` 函数（只读查询待转移票据） |
| src/api/main.py | 新增 `/oauth-popup-callback.html` 路由 |
| src/api/static/oauth-popup-callback.html | 新增中间页（postMessage + 降级跳转） |
| src/api/static/js/app.js | `oauthLogin` 改为 popup 模式；新增 `_doOAuthExchange` / `_doOAuthBindConfirm`；app.js v159 |
| src/api/static/js/pages/account.js | `_accountDoOauthBind` 改为 popup 模式；新增 `_accountConfirmBindSuccess` / `_accountFetchTransferContext` / `_accountTranslateOAuthError`；account.js v16 |
| src/api/static/index.html | account.js v15→v16, app.js v158→v159 |
| src/api/static/locales/zh/app.json | 新增 popup_blocked_hint / oauth_timeout_hint；更新 oauth_redirect_hint 文案 |
| src/api/static/locales/en/app.json | 同上英文 |

### 13.8 中间页降级体验修复（2026-10-13）

**问题：** 弹窗 OAuth 流程经历跨域重定向（gitcode.com → grape.com），部分浏览器在跨域导航后清除 `window.opener`。中间页检测到 `window.opener` 为 null 后走降级路径跳转首页，但首页在弹窗中加载，用户看到弹窗里出现一个完整页面不知道怎么办——"页面卡死"。

**修复：** 中间页 `window.opener` 为 null 时不再跳转首页，改为显示一个有操作指引的提示页面：
- 成功：显示"授权已完成，请关闭此窗口返回主页面"
- 需要转移：显示"请关闭此窗口，在主页面的账号设置中查看转移确认"
- 需要关联：显示"请关闭此窗口，在主页面完成注册或关联"
- 失败：显示错误信息 + "请关闭此窗口重试"
- 提供"关闭窗口"按钮（调 `window.close()`）

`window.close()` 被浏览器限制不生效时，`setTimeout` 检测 `!window.closed` 后显示可关闭提示。

### 13.9 GitCode 会话冲突 — prompt=login 强制重新认证

详见第 14 节（OAuth 会话隔离与绑定冲突处理设计）。

### 13.10 仓库缓存污染修复（2026-10-13）

**问题：** 用户绑定 GitHub 后仓库列表只有 GitCode 仓库，GitHub 仓库不出现。

**根因：** `/repos/member` 接口多平台拉取仓库时，如果某个平台（如 GitHub）网络超时拉取失败，`save_user_repos` 用只含成功平台的数据覆盖缓存。失败平台的仓库从缓存中消失，后续 30 分钟（`REPOS_CACHE_TTL`）内都返回不完整缓存。

**修复：**
- `repos.py`：部分平台拉取失败时，从旧缓存中补入失败平台的仓库（按 `repo_key` 前缀 `github:`/`gitcode:` 匹配），合并后再写入缓存
- `account.js`：绑定成功后调 `app._forceSyncRepos()` → `POST /repos/sync` 强制同步（不走缓存），而非 `loadRepos(true)` 走 `/repos/member` 缓存
- `app.js`：新增 `_forceSyncRepos` 函数，调 `/repos/sync` 接口强制从 VCS 拉取并刷新缓存

**变更文件清单（追加）：**

| 文件 | 变更 |
|------|------|
| src/api/routes/repos.py | 部分平台失败时合并旧缓存中失败平台的仓库 |
| src/api/static/js/pages/account.js | 绑定成功后调 _forceSyncRepos 强制同步；account.js v21 |
| src/api/static/js/app.js | 新增 _forceSyncRepos 函数；app.js v162 |
| src/api/static/index.html | account.js v20→v21, app.js v161→v162 |

### 13.11 占位账号自动合并

详见第 14 节（OAuth 会话隔离与绑定冲突处理设计）。

### 13.12 OAuth 绑定/解绑 UX 统一修复（2026-10-13）

#### 13.12.1 问题分析

审查发现三个 UX 问题：

| 问题 | 描述 | 影响 |
|------|------|------|
| OAuth弹窗残留旧modal | popup打开后选择弹窗（OAuth/Token）仍留在页面，用户点击其他平台绑定时产生多个popup | 用户困惑，多个弹窗叠加 |
| 弹窗手动关闭误判成功 | popup.closed轮询触发`_accountConfirmBindSuccess`，不管后端返回什么都走成功路径 | 用户没授权却看到"绑定成功"+跳转 |
| 三种绑定反馈不一致 | OAuth用独立成功层，手动令牌/转移用modalBox绿色勾，跳转目标不同（security tab vs repos） | 体验不统一 |

#### 13.12.2 修复方案

#### 修复1: OAuth弹窗打开后关闭选择modal

`_accountDoOauthBind` 中 popup 打开后立即调 `_accountCloseModal()`。选择弹窗的职责（选择OAuth/Token方式）已完成，后续流程由 popup 承载。

#### 修复2: 弹窗手动关闭不误判成功

`_accountConfirmBindSuccess` 新增 `fromPostMessage` 参数区分调用来源，记录确认前绑定状态 `_wasBound`，调 `/auth/me` 后对比：

```js
// fromPostMessage=true: postMessage 确认成功 → 绑定确实刚完成
// fromPostMessage=false: popup.closed 轮询/超时 → 用户可能手动关闭了弹窗
if (!_nowBound) {
  // 未绑定 → 恢复按钮 + 首次绑定提示"授权未完成"
} else if (!fromPostMessage && _wasBound) {
  // 重新授权场景：从绑定→绑定，状态没变 → 静默恢复按钮
} else {
  // 从无到有，或收到 postMessage 确认 → 绑定成功
}
```

#### 修复3: 统一三种绑定成功反馈+跳转

新增 `_accountShowBindSuccessToast`（独立成功反馈层）和 `_accountNavigateToRepos`（统一跳转）：

| 绑定方式 | 修复前反馈 | 修复前跳转 | 修复后反馈 | 修复后跳转 |
|---------|----------|----------|----------|----------|
| OAuth弹窗 | 独立成功层 | 留在security | 统一成功层 | 800ms后跳项目页 |
| 手动令牌 | modalBox绿色勾 | 立即跳repos | 统一成功层 | 800ms后跳项目页 |
| 转移绑定 | modalBox绿色勾 | 立即跳repos | 统一成功层 | 800ms后跳项目页 |

成功反馈层设计（Apple HIG 原则）：
- 居中遮罩 + 毛玻璃背景（`backdrop-filter:blur(4px)`）
- 圆形绿色勾（56px）+ 平台名 + "即将跳转到项目页面"提示
- `popIn` 弹性动画（`cubic-bezier(.4,0,.2,1)`）
- 1.5秒自动消失，支持点击遮罩快速关闭
- `prefers-reduced-motion` 降级

800ms 延迟跳转：让用户看到成功反馈再过渡到项目页，避免突然切页的割裂感。

#### 13.12.3 入口覆盖验证

| 入口 | 绑定 | 解绑 |
|------|------|------|
| 引导页 | `app.bindProvider` → `_accountBindProvider` ✅ | 无（引导页只在未绑定时显示） |
| 用户设置页 | `_accountBindProvider` ✅ | `_accountUnbindProvider` → `DELETE /auth/bind/{provider}` ✅ |
| settings.py 后端 | `PUT /settings/credentials`（手动令牌）✅ | `_check_login_method_before_unbind` + `delete_external_identity` ✅ |

z-index 层级：引导页 lobby(z:100) < 选择modal(z:300) < 成功反馈层(z:400)

#### 13.12.4 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/static/js/pages/account.js | popup打开后关闭modal；_accountConfirmBindSuccess新增fromPostMessage参数+三态判定；_accountShowBindSuccessToast独立成功层；_accountNavigateToRepos统一跳转；手动令牌/转移绑定统一反馈；account.js v26 |
| src/api/static/css/app.css | fadeBg/popIn动画 + prefers-reduced-motion降级；app.css v82 |
| src/api/static/index.html | account.js v22→v25, app.css v81→v82 |
| src/api/static/locales/zh/app.json | 新增 bind_success_hint / bind_cancelled |
| src/api/static/locales/en/app.json | 同上英文 |

#### 13.12.5 验证结果

- 90 passed, 0 failed
- 服务运行中 health ok


## 14. OAuth 会话隔离与绑定冲突处理设计（2026-10-13）

### 14.1 问题分析

OAuth 绑定流程中存在一个固有问题：弹窗与主页面共享浏览器 Cookie，第三方平台默认复用已有会话跳过登录步骤。如果用户浏览器中已登录 GitCode 账号 A，弹窗打开 GitCode 授权页时直接使用账号 A 的会话完成授权，用户无法选择切换到另一个账号。如果账号 A 已被其他 Grape 用户绑定，就会出现绑定冲突。

### 14.2 业界方案对比

| 方案 | 说明 | 适用场景 | 局限 |
|------|------|---------|------|
| `prompt=login` | OAuth 2.0 / OIDC 标准参数，强制第三方平台重新认证 | 重新授权/换绑场景 | 首次绑定不需要（用户本来可能未登录第三方） |
| `prompt=select_account` | 强制选择账号（Google/Microsoft 支持） | 多账号选择 | GitCode/GitHub 不支持 |
| `prompt=consent` | 强制重新确认授权 | 需要重新授权 | 不强制重新登录，不能解决会话复用 |
| 弹窗模式 | `window.open()` 打开弹窗 | 保持用户操作上下文 | 不能隔离 Cookie 会话，需配合 prompt=login |
| 无痕窗口 | 引导用户在无痕模式操作 | 多账号管理工具 | 体验最差，不可接受 |
| 后端冲突检测 | 回调时检查第三方账号是否已被绑定 | 所有场景 | 需配合前端提示和转移流程 |

### 14.3 最终方案：三层设计

#### 第一层：prompt=login（前端触发 + 后端透传）

**触发条件**：前端检测 `alreadyBound`（该平台已有绑定）时附加 `prompt=login`，首次绑定不附加。

**前端实现**（`account.js` `_accountDoOauthBind`）：

```js
var alreadyBound = bindings.indexOf(provider) >= 0;
var promptParam = alreadyBound ? '&prompt=login' : '';
// 弹窗模式
API.get('/auth/bind/' + provider + '?popup=true' + promptParam)
// 全页降级
API.get('/auth/bind/' + provider + (promptParam ? '?prompt=login' : ''))
```

**后端透传**（`grape_auth.py` `bind_provider`）：

```python
result = build_auth_url(provider, state, redirect_uri,
                        extra_state_data=extra_state,
                        prompt=prompt)
```

**URL 构建**（`providers.py` `build_auth_url`）：

```python
if prompt:
    params["prompt"] = prompt
auth_url = f"{cfg['auth_url']}?{urllib.parse.urlencode(params)}"
```

**代码位置**：
- 前端：`account.js` 第 481-484 行（`alreadyBound` 检测 + `promptParam` 拼接）
- 后端入口：`grape_auth.py` 第 879-881 行（`prompt` 透传到 `build_auth_url`）
- URL 构建：`providers.py` 第 219-220 行（拼入 OAuth 授权 URL 参数）

#### 第二层：弹窗模式（popup + postMessage）

**绑定场景用弹窗，登录场景用全页重定向**：

| 场景 | 模式 | 原因 |
|------|------|------|
| 绑定第三方平台 | 弹窗 + postMessage | 用户已登录在设置页操作，保持上下文 |
| 已绑定重新授权 | 弹窗 + prompt=login | 保持上下文 + 强制重新认证 |
| OAuth 登录 | 全页重定向 | 兼容性优先，跨域重定向后 window.opener 可能丢失 |
| 移动端绑定 | 全页重定向 | 移动端弹窗体验差 |
| 弹窗被拦截 | 降级为全页跳转 | 保证流程不中断 |

**弹窗安全设计**：
- 父窗口不信任 postMessage 的 success 信号，收到后调后端接口确认最终状态
- binding_ticket 不走 postMessage，留在服务端与 JWT 绑定
- postMessage targetOrigin 显式指定为 `window.location.origin`
- message handler 幂等（`handled` 标志位 + cleanup）

#### 第三层：后端冲突检测（`_handle_oauth_bind`）

回调时 `_handle_oauth_bind` 检查第三方账号（`external_id`）是否已被绑定，分三种情况处理：

**情况 1：活跃绑定属于其他用户 + 对方是占位账号且无业务数据 → 自动转移**

```python
existing = get_external_identity(provider, external_id)
if existing and existing["grape_user_id"] != binding_user_id:
    conflict_user_id = existing["grape_user_id"]
    if is_placeholder_account_mergeable(conflict_user_id):
        # 自动转移，不需要用户确认
        merge_ok = merge_placeholder_account(conflict_user_id, binding_user_id, provider, external_id)
```

占位账号条件（全部满足）：`email_is_placeholder=1 + created_by_oauth=1 + 无密码 + 无其他活跃绑定 + status=1 + 无业务数据`

`is_placeholder_account_mergeable` 通过 `_has_business_data` 检查以下 11 张业务表：
user_repos(is_admin=1)、agent_tasks、chat_agent_session、chat_conversations、issue、user_feedback、user_credentials、api_tokens、session_api_tokens、scene_likes、skill_likes

**情况 1b：活跃绑定属于其他用户 + 对方是占位账号但有业务数据 → 转移确认**

```python
    else:
        # 不可自动合并 → 检查是否是占位账号（有业务数据）还是真实用户
        conflict_account = get_grape_account_by_id(conflict_user_id)
        if conflict_account and conflict_account.get("email_is_placeholder") and conflict_account.get("created_by_oauth"):
            # 占位账号但有业务数据 → 走转移确认流程（用户确认后转移绑定+业务数据）
            _ticket = save_binding_ticket(...)
```

用户确认后调用 `POST /auth/transfer-bind`，在同一事务内：
1. `transfer_business_data` 转移全部业务数据（UPDATE user_id）
2. `transfer_external_identity_in_tx` 转移绑定关系
3. 占位账号标记 `status=4`（merged）

**情况 2：活跃绑定属于其他用户 + 对方不是占位账号 → 绑定冲突错误**

```python
        else:
            return _bind_error_redirect(request, "bind_conflict", popup=popup)
```

**情况 3：历史绑定（已解绑 status=0）属于其他用户 → 转移确认**

```python
existing_any = get_external_identity_any_status(provider, external_id)
if existing_any and existing_any["status"] == 0 and existing_any["grape_user_id"] != binding_user_id:
    _ticket = save_binding_ticket(...)
    # popup 模式：只传 need_transfer，不传 binding_ticket
    # 非 popup 模式：传 binding_ticket 在 URL
```

**代码位置**：`grape_auth.py` 第 1286-1361 行（`_handle_oauth_bind` 完整函数）

### 14.4 完整流程图

```
用户点击"授权绑定/重新授权"
         │
         ▼
   前端检测 alreadyBound
    ┌────┴────┐
    │         │
  首次绑定  已绑定
    │         │
  无prompt  prompt=login
    │         │
    └────┬────┘
         │
         ▼
   打开弹窗 (window.open)
    ┌────┴────────┐
    │             │
  正常打开      被拦截
    │             │
    │          降级全页跳转
    │          (仍带 prompt=login)
    │             │
    └────┬────────┘
         │
         ▼
   第三方平台授权
   (prompt=login 时强制重新登录)
         │
         ▼
   回调 /api/auth/oauth/callback
         │
         ▼
   _handle_oauth_bind 检查冲突
    ┌────┼────┬────┐
    │    │    │    │
  无冲突 占位账号 非占位 历史绑定
    │    │    │    │
  直接绑定 自动转移 冲突错误 转移票据
    │    │    │    │
    └────┼────┴────┘
         │
         ▼
   postMessage → 父窗口
   (不含 access_token / binding_ticket)
         │
         ▼
   父窗口调后端确认最终状态
```

### 14.5 各场景触发条件汇总

| 场景 | prompt=login | 弹窗/全页 | 后端冲突处理 | 用户是否需确认 |
|------|-------------|-----------|-------------|--------------|
| 首次绑定 | 不附加 | 弹窗（移动端全页） | 按冲突检测结果处理 | 无冲突时不需要 |
| 已绑定重新授权 | 附加 | 弹窗 + prompt=login | 按冲突检测结果处理 | 无冲突时不需要 |
| 冲突-占位账号(无业务数据) | 同上 | 同上 | 自动转移 | 不需要 |
| 冲突-占位账号(有业务数据) | 同上 | 同上 | 生成转移票据，用户确认后转移绑定+业务数据 | 需要确认 |
| 冲突-真实用户 | 同上 | 同上 | 返回冲突错误 | N/A（操作失败） |
| 冲突-历史绑定 | 同上 | 同上 | 生成转移票据 | 需要确认 |
| 弹窗被拦截 | 同上 | 降级全页跳转 | 同上 | 同上 |
| 移动端 | 同上 | 全页跳转 | 同上 | 同上 |
| OAuth 登录 | 不附加 | 全页重定向 | 走登录流程 | N/A |

### 14.6 变更文件清单

本节是对已有第 13 节（弹窗模式）中分散描述的 prompt=login 和冲突处理的归一整理。相关代码变更已在以下小节记录：

| 能力 | 设计文档位置 | 代码位置 |
|------|------------|---------|
| 弹窗模式 + postMessage | 第 13.2-13.7 节 | account.js / app.js / grape_auth.py / oauth-popup-callback.html |
| prompt=login | 第 14 节 | account.js 第 481-484 行 / grape_auth.py `bind_provider` / providers.py `build_auth_url` |
| 仓库缓存污染修复 | 第 13.10 节 | repos.py / account.js / app.js |
| 占位账号自动合并(无业务数据) | 第 14 节 | db_setup.py / grape_account_db.py `is_placeholder_account_mergeable` + `merge_placeholder_account` |
| **业务数据检查(新增)** | **本节 27.3 情况 1b** | **grape_account_db.py `_has_business_data` + `transfer_business_data` + `transfer_external_identity_in_tx`** |
| **占位账号有业务数据降级转移(新增)** | **本节 27.3 情况 1b** | **grape_auth.py `_handle_oauth_bind` 降级路径 + `transfer_bind` 事务转移** |
| 转移绑定票据 | 第 13.4 节 | grape_auth.py `_handle_oauth_bind` / `GET /auth/transfer-context` |
| 中间页降级体验 | 第 13.8 节 | oauth-popup-callback.html |

**业务表清单维护说明**：

`_BUSINESS_TABLES` 定义在 `grape_account_db.py` 中，列出了所有需要检查和转移的 user_id 关联业务表。遗漏任何一张表都会导致占位账号自动转移时跳过该表的数据检查，有业务数据的占位账号被当作"无数据"自动合并，造成数据丢失。

**新增业务表时的操作步骤**：

1. 在 `_BUSINESS_TABLES` 中添加 `(表名, user_id列名, 额外WHERE条件)`
2. 如果表有复合主键（如 user_repos 的 `(user_id, repo_key)`），在 `transfer_business_data` 中添加冲突处理分支
3. 在设计文档第 14.6 节"业务表清单维护说明"中更新表清单
4. 在需求文档第 16.3 节业务规则中更新表数量

**当前清单（11 张表）**：

| 表名 | user_id 列 | 额外过滤 | 说明 |
|------|-----------|---------|------|
| user_repos | user_id | is_admin=1 | 只检查托管仓库，普通缓存可重建 |
| agent_tasks | user_id | — | agent 任务 |
| chat_agent_session | user_id | — | chat 会话配置 |
| chat_conversations | user_id | — | chat 对话 |
| issue | user_id | — | issue 数据 |
| user_feedback | user_id | — | 用户反馈 |
| user_credentials | user_id | — | 用户凭据 |
| api_tokens | user_id | — | API tokens |
| session_api_tokens | user_id | — | session API tokens |
| scene_likes | user_id | — | 场景点赞 |
| skill_likes | user_id | — | 技能点赞 |

## 15. 安全审查 P0+P1 修复（2026-10-13）

### 15.1 背景

外部安全审查（3 路子代理并行 + 父代理逐项验证）发现 20 个有效问题。P0（4 项）阻断合并，P1（7 项，5 项修复 + 2 项误报排除）合并前修复。全部修复后 90 passed。

### 15.2 P0 修复

#### P0-1: 注册防枚举——已注册邮箱发送通知邮件

**问题**：需求 1 要求"对已注册邮箱发送'您的邮箱已注册'通知邮件（不发送验证码）"。代码 `register_send_code` 的 `already_registered` 分支只调 `check_rate_limit` 模拟限流，无邮件发送。

**修复**（`grape_auth.py` `register_send_code`）：

```python
else:
    rl_result = auth_verification.check_rate_limit(email, "register", ip)
    if not rl_result["ok"]:
        raise HTTPException(status_code=429, ...)
    # P0-1: 对已注册邮箱发送"您的邮箱已注册"通知邮件
    try:
        from src.auth.email_service import get_email_service
        _svc = get_email_service()
        _svc.send_notification(to=email, subject="您的邮箱已注册",
                               body="您的邮箱已在本系统注册，如非本人操作请忽略此邮件。")
    except Exception as _e:
        log.warning(f"Failed to send already-registered notification to {email}: {_e}")
```

邮件发送失败不阻断流程（只记日志），防枚举效果由"统一返回验证码已发送"保证。

#### P0-2: OAuth 已绑定用户跳过白名单检查

**问题**：`_handle_oauth_login` 已绑定分支（`if binding:` 内）调 `_check_whitelist`。需求 4 明确说"已绑定的用户授权后直接登录成功"、"首次自动创建需通过 OAuth 白名单校验"（仅首次）。

**修复**（`grape_auth.py` `_handle_oauth_login`）：已绑定分支移除 `_check_whitelist` 调用。白名单只在自动创建路径（`else` 分支 L1500）和 `oauth_register`（P0-3 修复）中检查。

#### P0-3: oauth_register 纯 OAuth 注册路径加白名单检查

**问题**：`oauth_register`（L1855）创建账号时不调 `_check_whitelist`。而 `_handle_oauth_login` 的自动创建路径（L1500）有检查。两条路径不一致。

**修复**（`grape_auth.py` `oauth_register`）：

```python
external_login = state_data.get("external_login", "")
# P0-3: 纯 OAuth 注册路径也需检查白名单
if not _check_whitelist(provider, external_login):
    raise HTTPException(status_code=403, detail={"code": 403, "message": "您不在平台白名单中，无法注册"})
```

#### P0-4: settings.py 手动令牌解绑加登录方式检查

**问题**：`settings.py` L203-204 和 L294-295 手动清除 GitHub/GitCode token 时直接调 `delete_external_identity`，无 `has_password` + `count_user_identities` 检查。而 `grape_auth.py` 的 `unbind_provider` 有完整检查。

**修复**（`settings.py`）：新增公共函数 `_check_login_method_before_unbind`，与 `unbind_provider` 逻辑一致：

```python
def _check_login_method_before_unbind(user_id: int, provider: str):
    from src.auth.grape_account_db import has_password, count_user_identities
    if not has_password(user_id) and count_user_identities(user_id) <= 1:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "至少保留一种登录方式，请先设置 Grape 密码",
            "need_set_password": True,
        })
```

GitHub 和 GitCode 两处解绑前均调用此函数。

### 15.3 P1 修复

#### P1-2: 注销返回消息补全冷静期完整说明

**修复**（`grape_auth.py` `delete_account` 返回值）：

```python
# 修改前
return {"message": "账号注销成功，进入 30 天冷静期"}
# 修改后
return {"message": "账号已注销，进入 30 天冷静期。冷静期内可联系管理员恢复账号，30 天后数据将永久删除"}
```

#### P1-3: 注销增加 force 参数支持确认强制注销

**问题**：需求 14 业务流程第 4 步要求"若有未完成资源，提示用户先处理或确认强制注销"。原来有运行中任务时直接返回 400 拒绝。

**修复**（`grape_auth.py`）：

- `DeleteAccountBody` 新增 `force: bool = False`
- 有运行中任务时返回 409 + `need_force=True`（而非 400 直接拒绝）
- 用户传 `force=true` 后放行注销

#### P1-4: merge_placeholder_account 释放用户名

**问题**：`is_username_taken` 不过滤 status，merged(status=4) 账号 username 不变 → 用户名永久占用。

**修复**（`grape_account_db.py` `merge_placeholder_account` + `grape_auth.py` `transfer_bind`）：

```sql
-- 修改前
UPDATE grape_accounts SET status=4, updated_at=? WHERE id=?
-- 修改后（释放用户名）
UPDATE grape_accounts SET status=4, username='merged_' || CAST(id AS TEXT), updated_at=? WHERE id=?
```

两处标记 merged 的代码同步修复。

#### P1-5: 迁移脚本 TOCTOU 修复

**问题**：`_ensure_grape_account` / `_ensure_external_identity` / `_ensure_user_credentials` 三个函数的 SELECT 和 INSERT 在两个独立 `with db_lock` 块中。注释声称"BUG-M3 修复：幂等检查和 INSERT 在同一个锁内"但实际不在。

**修复**（`scripts/migrate_user_accounts_to_grape.py`）：三个函数的 SELECT + dry_run 检查 + INSERT 合并到同一个 `with db_lock` 块内。

#### P1-6: cooldown.py 匿名化清理全部关联数据

**问题**：需求 22 要求删除"个人工作空间和相关数据"。Step3 只删 `organization` + `org_member`，未清理业务数据。

**修复**（`src/admin/cooldown.py` `_anonymize_account` Step3）：

补全 14 张表的 DELETE：user_repos、agent_tasks、chat_agent_session、chat_conversations、issue、user_feedback、user_credentials、api_tokens、session_api_tokens、scene_likes、skill_likes、org_member、unified_user_mapping、feishu_onboard_task。

### 15.4 误报排除

| 编号 | 排除理由 |
|------|---------|
| P1-1 | 需求 19（后补充）明确说 5 次/10 分钟且"IP 限流计数共享"，覆盖需求 1 的 10 次。代码按需求 19 实现正确 |
| P1-7 | 需求 8.2 是后补充的安全加固节。验证码有效期 10 分钟 + 限流，安全性可接受。降级为 P2 |

### 15.5 变更文件清单

| 文件 | 修复项 |
|------|--------|
| src/api/routes/grape_auth.py | P0-1 通知邮件 / P0-2 白名单移除 / P0-3 白名单新增 / P1-2 注销消息 / P1-3 force 参数 / P1-4 transfer_bind 释放用户名 |
| src/api/routes/settings.py | P0-4 _check_login_method_before_unbind + 两处解绑调用 |
| src/auth/grape_account_db.py | P1-4 merge_placeholder_account 释放用户名 |
| src/admin/cooldown.py | P1-6 匿名化清理全部关联数据 |
| scripts/migrate_user_accounts_to_grape.py | P1-5 三个函数 TOCTOU 修复 |
| tests/test_transfer_bind.py | 测试 mock 更新（transfer_external_identity_in_tx + transfer_business_data + get_grape_account_by_id）|

### 15.6 验证结果

- 90 passed, 0 failed（test_transfer_bind 20 + test_auth_routes 16 + test_auth_token 14 + test_grape_auth 40）
- 服务运行中 health ok

## 16. OAuth 登录组织上下文残留修复（2026-10-13）

### 16.1 问题分析

用户通过 OAuth 登录切换账号后访问仓库列表时遇到 403"不是该组织的成员"。

**根因**：`api.js` 每次请求从 `localStorage.current_org_id` 读取并设为 `X-Org-ID` header。后端 `resolve_org_context`（`src/api/org_context.py`）收到 `X-Org-ID` 后校验成员关系，非成员返回 403。

三条登录路径对 `current_org_id` 的处理不一致：

| 登录路径 | 清除 current_org_id | 代码位置 |
|---------|-------------------|---------|
| 密码登录（pwdLogin） | 是 | app.js `pwdLogin` L2024 |
| OAuth exchange_code 登录 | **否（bug）** | app.js `init` L2422-2426 |
| OAuth legacy token 登录 | **否（bug）** | app.js `init` L2453-2456 |

用户 A 在浏览器中 `current_org_id = X`（组织 X），OAuth 登录到账号 B 后，`current_org_id` 仍为 X。如果 B 不属于组织 X，所有 API 请求返回 403。

### 16.2 修复方案

两个 OAuth 登录路径都加 `localStorage.removeItem('current_org_id')`，与 `pwdLogin` 保持一致：

```js
// exchange_code 路径（L2424）
localStorage.removeItem(REPO_CACHE_KEY);
localStorage.removeItem('currentRepo');
localStorage.removeItem('current_org_id');  // 新增
sessionStorage.removeItem('currentRepo');

// legacy token 路径（L2453）
localStorage.removeItem(REPO_CACHE_KEY);
localStorage.removeItem('currentRepo');
localStorage.removeItem('current_org_id');  // 新增
sessionStorage.removeItem('currentRepo');
```

清除后由 `showApp → loadProfile → _loadOrgSelect` 重新初始化为当前用户的个人组织。`_loadOrgSelect`（app.js L247）检查 `current_org_id` 是否在 `/orgs` 返回的列表中，不在则回退到 `is_personal=1` 的个人组织。

### 16.3 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/static/js/app.js | exchange_code 路径加 removeItem('current_org_id')；legacy token 路径加 removeItem('current_org_id')；app.js v165 |
| src/api/static/index.html | app.js v164→v165 |

### 16.4 验证结果

- 90 passed, 0 failed
- 服务运行中 health ok

## 17. 安全配置统一到 configs/security.json（2026-10-13）

### 17.1 现状问题

当前 4 个安全变量分散在 3 个文件的环境变量读取中：

| 变量 | 读取位置 | 说明 |
|------|---------|------|
| APP_ENV | `config.py` L174 | 生产环境标识 |
| JWT_SECRET_KEYS / JWT_KID_CURRENT | `jwt_utils.py` L40-41 | JWT 密钥轮换 |
| GRAPE_CREDENTIAL_KEYS | `credential_crypto.py` L26 | 凭据 AES-256-GCM 加密密钥 |

另有 `configs/crypto.json` 存储凭据密钥值，与 `credential_crypto.py` 的环境变量读取重复。`email_service.py` 有独立的 `is_production_env()` 逻辑。生产部署需修改 `/etc/grape/grape.env` 加 4 行环境变量，容易遗漏。

### 17.2 设计方案

新增 `configs/security.json` + `configs/security.example.json`，收编全部安全变量。**有默认值兜底——不创建文件 = 开发模式，全部用默认值。**

**配置优先级**：configs/security.json > 默认值（无文件即开发模式）

**文件结构**：

```json
// configs/security.example.json（仓库内模板）
{
  "env": "development",
  "jwt": {
    "secret_keys": "",
    "kid_current": ""
  },
  "credential_keys": ""
}
```

### 17.3 新增模块：src/auth/security_config.py

统一读取安全配置，模块级缓存 + 热加载。每个 getter 两级 fallback：security.json > 默认值。

```python
def get_env() -> str:
    # security.json env > "development"

def get_jwt_secret_keys() -> str:
    # security.json jwt.secret_keys > ""（空，jwt_utils 内部 fallback 到 JWT_SECRET）

def get_jwt_kid_current() -> str:
    # security.json jwt.kid_current > ""

def get_credential_keys() -> str:
    # security.json credential_keys > 从 JWT_SECRET 派生 SHA256 32字节 > "dev:MDAw..."（默认测试密钥）
```

### 17.4 改造点

| 文件 | 修改前 | 修改后 |
|------|--------|--------|
| `config.py` | `APP_ENV = os.environ.get("APP_ENV")` | `from security_config import is_production; IS_PRODUCTION = is_production()` |
| `jwt_utils.py` | `os.environ.get("JWT_SECRET_KEYS")` | `from security_config import get_jwt_secret_keys` |
| `credential_crypto.py` | `os.environ.get("GRAPE_CREDENTIAL_KEYS")` | `from security_config import get_credential_keys` |
| `main.py` 门禁 | `os.environ.get("JWT_KID_CURRENT")` | `from security_config import get_jwt_kid_current` |
| `email_service.py` | 自己读 email.json env + APP_ENV | 委托 `security_config.is_production()`，email.json 的 env 字段保留给邮件服务自己用 |
| `.gitignore` | — | 追加 `configs/security.json` |
| `configs/security.example.json` | — | 新增模板 |
| `configs/crypto.json` | 存凭据密钥值 | 废弃，值迁入 security.json credential_keys |

### 17.5 各环境行为

| 配置项 | 开发环境（无 security.json） | 生产环境（有 security.json） |
|--------|---------------------------|---------------------------|
| env | development（默认兜底） | production |
| JWT 密钥 | JWT_SECRET 环境变量 → 默认密钥 | security.json jwt.secret_keys |
| 凭据密钥 | 从 JWT_SECRET 派生 → 默认测试密钥 | security.json credential_keys |
| 安全门禁 | warning 不阻断 | 阻断 |
| 邮件服务 | 固定验证码 / LOG_ONLY | SMTP / Resend |

### 17.6 废弃项

- 环境变量 `APP_ENV` / `JWT_SECRET_KEYS` / `JWT_KID_CURRENT` / `GRAPE_CREDENTIAL_KEYS` 全部废弃，不再被读取
- `email.json` 的 `env` 字段保留给邮件服务自己单独使用，不作为 `security_config.get_env()` 的 fallback
- `crypto.json` 废弃，密钥值迁入 security.json credential_keys 后可正常加解密已有凭据
- `JWT_SECRET` 环境变量保留（这是基础 JWT 密钥，不是本次需求新增的）

---

## 附录 A: 运修记录

> 以下为开发过程中的运修记录，按时间顺序排列。这些记录保留原始变更细节供溯源参考。

## A.1. D48：认证页面 i18n 统一（2026-10-12）

### A.1.1 问题根因分析

三个认证页面（登录/注册/重置密码）存在两类 i18n 问题：

#### 问题 A：语言切换按钮始终显示"EN"（实现缺陷）

根因链：

1. register.html / forgot.html 加载了 `i18n.js` 但从未调用 `I18N.init()`
2. I18N 模块存在但未初始化（`_currentLang` 是默认 'zh'，`_loadedLangs` 全 false）
3. auth.js `toggleLang()` 检测到 `typeof I18N !== 'undefined'` → 调用 `I18N.setLang(newLang)` → return
4. `I18N.setLang()` 的 `applyI18nToDOM()` 只更新 `data-i18n` 元素，而 `langSwitchLabel` 这个 span 没有 `data-i18n` 属性
5. auth.js 的 `_updateLangSwitch()` 被 return 跳过
6. `I18N.setLang()` 还会调用 `_updateUrlLang()` 把 URL 改成 `/en/register` — SPA 路由前缀不适用于独立认证页面

#### 问题 B：页面跳转时文字闪烁 FOUT（实现缺陷）

根因：HTML 硬编码中文 + 异步 fetch 语言包。

- register.html / forgot.html：`Auth.initI18n()` 异步 fetch `app.json`，fetch 完成前显示硬编码中文，完成后 `_applyI18n()` 替换 → 可见跳变
- index.html：`I18N.init()` 同步应用 inline 最小包（仅 6 个 login key），其余 key 仍显示硬编码中文。全量包加载后 `applyI18nToDOM()` 再次执行更新剩余 key → 二次跳变

#### 问题 C：三套 i18n 机制并存（架构缺陷）

| 页面 | i18n 机制 | 按钮更新方式 |
|------|----------|-------------|
| index.html 登录页 | I18N 模块 + inline 最小包 | `_toggleLoginLang()` + `setTimeout(_updateLoginLangLabel, 500)` |
| register.html | 加载 i18n.js 不 init + auth.js 独立 fetch | `_updateLangSwitch()`（被跳过） |
| forgot.html | 同 register.html | 同 register.html |
| 工作台 SPA | I18N 模块完整初始化 | account.js `_accountLoadAppearance()` `selectLang()` |

### A.1.2 最终方案：inline 包扩充 + 委托 I18N 模块

核心思路：三个认证页面统一使用 I18N 模块，auth.js 彻底委托 I18N，不维护独立翻译逻辑。

**auth.js 的 `t()` 函数说明：** `Auth.t(key)` 是 `I18N.t(key)` 的 convenience wrapper，供 register.html / forgot.html 内联 JS 调用（如 `Auth.t('app.login.xxx')`），不是独立翻译逻辑。auth.js 只负责 I18N 不管的两件事：语言切换按钮互斥显示（`_updateLangSwitch`）和页面动态文案回调（`_onLangChange`）。

#### A.1.2.1 i18n.js 改造

_INLINE_PACKS app.login 从 6 key 扩充到 95 key（zh+en 全量），I18N.init() 同步应用 inline 包 → 零闪烁。

#### A.1.2.2 auth.js 改造

彻底委托 I18N 模块，移除所有独立翻译逻辑：

- 移除：`_loginPack`、`_applyI18n()`、`_tr()`、`_showAuthCard()`、`_setLangCookie()`
- `initI18n()`：调 `I18N.init('25')` + `_updateLangSwitch()`
- `toggleLang()`：调 `I18N.setLang(newLang).then()` → `_updateLangSwitch()` + `_onLangChange()`
- `t()`：直接调 `I18N.t(key)`
- `_updateLangSwitch()`：用 `t('app.login.lang_switch_en/zh')` 取值，fallback 硬编码

#### A.1.2.3 register.html / forgot.html 改造

- 恢复 `<script src="/static/js/i18n.js?v=8">` 标签
- 移除 `i18n-hidden` class（不再需要隐藏卡片）
- `Auth.initI18n()` → `I18N.init()` 同步 inline 包 → 立即渲染正确语言

#### A.1.2.4 index.html 登录页改造

`_updateLoginLangLabel()` 改用 `I18N.t('app.login.lang_switch_en/zh')` 取值（与 auth.js 一致），移除硬编码。

#### A.1.2.5 main.py 后端改动

`/en/{path:path}` 路由增加 register/forgot 判断，返回对应独立 HTML（不是 index.html）。

#### A.1.2.6 auth.css 改动

移除 `.auth-card.i18n-hidden` / `.auth-card.i18n-ready` 样式（不再需要防闪烁 hack）。

### A.1.3 改动清单

| 文件 | 改动 |
|------|------|
| i18n.js | _INLINE_PACKS app.login 从 6 key 扩充到 95 key（zh+en） |
| auth.js | 彻底委托 I18N，移除独立翻译逻辑（_loginPack/_applyI18n/_tr） |
| register.html | 恢复 i18n.js + 移除 i18n-hidden |
| forgot.html | 同 register.html |
| index.html | _updateLoginLangLabel 改用 I18N.t() |
| main.py | /en/{path} 路由增加 register/forgot 判断 |
| auth.css | 移除 i18n-hidden / i18n-ready 样式 |

### A.1.4 验证结果

- 70 单元测试通过
- Playwright 8 场景验证通过（注册/忘记密码/登录页 × 中英文 + /en/register + /en/forgot 路由）
- 语言切换 zh→en→zh 按钮和文案同步更新，URL 前缀联动

---

## A.2. D49：验收标准对齐 + 外部审计安全修复（2026-10-12）

### A.2.1 验收标准对齐（8 项）

需求文档产品经理视角优化后，代码同步对齐新增验收标准：

| # | 需求 | 修复内容 | 文件 |
|---|------|---------|------|
| 1 | 需求1 | 验证码提示显示完整邮箱地址（"验证码已发送到 xxx@xxx.com"） | register.html / forgot.html / 语言包 |
| 2 | 需求2 | 锁定提示文案改为"连续登录失败 5 次，账号已临时锁定 15 分钟。可通过邮箱验证码立即解锁。" | login_throttle.py |
| 3 | 需求3 | 重置成功后跳转 /?reset=1，登录页检测参数显示"密码已重置"提示 | forgot.html / index.html / 语言包 |
| 4 | 需求17 | 解绑前检查仓库使用情况：有 webhook 的仓库返回 409 + 仓库列表，用户确认后带 confirm=true 重新调用 | grape_auth.py / account.js |
| 5 | 需求14 | 注销提示补全冷静期含义（"冷静期内可联系管理员恢复账号，30天后数据永久删除"） | app.js |
| 6 | 需求21 | 管理员不可禁用自己（查 grape_accounts username 与 admin username 对比） | admin/routes.py |
| 7 | 需求22 | 恢复冷静期账号后通知用户（如有邮箱调 send_notification） | admin/routes.py |

### A.2.2 外部审计安全修复（6 项）

外部 AI 深度审计发现的安全问题，逐条验证后修复合理的 6 项：

| # | 优先级 | 问题 | 修复 | 文件 |
|---|--------|------|------|------|
| 1 | P0 | send_code 未检查邮件发送返回值：_send_smtp 返回 False 不抛异常，验证码仍落库 | 检查 send_verification_code 返回值，False 时不写 DB | verification.py |
| 2 | P1 | /identities 返回 access_token 给前端：OAuth token 通过 API 泄露，前端不需要 | get_user_identities 移除 access_token 字段，SELECT 不查该列 | grape_account_db.py |
| 3 | P1 | 账号锁定未记录审计日志：需求 16 要求记录 account_locked 事件 | check_throttle 返回 locked 时调 log_audit("account_locked") | grape_auth.py |
| 4 | P2 | isalnum() 允许中文用户名：'测试'.isalnum() 返回 True，绕过用户名规则 | 改为 c.isascii() and (c.isalnum() or c == "_") | grape_auth.py |
| 5 | P0 | 迁移脚本 OAuth token 明文存储：_ensure_external_identity 写入 access_token 未调 encrypt | 写入前调 credential_crypto.encrypt()，与运行时一致 | migrate_user_accounts_to_grape.py |
| 6 | P2 | 启动门禁未检查 email.json：用 email.json 配 SMTP 且不设环境变量时生产启动被拒 | 门禁增加读 configs/email.json 的 smtp.host | main.py |

### A.2.3 审计报告排除项（不修复，有理由）

| 报告项 | 排除理由 |
|--------|---------|
| P1-1 XFF 伪造 | 已有 TRUSTED_PROXY_IPS 白名单（D46 P2-1），生产部署配白名单即可 |
| P2-2 email_config_cache 永久缓存 | D46 P1-11 已修复 mtime 检查自动重载 |

### A.2.4 测试

新增 tests/test_d48_fixes.py（9 用例，完全 mock DB + 邮件）：
- 用 FastAPI dependency_overrides 注入 mock 用户，不走真实鉴权
- 用 patch 替换所有 DB 函数，不连任何真实数据库
- 用 patch 替换所有邮件函数，不产生真实邮件发送
- 覆盖：解绑仓库检查 3 场景 + 管理员禁用自己 2 场景 + 恢复通知 3 场景 + 锁定文案 1 场景

### A.2.5 验证结果

- 79 passed（70 旧 + 9 新），0 failed
- 测试原则：新需求测试完全 mock DB 和邮件，不连任何真实数据库

---

## A.3. 运维修复记录（2026-10-12）

### A.3.1 GitCode token 验证格式错误（500/401）

**根因：** `settings.py` PUT /credentials 验证 GitCode token 时用了 `Authorization: token ***` header 格式，GitCode API v5 不认此格式，返回 401 "token not found"。项目 `vcs/api_client.py` 早已发现并改用 `access_token` 查询参数，但 settings.py 漏改。

**修复：** 改为 `params={"access_token": _gc_tok}` 查询参数方式，与 `api_client.py` 一致。删除过时注释"GitCode API Authorization header 格式为 token xxx"。

### A.3.2 解绑 500 — managed_repos.user_id 列不存在（MySQL）

**根因：** `grape_auth.py` unbind_provider 函数解绑前检查受影响仓库时，查 `managed_repos WHERE user_id=?`，但 MySQL `managed_repos` 表无 `user_id` 列（用户仓库关联在 `user_repos` 表）。SQLite 旧表有此列故本地不报错，MySQL 上直接 500。

**修复：** 两处查询改为 `managed_repos m INNER JOIN user_repos u ON m.repo_key = u.repo_key WHERE u.user_id=?`。

### A.3.3 GitHub token 路径三处缺陷对齐

**问题 A：** GitHub token 验证失败只 log.warning 不抛异常，返回"凭据已更新"——用户看到成功但实际没保存。
**修复：** 对齐 GitCode 路径，验证失败抛 401，网络异常抛 502。

**问题 B：** GitHub token 写入时缺少"已被其他账号绑定"检查（GitCode 路径有 409 检查，GitHub 没有）。
**修复：** 补充 `existing["grape_user_id"] != user["id"]` 检查，抛 409。

**问题 C：** `create_external_identity`/`update_external_identity_tokens` 返回 False 时代码不报错，静默返回成功。
**修复：** GitHub 和 GitCode 两条路径都加返回值检查，False 时抛 500。

### A.3.4 注册防枚举设计移除

**问题：** 邮箱已注册时不发验证码，发一封"邮箱已注册"通知邮件，但前端统一显示"验证码已发送"。用户收到邮件打开发现没有验证码，体验极差。

**修复：** 移除防枚举设计，邮箱已注册时直接返回 409 + "该邮箱已注册 Grape 账号，请直接登录或重置密码"。

### A.3.5 全局防枚举策略统一移除

**问题：** 注册移除防枚举后，forgot-password/send-code 和 unlock/send-code 仍保留防枚举——邮箱未注册/账号不存在时统一返回"验证码已发送"但不发验证码。与注册策略自相矛盾（攻击者可通过 forgot-password 接口枚举），且用户体验相同问题（傻等验证码等不到）。

**修复：**
- forgot-password/send-code：邮箱未注册直接返回 404 + "该邮箱未注册 Grape 账号"
- unlock/send-code：账号不存在直接返回 404 + "账号不存在或未绑定邮箱"

### A.3.6 OAuth 登录 token 刷新静默失败

**问题：** `grape_auth.py:1257` OAuth 登录时 `update_external_identity_tokens` 返回值不检查——DB 写入失败不报错，用户登录成功但 token 没刷新，后续操作 401 且无法定位。

**修复：** 加返回值检查，失败时 log.warning 记录（不阻断登录流程，因为旧 token 可能仍有效）。

---

## A.4. 引导页简化为单屏绑定页（2026-10-12）

### A.4.1 设计变更

引导页从多屏滚动产品概览简化为单屏绑定页面。设计变更详见 onboarding-experience-requirements.md（需求 31）和 onboarding-experience-tech-design.md。

**核心变更：**
- 引导页只做绑定引导，不做产品介绍
- 所有内容在一屏内可见，不需要滚动
- 删除工作流演示、Agent 架构、基础设施特性、底部 CTA、引流入口
- 删除连续阻尼滚动引擎、进度条、浮动按钮、IO 揭示动画

### A.4.2 品牌色处理优化 — Apple 设计原则

**问题：** GitCode brand_color `#e7330f`（纯红色）和 GitHub brand_color `#24292f`（近黑色）直接用作按钮背景色，在 Apple 浅色设计系统中视觉突兀。红色按钮在引导页和登录页都打破了 System Blue 的视觉一致性。

**设计原则（Apple HIG）：**
- System Colors 优先 — primary action 按钮用系统蓝色 `--a`（#0071e3），不用第三方品牌色
- Brand as Accent — 品牌色用作图标点缀和微妙的背景 tint，不用作大面积填充
- Platform Identity through Icons — 平台身份通过图标识别，不通过按钮颜色识别

**优化方案：**
- 绑定按钮统一用 `var(--a)` 系统蓝色（引导页 + 登录页 + 用户设置页）
- 品牌色通过 CSS 变量 `--brand` 传递到卡片
- 平台图标区域用 `color-mix(in srgb, var(--brand) 8%, transparent)` 做微妙 tint 背景
- 图标颜色用 `var(--brand)` 品牌色
- 卡片 hover 时 border-color 用 `color-mix(in srgb, var(--brand) 30%, var(--b))` 品牌色 tint
- 删除 `oauth-btn--branded` 类，登录页 OAuth 按钮统一风格，品牌色只做图标 tint

**影响范围：**
- app.js: 引导页绑定卡片 + 登录页 OAuth 按钮
- app.css: `.ob-bind-icon` 加 tint 背景 + 品牌色图标
- auth.css: 删除 `.oauth-btn--branded`，新增 `.oauth-btn svg` 品牌色图标

### A.4.3 历史记录（已废弃的设计迭代）

以下为此前引导页的设计迭代记录，这些设计已被 16.1 的单屏绑定页方案替代，仅作历史记录保留：

- ~~16.1 双滚动条修复~~ — 单屏布局不再有滚动，双滚动条问题不复存在
- ~~16.2 去装饰性元素~~ — 单屏布局无装饰元素
- ~~16.3 布局对齐修复~~ — 单屏居中布局，无需多区域对齐
- ~~16.4 品牌色色条 + 能力卡片~~ — 删除全部卡片区域
- ~~16.5 容器嵌套背景色修复~~ — 删除嵌套容器
- ~~16.6 连续阻尼滚动进度引擎~~ — 删除滚动引擎
- ~~16.7 IO 揭示动画修复~~ — 删除 IO 动画
- ~~16.8 clip-path 文字揭示 + 环境浮动~~ — 删除揭示动画


## A.5. 通用设置移除 + 凭据迁移到用户设置独立 tab（2026-10-12）

### A.5.1 架构变更

通用设置页面移除，华为云 AK/SK 凭据迁移到用户设置新增 credentials tab。

**变更内容：**
- 通用设置页面移除（credentials/notifications/appearance/advanced tab 全部删除）
- 侧边栏移除"通用设置"入口，只保留"仓库设置"
- settings.js 从 411 行缩减到 ~100 行，只保留 gitcode tab（仓库设置）
- 华为云 AK/SK 迁移到用户设置新增 credentials tab（独立于 security tab）
- 用户设置 tabs：profile/security/credentials/appearance/apikeys/danger（6 个）
- app.js nav settings 默认 tab 从 credentials 改为 gitcode
- 通知 tab 移除（纯静态占位，无后端 API 无保存功能）
- 流水线模式（advanced tab）删除（死代码无入口）

### A.5.2 设计理由

- 认证（security tab）≠ 授权（credentials tab），分开职责清晰
- credentials tab 扩展性好，未来加 CodeArts/OBS Token 不挤在 security 卡片里
- 通知 tab 无效（纯静态 HTML 无后端 API 无保存功能）直接移除
- 外观 tab 和用户设置重复，移除通用设置的
- 通用设置移除后，侧边栏"设置"分组只有"仓库设置"，职责单一


## A.6. OAuth 安全加固（2026-10-12）

### A.6.1 问题分析

对 OAuth 自动登录流程进行架构审查后发现以下问题：

| 优先级 | 问题 | 影响 |
|--------|------|------|
| P0-1 | 用户名同名自动绑定 | 攻击者在第三方平台改名即可劫持已知用户名账号 |
| P0-2 | /login/{provider} 不校验已登录态 | 已登录用户重复 OAuth 会创建第二个账号 |
| P0-3 | access_token 通过 URL query 传递 | token 泄露到日志/Referer/浏览器历史 |
| P0-4 | 第三方邮箱未验证直接信任 | 虚假邮箱可注册 |
| supp-1 | associate_token user_id=0 JWT | 多余的弱 JWT |
| supp-2 | OAuth 端点无限流 | 可被滥用消耗 state 存储 |
| P1-1 | 自动创建无回滚 | 孤立账号 |
| P1-2 | oauth_callback 340 行 | 可维护性差 |
| P1-4 | 个人组织补建重复 3 次 | DRY 违反 |
| P1-5 | redirect URL 构造不一致 | nginx 反代下可能有问题 |
| P2-1 | state 无清理 | 过期记录积累 |
| P2-2 | Provider 缓存无失效 | admin 改配置需重启 |
| P2-3 | exchange_token 用 urllib | 与 get_user_info 风格不统一 |
| P2 | 中文用户名生成不直观 | 产生 user/user_2 |
| P2 | 后端错误消息硬编码中文 | 违反 i18n 纪律 |

### A.6.2 修复方案

#### P0-1：删除同名账号自动绑定

删除 grape_auth.py 中 `get_grape_account_by_username` 自动绑定分支。未绑定用户统一走"邮箱判断 → 自动创建 or 关联页"。用户名同名绝不作为绑定依据。

#### P0-2：已登录态保护

/login/{provider} 可选 JWT 认证：检查 Authorization header，有效 JWT 且 token_version + status 校验通过 → state 里带 binding_user_id 转绑定流程。无 JWT 或无效 → 走登录流程。

#### P0-3：exchange code 替代 URL 传 token

新建 exchange_codes 表（code TEXT PK, user_id, username, provider, token_version, is_new, created_at, expires REAL）。10 秒过期，一次性消费（查+删）。

流程：OAuth 回调成功 → 生成 exchange code 写 DB → 302 重定向到 `/?exchange_code=xxx` → 前端 POST /auth/exchange 换 JWT。

DB 存储而非 Redis——复用 session_manager 的 db 实例，加 db_lock 保证并发安全。懒清理：save 时顺带删除过期记录。

#### P0-4：email_verified 列 + 受限账号

grape_accounts 加 email_verified INTEGER DEFAULT 0 列。DDL + ALTER 迁移。

- create_grape_account（邮箱验证码注册）→ email_verified=1
- create_grape_account_oauth（OAuth 注册）→ 传入第三方 email_verified
- /auth/profile 返回 email_verified
- 前端 _checkOAuthCompletion 检查 email_verified=0 时弹窗引导

数据修复：47 个有邮箱+有密码的账号 UPDATE email_verified=1。

#### supp-1：删除 associate_token

_oauth_associate_redirect 不再签发 user_id=0 的 JWT。关联 API 认 state+密码，不需要 JWT。

#### supp-2：OAuth 端点限流

login_throttle.py 新增 check_oauth_rate_limit（内存滑动窗口）：
- /login/{provider}: 10 次/分钟
- /auth/exchange: 30 次/分钟

#### P1-1：自动创建回滚

绑定失败时 soft_delete_account（对齐 oauth_register 的做法）。

#### P1-2：oauth_callback 拆分

拆为三个独立函数：
- _handle_oauth_delete: 注销认证流程
- _handle_oauth_bind: 绑定流程
- _handle_oauth_login: 登录流程（已绑定 + 自动创建）

oauth_callback 只做公共前置（state 校验 + token 交换 + 用户信息获取）+ 分流，约 50 行。

公共前置的错误处理统一：`err_fn = _bind_error_redirect if binding_user_id else _oauth_error_redirect`。

#### P1-4：提取公共函数

- _ensure_personal_org(user_id, username) — 消除 3 处重复
- _check_whitelist(provider, external_login) — 消除 2 处重复

#### P1-5：redirect URL 统一

提取 _build_redirect_url(request, path, params, hash_frag)：
- 统一 scheme（生产强制 https）+ host（_get_trusted_host）+ lang prefix + app_base
- _oauth_error_redirect / _bind_error_redirect / _oauth_login_redirect / _oauth_associate_redirect 全部调用
- 消除 6 处重复拼装

#### P2-1：state 懒清理

- save_oauth_state: 1/10 概率清理过期 state
- save_exchange_code: 每次清理过期 code
- 启动时也清理 exchange_codes

#### P2-2：Provider 缓存热失效

- _load_providers 改为 TTL 缓存（5 分钟兜底）
- invalidate_provider_cache() 主动失效
- admin save_config 写完 meta 表后调 invalidate

#### P2-3：exchange_token 统一 requests

替代 urllib.request，与 get_user_info 保持一致。支持 HTTPS_PROXY。

#### P2：中文用户名生成

_generate_username 加 provider 参数。中文/非拉丁用户名清洗后为空时，用 `provider前缀 + md5(原始用户名)[:8]` 生成可读用户名（如 `gite_a1b2c3d4`）。

#### P2：后端错误消息 i18n

后端传错误码（auth_failed/state_invalid/token_failed/user_info_failed/no_external_id/account_not_found/account_disabled/create_failed/bind_failed/bind_conflict/delete_mismatch/exchange_code_invalid）替代中文。

前端：
- oauth_error: `I18N.t('app.login.oauth_error_' + code)` 翻译
- bind_error: _translateOAuthError 优先查 i18n key，兼容旧消息
- 新增 13 个错误码 i18n key（中英文）

### A.6.3 email Default 隐患修复

MySQL 中 grape_accounts.email 的 Default 是空字符串而非 NULL。代码层 `email = (email or "").lower().strip() or None` 正确转了 NULL，但 DDL Default 是空串有隐患。

修复：ALTER TABLE grape_accounts MODIFY COLUMN email VARCHAR(255) DEFAULT NULL。db_setup.py 加幂等迁移（MySQL 后端启动时自动修正）。

### A.6.4 向后兼容性

- 旧 JWT/refresh_token 验签逻辑未变，已登录用户正常使用
- 邮箱验证码注册账号 email_verified=1（数据已修复 47 条）
- 88 个无邮箱 OAuth 账号保持 email_verified=0（正确，需引导绑邮箱）
- 密码登录用户不触发 OAuth 完成度弹窗
- 前端兼容旧 URL token 流程（过渡期保留）

### A.6.5 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/routes/grape_auth.py | P0-1/P0-2/P0-3/P0-4/supp-1/supp-2/P1-1/P1-2/P1-4/P1-5/P2-username/P2-i18n |
| src/api/routes/auth.py | /auth/profile 返回 email/has_password/email_verified |
| src/auth/providers.py | P2-2 缓存TTL+invalidate / P2-3 exchange_token 用 requests |
| src/auth/grape_account_db.py | create_grape_account email_verified=1 / create_grape_account_oauth 加参数 / SELECT 加列 |
| src/auth/login_throttle.py | supp-2 check_oauth_rate_limit |
| src/user/db.py | P0-3 save_exchange_code/validate_exchange_code / P2-1 懒清理 |
| src/admin/config_store.py | P2-2 save_config 调 invalidate_provider_cache |
| src/api/db_setup.py | P0-3 exchange_codes DDL / P0-4 email_verified 列+迁移 / email Default 修复 |
| src/api/static/js/app.js | P0-3 exchange_code 前端 / P0-4 _checkOAuthCompletion / P2-i18n 错误码翻译 |
| src/api/static/index.html | early auth check 兼容 exchange_code |
| src/api/static/locales/zh/app.json | 13 个错误码 + email_unverified_hint |
| src/api/static/locales/en/app.json | 同上英文 |
| tests/test_auth_routes.py | mock 从 urllib 改为 requests.post / 断言改 exchange_code |

### A.6.6 提交记录

| 提交 | 内容 |
|------|------|
| 700734eff | P0-1/P0-2/P0-3/P0-4/supp-1/supp-2/P1-1/P1-4 |
| fb2db7a7c | email Default 修复 + 前端受限引导 |
| 2a8e781f7 | 向后兼容—邮箱验证码注册 email_verified=1 |
| c5f0295ba | P2-1/P2-2/P2-3 |
| 04eed29dd | P1-2 拆分 + P1-5 redirect 统一 + P2 中文用户名 + P2 错误 i18n |


## A.7. 注册/重置密码流程优化（2026-10-12）

### A.7.1 问题分析

对注册流程和重置密码流程进行深度代码审查后发现以下问题：

| # | 问题 | 影响 | 优先级 |
|---|------|------|--------|
| 1 | forgot-password/send-code 返回 404 暴露邮箱是否已注册 | 用户枚举漏洞 | 中 |
| 2 | forgot-password/reset 不检查账号状态 | 禁用用户重置密码后无明确提示 | 低 |
| 3 | register/send-code 传密码并检查密码强度 | 密码冗余传输，send-code 和 verify 双重检查 | 低 |
| 4 | register_verify 个人组织创建用内联代码，未用 _ensure_personal_org | 与 P1-4 提取的公共函数不一致 | 低 |
| 5 | change-password 密码强度检查传空 email | 跳过"密码不可与邮箱相同"检查 | 低 |
| 6 | set-password 不签发新 token | 设置密码后需重新登录才能用密码登录 | 低 |
| 7 | 验证码限流不区分场景 | 多场景操作时窗口提前用完 | 信息 |

### A.7.2 修复方案

#### 问题 1：forgot-password 用户枚举修复

forgot_password_send_code 改为统一返回 200 "验证码已发送"：
- 邮箱已注册 → 正常发送验证码
- 邮箱不存在 → 不发送邮件，返回相同响应
- 限流时返回 429（不泄露邮箱是否存在——攻击者无法区分限流和不存在）

#### 问题 2：forgot-password/reset 禁用状态提示

forgot_password_reset 重置成功后检查 account.status：
- status=0（禁用）→ "密码重置成功。账号当前已被禁用，请联系管理员恢复后登录"
- status=3（冷静期）→ "密码重置成功。账号处于注销冷静期，请联系管理员恢复后登录"
- status=1（正常）→ "密码重置成功"

不阻止禁用用户重置密码——重置只改密码+token_version+1，不会恢复账号状态。

#### 问题 3：register/send-code 去密码检查

- RegisterSendCodeBody.password 改为可选（`password: str = ""`）
- register_send_code 删除密码强度检查
- 密码强度检查只在 register_verify 阶段做（已有）
- 前端仍传 password（兼容），后端忽略

#### 问题 4：register_verify 统用 _ensure_personal_org

```python
# 旧：内联 create_org + get_org_by_slug + slug 冲突处理
# 新：
_ensure_personal_org(account["id"], body.username)
```

#### 问题 5：change-password 传 email

```python
# 旧
pwd_err = auth_password.check_password_strength(body.new_password, account["username"], "")
# 新
pwd_err = auth_password.check_password_strength(body.new_password, account["username"], account.get("email", ""))
```

#### 问题 6：set-password 签发新 token

set_password 函数加 response: Response 参数，设置密码成功后：
- 签发新 JWT（access + refresh）
- 设置 refresh cookie
- 返回 `{"message": "密码设置成功", "token": access}`

不调 increment_token_version（遵循需求 12"不触发其他设备下线"）。

#### 问题 7：验证码限流区分场景

verification.py _count_recent_sends 加 scene 参数：
- scene 非空时 `WHERE email=? AND scene=? AND created_at>=?`
- scene 为空时保持原行为（`WHERE email=? AND created_at>=?`）
- send_code 调用时传当前 scene

各场景（register/reset_password/set_password/change_email）独立计数，3 次/10 分钟，不互相挤占。

### A.7.3 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/routes/grape_auth.py | 问题 1-6 全部修复 |
| src/auth/verification.py | 问题 7 _count_recent_sends 加 scene 参数 |
| tests/test_grape_auth.py | test_register_send_code_weak_password 适配 |

### A.7.4 提交记录

| 提交 | 内容 |
|------|------|
| 5bbafafc1 | 注册/重置密码流程优化 — 防枚举+密码检查前移+组织统一+限流分场景 |


## A.8. 绑定/解绑仓库获取优化（2026-10-12）

### A.8.1 问题分析

用户反馈绑定成功后获取不到仓库。深度排查绑定/解绑全链路后发现：

**根因：手动令牌绑定/解绑后后端 user_repos 缓存未清理。**

| 操作路径 | 前端 localStorage 清理 | 后端 user_repos 清理 | 结果 |
|----------|----------------------|---------------------|------|
| OAuth 绑定 | _checkBindSuccess 清 ✅ | _handle_oauth_bind 清 ✅ | 正常 |
| OAuth 解绑 | account.js 清 ✅ | unbind_provider 清 ✅ | 正常 |
| 手动令牌绑定 | account.js 清 ✅ | **未清** ❌ | **获取不到仓库** |
| 手动令牌解绑 | account.js 清 ✅ | **未清** ❌ | **旧仓库残留** |

手动绑定走 PUT /settings/credentials，写入 external_identities 后直接返回。没有调 _clear_user_repos_cache。前端清了 localStorage 但后端 user_repos 旧缓存还在，/repos/member 读到未过期的旧缓存返回不含新平台仓库的数据。

同时发现三个 UI 问题：
- showToast 第二参数被当作 duration，描述文案不显示
- 绑定成功后留在设置页而非跳转项目页
- 解绑成功后不刷新侧边栏仓库列表

### A.8.2 修复方案

#### 问题 1（根因）：手动绑定/解绑后清 user_repos

settings.py update_credentials 在写入/删除 external_identities 后，如果改了 github_token 或 gitcode_token，清理 user_repos 缓存：

```python
if "github_token" in explicit or "gitcode_token" in explicit:
    with _sm_dl:
        _c = _sm_db.cursor()
        _c.execute("DELETE FROM user_repos WHERE user_id=?", (user["id"],))
        _sm_db.commit()
```

#### 问题 2：showToast 支持描述文案

showToast 改为三参数 `(msg, desc, duration)`：
- 有 desc 时显示主文案（粗体）+ 描述（小字）
- desc 为数字时向后兼容（视为 duration）
- 绑定/解绑成功 toast 现在正确显示描述文案

#### 问题 3：绑定成功后跳转项目页

- OAuth 绑定：后端重定向 hash_frag 从 "account" 改为 "repos"
- 手动绑定：前端从 renderAccount('security') 改为 nav('repos') + loadRepos(true)

#### 问题 4：解绑后刷新侧边栏

account.js 两处解绑成功回调加 `initSidebarRepos()` 刷新侧边栏仓库列表。

#### 问题 5：_checkBindSuccess 全局调用

从 nav('account') 提取到 init 全局（和 oauth_error 检测一起），绑定成功跳转到 #repos 时也能检测 bind_success 并显示 toast。

#### 问题 6：affected_repos 计算顺序

unbind_provider 中先 COUNT 受影响仓库数量再 DELETE user_repos（之前先删后查，结果永远为 0）。

### A.8.3 缓存清理全覆盖验证

修复后四种操作路径的缓存清理：

| 操作路径 | 前端 localStorage | 后端 user_repos | 跳转 |
|----------|------------------|-----------------|------|
| OAuth 绑定 | _checkBindSuccess 清 | _handle_oauth_bind 清 | #repos |
| OAuth 解绑 | account.js 清 | unbind_provider 清 | 刷新侧边栏 |
| 手动令牌绑定 | account.js 清 | settings.py 清（本次修复）| nav('repos') + loadRepos(true) |
| 手动令牌解绑 | account.js 清 | settings.py 清（本次修复）| 刷新侧边栏 |

### A.8.4 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/routes/settings.py | update_credentials 清 user_repos 缓存 |
| src/api/routes/grape_auth.py | _handle_oauth_bind 跳转 #repos + affected_repos 计算顺序修复 |
| src/api/static/js/app.js | showToast 三参数 + _checkBindSuccess 全局 + _translateOAuthError 去 isZh |
| src/api/static/js/pages/account.js | 手动绑定跳转项目页 + 解绑刷新侧边栏 |

### A.8.5 提交记录

| 提交 | 内容 |
|------|------|
| afac9215e | 绑定/解绑UI优化 — 跳转项目页+toast描述文案+affected_repos修复+全局检测 |
| 3916adf7f | 绑定/解绑后仓库获取失败 — 手动绑定清DB缓存+解绑刷新侧边栏+绑定跳项目页 |


### A.8.6 GitCode 仓库只显示 1 个 — repo_key 前缀缺失（2026-10-12）

**根因：** _fetch_and_cache 对 GitCode 仓库不设 repo_key 字段。save_user_repos 用 `to_repo_key(full_name)` 生成 repo_key 时，to_repo_key 默认 provider="gitcode"，导致 GitHub 仓库（如 yangaiwu/ces）也被标记为 `gitcode:yangaiwu/ces`，GitCode 和 GitHub 仓库混在一起。

**数据链路：**

```
fetch_repos_with_role(GitCode) → 40 repos, 无 repo_key 字段
fetch_github_repos_with_role(GitHub) → 6 repos, repo_key="github:yangaiwu/ces"
合并 46 repos
save_user_repos → to_repo_key(full_name) → GitHub 仓库被标记 gitcode: 前缀
DB: 46 repos 全部 gitcode: 前缀, github: 前缀 0 个
_filter_by_provider("gitcode") 匹配 46 个（含 GitHub 仓库）
_filter_by_provider("github") 匹配 0 个
```

**修复：** _fetch_and_cache 对 GitCode 仓库显式设 repo_key 字段（`gitcode:full_name`），与 GitHub 仓库处理方式一致。

**验证：** xiaowu2119 用户 API 返回从 7 个仓库（1 gitcode + 6 github）变为 46 个仓库（40 gitcode + 6 github）。

| 提交 | 内容 |
|------|------|
| ae4dd8189 | GitCode仓库只显示1个 — _fetch_and_cache未设repo_key导致GitHub仓库被误标gitcode前缀 |


## A.9. noreply 邮箱解绑残留修复（2026-10-13）

### A.9.1 问题分析

**故障路径：**

1. 用户用 GitCode OAuth 登录 → 自动创建 grape_accounts，邮箱写入 `xxx@noreply.gitcode.com`
2. 用户解绑 GitCode → external_identities 删除绑定，user_repos 清理
3. 但 grape_accounts.email 没清 — noreply 邮箱还在，占用唯一索引
4. 用户再次用 GitCode OAuth 登录 → 查 external_identities 无绑定 → 检查邮箱冲突 → `is_email_registered(noreply邮箱)` 返回 True（自己的旧账号还占着）
5. 邮箱冲突 → 跳关联页"你的 GitCode 尚未关联 Grape 账号"
6. 无密码账号无法走关联页（需要密码）→ 账号变孤岛

**根因：** noreply.gitcode.com 是 OAuth 提供商生成的占位邮箱，不是用户真实邮箱。解绑后不应继续占用 email 唯一索引。

### A.9.2 修复方案

#### 数据层：email_source 列

grape_accounts 加 `email_source VARCHAR(16) DEFAULT NULL`：
- `user`：用户自己绑定的真实邮箱（邮箱验证码注册）
- `gitcode`/`github`：OAuth 提供商写入的邮箱
- `NULL`：无邮箱或历史数据未标记

DDL + `_ensure_column` 幂等迁移。历史数据批量修复（2 个 gitcode + 45 个 user）。

`_row_to_account` 和 3 个 SELECT 加 email_source 列。

`create_grape_account` 写入 `email_source='user'`。
`create_grape_account_oauth` 加 `provider` 参数，写入 `email_source=provider`。

#### 逻辑层：is_email_registered 忽略 placeholder

新增 `is_placeholder_email(email)` 判断 noreply.gitcode.com / noreply.github.com。

`is_email_registered` 开头加 `if is_placeholder_email(email): return False`。

placeholder 邮箱不参与冲突检查——即使历史数据没清也不会卡住用户。

#### 逻辑层：解绑时清理 placeholder 邮箱

unbind_provider 删除 external_identities 后：
1. 查 grape_accounts 获取 email + email_source
2. 如果 `is_placeholder_email(email) and email_source == provider`
3. → UPDATE email=NULL, email_source=NULL
4. 用户真实邮箱（email_source='user'）不受影响

#### 解绑校验（已有，无需修改）

```python
if not user_has_password and identity_count <= 1:
    raise HTTPException(400, "至少保留一种登录方式，请先设置 Grape 密码")
```

无密码 + 仅 1 个 OAuth 绑定的账号会被拒绝解绑。不会产生新孤岛。

### A.9.3 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/db_setup.py | DDL 加 email_source 列 + _ensure_column 迁移 |
| src/auth/grape_account_db.py | _row_to_account + SELECT 加列 / create_grape_account 写 'user' / create_grape_account_oauth 加 provider 参数 / is_placeholder_email + is_email_registered 忽略 placeholder |
| src/api/routes/grape_auth.py | unbind_provider 解绑后清 placeholder 邮箱 / create_grape_account_oauth 调用传 provider |

### A.9.4 提交记录

| 提交 | 内容 |
|------|------|
| 71dbd5ab5 | noreply邮箱解绑残留问题 — email_source标记+placeholder忽略+解绑清理 |

## A.10. 绑定流程修复三连（2026-10-13）

### A.10.1 问题1：showApp Promise 链断裂导致页面卡在 loading

**故障路径：**

1. 页面刷新 → init() 检测到 localStorage 有 token → 调 showApp()
2. showApp() 调 loadProfile() → /auth/profile 请求成功返回
3. loadProfile().then() 回调执行 → 调用 `_hideBindBanner()`
4. `_hideBindBanner` 函数在 commit ab237b2cc 中被删除（提示条移除），但 showApp() 里的调用点漏改
5. ReferenceError 导致 Promise 链进入 .catch() 分支
6. catch 分支调 loadRepos() 降级 → /repos/member 返回 400（未绑定平台）→ loadRepos catch 只 console.error
7. 页面卡在 loading 状态，无法自动到引导页

**根因：** 删除函数时漏改调用点，Promise 链断裂走了错误的降级路径。

**修复：**
- 删除 showApp() 中残留的 `_hideBindBanner()` 调用
- catch 分支改为走 `_restoreNavAfterLogin()`（和 then 分支统一），检查 bindings → 无绑定→引导页 / 有绑定→loadRepos

### A.10.2 问题2：转移绑定 UNIQUE 约束冲突导致 500

**故障路径：**

1. 用户 A（grape_user_id=33）之前绑过 GitCode 账号 X（external_id=AAA），后解绑（status=0）
2. 用户 A 手动填 token 绑定 GitCode 账号 Y（external_id=BBB，曾属于用户 B）
3. settings.py 检测到 BBB 属于用户 B 的解绑记录 → 返回 409 + binding_ticket（转移确认）
4. 用户确认转移 → 调 transfer_external_identity
5. transfer_external_identity 清理了 provider+external_id=BBB 的旧记录（用户 B 的解绑记录）
6. 但没清理目标用户 A 的旧解绑记录（external_id=AAA, status=0）
7. INSERT 新绑定时 UNIQUE(grape_user_id, provider) 约束冲突 → 500

**根因：** transfer_external_identity 只清理了同 external_id 的旧记录，没清理目标用户同 provider 的旧解绑记录。UNIQUE(grape_user_id, provider) 约束不允许同用户同 provider 有多条记录（含 status=0）。

**修复：**
- transfer_external_identity：INSERT 前增加 `DELETE FROM external_identities WHERE grape_user_id=? AND provider=? AND status=0` 清理目标用户旧记录
- create_external_identity：INSERT 路径同样增加清理逻辑（防止相同隐患）

### A.10.3 问题3：转移绑定后 access_token 为空导致 /repos/member 400

**故障路径：**

1. 用户确认转移绑定 → transfer_bind 端点调用 transfer_external_identity
2. transfer_external_identity 成功创建绑定记录，但 access_token 为空
3. 安全审查决定不在 binding_ticket 里存 token（安全考虑）
4. transfer_bind 只从 ticket 取 external_login/external_name/external_avatar，不传 access_token
5. 绑定记录 status=1 但 access_token 为空
6. /repos/member 通过 _get_oauth_token_from_identity 读 access_token → 空 → 返回 400

**根因：** 转移绑定时没有把用户输入的 token 写入新绑定记录。ticket 不存 token 是正确的（安全），但 transfer_bind 需要从前端获取 token。

**修复：**
- TransferBindBody 加 access_token 可选字段
- transfer_bind 把 body.access_token 传给 transfer_external_identity
- 前端 _accountShowTransferConfirm 接收 token 参数
- 前端转移确认时把用户输入的 token 带上：`{binding_ticket, confirm: true, access_token: token}`

### A.10.4 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/static/js/app.js | 删除 _hideBindBanner() 残留调用；catch 分支走 _restoreNavAfterLogin() |
| src/api/static/js/pages/account.js | _accountShowTransferConfirm 接收 token；转移确认请求带 access_token |
| src/auth/grape_account_db.py | create_external_identity INSERT 前清理目标用户旧解绑记录；transfer_external_identity 同样清理；exc_info=True 增强错误日志 |
| src/api/routes/grape_auth.py | TransferBindBody 加 access_token 字段；transfer_bind 传 token 给 transfer_external_identity |
| src/api/routes/settings.py | GitCode 绑定加调试日志（gc_id/any_status） |
| src/api/static/index.html | app.js v149 / account.js v15 |

## A.11. 切换账号登录完整修复（2026-10-13）

### A.11.1 问题分析

切换账号登录（如 yangaiwu → 退出 → xiaowu2119 登录）出现空白页面，有四个独立根因叠加：

**根因1：`window._currentView` 残留**
- logout 调 showLogin 不 reload 页面，`_currentView` 残留为上次视图
- 第二次登录 `_restoreNavAfterLogin` 第191行检测 `_currentView` 非空直接 return，不渲染
- 修复：showLogin 设 `window._currentView = ''`

**根因2：`current_org_id` 残留 → 403**
- logout/pwdLogin 不清 `localStorage.current_org_id`
- 切换账号后 API 请求的 `X-Org-ID` header 残留上一个账号的组织 ID
- 后端检查新账号不属于该组织 → `/repos/member` 返回 403 → loadRepos 失败
- 修复：logout + pwdLogin 清 `localStorage.removeItem('current_org_id')`

**根因3：logout 的 `API.post` 触发 `doRefresh` → 页面跳转**
- logout 先清 token 再调 `API.post('/auth/logout')`
- `API.post` → `ensureTokenValid` → 无 token → `doRefresh` → refresh 失败 → `location.href='/login'`
- 页面被跳转，showLogin 没机会执行
- 修复1：logout 用原生 fetch（不走 ensureTokenValid）
- 修复2：`ensureTokenValid` 无 token 时直接返回 null

**根因4：sessionStorage currentRepo + URL hash 残留**

- pwdLogin 只清 localStorage，没清 sessionStorage 的 currentRepo
- localStorage.clear() 不影响 sessionStorage
- 切换账号时旧账号的仓库选择残留，导致新账号路由恢复到 dashboard 但无权限
- URL hash 也可能残留（如 `#dash`），pwdLogin 没有清除，_restoreNavAfterLogin 会走 hash 路由恢复到错误页面
- 修复：pwdLogin 登录成功后 `sessionStorage.removeItem('currentRepo')` + `window.history.replaceState(null, '', pathname)` 清除 URL hash

### A.11.2 修复方案汇总

| 位置 | 修复 |
|------|------|
| showLogin | `window._currentView = ''` |
| logout | 清 `current_org_id`；用原生 fetch 调 logout API；清 URL hash |
| pwdLogin | 清 `current_org_id` |
| ensureTokenValid (api.js) | 无 token 时返回 null，不调 doRefresh |
| init | 无 token 时清 sessionStorage currentRepo + URL hash |

### A.11.3 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/static/js/app.js | showLogin 清 _currentView；logout 清 orgId+原生 fetch+清 hash；pwdLogin 清 orgId+sessionStorage currentRepo+URL hash；init 清 currentRepo+hash；app.js v157 |
| src/api/static/js/api.js | ensureTokenValid 无 token 返回 null；api.js v28 |

## A.12. 平台品牌色统一 + 重置密码提示修复（2026-10-13）

### A.12.1 平台品牌色全局统一

**问题：** platformIcon 用 `fill="currentColor"`，颜色取决于父元素。引导页/登录页通过 `--brand` 传递品牌色，但仓库列表页/设置页没有 `--brand`，图标退化成灰色文字色，四个页面颜色不一致。

**方案：** 品牌色内联到 SVG fill，一个函数管所有页面。

- `PLATFORM_BRAND_COLOR` 常量：GitCode `#F54A36`（Apple systemRed 方向，比原始 `#e7330f` 更协调），GitHub `#1F2328`（GitHub 2024 新品牌色，深色模式可见）
- `platformIcon` 函数直接用品牌色 fill，不依赖 `currentColor`/`--brand`
- CSS `.platform-icon` 深色模式 `filter: brightness(1.4)` 提亮
- 后端 `brand_color` 不动（向后兼容），前端 `_brandOverride` 覆盖

**变更文件：** repos.js（platformIcon + PLATFORM_BRAND_COLOR）、app.js（_brandOverride 覆盖）、app.css（深色模式提亮）、index.html（登录页 inline --brand）

### A.12.2 重置密码提示三个问题

**问题1（i18n 时序竞态）：** IIFE 在 `I18N.init` 之前执行，`I18N.t()` 返回原始 key `app.login.toast_password_reset_notice` 而非翻译文本。原因是 IIFE 同步执行，`I18N.init` 在 DOMContentLoaded 回调里异步填充内联 pack。

**问题2（错误使用了元素 ID）：** 提示写到 `loginUserErr`（用户名输入框的错误提示元素），误导用户以为输入有错。

**问题3（URL 参数未清理）：** `?reset=1` 残留在 URL，刷新重复触发。

**修复：**
- 等 I18N 内联 pack 就绪后再显示（三层兜底：`isLangLoaded` / DOMContentLoaded+200ms / 1s setTimeout）
- 改用 `app.showToast`（登录页 app.js 未加载时用 inline toast 兜底）
- `replaceState` 清理 URL 参数

**变更文件：** index.html（重置密码提示 IIFE 重写）

### A.12.3 变更文件清单

| 文件 | 变更 |
|------|------|
| src/api/static/js/pages/repos.js | platformIcon 品牌色内联 + PLATFORM_BRAND_COLOR 常量；repos.js v52 |
| src/api/static/js/app.js | _renderOnboardingShell + renderOAuthButtons 加 _brandOverride；app.js v158 |
| src/api/static/css/app.css | .ob-bind-icon 深色提亮 + .platform-icon 深色 brightness；app.css v80 |
| src/api/static/index.html | 登录页 OAuth 按钮 inline --brand 色值更新；重置密码提示 IIFE 重写（i18n 时序+Toast+URL 清理）|

