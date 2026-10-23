# 认证页面邮箱配置闪动修复 — 设计文档

> **需求**: See `auth-page-email-config-flicker-requirements.md`.
> **影响文件**: `src/api/main.py`、`src/api/static/register.html`、`src/api/static/forgot.html`、`src/api/static/index.html`、`src/api/routes/grape_auth.py`、`src/api/static/js/i18n.js`、`src/api/static/js/auth.js`、`src/api/static/locales/zh/app.json`、`src/api/static/locales/en/app.json`

---

## 0. 重构铁律

1. 有邮箱配置场景行为等价
2. 无邮箱配置场景行为等价
3. OAuth 关联模式不受影响
4. 兜底安全：`window.__EMAIL_CONFIGURED__` 未定义时默认 `false`；CSS class 未注入时表单默认可见（后端返回错误，比"隐藏表单"更安全）
5. i18n 统一：error_code → `Auth.t('app.error.' + code, null, fallback)`
6. 防枚举方案 A：send-code 不暴露存在性；verify 先验码后查唯一性

---

## 1. 改动点总览

| 文件 | 改动 | 对应需求 |
|------|------|---------|
| `src/api/main.py` | `_inject_email_configured` + `_serve_auth_page`；`_spa_response`；4个路由；html class 注入；`__I18N_BOOST__` 注入 + early-translate script + try-except 兜底 | F9/F17/F22/F25/F28 |
| `src/api/static/register.html` | HTML初始状态；同步读取；error_code i18n；验证码页帮助入口；版本号对齐；CSS FOUC 规则；defer + `i18n-loading` class + 超时兜底 | F1-F3/F11/F13/F17/F19/F21/F23/F26/F27 |
| `src/api/static/forgot.html` | HTML初始状态；预置提示；同步读取；for/id；error_code i18n；版本号对齐；CSS FOUC 规则；defer + `i18n-loading` class + 超时兜底 | F4-F6/F8/F11/F13/F17/F19/F23/F26/F27 |
| `src/api/static/index.html` | 删除异步 fetch；同步读取；defer + `i18n-loading` class + 超时兜底 + `__I18N_CACHE_VER__`；body 30 个同步脚本 defer 化 | F7/F17/F26/F27/F29 |
| `src/api/routes/grape_auth.py` | HTTPException 补全 error_code；防枚举方案 A（send-code 统一成功+通知邮件；verify 先验码后查唯一性） | F10/F20 |
| `src/api/static/js/i18n.js` | `t(key, vars, fallback)`；inline pack 加 `common.server_error` + `toast_code_sent_hint` + `code_not_received_hint` + `or`；`_initialized` 防重复 + 自动 init + `__I18N_BOOST__` 读取 + `_deepMerge` + `classList` 替代 attribute | F18/F19/F21/F25/F26/F27 |
| `src/api/static/js/auth.js` | `t(key, vars, fallback)` | F18 |
| `src/api/static/locales/zh/app.json` | `error` 顶层对象 16 个 error_code + 新 i18n key | F12/F21 |
| `src/api/static/locales/en/app.json` | 同上 | F12/F21 |

**不改动**: `src/api/static/js/api.js`（R8 storage 事件监听除外，属仓库状态一致性修复）

---

## 2. 详细设计

### 2.1 main.py — 服务端注入
在 `</head>` 前注入 `<script>window.__EMAIL_CONFIGURED__=true/false;</script>` + `<html>` class + `window.__I18N_BOOST__`（双语翻译 JSON）。
在 `</body>` 前注入内联 early-translate script（零网络延迟，用 boost 数据翻译 DOM 并移除 `i18n-loading` class）。
CSS 在 HTML 解析阶段即确定元素显隐（F22/F23，修复 P26/P27 FOUC）。early-translate 在 HTML 解析到 `</body>` 时同步翻译所有 `[data-i18n]` 元素（F25，修复 P28/P29 i18n 闪动）。

### 2.2 register.html
**HTML**：`stepIndicator`/`emailField` 删除 `display:none`；`regCodeArea` 保持 `display:none`。验证码区域加帮助入口（"没收到验证码？返回登录或找回密码"）。

