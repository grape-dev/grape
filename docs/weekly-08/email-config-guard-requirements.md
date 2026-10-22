# 账号安全流程"邮箱验证码可用性"判断修复

> **分支**：`feat/fullscreen-zindex-fix`（R12/R13 同分支扩展）
> **基线**：`grape-dev/weekly-08` (06ad2e106)
> **状态**：待实施
> **类型**：Bug 修复 + 安全加固

## 一、总览

### 1.1 背景

Grape 账号安全流程（修改密码、设置密码、重置密码、修改邮箱、删除账号）中有多个功能依赖邮箱验证码。系统定义了两层可用性判断规则：第一层是服务器是否配置了邮件服务，第二层是用户是否绑定了邮箱。但部分流程入口只检查了第二层（用户邮箱），跳过了第一层（系统配置），导致未配置邮件服务的实例上仍显示邮箱验证码入口，用户被引导走完一段注定失败的流程。

### 1.2 需求范围

| 编号 | 需求 | 类型 |
|------|------|------|
| R14-1 | 前端补齐第一层判断——account.js 5 处流程入口加 email_configured 检查 | Bug 修复 |
| R14-2 | 后端补齐 3 个路由的第一层守卫——对齐 R7 既有模式 | 安全加固 |

### 1.3 不在范围

- window._emailConfigured 获取逻辑（L108-120）及 fail-open 默认值——展示类判断允许保守显示，维持现状
- OAuth 登录按钮不过滤 configured（app.js R7.5 设计决策）
- verification.py 内部逻辑
- 不加新接口、不动 DB、不动接口签名
- dev 环境 888888 固定验证码——守卫只在无邮件配置时拦截，verify 侧不动

---

## 二、问题分析

### 2.1 业务规则（大前提）

邮箱相关功能的可用性判断分两层，顺序固定，缺一不可：

**第一层（系统配置）**：服务器是否配置了邮件服务（configs/email.json 的 SMTP/Resend）。未配置 → 一切邮箱相关功能入口不显示，与用户数据无关。判断依据只能来自服务端信号（/auth/providers 的 email_configured），禁止前端从用户数据推断。

**第二层（用户数据）**：系统已配置邮件服务时，才看用户账号是否绑定邮箱。有邮箱 → 提供邮箱验证流程；无邮箱 → 引导绑定（而非隐藏能力）。

公式：流程入口显示 = 系统配置了邮件服务 AND（视流程而定）用户已绑定邮箱。

后端同责：所有依赖邮件服务的路由必须自带 is_email_configured 守卫（无配置 → 503 EMAIL_NOT_CONFIGURED），不依赖前端把门。

### 2.2 正面样板（已正确实现）

**邮箱管理卡片**（account.js L143-154）：已正确按两层规则隐藏。当 window._emailConfigured === false 时，整个邮箱管理卡片不渲染。这是正确实现，本次不改。

**/auth/providers 接口**（grape_auth.py L2265-2267）：已返回 email_configured 字段。前端已拉取并缓存到 window._emailConfigured（account.js L108-110）。

**register_send_code**（grape_auth.py L268）：已有 is_email_configured 守卫，R7 降级模式。

**forgot_password_send_code**（grape_auth.py L458）：已有 is_email_configured 守卫，503 EMAIL_NOT_CONFIGURED。

### 2.3 问题清单

#### 前端（account.js）：信号已存在但采纳不一致

| # | 位置 | 流程 | 现状判断 | 问题 |
|---|------|------|----------|------|
| 1 | L316, L328 | 修改密码弹层"忘记密码"链接 | `hasEmail` | 缺第一层：系统未配置时仍显示链接 |
| 2 | L335 | _accountShowResetPassword 守卫 | `if (!hasEmail)` | 缺第一层：系统未配置时仍进入流程 |
| 3 | L404 | _accountShowSetPassword 守卫 | `if (!_accountProfile.email)` | 缺第一层：系统未配置时仍进入流程 |
| 4 | L1275, L1281 | 删除账号"验证码验证"选项 | `if (hasEmail)` | 缺第一层：系统未配置时仍显示选项 |
| 5 | L450 | _accountShowChangeEmail 入口 | 无守卫 | 正常入口已被卡片守卫保护，但 L406 有跳转路径且无防御 |

