# Issue #63：页面刷新"signal is aborted without reason"错误修复

> 日期：2026-09-08
> 关联 Issue：GitCode #63 — 经常页面刷新出现"加载失败: signal is aborted without reason"
> 分支：fix/issue-63-abort-error-silent（从 weekly-02 拉取）

---

## 一、错误原因分析

### 1.1 现象

页面刷新时频繁出现"加载失败: signal is aborted without reason"错误提示，页面被错误内容覆盖，看起来像"卡住"。

### 1.2 根因

前端 `_fetchWithTimeout`（`src/api/static/js/api.js:178-189`）为每个 API 请求创建 AbortController 并注入 signal。页面刷新时浏览器自动中止所有进行中的 fetch 请求，触发 AbortError（message: `signal is aborted without reason`）。前端各页面 `.catch` 统一将错误显示为"加载失败: {e.message}"，未区分 AbortError 与真实网络/服务端错误。

### 1.3 为什么页面会"卡住"

请求是并行的，不是串行。卡住的原因是：

1. 页面打开，同时发多个请求（用户信息、仓库列表、Dashboard 数据等）——并行的
2. 用户按刷新（F5）
3. 浏览器刷新时**自动杀掉**所有还没返回的请求
4. 被杀掉的请求触发 AbortError（"signal is aborted without reason"）
5. 各页面的 catch 块把这个错误**渲染到页面上**——显示"加载失败"
6. 页面被错误内容覆盖了，看起来像"卡住"了

### 1.4 高频触发场景

1. Dashboard 30s 定时刷新（`setInterval(loadDash, 30000)`），刷新时正好有请求进行中
2. 页面初始化并发加载（`showApp()` 中 loadProfile + loadRepos，同时 `init()` hash 导航触发 nav() 发起额外请求）
3. `app.nav()` 切 tab 时，旧请求没被取消，旧回调覆盖新页面

### 1.5 30s 定时刷新是什么

Dashboard 页面（`app.nav('dash')`，第 302-322 行）每 30 秒自动刷新一次：
- 并行请求 `/dashboard/{owner}/{repo}` + `/scenes?repo=...`
- 拿到最新数据后重新渲染 Dashboard（任务状态、场景变更等）
- 目的：让用户看到实时状态，不用手动刷新
- 离开 Dashboard 时 `clearInterval(_refreshTimer)` 停止（第 290 行）

这个定时刷新是 AbortError 的高频触发源——30 秒到了发请求，如果用户正好刷新页面或切 tab，请求被浏览器中止。

### 1.6 为什么 30s 后恢复

Dashboard 的 `setInterval(loadDash, 30000)` 仍然在运行——30 秒后下次刷新会重新发请求，如果这次没有被浏览器中止（因为用户没有再刷新），页面就恢复正常了。所以"卡住"持续约 30 秒。

但如果是非 Dashboard 页面（如技能页、场景页）被 abort 卡住，没有定时刷新机制——只能用户手动重新进入页面才能恢复。

### 1.7 更严重的场景：切 tab 也卡死

用户操作流程：
1. 点击菜单进入某页面，第 288 行 `pc.innerHTML = '加载中'`
2. 请求发出但还没返回
3. 用户切换菜单（或刷新页面）
4. 旧请求没被取消（切 tab 不卸载页面，浏览器不自动 abort）
5. 旧请求响应回来（或报错），catch 回调执行 `pc.innerHTML = '加载失败'`
6. 覆盖了新页面的"加载中"状态
7. 页面一直停留在"加载失败"或"加载中"状态，切 tab 也没用

根因：旧请求的回调通过闭包持有 `pc`（`document.getElementById('pageContent')`）的引用。`pc` 指向的 DOM 元素不会因为 `pc.innerHTML` 被替换而改变——所以旧回调的 `pc.innerHTML = '错误'` 会直接覆盖新页面的内容。这是**竞态条件**——旧回调可能在新页面渲染之后执行。

### 1.8 闭包问题详解