**JS**：同步读取；`applyEmailConfig` 分流；catch 用 `Auth.t('app.error.'+code, null, Auth.t('common.server_error', null, err.message || fallback))`；send-code 成功文案改为 `toast_code_sent_hint`（不拼接邮箱地址）。

**CSS**（F23）：`<style>` 块中加 FOUC 防护规则：
```css
.email-not-configured #stepIndicator,
.email-not-configured #emailField { display: none !important; }
```
无邮箱时 CSS `!important` 在 HTML 解析阶段即隐藏元素，不依赖 JS 执行时机。有邮箱时 html 有 `email-configured` class，CSS 规则不匹配，元素默认可见。

### 2.3 forgot.html
**HTML**：`forgotStep1` 删除 `display:none`；预置 `forgotNotConfigured` div（有 data-i18n）；`forgotEmailDisplay` label 补 `for`。

**JS**：同步读取；`applyEmailConfig` 分流；catch 用 error_code i18n。

**CSS**（F23）：`<style>` 块中加 FOUC 防护规则：
```css
.email-not-configured #stepIndicator,
.email-not-configured #forgotStep1 { display: none !important; }
.email-not-configured #forgotNotConfigured { display: block !important; }
.email-configured #forgotNotConfigured { display: none !important; }
```
无邮箱时隐藏表单+步骤指示器、显示"未配置"提示。有邮箱时隐藏"未配置"提示、表单默认可见。`forgotNotConfigured` 的 inline `style="display:none"` 被 CSS `!important` 覆盖。

### 2.4 index.html
删除异步 fetch，同步读取。

### 2.5 grape_auth.py — 防枚举方案 A

**send-code**：已注册邮箱统一返回"验证码已发送" + 发通知邮件 + 限流检查。不暴露存在性。

**register/verify**：先校验验证码 → 验证码通过后检查邮箱唯一性（并发竞态兜底）。已注册邮箱没有验证码，永远返回 `CODE_INVALID`，不暴露存在性。

### 2.6 i18n.js + auth.js — t() fallback
`t(key, vars, fallback)`：缺 key 返回 fallback。inline pack 加 `common.server_error` + `toast_code_sent_hint`。

### 2.7 locale — error 顶层对象
16 个 error_code 放在 `app.json` 文件根级 `error` 对象（i18n 查询路径 `app.error.CODE` = `app.json` 的 `error.CODE`）。

### 2.8 错误处理 fallback 链
```
Auth.t('app.error.'+code, null,
  Auth.t('common.server_error', null,
    err.message || Auth.t('app.login.toast_xxx')
  )
)
```

### 2.9 i18n 零闪动 — defer + 服务端注入 early-translate

**业界方案四（同步初始化 + 预加载资源）的完整实现**：

**2.9.1 服务端注入翻译数据**（`__I18N_BOOST__`）

main.py 启动时读取 `locales/en/app.json` + `locales/en/organizations.json` + `locales/en/common.json`，提取 auth 页面 91 个 `[data-i18n]` key 的翻译，缓存为双语 JSON（~5KB）。在 `</head>` 前注入 `window.__I18N_BOOST__={...}`。

提取逻辑覆盖三种 key 结构：
- 嵌套 key：`app.login.xxx` → `app_data['login']['xxx']`
- 顶层字符串 key：`app.back_home` → `app_data['back_home']`
- 跨文件 key：`organizations.xxx` → `organizations.json` 的 `xxx`

构建失败时 try-except 返回 `'{}'`——early-translate 检测空数据直接 return，降级为 defer 翻译。

**2.9.2 内联 early-translate script**

在 `</body>` 前注入内联 `<script>`（不是外部文件，零网络延迟）。HTML 解析到 `</body>` 时 body 中所有 `[data-i18n]` 元素已解析完，`querySelectorAll` 能找到全部元素。script 逻辑：
1. 读 `localStorage.grapeLang` 检测语言
2. 中文用户：直接移除 `i18n-loading` class（HTML 默认中文文本已正确）
3. 英文用户：用 `__I18N_BOOST__[lang]` 数据翻译 `[data-i18n]` + `[data-i18n-placeholder]` 元素，然后移除 `i18n-loading` class

