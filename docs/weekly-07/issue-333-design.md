# Issue #333 设计方案：账号安全 UX 与 OAuth 绑定弹窗优化

> 分支：feat/issue-333-account-security-ux-oauth-fix
> 基线：weekly-07
> 日期：2026-10-14
> Issue: https://gitcode.com/grape-dev/grape/issues/333

---

## 一、问题总览

当前系统存在四类问题：

1. **OAuth 绑定流程性能问题**（P0）—— popup 模式 7 步操作导致卡顿，对比登录页 2 步全页跳转体验割裂
2. **账号安全页 UX/逻辑缺陷**（P1）—— 设置密码缺乏引导、前置条件未拦截、注销缺少 OAuth 入口
3. **Worker 死代码**（P1）—— config.py 顶层 import 污染 worker 启动链
4. **登录白名单无全局开关**（P1）—— 表有数据即启用校验，一个平台有白名单导致另一个平台被误拦

---

## 二、组件职责与能力边界定义

### 2.1 组件清单

| 组件 | 职责 | 能力边界 |
|------|------|----------|
| **app.js oauthLogin()** | 登录场景 OAuth 发起 | 仅全页跳转，不弹窗、不操作 DOM |
| **account.js _accountDoOauthBind()** | 绑定场景 OAuth 发起 | 管理 popup 生命周期 + 结果处理 |
| **account.js _accountShowModal/_accountCloseModal** | 模态框基础设施 | 仅负责显示/关闭/锁定，不关心业务内容 |
| **oauth-popup-callback.html** | popup 中间页 | 接收回调结果 → postMessage 通知父窗口 → 关闭自身 |
| **grape_auth.py oauth_callback()** | 后端回调处理 | 校验 → 换 token → 分流（bind/login/delete）→ 302 重定向 |
| **grape_auth.py _build_redirect_url()** | 重定向 URL 构造 | 根据 popup 标记决定重定向到中间页还是主站 |
| **grape_auth.py _handle_oauth_bind()** | 绑定业务逻辑 | 重复检查/占位合并/转移确认/创建绑定 → 重定向 |

### 2.2 职责分离原则

**前端职责：**
- 发起 OAuth 请求获取授权 URL
- 管理 popup 生命周期（打开/监听/关闭/超时）
- 接收 postMessage 结果并处理后端确认
- UI 状态反馈（按钮 loading、成功/失败提示）

**后端职责：**
- 构造授权 URL + 生成 state
- 回调校验（state 原子消费 + provider 一致性 + nonce）
- 换 token + 获取用户信息
- 业务分流（bind/login/delete）
- 重定向到前端页面

**中间页职责：**
- 解析 URL 参数得到结果
- postMessage 通知父窗口
- 自身关闭
- 降级：opener 不可用时显示静态提示

**禁止越界：**
- 前端不做后端业务判断（不信任 postMessage success，必须后端确认）
- 后端不关心前端是 popup 还是全页跳转（popup 标记仅影响重定向目标）
- 中间页不做业务逻辑（只传递结果，不调后端 API）

---

## 三、OAuth 流程设计方案

### 3.1 登录场景

**当前问题：新版 exchange_code 机制引入了额外网络往返**

旧版登录回调链路（改前）：
```
GitCode 回调 → 后端生成 JWT → 302 到 /?token=JWT
  → 首页 JS 从 URL 读 token → localStorage → showApp()
```
1 次重定向，0 次额外 API 请求。用户回到首页后立即进入应用。

新版登录回调链路（当前）：
```
GitCode 回调 → 后端生成 exchange_code 存 DB → 302 到 /?exchange_code=xxx
  → 首页 JS 从 URL 读 exchange_code → POST /api/auth/exchange
  → 后端查 DB 验证 → 返回 JWT → localStorage → showApp()
```
1 次重定向 + 1 次额外 API 请求。用户回到首页后多等一个网络往返才能进入应用。

exchange_code 机制的安全价值（不在 URL 中暴露 JWT）是正确的，但可以通过优化消除感知延迟。

**优化方向：后端在 302 重定向时同时设置 JWT cookie**

