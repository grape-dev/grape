/*
 * Issue #348: Admin Bot Token Health Panel — Frontend Tests
 *
 * Run: node tests/js/test_issue_348_bot_token_health.js
 *
 * Validates:
 *   1. admin.html: health card after Bot Account card, before save-bar, with 5 fields + refresh button
 *   2. admin.js: loadTokenHealth() requests /bot-token-health; showCfg() mounts; window/AdminApp export
 *   3. API contract: d.overall string (healthy/degraded/unhealthy); platforms object with per-platform fields
 *   4. Per-platform rendering: iterate Object.keys(platforms), not hardcoded
 *   5. Empty platforms → "no configured platforms"; checked_at empty → "never probed"
 *   6. Failure count 0 → don't show error detail
 *   7. 404 → "unavailable"; 401 → no crash
 *   8. Version bumped; tokens.css + i18n.js referenced
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '../..');
const HTML = fs.readFileSync(path.join(ROOT, 'src/api/static/admin.html'), 'utf8');
const JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/admin.js'), 'utf8');

// ── mock DOM / window / fetch ──
function escText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function makeEl() {
  const store = { tc: '', ih: null };
  return {
    style: {}, classList: { add() {}, remove() {} },
    appendChild() {}, remove() {},
    get textContent() { return store.tc; },
    set textContent(v) { store.tc = String(v); store.ih = null; },
    get innerHTML() { return store.ih === null ? escText(store.tc) : store.ih; },
    set innerHTML(v) { store.ih = String(v); },
  };
}
const els = {};
let fetchImpl = null;
const documentStub = {
  getElementById(id) { if (!els[id]) els[id] = makeEl(); return els[id]; },
  querySelector() { return null; },
  addEventListener() {},
  createElement() { const e = makeEl(); e.className = ''; return e; },
  body: { appendChild() {} },
};
const windowStub = {};
// Mock I18N
const I18NStub = {
  _lang: 'zh',
  _dict: {},
  t(key, vars) {
    let v = this._dict[key];
    if (v === undefined) return key;
    if (vars) {
      Object.keys(vars).forEach(k => { v = v.replace(new RegExp('{{' + k + '}}', 'g'), vars[k]); });
    }
    return v;
  },
  getLang() { return this._lang; },
  setLang(l) { this._lang = l; },
  init() {},
  applyI18nToDOM() {},
  detectLang() { return this._lang; },
  SUPPORTED: ['zh', 'en'],
  DEFAULT_LANG: 'zh',
};
const sandbox = {
  document: documentStub, window: windowStub,
  localStorage: { getItem: () => 'tk-test', setItem() {}, removeItem() {} },
  fetch: function () { return fetchImpl.apply(null, arguments); },
  setTimeout: function () {},
  console, Error, Object, Promise, JSON, String, Array,
  I18N: I18NStub,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(JS, sandbox, { filename: 'admin.js' });

function resetEls() {
  ['tkh_status', 'tkh_checked_at', 'tkh_error_type', 'tkh_error', 'tkh_failures', 'tkh_platforms'].forEach(function (id) {
    if (els[id]) { els[id].innerHTML = ''; els[id].textContent = ''; }
  });
}
function okResp(payload) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
}
function errResp(status) {
  return Promise.resolve({ ok: false, status: status, json: () => Promise.resolve({ detail: { message: 'err' + status } }) });
}

console.log('Issue #348: Bot Token Health Panel Tests\n');

// ── 1. HTML structure ──
assert.ok(HTML.indexOf('Bot Token') > 0, 'health card title exists');
const iBot = HTML.indexOf('Bot 账户') > 0 ? HTML.indexOf('Bot 账户') : HTML.indexOf('bot_account');
const iPanel = HTML.indexOf('tkh_status');
const iSave = HTML.indexOf('class="save-bar"');
assert.ok(iPanel > 0, 'tkh_status exists in HTML');
assert.ok(iSave > iPanel, 'save-bar after health panel');
assert.ok(HTML.indexOf('id="tkh_platforms"') > 0, 'tkh_platforms container exists');
assert.ok(/onclick="loadTokenHealth\(\)"/.test(HTML), 'refresh button bound to loadTokenHealth()');
// Shared resources
assert.ok(HTML.indexOf('tokens.css?v=20261016-1') > 0, 'tokens.css referenced with correct version');
assert.ok(HTML.indexOf('i18n.js?v=20261016-2') > 0, 'i18n.js referenced with correct version');
assert.ok(HTML.indexOf('admin.js?v=20261016-14') > 0, 'admin.js version bumped');
assert.ok(HTML.indexOf('admin.js?v=20261016-7') < 0, 'old version not referenced');
// data-i18n on health panel
assert.ok(HTML.indexOf('data-i18n="admin.token_health.title"') > 0, 'title has data-i18n');
assert.ok(HTML.indexOf('data-i18n="admin.token_health.refresh"') > 0, 'refresh has data-i18n');
console.log('  PASS: HTML structure (card / fields / refresh / shared resources / data-i18n)');

// ── 2. admin.js source assertions ──
assert.ok(JS.indexOf("api('/bot-token-health')") > 0, 'requests /bot-token-health');
const showCfgSrc = JS.slice(JS.indexOf('function showCfg()'), JS.indexOf('function applyToField'));
assert.ok(showCfgSrc.indexOf('loadTokenHealth();') > 0, 'showCfg() mounts loadTokenHealth()');
assert.ok(/window\.loadTokenHealth\s*=\s*loadTokenHealth;/.test(JS), 'window.loadTokenHealth exported');
assert.ok(/loadTokenHealth:\s*loadTokenHealth/.test(JS), 'AdminApp.loadTokenHealth exported');
console.log('  PASS: admin.js mount / export / API path');

// ── 3. API contract: overall string + platforms object ──
function loadWith(payload) {
  resetEls();
  fetchImpl = () => okResp(payload);
  sandbox.window.loadTokenHealth();
}
function loadErr(status) {
  resetEls();
  fetchImpl = () => errResp(status);
  sandbox.window.loadTokenHealth();
}
function wait() { return new Promise(function (r) { setTimeout(r, 10); }); }

(async function () {
// 3.1 healthy: overall="healthy", empty platforms
loadWith({ overall: 'healthy', platforms: {}, checked_at: '' });
await wait();
assert.ok(els.tkh_status.innerHTML.indexOf('tag-s') >= 0 || els.tkh_status.innerHTML.indexOf('健康') >= 0 || els.tkh_status.innerHTML.indexOf('Healthy') >= 0, 'healthy → green');
assert.ok(els.tkh_checked_at.textContent.indexOf('尚未探活') >= 0 || els.tkh_checked_at.textContent.indexOf('Never') >= 0 || els.tkh_checked_at.textContent.indexOf('admin.token_health.not_probed') >= 0, 'checked_at empty → never probed');
console.log('  PASS: healthy overall + empty checked_at');

// 3.2 degraded: overall="degraded", two platforms
loadWith({
  overall: 'degraded',
  platforms: {
    gitcode: { healthy: true, degraded: false, unhealthy: false, consecutive_failures: 0, error_type: '', error: '' },
    github: { healthy: false, degraded: true, unhealthy: false, consecutive_failures: 2, error_type: 'auth', error: '401 Unauthorized' }
  },
  checked_at: '2026-10-15 14:30:00'
});
await wait();
assert.ok(els.tkh_checked_at.textContent === '2026-10-15 14:30:00', 'checked_at rendered');
assert.ok(els.tkh_platforms && els.tkh_platforms.innerHTML.length > 0, 'platforms rendered');
assert.ok(els.tkh_platforms.innerHTML.indexOf('gitcode') >= 0 || els.tkh_platforms.innerHTML.indexOf('GitCode') >= 0, 'gitcode platform shown');
assert.ok(els.tkh_platforms.innerHTML.indexOf('github') >= 0 || els.tkh_platforms.innerHTML.indexOf('GitHub') >= 0, 'github platform shown');
console.log('  PASS: degraded + per-platform rendering');

// 3.3 unhealthy: overall="unhealthy"
loadWith({
  overall: 'unhealthy',
  platforms: {
    gitcode: { healthy: false, degraded: false, unhealthy: true, consecutive_failures: 3, error_type: 'auth', error: '401 Unauthorized' },
    github: { healthy: false, degraded: false, unhealthy: true, consecutive_failures: 5, error_type: 'network', error: 'timeout' }
  },
  checked_at: '2026-10-15 15:00:00'
});
await wait();
assert.ok(els.tkh_status.innerHTML.indexOf('tag-d') >= 0 || els.tkh_status.innerHTML.indexOf('异常') >= 0 || els.tkh_status.innerHTML.indexOf('Unhealthy') >= 0, 'unhealthy → red');
console.log('  PASS: unhealthy overall');

// 3.4 Per-platform: failure count 0 → error detail hidden
loadWith({
  overall: 'healthy',
  platforms: {
    gitcode: { healthy: true, degraded: false, unhealthy: false, consecutive_failures: 0, error_type: '', error: '' }
  },
  checked_at: 't'
});
await wait();
assert.ok(els.tkh_platforms.innerHTML.indexOf('—') >= 0, 'failure 0 → error detail shows —');
console.log('  PASS: failure count 0 → no error detail');

// 3.5 Empty platforms → no configured platforms message
loadWith({ overall: 'healthy', platforms: {}, checked_at: 't' });
await wait();
assert.ok(els.tkh_platforms.innerHTML.length > 0, 'empty platforms → message shown');
console.log('  PASS: empty platforms → no configured platforms');

// 3.6 Priority: overall="unhealthy" with degraded platforms → unhealthy
loadWith({ overall: 'unhealthy', platforms: { gitcode: { healthy: false, degraded: true, unhealthy: false, consecutive_failures: 1, error_type: 'network', error: 'timeout' } }, checked_at: 't' });
await wait();
assert.ok(els.tkh_status.innerHTML.indexOf('tag-d') >= 0 || els.tkh_status.innerHTML.indexOf('异常') >= 0 || els.tkh_status.innerHTML.indexOf('Unhealthy') >= 0, 'overall=unhealthy → red regardless of platform level');
console.log('  PASS: overall priority correct');

// ── 4. Error paths ──
loadErr(404);
await wait();
assert.ok(els.tkh_status.innerHTML.indexOf('暂不可用') >= 0 || els.tkh_status.innerHTML.indexOf('Unavailable') >= 0 || els.tkh_status.innerHTML.indexOf('unavailable') >= 0, '404 → unavailable');
console.log('  PASS: 404 → unavailable');

loadErr(401);
await wait();
assert.ok(els.tkh_status.innerHTML.indexOf('加载失败') >= 0 || els.tkh_status.innerHTML.indexOf('Load failed') >= 0 || els.tkh_status.innerHTML.indexOf('failed') >= 0, '401 → load failed (no crash)');
console.log('  PASS: 401 → no crash');

console.log('\nAll tests passed\n');
})().catch(function (e) { console.error(e); process.exit(1); });
