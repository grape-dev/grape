/*
 * Issue #1077 回归测试：需求中心「仓库全部」滚动分页加载体验优化
 *
 * 运行：node tests/js/test_issue_1077_smooth_pagination.js
 *
 * 验证点：
 *   1. _partialRenderTasks 存在；loadTasks append/merge 成功路径走局部更新而非整页 _render()
 *   2. append 路径：_tasksMode==='time' 时仅 insertAdjacentHTML 追加新行，不重绘已有行
 *   3. merge 路径：仅重绘列表容器内部（taskListBody/taskListPager），顶部/详情不动
 *   4. 局部更新不调用 _loadDeployLink（部署链接仅整页 _render 加载一次）
 *   5. IntersectionObserver 复用：_observeTaskSentinel 不再 disconnect 重建，含 200ms 重入窗口
 *   6. 列表/看板 sentinel 统一为 _grapeSpin spinner（不再出现纯文字「加载中…」sentinel）
 *   7. _render 列表结构含稳定的 taskListBody / taskListPager / taskBoardScroll 容器
 *
 * 策略：从 issues.js 源码提取 _partialRenderTasks 函数体，在 mock 沙箱执行。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const issuesJsPath = path.resolve(__dirname, '../../src/api/static/js/pages/issues.js');
const src = fs.readFileSync(issuesJsPath, 'utf8');

function extractFn(name) {
  const re = new RegExp('function ' + name + '\\([^)]*\\)\\s*\\{([\\s\\S]*?)\\n\\}(?:\\s*\\n)');
  const m = src.match(re);
  if (!m) {
    console.error('✗ 无法从 issues.js 提取 ' + name + ' 函数体');
    process.exit(1);
  }
  return m[1];
}

// ── 1. 静态断言：局部更新路径接入 loadTasks ──
assert.ok(src.includes('function _partialRenderTasks(newItems)'), '应定义 _partialRenderTasks(newItems)');
assert.ok(/_partialRenderTasks\(_append \? _newItems : null\)/.test(src), 'loadTasks 应调用 _partialRenderTasks（append 传新项，merge 传 null）');
assert.ok(src.includes('var _newItems = [];'), 'append 合并循环应记录新增项');
// deploy link 仅整页 _render 一次
const deployCalls = src.match(/_loadDeployLink\(\);/g) || [];
assert.strictEqual(deployCalls.length, 1, '_loadDeployLink() 应只在整页 _render 中调用一次，实际 ' + deployCalls.length + ' 次');

// ── 2. observer 复用 + 重入窗口 ──
const obsBody = extractFn('_observeTaskSentinel');
assert.ok(!obsBody.includes('disconnect()'), '_observeTaskSentinel 不应 disconnect 重建 observer（复用实例）');
assert.ok(/_taskObsReentryUntil = Date\.now\(\) \+ 200/.test(src), 'observer 重用时应设置 200ms 重入窗口');
assert.ok(/Date\.now\(\) < _taskObsReentryUntil/.test(obsBody), 'observer 回调应检查重入窗口');

// ── 3. sentinel spinner：列表/看板统一，无纯文字加载态 ──
assert.ok(src.includes('function _taskSentinelHtml()'), '应定义 _taskSentinelHtml 统一 sentinel');
assert.ok(/_grapeSpin/.test(extractFn('_taskSentinelHtml')), 'sentinel 应使用 _grapeSpin spinner 动画');
assert.ok(/animation:_grapeSpin \.7s linear infinite/.test(extractFn('_taskSentinelHtml')), 'spinner 应带旋转动画');
// 历史的纯文字 sentinel 文案已移除（spinner 取代）
assert.ok(!/id="taskListSentinel"[^>]*>.*加载中/.test(src), 'sentinel 不应再是纯文字「加载中」');
// 列表 pager 与看板 sentinel 都走 _taskSentinelHtml
assert.ok(extractFn('_taskListPagerHtml').includes('_taskSentinelHtml()'), '列表 pager sentinel 应复用 _taskSentinelHtml');

// ── 4. _render 结构：稳定容器 id（局部更新锚点） ──
assert.ok(src.includes('id="taskListBody"'), '_render 列表应有 taskListBody 容器');
assert.ok(src.includes('id="taskListPager"'), '_render 列表应有 taskListPager 容器');
assert.ok(src.includes('id="taskBoardScroll"'), '_render 看板应有 taskBoardScroll 容器');

// ── 5. 行为测试：沙箱执行 _partialRenderTasks ──
const partialBody = extractFn('_partialRenderTasks');
const emptyHtml = '<div class="tsk-empty">EMPTY</div>';

function runPartial(state, newItems, domOverrides) {
  const calls = { insertAdjacent: [], bodyInner: [], pagerInner: [], boardInner: [], deploy: 0, observe: 0, fullRender: 0 };
  const els = {
    taskListBody: {
      innerHTML: state.bodyHtml || '',
      set innerHTML(v) { this._ih = v; calls.bodyInner.push(v); },
      get innerHTML() { return this._ih !== undefined ? this._ih : (state.bodyHtml || ''); },
      insertAdjacentHTML: function(pos, html) { calls.insertAdjacent.push({pos, html}); },
    },
    taskListPager: { innerHTML: '', set innerHTML(v) { calls.pagerInner.push(v); } },
    taskBoardScroll: { innerHTML: '', set innerHTML(v) { calls.boardInner.push(v); } },
  };
  const document = {
    getElementById: (id) => (id in els ? (domOverrides && domOverrides[id] === null ? null : els[id]) : null),
    createElement: () => ({ set textContent(v) {}, }),
    head: { appendChild: () => {} },
  };
  const ctx = {
    document,
    window: {},
    _filteredTasks: () => state.filtered,
    _taskSel: state.taskSel || null,
    _taskView: state.taskView || 'list',
    _tasksAll: state.tasksAll,
    _tasksViewMode: 'repo_all',
    _tasksHasMore: state.hasMore !== undefined ? state.hasMore : true,
    _tasksMode: state.mode || 'time',
    _tasksTotal: state.total || state.tasksAll.length,
    _taskRowHtml: (t) => '<div class="tsk-row" data-id="' + t.id + '">' + t.title + '</div>',
    _taskListBodyHtml: (rows) => rows.map(_t => '<div class="tsk-row" data-id="' + _t.id + '">' + _t.title + '</div>').join('') || emptyHtml,
    _taskListPagerHtml: () => '<div id="taskListSentinel"></div>',
    _taskSentinelHtml: () => '<div id="taskListSentinel"></div>',
    _renderBoard: () => '<div class="board"></div>',
    _render: () => { calls.fullRender++; },
    _loadDeployLink: () => { calls.deploy++; },
    _observeTaskSentinel: () => { calls.observe++; },
    I18N: { t: (k) => k },
  };
  const fn = new Function(
    'document', 'window', '_filteredTasks', '_taskSel', '_taskView', '_tasksAll', '_tasksViewMode',
    '_tasksHasMore', '_tasksMode', '_tasksTotal', '_taskRowHtml', '_taskListBodyHtml',
    '_taskListPagerHtml', '_taskSentinelHtml', '_renderBoard', '_render', '_loadDeployLink',
    '_observeTaskSentinel', 'I18N',
    'return function _partialRenderTasks(newItems) {' + partialBody + '}'
  )(
    ctx.document, ctx.window, ctx._filteredTasks, ctx._taskSel, ctx._taskView, ctx._tasksAll, ctx._tasksViewMode,
    ctx._tasksHasMore, ctx._tasksMode, ctx._tasksTotal, ctx._taskRowHtml, ctx._taskListBodyHtml,
    ctx._taskListPagerHtml, ctx._taskSentinelHtml, ctx._renderBoard, ctx._render, ctx._loadDeployLink,
    ctx._observeTaskSentinel, ctx.I18N
  );
  fn(newItems);
  return calls;
}

const ROW_A = { id: 1, title: 'row-a' };
const ROW_B = { id: 2, title: 'row-b' };
const ROW_C = { id: 3, title: 'row-c' };

// 5a. append：time 模式仅 insertAdjacentHTML 追加新行，不整页 _render、不重载 deploy link
{
  const calls = runPartial(
    { filtered: [ROW_A, ROW_B, ROW_C], tasksAll: [ROW_A, ROW_B, ROW_C], mode: 'time', bodyHtml: '<div data-id="1"></div><div data-id="2"></div>' },
    [ROW_C]
  );
  assert.strictEqual(calls.fullRender, 0, 'append 不应整页 _render');
  assert.strictEqual(calls.deploy, 0, 'append 不应调用 _loadDeployLink');
  assert.strictEqual(calls.insertAdjacent.length, 1, 'append 应 insertAdjacentHTML 追加新行');
  assert.strictEqual(calls.insertAdjacent[0].pos, 'beforeend', '追加位置应为 beforeend');
  assert.ok(calls.insertAdjacent[0].html.includes('row-c'), '追加内容应为新行');
  assert.ok(!calls.insertAdjacent[0].html.includes('row-a'), '追加内容不应包含已有行');
  assert.strictEqual(calls.bodyInner.length, 0, 'append 不应重写列表容器 innerHTML');
  assert.strictEqual(calls.observe, 1, 'append 后应重新挂载 sentinel 观察器');
}

// 5b. merge：重绘列表容器内部（不追加、不整页渲染）
{
  const calls = runPartial(
    { filtered: [ROW_A, ROW_B], tasksAll: [ROW_A, ROW_B], mode: 'time' },
    null
  );
  assert.strictEqual(calls.fullRender, 0, 'merge 不应整页 _render');
  assert.strictEqual(calls.deploy, 0, 'merge 不应调用 _loadDeployLink');
  assert.strictEqual(calls.insertAdjacent.length, 0, 'merge 不应 insertAdjacentHTML');
  assert.strictEqual(calls.bodyInner.length, 1, 'merge 应重绘列表容器内部');
  assert.ok(calls.bodyInner[0].includes('row-a') && calls.bodyInner[0].includes('row-b'), 'merge 重绘应包含全部行');
  assert.strictEqual(calls.pagerInner.length, 1, 'merge 应更新 pager');
}

// 5c. 看板模式：仅重绘 taskBoardScroll 内部
{
  const calls = runPartial(
    { filtered: [ROW_A], tasksAll: [ROW_A], taskView: 'board', mode: 'time' },
    null
  );
  assert.strictEqual(calls.fullRender, 0, '看板 merge 不应整页 _render');
  assert.strictEqual(calls.deploy, 0, '看板 merge 不应调用 _loadDeployLink');
  assert.strictEqual(calls.boardInner.length, 1, '看板 merge 应重绘滚动区内部');
}

// 5d. 分区模式（mode!=time）append：回退容器重绘（保证分区顺序正确）
{
  const calls = runPartial(
    { filtered: [ROW_A, ROW_C], tasksAll: [ROW_A, ROW_C], mode: 'default' },
    [ROW_C]
  );
  assert.strictEqual(calls.fullRender, 0, '分区模式 append 不应整页 _render');
  assert.strictEqual(calls.insertAdjacent.length, 0, '分区模式 append 不应线性追加');
  assert.strictEqual(calls.bodyInner.length, 1, '分区模式 append 应重绘列表容器');
}

// 5e. 容器缺失：回退整页 _render（安全兜底）
{
  const calls = runPartial(
    { filtered: [ROW_A], tasksAll: [ROW_A], mode: 'time' },
    null,
    { taskListBody: null }
  );
  assert.strictEqual(calls.fullRender, 1, '容器缺失应回退整页 _render');
}

console.log('✓ Issue #1077 smooth pagination: all assertions passed');
