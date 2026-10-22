# R1–R4 Design: Login 401 Fix + Prompt Optimization + i18n

> **Branch**: `feat/login-error-prompt-optimization`  
> **Requirements doc**: `docs/weekly-08/login-error-prompt-optimization-requirements.md`  
> **Status**: Design — pending review before implementation  
> **Scope**: R1 (401 bug fix), R2 (prompt wording), R3 (prefix removal), R4 (i18n + error_code mapping)

---

## 1. Problem Summary

When a user enters wrong credentials on the login page, the frontend shows **"登录失败: 会话过期，请重新登录"** instead of the backend's actual message **"账号或密码错误"**.

**Root cause** (`api.js:196–206`): `handleResp` treats *all* 401 responses identically — if `retryFn` is present (which `_post` always passes), it attempts a JWT refresh. On the login page there is no valid `refresh_token` cookie, so refresh fails and throws `common.session_expired`, overriding the backend's credential-error message.

This design covers four requirements that together fix the bug and improve the entire login error UX:

| Req | Title | Files touched |
|-----|-------|---------------|
| R1 | Fix `handleResp` 401 branch — skip refresh for credential 401 | `api.js` |
| R2 | Optimize 5 login error prompts + add `error_code` field | `grape_auth.py`, `auth.py`, `login_throttle.py`, `deps.py` |
| R3 | Remove redundant "登录失败:" prefix in `app.js` | `app.js` |
| R4 | i18n language pack sync + `error_code` → i18n key mapping | `zh/app.json`, `en/app.json`, `zh/admin.json`, `en/admin.json`, `api.js`, `app.js`, `register.html` |

---

## 2. R1: Fix `handleResp` 401 Branch

### 2.1 Current Code (`api.js:194–207`)

```js
function handleResp(r, retryFn) {
  if (!r.ok) {
    if (r.status === 401 && retryFn) {
      return _safeErrJson(r).then(err => {
        if (err?.detail?.action === 'reauth_required') {
          _redirectToGitcodeAuth();
          throw new Error(err.detail.message);
        }
        // 普通 JWT 过期——走 JWT refresh 流程
        return handle401(retryFn);          // ← unconditional refresh
      });
    }
    // ...
```

**Problem**: The `handle401(retryFn)` call on line 206 fires unconditionally for every 401 with a `retryFn`. For login-endpoint 401s (credential errors), this is wrong — the user has no session to refresh.

### 2.2 Fix

Add a guard: if the user has **no token in `localStorage`** (i.e., not logged in), the 401 is a *credential* 401, not a *session* 401. Throw the backend's error message directly without attempting refresh.

```js
function handleResp(r, retryFn) {
  if (!r.ok) {
    if (r.status === 401 && retryFn) {
      return _safeErrJson(r).then(err => {
        if (err?.detail?.action === 'reauth_required') {
          _redirectToGitcodeAuth();
          throw new Error(err.detail.message);
        }
        // R1 fix: credential 401 on public auth endpoints — user has no token,
        // so this is a wrong-username/password response, not a session expiry.
        // Skip refresh and surface the backend message directly.
        if (!localStorage.getItem('token')) {
          const detail = (err && err.detail) || {};
          const e = new Error(detail.message || I18N.t('common.request_failed', {status: 401}));
          e.status = 401;
          e.detail = detail;
          e.error_code = detail.error_code || '';
          throw e;
        }
        // Session 401 — JWT expired, attempt refresh
        return handle401(retryFn);
      });
    }
    // ... rest unchanged
```

### 2.3 Why `!localStorage.getItem('token')` is the correct discriminator

| 401 type | Trigger context | `localStorage.token` | Correct action |
|----------|----------------|----------------------|----------------|
| Credential 401 | Login / refresh / exchange endpoints | absent | Show backend message |
| Session 401 | Protected endpoint, JWT expired | present | Attempt refresh → retry |

The `_post` method always passes `retryFn`, so we cannot rely on `retryFn` presence alone. Checking `localStorage.getItem('token')` cleanly distinguishes the two cases. This mirrors the pattern already used in `admin.js` (which excludes login paths via `path.indexOf('/login') < 0`) and in `ensureTokenValid` (which returns `null` when no token exists, line 136).

### 2.4 Edge case: already-logged-in user re-logging in

If a logged-in user (has token) enters wrong password on login, `localStorage.getItem('token')` is truthy, so the code falls through to `handle401`. This would attempt refresh with the *existing* (still valid) refresh token cookie, which succeeds, then retries the login POST — which fails again with 401, creating a loop.

**Mitigation**: The login endpoint `/auth/login` is a public auth endpoint. We can additionally check the request URL:

