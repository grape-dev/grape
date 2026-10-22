# R5–R9 + R11 Design: API Robustness, Direct Fetch Unification, and Admin Alignment

> **Status**: Design document — pending implementation approval.  
> **Branch**: `feat/login-error-prompt-optimization`  
> **Depends on**: R1 (handleResp 401 credential/session split) must be merged first.  
> **Requirements source**: `docs/weekly-08/login-error-prompt-optimization-requirements.md` §7.3 P2–P28

---

## 1. Scope Summary

| Req | Title | Files | Problems Addressed |
|-----|-------|-------|--------------------|
| R5 | `/auth/exchange` error parsing | `app.js` | P2 |
| R6 | Unify 11 direct `fetch` calls | `events.js`, `issues.js`, `skills.js`, `chat.js` | P3, P4, P5, P6 |
| R7 | Preserve `/en` language prefix on `/login` redirect | `api.js` | P9 |
| R8 | `api.js` robustness (200+HTML, ensureTokenValid retry) | `api.js` | P11, P12 |
| R9 | Upload 401, reauth dedup, knowledge silent, pipeline alert | `api.js`, `knowledge.js`, `pipeline.js` | P16, P17, P20, P21 |
| R11 | Admin change-password 401→400, doRefresh→showLogin | `admin/routes.py`, `api.js` | P25, P28 |

**Out of scope**: R1 (already designed separately), R10 (403 global — evaluation only), R12 (z-index), R13/R14.

---

## 2. R5: `/auth/exchange` Error Handling Optimization

### 2.1 Current Behavior (app.js:2557–2562)

```js
fetch('/api/auth/exchange', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({code: exchangeCode}),
}).then(r => {
  if (!r.ok) throw new Error('exchange failed');
  return r.json();
}).then(d => { /* ... */ })
  .catch(e => {
    showToast(I18N.t('app.login.oauth_exchange_failed') || 'OAuth login failed, please retry', 6000);
    if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
  });
```

**Problem**: When the backend returns 401 (e.g. `exchange_code_invalid`, account disabled) or other error statuses, the response body containing the specific error message is discarded. The user only sees the generic "OAuth login failed, please retry".

### 2.2 Root Cause

The `!r.ok` branch throws a hard-coded `'exchange failed'` without parsing the response body. The backend returns structured JSON:

```json
{"code": 401, "error_code": "EXCHANGE_CODE_INVALID", "message": "exchange_code 无效或已过期"}
```

### 2.3 Fix

Parse the error response body before throwing, so the `catch` handler can display the backend's specific message:

```js
fetch('/api/auth/exchange', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({code: exchangeCode}),
}).then(r => {
  if (!r.ok) {
    // Parse error body to surface backend-specific message
    return r.json().catch(() => null).then(errBody => {
      var detail = (errBody && errBody.detail) || {};
      var msg = detail.message || detail.error_code || (I18N.t('app.login.oauth_exchange_failed') || 'OAuth login failed, please retry');
      var e = new Error(msg);
      e.error_code = detail.error_code || '';
      e.status = r.status;
      throw e;
    });
  }
  return r.json();
}).then(d => {
  localStorage.setItem('token', d.token);
  // ── Preserve ALL existing success handler logic (app.js:2563-2579) ──
  localStorage.setItem('app_cache_ver', '43');
  localStorage.removeItem(REPO_CACHE_KEY);
  localStorage.removeItem('currentRepo');
  localStorage.removeItem('current_org_id');
  sessionStorage.removeItem('currentRepo');
  if (d.first_login) {
    sessionStorage.removeItem('currentRepo');
    $('sidebar').style.display = 'none';
    $('tb').style.display = 'none';
    $('mainView').style.display = 'flex';
    $('mainView').style.position = 'fixed';
    $('mainView').style.inset = '0';
    $('mainView').style.zIndex = '100';
    $('pageTitle').textContent = I18N.t('app.current_project');
  }
  showApp();
  API.scheduleProactiveRefresh();
}).catch(e => {
  // Display the specific backend error message, fallback to generic
  showToast(e.message || (I18N.t('app.login.oauth_exchange_failed') || 'OAuth login failed, please retry'), 6000);
  if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
});
```

### 2.4 Key Decisions

- **No `handleResp`**: `/auth/exchange` is a public credential endpoint (no token in localStorage at this point). Using `API.post` would route through `handleResp`, which (after R1) checks `!localStorage.getItem('token')` to skip refresh — so it would *not* trigger refresh. However, the exchange flow has special URL-cleaning logic (removing `exchange_code` from URL before the fetch) that is simpler to keep as a direct fetch. The fix is only to parse the error body.
- **error_code propagation**: The thrown error carries `error_code` for potential future i18n mapping (R4), but the immediate display uses `detail.message` which the backend already provides in the user's locale.

### 2.5 Verification

| Scenario | Expected Behavior |
|----------|-------------------|
| Exchange code expired | Toast shows "exchange_code 无效或已过期" (backend message) |
| Account disabled during OAuth | Toast shows "账号不存在或已被禁用" (backend message) |
| Network error | Toast shows "OAuth login failed, please retry" (fallback) |
| Successful exchange | Normal login flow, no toast |

---

## 3. R6: Unify 15 Direct `fetch` Calls

### 3.1 Problem Overview

15 call sites across 4 files use `fetch()` directly with manual `Authorization: 'Bearer ' + localStorage.getItem('token')` headers, bypassing `handleResp`. When the JWT expires:

- No refresh attempt (no `handle401` path)
- No login redirect (no `showLogin` path)
- Silent failure or generic "load failed" toast
- 401 response body may be parsed as JSON, yielding `undefined` fields → silent logic errors

### 3.2 Call Site Inventory

