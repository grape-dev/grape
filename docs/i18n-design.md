# Grape 前端 UI 国际化设计方案

> 分支：`feat/i18n`（基于 dev 最新代码 `40b1c6f9`）
> 日期：2026-08-27
> 参考：multica-ai/multica（packages/views/locales/ + packages/core/i18n/）

## 一、设计目标

1. 支持中文（默认）+ 英文两种语言
2. 文案与代码解耦——所有文案在独立 JSON 语言包里，代码只引用 key
3. 中文路由不变（`http://domain/#dash`），英文加 `/en` 前缀（`http://domain/en/#dash`）
4. 设置页 Appearance tab 底部新增"国际化配置 / Language"模块，可切换语言
5. 语言偏好存 localStorage（`grapeLang`），刷新后保持
6. 新增语言只需加 JSON 目录，代码不变

## 二、语言包结构

```
src/api/static/locales/
  zh/
    common.json       ← 通用文案（加载中、失败、确认、取消、保存、删除...）
    app.json          ← 主框架（导航、侧边栏、登录页...）
    settings.json     ← 设置页面
    knowledge.json    ← 知识看板
    skills.json       ← 技能管理
    chat.json         ← 聊天
    events.json       ← 事件详情
    agents.json       ← Agent 配置
    pipeline.json     ← 流水线
    runtimes.json     ← Worker 管理
    tasks.json        ← 任务中心
    repos.json        ← 仓库管理
    dashboard.json    ← 仪表盘
    observability.json ← 可观测性
    templates.json    ← 模板
  en/
    common.json
    app.json
    settings.json
    knowledge.json
    skills.json
    chat.json
    events.json
    agents.json
    pipeline.json
    runtimes.json
    tasks.json
    repos.json
    dashboard.json
    observability.json
    templates.json
```

### JSON 格式

按模块嵌套，支持变量插值（`{{name}}`）：

```json
// locales/zh/common.json
{
  "loading": "加载中...",
  "load_failed": "加载失败",
  "save": "保存",
  "cancel": "取消",
  "confirm": "确认",
  "delete": "删除",
  "login": "登录",
  "logout": "退出登录",
  "search": "搜索",
  "no_data": "暂无数据",
  "error": "错误",
  "success": "成功",
  "copied": "已复制",
  "copy_failed": "复制失败"
}

// locales/en/common.json
{
  "loading": "Loading...",
  "load_failed": "Load failed",
  "save": "Save",
  "cancel": "Cancel",
  "confirm": "Confirm",
  "delete": "Delete",
  "login": "Login",
  "logout": "Logout",
  "search": "Search",
  "no_data": "No data",
  "error": "Error",
  "success": "Success",
  "copied": "Copied",
  "copy_failed": "Copy failed"
}
```

```json
// locales/zh/app.json
{
  "nav": {
    "dashboard": "Dashboard",
    "pipeline": "Pipeline",
    "vod": "VOD",
    "issue": "Issue",
    "coding": "Coding",
    "qa": "QA",
    "sre": "SRE",
    "chat": "Chat",
    "settings": "Settings",
    "skills": "Skills",
    "knowledge": "知识库",
    "runtimes": "Runtimes",
    "events": "Events",
    "repos": "仓库",
    "tasks": "Tasks",
    "observability": "可观测性"
  },
  "login": {
    "gitcode_login": "使用 GitCode 登录",
    "pwd_login": "用户名密码登录",
    "username": "用户名",
    "password": "密码",
    "login_btn": "登录",
    "back_gitcode": "← 返回 GitCode 登录",
    "failed": "登录失败"
  },
  "repo": {
    "select": "选择仓库...",
    "load_failed": "无法加载仓库列表，请确认 GitCode Token 是否有效"
  }
}

// locales/en/app.json
{
  "nav": {
    "dashboard": "Dashboard",
    "pipeline": "Pipeline",
    "vod": "VOD",
    "issue": "Issue",
    "coding": "Coding",
    "qa": "QA",
    "sre": "SRE",
    "chat": "Chat",
    "settings": "Settings",
    "skills": "Skills",
    "knowledge": "Knowledge",
    "runtimes": "Runtimes",
    "events": "Events",
    "repos": "Repos",
    "tasks": "Tasks",
    "observability": "Observability"
  },
  "login": {
    "gitcode_login": "Sign in with GitCode",
    "pwd_login": "Sign in with username",
    "username": "Username",
    "password": "Password",
    "login_btn": "Sign In",
    "back_gitcode": "← Back to GitCode login",
    "failed": "Login failed"
  },
  "repo": {
    "select": "Select repository...",
    "load_failed": "Failed to load repositories, please check your GitCode Token"
  }
}
```