```js
// R1 fix: skip refresh for public auth endpoints (login, refresh, exchange, associate)
var reqUrl = r.url || '';
var isPublicAuthEndpoint = /\/api\/auth\/(login|refresh|exchange)$/.test(reqUrl)
  || /\/api\/auth\/login\/[^/]+\/associate$/.test(reqUrl);
if (!localStorage.getItem('token') || isPublicAuthEndpoint) {
  // credential 401 — throw backend message, no refresh
  ...
}
```

> **Note**: `r.url` is the response's final URL (after redirects). If `r.url` is unreliable in some browsers, the fallback is `!localStorage.getItem('token')` alone, which covers the primary scenario (user not logged in). The `isPublicAuthEndpoint` check is defense-in-depth for the re-login edge case.

### 2.5 P33: No-retryFn 401 branch — also apply isPublicAuthEndpoint guard

**Problem P33** (api.js:209-220): The no-retryFn 401 branch unconditionally does `localStorage.removeItem('token')` + `app.showLogin()`. If a logged-in user calls a public auth endpoint that returns 401 without retryFn (e.g., direct fetch to `/auth/refresh`), the existing valid token is cleared and the user is kicked to login.

**Fix**: Apply the same `isPublicAuthEndpoint` check to the no-retryFn 401 branch:

```js
if (r.status === 401) {
  // No retryFn — check if this is a public auth endpoint first
  return _safeErrJson(r).then(err => {
    if (err?.detail?.action === 'reauth_required') {
      _redirectToGitcodeAuth();
      throw new Error(err.detail.message);
    }
    // P33 fix: don't clear token for public auth endpoint 401s
    var reqUrl = r.url || '';
    var isPublicAuthEndpoint = /\/api\/auth\/(login|refresh|exchange)$/.test(reqUrl)
      || /\/api\/auth\/login\/[^/]+\/associate$/.test(reqUrl);
    if (isPublicAuthEndpoint) {
      // P37 fix: set error_code and status on the thrown Error so upstream
      // catch can do i18n mapping via _loginErrMsg(e) / errMsg(e).
      var detail33 = (err && err.detail) || {};
      var e33 = new Error(detail33.message || I18N.t('common.request_failed', {status: 401}));
      e33.status = 401;
      e33.error_code = detail33.error_code || '';
      throw e33;
    }
    // Session 401 without retryFn — clear token + show login (existing behavior)
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
    throw new Error(err?.detail?.message || I18N.t('common.session_expired'));
  });
}
```

**Scenario**:
```
User is logged in, browsing settings page.
A background check calls fetch('/api/auth/refresh') directly (no retryFn).
refresh endpoint returns 401 (refresh_token cookie expired).
Without P33 fix: token cleared → user kicked to login → form data lost.
With P33 fix: isPublicAuthEndpoint matches /api/auth/refresh → shows backend message
  → user stays on page, existing token preserved.
```

### 2.6 Verification matrix

| Scenario | `localStorage.token` | Expected behavior |
|----------|---------------------|-------------------|
| Not logged in + wrong password | absent | Show "账号或密码不正确，请重新输入", no refresh |
| Not logged in + correct password | absent | Login succeeds, no refresh triggered |
| Not logged in + account disabled | absent | Show "该账号已被停用，请联系管理员", no refresh |
| Not logged in + account locked | absent | Show lock message, no refresh |
| Logged in + JWT expired on protected endpoint | present | Refresh → retry original request |
| Logged in + refresh also fails | present | Show "会话过期，请重新登录", redirect to login |
| Logged in + re-login with wrong password | present | `isPublicAuthEndpoint` guard fires — show backend message, no refresh loop |
| Logged in + public endpoint 401 without retryFn (P33) | present | `isPublicAuthEndpoint` guard fires — show backend message, don't clear token |

---

## 3. R2: Optimize Login Error Prompts + Add `error_code`

### 3.1 Design principles (Apple HIG)

| Principle | Source | Current violation |
|-----------|--------|-------------------|
| State what happened and what the user can do | Apple HIG Alerts | Missing action guidance |
| Never blame the user — attribute to system | Apple HIG Alerts | "错误" tone is accusatory |
| Never expose internal technical details | Apple HIG | Exposes IP address, failure count |
| Every error state has an exit / recovery action | Apple HIG | "账号已被禁用" is a dead end |
| Copy voice: short, confident, no redundant labels | Apple HIG | "登录失败:" prefix is redundant |

### 3.2 Prompt optimization table

#### R2-1: Credential incorrect

| Item | Current | New |
|------|---------|-----|
| zh | `账号或密码错误` | `账号或密码不正确，请重新输入` |
| en | (hardcoded zh only) | `The account or password is incorrect. Please try again.` |
| Location | `grape_auth.py:410`, `grape_auth.py:2035` | same |
| error_code | (none) | `CREDENTIAL_INCORRECT` |
| Principle | Neutral phrasing ("不正确" vs "错误") + action guidance ("请重新输入") |

