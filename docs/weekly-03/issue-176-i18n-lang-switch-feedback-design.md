# Issue #176 — 国际化页面反馈迟缓

> 设计方案 v9 | 2026-09-17 | 分支：feat/issue-176-i18n-lang-switch-feedback
>
> v8→v9 变更：经八轮评审确认四个点——① 定时器清理从第二个 .then 改为 .finally（与 api.js:268 已有模式一致，覆盖成功/失败/异常三路径）；② 确认 fallback 链终点是 inline packs（common.error/success 两语言均覆盖），部分失败警告 Toast 可读；③ 确认点击当前语言早退已有（selectLang 第 315 行）；④ 确认 inline packs 覆盖全部三类 Toast 文案。
> v7→v8 变更：修复缓存命中 bug（0 < 0）+ Toast i18n 悖论 + `<html lang>` + showToast 无障碍 + 加载期间导航分析。
> v6→v7 变更：定时器统一清理 + 状态变更移到 loadPacks 成功之后。
> v5→v6 变更：锁释放改回 finally + 全部失败状态处理。
> v3→v4 变更：setLang 全部失败跳过 rerender + try/catch + failedCount 覆盖所有失败路径。
> v2→v3 变更：修正三个误判。
> v1→v2 变更：failedCount 计数、_loadedLangs 条件标记、全局锁。

## 1. 问题分析

### 1.1 原始问题

文件 `src/api/static/js/pages/settings.js` 中语言切换按钮 `selectLang` 缺少即时反馈，且 `src/api/static/js/i18n.js` 中 `setLang` 主动破坏语言包缓存导致每次切换都全量重拉 15 个 JSON 文件。

### 1.2 根因（源码级，3 层）

**第 1 层 — settings.js `selectLang()`：零反馈**

```js
function selectLang(lang) {
  if (lang === I18N.getLang()) return;
  I18N.setLang(lang);
}
```

- 无按钮禁用/loading 状态
- 无 Toast 提示
- 无防抖（重复点击保护）
- 无错误回调
- `I18N.setLang` 是 fire-and-forget，调用方拿不到完成信号

**第 2 层 — i18n.js `setLang()`：主动破坏缓存 + 静默吞错误**

```js
function setLang(lang) {
  // ...
  _loadedLangs[lang] = false;    // ← 强制重置，即使之前已加载过
  loadPacks(lang).then(function () {
    applyI18nToDOM();
    _rerenderCurrentPage();
  }).catch(function () {});       // ← 空函数吞掉所有错误
}
```

两个问题：

- `_loadedLangs[lang] = false` 使 `loadPacks` 里的缓存短路 `if (_loadedLangs[lang]) return Promise.resolve()` 失效。每次切换都重新发起 15 个 fetch，即使目标语言之前已完整加载过。
- `.catch(function () {})` 静默吞掉所有错误，`selectLang` 拿不到成功/失败信号，无法做按钮恢复。

**第 3 层 — i18n.js `loadPacks()`：15 个 JSON 并发等待 + catch 吞错误**

```js
var promises = MODULES.map(function (mod) {
  return fetch(base + mod + '.json' + ver)
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + mod);
      return r.json();
    })
    .then(function (data) {
      if (!_packs[lang]) _packs[lang] = {};
      _packs[lang][mod] = data;
    })
    .catch(function (e) {
      // Module load failed — keep inline pack if present, log warning
      console.warn('[i18n] Failed to load pack: ' + lang + '/' + mod + '.json', e.message);
    });
});

return Promise.all(promises).then(function () {
  _loadedLangs[lang] = true;   // ← 无条件标记，即使部分 fetch 失败
  applyI18nToDOM();
});
```

三个问题：

- 15 个语言包 JSON 并发请求，`Promise.all` 等全部完成才继续。网络慢时延迟可观，期间 DOM 不更新。
- 每个 fetch 的 `.catch` 吞掉错误返回 undefined，`Promise.all` 永远 resolve。即使全部 15 个 fetch 都失败，`loadPacks` 仍走 resolve 路径。
- `_loadedLangs[lang] = true` 无条件执行，部分失败时也标记为已加载。下次切回该语言直接走缓存，缺失的包永久不重试。

