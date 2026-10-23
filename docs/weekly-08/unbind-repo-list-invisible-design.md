# 仓库状态一致性修复 — 设计文档

> **Status**: Implemented. 41/41 tests pass.
> **Requirements**: See `unbind-repo-list-invisible-requirements.md`.
> **Branch**: `feat/unbind-repo-state-consistency` based on `grape-dev/weekly-08`.
> **影响文件**: `src/api/static/js/pages/account.js`、`src/api/static/js/app.js`、`src/api/static/js/api.js`、`src/api/static/js/pages/repos.js`、`src/api/static/js/pages/settings.js`、`src/api/static/js/pages/issues.js`、`src/api/static/js/pages/feedback.js`、`src/api/static/index.html`

---

## 0. 重构铁律（AI 协作必读）

> **铁律：优化重构不得破坏现有功能逻辑。此为绝对约束，优先级高于一切代码整洁目标。**

任何 AI 在修改本文件涉及的代码时，必须遵守以下规则：

1. **执行路径等价**：重构后每一条代码路径的调用顺序、条件判断、参数传递必须与重构前完全等价。提取函数只是将代码块移动到新函数中，不改变任何运行时行为。
2. **条件守卫保留**：原代码的条件守卫（如 `typeof app !== 'undefined'`、`window._currentView !== 'repos'`）必须原样保留，不得简化、合并或删除。
3. **行为模拟测试不被破坏**：`_accountCleanupAfterUnbind` 函数被测试通过 `new Function()` 提取函数体执行模拟。其内部变量名、调用方式、`typeof app` 守卫不得改为间接调用（如 `_safeCall` 辅助函数），否则行为模拟测试会失败。
4. **合并条件需验证等价性**：如果合并两条路径的条件判断（如统一错误处理），必须在注释中说明等价性依据，并验证后端实际行为。例如 `_accountOnUnbindError` 统一了 `need_set_password` 检查——验证依据是后端只在 `status_code=400` 时返回 `need_set_password=True`（grape_auth.py:1058-1063, settings.py:25-30），因此 `code===400` 是冗余的二次确认。
5. **测试同步更新**：如果重构改变了函数调用链（如提取 `_accountOnUnbindSuccess`），测试用例必须同步更新搜索目标，但断言的**行为语义不变**。

### 已完成的重构

`_accountUnbindProvider` 的两条成功回调路径（普通解绑 + 确认后解绑）有大量重复代码。提取为：

- `_accountOnUnbindSuccess(provider, d, isZh)`：成功统一处理。内部执行顺序与原代码完全一致：`_accountShowUnbindSuccess` → 更新 bindings → `removeItem` → `_accountCleanupAfterUnbind` → `setTimeout(initSidebarRepos)` → `_currentView` 守卫 → `renderAccount`
- `_accountOnUnbindError(e)`：失败统一处理。原普通路径只检查 `need_set_password`，确认后路径额外检查 `code===400`。统一为 `need_set_password` 检查——等价性依据见上方第 4 条。
- `_accountCleanupAfterUnbind(provider)`：**未重构**，内部逻辑完全保持原样。这是行为模拟测试的目标函数，任何内部改动都会破坏 T05-T10/T25 的 `runCleanup` 行为模拟。

---

## 1. 问题陈述

### 1.1 多层仓库状态

前端维护多层仓库状态，状态变更操作只清理了其中部分层级，没有同步清理的统一机制：

| 层级 | 存储位置 | 用途 |
|------|---------|------|
| L1 | `sessionStorage.currentRepo` | 当前选中仓库 |
| L2 | `localStorage.repo_member_cache` | /repos/member 响应缓存 |
| L3 | app.js 闭包变量 `_cachedReposData` | 语言切换轻量重渲染 |
| L4 | `localStorage.user.bindings` | 已绑定平台列表 |
| L6 | DOM `userRole.textContent` | 顶栏角色显示 |

### 1.2 各需求对应的问题

