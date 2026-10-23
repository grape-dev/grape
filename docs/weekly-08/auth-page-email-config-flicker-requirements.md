# 认证页面邮箱配置闪动修复

> **分支**: `feat/unbind-repo-state-consistency`（基于 `grape-dev/weekly-08` @ 31bbbaac8）
> **影响文件**: `src/api/main.py`、`src/api/static/register.html`、`src/api/static/forgot.html`、`src/api/static/index.html`、`src/api/routes/grape_auth.py`、`src/api/static/js/i18n.js`、`src/api/static/js/auth.js`、`src/api/static/locales/zh/app.json`、`src/api/static/locales/en/app.json`

---

## 一、问题陈述

### 1.1 闪动根因

**第一层（异步请求闪动）**：三个页面的邮箱配置状态判断都依赖**客户端异步请求** `GET /auth/providers`。页面 HTML 初始按"无邮箱"状态渲染（元素隐藏），异步请求返回后再用 JS 切换显示状态——这个从"隐藏→显示"的异步切换就是闪动来源。已通过服务端注入 `__EMAIL_CONFIGURED__` + 同步读取修复。

**第二层（FOUC 闪动）**：第一层修复后，`stepIndicator`/`emailField`/`forgotStep1` 的 HTML 初始状态改为可见（无 `display:none`），JS `applyEmailConfig()` 在 `</body>` 前同步切换为隐藏。但浏览器渲染管线在执行 `</body>` 前的 JS 前已完成首次绘制——无邮箱场景用户会短暂看到表单再切换为"未配置"提示。这是 FOUC（Flash of Unstyled Content），需通过 CSS class 驱动显隐修复。

**第三层（i18n 空白闪动）**：英文用户的 `data-i18n-pending="1"` 在 `<head>` 内联 script 中设置，CSS `[data-i18n-pending="1"] [data-i18n]{visibility:hidden}` 在首次绘制前生效——所有 `[data-i18n]` 元素 `visibility:hidden`。文字要显示必须等 `I18N.init()` → `applyI18nToDOM()` → 移除 pending，但 init 依赖 i18n.js 同步下载 + DOMContentLoaded 触发——远晚于首次绘制。用户从 register/forgot 切换到英文后返回登录页，或英文用户首次加载任何认证页面，都会看到"空白→英文"闪动。需通过服务端注入翻译数据 + 内联 early-translate script 修复。

**第四层（脚本阻塞抢跑首绘）**：第三层修复后 early-translate 在 `</body>` 前同步执行，但 index.html 行 311-340 有 30 个同步阻塞脚本（Vue、jQuery、echarts、api.js、22 个页面 JS、app.js）排在 early-translate 之前。浏览器解析完表单 DOM 后被这批脚本阻塞，阻塞期间抢跑首绘——此时文字被 i18n-loading 隐藏、输入框正常渲染 → "先框后字"。需将 30 个脚本 defer 化，消除阻塞窗口。register/forgot 不受影响因只有 2 个同步脚本，阻塞窗口极短。

### 1.2 全量问题清单

深度审计发现 25 个问题：

#### register.html（10 个）
| 编号 | 问题 | 严重度 |
|------|------|--------|
| P1 | 有邮箱场景邮箱字段/步骤指示器闪动 | P1 |
| P2 | 有邮箱场景验证码区域提前可见（双按钮 bug） | P1 |
| P3 | API 失败兜底不一致 | P2 |
| P4 | 无邮箱场景冗余操作 | P4 |
| P5 | emailConfigured 变量初始值为 true | P3 |
| P6 | goToStep2 错误处理用字符串匹配 | P2 |
| P7 | registerVerify 错误处理用字符串匹配 | P2 |
| P8 | regSubmitBtn 按钮 data-i18n 被 JS 动态修改 | P4 |
| P9 | 密码强度规则引用隐藏 email 字段 | P4 |
| P10 | api.js/app.css 版本号不一致 | P3 |

#### forgot.html（8 个）
| 编号 | 问题 | 严重度 |
|------|------|--------|
| P11 | 有邮箱场景整页空白闪动 | P1 |
| P12 | 步骤指示器与表单初始状态矛盾 | P2 |
| P13 | 无邮箱场景动态 createElement 提示 | P2 |
| P14 | 无邮箱场景步骤指示器残留 | P2 |
| P15 | API 失败兜底显示表单不安全 | P2 |
| P16 | forgotEmailDisplay 缺少 for/id | P3 |
| P17 | 无邮箱场景 backLinks 间距不合理 | P3 |
| P18 | step3 永远不在指示器中激活 | P4 |

