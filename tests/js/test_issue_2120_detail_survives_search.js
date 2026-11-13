/*
 * Issue #2120 回归测试：需求中心「仓库全部」搜索响应落地时把详情页顶回列表
 *
 * 运行：node tests/js/test_issue_2120_detail_survives_search.js
 *
 * 验证点：
 *   1. 搜索框不再绑 onchange 提交；Enter / 搜索按钮显式提交 _submitTaskSearch。
 *   2. _submitTaskSearch 只在提交时写 _taskQuery，输入过程不触发重置。
 *   3. 详情态下重置类响应不调用 _partialRenderTasks / _render（真实容器缺失时
 *      _partialRenderTasks 会回退整页 _render），只刷新数据引用并按需就地更新详情。
 *   4. 重置后 _currentDetailTask / _taskSel 保留并合并新字段；不在结果集时保留旧对象并提示。
 *   5. _render 入口兜底仍保留：整页渲染后重开详情，新结果集缺失时用旧对象。
 *   6. index.html chunk 版本号已 bump 到 137，locale 键落到 zh/en。
 *
 * 策略：从 issues.js 源码提取 loadTasks / _partialRenderTasks / _render 等真实函数体，
 * 在 mock 沙箱执行（不 stub 被测的真实渲染函数，只 stub 其外围依赖）。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const root = path.resolve(__dirname, '../..');
const src = fs.readFileSync(path.join(root, 'src/api/static/js/pages/issues.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'src/api/static/index.html'), 'utf8');
const zhTasks = JSON.parse(fs.readFileSync(path.join(root, 'src/api/static/locales/zh/tasks.json'), 'utf8'));
const enTasks = JSON.parse(fs.readFileSync(path.join(root, 'src/api/static/locales/en/tasks.json'), 'utf8'));

function extractFn(name) {
  const re = new RegExp('function ' + name + '\\([^)]*\\)\\s*\\{([\\s\\S]*?)\\n\\}(?:\\s*\\n)');
  const m = src.match(re);
  if (!m) {
    console.error('✗ 无法从 issues.js 提取 ' + name + ' 函数体');
    process.exit(1);
  }
  return m[1];
}

function tick() {
  return new Promise(function (resolve) { setImmediate(resolve); });
}

// ── 1. 静态断言：搜索显式提交 + 版本号 / locale ──
assert.ok(!src.includes('onchange="_taskQuery=this.value;_resetTaskListAndLoad()"'),
  '搜索框不应再绑定 onchange 失焦即提交');
assert.ok(src.includes('function _submitTaskSearch()'), '应定义 _submitTaskSearch 显式提交入口');
const submitRefs = src.match(/_submitTaskSearch\(\)/g) || [];
assert.ok(submitRefs.length >= 5, 'Enter + 搜索按钮（看板/列表两处）均应调用 _submitTaskSearch，实际 ' + submitRefs.length);
assert.ok(/issues\.js\?v=142/.test(indexHtml), 'index.html 中 issues.js 版本号应 bump 到 v=142');
assert.ok(!/issues\.js\?v=138/.test(indexHtml), 'index.html 不应残留旧版本 v=141');
assert.strictEqual(zhTasks.detail_not_in_results, '该需求不在当前筛选结果中', 'zh tasks.json 应存在 detail_not_in_results');
assert.strictEqual(enTasks.detail_not_in_results, 'This issue is not in the current filter results', 'en tasks.json 应存在 detail_not_in_results');

// 详情态分支不得再走 _partialRenderTasks（真实容器缺失会回退整页 _render）
assert.ok(src.includes('function _refreshDetailInPlace(t, force)'), '应定义详情就地刷新函数 _refreshDetailInPlace');
assert.ok(src.includes('id="_boardDetailTitle"'), '详情页头部标题应有 _boardDetailTitle 容器标识供就地刷新');
assert.ok(src.includes('id="_boardDetailStatusActions"'), '详情页状态动作按钮应有 _boardDetailStatusActions 容器标识供就地刷新');
const detailBranch = src.slice(src.indexOf('} else if (window._currentDetailTask) {'), src.indexOf('} else {', src.indexOf('} else if (window._currentDetailTask) {')));
assert.ok(!detailBranch.includes('_partialRenderTasks('), '详情态重置分支不应调用 _partialRenderTasks');
assert.ok(detailBranch.includes('_refreshDetailInPlace('), '详情态重置分支应就地刷新详情内容');

// ── 2. 沙箱：_submitTaskSearch 仅提交时写 _taskQuery ──
const submitBody = extractFn('_submitTaskSearch');
function buildSubmit(initialValue) {
  const calls = { reset: 0 };
  const fn = new Function('document', '_resetTaskListAndLoad',
    'var _taskQuery = "old";\n' +
    'return function () {' + submitBody + '\nreturn _taskQuery; };'
  )(
    { getElementById: function () { return { value: initialValue }; } },
    function () { calls.reset++; }
  );
  return { fn: fn, calls: calls };
}
{
  const s = buildSubmit('needle');
  assert.strictEqual(s.fn(), 'needle', '提交时应把输入框值写入 _taskQuery');
  assert.strictEqual(s.calls.reset, 1, '提交应触发一次重置加载');
}
{
  const s = buildSubmit('typed-not-submitted');
  assert.strictEqual(src.includes('_taskQuery=this.value;_resetTaskListAndLoad()'), false,
    '输入过程不应直接写 _taskQuery 并发起重置');
  assert.ok(s.calls.reset === 0, '未调用提交入口时不应重置');
}

// ── 3. 真实渲染链路沙箱（不 stub _partialRenderTasks / _render） ──
const loadBody = extractFn('loadTasks');
const partialBody = extractFn('_partialRenderTasks');
const renderBody = extractFn('_render');
const signatureBody = extractFn('_detailSignature');
const mergeBody = extractFn('_mergeDetailFields');
const refreshBody = extractFn('_refreshDetailInPlace');
const restoreBody = extractFn('_restoreDetailAfterRender');

const SANDBOX_SRC = 'with (ctx) { return (function () {\n'
  + 'function loadTasks(opts) {' + loadBody + '}\n'
  + 'function _partialRenderTasks(newItems) {' + partialBody + '}\n'
  + 'function _render() {' + renderBody + '}\n'
  + 'function _detailSignature(t) {' + signatureBody + '}\n'
  + 'function _mergeDetailFields(prevDetail, next) {' + mergeBody + '}\n'
  + 'function _refreshDetailInPlace(t, force) {' + refreshBody + '}\n'
  + 'function _restoreDetailAfterRender(taskId, prevDetail) {' + restoreBody + '}\n'
  + 'return { loadTasks: loadTasks, _partialRenderTasks: _partialRenderTasks, _render: _render, _detailSignature: _detailSignature, _restoreDetailAfterRender: _restoreDetailAfterRender };\n'
  + '})(); }';

function element(initialHtml) {
  return {
    changes: 0,
    _html: initialHtml || '',
    textContent: initialHtml || '',
    set innerHTML(v) { this._html = String(v); this.changes++; },
    get innerHTML() { return this._html; },
  };
}

function makeHarness(opts) {
  opts = opts || {};
  const els = {};
  const page = element(opts.pageHtml || '');
  els.pageContent = page;
  let resolveApi = null;
  const calls = { open: 0, toast: [], attachments: 0, busy: 0, prDetail: 0 };
  const document = { getElementById: function (id) { return els[id] || null; } };
  const window = {
    _currentView: 'tasks',
    _currentDetailTask: null,
    _tasksById: {},
    _pendingTaskDetailId: null,
    _pendingIssueNumber: null,
    _detailMissingNotified: false,
    _detailPendingRefresh: false,
    _pipeEventsShown: 20,
  };
  const ctx = {
    window: window,
    document: document,
    app: {
      currentRepo: function () { return { owner: 'o', name: 'r', repoKey: 'gitcode:o/r', fullName: 'o/r' }; },
      showToast: function (m) { calls.toast.push(m); },
      nav: function () {},
    },
    API: { get: function () { return new Promise(function (resolve) { resolveApi = resolve; }); } },
    I18N: { t: function (k, params, fallback) { return fallback !== undefined ? fallback : k; } },
    _tasksAll: opts.tasksAll || [],
    _taskPage: 1,
    _tasksTotal: 0,
    _tasksHasMore: false,
    _tasksLoading: false,
    _taskReqSeq: 0,
    _taskFilterStatuses: [],
    _taskFilterAssignees: [],
    _taskQuery: '',
    _tasksMode: 'time',
    _tasksSortOrder: 'desc',
    _tasksViewMode: 'repo_all',
    _taskView: 'list',
    _taskSel: opts.taskSel || null,
    _TASKS_PER_PAGE: 30,
    _TASK_STATUS_TO_API: {},
    _taskFilterPopoverOpen: false,
    _loadTasksSortPref: function () {},
    _renderTaskListBusyIfNeeded: function () { calls.busy++; },
    _filteredTasks: function () { return ctx._tasksAll; },
    _mapStatusL: function () { return 'pending'; },
    _lStatusInfoMap: function () { return {}; },
    _taskRowHtml: function (t) { return '<div class="row">' + t.title + '</div>'; },
    _taskListBodyHtml: function (rows) { return rows.map(function (t) { return '<div class="row">' + t.title + '</div>'; }).join(''); },
    _taskListEmptyHtml: function () { return ''; },
    _taskListPagerHtml: function () { return ''; },
    _detailHtml: function (t) { return '<div class="detail">' + (t.title || '') + '</div>'; },
    _renderBoard: function () { return ''; },
    _boardPagingActive: function () { return false; },
    _taskSentinelHtml: function () { return ''; },
    _loadDeployLink: function () {},
    _observeTaskSentinel: function () {},
    _bindBoardScroll: function () {},
    _renderTaskActionsInline: function (t2) { return '<span class="act">' + ((t2 && t2.status) || '') + '</span>'; },
    _renderCrossPipelineButton: function () { return ''; },
    _vcsUrl: function () { return ''; },
    _applyFilterPopoverState: function () {},
    _openDeeplinkDetailViaApi: function () {},
    _openPRDetail: function (t2) { calls.prDetail++; window._prDetailTask = t2; },
    _openBoardDetail: function (id) { calls.open++; window._currentDetailTask = window._tasksById[id] || window._currentDetailTask; window._pipeEventsShown = 20; if (!els._boardDetailContent) els._boardDetailContent = element('<div class="detail">reopened</div>'); },
    _loadTaskAttachments: function () { calls.attachments++; },
  };
  window._openBoardDetail = ctx._openBoardDetail;
  const api = new Function('ctx', SANDBOX_SRC)(ctx);

  if (opts.detail) {
    // 模拟用户点卡片进入详情：_openBoardDetail 整体替换 #pageContent，列表容器消失
    els._boardDetailContent = element('<div class="detail">' + (opts.detail.title || '') + '</div>');
    els._boardPipelineSection = element('<div class="pipeline">loaded</div>');
    // 头部标题与状态动作按钮：与真实详情页一致，位于 #_boardDetailContent 之外
    var titleEl = element('');
    titleEl.textContent = opts.detail.title || '';
    els._boardDetailTitle = titleEl;
    els._boardDetailStatusActions = element(ctx._renderTaskActionsInline(opts.detail));
    page._html = '<div class="detail-page"><div id="_boardDetailTitle"></div><div id="_boardDetailContent"></div><div id="_boardPipelineSection">loaded</div></div>';
    window._currentDetailTask = opts.detail;
    ctx._taskSel = opts.detail;
  }
  page.changes = 0;  // 只统计测试开始后的整页写入
  return {
    api: api, ctx: ctx, window: window, document: document, els: els, page: page, calls: calls,
    resolve: function (payload) { resolveApi(payload); },
  };
}

async function runDetailReset(opts) {
  const h = makeHarness(opts);
  h.api.loadTasks();
  await tick();
  const pageWritesBefore = h.page.changes;
  h.resolve(opts.response);
  await tick();
  await tick();
  return { h: h, pageWritesBefore: pageWritesBefore };
}

(async function () {
  // 3a. 详情态：延时响应落地不整页重绘，详情 DOM 保留，内容就地更新
  {
    const oldDetail = { id: 7, title: 'old', status: 'active', task_description: 'keep me' };
    const r = await runDetailReset({
      detail: oldDetail,
      taskSel: oldDetail,
      response: { issues: [{ id: 7, title: 'new', status: 'processing' }, { id: 8, title: 'other' }], total: 2, has_more: false, page: 1 },
    });
    assert.strictEqual(r.pageWritesBefore, 0, '响应落地前不应写整页 DOM');
    assert.strictEqual(r.h.page.changes, 0, '详情态重置响应不应整页重绘 _render');
    assert.strictEqual(r.h.calls.open, 0, '详情态重置不应重开详情');
    assert.strictEqual(r.h.window._pipeEventsShown, 20, '详情流水线展开量不应被重置');
    assert.strictEqual(r.h.calls.attachments, 1, '内容就地刷新后应复用缓存重新渲染附件（不重开详情）');
    assert.strictEqual(r.h.window._currentDetailTask, oldDetail, '详情对象引用应保留（合并新字段）');
    assert.strictEqual(r.h.window._currentDetailTask.title, 'new', '详情引用应合并新结果字段');
    assert.strictEqual(r.h.window._currentDetailTask.task_description, 'keep me', '详情专属字段不应被列表瘦身对象覆盖');
    assert.strictEqual(r.h.ctx._taskSel, oldDetail, '选中项应跟随详情对象');
    assert.strictEqual(r.h.els._boardDetailContent.changes, 1, '字段变化时应就地刷新 #_boardDetailContent');
    assert.ok(/new/.test(r.h.els._boardDetailContent.innerHTML), '详情内容应显示新标题');
    assert.strictEqual(r.h.els._boardDetailTitle.textContent, 'new', '头部标题应同步就地刷新');
    assert.strictEqual(r.h.els._boardDetailStatusActions.changes, 1, '状态相关动作按钮应同步重渲染');
    assert.ok(/processing/.test(r.h.els._boardDetailStatusActions.innerHTML), '动作按钮应反映合并后的新状态');
    assert.ok(r.h.window._tasksById[7] === oldDetail, '_tasksById 应指向合并后的详情对象');
  }
  // 3b. 详情态：字段未变化时不做多余的就地重绘
  {
    const oldDetail = { id: 7, title: 'same', status: 'active', task_description: 'keep me' };
    const r = await runDetailReset({
      detail: oldDetail,
      taskSel: oldDetail,
      response: { issues: [{ id: 7, title: 'same', status: 'active' }], total: 1, has_more: false, page: 1 },
    });
    assert.strictEqual(r.h.page.changes, 0, '字段未变化仍不应整页重绘');
    assert.strictEqual(r.h.els._boardDetailContent.changes, 0, '字段未变化不应重绘详情内容');
    assert.strictEqual(r.h.els._boardDetailStatusActions.changes, 0, '字段未变化不应重绘头部动作按钮');
    assert.strictEqual(r.h.calls.attachments, 0, '字段未变化不应重拉附件');
  }
  // 3c. 详情对象不在新结果集：保留旧对象、详情不关闭、提示一次
  {
    const oldDetail = { id: 7, title: 'gone' };
    const r = await runDetailReset({
      detail: oldDetail,
      taskSel: oldDetail,
      response: { issues: [{ id: 8, title: 'other' }], total: 1, has_more: false, page: 1 },
    });
    assert.strictEqual(r.h.page.changes, 0, '保留旧详情时仍不应整页重绘');
    assert.strictEqual(r.h.window._currentDetailTask, oldDetail, '详情对象不在新结果集时应保留旧对象（详情不关闭）');
    assert.strictEqual(r.h.calls.toast.length, 1, '应提示该需求不在当前筛选结果中');
    assert.ok(/不在当前筛选结果|not in the current filter/.test(r.h.calls.toast[0]), '提示文案应说明详情不在筛选结果中');
    assert.strictEqual(r.h.els._boardDetailContent.changes, 0, '缺失时不应重绘详情内容');
  }
  // 3d. 非详情态重置：真实 _render 正常整页渲染（不回归）
  {
    const r = await runDetailReset({ response: { issues: [{ id: 8, title: 'only' }], total: 1, has_more: false, page: 1 } });
    assert.strictEqual(r.h.page.changes, 1, '非详情态重置仍走整页 _render');
    assert.strictEqual(r.h.calls.open, 0, '非详情态不应打开详情');
    assert.strictEqual(r.h.ctx._tasksAll.length, 1, '非详情态重置应更新 _tasksAll');
  }
  // 3e. 对照：真实 _partialRenderTasks 在详情 DOM（容器缺失）下必定回退整页 _render
  {
    const detail = { id: 7, title: 'detail' };
    const h = makeHarness({ detail: detail, tasksAll: [detail], taskSel: detail });
    h.api._partialRenderTasks(null);
    assert.strictEqual(h.page.changes, 1, '真实 _partialRenderTasks 容器缺失时应回退整页 _render（证明未 stub）');
    assert.strictEqual(h.calls.open, 1, '真实 _render 兜底会重开详情（说明详情态必须绕开该路径）');
  }

  // 3f. PR 伪卡片详情：无 #_boardDetailContent，状态变化时应按需重渲染 _openPRDetail
  {
    const prDetail = { id: 9, title: 'pr', status: 'processing', is_pr_event: true };
    const h = makeHarness({ detail: prDetail, taskSel: prDetail });
    delete h.els._boardDetailContent;  // PR 伪卡片详情没有该内容容器
    h.api.loadTasks();
    await tick();
    h.resolve({ issues: [{ id: 9, title: 'pr', status: 'completed' }], total: 1, has_more: false, page: 1 });
    await tick();
    await tick();
    assert.strictEqual(h.page.changes, 0, 'PR 详情刷新不应整页重绘 _render');
    assert.strictEqual(h.calls.open, 0, 'PR 详情刷新不应重开需求详情');
    assert.strictEqual(h.window._currentDetailTask.status, 'completed', 'PR 详情引用应合并新状态');
    assert.strictEqual(h.calls.prDetail, 1, 'PR 详情状态变化时应按需重渲染 _openPRDetail');
    assert.strictEqual(h.window._prDetailTask, prDetail, '重渲染应传入合并后的详情对象');
    assert.strictEqual(h.window._detailPendingRefresh, false, '重渲染后应清理待刷新标记');
  }
  // 3g. PR 伪卡片详情：状态未变化时不做多余重渲染
  {
    const prDetail = { id: 9, title: 'pr', status: 'processing', is_pr_event: true };
    const h = makeHarness({ detail: prDetail, taskSel: prDetail });
    delete h.els._boardDetailContent;
    h.api.loadTasks();
    await tick();
    h.resolve({ issues: [{ id: 9, title: 'pr', status: 'processing' }], total: 1, has_more: false, page: 1 });
    await tick();
    await tick();
    assert.strictEqual(h.calls.prDetail, 0, 'PR 详情状态未变化时不应重渲染');
    assert.strictEqual(h.page.changes, 0, 'PR 详情未变化时也不应整页重绘');
  }

  // 3h. _restoreDetailAfterRender：新结果集对象与 prevDetail 合并，保留详情专属字段
  {
    const h = makeHarness();
    const prev = { id: 7, title: 'old', status: 'active', task_description: 'keep desc', attachments: [{ filename: 'a.png' }] };
    h.window._currentDetailTask = prev;
    h.window._tasksById = { 7: { id: 7, title: 'new', status: 'completed' } };
    const ok = h.api._restoreDetailAfterRender(7, prev);
    assert.strictEqual(ok, true, '应成功重开详情');
    assert.strictEqual(h.window._tasksById[7], prev, '_tasksById 应指向合并后的旧详情对象');
    assert.strictEqual(prev.title, 'new', '新结果集字段应覆盖旧值');
    assert.strictEqual(prev.status, 'completed', '新状态应合并到详情对象');
    assert.strictEqual(prev.task_description, 'keep desc', 'task_description 不应被列表瘦身对象丢失');
    assert.deepStrictEqual(prev.attachments.map(function (a) { return a.filename; }), ['a.png'], '附件清单不应丢失');
  }
  // 3i. _detailSignature 覆盖附件等详情渲染字段
  {
    const h = makeHarness();
    const base = { id: 1, title: 't', status: 'active' };
    const noAtt = h.api._detailSignature(base);
    const withCount = h.api._detailSignature(Object.assign({}, base, { attachments_count: 2 }));
    assert.notStrictEqual(withCount, noAtt, 'attachments_count 变化应触发详情刷新签名变化');
    const withAtts = h.api._detailSignature(Object.assign({}, base, { attachments: [{ filename: 'x.png', size: 10 }] }));
    assert.notStrictEqual(withAtts, noAtt, '附件清单变化应触发详情刷新签名变化');
    assert.notStrictEqual(h.api._detailSignature(Object.assign({}, base, { created_at: '2026-01-01' })), noAtt,
      'created_at 变化应触发详情刷新签名变化');
  }
  // 3j. _renderBoardCols：详情态下列容器缺失不得回退 _render，应就地刷新详情
  {
    const boardBody = extractFn('_renderBoardCols');
    const detail = { id: 7, title: 'old', status: 'active', task_description: 'keep' };
    const next = { id: 7, title: 'new', status: 'completed' };
    let renders = 0;
    let refreshCalls = [];
    const ctx = {
      window: { _currentDetailTask: detail, _tasksById: {}, _detailPendingRefresh: true },
      document: { getElementById: function () { return null; } },
      _boardPagingActive: function () { return true; },
      _filteredTasks: function () { return [next]; },
      _detailSignature: function (t) { return [t.title, t.status, t.task_description].join('|'); },
      _mergeDetailFields: function (prev, n) { Object.keys(n).forEach(function (k) { if (n[k] !== undefined) prev[k] = n[k]; }); return prev; },
      _refreshDetailInPlace: function (t, force) { refreshCalls.push({ t: t, force: force }); return true; },
      _openPRDetail: function () { throw new Error('非 PR 详情不应重渲染'); },
      _render: function () { renders++; },
      _taskSel: null,
      _BOARD_COLS: [],
    };
    const fn = new Function('ctx', 'with (ctx) { return function _renderBoardCols() {' + boardBody + '}; }')(ctx);
    fn();
    assert.strictEqual(renders, 0, '详情态列容器缺失不应回退整页 _render');
    assert.strictEqual(refreshCalls.length, 1, '详情态列响应落地应就地刷新详情');
    assert.strictEqual(refreshCalls[0].t, detail, '刷新应使用合并后的详情对象');
    assert.strictEqual(refreshCalls[0].force, true, '字段变化时应强制刷新详情');
    assert.strictEqual(detail.status, 'completed', '详情对象应合并列响应新状态');
    assert.strictEqual(detail.task_description, 'keep', '详情专属字段应保留');
    assert.strictEqual(ctx.window._tasksById[7], detail, '_tasksById 应指向详情对象');
  }

  // ── 4. _render 兜底闸门仍保留 ──
  assert.ok(src.includes('var _detailIdBeforeRender = window._currentDetailTask'),
    '_render 入口应记录详情 id（独立变量兜底）');
  assert.ok(src.includes('_restoreDetailAfterRender(_detailIdBeforeRender, _detailBeforeRender)'),
    '_render 渲染完成后应重开详情');
  assert.ok(!/_detailIdBeforeRender\s*=\s*window\._pendingTaskDetailId/.test(src),
    '兜底变量不得与 _pendingTaskDetailId 深链变量混用');

  // ── 5. 语法完整性 ──
  new (require('vm').Script)(src, { filename: 'issues.js' });

  console.log('✓ Issue #2120 detail survives search: all assertions passed');
})().catch(function (e) {
  console.error(e);
  process.exit(1);
});