| 需求 | 问题 | 根因 |
|------|------|------|
| R1/R2/R3 | 解绑平台后仓库列表不可见 + 角色过期 | account.js 解绑回调未清 L1/L3/L6，未引导跳转 |
| R4 | 解除托管后 loadRepos 短暂渲染旧缓存 | repos.js/settings.js 未清 L2 |
| R5 | 组织切换后语言切换渲染旧组织仓库 | app.js switchOrg 未清 L3 |
| R6 | issues.js 跳转后 GitHub 仓库显示 GitCode 图标 | issues.js 三处缺 repoKey |
| R7 | 新建仓库后语言切换新仓库消失 | app.js _forceSyncRepos 未清 L3 |
| R8 | 多标签页解绑后 Tab B 卡死 | api.js storage 事件只处理 token |
| R9 | 提交反馈不带 repo_key | feedback.js 使用不存在的 window._currentRepo |

### 1.3 后端已正确处理（无需改动）

| 后端操作 | 位置 | 说明 |
|----------|------|------|
| `delete_external_identity` | grape_account_db.py:703-737 | 软删除（status=0），清空 token |
| `DELETE FROM user_repos WHERE repo_key LIKE '{provider}:%'` | grape_auth.py:1088-1093 | 清理该平台仓库缓存 |
| `get_user_identities` 只查 `status IN (1, 4)` | grape_account_db.py:539-541 | 解绑后不再返回该 provider |
| `/repos/member` 只拉 `_bound_providers` | repos.py:480-492 | 不会尝试拉取已解绑平台 |
| `check_repo_access` | deps.py:632-655 | 不含已解绑仓库 → 403 |

### 1.4 dashboard 403 自愈机制（已有实现，本次不改）

app.js:1449-1471 已有 dashboard 403 自愈。其他视图（pipe/vod/issue/coding/qa/sre/chat）的 catch 块完全没有 403 自愈。本次修复通过 R1+R2 在解绑时主动清理，避免用户后续走 403 路径。

---

## 2. 设计

### 2.1 R1/R2/R3：解绑平台——提取公共清理函数

在 account.js 中提取 `_accountCleanupAfterUnbind(provider)`，统一处理 R1/R2/R3。

```js
// account.js 新增函数
function _accountCleanupAfterUnbind(provider) {
  // R1: 检查 currentRepo 是否属于被解绑的平台
  var _needRedirect = false;
  try {
    var cr = JSON.parse(sessionStorage.getItem('currentRepo') || 'null');
    if (cr && cr.repoKey && cr.repoKey.indexOf(provider + ':') === 0) {
      sessionStorage.removeItem('currentRepo');
      _needRedirect = true;
    }
  } catch(e) {}

  // R3: 清理内存缓存 _cachedReposData（无条件）
  if (typeof app !== 'undefined' && app._clearCachedReposData) {
    app._clearCachedReposData();
  }

  // R2: 刷新角色（无条件——解绑可能影响托管仓库列表，角色可能变化）
  if (typeof app !== 'undefined' && app.loadProfile) {
    app.loadProfile();
  }

  // R2: 如果清理了 currentRepo，引导用户回到仓库选择页
  if (_needRedirect) {
    if (typeof app !== 'undefined' && app.goToRepos) {
      app.goToRepos();
    }
  }
}
```

**R2 中 `loadProfile()` 无条件调用的理由**：即使 currentRepo 不属于被解绑平台（场景 3），解绑可能影响用户托管仓库列表（如解绑 GitHub 后用户不再是某个 GitHub 仓库的 admin），角色显示需要刷新。`loadProfile()` 内部有 `window._currentView` 守卫（app.js:193），不会触发路由恢复竞争。

### 2.2 R1 判断逻辑：repoKey 前缀匹配

| provider | 匹配前缀 | 示例 |
|----------|---------|------|
| `github` | `github:` | `github:octocat/my-repo` |
| `gitcode` | `gitcode:` | `gitcode:dev/project` |