```javascript
// app.js nav() 里
var pc = document.getElementById('pageContent');
pc.innerHTML = '<div class="loading">加载中</div>';

API.get('/skills').then(data => {
  // 这个闭包持有 pc 的引用
  // pc 指向的 DOM 元素没变（只是 innerHTML 被新页面替换了）
  pc.innerHTML = renderSkills(data);  // 如果用户已切 tab，这里覆盖新页面
}).catch(e => {
  pc.innerHTML = '加载失败: ' + e.message;  // 同样覆盖新页面
});
```

切 tab 时 `pc` 指向的 DOM 元素不变，`pc.innerHTML` 直接写入当前 DOM。旧回调和新回调在抢同一个 DOM 元素。

Vue 等框架通过组件卸载解决——组件卸载后 `this` 失效，数据更新自动忽略。原生 JS 没有组件生命周期，需要手动检查。

---

## 二、修复方案

### 2.1 方案：stale closure guard + AbortError 静默

这是业界标准的"过时闭包防护"模式。React 的 `useEffect` cleanup、Vue 的 `onUnmounted`、iOS 的 `weak self` 都是同一原理——旧回调检查自己是否还有效。

但 stale guard 只管回调不管请求——旧请求仍在后台发到服务器，浪费带宽和服务器资源。Vue 组件卸载时会主动 abort 请求，不仅回调不执行，请求本身也被取消。

正确做法：**stale guard + AbortController 结合**——主动取消请求 + 回调兜底。

**原理**：
1. `app.nav()` / 页面内 tab 切换时，主动 abort 旧请求（像 Vue 组件卸载一样），请求不发到服务器或在传输中被取消
2. 回调里加 stale guard 作为兜底（万一 abort 没拦住，回调仍然不操作 DOM）
3. API 层 AbortError 标记 silent，不显示错误

### 2.2 改动点

**第一处：API 层支持外部 AbortController**

- 文件：`src/api/static/js/api.js`
- 位置：`_fetchWithTimeout` 函数 + `_get` / `_post` / `_put` / `_del`
- 改动：
  1. `_fetchWithTimeout` 接受可选的 `externalSignal` 参数，与超时 controller 合并
  2. 超时 abort 前设 `controller._timedOut = true`
  3. catch 里区分：超时 abort 抛 TimeoutError（显示错误），外部 abort 标记 `e.silent = true`
  4. `_get` / `_post` / `_put` / `_del` 接受可选 signal 参数透传

```javascript
function _fetchWithTimeout(url, opts, timeout, _retried, externalSignal) {
  const controller = new AbortController();
  const timer = setTimeout(() => { controller._timedOut = true; controller.abort(); }, timeout || _DEFAULT_TIMEOUT);
  // 合并外部 signal：外部 abort 时也 abort 内部
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener('abort', () => controller.abort());
  }
  opts = Object.assign({}, opts, { signal: controller.signal });
  return fetch(url, opts).finally(() => clearTimeout(timer)).catch(e => {
    if (e.name === 'AbortError') {
      if (controller._timedOut) throw e;  // 超时，正常显示错误
      e.silent = true; throw e;  // 外部取消，静默
    }
    if (!_retried && e instanceof TypeError) {
      return new Promise(r => setTimeout(r, 500)).then(() => _fetchWithTimeout(url, opts, timeout, true, externalSignal));
    }
    throw e;
  });
}
```

**第二处：app.js 维护全局 AbortController + 页面标识**

- 文件：`src/api/static/js/app.js`
- 位置：`app.nav()` 函数（第 288 行附近）
- 改动：
  1. 全局 `var _navAbortController = null;` `var _currentView = '';`
  2. nav 开头：`if (_navAbortController) _navAbortController.abort();` 取消旧请求
  3. 创建新 controller：`_navAbortController = new AbortController();`
  4. 设标识：`_currentView = view;`
  5. 把 `_navAbortController.signal` 传给页面请求

**第三处：页面内 tab 切换也更新标识 + 取消旧请求**

- 文件：scene-market.js（`_switchSceneTab`）、skills.js（`_switchSkillTab`）
- 改动：切 tab 时调 `app.abortNav()` 取消旧请求 + 设 `_currentView = 'scene-market:' + tab`