### 1.3 用户感知

点击中文/英文按钮后页面无任何视觉变化（按钮不变、DOM 不更新），需等全部 15 个语言包加载完成后才整页重渲染。网络慢时秒级无反馈，用户以为页面卡死。

### 1.4 与 #175 的关系

#175（需求卡片页面反馈迟缓）是同类问题——按钮操作缺 loading 状态。修复模式一致：按钮 loading/禁用 → 异步操作 → 成功刷新/失败恢复。本 Issue 复用该模式。

## 2. 设计方案

### 2.1 交互流程

1. 用户点击【中文】或【English】按钮
2. 立即进入全局切换锁 `_isSwitching = true`，禁用两个语言按钮 + 被点击按钮文案变为「切换中…」/「Switching…」
3. 调用 `I18N.setLang(lang)`（改为返回 Promise，携带加载结果）
4. 成功（全部 15 包加载完成）→ `_rerenderCurrentPage` 在 setLang 内完成 DOM 重建 → 释放锁 → Toast 成功提示
5. 部分成功（部分包加载失败）→ DOM 重建（已有包生效，缺失包靠 t() fallback）→ 释放锁 → Toast 提示「切换完成，部分资源加载失败」
6. 全部失败 → DOM 不重建 → 释放锁 → 恢复按钮 → Toast 错误提示
7. 切换过程中全局锁阻止任何新 setLang 调用

### 2.2 setLang 改为返回 Promise + 携带加载结果

当前 `setLang` 是 fire-and-forget。改为返回 Promise，resolve 值携带 `{failed: N, total: N}` 供调用方区分全部成功/部分失败/全部失败。

```js
function setLang(lang) {
  if (SUPPORTED.indexOf(lang) < 0) lang = DEFAULT_LANG;
  if (lang === _currentLang) return Promise.resolve({failed: 0, total: 0, skipped: true});

  // 状态变更（_currentLang/localStorage/cookie/URL）放在 loadPacks 成功之后
  // 从源头避免"状态已改、视图未改"的中间态——全部失败时无需回滚
  return loadPacks(lang).then(function (result) {
    // 全部成功(failed<total)、部分成功(failed<total)、缓存命中(cached) 都提交状态
    // 全部失败(failed===total && !cached) 不提交
    if (result.failed < result.total || result.cached) {
      _currentLang = lang;
      try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) {}
      _setCookie(STORAGE_KEY, lang, 31536000, '/');
      _updateUrlLang(lang);
      applyI18nToDOM();   // 含 document.documentElement.lang 更新
      _rerenderCurrentPage();
    }
    // 全部失败：不提交任何状态变更，_currentLang/localStorage/cookie/URL 保持原值
    // DOM 不重建，旧按钮仍禁用，selectLang 手动恢复
    return result;  // {failed: N, total: 15} 或 {failed: 0, total: 0, cached: true}
  });
}
```

关键变更：
- **状态变更移到 loadPacks 成功之后**：`_currentLang`/localStorage/cookie/URL 的写入全部放在 `result.failed < result.total || result.cached` 分支内。全部失败时不提交任何状态变更，无需回滚，从源头避免"状态已改、视图未改"的中间态
- **修复缓存命中 bug**：`result.failed < result.total` 对缓存命中 `{failed:0, total:0, cached:true}` 返回 false（0 < 0 = false），导致缓存命中时不提交状态不 rerender。加 `|| result.cached` 修复
- **`<html lang>` 更新**：`applyI18nToDOM()` 内部已有 `document.documentElement.setAttribute('lang', _currentLang === 'en' ? 'en' : 'zh-CN')`。v7 状态变更移到 loadPacks 后确保 `_currentLang` 正确，`applyI18nToDOM` 用 `_currentLang` 设 lang 属性，无障碍合规
- 删除 `_loadedLangs[lang] = false`：不再强制重置缓存标记
- 返回 `loadPacks(...)` 的 Promise，将 resolve 值透传给调用方
- 删除尾部空 `.catch(function () {})`：错误处理交给调用方
- 删除 v5 的 prevLang 回滚逻辑：不再需要