#### 后端（grape_auth.py）：3 个发码路由缺第一层守卫

| 路由 | 行号 | 守卫 | 无配置时行为 |
|------|------|------|-------------|
| set_password_send_code | L565 | 无 | 生产靠 RuntimeError 兜底；非生产 LOG_ONLY 模式下验证码只写日志、接口返回"验证码已发送"假成功 |
| change_email_send_code | L640 | 无 | 同上 |
| delete_account_send_code | L716 | 无 | 同上 |

同一族功能一半有 R7 降级一半没有，属当年 R7 改漏。

### 2.4 实测复现

本机服务未配置邮件服务，测试库用户有邮箱。用户打开"修改密码"弹层：

1. 弹层底部显示"忘记当前密码？"链接——违反第一层
2. 点击进入重置流程，显示"发送验证码"界面
3. 点击"发送验证码"，前端乐观切到"已发送"步骤，后端才返回 503 EMAIL_NOT_CONFIGURED——用户被引导走完一段注定失败的流程

---

## 三、修复方案

### 3.1 R14-1：前端补齐第一层判断（account.js，5 处）

判断统一改为 `hasEmail && (window._emailConfigured !== false)`（信号已缓存，无新增请求）。

#### 改动 1：L316 / L328 — 修改密码弹层"忘记密码"链接

**当前**：
```js
(hasEmail ? '<p ...><a ... onclick="_accountShowResetPassword()">' + I18N.t('app.account.forgot_password_link') + '</a></p>' : '')
```

**改为**：
```js
(hasEmail && window._emailConfigured !== false ? '<p ...><a ... onclick="_accountShowResetPassword()">' + I18N.t('app.account.forgot_password_link') + '</a></p>' : '')
```

**场景用例**：
```
系统未配置邮件服务，用户已绑定邮箱。
用户打开"修改密码"弹层。
→ 当前：弹层底部显示"忘记当前密码？"链接
→ 用户点击链接 → 进入重置流程 → 点"发送验证码" → 前端乐观切到"已发送" → 后端返回 503 → 用户经历了一段注定失败的流程
修复后：window._emailConfigured === false → 链接不渲染 → 用户只看到密码修改表单，不会被误导
```

```
系统已配置邮件服务，用户已绑定邮箱。
用户打开"修改密码"弹层。
→ window._emailConfigured === true → 链接正常显示 → 流程可走通
```

#### 改动 2：L335 — _accountShowResetPassword 守卫

**当前**：
```js
function _accountShowResetPassword() {
  var hasEmail = _accountProfile && _accountProfile.email;
  if (!hasEmail) {
    app.showToast(I18N.t('app.account.set_password_needs_email'));
    return;
  }
```

**改为**：
```js
function _accountShowResetPassword() {
  var hasEmail = _accountProfile && _accountProfile.email;
  if (!hasEmail || window._emailConfigured === false) {
    if (window._emailConfigured === false) {
      app.showToast(I18N.t('app.account.email_not_configured') || 'Email service not configured, please contact administrator');
    } else {
      app.showToast(I18N.t('app.account.set_password_needs_email'));
    }
    return;
  }
```

**场景用例**：
```
系统未配置邮件服务，用户已绑定邮箱。
用户在浏览器控制台直接执行 _accountShowResetPassword()。
→ 当前：hasEmail 为 true → 通过守卫 → 渲染重置流程
修复后：window._emailConfigured === false → toast 提示"邮件服务未配置，请联系管理员" → 不渲染流程
```

#### 改动 3：L404 — _accountShowSetPassword 守卫

**当前**：
```js
function _accountShowSetPassword() {
  if (!_accountProfile || !_accountProfile.email) {
    app.showToast(I18N.t('app.account.set_password_needs_email'));
    _accountShowChangeEmail();
    return;
  }
```

**改为**：
```js
function _accountShowSetPassword() {
  if (window._emailConfigured === false) {
    app.showToast(I18N.t('app.account.email_not_configured') || 'Email service not configured, please contact administrator');
    return;
  }
  if (!_accountProfile || !_accountProfile.email) {
    app.showToast(I18N.t('app.account.set_password_needs_email'));
    _accountShowChangeEmail();
    return;
  }
```