**2.9.3 i18n.js defer + 自动 init**

三页面统一 `<script defer src="i18n.js">` 放 `<head>`。defer 并行下载不阻塞 HTML 解析，在 DOM 解析完成后、DOMContentLoaded 前执行。i18n.js 末尾自动 init（检查 `window.__I18N_CACHE_VER__` 是否设置——仅 defer 模式触发，admin.html 同步模式不受影响）。`_initialized` 标志防重复 init。

**2.9.4 CSS class 替代 attribute**

`data-i18n-pending="1"` → `html.i18n-loading` class。`applyI18nToDOM()` 移除 class。5 秒超时兜底在内联 `<script>` 中设置。

**2.9.5 执行时序**

```
1. HTML 解析 <head>：i18n-loading class 设置 + __I18N_BOOST__ 注入 + defer i18n.js 开始下载
2. HTML 解析 <body>：[data-i18n] 元素 visibility:hidden（i18n-loading class）
3. </body> 前内联 early-translate script 同步执行：翻译 + 移除 i18n-loading
4. 首次绘制：英文文字已就绪（零网络延迟，不依赖 i18n.js 下载）
5. defer i18n.js 执行：自动 init + _deepMerge boost + applyI18nToDOM（重复翻译但内容一致）
6. DOMContentLoaded：业务 JS 执行
7. 异步：全量 locale JSON 加载 → applyI18nToDOM 补全 boost 未覆盖的 key
```

间隙 = 步骤 2 到步骤 3 = HTML 解析时间（纯本地，零网络延迟）。

**2.9.6 index.html body 脚本 defer 化**（F29，修复 P30）

index.html 行 311-340 有 30 个同步 `<script src>`（Vue、jQuery、echarts、api.js、22 个页面 JS、app.js）。它们排在 early-translate script 之前，同步下载+执行阻塞 HTML 解析。浏览器解析完表单 DOM 后被阻塞，阻塞期间抢跑首绘——英文模式下输入框先出现，1.6-2.8 秒后文字才显示。

**改动**：30 个 `<script src>` 加 `defer` 属性。其余不动（i18n.js 已 defer、head 内联脚本、`</body>` 前 early-translate、CSS 规则、超时兜底）。

**修复后时序**：
```
1. HTML 连续解析（defer 不阻塞）→ 表单 DOM + 所有元素就绪
2. </body> 前 early-translate 同步执行：翻译 + 移除 i18n-loading
3. 首绘：完整英文表单（文字与输入框同帧出现）
4. defer 脚本按文档顺序执行（vue→jquery→api.js→页面JS→app.js）
5. DOMContentLoaded
```

**defer 化安全性**：
1. defer 保持文档顺序——依赖链 vue→jquery→api.js→页面JS→app.js 不变
2. 执行时机从"解析到行 311"变为"DOM 完整解析后"——页面 JS 访问的 DOM 只会更全
3. DOMContentLoaded 监听不受影响——defer 脚本保证在其前执行完
4. 30 个脚本已 grep 核验无 `document.write()`
5. 残余理论窗口：解析完成与 defer 执行间浏览器可能抢跑首绘——但此时文字已翻译、i18n-loading 已移除，抢跑绘制的是完整表单，无视觉差异

**职责声明**：零闪动的责任方是"服务端注入 + 内联同步翻译"，不是 defer。defer 化只是移除阻塞、为内联脚本扫清"首绘前执行"的路径。defer 的 i18n.js 只负责全量包加载与语言切换，不承担首屏可见性。

register/forgot 不需要此改动——只有 2 个同步脚本（api.js + auth.js），阻塞窗口极短。

---

## 3. 行为等价性验证表