#### R2-2: Account deactivated

| Item | Current | New |
|------|---------|-----|
| zh | `账号已被禁用` | `该账号已被停用，请联系管理员` |
| en | (hardcoded zh only) | `This account has been deactivated. Please contact the administrator.` |
| Location | `grape_auth.py:420`, `grape_auth.py:2042` | same |
| error_code | (none) | `ACCOUNT_DEACTIVATED` |
| Principle | Provides an exit ("请联系管理员"); "停用" is more neutral than "禁用" |

#### R2-3: Account deactivating (cooldown)

| Item | Current | New |
|------|---------|-----|
| zh | `账号已注销，处于冷静期` | `该账号正在注销中，暂时无法登录` |
| en | (hardcoded zh only) | `This account is being deactivated and cannot be accessed at this time.` |
| Location | `grape_auth.py:419`, `grape_auth.py:2044`, `auth.py:59` | same |
| error_code | (none) | `ACCOUNT_DEACTIVATING` |
| Principle | User-understandable language; hides internal "cooldown" terminology |

> **P30 fix**: `grape_auth.py:2044` (associate endpoint) currently returns `"账号已注销"` (missing "处于冷静期"). This is inconsistent with L419's `"账号已注销，处于冷静期"`. Both must be unified to the new message `"该账号正在注销中，暂时无法登录"`.
>
> **Scenario**: User registers via GitCode OAuth, then tries to associate an existing Grape account that is in the 7-day deactivation cooldown period (status=3). The associate endpoint returns 403 with `"账号已注销"` — user thinks the account is permanently deleted. With the fix, both login and associate endpoints return the same `"该账号正在注销中，暂时无法登录"`, informing the user it's temporary.

#### R2-4: Account locked

| Item | Current | New |
|------|---------|-----|
| zh (email configured) | `连续登录失败 N 次，账号已临时锁定 N 分钟。可通过邮箱验证码立即解锁。` | `出于安全保护，账号已临时锁定。请 {minutes} 分钟后重试，或通过邮箱验证码立即解锁。` |
| zh (email NOT configured) | (same — no distinction) | `出于安全保护，账号已临时锁定。请 {minutes} 分钟后重试。` |
| en (email configured) | (hardcoded zh only) | `For security reasons, this account has been temporarily locked. Please try again in {minutes} minutes, or use email verification to unlock immediately.` |
| en (email NOT configured) | (same — no distinction) | `For security reasons, this account has been temporarily locked. Please try again in {minutes} minutes.` |
| Location | `login_throttle.py:122–124` | same |
| error_code | (none) | `ACCOUNT_LOCKED` |
| Principle | Attributes to "security protection" instead of scolding user with failure count; preserves unlock path |

> **P41 fix**: The original design used a single message that always mentions "email verification unlock". If email service is not configured (`is_email_configured()` returns False), the user sees the unlock hint but can never receive a verification code — creating a dead end. The fix:
> 1. `check_throttle` calls `is_email_configured()` and returns `"email_configured": True/False` in the throttle dict.
> 2. `grape_auth.py` passes `email_configured` through in the 429 response detail.
> 3. The message is dynamically generated based on `email_configured`:
>    - Configured: includes "或通过邮箱验证码立即解锁"
>    - Not configured: only "请 {minutes} 分钟后重试"
> 4. i18n keys split: `app.login.account_locked` (with unlock hint) + `app.login.account_locked_no_email` (without unlock hint).
> 5. Frontend `_loginErrMsg` checks `e.detail.email_configured` (or `e.email_configured`) to pick the right key.
>
> **Scenario**: Dev environment without SMTP/Resend. User fails login 5 times → locked. Without P41: user sees "通过邮箱验证码立即解锁" → tries to send code → backend returns fake success (防枚举) → user waits forever for email that never arrives. With P41: user sees "请 15 分钟后重试" → no dead-end unlock hint → user waits and retries.

> **P43 fix**: The original design used `ACCOUNT_LOCK_DURATION // 60` (fixed 15 minutes) in the lock message. This is wrong — it should use `remaining // 60 + 1` (dynamic remaining time), matching the IP rate limit message which already uses `remaining // 60 + 1`.
>
> ```python
> # Before (P43 bug):
> lock_minutes = ACCOUNT_LOCK_DURATION // 60  # always 15, regardless of elapsed time
>
> # After (P43 fix):
> lock_minutes = remaining // 60 + 1  # dynamic: 15 at lock start, 5 after 10 minutes
> ```
>
> **Scenario**: User locked at 09:00 until 09:15. At 09:10, user retries login. Before P43: message says "请 15 分钟后重试" → user thinks they must wait until 09:25. After P43: message says "请 6 分钟后重试" → user knows they only need to wait until ~09:16. Consistent with IP rate limit behavior.