**第四处：各页面回调加 stale guard 兜底**

- 文件：各页面 JS
- 改动：`.then` / `.catch` 开头加 `if (_currentView !== 'xxx') return;`
- 目的：万一 abort 没拦住（比如请求已经到了服务器正在处理），回调仍然不操作 DOM

```javascript
// skills.js
API.get('/skills').then(data => {
  if (_currentView !== 'skills') return;  // 用户已切走，丢弃
  renderSkills(data);
}).catch(e => {
  if (_currentView !== 'skills') return;  // 同上
  showError(e);
});
```

### 2.3 需要改的文件清单

| 文件 | 改动 | 说明 |
|------|------|------|
| api.js | 1 行 | AbortError 标记 silent |
| app.js | 1 行 | nav 里存 `_currentView` |
| dashboard.js | 2 处 | loadDash 的 .then + .catch |
| skills.js | 2 处 | _reloadSkills 的 .then + .catch |
| scene-market.js | 2 处 | _loadSceneMarket + _loadCustomScenes 的 .then + .catch |
| repos.js | 1 处 | loadRepos 的 .then |
| chat.js | 1 处 | pollOnboardStatus 的 .then |

### 2.4 为什么 stale guard + AbortController 结合

纯 stale guard（只忽略回调，不取消请求）的问题：
- 旧请求仍在后台发到服务器，浪费带宽和服务器资源
- 服务器处理了无效请求，浪费 CPU/DB 资源
- 和 Vue 组件卸载的行为不一致

纯 AbortController（只取消请求，不检查回调）的问题：
- 需要 controller 生命周期管理，复杂度高
- 万一 abort 没拦住（请求已到服务器正在处理），回调仍会执行
- 边界条件多：超时/浏览器/导航 abort 需要区分

结合方案：AbortController 主动取消请求（从根源止损）+ stale guard 兜底（回调不操作 DOM）。两层各司其职：
- AbortController 解决"请求不被取消"的问题
- stale guard 解决"万一回调还是执行了"的问题
- API 层区分超时 abort（显示错误）和外部 abort（静默）

### 2.5 不影响的

- CSS / HTML 结构不变
- 后端不变
- Select2 / jQuery 不变
- 不引入新依赖
- 现有功能逻辑不变，只加判断行
- 全局初始化请求（profile/repos/agentTypes）不受影响

### 2.6 改动对系统的影响分析

#### 影响点 1：旧请求被主动取消，不浪费带宽

**影响**：切 tab/切页面时主动 abort 旧请求，请求在传输中被取消，不发到服务器或服务器收到 RST 后丢弃。不浪费带宽和服务器资源。

**理由**：和 Vue 组件卸载时 `onUnmounted` + `controller.abort()` 行为一致。stale guard 作为兜底——万一 abort 没拦住（请求已到服务器正在处理），回调仍然不操作 DOM。两层结合，既取消请求又保证回调安全。

#### 影响点 2：observability.js 的定时轮询切走后未停止

**影响**：observability.js 有 `setInterval(refreshObs, 10000)`（第 63 行），app.js 切走 observability 页面时没有调 `stopObsAuto()` 清理——定时轮询在后台继续跑，持续发请求。

**理由**：不可接受。和 Dashboard 30s 定时刷新同样的问题——切走后 timer 不停就是 bug。`app.nav()` 切走 observability 时必须调 `stopObsAuto()` 清理。同理，chat.js 的 `_onboardTimer` 也需要在切走时清理。

**修复**：`app.nav()` 里加：
```javascript
if (view !== 'observability' && typeof stopObsAuto === 'function') stopObsAuto();
if (view !== 'chat' && typeof _stopOnboardPoll === 'function') _stopOnboardPoll();
```

#### 影响点 3：chat.js 的会话消息轮询切走后未停止

**影响**：chat.js 有 `pollOnboardStatus`（`setInterval`）和消息加载请求（`API.get('/chat/messages/...')`），app.js 切走 chat 页面时没有清理——轮询在后台继续跑，消息加载请求的回调操作已不存在的 DOM（`document.getElementById('chatBody')` 返回 null），会报 `Cannot set property 'innerHTML' of null` 错误。

