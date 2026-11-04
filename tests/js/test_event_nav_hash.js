/**
 * Issue #224: 事件详情 navEvent URL hash 同步 + 403 错误处理测试。
 *
 * 零依赖：仅用 Node 内置模块（node:test / node:assert / node:vm / node:fs）。
 * 运行方式：node tests/frontend/test_event_nav_hash.js
 *
 * 覆盖：
 *   1) navEvent 成功渲染后调 history.replaceState 同步 URL hash
 *   2) navEvent 403 → app.showToast 提示用户（非静默失败）
 *   3) navEvent 404 带 max_seq → 跳转 max_seq（保留原有逻辑）
 *   4) navEvent seq 无效 → 提前返回（不调 API）
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const EVENTS_SRC = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'static', 'js', 'pages', 'events.js'),
  'utf8'
);

/**
 * 构造沙箱：注入 events.js 所需的最小全局桩。
 */
function buildSandbox(opts) {
  opts = opts || {};
  const replaceStateCalls = [];
  const showToastCalls = [];
  const locStub = { hash: opts.initialHash || '#event/100' };
  const docStub = {
    getElementById: () => null,
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
    head: { appendChild() {} },
    documentElement: { setAttribute() {}, removeAttribute() {} },
  };

  let _apiGetHandler = opts.apiGetHandler || function() { return Promise.resolve({ seq: 100, agent_type: 'vod', repo_key: 'gitcode:o/r' }); };

  const sandbox = {
    window: {},
    document: docStub,
    location: locStub,
    history: {
      back: () => {},
      pushState: () => {},
      replaceState: function() { replaceStateCalls.push(Array.from(arguments)); },
    },
    I18N: { t: function(k, p) { return k + (p ? ':' + JSON.stringify(p) : ''); }, getLang: () => 'zh' },
    app: {
      nav: () => {},
      showToast: function(msg) { showToastCalls.push(msg); },
    },
    API: {
      get: function(url) { return Promise.resolve().then(() => _apiGetHandler(url)); },
      post: () => Promise.resolve({}),
      put: () => Promise.resolve({}),
    },
    localStorage: {
      _store: {},
      getItem(k) { return this._store[k] || null; },
      setItem(k, v) { this._store[k] = String(v); },
      removeItem(k) { delete this._store[k]; },
      clear() { this._store = {}; },
    },
    _AGENT_TYPES: {},
    _vcsUrl: (rk, p) => (rk || '') + (p || ''),
    renderMarkdown: (s) => s,
    _convStreamAbort: null,
    _promptFs: false,
    _promptFsPlaceholder: null,
    _stopScenePoll: () => {},
    fmtLocalTime: (s) => s,
    console,
    setTimeout: () => 0,
    setInterval: () => 0,
    clearInterval: () => {},
    requestAnimationFrame: () => 0,
    _replaceStateCalls: replaceStateCalls,
    _showToastCalls: showToastCalls,
    _setApiGetHandler: function(h) { _apiGetHandler = h; },
  };
  Object.defineProperty(sandbox, 'window', {
    value: new Proxy(sandbox, {
      get(_, key) { return sandbox[key]; },
      set(_, key, val) { sandbox[key] = val; return true; },
    }),
    writable: false,
    configurable: false,
  });
  vm.createContext(sandbox);
  vm.runInContext(EVENTS_SRC, sandbox, { filename: 'events.js' });
  return sandbox;
}

test('navEvent 成功渲染后调 history.replaceState 同步 URL hash', async () => {
  const sb = buildSandbox({
    apiGetHandler: () => ({ seq: 200, agent_type: 'vod', repo_key: 'gitcode:o/r', prev_seq: 0, next_seq: 0 }),
  });
  sb.window.navEvent(200);
  await new Promise(r => setTimeout(r, 10));
  assert.strictEqual(sb._replaceStateCalls.length, 1, 'replaceState should be called once');
  assert.strictEqual(sb._replaceStateCalls[0][2], '#event/200', 'replaceState should set hash to #event/200');
});

test('navEvent 403 → app.showToast 提示用户（非静默失败）', async () => {
  const sb = buildSandbox({
    apiGetHandler: () => { const e = new Error('Forbidden'); e.status = 403; e.detail = { code: 403, message: 'No permission' }; throw e; },
  });
  sb.window.navEvent(300);
  await new Promise(r => setTimeout(r, 10));
  assert.strictEqual(sb._showToastCalls.length, 1, 'showToast should be called for 403');
  assert.ok(sb._showToastCalls[0].indexOf('nav_forbidden') !== -1, 'toast message should contain nav_forbidden key');
  assert.strictEqual(sb._replaceStateCalls.length, 0, 'replaceState should NOT be called on 403');
});

test('navEvent 404 带 max_seq → 跳转 max_seq（保留原有逻辑）', async () => {
  let callCount = 0;
  const sb = buildSandbox({
    apiGetHandler: (url) => {
      callCount++;
      if (callCount === 1) {
        const e = new Error('Not found'); e.status = 404; e.detail = { code: 404, message: 'not found', max_seq: 999 }; throw e;
      }
      return { seq: 999, agent_type: 'vod', repo_key: 'gitcode:o/r', prev_seq: 0, next_seq: 0 };
    },
  });
  sb.window.navEvent(404);
  await new Promise(r => setTimeout(r, 20));
  assert.ok(callCount >= 2, 'API.get should be called again with max_seq');
  assert.strictEqual(sb._replaceStateCalls.length, 1, 'replaceState should be called after fallback to max_seq');
  assert.strictEqual(sb._replaceStateCalls[0][2], '#event/999', 'replaceState should set hash to #event/999');
});

test('navEvent seq 无效 → 提前返回（不调 API）', async () => {
  let apiCalled = false;
  const sb = buildSandbox({
    apiGetHandler: () => { apiCalled = true; return { seq: 0 }; },
  });
  sb.window.navEvent(0);
  sb.window.navEvent(-1);
  await new Promise(r => setTimeout(r, 10));
  assert.strictEqual(apiCalled, false, 'API.get should NOT be called for invalid seq');
  assert.strictEqual(sb._replaceStateCalls.length, 0, 'replaceState should NOT be called for invalid seq');
});