### 2.3 loadPacks 改为返回加载结果 + 条件标记缓存

当前 `loadPacks` 的 catch 吞掉每个 fetch 错误，`Promise.all` 永远 resolve，且无条件标记 `_loadedLangs[lang] = true`。

改动：每个 fetch 的 `.catch` 收集失败信息而非静默吞掉，`Promise.all` resolve 后根据失败数决定是否标记 `_loadedLangs[lang] = true`，并返回结果对象。

```js
function loadPacks(lang) {
  if (!SUPPORTED.indexOf || SUPPORTED.indexOf(lang) < 0) lang = DEFAULT_LANG;
  if (_loadedLangs[lang]) return Promise.resolve({failed: 0, total: 0, cached: true});

  var base = '/static/locales/' + lang + '/';
  var ver = _cacheVer ? '?v=' + _cacheVer : '';
  var failedCount = 0;
  var total = MODULES.length;

  var promises = MODULES.map(function (mod) {
    // 每个 fetch 加 15 秒超时——弱网下不让单个请求无限挂起
    var controller = new AbortController();
    var timer = setTimeout(function() {
      controller._timedOut = true;
      controller.abort();
    }, 15000);

    return fetch(base + mod + '.json' + ver, { signal: controller.signal })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + mod);
        return r.json();
      })
      .then(function (data) {
        if (!_packs[lang]) _packs[lang] = {};
        _packs[lang][mod] = data;
      })
      .catch(function (e) {
        // 覆盖所有失败路径：fetch reject(网络错误)、!r.ok(HTTP 4xx/5xx)、r.json() parse 失败、超时 abort
        failedCount++;
        console.warn('[i18n] Failed to load pack: ' + lang + '/' + mod + '.json', e.message);
      })
      .finally(function () {
        // 无论成功或失败，都清理定时器——与 api.js _fetchWithTimeout 模式一致
        clearTimeout(timer);
      });
  });

  return Promise.all(promises).then(function () {
    // 只有全部成功才标记为已加载——部分失败保持 false，下次切回会重试缺失包
    if (failedCount === 0) {
      _loadedLangs[lang] = true;
    }
    applyI18nToDOM();
    return {failed: failedCount, total: total};
  });
}
```

关键变更：
- 新增 `failedCount` 计数器，每个 `.catch` 递增。覆盖所有失败路径：fetch reject（网络错误）、`!r.ok`（HTTP 4xx/5xx）、`r.json()` parse 失败、**超时 abort**。这四类都会走到 `.catch`，确保 `failedCount` 准确
- **每个 fetch 加 15 秒超时**：使用 `AbortController + setTimeout`，超时后 abort 触发 AbortError，走到 `.catch` 计入 failedCount。与 API.js 的 `_fetchWithTimeout` 机制一致（30 秒），语言包是静态资源体积小，15 秒足够
- `Promise.all` resolve 后：`failedCount === 0` 才标记 `_loadedLangs[lang] = true`；部分失败保持 false，下次切回时 `loadPacks` 重新拉取（含已成功的——fetch 不会因已存在而跳过，但写入 `_packs[lang][mod]` 是覆盖写，无副作用）
- 返回 `{failed: N, total: 15}` 供调用方区分成功/部分失败/全部失败
- 保留 `applyI18nToDOM()` 调用——部分成功的包也应应用到 DOM
- `clearTimeout(timer)` 放在 `.finally` 中，与 api.js `_fetchWithTimeout` 的 `.finally(() => clearTimeout(timer))` 模式一致（api.js:268 已有先例）。确保成功/失败/异常三条路径都清理定时器，避免 15 个定时器泄漏