## 三、i18n 核心模块

### 3.1 i18n.js — 翻译核心

新增 `src/api/static/js/i18n.js`，提供 `t()` 函数和语言切换能力：

```javascript
// src/api/static/js/i18n.js
(function() {
  const SUPPORTED = ['zh', 'en'];
  const DEFAULT_LANG = 'zh';
  const STORAGE_KEY = 'grapeLang';

  let _lang = DEFAULT_LANG;
  let _packs = {};  // { zh: { common: {...}, app: {...} }, en: { ... } }

  // 从 URL 检测语言：/en/... → en，其他 → zh
  function detectLang() {
    const path = window.location.pathname;
    for (const lang of SUPPORTED) {
      if (lang !== DEFAULT_LANG) {
        if (path === '/' + lang || path.startsWith('/' + lang + '/')) {
          return lang;
        }
      }
    }
    // URL 没有语言前缀 → 看 localStorage
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && SUPPORTED.includes(stored)) return stored;
    return DEFAULT_LANG;
  }

  // 异步加载语言包
  async function loadPacks(lang) {
    if (_packs[lang]) return _packs[lang];
    const modules = [
      'common', 'app', 'settings', 'knowledge', 'skills', 'chat',
      'events', 'agents', 'pipeline', 'runtimes', 'tasks', 'repos',
      'dashboard', 'observability', 'templates'
    ];
    const pack = {};
    const promises = modules.map(async (mod) => {
      try {
        const resp = await fetch(`/static/locales/${lang}/${mod}.json?v=${_CACHE_VER}`);
        if (resp.ok) {
          pack[mod] = await resp.json();
        }
      } catch (e) {
        console.warn(`i18n: failed to load ${lang}/${mod}.json`, e);
      }
    });
    await Promise.all(promises);
    _packs[lang] = pack;
    return pack;
  }

  // 翻译函数：t('common.loading') / t('app.nav.dashboard') / t('common.load_failed', {name: 'xxx'})
  function t(key, vars) {
    const parts = key.split('.');
    const mod = parts[0];
    const path = parts.slice(1);
    let val = (_packs[_lang] || {})[mod];
    for (const p of path) {
      if (val && typeof val === 'object') {
        val = val[p];
      } else {
        val = undefined;
        break;
      }
    }
    if (val === undefined || val === null) {
      // 回退到默认语言
      if (_lang !== DEFAULT_LANG) {
        let fallback = (_packs[DEFAULT_LANG] || {})[mod];
        for (const p of path) {
          if (fallback && typeof fallback === 'object') {
            fallback = fallback[p];
          } else {
            fallback = undefined;
            break;
          }
        }
        val = fallback;
      }
      // 仍然没有 → 返回 key 本身
      if (val === undefined || val === null) return key;
    }
    // 变量插值：{{name}} → vars.name
    if (vars && typeof val === 'string') {
      val = val.replace(/\{\{(\w+)\}\}/g, (_, k) => vars[k] !== undefined ? vars[k] : '');
    }
    return val;
  }

  // 切换语言
  async function setLang(lang) {
    if (!SUPPORTED.includes(lang)) lang = DEFAULT_LANG;
    localStorage.setItem(STORAGE_KEY, lang);
    _lang = lang;
    await loadPacks(lang);
    // 刷新页面以重新渲染所有文案
    _applyUrlLang(lang);
  }

  // 调整 URL 语言前缀
  function _applyUrlLang(lang) {
    const path = window.location.pathname;
    let newPath;
    if (lang === DEFAULT_LANG) {
      // 去掉 /en 前缀
      newPath = path.replace(/^\/en(\/|$)/, '/');
      if (newPath === '') newPath = '/';
    } else {
      // 加 /en 前缀
      if (!path.startsWith('/' + lang)) {
        newPath = '/' + lang + (path === '/' ? '' : path);
      } else {
        newPath = path;
      }
    }
    // 刷新页面（hash 路由保持不变）
    window.location.href = newPath + window.location.hash;
    window.location.reload();
  }

  // 获取当前语言
  function getLang() {
    return _lang;
  }

  // 初始化
  async function init(cacheVer) {
    _CACHE_VER = cacheVer || '1';
    _lang = detectLang();
    await loadPacks(_lang);
    // 如果是默认语言也加载回退包
    if (_lang !== DEFAULT_LANG) {
      await loadPacks(DEFAULT_LANG);
    }
  }

  let _CACHE_VER = '1';

  window.I18N = { t, setLang, getLang, init, detectLang, SUPPORTED, DEFAULT_LANG };
})();
```

