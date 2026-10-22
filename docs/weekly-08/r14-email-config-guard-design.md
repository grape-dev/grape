# R14 设计：账号安全流程"邮箱验证码可用性"判断修复

> **状态**：已实施，已推送
> **分支**：`feat/fullscreen-zindex-fix` → target `grape-dev/weekly-08`
> **影响文件**：account.js + grape_auth.py + index.html
> **需求文档**：`docs/weekly-08/email-config-guard-requirements.md`

---

## 1. 问题总结

Grape 账号安全流程定义了两层可用性判断规则：第一层是系统邮件服务配置，第二层是用户邮箱绑定。但 5 个前端流程入口和 3 个后端发码路由只检查了第二层，跳过了第一层，导致未配置邮件服务的实例上仍显示邮箱验证码入口，用户被引导走完一段注定失败的流程。

### 1.1 两层判断规则

| 层级 | 判断依据 | 来源 | 未满足时 |
|------|----------|------|----------|
| 第一层（系统配置） | 服务器是否配置邮件服务 | /auth/providers 的 email_configured | 一切邮箱功能入口不显示 |
| 第二层（用户数据） | 用户是否绑定邮箱 | _accountProfile.email | 引导绑定（而非隐藏能力） |

公式：流程入口显示 = 系统配置了邮件服务 AND（视流程而定）用户已绑定邮箱。

### 1.2 正面样板（已正确实现）

| 样板 | 位置 | 说明 |
|------|------|------|
| 邮箱管理卡片 | account.js L143-154 | window._emailConfigured !== false 时才渲染 |
| /auth/providers | grape_auth.py L2265-2267 | 返回 email_configured 字段 |
| _emailConfigured 缓存 | account.js L108-114 | 拉取后缓存到 window._emailConfigured |
| register_send_code | grape_auth.py L268 | 有 is_email_configured 守卫（R7 降级） |
| forgot_password_send_code | grape_auth.py L458 | 有 is_email_configured 守卫（503） |

### 1.3 问题清单

前端（account.js）5 处：

| # | 位置 | 流程 | 现状 | 问题 |
|---|------|------|------|------|
| 1 | L328 | 修改密码"忘记密码"链接 | hasEmail | 缺第一层 |
| 2 | L335 | _accountShowResetPassword 守卫 | !hasEmail | 缺第一层 |
| 3 | L407 | _accountShowSetPassword 守卫 | !_accountProfile.email | 缺第一层 |
| 4 | L1296 | 删除账号验证码选项 | hasEmail | 缺第一层 |
| 5 | L460 | _accountShowChangeEmail 入口 | 无守卫 | 无防御 |

后端（grape_auth.py）3 个路由：

| 路由 | 行号 | 守卫 | 无配置时行为 |
|------|------|------|-------------|
| set_password_send_code | L568 | 无 | 假成功或 RuntimeError |
| change_email_send_code | L649 | 无 | 同上 |
| delete_account_send_code | L729 | 无 | 同上 |

---

## 2. R14-1：前端改动（5 处）

### 2.1 改动 1 — L328：修改密码"忘记密码"链接

**当前**：
```js
(hasEmail ? '<p ...><a ... onclick="_accountShowResetPassword()">' + ... + '</a></p>' : '')
```

**改为**：
```js
(hasEmail && window._emailConfigured !== false ? '<p ...><a ... onclick="_accountShowResetPassword()">' + ... + '</a></p>' : '')
```

**Diff**：
```diff
-    (hasEmail ? '<p style="text-align:center;margin-top:12px"><a ...
+    (hasEmail && window._emailConfigured !== false ? '<p style="text-align:center;margin-top:12px"><a ...
```

**场景用例**：
```
系统未配置邮件服务，用户已绑定邮箱。
用户打开"修改密码"弹层。
→ 当前：弹层底部显示"忘记当前密码？"链接
→ 用户点击 → 进入重置流程 → 点"发送验证码" → 前端乐观切到"已发送" → 后端返回 503
修复后：window._emailConfigured === false → 链接不渲染 → 用户只看到密码修改表单
```

