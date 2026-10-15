# 重新绑定体验 — 技术设计文档

## 第22节：external_identities 软删除 + 自动恢复绑定

### 22.1 DDL 变更

external_identities 表新增两列：

```sql
ALTER TABLE external_identities ADD COLUMN status INTEGER DEFAULT 1;
ALTER TABLE external_identities ADD COLUMN unbound_at TEXT DEFAULT NULL;
```

最终 DDL（新库 CREATE TABLE 含全部列）：

```sql
CREATE TABLE IF NOT EXISTS external_identities (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    grape_user_id     INTEGER NOT NULL,
    provider          VARCHAR(32) NOT NULL,
    external_id       VARCHAR(128) NOT NULL,
    external_login    VARCHAR(128) DEFAULT '',
    external_name     VARCHAR(128) DEFAULT '',
    external_avatar   VARCHAR(512) DEFAULT '',
    access_token      TEXT DEFAULT '',
    refresh_token     TEXT DEFAULT '',
    token_expires_at  TEXT DEFAULT '',
    metadata          TEXT DEFAULT '{}',
    status            INTEGER DEFAULT 1,
    unbound_at        TEXT DEFAULT NULL,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (provider, external_id),
    UNIQUE (grape_user_id, provider)
)
```

status 含义：
- 1：活跃绑定（默认）
- 0：已解绑（历史绑定记录）

unbound_at：解绑时间戳，用于审计。

#### 唯一约束设计

UNIQUE(provider, external_id) 和 UNIQUE(grape_user_id, provider) 约束对 status=0 的记录同样生效。一条 (provider, external_id) 记录无论 status 是 0 还是 1，都占据唯一约束。这确保：
- 一个第三方账号只能对应一条 external_identities 记录（无论活跃还是历史）
- 一个 Grape 账号在一个 provider 下只能有一条记录（无论活跃还是历史）

#### external_id 存储方式

external_id 明文存储。理由：
- external_id 是第三方平台公开的用户标识，不是机密凭据
- UNIQUE(provider, external_id) 依赖明文值做精确匹配和快速查询
- 凭据安全通过 access_token/refresh_token 的 AES-GCM 加密保证
- 拖库防护靠 DB 访问控制 + 网络隔离
- 业界主流做法：活跃绑定表和历史记录均明文存 provider_user_id

### 22.2 DB 层变更（grape_account_db.py）

#### 修改函数

1. **delete_external_identity** → 改为软删除
   - `UPDATE external_identities SET status=0, access_token='', refresh_token='', unbound_at=? WHERE grape_user_id=? AND provider=? AND status=1`
   - access_token/refresh_token 清空（安全：解绑后旧 token 不可用）
   - require_keep_one 逻辑不变（检查活跃绑定数 + 密码）
   - 返回 bool

2. **get_external_identity** → 加 `AND status=1` 过滤
   - 只返回活跃绑定

3. **get_user_identities** → 加 `AND status=1` 过滤
   - 前端只展示活跃绑定

4. **count_user_identities** → 加 `AND status=1` 过滤
   - 统计活跃绑定数（用于 require_keep_one 检查）

5. **update_external_identity_tokens** → WHERE 加 `AND status=1`
   - 只更新活跃绑定的 token

#### 新增函数

6. **get_unbound_identity(provider, external_id)** → dict | None
   - `SELECT grape_user_id, external_login, external_name, external_avatar, unbound_at FROM external_identities WHERE provider=? AND external_id=? AND status=0`
   - 返回原绑定信息，用于自动恢复绑定时的匹配
   - 不返回 access_token/refresh_token（已清空）

7. **restore_external_identity(grape_user_id, provider, access_token, refresh_token, external_name, external_avatar)** → bool
   - `UPDATE external_identities SET status=1, access_token=encrypt(?), refresh_token=encrypt(?), external_name=?, external_avatar=?, unbound_at=NULL, updated_at=? WHERE grape_user_id=? AND provider=? AND status=0`
   - 原子恢复绑定 + 更新 token

