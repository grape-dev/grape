# Plan: Issue #275 — 工作台语言继承首页选择

## 方案

单点修改 `src/api/static/js/i18n.js` 的 `detectLang()`：

```js
function detectLang() {
  var path = window.location.pathname || '';
  // /en/ or /en at start → en
  if (path === '/en' || path.indexOf('/en/') === 0) return 'en';
  // No URL prefix → check localStorage
  try {
    var stored = localStorage.getItem(STORAGE_KEY);        // grapeLang（工作台 key）
    if (stored && SUPPORTED.indexOf(stored) >= 0) return stored;
    // Issue #275: 官网首页用 grape-lang key——未设置 grapeLang 时兼容读取，
    // 使首页切换语言后新打开的工作台继承该选择（仅读取，不回写）
    var siteLang = localStorage.getItem('grape-lang');
    if (siteLang && SUPPORTED.indexOf(siteLang) >= 0) return siteLang;
  } catch (e) {}
  return DEFAULT_LANG;
}
```

优先级链：**URL `/en/` 前缀 → `grapeLang`（工作台自身状态）→ `grape-lang`（首页桥接）→ 默认 zh**。

不回写理由：`init()` 已把检测到的语言 `_setCookie(grapeLang)` 双写 cookie；若回写 `grapeLang`，用户在工作台内切回中文后，首页的 `grape-lang` 与工作台 `grapeLang` 会互相覆盖语义（各页面各自维护，仅做单向桥接初始化）。`setLang()` 在工作台内切换语言时正常写 `grapeLang`——一旦用户在工作台内切换过，优先级链中 `grapeLang` 有值，`grape-lang` 不再参与判定，两页面互不干扰。

## 测试设计（tests/js/test_issue_275_lang_inherit.js）

沿用 `test_issue_273_en_prefix_refresh.js` 的 DOM mock 模式，直接 `require` 加载 `i18n.js` 源码（IIFE 赋值到 `var I18N`，用 `new Function` + sandbox 求值），覆盖 6 场景：

1. URL 无前缀 + `grapeLang=en` → en（工作台自身状态，基线）
2. URL 无前缀 + 仅 `grape-lang=en`（首页切英文后进工作台）→ en（**核心修复**）
3. URL 无前缀 + 仅 `grape-lang=zh` → zh（首页中文 → 工作台中文）
4. URL 无前缀 + 两 key 均无 → zh（默认，基线）
5. URL `/en/app/` 前缀 + 仅 `grape-lang=zh` → en（URL 前缀优先级最高，覆盖首页桥接）
6. `grapeLang=zh` + `grape-lang=en` → zh（grapeLang 优先于 grape-lang，工作台内切换过语言不受首页影响）

另验证：检测为 en 后 `init()` 写 cookie `grapeLang=en`（保持服务端一致性逻辑不回归）。

## 影响面

| 文件 | 改动 |
|------|------|
| `src/api/static/js/i18n.js` | detectLang() +5 行 + 机制注释 |
| `tests/js/test_issue_275_lang_inherit.js` | 新增 ~150 行 |
| `docs/issue-275/spec.md` | 需求文档 |

无后端改动、无路由改动、无 `setLang`/`applyI18nToDOM` 改动 → 回归风险极低。i18n.js 修改需 bump `index.html` 引用版本号（`?v=N`）以破缓存。