不匹配（场景 3）：GitCode 仓库解绑 GitHub → 不清理。边界：currentRepo 为 null 或无 repoKey → `cr && cr.repoKey` 为 false → 不清理。

### 2.3 R3/R5/R7 实现：app.js 暴露 `_clearCachedReposData`

```js
// app.js — 新增函数
function _clearCachedReposData() {
  _cachedReposData = null;
}

// 在 return 导出对象中新增 _clearCachedReposData
```

R5 中 `switchOrg` 在 app.js 闭包内部，直接 `_cachedReposData = null`。
R7 中 `_forceSyncRepos` 在 app.js 闭包内部，直接 `_cachedReposData = null`。

### 2.4 R4：解除托管后清理 L2

**repos.js `_doUnhost`**（repos.js:626 处）：
```js
API.post('/repos/remove', {full_name: fullName}).then(r => {
  app.showToast(I18N.t('repos.unhost.done'));
  // R4: 清除仓库缓存
  localStorage.removeItem('repo_member_cache');
  app.loadProfile().then(() => { /* 后续不变 */ });
```

**settings.js `unhostFromSettings`**（settings.js:66 处）：
```js
API.post('/repos/remove', {full_name: fullName}).then(r => {
  app.showToast(I18N.t('settings.host.unhost_done'));
  // R4: 清除仓库缓存
  localStorage.removeItem('repo_member_cache');
  const cur = app.currentRepo(); /* 后续不变 */
```

### 2.5 R5：组织切换后清理 L3

```js
// app.js switchOrg 中，在 localStorage.removeItem(REPO_CACHE_KEY) 之后
function switchOrg(orgId) {
  // ...
  localStorage.removeItem(REPO_CACHE_KEY);
  // R5: 清除内存缓存
  _cachedReposData = null;
  // ...
```

### 2.6 R7：仓库列表变更后清理 L3

三个操作路径：

**`_forceSyncRepos`（app.js 闭包内，直接赋值）**：
```js
function _forceSyncRepos() {
  API.post('/repos/sync').then(function(d) {
    var repos = d.repos || [];
    try { localStorage.setItem(REPO_CACHE_KEY, JSON.stringify({repos: repos, ts: Date.now()})); } catch(e) {}
    // R7: 清除内存缓存
    _cachedReposData = null;
    renderReposFromData(repos);
  }).catch(function(e) { loadRepos(true); });
}
```

**`refreshRepos`（repos.js，通过 app 方法清理）**：
```js
function refreshRepos(btn) {
  if (btn) { btn.disabled = true; btn.textContent = I18N.t('repos.sync.syncing'); }
  API.post('/repos/sync').then(d => {
    const repos = (d.repos || []);
    _repoPage = 0;
    // R7: 清除内存缓存
    if (typeof app !== 'undefined' && app._clearCachedReposData) app._clearCachedReposData();
    renderRepoSelection(repos);
    app.showToast(I18N.t('repos.sync.refresh_success'));
  }).catch(e => { /* 不变 */ });
}
```

**`submitCreateRepo`（repos.js，通过 app 方法清理）**：
```js
API.post('/repos/sync').then(function(d) {
  var repos = (d && d.repos) || [];
  try { localStorage.setItem('repo_member_cache', JSON.stringify({repos: repos, ts: Date.now()})); } catch(e) {}
  // R7: 清除内存缓存
  if (typeof app !== 'undefined' && app._clearCachedReposData) app._clearCachedReposData();
  renderRepoSelection(repos);
}).catch(function() { /* 不变 */ });
```

### 2.7 R6：issues.js 补全 repoKey

三处修改，统一模式：

```js
// 修复前
const repoObj = {owner: parts[0], name: parts[1], fullName: parts[0] + '/' + parts[1]};

// 修复后
const repoObj = {owner: parts[0], name: parts[1], fullName: parts[0] + '/' + parts[1], repoKey: t.repo_key};
```

### 2.8 R8：多标签页 localStorage 同步

在 api.js:54 的 `storage` 事件监听中扩展处理：