**理由**：不可接受。切走时 `pc.innerHTML = '加载中'` 把 chat 的 DOM 全清了，旧请求回调操作已不存在的 DOM 会报 JS 错误。请求本身也在后台浪费资源。和影响点 2 同样的问题。

**修复**：在影响点 2 的修复中一并处理——app.nav 切走 chat 时停 `_onboardTimer` + abort 请求。chat 的消息加载请求通过 AbortController 被取消，回调不执行。

**切回来功能是否正常**：`pollOnboardStatus` 不是页面加载时自动启动的——是用户点了"飞书接入"按钮后才启动（第 795 行）。切走时停 timer 不影响功能，飞书接入任务在服务器端继续跑。但切回来时如果用户正在接入过程中（`_onboardTaskId` 非空），需要自动恢复轮询——否则用户看不到接入进度。

**修复**：`renderChat` 末尾加恢复逻辑：
```javascript
// 切回来时如果有进行中的接入任务，恢复轮询
if (_onboardTaskId) pollOnboardStatus();
```

#### 影响点 4：`loadProfile` / `loadRepos` / `loadAgentTypes` 全局请求

**影响**：这些在 `showApp()` 里调用，不通过 `app.nav()`。`showApp()` 有 3 个调用点：登录成功后（第 448 行）、OAuth 回来时（第 513 行）、**页面刷新时 token 还在（第 537 行）**。页面刷新时 `showApp()` + `init()` 同时执行，`loadProfile` + `loadRepos` + `nav()` 的请求并行发出，可能被浏览器 abort。

**理由**：不能说"影响不大"。`loadRepos` 无缓存时被 abort，catch 执行隐藏侧边栏（`$('sidebar').style.display = 'none'`），此时 `init()` 的 hash 导航 `nav()` 已经触发并试图渲染页面内容——侧边栏没了，页面布局残缺。用户看到的是没有侧边栏的空白页面，不是"看不到"。如果刷新因网络慢/服务器响应慢而延迟，这个残缺状态会持续更久。

**修复**：`loadRepos` 的 catch 加 `if (e.name === 'AbortError') return`，abort 时不隐藏侧边栏、不做任何 DOM 操作。`loadProfile` 的 catch 已为空（第 69 行），不需要改。

**实际现象**：用户页面刷新时莫名看到左侧菜单消失，就是这个引起的。触发条件：仓库列表无缓存（首次登录、清了浏览器缓存、换了设备/浏览器、网络慢时刷新）。有缓存时 catch 走第 100 行 `if (cachedRepos) return` 静默返回不隐藏侧边栏；无缓存时 catch 执行 `$('sidebar').style.display = 'none'` 导致侧边栏消失。

**修复后**：abort 时直接 return，不执行隐藏侧边栏的代码，侧边栏保持不变。这里不需要 stale guard（`loadRepos` 不走 `app.nav()`，没有 `_currentView` 可比对），只需要 API 层 AbortError 静默 + catch 判断 `e.name === 'AbortError'`。

#### 影响点 5：Dashboard 30s 定时刷新的 abort 处理

**影响**：两种场景：

场景 A（切 tab）：用户在 Dashboard 切到其他页面，`clearInterval` 已停（第 290 行），不会再发新请求。但最后一次定时请求可能还在路上——stale guard（`_currentView !== 'dash'`）忽略它的回调，不覆盖新页面。合理。

场景 B（页面刷新）：用户在 Dashboard **没切走**，30s 定时刷新发请求时用户按了 F5 刷新页面。请求被浏览器 abort → AbortError → catch 执行 `pc.innerHTML = '加载失败'`。此时 `_currentView` 还是 'dash'，stale guard 认为回调有效，拦不住——页面显示错误。

**理由**：stale guard 只能拦截"切 tab"场景，拦不住"页面刷新"场景。页面刷新时 `_currentView` 不变，stale guard 认为回调还有效。必须靠 API 层 AbortError 静默（`e.silent = true`）+ catch 判断 `e.silent` 不渲染错误。