```
GitCode 回调 → 后端生成 exchange_code → 存 DB
  → 同时生成 JWT → 设置 HttpOnly cookie（grape_refresh 已有此机制）
  → 也设置一个非 HttpOnly 的临时 cookie（grape_exchange=exchange_code）
  → 302 到 /
  → 首页 JS 检测 cookie → 直接用 cookie 中的 exchange_code 换 JWT
    → 或：后端在 302 响应中直接设置 JWT 到 cookie，前端读 cookie 即可
```

或更简单的方案：后端在重定向时把 JWT 同时写入 cookie（已有 `_set_refresh_cookie` 机制），前端 showApp 时如果 URL 没有 exchange_code 但 cookie 有 JWT，直接用 cookie 中的 JWT，不等 POST /auth/exchange 返回。

**本次 issue 修复范围：** 登录场景的 exchange_code 延迟问题记录为已知优化项，本次不实施改动（涉及安全机制调整，需单独评估）。保持当前全页跳转机制不变。

### 3.2 绑定场景（优化后）

**设计决策：保留 popup 模式，但消除中间步骤**

用户在已登录的设置页面，全页跳转会丢失当前页面状态。popup 模式是正确的选择，问题在于实现引入了不必要的中间操作。

**优化后流程（4 步）：**

```
用户点击"前往授权"
  → ① 按钮显示 spinner（同步，用户点击上下文内）
  → ② 同步打开空白弹窗 window.open('')（防浏览器拦截，必须同步）
  → ③ 异步 API.get('/auth/bind/{provider}?popup=true')
       → 成功：popup.location.href = redirect_url（弹窗直接跳到 GitCode）
       → 失败：popup.close() + 按钮恢复 + toast 错误
  → ④ 注册 message handler + setInterval 轮询 popup.closed
```

**关键优化点：**

| 优化 | 旧代码 | 新代码 | 效果 |
|------|--------|--------|------|
| 模态框内容替换 | innerHTML 替换为 spinner + cloneNode 重建 overlay | **不替换**，模态框保持原样，按钮已有 spinner | 消除 2 次 DOM 操作 |
| 禁用遮罩关闭 | cloneNode(true) + replaceChild | `overlay.dataset.locked = '1'`，点击事件检查 `if (!overlay.dataset.locked)` | 1 个属性赋值 vs DOM 重建 |
| popup 初始内容 | document.write loading HTML | **不写入**，用 about:blank 默认白页 | 消除 document.write 阻塞 |
| 视觉切换 | 3 次（弹窗弹出 → loading → 跳转） | 1 次（弹窗弹出 → 直接跳转 GitCode） | 用户感知一致 |

**模态框锁定机制（替代 cloneNode）：**

```js
// _accountShowModal 创建时：
overlay.addEventListener('click', function(e) {
    if (e.target === overlay && !overlay.dataset.locked) _accountCloseModal();
});

// 授权进行时锁定：
overlay.dataset.locked = '1';

// 授权结束后解锁：
delete overlay.dataset.locked;
```

### 3.3 popup-callback.html 降级路径优化

**当前问题：** window.opener 被浏览器切断后，降级路径跳转到主站 `/?xxx#account`，弹窗加载完整 SPA。

**优化方案：降级时只显示静态提示，不跳转主站。**

```
popup-callback.html 逻辑：
  1. 解析 URL 参数得到结果（bind_success / bind_error / need_transfer / oauth_error）
  2. 尝试 postMessage 通知父窗口
  3. 如果 window.opener 可用 → postMessage → setTimeout 关闭自身
  4. 如果 window.opener 不可用（降级）→ showFallback 静态提示"授权已完成，请返回主窗口"
     → 不跳转主站
```

**安全性保障：** 父窗口的 `popup.closed` 轮询（500ms 间隔）在弹窗关闭后主动调 `/auth/me` 后端确认绑定状态，不依赖 postMessage。即使降级路径不 postMessage，父窗口也能正确感知结果。

### 3.4 oauth_callback 早期错误路径修复

**当前问题：** popup 变量在第 1712 行才提取，第 1697-1709 行的早期错误分支未传 popup 参数。

**修复：** 将 `popup = bool(state_data.get("popup"))` 提前到 state 校验之后、provider 校验之前。对于 state_data 为 None 的分支（state 已过期），统一用 popup-callback.html 兜底——即使不是 popup 模式走到这里，popup-callback.html 找不到 opener 也会走降级提示，无副作用。

