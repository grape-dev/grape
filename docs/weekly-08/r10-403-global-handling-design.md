# R10: 403 Global Unified Handling — Design Document

> **Status**: Design proposal, zero implementation.
> **Requirements**: See `login-error-prompt-optimization-requirements.md` §7.3 P19.
> **Branch**: `feat/login-error-prompt-optimization` based on `weekly-08`.

---

## 1. Problem Statement

### 1.1 Current State

`handleResp` in `api.js` has no 403-specific branch. All non-401 non-ok responses fall through to the generic handler (api.js:224-231):

```js
return _safeErrJson(r).then(err => {
  const detail = (err && err.detail) || {};
  const e = new Error(detail.message || I18N.t('common.request_failed', {status: r.status}));
  e.status = r.status;
  e.error_code = detail.error_code || '';
  throw e;
});
```

The error object carries `e.status = 403` and `e.error_code` (if backend provides one), but **no global handling** — each page catch block must independently check `e.status === 403`.

### 1.2 Pages That Handle 403

| Page | File:Line | Behavior |
|------|-----------|----------|
| Dashboard | app.js:1449 | Auto-switch to next accessible repo |
| Event navigation | events.js:37 | Toast "forbidden" + stop |

### 1.3 Pages That Don't Handle 403 (~15 pages)

These pages show raw `e.message` (backend Chinese text) or generic "load failed":

| Page | File | Catch Pattern |
|------|------|--------------|
| observability | observability.js | `.catch(e => null)` — silent swallow |
| autopilot | autopilot.js:129 | `load_failed + e.message` in error div |
| skills | skills.js:203 | `load_failed + e.message` in error div |
| knowledge | knowledge.js:153 | Silent (only checks AbortError) |
| dashboard scene detail | dashboard.js:543 | `load_failed + e.message` in error div |
| dashboard save | dashboard.js:1278 | `save_failed + e.detail.message` toast |
| organizations | organizations.js:38 | `load_failed + e.message` in red div |
| repos sync | repos.js:488 | `sync_failed + e.message` toast |
| repos unhost | repos.js:656 | `unhost.failed + e.message` toast |
| im_channel | im_channel.js:49 | `load_failed` in red div (no detail) |
| my_feedback | my_feedback.js:28 | `load_failed + err.message` |
| my_feedback_detail | my_feedback_detail.js:38 | `load_failed + err.message` |
| chat onboard | chat.js:1006 | Generic error (only 409 handled) |
| scene-market | scene-market.js:657 | Generic (only 409 handled) |
| settings update | settings.js:61 | `update_failed` toast (no detail) |
| settings revoke | settings.js:307 | `revoke_failed + e.message` toast |
| account bind | account.js:909 | Switch on status code, 403 falls to default |
| account transfer | account.js:961 | Only 410/400 handled, 403 falls to generic |
| agents unbind | agents.js:450 | `unbind_failed + e.message` toast |
| pipeline trigger | pipeline.js:375 | `alert()` with error message |

### 1.4 Backend 403 Error Codes

Most backend 403 responses have **no error_code** — only `message`:

| Source | File:Line | Has error_code? | Message |
|--------|-----------|----------------|---------|
| admin CSRF | admin/routes.py:108 | MISSING_CSRF_HEADER | Missing CSRF header |
| repo manage | repos.py:1161 | PERMISSION_DENIED | 无权限托管 |
| repo manage | repos.py:1177 | TOKEN_PERMISSION_INSUFFICIENT | Token 权限不足 |
| scene fork | scenes.py:986 | No | 需要维护者权限 |
| scene edit | scenes.py:1142 | No | 官方场景不可编辑 |
| scene delete | scenes.py:1204 | No | 官方场景不可删除 |
| knowledge | knowledge.py:67 | No | 无权访问此仓库的知识库 |
| knowledge | knowledge.py:338 | No | 仅创建者可管理关联 |
| knowledge | knowledge.py:624 | No | 系统生成的知识不可编辑 |
| runtime | settings.py:1346 | No | 仅仓库级/个人级 worker |
| credential | settings.py:1537 | No | 仅凭据 owner 可修改 |
| git api | git_repo_api.py:77 | No | Git provider 未接入 |
| git api | git_repo_api.py:94 | No | 会话级临时 token 专用 |
| a2a | webhook.py:817 | No | agent disabled |
| issues | issues_center.py:30 | No | 需会话 token |

### 1.5 Existing i18n Keys

| Key | zh | en | Location |
|-----|----|----|----------|
| common.permission_denied | 无权限 | Permission denied | common.json:96 |
| app.error.PERMISSION_DENIED | 权限不足，您没有该仓库的访问权限。 | (same structure) | app.json:630 |
| app.error.WEBHOOK_PERMISSION_DENIED | 需要仓库 admin 权限创建 webhook... | (same) | app.json:637 |
| events.nav_forbidden | 无权限访问事件 #{{seq}}... | (same) | events.json:102 |

