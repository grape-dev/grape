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