```js
window.addEventListener('storage', (e) => {
  if (e.key === 'token') {
    /* 原有逻辑不变 */
  }
  // R8: 其他标签页清除 repo_member_cache 或修改 user.bindings 时
  // 检查当前标签页的 currentRepo 是否仍有效
  if (e.key === 'repo_member_cache' || e.key === 'user') {
    try {
      var cr = JSON.parse(sessionStorage.getItem('currentRepo') || 'null');
      if (cr && cr.repoKey) {
        // 从 localStorage.user.bindings 检查 currentRepo 的 provider 是否仍绑定
        var _u = JSON.parse(localStorage.getItem('user') || '{}');
        var _bindings = _u.bindings || [];
        // 提取 currentRepo 的 provider 前缀（使用 indexOf + substring，比 split 更安全）
        var _colonIdx = cr.repoKey.indexOf(':');
        if (_colonIdx > 0) {
          var _provider = cr.repoKey.substring(0, _colonIdx);
          // _provider !== cr.repoKey 防止无冒号时 _provider 等于完整 repoKey 导致误判
          if (_provider && _provider !== cr.repoKey && _bindings.indexOf(_provider) < 0) {
            // provider 已被其他标签页解绑 → 清理当前标签页状态
            sessionStorage.removeItem('currentRepo');
            if (typeof app !== 'undefined' && app._clearCachedReposData) app._clearCachedReposData();
            if (typeof app !== 'undefined' && app.goToRepos) app.goToRepos();
          }
        }
      }
    } catch(ex) {}
  }
});
```

**判断逻辑**：从 `currentRepo.repoKey` 提取 provider 前缀（`github:xxx` → `github`），检查 `localStorage.user.bindings` 是否仍包含该 provider。不包含说明其他标签页已解绑该平台。

**provider 提取方式**：使用 `indexOf(':')` + `substring(0, _colonIdx)` 而非 `split(':')[0]`。原因：`split(':')[0]` 对无冒号字符串返回完整字符串，需额外 `_provider !== cr.repoKey` 防御。`indexOf(':')` 返回 -1 时 `_colonIdx > 0` 守卫直接跳过，更安全。

**为什么从 `localStorage.user.bindings` 判断而非调后端 API**：`storage` 事件是同步触发的，此时 `localStorage.user` 已被其他标签页更新。直接读取即可判断，无需额外 API 调用。如果 `user.bindings` 被修改但 currentRepo 的 provider 仍在 bindings 中（如其他标签页只是刷新了 token），不会误清理。

**`e.key === 'user'` 触发频率说明**：`loadProfile()` 会写 `localStorage.setItem('user', ...)`，触发其他标签页的 storage 事件。R8 处理逻辑每次会读取 currentRepo + bindings 做检查，但这是纯同步内存操作（JSON.parse + indexOf），性能可忽略。且 `loadProfile` 不改变 `bindings`（后端 `/auth/profile` 返回的 bindings 与解绑前一致），所以不会误清理。

### 2.9 R9：feedback.js 修复

```js
// 修复前（feedback.js:326-335）
var repoKey = '';
try {
  if (window._currentRepo && typeof window._currentRepo === 'function') {
    var r = window._currentRepo();
    if (r) repoKey = (r.owner || '') + '/' + (r.name || '');
  } else if (window._currentRepo) {
    var r2 = window._currentRepo;
    if (r2) repoKey = (r2.owner || '') + '/' + (r2.name || '');
  }
} catch (e) {}

// 修复后
var repoKey = '';
try {
  var r = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  if (r) repoKey = (r.owner || '') + '/' + (r.name || '');
} catch (e) {}
```

### 2.10 R10：index.html 版本号递增

在 `src/api/static/index.html` 中，将 7 个改动 JS 文件的 `?v=` 查询参数递增：