```python
# 修复后顺序：
state_data = validate_oauth_state(state)
if state_data is None:
    # state 无效 — 无法判断 popup 模式，用 popup-callback 兜底
    return _oauth_error_redirect(request, "state_invalid", popup=True)

popup = bool(state_data.get("popup"))  # ← 提前提取

# provider 校验（此时已有 popup 变量）
state_provider = state_data.get("_provider", "")
if not state_provider:
    return _oauth_error_redirect(request, "provider_unknown", popup=popup)
if provider != state_provider:
    return _oauth_error_redirect(request, "provider_mismatch", popup=popup)
```

---

## 四、账号安全页 UX 修复方案

### 4.1 密码管理完整设计（三种场景）

账号安全页的密码管理需要覆盖三种场景，不留死角：

| 场景 | 用户状态 | 入口 | 流程 |
|------|----------|------|------|
| 设置密码 | 无密码（OAuth 用户） | "设置密码"按钮 | 邮箱验证码 → 新密码（无确认密码） |
| 修改密码 | 有密码，记得当前密码 | "修改密码"按钮 | 当前密码 → 新密码 → 确认新密码 |
| 重置密码 | 有密码，不记得当前密码 | 修改密码模态框内"忘记当前密码？"链接 | 邮箱验证码 → 新密码 |

**场景 1：设置密码（无密码用户）**

已在本节下方 4.1 详细描述。

**场景 2：修改密码（有密码用户）**

- 当前密码输入框（`autocomplete="current-password"`）
- 新密码输入框（`autocomplete="new-password"`）
- 确认新密码输入框（`autocomplete="new-password"`）——**保留**（修改密码场景确认密码有价值：用户输入了当前密码，如果新密码打错会锁死）
- 底部加"忘记当前密码？"链接（仅有邮箱时显示）

**场景 3：重置密码（有密码但忘记当前密码）**

- 复用后端已有的 `/auth/forgot-password/send-code` + `/auth/forgot-password/reset` 接口
- 前端在修改密码模态框内加"忘记当前密码？"链接
- 点击后模态框内容切换为重置流程：邮箱验证码 → 新密码（无确认密码，与设置密码一致）
- 重置后 token_version +1，全设备下线（后端已有逻辑）
- 前置条件：需要绑定邮箱（无邮箱时不显示链接）

### 4.2 设置密码流程优化（问题 3/4/5）

**问题 3：缺乏上下文引导**
**问题 4：前置条件（邮箱）未在 UI 层拦截**
**问题 5：确认密码字段**

**优化后模态框设计：**

```
步骤 0（前置检查）：
  _accountShowSetPassword() 调用时检查 _accountProfile.email
  → 无邮箱：不弹模态框，toast 提示"请先绑定邮箱后再设置密码"，
    自动跳转到邮箱绑定流程
  → 有邮箱：继续弹出模态框

步骤 1（说明 + 发送验证码）：
  标题："设置 Grape 账号密码"
  说明文案："为您的 Grape 账号设置一个密码，设置后可用邮箱/用户名 + 密码登录，
            不再依赖第三方平台。验证码将发送到 xxx@email.com"
  按钮："发送验证码"

步骤 2（输入验证码 + 新密码）：
  验证码输入框
  新密码输入框（autocomplete="new-password"）
  [移除确认密码字段]
  按钮："确认设置"
```

**变更点：**
- 模态框标题从"设置密码"改为"设置 Grape 账号密码"
- 新增说明文案（解释为什么设置、设置后能做什么）
- 显示具体邮箱地址（动态插入 `_accountProfile.email`）
- 无邮箱时前端拦截，引导先绑定邮箱
- 移除确认密码字段（同步修改需求 12）
- 新密码输入框加 `autocomplete="new-password"`

**需求 12 同步修改：**
- 第 539 行："用户输入验证码、新密码、确认新密码" → "用户输入验证码、新密码"
- 第 549 行："两次密码输入不一致时提示" → 删除
- 后端 `/auth/set-password` 不需要改（只接收 `new_password`）

### 4.2 修改密码流程（同步优化）

- 当前密码输入框加 `autocomplete="current-password"`
- 新密码输入框加 `autocomplete="new-password"`
- 确认新密码字段：**保留**（修改密码场景确认密码有价值——用户输入了当前密码，如果新密码打错会锁死）

### 4.3 注销账号 OAuth 认证入口（问题 6b）

