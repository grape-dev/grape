/*
 * Issue #1961 回归测试：需求中心「仓库全部」看板搜索时空态误报
 *
 * 运行：node tests/js/test_issue_1961_board_searching_empty.js
 *
 * 验证点：
 *   1. 看板六列在请求未发出（seq===0）/ 在途（loading===true）时显示忙态文案，
 *      任一列都不得出现「当前筛选下没有匹配的需求」。
 *   2. 有搜索词取 tasks.searching，无搜索词取 tasks.loading。
 *   3. 请求已返回且该列为空（seq>0、loading===false）才显示空态文案。
 *   4. 有数据的列正常渲染卡片，不残留忙态文案。
 *   5. 列表视图空态同语义（复用 _tasksLoading / _taskReqSeq）。
 *   6. zh / en 均真实存在 tasks.searching 键；issues.js 版本号已 bump。
 *
 * 策略：从 issues.js 源码提取相关函数，在 mock 沙箱执行；locale 文案直接读取真实 JSON。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const root = path.resolve(__dirname, '../..');
const issuesJsPath = path.join(root, 'src/api/static/js/pages/issues.js');
const src = fs.readFileSync(issuesJsPath, 'utf8');

const zhTasks = JSON.parse(fs.readFileSync(path.join(root, 'src/api/static/locales/zh/tasks.json'), 'utf8'));
const enTasks = JSON.parse(fs.readFileSync(path.join(root, 'src/api/static/locales/en/tasks.json'), 'utf8'));
const indexHtml = fs.readFileSync(path.join(root, 'src/api/static/index.html'), 'utf8');

function extractFn(name) {
  const re = new RegExp('function ' + name + '\\([^)]*\\)\\s*\\{([\\s\\S]*?)\\n\\}(?:\\s*\\n)');
  const m = src.match(re);
  if (!m) {
    console.error('✗ 无法从 issues.js 提取 ' + name + ' 函数体');
    process.exit(1);
  }
  return m[1];
}

function makeI18N(dict, prefix) {
  return {
    t: function (key) {
      var parts = key.split('.');
      var v = dict;
      for (var i = 0; i < parts.length; i++) {
        if (v === undefined || v === null) return key;
        v = v[parts[i]];
      }
      return v === undefined ? key : v;
    },
  };
}

// ── 1. 静态断言：locale 键真实落盘 + 版本号 bump ──
assert.strictEqual(zhTasks.searching, '搜索中...', 'zh tasks.json 应真实存在 searching 键');
assert.strictEqual(enTasks.searching, 'Searching...', 'en tasks.json 应真实存在 searching 键');
assert.ok(/issues\.js\?v=136/.test(indexHtml), 'index.html 中 issues.js 版本号应 bump 到 v=136');
assert.ok(!/issues\.js\?v=135/.test(indexHtml), 'index.html 不应残留旧版本 v=135');

// ── 2. 静态断言：调用点补传列游标 + 列表空态走统一判定 ──
assert.ok(src.includes('function _boardColListHtml(colKey, list, cur)'), '_boardColListHtml 应支持可选列游标第三参');
assert.ok(/_boardColListHtml\(c, list, _colCursor\[_BOARD_CURSOR_OF_COL\[c\]\]\)/.test(src),
  '_renderBoardCols 应补传该列游标');
assert.ok(/_boardColListHtml\(colKey, list, _colCursor\[_BOARD_CURSOR_OF_COL\[colKey\]\]\)/.test(src),
  '_boardColHtml 应补传该列游标');
assert.ok(src.includes('function _taskListEmptyHtml()'), '应定义列表空态统一判定 _taskListEmptyHtml');
const listEmptyCalls = src.match(/_taskListEmptyHtml\(\)/g) || [];
assert.ok(listEmptyCalls.length >= 3,
  '列表空态分支应复用 _taskListEmptyHtml（含定义与调用），实际 ' + listEmptyCalls.length);
assert.ok(/_tasksLoading = true;[\s\S]{0,240}_renderTaskListBusyIfNeeded\(\);/.test(src),
  '重置类 loadTasks 应在置 _tasksLoading=true 后先渲染列表忙态');
assert.ok(src.includes('function _renderTaskListBusyIfNeeded()'),
  '应定义 _renderTaskListBusyIfNeeded 使列表在途忙态可达');

// ── 3. 沙箱：看板列空态三态判定 ──
const busyBody = extractFn('_tasksBusyText');
const colListBody = extractFn('_boardColListHtml');

function buildColListFn(query, i18n) {
  return new Function(
    'I18N', '_taskQuery', '_boardStageMeta', '_boardCardHtml',
    'function _tasksBusyText() {' + busyBody + '}\n' +
    'return function _boardColListHtml(colKey, list, cur) {' + colListBody + '}'
  )(
    i18n, query,
    function () { return {}; },
    function (t) { return '<div class="tsk-card" data-id="' + t.id + '">' + t.title + '</div>'; }
  );
}

// 常量从源码提取，避免测试与看板列/游标映射硬编码耦合
const boardColsSrc = src.match(/const _BOARD_COLS = (\[[^\]]*\])/);
const cursorOfColSrc = src.match(/const _BOARD_CURSOR_OF_COL = (\{[\s\S]*?\n\})/);
assert.ok(boardColsSrc, '应从 issues.js 提取 _BOARD_COLS');
assert.ok(cursorOfColSrc, '应从 issues.js 提取 _BOARD_CURSOR_OF_COL');
const BOARD_COLS = new Function('return ' + boardColsSrc[1])();
const CURSOR_OF_COL = new Function('return ' + cursorOfColSrc[1])();

function cursorsFor(seq, loading) {
  const cursors = {};
  const keys = new Set(BOARD_COLS.map(function (c) { return CURSOR_OF_COL[c]; }));
  keys.forEach(function (k) {
    cursors[k] = { page: 1, hasMore: true, loading: loading, total: null, seq: seq };
  });
  return cursors;
}

function renderAllCols(fn, cursors, lists) {
  return BOARD_COLS.map(function (c) {
    var list = (lists && lists[c]) || [];
    return { col: c, html: fn(c, list, cursors[CURSOR_OF_COL[c]]) };
  });
}

const zhI18N = makeI18N({tasks: zhTasks});
const enI18N = makeI18N({tasks: enTasks});

// 3a. 搜索无结果词：尚未发出（seq===0）→ 六列全部「搜索中...」，无空态文案
{
  const fn = buildColListFn('needle', zhI18N);
  const cols = renderAllCols(fn, cursorsFor(0, false));
  cols.forEach(function (o) {
    assert.ok(o.html.indexOf(zhTasks.searching) >= 0, o.col + ' 未发出请求时应显示「搜索中...」');
    assert.ok(o.html.indexOf(zhTasks.empty.no_tasks_desc) < 0, o.col + ' 未发出请求时不得显示空态文案');
  });
}

// 3b. 搜索无结果词：在途（seq>0 且 loading）→ 六列全部「搜索中...」
{
  const fn = buildColListFn('needle', zhI18N);
  const cols = renderAllCols(fn, cursorsFor(1, true));
  cols.forEach(function (o) {
    assert.ok(o.html.indexOf(zhTasks.searching) >= 0, o.col + ' 在途时应显示「搜索中...」');
    assert.ok(o.html.indexOf(zhTasks.empty.no_tasks_desc) < 0, o.col + ' 在途时不得显示空态文案');
  });
}

// 3c. 已返回且确实 0 条 → 六列全部空态文案，不残留忙态
{
  const fn = buildColListFn('needle', zhI18N);
  const cols = renderAllCols(fn, cursorsFor(2, false));
  cols.forEach(function (o) {
    assert.ok(o.html.indexOf(zhTasks.empty.no_tasks_desc) >= 0, o.col + ' 已返回且为空应显示空态文案');
    assert.ok(o.html.indexOf(zhTasks.searching) < 0, o.col + ' 已返回且为空不应残留「搜索中...」');
  });
}

// 3d. 清空搜索词：加载阶段取 tasks.loading（而非搜索中）
{
  const fn = buildColListFn('', zhI18N);
  const cols = renderAllCols(fn, cursorsFor(0, true));
  cols.forEach(function (o) {
    assert.ok(o.html.indexOf(zhTasks.loading) >= 0, o.col + ' 无搜索词加载阶段应显示「加载中...」');
    assert.ok(o.html.indexOf(zhTasks.searching) < 0, o.col + ' 无搜索词不应显示「搜索中...」');
    assert.ok(o.html.indexOf(zhTasks.empty.no_tasks_desc) < 0, o.col + ' 加载阶段不得显示空态文案');
  });
}

// 3e. 搜索有结果词：命中列渲染卡片、未命中列空态，不残留忙态
{
  const fn = buildColListFn('hit', zhI18N);
  const lists = { pending: [{ id: 1, title: 'hit-item' }] };
  const cursors = cursorsFor(2, false);
  const cols = renderAllCols(fn, cursors, lists);
  cols.forEach(function (o) {
    if (o.col === 'pending') {
      assert.ok(o.html.indexOf('hit-item') >= 0, '命中列应渲染卡片');
      assert.ok(o.html.indexOf(zhTasks.searching) < 0, '命中列不应残留忙态文案');
      assert.ok(o.html.indexOf(zhTasks.empty.no_tasks_desc) < 0, '命中列不应显示空态文案');
    } else {
      assert.ok(o.html.indexOf(zhTasks.empty.no_tasks_desc) >= 0, o.col + ' 未命中列应显示空态文案');
      assert.ok(o.html.indexOf(zhTasks.searching) < 0, o.col + ' 未命中列不应残留忙态文案');
    }
  });
}

// 3f. cur 缺省保持旧行为（向后兼容）
{
  const fn = buildColListFn('', zhI18N);
  const html = fn('pending', [], undefined);
  assert.ok(html.indexOf(zhTasks.empty.no_tasks_desc) >= 0, 'cur 缺省时应维持既有空态文案');
}

// 3g. en locale 同样生效
{
  const fn = buildColListFn('needle', enI18N);
  const cols = renderAllCols(fn, cursorsFor(1, true));
  cols.forEach(function (o) {
    assert.ok(o.html.indexOf(enTasks.searching) >= 0, o.col + ' en 在途应显示 Searching...');
    assert.ok(o.html.indexOf(enTasks.empty.no_tasks_desc) < 0, o.col + ' en 在途不得显示空态文案');
  });
}

// ── 4. 沙箱：列表视图空态同语义 ──
const listBusyBody = extractFn('_tasksListBusy');
const listBusyHtmlBody = extractFn('_tasksListBusyHtml');
const listEmptyBody = extractFn('_taskListEmptyHtml');

function buildListHelpers(query, loading, reqSeq, i18n) {
  return new Function(
    'I18N', '_taskQuery', '_tasksLoading', '_taskReqSeq',
    'function _tasksBusyText() {' + busyBody + '}\n' +
    'function _tasksListBusy() {' + listBusyBody + '}\n' +
    'function _tasksListBusyHtml() {' + listBusyHtmlBody + '}\n' +
    'function _taskListEmptyHtml() {' + listEmptyBody + '}\n' +
    'return { empty: _taskListEmptyHtml, listBusyHtml: _tasksListBusyHtml };'
  )(i18n, query, loading, reqSeq);
}

function buildListEmptyFn(query, loading, reqSeq, i18n) {
  return buildListHelpers(query, loading, reqSeq, i18n).empty;
}

// 4a. 本轮未发出（seq===0）→ 忙态
{
  const fn = buildListEmptyFn('needle', false, 0, zhI18N);
  const html = fn();
  assert.ok(html.indexOf(zhTasks.searching) >= 0, '列表未发出请求应显示「搜索中...」');
  assert.ok(html.indexOf(zhTasks.empty.no_tasks_desc) < 0, '列表未发出请求不得显示空态文案');
}

// 4b. 在途（loading）→ 忙态
{
  const fn = buildListEmptyFn('needle', true, 3, zhI18N);
  const html = fn();
  assert.ok(html.indexOf(zhTasks.searching) >= 0, '列表在途应显示「搜索中...」');
  assert.ok(html.indexOf(zhTasks.empty.no_tasks_desc) < 0, '列表在途不得显示空态文案');
}

// 4c. 已返回且为空 → 空态文案
{
  const fn = buildListEmptyFn('needle', false, 3, zhI18N);
  const html = fn();
  assert.ok(html.indexOf(zhTasks.empty.no_tasks_desc) >= 0, '列表已返回且为空应显示空态文案');
  assert.ok(html.indexOf(zhTasks.searching) < 0, '列表已返回且为空不应残留忙态文案');
}

// 4d. 无搜索词加载阶段 → loading 文案
{
  const fn = buildListEmptyFn('', true, 3, zhI18N);
  const html = fn();
  assert.ok(html.indexOf(zhTasks.loading) >= 0, '列表无搜索词加载阶段应显示「加载中...」');
  assert.ok(html.indexOf(zhTasks.searching) < 0, '列表无搜索词不应显示「搜索中...」');
}

// ── 5. 沙箱：重置类请求在途时真实渲染列表忙态（在途忙态可达） ──
const renderBusyBody = extractFn('_renderTaskListBusyIfNeeded');

function runBusyRender(state) {
  const calls = { body: [], pager: [] };
  const els = {
    taskListBody: {
      _ih: state.bodyHtml || '',
      set innerHTML(v) { this._ih = v; calls.body.push(v); },
      get innerHTML() { return this._ih; },
    },
    taskListPager: {
      _ih: '',
      set innerHTML(v) { this._ih = v; calls.pager.push(v); },
      get innerHTML() { return this._ih; },
    },
  };
  const document = {
    getElementById: function (id) {
      if (state.missingBody && id === 'taskListBody') return null;
      return els[id] || null;
    },
  };
  const helpers = buildListHelpers(state.query, state.loading, state.reqSeq, zhI18N);
  const fn = new Function(
    'document', '_tasksViewMode', '_taskView', '_tasksAll',
    '_tasksListBusyHtml', '_taskListPagerHtml',
    'return function _renderTaskListBusyIfNeeded() {' + renderBusyBody + '}'
  )(
    document, state.viewMode || 'repo_all', state.view || 'list', state.tasksAll || [],
    helpers.listBusyHtml,
    function () { return '<div id="taskListSentinel"></div>'; }
  );
  const applied = fn();
  return { applied, calls, els };
}

// 5a. 搜索重拉在途且本地已清空 → 立即渲染「搜索中...」，不残留旧空态文案
{
  const r = runBusyRender({ query: 'needle', loading: true, reqSeq: 2, bodyHtml: '<div>old</div>' });
  assert.strictEqual(r.applied, true, '搜索在途应渲染列表忙态');
  assert.ok(r.calls.body[0].indexOf(zhTasks.searching) >= 0, '列表在途应显示「搜索中...」');
  assert.ok(r.calls.body[0].indexOf(zhTasks.empty.no_tasks_desc) < 0, '列表在途不得显示空态文案');
  assert.ok(r.calls.pager.length === 1, '忙态渲染应同步更新 pager');
}

// 5b. 清空搜索词在途 → 渲染「加载中...」
{
  const r = runBusyRender({ query: '', loading: true, reqSeq: 2 });
  assert.strictEqual(r.applied, true, '清空搜索词在途应渲染列表忙态');
  assert.ok(r.calls.body[0].indexOf(zhTasks.loading) >= 0, '无搜索词在途应显示「加载中...」');
  assert.ok(r.calls.body[0].indexOf(zhTasks.searching) < 0, '无搜索词不应显示「搜索中...」');
}

// 5c. 已返回（loading=false）→ 不覆盖 DOM，交由 _taskListEmptyHtml 走真实空态
{
  const r = runBusyRender({ query: 'needle', loading: false, reqSeq: 2, bodyHtml: '<div>old</div>' });
  assert.strictEqual(r.applied, false, '已返回时不应强制渲染忙态');
  assert.strictEqual(r.calls.body.length, 0, '已返回时不应覆盖列表现有 DOM');
}

// 5d. 已有数据 / 非列表 / 非仓库全部 / 容器缺失 → 不介入
{
  assert.strictEqual(runBusyRender({ query: 'x', loading: true, reqSeq: 2, tasksAll: [{ id: 1 }] }).applied, false, '已有数据不应渲染忙态');
  assert.strictEqual(runBusyRender({ query: 'x', loading: true, reqSeq: 2, view: 'board' }).applied, false, '看板视图不应走列表忙态');
  assert.strictEqual(runBusyRender({ query: 'x', loading: true, reqSeq: 2, viewMode: 'mine' }).applied, false, '我的需求视图不改');
  assert.strictEqual(runBusyRender({ query: 'x', loading: true, reqSeq: 2, missingBody: true }).applied, false, '列表容器缺失时安全跳过');
}

console.log('✓ Issue #1961 board searching empty: all assertions passed');
