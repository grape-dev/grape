# Issue #63：页面刷新"signal is aborted without reason"错误修复

> 日期：2026-09-08
> 分支：fix/issue-63-abort-error-silent（从 weekly-02 拉取，rebase 到最新）

---

## 一、错误原因分析

### 1.1 现象

- 页面刷新时频繁出现"加载失败: signal is aborted without reason"
- 切 tab 后页面卡在"加载中"或"加载失败"
- 侧边栏菜单莫名消失
- repos 页面内容渲染到侧边栏区域
- "加载失败: Unexpected token '<'"（token 过期后 API 返回 HTML 登录页）

### 1.2 根因

| 问题 | 根因 |
|------|------|
| AbortError 显示"加载失败" | `_fetchWithTimeout` 为每个请求创建 AbortController，浏览器刷新时中止请求触发 AbortError，各页面 catch 统一渲染为"加载失败" |
| 切 tab 卡死 | 旧请求回调通过闭包持有 `pageContent` 引用，回调执行时覆盖新页面 DOM |
| 侧边栏消失 | `loadRepos` 失败时 catch 隐藏 sidebar（`display:none`），浏览器刷新 abort 表现为 TypeError 绕过 AbortError 判断 |
| repos 渲染到侧边栏 | `nav('repos')` 走 `loadRepos` → `renderReposFromData` 异步回调操作 sidebar/tb/mainView style，无 stale guard |
| Unexpected token '<' | token 过期后 nginx 返回 HTML 登录页，`r.json()` 解析 HTML 报错 |
| 定时轮询切走不停 | observability/chat/tasks 的 `setInterval` 在切走时没清理 |
| 401 refresh 清 token | `doRefresh` catch 把 abort/网络错误/HTTP 错误混在一起，全部清 token 跳登录 |

### 1.3 为什么切 tab 会卡死

1. 用户点击菜单进入页面 A，`pc.innerHTML = '加载中'`
2. 请求发出但还没返回
3. 用户切换到页面 B
4. 旧请求回调执行 `pc.innerHTML = '加载失败'` 或渲染页面 A 内容
5. 覆盖了页面 B 的 DOM
6. 页面 B 卡在错误状态

根因：旧请求的回调通过闭包持有 `pc`（`document.getElementById('pageContent')`）引用。`pc` 指向的 DOM 元素不会因为 `innerHTML` 被替换而改变——旧回调直接写入当前 DOM。

Vue 等框架通过组件卸载解决——组件卸载后 `this` 失效，数据更新自动忽略。原生 JS 没有组件生命周期，需要手动检查。

---

## 二、修复方案

### 2.1 方案：stale guard + AbortController + null guard 三层防护

| 层 | 职责 | 实现 |
|----|------|------|
| AbortController | 切 tab/页面时主动取消旧请求 | `window._navSignal` / `window._tabSignal` |
| stale guard | 旧回调检查 `_currentView` 是否还有效 | `if (window._currentView !== 'xxx') return` |
| null guard | 异步回调操作内部 DOM 时检查 null | `var el = document.getElementById('xxx'); if (!el) return` |

### 2.2 实际改动文件清单

| 文件 | 改动内容 |
|------|---------|
| **api.js** | `_fetchWithTimeout` 合并全局 `_navSignal`/`_tabSignal`（读操作合并，写操作 `_skipNavSignal=true` 跳过）；超时 abort 标记 `controller._timedOut`；外部 abort 标记 `e.silent=true`；`doRefresh` catch 区分 AbortError/TypeError/HTTP 错误；`handle401` catch 同样区分；`handleResp` 检查 Content-Type，HTML 响应按 session expired 处理 |
| **app.js** | `nav()` 维护 `window._navAbortController` + `window._navSignal` + `window._currentView`；切走时停 observability/chat/tasks timer；恢复 sidebar/tb/mainView 布局（离开 repos 时）；`loadRepos` catch 不隐藏侧边栏；`loadRepos` then 检查 `_currentView` 已设时只更新缓存不渲染；`goToRepos` stale guard；`nav('repos')` 走 `goToRepos`；loadDash stale guard + AbortError 静默；pipe/agent/event/runtimes/skill/skill-sub 回调 stale guard |
| **skills.js** | `_switchSkillTab` 设 `_tabAbortController` + `_currentView`；`_reloadSkills` 回调 stale guard；`loadSubSkillDetail` 回调 stale guard + null guard |
| **scene-market.js** | `_switchSceneTab` 设 `_tabAbortController` + `_currentView`；`_loadSceneMarketList`/`_loadCustomScenes` 回调 stale guard |
| **chat.js** | `_stopOnboardPoll` 导出；`renderChat` 恢复轮询；`loadConversations`/`newConversation`/`_doDeleteConv` 回调 null guard |
| **dashboard.js** | 3 处 `renderDashboard` 回调 stale guard |
| **knowledge.js** | 列表加载 + 内容加载回调 stale guard |
| **tasks.js** | 任务列表加载回调 stale guard |
| **repos.js** | 删除仓库回调 stale guard |
| **settings.js** | 删除仓库回调 stale guard；`loadCredentials`/`loadGitcodeRepo`/`loadPipelineMode` 回调 null guard |
| **runtimes.js** | refresh 回调 stale guard |
| **index.html** | 升版本号：api.js?v=22, app.js?v=61, dashboard.js?v=48, chat.js?v=32, skills.js?v=35, scene-market.js?v=29, knowledge.js?v=10, tasks.js?v=51, repos.js?v=33, settings.js?v=15, runtimes.js?v=30 |