#### index.html（2 个）
| 编号 | 问题 | 严重度 |
|------|------|--------|
| P19 | "忘记密码"链接闪动 | P3 |
| P20 | 用原生 fetch 而非 API.get | P4 |

#### 共性/后端（5 个）
| 编号 | 问题 | 严重度 |
|------|------|--------|
| P21 | 三页面各自独立请求 /auth/providers | P4 |
| P22 | register/forgot 错误处理均用字符串匹配 | P2 |
| P23 | 注册/忘记密码链路 HTTPException 缺少 error_code | P2 |
| P24 | 邮箱已注册防枚举策略不合理 | P2 |
| P25 | locale error_code 嵌套层级错误 | P1 |
| P26 | 无邮箱场景 register.html stepIndicator/emailField FOUC（HTML 初始可见，JS 底部才隐藏） | P1 |
| P27 | 无邮箱场景 forgot.html stepIndicator/forgotStep1 FOUC（同上） | P1 |
| P28 | 英文用户跨页面导航（register/forgot→login）后登录页 [data-i18n] 元素空白闪动（data-i18n-pending 在 head 设置 visibility:hidden，I18N.init 在 DOMContentLoaded 才移除） | P1 |
| P29 | 英文用户首次加载 register/forgot 页面时 [data-i18n] 元素空白闪动（同上根因，i18n.js 同步加载阻塞 body 解析） | P1 |
| P30 | index.html 登录页 30 个同步阻塞脚本排在 early-translate 之前，浏览器解析表单 DOM 后被脚本阻塞抢跑首绘 → 英文模式"先框后字"（输入框先出现，1.6-2.8 秒后文字才显示） | P1 |

---

## 二、注册逻辑全流程

### 2.1 有邮箱配置场景

| 步骤 | 前端 | 后端 | 说明 |
|------|------|------|------|
| 1. 页面加载 | 同步读取 `__EMAIL_CONFIGURED__=true`，显示邮箱字段+步骤指示器（2步），按钮="发送验证码"，验证码区域隐藏 | 服务端注入 `window.__EMAIL_CONFIGURED__=true` | 零闪动 |
| 2. 点"发送验证码" | goToStep2：校验用户名+邮箱+密码+确认密码 → POST /register/send-code | 用户名校验→用户名唯一→邮箱格式→注册策略→防枚举检查 | |
| 2a. 未注册邮箱 | 收到成功→隐藏第一步按钮→显示验证码区域→倒计时 | 发送验证码，返回"验证码已发送" | 正常流程 |
| 2b. 已注册邮箱 | 收到成功→同样进入验证码区域（不暴露存在性） | 不发验证码，发通知邮件，统一返回"验证码已发送" | 防枚举：用户不知道邮箱已注册 |
| 2c. send-code 成功提示 | "如果该邮箱可注册，验证码已发送，请查收邮件"（不拼接邮箱地址） | - | 模糊提示，不暴露存在性 |
| 2d. send-code 失败 | error_code 定位字段；找不到 error_code 显示"服务器错误" | 返回对应 error_code + message | i18n 本地化 |
| 3. 验证码页帮助入口 | "没收到验证码？如果你已注册，请返回登录或找回密码"——对所有用户一致显示 | - | 不暴露单个邮箱状态 |
| 4. 输入验证码点"注册" | registerVerify：校验验证码非空+6位 → POST /register/verify | 用户名+密码再次校验→**先校验验证码**→验证码通过后检查邮箱唯一性→创建账号 | 验证码是防枚举屏障 |
| 4a. 验证码错误 | showFieldError('regCode', i18n文案) | 返回 400 CODE_INVALID | 已注册邮箱没有验证码，永远返回 CODE_INVALID，不暴露存在性 |
| 4b. 注册成功 | 隐藏表单→显示成功面板→存 token→跳首页（直接登录） | 创建账号+个人组织+签发 JWT | 不跳登录页，直接进入应用 |
| 4c. 并发竞态 | showFieldError 定位字段 | create_grape_account 失败→检查 email/username 冲突→返回对应 error_code | 兜底 |