**场景用例**：
```
系统未配置邮件服务，用户无密码但已绑定邮箱。
用户点击"设置密码"。
→ 当前：hasEmail 为 true → 通过守卫 → 渲染设置密码流程（含验证码步骤）
修复后：window._emailConfigured === false → toast 提示"邮件服务未配置" → 不渲染流程
```

```
系统已配置邮件服务，用户无密码且无邮箱。
用户点击"设置密码"。
→ window._emailConfigured !== false → 通过第一层 → !_accountProfile.email → toast 提示先绑定邮箱 → 跳转修改邮箱
→ 第二层"引导绑定而非隐藏"行为正确
```

#### 改动 4：L1275 / L1281 — 删除账号"验证码验证"选项

**当前**：
```js
var hasEmail = _accountProfile ? !!_accountProfile.email : false;
...
if (hasEmail) methods.push({id: 'code', label: I18N.t('app.account.verify_with_code')});
```

**改为**：
```js
var hasEmail = _accountProfile ? !!_accountProfile.email : false;
var canVerifyByEmail = hasEmail && window._emailConfigured !== false;
...
if (canVerifyByEmail) methods.push({id: 'code', label: I18N.t('app.account.verify_with_code')});
```

**场景用例**：
```
系统未配置邮件服务，用户已绑定邮箱且有密码。
用户打开"删除账号"弹层。
→ 当前：验证方式列表包含"验证码验证"选项
→ 用户选验证码 → 点发送验证码 → 后端 503 或假成功
修复后：canVerifyByEmail = false → 验证方式列表只有"密码验证" → 用户仍可删除账号，能力域完整
```

```
系统未配置邮件服务，用户已绑定邮箱，无密码，无 OAuth 绑定。
用户打开"删除账号"弹层。
→ canVerifyByEmail = false, hasPwd = false, bindings = [] → methods 为空
→ toast 提示"至少保留一种登录方式" → 用户无法删除账号
→ 这是正确的：无邮件服务时验证码不可用，用户没有其他验证方式，不能删除账号
→ 修复方式：联系管理员配置邮件服务或先绑定 OAuth
```

#### 改动 5：L450 — _accountShowChangeEmail 防御性守卫

**当前**：无守卫。

**改为**：在函数开头加防御性守卫：
```js
function _accountShowChangeEmail() {
  if (window._emailConfigured === false) {
    app.showToast(I18N.t('app.account.email_not_configured') || 'Email service not configured, please contact administrator');
    return;
  }
  var isZh = _accountIsZh();
  _accountShowModal(...)
```

**场景用例**：
```
系统未配置邮件服务。
用户在浏览器控制台直接执行 _accountShowChangeEmail()。
→ 当前：无守卫 → 渲染修改邮箱弹层（含验证码步骤）
→ 正常入口已被邮箱卡片第一层守卫保护（L143-154），此行只防未来新增调用点漏判
修复后：window._emailConfigured === false → toast 提示 → 不渲染弹层
```

### 3.2 R14-2：后端补齐 3 个路由的第一层守卫

在 set_password_send_code（L565）、change_email_send_code（L640）、delete_account_send_code（L716）入口处，仿照 forgot_password_send_code（L458）：

```python
from src.auth.email_service import is_email_configured
if not is_email_configured():
    raise HTTPException(status_code=503, detail={
        "code": 503,
        "error_code": "EMAIL_NOT_CONFIGURED",
        "message": "Email service not configured, please contact administrator"
    })
```

守卫位置放在用户/邮箱个体检查之前（全局能力先于个体数据，与 forgot_password 顺序一致）。

**场景用例**：
```
系统未配置邮件服务，用户已绑定邮箱。
攻击者绕过前端，直接 POST /api/auth/set-password/send-code。
→ 当前：无守卫 → 进入用户检查 → 调 auth_verification.send_code
→ 生产环境：RuntimeError 兜底（500 Internal Server Error）
→ 非生产 LOG_ONLY 模式：验证码只写日志，接口返回"验证码已发送"假成功
修复后：is_email_configured() === False → 503 EMAIL_NOT_CONFIGURED → 清晰错误，不进入用户检查
```

