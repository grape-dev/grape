/*
 * Issue #205 回归测试：内测申请弹窗防误关闭 + 表单数据 sessionStorage 持久化
 *
 * 运行：node tests/js/test_issue_205_beta_apply_persist.js
 *
 * 验证点：
 *   1. openBetaApply() 创建的 overlay 不再绑定「点击遮罩即关闭」逻辑
 *   2. 表单字段 input 后写入 sessionStorage(beta_apply_form)
 *   3. 重新打开弹窗时从 sessionStorage 自动回填已填字段
 *   4. 提交成功后 sessionStorage(beta_apply_form) 被清空
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

// ── DOM mock ──
function makeDom() {
  const elements = {};
  function mkEl(id) {
    const el = {
      _id: id,
      classList: { _set: new Set(), add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); }, toggle(c, f) { if (f === undefined) f = !this._set.has(c); f ? this._set.add(c) : this._set.delete(c); }, contains(c) { return this._set.has(c); } },
      style: {},
      innerHTML: '',
      textContent: '',
      _value: '',
      get value() { return this._value; },
      set value(v) { this._value = String(v); },
      dataset: {},
      checked: false,
      type: 'text',
      name: '',
      tagName: 'DIV',
      eventListeners: {},
      addEventListener(ev, fn) { (this.eventListeners[ev] = this.eventListeners[ev] || []).push(fn); },
      removeEventListener() {},
      dispatchEvent(ev) { (this.eventListeners[ev.type] || []).forEach(fn => fn(ev)); },
      setAttribute() {},
      removeAttribute() {},
      getAttribute() { return null; },
      querySelector: () => null,
      querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
      remove() { this._removed = true; },
      appendChild() {},
      parentElement: null,
      closest: () => null,
      nextElementSibling: null,
      focus() {},
      click() {},
    };
    return el;
  }
  const dom = {
    getElementById(id) { if (!elements[id]) elements[id] = mkEl(id); return elements[id]; },
    querySelector: () => null,
    querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
    createElement(tag) {
      const el = mkEl('created');
      el.tagName = (tag || 'div').toUpperCase();
      // 当 .id 被赋值时注册到 elements，使 getElementById 能查到
      Object.defineProperty(el, 'id', {
        configurable: true,
        get() { return this._idVal || ''; },
        set(v) { this._idVal = v; if (v) elements[v] = this; },
      });
      return el;
    },
    body: { appendChild() {} },
    documentElement: { setAttribute() {}, removeAttribute() {}, getAttribute() { return null; } },
    addEventListener: () => {},
  };
  return { dom, elements };
}

function makeSessionStorage() {
  const store = {};
  return {
    _data: store,
    getItem(k) { return store[k] ?? null; },
    setItem(k, v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
    clear() { for (const k of Object.keys(store)) delete store[k]; },
  };
}

const appJsPath = path.resolve(__dirname, '../../src/api/static/js/app.js');
const src = fs.readFileSync(appJsPath, 'utf8');

function loadApp(dom, sessionStorage) {
  const window = { _currentView: '', history: { pushState() {}, replaceState() {}, state: null }, location: { hash: '' }, addEventListener: () => {} };
  const API = {
    get: () => Promise.resolve({ repos: [] }),
    post: (url) => {
      if (url === '/settings/beta-apply/submit') return Promise.resolve({ success: true, message: 'ok' });
      return Promise.resolve({});
    },
  };
  const I18N = { t: (k) => k, getLang: () => 'zh' };
  const console_ = { log: () => {}, error: () => {}, warn: () => {} };
  const sandbox = {
    document: dom, window,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} },
    sessionStorage,
    API, I18N, console: console_,
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math, encodeURIComponent, decodeURIComponent,
    URLSearchParams, CustomEvent: function() {}, AbortController: function() { this.abort = () => {}; this.signal = {}; },
    requestAnimationFrame: (fn) => { setTimeout(fn, 0); return 1; },
  };
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  const fn = new Function(...keys, src + '\n; return (typeof app !== "undefined") ? app : null;');
  const app = fn(...vals);
  return { app, window, sessionStorage };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch (e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

console.log('Issue #205: 内测申请弹窗防误关闭 + sessionStorage 持久化 — 回归测试\n');

// ─── 测试 1：overlay 不绑定 click→remove ───
test('openBetaApply: overlay 不绑定 click→remove（点击遮罩不关闭）', () => {
  const { dom } = makeDom();
  const ss = makeSessionStorage();
  const { app } = loadApp(dom, ss);
  app.openBetaApply();
  const overlay = dom.getElementById('betaApplyModal');
  assert.ok(overlay, 'overlay 应被创建');
  const clickHandlers = (overlay.eventListeners.click) || [];
  assert.strictEqual(clickHandlers.length, 0, 'overlay 不应绑定 click 事件（遮罩点击关闭已移除）');
});

// ─── 测试 2：input 事件触发后写入 sessionStorage ───
test('表单 input 事件触发后 sessionStorage 写入 beta_apply_form', () => {
  const { dom } = makeDom();
  const ss = makeSessionStorage();
  const { app } = loadApp(dom, ss);
  app.openBetaApply();
  const overlay = dom.getElementById('betaApplyModal');
  const nameInput = dom.getElementById('betaAccountName');
  assert.ok(nameInput, 'betaAccountName 应存在');
  nameInput.value = 'testuser';
  const inputHandlers = overlay.eventListeners.input || [];
  assert.ok(inputHandlers.length > 0, 'overlay 应绑定 input 事件用于持久化');
  inputHandlers.forEach(fn => fn({ target: nameInput }));
  const raw = ss.getItem('beta_apply_form');
  assert.ok(raw, 'sessionStorage 应有 beta_apply_form');
  const data = JSON.parse(raw);
  assert.strictEqual(data.betaAccountName, 'testuser', '用户名应被持久化');
});

// ─── 测试 3：重新打开时回填 ───
test('重新打开弹窗：sessionStorage 中的字段被自动回填', () => {
  const { dom } = makeDom();
  const ss = makeSessionStorage();
  ss.setItem('beta_apply_form', JSON.stringify({
    betaPlatform: 'github', betaAccountName: 'prev_user', betaEmail: 'prev@test.com',
    betaPhone: '13800000000', betaReason: 'reason text', identity: '',
  }));
  const { app } = loadApp(dom, ss);
  app.openBetaApply();
  assert.strictEqual(dom.getElementById('betaPlatform').value, 'github', '平台应回填');
  assert.strictEqual(dom.getElementById('betaAccountName').value, 'prev_user', '用户名应回填');
  assert.strictEqual(dom.getElementById('betaEmail').value, 'prev@test.com', '邮箱应回填');
  assert.strictEqual(dom.getElementById('betaPhone').value, '13800000000', '手机号应回填');
  assert.strictEqual(dom.getElementById('betaReason').value, 'reason text', '理由应回填');
});

// ─── 测试 4：提交成功后清空 sessionStorage（异步）───
test('submitBetaApply 成功后 sessionStorage 被清空', (done) => {
  const { dom } = makeDom();
  const ss = makeSessionStorage();
  ss.setItem('beta_apply_form', JSON.stringify({ betaAccountName: 'will_submit' }));
  const { app } = loadApp(dom, ss);
  app.openBetaApply();
  dom.getElementById('betaAccountName').value = 'submituser';
  dom.getElementById('betaEmail').value = 'ok@test.com';
  app.baSelectIdentity('student');
  // app.js 的 _baRadio/_baChecks 用 document.querySelector（全局），需 mock
  dom.querySelector = (sel) => {
    if (sel === 'input[name="baEdu"]:checked') return { value: '本科生', parentElement: { querySelector: () => null } };
    if (sel === 'input[name="baMajor"]:checked') return { value: '计算机/软件工程', parentElement: { querySelector: () => null } };
    return null;
  };
  dom.querySelectorAll = (sel) => {
    if (sel.indexOf(':checked') > 0) return [];
    return [];
  };
  app.submitBetaApply();
  setTimeout(() => {
    try {
      assert.strictEqual(ss.getItem('beta_apply_form'), null, '提交成功后 sessionStorage 应被清空');
      passed++; console.log('  \u2713 [异步] 提交后 sessionStorage 已清空');
    } catch (e) {
      failed++; console.log('  \u2717 [异步] ' + e.message);
    }
    done && done();
  }, 60);
});

// 等待异步断言
setTimeout(() => {
  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed > 0 ? 1 : 0);
}, 200);