### 2.2 无邮箱配置场景

| 步骤 | 前端 | 后端 | 说明 |
|------|------|------|------|
| 1. 页面加载 | 同步读取 `__EMAIL_CONFIGURED__=false`，隐藏邮箱字段/步骤指示器/验证码区域，按钮="注册" | 服务端注入 `false` | 零闪动 |
| 2. 点"注册" | registerVerify：跳过验证码校验 → POST /register/verify | email_configured=false→跳过验证码校验→直接创建账号 | 无验证码流程 |
| 3. 注册成功 | 存 token→跳首页 | 创建账号（email_verified=false）+个人组织+签发 JWT | 直接登录 |

### 2.3 OAuth 关联模式

| 步骤 | 前端 | 后端 | 说明 |
|------|------|------|------|
| 1. 页面加载 | oauthMode=true→跳过 applyEmailConfig，显示 OAuth tabs | - | 不受邮箱配置影响 |
| 2. 点"注册" | doOAuthRegister → POST /login/{provider}/register | 校验 state→用户名→白名单→创建账号+绑定 | |

### 2.4 防枚举策略（方案 A：全程防枚举）

| 阶段 | 行为 | 返回 | 安全考量 |
|------|------|------|---------|
| send-code 未注册 | 发送验证码 | 统一成功 | 正常流程 |
| send-code 已注册 | 不发验证码，发通知邮件 | 统一成功 | 不暴露存在性 |
| send-code 限流 | 已注册邮箱也调 check_rate_limit | 429（与未注册不可区分） | 200 vs 429 不可区分 |
| verify 已注册邮箱 | 先校验验证码→验证码不匹配→CODE_INVALID | 400 CODE_INVALID | 已注册邮箱没有验证码，永远返回 CODE_INVALID，不暴露存在性 |
| verify 验证码通过后 | 检查邮箱唯一性（并发竞态兜底） | 409 EMAIL_ALREADY_REGISTERED | 仅在验证码通过后触发，枚举成本极高 |
| 前端帮助入口 | "没收到验证码？返回登录或找回密码" | 对所有用户一致显示 | 不针对单个邮箱暴露状态 |
| 通知邮件 | 发给原账号持有人 | "有人尝试使用您的邮箱注册，如非本人请忽略，如是本人请登录或找回密码" | 不含敏感信息 |

### 2.5 错误处理 fallback 链

```
Auth.t('app.error.'+code, null,
  Auth.t('common.server_error', null,
    err.message || Auth.t('app.login.toast_xxx')
  )
)
```

1. error_code 在 locale `error.*` 命中 → 本地化文案
2. error_code 未命中 → `common.server_error` "服务器错误，请稍后重试"
3. server_error 没加载 → `err.message`
4. `err.message` 为空 → "发送失败"等兜底

### 2.6 通知邮件

| 触发条件 | 收件人 | 主题 | 正文 |
|---------|--------|------|------|
| send-code 阶段邮箱已注册 | 原账号持有人邮箱 | "您的邮箱注册提醒" | "有人尝试使用您的邮箱在本系统注册。如非本人操作请忽略此邮件；如是您本人，请直接登录或找回密码。" |

不包含：用户名、昵称、注册时间、账号状态等敏感信息。

---

## 三、场景用例

### 3.1 注册页 — 有邮箱配置

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U1-1 | 配置 SMTP 后访问 /register | 首次渲染即显示邮箱字段+步骤指示器，验证码区域不显示，一个"发送验证码"按钮 | P1/P2 |
| U1-2 | 点"发送验证码" | 成功后显示验证码区域 | 无 |
| U1-3 | 输入验证码点"注册" | 校验→注册成功→跳首页（直接登录） | 无 |
| U1-4 | send-code 失败 | error_code 定位字段，i18n 本地化 | P6/P23 |
| U1-5 | 切换语言 | 文案正确切换 | P8 |

### 3.2 注册页 — 无邮箱配置

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U2-1 | 不配置 SMTP 访问 /register | HTML 解析阶段即隐藏邮箱字段/步骤指示器/验证码区域，首次绘制即为"无邮箱"状态，无 FOUC | P5/P26 |
| U2-2 | 点"注册" | 直接注册成功 | 无 |
| U2-3 | FOUC 验证：CSS class 驱动 | `<html>` 有 `email-not-configured` class，CSS `!important` 在 HTML 解析阶段隐藏元素，不依赖 JS 执行时机 | P26 |

