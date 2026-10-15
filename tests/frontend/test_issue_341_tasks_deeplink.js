/**
 * 轻量前端测试 — Issue #341: 需求中心深链 #tasks/<issue号> 直达详情页
 *
 * 零依赖：仅用 Node 内置模块（node:test / node:assert / node:vm / node:fs）。
 * 运行方式：node tests/frontend/test_issue_341_tasks_deeplink.js
 *
 * 覆盖：
 *   1) renderTasks('341') 设置 _pendingIssueNumber + _deeplinkIssueNumber（复用 #166 链路）
 *   2) renderTasks()（原 #tasks 入口）不设置深链标志、不覆盖场景跳转预置的 _pendingIssueNumber
 *   3) renderTasks('abc')（畸形 hash）视为原 #tasks 行为
 *   4) 深链命中 → _openBoardDetail 打开对应详情，深链标志清除
 *   5) 深链无匹配（无效编号）→ _render 回退列表 + Toast 提示，标志清理
 *   6) 场景跳转链路（_taskDetailFromScene.latestSeq）不受深链影响（无 Toast）
 *   7) loadTasks 失败 → 深链标志清理（不残留）
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const ISSUES_SRC = fs.readFileSync(
  path.join(ROOT, 'src', 'api', 'static', 'js', 'pages', 'issues.js'),
  'utf8'
);

function buildSandbox(opts) {
  opts = opts || {};
  const win = {};
  const pageEl = { innerHTML: '' };
  const docStub = {
    getElementById: (id) => (id === 'pageContent' ? pageEl : null),
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
    head: { appendChild() {} },
    documentElement: { setAttribute() {}, removeAttribute() {} },
  };
  const toasts = [];
  const navs = [];
  const sandbox = {
    window: win,
    document: docStub,
    location: { hash: '' },
    history: { back: () => {}, pushState: () => {}, replaceState: () => {} },
    I18N: { t: (k, v) => k + (v && v.n !== undefined ? ':' + v.n : ''), getLang: () => 'zh' },
    app: {
      nav: (...a) => navs.push(a),
      showToast: (m) => toasts.push(m),
      currentRepo: () => ({ owner: 'o', name: 'r', repoKey: 'gitcode:o/r', fullName: 'o/r' }),
    },
    API: {
      get: (url) => Promise.resolve(opts.apiResult !== undefined ? opts.apiResult : { issues: [] }),
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
    _stopScenePoll: () => {},
    fmtLocalTime: (s) => s,
    console,
    setTimeout: () => 0,
    setInterval: () => 0,
    clearInterval: () => {},
    requestAnimationFrame: () => 0,
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
  vm.runInContext(ISSUES_SRC, sandbox, { filename: 'issues.js' });

  // spy：_render / _openBoardDetail（loadTasks 深链分支的关键观测点）
  const opened = [];
  const rendered = [];
  sandbox._openBoardDetail = (id) => opened.push(id);
  sandbox._render = () => rendered.push(1);

  sandbox.__spies = { toasts, navs, opened, rendered, pageEl };
  return sandbox;
}

function flush() {
  return new Promise((resolve) => setImmediate(() => setImmediate(resolve)));
}

test('renderTasks(<数字>) 设置深链编号（复用 #166 _pendingIssueNumber 链路）', () => {
  const sb = buildSandbox();
  sb.window._currentView = 'tasks';
  sb.renderTasks('341');
  assert.strictEqual(sb.window._pendingIssueNumber, '341');
  assert.strictEqual(sb.window._deeplinkIssueNumber, '341');
});

test('renderTasks()（原 #tasks 入口）零回归：不设深链标志、不覆盖场景跳转预置编号', () => {
  const sb = buildSandbox();
  sb.window._currentView = 'tasks';
  // 场景详情页跳转先设置 _pendingIssueNumber 再 nav('tasks')（dashboard.js 链路）
  sb.window._pendingIssueNumber = '166';
  sb.window._deeplinkIssueNumber = 'stale';
  sb.renderTasks();
  assert.strictEqual(sb.window._pendingIssueNumber, '166', '不应覆盖场景跳转预置编号');
  assert.strictEqual(sb.window._deeplinkIssueNumber, null, '非深链入口应清理深链标志');
});

test('renderTasks(<非数字>)（畸形 hash #tasks/abc）视为原 #tasks 行为', () => {
  const sb = buildSandbox();
  sb.window._currentView = 'tasks';
  sb.renderTasks('abc');
  assert.strictEqual(sb.window._deeplinkIssueNumber, null);
});

test('深链命中：#tasks/341 直达对应需求详情', async () => {
  const sb = buildSandbox({ apiResult: { issues: [{ id: 9, issue_number: 341, title: 'x' }] } });
  sb.window._currentView = 'tasks';
  sb.renderTasks('341');
  await flush();
  assert.deepStrictEqual(sb.__spies.opened, [9], '应打开 issue_number=341 的任务详情');
  assert.strictEqual(sb.window._deeplinkIssueNumber, null, '命中后深链标志应清除');
  assert.strictEqual(sb.window._pendingIssueNumber, null);
  assert.strictEqual(sb.__spies.toasts.length, 0, '命中不应 Toast');
});

test('深链无匹配（无效编号）：回退列表 + Toast 提示，不白屏不报错', async () => {
  const sb = buildSandbox({ apiResult: { issues: [{ id: 1, issue_number: 5, title: 'y' }] } });
  sb.window._currentView = 'tasks';
  sb.renderTasks('999999999');
  await flush();
  assert.deepStrictEqual(sb.__spies.opened, [], '不应打开任何详情');
  assert.strictEqual(sb.__spies.rendered.length, 1, '应回退渲染任务列表');
  assert.strictEqual(sb.__spies.toasts.length, 1, '应 Toast 提示');
  assert.ok(sb.__spies.toasts[0].indexOf('tasks.deeplink_not_found:999999999') >= 0, 'Toast 文案应含键名与编号');
  assert.strictEqual(sb.window._deeplinkIssueNumber, null, '提示后标志应清理');
});

test('场景跳转链路（latestSeq 回退事件页）不受深链影响', async () => {
  const sb = buildSandbox({ apiResult: { issues: [{ id: 1, issue_number: 5, title: 'y' }] } });
  sb.window._currentView = 'tasks';
  sb.window._pendingIssueNumber = '7';
  sb.window._taskDetailFromScene = { sceneId: 's', latestSeq: 55 };
  sb.renderTasks();  // 场景跳转入口无 param
  await flush();
  assert.deepStrictEqual(sb.__spies.navs.filter(a => a[0] === 'event' && a[1] === 55).length, 1, '应跳转事件详情');
  assert.strictEqual(sb.__spies.toasts.length, 0, '场景跳转回退不应 Toast');
});

test('loadTasks 失败：深链标志清理（不残留），显示错误态', async () => {
  const sb = buildSandbox();
  sb.API.get = () => Promise.reject(new Error('boom'));
  sb.window._currentView = 'tasks';
  sb.renderTasks('341');
  await flush();
  assert.strictEqual(sb.window._deeplinkIssueNumber, null);
  assert.ok(sb.__spies.pageEl.innerHTML.indexOf('error') >= 0, '应显示错误态而非白屏');
});
