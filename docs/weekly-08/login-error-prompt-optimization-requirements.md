# 登录错误提示优化 + 401 语义混淆全流程修复

> **实现状态声明**：本文档为零实现需求提案。涉及前端 api.js 逻辑修复 + 后端/前端提示词优化 + i18n 语言包同步。
> 设计文档确认后方可实施。

## 一、总览

### 1.1 背景

用户在登录页输入错误的账号或密码时，前端提示"登录失败: 会话过期，请重新登录"，而非后端实际返回的"账号或密码错误"。

同时，现有登录错误提示词整体存在语气生硬、缺少行动指引、暴露技术细节等问题，不符合 Apple HIG 错误状态设计原则。

### 1.2 需求范围

| 编号 | 需求 | 类型 |
|------|------|------|
| R1 | 修复登录 401 误触发 token refresh 导致提示词被覆盖的 bug | Bug 修复 |
| R2 | 优化登录失败错误提示词，对齐 Apple HIG 错误状态设计原则 | 体验优化 |
| R3 | 前端展示方式优化：去掉冗余"登录失败:"前缀 | 体验优化 |
| R4 | i18n 语言包同步（zh + en） | i18n |
| R5 | /auth/exchange 错误处理优化，展示后端具体错误消息 | Bug 修复 |
| R6 | 统一前端 15 处直接 fetch 调用的 401 处理，复用 handleResp | Bug 修复 |
| R7 | 跳转 /login 保留 /en 语言前缀 | Bug 修复 |
| R8 | api.js 健壮性优化（200+HTML 提示词 + 网络错误重试策略） | 健壮性 |
| R9 | api.js 健壮性补充（upload 401 + reauth 去重 + knowledge 静默 + pipeline alert） | Bug 修复 |
| R10 | 403 全局统一处理（评估项，不在本次范围） | 评估 |
| R11 | admin change-password 状态码对齐 + 跳登录方式统一 | Bug 修复 |

> **Epic 视图**：R1-R11 是"登录 401 语义 + api.js 健壮性"域。原始需求还包含以下已拆分到独立分支的需求项：
> - R12（对话全屏 z-index）→ `feat/fullscreen-zindex-fix`
> - R13（skill/scene 同步合并）→ `feat/skill-scene-sync-merge`
> - R14（一键部署脚本）→ `feat/one-click-deploy-script`
>
> 拆分理由：R12/R13/R14 与登录 401 链路零交集，分属不同域（events.js 交互缺陷 / 后端存储架构 / 部署工具新功能），合在一个 MR 会导致 diff 评审范围扩大、验证失焦、回滚连带。各独立分支有自己的需求文档和设计文档。

### 1.3 不在范围

- 登录限流策略调整（阈值、窗口、锁定时长不变）
- 后端审计日志格式不变（只改用户可见的 message 字段）
- OAuth 登录流程的错误提示（仅涉及账号密码登录）
- 密码修改/重置流程的提示词（仅涉及登录端点）
- R12/R13/R14（已拆分到独立分支，见 1.2 Epic 视图）

---

## 二、R1：修复登录 401 误触发 token refresh

### 2.1 问题描述

**现象**：用户在登录页输入错误密码，前端提示"登录失败: 会话过期，请重新登录"。

**根因**：`api.js` 的 `handleResp` 函数对所有 401 响应统一走 token refresh 流程，没有区分两种 401：

| 401 类型 | 触发场景 | 正确处理 |
|----------|---------|---------|
| 凭证错误 401 | 登录端点 `/auth/login` 拒绝账号/密码 | 直接展示后端返回的错误消息 |
| 会话过期 401 | 受保护端点的 JWT 过期 | 尝试 refresh token，成功后重发请求 |

**调用链路**：

```
用户输入错误密码
→ API.post('/auth/login', {identifier, password})          // api.js:327 _post
→ 后端返回 401 {"code":401, "message":"账号或密码错误"}
→ handleResp(r, retryFn)                                    // api.js:194
→ r.status === 401 && retryFn                               // api.js:196 — retryFn 总是被传入
→ _safeErrJson(r).then(err => ...)                          // api.js:199
→ err.detail.action !== 'reauth_required'                   // 不是 OAuth 过期，跳过
→ handle401(retryFn)                                        // api.js:206 — 无条件走 refresh
→ doRefresh()                                               // api.js:73
→ fetch('/api/auth/refresh', ...)                           // 用户在登录页，无有效 refresh_token cookie
→ refresh 失败
→ throw new Error(I18N.t('common.session_expired'))        // api.js:181 — "会话过期，请重新登录"
→ app.js:2177 catch
→ showToast(I18N.t('app.login.failed') + ': ' + errMsg(e))
→ 最终展示："登录失败: 会话过期，请重新登录"
```

**影响**：后端返回的"账号或密码错误"消息被丢弃，用户看到的是误导性提示"会话过期"。

### 2.2 触发条件

- 用户未登录（localStorage 中无 token）
- 用户在登录页输入错误的账号或密码
- 后端 `/auth/login` 返回 401

### 2.3 修复要求

在 `handleResp` 的 401 分支中，先检查响应体和用户登录状态：

- 如果响应体中有 `detail.message`（后端业务层 401）且用户当前无 token（未登录状态），直接抛出后端消息，不走 refresh 流程
- 其余 401 保持现有逻辑（尝试 refresh）

**判断条件**：`!localStorage.getItem('token')` 表示用户未登录，此时的 401 是凭证错误而非会话过期。

### 2.4 验证标准

| 场景 | 预期行为 |
|------|---------|
| 未登录 + 错误密码 | 展示"账号或密码不正确，请重新输入"，不触发 refresh |
| 未登录 + 正确密码 | 正常登录，不触发 refresh |
| 已登录 + token 过期访问受保护端点 | 触发 refresh，成功后重发请求 |
| 已登录 + refresh 也失败 | 展示"会话过期，请重新登录"，跳转登录页 |
| 未登录 + 账号被禁用（后端返回 403） | 展示"该账号已被停用，请联系管理员"——403 不走 401 分支，本来就不触发 refresh，R1 不影响此场景 |
| 未登录 + 账号锁定（后端返回 429） | 展示锁定提示——429 不走 401 分支，本来就不触发 refresh，R1 不影响此场景 |

> **注意**：R1 修复范围仅限 `/auth/login` 返回 401（密码错误）的场景。账号禁用（403）、冷静期（403）、锁定（429）、IP 限流（429）使用不同的 HTTP 状态码，不走 handleResp 的 401 分支，本来就不会误触发 refresh。这些场景的正确展示由 R2（提示词优化）+ R4（error_code i18n 映射）保证。

---

## 三、R2：优化登录失败错误提示词

### 3.1 设计原则（Apple HIG）

| 原则 | 来源 | 当前违反点 |
|------|------|-----------|
| State what happened and what the user can do | Apple HIG Alerts | 缺少行动指引 |
| Never blame the user — attribute to system/network | Apple HIG Alerts | "错误"语气指责 |
| Never expose internal technical details to users | Apple HIG | 暴露 IP、失败次数计数 |
| Every error state has an exit / recovery action | Apple HIG | "账号已被禁用"是死胡同 |
| Copy voice: short, confident, no redundant labels | Apple HIG | "登录失败:"前缀冗余 |

### 3.2 提示词优化对照表

#### R2-1：账号或密码错误

| 项目 | 当前 | 优化后 |
|------|------|--------|
| 中文 | 账号或密码错误 | 账号或密码不正确，请重新输入 |
| 英文 | (无 i18n，后端硬编码) | The account or password is incorrect. Please try again. |
| 位置 | grape_auth.py:410, 2035 | 同 |
| 原则 | 中性陈述（"不正确"而非"错误"）+ 行动指引（"请重新输入"） |

#### R2-2：账号被禁用

| 项目 | 当前 | 优化后 |
|------|------|--------|
| 中文 | 账号已被禁用 | 该账号已被停用，请联系管理员 |
| 英文 | (无 i18n) | This account has been deactivated. Please contact the administrator. |
| 位置 | grape_auth.py:420, 2042 | 同 |
| 原则 | 提供出路（"请联系管理员"），不用"禁用"（更接近"deactivated"的中性表达） |

#### R2-3：注销冷静期

| 项目 | 当前 | 优化后 |
|------|------|--------|
| 中文 | 账号已注销，处于冷静期 | 该账号正在注销中，暂时无法登录 |
| 英文 | (无 i18n) | This account is being deactivated and cannot be accessed at this time. |
| 位置 | grape_auth.py:419 | 同 |
| 原则 | 用户可理解的语言，不暴露内部状态术语"冷静期" |

#### R2-4：账号锁定

| 项目 | 当前 | 优化后 |
|------|------|--------|
| 中文 | 连续登录失败 N 次，账号已临时锁定 N 分钟。可通过邮箱验证码立即解锁。 | 出于安全保护，账号已临时锁定。请 N 分钟后重试，或通过邮箱验证码立即解锁。 |
| 英文 | (无 i18n) | For security reasons, this account has been temporarily locked. Please try again in N minutes, or use email verification to unlock immediately. |
| 位置 | login_throttle.py:122-124 | 同 |
| 原则 | 归因"安全保护"而非数落用户失败次数；保留解锁路径 |

#### R2-5：IP 限流

| 项目 | 当前 | 优化后 |
|------|------|--------|
| 中文 | 您的 IP 请求过于频繁，请 N 分钟后再试 | 操作过于频繁，请 N 分钟后再试 |
| 英文 | (无 i18n) | Too many attempts. Please try again in N minutes. |
| 位置 | login_throttle.py:107 | 同 |
| 原则 | 不暴露技术术语"IP"；"操作过于频繁"是用户可理解的表达 |

### 3.3 参考依据

Apple 自家产品（appleid.apple.com / iCloud / App Store）的登录错误提示：

| 场景 | Apple 原文 | 设计意图 |
|------|-----------|---------|
| 密码错误 | Your Apple ID or password was incorrect. | 中性陈述，不指责 |
| 账号锁定 | This Apple ID has been locked for security reasons. | 归因安全，不提失败次数 |
| 账号停用 | This Apple ID is not active. | 简洁，不加冗余修饰 |
| 频率限制 | Try again later. | 极简，不暴露技术细节 |

---

## 四、R3：前端展示优化

### 4.1 去掉冗余"登录失败:"前缀

**当前代码**（app.js:2177）：
```js
showToast(I18N.t('app.login.failed') + ': ' + errMsg(e))
// 实际效果："登录失败: 账号或密码错误"
```

**优化后**：
```js
showToast(errMsg(e))
// 实际效果："账号或密码不正确，请重新输入"
```

**依据**：Apple HIG "Copy voice: short, confident, no redundant labels"。用户点了登录没进去，已经知道失败了，"登录失败"是冗余信息。

### 4.2 保持特殊错误的原有展示逻辑

以下错误维持现有特殊处理不变（app.js:2170-2175）：

| 错误类型 | 展示方式 | 原因 |
|----------|---------|------|
| 账号锁定 / IP 限流 | showToast(msg) 直接显示后端消息 | 含动态时长信息，需完整展示 |
| 验证码 / 人机验证 | showToast(msg) 直接显示后端消息 | 需展示验证码指引 |
| 白名单限制 | _showWhitelistGuidance() | 需引导用户配置白名单 |

### 4.3 改动范围

仅改动 app.js:2177 这一行——else 分支中去掉 `I18N.t('app.login.failed') + ': '` 前缀。

其余分支（锁定、验证码、白名单）不变。

---

## 五、R4：i18n 语言包同步

### 5.1 当前问题