8. **delete_unbound_identity(provider, external_id)** → bool
   - `DELETE FROM external_identities WHERE provider=? AND external_id=? AND status=0`
   - 硬删除历史绑定记录（保留给未来转移绑定场景用）

### 22.3 OAuth 登录流程变更（grape_auth.py）

#### _handle_oauth_login 修改

新流程（历史绑定优先于邮箱冲突）：
```
1. get_external_identity(provider, external_id)         # 查活跃绑定
   → 有：直接登录（不变）

2. get_unbound_identity(provider, external_id)          # 查历史绑定
   → 有：查原账号状态
     → status=1（正常）：自动恢复绑定并登录
       - restore_external_identity(status 0→1, 更新 token)
       - 签发 JWT + exchange_code 重定向
       - 审计日志：rebind + login_success
     → status=0（禁用）：错误提示"原账号已被禁用"
     → status=3（注销）：错误提示"原账号已注销，无法恢复"
     → 不存在：错误提示"原账号不存在"
   → 无：继续

3. 查邮箱冲突（现有逻辑不变）
   → 邮箱被占用：跳关联页（验证所有权）
   → 邮箱无冲突：自动创建
```

关键：历史绑定优先于邮箱冲突。一旦命中历史绑定，不再走邮箱冲突逻辑，避免"自己和自己冲突"。

自动恢复绑定是后端原子操作，不需要前端交互——用户解绑后再次 OAuth 登录，直接登录到原账号，无感知。

#### 邮箱冲突检查忽略 placeholder 邮箱

现有 `is_email_registered` 已排除 placeholder 邮箱（noreply.gitcode.com / noreply.github.com）。此逻辑不变。

### 22.4 _ensure_column 幂等升级

db_setup.py 中新增：
```python
_ensure_column(c, "external_identities", "status", "INTEGER DEFAULT 1")
_ensure_column(c, "external_identities", "unbound_at", "TEXT DEFAULT NULL")
```

老库升级 ALTER 语句保留，新库 CREATE TABLE 已含全部列（DDL 统一原则）。

### 22.5 其他文件变更

- **deps.py** — `_get_oauth_token_from_identity` 查 external_identities 加 `AND status=1`
- **migrate_user_accounts_to_grape.py** — `_ensure_external_identity` INSERT 带 `status=1`
- **grape_account_db.py** — `is_placeholder_email` 结合 email_source 字段判断
- **i18n (zh/en app.json)** — 新增 original_account 错误码文案

### 22.6 关联页增强（邮箱冲突场景）

#### 关联页支持多种验证方式

`OAuthAssociateBody` 新增 `code` 字段，支持两种验证方式（任选其一）：
- 密码验证：`identifier + password`
- 邮箱验证码验证：`identifier（邮箱）+ code`

#### 新增 API：POST /api/auth/login/{provider}/associate/send-code

发送关联验证码到用户邮箱。防用户枚举：统一返回"验证码已发送"，邮箱不存在时不实际发送。

#### 关联验证流程

1. 有 code → 邮箱验证码验证：verify_code(email, "oauth_associate", code) → 按邮箱查账号
2. 有 password → 密码验证：verify_grape_account(identifier, password)
3. 验证通过后检查账号状态（status=1 才允许，封禁/注销拒绝）
4. 写入绑定关系 → 签发 JWT → 登录

### 22.7 安全设计

| 风险 | 处理 |
|------|------|
| 封禁绕过 | 恢复前重新查 DB 确认原账号 status=1，非 1 拒绝 |
| Token 泄露 | 解绑时清空 access_token/refresh_token，恢复时用最新 OAuth token 覆盖 |
| 账号劫持 | external_id 来自 OAuth 回调，不接受前端传入 |
| 重复账号 | 历史绑定优先于邮箱冲突，自动恢复不创建新账号 |
| 静默合并 | 邮箱注册账号用 OAuth 登录时邮箱冲突走关联页验证所有权，不自动关联 |