### 2.3 核心代码

**api.js — `_fetchWithTimeout` 合并全局 signal**

```javascript
function _fetchWithTimeout(url, opts, timeout, _retried, _skipNavSignal) {
  const controller = new AbortController();
  const timer = setTimeout(() => { controller._timedOut = true; controller.abort(); }, timeout || _DEFAULT_TIMEOUT);
  if (!_skipNavSignal) {
    var navSignal = window._navSignal;
    var tabSignal = window._tabSignal;
    if (navSignal) { if (navSignal.aborted) controller.abort(); else navSignal.addEventListener('abort', () => controller.abort()); }
    if (tabSignal) { if (tabSignal.aborted) controller.abort(); else tabSignal.addEventListener('abort', () => controller.abort()); }
  }
  opts = Object.assign({}, opts, { signal: controller.signal });
  return fetch(url, opts).finally(() => clearTimeout(timer)).catch(e => {
    if (e.name === 'AbortError') {
      if (controller._timedOut) throw e;  // 超时，正常显示错误
      e.silent = true; throw e;            // 外部取消，静默
    }
    if (!_retried && e instanceof TypeError) {
      return new Promise(r => setTimeout(r, 500)).then(() => _fetchWithTimeout(url, opts, timeout, true, _skipNavSignal));
    }
    throw e;
  });
}
// 读操作合并 signal
function _get(path, timeout) {
  return _fetchWithTimeout(BASE + path, {headers: headers()}, timeout, false, false)...
}
// 写操作跳过 signal
function _post(path, body, timeout) {
  return _fetchWithTimeout(BASE + path, {method: 'POST', ...}, timeout, false, true)...
}
```

**app.js — `nav()` 维护全局状态 + 清理 timer**

```javascript
// nav() 开头
if (window._navAbortController) window._navAbortController.abort();
window._navAbortController = new AbortController();
window._navSignal = window._navAbortController.signal;
window._tabSignal = null;
window._currentView = view;
// 离开 repos 时恢复布局
if (view !== 'repos') {
  $('sidebar').style.display = 'flex';
  $('tb').style.display = 'flex';
  $('mainView').style.position = ''; $('mainView').style.inset = ''; $('mainView').style.zIndex = '';
}
// 停定时轮询
if (view !== 'tasks' && typeof _stopScenePoll === 'function') _stopScenePoll();
if (view !== 'observability' && typeof stopObsAuto === 'function') stopObsAuto();
if (view !== 'chat' && typeof _stopOnboardPoll === 'function') _stopOnboardPoll();
```

**页面回调 stale guard**

```javascript
// 操作 pageContent 的回调
API.get('/skills').then(d => {
  if (window._currentView !== 'skills' && window._currentView !== 'skills:' + _skillTab) return;
  renderSkills(d);
}).catch(e => {
  if (e.name === 'AbortError' && e.silent) return;  // abort 静默
  if (window._currentView !== 'skills' && window._currentView !== 'skills:' + _skillTab) return;
  showError(e);
});

// 操作内部 DOM 的回调——null guard
API.get('/settings/credentials').then(d => {
  var _sc = document.getElementById('sCredentials');
  if (!_sc) return;
  _sc.innerHTML = ...;
});
```

### 2.4 防护规则

| 回调操作对象 | 防护方式 | 理由 |
|-------------|---------|------|
| `pageContent` / `pc.innerHTML` | stale guard（`_currentView`） | 覆盖整个页面内容，必须检查当前页面 |
| sidebar/tb/mainView style | stale guard | 改变页面布局，切走后不该执行 |
| 页面内部 DOM（sCredentials/chatBody 等） | null guard | 切走后 DOM 不存在，`getElementById` 返回 null |
| modal DOM（挂在 body 上） | 不需要 | modal 不在 pageContent 里，切 tab 不移除 |
| 闭包持有的旧 DOM 引用 | 不需要 | 写入已移除元素不报错，不影响当前页面 |
| 写操作（API.post/put/del） | `_skipNavSignal=true` | 用户点了操作不应因切 tab 取消，showToast 始终执行 |