**当前问题：** 后端完整实现了 OAuth 注销认证，前端没有入口。

**优化后注销模态框：**

```
步骤 1：选择验证方式
  → 有密码：显示"用密码验证" + "用验证码验证"
  → 无密码有邮箱：显示"用验证码验证" + "用 OAuth 重新授权"
  → 无密码无邮箱：显示"用 OAuth 重新授权"（唯一选项）
  → 无密码无邮箱无 OAuth 绑定：显示"无法注销，请先绑定邮箱或设置密码"
```

**前端逻辑：**
```js
function _accountShowDeleteAccount() {
    var hasPwd = _accountProfile.has_password;
    var hasEmail = _accountProfile.email;
    var bindings = _accountProfile.bindings || [];

    // 动态渲染可用验证方式
    var methods = [];
    if (hasPwd) methods.push({id: 'pwd', label: '用密码验证'});
    if (hasEmail) methods.push({id: 'code', label: '用验证码验证'});
    if (bindings.length > 0) methods.push({id: 'oauth', label: '用第三方平台重新授权'});

    if (methods.length === 0) {
        // 无任何验证方式
        app.showToast('请先绑定邮箱或设置密码后再注销');
        return;
    }
    // 渲染模态框...
}
```

**OAuth 注销流程：**
```
用户选择"用第三方平台重新授权"
  → 如果只有一个绑定平台：直接发起 OAuth
  → 如果有多个绑定平台：先选择平台
  → API.get('/auth/delete-account/oauth/{provider}?popup=true')
  → popup 模式走 GitCode 授权
  → 回调 _handle_oauth_delete → 校验 external_id 匹配
  → 302 到 popup-callback.html?delete_oauth_state=xxx
  → postMessage 通知父窗口
  → 父窗口调 POST /auth/delete-account {oauth_state: xxx}
  → 注销完成
```

### 4.4 注销验证码前置条件（问题 12）

无邮箱时不显示"用验证码验证"按钮（已在 4.3 中通过动态渲染解决）。

### 4.5 安全提示条样式（问题 9）

**当前：** 硬编码 Bootstrap 黄色 `#fff3cd` / `#ffe69c`
**优化：** 使用 CSS 变量

```js
// 旧：
banner.style.cssText = '...background:#fff3cd;border-bottom:1px solid #ffe69c;...';

// 新：
banner.style.cssText = '...background:var(--warn-bg, rgba(255,204,0,0.12));'
    + 'border-bottom:1px solid var(--warn-border, rgba(255,204,0,0.3));'
    + 'color:var(--t);...';
```

需要在 CSS 中新增警告色变量（两个主题各一套）。

### 4.6 模态框输入框宽度（问题 10）

**当前：** `.kb-input { width: 200px }` 固定宽度
**优化：** 模态框内覆盖

```css
#accountModal .kb-input {
    width: auto;
    flex: 1;
}
```

### 4.7 设置密码/修改邮箱提示显示具体邮箱（问题 11）

```js
// 设置密码模态框：
var emailHint = I18N.t('app.account.code_sent_to_email_hint') + '：' + _accountEsc(_accountProfile.email);

// 修改邮箱第二步（回显用户输入的新邮箱）：
var newEmail = document.getElementById('ceEmail').value.trim();
var hint = I18N.t('app.account.code_sent_to_new_email') + '：' + _accountEsc(newEmail);
```

---

## 五、Worker 死代码修复（问题 13）

### 5.1 修复方案

直接删除 `src/config.py` 第 195-197 行：

```python
# 删除这三行：
# 生产环境标识（从 configs/security.json 读取，无文件则默认 development）
from src.auth.security_config import is_production as _is_prod
IS_PRODUCTION = _is_prod()
```

### 5.2 安全性验证

- `IS_PRODUCTION` 全库零消费（只有定义没有读取）——已验证
- 所有真正用 `is_production()` 的地方都是自己懒加载 import：
  - `email_service.py:27` — 函数级
  - `providers.py:405` — 函数级
  - `grape_auth.py:1117` — 函数级
  - `grape_auth.py:1313` — 函数级
  - `main.py:24` — 启动时
- 删除后 worker 启动链不再 import `src.auth.security_config`

### 5.3 防御测试

新增测试：在干净子进程中 import `src.config`，验证不拉起 `src.auth` 模块：

