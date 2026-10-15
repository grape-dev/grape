# Grape 账号体系 OAuth 链路安全加固需求文档

> **分支**：`feat/oauth-security-hardening-330`（基于 `weekly-07-yaw`）
> **Issue**：[#330](https://gitcode.com/grape-dev/grape/issues/330)
> **关联文档**：`docs/weekly-06/grape-account-system-tech-design-v2.md`（现有账号体系设计）
> **状态**：待评审

---

## 1. 背景与目标

### 1.1 为什么需要这次改造

Grape 账号体系在 weekly-06 完成了独立账号 + 第三方 OAuth 绑定的主体建设，经过 46 轮缺陷修复和 3 轮外部安全审计后功能基本完整。但在最近一轮针对 OAuth 登录 / 绑定解绑 / token 刷新 / 冷静期匿名化等链路的逐行代码审查中，发现了一批安全漏洞、数据一致性缺陷和存储链路断裂问题。

这些问题不是设计文档层面的缺失——设计文档很完整——而是代码实现中暴露的：

- **安全漏洞**：OAuth 登录的 state 不绑定浏览器 session，存在 login CSRF 风险
- **存储链路断裂**：refresh_token 写入 external_identities 但刷新时从 user_credentials 读取，读不到
- **数据一致性**：冷静期匿名化分步执行跨多个独立事务，中间失败留下半完成状态
- **MySQL 兼容性遗漏**：cooldown.py 两处 SQL 仍用 SQLite 专有函数 `datetime('now')`
- **线上回调地址断裂**：GitCode 平台侧注册的回调地址 `/api/auth/callback/gitcode` 在代码中已被删除，GitCode OAuth 回调 404

### 1.2 改造目标

| 目标 | 衡量标准 |
|------|---------|
| 消除 login CSRF 漏洞 | 攻击者无法通过预置 code+state 让受害者登录到攻击者账号 |
| 修复 token 刷新链路 | 新注册的 OAuth 用户 token 过期后自动刷新成功 |
| 恢复 GitCode OAuth 登录 | GitCode 平台侧回调地址与代码路由一致，登录不再 404 |
| 保证匿名化数据一致性 | 冷静期匿名化要么全部成功，要么全部回滚，不残留半完成状态 |
| MySQL 兼容 | cooldown.py 无 SQLite 专有函数，MySQL 后端正常运行 |

### 1.3 改造原则

1. **不引入新基础设施**：不引入 Redis、不引入消息队列，在现有 DB + 内存缓存范围内解决
2. **不改变表结构**：通过 `_ensure_column` 幂等补列除外，不新增表、不改列定义
3. **向前兼容**：GitCode 平台侧已注册的回调地址 `/api/auth/callback/gitcode` 无需修改，代码恢复该路由
4. **不中断服务**：系统尚未正式上线，分路径改造直接删除旧统一回调地址，不保留 302 重定向兼容
5. **分优先级交付**：P0 安全/功能修复先行，P1 数据一致性紧随，P2 纵深防御/代码规范最后

---

## 2. 用户角色

| 角色 | 说明 | 本次改造中的关注点 |
|------|------|------------------|
| OAuth 用户 | 通过 GitCode/GitHub 登录 Grape 的用户 | token 过期后自动刷新，不需要重新登录 |
| 邮箱注册用户 | 通过邮箱密码注册、绑定了第三方平台的用户 | 绑定/解绑流程正常，数据不丢失 |
| 管理员 | 管理用户账号状态、注册策略的系统管理者 | 冷静期匿名化正常执行，MySQL 兼容 |
| 系统维护者 | 负责部署、配置、排障的运维人员 | 回调地址与平台侧一致，redirect_uri 生产环境固定 |
| 安全审查者 | 对系统进行安全评估的内部/外部人员 | state 防 CSRF、redirect_uri 防开放重定向 |

---

## 3. 现状分析

### 3.1 OAuth 回调地址现状

```
GitCode 平台侧注册:  https://grape.example.com/api/auth/callback/gitcode  ← 旧路径（D26/D31 改造前）
GitHub 平台侧:       未配置固定 redirect_uri
代码实际路由:        /api/auth/oauth/callback                                    ← 统一路径（D26/D31 改造后）
旧路由 /api/auth/callback/{provider}:  已删除（D1 修复）
```

**问题**：GitCode 平台侧的回调地址指向 `/api/auth/callback/gitcode`，但代码中该路由已被删除 → GitCode OAuth 回调 404。

### 3.2 token 刷新存储链路现状

```
OAuth 登录/绑定回调
  → create_external_identity / update_external_identity_tokens
  → refresh_token 写入 external_identities.refresh_token（加密）  ← 写这里

token 预刷新 (deps.py get_user_token_with_refresh)
  → try_refresh_gitcode_token → refresh_oauth_token
  → get_credential(user_id, "gitcode_oauth")
  → 从 user_credentials 表读 refresh_token                              ← 读这里
  → 读不到 → 返回 None → 预刷新失败
```

**问题**：写入和读取不在同一张表，所有新 OAuth 用户 token 刷新失效。

### 3.3 冷静期匿名化事务现状

```
Step 1: UPDATE status=2          ← db_lock 块 A（独立 commit）
Step 2: DELETE external_identities ← db_lock 块 B（独立 commit）
Step 3: DELETE 组织 + 14 张业务表   ← db_lock 块 C（独立 commit）
Step 4: UPDATE 匿名化字段          ← db_lock 块 D（独立 commit）
```

**问题**：Step 2 成功但 Step 3 失败时，status=2 已标记，绑定已删除，但业务数据残留。下次定时任务不再处理（status=2 不在 WHERE status=3 查询范围内）。

### 3.4 问题清单总览

| # | 问题 | 严重度 | 影响范围 | 代码位置 |
|---|------|--------|---------|---------|
| 1 | login state 不绑定 session → login CSRF | P0 | 所有 OAuth 登录用户 | grape_auth.py L1255 |
| 2 | validate_oauth_state SELECT+DELETE 非原子 | P0 | 集群多实例 | user/db.py L357-363 |
| 3 | refresh_token 写入与读取跨表 → 刷新失效 | P0 | 所有新 OAuth 用户 | user/db.py L2133 |
| 4 | cooldown.py datetime('now') MySQL 不兼容 | P0 | MySQL 后端 | cooldown.py L62, L126 |
| 5 | refresh_oauth_token 硬编码 if provider=="gitcode" | P1 | GitHub 用户 | user/db.py L2187 |
| 6 | merge_placeholder_account 先标记后绑定 → 不可逆 | P1 | 占位账号合并 | grape_auth.py L1313-1315 |
| 7 | cooldown 匿名化跨 4 个独立事务 | P1 | 冷静期到期账号 | cooldown.py L74-131 |
| 8 | 生产环境 redirect_uri 从 Host header 动态构造 | P1 | 生产环境 | providers.py L416-418 |
| 9 | 统一回调地址缺少纵深防御 + GitCode 404 | P2 | GitCode OAuth 登录 | grape_auth.py L1577 |
| 10 | transfer_bind 后 status=4 账号邮箱不清除 | P2 | 被合并账号的邮箱 | grape_auth.py L2117 |
| 11 | 解绑 placeholder 邮箱清理绕过数据访问层 | P2 | 代码规范 | grape_auth.py L950-966 |

---

## 4. 需求清单

### 需求 1：OAuth 登录 state 绑定浏览器 session

**用户故事**

作为一名 OAuth 用户，我希望系统确保只有我自己发起的 OAuth 登录才能完成回调，防止攻击者用预置的授权码诱导我登录到他的账号。

**问题说明**

当前 OAuth 登录发起时（`GET /api/auth/login/{provider}`），state 只存了 `{"login_flow": True}`，没有任何浏览器 session 标识。攻击者可以：

1. 自己发起 GitHub 授权，拿到合法的 `code` 和 `state`
2. 不完成回调，把回调 URL 发给受害者
3. 受害者浏览器访问回调 URL → 系统验证 state 有效（DB 里有记录，还没被消费）→ 完成 token 交换 → 签发 JWT → 受害者登录到攻击者的账号

如果受害者在攻击者的账号下托管仓库、配置 API Key，攻击者随后可以 OAuth 登录同一个账号看到这些数据。

**业务规则**

1. 发起 OAuth **登录**（`GET /api/auth/login/{provider}`）时，后端生成一个一次性 nonce（32 字节随机值的 hex），同时：
   - 将 nonce 存入 state 记录的 data_json 中（字段名 `session_nonce`）
   - 通过 `Set-Cookie` 设置一个短期 cookie：名称 `oauth_nonce`，值为 nonce，属性 `HttpOnly; Secure; SameSite=Lax; Max-Age=600; Path=/`
2. OAuth **回调**时，后端校验：
   - 从 cookie 读取 `oauth_nonce`
   - 从 state 记录读取 `session_nonce`
   - 两者必须一致；cookie 不存在、state 中无 session_nonce、或两者不匹配 → 拒绝回调，重定向到登录页并带 `oauth_error=state_mismatch`
3. 回调完成后（无论成功或失败），立即清除 `oauth_nonce` cookie（`Set-Cookie: oauth_nonce=; Max-Age=0; Path=/`）
4. **绑定流程**（已登录用户发起绑定）不需要此机制——state 中已有 `binding_user_id` 标识当前用户，login CSRF 不适用
5. **注销认证流程**（`delete-account/oauth/{provider}`）不需要此机制——state 中已有 `delete_account_user_id` 标识当前用户
6. cookie 的 `Secure` 属性在生产环境（HTTPS）下生效；开发环境（HTTP）下 `Secure` 不设置（否则 cookie 不被浏览器接受）
7. **SameSite=Lax 适用范围**：当前 OAuth 回调均为 302 GET 请求（顶层导航），SameSite=Lax 下 cookie 可正常携带。如未来接入 OIDC `response_mode=form_post`（跨站 POST），Lax 不会携带 cookie，需改为 `SameSite=None; Secure`。本次改造仅支持 GET 回调
8. **popup 模式**：popup 通过 `window.open` 打开 OAuth 授权页，回调 302 链与普通模式一致，nonce cookie 在同域内正常传递。中间页 `/oauth-popup-callback.html` 不需要读取 nonce cookie（cookie 是 HttpOnly）——nonce 校验发生在后端回调端点，中间页只负责 postMessage

**判定表**

| 场景 | state 含 login_flow | cookie oauth_nonce | state 含 session_nonce | 结果 |
|------|:---:|:---:|:---:|------|
| 正常登录 | 是 | 存在且匹配 | 存在且匹配 | 通过 |
| 攻击者预置 state（受害者无 cookie） | 是 | 不存在 | 存在 | 拒绝，重定向登录页 |
| cookie 被篡改 | 是 | 存但不匹配 | 存在 | 拒绝，重定向登录页 |
| 正常绑定 | 否（有 binding_user_id） | — | — | 不校验，通过 |
| 正常注销认证 | 否（有 delete_account_user_id） | — | — | 不校验，通过 |

**验收标准**

- [ ] 正常 OAuth 登录：发起时浏览器收到 `oauth_nonce` cookie，回调时 cookie 与 state 匹配，登录成功
- [ ] 攻击者拿到合法 state+code 后发给受害者：受害者浏览器无 `oauth_nonce` cookie（或 nonce 不匹配），回调被拒绝，重定向到登录页带 `oauth_error=state_mismatch`
- [ ] 回调完成后（成功和失败），`oauth_nonce` cookie 被清除
- [ ] 绑定流程不受影响：不设置也不校验 `oauth_nonce` cookie
- [ ] 注销认证流程不受影响：不设置也不校验 `oauth_nonce` cookie
- [ ] 开发环境（HTTP）cookie 不带 `Secure` 属性，生产环境（HTTPS）带 `Secure`
- [ ] popup 模式 OAuth 登录：nonce cookie 正常设置和校验，中间页不读 cookie

**异常处理**

- cookie 不存在时回调被拒绝，不是错误——用户可能是从书签直接访问回调 URL
- state 记录已被消费（validate_oauth_state 返回 None）时，走现有的 state_invalid 错误流程，不额外校验 cookie

---

### 需求 2：OAuth state 原子消费

**用户故事**

作为系统安全负责人，我希望同一个 OAuth state 在集群环境下只能被消费一次，防止两个实例同时处理同一个回调。

**问题说明**

当前 `validate_oauth_state` 用 SELECT 查到记录后再 DELETE，两步分开执行。虽然单实例下 db_lock（RLock）保证线程安全，但集群下两个实例各有独立的 db_lock——两个实例同时收到同一个 state（浏览器重试或攻击者重放），都可能 SELECT 成功并拿到 state 数据。

**业务规则**

1. `validate_oauth_state` 改为原子消费，确保只有一个请求拿到 state 数据
2. 统一使用 SELECT + DELETE + 检查 rowcount，SELECT 与 DELETE 必须在同一事务内
3. DELETE 条件必须包含 `state` 和 `expires > time.time()`，确保过期的 state 不能被消费
4. rowcount=0 时立即返回 None（已被消费或已过期），不能使用之前 SELECT 到的数据
5. `db_lock` 仅作为 SQLite/单实例的纵深防御；MySQL 多实例下 `db_lock` 是进程内 RLock，跨实例无效，原子性完全依赖 DELETE 的 rowcount 检查
6. 不需要 MySQL 专用 `SELECT ... FOR UPDATE`——DELETE 在 InnoDB 下会加行锁，配合 rowcount 检查即可保证原子性，避免方言判断
   ```python
   with db_lock:
       c.execute("SELECT provider, data_json, expires FROM oauth_states WHERE state=?", (state,))
       row = c.fetchone()
       if row:
           c.execute("DELETE FROM oauth_states WHERE state=? AND expires > ?", (state, time.time()))
           if c.rowcount == 0:
               return None  # 已被消费或已过期
       db.commit()
   ```
7. `validate_oauth_state` 返回 None 时，调用方必须拒绝回调（当前代码已正确处理）

**验收标准**

- [ ] 单实例：同一个 state 被两次 `validate_oauth_state` 调用，第一次返回数据，第二次返回 None
- [ ] state 过期后 `validate_oauth_state` 返回 None
- [ ] DELETE rowcount=0 时返回 None（已被消费）

**异常处理**

- DB 查询异常时返回 None（fail-closed），不允许在 DB 故障时放行回调

---

### 需求 3：OAuth 回调恢复分路径（向前兼容）

**用户故事**

作为系统维护者，我希望不同 OAuth 平台的回调路径独立，且与各平台侧已注册的回调地址保持一致——不需要去 GitCode/GitHub 平台修改回调配置。

**背景**

D26/D31 把回调从分路径 `/api/auth/callback/{provider}` 改为统一 `/api/auth/oauth/callback`，但 GitCode 平台侧的回调地址没有同步更新——旧路径 `/api/auth/callback/gitcode` 在代码中已删除（D1 修复），导致 GitCode OAuth 回调 404。

**业务规则**

1. 回调地址从统一 `/api/auth/oauth/callback` 恢复为分路径 `/api/auth/callback/{provider}`
2. 路径格式与 D26/D31 改造前一致：
   - GitCode：`/api/auth/callback/gitcode`（与 GitCode 平台侧已注册的地址一致，无需修改平台配置）
   - GitHub：`/api/auth/callback/github`
3. 回调路由从 URL 路径参数获取 provider，不再依赖 state DB 查询恢复 provider
4. state 中仍存 provider 字段，用于审计日志和二次校验——回调时校验 URL 路径中的 provider 与 state 中的 provider 一致，不一致则拒绝
5. 发起 OAuth 时 redirect_uri 改为 `get_redirect_uri(request, provider)`，返回 provider 配置中的 `redirect_uri`（完整地址，含路径）
6. 旧统一回调地址 `/api/auth/oauth/callback` 直接删除——系统尚未正式上线，不需要迁移期兼容
7. 绑定流程的回调也走分路径：`GET /api/auth/callback/{provider}` 中通过 state 的 `binding_user_id` 分流绑定逻辑
8. popup 模式回调也走分路径：中间页 URL 不变（`/oauth-popup-callback.html`），但后端回调入口改为分路径

**验收标准**

- [ ] GitCode 平台侧回调地址 `https://grape.example.com/api/auth/callback/gitcode` 无需修改，代码恢复该路由
- [ ] GitHub 平台侧回调地址为 `https://{domain}/api/auth/callback/github`
- [ ] 回调进来时 provider 从 URL 路径参数获取，不查 DB 做 provider 分流
- [ ] URL 路径中的 provider 与 state 中的 provider 不一致时，拒绝回调
- [ ] 旧统一回调地址 `/api/auth/oauth/callback` 已删除
- [ ] 绑定流程回调走分路径，state 中 `binding_user_id` 分流逻辑不变
- [ ] 新增平台时在配置文件加 provider + 在平台侧注册 `/api/auth/callback/{provider}` 回调地址

**迁移说明**

系统尚未正式上线，旧统一回调地址 `/api/auth/oauth/callback` 直接删除，不保留 302 重定向兼容。发布后 GitCode/GitHub 平台侧回调地址无需修改。

---

### 需求 4：refresh_token 存储链路统一

**用户故事**

作为一名 OAuth 用户，我希望 token 过期后系统能自动刷新，不需要重新 OAuth 登录。

**问题说明**

OAuth 登录/绑定时 refresh_token 写入 `external_identities.refresh_token`（加密存储），但 token 刷新函数 `refresh_oauth_token` 从 `user_credentials` 表读取 refresh_token——读不到。所有新注册的 OAuth 用户 token 过期后无法自动刷新。

**业务规则**

1. refresh_token 的权威存储为 `external_identities` 表（加密存储），`refresh_oauth_token` 改为从该表读取 refresh_token（解密）
2. refresh 成功后更新 `external_identities.access_token` + `external_identities.refresh_token`（如平台返回了新的 refresh_token）
3. **fallback 判定链**（从 external_identities 读 refresh_token 失败时 fallback 读 user_credentials）：
   - 查 `external_identities WHERE grape_user_id=? AND provider=? AND status=1`
   - 无记录 → fallback 读 `user_credentials`
   - 记录存在但 `refresh_token` 为空字符串 → fallback 读 `user_credentials`
   - 记录存在且非空 → 解密；解密返回空字符串（`_safe_decrypt` 失败） → fallback 读 `user_credentials`
   - 解密成功 → 使用该 refresh_token
4. **fallback 后统一错误处理**：如果 `external_identities` 解密失败，`user_credentials` 里的 refresh_token 很可能也用了同一密钥，也会解密失败。fallback 读取后仍走 `_safe_decrypt`，失败则返回 None，用户需重新 OAuth 登录
5. `user_credentials` 表中的 `{provider}_oauth` 记录不删除（迁移期保留），但不再作为主要读取来源
6. `get_user_token_with_refresh` 预刷新逻辑不变，底层 `refresh_oauth_token` 改为从 external_identities 读
7. 解绑后重新绑定时，refresh_token 从新绑定的 OAuth 回调写入 external_identities，刷新链路恢复
8. **空 refresh_token 不覆盖旧值**：`update_external_identity_tokens` 在 `refresh_token` 参数为空字符串时不执行 UPDATE 该字段（函数签名默认空字符串，空字符串不 append 到 SET 子句）。此行为正确——部分 OAuth 平台刷新后不返回新 refresh_token，应保留旧值。实现时不得改为覆盖

**数据流**

```
OAuth 登录/绑定回调
  → create_external_identity / update_external_identity_tokens
  → refresh_token 写入 external_identities.refresh_token（加密）  ← 权威存储

token 预刷新
  → refresh_oauth_token
  → 从 external_identities 读 refresh_token（解密）              ← 从权威存储读
  → fallback: external_identities 无记录时读 user_credentials    ← 迁移期兼容
  → 调 OAuth refresh endpoint
  → 更新 external_identities.access_token + refresh_token        ← 写回权威存储
```

**验收标准**

- [ ] 新注册的 OAuth 用户（通过 grape_accounts 新表）：token 过期后自动刷新成功，无需重新登录
- [ ] 迁移期老用户（user_credentials 有 refresh_token）：token 刷新成功（fallback 读取）
- [ ] 解绑后重新绑定的用户：refresh_token 从新绑定的 OAuth 回调写入 external_identities，刷新成功
- [ ] 无 refresh_token 的用户（纯 OAuth 账号从未刷新过）：预刷新返回空字符串，不报错

**异常处理**

- refresh_token 解密失败（密钥轮换后旧密钥丢失）：走 `_safe_decrypt` 返回空字符串，日志记录 warning，用户需重新 OAuth 登录
- OAuth refresh endpoint 调用失败（网络超时/平台故障）：返回 None，上层使用旧 token（可能已过期），用户操作时触发 401 重新登录

---

### 需求 5：refresh_oauth_token 消除 provider 硬编码

**用户故事**

作为系统维护者，我希望 token 刷新逻辑对所有 OAuth 平台一致，不按 provider 名硬编码分叉。

**问题说明**

`refresh_oauth_token` 中更新 external_identities 时硬编码了 `if provider == "gitcode":`，GitHub token 刷新后 external_identities 不更新，deps.py 读到旧 token。

**业务规则**

1. 删除 `refresh_oauth_token` 中的 `if provider == "gitcode":` 分支
2. 统一调用 `update_external_identity_tokens(user_id, provider, access_token=***`（适用于所有 provider）
3. `refresh_oauth_token` 从 external_identities 读 refresh_token（按需求 4），不限制 provider

**验收标准**

- [ ] gitcode 用户 token 刷新后 `external_identities.access_token` 更新
- [ ] github 用户 token 刷新后 `external_identities.access_token` 更新
- [ ] 代码中无 `if provider == "gitcode"` 或 `if provider == "github"` 条件分支（providers.py 中 GitHub email 获取的特殊处理除外，那是平台 API 差异不是 token 刷新分叉）

---

### 需求 6：占位账号合并与新绑定创建改为同事务

**用户故事**

作为系统安全负责人，我希望占位账号合并与新绑定创建是原子操作，避免占位账号被销毁但新绑定未建立的不一致状态。

**问题说明**

`_handle_oauth_bind` 中 `merge_placeholder_account` 先标记占位账号 status=4 并 commit，然后继续走 `create_external_identity`。如果后者失败，占位账号已被标记为 merged（status=4），不可逆——账号被销毁了但新绑定没建立。

**业务规则**

1. `merge_placeholder_account` 改为不在内部 commit/rollback，由调用方持有 db_lock 和事务
2. `_handle_oauth_bind` 中 unbind 旧绑定 + create 新绑定 + 标记占位账号 status=4 三步在同一个 `with db_lock:` 事务内执行
3. **执行顺序**（修正：原需求文档写"先 create 再标记 status=4"会导致 `UNIQUE(provider, external_id)` 约束冲突，因为占位账号的旧绑定还是 status=1）：
   - **Step 1：unbind 旧绑定**——把占位账号的 `external_identities` 记录标记为 `status=0`（软删除，清空 access_token/refresh_token）
   - **Step 2：create 新绑定**——此时 `create_external_identity` 会查到 `status=0` 的记录，走"恢复"逻辑（UPDATE status=0 → status=1，改 grape_user_id），而不是 INSERT，避免 UNIQUE 约束冲突
   - **Step 3：标记占位账号 status=4**——更新 `grape_accounts SET status=4, username='merged_' || CAST(id AS TEXT), email=NULL, ...`
4. **额外检查 UNIQUE(grape_user_id, provider)**：如果当前绑定用户已有该 provider 的活跃记录（status=1），也要先处理（报冲突或走解绑流程），否则 create 时同样会 UNIQUE 冲突
5. Step 1（unbind）失败时：占位账号不变（status=1），新绑定未建立——状态一致
6. Step 2（create）失败时：事务回滚，unbind 撤销，占位账号恢复 status=1——状态一致
7. Step 3（标记 status=4）失败时：事务回滚，unbind 撤销，新绑定撤销——状态一致
8. `merge_placeholder_account` 不再内部 commit 和 rollback，由调用方控制事务生命周期
9. 实现方式可选：
   - 方案 A：`merge_placeholder_account` 去掉内部 `with db_lock:` / `commit()` / `rollback()`，改为接收调用方的 cursor 参数
   - 方案 B：拆成 `unbind_placeholder_binding(cursor, ...)` + `mark_account_merged(cursor, ...)` 两步，由调用方组合
   - 两种方案都需要 `create_external_identity` 同样支持外部传 cursor（或新增 `_in_tx` 版本）

**验收标准**

- [ ] 占位账号合并 + 新绑定创建全部成功：占位账号 status=4 + email=NULL，旧绑定 status=0，新绑定 status=1
- [ ] unbind 旧绑定失败：占位账号 status=1 不变，无新绑定
- [ ] create 新绑定失败：事务回滚，unbind 撤销，占位账号 status=1 恢复，旧绑定 status=1 恢复
- [ ] 标记 status=4 失败：事务回滚，unbind 撤销，新绑定撤销，占位账号 status=1 恢复
- [ ] 代码中 unbind + create + mark status=4 在同一个 `with db_lock:` 块内
- [ ] 当前绑定用户已有该 provider 活跃记录时，正确处理冲突（不直接 INSERT 导致 UNIQUE 报错）

---

### 需求 7：合并账号邮箱清理

**用户故事**

作为系统维护者，我希望被合并的账号（status=4）不占用邮箱唯一索引，邮箱可被新用户注册。

**问题说明**

`transfer_bind` 和 `merge_placeholder_account` 标记 status=4 时只把 username 改为 `merged_{id}`，email 不清。email 仍占唯一索引，新用户无法用该邮箱注册。

**业务规则**

1. `transfer_bind` 中占位账号标记 status=4 时，同时清空邮箱相关字段：
   - `email=NULL, email_source=NULL, email_is_placeholder=0`
2. `merge_placeholder_account` 中同样清空邮箱（与 transfer_bind 保持一致）
3. 邮箱设为 NULL 后不参与唯一索引（SQL 标准），新用户可注册该邮箱
4. 如果 status=4 账号有真实邮箱（非 placeholder），清空前不发送通知——账号已被合并，通知无意义

**验收标准**

- [ ] `transfer_bind` 后 status=4 账号的 email 为 NULL
- [ ] `merge_placeholder_account` 后 status=4 账号的 email 为 NULL
- [ ] 被合并账号的原邮箱可被新用户注册
- [ ] 被合并账号的原用户名（已改为 `merged_{id}`）可被新用户注册

---

### 需求 8：冷静期匿名化改为单事务

**用户故事**

作为系统维护者，我希望冷静期匿名化的数据清理步骤是原子操作，不出现半完成状态——要么全部成功，要么全部回滚。

**问题说明**

`_anonymize_account` 的 Step 1-4 各自在独立的 `with db_lock:` 块中执行。Step 2 成功但 Step 3 失败时，status=2 已标记、绑定已删除，但业务数据残留。下次定时任务不再处理（status=2 不在查询范围内）。

**业务规则**

1. Step 1-4 全部放入同一个 `with db_lock:` 事务，任一步骤失败整体 rollback
2. 幂等性由 Step 1 的 `WHERE status=3` 保证——已被处理的账号（status=2）rowcount=0，后续步骤对 status=2 的账号执行是安全的（DELETE 已删除数据是 no-op，清空已空字段是 no-op）
3. Step 4 的 `updated_at` 用 Python `time.strftime` 生成时间戳（同需求 9）
4. Step 5（审计日志）保持独立——审计日志写入失败不应回滚匿名化操作

**事务流程**

```
with db_lock:
    Step 1: UPDATE status=2 WHERE id=? AND status=3       ← rowcount=0 则幂等跳过
    Step 2: DELETE FROM external_identities WHERE grape_user_id=?
    Step 3: DELETE 组织 + 14 张业务表
    Step 4: UPDATE username='deleted_{id}', email=NULL, password_hash='', ...
    commit                                                ← 全部成功才 commit
    # Step 5 审计日志在 commit 之后单独执行
```

**验收标准**

- [ ] 匿名化全部步骤成功：status=2，绑定删除，业务数据删除，字段匿名化
- [ ] 匿名化中间步骤失败：事务回滚，status 保持 3，下次定时任务重试
- [ ] 匿名化已完成的账号（status=2）再次执行：幂等，所有步骤 no-op
- [ ] 审计日志写入失败不影响匿名化结果

---

### 需求 9：cooldown.py 消除 datetime('now') MySQL 不兼容

**用户故事**

作为系统维护者，我希望冷静期定时任务在 MySQL 后端下正常运行。

**问题说明**

D46 P1-14 修复了 `grape_account_db.py` 中的两处 `datetime('now')`，但 `cooldown.py` 的 Step 1（L62）和 Step 4（L126）遗漏——在 MySQL 后端下 `datetime('now')` 不是合法函数，匿名化直接报错。

**业务规则**

1. `_anonymize_account` Step 1 的 `updated_at=datetime('now')` 改为 Python `time.strftime("%Y-%m-%d %H:%M:%S")`
2. `_anonymize_account` Step 4 的 `updated_at=datetime('now')` 同上
3. 与 D46 P1-14 修复方式一致
4. 本需求与需求 8（单事务改造）同步实施——改单事务时一并修复 datetime

**验收标准**

- [ ] `cooldown.py` 中无 `datetime('now')` SQL 内建函数
- [ ] MySQL 后端下冷静期匿名化正常执行
- [ ] SQLite 后端下行为不变

---

### 需求 10：解绑时 placeholder 邮箱清理走数据访问层

**用户故事**

作为系统维护者，我希望解绑时的邮箱清理通过统一的数据访问层接口完成，不绕过它直接写 SQL。

**问题说明**

`unbind_provider` 中清理 placeholder 邮箱时直接在 `grape_auth.py` 中写 SQL 操作 `grape_accounts` 表，绕过了 `grape_account_db.py` 数据访问层。如果 `grape_account_db.py` 的 `update_email` 未来增加逻辑（缓存失效、审计日志），这里不会同步。

**业务规则**

1. `grape_account_db.py` 新增 `clear_placeholder_email(user_id: int, provider: str) -> bool` 函数
   - 查询当前 email、email_source、email_is_placeholder
   - 如果 email 是 placeholder 且 email_source == provider → UPDATE `email=NULL, email_source=NULL, email_is_placeholder=0`
   - 否则不动，返回 True（非 placeholder 邮箱不需要清理）
2. `unbind_provider` 中的邮箱清理逻辑改为调用 `clear_placeholder_email(user["id"], provider)`
3. `grape_auth.py` 中不再直接写 SQL 操作 `grape_accounts` 表

**验收标准**

- [ ] `grape_auth.py` `unbind_provider` 中无直接 SQL 操作 `grape_accounts` 表
- [ ] 解绑后 placeholder 邮箱被清理（email=NULL, email_source=NULL, email_is_placeholder=0）
- [ ] 非 placeholder 邮箱不受影响

---

### 需求 11：生产环境 redirect_uri 强制固定

**用户故事**

作为系统安全负责人，我希望生产环境的 OAuth redirect_uri 从固定配置读取，不从请求 Host header 动态构造。

**问题说明**

`get_redirect_uri` 从请求 Host header 动态构造 redirect_uri。虽然 `_get_trusted_host` 做了 TRUSTED_HOSTS 白名单校验，但动态构造本身就是危险点——生产环境应从固定配置读取。

**业务规则**

1. redirect_uri 作为 provider 配置字段，配置在 `configs/oauth-providers.json` 中每个 provider 的 `redirect_uri` 字段（完整地址，含路径）
2. `_sync_providers_from_config` 不剥离 `redirect_uri`（它是协议参数，不是凭据），随 provider 配置一起写入 meta 表 `oauth_providers_config`
3. `get_redirect_uri(request, provider)` 从 provider 配置读取 `redirect_uri` 字段：
   - provider 配置有 `redirect_uri` → 直接返回该值（生产 + 开发环境均适用）
   - provider 配置无 `redirect_uri` → 开发环境 fallback 从 Host header 构造 `{scheme}://{host}/api/auth/callback/{provider}`
   - provider 配置无 `redirect_uri` 且生产环境 → 拒绝启动（启动校验拦截）
4. 生产环境启动时校验：`is_production()` 且任意已配置 provider 缺少 `redirect_uri` → 拒绝启动，报错信息明确列出缺失的 provider
5. 开发环境（非 production）：provider 未配 `redirect_uri` 时从 Host header 动态构造（方便本地开发）
6. TRUSTED_HOSTS 白名单校验保留（开发环境 fallback 构造时校验 Host header）
7. **移除 `OAUTH_REDIRECT_URI` 环境变量**和 `security_config.get_oauth_redirect_uri()`——redirect_uri 统一从 provider 配置读取，不再使用环境变量或 security.json
8. **直接使用 `security_config.is_production()`**，不通过 `email_service.is_production_env()` 间接调用

**验收标准**

- [ ] 生产环境任意 provider 未配置 `redirect_uri` 时，服务拒绝启动并报错
- [ ] 生产环境 redirect_uri 从 provider 配置读取，不含 Host header 值
- [ ] 开发环境 provider 配了 `redirect_uri` 时使用配置值
- [ ] 开发环境 provider 未配 `redirect_uri` 时从 Host header 动态构造（行为不变）
- [ ] 代码中无 `OAUTH_REDIRECT_URI` 环境变量引用
- [ ] 代码中无 `security_config.get_oauth_redirect_uri()`

**配置示例**

```json
// configs/oauth-providers.json
{
  "gitcode": {
    "name": "GitCode",
    "auth_url": "https://gitcode.com/oauth/authorize",
    "token_url": "https://gitcode.com/oauth/token",
    "user_url": "https://gitcode.com/api/v5/user",
    "redirect_uri": "https://grape.example.com/api/auth/callback/gitcode",
    "scope": "all",
    "id_field": "id",
    "login_field": "login",
    "name_field": "nickname",
    "pkce_enabled": true,
    "brand_color": "#e7330f",
    "whitelist_column": "gitcode_username",
    "accept_header": "application/json",
    "client_id": "",
    "client_secret": ""
  },
  "github": {
    "name": "GitHub",
    "auth_url": "https://github.com/login/oauth/authorize",
    "token_url": "https://github.com/login/oauth/access_token",
    "user_url": "https://api.github.com/user",
    "redirect_uri": "https://grape.example.com/api/auth/callback/github",
    "scope": "read:user user:email",
    "id_field": "id",
    "login_field": "login",
    "name_field": "name",
    "pkce_enabled": true,
    "brand_color": "#24292f",
    "whitelist_column": "github_username",
    "accept_header": "application/vnd.github+json",
    "client_id": "",
    "client_secret": ""
  }
}
```

```python
# get_redirect_uri 逻辑
def get_redirect_uri(request, provider: str) -> str:
    cfg = get_provider(provider)
    if cfg and cfg.get("redirect_uri"):
        return cfg["redirect_uri"]
    # fallback：开发环境从 Host header 构造
    path = f"/api/auth/callback/{provider}"
    scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else "http"
    host = _get_trusted_host(request)
    return f"{scheme}://{host}{path}"
```

---

## 5. 实施计划

### 5.1 优先级与工作量

| 优先级 | 需求 | 工作量 | 理由 |
|:---:|------|:---:|------|
| P0 | 需求 1：state 绑定 session | 1 天 | login CSRF 漏洞，直接影响用户安全 |
| P0 | 需求 2：state 原子消费 | 0.5 天 | 集群下 state 双消费，安全风险 |
| P0 | 需求 4：refresh_token 链路统一 | 1 天 | 所有新 OAuth 用户 token 刷新失效 |
| P0 | 需求 9：cooldown datetime 修复 | 0.5 天 | MySQL 下匿名化直接报错 |
| P1 | 需求 5：消除 provider 硬编码 | 0.5 天 | GitHub token 不更新，与需求 4 同源 |
| P1 | 需求 6：merge 同事务 | 0.5 天 | 占位账号合并不可逆失败 |
| P1 | 需求 8：cooldown 单事务 | 0.5 天 | 匿名化半完成状态 |
| P1 | 需求 11：redirect_uri 固定 | 0.5 天 | 开放重定向风险 |
| P2 | 需求 3：回调分路径 | 1 天 | 纵深防御 + 修复 GitCode 404 |
| P2 | 需求 7：status=4 邮箱清理 | 0.5 天 | 邮箱占用，影响范围小 |
| P2 | 需求 10：解绑走数据访问层 | 0.5 天 | 代码规范，无功能影响 |

**总计**：约 7.5 人天

### 5.2 交付批次

| 批次 | 包含需求 | 目标 | 交付物 |
|------|---------|------|--------|
| 第一批 | 需求 1, 2, 4, 9 | 消除安全漏洞 + 修复核心功能 | P0 修复 + 测试 + 验证报告 |
| 第二批 | 需求 5, 6, 8, 11 | 数据一致性 + 安全加固 | P1 修复 + 测试 |
| 第三批 | 需求 3, 7, 10 | 纵深防御 + 代码规范 | P2 修复 + 迁移说明 |

### 5.3 依赖关系

```
需求 4（refresh_token 链路） ──→ 需求 5（消除硬编码）     ← 同源，需求 4 先做
需求 8（cooldown 单事务）   ──→ 需求 9（datetime 修复）    ← 同文件，合并做
需求 3（分路径）           ──→ 需求 11（redirect_uri 固定）  ← 分路径后 redirect_uri 格式才确定
需求 6（merge 同事务）      ──  独立
需求 7（邮箱清理）          ──  独立（但建议与需求 6 同批，都在 merge_placeholder_account）
需求 1（state 绑定 session）──  独立
需求 2（state 原子消费）     ──  独立
需求 10（解绑走数据访问层）  ──  独立
```

---

## 6. 约束

1. **不引入新基础设施**：不引入 Redis、消息队列，在现有 DB + 内存缓存范围内解决
2. **不改变表结构**：通过 `_ensure_column` 幂等补列除外，不新增表、不改列定义
3. **向前兼容**：GitCode/GitHub 平台侧已注册的回调地址无需修改
4. **不中断服务**：系统尚未正式上线，分路径改造直接删除旧统一回调地址，不保留 302 重定向兼容
5. **分优先级交付**：P0 先行，P1 紧随，P2 最后
6. **测试要求**：所有修改需通过现有单元测试 + 新增安全场景测试
7. **方案先行**：技术设计文档经评审确认后再进入实现

---

## 7. 术语表

| 术语 | 说明 |
|------|------|
| OAuth state | 服务端生成的随机值，防止 CSRF，存在 oauth_states 表中，一次性消费 |
| login CSRF | 攻击者预置合法的 OAuth code+state，诱导受害者完成回调，受害者登录到攻击者账号 |
| exchange_code | OAuth 回调成功后生成的一次性交换码（10s 过期），前端用它换 JWT |
| placeholder 邮箱 | OAuth 平台返回的 noreply 邮箱（如 `xxx@noreply.gitcode.com`），不是用户真实邮箱 |
| 占位账号 | OAuth 自动创建的无密码无邮箱账号（email_is_placeholder=1, created_by_oauth=1） |
| status=4 (merged) | 占位账号被合并后的终态，不可恢复，username 改为 `merged_{id}` |
| db_lock | session_manager.py 中的 `threading.RLock()`，全局可重入锁 |
| _ensure_column | 幂等补列机制，通过 `ALTER TABLE ADD COLUMN IF NOT EXISTS` 方式给已有表加列 |