### 3.3 注册页 — OAuth 关联模式

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U3-1 | OAuth 跳转 /register?oauth_associate=1 | OAuth tabs+用户名+密码 | 无 |
| U3-2 | 点"注册" | 关联成功→跳首页 | 无 |

### 3.4 忘记密码页 — 有邮箱配置

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U4-1 | 配置 SMTP 访问 /forgot | 首次渲染即显示表单+步骤指示器 | P11/P12 |
| U4-2 | 点"发送验证码" | step2 显示 | 无 |
| U4-3 | 点"重置密码" | 重置成功→跳首页 | 无 |

### 3.5 忘记密码页 — 无邮箱配置

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U5-1 | 不配置 SMTP 访问 /forgot | HTML 解析阶段即隐藏表单+步骤指示器，显示"未配置"提示，首次绘制即为正确状态，无 FOUC | P11/P13/P14/P27 |
| U5-2 | 切换语言 | 提示文案正确切换 | P13 |
| U5-3 | FOUC 验证：CSS class 驱动 | `<html>` 有 `email-not-configured` class，CSS `!important` 在 HTML 解析阶段确定显隐，不依赖 JS 执行时机 | P27 |

### 3.6 登录页

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U6-1 | 有邮箱时访问登录页 | "忘记密码"链接首次渲染即可见 | P19 |
| U6-2 | 无邮箱时访问登录页 | "忘记密码"链接不显示 | 无 |

### 3.7 错误处理 — error_code i18n

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U7-1 | 注册用户名被占用 | `Auth.t('app.error.USERNAME_TAKEN')` 本地化文案 | P6/P23 |
| U7-2 | 注册邮箱格式错误 | `Auth.t('app.error.EMAIL_INVALID')` | P6/P23 |
| U7-3 | 注册密码强度不足 | `Auth.t('app.error.PASSWORD_WEAK')` | P6/P23 |
| U7-4 | 注册验证码错误 | `Auth.t('app.error.CODE_INVALID')` | P7/P23 |
| U7-5 | 重置密码验证码错误 | `Auth.t('app.error.CODE_INVALID')` | P22/P23 |
| U7-6 | 重置密码账号不存在 | `Auth.t('app.error.ACCOUNT_NOT_FOUND')` | P22/P23 |
| U7-7 | 重置密码密码强度不足 | `Auth.t('app.error.PASSWORD_WEAK')` | P22/P23 |
| U7-8 | 英文环境注册失败 | 错误信息为英文 | P6/P23 |
| U7-9 | 未知 error_code | 显示"服务器错误，请稍后重试" | P24 |
| U7-10 | 已知 error_code 显示正确 | `Auth.t('app.error.USERNAME_TAKEN')` "用户名已被占用" | P25 |

### 3.8 防枚举验证

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U8-1 | 验证码页帮助入口 | "没收到验证码？返回登录或找回密码"——对所有用户一致显示 | P24 |
| U8-2 | send-code 成功文案 | "如果该邮箱可注册，验证码已发送"（不拼接邮箱地址） | P24 |
| U8-3 | verify 不先检查邮箱已注册 | 先校验验证码，验证码通过后才检查唯一性 | P24 |
| U8-4 | send-code 已注册统一成功 | 不暴露存在性 | P24 |
| U8-5 | 不含 regEmailRegistered | 已移除，不需要 | P24 |
| U8-6 | registerVerify catch 不特殊处理 | EMAIL_ALREADY_REGISTERED 走通用 fieldMap | P24 |

### 3.9 边界场景

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U9-1 | 直接打开 register.html | 兜底无邮箱模式 | 需新增兜底 |
| U9-2 | 英文路由 /en/register | 零闪动 | P1/P2 |
| U9-3 | 英文路由 /en/forgot | 零闪动 | P11/P12 |

### 3.10 i18n 零闪动验证

