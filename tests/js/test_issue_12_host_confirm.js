/*
 * Issue #12 回归测试：添加托管前确认弹窗
 *
 * 运行：node tests/js/test_issue_12_host_confirm.js
 *
 * 验证点：
 *   1. hostAndSelect 调用后先弹确认框（_grapeConfirm），不直接发起 API.post('/repos/host')
 *   2. 用户取消（不调 onConfirm）→ API.post 不被调用
 *   3. 用户确认（调 onConfirm）→ API.post('/repos/host') 被调用
 *   4. _grapeConfirm 不可用时回退 app.showConfirm，确认后仍发起请求
 *   5. 确认框文案使用 repos.host.confirm_title / repos.host.confirm / repos.host.confirm_btn
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

// ── 极简 DOM mock ──
function makeDom() {
  const elements = {};
  const ids = ['pageContent','ptContent','sidebar','mainView','tb','pageTitle'];
  for (const id of ids) {
    elements[id] = {
      _id: id,
      classList: { _set:new Set(), add(c){this._set.add(c);}, remove(c){this._set.delete(c);}, toggle(c,f){ if(f===undefined) f=!this._set.has(c); f?this._set.add(c):this._set.delete(c);}, contains(c){return this._set.has(c);} },
      style: {},
      innerHTML: '',
      textContent: '',
      value: '',
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
      closest: () => null,
    };
  }
  const dom = {
    getElementById(id) { return elements[id] || null; },
    querySelector: () => null,
    querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
    createElement: () => ({ style:{}, classList:elements['pageContent'].classList, textContent:'', innerHTML:'', appendChild:()=>{}, remove(){}, querySelector:()=>null }),
    body: { appendChild: () => {}, style:{} },
    documentElement: { setAttribute(){} },
    addEventListener: () => {},
  };
  return { dom, elements };
}

// ── 加载 repos.js 源码到 sandbox ──
const reposJsPath = path.resolve(__dirname, '../../src/api/static/js/pages/repos.js');
const src = fs.readFileSync(reposJsPath, 'utf8');

function loadRepos(dom, opts) {
  opts = opts || {};
  var apiPostCalls = [];
  var confirmOpts = null;        // _grapeConfirm 捕获
  var showConfirmOpts = null;    // app.showConfirm 捕获
  var toastMsgs = [];

  var API = {
    get: () => Promise.resolve({repos:[]}),
    post: function(url, body) { apiPostCalls.push({url, body}); return Promise.resolve({}); }
  };
  var I18N = { t: function(k){ return k; }, getLang: () => 'zh' };
  var app = {
    showToast: function(m){ toastMsgs.push(m); },
    loadProfile: () => Promise.resolve(),
    showConfirm: function(msg, onConfirm, o) { showConfirmOpts = {msg, onConfirm, o}; },
    initSidebarRepos: () => {},
    nav: () => {},
    currentRepo: () => null,
  };
  var window_ = {
    open: () => {},
    innerWidth: 800,
    innerHeight: 600,
    location: { hash:'' },
  };
  // _grapeConfirm 在浏览器中是 window 全局（window._grapeConfirm = fn），
  // repos.js 通过 bare identifier 调用；sandbox 需要单独注入变量
  var _grapeConfirmRef = null;
  if (opts.grapeConfirm !== false) {
    _grapeConfirmRef = function(o, event) { confirmOpts = o; _grapeConfirmEvent = event; };
    window_._grapeConfirm = _grapeConfirmRef;
  }
  var _grapeConfirmEvent = null;
  var localStorage = {
    _data: {},
    getItem(k) { return this._data[k] ?? null; },
    setItem(k,v) { this._data[k] = String(v); },
    removeItem(k) { delete this._data[k]; },
  };

  var sandbox = {
    document: dom,
    window: window_,
    localStorage,
    API,
    I18N,
    app,
    _grapeConfirm: _grapeConfirmRef,
    console: { log:()=>{}, error:()=>{}, warn:()=>{} },
    setTimeout, clearTimeout,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent,
  };
  var keys = Object.keys(sandbox);
  var vals = keys.map(k => sandbox[k]);
  // repos.js 定义全局函数（非 IIFE），直接 eval 到 sandbox 作用域
  var fn = new Function(...keys, src + '\n; return { hostAndSelect: typeof hostAndSelect !== "undefined" ? hostAndSelect : null, unhostRepo: typeof unhostRepo !== "undefined" ? unhostRepo : null };');
  var exported = fn(...vals);

  return { exported, apiPostCalls, getConfirmOpts: () => confirmOpts, showConfirmOpts: () => showConfirmOpts, toastMsgs, window: window_, app, API, getGrapeConfirmEvent: () => _grapeConfirmEvent };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch(e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

console.log('Issue #12: 添加托管前确认弹窗 — 回归测试\n');

// ─── 测试 1：hostAndSelect 先弹确认框，不直接发起 API.post ───
test('hostAndSelect: 调用后先弹 _grapeConfirm，不直接 API.post', () => {
  const { dom } = makeDom();
  const ctx = loadRepos(dom);
  ctx.exported.hostAndSelect('owner/repo', 'owner', 'repo', '');

  assert.ok(ctx.getConfirmOpts(), '应调用 _grapeConfirm');
  assert.strictEqual(ctx.apiPostCalls.length, 0, '确认前不应调用 API.post');
});

// ─── 测试 2：用户取消 → API.post 不被调用 ───
test('hostAndSelect: 用户取消（不调 onConfirm）→ API.post 不被调用', () => {
  const { dom } = makeDom();
  const ctx = loadRepos(dom);
  ctx.exported.hostAndSelect('owner/repo', 'owner', 'repo', '');

  // 不调用 onConfirm（模拟用户点取消）
  assert.strictEqual(ctx.apiPostCalls.length, 0, '取消后不应调用 API.post');
});

// ─── 测试 3：用户确认 → API.post('/repos/host') 被调用 ───
test('hostAndSelect: 用户确认（调 onConfirm）→ API.post 被调用', () => {
  const { dom } = makeDom();
  const ctx = loadRepos(dom);
  ctx.exported.hostAndSelect('owner/repo', 'owner', 'repo', '');

  assert.ok(ctx.getConfirmOpts().onConfirm, 'onConfirm 回调应存在');
  ctx.getConfirmOpts().onConfirm();  // 模拟用户点确认

  assert.strictEqual(ctx.apiPostCalls.length, 1, '确认后应调用一次 API.post');
  assert.strictEqual(ctx.apiPostCalls[0].url, '/repos/host', '应调用 /repos/host');
  assert.deepStrictEqual(ctx.apiPostCalls[0].body, {full_name: 'owner/repo'}, '请求体应含 full_name');
});

// ─── 测试 4：_grapeConfirm 不可用时回退 app.showConfirm ───
test('hostAndSelect: _grapeConfirm 不可用 → 回退 app.showConfirm', () => {
  const { dom } = makeDom();
  const ctx = loadRepos(dom, { grapeConfirm: false });
  ctx.exported.hostAndSelect('owner/repo', 'owner', 'repo', '');

  assert.ok(ctx.showConfirmOpts(), '应调用 app.showConfirm');
  assert.strictEqual(ctx.apiPostCalls.length, 0, '确认前不应调用 API.post');

  // 模拟用户确认
  ctx.showConfirmOpts().onConfirm();
  assert.strictEqual(ctx.apiPostCalls.length, 1, '确认后应调用 API.post');
});

// ─── 测试 5：确认框文案使用 repos.host.* i18n key ───
test('hostAndSelect: 确认框文案使用 repos.host.* i18n key', () => {
  const { dom } = makeDom();
  const ctx = loadRepos(dom);
  ctx.exported.hostAndSelect('owner/repo', 'owner', 'repo', '');

  assert.strictEqual(ctx.getConfirmOpts().title, 'repos.host.confirm_title', 'title 应使用 repos.host.confirm_title');
  assert.strictEqual(ctx.getConfirmOpts().message, 'repos.host.confirm', 'message 应使用 repos.host.confirm');
  assert.strictEqual(ctx.getConfirmOpts().confirmText, 'repos.host.confirm_btn', 'confirmText 应使用 repos.host.confirm_btn');
});

// ─── 测试 6：event 参数传递给 _grapeConfirm（用于定位） ───
test('hostAndSelect: event 参数透传给 _grapeConfirm', () => {
  const { dom } = makeDom();
  const ctx = loadRepos(dom);
  var fakeEvent = { clientX: 100, clientY: 200 };
  ctx.exported.hostAndSelect('o/r', 'o', 'r', '', fakeEvent);

  assert.strictEqual(ctx.getGrapeConfirmEvent(), fakeEvent, 'event 应透传给 _grapeConfirm');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