```python
def test_config_no_auth_dependency():
    """config.py 不应依赖 src.auth——worker 共享此文件"""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, '-c', 'import src.config; print("OK")'],
        capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert 'src.auth' not in result.stderr  # 无 auth 模块导入错误
```

---

## 六、登录白名单全局开关（问题 14）

### 6.1 问题分析

**当前逻辑（config.py 第 325-365 行）：**

```python
def is_user_allowed(whitelist_column, login):
    # 查 DB
    count = SELECT COUNT(*) FROM login_whitelist
    if count > 0:                          # 表有数据 → 启用校验
        row = SELECT 1 FROM login_whitelist WHERE {col}={login}
        return row is not None             # 查不到 → 拦截
    # 表为空 → 降级文件
    if not 文件存在: return True
    ...
```

**设计缺陷：**

1. **没有全局开关**——用"表是否有数据"隐式判断开关状态，不是显式配置
2. **跨平台污染**——表结构是每行同时有 `gitcode_username` + `github_username` 两个字段。表里有 GitCode 白名单数据（`github_username=''`），`COUNT(*) > 0` 为 True，GitHub 登录时 `WHERE github_username='yangaiwu'` 查不到 → 被拦截
3. **admin 无法管理**——admin 管理的是 `signup_whitelist`（注册白名单），`login_whitelist`（登录白名单）没有管理入口，只能直接改 DB
4. **对比 signup_whitelist**——注册白名单有显式开关 `allow_signup`（meta 表），login_whitelist 没有对应开关

### 6.2 修复方案

**方案：显式全局开关 + 按平台独立校验**

#### 6.2.1 新增 meta 配置项

在 `meta` 表中新增 `login_whitelist_enabled` 配置项：

| key | 值 | 语义 |
|-----|-----|------|
| `login_whitelist_enabled` | `true` / `1` / `yes` | 白名单启用 |
| `login_whitelist_enabled` | `false` / `0` / `no` | 白名单关闭，全部放行 |
| `login_whitelist_enabled` | 未设置 / 空 | **兼容旧部署**：查表有数据则隐式启用 + WARNING 日志，表为空则关闭 |

兼容旧部署的理由：存量部署未配置 meta 但表有白名单数据，直接关闭会导致安全策略静默失效。
隐式启用 + WARNING 提示管理员显式设置开关。新部署表为空时默认关闭不拦截。

#### 6.2.2 修改 is_user_allowed 逻辑

```python
def is_user_allowed(whitelist_column: str, login: str) -> bool:
    """白名单判定——需显式启用才校验，未启用时全部放行。

    开关：meta 表 login_whitelist_enabled（true=启用，未设置=兼容旧部署隐式启用）
    启用时：按平台列独立校验，查 WHERE {col}=?（if not login 守卫空字符串入参）
    未启用时：直接返回 True
    """
    if not login:
        return False

    # 显式开关检查
    if not _is_login_whitelist_enabled():
        return True

    # 列名安全校验
    import re
    if not re.match(r'^[a-z_]+$', whitelist_column):
        log.warning(f"is_user_allowed: invalid whitelist_column='{whitelist_column}'")
        return False

    # 查 DB——按平台列独立校验
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            row = c.execute(
                f"SELECT 1 FROM login_whitelist WHERE {whitelist_column}=?", (login,)
            ).fetchone()
            return row is not None
    except Exception:
        pass  # DB 不可用，降级文件

    # 降级：读文件
    try:
        if not os.path.exists(LOGIN_WHITELIST_PATH):
            return True
        with open(LOGIN_WHITELIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        wl = data.get("whitelist") if isinstance(data, dict) else None
        if not wl:
            return True
        return login in wl
    except Exception:
        return True


def _is_login_whitelist_enabled() -> bool:
    """登录白名单是否启用。未配置时默认关闭。"""
    try:
        from src.user.db import get_meta_value
        val = get_meta_value("login_whitelist_enabled")
        if val is None or val == "":
            return False  # 未配置默认关闭
        return val.lower() in ("true", "1", "yes")
    except Exception:
        return False  # 异常时默认关闭（不拦截）
```

#### 6.2.3 关键变更