| # | File | Line | Endpoint | Method | Usage | Current 401 Behavior | Fix Strategy |
|---|------|------|----------|--------|-------|---------------------|--------------|
| 1 | events.js | 407 | `/api/task-logs/{id}/info` | GET | Prompt detail | `r.ok ? r.json() : null` → silent null | A: `API.get` |
| 2 | events.js | 594 | `/api/events/{seq}/knowledge/{scope}` | DELETE | Delete knowledge | 401 body parsed as JSON → `d.ok` undefined → "delete failed" | A: `API.del` |
| 3 | events.js | 659 | `/api/events/{seq}/chat` | DELETE | Clear conversation | Same as #2 | A: `API.del` |
| 4 | events.js | 733 | `/api/events/chat/task/{id}/stream` | GET (SSE) | SSE stream | `!resp.ok` → "send failed" | B: `API.ensureTokenValid` + raw fetch |
| 5 | events.js | 784 | `/api/events/chat/task/{id}/status` | GET | Task status | Not checked → `st.status` undefined → silent | A: `API.get` |
| 6 | events.js | 1249 | `/api/task-logs/{id}/info` | GET | Task log info | `r.ok ? r.json() : null` → silent null | A: `API.get` |
| 7 | events.js | 1285 | `/api/task-logs/{id}/stream` | GET (SSE) | Task log SSE stream | `!resp.ok` → "HTTP {status}" → connection failed | B: `API.ensureTokenValid` + raw fetch |
| 8 | events.js | 1426 | `/api/runtime/tasks/{id}/status` | GET | Task status | `r.ok ? r.json() : null` → silent null | A: `API.get` |
| 9 | issues.js | 1165 | `/api/issues/{id}/attachments` | POST (FormData) | Upload attachment | No 401 handling → catch shows "upload failed" | A: `API.upload` |
| 10 | issues.js | 1204 | `/api/issues/attachments/pre-upload` | POST (FormData) | Pre-upload attachment | Same as #9 | A: `API.upload` |
| 11 | issues.js | 1238 | `/api/issues/attachments/pre-upload/{name}` | DELETE | Delete pre-uploaded attachment | No 401 handling → catch removes attachment silently | A: `API.del` |
| 12 | skills.js | 961 | `/api/skills/by-id/{id}/download` | GET (blob) | Download skill ZIP | `.catch(() => showToast('下载失败'))` | A: `API.getRaw` + blob handling |
| 13 | chat.js | 500 | `/api/chat/task/{id}/status` | GET | Task status (switchConversation) | `r.json()` on 401 → `st.status` undefined → silent | A: `API.get` |
| 14 | chat.js | 772 | `/api/chat/task/{id}/stream` | GET (SSE) | SSE stream | `!resp.ok` → connection failed | B: `API.ensureTokenValid` + raw fetch |
| 15 | chat.js | 845 | `/api/chat/task/{id}/status` | GET | Task status (resumeChatTask) | Same as #13 | A: `API.get` |

### 3.3 Strategy A: Replace with `API.get` / `API.post` / `API.del`

For standard JSON endpoints, replace `fetch(url, {headers: {'Authorization': ...}})` with `API.get(path)` / `API.del(path)`. This routes through `handleResp`, which provides:

1. **401 → refresh + retry**: `handleResp(r, retryFn)` calls `handle401(retryFn)` on session-expired 401s
2. **Error body parsing**: `_safeErrJson` extracts `detail.message`, sets `e.status` and `e.error_code`
3. **Consistent error messages**: Uses i18n keys (`common.request_failed`, `common.session_expired`)
4. **Timeout + nav signal**: `_fetchWithTimeout` provides 30s timeout + tab/navigation cancellation
5. **Proactive token refresh**: `_makeHeaders` calls `ensureTokenValid` before each request

**Example transformation (events.js:407)**:

```js
// Before
const token = localStorage.getItem('token') || '';
fetch('/api/task-logs/' + encodeURIComponent(taskId) + '/info', {headers: {'Authorization': 'Bearer ' + token}})
  .then(r => r.ok ? r.json() : null)
  .then(info => { /* ... */ })
  .catch(() => { /* show load failed */ });

// After
API.get('/task-logs/' + encodeURIComponent(taskId) + '/info')
  .then(info => { /* ... same logic ... */ })
  .catch(e => {
    if (e.name === 'AbortError' && e.silent) return;  // nav/tab cancel
    // show load failed with e.message
  });
```

**For DELETE operations (events.js:594)**:

```js
// Before
fetch('/api/events/' + _convSeq + '/knowledge/' + scope, {
  method: 'DELETE',
  headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')}
}).then(function(r) { return r.json(); }).then(function(d) {
  if (d.ok) { /* success */ } else { /* fail */ }
}).catch(function(e) { /* fail */ });

// After
API.del('/events/' + _convSeq + '/knowledge/' + scope).then(function(d) {
  if (d.ok) { /* success */ } else { /* fail */ }
}).catch(function(e) {
  // e.message has backend detail or i18n fallback
  app.showToast(I18N.t('events.knowledge_delete_failed', {msg: e.message}));
});
```

**For FormData uploads (issues.js:1165)**:

```js
// Before
fetch('/api/issues/' + taskId + '/attachments', {
  method: 'POST',
  headers: { 'Authorization': 'Bearer ' + localStorage.getItem('token') },
  body: formData
}).then(function(r) { return r.json(); }).then(function() {
  _loadTaskAttachments(taskId);
}).catch(function(e) { /* upload failed */ });

// After — use API.upload which handles 401 (with refresh after R9-1)
API.upload('/issues/' + taskId + '/attachments', formData).then(function() {
  _loadTaskAttachments(taskId);
}).catch(function(e) {
  if (list) list.innerHTML = '<div style="font-size:12px;color:#ff3b30">上传失败: ' + e.message + '</div>';
  _loadTaskAttachments(taskId);
});
```

**For pre-upload in create Issue form (issues.js:1204, P32)**:

The pre-upload endpoint uses `alert()` for error display, which blocks UI. The fix replaces it with `showToast` or inline error display:

```js
// Before (issues.js:1204-1215)
fetch('/api/issues/attachments/pre-upload?repo_key=' + encodeURIComponent(repoKey), {
  method: 'POST',
  headers: { 'Authorization': 'Bearer ' + localStorage.getItem('token') },
  body: formData
}).then(function(r) { return r.json(); }).then(function(d) {
  loading.remove();
  if (d.error) { alert('上传失败: ' + d.error); return; }  // ← P32: alert blocks UI
  _createAttachments.push({filename: d.filename, safe_name: d.safe_name, size: d.size, url: d.url});
  _renderCreateAttachments();
}).catch(function(e) {
  loading.remove();
  alert('上传失败: ' + e.message);  // ← P32: alert blocks UI
});

// After — use API.upload + showToast (no alert)
API.upload('/issues/attachments/pre-upload?repo_key=' + encodeURIComponent(repoKey), formData).then(function(d) {
  loading.remove();
  if (d.error) { app.showToast(I18N.t('issues.upload_failed', {msg: d.error})); return; }
  _createAttachments.push({filename: d.filename, safe_name: d.safe_name, size: d.size, url: d.url});
  _renderCreateAttachments();
}).catch(function(e) {
  loading.remove();
  app.showToast(I18N.t('issues.upload_failed', {msg: e.message || ''}));
});
```

> **P32 scenario**: User fills in Issue create form (title, description), then uploads attachment `screenshot.png`. File exceeds 50MB → backend returns 413. `alert("上传失败: Payload Too Large")` blocks UI. User clicks "OK" — alert closes but loading div still shows "上传中...". User confused: retry or re-select? With fix: `showToast("上传失败: Payload Too Large")` shows 3 seconds, non-blocking, loading div properly removed in catch.

The download case is special — the response is a binary blob, not JSON. `API.get` calls `handleResp` which does `r.json()`, which would fail on a blob. Two options:

- **Option 1 (recommended)**: Keep as direct fetch but wrap with `_fetchWithTimeout` + manual 401 check that calls `handle401`. This gives timeout/nav-signal benefits without forcing JSON parsing.
- **Option 2**: Add a `API.getRaw(path)` method that uses `_fetchWithTimeout` + `_makeHeaders` but returns the raw `Response` (no `handleResp`), letting the caller handle the response type.

**Option 1 implementation**:

```js
function downloadSkillZip(id) {
  API.get('/skills/by-id/' + encodeURIComponent(id) + '/download', 30000)
    .then(d => {
      // If backend returns JSON with a URL, open it
      if (d && d.url) { window.open(d.url, '_blank'); return; }
    })
    .catch(e => {
      if (e.name === 'AbortError' && e.silent) return;
      // Fallback: direct fetch for blob response
      // (This handles the case where the endpoint returns binary, not JSON)
      _downloadSkillBlob(id);
    });
}

function _downloadSkillBlob(id) {
  _makeHeaders().then(h => {
    return _fetchWithTimeout(BASE + '/skills/by-id/' + encodeURIComponent(id) + '/download', {headers: h}, 30000);
  }).then(r => {
    if (!r.ok) throw new Error('download failed');
    return r.blob();
  }).then(b => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(b);
    a.download = 'skill-' + id + '.zip';
    a.click();
    URL.revokeObjectURL(a.href);
  }).catch(() => {
    if (app && app.showToast) app.showToast(I18N.t('skills.download_failed') || '下载失败');
  });
}
```

> **Note**: The skills download endpoint currently returns either JSON (`{url: ...}`) or a binary blob depending on configuration. The implementation must check `content-type` and branch accordingly. Since this requires exposing `_makeHeaders` / `_fetchWithTimeout` from the API module (currently private), **Option 2** (adding `API.getRaw`) is cleaner.

**Recommended: Add `API.getRaw(path, timeout)` to the API module**:

```js
// In api.js return block:
getRaw: (path, timeout) => {
  return _makeHeaders().then(h => _fetchWithTimeout(BASE + path, {headers: h}, timeout, false, false));
},
```

This returns a raw `Response` (not parsed), with auth headers + timeout + nav-signal. The caller handles `r.ok`, `r.blob()`, and manual 401 checking. For 401 on `getRaw`, the caller can call `API.get(path)` as a retry (which goes through `handleResp`).

### 3.4 Strategy B: `_fetchWithAuth` for SSE Streams

SSE streaming responses (`text/event-stream`) cannot use `API.get` because `handleResp` calls `r.json()`. These need a wrapper that:

1. Attaches auth headers (via `_makeHeaders`)
2. Uses `_fetchWithTimeout` for timeout + nav signal
3. Does **not** call `handleResp` (no JSON parsing)
4. Still handles 401 by attempting refresh + retry

**Affected call sites**:
- events.js:733 — `/api/events/chat/task/{id}/stream` (SSE)
- chat.js:794 — `/api/chat/task/{id}/stream` (SSE)
- events.js:1285 — `/api/task-logs/{id}/stream` (SSE, in `_taskLogStream`)

**Implementation**: Use `API.getRaw(path)` (from Strategy A) to get the raw `Response` with auth headers. Then check `r.ok` manually. If 401, fall back to `API.get(path)` (which triggers refresh) then re-stream. In practice, SSE streams are short-lived and the token is validated at connection time — if the token is expired, the SSE connection will fail with 401 and the user can retry. A simpler approach is acceptable:

```js
// events.js streamConvTask — Before:
const resp = await fetch('/api/events/chat/task/' + taskId + '/stream', {
  headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
  signal: ac.signal
});

// After:
const token = await _ensureToken();  // or API.getRaw
const resp = await fetch('/api/events/chat/task/' + taskId + '/stream', {
  headers: {'Authorization': 'Bearer ' + token},
  signal: ac.signal
});
if (!resp.ok) {
  if (resp.status === 401) {
    // Token expired — try refresh then show error
    try { await API.get('/events/chat/task/' + taskId + '/status'); }
    catch(_) { /* refresh failed — showLogin already triggered by handleResp */ }
  }
  if (el) el.textContent = I18N.t('events.send_failed_paren');
  _clearConvTask(seq);
  return;
}
```

> **Simplification**: For SSE streams, the pragmatic approach is to use `ensureTokenValid()` (exposed as `API.ensureTokenValid()`) before the fetch to get a fresh token, then do the direct fetch with the SSE signal. This avoids the complexity of refreshing mid-stream. If the token is already expired and refresh fails, `ensureTokenValid` returns the stale token, the SSE fetch gets 401, and we show an error. The user can re-authenticate.

**Expose `ensureTokenValid` from the API module**:

```js
// In api.js return block:
ensureTokenValid: ensureTokenValid,
```

### 3.5 Migration Checklist