注意：`loadPacks` 仍然永远 resolve（不 reject）。这是正确的——部分失败时已有的包应生效，DOM 应更新。reject 只用于 `loadPacks` 自身代码异常（如 `MODULES.map` 同步抛出），不影响单个 fetch 失败的行为。

超时说明：超时后 `controller.abort()` 会使 fetch 抛出 `AbortError`（name === 'AbortError'），走到 `.catch` 中 `failedCount++`。这与网络错误、HTTP 4xx、JSON parse 失败走同一路径，不需要特殊处理。`controller._timedOut = true` 标记可用于调试，但 `.catch` 不区分超时和其他失败——对用户来说都是"该语言包加载失败"。

边界说明：如果 fetch 返回 200 但 JSON 内容为 `{}` 或 `null`，`.then(function(data)` 会正常执行 `_packs[lang][mod] = data`，`failedCount` 不递增。这不是"加载失败"——空 JSON 是合法的 HTTP 响应，`t()` 会走 fallback 逻辑（key 本身或默认语言）。只有 fetch 请求本身失败（网络层/HTTP 状态码/JSON 解析/超时）才计入 `failedCount`。

### 2.4 selectLang 增加按钮反馈 + 全局锁

```js
var _langSwitching = false;  // 全局切换锁

function selectLang(lang) {
  if (lang === I18N.getLang()) return;  // 点击当前语言，直接退出
  if (_langSwitching) return;  // 全局锁，防并发切换

  _langSwitching = true;

  // 禁用两个语言按钮 + 文案变更（UI 层反馈，非互斥手段）
  var buttons = document.querySelectorAll('[onclick^="selectLang"]');
  var origTexts = [];
  buttons.forEach(function(btn, i) {
    origTexts[i] = btn.textContent;
    btn.disabled = true;
    btn.style.opacity = '0.5';
    btn.style.cursor = 'wait';
    btn.textContent = I18N.getLang() === 'zh' ? '切换中…' : 'Switching…';
  });

  function restoreButtons() {
    buttons.forEach(function(btn, i) {
      btn.disabled = false;
      btn.style.opacity = '';
      btn.style.cursor = '';
      btn.textContent = origTexts[i];
    });
  }

  // 全部失败时 _currentLang 未变更，I18N.t() 用旧语言取文案
  // inline packs 覆盖 common.error/success（zh: '错误'/'成功', en: 'Error'/'Success'）
  // 即使语言包未加载，inline packs 保证 Toast 文案可读
  var prevLang = I18N.getLang();  // 保存旧语言用于失败 Toast

  try {
    I18N.setLang(lang).then(function(result) {
      if (result.cached) {
        // 缓存命中：setLang 已在内部完成状态变更 + rerender，旧按钮已被替换
        // 不弹 Toast——缓存命中是秒级操作，用户不需要"成功"反馈
      } else if (result.failed === 0) {
        // 全部成功：_rerenderCurrentPage 已在 setLang 内同步完成 DOM 重建
        if (typeof app !== 'undefined' && app.showToast) {
          app.showToast(I18N.t('common.success'));
        }
      } else if (result.failed < result.total) {
        // 部分成功：DOM 已重建，部分文案靠 t() fallback
        if (typeof app !== 'undefined' && app.showToast) {
          app.showToast(I18N.t('common.success') + ' (' + result.failed + '/' + result.total + ' failed)');
        }
      } else {
        // 全部失败：setLang 未提交状态变更，_currentLang 仍为 prevLang
        // DOM 不重建，旧按钮仍禁用，必须手动恢复
        // Toast 文案用 I18N.t()——此时 _currentLang 是旧语言，inline packs 保证文案可读
        restoreButtons();
        if (typeof app !== 'undefined' && app.showToast) {
          app.showToast(I18N.t('common.error'));
        }
      }
    }).catch(function(e) {
      // setLang 自身代码异常（非 fetch 失败）
      restoreButtons();
      if (typeof app !== 'undefined' && app.showToast) {
        app.showToast(I18N.t('common.error') + ': ' + (e && e.message ? e.message : ''));
      }
    });
  } catch(e) {
    // setLang 同步异常（如 _updateUrlLang 抛出）
    restoreButtons();
    if (typeof app !== 'undefined' && app.showToast) {
      app.showToast(I18N.t('common.error') + ': ' + (e && e.message ? e.message : ''));
    }
  } finally {
    // 无论如何释放全局锁——避免锁死
    _langSwitching = false;
  }
}
```

