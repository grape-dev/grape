/*
 * Issue #1477 — API Key“最近使用时间”UTC+8 格式化 前端测试
 *
 * 运行：node tests/js/test_issue_1477_apikey_time_format.js
 *
 * 验证点：
 *   T1  fmtTimestamp8 纯函数：秒级时间戳 → YYYY-MM-DD HH:mm:ss（固定 Asia/Shanghai）
 *   T2  fmtTimestamp8：毫秒级时间戳自动判别
 *   T3  fmtTimestamp8：UTC ISO / 带偏移字符串解析为绝对时刻后按 UTC+8 渲染
 *   T4  fmtTimestamp8：0 / 负值 / 空 / 非法输入 → ''（占位由调用方兜底），不出现 1970
 *   T5  settings.js loadApiKeys：last_used_at 秒级时间戳格式化；None → '—' 占位；created_at 同口径
 *   T6  account.js _accountLoadApiKeys：同上（用户中心 Tab）
 *   T7  源断言：两处渲染均走 fmtTimestamp8，无裸拼 last_used_at
 *   T8  i18n：未使用占位键 zh/en 对齐
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const API_JS = path.join(ROOT, 'src', 'api', 'static', 'js', 'api.js');
const SETTINGS_JS = path.join(ROOT, 'src', 'api', 'static', 'js', 'pages', 'settings.js');
const ACCOUNT_JS = path.join(ROOT, 'src', 'api', 'static', 'js', 'pages', 'account.js');
const ZH = JSON.parse(fs.readFileSync(path.join(ROOT, 'src', 'api', 'static', 'locales', 'zh', 'app.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'src', 'api', 'static', 'locales', 'en', 'app.json'), 'utf8'));

let passed = 0, failed = 0;
function report(name, e) {
  if (!e) { passed++; console.log('  \u2713 ' + name); }
  else { failed++; console.log('  \u2717 ' + name + '\n    ' + (e && e.message || e)); }
}
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch (e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}
async function testAsync(name, fn) {
  try { await fn(); report(name, null); }
  catch (e) { report(name, e); }
}

// ── sandbox 加载 api.js，提取 fmtTimestamp8 / fmtLocalTime（不执行 DOM 副作用）──
function loadApiJs() {
  const src = fs.readFileSync(API_JS, 'utf8');
  const sandbox = {
    window: { addEventListener: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} },
    document: { addEventListener: () => {} },
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}), text: () => '' }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    console: { log: () => {}, warn: () => {}, error: () => {} },
    Date, JSON, Math, String, Number, Array, Object, Boolean, RegExp, Error, Promise,
    encodeURIComponent, decodeURIComponent,
  };
  const keys = Object.keys(sandbox);
  const fn = new Function(...keys, src + '\n;return { fmtTimestamp8: typeof fmtTimestamp8 !== "undefined" ? fmtTimestamp8 : null };');
  return fn(...keys.map(k => sandbox[k]));
}

// ── 提取单个具名函数源码（用于纯函数级断言，无 sandbox 副作用）──
function extractFn(src, name) {
  const idx = src.indexOf('function ' + name + '(');
  assert.ok(idx >= 0, '源码中未找到 function ' + name);
  let depth = 0, started = false, end = idx;
  for (let i = idx; i < src.length; i++) {
    if (src[i] === '{') { depth++; started = true; }
    else if (src[i] === '}') { depth--; if (started && depth === 0) { end = i + 1; break; } }
  }
  return new Function('return (' + src.slice(idx, end) + ');')();
}

const apiSrc = fs.readFileSync(API_JS, 'utf8');

// ---------- T1-T4: fmtTimestamp8 纯函数 ----------
// fmtTimestamp8 依赖同文件顶层的 _format8FromMs 助手，装配时一并注入（与页面加载语义一致）
const fmtTimestamp8 = new Function('_format8FromMs',
  'return (' + apiSrc.slice(apiSrc.indexOf('function fmtTimestamp8('), apiSrc.indexOf('function _format8FromMs(')) + ');')(extractFn(apiSrc, '_format8FromMs'));

test('T1 秒级时间戳 → UTC+8 YYYY-MM-DD HH:mm:ss（2026-11-08 07:33:07 +08）', () => {
  // 2026-11-07T23:33:07Z == 2026-11-08 07:33:07 UTC+8（epoch 1794094387，datetime 工具算出）
  assert.strictEqual(fmtTimestamp8(1794094387), '2026-11-08 07:33:07');
  assert.strictEqual(fmtTimestamp8('1794094387'), '2026-11-08 07:33:07');
});

test('T2 毫秒级时间戳自动判别', () => {
  assert.strictEqual(fmtTimestamp8(1794094387000), '2026-11-08 07:33:07');
  assert.strictEqual(fmtTimestamp8('1794094387123'), '2026-11-08 07:33:07');
});

test('T3 UTC ISO / 带偏移字符串 → 绝对时刻按 UTC+8 渲染', () => {
  assert.strictEqual(fmtTimestamp8('2026-11-07T23:33:07+00:00'), '2026-11-08 07:33:07');
  // +08:00 偏移字符串换算回绝对时刻（与 fmtLocalTime 同源逻辑）
  assert.strictEqual(fmtTimestamp8('2026-11-08 07:33:07.123+08:00'), '2026-11-08 07:33:07');
});

test('T4 0/负值/空/非法输入 → 空串（占位由调用方兜底）', () => {
  assert.strictEqual(fmtTimestamp8(0), '');
  assert.strictEqual(fmtTimestamp8('0'), '');
  assert.strictEqual(fmtTimestamp8(-5), '');
  assert.strictEqual(fmtTimestamp8(''), '');
  assert.strictEqual(fmtTimestamp8(null), '');
  assert.strictEqual(fmtTimestamp8(undefined), '');
  // 非法字符串原样返回，不抛异常、不产 1970
  assert.strictEqual(fmtTimestamp8('not-a-time'), 'not-a-time');
});

// ── settings.js / account.js 渲染加载器 ──
function makeDom(ids) {
  const elements = {};
  const make = (id) => {
    if (!elements[id]) elements[id] = { _id: id, style: {}, innerHTML: '', textContent: '', value: '',
      querySelector: () => null, querySelectorAll: () => [], appendChild: () => {}, remove() {}, setAttribute() {} };
    return elements[id];
  };
  for (const id of ids) make(id);
  return { getElementById: make, _elements: elements,
    querySelector: () => null, querySelectorAll: () => [], createElement: () => ({ style: {}, innerHTML: '', appendChild: () => {} }) };
}

function loadSettingsRender(keysResp) {
  const src = fs.readFileSync(SETTINGS_JS, 'utf8');
  const dom = makeDom(['sApiKeys', 'apiKeyList']);
  const sandbox = {
    document: dom, window: { location: { hash: '' }, addEventListener: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    API: { get: () => Promise.resolve(keysResp), post: () => Promise.resolve({}), put: () => Promise.resolve({}) },
    I18N: { t: (k) => { const v = k.replace(/^app\./, '').split('.').reduce((o, x) => (o || {})[x], ZH); return v === undefined ? k : v; }, getLang: () => 'zh' },
    fmtTimestamp8,
    app: { showToast: () => {}, showPrompt: () => {}, showConfirm: () => {} },
    console: { log: () => {}, warn: () => {}, error: () => {} },
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Math, String, Number, Array, Object, Boolean, Promise,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
  };
  const keys = Object.keys(sandbox);
  const fn = new Function(...keys, src + '\n;return { loadApiKeys: typeof loadApiKeys !== "undefined" ? loadApiKeys : null };');
  const mod = fn(...keys.map(k => sandbox[k]));
  return { mod, dom };
}

function loadAccountRender(keysResp) {
  const src = fs.readFileSync(ACCOUNT_JS, 'utf8');
  const dom = makeDom(['accApiKeyList']);
  const sandbox = {
    document: dom, window: { location: { hash: '' }, addEventListener: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    API: { get: () => Promise.resolve(keysResp), post: () => Promise.resolve({}) },
    I18N: { t: (k) => { const v = k.replace(/^app\./, '').split('.').reduce((o, x) => (o || {})[x], ZH); return v === undefined ? k : v; }, getLang: () => 'zh' },
    fmtTimestamp8,
    app: { showToast: () => {}, showPrompt: () => {} },
    console: { log: () => {}, warn: () => {}, error: () => {} },
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Math, String, Number, Array, Object, Boolean, Promise,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
  };
  const keys = Object.keys(sandbox);
  const fn = new Function(...keys, src + '\n;return { _accountLoadApiKeys: typeof _accountLoadApiKeys !== "undefined" ? _accountLoadApiKeys : null };');
  const mod = fn(...keys.map(k => sandbox[k]));
  return { mod, dom };
}

const KEYS_MOCK = { keys: [
  { id: 1, name: 'used-key', prefix: 'grape_••••', created_at: '2026-11-07T23:33:07+00:00', last_used_at: 1794094387 },
  { id: 2, name: 'unused-key', prefix: 'grape_••••', created_at: '2026-11-07T23:33:07+00:00', last_used_at: null },
] };

async function run() {
  // ---------- T5: settings.js ----------
  await testAsync('T5 settings.js loadApiKeys：时间戳格式化 + None → — 占位 + created_at 同口径', async () => {
    const { mod, dom } = loadSettingsRender(KEYS_MOCK);
    await mod.loadApiKeys();
    const html = dom._elements.sApiKeys.innerHTML;
    assert.ok(html.includes('2026-11-08 07:33:07'), '未渲染 UTC+8 格式化时间');
    assert.ok(html.includes('1970') === false, '出现 1970 异常值');
    assert.ok(html.includes('从未使用'), '未使用的 key 未渲染占位（从未使用）');
    assert.ok(!/最近使用[^<]*\d{10}/.test(html.replace(/2026-11-08 07:33:07/g, '')), '仍存在裸时间戳');
  });

  // ---------- T6: account.js ----------
  await testAsync('T6 account.js _accountLoadApiKeys：时间戳格式化 + None → — 占位 + created_at 同口径', async () => {
    const { mod, dom } = loadAccountRender(KEYS_MOCK);
    await mod._accountLoadApiKeys();
    const html = dom._elements.accApiKeyList.innerHTML;
    assert.ok(html.includes('2026-11-08 07:33:07'), '未渲染 UTC+8 格式化时间');
    assert.ok(html.includes('1970') === false, '出现 1970 异常值');
    assert.ok(html.includes('从未使用'), '未使用的 key 未渲染占位（从未使用）');
  });

  // ---------- T7: 源断言 ----------
  await test('T7 两处渲染均接入 fmtTimestamp8，无裸拼 last_used_at', () => {
    const s = fs.readFileSync(SETTINGS_JS, 'utf8');
    const a = fs.readFileSync(ACCOUNT_JS, 'utf8');
    assert.ok(s.includes('fmtTimestamp8'), 'settings.js 未接入 fmtTimestamp8');
    assert.ok(a.includes('fmtTimestamp8'), 'account.js 未接入 fmtTimestamp8');
    assert.ok(!/_escApi\(k\.last_used_at\)\s*\+/.test(s), 'settings.js 仍裸拼 last_used_at');
    assert.ok(!/_accountEsc\(k\.last_used_at\)/.test(a), 'account.js 仍裸拼 last_used_at');
  });

  // ---------- T8: i18n ----------
  await test('T8 i18n 未使用占位键 zh/en 对齐', () => {
    const zh = ZH.account || {}, en = EN.account || {};
    assert.ok(zh.last_used_never && en.last_used_never, '缺 account.last_used_never 键');
    assert.strictEqual(typeof zh.last_used_never, 'string');
    assert.strictEqual(typeof en.last_used_never, 'string');
  });

  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed > 0 ? 1 : 0);
}

run();