| 用例 | 操作 | 预期行为 | 问题 |
|------|------|---------|------|
| U12-1 | 英文用户首次加载 /register | HTML 解析到 `</body>` 时 early-translate 同步翻译所有 [data-i18n] + [data-i18n-placeholder] + 移除 i18n-loading class → 首次绘制即为英文，无空白闪动 | P28/P29 |
| U12-2 | 英文用户首次加载 /forgot | 同上 | P28/P29 |
| U12-3 | 英文用户从 register 切换语言后点"返回登录"到 / | early-translate 在 `</body>` 前用 boost 数据翻译登录页 [data-i18n] 元素 → 无空白闪动 | P28 |
| U12-4 | i18n.js 加载失败 | 5 秒超时兜底移除 i18n-loading class → 内容可见（中文默认文本）→ 后续 i18n.js 加载成功时翻译 | P28/P29 |
| U12-5 | boost 构建失败（locale JSON 读取异常） | `_get_i18n_boost()` 返回 `'{}'` → early-translate 检测 `__I18N_BOOST__` 为空直接 return → 降级为 defer i18n.js 翻译（有短暂闪动但不崩溃） | P28/P29 |
| U12-6 | admin.html 不受影响 | admin.html 走 FileResponse（不走 _serve_auth_page），不注入 boost/early-translate，i18n.js 同步加载 + `I18N.init('20261016-8')` 正常调用（自动 init 不触发因 `__I18N_CACHE_VER__` 未设置） | — |
| U12-7 | 英文用户冷加载登录页 / | HTML 连续解析（30 个脚本 defer 不阻塞）→ early-translate 在 `</body>` 前同步翻译 + 移除 i18n-loading → 首绘即为完整英文表单（文字与输入框同帧出现，无"先框后字"） | P30 |

---

## 四、需求

| 编号 | 需求 | 对应问题 | 类型 |
|------|------|---------|------|
| F1 | 注册页面零闪动 | P1/P5 | Bug 修复 |
| F2 | 验证码区域不提前显示 | P2 | Bug 修复 |
| F3 | 两条互斥注册链路 | P2/P5 | Bug 修复 |
| F4 | 忘记密码页零闪动 | P11/P12/P14 | Bug 修复 |
| F5 | 无邮箱提示预置+data-i18n | P13 | Bug 修复 |
| F6 | 无邮箱隐藏步骤指示器 | P14 | Bug 修复 |
| F7 | 登录页"忘记密码"链接零闪动 | P19 | Bug 修复 |
| F8 | forgotEmailDisplay for/id | P16 | 体验优化 |
| F9 | 服务端注入+同步读取 | P1/P11/P19/P21 | Bug 修复 |
| F10 | 后端 HTTPException 补全 error_code | P23 | Bug 修复 |
| F11 | 前端 error_code i18n，零字符串匹配 | P6/P7/P22 | Bug 修复 |
| F12 | locale `error` 顶层对象补全 16 个 error_code | P23/P25 | Bug 修复 |
| F13 | 版本号对齐 | P10 | 规范修复 |
| F14 | 有邮箱场景功能正常 | — | 兼容保障 |
| F15 | OAuth 模式不受影响 | — | 兼容保障 |
| F16 | 英文路由零闪动 | — | 兼容保障 |
| F17 | 兜底 false | — | 兜底安全 |
| F18 | `t(key, vars, fallback)` 第三参数 | P6/P7 | 基础设施 |
| F19 | 未知 error_code 显示"服务器错误" | P24 | Bug 修复 |
| F20 | 防枚举方案 A：send-code 统一成功+通知邮件；verify 先验码后查唯一性 | P24 | 安全修复 |
| F21 | 验证码页帮助入口（对所有用户一致显示） | P24 | 体验优化 |
| F22 | main.py `_inject_email_configured` 同时注入 `<html>` class（`email-configured`/`email-not-configured`） | P26/P27 | Bug 修复 |
| F23 | register.html/forgot.html CSS 规则根据 html class 驱动显隐，`!important` 优先于 inline style | P26/P27 | Bug 修复 |
| F24 | 兜底安全：注入失败时无 CSS class → 表单默认可见 → 后端返回错误（比"隐藏表单"更安全） | P26/P27 | 兜底安全 |
| F25 | main.py 服务端注入 `window.__I18N_BOOST__`（双语 ~5KB JSON，91 个 auth 页面 data-i18n key）+ `</body>` 前注入内联 early-translate script（零网络延迟翻译 DOM） | P28/P29 | Bug 修复 |
| F26 | i18n.js 用 `defer` 放 `<head>` + 自动 init（`_initialized` 防重复 + `__I18N_CACHE_VER__` 守卫，仅 defer 模式触发） | P28/P29 | Bug 修复 |
| F27 | `data-i18n-pending` attribute 改为 `html.i18n-loading` CSS class + 5 秒超时兜底 | P28/P29 | Bug 修复 |
| F28 | boost 构建失败时返回空 JSON `{}`，early-translate 检测空数据直接 return，降级为 defer 翻译（有短暂闪动但不崩溃） | P28/P29 | 兜底安全 |
| F29 | index.html body 中 30 个同步 `<script src>` 加 `defer` 属性，消除脚本阻塞导致的抢跑首绘（依赖链顺序不变，DOMContentLoaded 前执行完） | P30 | Bug 修复 |