- 后端 5 处提示词全部硬编码中文，无英文版本
- 前端 `app.login.failed`（"登录失败" / "Login failed"）在 R3 后不再用于登录错误展示，但保留在语言包中不删除（其他地方可能引用）
- `common.session_expired`（"会话过期，请重新登录"）保留不变（R1 修复后仅在真正的会话过期场景使用）

### 5.2 语言包改动

#### zh/app.json

| key | 当前值 | 新增值 |
|-----|--------|--------|
| app.login.credential_incorrect | (无) | 账号或密码不正确，请重新输入 |
| app.login.account_deactivated | (无) | 该账号已被停用，请联系管理员 |
| app.login.account_deactivating | (无) | 该账号正在注销中，暂时无法登录 |
| app.login.account_locked | (无) | 出于安全保护，账号已临时锁定。请 {minutes} 分钟后重试，或通过邮箱验证码立即解锁。 |
| app.login.too_many_attempts | (无) | 操作过于频繁，请 {minutes} 分钟后再试 |

#### en/app.json

| key | 值 |
|-----|-----|
| app.login.credential_incorrect | The account or password is incorrect. Please try again. |
| app.login.account_deactivated | This account has been deactivated. Please contact the administrator. |
| app.login.account_deactivating | This account is being deactivated and cannot be accessed at this time. |
| app.login.account_locked | For security reasons, this account has been temporarily locked. Please try again in {minutes} minutes, or use email verification to unlock immediately. |
| app.login.too_many_attempts | Too many attempts. Please try again in {minutes} minutes. |

### 5.3 后端 i18n 方向

后端提示词当前硬编码中文。根据现有架构（error_code + 前端映射的方向），有两种方案：

**方案 A（最小改动）**：后端继续返回中文 message，前端根据 error_code 映射到 i18n key。

后端在已有的 `{"code": 401, "message": "..."}` 基础上增加 `error_code` 字段：
```json
{"code": 401, "error_code": "CREDENTIAL_INCORRECT", "message": "账号或密码不正确，请重新输入"}
```

前端 api.js 解析 error_code，用 I18N.t() 渲染对应语言包。

**方案 B（后端 i18n）**：后端根据请求 lang 参数返回对应语言的 message。

考虑到现有代码中已有 `error_code` 模式（admin.json 中有 INVALID_CREDENTIALS 等），且用户的 i18n 纪律是"error_code + 前端映射，原始 message 进 console"，**采用方案 A**。

### 5.4 error_code 映射表

#### 登录认证端点（凭证 401）

| 场景 | error_code | i18n key |
|------|-----------|----------|
| 账号或密码不正确 | CREDENTIAL_INCORRECT | app.login.credential_incorrect |
| 账号已被停用 | ACCOUNT_DEACTIVATED | app.login.account_deactivated |
| 注销冷静期 | ACCOUNT_DEACTIVATING | app.login.account_deactivating |
| 账号锁定 | ACCOUNT_LOCKED | app.login.account_locked |
| 操作过于频繁 | TOO_MANY_ATTEMPTS | app.login.too_many_attempts |

#### 会话认证中间件（会话 401，需新增 error_code）

| 场景 | 当前 message | error_code（新增） | i18n key |
|------|-------------|-------------------|----------|
| 未登录 | Not logged in | NOT_LOGGED_IN | app.error.NOT_LOGGED_IN |
| 会话过期 | Session expired | SESSION_EXPIRED | app.error.SESSION_EXPIRED |
| 用户不存在 | User not found | USER_NOT_FOUND | app.error.USER_NOT_FOUND |
| 会话被撤销 | Session revoked | SESSION_REVOKED | app.error.SESSION_REVOKED |
| 账号已被禁用 | 账号已被禁用 | ACCOUNT_DISABLED | app.error.ACCOUNT_DISABLED |
| 会话无效 | Session invalid | SESSION_INVALID | app.error.SESSION_INVALID |
| refresh_token 缺失 | 缺少 refresh_token | MISSING_REFRESH_TOKEN | app.error.MISSING_REFRESH_TOKEN |
| refresh_token 过期 | refresh_token 过期或无效 | REFRESH_TOKEN_INVALID | app.error.REFRESH_TOKEN_INVALID |

**注意**：会话 401 的 error_code 用于 refresh 失败后展示给用户的提示。其中 SESSION_REVOKED 应映射为"您的账号在其他设备上修改了密码，请重新登录"而非通用的"会话过期"。

### 5.5 前端 error_code 渲染逻辑

api.js 错误对象已携带 error_code（api.js:230 `e.error_code = detail.error_code`）。app.js 登录 catch 分支中增加 error_code → i18n 映射：

```js
// 优先用 error_code 映射 i18n，兜底用后端 message
function _loginErrorMsg(e) {
    var code = e.error_code || '';
    var key = {
        'CREDENTIAL_INCORRECT': 'app.login.credential_incorrect',
        'ACCOUNT_DEACTIVATED': 'app.login.account_deactivated',
        'ACCOUNT_DEACTIVATING': 'app.login.account_deactivating',
        'ACCOUNT_LOCKED': 'app.login.account_locked',
        'TOO_MANY_ATTEMPTS': 'app.login.too_many_attempts',
    }[code];
    if (key) return I18N.t(key);
    return errMsg(e);  // 兜底：直接用后端 message
}
```

---

## 九、改动点汇总

| 文件 | 改动内容 | 需求编号 |
|------|---------|---------|
| src/api/static/js/api.js | handleResp 401 分支：无 token 时不走 refresh | R1 |
| src/api/static/js/api.js | 跳转 /login 保留 /en 语言前缀 | R7 |
| src/api/static/js/api.js | 200+HTML 改为 server_error + 网络错误不重试 | R8 |
| src/api/static/js/api.js | upload 401 对齐 fetch + reauth 去重 | R9 |
| src/api/static/js/api.js | doRefresh 跳登录统一为 showLogin | R11 |
| src/admin/routes.py | change-password 旧密码错误 401→400 | R11 |
| src/api/static/js/app.js | 登录 catch 去掉"登录失败:"前缀 + error_code i18n 映射 | R3, R4 |
| src/api/static/js/app.js | /auth/exchange 解析 401 响应体展示具体错误 | R5 |
| src/api/routes/grape_auth.py | 5 处提示词优化 + 增加 error_code 字段 | R2, R4 |
| src/auth/login_throttle.py | 2 处提示词优化 + error_code | R2, R4 |
| src/api/static/locales/zh/app.json | 新增 5 个 i18n key | R4 |
| src/api/static/locales/en/app.json | 新增 5 个 i18n key | R4 |
| src/api/static/js/pages/events.js | 8 处直接 fetch 改用 API.get/post/del 或 _fetchWithAuth（含 2 处 SSE 流） | R6 |
| src/api/static/js/pages/issues.js | 3 处改用 API.upload/API.del（2 处 FormData 上传 + 1 处 DELETE 删除附件） | R6 |
| src/api/static/js/pages/skills.js | 1 处下载改用 API.getRaw（blob 下载需原始 Response） | R6 |
| src/api/static/js/pages/chat.js | 3 处改用 API.get + 1 处 SSE 流用 API.ensureTokenValid（2 处任务状态查询 + 1 处 SSE 流） | R6 |
| src/api/static/js/pages/knowledge.js | 2 处增加加载失败提示 | R9 |
| src/api/static/js/pages/pipeline.js | 4 处 alert() 改为 showToast()（含成功+失败场景） | R9 |

> **R12/R13/R14 改动文件**：已拆分到独立分支，不在此改动点汇总中。见 §1.2 Epic 视图。

---

## 七、401 语义混淆全流程全场景分析

通过对后端全部 44 处 `status_code=401` 和前端所有 fetch 调用的逐条排查，发现以下 8 个问题。

### 7.1 401 的三种语义分类

后端所有 401 响应按语义分为三类：

| 语义 | 含义 | 产生场景 | 正确处理 |
|------|------|---------|---------|
| 凭证 401 | 账号/密码/token 本身不对 | 登录、refresh、exchange 等公开认证端点 | 直接展示错误消息 |
| 会话 401 | JWT access token 过期/失效 | 受保护端点的认证中间件 (deps.py) | 尝试 refresh，成功后重发 |
| reauth 401 | OAuth token 过期 | VCS 平台 API 调用 (oauth_token.py) | 跳转平台授权页 |

reauth 401 已通过 `action: "reauth_required"` 标记被前端正确识别。**此类无问题。**

### 7.2 后端 401 端点全量分类

#### 凭证 401（公开认证端点，不应触发 refresh）

| 端点 | 文件 | 行号 | 错误消息 |
|------|------|------|---------|
| POST /auth/login | grape_auth.py | 410, 2035 | 账号或密码错误 |
| POST /auth/refresh | auth.py | 39, 43, 48, 54, 59, 60 | refresh_token 无效/过期/账号禁用 |
| POST /auth/exchange | grape_auth.py | 1927, 1932, 1937 | exchange_code 无效/账号禁用 |
| POST /admin/login | admin/routes.py | 87 | Invalid username or password |
| POST /admin/refresh | admin/routes.py | 111, 115 | admin refresh_token 无效 |
| POST /auth/associate | grape_auth.py | 2035 | 关联验证密码错误 |

#### 会话 401（受保护端点，应触发 refresh）

| 来源 | 文件 | 行号 | 错误消息 |
|------|------|------|---------|
| get_current_user | deps.py | 53, 59, 64, 70, 74, 80 | Not logged in / Session expired / Session revoked |
| get_current_user_or_api_key | deps.py | 147, 152, 167, 173 | 同上 + API Key 无效 |
| require_admin | deps.py | 213, 218, 225, 229 | Not logged in / Session expired / User not found / Session revoked |

#### reauth 401（OAuth token 失效，已正确处理）

| 来源 | 文件 | 行号 |
|------|------|------|
| with_oauth_token | oauth_token.py | 85, 101, 113 |
| repos 列表 | repos.py | 582 |
| issues_center | issues_center.py | 1371, 1450 |

### 7.3 问题清单

#### 问题 P1（已知）：api.js handleResp 对凭证 401 误触发 refresh

- **文件**：api.js:196-206
- **问题**：handleResp 收到 401 时，只要 retryFn 存在就尝试 refresh，不区分凭证 401 和会话 401。_post 总是传入 retryFn，所以 API.post('/auth/login') 返回 401 时也触发 refresh。
- **受影响端点**：POST /auth/login、POST /auth/exchange（通过 API.post 调用时）、POST /auth/associate
- **触发条件**：用户未登录（localStorage 无 token）+ 上述端点返回 401
- **影响**：后端返回的具体错误消息被丢弃，用户看到"会话过期，请重新登录"
- **对比**：admin.js:46 用 `path.indexOf('/login') < 0` 排除了登录端点，api.js 没有做同样的排除
- **修复**：在 R1 中已覆盖

#### 问题 P2：/auth/exchange 直接 fetch 绕过 handleResp，错误消息被丢弃

- **文件**：app.js:2557-2562
- **问题**：OAuth exchange 用直接 fetch 而非 API.post，`!r.ok` 时只抛 `'exchange failed'`，丢弃后端返回的具体错误（"exchange_code_invalid"、"账号不存在或已被禁用"）
- **触发条件**：OAuth 回调 exchange_code 过期或无效
- **影响**：用户只看到"OAuth login failed, please retry"，不知道具体原因
- **注意**：此调用绕过 handleResp 意味着不会误触发 refresh（这是对的），但错误处理太粗糙
- **修复方向**：解析 401 响应体的 detail.message 并展示

#### 问题 P3：events.js 8 处直接 fetch 绕过 handleResp，会话 401 静默失败

