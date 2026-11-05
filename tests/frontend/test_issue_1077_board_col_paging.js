/*
 * Issue #1077-F：需求中心「仓库全部」看板按列独立分页 — 前端回归测试
 *
 * 运行：node tests/frontend/test_issue_1077_board_col_paging.js
 *
 * 背景：原先靠整页 sentinel + IntersectionObserver 触发，但看板是「每列内部滚动」，
 * sentinel 被容器裁切后几乎不触发 → 只加载首屏 30 条。
 * 现在每列一个游标，列内触底只加载该列下一页。
 *
 * 覆盖：
 *   T1. 首屏对 5 个游标各发一次 page=1（per_page=30），列内按真实映射分列
 *   T2. 滚某列触底 → 只发该列游标下一页，其他列请求数不变
 *   T3. 列头计数：单列游标显示「已加载/总数」，共享游标只显示已加载
 *   T4. 切列表视图 → 退回全局分页（请求不带 statuses）
 *   T5. 我的需求 → 不分列、一次请求
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, '..', '..', 'src', 'api', 'static', 'js', 'pages', 'issues.js');

function elStub(id) {
  return {
    id, innerHTML: '', style: {}, textContent: '', value: '', dataset: {},
    scrollTop: 0, clientHeight: 600, scrollHeight: 1200,
    classList: { add() {}, remove() {}, contains() { return false; } },
    handlers: {},
    addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); },
    appendChild() {}, insertAdjacentHTML(p, h) { this.innerHTML += h; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    focus() {}, remove() {}, getBoundingClientRect() { return { top: 0, height: 600, bottom: 600 }; }
  };
}

function buildSandbox() {
  const els = {};
  const document = {
    getElementById(id) { return els[id] || (els[id] = elStub(id)); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, createElement() { return elStub('x'); },
    body: elStub('body'), head: elStub('head'), documentElement: elStub('html')
  };
  // 与服务端一致的数据分布（304 张）
  const DB = [];
  let n = 0;
  const add = (st, stage, cnt) => { for (let i = 0; i < cnt; i++) { n++; DB.push({
    id: n, issue_number: n, title: '卡' + n, status: st, stage,
    repo_key: 'gitcode:o/r', created_at: '2026-11-05T10:00:00+00:00', creator: '', agent_statuses: {} }); } };
  add('active', 'vod', 122); add('processing', 'coding', 62); add('pending_review', 'qa', 60);
  add('completed', 'done', 40); add('cancelled', 'sre', 20);

  const reqs = [];
  const repo = { owner: 'o', name: 'r', repoKey: 'gitcode:o/r', fullName: 'o/r' };
  const ctx = {
    console, document,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    sessionStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    I18N: { t: (k) => k, getLang: () => 'zh' },
    app: { currentRepo: () => repo, showToast() {}, nav() {}, _user: { username: 'mys' } },
    API: {
      get(url) {
        if (!url.startsWith('/issues/')) return Promise.resolve({});
        const u = new URL('http://x' + url);
        const sts = (u.searchParams.get('statuses') || '').split(',').filter(Boolean);
        const page = +(u.searchParams.get('page') || 1);
        const per = +(u.searchParams.get('per_page') || 30);
        reqs.push({ path: u.pathname, sts: sts.join(','), page });
        if (u.pathname === '/issues/mine') return Promise.resolve({ issues: DB.slice(), total: DB.length });
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
    IntersectionObserver: class { constructor(cb) { this.cb = cb; } observe() {} disconnect() {} },
    $: (id) => document.getElementById(id), open() {}
  };
  ctx.window = ctx; ctx.globalThis = ctx; ctx._currentView = 'tasks';
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(SRC, 'utf8'), ctx, { filename: 'issues.js' });
  return { ctx, els, reqs, vm, DB };
}

const sleep = (ms) => new Promise((r) => global.setTimeout(r, ms));
const cardsIn = (els, col) => {
  const html = (els['colList-' + col] || {}).innerHTML || '';
  return (html.match(/_openBoardDetail/g) || []).length;
};

(async () => {
  const sb = buildSandbox();
  const { ctx, els, reqs } = sb;

  // ---- T1 首屏 ----
  vm.runInContext("_tasksViewMode='repo_all'; _taskView='board';", ctx);
  ctx.renderTasks();
  await sleep(80);
  assert.strictEqual(reqs.length, 5, '首屏应对 5 个游标各发一次，实际 ' + reqs.length);
  assert.ok(reqs.every((r) => r.page === 1), '首屏请求都应是 page=1');
  assert.deepStrictEqual(reqs.map((r) => r.sts).sort(),
    ['active,processing', 'awaiting_input', 'cancelled', 'completed', 'pending_review'].sort());
  const counts = ['pending', 'in_progress', 'awaiting_input', 'pending_review', 'done', 'cancelled'].map((c) => cardsIn(els, c));
  console.log('ok 1 - 首屏 5 个游标各拉 1 页，列卡片数:', counts.join('/'));
  assert.deepStrictEqual(counts, [0, 30, 0, 30, 30, 20], '列分布不符: ' + counts.join('/'));

  // ---- T2 单列触底只影响该列 ----
  const before = reqs.length;
  const el = els['colList-in_progress'];
  el.scrollTop = 1200; el.clientHeight = 600; el.scrollHeight = 1200;
  (el.handlers.scroll || []).forEach((f) => f());
  await sleep(60);
  const added = reqs.slice(before);
  assert.strictEqual(added.length, 1, '单列触底只应发 1 个请求，实际 ' + added.length);
  assert.strictEqual(added[0].sts, 'active,processing', '只应加载该列游标');
  assert.strictEqual(added[0].page, 2);
  assert.strictEqual(cardsIn(els, 'in_progress'), 60, '该列应追加到 60 张');
  assert.strictEqual(cardsIn(els, 'pending_review'), 30, '其他列不应变化');
  console.log('ok 2 - 滚动 in_progress 列只拉该列下一页（30→60），其他列不变');

  // ---- T3 列头计数 ----
  const txt = (c) => els['colCount-' + c].textContent;
  assert.strictEqual(txt('pending_review'), '30/60', '单列游标应显示 已加载/总数');
  assert.strictEqual(txt('cancelled'), '20/20');
  assert.strictEqual(txt('in_progress'), '60', '共享游标只显示已加载数');
  console.log('ok 3 - 列头计数正确（pending_review=30/60, in_progress=60 共享游标）');

  // ---- T4 切列表 → 全局分页 ----
  reqs.length = 0;
  ctx._switchTaskView('list');
  await sleep(60);
  assert.strictEqual(reqs.length, 1);
  assert.strictEqual(reqs[0].sts, '', '列表视图不应带 statuses（走全局排序窗口）');
  assert.strictEqual(reqs[0].path, '/issues/o/r');
  console.log('ok 4 - 切列表视图回到全局分页（无 statuses）');

  // ---- T5 我的需求：不分列 ----
  reqs.length = 0;
  ctx._switchTasksViewMode('mine');
  await sleep(60);
  assert.strictEqual(reqs.length, 1, '我的需求只发 1 个请求');
  assert.strictEqual(reqs[0].path, '/issues/mine');
  console.log('ok 5 - 我的需求不分列（单请求 /issues/mine）');

  console.log('\n5 passing');
})();