锁释放策略说明：

用 finally 释放锁。理由：
- finally 与 try 块尾部是连续同步执行的（同一微任务），不存在"旧 DOM 仍在"的时序窗口
- 显式三路径释放有锁泄漏风险：如果 showToast() 或 restoreButtons() 抛异常，锁永久泄漏，用户再也切不了语言
- finally 保证无论什么异常路径，锁一定释放

关键设计决策：

| 决策 | 选择 | 理由 |
|------|------|------|
| loading 表现 | 按钮文字+禁用 | 与 #175 模式一致，最小改动，不引入遮罩组件 |
| 防抖机制 | 全局锁 `_langSwitching`（业务层互斥） + 按钮禁用（UI 层反馈） | 两套防护职责区分：全局锁是互斥手段，阻止并发 setLang 竞态覆盖；按钮 disabled+loading 文案仅给用户视觉反馈，不作为互斥手段。禁用按钮不够——两个按钮 A/B，点 A 后 A 禁用但 B 仍可点，会并发 setLang 竞态覆盖 |
| 成功后按钮恢复 | 不手动恢复 | `_rerenderCurrentPage` 调 `renderSettings('appearance')` 同步重建整个设置页 DOM，旧按钮节点已脱离文档。对旧节点设 disabled=false 无害（节点已不在 DOM 树中） |
| 部分成功后按钮恢复 | 同上，不手动恢复 | DOM 已重建（applyI18nToDOM + _rerenderCurrentPage 已执行），旧按钮已被替换 |
| 全部失败后按钮恢复 | 手动恢复 | 全部失败时 setLang 跳过 `_rerenderCurrentPage`，DOM 不重建，按钮停在禁用态必须手动恢复 |
| catch 路径 | restoreButtons + Toast | 覆盖 setLang 自身代码异常（非 fetch 失败），如 `_updateUrlLang` 抛出 |
| 锁释放位置 | finally 块 | finally 与 try 尾部连续同步执行，无时序窗口。finally 保证 showToast/restoreButtons 抛异常时锁也释放，避免锁泄漏 |
| try/catch/finally 结构 | try 包 setLang 调用 + .then/.catch 链 | try 捕获同步异常，.catch 捕获异步异常，finally 释放锁。三层覆盖所有异常路径 |

### 2.5 全部失败时按钮恢复与状态一致性

全部失败（`result.failed === result.total && !result.cached`）时：
- `setLang` 中 `result.failed < result.total || result.cached` 为 false，不提交任何状态变更（`_currentLang`/localStorage/cookie/URL 保持原值）
- 跳过 `applyI18nToDOM()` + `_rerenderCurrentPage()`
- DOM 不重建，旧按钮仍禁用
- `selectLang` 走 else 分支，调 `restoreButtons()` 恢复按钮 + Toast 错误提示

逻辑自洽链路：全部失败 → 不提交状态变更（状态保持旧语言）→ 跳过 rerender（DOM 保持旧语言）→ 手动恢复按钮。状态层和视图层天然一致，无需回滚。

Toast 文案 i18n 悖论处理：全部失败时 `_currentLang` 未变更（仍为旧语言），`I18N.t('common.error')` 用旧语言查包。inline packs 覆盖 `common.error`（zh: '错误', en: 'Error'），即使完整语言包未加载，inline packs 保证 Toast 文案可读。`prevLang` 变量保存旧语言值用于调试参考，实际不需要手动切换语言——`_currentLang` 自然保持原值。