> **P44 fix**: `unlock/send-code` endpoint (grape_auth.py:907) was missing `is_email_configured()` check. `forgot-password/send-code` (L458) already has this check and returns 503. Without the check, `send_code()` falls through to LOG_ONLY mode in dev environments, returning success but not actually sending email.
>
> ```python
> # P44 fix: add at the start of unlock_send_code
> from src.auth.email_service import is_email_configured
> if not is_email_configured():
>     raise HTTPException(status_code=503, detail={
>         "code": 503, "error_code": "EMAIL_NOT_CONFIGURED",
>         "message": "Email service not configured, please contact administrator"
>     })
> ```
>
> **Scenario**: Dev environment without email config. User calls POST /api/auth/unlock/send-code directly (via curl or dev tools). Before P44: returns 200 {"message": "验证码已发送"} but no email sent (LOG_ONLY). User waits forever. After P44: returns 503 with EMAIL_NOT_CONFIGURED. Behavior now consistent with forgot-password/send-code.

#### R2-5: IP rate limited

| Item | Current | New |
|------|---------|-----|
| zh | `您的 IP 请求过于频繁，请 N 分钟后再试` | `操作过于频繁，请 {minutes} 分钟后再试` |
| en | (hardcoded zh only) | `Too many attempts. Please try again in {minutes} minutes.` |
| Location | `login_throttle.py:107` | same |
| error_code | (none) | `TOO_MANY_ATTEMPTS` |
| Principle | Hides technical term "IP"; "操作过于频繁" is user-understandable |

### 3.3 Backend `error_code` field additions

Each modified `HTTPException` gains an `error_code` field in the `detail` dict:

#### `grape_auth.py`

```python
# Line 410 — login credential error
raise HTTPException(status_code=401, detail={
    "code": 401,
    "error_code": "CREDENTIAL_INCORRECT",
    "message": "账号或密码不正确，请重新输入"
})

# Line 419 — account deactivating (cooldown)
raise HTTPException(status_code=403, detail={
    "code": 403,
    "error_code": "ACCOUNT_DEACTIVATING",
    "message": "该账号正在注销中，暂时无法登录"
})

# Line 420 — account deactivated
raise HTTPException(status_code=403, detail={
    "code": 403,
    "error_code": "ACCOUNT_DEACTIVATED",
    "message": "该账号已被停用，请联系管理员"
})

# Line 2035 — associate credential error
raise HTTPException(status_code=401, detail={
    "code": 401,
    "error_code": "CREDENTIAL_INCORRECT",
    "message": "账号或密码不正确，请重新输入"
})

# Line 2042 — associate account deactivated
raise HTTPException(status_code=403, detail={
    "code": 403,
    "error_code": "ACCOUNT_DEACTIVATED",
    "message": "该账号已被停用，请联系管理员"
})

# Line 2044 — associate account deactivating
raise HTTPException(status_code=403, detail={
    "code": 403,
    "error_code": "ACCOUNT_DEACTIVATING",
    "message": "该账号正在注销中，暂时无法登录"
})
```

#### `login_throttle.py`

```python
# Line 107 — IP rate limited
return {
    "allowed": False,
    "ip_locked": True,
    "error_code": "TOO_MANY_ATTEMPTS",
    "message": f"操作过于频繁，请 {remaining // 60 + 1} 分钟后再试",
    "retry_after": remaining,
}

# Lines 122–124 — account locked
return {
    "allowed": False,
    "locked": True,
    "error_code": "ACCOUNT_LOCKED",
    "message": (
        f"出于安全保护，账号已临时锁定。请 {ACCOUNT_LOCK_DURATION // 60} 分钟后重试，"
        "或通过邮箱验证码立即解锁。"
    ),
    "retry_after": remaining,
}
```

> **Note**: `login_throttle.check_throttle` returns a dict, not an `HTTPException`. The caller in `grape_auth.py:388–401` wraps the message into an `HTTPException`. The `error_code` must be propagated:

```python
# grape_auth.py:399–401 — propagate error_code from throttle
if throttle.get("captcha"):
    raise HTTPException(status_code=429, detail={
        "code": 429,
        "error_code": throttle.get("error_code", ""),
        "message": throttle["message"],
        "captcha": True
    })
raise HTTPException(status_code=429, detail={
    "code": 429,
    "error_code": throttle.get("error_code", ""),
    "message": throttle["message"]
})
```