### 22.8 审计日志

| 事件 | key | detail |
|------|-----|--------|
| 自动恢复绑定 | rebind | {provider} |
| 登录成功(恢复) | login_success | {method: oauth, provider, via: rebind} |
| 解绑 | unbind | {provider}（已有，不变） |

### 22.9 影响范围

修改文件：
- src/api/db_setup.py — DDL + _ensure_column
- src/auth/grape_account_db.py — 软删除 + 新增 3 个函数 + 5 个查询加 status=1 过滤 + is_placeholder_email 结合 email_source
- src/api/routes/grape_auth.py — _handle_oauth_login 加历史绑定检查 + 自动恢复 + 关联页增强(邮箱验证码) + 新增 send-code API
- src/api/deps.py — 查 external_identities 加 status=1
- scripts/migrate_user_accounts_to_grape.py — _ensure_external_identity 带 status=1
- src/api/static/locales/zh/app.json — 新增错误码文案
- src/api/static/locales/en/app.json — 新增错误码文案

新增 API：
- POST /api/auth/login/{provider}/associate/send-code — 关联页发送邮箱验证码（防枚举）

## 第23节：binding_ticket 转移绑定

### 23.1 设计概述

当用户B（已登录）主动绑定一个曾属于用户A（已解绑，status=0）的第三方账号时，系统不静默转移也不直接拒绝，而是：
1. 生成一次性 binding_ticket（服务端签发，10分钟过期，绑定会话用户）
2. 前端展示转移确认页面
3. 用户确认后凭票据完成原子转移

OAuth 登录的自动恢复（第22节）不受影响——转移确认只在主动绑定时触发。

### 23.2 DDL 变更

```sql
CREATE TABLE IF NOT EXISTS binding_tickets (
    ticket           TEXT PRIMARY KEY,
    initiating_user_id  INTEGER NOT NULL,    -- 当前操作用户B
    provider         VARCHAR(32) NOT NULL,
    external_id      VARCHAR(128) NOT NULL,
    external_login   VARCHAR(128) DEFAULT '',
    external_name    VARCHAR(128) DEFAULT '',
    external_avatar  VARCHAR(512) DEFAULT '',
    original_user_id INTEGER NOT NULL,        -- 原账号用户A
    created_at       REAL NOT NULL,
    expires          REAL NOT NULL            -- created_at + 600（10分钟）
)
```

安全设计：票据不存 access_token/refresh_token，避免凭据泄露。
转移后新绑定的 access_token 留空，用户下次 OAuth 登录时自动刷新。

### 23.3 DB 层变更（grape_account_db.py）

新增 3 个函数：

| 函数 | 职责 |
|------|------|
| `save_binding_ticket(initiating_user_id, provider, external_id, ..., original_user_id)` | 生成 `secrets.token_urlsafe(32)` 票据，加密存储 token，写入 DB，返回 ticket 字符串 |
| `validate_binding_ticket(ticket, initiating_user_id)` | 查+删（一次性消费），校验未过期 + initiating_user_id 匹配，返回票据数据 dict |
| `transfer_external_identity(new_user_id, provider, external_id, ..., original_user_id)` | 原子转移：UPDATE 旧绑定 status=0+清token → DELETE 同 provider+external_id 的旧解绑记录 → INSERT 新活跃绑定 |

`transfer_external_identity` 事务内执行，失败时 rollback。

### 23.4 API 变更（grape_auth.py + settings.py）

新增 API：

```
POST /api/auth/transfer-bind
Body: { binding_ticket: str, confirm: bool }
依赖: require_jwt_only
```