### 3.2 index.html 引入 i18n.js

在 app.js 之前加载 i18n.js，初始化后再加载 app.js：

```html
<!-- index.html -->
<script src="/static/js/i18n.js?v=1"></script>
<script>
// i18n 初始化（必须在 app.js 之前完成）
I18N.init('1').then(() => {
  const script = document.createElement('script');
  script.src = '/static/js/app.js?v=32';
  script.onload = () => { if (window.app && app.init) app.init(); };
  document.head.appendChild(script);
});
</script>
```

### 3.3 代码中使用 t()

```javascript
// 之前（硬编码）
pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
showToast('登录失败: ' + e.message);
el.innerHTML = '<div class="loading">加载中...</div>';

// 之后（引用 key）
pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>';
showToast(I18N.t('app.login.failed') + ': ' + e.message);
el.innerHTML = '<div class="loading">' + I18N.t('common.loading') + '...</div>';
```

### 3.4 index.html 动态文案

index.html 里的中文也需要改成动态渲染。在 i18n.js 初始化后，用 JS 替换：

```javascript
// i18n 初始化后替换 index.html 中的文案
function applyI18nToDOM() {
  document.documentElement.lang = I18N.getLang() === 'en' ? 'en' : 'zh-CN';
  // 登录页
  const gitBtn = document.getElementById('gitBtn');
  if (gitBtn) gitBtn.querySelector('span')?.textContent = I18N.t('app.login.gitcode_login');
  // 侧边栏
  const repoSelect = document.getElementById('repoSelect');
  if (repoSelect && repoSelect.options[0]) repoSelect.options[0].text = I18N.t('app.repo.select');
  // 导航
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = I18N.t(el.getAttribute('data-i18n'));
  });
}
```

index.html 中的硬编码改为 `data-i18n` 属性：
```html
<!-- 之前 -->
<a id="nav-dash" class="act" onclick="app.nav('dash')"><span class="ic">■</span>Dashboard</a>

<!-- 之后 -->
<a id="nav-dash" class="act" onclick="app.nav('dash')"><span class="ic">■</span><span data-i18n="app.nav.dashboard">Dashboard</span></a>
```

## 四、后端路由调整

### 4.1 /en 前缀路由

在 `src/api/main.py` 中添加 `/en` 路由，返回同一个 index.html：

```python
# 现有：所有非 /api、非 /static 的路径都返回 index.html
@app.get("/{path:path}")
async def serve_spa(path: str):
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

# 调整：/en、/en/、/en/#xxx 都返回 index.html（不干扰 /api、/static）
# 现有的 catch-all 路由已经能处理 /en/... → 返回 index.html
# 只需确保 /en 不被 /api 拦截即可
```

现有 catch-all 路由 `/{path:path}` 已经能处理 `/en/...` → 返回 index.html，**后端不需要改**。

### 4.2 静态文件

语言包 JSON 文件放在 `src/api/static/locales/` 下，通过现有 `/static` 挂载自动提供服务：

```
GET /static/locales/zh/common.json   → 中文语言包
GET /static/locales/en/common.json   → 英文语言包
```

**后端不需要改。**

## 五、设置页国际化配置模块

### 5.1 UI 设计

在 settings.js 的 Appearance tab，"外观模式"下面新增：

```html
<div class="cd mt16">
  <h3 style="font-size:13px;font-weight:600;margin-bottom:12px">
    国际化配置 / Language
  </h3>
  <p class="tx t2" style="margin-bottom:14px;font-size:12px">
    选择界面显示语言。切换后页面将自动刷新。
  </p>
  <div style="display:flex;gap:10px">
    <button onclick="selectLang('zh')"
      style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:${curLang==='zh'?'var(--a)':'var(--c)'};color:${curLang==='zh'?'#fff':'var(--t2)'}">
      🇨🇳 中文
    </button>
    <button onclick="selectLang('en')"
      style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:${curLang==='en'?'var(--a)':'var(--c)'};color:${curLang==='en'?'#fff':'var(--t2)'}">
      🇺🇸 English
    </button>
  </div>
</div>
```

### 5.2 selectLang 函数

```javascript
function selectLang(lang) {
  if (lang === I18N.getLang()) return;
  I18N.setLang(lang);  // 存 localStorage + 调整 URL + 刷新
}
```