#### `auth.py` (refresh endpoint)

```python
# Line 39 — missing refresh_token
raise HTTPException(status_code=401, detail={
    "code": 401,
    "error_code": "MISSING_REFRESH_TOKEN",
    "message": "缺少 refresh_token"
})

# Line 43 — refresh_token invalid/expired
raise HTTPException(status_code=401, detail={
    "code": 401,
    "error_code": "REFRESH_TOKEN_INVALID",
    "message": "refresh_token 过期或无效"
})

# Line 48 — user not found
raise HTTPException(status_code=401, detail={
    "code": 401,
    "error_code": "USER_NOT_FOUND",
    "message": "用户不存在"
})

# Line 54 — session revoked (token_version mismatch)
raise HTTPException(status_code=401, detail={
    "code": 401,
    "error_code": "SESSION_REVOKED",
    "message": "会话已失效"
})

# Line 59 — account deactivating (in refresh)
raise HTTPException(status_code=401, detail={
    "code": 401,
    "error_code": "ACCOUNT_DEACTIVATING",
    "message": "该账号正在注销中，暂时无法登录"
})

# Line 60 — account deactivated (in refresh)
raise HTTPException(status_code=401, detail={
    "code": 401,
    "error_code": "ACCOUNT_DEACTIVATED",
    "message": "该账号已被停用，请联系管理员"
})
```

#### `deps.py` (session middleware 401s)

Add `error_code` to all 401 responses in `get_current_user`, `get_current_user_or_api_key`, and `require_admin`:

| Line | Current message | error_code (new) |
|------|----------------|-----------------|
| 53 | `Not logged in` | `NOT_LOGGED_IN` |
| 59 | `Session expired` | `SESSION_EXPIRED` |
| 64 | `User not found` | `USER_NOT_FOUND` |
| 70 | `Session revoked` | `SESSION_REVOKED` |
| 74 | `账号已被禁用` | `ACCOUNT_DISABLED` |
| 80 | `Session invalid` | `SESSION_INVALID` |
| 147 | `Not logged in` | `NOT_LOGGED_IN` |
| 152 | `Invalid or expired session token` | `SESSION_TOKEN_INVALID` |
| 167 | `Invalid or revoked API Key` | `API_KEY_INVALID` |
| 173 | `账号已被禁用` | `ACCOUNT_DISABLED` |
| 213 | `Not logged in` | `NOT_LOGGED_IN` |
| 218 | `Session expired` | `SESSION_EXPIRED` |
| 225 | `User not found` | `USER_NOT_FOUND` |
| 229 | `Session revoked` | `SESSION_REVOKED` |

Example diff for `deps.py:53`:

```python
# Before
raise HTTPException(status_code=401, detail={"code": 401, "message": "Not logged in"})

# After
raise HTTPException(status_code=401, detail={
    "code": 401,
    "error_code": "NOT_LOGGED_IN",
    "message": "Not logged in"
})
```

---

## 4. R3: Remove "登录失败:" Prefix in `app.js`

### 4.1 Current code (`app.js:2166–2178`)

```js
}).catch(e => {
  _setLoginBtnLoading(btn, false);
  var msg = e.message || 'Failed';
  // 限流反馈
  if (msg.includes('锁定') || msg.includes('lock') || msg.includes('IP')) {
    showToast(msg);
  } else if (msg.includes('验证码') || msg.includes('captcha') || msg.includes('人机')) {
    showToast(msg);
  } else if (msg.includes('白名单')) {
    _showWhitelistGuidance('gitcode', u);
  } else {
    showToast(I18N.t('app.login.failed') + ': ' + errMsg(e));   // ← line 2177
  }
});
```

### 4.2 Change

Replace line 2177 — remove the `I18N.t('app.login.failed') + ': '` prefix:

```js
  } else {
    showToast(_loginErrMsg(e));   // R3+R4: error_code i18n mapping, no prefix
  }
```

Where `_loginErrMsg` is the new function from R4 (section 5.5). The other branches (lock/captcha/whitelist) remain unchanged — they already display the backend message directly.

### 4.3 Rationale

Per Apple HIG "Copy voice: short, confident, no redundant labels" — the user clicked "Login" and saw a toast; they already know the login failed. "登录失败:" is redundant information that adds visual noise.

---

## 5. R4: i18n Language Pack Sync + error_code Mapping

### 5.1 Approach: Plan A (backend returns `error_code`, frontend maps to i18n)

The backend continues returning Chinese `message` (for console logs and fallback), but adds an `error_code` field. The frontend reads `error_code` and maps it to the appropriate i18n key via `I18N.t()`. If `error_code` is absent or unrecognized, it falls back to the raw `message`.