对比成功/部分成功/缓存命中路径：
- `result.cached` 或 `result.failed < result.total` 为 true → 提交状态变更 → 执行 `applyI18nToDOM()`（含 `<html lang>` 更新）+ `_rerenderCurrentPage()`
- `_currentLang` 写为新语言 → DOM 重建为新语言 → 不需要 restoreButtons
- finally 释放锁
- 缓存命中不弹 Toast（秒级操作无需反馈），首次成功/部分成功弹 Toast

### 2.6 无障碍与 showToast

**`<html lang>` 更新**：`applyI18nToDOM()` 内部已有 `document.documentElement.setAttribute('lang', _currentLang === 'en' ? 'en' : 'zh-CN')`。v7 状态变更移到 loadPacks 后确保 `_currentLang` 正确，`applyI18nToDOM` 在 `result.failed < result.total || result.cached` 分支内调用，用 `_currentLang` 设 lang 属性。全部失败时不调 `applyI18nToDOM`，`<html lang>` 保持旧值——与 DOM 保持旧语言一致，无障碍合规。

**showToast 无障碍属性**：当前 `showToast` 实现是 `document.createElement('div')` + `textContent`，无 `role`/`aria-live`。本次在 `showToast` 创建的 div 上加 `t.setAttribute('role', 'status'); t.setAttribute('aria-live', 'polite');`，让屏幕阅读器播报切换结果。成本极低（两行代码），不影响视觉表现。

### 2.7 加载期间用户导航

用户点击切换语言后 loadPacks 最长 15 秒，期间用户可能导航到其他页面。当前方案的风险：
- `_langSwitching` 锁仍被持有，但 finally 会在 Promise 完成后释放——不会永久锁死
- `_rerenderCurrentPage` 在 setLang 内调用，会根据 `window.history.state` 重新渲染当前视图。如果用户已导航到其他页面，`history.state.view` 是新页面的，`_rerenderCurrentPage` 会渲染新页面（带新语言）——这不是 bug，是正确行为（语言切换应全局生效）

不引入切换 token——当前 `_rerenderCurrentPage` 读 `history.state` 的设计已经天然处理了导航场景。锁在 finally 释放，不会阻塞新页面操作。

### 2.8 语言包缓存恢复

删除 `setLang` 中的 `_loadedLangs[lang] = false` 即可恢复 `loadPacks` 的缓存能力：

- 首次切换 zh→en：`_loadedLangs['en']` 为 false → 发起 15 个 fetch → 全部成功后标记 true
- 切回 en→zh：`_loadedLangs['zh']` 为 true → `loadPacks` 直接返回 `{cached: true}` → 跳过 fetch
- 再切 zh→en：`_loadedLangs['en']` 已为 true → 直接返回缓存 → 切换接近即时

缓存失效由 `I18N.init(cacheVer)` 中的 `_cacheVer` 控制：版本更新时 URL 带 `?v=新版本`，浏览器 HTTP 缓存自动失效。但 `_loadedLangs` 是内存标记，同一页面生命周期内 `_cacheVer` 不变——这是正确行为，因为页面刷新后 `_loadedLangs` 重置为空对象。

部分失败时 `_loadedLangs[lang]` 保持 false，下次切回该语言会重新拉取全部 15 个包（含已成功的——覆盖写无副作用）。这确保缺失的包有机会重试。

取舍说明：部分失败后下次切换重拉全部 15 包而非只重试失败的包。优点：实现简单，不需要维护"哪些包失败"的内存状态；缺点：弱网下部分失败后再次切换要重新拉全部 15 个。hotfix 场景可接受，后续可优化为单包增量重试。

### 2.9 版本号递增与缓存机制

当前缓存体系已完善，两层：

**HTTP 缓存层**：语言包 fetch URL 已带版本号
```js
// i18n.js loadPacks 内
var ver = _cacheVer ? '?v=' + _cacheVer : '';
fetch(base + mod + '.json' + ver)   // 如 /static/locales/zh/common.json?v=20
```
`I18N.init('20')` 传入 cacheVer=20。发版时递增 init 参数即可使浏览器 HTTP 缓存失效。