| 场景 | 修改前 | 修改后 | 等价 |
|------|--------|--------|------|
| 有邮箱+注册页加载 | 异步→显示 | 同步→显示 | ✅ |
| 无邮箱+注册页加载 | 异步→隐藏 | 同步→隐藏 | ✅ |
| 无邮箱+注册页 FOUC | HTML 初始可见→JS 隐藏（闪动） | CSS class 在解析阶段隐藏（零闪动） | ✅ 修复 P26 |
| 有邮箱+忘记密码加载 | 异步→显示 | 同步→显示 | ✅ |
| 无邮箱+忘记密码加载 | 异步→createElement | 同步→预置提示 | ✅ |
| 无邮箱+忘记密码 FOUC | HTML 初始可见→JS 隐藏（闪动） | CSS class 在解析阶段隐藏（零闪动） | ✅ 修复 P27 |
| 注入失败兜底 | JS 默认 false → 隐藏 | 无 CSS class → 默认可见 → 后端返回错误 | ✅ 更安全（F24） |
| 英文首次加载 register | 空白（长）→ init → 英文 | early-translate 同步翻译 → 英文（零间隙） | ✅ 修复 P29 |
| 英文跨页面导航→login | 空白（长）→ init → 英文 | early-translate 同步翻译 → 英文（零间隙） | ✅ 修复 P28 |
| i18n.js 加载失败 | 永久隐藏 | 5 秒超时兜底移除 i18n-loading | ✅ 更安全（F27） |
| boost 构建失败 | N/A | 返回 '{}' → early-translate 跳过 → 降级 defer 翻译 | ✅ 不崩溃（F28） |
| admin.html | 同步 i18n.js + init | 不受影响（FileResponse，不注入 boost/early-translate） | ✅ 向前兼容 |
| 重复 init | 可能二次 init | _initialized 标志防重复 | ✅ |
| 英文冷加载登录页 | 30 个同步脚本阻塞→抢跑首绘→"先框后字" | defer 化→HTML 连续解析→early-translate 首绘前执行→同帧完整呈现 | ✅ 修复 P30 |
| OAuth 关联注册 | 跳过邮箱配置 | 同上 | ✅ |
| 错误处理 | msg.indexOf | Auth.t app.error | ✅ |
| 未知 error_code | 后端 message | "服务器错误" | ✅ 更安全 |
| 邮箱已注册 send-code | 伪装成功 | 统一成功+通知邮件 | ✅ 保持防枚举 |
| 邮箱已注册 verify | 先查邮箱暴露存在性 | 先验码后查唯一性 | ✅ 修复防枚举漏洞 |
| 注册成功 | 存 token+跳首页 | 同上 | ✅ 直接登录 |

---

## 4. i18n 架构

- **i18n.js**：`I18N.t(key, vars, fallback)` 查 locale pack，找不到返回 fallback
- **auth.js**：`Auth.t(key, vars, fallback)` 内部调 `I18N.t`
- **inline pack**：同步加载，覆盖 `common.*`(含 `server_error`) + `app.login.*`(含 `toast_code_sent_hint`) + `app.repo.*`
- **全量 locale JSON**：异步加载，覆盖 `error.*`（app.json 根级 `error` 对象）
- **catch 统一模式**：`Auth.t('app.error.'+code, null, Auth.t('common.server_error', null, err.message || fallback))`

---

## 5. 测试用例

### 5.1 静态断言测试（tests/js/test_auth_page_email_config.js，59 个）