This approach is chosen because:
- The admin frontend already uses this pattern (`admin.json` has `INVALID_CREDENTIALS`, `SESSION_EXPIRED`, etc.)
- No backend language negotiation needed (avoids `Accept-Language` parsing complexity)
- The raw `message` remains available as a fallback and for debugging

### 5.2 New i18n keys — `zh/app.json`

Add inside the `"login"` object (after `"failed": "登录失败"`):

```json
"credential_incorrect": "账号或密码不正确，请重新输入",
"account_deactivated": "该账号已被停用，请联系管理员",
"account_deactivating": "该账号正在注销中，暂时无法登录",
"account_locked": "出于安全保护，账号已临时锁定。请 {minutes} 分钟后重试，或通过邮箱验证码立即解锁。",
"too_many_attempts": "操作过于频繁，请 {minutes} 分钟后再试"
```

### 5.3 New i18n keys — `en/app.json`

```json
"credential_incorrect": "The account or password is incorrect. Please try again.",
"account_deactivated": "This account has been deactivated. Please contact the administrator.",
"account_deactivating": "This account is being deactivated and cannot be accessed at this time.",
"account_locked": "For security reasons, this account has been temporarily locked. Please try again in {minutes} minutes, or use email verification to unlock immediately.",
"too_many_attempts": "Too many attempts. Please try again in {minutes} minutes."
```

### 5.4 Session 401 error_code mapping

For session-expiry 401s (from `deps.py` / `auth.py`), the `error_code` is used when refresh fails and the user is shown the session-expired message. These are added to `app.json` under the `"error"` section:

> **P40 fix**: The original design listed only 8 keys. Implementation audit found `deps.py:get_current_user_or_api_key` has 2 additional 401s (SESSION_TOKEN_INVALID, API_KEY_INVALID). All 10 keys must be added to zh/en `app.json` error section. Without these keys, `errMsg(e)` falls back to raw backend message (Chinese in English locale).

| error_code | zh message | en message | i18n key |
|------------|-----------|-----------|----------|
| `NOT_LOGGED_IN` | 请先登录 | Please sign in first. | `app.error.NOT_LOGGED_IN` |
| `SESSION_EXPIRED` | 会话过期，请重新登录 | Your session has expired. Please sign in again. | `app.error.SESSION_EXPIRED` |
| `USER_NOT_FOUND` | 用户不存在 | User not found. | `app.error.USER_NOT_FOUND` |
| `SESSION_REVOKED` | 您的账号在其他设备上修改了密码，请重新登录 | Your password was changed on another device. Please sign in again. | `app.error.SESSION_REVOKED` |
| `SESSION_INVALID` | 会话无效，请重新登录 | Session invalid. Please sign in again. | `app.error.SESSION_INVALID` |
| `ACCOUNT_DISABLED` | 该账号已被停用，请联系管理员 | This account has been deactivated. Please contact the administrator. | `app.error.ACCOUNT_DISABLED` |
| `MISSING_REFRESH_TOKEN` | 会话已过期，请重新登录 | Your session has expired. Please sign in again. | `app.error.MISSING_REFRESH_TOKEN` |
| `REFRESH_TOKEN_INVALID` | 会话已过期，请重新登录 | Your session has expired. Please sign in again. | `app.error.REFRESH_TOKEN_INVALID` |
| `SESSION_TOKEN_INVALID` | 会话令牌无效或已过期 | Invalid or expired session token. | `app.error.SESSION_TOKEN_INVALID` |
| `API_KEY_INVALID` | API Key 无效或已撤销 | Invalid or revoked API Key. | `app.error.API_KEY_INVALID` |

> **Note on `SESSION_REVOKED`**: This maps to a more specific message ("password changed on another device") rather than generic "session expired", helping the user understand why they were logged out.

### 5.5 Frontend error_code rendering logic (`app.js`)

Add a helper function `_loginErrMsg(e)` that maps `error_code` to i18n keys, with fallback to the backend `message`:

```js
/**
 * R4: Map error_code to localized login error message.
 * Falls back to backend message if error_code is absent or unrecognized.
 * @param {Error} e - Error object from API catch
 * @returns {string} Localized error message
 */
function _loginErrMsg(e) {
  var code = e.error_code || '';
  var key = {
    'CREDENTIAL_INCORRECT': 'app.login.credential_incorrect',
    'ACCOUNT_DEACTIVATED': 'app.login.account_deactivated',
    'ACCOUNT_DEACTIVATING': 'app.login.account_deactivating',
    'ACCOUNT_LOCKED': 'app.login.account_locked',
    'TOO_MANY_ATTEMPTS': 'app.login.too_many_attempts',
  }[code];
  if (key) {
    // For messages with {{minutes}} placeholder, extract minutes from message if available
    var msg = I18N.t(key);
    if (msg.indexOf('{{minutes}}') >= 0) {
      var minutes = _extractMinutes(e.message || '');
      msg = msg.replace('{{minutes}}', minutes);
    }
    return msg;
  }
  return errMsg(e);  // fallback: use backend message
}

/**
 * Extract minutes number from backend message string.
 * Backend sends messages like "请 15 分钟后再试" — extract the integer.
 */
function _extractMinutes(msg) {
  var m = msg.match(/(\d+)\s*分钟/);
  return m ? m[1] : '15';  // default fallback
}
```