**纯前端改动**：`e.silent = true` 是前端 JavaScript 给 Error 对象加属性，不需要后端配合。整个流程：浏览器 abort 请求 → 前端 `_fetchWithTimeout` catch 捕获 → 前端打标记 `e.silent = true` → 前端页面 catch 判断 `e.silent` return。后端 API 接口/代码/响应不需要任何改动。

两层各管各的场景：
- stale guard 管"切 tab"——旧回调不该操作新页面
- `e.silent` 管"页面刷新"——被浏览器 abort 的请求不该显示错误

**修复**：`loadDash` 的 catch 加 `if (e.name === 'AbortError') return`（或判断 `e.silent`）。两层配合：
- 切 tab 场景：stale guard 拦截
- 页面刷新场景：API 层静默 + catch 判断拦截

#### 影响点 6：页面内 tab 切换（_switchSceneTab / _switchSkillTab）

**影响**：页面内 tab 切换也会发请求，旧请求的回调同样会覆盖新 tab 内容。和 `app.nav()` 级别是同一个问题，根因一样。

**理由**：不能推迟。如果只修 nav 级别不修页面内，用户在场景页切 official/repo tab 时仍然会卡死。本次一并解决。

**修复**：`_currentView` 存完整路径（如 `scene-market:official` / `skills:user`），页面内 tab 切换时也更新标识。回调检查时比对完整路径：

```javascript
// _switchSceneTab('repo')
_currentView = 'scene-market:repo';

// 回调里
if (_currentView !== 'scene-market:repo') return;
```

#### 影响点 7：401 refresh 连锁——清 token 跳登录过于激进

**影响**：`doRefresh` 的 catch（第 98-102 行）注释写"超时/abort/网络错误 → 清 token 跳登录页"——四种不同情况混在一起处理，全部清 token 跳登录：

1. **超时 abort**（refresh 10s 超时）：token 可能还有效（只是网络慢），不应该清 token
2. **浏览器 abort**（页面刷新）：token 可能还有效，不应该清 token。页面马上重新加载，如果清了 token 用户要重新登录
3. **网络错误**（TypeError）：refresh 发不出去，token 可能还有效，不应该清 token
4. **refresh 真的失败**（HTTP 401/403）：refresh_token 过期，**这种才应该**清 token 跳登录

**理由**：当前代码把 1/2/3 和 4 混在一起——任何错误都清 token 跳登录。页面刷新时 refresh 被 abort → token 被清 → 重新加载后用户被登出。网络抖动 → refresh 超时 → 用户被登出。这比 AbortError 显示错误更严重——用户直接丢失登录状态。

**修复**：`doRefresh` 的 catch 区分错误类型：
```javascript
}).catch(e => {
  if (e.name === 'AbortError') {
    // 浏览器 abort/超时——不清 token，不跳登录，静默 return
    throw e;
  }
  if (e instanceof TypeError) {
    // 网络错误——不清 token，不跳登录
    throw e;
  }
  // refresh 真的失败（HTTP 401/403）——清 token 跳登录
  localStorage.removeItem('token');
  if (location.pathname !== '/login') location.href = '/login';
  throw e;
});
```

**注意**：`doRefresh` 用的是自己的 `fetch` + `AbortController`（第 78-79 行），不走 `_fetchWithTimeout`。需要单独处理 AbortError 判断。

#### 影响点 8：写操作回调被拦截——showToast 和 modal.remove 会受影响

**影响**：写操作（删除技能、转换技能、创建场景等）的 `.then` 回调里通常包含三类操作：
1. `modal.remove()`——关闭弹出层
2. `showToast(...)`——显示成功提示
3. `renderSkills()` / `renderSceneMarket()`——刷新页面列表

如果 stale guard 加在 `.then` 开头（`if (_currentView !== 'xxx') return`），三类操作全部不执行——弹窗不关闭、用户不知道操作成功、列表不刷新。

**理由**：不合理。`modal.remove()` 和 `showToast` 操作的不是 `pageContent`，是全局 DOM（弹窗挂在 body 上，toast 是全局组件）。不应该被 stale guard 拦截。只有 `renderSkills()` 等操作 `pageContent` 的才应该拦截。