---

## 2. Design

### 2.1 Strategy: handleResp 403 Branch + error_code + Fallback

Add a 403-specific branch in `handleResp` that sets a default `error_code` if backend doesn't provide one:

```js
// In handleResp, before the generic non-ok handler (api.js:224):
if (r.status === 403) {
  return _safeErrJson(r).then(err => {
    const detail = (err && err.detail) || {};
    const e = new Error(detail.message || I18N.t('common.permission_denied'));
    e.status = 403;
    e.error_code = detail.error_code || 'FORBIDDEN';
    e.errors = detail.errors || null;
    e.detail = detail;
    throw e;
  });
}
```

**Key decision**: The 403 branch does NOT redirect or auto-switch repos — that's page-specific behavior. It only ensures:
1. `e.error_code` is always set (backend-provided or fallback `FORBIDDEN`)
2. Default message is `common.permission_denied` (not raw backend text)
3. `e.status = 403` is preserved for pages that check it

### 2.2 Frontend errMsg() Enhancement

`app.js:2400` `errMsg()` already maps `app.error.{error_code}` via i18n. With the 403 branch setting `error_code = 'FORBIDDEN'`, we need a new i18n key:

```json
// zh/app.json → error section:
"FORBIDDEN": "权限不足，无法执行此操作。"

// en/app.json → error section:
"FORBIDDEN": "Permission denied. You do not have access to this resource."
```

When backend provides a specific `error_code` (e.g., `PERMISSION_DENIED`, `TOKEN_PERMISSION_INSUFFICIENT`), `errMsg()` will use that specific key. When no specific code, it falls back to `FORBIDDEN`.

### 2.3 Backend error_code Standardization

Add `error_code` to all 403 responses that currently lack it:

| Source | Current | Add error_code |
|--------|---------|---------------|
| scenes.py:986 | No | `SCENE_MAINTAINER_REQUIRED` |
| scenes.py:1142 | No | `OFFICIAL_SCENE_READONLY` |
| scenes.py:1204 | No | `OFFICIAL_SCENE_READONLY` |
| knowledge.py:67 | No | `KNOWLEDGE_REPO_MISMATCH` |
| knowledge.py:338 | No | `KNOWLEDGE_OWNER_ONLY` |
| knowledge.py:624 | No | `KNOWLEDGE_SYSTEM_READONLY` |
| settings.py:1346 | No | `RUNTIME_SCOPE_RESTRICTED` |
| settings.py:1537 | No | `CREDENTIAL_OWNER_ONLY` |
| git_repo_api.py:77 | No | `GIT_PROVIDER_NOT_SUPPORTED` |
| git_repo_api.py:94 | No | `GIT_TOKEN_TYPE_INVALID` |
| webhook.py:817 | No | `AGENT_DISABLED` |
| issues_center.py:30 | No | `SESSION_TOKEN_REQUIRED` |
| main.py:1105,1128,1197 | No | `FORBIDDEN` |

**Design principle**: Only add error_code, don't change the message text (messages remain in Chinese for backward compatibility — the frontend `errMsg()` will use the error_code to render i18n, falling back to raw message if no mapping).

### 2.4 error_code → i18n Mapping Table

#### New keys to add (zh/en app.json error section):

| error_code | zh | en |
|-----------|----|----|
| FORBIDDEN | 权限不足，无法执行此操作。 | Permission denied. You do not have access to this resource. |
| SCENE_MAINTAINER_REQUIRED | 需要仓库维护者权限才能执行此操作。 | Maintainer permission is required for this action. |
| OFFICIAL_SCENE_READONLY | 官方场景不可修改或删除。 | Official scenes cannot be modified or deleted. |
| KNOWLEDGE_REPO_MISMATCH | 无权访问此仓库的知识库。 | You do not have access to this repository's knowledge base. |
| KNOWLEDGE_OWNER_ONLY | 仅创建者可执行此操作。 | Only the creator can perform this action. |
| KNOWLEDGE_SYSTEM_READONLY | 系统生成的知识不可修改。 | System-generated knowledge cannot be modified. |
| RUNTIME_SCOPE_RESTRICTED | 仅仓库级或个人级 worker 设备支持此操作。 | Only repo-scope or personal-scope worker engines support this action. |
| CREDENTIAL_OWNER_ONLY | 仅凭据所有者可修改配置。 | Only the credential owner can modify this configuration. |
| GIT_PROVIDER_NOT_SUPPORTED | 不支持的 Git 平台。 | Unsupported Git provider. |
| GIT_TOKEN_TYPE_INVALID | 此接口仅支持会话级临时 token。 | This endpoint only accepts session-level temporary tokens. |
| AGENT_DISABLED | 该 Agent 已被禁用。 | This agent has been disabled. |
| SESSION_TOKEN_REQUIRED | 需要会话 token 才能访问此接口。 | A session token is required to access this endpoint. |