### 5.6 Update login catch block (`app.js:2166–2178`)

The full updated catch block integrates R3 + R4:

```js
}).catch(e => {
  _setLoginBtnLoading(btn, false);
  var msg = e.message || 'Failed';
  var code = e.error_code || '';
  // 限流反馈 — use error_code or message keyword matching (backward compat)
  if (code === 'ACCOUNT_LOCKED' || msg.includes('锁定') || msg.includes('lock')) {
    showToast(_loginErrMsg(e));
  } else if (code === 'TOO_MANY_ATTEMPTS' || msg.includes('IP') || msg.includes('频繁')) {
    showToast(_loginErrMsg(e));
  } else if (msg.includes('验证码') || msg.includes('captcha') || msg.includes('人机')) {
    showToast(msg);
  } else if (msg.includes('白名单')) {
    _showWhitelistGuidance('gitcode', u);
  } else {
    showToast(_loginErrMsg(e));   // R3: no "登录失败:" prefix; R4: error_code i18n
  }
});
```

### 5.7 `register.html` — associate error handling

The `associateAccount()` function (`register.html:445–450`) currently matches Chinese keywords to decide whether to show a field error or a toast:

```js
// Current (line 448)
if(msg.indexOf('密码')>=0||msg.indexOf('账号')>=0||msg.indexOf('用户')>=0||msg.indexOf('password')>=0)
  Auth.showFieldError('assocIdentifier',msg);
else Auth.showToast(msg);
```

Change to use `error_code`:

```js
// R4: use error_code instead of string matching
var code = err.error_code || '';
if (code === 'CREDENTIAL_INCORRECT' || code === 'ACCOUNT_DEACTIVATED' || code === 'ACCOUNT_DEACTIVATING') {
  Auth.showFieldError('assocIdentifier', _loginErrMsg(err));
} else {
  Auth.showToast(err.message || Auth.t('app.login.toast_associate_failed'));
}
```

> **Note**: `register.html` uses `Auth.t()` (not `I18N.t()`) for i18n. The `_loginErrMsg` helper should be adapted to use whichever i18n accessor is in scope. In `register.html`, use `Auth.t()` instead of `I18N.t()`.

### 5.8 Admin language packs (`zh/admin.json`, `en/admin.json`)

The admin frontend already has an `error` section with `error_code` → message mappings. The following keys already exist and need **no changes**:

- `INVALID_CREDENTIALS` — already present in both zh and en
- `MISSING_REFRESH_TOKEN` — already present
- `REFRESH_TOKEN_INVALID` — already present
- `SESSION_EXPIRED` — already present
- `ACCOUNT_NOT_FOUND` — already present

No new keys are needed for admin.json. The admin login error handling (`admin.js`) already uses `error_code` mapping.

---

## 6. Dependency Graph

```
R2 (error_code in backend responses)
  │
  ├──▶ R4 (frontend error_code → i18n mapping depends on error_code existing)
  │      │
  │      └──▶ R3 (app.js catch block rewrite uses _loginErrMsg from R4)
  │
  └──▶ R1 (handleResp 401 fix throws error with error_code, consumed by R4)
         │
         └──▶ R3 (login catch receives error with error_code, needs R1 fix to get backend message)
```

### 6.1 Implementation order

1. **R2 first** — Add `error_code` fields to all backend 401/403/429 responses. This is the foundation — the frontend cannot map what doesn't exist.

2. **R1 second** — Fix `handleResp` 401 branch. After R2, the thrown error will carry `error_code`, which R4 needs. R1 must be done before R3 because R3's catch block expects the backend message (not "会话过期") to reach it.

3. **R4 third** — Add i18n keys to language packs + implement `_loginErrMsg` mapping function + update `register.html`. R4 depends on R2's `error_code` fields.

4. **R3 last** — Update the `app.js` login catch block to use `_loginErrMsg` and remove the prefix. R3 is the smallest change but depends on R1 (correct error reaches catch) and R4 (mapping function exists).

### 6.2 Testing order