**修复**：stale guard 不能加在 `.then` 开头，要加在操作 `pageContent` 的那行前面：

```javascript
API.del('/skills/by-id/' + skillId).then(r => {
  modal.remove();                                    // 始终执行——关闭弹窗
  if (app && app.showToast) app.showToast(...);      // 始终执行——提示用户
  if (_currentView !== 'skills') return;              // stale guard——用户切走了不刷新列表
  renderSkills();                                    // 只在当前页面时刷新
}).catch(e => {
  btn.disabled = false;
  btn.textContent = ...;
  if (app && app.showToast) app.showToast(...);      // 始终执行——提示失败
});
```

**规则**：stale guard 只放在操作 `pageContent`（`pc.innerHTML` / `renderXxx()`）的前面，不放在 `.then` / `.catch` 开头。`showToast` / `modal.remove` / `btn.disabled` 等非 pageContent 操作不受 stale guard 影响。

#### 影响点 9：signal 透传链路——用全局变量而非参数透传

**影响**：`_get` / `_post` / `_put` / `_del` 的函数签名是 `_get(path, timeout)`——没有 signal 参数。如果改签名加 signal 参数，现有所有调用点（`API.get('/skills')` 等几十处）都要改加 signal 参数，改动量大。

**理由**：不合理。改签名方案改动量大且容易遗漏。改用全局变量方案——`_fetchWithTimeout` 内部读 `window._navSignal`，自动合并。现有调用点不用改。

**修复**：`_fetchWithTimeout` 内部读全局变量：
```javascript
function _fetchWithTimeout(url, opts, timeout, _retried) {
  const controller = new AbortController();
  const timer = setTimeout(() => { controller._timedOut = true; controller.abort(); }, timeout || _DEFAULT_TIMEOUT);
  // 合并全局 nav signal（切 tab 时 abort）
  var navSignal = window._navSignal;
  if (navSignal) {
    if (navSignal.aborted) controller.abort();
    else navSignal.addEventListener('abort', () => controller.abort());
  }
  // ...
}
```

这样所有走 `API.get` / `API.post` 的请求自动合并全局 signal——不需要改调用点。nav 切 tab 时设 `window._navSignal = newController.signal`，所有进行中的请求自动被 abort。

但写操作也不应该被 abort——写操作走 `API.post` / `API.put` / `API.del`，也会读 `window._navSignal`。需要区分：读操作合并 signal，写操作不合并。

**修正**：`_get` 加参数 `_skipNavSignal`，写操作（`_post` / `_put` / `_del`）默认跳过：
```javascript
function _fetchWithTimeout(url, opts, timeout, _retried, _skipNavSignal) {
  // ...
  if (!_skipNavSignal) {
    var navSignal = window._navSignal;
    if (navSignal) { ... }
  }
}
function _get(path, timeout) {
  return _fetchWithTimeout(BASE + path, {headers: headers()}, timeout, false, false);  // 合并 signal
}
function _post(path, body, timeout) {
  return _fetchWithTimeout(BASE + path, {method: 'POST', ...}, timeout, false, true);  // 跳过 signal
}
```

#### 影响点 10：observability.js 用原生 fetch 不走 API 层

**影响**：observability.js 第 88 行 `fetch('/health/realtime', {signal: AbortSignal.timeout(10000)})` 用原生 fetch + `AbortSignal.timeout`——10 秒超时自动 abort。第 87 行 `API.get('/dashboard/ops/overview')` 走 API 层。

**确认**：合理。原生 fetch 的 catch 是 `() => null`（第 88 行），不渲染错误。API.get 的 catch 也是 `() => null`（第 87 行）。两条路径都不渲染错误到 pageContent，不需要 `e.silent` 标记。停 timer（影响点 2 修复）就够了。

但第 87 行的 `API.get` 会读 `window._navSignal`——nav 切走时如果请求还在进行中会被 abort。Promise.all 的 catch 是 `() => null`，返回 null。`_obsData.ops` 为 null，`renderObsContent` 会处理 null 值。不会报错。合理。