- **文件**：events.js
- **问题**：8 处直接 fetch 手动拼 Authorization header，完全绕过 handleResp。token 过期时不触发 refresh、不跳登录页、静默失败或显示"加载失败"

| 行号 | 端点 | 用途 | 401 处理方式 |
|------|------|------|-------------|
| 407 | /api/task-logs/{id}/info | prompt 详情 | `r.ok ? r.json() : null` → 静默失败 |
| 594 | /api/events/{seq}/knowledge/{scope} | 删除知识 | 401 响应体被当 JSON 解析 |
| 659 | /api/events/{seq}/chat | 删除对话 | 同上 |
| 733 | /api/events/chat/task/{id}/stream | SSE 流 | `!resp.ok` 显示"发送失败" |
| 784 | /api/events/chat/task/{id}/status | 任务状态 | 未检查 r.ok |
| 1249 | /api/task-logs/{id}/info | task log info | `r.ok ? r.json() : null` → 静默失败 |
| 1285 | /api/task-logs/{id}/stream | SSE 流（task log） | `!resp.ok` 抛 `HTTP {status}` → 连接失败 |
| 1426 | /api/runtime/tasks/{id}/status | 任务状态 | `r.ok ? r.json() : null` → 静默失败 |

- **触发条件**：用户长时间停留，JWT 过期后操作这些功能
- **影响**：用户看到"加载失败"而非"会话过期，请重新登录"
- **修复方向**：统一改用 API.get/API.post/API.del，或抽取公共 401 处理函数

#### 问题 P4：issues.js 附件上传直接 fetch 绕过 handleResp

- **文件**：issues.js:1165, 1204
- **问题**：上传附件用直接 fetch，token 过期时不触发 refresh，用户看到"上传失败"
- **不一致**：同文件 issues.js:1180 删除附件用 API.del() 走 handleResp，是对的。上传和删除用了不同的 API 调用方式
- **修复方向**：上传走 API.uploadFile()（api.js:413 的 401 处理是清 token 跳登录，可以接受）

#### 问题 P5：skills.js 下载直接 fetch 绕过 handleResp

- **文件**：skills.js:961
- **问题**：下载技能包用直接 fetch，401 时 `.catch(() => showToast('下载失败'))`，不触发 refresh
- **修复方向**：同 P3

#### 问题 P6：chat.js 任务状态查询直接 fetch 绕过 handleResp

- **文件**：chat.js:522
- **问题**：`fetch('/api/chat/task/{id}/status')` 未检查 r.ok，401 响应体被当 JSON 解析，`st.status` 为 undefined 走 else 分支静默忽略
- **影响**：用户看不到任何错误提示，任务状态查询静默失败
- **修复方向**：改用 API.get

#### 问题 P7：api.js handleResp 401 无 retryFn 分支——对凭证 401 清 token

- **文件**：api.js:209-220
- **问题**：无 retryFn 的 401 分支会 `localStorage.removeItem('token')`。如果用户在已登录状态下调用公开端点返回 401（比如已登录时重新登录），会清除现有 token
- **触发条件**：已登录 + 调用公开认证端点返回 401 + 无 retryFn
- **实际风险**：低——正常流程中 _get/_post/_put/_del 都传了 retryFn，此分支很少走到
- **修复方向**：同 R1，检查 localStorage 是否有 token 来区分

#### 问题 P8（非 bug，记录）：app.js /auth/logout 直接 fetch

- **文件**：app.js:2375
- **说明**：有意绕过 handleResp（注释说明），logout 失败静默处理 `.catch(() => {})`。设计合理——无论如何都清除 token 并跳登录页。**不是 bug。**

#### 问题 P9：doRefresh 跳转 /login 硬编码，丢失 /en 语言前缀

- **文件**：api.js:101, 255, 258
- **问题**：`location.href = '/login'` 和 `location.pathname !== '/login'` 硬编码了 `/login`，没有考虑英文环境的 `/en/login` 路径前缀
- **触发条件**：用户在英文环境（URL 为 /en/...）+ refresh_token 失效
- **影响**：
  1. `location.pathname !== '/login'` — 用户在 `/en/` 页面时条件为 true（不在 /login），会跳转
  2. `location.href = '/login'` — 跳到中文登录页，丢失英文环境。用户从英文界面突然跳到中文登录页
  3. `_redirectToGitcodeAuth` (api.js:255,258) 也有同样问题
- **对比**：admin.js 用 DOM 切换（loginView.classList），不涉及 URL 导航，没有此问题
- **修复方向**：跳转时保留当前语言前缀，如 `location.href = (I18N.getLang() === 'en' ? '/en' : '') + '/login'`

#### 问题 P10：handle401 无条件重试 POST 请求——非幂等重复提交风险

- **文件**：api.js:173-174, 327
- **问题**：handle401 refresh 成功后调用 `retryFn()` 重发原请求。`_post` 传入的 retryFn 是 `() => _post(path, body, timeout)`——会重发整个 POST 请求。但 POST 不是幂等的。
- **触发条件**：已登录 + JWT 过期 + 用户提交了一个 POST 请求（如创建 Issue）+ 后端实际已处理但响应前 JWT 认证失败
- **影响**：理论上存在重复提交风险——如果后端在 JWT 过期前已开始处理请求，retry 会再提交一次
- **实际风险**：低——JWT 过期是客户端时间判断，后端 verify_access_token 会检查 exp，如果请求到达后端时 JWT 已过期，后端返回 401 不会处理。但如果请求到达后端时 JWT 还有效（刚好在过期前几毫秒），后端处理完成后返回 200，但客户端因为某种延迟认为需要 refresh——这种场景下 handleResp 收到的是 200 不会走 401 分支，所以实际不会重试
- **结论**：理论风险，实际几乎不可能触发。记录但不需修复

#### 问题 P11：handleResp 200 + HTML 误判为 session_expired

- **文件**：api.js:236-240
- **问题**：当响应是 200 + text/html 时，代码注释说"可能是 token 过期后反代返回登录页 HTML"，直接抛 `session_expired`。但 200 + HTML 也可能是其他原因——比如 nginx 配置错误将 API 路由到了静态页面
- **触发条件**：nginx 配置变更/错误 + API 请求返回 200 + HTML
- **影响**：用户看到"会话过期"而非"服务异常"，误导排障方向
- **实际风险**：低——200 + HTML 在正常部署中不应出现
- **修复方向**：将提示改为更中性的"服务异常，请稍后重试"而非 session_expired，不假设是 session 问题

#### 问题 P12：ensureTokenValid 对已过期间 token 重试 doRefresh 2 次，每次 10 秒超时

- **文件**：api.js:139-150
- **问题**：token 剩余不足阈值时调 doRefresh，失败后 500ms 重试 1 次。doRefresh 有 10 秒超时（api.js:76）。如果 refresh_token 已失效（后端返回 401），doRefresh 会在 fetch 完成后立即失败（不需要等 10 秒）。但如果后端不可达（网络错误），每次 doRefresh 要等 10 秒超时，2 次共 ~20 秒
- **触发条件**：用户 JWT 即将过期 + 后端不可达（网络断开/服务器宕机）
- **影响**：用户操作会被阻塞约 20 秒（等待 2 次 refresh 超时），然后才返回原 token 走 401 兜底
- **对比**：app.js:2151-2152 在登录前清除了 token，所以登录流程不会走 ensureTokenValid。但已登录用户的其他 API 调用会走
- **修复方向**：对网络错误（TypeError）不重试，只对 HTTP 错误重试。或减少超时时间

#### 问题 P13：多 tab 并发 refresh — 第一个 tab refresh 成功后第二个 tab 的 _refreshPromise 已清空

- **文件**：api.js:74, 103-106
- **问题**：`_refreshPromise` 是模块级变量，不跨 tab 共享。两个 tab 同时 401 时：
  - Tab A: doRefresh() → 发起 fetch → _refreshPromise 设置 → 成功 → _refreshPromise 清空
  - Tab B: doRefresh() → 发现 _refreshPromise 已清空 → 发起第二个 fetch
  - 但 refresh_token 是 JWT，不是一次性的，两个 tab 用同一个 refresh_token 都能成功
- **结论**：refresh_token 是无状态 JWT，可重用，不会有竞态问题。多 tab 场景通过 storage 事件同步 token（api.js:54-67）。**不是 bug。**

#### 问题 P14：app.js 登录 catch 用字符串匹配判断错误类型——脆弱

- **文件**：app.js:2170-2177
- **问题**：用 `msg.includes('锁定')`、`msg.includes('验证码')`、`msg.includes('白名单')` 等字符串匹配来判断错误类型。这依赖后端消息文本，一旦后端修改消息文案（如 R2 优化提示词），前端匹配就会失效
- **触发条件**：R2 修改提示词后，如"连续登录失败 N 次"改为"出于安全保护"，`msg.includes('锁定')` 仍然能匹配（因为新文案中有"锁定"）。但如果后续进一步改文案去掉"锁定"二字，就会漏匹配
- **影响**：锁定/限流/白名单等特殊错误会走到 else 分支，显示为普通登录失败
- **修复方向**：用 error_code 判断而非字符串匹配（与 R4 的 error_code 方案统一）。R4 已覆盖此问题——前端用 error_code 映射 i18n，不再依赖字符串匹配

#### 问题 P15：admin.js 401 处理正确但 api.js 没有对齐

- **文件**：admin.js:46 vs api.js:196
- **问题**：admin.js 的 `api()` 函数用 `path.indexOf('/login') < 0` 排除了登录端点，不对登录 401 触发 refresh。api.js 没有做同样的排除
- **影响**：这是 P1 的另一个角度——admin 后台登录没有"会话过期"bug，但用户端登录有
- **修复方向**：R1 已覆盖。可以在 handleResp 中参考 admin.js 的做法，用 URL path 排除公开端点

#### 问题 P16：api.js upload XHR 401 跳过 refresh——与 fetch API 不一致

- **文件**：api.js:413-417
- **问题**：upload 用 XMLHttpRequest，401 时直接 `localStorage.removeItem('token') + showLogin()`，不尝试 refresh。而 fetch API（handleResp）会先尝试 refresh 再重试。
- **触发条件**：已登录 + token 过期 + 上传文件
- **影响**：用户上传文件时 token 过期，直接被踢到登录页，不尝试 refresh 自动续期。而其他 API 调用（GET/POST/PUT/DELETE）会先 refresh+重试。行为不一致。
- **修复方向**：upload 的 401 也应尝试 refresh 后重试上传。或至少与 handleResp 行为对齐。

#### 问题 P17：_redirectToGitcodeAuth 无去重——并发 reauth 401 多次跳转

- **文件**：api.js:248-258
- **问题**：`_redirectToGitcodeAuth` 没有防重入。如果多个 API 请求同时返回 `reauth_required`（如同时加载仓库列表和 Issue 列表），会触发多次 `_redirectToGitcodeAuth` → 多次 `API.get('/auth/login/gitcode')` → 多次 `location.href` 跳转。
- **触发条件**：多个 API 请求同时返回 OAuth token 过期
- **影响**：浏览器多次导航竞态，可能导致跳转到错误的页面
- **修复方向**：加去重标志 `_reauthRedirecting`，已在跳转中时直接 return

#### 问题 P18：200+HTML 抛 session_expired 但不清 token、不跳登录

- **文件**：api.js:238-240
- **问题**：200+HTML 分支抛 `session_expired` 错误，但不执行 `localStorage.removeItem('token')` 也不调 `showLogin()`。用户看到"会话过期"toast，但 token 仍在 localStorage 中，用户留在原页面继续操作。
- **触发条件**：nginx 配置错误或反代返回 HTML 登录页
- **影响**：用户看到"会话过期"但界面没变化，继续操作会持续失败
- **修复方向**：与 R8-1 统一——改为 server_error 中性提示。如果确实需要清 token，应该和 handle401 的逻辑一致。