**内存缓存层**：`_loadedLangs` 标记同一页面会话内有效。页面刷新后重置为空对象，`_cacheVer` 从 `I18N.init('新版本号')` 重新初始化。无需额外机制。

本次改动需递增三处版本号：

| 位置 | 当前 | 改后 | 作用 |
|------|------|------|------|
| index.html `i18n.js?v=` | 1 | 2 | JS 文件缓存刷新 |
| index.html `settings.js?v=` | 21 | 22 | JS 文件缓存刷新 |
| index.html `I18N.init('')` | 20 | 21 | 语言包 JSON 缓存刷新 |

注意：`I18N.init` 的 cacheVer 也需要递增——虽然本次没改语言包 JSON 文件，但改了 i18n.js 自身逻辑，递增 cacheVer 是好习惯，确保用户拿到一致的 JS + JSON 版本。

### 2.10 setLang 调用方排查

经全仓库 grep 确认：`I18N.setLang` 仅在 `settings.js:316` 的 `selectLang` 中被调用。`init()` 调的是 `loadPacks` 不走 `setLang`。无其他调用方，不存在未处理 Promise 的 unhandled rejection 风险。

## 3. 改动清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/api/static/js/i18n.js` | `loadPacks` 函数修改 | 新增 failedCount 计数；每个 fetch 加 15 秒超时（AbortController）；`_loadedLangs[lang] = true` 改为条件标记（failedCount === 0）；返回 `{failed, total}` 结果对象 |
| `src/api/static/js/i18n.js` | `setLang` 函数修改 | 删除 `_loadedLangs[lang] = false`（恢复缓存）；状态变更移到 loadPacks 成功之后（`result.failed < result.total \|\| result.cached`）；全部失败时不提交无需回滚；返回 Promise 透传 loadPacks 结果；删除尾部空 `.catch` |
| `src/api/static/js/pages/settings.js` | `selectLang` 函数重写 | 全局锁 `_langSwitching` + 按钮禁用/文案变更 + 四态处理（缓存命中/全部成功/部分成功/全部失败）+ restoreButtons 兜底；缓存命中不弹 Toast |
| `src/api/static/js/app.js` | `showToast` 函数修改 | 创建的 div 加 `role="status"` + `aria-live="polite"` 无障碍属性 |
| `src/api/static/index.html` | i18n.js 版本号 1→2，settings.js 版本号 21→22，I18N.init cacheVer 20→21 | 三处缓存刷新 |

## 4. 不做的事

- 不做乐观更新（optimistic UI）——Issue 标记可选，本期不实现
- 不做空闲时预加载非当前语言包——用缓存解决切回场景已足够，预加载可作为后续优化
- 不改 `loadPacks` 为 reject on failure——每个 fetch 已有独立 `.catch` 将 reject 转为 resolve，`Promise.all` 不会因单个失败而 reject。部分失败时已有包应生效，DOM 应更新。改为 `Promise.allSettled` 无额外收益（当前行为已等价于 allSettled + 过滤失败项）
- 不合并 15 个语言包为单文件——架构级优化，超出本 Issue 范围
- 不改 `_rerenderCurrentPage` 逻辑——保持现有渲染逻辑
- 不改 `init()` 逻辑——init 中的 `_loadedLangs[lang] = false` 是正确的（首次加载确实未加载）
- 不做跨标签页同步（storage 事件监听）——记录为已知限制，后续可加 `window.addEventListener('storage', ...)`
- 不做 Intl 格式化器重建——项目中未使用 Intl.DateTimeFormat/NumberFormat（grep 确认），不受影响
- 不做 Accept-Language 头处理——API 层未使用 Accept-Language（grep 确认），不受影响
- 不做加载期间切换 token——`_rerenderCurrentPage` 读 `history.state` 天然处理导航场景，锁在 finally 释放
- 不做分级超时提示（5 秒后文案变"网络较慢"）——打磨项，15 秒超时当前可接受
- 不做失败监控上报——后续可加 sendBeacon 上报 lang/failedPacks/duration