#### 影响点 11：TypeError 重试时的 signal 传递

**影响**：TypeError 重试时（第 129-130 行）递归调用 `_fetchWithTimeout`。如果用全局变量方案，重试时读到的 `window._navSignal` 可能已经变了（用户切了 tab 又切回来）。

**确认**：合理。重试时读新 signal 是正确行为——如果用户切走了，重试请求也应该被 abort（新 signal 是新页面的，不包含旧请求）。如果用户切回来了，新 signal 是有效的，重试正常进行。边界情况：重试过程中 signal 被 abort，`_fetchWithTimeout` 第 120 行立即 abort，不会真正发请求。正确。

#### 影响点 12：nav 后异步加载用新 signal

**影响**：skills.js 第 106-107 行：`renderSkills()` 在 `app.nav()` 里调用，renderSkills 调 `_loadSkills()` 发请求。此时 `window._navSignal` 是 nav 刚创建的新 signal。

**确认**：合理。nav 先设 `window._navSignal = newController.signal`，然后调 renderSkills → _loadSkills → _reloadSkills → API.get。API.get 读到的 `window._navSignal` 是最新的。异步请求用新 signal，切 tab 时被 abort。正确。

#### 影响点 13：页面内 tab 用独立 AbortController

**影响**：页面内 tab 切换（`_switchSkillTab`）如果复用 `window._navSignal`，会 abort 整个页面的请求。但页面内 tab 切换只应该取消旧 tab 的请求。

**确认**：合理。页面内 tab 用独立的 `window._tabSignal`：
```javascript
// _switchSkillTab
if (window._tabSignal) { /* abort 旧 tab signal */ }
var tabController = new AbortController();
window._tabSignal = tabController.signal;
_currentView = 'skills:' + tab;
```

`_fetchWithTimeout` 同时合并 `window._navSignal` 和 `window._tabSignal`——nav 切 tab 时 abort 所有，页面内 tab 切换时只 abort 旧 tab 的。

但写操作用 `_skipNavSignal=true`——跳过 nav signal。也应该跳过 tab signal：
```javascript
if (!_skipNavSignal) {
  // 合并 nav signal
  // 合并 tab signal
}
```

### 2.7 对系统现有能力的影响汇总

| 现有能力 | 是否受影响 | 证据 |
|---------|-----------|------|
| 页面数据加载 | ✅ 改善 | 切 tab 时旧请求被取消，不覆盖新页面 |
| 写操作（创建/删除/更新） | ❌ 不受影响 | 见下方证据 1 |
| Dashboard 30s 定时刷新 | ✅ 改善 | 切走停 timer + abort，页面刷新时静默不报错 |
| 全局初始化（profile/repos） | ✅ 改善 | abort 时不隐藏侧边栏 |
| Select2 AJAX 搜索 | ❌ 不受影响 | 见下方证据 2 |
| 401 refresh | ✅ 改善 | abort/网络错误不清 token，只有真失败才跳登录 |
| observability 轮询 | ✅ 改善 | 切走停 timer |
| chat 飞书接入轮询 | ✅ 改善 | 切走停 timer + 切回恢复 |
| 技能点赞/场景点赞 | ❌ 不受影响 | 见下方证据 3 |
| 页面内 tab 切换 | ✅ 改善 | 独立 AbortController，不影响 nav 级别 |
| 真实网络错误显示 | ❌ 不受影响 | 见下方证据 4 |
| 超时错误显示 | ❌ 不受影响 | 见下方证据 5 |

**证据 1：写操作不受影响**

写操作调用 `API.post` / `API.put` / `API.del`，这些函数签名是 `_post(path, body, timeout)` / `_put(path, body, timeout)` / `_del(path)`（api.js 第 194-200 行）——没有 signal 参数。改动后 signal 是可选参数，不传时 `_fetchWithTimeout` 的 `externalSignal` 为 undefined，不会合并外部 signal。写操作不会被 nav abort 取消。