流程：
1. `validate_binding_ticket(ticket, user["id"])` — 票据无效/过期/用户不匹配 → 410
2. `confirm` 为 false → 400
3. `has_password(original_user_id)` + `count_user_identities(original_user_id)` — 原账号无其他登录方式 → 400
4. `transfer_external_identity(...)` — 原子转移
5. `auth_audit.log_audit("transfer_bind", ...)` — 审计日志
6. `_clear_user_repos_cache(user["id"])` + `_clear_user_repos_cache(original_user_id)` — 清理缓存
7. 返回 `{"message": "绑定转移成功"}`

修改的绑定路径（4处）：

| 路径 | 文件 | 变更 |
|------|------|------|
| GitCode token 绑定 | settings.py | 检测 status=0 属于其他用户 → `save_binding_ticket` → 返回 409 + `need_transfer` + `binding_ticket` |
| GitHub token 绑定 | settings.py | 同上 |
| OAuth 关联 | grape_auth.py oauth_associate | 同上 |
| OAuth 绑定回调 | grape_auth.py _handle_oauth_bind | 检测 status=0 属于其他用户 → `save_binding_ticket` → URL 参数重定向到前端确认页 |

OAuth 绑定回调路径的转移确认通过 URL 参数传递：
- 后端重定向到 `/?need_transfer={provider}&binding_ticket={ticket}#account`
- 前端 `app.js _checkBindSuccess` 检测 URL 参数，延迟调用 `account.js _accountShowTransferConfirm` 展示确认对话框

返回格式（settings.py / oauth_associate 路径）：
```json
{
  "code": 409,
  "message": "该平台账号曾绑定过其他账号，是否转移到当前账号？",
  "need_transfer": true,
  "binding_ticket": "xxx"
}
```

### 23.5 安全设计

| 风险 | 处理 |
|------|------|
| 票据伪造 | ticket 是 `secrets.token_urlsafe(32)`，不可猜测 |
| 票据重放 | 一次性消费（查+删），第二次返回 None |
| 票据劫持 | 绑定 initiating_user_id，不匹配时返回 None |
| 原账号变孤岛 | 转移前检查原账号是否有密码或其他活跃 OAuth 绑定 |
| 并发转移 | db_lock + 事务内 UPDATE+DELETE+INSERT |
| 防枚举 | 提示文案不暴露原账号信息，只说"曾绑定过其他账号" |

### 23.6 审计日志

| 事件 | key | detail |
|------|-----|--------|
| 转移绑定 | transfer_bind | {provider, external_id, original_user_id} |

### 23.7 影响范围

修改文件：
- src/api/db_setup.py — binding_tickets 表 DDL（不含 access_token/refresh_token）
- src/auth/grape_account_db.py — 新增 3 个函数（save_binding_ticket / validate_binding_ticket / transfer_external_identity）
- src/api/routes/grape_auth.py — 新增 transfer-bind API + oauth_associate 检测历史绑定 + _handle_oauth_bind 检测历史绑定
- src/api/routes/settings.py — token 绑定路径检测历史绑定生成票据
- src/api/static/locales/zh/app.json — 9 个 i18n key
- src/api/static/locales/en/app.json — 9 个 i18n key
- src/api/static/js/pages/account.js — 前端转移确认对话框（全部文案走 I18N.t()）
- src/api/static/js/app.js — _checkBindSuccess 检测 OAuth 回调 URL 中的 need_transfer 参数

新增 API：
- POST /api/auth/transfer-bind — 转移绑定确认

## 第24节：绑定/解绑交互体验统一

### 24.1 设计概述

按 Apple HIG 感知延迟阈值（100ms/1s/10s）统一引导页和设置页的绑定/解绑交互。
引导页 `app.bindProvider` 改为复用设置页 `_accountBindProvider` 模态框，
两个入口提供完全一致的交互流程。

### 24.2 CSS 变更（app.css v78）