| # | File | Line | Strategy | Notes |
|---|------|------|----------|-------|
| 1 | events.js | 407 | A → `API.get` | `_loadEventPrompt` — add catch with error toast |
| 2 | events.js | 594 | A → `API.del` | `_deleteKnowledge` — already has catch with toast |
| 3 | events.js | 659 | A → `API.del` | `_doClearConv` — already has catch with toast |
| 4 | events.js | 733 | B → `ensureTokenValid` + direct fetch | `streamConvTask` — SSE, keep signal |
| 5 | events.js | 784 | A → `API.get` | `resumeConvTask` — add catch |
| 6 | events.js | 1249 | A → `API.get` | `_tlLoadInfo` — add catch with silent abort |
| 7 | events.js | 1426 | A → `API.get` | `_tlCheckTaskStatus` — add catch |
| 8 | issues.js | 1165 | A → `API.upload` | `_onTaskAttachSelect` — FormData upload |
| 9 | issues.js | 1204 | A → `API.upload` | `_onTaskAttachSelect` (create) — FormData upload |
| 10 | skills.js | 961 | A → `API.getRaw` | `downloadSkillZip` — blob/JSON hybrid |
| 11 | chat.js | 522 | A → `API.get` | `switchConversation` status check |
| — | chat.js | 867 | A → `API.get` | `resumeChatTask` status check (same pattern) |
| — | chat.js | 794 | B → `ensureTokenValid` + direct fetch | SSE stream (same as #4) |
| — | events.js | 1285 | B → `ensureTokenValid` + direct fetch | `_taskLogStream` SSE (same as #4) |

### 3.6 API Module Exports to Add

The API module (`api.js`) currently exports: `get`, `post`, `put`, `del`, `deleteFeedback`, `scheduleProactiveRefresh`, `upload`.

**New exports needed**:

```js
return {
  get: _get,
  post: _post,
  put: _put,
  del: _del,
  deleteFeedback: deleteFeedback,
  scheduleProactiveRefresh: scheduleProactiveRefresh,
  upload: (path, formData, onUploadProgress) => { /* ... existing ... */ },
  // ── R6 additions ──
  getRaw: (path, timeout) => {
    return _makeHeaders().then(h => _fetchWithTimeout(BASE + path, {headers: h}, timeout, false, false));
  },
  ensureTokenValid: ensureTokenValid,
};
```

---

## 4. R7: Preserve `/en` Language Prefix on `/login` Redirect

### 4.1 Current Behavior (api.js)

Three hardcoded `/login` references:

| Line | Code | Context |
|------|------|---------|
| 101 | `if (location.pathname !== '/login') location.href = '/login';` | `doRefresh` catch — refresh failed |
| 255 | `location.href = '/login';` | `_redirectToGitcodeAuth` — no redirect_url |
| 258 | `location.href = '/login';` | `_redirectToGitcodeAuth` — fetch failed |

**Problem**: When the user is in English mode (URL is `/en/...`), these redirects go to `/login` (Chinese login page), losing the language context.

### 4.2 Fix

Add a helper function and replace all 3 occurrences:

```js
// Language-aware login URL
function _langLoginUrl() {
  var lang = (typeof I18N !== 'undefined' && I18N.getLang) ? I18N.getLang() : (localStorage.getItem('grapeLang') || 'zh');
  return (lang === 'en' ? '/en' : '') + '/login';
}

// Check if currently on any login page (both /login and /en/login)
function _isOnLoginPage() {
  var p = location.pathname;
  return p === '/login' || p === '/en/login';
}
```

**Line 101** (doRefresh catch) — **Note**: This will be replaced by `app.showLogin()` in R11-2. If R11-2 is implemented, this line no longer needs the language prefix fix. However, if R11 is deferred, apply the R7 fix here:

```js
// If R11-2 is NOT yet applied:
if (!_isOnLoginPage()) location.href = _langLoginUrl();

// If R11-2 IS applied (preferred):
if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
else if (!_isOnLoginPage()) location.href = _langLoginUrl();
```

**Lines 255, 258** (_redirectToGitcodeAuth):

```js
function _redirectToGitcodeAuth() {
  if (_reauthRedirecting) return;  // R9-2 dedup
  if (_isOnLoginPage()) return;
  _reauthRedirecting = true;
  API.get('/auth/login/gitcode').then(d => {
    if (d.redirect_url) {
      location.href = d.redirect_url;
    } else {
      location.href = _langLoginUrl();
    }
  }).catch(() => {
    location.href = _langLoginUrl();
  });
}
```

### 4.3 Verification

| Scenario | URL Before | Expected URL After |
|----------|------------|-------------------|
| English mode, refresh fails | `/en/dashboard` | `/en/login` |
| Chinese mode, refresh fails | `/dashboard` | `/login` |
| English mode, already on login | `/en/login` | No redirect (stay) |
| Chinese mode, OAuth reauth fails | `/en/settings` | `/en/login` |

---

## 5. R8: `api.js` Robustness

### 5.1 R8-1: 200 + HTML → `server_error` Instead of `session_expired`

**Current** (api.js:236–240):

```js
if (ct.indexOf('text/html') !== -1) {
  if (r.status === 200) {
    // 可能是 token 过期后反代返回登录页 HTML
    throw new Error(I18N.t('common.session_expired') || '登录已过期，请重新登录');
  }
  throw new Error(I18N.t('common.server_error') || '服务器异常，请稍后重试');
}
```

**Problem**: 200 + HTML is not necessarily session expiry — it's more likely an nginx misconfiguration or proxy error. Throwing `session_expired` misleads users and troubleshooters.

**Fix**:

```js
if (ct.indexOf('text/html') !== -1) {
  // 200 + HTML = nginx/proxy returned a page instead of API JSON.
  // This is a server configuration issue, not a session problem.
  // Use neutral server_error for all HTML responses (200 or otherwise).
  var e = new Error(I18N.t('common.server_error') || '服务器异常，请稍后重试');
  e.status = r.status;
  throw e;
}
```

**Key change**: Remove the `r.status === 200` special case that throws `session_expired`. All HTML responses (regardless of status code) now throw `server_error`. This also fixes P18 (200+HTML doesn't clear token or redirect — now it doesn't pretend to be a session issue).

### 5.2 R8-2: `ensureTokenValid` Network Error — Skip Retry

**Current** (api.js:139–150):

```js
try {
  return await doRefresh();
} catch (e) {
  // 重试 1 次（500ms 后）
  await new Promise(r => setTimeout(r, 500));
  try {
    return await doRefresh();
  } catch (e2) {
    return token;  // 重试仍失败——返回原 token，交给 401 兜底
  }
}
```

**Problem**: When the backend is unreachable (network error / `TypeError`), each `doRefresh()` waits up to 10 seconds (AbortController timeout). Two attempts = ~20 seconds of blocked UI.

**Fix**: Distinguish network errors (`TypeError`) from HTTP errors. Network errors are transient and unlikely to resolve in 500ms — skip the retry and return the stale token immediately. HTTP errors (refresh_token expired) are worth retrying once.

```js
async function ensureTokenValid() {
  const token = localStorage.getItem('token');
  if (!token) return null;
  const remaining = _getRemainingMs(token);
  if (remaining > _REFRESH_THRESHOLD_MS) return token;
  try {
    return await doRefresh();
  } catch (e) {
    // Network error (TypeError) — backend unreachable, retry won't help.
    // Return stale token; the subsequent request will fail and 401兜底 handles it.
    if (e instanceof TypeError) return token;
    if (e.name === 'AbortError') return token;  // Timeout — same as network error
    // HTTP error (refresh_token rejected) — retry once after short delay
    await new Promise(r => setTimeout(r, 500));
    try {
      return await doRefresh();
    } catch (e2) {
      return token;  // Retry failed — return stale token, 401兜底
    }
  }
}
```

**Impact**: Network-down scenario goes from ~20s blocked → ~0s (immediate return of stale token, then the actual request fails fast with TypeError → `_fetchWithTimeout` network error handling kicks in).

### 5.3 Verification

| Scenario | Before | After |
|----------|--------|-------|
| 200 + HTML response | "会话过期" (session_expired) | "服务异常，请稍后重试" (server_error) |
| Network down + token expiring | ~20s blocked, then stale token | Immediate stale token, request fails fast |
| refresh_token expired | ~0.5s + 1 retry, then stale token | Same (retry preserved for HTTP errors) |
| Normal refresh | Immediate new token | Same |

---

## 6. R9: `api.js` Robustness Supplement + Page-Level Fixes

### 6.1 R9-1: Upload 401 — Align with `handleResp` Behavior

**Current** (api.js:413–417):

```js
if (xhr.status === 401) {
  // upload 401 不自动 refresh——直接跳登录
  localStorage.removeItem('token');
  localStorage.removeItem('user');
  if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
}
```

**Problem**: Upload (XMLHttpRequest) 401 immediately clears token and shows login. Other API calls (fetch via `handleResp`) attempt refresh first. This means uploading a file with an expired token kicks the user to login without trying to refresh, while a GET request would silently refresh and retry.

**Fix**: Attempt refresh before giving up. Since `XMLHttpRequest` can't easily retry the upload mid-flight, use a simpler approach: check `ensureTokenValid()` before the upload (already done via the `ensureTokenValid().then(token => ...)` wrapper at line 394). The `ensureTokenValid` call should refresh the token if it's close to expiry. If the token is already expired and refresh fails, `ensureTokenValid` returns null, and the upload proceeds without auth → gets 401 → clears token + showLogin (same as current).

The key improvement is to **attempt refresh on 401** rather than immediately clearing:

```js
xhr.onload = () => {
  const resp = xhr.responseText;
  let json = null;
  try { json = JSON.parse(resp); } catch { /* ignore */ }
  if (xhr.status >= 200 && xhr.status < 300) {
    resolve(json || {message: resp});
  } else {
    if (xhr.status === 401) {
      // Attempt refresh, then retry upload once
      doRefresh().then(newToken => {
        // Retry upload with new token
        // NOTE: FormData is consumed after xhr.send() — cannot reuse directly.
        // The retry helper must reconstruct FormData from the original source
        // (File/Blob objects are not consumed by send() and can be re-wrapped).
        // Callers should pass a formDataFactory function instead of a FormData instance.
        _retryUpload(path, formDataFactory, onUploadProgress, newToken, resolve, reject);
      }).catch(() => {
        // Refresh failed — clear token + show login (same as before)
        localStorage.removeItem('token');
        localStorage.removeItem('user');
        if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
        const err = new Error(json?.detail?.message || I18N.t('common.session_expired'));
        err.errors = json?.detail?.errors || null;
        reject(err);
      });
      return;  // Don't reject yet — refresh in progress
    }
    const err = new Error(json?.detail?.message || I18N.t('common.request_failed', {status: xhr.status}));
    err.errors = json?.detail?.errors || null;
    reject(err);
  }
};
```

> **FormData 复用问题（重要）**：`XMLHttpRequest.send(formData)` 会消费 FormData 对象，重试时不能直接传入同一个 FormData 实例。有两种解决方案：
>
> - **方案 A（推荐）**：`API.upload` 签名改为接收 `formDataFactory: () => FormData` 函数而非 `FormData` 实例。重试时调用 `formDataFactory()` 生成新的 FormData。File/Blob 对象本身不会被 `send()` 消费（只有 FormData 的内部 entries 被读走），所以 factory 可以引用同一个 File 对象。
> - **方案 B**：在 `API.upload` 内部用 `new FormData()` + 遍历原 FormData 的 entries 重建。但 `formData.entries()` 是迭代器，遍历后也会消费。
>
> 采用方案 A：调用方传入 `() => { const fd = new FormData(); fd.append('file', fileObj); return fd; }`。
>
> 如果调用方不改（仍传 FormData 实例），则 upload 重试时检测到 FormData 已消费（无 entries）则跳过重试，直接走清 token + showLogin 路径——退化为当前行为，不会更差。

**Retry helper**:

```js
function _retryUpload(path, formData, onUploadProgress, token, resolve, reject) {
  const xhr = new XMLHttpRequest();
  xhr.open('POST', BASE + path);
  xhr.setRequestHeader('Authorization', 'Bearer ' + token);
  const _orgId = localStorage.getItem('current_org_id');
  if (_orgId) xhr.setRequestHeader('X-Org-ID', _orgId);
  xhr.upload.onprogress = (e) => {
    if (onUploadProgress && e.lengthComputable) {
      onUploadProgress(Math.round((e.loaded / e.total) * 100));
    }
  };
  xhr.onload = () => {
    const resp = xhr.responseText;
    let json = null;
    try { json = JSON.parse(resp); } catch { /* ignore */ }
    if (xhr.status >= 200 && xhr.status < 300) {
      resolve(json || {message: resp});
    } else {
      // Retry also failed — clear token + show login
      if (xhr.status === 401) {
        localStorage.removeItem('token');
        localStorage.removeItem('user');
        if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
      }
      const err = new Error(json?.detail?.message || I18N.t('common.request_failed', {status: xhr.status}));
      err.errors = json?.detail?.errors || null;
      // P38/P39 fix: set error_code and status so upstream catch can do i18n mapping
      err.status = xhr.status;
      err.error_code = json?.detail?.error_code || '';
      reject(err);
    }
  };
  xhr.onerror = () => reject(new Error(I18N.t('common.network_upload_error')));
  xhr.send(formData);
}
```

**Simpler alternative** (if retry complexity is a concern): Just ensure `ensureTokenValid()` is called before upload (it already is at line 394). The `ensureTokenValid` call refreshes if the token is within 2 minutes of expiry. For tokens that expire between the `ensureTokenValid` check and the upload completion, the 401 will occur — in that case, clear token + showLogin (current behavior). This is acceptable because the window is very small (<2 min) and the upload would need to span that window. **Recommend the simpler alternative** unless upload 401s are frequently reported.

### 6.2 R9-2: `_redirectToGitcodeAuth` Dedup

**Current**: No re-entrancy guard. Multiple concurrent `reauth_required` 401s trigger multiple `API.get('/auth/login/gitcode')` calls and multiple `location.href` assignments.

**Fix**: Add module-level flag:

```js
var _reauthRedirecting = false;

function _redirectToGitcodeAuth() {
  if (_reauthRedirecting) return;  // Already redirecting
  if (_isOnLoginPage()) return;    // R7: check both /login and /en/login
  _reauthRedirecting = true;
  API.get('/auth/login/gitcode').then(d => {
    if (d.redirect_url) {
      location.href = d.redirect_url;
    } else {
      location.href = _langLoginUrl();  // R7: language-aware
    }
  }).catch(() => {
    location.href = _langLoginUrl();
  });
  // Note: _reauthRedirecting is never reset — once we start redirecting,
  // we don't want any more redirects. The page navigation clears it implicitly.
}
```

### 6.3 R9-3: `knowledge.js` Silent Failure → Add Error Toast

**Current** (knowledge.js:153, 172):

```js
// loadList catch:
} catch (e) {
  if (e.name === 'AbortError' && e.silent) return;
  if (window._currentView !== 'knowledge') return;
  // ← No error toast, list silently shows empty/stale data
}

// loadStats catch:
} catch (e) {
  if (e.name === 'AbortError' && e.silent) return;
  // ← No error toast, stats silently show 0s
}
```

**Fix**: Add user-visible error feedback:

```js
// loadList catch:
} catch (e) {
  if (e.name === 'AbortError' && e.silent) return;
  if (window._currentView !== 'knowledge') return;
  app.showToast(e.message || I18N.t('common.load_failed'));
} finally {
  if (window._currentView === 'knowledge') this.loading = false;
}

// loadStats catch:
} catch (e) {
  if (e.name === 'AbortError' && e.silent) return;
  // Stats failure is non-critical — don't toast, just leave defaults
  // (stats are supplementary info, not the main content)
}
```

**Decision**: `loadList` failure should toast (user sees empty list and needs to know why). `loadStats` failure can remain silent (stats are supplementary; toasting on both would be noisy if both fail simultaneously).

### 6.4 R9-4: `pipeline.js` `alert()` → `showToast()`

**Current** (pipeline.js:367–376):

```js
}).then(d => {
  if (d.skipped) {
    alert(I18N.t('pipeline.sre_skipped', {skipped: d.skipped}));
  } else if (d.seq > 0) {
    alert(I18N.t('pipeline.sre_triggered', {seq: d.seq}));
    if (typeof loadPipeline === 'function') loadPipeline();
  } else {
    alert(I18N.t('pipeline.sre_result', {result: JSON.stringify(d)}));
  }
}).catch(e => {
  alert(I18N.t('pipeline.sre_trigger_failed', {msg: (e.message || e)}));
});
```

**Fix**: Replace all `alert()` with `app.showToast()`:

```js
}).then(d => {
  if (d.skipped) {
    app.showToast(I18N.t('pipeline.sre_skipped', {skipped: d.skipped}));
  } else if (d.seq > 0) {
    app.showToast(I18N.t('pipeline.sre_triggered', {seq: d.seq}));
    if (typeof loadPipeline === 'function') loadPipeline();
  } else {
    app.showToast(I18N.t('pipeline.sre_result', {result: JSON.stringify(d)}));
  }
}).catch(e => {
  app.showToast(I18N.t('pipeline.sre_trigger_failed', {msg: (e.message || e)}));
});
```

**Also**: The `confirm()` at line 352 (`triggerSREDeploy`) should be replaced with a modal dialog for consistency. However, `confirm()` is synchronous and simpler — this can be deferred. The requirement only specifies `alert()` → `showToast()`.

---

## 7. R11: Admin Change-Password 401→400 + doRefresh→showLogin

### 7.1 R11-1: Admin `/change-password` 401→400

**Current** (admin/routes.py:145–146):

```python
if not verify_password(body.old_password, _admin["password_hash"]):
    raise HTTPException(status_code=401, detail={"code": 401, "error_code": "OLD_PASSWORD_INCORRECT", "message": "Old password incorrect"})
```

**Problem**: Old password incorrect is a credential error (user input), not a session expiry. Returning 401 triggers `admin.js`'s refresh logic:
1. `admin.js` tries refresh → succeeds (refresh_token still valid)
2. Retries `/change-password` with new access token
3. Backend returns 401 again (old password still wrong)
4. Retry fails → error displayed

This wastes a refresh + retry cycle on every wrong password attempt.

**Fix**: Change status code to 400, aligning with the user-side `/auth/change-password` (grape_auth.py returns 400):

```python
if not verify_password(body.old_password, _admin["password_hash"]):
    raise HTTPException(status_code=400, detail={"code": 400, "error_code": "OLD_PASSWORD_INCORRECT", "message": "Old password incorrect"})
```

Also change the `ACCOUNT_NOT_FOUND` 401 at line 142 to 400 (same reasoning — it's not a session issue, it's an account state error encountered during an authenticated request):

```python
if not _admin:
    raise HTTPException(status_code=400, detail={"code": 400, "error_code": "ACCOUNT_NOT_FOUND", "message": "Account not found"})
```

> **Note**: `ACCOUNT_NOT_FOUND` at line 142 is reached after `require_admin` has already validated the JWT (line 139). The admin exists in the token but not in the DB — this is a data integrity issue, not a session issue. 400 is more appropriate than 401.

### 7.2 R11-2: `doRefresh` Catch → `app.showLogin()` (Unify with `handle401`)

**Current**:

| Path | Code | Behavior |
|------|------|----------|
| `doRefresh` catch (api.js:101) | `location.href = '/login'` | Full page navigation, loses SPA state, has R7 language prefix issue |
| `handle401` catch (api.js:180) | `app.showLogin()` | DOM switch, preserves SPA state, no URL change |
| `handleResp` 401 no-retry (api.js:218) | `app.showLogin()` | DOM switch |

**Problem**: Same user (refresh failed) gets different behavior depending on which code path triggered the refresh failure. `ensureTokenValid` → `doRefresh` failure does full page navigation; `handle401` → `doRefresh` failure does DOM switch.

**Fix**: Unify `doRefresh` catch to use `app.showLogin()`:

```js
function doRefresh() {
  if (_refreshPromise) return _refreshPromise;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  _refreshPromise = fetch(BASE + '/auth/refresh', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
    },
    credentials: 'same-origin',
    signal: controller.signal,
  }).then(r => {
    if (!r.ok) throw new Error('refresh failed');
    return r.json();
  }).then(d => {
    if (d.token) {
      localStorage.setItem('token', d.token);
      return d.token;
    }
    throw new Error('no token in refresh response');
  }).catch(e => {
    // Issue #63: abort/网络错误不清 token
    if (e.name === 'AbortError') throw e;
    if (e instanceof TypeError) throw e;
    // refresh 真的失败（HTTP 401/403）——清 token 跳登录
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    // R11-2: Unified to app.showLogin() (DOM switch) instead of location.href
    if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
    throw e;
  }).finally(() => {
    clearTimeout(timer);
    _refreshPromise = null;
  });
  return _refreshPromise;
}
```

**Key change**: Replace `if (location.pathname !== '/login') location.href = '/login';` with `if (typeof app !== 'undefined' && app.showLogin) app.showLogin();`.

**Side effect**: This also resolves R7 for this specific line — `showLogin()` is a DOM switch that doesn't involve URL navigation, so there's no language prefix issue. R7's `_langLoginUrl()` helper is still needed for `_redirectToGitcodeAuth` (lines 255, 258) which uses `location.href` for cross-domain OAuth redirects.

**Important**: `doRefresh` is called from two contexts:
1. `handle401` (api.js:174) — already has its own `showLogin()` in the catch (api.js:180). If `doRefresh` also calls `showLogin()`, it would be called twice. However, `handle401`'s catch only fires if `doRefresh` throws — and `doRefresh`'s catch also throws (after calling `showLogin`). So `showLogin` is called in `doRefresh` catch, then the error propagates to `handle401` catch which calls `showLogin` again. This is idempotent (showing login twice has no visible effect). **P34 fix**: Remove both `showLogin` AND `localStorage.removeItem` from `handle401`'s catch since `doRefresh` already handles both:

```js
function handle401(retryFn) {
  return doRefresh().then(() => retryFn()).catch(e => {
    if (e.name === 'AbortError' || e instanceof TypeError) throw e;
    // P34 fix: doRefresh already cleared token + called showLogin.
    // No need to duplicate — just throw the session_expired error.
    throw new Error(I18N.t('common.session_expired'));
  });
}
```

> **P34 scenario**: Without this fix, refresh failure causes showLogin to be called twice (doRefresh catch + handle401 catch). While idempotent, the double-call has unnecessary DOM operation overhead. The fix ensures doRefresh is the single source of truth for token cleanup + login redirect on refresh failure.

> Actually, `doRefresh`'s catch clears `token` and `user` already. `handle401`'s catch also clears them — this is redundant but harmless. The key is to not call `showLogin()` twice. Since `doRefresh` now calls it, remove it from `handle401`.

### 7.3 Verification

| Scenario | Before | After |
|----------|--------|-------|
| Admin wrong old password | 401 → refresh → retry → 401 → error shown (wasted cycle) | 400 → error shown directly |
| User-side refresh fails (ensureTokenValid path) | `location.href='/login'` (full reload, loses SPA state) | `app.showLogin()` (DOM switch) |
| User-side refresh fails (handle401 path) | `app.showLogin()` (DOM switch) | Same (now doRefresh handles it) |
| Network error during refresh | Token not cleared, error thrown (unchanged) | Same |
| refresh_token expired | Token cleared, `showLogin()` called | Same (but via showLogin instead of location.href) |

---

## 8. Dependency Graph

```
R1 (handleResp 401 split) ←── MUST be merged first
 │
 ├── R5 (exchange error parsing) — independent of R1, but R1 ensures 
 │    API.post on /auth/exchange wouldn't trigger refresh (not used here)
 │
 ├── R6 (direct fetch unification) — depends on R1 for handleResp 401
 │    behavior to be correct (credential vs session 401)
 │    │
 │    ├── R9-1 (upload 401 refresh) — depends on doRefresh being stable
 │    │
 │    └── R9-2 (reauth dedup) — independent
 │
 ├── R7 (language prefix) — independent, but R11-2 simplifies it
 │    │
 │    └── R11-2 (doRefresh→showLogin) — supersedes R7 for doRefresh line 101
 │         R7 still needed for _redirectToGitcodeAuth lines 255, 258
 │
 ├── R8 (200+HTML, ensureTokenValid retry) — independent
 │
 ├── R9-3 (knowledge silent) — independent
 │
 ├── R9-4 (pipeline alert) — independent
 │
 └── R11-1 (admin 401→400) — independent (backend only)
      R11-2 (doRefresh→showLogin) — depends on R7 helper for _redirectToGitcodeAuth
```

### Recommended Implementation Order

1. **R11-1** (admin/routes.py) — backend-only, no frontend dependencies
2. **R8** (api.js 200+HTML + ensureTokenValid) — isolated changes
3. **R7** (api.js language prefix helpers) — adds `_langLoginUrl()` + `_isOnLoginPage()`
4. **R11-2** (api.js doRefresh→showLogin) — uses R7's `_isOnLoginPage()` indirectly (showLogin doesn't need it, but _redirectToGitcodeAuth does)
5. **R9-2** (api.js reauth dedup) — uses R7's helpers
6. **R9-1** (api.js upload 401) — modifies upload function
7. **R9-3** (knowledge.js) — page-level fix
8. **R9-4** (pipeline.js) — page-level fix
9. **R5** (app.js exchange) — isolated
10. **R6** (events.js, issues.js, skills.js, chat.js) — largest change, do last; add `API.getRaw` + `API.ensureTokenValid` exports first

---

## 9. Files Changed Summary

| File | Changes | Requirements |
|------|---------|-------------|
| `src/api/static/js/api.js` | R7: `_langLoginUrl()` + `_isOnLoginPage()`, replace 3 `/login` redirects; R8: 200+HTML→`server_error`, ensureTokenValid network-error skip; R9-1: upload 401 refresh+retry; R9-2: `_reauthRedirecting` dedup; R11-2: doRefresh catch→`showLogin()`, remove `showLogin` from `handle401` catch; R6: export `getRaw` + `ensureTokenValid` | R7, R8, R9-1, R9-2, R11-2, R6 |
| `src/api/static/js/app.js` | R5: parse `/auth/exchange` error body, throw with `detail.message` | R5 |
| `src/api/static/js/pages/events.js` | R6: 7 fetch→`API.get`/`API.del`/`API.getRaw`; SSE streams use `ensureTokenValid` | R6 |
| `src/api/static/js/pages/issues.js` | R6: 2 fetch→`API.upload` | R6 |
| `src/api/static/js/pages/skills.js` | R6: 1 fetch→`API.getRaw` (blob download) | R6 |
| `src/api/static/js/pages/chat.js` | R6: 2 fetch→`API.get` (status checks); SSE stream uses `ensureTokenValid` | R6 |
| `src/api/static/js/pages/knowledge.js` | R9-3: add toast in `loadList` catch | R9-3 |
| `src/api/static/js/pages/pipeline.js` | R9-4: 4× `alert()`→`app.showToast()` | R9-4 |
| `src/admin/routes.py` | R11-1: change-password 401→400 (2 places) | R11-1 |

**Total**: 9 files, ~15 distinct change points.

---

## 10. Testing Strategy

### 10.1 Unit/Manual Test Cases

| Test ID | Requirement | Scenario | Expected Result |
|---------|------------|----------|-----------------|
| T-R5-1 | R5 | OAuth exchange with expired code | Toast shows backend error message |
| T-R5-2 | R5 | OAuth exchange with disabled account | Toast shows "account disabled" message |
| T-R6-1 | R6 | Token expires while viewing event detail | Prompt/task-info loads trigger refresh, not silent failure |
| T-R6-2 | R6 | Token expires during file upload | Upload triggers refresh + retry, not immediate login redirect |
| T-R6-3 | R6 | Token expires during skill download | Download shows "session expired" toast, not silent failure |
| T-R7-1 | R7 | English mode, refresh fails | Redirects to `/en/login`, not `/login` |
| T-R7-2 | R7 | Chinese mode, refresh fails | Redirects to `/login` |
| T-R8-1 | R8 | nginx returns 200+HTML | Toast shows "server error", not "session expired" |
| T-R8-2 | R8 | Network down + token expiring | No 20s hang; immediate failure |
| T-R9-1 | R9-1 | Upload with expiring token | Refresh attempted before login redirect |
| T-R9-2 | R9-2 | Multiple concurrent reauth 401s | Only one redirect to GitCode auth |
| T-R9-3 | R9-3 | Knowledge list load fails | Toast shows error, not empty list |
| T-R9-4 | R9-4 | SRE deploy trigger | Toast shown, not blocking alert() |
| T-R11-1 | R11-1 | Admin wrong old password | Immediate error, no refresh cycle |
| T-R11-2 | R11-2 | Refresh fails via ensureTokenValid | DOM switch (showLogin), not full page reload |

### 10.2 Regression Tests

- **R1 compatibility**: Verify that R1's credential/session 401 split still works after R8/R11 changes to `handleResp` and `doRefresh`.
- **Multi-tab**: Verify that `doRefresh`'s `_refreshPromise` singleton still works after R11-2 changes.
- **Upload retry**: Verify that R9-1's retry doesn't create duplicate uploads (FormData is consumed once — the retry must create a new FormData or the original must be cloned).
- **SSE streams**: Verify that `ensureTokenValid` before SSE fetch doesn't break existing stream consumption logic.

---

## 11. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| R6: `API.get` adds 30s timeout to endpoints that previously had no timeout | Medium | Low — endpoints respond faster than 30s | Verify no endpoint intentionally hangs |
| R6: SSE `ensureTokenValid` adds latency before stream | Low | Low — `ensureTokenValid` returns immediately if token is valid | Acceptable tradeoff for auth correctness |
| R9-1: Upload retry consumes FormData | Medium | Medium — FormData can only be sent once | Create new FormData in retry helper, or pass the original file + form data |
| R11-2: `showLogin()` called from `doRefresh` when `app` is not yet defined | Low | Medium — `typeof app !== 'undefined'` guard prevents crash | Already guarded with `typeof app !== 'undefined'` check |
| R8-1: 200+HTML was actually session expiry in some nginx configs | Low | Medium — user sees "server error" instead of being redirected to login | Acceptable: 200+HTML is never a correct API response; the nginx config should be fixed, not the frontend |