```html
<!-- 修复前（基线 weekly-08） -->
<script src="/static/js/api.js?v=30"></script>
<script src="/static/js/app.js?v=184"></script>
<script src="/static/js/pages/account.js?v=42"></script>
<script src="/static/js/pages/feedback.js?v=8"></script>
<script src="/static/js/pages/issues.js?v=117"></script>
<script src="/static/js/pages/repos.js?v=61"></script>
<script src="/static/js/pages/settings.js?v=33"></script>

<!-- 修复后 -->
<script src="/static/js/api.js?v=31"></script>
<script src="/static/js/app.js?v=185"></script>
<script src="/static/js/pages/account.js?v=43"></script>
<script src="/static/js/pages/feedback.js?v=9"></script>
<script src="/static/js/pages/issues.js?v=118"></script>
<script src="/static/js/pages/repos.js?v=62"></script>
<script src="/static/js/pages/settings.js?v=34"></script>
```

**设计原则**：
- 只递增有代码改动的文件，未修改的文件（如 `dashboard.js?v=98`、`pipeline.js?v=46`）版本号不变
- CSS 文件本次无改动，版本号不变
- `index.html` 本身通过 `Cache-Control: no-cache` 提供，无需版本号
- 已全部实施，T31-T37 验证通过

---

## 3. 修改点清单

| 文件 | 修改点 | 需求 |
|------|--------|------|
| `account.js` | 新增 `_accountCleanupAfterUnbind(provider)` 函数 | R1/R2/R3 |
| `account.js` | 普通解绑路径成功回调调用 `_accountCleanupAfterUnbind(provider)` | R1/R2/R3 |
| `account.js` | 确认后解绑路径成功回调调用 `_accountCleanupAfterUnbind(provider)` | R1/R2/R3 |
| `account.js` | 两条路径 `renderAccount('security')` 增加 `_currentView !== 'repos'` 守卫 | 防冲突 |
| `app.js` | 新增 `_clearCachedReposData()` 函数 | R3 |
| `app.js` | return 导出对象新增 `_clearCachedReposData` | R3 |
| `app.js` | `switchOrg` 增加 `_cachedReposData = null` | R5 |
| `app.js` | `_forceSyncRepos` 成功回调增加 `_cachedReposData = null` | R7 |
| `repos.js` | `refreshRepos` 成功回调增加 `app._clearCachedReposData()` | R7 |
| `repos.js` | `submitCreateRepo` 中 `/repos/sync` 成功回调增加 `app._clearCachedReposData()` | R7 |
| `repos.js` | `_doUnhost` 中 `/repos/remove` 成功后增加 `localStorage.removeItem('repo_member_cache')` | R4 |
| `settings.js` | `unhostFromSettings` 中 `/repos/remove` 成功后增加 `localStorage.removeItem('repo_member_cache')` | R4 |
| `issues.js` | `jumpToPipeline` repoObj 增加 `repoKey: t.repo_key` | R6 |
| `issues.js` | `_jumpToNodeEvent` repoObj 增加 `repoKey: t.repo_key` | R6 |
| `issues.js` | `_jumpToPipePage` repoObj 增加 `repoKey: t.repo_key` | R6 |
| `api.js` | `storage` 事件监听扩展处理 `repo_member_cache` 和 `user` 键 | R8 |
| `feedback.js` | `window._currentRepo` 替换为 `app.currentRepo()` | R9 |
| `index.html` | 7 个改动 JS 文件的 `?v=` 版本号递增 | R10 |

---

## 4. 影响分析

### 4.1 不受影响

| 组件 | 说明 |
|------|------|
| 后端全部 | 无改动 |
| `localStorage.user.bindings` 更新逻辑 | 不变 |
| `initSidebarRepos()` 调用 | 不变 |
| dashboard 403 自愈逻辑 | 不变 |
| OAuth 登录/密码登录/logout | 不变 |

### 4.2 新增行为