### 3.3 不改的（避免为改而改）

- window._emailConfigured 获取逻辑（L108-120）及 fail-open 默认值——展示类判断允许保守显示，维持现状
- OAuth 登录按钮不过滤 configured（app.js R7.5 注释明确的设计决策）
- verification.py 内部逻辑
- 不加新接口、不动 DB、不动接口签名——全部复用既有信号与既有降级模式

---

## 四、改动汇总

| # | 改动点 | 文件 | 行号 | 类型 | 改动内容 |
|---|--------|------|------|------|---------|
| 1 | 修改密码"忘记密码"链接 | account.js | L316, L328 | R14-1 | hasEmail → hasEmail && _emailConfigured !== false |
| 2 | 重置密码守卫 | account.js | L335 | R14-1 | 加 _emailConfigured 检查 |
| 3 | 设置密码守卫 | account.js | L404 | R14-1 | 加 _emailConfigured 检查（在邮箱检查之前） |
| 4 | 删除账号验证码选项 | account.js | L1275, L1281 | R14-1 | hasEmail → canVerifyByEmail = hasEmail && _emailConfigured !== false |
| 5 | 修改邮箱防御性守卫 | account.js | L450 | R14-1 | 函数开头加 _emailConfigured 检查 |
| 6 | set_password_send_code 守卫 | grape_auth.py | L565 | R14-2 | 加 is_email_configured 503 守卫 |
| 7 | change_email_send_code 守卫 | grape_auth.py | L640 | R14-2 | 加 is_email_configured 503 守卫 |
| 8 | delete_account_send_code 守卫 | grape_auth.py | L716 | R14-2 | 加 is_email_configured 503 守卫 |
| 9 | cache busting | index.html | L327 | 顺手 | account.js v=41 → v=42 |

改动总量：前端约 10 行 + 1 个缓存号；后端 3 段相同守卫约 12 行。

---

## 五、验证场景

### 5.1 无邮件服务实例（本机 124.70.82.58）+ 有邮箱账号

| # | 场景 | 期望行为 | 验证方法 |
|---|------|----------|----------|
| 1 | 修改密码弹层 | 不出现"忘记当前密码？"链接 | 打开弹层，检查底部无链接 |
| 2 | 直接调 _accountShowResetPassword() | toast 提示邮件服务未配置，不渲染流程 | 控制台执行函数 |
| 3 | 直接调 _accountShowSetPassword() | toast 提示邮件服务未配置，不渲染流程 | 控制台执行函数 |
| 4 | 直接调 _accountShowChangeEmail() | toast 提示邮件服务未配置，不渲染弹层 | 控制台执行函数 |
| 5 | 删除账号弹层 | 验证方式无验证码选项，只有密码/OAuth | 打开弹层，检查验证方式列表 |
| 6 | 直接 POST /api/auth/set-password/send-code | 503 EMAIL_NOT_CONFIGURED | curl 测试 |
| 7 | 直接 POST /api/auth/change-email/send-code | 503 EMAIL_NOT_CONFIGURED | curl 测试 |
| 8 | 直接 POST /api/auth/delete-account/send-code | 503 EMAIL_NOT_CONFIGURED | curl 测试 |

### 5.2 有邮件服务实例（配置 email.json 后）— 回归确认不误伤

| # | 场景 | 期望行为 | 验证方法 |
|---|------|----------|----------|
| 9 | 修改密码弹层 | "忘记密码"链接正常显示 | 打开弹层 |
| 10 | 重置密码流程 | 正常走通 | 点击链接→发送验证码→输入验证码→重置 |
| 11 | 设置密码流程 | 正常走通 | 点击设置密码→发送验证码→输入→设置 |
| 12 | 修改邮箱流程 | 正常走通 | 点击修改邮箱→输入新邮箱→发送验证码→确认 |
| 13 | 删除账号验证码选项 | 正常显示 | 打开删除账号弹层 |

### 5.3 第二层引导行为

| # | 场景 | 期望行为 | 验证方法 |
|---|------|----------|----------|
| 14 | 有邮件服务 + 用户无邮箱 + 设置密码 | toast 提示先绑定邮箱并跳转修改邮箱 | 点击设置密码 |
| 15 | 有邮件服务 + 用户无邮箱 + 修改密码弹层 | 不显示"忘记密码"链接（hasEmail 为 false） | 打开弹层 |