## 5. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| `_rerenderCurrentPage` 重建 DOM 后旧按钮引用失效 | 中 | 成功/部分成功路径不依赖旧按钮引用（新 DOM 已正常）。全部失败路径调 restoreButtons 兜底——对旧节点操作无效但无害，新 DOM 中按钮本就正常 |
| `_rerenderCurrentPage` 异常未执行（try/catch 吞掉） | 中 | 此时 DOM 不重建，旧按钮仍禁用。restoreButtons 恢复旧按钮——这正是保留 restoreButtons 的原因 |
| 删除 `_loadedLangs[lang] = false` 后语言包更新不及时 | 低 | `_cacheVer` 控制 HTTP 缓存失效；页面刷新后 `_loadedLangs` 重置。同一页面生命周期内语言包不会变 |
| 部分失败后 `_loadedLangs` 保持 false，每次切回都全量重拉 | 低 | 15 个 fetch 并发，正常网络下毫秒级完成。比 v1 的"缓存坏结果永久 fallback"好得多 |
| `setLang` 返回 Promise 后其他调用方未处理 | 无 | 全仓库 grep 确认：仅 `settings.js:316` selectLang 一个调用方。`init` 调 `loadPacks` 不走 `setLang`。无 unhandled rejection 风险 |
| 语言包 JSON 缓存版本不匹配 | 无 | fetch URL 已带 `?v=` + cacheVer（`I18N.init('20')` 传入）。发版递增 init 参数即可。页面刷新后 `_loadedLangs` 重置 + `_cacheVer` 重新初始化。现有机制完善 |
| 并发安全：快速点击 zh→en→zh | 低 | 全局锁 `_langSwitching` 在 selectLang 入口拦截，第二个点击直接 return |
| `selectLang` 中 querySelector `[onclick^="selectLang"]` 匹配范围 | 低 | 设置页 appearance tab 只有这两个语言按钮用 `selectLang`，不会误匹配 |

## 6. 验收标准

- [ ] 点击【中文】/【English】按钮后立即按钮禁用 + 文案变为「切换中…」/「Switching…」
- [ ] 切换过程中两个语言按钮均不可点击（全局锁 `_langSwitching`）
- [ ] 全部成功（含缓存命中）：切换完成后 UI 正确切换为目标语言
- [ ] 缓存命中（切回已加载语言）：秒级切换，不弹 Toast，按钮无 loading 闪烁
- [ ] 部分成功（部分包 404）：切换完成后已有包生效，缺失包靠 t() fallback + Toast 提示「部分资源加载失败」
- [ ] 全部失败（断网或全部超时）：Toast 错误提示 + 按钮恢复可点 + _currentLang/localStorage/cookie/URL 保持原值（不提交状态变更），页面语言保持旧版本
- [ ] Toast 文案在全部失败时可读（inline packs 覆盖 common.error，用旧语言取文案）
- [ ] `<html lang>` 属性随语言切换正确更新（applyI18nToDOM 内部已有）
- [ ] showToast 有 role="status" + aria-live="polite" 无障碍属性
- [ ] 单个语言包超时（15s）：超时包计入 failedCount，走部分失败或全部失败路径
- [ ] 快速连击两个按钮（zh→en→zh）：全局锁生效，第二次点击无响应
- [ ] 重复切换已加载过的语言：复用内存缓存，无网络请求（`_loadedLangs` 命中）
- [ ] 部分失败后再次切回该语言：`_loadedLangs` 保持 false，重新拉取全部 15 包（缺失包重试）
- [ ] 切换成功后按钮恢复可点（经 _rerenderCurrentPage 重建为新 DOM，旧按钮已被替换）
- [ ] URL 前缀（/en）正确更新，localStorage/cookie 正确写入
- [ ] 切换过程中无中间态闪烁
- [ ] 版本号递增：i18n.js v=2, settings.js v=22, I18N.init('21') 三处一致
- [ ] 所有现有测试通过（基线已有的失败除外）