现有写操作调用点（均未传 signal）：
- skills.js:433 `API.post('/skills/' + skillId + '/like', {})`——技能点赞
- skills.js:647 `API.del('/skills/by-id/' + skillId)`——删除技能
- skills.js:694 `API.post('/skills/convert', {skill_name, repo})`——转换技能
- scene-market.js:539 `API.post('/scenes/market/' + sceneId + '/like', {})`——场景点赞
- scene-market.js:599 `API.post('/scenes/market/' + sceneId + '/apply...')`——应用场景
- scene-market.js:734 `API.post('/scenes/market/' + sceneId + '/experience', {})`——体验场景
- scene-market.js:1014 `API.del('/scenes/market/' + sceneId)`——删除场景
- scene-market.js:1688 `API.put('/scenes/market/' + id, body)`——更新场景
- scene-market.js:1695 `API.post('/scenes/market?repo=...', body)`——创建场景

**证据 2：Select2 AJAX 搜索不受影响**

Select2 的 AJAX 配置（scene-market.js:1352-1356）用 jQuery `$.ajax`（`url: '/api/scenes/skills/' + agentType`），不走 `API.get` / `_fetchWithTimeout`。Select2 内部管理自己的请求生命周期——`select2('destroy')` 时自动取消进行中的 AJAX 请求。不经过 API 层，不传 signal，不受 nav abort 影响。

**证据 3：技能点赞/场景点赞不受影响**

点赞是写操作（`API.post('/skills/' + id + '/like', {})` 和 `API.post('/scenes/market/' + id + '/like', {})`），走 `API.post` → `_post(path, body, timeout)` → `_fetchWithTimeout`。如证据 1 所述，`_post` 不传 signal，点赞请求不会被 nav abort 取消。点赞回调里的 `showToast` 和 `renderSkills` 也不受 stale guard 影响（如影响点 8 所述，stale guard 只放在 `renderXxx()` 前面，`showToast` 始终执行）。

**证据 4：真实网络错误显示不受影响**

`_fetchWithTimeout` catch（api.js:184）判断 `e instanceof TypeError` 时重试一次，重试仍失败则 `throw e`。改动后这段逻辑不变——TypeError 不被标记 `e.silent`，正常抛出到页面层。页面 catch 判断 `e.name === 'AbortError'` 时不渲染——TypeError 的 `e.name` 是 `'TypeError'`，不匹配，正常渲染错误。

HTTP 错误（4xx/5xx）在 `handleResp`（api.js:121-157）里处理，抛出 `new Error(detail.message)`——`e.name` 是 `'Error'`，不是 `'AbortError'`，不匹配，正常渲染错误。

**证据 5：超时错误显示不受影响**

`_fetchWithTimeout` 改动后，超时 abort 前设 `controller._timedOut = true`（第 117 行）。catch 判断 `e.name === 'AbortError'` 后再检查 `controller._timedOut`——如果为 true，`throw e`（不标记 silent），正常抛出到页面层。页面 catch 判断 `e.name === 'AbortError'` 时不渲染——但超时错误不是 AbortError，是浏览器抛的 `DOMException` with name `TimeoutError`... 

实际上超时 abort 触发的是 `AbortError`（因为调了 `controller.abort()`），但 `controller._timedOut = true` 标记了。API 层 catch 判断 `if (controller._timedOut) throw e`——不标记 silent，正常抛出。页面层 catch 判断 `if (e.name === 'AbortError') return` 会静默超时错误——这是问题。

**修正**：页面层 catch 不能只判断 `e.name === 'AbortError'`，要同时判断 `!e.silent`：
```javascript
if (e.name === 'AbortError' && e.silent) return;  // 只静默外部取消，不静默超时
```
超时错误 `e.silent` 为 undefined（API 层没标记），不匹配，正常渲染错误。

### 2.8 验收标准

- [ ] 页面刷新时不再出现"加载失败: signal is aborted without reason"提示
- [ ] 切 tab 时旧回调不覆盖新页面（stale guard 生效）
- [ ] 30s 定时刷新失败不覆盖页面
- [ ] 真实网络错误（超时、服务端 5xx 等）仍正常显示错误提示
- [ ] 切 tab 时不会卡死，切回后正常加载