T01-T04: main.py 服务端注入
T05-T12: register.html HTML + JS + error_code
T13-T22: forgot.html HTML + JS + error_code
T23-T24: index.html
T25-T28: grape_auth.py 后端 error_code
T29-T32: locale error.* 16 key（按 JSON 路径解析，防嵌套层级 bug 回归）
T33-T34: auth.js t() fallback + 无新增函数
T35: i18n.js inline pack 含 common.server_error
T36: register.html 验证码区域含帮助入口
T37-T38: register.html/forgot.html catch 含 common.server_error 兜底
T39: send-code 不返回 EMAIL_ALREADY_REGISTERED
T40: main.py 注入 email-configured/email-not-configured html class（F22）
T41: register.html CSS 含 .email-not-configured #emailField 隐藏规则（F23）
T42: register.html CSS 含 .email-not-configured #stepIndicator 隐藏规则（F23）
T43: forgot.html CSS 含 .email-not-configured #forgotStep1 隐藏规则（F23）
T44: forgot.html CSS 含 .email-not-configured #forgotNotConfigured 显示规则（F23）
T45: forgot.html CSS 含 .email-configured #forgotNotConfigured 隐藏规则（F23）
T46: main.py 注入 __I18N_BOOST__ 翻译数据（F25）
T47: i18n.js 含 _initialized 防重复标志（F26）
T48: i18n.js 含自动 init readyState 检查（F26）
T49: i18n.js 含 __I18N_BOOST__ 读取 + _deepMerge（F25）
T50: register/forgot/index i18n.js 标签含 defer 属性（F26）
T51: 三页面 CSS 含 html.i18n-loading 规则（F27）
T52: 三页面含 5 秒超时兜底（F28）
T53: 三页面含 window.__I18N_CACHE_VER__ 设置（F26）
T54: i18n.js applyI18nToDOM 移除 i18n-loading class（F27）
T55: main.py 含 _EARLY_TRANSLATE_SCRIPT + querySelectorAll + classList.remove（F25）
T56: main.py boost 构建含 try-except 兜底返回 '{}'（F28）
T57: main.py boost 提取覆盖嵌套 key（app.login.xxx）+ 顶层字符串 key（app.back_home）+ 跨文件 key（organizations.xxx）
T58: early-translate script 处理 data-i18n-placeholder（F25）
T59: index.html body 中所有 `<script src>` 含 defer（无同步阻塞脚本）（F29）

### 5.2 场景用例测试（tests/js/test_auth_page_scenarios.js，48 个）

U1-1~U1-3: 有邮箱注册页
U2-1~U2-3: 无邮箱注册页
U3-1: OAuth 模式
U4-1~U4-2: 有邮箱忘记密码页
U5-1~U5-3: 无邮箱忘记密码页
U6-1~U6-2: 登录页
U7-1~U7-7: error_code i18n + t() fallback
U8-1~U8-6: 防枚举验证（帮助入口+文案+verify 顺序+统一成功+无 regEmailRegistered+不特殊处理）
U9-1~U9-3: 边界场景
U10-1~U10-4: FOUC 验证（CSS class 驱动显隐，HTML 解析阶段确定状态）
U11-1~U11-7: i18n 零闪动验证（defer + boost + 自动 init + 超时兜底 + 防重复）
U12-1~U12-7: early-translate 验证（内联 script 翻译 + boost 覆盖全 key + try-except 兜底 + admin 不受影响 + 登录页 defer 化）

### 5.3 E2E 验证（Playwright DOM 检查，已验证通过）

**无邮箱场景**：
- /register: emailField 不可见、stepIndicator 不可见、regCodeArea 不可见、按钮="注册"、auth-btn 数量=1
- /forgot: forgotStep1 不可见、stepIndicator 不可见、forgotNotConfigured 可见且文案正确
- /: forgotLink 不可见

**有邮箱场景**：
- /register: emailField 可见、stepIndicator 可见、regCodeArea 不可见、按钮="发送验证码"、auth-btn 数量=1
- /forgot: forgotStep1 可见、stepIndicator 可见、forgotNotConfigured 不可见、按钮="发送验证码"
- /: forgotLink 可见
- /en/register: emailField 可见、regCodeArea 不可见

### 5.4 API 验证（已验证通过）

- 无邮箱 send-code: 返回 `EMAIL_NOT_CONFIGURED`
- 无邮箱 forgot/send-code: 返回 503 `EMAIL_NOT_CONFIGURED`
- 有邮箱 send-code 坏输入: 返回 400 `USERNAME_INVALID`
- 有邮箱 forgot/send-code: 返回 200 "验证码已发送"
- 有邮箱 forgot/reset 坏验证码: 返回 400 `CODE_INVALID`
- OAuth register 坏 state: 返回 400 `OAUTH_STATE_EXPIRED`