| 行为 | 触发条件 | 需求 |
|------|---------|------|
| 清理 sessionStorage.currentRepo | 解绑时 repoKey 匹配 | R1 |
| 调 loadProfile() | 解绑成功后无条件 | R2 |
| 调 goToRepos() | R1 清理了 currentRepo | R2 |
| 清理 _cachedReposData | 解绑成功后无条件 | R3 |
| 清理 repo_member_cache | 解除托管成功后 | R4 |
| 清理 _cachedReposData | 组织切换时 | R5 |
| currentRepo 补全 repoKey | issues.js 三处跳转 | R6 |
| 清理 _cachedReposData | _forceSyncRepos 成功后 | R7 |
| storage 事件扩展 | localStorage repo_member_cache/user 变化 | R8 |
| app.currentRepo() 替代 window._currentRepo | feedback.js 提交反馈 | R9 |
| 递增 ?v= 版本号 | 7 个 JS 文件改动后 | R10 |

---

## 5. 边界情况

### 5.1 currentRepo 的 repoKey 无前缀（R1/R8）

历史数据可能无前缀。`indexOf(provider + ":")` != 0 → 不清理。R8 中 `cr.repoKey.split(':')[0]` 可能得到完整的 repoKey（无冒号）→ 不在 bindings 中 → 误清理。

**R8 防御**：增加冒号存在性检查：`if (_provider && _provider !== cr.repoKey && _bindings.indexOf(_provider) < 0)`。如果 repoKey 无冒号，`_provider === cr.repoKey` → 跳过。

### 5.2 issues.js 的 t.repo_key 无前缀（R6）

`repoKey: t.repo_key` 存入无前缀值 → `_rk.indexOf('github:')` 仍为 false → 显示 GitCode 图标。与修复前行为一致，不引入新问题。

### 5.3 goToRepos 和 renderAccount 执行顺序（R2）

`_accountCleanupAfterUnbind` 同步调 `goToRepos()`（同步设置 `_currentView = 'repos'`）。随后 `if (window._currentView !== 'repos') renderAccount('security')` → 跳过。守卫可靠。

### 5.4 R8 中 loadProfile/goToRepos 不存在

`storage` 事件在页面初始化前可能触发。使用 `typeof app !== 'undefined'` 守卫。

### 5.5 R8 中 storage 事件不触发本标签页

`storage` 事件只在其他标签页修改 localStorage 时触发，不在本标签页触发。不会重复执行解绑回调中的清理逻辑。

### 5.6 R10 中未修改文件的版本号不变

只递增有代码改动的 7 个文件的 `?v=` 值。未修改的 JS 文件（如 `dashboard.js`、`pipeline.js` 等）和 CSS 文件版本号不变，避免不必要的缓存失效。

---

## 6. 测试策略

测试文件：`tests/js/test_unbind_repo_list_invisible.js`