#### Existing keys (already in app.json, no change needed):

| error_code | Already exists |
|-----------|----------------|
| PERMISSION_DENIED | app.json:630 |
| WEBHOOK_PERMISSION_DENIED | app.json:637 |
| TOKEN_PERMISSION_INSUFFICIENT | (add new, see below) |
| MISSING_CSRF_HEADER | (add new, see below) |

| error_code | zh | en |
|-----------|----|----|
| TOKEN_PERMISSION_INSUFFICIENT | Token 权限不足，请检查 Token 权限设置。 | Token permissions are insufficient. Please check your token settings. |
| MISSING_CSRF_HEADER | CSRF 验证失败，请刷新页面重试。 | CSRF validation failed. Please refresh the page and retry. |

### 2.5 Pages That Need Special 403 Handling

These pages have **page-specific 403 behavior** that should NOT be replaced by the generic FORBIDDEN message:

| Page | Current 403 Behavior | Keep? |
|------|---------------------|-------|
| app.js:1449 (dashboard) | Auto-switch to next repo | Yes — keep special logic |
| events.js:37 (event nav) | Toast "nav_forbidden" + stop | Yes — keep special logic |

These pages already check `e.status === 403` before calling `errMsg()`, so they won't be affected by the generic FORBIDDEN fallback.

### 2.6 Pages That Benefit Automatically

Pages using `errMsg(e)` in their catch block will automatically benefit — `errMsg()` now returns the i18n-mapped FORBIDDEN message instead of raw backend Chinese text:

| Page | Catch uses errMsg()? | Benefit |
|------|---------------------|---------|
| autopilot.js:129 | `_apt('list.load_failed', {msg: _esc(e.message)})` | No — uses e.message directly. **Needs update to use errMsg()** |
| skills.js:203 | `I18N.t('common.load_failed') + ': ' + (e.message)` | No — uses e.message directly. **Needs update** |
| dashboard.js:543 | `I18N.t('common.load_failed') + ': ' + (e.message)` | No — **Needs update** |
| dashboard.js:1278 | `I18N.t('dashboard.save_failed', {msg: e.detail?.message})` | No — **Needs update** |
| organizations.js:38 | `I18N.t('organizations.load_failed', {msg: e.message})` | No — **Needs update** |
| repos.js:488 | `I18N.t('repos.sync.sync_failed', {msg: e.message})` | No — **Needs update** |
| settings.js:61 | `I18N.t('settings.common.update_failed')` | No detail at all — **Needs update** |
| settings.js:307 | `'Revoke failed: ' + (e.message)` | No — **Needs update** |
| agents.js:450 | `I18N.t('agents.config.unbind_failed', {msg: e.message})` | No — **Needs update** |

**Design decision**: For these pages, replace `e.message` with `errMsg(e)` in the catch block. `errMsg()` will return the i18n-mapped message (FORBIDDEN or specific error_code), falling back to `e.message` if no mapping exists.

**Template**:
```js
// Before:
.catch(e => { el.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + (e.message||'') + '</div>'; })

// After:
.catch(e => {
  if (e.name === 'AbortError' && e.silent) return;
  el.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div>';
})
```

### 2.7 Pages Using String Matching for 403

| Page | Current | Fix |
|------|---------|-----|
| templates.js:837 | `errMsg.indexOf('权限') >= 0 \|\| errMsg.indexOf('403') >= 0` | Replace with `e.status === 403 \|\| e.error_code === 'FORBIDDEN'` |
| runtimes.js:1790 | `msg.indexOf('Unauthorized') >= 0` | This checks for 401, not 403. Leave as-is (covered by R5-R9) |

---

## 3. Changes Summary

### 3.1 Backend (add error_code to 403 responses)

| File | Lines | Change |
|------|-------|--------|
| src/api/routes/scenes.py | 986, 1017, 1142, 1204, 1565 | Add error_code field |
| src/api/routes/knowledge.py | 67, 71, 76, 80, 338, 348, 352, 624, 626, 672, 674 | Add error_code field |
| src/api/routes/settings.py | 1346, 1537 | Add error_code field |
| src/api/git_repo_api.py | 77, 94 | Add error_code field |
| src/api/routes/webhook.py | 817 | Add error_code field |
| src/api/issues_center.py | 30, 33 | Add error_code field |
| src/api/main.py | 1105, 1128, 1197 | Add error_code field |

### 3.2 Frontend api.js