### 5.4 边界场景

| # | 场景 | 期望行为 | 验证方法 |
|---|------|----------|----------|
| 16 | 无邮件服务 + 无邮箱账号 | 与现状一致（本就不显示） | 打开各弹层 |
| 17 | /auth/providers 请求失败 | 展示卡片按现状 fail-open（_emailConfigured=true），流程入口放行后由后端 503 兜底 | 模拟请求失败 |
| 18 | dev 环境 888888 固定验证码 | 不受影响（守卫只在无邮件配置时拦截，verify 侧不动） | 配置邮件服务后测试 |
| 19 | 无邮件服务 + 无密码 + 无 OAuth 绑定 + 删除账号 | methods 为空 → toast"至少保留一种登录方式" → 无法删除 | 打开删除账号弹层 |

---

## 六、改动文件

| 文件 | 改动内容 |
|------|---------|
| src/api/static/js/pages/account.js | 5 处流程入口加 email_configured 第一层检查 |
| src/api/routes/grape_auth.py | 3 个发码路由加 is_email_configured 503 守卫 |
| src/api/static/index.html | account.js cache busting v=41 → v=42 |

## 七、设计文档

详细设计见同目录下 `r14-email-config-guard-design.md`。

## 八、测试用例代码

### 8.1 Playwright 前端测试（test_r14_email_config_guard.py）

```python
"""
R14: 账号安全流程邮箱验证码可用性判断修复
用法: python3.9 tests/playwright/test_r14_email_config_guard.py
前置: 服务运行在 localhost:3000，未配置邮件服务，已登录（admin/admin）
"""
from playwright.sync_api import sync_playwright
import sys

BASE = 'http://localhost:3000'
results = []

def test(name, condition, detail=''):
    status = '✓' if condition else '✗'
    results.append((name, condition, detail))
    print(f'  {status} {name}' + (f' — {detail}' if detail else ''))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 800})

    # 登录
    page.goto(BASE + '/')
    page.wait_for_timeout(3000)
    try:
        page.fill('#loginUser', 'admin')
        page.fill('#loginPassword', 'admin')
        page.click('form#pwdLogin button')
        page.wait_for_timeout(3000)
    except:
        pass
    logged_in = page.evaluate("() => document.documentElement.getAttribute('data-logged-in')")
    if logged_in != '1':
        print('Login failed — aborting')
        sys.exit(1)

    # 确认邮件服务未配置
    email_cfg = page.evaluate("() => window._emailConfigured")
    print(f'\nemail_configured: {email_cfg}')

    # 导航到账号页
    page.evaluate("window.location.hash = '#account'")
    page.wait_for_timeout(2000)
    try:
        page.click('.stabs a:nth-child(3)')
        page.wait_for_timeout(1000)
    except:
        pass

    # === 测试 1: 修改密码弹层不显示"忘记密码"链接 ===
    print('\n=== 测试 1: 修改密码弹层不显示"忘记密码"链接 ===')
    try:
        btns = page.query_selector_all('button')
        for b in btns:
            txt = b.text_content() or ''
            if 'password' in txt.lower() or '密码' in txt:
                b.click()
                break
        page.wait_for_timeout(1000)
    except:
        pass
    has_forgot_link = page.evaluate("""() => {
        const modal = document.getElementById('accountModal');
        if (!modal) return false;
        const links = modal.querySelectorAll('a');
        for (const a of links) {
            if (a.textContent.includes('忘记') || a.getAttribute('onclick')?.includes('ResetPassword')) return true;
        }
        return false;
    }""")
    test('修改密码弹层无"忘记密码"链接', not has_forgot_link, f'link found: {has_forgot_link}')
    page.keyboard.press('Escape')
    page.wait_for_timeout(500)

    # === 测试 2: _accountShowResetPassword 守卫 ===
    print('\n=== 测试 2: _accountShowResetPassword 守卫 ===')
    page.evaluate('_accountShowResetPassword()')
    page.wait_for_timeout(500)
    has_modal = page.evaluate("document.getElementById('accountModal') !== null")
    test('_accountShowResetPassword 不渲染流程', not has_modal, f'modal shown: {has_modal}')

    # === 测试 3: _accountShowSetPassword 守卫 ===
    print('\n=== 测试 3: _accountShowSetPassword 守卫 ===')
    page.evaluate('_accountShowSetPassword()')
    page.wait_for_timeout(500)
    has_modal = page.evaluate("document.getElementById('accountModal') !== null")
    test('_accountShowSetPassword 不渲染流程', not has_modal, f'modal shown: {has_modal}')

    # === 测试 4: _accountShowChangeEmail 守卫 ===
    print('\n=== 测试 4: _accountShowChangeEmail 守卫 ===')
    page.evaluate('_accountShowChangeEmail()')
    page.wait_for_timeout(500)
    has_modal = page.evaluate("document.getElementById('accountModal') !== null")
    test('_accountShowChangeEmail 不渲染弹层', not has_modal, f'modal shown: {has_modal}')

    # === 测试 5: 删除账号无验证码选项 ===
    print('\n=== 测试 5: 删除账号无验证码选项 ===')
    page.evaluate('_accountShowDeleteAccount()')
    page.wait_for_timeout(500)
    has_code_option = page.evaluate("""() => {
        const modal = document.getElementById('accountModal');
        if (!modal) return false;
        const btns = modal.querySelectorAll('button');
        for (const b of btns) {
            if (b.textContent.includes('验证码') || b.getAttribute('onclick')?.includes('daChooseCode')) return true;
        }
        return false;
    }""")
    test('删除账号无验证码选项', not has_code_option, f'code option found: {has_code_option}')
    page.keyboard.press('Escape')
    page.wait_for_timeout(500)

    # === 测试 6: cache busting ===
    print('\n=== 测试 6: cache busting ===')
    src = page.evaluate("""() => {
        const scripts = document.querySelectorAll('script[src*="account.js"]');
        return scripts.length > 0 ? scripts[0].src : null;
    }""")
    has_v42 = src and 'v=42' in src
    test('account.js cache version v=42', has_v42, f'src={src}')

    browser.close()

# 汇总
print('\n' + '=' * 50)
passed = sum(1 for _, c, _ in results if c)
failed = sum(1 for _, c, _ in results if not c)
print(f'{passed} passed, {failed} failed')
if failed > 0:
    print('\nFailed tests:')
    for name, cond, detail in results:
        if not cond:
            print(f'  ✗ {name} — {detail}')
    sys.exit(1)
```