#### 问题 P19：403 无全局统一处理——各页面自行判断，大部分不判断

- **文件**：api.js handleResp + 多个 pages/*.js
- **问题**：handleResp 没有 403 专用分支。403 走通用错误处理（api.js:224-231），创建 `new Error(detail.message)` + `e.status = 403`。各页面需要自己检查 `e.status === 403`：
  - app.js:1448 — dashboard 403 有处理（切换仓库）
  - events.js:35 — 事件导航 403 有处理（toast forbidden）
  - issues.js:1259 — 前端置灰按钮（注释说服务端 403 是最终权威）
  - 其余 ~15 个页面——**都不判断 403**，直接展示 `e.message` 或"加载失败"
- **触发条件**：用户无权限访问某资源（如非 admin 访问 admin 端点、无仓库权限访问仓库数据）
- **影响**：用户看到原始后端消息或"加载失败"，不知道是权限问题还是服务异常
- **修复方向**：在 handleResp 中增加 403 专用分支，统一返回友好提示。或在各页面 catch 中统一检查 e.status === 403。这是一个系统性问题，需要单独评估范围。

#### 问题 P20：knowledge.js 加载失败完全静默——无任何错误提示

- **文件**：knowledge.js:153, 172
- **问题**：knowledge 列表和统计加载失败时，只检查 AbortError，其他错误完全静默。用户看到空列表，无任何提示。
- **触发条件**：任何加载失败（401、403、500、网络错误）
- **影响**：用户以为没有知识库内容，实际是加载失败
- **修复方向**：增加错误提示

#### 问题 P21：pipeline.js 用 alert() 显示错误——UI 不一致

- **文件**：pipeline.js:375
- **问题**：用 `alert()` 而非 `app.showToast()` 显示错误。alert() 阻塞 UI，与全站 toast 风格不一致。
- **修复方向**：改为 showToast

#### 问题 P22：大量页面用字符串匹配判断错误类型——脆弱

- **文件**：
  - templates.js:837 — `errMsg.indexOf('权限') >= 0 || errMsg.indexOf('403') >= 0`
  - runtimes.js:1790 — `msg.indexOf('Unauthorized') >= 0 || msg.indexOf('APIGW.0301') >= 0`
  - runtimes.js:1988 — `msg.indexOf('98320005') >= 0 || msg.indexOf('already been used') >= 0`
  - feedback.js:296 — 硬编码中文 `'上传失败'`
- **问题**：与 P14 同类问题，但范围更广。用字符串匹配 e.message 判断错误类型，依赖后端消息文案和语言。R2 改提示词后部分匹配可能失效。
- **修复方向**：统一用 error_code / e.status 判断。R4 的 error_code 方案可逐步推广到这些页面。

#### 问题 P23：register.html 5 处 API.post 调用受 P1 影响 + 字符串匹配

- **文件**：register.html:329, 367, 390, 410, 436
- **问题**：register.html 引用全局 api.js 的 API.post，所有调用走 handleResp。其中：
  - **associateAccount()** (行436)：`API.post('/auth/login/{provider}/associate', ...)` — 后端返回 401 时（grape_auth.py:2035 "账号或密码错误"），会触发 refresh，与 P1 完全相同的 bug
  - **registerVerify()** (行390)：后端返回 400，不受 P1 影响
  - **doOAuthRegister()** (行410)：后端返回 400/409，不受 P1 影响
  - **send-code / resendCode** (行329, 367)：后端返回 400/429，不受 P1 影响
- **受影响端点**：POST /auth/login/{provider}/associate — 关联已有账号时密码错误
- **额外问题**：5 处 catch 全部用字符串匹配判断错误类型（`msg.indexOf('密码')`、`msg.indexOf('用户名')`、`msg.indexOf('验证码')` 等），与 P14/P22 同类问题
- **修复方向**：R1 覆盖 associate 的 401 问题。字符串匹配由 R4 的 error_code 方案覆盖。

#### 问题 P24：后端错误消息语言不一致——英文和中文混用

- **文件**：deps.py, auth.py, grape_auth.py, admin/routes.py
- **问题**：后端 401 错误消息中英文混用：
  - deps.py:53 — "Not logged in"（英文）
  - deps.py:59 — "Session expired"（英文）
  - deps.py:70 — "Session revoked"（英文）
  - deps.py:74 — "账号已被禁用"（中文）
  - auth.py:39 — "缺少 refresh_token"（中文）
  - auth.py:43 — "refresh_token 过期或无效"（中文）
  - grape_auth.py:410 — "账号或密码错误"（中文）
  - admin/routes.py:87 — "Invalid username or password"（英文）
- **触发条件**：用户在中文界面下，后端返回英文消息（如 "Session expired"、"Session revoked"）
- **影响**：中文界面中突然出现英文错误提示，用户体验不一致。特别是多 tab 场景——tab A 改密码后，tab B 收到 "Session revoked"（英文），用户在中文界面看到英文。
- **修复方向**：R4 的 error_code + 前端映射方案覆盖——前端用 error_code 映射 i18n key，不直接展示后端 message。但需要给 deps.py 的所有 401 也加上 error_code 字段。

#### 问题 P25：admin change-password 返回 401（旧密码错误）触发无意义 refresh

- **文件**：admin/routes.py:146 + admin.js:46
- **问题**：admin 的 /change-password 端点在旧密码错误时返回 401（error_code: OLD_PASSWORD_INCORRECT）。admin.js:46 的排除条件是 `path.indexOf('/login') < 0`——`/change-password` 不包含 `/login`，所以这个 401 会触发 refresh。
  1. admin.js 尝试 refresh
  2. refresh 成功（refresh_token 仍有效）
  3. 重试 /change-password（带着新 access token）
  4. 后端再次返回 401（旧密码还是错的）
  5. retry 的 fetch 直接抛出 Error
- **影响**：每次旧密码错误都会多一次 refresh + 重试，效率浪费。功能上最终能正确展示错误消息。
- **对比**：用户端 /auth/change-password 返回 400（不是 401），不受此问题影响。两个系统对相同语义的错误用了不同的 HTTP 状态码。
- **修复方向**：admin /change-password 的旧密码错误应返回 400 而非 401（与用户端对齐）。或 admin.js 的 refresh 排除条件增加 `/change-password`。

#### 问题 P26：token_version 变更后多 tab 体验——"Session revoked" 无友好提示

- **文件**：deps.py:70 + api.js:181
- **问题**：用户在 tab A 改密码/重置密码（token_version+1），tab B 的下一个 API 请求收到 401 "Session revoked"。handleResp 走 refresh，refresh 也失败（refresh_token 的 ver 也不匹配），最终抛出"会话过期，请重新登录"。
- **触发条件**：多 tab + 一个 tab 改密码
- **影响**：用户看到"会话过期"，不知道是因为在另一个 tab 改了密码。用户可能困惑——"我明明刚登录，为什么会话过期？"
- **修复方向**：R4 的 error_code 方案可覆盖——后端返回 error_code: SESSION_REVOKED，前端映射为"您的账号在其他设备上修改了密码，请重新登录"。

#### 问题 P27：handle401 refresh 成功后重发请求，但 retry 的 POST 不经过 _makeHeaders（token 可能过期）

- **文件**：api.js:173-174, 327
- **问题**：handle401 调用 `retryFn()` 即 `() => _post(path, body, timeout)`。_post 内部先调 `_makeHeaders()` 再 fetch。_makeHeaders 调 `ensureTokenValid()`——此时 localStorage 中的 token 是 doRefresh 刚写入的新 token，所以 ensureTokenValid 会返回新 token。**这个流程是正确的。**
- **结论**：不是 bug。记录确认 retry POST 会正确使用新 token。

#### 问题 P28：doRefresh 的 location.href='/login' 与 handle401 的 app.showLogin() 行为不一致

- **文件**：api.js:101 vs api.js:180
- **问题**：两个"跳登录"路径行为不同：
  - doRefresh catch（api.js:101）：`location.href = '/login'` — 浏览器导航，整页刷新，丢失 SPA 状态
  - handle401 catch（api.js:180）：`app.showLogin()` — DOM 切换，保留 SPA 状态
- **触发条件**：
  - ensureTokenValid 调 doRefresh 失败 → 走 api.js:101（location.href）
  - handle401 调 doRefresh 失败 → 走 api.js:180（showLogin）
- **影响**：同一用户在不同场景下 refresh 失败，体验不同。ensureTokenValid 路径会整页刷新（更慢），handle401 路径只切换 DOM（更快）。
- **修复方向**：统一为 app.showLogin()（SPA 切换），不用 location.href。或统一为 location.href（整页刷新）。考虑到 R7 的语言前缀问题，用 app.showLogin() 更简单。

#### 问题 P29：scene_sync.py sync_official_scenes 临时目录泄漏

- **文件**：src/storage/scene_sync.py:308, 316, 508
- **问题**：`sync_official_scenes` 函数中 `tmp_dir` 变量始终为 `None`——L308 初始化为 `None`，L316 下载成功后赋值给 `repo_root`（不是 `tmp_dir`），缺少 `tmp_dir = repo_root` 赋值。导致 L508 `finally: if tmp_dir: shutil.rmtree(tmp_dir, ignore_errors=True)` 的条件永远为 false，清理**永远不执行**。
- **对比**：skill_sync.py:238 正确执行了 `tmp_dir = repo_root`，L318-319 finally 块正常清理。
- **触发条件**：每次 scene_sync 独立运行（`_scene_sync_worker` 调用 `sync_official_scenes(repo_root=None)` 自行下载）时，下载的 ZIP 解压临时目录不会被清理。
- **影响**：每个同步周期泄漏一个 ZIP 解压临时目录（假设 50MB），日积月累占满磁盘。在容器环境中尤其严重（磁盘通常较小）。
- **场景用例**：
  ```
  服务正常运行，scene_sync_worker 每 30 分钟独立运行一次。
  每次 sync_official_scenes() 下载 ZIP → 解压到 /tmp/tmpXXXXXX → 遍历场景 → 函数返回。
  finally 块执行：if tmp_dir（None）→ 跳过清理。
  临时目录 /tmp/tmpXXXXXX 残留在磁盘上。
  30 分钟后下一个周期：又下载 → 又解压到新的 /tmp/tmpYYYYYY → 又不清理。
  每天泄漏 48 个临时目录 × 50MB = 2.4GB。
  服务器磁盘逐渐被占满 → scene_sync 下载失败 → 场景市场停止更新。
  ```
- **修复方向**：在 scene_sync.py L316 后加 `tmp_dir = repo_root`。R13 合并后 `sync_official_scenes` 的独立调用模式（`repo_root=None`）仍需正确清理。R13 设计 4.2 节的改动已包含 `tmp_dir = repo_root` 赋值逻辑，但需确认实施时覆盖此修复。
- **需求编号**：R13 覆盖（合并时同步修复）

#### 问题 P30：grape_auth.py associate 端点冷静期消息与登录端点不一致

- **文件**：src/api/routes/grape_auth.py:419 vs grape_auth.py:2044
- **问题**：同一个"账号处于注销冷静期"场景，两个端点返回的消息不一致：
  - L419（登录端点 `/auth/login`）：`"账号已注销，处于冷静期"`
  - L2044（关联端点 `/auth/login/{provider}/associate`）：`"账号已注销"`（不含"处于冷静期"）
- **触发条件**：用户通过 OAuth 关联已有 Grape 账号时，该 Grape 账号恰好处于注销冷静期（status==3）。
- **影响**：用户在关联流程中看到"账号已注销"，不知道是永久注销还是临时冷静期，信息不完整。
- **场景用例**：
  ```
  用户通过 GitCode OAuth 注册了新账号，然后尝试关联已有的 Grape 账号 yangaiwu。
  yangaiwu 账号此前申请了注销，正处于 7 天冷静期（status=3）。
  关联端点 /auth/login/gitcode/associate 返回 403: "账号已注销"。
  用户看到"账号已注销"——以为账号已被永久删除，无法恢复。
  实际上冷静期结束后账号可以恢复，但消息没有告知这一点。
  用户放弃关联，创建了重复账号。
  ```
- **修复方向**：R2 优化提示词时统一两处消息为 `"该账号正在注销中，暂时无法登录"`。
- **需求编号**：R2 覆盖

#### 问题 P31：pipeline.js 成功场景也使用 alert()，与全站 toast 风格不一致

- **文件**：src/api/static/js/pages/pipeline.js:371, 374
- **问题**：pipeline.js 的 SRE deploy 触发结果不仅错误用 alert()，成功场景也用 alert()：
  - L371: `alert(I18N.t('pipeline.sre_triggered', {seq: d.seq}))` — 成功触发也用 alert
  - L374: `alert(I18N.t('pipeline.sre_result', {result: JSON.stringify(d)}))` — 其他结果也用 alert
  - L378: `alert(I18N.t('pipeline.sre_trigger_failed', {msg: ...}))` — 失败用 alert（R9-4 已覆盖）
- **触发条件**：用户在 pipeline 页面触发 SRE 部署。
- **影响**：alert() 阻塞 UI，用户必须点击"确定"才能继续操作。与全站 toast 非阻塞风格不一致。成功场景本应用 toast 轻提示，不需要阻塞。
- **场景用例**：
  ```
  用户在 pipeline 页面点击"触发 SRE 部署"。
  confirm() 弹窗确认 → 用户点"确定"。
  API.post('/a2a', {...}) 返回 {seq: 101}。
  alert("SRE 部署已触发，事件 #101") → 用户被阻塞，必须点"确定"。
  用户点"确定"后才能继续操作 pipeline 页面。
  如果用户连续触发 3 次部署，需要点 3 次 confirm + 3 次 alert = 6 次点击。
  全站其他操作（如创建 Issue）都用 toast 非阻塞提示，只有 pipeline 用 alert。
  ```
- **修复方向**：R9-4 将 L371/L374/L378 的 alert() 全部改为 app.showToast()。成功场景 toast 3 秒自动消失，失败场景 toast 6 秒。
- **需求编号**：R9 覆盖（扩展 L375 → L371/L374/L378 全改）

#### 问题 P32：issues.js pre-upload 错误使用 alert()，与全站 toast 风格不一致

- **文件**：src/api/static/js/pages/issues.js:1213
- **问题**：issues.js 的 pre-upload（创建 Issue 时的附件上传）错误处理用 `alert('上传失败: ' + e.message)`，而非 showToast。同文件的 post-upload（L1165 附件上传到已有 Issue）用 innerHTML 显示错误，两处风格不一致。
- **触发条件**：用户在创建 Issue 页面上传附件时发生错误（如文件过大、网络断开、token 过期）。
- **影响**：alert() 阻塞 UI。用户在创建 Issue 的表单中被 alert 打断，点"确定"后表单内容仍在但上传状态丢失。
- **场景用例**：
  ```
  用户在创建 Issue 页面填写标题、描述，然后上传附件 screenshot.png。
  附件超过服务端限制（如 50MB）→ 后端返回 413。
  alert("上传失败: Payload Too Large") → 用户被阻塞。
  用户点"确定"——alert 关闭，但上传中... 的 loading div 仍在显示。
  用户困惑：是重试还是重新选择文件？
  对比：同文件 L1165 的已有 Issue 附件上传错误用 innerHTML 红色提示，非阻塞。
  ```
- **修复方向**：R6 改用 API.upload 时，catch 中用 showToast 或 innerHTML 显示错误，不用 alert。
- **需求编号**：R6 覆盖

#### 问题 P33：api.js 无 retryFn 的 401 分支对公开端点误清 token

- **文件**：src/api/static/js/api.js:209-220
- **问题**：无 retryFn 的 401 分支会 `localStorage.removeItem('token')` + `app.showLogin()`。如果已登录用户通过某种方式调用公开认证端点返回 401（如已登录时重新登录 / 已登录时调用 /auth/refresh），会清除现有的有效 token。
- **触发条件**：已登录用户（localStorage 有 token）+ 调用公开认证端点返回 401 + 无 retryFn（如直接 fetch 或未传 retryFn 的 API 调用）。
- **实际风险**：低——正常流程中 _get/_post/_put/_del 都传了 retryFn，此分支很少走到。但 register.html 的 associate 流程如果走 API.post（有 retryFn），则走的是 401+retryFn 分支（R1 已覆盖）。如果走直接 fetch（无 retryFn），则走此分支。
- **影响**：用户被踢出登录，丢失当前工作状态。
- **场景用例**：
  ```
  用户已登录，在设置页面查看账号绑定状态。
  前端代码中某处直接 fetch('/api/auth/refresh')（假设场景——检查 refresh 端点可用性）。
  refresh 端点返回 401（refresh_token cookie 不存在或已过期）。
  handleResp 无 retryFn 的 401 分支执行：
    localStorage.removeItem('token') → 用户的 access token 被清除
    localStorage.removeItem('user') → 用户信息被清除
    app.showLogin() → 跳转到登录页
  用户正在填写的表单数据丢失，被迫重新登录。
  ```
- **修复方向**：R1 的 isPublicAuthEndpoint 检查应同时覆盖此分支——对公开认证端点的 401，不清除 token、不跳登录，直接展示后端错误消息。
- **需求编号**：R1 覆盖（扩展 isPublicAuthEndpoint 检查到无 retryFn 分支）

### 7.4 问题优先级

| 问题 | 严重度 | 修复范围 | 需求编号 |
|------|--------|---------|---------|
| P1 | P0 — 用户可感知的误导 | api.js 1 处 | R1 |
| P2 | P1 — 错误消息丢失 | app.js 1 处 | R5 |
| P3 | P1 — 会话过期静默失败 | events.js 7 处 | R6 |
| P4 | P2 — 上传失败无 refresh | issues.js 3 处 | R6 |
| P5 | P2 — 下载失败无 refresh | skills.js 1 处 | R6 |
| P6 | P1 — 静默失败无提示 | chat.js 3 处 | R6 |
| P7 | P3 — 低概率清 token | api.js 1 处 | R1 覆盖 |
| P8 | 无 | 不需修复 | — |
| P9 | P1 — 英文环境跳中文登录页 | api.js 3 处 | 新增 R7 |
| P10 | P3 — 理论重复提交 | 不需修复 | — |
| P11 | P3 — 200+HTML 误判 | api.js 1 处 | 新增 R8 |
| P12 | P2 — 网络断开时阻塞 20 秒 | api.js 1 处 | 新增 R8 |
| P13 | 无 | 不需修复 | — |
| P14 | P2 — 字符串匹配脆弱 | app.js 1 处 | R4 覆盖 |
| P15 | P0 的另一个角度 | — | R1 覆盖 |
| P16 | P2 — upload 401 行为不一致 | api.js 1 处 | 新增 R9 |
| P17 | P2 — 并发跳转竞态 | api.js 1 处 | 新增 R9 |
| P18 | P3 — 200+HTML 不清 token | api.js 1 处 | R8 覆盖 |
| P19 | P2 — 403 无全局处理 | ~15 个页面 | 新增 R10（评估） |
| P20 | P2 — 静默失败 | knowledge.js 2 处 | 新增 R9 |
| P21 | P3 — UI 不一致 | pipeline.js 1 处 | R9 |
| P22 | P2 — 字符串匹配脆弱 | 4 个页面 | R4 推广 |
| P23 | P1 — register.html associate 401 误触发 refresh | register.html 1 处 | R1 覆盖 |
| P24 | P1 — 中英文消息混用 | 后端 8+ 处 | R4 覆盖（加 error_code） |
| P25 | P2 — admin change-password 401 无意义 refresh | admin routes + admin.js | 新增 R11 |
| P26 | P2 — 多 tab 改密码后提示不友好 | deps.py + api.js | R4 覆盖 |
| P27 | 无 — 确认正确 | 不需修复 | — |
| P28 | P2 — 跳登录方式不一致 | api.js 2 处 | 新增 R11 |
| P29 | P1 — 临时目录泄漏 | scene_sync.py 1 处 | R13 覆盖（已拆分到 feat/skill-scene-sync-merge） |
| P30 | P2 — 消息不一致 | grape_auth.py 1 处 | R2 覆盖 |
| P31 | P3 — 成功也用 alert | pipeline.js 2 处 | R9 覆盖 |
| P32 | P3 — pre-upload 用 alert | issues.js 1 处 | R6 覆盖 |
| P33 | P3 — 无 retryFn 401 误清 token | api.js 1 处 | R1 覆盖 |
| P34 | P3 — showLogin 双重调用 | api.js handle401 | R11 覆盖 |
| P35 | P1 — ?v= 未 bump 导致缓存 | index.html 8 处 | 全局 |
| P36 | P2 — app_cache_ver 未 bump | app.js 4 处 | 全局 |
| P37 | P2 — P33 Error 缺 error_code | api.js 1 处 | R1 覆盖 |
| P38 | P2 — upload retry err2 缺 error_code | api.js 1 处 | R9 覆盖 |
| P39 | P2 — upload err 缺 error_code | api.js 1 处 | R9 覆盖 |
| P40 | P1 — session 401 i18n key 缺失 | app.json 10 处 | R4 覆盖 |
| P41 | P1 — 锁定提示未区分邮箱配置 | login_throttle.py | R2 覆盖 |
| P42 | P2 — 跨 IP 累加导致误锁 | login_throttle.py | 待评估 |
| P43 | P2 — 锁定消息用固定时长非剩余时间 | login_throttle.py | R2 覆盖 |
| P44 | P2 — unlock/send-code 缺邮箱配置检查 | grape_auth.py | R2 覆盖 |

#### 问题 P34：handle401 catch 中 showLogin 双重调用

- **文件**：src/api/static/js/api.js:183-190（handle401）+ api.js:97-104（doRefresh catch）
- **问题**：R11-2 将 doRefresh catch 从 `location.href='/login'` 改为 `app.showLogin()`。但 handle401 catch 中也调用了 `localStorage.removeItem('token')` + `app.showLogin()`。当 refresh 失败时，doRefresh catch 先调 showLogin + throw e，然后 handle401 catch 收到 throw 再调一次 showLogin。showLogin 被调用两次。
- **触发条件**：已登录 + JWT 过期 + handle401 调 doRefresh 失败。
- **影响**：showLogin 是幂等的（DOM 切换两次效果相同），功能不受影响。但双重调用违反"单一职责"原则，且额外的 DOM 操作有性能开销。
- **场景用例**：
  ```
  用户已登录，JWT 过期后访问受保护端点。
  handle401 调 doRefresh() → refresh 失败（refresh_token 也过期）。
  doRefresh catch 执行：
    localStorage.removeItem('token') → 清除 token
    app.showLogin() → 第一次调用 showLogin，切换到登录视图
    throw e → 错误传播到 handle401 catch
  handle401 catch 执行：
    localStorage.removeItem('token') → token 已清空，重复清除（无副作用）
    app.showLogin() → 第二次调用 showLogin（冗余，DOM 已在登录视图）
    throw new Error('会话过期') → 错误传播到页面 catch
  页面 catch 显示 toast "会话过期"。
  用户看到：登录视图 + "会话过期" toast。功能正确但 showLogin 被调了两次。
  ```
- **修复方向**：从 handle401 catch 中移除 `localStorage.removeItem` + `app.showLogin()`——doRefresh catch 已经处理了这些。handle401 catch 只需 throw session_expired 错误。
- **需求编号**：R11 覆盖

#### 问题 P35：修改 JS 文件未 bump ?v= cache busting 版本号

- **文件**：src/api/static/index.html
- **问题**：修改了 api.js, app.js, events.js, chat.js, issues.js, skills.js, knowledge.js, pipeline.js 共 8 个 JS 文件，但 index.html 中的 `<script src="...?v=N">` 版本号未 bump。已登录用户的浏览器会加载旧的缓存 JS，看不到修复效果。
- **触发条件**：用户浏览器已缓存旧版本 JS 文件。
- **影响**：用户清除缓存或硬刷新前看不到任何修复——登录错误提示仍然是"会话过期"，upload 401 仍然直接跳登录。
- **场景用例**：
  ```
  用户小张上次访问 grape 是上周，浏览器缓存了 api.js?v=29。
  本次部署后 api.js 修改了 handleResp 401 分支。
  小张再次访问 grape，浏览器看到 <script src="api.js?v=29">。
  ?v=29 没变 → 浏览器使用缓存中的旧 api.js。
  小张输入错误密码 → 仍然看到"登录失败: 会话过期"（旧 bug 未修复）。
  小张联系运维反馈"修了还是不行" → 运维排查半天发现是缓存问题。
  ```
- **修复方向**：bump 所有修改过的 JS 文件的 ?v= 版本号。
- **需求编号**：全局（部署规范）

#### 问题 P36：app_cache_ver 未 bump

- **文件**：src/api/static/js/app.js（app_cache_ver 值 + 比较条件）
- **问题**：app.js 中 `app_cache_ver` 硬编码为 '43'，用于前端缓存版本检查。修改了前端 JS 后未 bump 这个版本号，已登录用户的 localStorage 不会被清除，可能残留旧版本的缓存数据。
- **触发条件**：已登录用户加载新版本 JS 后，app_cache_ver 仍为 '43'（匹配），不触发缓存清除。
- **影响**：旧版本的 localStorage 数据残留，可能与新代码不兼容。
- **场景用例**：
  ```
  用户小张已登录，localStorage 中 app_cache_ver='43'。
  部署新版本后，小张加载了新 app.js（因为 ?v= bump）。
  新 app.js 检查：localStorage.getItem('app_cache_ver') === '43' → 匹配 → 不清除缓存。
  小张的 localStorage 中残留旧版本的数据（如旧的 repo 缓存键名）。
  新代码期望新的缓存结构 → 读到旧结构 → 可能显示异常。
  ```
- **修复方向**：将 app_cache_ver 从 '43' bump 到 '45'，同时更新比较条件。
- **需求编号**：全局（部署规范）

#### 问题 P37：P33 无 retryFn 分支抛出的 Error 缺少 error_code 和 status

- **文件**：src/api/static/js/api.js:240-243（P33 fix 分支）
- **问题**：P33 修复中，无 retryFn 的 401 分支对公开认证端点抛出 `new Error(err?.detail?.message || ...)`，但没有设置 `e.error_code` 和 `e.status`。上层 catch 需要 `e.error_code` 来做 i18n 映射，缺失会导致 fallback 到原始 message 而非 i18n key。
- **触发条件**：已登录用户调用公开认证端点返回 401（无 retryFn），前端 catch 尝试用 error_code 做 i18n 映射。
- **影响**：error_code 为 undefined，`_loginErrMsg(e)` 走 fallback 到 `errMsg(e)` → `e.message`（后端原始消息），不经过 i18n 映射。英文环境下用户看到中文后端消息。
- **场景用例**：
  ```
  用户在英文环境（I18N.getLang() === 'en'）下已登录。
  前端代码调用 fetch('/api/auth/refresh')（无 retryFn）→ 返回 401。
  P33 分支抛出 Error('refresh_token 过期或无效')，但 e.error_code 未设置。
  上层 catch 调用 _loginErrMsg(e)：
    e.error_code = undefined → 不匹配任何 key → fallback 到 errMsg(e)
    errMsg(e): e.error_code 为空 → 不走 i18n 映射 → 返回 e.message
    结果：用户看到中文 "refresh_token 过期或无效"（应为英文 "Your session has expired."）
  ```
- **修复方向**：P33 分支抛出的 Error 对象设置 `e.status = 401` 和 `e.error_code = detail.error_code || ''`。
- **需求编号**：R1 覆盖

#### 问题 P38：R9-1 upload retry 成功后的错误对象缺少 error_code 和 status

- **文件**：src/api/static/js/api.js:483-489（xhr2.onload 非 200 分支）
- **问题**：R9-1 upload retry 创建的 xhr2 在非 200 响应时抛出 `err2`，但只设置了 `err2.errors`，没有设置 `err2.status` 和 `err2.error_code`。上层 catch 无法用 error_code 做 i18n 映射。
- **触发条件**：upload 401 → refresh 成功 → retry upload → retry 返回非 200（如 413 文件过大）。
- **影响**：retry 的错误缺少 error_code，i18n 映射失败，用户看到原始后端消息而非本地化提示。
- **场景用例**：
  ```
  用户上传 60MB 附件 → upload 401（JWT 过期）。
  R9-1: doRefresh 成功 → retry upload with new token。
  retry 返回 413（文件过大，detail: {"error_code": "FILE_TOO_LARGE", "message": "文件过大"}）。
  xhr2.onload 创建 err2：err2.message = "文件过大"，但 err2.error_code 和 err2.status 未设置。
  上层 catch 调用 errMsg(e)：e.error_code 为 undefined → fallback → 返回中文 "文件过大"。
  英文环境用户看到中文消息（应为 "File too large."）。
  ```
- **修复方向**：xhr2.onload 非 200 分支设置 `err2.status = xhr2.status` 和 `err2.error_code = json2?.detail?.error_code || ''`。
- **需求编号**：R9 覆盖

#### 问题 P39：原始 upload 非 401 错误缺少 error_code 和 status

- **文件**：src/api/static/js/api.js:506-508（xhr.onload 非 401 非 200 分支）
- **问题**：原始 upload 的非 401 错误只设置了 `err.errors`，没有设置 `err.status` 和 `err.error_code`。与 P38 同类问题。
- **触发条件**：upload 返回 400/403/413/500 等非 401 非 200 状态码。
- **影响**：上层 catch 无法用 error_code 做 i18n 映射。
- **场景用例**：
  ```
  用户上传附件 → 后端返回 403（权限不足，detail: {"error_code": "FORBIDDEN", "message": "无权限"}）。
  xhr.onload 创建 err：err.message = "无权限"，但 err.status 和 err.error_code 未设置。
  上层 catch 调用 errMsg(e)：e.error_code 为 undefined → fallback → 返回中文 "无权限"。
  英文环境用户看到中文消息。
  ```
- **修复方向**：设置 `err.status = xhr.status` 和 `err.error_code = json?.detail?.error_code || ''`。
- **需求编号**：R9 覆盖

#### 问题 P40：session 401 error_code 的 i18n key 未添加到 app.json

- **文件**：src/api/static/locales/zh/app.json + en/app.json（error section）
- **问题**：R2 为 deps.py 和 auth.py 的 401 响应添加了 error_code，但前端 app.json 的 error section 中没有添加对应的 i18n key。`errMsg(e)` 尝试 `I18N.t('app.error.' + code)` 时找不到 key，fallback 到后端原始 message（中英文混用）。
- **触发条件**：session 401 发生后（如 JWT 过期、多 tab 改密码），前端展示错误提示。
- **影响**：英文环境下用户看到中文后端消息（如 "会话已失效"、"账号已被禁用"），而非本地化提示。
- **场景用例**：
  ```
  用户在英文环境已登录。在另一个 tab 修改了密码。
  当前 tab 的下一个 API 请求收到 401（SESSION_REVOKED）。
  handle401 → doRefresh 失败 → throw Error(I18N.t('common.session_expired'))。
  common.session_expired = "会话过期，请重新登录"（中文）。
  英文环境用户看到中文 "会话过期，请重新登录"。
  设计文档期望：英文环境应显示 "Your password was changed on another device."
  ```
- **修复方向**：在 zh/en app.json 的 error section 中添加 10 个 session 401 error_code 的 i18n key。同时 `deps.py:require_admin` 的 4 个 401 响应（NOT_LOGGED_IN / SESSION_EXPIRED / USER_NOT_FOUND / SESSION_REVOKED）也需补加 error_code，与 `get_current_user` 和 `get_current_user_or_api_key` 保持一致。
- **需求编号**：R4 覆盖

#### 问题 P41：账号锁定提示未区分邮箱配置状态

- **文件**：src/auth/login_throttle.py:122-124（check_throttle 返回的锁定消息）
- **问题**：锁定消息硬编码"或通过邮箱验证码立即解锁"，但没有区分系统是否配置了邮箱服务。如果 `is_email_configured()` 返回 False（未配置 SMTP/Resend），用户看到提示后尝试用邮箱验证码解锁，但解锁端点 `/unlock/send-code` 依赖邮件服务发送验证码——没有邮箱配置就发不了验证码，用户陷入死胡同。
- **触发条件**：系统未配置邮箱服务 + 用户账号被锁定（5 次失败）。
- **影响**：用户看到"通过邮箱验证码立即解锁"提示，点击发送验证码 → 后端返回成功（防枚举，不暴露账号是否存在）→ 但实际没有发送邮件 → 用户永远收不到验证码 → 只能等 15 分钟自动解锁。用户体验极差且产生信任危机。
- **场景用例**：
  ```
  开发环境部署 grape，未配置 SMTP/Resend 邮箱服务。
  用户连续输错 5 次密码 → 账号被锁定 15 分钟。
  提示："出于安全保护，账号已临时锁定。请 15 分钟后重试，或通过邮箱验证码立即解锁。"
  用户看到"邮箱验证码解锁"选项 → 点击"发送验证码"。
  后端 /unlock/send-code：账号存在但无邮箱配置 → 返回 {"message": "验证码已发送"}（防枚举）。
  用户等 5 分钟没收到邮件 → 再试 → 还是没收到。
  用户困惑：系统说发了但没收到，是邮箱填错了还是系统有 bug？
  实际：系统根本没发邮件，只是防枚举返回了假成功。
  用户被迫等 15 分钟，对系统失去信任。
  ```
- **修复方向**：`check_throttle` 返回的锁定消息应根据 `is_email_configured()` 动态生成：
  - 邮箱已配置：`"出于安全保护，账号已临时锁定。请 {minutes} 分钟后重试，或通过邮箱验证码立即解锁。"`
  - 邮箱未配置：`"出于安全保护，账号已临时锁定。请 {minutes} 分钟后重试。"`
  - 同时在 throttle 返回 dict 中加 `"email_configured": True/False` 字段，前端据此决定是否显示"发送验证码"按钮。
- **需求编号**：R2 覆盖（提示词优化扩展）

#### 问题 P42：账号锁定跨 IP 累加——无辜用户被他人失败拖累

- **文件**：src/auth/login_throttle.py:106-108, 115-117（check_throttle 账号维度检查）
- **问题**：`get_failure_count(identifier)` 按 identifier 统计窗口内失败次数，不区分 IP。多个不同 IP 对同一个 identifier 的失败请求会累加。如果攻击者从 IP-A 用 `user_a` 尝试 4 次错误密码，真正用户从 IP-B 用 `user_a` 第一次输错密码就是第 5 次 → 触发锁定。用户甚至不知道之前有人试过他的账号。
- **触发条件**：攻击者知道目标用户名 + 从不同 IP 尝试 + 真正用户在 5 分钟窗口内输错一次密码。
- **影响**：无辜用户被锁定 15 分钟，且不知道原因。攻击者可以利用这个机制恶意锁定目标用户（已知用户名即可），造成拒绝服务。
- **场景用例**：
  ```
  攻击者知道用户 yangaiwu 的用户名。
  从 IP-A（VPN/代理）用 yangaiwu 尝试 4 次随机密码 → 全部失败 → 4 条失败记录。
  真正的 yangaiwu 用户从 IP-B（自己的电脑）登录。
  第一次输入密码时手误打错了 → 第 5 条失败记录 → 达到阈值 → 账号锁定。
  yangaiwu 困惑："我才输错一次，为什么就锁定了？"
  → 必须等 15 分钟或用邮箱验证码解锁。
  攻击者可以利用此机制反复锁定目标用户（每 5 分钟试 4 次，
  等用户下次输错就触发锁定），实现针对性拒绝服务。
  ```
- **修复方向**（需评估，不在本次范围）：
  - 方案 A：账号维度锁定也加 IP 限制——只有同一 IP 的失败次数达到阈值才锁定该 IP，不跨 IP 累加。
  - 方案 B：账号维度锁定阈值提高到 10 次（降低误锁概率），同时保留 IP 维度的 20 次阈值。
  - 方案 C：账号维度锁定后允许同 IP 的正确密码登录（只锁定错误密码尝试，不锁正确登录）。
  - 此问题是限流策略设计问题，涉及安全权衡，不在本次 R1-R11 范围内。记录为待评估项。

#### 问题 P43：账号锁定消息用固定时长而非剩余时间

- **文件**：src/auth/login_throttle.py:125（lock_minutes 计算）
- **问题**：锁定消息中 `lock_minutes = ACCOUNT_LOCK_DURATION // 60`（固定值 15），不是剩余时间。用户在锁定 10 分钟后尝试登录时，消息仍显示"请 15 分钟后重试"，但实际只需等 5 分钟。
- **对比**：IP 限流消息用 `remaining // 60 + 1`（动态剩余时间），账号锁定应该一致。
- **触发条件**：账号锁定后用户在锁定期间再次尝试登录。
- **影响**：用户看到的等待时间比实际需要的时间长，产生不必要的焦虑和等待。
- **场景用例**：
  ```
  用户 09:00 连续输错 5 次密码 → 账号锁定到 09:15。
  09:10 用户再次尝试登录 → 被拒绝。
  消息显示"出于安全保护，账号已临时锁定。请 15 分钟后重试。"
  用户以为要等到 09:25（当前 09:10 + 15 分钟）。
  实际只需等到 09:15（剩余 5 分钟）。
  用户多等了 10 分钟（或反复尝试发现 09:15 就能登录，对提示失去信任）。
  ```
- **修复方向**：`lock_minutes = remaining // 60 + 1`（与 IP 限流一致）。
- **需求编号**：R2 覆盖

#### 问题 P44：unlock/send-code 端点缺少邮箱配置检查

- **文件**：src/api/routes/grape_auth.py:907-925（unlock_send_code）
- **问题**：`unlock/send-code` 端点直接调 `auth_verification.send_code()`，没有先检查 `is_email_configured()`。对比 `forgot-password/send-code`（L458-460）做了检查并返回 503。在未配置邮箱服务的环境中，`send_code` 走 LOG_ONLY 模式返回成功但不发邮件，用户永远收不到验证码。
- **触发条件**：系统未配置邮箱服务 + 用户尝试调用解锁端点发送验证码。
- **影响**：用户收到"验证码已发送"但实际没发送，永远收不到验证码。虽然 P41 已在前端不显示解锁提示，但如果用户直接调用 API，仍会触发此问题。
- **场景用例**：
  ```
  开发环境未配置邮箱服务。用户账号被锁定。
  用户通过浏览器开发者工具或 curl 调用 POST /api/auth/unlock/send-code。
  后端：is_email_configured() 未检查 → send_code() 走 LOG_ONLY → 返回 True。
  响应：{"message": "验证码已发送"}。
  用户等待邮件 → 永远收不到 → 困惑。
  对比：POST /api/auth/forgot-password/send-code 返回 503 "Email service not configured"。
  两个端点行为不一致。
  ```
- **修复方向**：在 `unlock_send_code` 开头加 `is_email_configured()` 检查，未配置时返回 503（与 `forgot-password/send-code` 一致）。
- **需求编号**：R2 覆盖

### 7.5 新增需求项

#### R5：/auth/exchange 错误处理优化

**当前**（app.js:2557-2562）：
```js
if (!r.ok) throw new Error('exchange failed');
```

**要求**：解析 401 响应体的 detail.message，展示后端返回的具体错误（exchange_code_invalid / 账号不存在或已被禁用 / 会话已失效），而非统一的"OAuth login failed"。

#### R6：统一直接 fetch 调用的 401 处理

**范围**：events.js（8 处，含 2 处 SSE 流）、issues.js（3 处，含 2 处 FormData 上传 + 1 处 DELETE 删除附件）、chat.js（3 处，含 1 处 SSE 流 + 2 处任务状态查询）、skills.js（1 处 blob 下载），共 15 处直接 fetch。

**两种修复方案**：

**方案 A（推荐）**：改用 API.get/API.post/API.del，复用 handleResp 的 401 + refresh 逻辑。

**方案 B**：抽取公共的 `_fetchWithAuth(url, opts)` 函数，内部调用 handleResp 处理 401。适用于 SSE 流等不能直接用 API.get 的场景。

**注意**：events.js:733 的 SSE 流是 streaming 响应，不能直接改成 API.get（API.get 会 r.json() 解析），需要方案 B。

#### R7：跳转 /login 保留语言前缀

**当前**（api.js:101, 255, 258）：
```js
if (location.pathname !== '/login') location.href = '/login';
// 以及
location.href = '/login';  // _redirectToGitcodeAuth
```

**问题**：用户在英文环境（URL 为 /en/...）时，refresh 失效跳转会到 /login（中文登录页），丢失语言环境。

**要求**：跳转时保留当前语言前缀：
```js
var _langPrefix = (typeof I18N !== 'undefined' && I18N.getLang() === 'en') ? '/en' : '';
location.href = _langPrefix + '/login';
```

同时 `location.pathname !== '/login'` 的判断也要兼容 `/en/login`：
```js
if (location.pathname !== '/login' && location.pathname !== '/en/login') location.href = _langPrefix + '/login';
```

**改动点**：api.js 3 处（行 101, 255, 258）

#### R8：api.js 健壮性优化

**R8-1：200 + HTML 提示词修正**

**当前**（api.js:240）：
```js
throw new Error(I18N.t('common.session_expired') || '登录已过期，请重新登录');
```

**问题**：200 + HTML 不一定是 session 过期，可能是 nginx 配置错误。抛 session_expired 误导用户。

**要求**：改为中性提示：
```js
throw new Error(I18N.t('common.server_error') || '服务异常，请稍后重试');
```

**R8-2：ensureTokenValid 网络错误不重试**

**当前**（api.js:139-150）：doRefresh 失败后 500ms 重试 1 次，doRefresh 有 10 秒超时。网络断开时 2 次共 ~20 秒。

**要求**：对 TypeError（网络错误）不重试，只对 HTTP 错误（refresh_token 过期）重试：
```js
try {
    return await doRefresh();
} catch (e) {
    if (e instanceof TypeError) return token;  // 网络错误——不重试，返回原 token 交 401 兜底
    // HTTP 错误——重试 1 次
    await new Promise(r => setTimeout(r, 500));
    try {
        return await doRefresh();
    } catch (e2) {
        return token;
    }
}
```

#### R9：api.js 健壮性补充 + 页面级修复

**R9-1：upload 401 对齐 fetch API 行为**（P16）

**当前**（api.js:413-417）：upload XHR 401 直接清 token + showLogin，不尝试 refresh。

**要求**：upload 401 先尝试 refresh，refresh 成功后重试上传；refresh 失败再清 token + showLogin。与 handle401 行为对齐。

**R9-2：_redirectToGitcodeAuth 防重入**（P17）

**当前**（api.js:248-258）：无去重。

**要求**：加模块级标志 `_reauthRedirecting`：
```js
var _reauthRedirecting = false;
function _redirectToGitcodeAuth() {
    if (_reauthRedirecting) return;  // 已在跳转中
    if (location.pathname.includes('/login')) return;
    _reauthRedirecting = true;
    API.get('/auth/login/gitcode').then(d => {
        if (d.redirect_url) { location.href = d.redirect_url; }
        else { location.href = _langLoginUrl(); }
    }).catch(() => { location.href = _langLoginUrl(); });
}
```

**R9-3：knowledge.js 增加错误提示**（P20）

**当前**：knowledge.js:153, 172 加载失败完全静默。

**要求**：增加"加载失败"提示，与其他页面一致。

**R9-4：pipeline.js alert 改 showToast**（P21 + P31）

**当前**：pipeline.js:371/374/378 共 4 处 `alert()`（含成功场景）。

**要求**：全部改为 `app.showToast()`。成功场景 toast 3 秒自动消失，失败场景 toast 6 秒。

> **P31 扩展**：原 R9-4 只覆盖 L375（失败 alert）。深度审计发现 L371（成功触发）、L374（其他结果）也用 alert()。全站其他操作用 toast 非阻塞提示，只有 pipeline 用 alert 阻塞 UI。改为 showToast 后，连续触发 SRE 部署不再需要多次点击"确定"。
>
> **场景用例**：用户在 pipeline 页面点击"触发 SRE 部署"→ confirm 确认 → API.post 返回 {seq: 101} → alert("SRE 部署已触发，事件 #101") 阻塞 UI。用户必须点"确定"才能继续。如果连续触发 3 次，需要点 3 次 confirm + 3 次 alert = 6 次点击。改为 showToast 后，toast 3 秒自动消失，不阻塞操作。

#### R10：403 全局统一处理（评估项）

**问题**（P19）：handleResp 没有 403 专用分支，~15 个页面不判断 403。

**评估**：这是一个系统性问题，涉及面广。建议分两步：
1. 在 handleResp 中增加 403 分支，统一设置 `e.error_code = 'FORBIDDEN'`，前端可用 error_code 映射友好提示
2. 逐步在各页面推广 `e.status === 403` 或 `e.error_code === 'FORBIDDEN'` 判断

**不在本次需求范围**：R10 作为评估项记录，待本次 R1-R9 完成后单独评估是否需要实施。

#### R11：admin change-password 状态码对齐 + 跳登录方式统一

**R11-1：admin /change-password 旧密码错误返回 400 而非 401**（P25）

**当前**（admin/routes.py:146）：
```python
raise HTTPException(status_code=401, detail={"code": 401, "error_code": "OLD_PASSWORD_INCORRECT", "message": "Old password incorrect"})
```

**问题**：旧密码错误是凭证错误（用户输入问题），不是会话过期。返回 401 触发 admin.js 的 refresh 逻辑，导致每次旧密码错误都多一次 refresh + 重试。

**要求**：改为 400，与用户端 /auth/change-password（grape_auth.py:533 返回 400）对齐：
```python
raise HTTPException(status_code=400, detail={"code": 400, "error_code": "OLD_PASSWORD_INCORRECT", "message": "Old password incorrect"})
```

**R11-2：doRefresh 跳登录统一为 app.showLogin()**（P28）

**当前**：
- doRefresh catch（api.js:101）：`location.href = '/login'` — 整页刷新
- handle401 catch（api.js:180）：`app.showLogin()` — DOM 切换

**问题**：同一用户不同场景下 refresh 失败，跳登录行为不一致。且 doRefresh 的 location.href 有 R7 的语言前缀问题。

**要求**：doRefresh catch 改为 `app.showLogin()`，与 handle401 统一。去掉 `location.href = '/login'`，同时解决 R7 的语言前缀问题（showLogin 是 DOM 切换，不涉及 URL）。

**注意**：doRefresh 也在 ensureTokenValid 中被调用（api.js:141, 146）。ensureTokenValid 的 catch 不会直接调 showLogin——它返回原 token 交 401 兜底。所以只需改 doRefresh 自身的 catch。

---

## 八、验证清单

### 8.1 功能验证（R1-R3）

- [ ] 未登录 + 错误密码 → 展示"账号或密码不正确，请重新输入"，不触发 refresh
- [ ] 未登录 + 错误账号 → 同上
- [ ] 未登录 + 正确密码 → 正常登录
- [ ] 未登录 + 被禁用账号 + 正确密码 → 展示"该账号已被停用，请联系管理员"
- [ ] 未登录 + 注销冷静期账号 + 正确密码 → 展示"该账号正在注销中，暂时无法登录"
- [ ] 未登录 + 连续错误触发锁定 → 展示"出于安全保护，账号已临时锁定..."
- [ ] 未登录 + IP 限流 → 展示"操作过于频繁，请 N 分钟后再试"
- [ ] 已登录 + token 过期 → 触发 refresh（R1 不影响此场景）
- [ ] 已登录 + refresh 失败 → 展示"会话过期，请重新登录"
- [ ] 提示词不含"登录失败:"前缀

### 8.2 i18n 验证（R4）

- [ ] 中文环境 → 所有提示词为中文
- [ ] 英文环境 → 所有提示词为英文
- [ ] error_code 映射正确
- [ ] 兜底：未知 error_code 时显示后端原始 message

### 8.3 401 语义验证（R1, R5, R6）

- [ ] 未登录 + /auth/login 401 → 不触发 refresh，展示后端消息
- [ ] 未登录 + /auth/exchange 401 → 展示具体错误（非"exchange failed"）
- [ ] 未登录 + /auth/exchange 过期 code → 展示"exchange_code_invalid"对应文案
- [ ] 未登录 + /auth/exchange 账号禁用 → 展示"账号不存在或已被禁用"
- [ ] 已登录 + token 过期 + events.js 加载 prompt → 触发 refresh 或跳登录（非静默失败）
- [ ] 已登录 + token 过期 + events.js 删除知识 → 触发 refresh 或跳登录（非静默失败）
- [ ] 已登录 + token 过期 + events.js SSE 流 → 显示"会话过期"（非"发送失败"）
- [ ] 已登录 + token 过期 + issues.js 上传附件 → 触发 refresh 或跳登录（非"上传失败"）
- [ ] 已登录 + token 过期 + skills.js 下载 → 触发 refresh 或跳登录（非"下载失败"）
- [ ] 已登录 + token 过期 + chat.js 任务状态查询 → 触发 refresh 或跳登录（非静默失败）

### 8.4 语言前缀验证（R7）

- [ ] 英文环境 + refresh 失效 → 跳转到 /en/login（非 /login）
- [ ] 中文环境 + refresh 失效 → 跳转到 /login
- [ ] 英文环境 + OAuth reauth → 跳转到 /en/login（如 OAuth 授权页不可用）
- [ ] 英文环境 + /en/login 页面上 refresh 失效 → 不重复跳转

### 8.5 健壮性验证（R8）

- [ ] 200 + HTML 响应 → 展示"服务异常"（非"会话过期"）
- [ ] 网络断开 + JWT 即将过期 → 不重试 doRefresh，快速返回 401 兜底
- [ ] 网络断开 + API 调用 → ~10 秒超时（非 ~20 秒双次重试）

### 8.6 安全验证

- [ ] 账号不存在和密码错误返回相同的 error_code（CREDENTIAL_INCORRECT），不泄露账号是否存在
- [ ] 忘记密码端点不受影响（仍统一返回"验证码已发送"）
- [ ] 审计日志格式不变
- [ ] 限流策略不变

### 8.7 回归验证

- [ ] OAuth 登录流程不受影响
- [ ] 密码修改/重置流程不受影响
- [ ] admin 登录不受影响（admin 路由单独处理）
- [ ] 已登录用户的 API 请求 401 仍正常触发 refresh

### 8.8 新发现问题验证（P29-P33）

#### P29: scene_sync 临时目录泄漏

- [ ] `sync_official_scenes(repo_root=None)` 独立调用后，临时目录被清理（`ls /tmp/tmp*` 无残留）
- [ ] `sync_official_scenes(repo_root=path)` 复用模式调用后，调用方清理临时目录（scene_sync 自身不清理）
- [ ] 连续运行 3 个同步周期，`/tmp` 下无泄漏的临时目录

#### P30: associate 端点冷静期消息一致性

- [ ] 登录端点 `/auth/login` 对 status=3 账号返回"该账号正在注销中，暂时无法登录"
- [ ] 关联端点 `/auth/login/{provider}/associate` 对 status=3 账号返回相同消息
- [ ] 两端点的 error_code 均为 `ACCOUNT_DEACTIVATING`

#### P31: pipeline.js 全部 alert 改 showToast

- [ ] SRE 部署成功触发 → showToast（非 alert），3 秒自动消失，不阻塞 UI
- [ ] SRE 部署跳过 → showToast（非 alert）
- [ ] SRE 部署失败 → showToast（非 alert），6 秒消失
- [ ] 连续触发 3 次部署 → 无阻塞弹窗，toast 依次显示

#### P32: issues.js pre-upload alert 改 showToast

- [ ] 创建 Issue 时上传超大附件（>50MB）→ showToast 显示"上传失败"（非 alert）
- [ ] 创建 Issue 时上传附件网络断开 → showToast 显示错误（非 alert）
- [ ] loading div 在错误后正确移除

#### P33: 无 retryFn 401 分支 isPublicAuthEndpoint 检查

- [ ] 已登录用户访问公开认证端点返回 401 → 展示后端消息，不清除 token
- [ ] 已登录用户访问受保护端点返回 401（无 retryFn）→ 清除 token + 跳登录（现有行为不变）
- [ ] 已登录用户的 access token 在公开端点 401 后仍然有效

### 8.9 实施复查发现验证（P34-P40）

#### P34: handle401 catch showLogin 双重调用

- [ ] refresh 失败时 showLogin 只被调用一次（doRefresh catch 中）
- [ ] handle401 catch 不再包含 `localStorage.removeItem` 或 `app.showLogin()`
- [ ] refresh 失败后用户看到登录视图 + "会话过期" toast（功能不变）

#### P35: ?v= cache busting

- [ ] api.js ?v= 已 bump（v29→v30）
- [ ] app.js ?v= 已 bump（v180→v181）
- [ ] events.js ?v= 已 bump（v56→v57）
- [ ] chat.js ?v= 已 bump（v46→v47）
- [ ] issues.js ?v= 已 bump（v114→v115）
- [ ] skills.js ?v= 已 bump（v45→v46）
- [ ] knowledge.js ?v= 已 bump（v149→v150）
- [ ] pipeline.js ?v= 已 bump（v44→v45）

#### P36: app_cache_ver bump

- [ ] app_cache_ver 从 '43' 改为 '45'
- [ ] 比较条件 `!== '43'` 改为 `!== '45'`
- [ ] 已登录用户加载新 JS 后 localStorage 被清除（保留 token/user）

#### P37: P33 Error 缺 error_code

- [ ] P33 分支抛出的 Error 设置了 `e.status = 401`
- [ ] P33 分支抛出的 Error 设置了 `e.error_code = detail.error_code || ''`
- [ ] 英文环境下公开端点 401 显示英文 i18n 消息（非中文后端 message）

#### P38: upload retry err2 缺 error_code

- [ ] xhr2.onload 非 200 分支设置了 `err2.status = xhr2.status`
- [ ] xhr2.onload 非 200 分支设置了 `err2.error_code = json2?.detail?.error_code || ''`

#### P39: upload err 缺 error_code

- [ ] 原始 upload 非 401 错误设置了 `err.status = xhr.status`
- [ ] 原始 upload 非 401 错误设置了 `err.error_code = json?.detail?.error_code || ''`

#### P40: session 401 i18n key

- [ ] zh/app.json error section 包含 NOT_LOGGED_IN, SESSION_EXPIRED, SESSION_REVOKED, SESSION_INVALID, ACCOUNT_DISABLED, USER_NOT_FOUND, MISSING_REFRESH_TOKEN, REFRESH_TOKEN_INVALID, SESSION_TOKEN_INVALID, API_KEY_INVALID
- [ ] en/app.json error section 包含相同 10 个 key 的英文翻译
- [ ] 英文环境下 session 401 显示英文消息（如 "Your password was changed on another device."）
- [ ] SESSION_REVOKED 映射为"您的账号在其他设备上修改了密码"（非通用"会话过期"）

#### P41: 锁定提示未区分邮箱配置

- [ ] 邮箱已配置时锁定消息包含"或通过邮箱验证码立即解锁"
- [ ] 邮箱未配置时锁定消息只显示"请 N 分钟后重试"，不提邮箱验证码
- [ ] throttle 返回 dict 包含 `email_configured` 字段供前端判断
- [ ] 前端在邮箱未配置时不显示"发送验证码"按钮

#### P42: 跨 IP 累加导致误锁（待评估）

- [ ] 评估方案 A：账号维度锁定改为同 IP 累加（不跨 IP）
- [ ] 评估方案 B：提高账号维度锁定阈值
- [ ] 评估方案 C：锁定后允许同 IP 正确密码登录
- [ ] 此项为限流策略设计问题，不在本次 R1-R11 范围内

#### P43: 锁定消息用剩余时间而非固定时长

- [ ] 锁定消息中的分钟数 = `remaining // 60 + 1`（动态剩余时间）
- [ ] 用户在锁定 10 分钟后尝试登录 → 消息显示"请 5 分钟后重试"（非"请 15 分钟后重试"）
- [ ] 与 IP 限流消息的计算方式一致

#### P44: unlock/send-code 缺邮箱配置检查

- [ ] 未配置邮箱时 `POST /unlock/send-code` 返回 503
- [ ] 已配置邮箱时 `POST /unlock/send-code` 正常发送验证码
- [ ] 503 响应包含 `error_code: "EMAIL_NOT_CONFIGURED"`
- [ ] 行为与 `forgot-password/send-code` 一致

---

> **R12/R13/R14 已拆分到独立分支**（见 §1.2 Epic 视图）：
> - R12（对话全屏 z-index）→ `feat/fullscreen-zindex-fix`
> - R13（skill/scene 同步合并）→ `feat/skill-scene-sync-merge`
> - R14（一键部署脚本）→ `feat/one-click-deploy-script`
>
> 拆分理由：R12/R13/R14 与登录 401 链路零交集，分属不同域（events.js 交互缺陷 / 后端存储架构 / 部署工具新功能），合在一个 MR 会导致 diff 评审范围扩大、验证失焦、回滚连带。各独立分支有自己的需求文档和设计文档。