| 旧逻辑 | 新逻辑 | 效果 |
|--------|--------|------|
| `COUNT(*) > 0` 隐式判断开关 | 显式查 `meta.login_whitelist_enabled` | 关了开关就全部放行 |
| 表有数据即启用校验 | 开关启用才校验 | GitCode 白名单不影响 GitHub |
| `WHERE {col}=?`（空字符串也能匹配） | 同样 `WHERE {col}=?`，但只在开关启用时查 | 按平台列独立校验 |
| 未配置默认放行 | 未配置默认关闭（不拦截） | 新部署不拦用户 |

#### 6.2.4 不改动的部分

- `login_whitelist` 表结构不变（不删列、不加列）
- `_check_whitelist()` 调用方不变（grape_auth.py 第 1095-1105 行）
- `signup_whitelist` 不受影响（独立体系）
- admin 管理入口不在本次范围（后续可加 login_whitelist 管理 UI）

---

## 七、改动清单

### 后端

| 文件 | 改动 | 问题 |
|------|------|------|
| `src/config.py` | 删除第 195-197 行 IS_PRODUCTION 死代码 | 13 |
| `src/config.py` | is_user_allowed 加显式开关 _is_login_whitelist_enabled | 14 |
| `src/api/routes/grape_auth.py` | popup 变量提前到 provider 校验之前 | 2 |
| `src/api/routes/grape_auth.py` | state_data 为 None 时 popup=True 兜底 | 2 |
| `src/api/routes/grape_auth.py` | 注销前端 need_oauth 响应已有，无需改后端 | 6b |

### 前端

| 文件 | 改动 | 问题 |
|------|------|------|
| `src/api/static/js/pages/account.js` | _accountDoOauthBind 消除 3 次 DOM 操作 | 7 |
| `src/api/static/js/pages/account.js` | _accountShowModal 用 dataset.locked 替代 cloneNode | 7 |
| `src/api/static/js/pages/account.js` | _accountShowSetPassword 加说明文案 + 前置检查邮箱 + 移除确认密码 | 3/4/5 |
| `src/api/static/js/pages/account.js` | _accountShowChangePassword 加 autocomplete | 8 |
| `src/api/static/js/pages/account.js` | _accountShowDeleteAccount 动态渲染验证方式 + OAuth 入口 | 6b/12 |
| `src/api/static/js/pages/account.js` | 设置密码/修改邮箱提示显示具体邮箱 | 11 |
| `src/api/static/js/pages/account.js` | 密码输入框加 autocomplete | 8 |
| `src/api/static/oauth-popup-callback.html` | 降级路径改为 showFallback 静态提示 | 1 |
| `src/api/static/js/app.js` | 安全提示条改用 CSS 变量 | 9 |
| `src/api/static/css/app.css` | 模态框内 .kb-input 覆盖宽度 | 10 |
| `src/api/static/css/app.css` | 新增警告色 CSS 变量 | 9 |

### 文档

| 文件 | 改动 |
|------|------|
| `docs/weekly-06/grape-account-system-requirements.md` | 需求 12 移除确认密码字段 |
| `docs/weekly-06/oauth-popup-redirect-issues-2026-10-14.md` | 问题文档更新修复状态 |
| `docs/weekly-06/account-security-ux-issues-2026-10-14.md` | 问题文档更新修复状态 |

### 测试

| 文件 | 改动 |
|------|------|
| 新增 `tests/test_config_no_auth_dependency.py` | worker 启动链防御测试 |

---

## 七、流程闭环验证

### 7.1 绑定流程闭环

```
用户点击"前往授权"
  → [按钮 spinner] → [同步开空白 popup] → [API 请求 URL]
  → 成功：popup.location.href = GitCode 授权页
    → 用户在 popup 中授权
    → GitCode 回调 /api/auth/callback/{provider}
    → 后端校验 → 换 token → _handle_oauth_bind
    → 302 到 popup-callback.html?bind_success=gitcode
    → popup-callback.html postMessage 通知父窗口
    → 父窗口 _accountConfirmBindSuccess → API.get('/auth/me') 后端确认
    → 成功：显示成功提示 + 跳转项目页
    → 失败：toast 错误
  → popup 关闭（postMessage 后 setTimeout 关闭）
  → 父窗口轮询 popup.closed 兜底

异常路径：
  → popup 被拦截：回退全页跳转
  → window.opener 被切断：popup-callback.html showFallback 静态提示
    → 父窗口轮询 popup.closed → 后端确认 → 正确处理
  → API 请求失败：popup.close() + 按钮恢复 + toast
  → 60s 超时：按钮恢复 + toast 超时提示
  → 需要转移确认：postMessage need_transfer → 父窗口获取 transfer-context → 显示确认弹窗
```