| Step | Test | Validates |
|------|------|-----------|
| 1 | Backend unit test: login with wrong password returns `error_code: "CREDENTIAL_INCORRECT"` | R2 |
| 2 | Backend unit test: throttle returns `error_code: "ACCOUNT_LOCKED"` / `TOO_MANY_ATTEMPTS` | R2 |
| 3 | Backend unit test: refresh endpoint returns `error_code` for each failure case | R2 |
| 4 | E2E: not-logged-in user enters wrong password → sees "账号或密码不正确，请重新输入" (zh) | R1 + R2 + R3 + R4 |
| 5 | E2E: not-logged-in user enters wrong password in EN locale → sees English message | R4 |
| 6 | E2E: account locked → sees lock message with correct minutes | R2 + R4 |
| 7 | E2E: logged-in user, JWT expires → refresh triggers, request retried | R1 regression |
| 8 | E2E: logged-in user, refresh fails → sees "会话过期，请重新登录", redirect to login | R1 regression |
| 9 | E2E: associate flow with wrong password → field error shows localized message | R4 |

---

## 7. Files Changed Summary

| File | Changes | Req |
|------|---------|-----|
| `src/api/static/js/api.js` | `handleResp` 401 branch: skip refresh when no token / public auth endpoint | R1 |
| `src/api/static/js/app.js` | Login catch: remove "登录失败:" prefix, use `_loginErrMsg(e)` with error_code mapping | R3, R4 |
| `src/api/routes/grape_auth.py` | 5 prompt rewrites + `error_code` fields (lines 410, 419, 420, 2035, 2042, 2044); propagate `error_code` from throttle (lines 399–401) | R2, R4 |
| `src/api/routes/auth.py` | 6 prompt rewrites + `error_code` fields (lines 39, 43, 48, 54, 59, 60) | R2, R4 |
| `src/auth/login_throttle.py` | 2 prompt rewrites + `error_code` in throttle return dict (lines 107, 122–124) | R2, R4 |
| `src/api/deps.py` | Add `error_code` to all 401 responses in `get_current_user`, `get_current_user_or_api_key`, and `require_admin` (lines 53, 59, 64, 70, 74, 80, 147, 152, 167, 173, 213, 218, 225, 229) | R4 |
| `src/api/static/locales/zh/app.json` | Add 5 login error keys + 8 session error keys | R4 |
| `src/api/static/locales/en/app.json` | Add 5 login error keys + 8 session error keys | R4 |
| `src/api/static/locales/zh/admin.json` | No changes needed (existing keys sufficient) | — |
| `src/api/static/locales/en/admin.json` | No changes needed (existing keys sufficient) | — |
| `src/api/static/register.html` | `associateAccount()` catch: use `error_code` instead of string matching (line 448) | R4 |

---

## 8. Backward Compatibility

- **No token in localStorage**: R1 fix only activates when `!localStorage.getItem('token')` or `isPublicAuthEndpoint`. Existing logged-in 401 → refresh → retry flow is unchanged.
- **Missing `error_code`**: `_loginErrMsg` falls back to `errMsg(e)` (the backend `message`). Old backend responses without `error_code` still display correctly.
- **Language packs**: New keys are additive — no existing keys are removed or renamed. `app.login.failed` ("登录失败" / "Login failed") is retained in language packs (may be referenced elsewhere).
- **String keyword matching in app.js**: The lock/captcha/whitelist branches retain `msg.includes(...)` checks alongside the new `error_code` checks for backward compatibility with any backend that hasn't been updated yet.
- **Admin frontend**: No changes — admin already uses `error_code` mapping via `admin.json`.

---

## 9. Open Questions

1. **`r.url` reliability**: The `isPublicAuthEndpoint` guard in R1 relies on `r.url` (the Response object's URL). If this is empty or unreliable in certain browsers, the fallback is `!localStorage.getItem('token')` alone. Need to verify `r.url` is populated for fetch responses in all target browsers (Chrome 90+, Firefox 88+, Safari 14+).

2. **`{{minutes}}` placeholder interpolation**: The i18n system (`I18N.t()`) uses `{{label}}` double-brace syntax (e.g., `bind_success: "{{label}} 绑定成功"`). The implementation uses `{{minutes}}` in both zh/en app.json (e.g., `"请 {{minutes}} 分钟后重试"`). The `_loginErrMsg` helper checks `msg.indexOf('{{minutes}}')` and replaces accordingly. **Resolved**: double-brace `{{minutes}}` is the correct syntax, aligned with the existing i18n system.

3. **Throttle `error_code` propagation for captcha case**: When `throttle.get("captcha")` is true, the response includes `"captcha": True` in the detail. The `error_code` in this case would be `ACCOUNT_LOCKED` (same as non-captcha lock). Need to verify the captcha flow still works with the additional `error_code` field.