| File | Line | Change |
|------|------|--------|
| src/api/static/js/api.js | ~224 | Add 403 branch before generic non-ok handler |

### 3.3 Frontend pages (replace e.message with errMsg(e))

| File | Lines | Change |
|------|-------|--------|
| src/api/static/js/pages/autopilot.js | 129 | `e.message` → `errMsg(e)` |
| src/api/static/js/pages/skills.js | 203 | `e.message` → `errMsg(e)` |
| src/api/static/js/pages/dashboard.js | 543, 1278 | `e.message` → `errMsg(e)` |
| src/api/static/js/pages/organizations.js | 38 | `e.message` → `errMsg(e)` |
| src/api/static/js/pages/repos.js | 488, 656 | `e.message` → `errMsg(e)` |
| src/api/static/js/pages/settings.js | 61, 307 | Add `errMsg(e)` |
| src/api/static/js/pages/agents.js | 450 | `e.message` → `errMsg(e)` |
| src/api/static/js/pages/templates.js | 837 | String match → `e.status === 403` |

### 3.4 i18n (new keys)

| File | Change |
|------|--------|
| src/api/static/locales/zh/app.json | Add 14 error_code keys |
| src/api/static/locales/en/app.json | Add 14 error_code keys |

---

## 4. Dependency Graph

```
Backend error_code (3.1)
  ↓ provides error_code in 403 responses
api.js 403 branch (3.2)
  ↓ sets e.error_code = detail.error_code || 'FORBIDDEN'
i18n keys (3.4)
  ↓ provides app.error.{error_code} mapping
Page catch updates (3.3)
  ↓ uses errMsg(e) which maps error_code → i18n
User sees friendly message
```

Implementation order:
1. i18n keys (3.4) — no dependencies, safe to add first
2. Backend error_code (3.1) — additive, no breaking change
3. api.js 403 branch (3.2) — depends on i18n keys for fallback
4. Page catch updates (3.3) — depends on api.js 403 branch + errMsg()

---

## 5. Compatibility

| Scenario | Before | After |
|----------|--------|-------|
| Backend 403 with error_code | Frontend may or may not use it | Frontend always uses it via errMsg() |
| Backend 403 without error_code | Frontend shows raw message | Frontend shows FORBIDDEN i18n message |
| Page with special 403 handling (dashboard, events) | Works | Unchanged — checks e.status === 403 before errMsg() |
| Page without 403 handling | Shows raw backend message | Shows i18n-mapped message (if using errMsg()) |
| Page using string matching (templates.js) | Fragile | Uses e.status === 403 (robust) |
| Backend 403 message text | Chinese hardcoded | Unchanged (frontend uses error_code for i18n, message is fallback) |

---

## 6. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Backend error_code typo | Wrong i18n key, shows raw message | errMsg() falls back to e.message if key not found |
| Page forgets to use errMsg() | Still shows raw message | Same as before — no regression |
| Dashboard 403 auto-switch breaks | User stuck on forbidden repo | Dashboard keeps its own e.status === 403 check before errMsg() |
| Too many new i18n keys | Maintenance burden | Only add keys for 403 that actually occur in user-facing flows |
| Backend 403 response format varies | Some use {error: "forbidden"} not {detail: {}} | _safeErrJson handles both — detail falls back to {} |

---

## 7. Verification Checklist

### 7.1 Backend

- [ ] All 403 responses have error_code field
- [ ] error_code values are uppercase SNAKE_CASE
- [ ] Existing error_code (PERMISSION_DENIED, TOKEN_PERMISSION_INSUFFICIENT, MISSING_CSRF_HEADER) unchanged

### 7.2 Frontend api.js

- [ ] handleResp 403 branch sets e.error_code (backend or FORBIDDEN fallback)
- [ ] e.status = 403 preserved
- [ ] e.detail preserved for pages that read it

### 7.3 i18n

- [ ] 14 new keys added to zh/app.json error section
- [ ] 14 new keys added to en/app.json error section
- [ ] errMsg() correctly maps FORBIDDEN → i18n message
- [ ] errMsg() falls back to e.message when key not found

### 7.4 Pages

- [ ] Pages using errMsg(e) show i18n FORBIDDEN message on 403
- [ ] Dashboard 403 auto-switch still works (not affected)
- [ ] Event nav 403 toast still works (not affected)
- [ ] templates.js string matching replaced with e.status === 403
- [ ] No page shows raw backend Chinese on 403 (when using errMsg())

### 7.5 Regression

- [ ] 401 handling unaffected (403 branch is after 401 branch)
- [ ] 404/409/500 handling unaffected (403 branch is before generic handler)
- [ ] AbortError silent handling preserved in page catches
- [ ] Existing app.error.* keys still work (PERMISSION_DENIED, etc.)