## 六、语言切换流程

```
用户在设置页选"English"
  → selectLang('en')
  → I18N.setLang('en')
    → localStorage.setItem('grapeLang', 'en')
    → URL 从 /#dash 变为 /en/#dash
    → window.location.reload()
  → 页面重新加载
  → i18n.js 初始化：detectLang() 从 URL /en 检测到 en
  → 加载 en/ 语言包
  → app.js 渲染时调 t() 返回英文文案
```

```
用户切回中文
  → selectLang('zh')
  → I18N.setLang('zh')
    → localStorage.setItem('grapeLang', 'zh')
    → URL 从 /en/#dash 变为 /#dash
    → window.location.reload()
  → 页面重新加载
  → i18n.js 初始化：detectLang() 从 URL 无 /en → zh
  → 加载 zh/ 语言包
  → app.js 渲染时调 t() 返回中文文案
```

## 七、改动清单

### 7.1 新增文件

| 文件 | 说明 |
|------|------|
| src/api/static/js/i18n.js | i18n 核心模块（t() / setLang / detectLang / init） |
| src/api/static/locales/zh/*.json | 中文语言包（15 个文件） |
| src/api/static/locales/en/*.json | 英文语言包（15 个文件） |

### 7.2 修改文件

| 文件 | 改动 |
|------|------|
| src/api/static/index.html | 引入 i18n.js + 硬编码文案改 data-i18n 属性 |
| src/api/static/js/app.js | 中文文案改 t() 调用 |
| src/api/static/js/api.js | 中文文案改 t() 调用 |
| src/api/static/js/pages/settings.js | 新增国际化配置模块 + 中文文案改 t() |
| src/api/static/js/pages/knowledge.js | 中文文案改 t() |
| src/api/static/js/pages/skills.js | 中文文案改 t() |
| src/api/static/js/pages/chat.js | 中文文案改 t() |
| src/api/static/js/pages/events.js | 中文文案改 t() |
| src/api/static/js/pages/agents.js | 中文文案改 t() |
| src/api/static/js/pages/pipeline.js | 中文文案改 t() |
| src/api/static/js/pages/runtimes.js | 中文文案改 t() |
| src/api/static/js/pages/tasks.js | 中文文案改 t() |
| src/api/static/js/pages/repos.js | 中文文案改 t() |
| src/api/static/js/pages/dashboard.js | 中文文案改 t() |
| src/api/static/js/pages/observability.js | 中文文案改 t() |
| src/api/static/js/pages/templates.js | 中文文案改 t() |

### 7.3 不改的

| 文件 | 原因 |
|------|------|
| src/api/main.py | catch-all 路由已支持 /en/... |
| 后端 Python 代码 | 一期只做前端 UI |
| Agent prompt | 二期 |
| 错误消息 | 二期 |
| 飞书通知 | 二期 |

## 八、实施计划

### 第一阶段：基础设施（i18n 核心 + 语言包骨架）

1. 创建 `src/api/static/js/i18n.js`
2. 创建 `src/api/static/locales/zh/` 和 `src/api/static/locales/en/` 目录
3. 先建 `common.json` 和 `app.json`（最高频文案）
4. index.html 引入 i18n.js + 改 data-i18n 属性
5. app.js 核心文案改 t()

### 第二阶段：逐页改造

按中文字符数从多到少排序：

| 顺序 | 文件 | 中文字符数 |
|:---:|------|:---:|
| 1 | observability.js | 1274 |
| 2 | events.js | 1044 |
| 3 | agents.js | 671 |
| 4 | chat.js | 630 |
| 5 | runtimes.js | 659 |
| 6 | skills.js | 651 |
| 7 | settings.js | 612 |
| 8 | tasks.js | 553 |
| 9 | repos.js | 483 |
| 10 | app.js | 479 |
| 11 | pipeline.js | 357 |
| 12 | templates.js | 356 |
| 13 | knowledge.js | 325 |
| 14 | api.js | 290 |
| 15 | dashboard.js | 174 |

### 第三阶段：设置页国际化配置模块

1. settings.js Appearance tab 底部新增语言切换 UI
2. selectLang 函数
3. 测试语言切换

## 九、设计原则

1. **文案与代码解耦** — 所有文案在 JSON 文件里，代码只引用 key
2. **中文路由不变** — 不带 /en 就是中文，保持向后兼容
3. **回退机制** — 英文包缺失的 key 回退到中文包，中文包也缺失返回 key 本身
4. **变量插值** — 支持 `{{name}}` 模板变量
5. **按模块拆分** — 语言包按页面/功能模块拆分，便于维护
6. **缓存控制** — JSON 文件带版本号（`?v=1`），更新语言包时 bump 版本
7. **新增语言不改代码** — 加一个 JSON 目录即可


## 十、方案深度检查——遗漏和待优化点修复（2026-08-27）

### 问题 1：后端只有 @app.get("/") 没有 catch-all ❌ 必须修

**问题：** 后端只注册了 `@app.get("/")` 返回 index.html。`/en` 或 `/en/` 没有对应路由 → 404。

方案里说"catch-all 路由已支持 /en/..."——但实际上没有 catch-all 路由。

**修复：** 在 `src/api/main.py` 新增 `/en` 路由：

```python
@app.get("/en")
@app.get("/en/{path:path}")
async def serve_spa_en(path: str = ""):
    resp = FileResponse(os.path.join(STATIC_DIR, "index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp
```

注意：`/en/{path:path}` 不能匹配 `/api/...` 和 `/static/...`——这些在 `serve_spa_en` 之前已被 FastAPI 路由注册匹配。

### 问题 2：JS 同步加载 vs i18n 异步初始化竞态 ⚠️

**问题：** 现有 index.html 用同步 `<script>` 标签加载所有 JS。方案改为异步加载 i18n.js → init → 再加载 app.js。但 pages/*.js 在 app.js 之前同步执行，如果模块级代码调 t() → i18n 还没初始化 → 报错。

**修复：** i18n.js 用同步 `<script>` 加载（不加 async/defer），放在所有 JS 之前。语言包加载改为同步 XHR 或内联到 index.html。

方案调整：i18n.js 内联最小语言包（common + app login 相关 key），其他语言包用 fetch 异步加载（加载完成后调 applyI18nToDOM() 刷新）。

```javascript
// i18n.js 同步加载，内联最小语言包
const _INLINE_PACKS = {
  zh: {
    common: { loading: "加载中...", load_failed: "加载失败", /* ... */ },
    app: { login: { gitcode_login: "使用 GitCode 登录", /* ... */ } }
  },
  en: {
    common: { loading: "Loading...", load_failed: "Load failed", /* ... */ },
    app: { login: { gitcode_login: "Sign in with GitCode", /* ... */ } }
  }
};