### 7.2 设置密码流程闭环

```
用户点击"设置密码"
  → [前端检查 email]
  → 无邮箱：toast"请先绑定邮箱" → 引导到邮箱绑定
  → 有邮箱：弹出模态框（含说明文案 + 显示邮箱地址）
  → 用户点击"发送验证码"
  → API.post('/auth/set-password/send-code')
  → 成功：切换到步骤 2（验证码 + 新密码，无确认密码）
  → 用户输入验证码 + 新密码 → 点击"确认设置"
  → API.post('/auth/set-password', {code, new_password})
  → 成功：关闭模态框 + toast"密码设置成功" + 刷新安全页
  → 失败：toast 错误
```

### 7.3 注销流程闭环

```
用户点击"注销账号"
  → [前端动态渲染验证方式]
  → 有密码：密码验证 → 输入密码 → POST /auth/delete-account {password}
  → 有邮箱：验证码验证 → 发送验证码 → 输入验证码 → POST /auth/delete-account {code}
  → 有 OAuth 绑定：OAuth 重新授权
    → API.get('/auth/delete-account/oauth/{provider}?popup=true')
    → popup 模式走 GitCode 授权
    → 回调 _handle_oauth_delete → 校验 external_id 匹配
    → 302 到 popup-callback.html?delete_oauth_state=xxx
    → postMessage 通知父窗口
    → 父窗口 POST /auth/delete-account {oauth_state: xxx}
    → 注销完成 → 清除 localStorage → 跳转首页
  → 无任何验证方式：toast"请先绑定邮箱或设置密码"
```

---

## 八、不改动项

| 组件 | 原因 |
|------|------|
| 登录场景 oauthLogin() | 全页跳转已最优，不改 |
| 后端 oauth_callback 主逻辑 | 仅 popup 变量位置调整，业务逻辑不动 |
| 后端 _handle_oauth_bind | 不改动，仅受益于 popup 参数修复 |
| 后端 delete_account_via_oauth | 已完整实现，仅前端对接 |
| 后端 /auth/set-password 接口 | 只接收 new_password，不接收确认密码，不需改 |
| 移动端绑定流程 | 已用全页跳转，无 popup 问题 |

---

# Issue #334: Project Page UX + Hosting Failure Fix

> Issue: https://gitcode.com/grape-dev/grape/issues/334

## Problem List

| # | Priority | Problem | Type |
|---|----------|---------|------|
| 1 | P2 | Multi-platform new-project buttons should merge into single entry | UX |
| 2 | P2 | Button visual prominence too weak | UX |
| 3 | P1 | Hosting failure: cannot add global Bot as maintainer | Bug |
| 4 | P0 | check_repo_name missing user_id for token refresh | Bug |
| 5 | P0 | create_from_template using get_user_token without refresh | Bug |
| 6 | P1 | scenes.py two sites using get_user_token without refresh | Bug |
| 7 | P2 | add_collaborator error handling not precise (403/404/5xx all same) | Bug |
| 8 | P2 | create_repo host_after_create using personal token for webhook (Plan C violation) | Bug |
| 9 | P2 | Frontend submitCreateRepo not checking r.hosted field | Bug |

## Root Cause: Hosting Failure

host_repo used get_user_token (no refresh), user OAuth token expired -> add_collaborator
received 401 -> returned False -> bot self-check found bot not in repo -> 502 error.

Fix: All 12 get_user_token call sites changed to get_user_token_with_refresh across
repos.py (9), templates.py (1), scenes.py (2). _get_user_provider_and_token gained
user_id parameter (from JWT session, not frontend).

## add_collaborator Error Precision

Return type changed from bool to (bool, str). HTTP code extracted from RuntimeError,
mapped to friendly messages: 401 -> token expired, 403 -> no permission,
404 -> repo not found, 5xx -> service unavailable.

## Verification

1. Single new-project button for multi-platform users
2. Button visual enhancement confirmed
3. Token-expired user hosting no longer fails
4. scenes.py works after token expiry
5. add_collaborator shows specific error for 404/5xx