### 2.5 stale guard 放置位置

stale guard 只放在操作 `pageContent`（`pc.innerHTML` / `renderXxx()`）的前面，不放在 `.then` / `.catch` 开头。`showToast` / `modal.remove` / `btn.disabled` 等非 pageContent 操作始终执行。

```javascript
API.del('/skills/by-id/' + skillId).then(r => {
  modal.remove();                                    // 始终执行
  if (app && app.showToast) app.showToast(...);      // 始终执行
  if (window._currentView !== 'skills') return;      // stale guard
  renderSkills();                                    // 只在当前页面时刷新
}).catch(e => {
  btn.disabled = false;
  if (app && app.showToast) app.showToast(...);      // 始终执行
});
```

---

## 三、对系统现有能力的影响

| 现有能力 | 是否受影响 | 证据 |
|---------|-----------|------|
| 页面数据加载 | ✅ 改善 | 切 tab 时旧请求被取消，不覆盖新页面 |
| 写操作（创建/删除/更新） | ❌ 不受影响 | `_post`/`_put`/`_del` 用 `_skipNavSignal=true` 跳过 signal，9 个写操作调用点均未传 signal |
| Dashboard 30s 定时刷新 | ✅ 改善 | 切走停 timer + abort，页面刷新时静默不报错 |
| 全局初始化（profile/repos） | ✅ 改善 | `loadRepos` catch 不隐藏侧边栏 |
| Select2 AJAX 搜索 | ❌ 不受影响 | 走 `$.ajax` 不走 `API.get`/`_fetchWithTimeout` |
| 401 refresh | ✅ 改善 | abort/网络错误不清 token，只有 HTTP 401/403 才跳登录 |
| observability 轮询 | ✅ 改善 | 切走停 timer（35 秒内 0 个 API 调用已验证） |
| chat 飞书接入轮询 | ✅ 改善 | 切走停 timer + 切回恢复 |
| 技能点赞/场景点赞 | ❌ 不受影响 | 写操作走 `API.post` 不传 signal |
| 页面内 tab 切换 | ✅ 改善 | 独立 `_tabAbortController`，不影响 nav 级别 |
| 真实网络错误显示 | ❌ 不受影响 | TypeError `e.name='TypeError'`，HTTP 错误 `e.name='Error'`，不匹配 `'AbortError'` |
| 超时错误显示 | ❌ 不受影响 | 超时 abort 标记 `controller._timedOut=true`，API 层 `throw e` 不标记 silent |

---

## 四、验收标准

- [x] 页面刷新时不再出现"加载失败: signal is aborted without reason"
- [x] 切 tab 时旧回调不覆盖新页面（stale guard 生效）
- [x] 30s 定时刷新失败不覆盖页面
- [x] 真实网络错误（超时、5xx）仍正常显示错误提示
- [x] 切 tab 时不会卡死，切回后正常加载
- [x] 侧边栏菜单不再莫名消失
- [x] repos 页面内容不再渲染到侧边栏区域
- [x] "Unexpected token '<'" 不再出现（Content-Type 检查）
- [x] 所有定时轮询切走时停止（observability/chat/tasks/dashboard）
- [x] 401 refresh 被 abort 时不清 token 不跳登录
- [x] 页面内 tab 切换（official/repo/user）也取消旧请求
- [x] 写操作不被 nav abort 取消
- [x] 所有异步回调操作 DOM 都有 stale guard 或 null guard

---

## 五、Playwright 验证结果

10/10 PASS：
1. 全页面快速切换（16 个页面 0.3s 间隔）→ 无 loading/error/侧边栏丢失
2. 技能 tab 切换（official→repo→user→official）→ 20 条技能正常加载
3. 场景 tab 切换（official→repo→official）→ 10 个卡片正常
4. repos 刷新 → sidebar=flex, view=dash 正常
5. chat 切走切回 → chatBody 存在
6. settings 切走切回 → sCredentials 存在
7. 反复快速切换（15 次 0.15s 间隔）→ 无 loading/error/侧边栏丢失
8. 无 JS 错误（0 个 PAGEERROR）
9. navSignal 存在
10. Dashboard 正常（content=14157, loading=False）

定时器验证：
- observability 轮询切走停止（timer 47 → null）
- tasks 轮询切走停止（timer 67 → null）
- Dashboard 30s 定时刷新切走停止（35 秒内 0 个 API 调用）