### 8.2 后端守卫测试（test_r14_backend_guard.sh）

```bash
#!/bin/bash
# R14 后端守卫测试：无邮件配置时 3 个发码接口返回 503
# 用法: bash tests/curl/test_r14_backend_guard.sh
# 前置: 服务运行在 localhost:3000，未配置邮件服务
# 需要: 有效 JWT token（从 localStorage 获取）

BASE="http://localhost:3000"
TOKEN="${1:-}"
if [ -z "$TOKEN" ]; then
  echo "Usage: bash test_r14_backend_guard.sh <jwt_token>"
  echo "获取 token: 登录后从浏览器 localStorage.getItem('token') 复制"
  exit 1
fi

passed=0
failed=0

check_503() {
  local name=$1
  local url=$2
  local method=$3
  local data=$4
  local status
  if [ "$method" = "POST" ]; then
    status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$url" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "$data")
  else
    status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$url" \
      -H "Authorization: Bearer $TOKEN")
  fi
  if [ "$status" = "503" ]; then
    echo "  ✓ $name → 503 EMAIL_NOT_CONFIGURED"
    passed=$((passed + 1))
  else
    echo "  ✗ $name → got $status (expected 503)"
    failed=$((failed + 1))
  fi
}

echo "R14 后端守卫测试"
echo "="
check_503 "set-password/send-code" "$BASE/api/auth/set-password/send-code" "POST" "{}"
check_503 "change-email/send-code" "$BASE/api/auth/change-email/send-code" "POST" '{"new_email":"test@example.com"}'
check_503 "delete-account/send-code" "$BASE/api/auth/delete-account/send-code" "POST" ""

echo ""
echo "$passed passed, $failed failed"
[ $failed -eq 0 ]
```