### 2.2 改动 2 — L335-342：_accountShowResetPassword 守卫

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
用户在控制台执行 _accountShowResetPassword()。
→ 当前：hasEmail 为 true → 通过守卫 → 渲染重置流程
修复后：window._emailConfigured === false → toast"邮件服务未配置" → 不渲染流程
```

### 2.3 改动 3 — L407-412：_accountShowSetPassword 守卫

**当前**：
```js
function _accountShowSetPassword() {
  // 前置检查：设置密码需要邮箱验证码，无邮箱时引导先绑定邮箱
  if (!_accountProfile || !_accountProfile.email) {
    app.showToast(I18N.t('app.account.set_password_needs_email'));
    _accountShowChangeEmail();
    return;
  }
```

**改为**：
```js
function _accountShowSetPassword() {
  // 第一层：系统邮件服务配置检查
  if (window._emailConfigured === false) {
    app.showToast(I18N.t('app.account.email_not_configured') || 'Email service not configured, please contact administrator');
    return;
  }
  // 第二层：用户邮箱检查（系统已配置但用户无邮箱 → 引导绑定）
  if (!_accountProfile || !_accountProfile.email) {
    app.showToast(I18N.t('app.account.set_password_needs_email'));
    _accountShowChangeEmail();
    return;
  }
```

**场景用例**：
```
系统未配置邮件服务，用户无密码但已绑定邮箱。
→ 当前：hasEmail 为 true → 通过守卫 → 渲染设置密码流程
修复后：window._emailConfigured === false → toast → 不渲染

系统已配置邮件服务，用户无密码且无邮箱。
→ 第一层通过 → 第二层 !_accountProfile.email → toast"请先绑定邮箱" → 跳转修改邮箱
→ 第二层"引导绑定而非隐藏"行为正确
```

### 2.4 改动 4 — L1296/L1302：删除账号验证码选项

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
系统未配置邮件服务，用户有邮箱+密码。
→ 当前：验证方式列表包含"验证码验证"
修复后：canVerifyByEmail = false → 只有"密码验证" → 用户仍可删除账号

系统未配置邮件服务，用户有邮箱，无密码，无 OAuth。
→ canVerifyByEmail=false, hasPwd=false, bindings=[] → methods 为空
→ toast"至少保留一种登录方式" → 无法删除账号
→ 正确：无邮件服务时验证码不可用，没有其他验证方式
```

### 2.5 改动 5 — L460-463：_accountShowChangeEmail 防御性守卫

**当前**：
```js
function _accountShowChangeEmail() {
  var isZh = _accountIsZh();
```

**改为**：
```js
function _accountShowChangeEmail() {
  if (window._emailConfigured === false) {
    app.showToast(I18N.t('app.account.email_not_configured') || 'Email service not configured, please contact administrator');
    return;
  }
  var isZh = _accountIsZh();
```

**场景用例**：
```
系统未配置邮件服务，用户在控制台执行 _accountShowChangeEmail()。
→ 当前：无守卫 → 渲染修改邮箱弹层
→ 正常入口已被邮箱卡片守卫保护，此行防未来新增调用点漏判
修复后：toast → 不渲染弹层
```

---

## 3. R14-2：后端改动（3 个路由）

### 3.1 set_password_send_code（L568-570）

**当前**：
```python
def set_password_send_code(user: dict = Depends(require_jwt_only), request: Request = None):
    """发送设置密码验证码（无密码用户）。"""
    ip = _get_client_ip(request) if request else ""
```

**改为**：
```python
def set_password_send_code(user: dict = Depends(require_jwt_only), request: Request = None):
    """发送设置密码验证码（无密码用户）。"""
    # R14-2: 无邮箱配置降级——返回 503，对齐 forgot_password_send_code 模式
    from src.auth.email_service import is_email_configured
    if not is_email_configured():
        raise HTTPException(status_code=503, detail={"code": 503, "error_code": "EMAIL_NOT_CONFIGURED", "message": "Email service not configured, please contact administrator"})

    ip = _get_client_ip(request) if request else ""
```

### 3.2 change_email_send_code（L649-651）

同模式，在函数开头加守卫。

### 3.3 delete_account_send_code（L729-731）

同模式，在函数开头加守卫。

### 3.4 场景用例（后端通用）

```
系统未配置邮件服务，攻击者绕过前端直接 POST /api/auth/set-password/send-code。
→ 当前：无守卫 → 进入用户检查 → 调 send_code
→ 生产：RuntimeError 兜底（500）
→ 非生产 LOG_ONLY：验证码只写日志，返回"验证码已发送"假成功
修复后：is_email_configured() === False → 503 EMAIL_NOT_CONFIGURED → 清晰错误
```

---

## 4. 不改的（避免为改而改）

| 项 | 原因 |
|----|------|
| window._emailConfigured 获取逻辑（L108-120） | 信号获取已正确，fail-open 默认值是展示类判断的保守策略 |
| OAuth 登录按钮不过滤 configured | app.js R7.5 注释明确的设计决策 |
| verification.py 内部逻辑 | 守卫在路由入口拦截，不需要改 verify 侧 |
| 不加新接口 | 复用既有 /auth/providers 信号 |
| 不动 DB | 纯配置检查，不涉及数据层 |
| 不动接口签名 | 守卫在函数内部，不改路由参数 |
| dev 环境 888888 固定验证码 | 守卫只在无邮件配置时拦截，有配置时不受影响 |

---

## 5. 风险分析

### 5.1 fail-open 默认值

window._emailConfigured 在 /auth/providers 请求失败时默认 true（account.js L114）。这意味着网络异常时流程入口会显示，但后端 503 守卫会兜底。与现状一致，不引入新风险。

### 5.2 删除账号边界场景

无邮件服务 + 用户无密码 + 无 OAuth 绑定 → methods 为空 → 无法删除账号。这是正确行为：无邮件服务时验证码不可用，用户没有其他验证方式。修复方式：联系管理员配置邮件服务或先绑定 OAuth。

### 5.3 dev 环境 888888 固定验证码

守卫只在 is_email_configured() === False 时拦截。配置了邮件服务（即使是 dev 模式）时不拦截，888888 固定验证码不受影响。

### 5.4 回归风险

- 前端：5 处改动都是条件加严（hasEmail → hasEmail && _emailConfigured !== false），不会让原来不显示的入口突然显示
- 后端：3 处改动都是加守卫（原来无 → 有 503），不会让原来通过的请求被拒绝（配置了邮件服务时 is_email_configured() === True）
- 有邮件服务的实例：所有流程不受影响

---

## 6. 验证方法

### 6.1 Playwright 前端测试

见 §8 测试用例代码。

### 6.2 curl 后端测试

见 §8 测试用例代码。

### 6.3 验证结果

- JS 语法检查：account.js node -c 通过
- Python 编译：grape_auth.py py_compile 通过
- 后端测试：test_grape_auth.py 44 passed, 0 failed
- JS 测试：test_issue_294_im_unbind.js 8/8 passed

---

## 7. 改动汇总表

| # | 类型 | 文件 | 行号 | 改动 |
|---|------|------|------|------|
| 1 | R14-1 | account.js | L328 | hasEmail → hasEmail && _emailConfigured !== false |
| 2 | R14-1 | account.js | L335-342 | 加 _emailConfigured 检查 |
| 3 | R14-1 | account.js | L407-412 | 加第一层检查（在第二层之前） |
| 4 | R14-1 | account.js | L1296/L1302 | hasEmail → canVerifyByEmail |
| 5 | R14-1 | account.js | L460-463 | 函数开头加防御性守卫 |
| 6 | R14-2 | grape_auth.py | L568-570 | is_email_configured 503 守卫 |
| 7 | R14-2 | grape_auth.py | L649-651 | 同上 |
| 8 | R14-2 | grape_auth.py | L729-731 | 同上 |
| 9 | cache | index.html | L327 | account.js v=41 → v=42 |

---

## 8. 测试用例代码

### 8.1 Playwright 前端测试

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

### 8.2 curl 后端测试

```bash
#!/bin/bash
# R14 后端守卫测试：无邮件配置时 3 个发码接口返回 503
# 用法: bash tests/curl/test_r14_backend_guard.sh <jwt_token>
# 前置: 服务运行在 localhost:3000，未配置邮件服务
# 获取 token: 登录后从浏览器 localStorage.getItem('token') 复制

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
  local data=$3
  local status
  if [ -n "$data" ]; then
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
echo "================="
check_503 "set-password/send-code" "$BASE/api/auth/set-password/send-code" "{}"
check_503 "change-email/send-code" "$BASE/api/auth/change-email/send-code" '{"new_email":"test@example.com"}'
check_503 "delete-account/send-code" "$BASE/api/auth/delete-account/send-code" ""

echo ""
echo "$passed passed, $failed failed"
[ $failed -eq 0 ]
```