| 用例 ID | 验证项 | 测试方法 |
|---------|--------|---------|
| T01 | account.js 存在 `_accountCleanupAfterUnbind` | 源码断言 |
| T02 | 普通解绑路径调用 `_accountCleanupAfterUnbind` | 源码断言 |
| T03 | 确认后解绑路径调用 `_accountCleanupAfterUnbind` | 源码断言 |
| T04 | `_accountCleanupAfterUnbind` 检查 repoKey 前缀匹配 | 源码断言 |
| T05 | repoKey 匹配时清除 currentRepo | 行为模拟 |
| T06 | repoKey 不匹配时不清除 | 行为模拟 |
| T07 | currentRepo 为 null 时不报错 | 行为模拟 |
| T08 | 清除后调 goToRepos | 行为模拟 |
| T09 | 未清除时不调 goToRepos | 行为模拟 |
| T10 | 无条件调 `_clearCachedReposData` | 行为模拟 |
| T11 | app.js 存在 `_clearCachedReposData` | 源码断言 |
| T12 | return 导出 `_clearCachedReposData` | 源码断言 |
| T13 | `_clearCachedReposData` 置 null | 源码断言 |
| T14 | 普通解绑路径 renderAccount 有守卫 | 源码断言 |
| T15 | 确认后解绑路径 renderAccount 有守卫 | 源码断言 |
| T16 | `_accountCleanupAfterUnbind` 在 removeItem 之后 | 源码断言 |
| T17 | `_accountCleanupAfterUnbind` 在 renderAccount 之前 | 源码断言 |
| T18 | repos.js `_doUnhost` 清 repo_member_cache（R4） | 源码断言 |
| T19 | settings.js `unhostFromSettings` 清 repo_member_cache（R4） | 源码断言 |
| T20 | app.js `switchOrg` 清 _cachedReposData（R5） | 源码断言 |
| T21 | issues.js `jumpToPipeline` 设 repoKey（R6） | 源码断言 |
| T22 | issues.js `_jumpToNodeEvent` 设 repoKey（R6） | 源码断言 |
| T23 | issues.js `_jumpToPipePage` 设 repoKey（R6） | 源码断言 |
| T24 | app.js `_forceSyncRepos` 清 _cachedReposData（R7） | 源码断言 |
| T24b | repos.js `refreshRepos` 调 `_clearCachedReposData`（R7） | 源码断言 |
| T24c | repos.js `submitCreateRepo` 调 `_clearCachedReposData`（R7） | 源码断言 |
| T25 | account.js `_accountCleanupAfterUnbind` 调 loadProfile（R2） | 源码断言 |
| T26 | api.js storage 事件处理 repo_member_cache（R8） | 源码断言 |
| T27 | api.js storage 事件处理 user/bindings（R8） | 源码断言 |
| T28 | feedback.js 使用 `app.currentRepo()`（R9） | 源码断言 |
| T29 | feedback.js 不再使用 `window._currentRepo`（R9） | 源码断言 |
| T30 | R8 中 repoKey 无前缀时不误清理（边界） | 源码断言 |
| T31 | index.html 中 api.js ?v= 值大于基线 30 | 源码断言 |
| T32 | index.html 中 app.js ?v= 值大于基线 184 | 源码断言 |
| T33 | index.html 中 account.js ?v= 值大于基线 42 | 源码断言 |
| T34 | index.html 中 feedback.js ?v= 值大于基线 8 | 源码断言 |
| T35 | index.html 中 issues.js ?v= 值大于基线 117 | 源码断言 |
| T36 | index.html 中 repos.js ?v= 值大于基线 61 | 源码断言 |
| T37 | index.html 中 settings.js ?v= 值大于基线 33 | 源码断言 |
| T38 | 未修改的 dashboard.js ?v= 值不变（基线 98） | 源码断言 |
| T39 | 未修改的 pipeline.js ?v= 值不变（基线 46） | 源码断言 |

---

## 7. 重构场景与验证

### 场景 R-1：解绑成功路径代码去重（_accountOnUnbindSuccess 提取）

**动机**：`_accountUnbindProvider` 的两条成功回调路径（普通解绑 `API.del(url).then` + 确认后解绑 `API.del(url + '?confirm=true').then`）有完全相同的后续处理代码（约 15 行重复）。

**重构方式**：提取 `_accountOnUnbindSuccess(provider, d, isZh)` 函数，两条路径都调用它。

**等价性验证**：
- 执行顺序不变：`_accountShowUnbindSuccess` → 更新 bindings → `removeItem('repo_member_cache')` → `_accountCleanupAfterUnbind` → `setTimeout(initSidebarRepos, 100)` → `_currentView !== 'repos'` 守卫 → `renderAccount('security')`
- 条件守卫不变：`if (window._currentView !== 'repos')` 原样保留
- 参数传递不变：`provider`、`d`、`isZh` 三个参数按原顺序传入

#### 用例 R-1.1：普通解绑成功路径调用 _accountOnUnbindSuccess

1. 用户解绑 GitHub（无受影响仓库）→ `API.del('/auth/bind/github')` 成功
2. `.then` 回调调用 `_accountOnUnbindSuccess('github', d, isZh)`
3. `_accountOnUnbindSuccess` 内部按原顺序执行所有清理和渲染
4. **行为与重构前完全一致**

#### 用例 R-1.2：确认后解绑成功路径调用 _accountOnUnbindSuccess