| 样式类 | 用途 |
|--------|------|
| `.bt:active` | 按钮点击缩放 scale(0.97)，0.08s 回弹 |
| `#accountModal` | 模态框渐入渐出（opacity + scale，0.2s） |
| `.acct-btn-spinner` | 按钮内嵌微型 spinner（14px 旋转圆环） |
| `#bindPanelOauth/Token` | Tab 切换 0.2s opacity fade |
| `.acct-inline-success` | 成功内联状态（绿色勾 + 弹性弹出） |
| `.acct-platform-card.removing` | 解绑卡片渐出动画 |
| `@media prefers-reduced-motion` | 所有动画降级禁用 |

### 24.3 JS 变更

**app.js（v147）:**
- `bindProvider` 改为调用 `_accountBindProvider`（复用设置页模态框）
- 兜底：account.js 未加载时回退到直接 OAuth 跳转

**account.js（v12）:**
- `_accountDoOauthBind`: 按钮 spinner + 文字变化即时反馈
- `_accountDoManualBind`: 按钮 spinner + 成功内联绿色勾 0.5s
- `_accountShowTransferConfirm`: 改用 `_accountShowModal` 统一风格 + 成功绿色勾 0.6s
- `_accountCloseModal`: 渐出动画 0.15s 后 remove

### 24.4 影响范围

修改文件：
- src/api/static/css/app.css — 7 个交互样式类 + reduced-motion 降级
- src/api/static/js/app.js — bindProvider 复用 + 版本号
- src/api/static/js/pages/account.js — spinner + 内联成功 + 模态框过渡 + 转移弹窗统一
- src/api/static/index.html — 版本号 app.css v78 / account.js v12 / app.js v147

版本号：app.css v78 / account.js v12 / app.js v147 / cache_ver 40

## 第25节：UX 修复 — 提示条移除 + 返回工作台403 + 凭据样式恢复

### 25.1 移除未绑定提示条

"🔗 还未绑定代码托管平台，点击此处完成绑定"提示条已移除。
未绑定用户通过引导页（goToRepos → _renderBindingGuide）引导绑定，不需要额外提示条。
- `_showBindBanner` / `_hideBindBanner` 删除
- `_showBindBannerIfNeeded` 置空

### 25.2 修复未绑定用户返回工作台 403

**根因**：`_accountGoBack` 先查 `currentRepo()`，如果 localStorage 有残留仓库，走 `nav('dash')` → 进入仓库依赖视图 → `require_repo_access` 检查用户是否有该仓库权限 → 未绑定用户无仓库 → 403 "No permission to access this repository"。

**修复**：`_accountGoBack` 先检查 bindings，未绑定直接 `goToRepos()` 显示引导页。

### 25.3 凭据 tab 恢复原通用配置风格

恢复和原通用配置页一致的样式（非 Apple Settings 纵向布局）：
- 标题 "Huawei Cloud"（非"云平台凭据"）
- `.srow` 横向布局（label + input + 👁 按钮）
- 👁 emoji 30x30px 方块按钮（非文字"显示/隐藏"按钮）
- 描述文字在字段下方

### 25.4 👁眼睛按钮 onclick 转义修复

**根因**：JS 单引号字符串拼接中 `\'` 转义在生成的 HTML onclick 属性中产生 `\'` 字面量，浏览器无法正确解析 JS 参数。

**修复**：改用 `&apos;` HTML 实体，浏览器解析为 `'` 后正确执行。

### 25.5 语言切换不刷新页面

**根因**：`i18n.js _rerenderCurrentPage` 未处理 `view === 'account'`，走 `app.nav('account')` 重新渲染整个页面。

**修复**：加 `view === 'account'` 处理，保持当前 sub-tab，调 `renderAccount(currentTab)`。

### 25.6 影响范围

修改文件：
- src/api/static/js/app.js — 提示条移除 + _accountGoBack 修复 + 版本号
- src/api/static/js/i18n.js — _rerenderCurrentPage 加 account 处理
- src/api/static/js/pages/account.js — 凭据样式恢复 + 👁 onclick 修复
- src/api/static/index.html — 版本号

版本号：app.css v78 / account.js v14 / app.js v148 / i18n.js v14 / cache_ver 43