---

## 五、不在范围

- 后端 `is_email_configured()` 逻辑（已正确实现）
- OAuth 关联注册模式（不受影响）
- P4/P8/P9/P17/P18/P20/P21：修复后自然消除

---

## 六、验收标准

| 编号 | 验收点 | 对应需求 |
|------|--------|---------|
| V1 | 有邮箱时注册页首次渲染即显示邮箱字段+步骤指示器，验证码区域不显示 | F1/F2/F3 |
| V2 | 无邮箱时注册页首次渲染即不显示邮箱字段/步骤指示器/验证码区域 | F1/F3 |
| V3 | 点"发送验证码"成功后验证码区域才显示 | F2/F3 |
| V4 | 无邮箱时点"注册"直接注册 | F3 |
| V5 | 有邮箱时忘记密码页首次渲染即显示表单+步骤指示器 | F4 |
| V6 | 无邮箱时忘记密码页首次渲染即显示提示 | F4/F5/F6 |
| V7 | 无邮箱提示有 data-i18n | F5 |
| V8 | 登录页"忘记密码"链接有邮箱时可见 | F7 |
| V9 | forgotEmailDisplay label 有 for | F8 |
| V10 | 服务端注入 `__EMAIL_CONFIGURED__` | F9 |
| V11 | 三页面不含异步请求 | F9 |
| V12 | 后端 HTTPException 含 error_code | F10 |
| V13 | 前端零 `msg.indexOf` | F11 |
| V14 | locale `error` 含 16 个 error_code（按 JSON 路径解析验证） | F12 |
| V15 | 版本号对齐 | F13 |
| V16 | OAuth 不受影响 | F15 |
| V17 | 英文路由零闪动 | F16 |
| V18 | 兜底 false | F17 |
| V19 | 注册全流程正常 | F14 |
| V20 | 忘记密码全流程正常 | F14 |
| V21 | `t(key, vars, fallback)` 第三参数 | F18 |
| V22 | 未知 error_code 显示"服务器错误" | F19 |
| V23 | send-code 已注册统一成功，不暴露存在性 | F20 |
| V24 | verify 先验码后查唯一性 | F20 |
| V25 | 验证码页有帮助入口 | F21 |
| V26 | send-code 成功文案不拼接邮箱地址 | F20 |
| V27 | main.py 注入 `email-configured` 或 `email-not-configured` html class | F22 |
| V28 | register.html CSS 含 `.email-not-configured #emailField`/`#stepIndicator` 隐藏规则 | F23 |
| V29 | forgot.html CSS 含 `.email-not-configured #forgotStep1` 隐藏 + `#forgotNotConfigured` 显示规则 | F23 |
| V30 | forgot.html CSS 含 `.email-configured #forgotNotConfigured` 隐藏规则 | F23 |
| V31 | 无邮箱场景 CSS class 在 HTML 解析阶段即确定显隐（不依赖 JS 执行时机） | F22/F23 |
| V32 | main.py 注入 `__I18N_BOOST__` + `</body>` 前注入 early-translate 内联 script | F25 |
| V33 | early-translate script 含 querySelectorAll('[data-i18n]') 翻译 + classList.remove('i18n-loading') | F25 |
| V34 | i18n.js 含 `_initialized` 防重复 + 自动 init（仅 `__I18N_CACHE_VER__` 存在时触发） | F26 |
| V35 | 三页面 CSS 含 `html.i18n-loading` 规则 + 5 秒超时兜底 | F27 |
| V36 | boost 构建失败时返回 `'{}'`（try-except 兜底） | F28 |
| V37 | index.html body 中所有 `<script src>` 含 defer 属性（无同步阻塞脚本） | F29 |
