/* app.js 加载冒烟：证明 IIFE 能在最小沙箱里跑完并挂出 window.app。
 *
 * 回归背景：PR #1377 复审只做了 `node --check`（语法过），但 app.js 的返回对象引用了
 * 已删除的 `search` 函数 → IIFE 求值时 ReferenceError，window.app 永不出现，整个壳空白。
 * 语法检查抓不到未定义标识符，故本用例真跑一遍加载。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const appJs = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/shell/js/app.js'), 'utf8');

function makeEl() {
  return {
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, f) { if (f === undefined) f = !this._s.has(c); f ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); } },
    style: {}, innerHTML: '', textContent: '', value: '', options: [], selectedIndex: -1,
    dataset: {}, nextElementSibling: null,
    querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {},
    closest: () => null, appendChild: () => {}, remove: () => {}, focus: () => {},
  };
}

const store = {};
const els = {};
const document = {
  getElementById: (id) => (els[id] = els[id] || makeEl()),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeEl(),
  body: makeEl(),
  documentElement: { setAttribute: () => {}, removeAttribute: () => {}, classList: makeEl().classList },
  addEventListener: () => {},
};
const localStorage = {
  _d: store, getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; },
  clear: () => { for (const k of Object.keys(store)) delete store[k]; },
};

const sandbox = {
  document, localStorage, sessionStorage: localStorage,
  window: { location: { hash: '', pathname: '/', search: '', href: 'http://x/' },
    history: { pushState: () => {}, replaceState: () => {}, state: null },
    addEventListener: () => {}, dispatchEvent: () => {}, matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    localStorage, sessionStorage: localStorage, navigator: { userAgent: 'node' } },
  navigator: { userAgent: 'node', language: 'zh' },
  location: { hash: '', pathname: '/', search: '', href: 'http://x/' },
  history: { pushState: () => {}, replaceState: () => {}, state: null },
  API: { get: () => Promise.resolve({}), post: () => Promise.resolve({}), put: () => Promise.resolve({}), del: () => Promise.resolve({}) },
  I18N: { t: (k) => k, getLang: () => 'zh', init: () => {} },
  fmtLocalTime: () => '-',
  fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
  console: { log: () => {}, error: () => {}, warn: () => {}, info: () => {} },
  setTimeout, clearTimeout, setInterval, clearInterval,
  Date, JSON, Math, URLSearchParams, CustomEvent: function () {}, AbortController: function () { this.abort = () => {}; this.signal = {}; },
};

const keys = Object.keys(sandbox);
const app = new Function(...keys, appJs + '\n; return (typeof app !== "undefined") ? app : null;')(...keys.map(k => sandbox[k]));

assert.ok(app, 'app.js 加载后没有挂出 app 对象（IIFE 提前抛错？）');
for (const fn of ['init', 'nav', 'loadRepos', 'syncAppPath', 'toggleSidebar', 'showToast']) {
  assert.strictEqual(typeof app[fn], 'function', 'app.' + fn + ' 缺失');
}
console.log('✓ app.js 加载冒烟通过：' + Object.keys(app).length + ' 个导出');