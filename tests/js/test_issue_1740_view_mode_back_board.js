/**
 * Issue #1740：需求中心「视图模式持久化 + 详情返回看板」回归测试
 *
 * 运行：node tests/frontend/test_issue_1740_view_mode_back_board.js
 *
 * 覆盖：
 *   T1. 「我的需求 / 仓库全部」视图模式从 localStorage 初始化，刷新后保持上次选择
 *   T2. 切换视图模式写入 localStorage；非法缓存值回退 mine
 *   T3. 仓库全部·看板：详情返回时先用缓存 _render 立即渲染，再按列 merge 校准并恢复轮询，
 *       不走全局 loadTasks（避免列游标缓存被单页覆盖，修复“仅处理中列有数据”）
 *   T4. 我的需求：详情返回时缓存渲染 + 后台 loadTasks({merge:true}) 校准 + 恢复轮询
 *   T5. 返回时清理详情态 _currentDetailTask 与详情轮询 _taskPipeTimer
 *   T6. _mapStatus/_mapStatusL 统一映射，兼容 /issues/mine 与 /issues/{owner}/{repo} 的
 *       status/stage 字段差异
 *   T7. 详情页返回按钮统一走 _backToBoard()（含 PR 详情页）
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

function elStub(id) {
  return {
    id, innerHTML: '', style: {}, textContent: '', value: '', dataset: {},
    scrollTop: 0, clientHeight: 600, scrollHeight: 1200,
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, appendChild() {},
    insertAdjacentHTML(p, h) { this.innerHTML += h; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    remove() {}, getBoundingClientRect() { return { top: 0, height: 600, bottom: 600 }; },
  };
}

function buildSandbox(savedMode) {
  const els = {};
  const document = {
    getElementById(id) { return els[id] || (els[id] = elStub(id)); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, createElement() { return elStub('x'); },
    body: elStub('body'), head: elStub('head'), documentElement: elStub('html'),
  };
  const store = {};
  if (savedMode !== undefined) store.tasks_view_mode = savedMode;
  const repo = { owner: 'o', name: 'r', repoKey: 'gitcode:o/r', fullName: 'o/r' };
  const sandbox = {
    console, document,
    localStorage: {
      getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
      setItem(k, v) { store[k] = String(v); },
      removeItem(k) { delete store[k]; },
      clear() { Object.keys(store).forEach((k) => delete store[k]); },
    },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    I18N: { t: (k) => k, getLang: () => 'zh' },
    app: { currentRepo: () => repo, showToast() {}, nav() {}, _user: { username: 'u' } },
    API: { get() { return Promise.resolve({ issues: [] }); }, post() { return Promise.resolve({}); } },
    fmtLocalTime: (s) => s || '', _vcsUrl: () => 'x',
    setInterval: () => 0, clearInterval() {},
    setTimeout: () => 0,
    IntersectionObserver: class { observe() {} disconnect() {} },
    history: { back() {} },
    open() {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox._currentView = 'tasks';
  vm.createContext(sandbox);
  vm.runInContext(ISSUES_SRC, sandbox, { filename: 'issues.js' });
  return { ctx: sandbox, els, store };
}

test('T1 视图模式从 localStorage 初始化（刷新保持仓库全部）', () => {
  const sb = buildSandbox('repo_all');
  assert.strictEqual(vm.runInContext('_tasksViewMode', sb.ctx), 'repo_all');
  const sb2 = buildSandbox();
  assert.strictEqual(vm.runInContext('_tasksViewMode', sb2.ctx), 'mine', '无缓存默认我的需求');
});

test('T2 切换视图模式写入 localStorage；非法值回退 mine', () => {
  const sb = buildSandbox('repo_all');
  const calls = [];
  sb.ctx.loadTasks = (opts) => calls.push(opts || {});
  sb.ctx._switchTasksViewMode('mine');
  assert.strictEqual(sb.store.tasks_view_mode, 'mine');
  assert.strictEqual(vm.runInContext('_tasksViewMode', sb.ctx), 'mine');
  sb.ctx._switchTasksViewMode('repo_all');
  assert.strictEqual(sb.store.tasks_view_mode, 'repo_all');

  const bad = buildSandbox('bogus');
  assert.strictEqual(vm.runInContext('_tasksViewMode', bad.ctx), 'mine', '非法缓存值回退 mine');
});

test('T3 仓库全部看板返回：缓存渲染 + 按列 merge 校准，不走全局 loadTasks', () => {
  const sb = buildSandbox('repo_all');
  const order = [];
  vm.runInContext(
    "_taskView='board'; _tasksAll=[{id:1,status:'processing',stage:'coding'},{id:2,status:'active',stage:''}];" +
    'window._currentDetailTask={id:1}; window._taskPipeTimer=42;',
    sb.ctx
  );
  sb.ctx._startScenePoll = () => order.push('poll');
  sb.ctx._render = () => order.push('render');
  sb.ctx._boardLoadAll = (opts) => { order.push('boardLoadAll:' + JSON.stringify(opts || {})); return Promise.resolve(); };
  sb.ctx.loadTasks = () => order.push('loadTasks');
  sb.ctx._backToBoard();
  assert.deepStrictEqual(order, ['poll', 'render', 'boardLoadAll:{"merge":true}']);
  assert.strictEqual(sb.ctx.window._currentDetailTask, null, '详情态必须清理');
  assert.strictEqual(sb.ctx.window._taskPipeTimer, null, '详情轮询必须清理');
});

test('T4 我的需求返回：缓存渲染 + loadTasks({merge:true}) 校准 + 恢复轮询', () => {
  const sb = buildSandbox('mine');
  const order = [];
  vm.runInContext(
    "_taskView='board'; _tasksAll=[{id:1,status:'processing',stage:'coding'}]; window._currentDetailTask={id:1};",
    sb.ctx
  );
  sb.ctx._startScenePoll = () => order.push('poll');
  sb.ctx._render = () => order.push('render');
  sb.ctx.loadTasks = (opts) => order.push('loadTasks:' + JSON.stringify(opts || {}));
  sb.ctx._backToBoard();
  assert.deepStrictEqual(order, ['poll', 'render', 'loadTasks:{"merge":true}']);
});

test('T5 场景来源返回：清理详情态后回退历史栈，不渲染看板', () => {
  const sb = buildSandbox();
  let backed = false;
  sb.ctx.history.back = () => { backed = true; };
  vm.runInContext(
    'window._currentDetailTask={id:9}; window._taskPipeTimer=99; window._taskDetailFromScene={latestSeq:7};',
    sb.ctx
  );
  let rendered = 0;
  sb.ctx._render = () => rendered++;
  sb.ctx._backToBoard();
  assert.strictEqual(backed, true, '场景来源应 history.back');
  assert.strictEqual(rendered, 0, '不应渲染需求看板');
  assert.strictEqual(sb.ctx.window._currentDetailTask, null);
  assert.strictEqual(sb.ctx.window._taskPipeTimer, null);
  assert.strictEqual(sb.ctx.window._taskDetailFromScene, null);
});

test('T6 _mapStatus/_mapStatusL 统一映射兼容两套字段口径', () => {
  const sb = buildSandbox();
  const m = (t) => sb.ctx._mapStatus(t);
  // /issues/mine 典型数据
  assert.strictEqual(m({ status: 'processing', stage: 'coding' }), 'in_progress');
  assert.strictEqual(m({ status: 'active', stage: 'issue' }), 'in_progress');
  assert.strictEqual(m({ status: 'active', stage: '' }), 'pending');
  assert.strictEqual(m({ status: 'awaiting_input', stage: 'coding' }), 'awaiting_input');
  assert.strictEqual(m({ status: 'pending_review', stage: 'qa' }), 'pending_review');
  assert.strictEqual(m({ status: 'completed', stage: 'coding' }), 'done');
  assert.strictEqual(m({ status: 'cancelled', stage: 'sre' }), 'cancelled');
  // /issues/{owner}/{repo} 以 stage 为主导的差异数据
  assert.strictEqual(m({ status: 'active', stage: 'done' }), 'done');
  assert.strictEqual(m({ status: 'active', stage: 'pending_review' }), 'pending_review');
  assert.strictEqual(m({ status: 'active', stage: 'cancelled' }), 'cancelled');
  assert.strictEqual(m({ status: 'active', stage: 'sre' }), 'in_progress');
  // 同一映射入口
  assert.strictEqual(sb.ctx._mapStatusL({ status: 'processing', stage: 'coding' }), 'in_progress');
  assert.strictEqual(sb.ctx._mapStatusL({ status: 'active', stage: 'done' }), 'done');
});

test('T7 详情返回按钮统一调用 _backToBoard（含 PR 详情页）', () => {
  const openDetail = ISSUES_SRC.match(/window\._openBoardDetail = function\(taskId\) \{[\s\S]*?\n\};/);
  assert.ok(openDetail, '应存在 _openBoardDetail');
  assert.ok(openDetail[0].indexOf("var _backAction = '_backToBoard()';") >= 0,
    '_openBoardDetail 返回按钮应走 _backToBoard()');
  const prDetail = ISSUES_SRC.match(/window\._openPRDetail = function\(t\) \{[\s\S]*?\n\};/);
  assert.ok(prDetail, '应存在 _openPRDetail');
  assert.ok(prDetail[0].indexOf('onclick="_backToBoard()"') >= 0,
    '_openPRDetail 返回按钮应走 _backToBoard()');
  assert.ok(prDetail[0].indexOf('onclick="loadTasks()"') === -1,
    'PR 详情返回不应再直接 loadTasks()');
});
// ── T8：完整看板链路（真实 _render/_boardLoadAll）────────────────────────────
function buildBoardSandbox() {
  const els = {};
  const document = {
    getElementById(id) { return els[id] || (els[id] = elStub(id)); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, createElement() { return elStub('x'); },
    body: elStub('body'), head: elStub('head'), documentElement: elStub('html'),
  };
  const DB = [];
  let n = 0;
  const add = (st, stage, cnt) => {
    for (let i = 0; i < cnt; i++) {
      n++;
      DB.push({ id: n, issue_number: n, title: '卡' + n, status: st, stage,
        repo_key: 'gitcode:o/r', created_at: '2026-11-05T10:00:00+00:00',
        creator: '', agent_statuses: {} });
    }
  };
  add('active', 'vod', 122);
  add('processing', 'coding', 62);
  add('pending_review', 'qa', 60);
  add('completed', 'done', 40);
  add('cancelled', 'sre', 20);
  const reqs = [];
  const repo = { owner: 'o', name: 'r', repoKey: 'gitcode:o/r', fullName: 'o/r' };
  const store = { tasks_view_mode: 'repo_all' };
  const sandbox = {
    console, document,
    localStorage: {
      getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
      setItem(k, v) { store[k] = String(v); }, removeItem(k) { delete store[k]; },
    },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    I18N: { t: (k) => k, getLang: () => 'zh' },
    app: { currentRepo: () => repo, showToast() {}, nav() {}, _user: { username: 'u' } },
    API: {
      get(url) {
        if (!url.startsWith('/issues/')) return Promise.resolve({});
        if (/\/pipeline$/.test(url)) return Promise.resolve({ nodes: [], events: [] });
        const u = new URL('http://x' + url);
        const sts = (u.searchParams.get('statuses') || '').split(',').filter(Boolean);
        const page = +(u.searchParams.get('page') || 1);
        const per = +(u.searchParams.get('per_page') || 30);
        reqs.push({ path: u.pathname, sts: sts.join(','), page });
        if (u.pathname === '/issues/mine') return Promise.resolve({ issues: DB.slice(), total: DB.length });
        if (!/^\/issues\/o\/r$/.test(u.pathname)) return Promise.resolve({ issues: [] });
        const match = sts.length ? DB.filter((t) => sts.includes(t.status)) : DB.slice();
        const slice = match.slice((page - 1) * per, page * per);
        return Promise.resolve({ issues: slice.map((t) => Object.assign({}, t)), total: match.length,
          page, per_page: per, has_more: page * per < match.length });
      },
      post() { return Promise.resolve({}); }
    },
    fmtLocalTime: (s) => s || '', _vcsUrl: () => 'x',
    setInterval: () => 0, clearInterval() {},
    setTimeout: (f, ms) => global.setTimeout(f, ms),
    IntersectionObserver: class { observe() {} disconnect() {} },
    history: { back() {} }, open() {},
  };
  sandbox.window = sandbox; sandbox.globalThis = sandbox; sandbox._currentView = 'tasks';
  vm.createContext(sandbox);
  vm.runInContext(ISSUES_SRC, sandbox, { filename: 'issues.js' });
  return { ctx: sandbox, els, reqs };
}

const sleep = (ms) => new Promise((r) => global.setTimeout(r, ms));
const colCards = (els, col) => ((els['colList-' + col] || {}).innerHTML || '').match(/_openBoardDetail/g) || [];

test('T8 仓库全部看板：打开详情后返回，各列数据立即完整且后台按列 merge 校准', async () => {
  const sb = buildBoardSandbox();
  sb.ctx.renderTasks();
  await sleep(80);
  const cols = ['pending', 'in_progress', 'awaiting_input', 'pending_review', 'done', 'cancelled'];
  const before = cols.map((c) => colCards(sb.els, c).length);
  assert.deepStrictEqual(before, [0, 30, 0, 30, 30, 20], '首屏列分布: ' + before.join('/'));

  const reqBefore = sb.reqs.length;
  sb.ctx._openBoardDetail(1);
  await sleep(10);
  sb.ctx._backToBoard();
  await sleep(80);
  const after = cols.map((c) => colCards(sb.els, c).length);
  assert.deepStrictEqual(after, before, '返回后各列数据应与首屏一致: ' + after.join('/'));
  const backReqs = sb.reqs.slice(reqBefore).filter((r) => r.path === '/issues/o/r');
  assert.strictEqual(backReqs.length, 5, '返回后应按 5 个列游标 merge 校准，实际 ' + backReqs.length);
  assert.ok(backReqs.every((r) => r.page === 1), '校准应取各列第 1 屏');
  assert.ok(backReqs.every((r) => r.sts !== ''), '不应回退到全局无 statuses 分页请求');
});
