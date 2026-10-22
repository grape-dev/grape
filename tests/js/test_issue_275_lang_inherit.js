/*
 * Issue #275 回归测试：工作台语言继承首页选择
 *
 * 运行：node tests/js/test_issue_275_lang_inherit.js
 *
 * 根因：官网首页（localStorage key=grape-lang）与工作台（key=grapeLang）
 * 使用不同 key，首页切换语言后新打开的工作台不继承。
 * 修复：detectLang() 在 grapeLang 未设置时兼容读取 grape-lang。
 *
 * 验证点（优先级链：URL /en/ 前缀 → grapeLang → grape-lang → 默认 zh）：
 *   1. 无前缀 + grapeLang=en → en（工作台自身状态，基线）
 *   2. 无前缀 + 仅 grape-lang=en → en（首页切英文后进工作台——核心修复）
 *   3. 无前缀 + 仅 grape-lang=zh → zh（首页中文 → 工作台中文）
 *   4. 无前缀 + 两 key 均无 → zh（默认，基线）
 *   5. /en/app/ 前缀 + 仅 grape-lang=zh → en（URL 前缀优先级最高）
 *   6. grapeLang=zh + grape-lang=en → zh（grapeLang 优先于 grape-lang）
 *   7. init() 检测为 en 后写 cookie grapeLang=en（服务端一致性不回归）
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

// ── 极简 DOM/location mock ──
function makeEnv(opts) {
  opts = opts || {};
  const store = {};
  const localStorage = {
    getItem(k) { return (k in store) ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
    clear() { for (const k of Object.keys(store)) delete store[k]; },
  };
  if (opts.grapeLang !== undefined) store['grapeLang'] = opts.grapeLang;
  if (opts.siteLang !== undefined) store['grape-lang'] = opts.siteLang;

  const cookieJar = { value: '' };
  const document = {
    documentElement: { setAttribute() {}, removeAttribute() {}, hasAttribute() { return false; } },
    querySelectorAll() { const a = []; a.forEach = Array.prototype.forEach; return a; },
    cookie: '',
    set cookieVal(v) { cookieJar.value = v; },
    get cookieVal() { return cookieJar.value; },
  };
  // document.cookie setter 捕获
  Object.defineProperty(document, 'cookie', {
    set(v) { cookieJar.value = v; },
    get() { return cookieJar.value; },
  });

  const window = {
    location: {
      pathname: opts.pathname || '/',
      hash: '',
      search: '',
    },
    history: { replaceState() {}, pushState() {} },
    addEventListener() {},
    fetch: () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) }),
  };

  return { localStorage, document, window, cookieJar, store };
}

// ── 加载 i18n.js 源码（IIFE 赋值 var I18N，用 new Function + sandbox 求值）──
const i18nPath = path.resolve(__dirname, '../../src/api/static/js/i18n.js');
const src = fs.readFileSync(i18nPath, 'utf8');

function loadI18N(env) {
  const sandbox = {
    window: env.window,
    document: env.document,
    localStorage: env.localStorage,
    console: { log() {}, warn() {}, error() {} },
    fetch: env.window.fetch,
    setTimeout, clearTimeout, Promise, Date, JSON,
  };
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  // 严格模式 IIFE：内部 var I18N 不进 sandbox——通过源码尾部 return 引出
  const wrapped = src + '\n;return typeof I18N !== "undefined" ? I18N : null;';
  const fn = new Function(...keys, wrapped);
  return fn(...vals);
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('Issue #275: 工作台语言继承首页选择 — 回归测试\n');

// ─── 测试 1：无前缀 + grapeLang=en → en（基线）───
test('detectLang: 无前缀 + grapeLang=en → en（基线）', () => {
  const env = makeEnv({ grapeLang: 'en' });
  const I18N = loadI18N(env);
  assert.strictEqual(I18N.detectLang(), 'en',
    'grapeLang=en 应检测为 en，实际为 ' + I18N.detectLang());
});

// ─── 测试 2：无前缀 + 仅 grape-lang=en → en（核心修复）───
test('detectLang: 无前缀 + 仅 grape-lang=en → en（首页切英文后进工作台）', () => {
  const env = makeEnv({ siteLang: 'en' });
  const I18N = loadI18N(env);
  assert.strictEqual(I18N.detectLang(), 'en',
    '仅 grape-lang=en（首页语言）应桥接检测为 en，实际为 ' + I18N.detectLang());
});

// ─── 测试 3：无前缀 + 仅 grape-lang=zh → zh ───
test('detectLang: 无前缀 + 仅 grape-lang=zh → zh（首页中文 → 工作台中文）', () => {
  const env = makeEnv({ siteLang: 'zh' });
  const I18N = loadI18N(env);
  assert.strictEqual(I18N.detectLang(), 'zh',
    '仅 grape-lang=zh 应检测为 zh，实际为 ' + I18N.detectLang());
});

// ─── 测试 4：无前缀 + 两 key 均无 → zh（默认）───
test('detectLang: 无前缀 + 两 key 均无 → zh（默认，基线）', () => {
  const env = makeEnv({});
  const I18N = loadI18N(env);
  assert.strictEqual(I18N.detectLang(), 'zh',
    '无任何语言状态应默认 zh，实际为 ' + I18N.detectLang());
});

// ─── 测试 5：/en/app/ 前缀 + 仅 grape-lang=zh → en（URL 优先）───
test('detectLang: /en/app/ 前缀 + 仅 grape-lang=zh → en（URL 前缀优先级最高）', () => {
  const env = makeEnv({ pathname: '/en/app/', siteLang: 'zh' });
  const I18N = loadI18N(env);
  assert.strictEqual(I18N.detectLang(), 'en',
    'URL /en/ 前缀应优先于 grape-lang，实际为 ' + I18N.detectLang());
});

// ─── 测试 6：grapeLang=zh + grape-lang=en → zh（grapeLang 优先）───
test('detectLang: grapeLang=zh + grape-lang=en → zh（工作台自身状态优先）', () => {
  const env = makeEnv({ grapeLang: 'zh', siteLang: 'en' });
  const I18N = loadI18N(env);
  assert.strictEqual(I18N.detectLang(), 'zh',
    'grapeLang 应优先于 grape-lang，实际为 ' + I18N.detectLang());
});

// ─── 测试 7：init() 检测为 en 后写 cookie grapeLang=en ───
test('init: 桥接检测 en 后写 cookie grapeLang=en（服务端一致性不回归）', () => {
  const env = makeEnv({ siteLang: 'en' });
  const I18N = loadI18N(env);
  I18N.init('test');
  assert.strictEqual(I18N.getLang(), 'en',
    'init() 后当前语言应为 en，实际为 ' + I18N.getLang());
  assert.ok(env.cookieJar.value.indexOf('grapeLang=en') >= 0,
    'cookie 应写入 grapeLang=en，实际为 "' + env.cookieJar.value + '"');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