// init 同步用内联包，异步加载完整包
function init(cacheVer) {
  _CACHE_VER = cacheVer || '1';
  _lang = detectLang();
  _packs[_lang] = _INLINE_PACKS[_lang] || {};
  if (_lang !== DEFAULT_LANG) {
    _packs[DEFAULT_LANG] = _INLINE_PACKS[DEFAULT_LANG] || {};
  }
  // 异步加载完整语言包（不阻塞页面渲染）
  loadPacks(_lang).then(() => {
    // 加载完成后刷新 DOM 文案
    if (typeof applyI18nToDOM === 'function') applyI18nToDOM();
  });
}
```

index.html 调整：
```html
<!-- i18n.js 同步加载，放在所有 JS 之前 -->
<script src="/static/js/i18n.js?v=1"></script>
<script>
// 同步初始化（用内联包，不阻塞）
I18N.init('1');
</script>
<!-- 其他 JS 正常同步加载 -->
<script src="/static/js/vendor/marked.min.js?v=1"></script>
<script src="/static/js/api.js?v=14"></script>
<!-- ... pages/*.js ... -->
<script src="/static/js/app.js?v=33"></script>
```

### 问题 3：OAuth 回调丢语言前缀 ❌ 必须修

**问题：** OAuth 回调 `RedirectResponse(url=f"/?token=xxx")` → 跳到 `/`。如果用户选了英文（localStorage.grapeLang=en），OAuth 回调后跳到 `/`（中文）→ 丢失语言偏好。

**修复（前端处理，后端不改）：** app.js init() 检测到 URL 有 token 参数后，检查 localStorage.grapeLang：

```javascript
// app.js init() 里 OAuth token 处理
const p = new URLSearchParams(window.location.search);
const urlToken = p.get('token');
if (urlToken) {
  localStorage.setItem('token', urlToken);
  // ... 现有逻辑 ...

  // 保留语言前缀
  const lang = localStorage.getItem('grapeLang') || 'zh';
  const langPrefix = lang === 'en' ? '/en' : '';
  if (window.history.replaceState) {
    window.history.replaceState(null, '', langPrefix + '/');
  }
  return;
}
```

### 问题 4：replaceState('') 清掉 /en 前缀 ❌ 必须修

**问题：** app.js 第 432-433 行 `window.history.replaceState(null, '', '/')` 清掉语言前缀。

**修复：** 保留语言前缀：

```javascript
// 之前
window.history.replaceState(null, '');

// 之后
const _lang = I18N.getLang();
const _prefix = _lang === 'en' ? '/en' : '';
window.history.replaceState(null, '', _prefix + '/');
```

### 问题 5：hash 路由兼容性 ✅ 无需修

`/en` path 部分不受 hash 路由影响。history.pushState/replaceState 改的是 hash（#xxx），不改 path。`/en` 前缀保持稳定。

### 问题 6：语言包加载失败处理 ⚠️

**修复：** i18n.js 加超时 + 重试 + 降级：

```javascript
async function loadPack(lang, mod) {
  const maxRetries = 2;
  for (let i = 0; i <= maxRetries; i++) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 5000);
      const resp = await fetch(`/static/locales/${lang}/${mod}.json?v=${_CACHE_VER}`, {
        signal: controller.signal
      });
      clearTimeout(timer);
      if (resp.ok) return await resp.json();
    } catch (e) {
      if (i < maxRetries) continue;
      console.warn(`i18n: failed to load ${lang}/${mod}.json after ${maxRetries + 1} attempts`, e);
    }
  }
  return null;  // 降级：用内联包或回退到默认语言
}
```

### 问题 7：缓存版本管理 ⚠️

**修复：** i18n.js init(cacheVer) 接收版本号，和 index.html 的 JS 版本号联动：

```html
<!-- index.html -->
<script>I18N.init('34');</script>  <!-- 和 app.js?v=34 版本同步 -->
```

### 问题 8：登录页中文闪动 ⚠️

**修复：** index.html 用 data-i18n 属性 + 默认空文案，i18n.js 同步初始化后立即填充：

```html
<!-- 之前 -->
<button class="obtn" id="gitBtn" onclick="app.oauthLogin()">
  使用 GitCode 登录
</button>

<!-- 之后 -->
<button class="obtn" id="gitBtn" onclick="app.oauthLogin()">
  <span data-i18n="app.login.gitcode_login">使用 GitCode 登录</span>
</button>
```

i18n.js init() 同步执行后立即调 applyI18nToDOM() 填充文案——因为内联包已加载，不需要等 fetch。

### 修复总结

| # | 问题 | 严重度 | 修复方案 | 改动文件 |
|---|------|:---:|------|------|
| 1 | 后端没有 /en 路由 | ❌ | 新增 /en + /en/{path:path} 路由 | main.py |
| 2 | JS 同步加载竞态 | ⚠️ | 内联最小语言包 + 异步加载完整包 | i18n.js + index.html |
| 3 | OAuth 回调丢语言 | ❌ | 前端 init() 检测 grapeLang 保留前缀 | app.js |
| 4 | replaceState 清 /en | ❌ | 保留语言前缀 | app.js |
| 5 | hash 路由兼容 | ✅ | 无需修 | — |
| 6 | 语言包加载失败 | ⚠️ | 超时 + 重试 + 降级 | i18n.js |
| 7 | 缓存版本管理 | ⚠️ | init(cacheVer) 联动 | i18n.js + index.html |
| 8 | 登录页闪动 | ⚠️ | data-i18n + 内联包同步填充 | index.html + i18n.js |

### 第二轮深度检查（2026-08-27）

#### /en/{path:path} 路由安全性 ⚠️

**问题：** `/en/{path:path}` 理论上会匹配 `/en/api/xxx` 和 `/en/static/xxx`。

**实际不影响：** api.js 用 `const BASE = '/api'` 绝对路径 → 请求 `/api/xxx` 不带 `/en` → 不会被拦截。

**防护措施：** 后端 `/en/{path:path}` 里排除 api/static，防止意外请求：

```python
@app.get("/en")
@app.get("/en/{path:path}")
async def serve_spa_en(path: str = ""):
    if path.startswith("api/") or path.startswith("static/"):
        raise HTTPException(status_code=404)
    resp = FileResponse(os.path.join(STATIC_DIR, "index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp
```

#### api.js 请求路径确认 ✅

`const BASE = '/api'` → 绝对路径，不受 `/en` 前缀影响。所有 API 请求走 `/api/xxx`，不经过 `/en/{path:path}`。

#### replaceState 只有一处 ✅

grep 确认 `replaceState` 只在 app.js 第 432 行一处调用。修复只需改这一处。

#### pages/*.js 模块级代码不调 t() ✅

pages/*.js 模块级代码都是变量声明（`let _knowledgeTab = 'project'`），不调 t()。所有 t() 调用都在函数内部（renderXXX），运行时才执行 → 不会在 i18n 初始化前报错。

#### logout 不清 grapeLang ✅

logout() 只清 `token` 和 `user`，不清 `grapeLang` → 语言偏好在退出登录后保持。

#### window.location.origin 不含 /en ✅

runtimes.js 用 `window.location.origin` 拼 URL（`origin = 协议+域名+端口`，不含 path）→ `/api/runtime/install` 不受 `/en` 影响。

#### showToast 无硬编码 ✅

showToast(msg) 接收参数，本身无中文硬编码。调用方传 t() 结果即可。

### 第二轮检查总结

| # | 检查项 | 结果 | 说明 |
|---|--------|:---:|------|
| 9 | /en/{path:path} 拦截 api/static | ⚠️ 防护 | api.js 绝对路径不影响，后端加防护排除 |
| 10 | api.js 请求路径 | ✅ | `const BASE = '/api'` 绝对路径 |
| 11 | replaceState 调用 | ✅ | 只有一处（第 432 行） |
| 12 | pages/*.js 模块级代码 | ✅ | 变量声明，不调 t() |
| 13 | OAuth 跳转 | ✅ | 已在问题 3 覆盖 |
| 14 | logout 清 localStorage | ✅ | 只清 token/user，不清 grapeLang |
| 15 | window.location.origin | ✅ | 不含 /en，不影响 |
| 16 | showToast | ✅ | 接收参数，无硬编码 |

无新遗漏。方案完备。

### 第三轮深度检查（2026-08-27）

#### 15 个 JSON 并发加载性能 ⚠️ → 调整为初始化全加载

**问题：** 浏览器对同一域名并发连接限制 6 个 → 15 个 JSON 需要 3 轮。

**方案调整：** 初始化时加载所有 15 个 JSON（不按需加载），原因：
1. HTTP/2 下并发请求不受 6 连接限制（nginx 已支持）
2. 按需加载需要 nav() 改 async → 改动大 + 闪动
3. 15 个 JSON 总大小预估 < 50KB → 一次加载可接受
4. 内联最小包（common + app login）保证首屏不闪动，完整包异步加载后刷新

#### document.title ✅

`<title>Grape — Multi-Agent DevOps</title>` 是英文 → 不需要翻译。

events.js 里的动态 title（`btn.title = '退出全屏'`）→ 已在逐页改造覆盖。

#### html lang 属性 ✅

`<html lang="zh-CN">` → i18n.js applyI18nToDOM() 动态设置 `document.documentElement.lang`。

#### confirm() / alert() 中文 ✅

6 个页面的 confirm/alert 中文 → 已在逐页改造计划覆盖（改为 t()）。

#### CSS 无中文 ✅

CSS content 属性用 Unicode 转义（`\2192` → →）→ 不需要翻译。

#### placeholder 中文 ✅

JS 动态生成的 placeholder（如 `搜索知识...`）→ 已在逐页改造覆盖。

#### 语言包加载策略最终方案

```
i18n.js init():
  1. 同步：用内联最小包（common + app login key）填充 _packs
  2. 同步：applyI18nToDOM() 填充 index.html 首屏文案（登录页、侧边栏）
  3. 异步：fetch 15 个 JSON 完整包 → 加载完成后刷新 DOM（如果页面已渲染）
```

首屏不闪动（内联包同步填充），完整包异步加载不阻塞页面。

### /en 路由与 API 请求关系说明（2026-08-27）

#### /en 路由只负责返回 index.html

```python
@app.get("/")
@app.get("/en")
@app.get("/en/{path:path}")
async def serve_spa(path: str = ""):
    if path.startswith("api/") or path.startswith("static/"):
        raise HTTPException(status_code=404)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
```

`/` 和 `/en` 返回同一个 index.html —— 区别只在前端：i18n.js 从 URL 检测到 `/en` → 加载英文语言包 → 渲染英文文案。

#### API 请求不受 /en 影响

api.js 用 `const BASE = '/api'` 绝对路径，不管用户在 `/` 还是 `/en/` 页面，API 请求始终是 `/api/xxx`。

| 用户 URL | 页面请求 | API 请求 | 后端路由 |
|---------|---------|---------|---------|
| `http://domain/#dash` | `/` → index.html | `/api/knowledge` | `@app.get("/")` + `include_router(/api)` |
| `http://domain/en/#dash` | `/en/` → index.html | `/api/knowledge` | `@app.get("/en/{path}")` + `include_router(/api)` |

`/en` 路由只负责返回前端页面（index.html），API 请求走 `/api/...` 不经过 `/en` 路由。两者完全独立。

#### /en/{path:path} 防护

虽然前端用绝对路径不会请求 `/en/api/xxx`，但后端加防护防止意外：

```python
if path.startswith("api/") or path.startswith("static/"):
    raise HTTPException(status_code=404)
```

### OAuth 回调语言前缀方案修正（2026-08-27）

#### 原方案问题

原方案 A（前端 init 跳转）：OAuth 回调后跳到 `/?token=xxx`，前端 init() 检测 grapeLang=en 再跳 `/en/?token=xxx` → 多一次页面刷新 → 闪动 + 慢。

#### 修正方案：OAuth state 带语言（方案 C）

把语言偏好存在 OAuth state 里，后端回调时直接跳正确 URL，不多一次刷新。

#### 完整链路

```
1. 前端点"GitCode 登录" → API.get('/auth/login/gitcode?lang=en')
2. 后端把 lang=en 存到 OAuth state → save_oauth_state(state, "gitcode", {"lang": "en"})
3. 用户在 GitCode 授权
4. GitCode 回调 → GET /api/auth/callback/gitcode?code=xxx&state=xxx
5. 后端 validate_oauth_state 拿到 state_data → 取 lang=en
6. 后端 RedirectResponse(f"/en/?token=xxx")  ← 直接跳正确 URL
7. 前端 init() → 检测 token → replaceState 保留 /en 前缀
```

#### 改动清单

**前端 oauthLogin()：**
```javascript
function oauthLogin() {
  const lang = localStorage.getItem('grapeLang') || 'zh';
  API.get('/auth/login/gitcode?lang=' + lang)
    .then(d => { if (d.redirect_url) window.location.href = d.redirect_url; })
    .catch(e => showToast(I18N.t('app.login.oauth_not_configured')));
}
```

**后端 gitcode_oauth_login()：**
```python
@router.get("/login/gitcode")
async def gitcode_oauth_login(request: Request, lang: str = "zh"):
    # ... 现有逻辑 ...
    # 把 lang 存到 OAuth state
    user_db.save_oauth_state(state, "gitcode", {"lang": lang})
    # ... 返回授权 URL ...
```

**后端 gitcode_oauth_callback()：**
```python
@router.get("/callback/gitcode")
async def gitcode_oauth_callback(code: str = "", state: str = "", error: str = ""):
    # ... 现有逻辑 ...
    state_data = user_db.validate_oauth_state(state, "gitcode")
    # 从 state 取语言偏好
    lang = "zh"
    if state_data and isinstance(state_data, dict):
        lang = state_data.get("lang", "zh")
    prefix = f"/{lang}" if lang != "zh" else ""
    # ... 换 token / 创建用户 / 签 JWT ...
    resp = RedirectResponse(url=f"{prefix}/?token={jwt_access_token}&first_login={'1' if not has_repos else '0'}", status_code=302)
    _set_refresh_cookie(resp, jwt_refresh_token)
    return resp
```

**前端 init() replaceState：**
```javascript
if (window.history.replaceState) {
  const lang = I18N.getLang();
  const prefix = lang === 'en' ? '/en' : '';
  window.history.replaceState(null, '', prefix + '/');
}
```

#### 场景验证

| 场景 | 前端传 lang | 后端跳转 | 结果 |
|------|:---:|------|:---:|
| 首次登录（未选语言） | zh | `/?token=xxx` | 中文 ✅ |
| 中文用户重新登录 | zh | `/?token=xxx` | 中文 ✅ |
| 英文用户重新登录 | en | `/en/?token=xxx` | 英文 ✅ |
| 英文用户首次登录后选英文 | zh → 选英文后刷新 | `/en/#dash` | 英文 ✅ |

#### 优点

1. 无额外页面刷新——后端直接跳正确 URL
2. 不依赖 localStorage——语言偏好在 OAuth state 里传递
3. 改动小——auth.py 加 3 行 + 前端 oauthLogin 改 1 行 + init replaceState 改 1 行
4. 首次登录用户没有 lang 参数 → 默认 zh → 跳 / → 正确