1. 用户解绑 GitHub（有受影响仓库）→ 409 → 确认 → `API.del('/auth/bind/github?confirm=true')` 成功
2. `.then` 回调调用 `_accountOnUnbindSuccess('github', d, isZh)`
3. **行为与重构前完全一致**

### 场景 R-2：解绑失败路径代码去重（_accountOnUnbindError 提取）

**动机**：两条错误路径的处理逻辑几乎相同，但条件不完全一致——普通路径只检查 `need_set_password`，确认后路径额外检查 `code === 400`。

**重构方式**：提取 `_accountOnUnbindError(e)` 函数，统一为 `need_set_password` 检查。

**等价性验证**：
- 后端 `grape_auth.py:1058-1063` 和 `settings.py:25-30` 只在 `status_code=400` + `code=400` 时返回 `need_set_password=True`
- 因此 `need_set_password` 为 true 时 `code` 必然是 400——`code === 400` 是冗余的二次确认
- 统一为 `need_set_password` 检查后，行为与原代码完全等价

#### 用例 R-2.1：普通解绑失败 → need_set_password

1. 用户仅绑定 GitHub（无密码）→ 解绑 → 后端返回 400 `{need_set_password: true}`
2. `_accountOnUnbindError(e)` → `e.detail.need_set_password` 为 true → 显示「至少保留一种登录方式」
3. **行为与重构前完全一致**

#### 用例 R-2.2：确认后解绑失败 → need_set_password

1. 用户确认后解绑 → 后端返回 400 `{code: 400, need_set_password: true}`
2. `_accountOnUnbindError(e)` → `e.detail.need_set_password` 为 true → 显示「至少保留一种登录方式」
3. 原代码额外检查 `code === 400`——但 `need_set_password` 为 true 时 `code` 必然是 400
4. **行为与重构前完全一致**

#### 用例 R-2.3：解绑失败 → 其他错误

1. 用户解绑 → 后端返回 500
2. `_accountOnUnbindError(e)` → `e.detail.need_set_password` 不存在 → 显示 `e.message` 或「操作失败」
3. **行为与重构前完全一致**

### 场景 R-3：_accountCleanupAfterUnbind 未重构（保持原样）

**动机**：该函数被测试通过 `new Function()` 提取函数体执行行为模拟（T05-T10/T25）。任何内部改动（如引入 `_safeCall` 辅助函数、改变 `typeof app` 守卫写法）都会破坏行为模拟测试。

**决策**：保持 `_accountCleanupAfterUnbind` 内部逻辑完全原样，不重构。

#### 用例 R-3.1：行为模拟测试验证

1. 测试 `runCleanup('github', {repoKey: 'github:octocat/my-repo'})` 提取函数体执行
2. 模拟环境中 `sessionStorage` / `app._clearCachedReposData` / `app.loadProfile` / `app.goToRepos` 被正确调用
3. `typeof app !== 'undefined' && app._clearCachedReposData` 守卫原样保留
4. **32/32 测试通过，行为模拟未被破坏**

---

## 8. 重构验证清单

| 验证项 | 场景 | 预期结果 |
|--------|------|----------|
| RV1 | 普通解绑成功 → _accountOnUnbindSuccess 内部执行顺序 | 与重构前完全一致（T16/T17 验证顺序） |
| RV2 | 确认后解绑成功 → _accountOnUnbindSuccess | 与重构前完全一致（T03 验证调用） |
| RV3 | 解绑失败 need_set_password → _accountOnUnbindError | 显示「至少保留一种登录方式」（T25 验证 loadProfile） |
| RV4 | 解绑失败其他错误 → _accountOnUnbindError | 显示错误消息 |
| RV5 | _accountCleanupAfterUnbind 内部逻辑 | 未重构，行为模拟测试 T05-T10 全部通过 |
| RV6 | _currentView !== 'repos' 守卫 | 原样保留（T14/T15 验证） |
| RV7 | 全部 41 个测试 | 41 passed, 0 failed |
