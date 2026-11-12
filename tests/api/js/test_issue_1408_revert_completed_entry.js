/*
 * Issue #1408 回归测试：需求详情页「撤销完成」入口（本人 / 仓库管理员可见可点）
 *
 * 运行：node tests/api/js/test_issue_1408_revert_completed_entry.js
 *
 * 验证点：
 *   1. completed 状态：本人可见可点（onclick=revertTaskCompleted）
 *   2. completed 状态：仓库管理员可撤销他人需求（_isRepoAdmin393 命中）
 *   3. completed 状态：非本人且非管理员 → 不渲染该按钮
 *   4. 非 completed 状态（pending_review/cancelled/processing）一律不渲染
 *   5. 按钮带 tooltip 功能说明（tasks.revert_completed_hint）且使用任务操作区按钮样式
 *   6. revertTaskCompleted 存在且 PUT /issues/{id}/status {"status":"pending_review"}
 *   7. 二次确认（_grapeConfirm）+ 成功 toast key + 流水线区刷新链路
 *   8. 旧的 @deprecated changeTaskStatus 死代码已移除
 *   9. zh/en 双语 locale key 齐备
 *
 * 策略：从 issues.js 源码提取函数体，在 mock 沙箱执行。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const issuesJsPath = path.resolve(__dirname, '../../../src/api/static/js/pages/issues.js');
const src = fs.readFileSync(issuesJsPath, 'utf8');

// ── 提取函数体 ──
function extractFn(name) {
  const re = new RegExp('function ' + name + '\\([^)]*\\)\\s*\\{([\\s\\S]*?)\\n\\}(?:\\s*\\n)');
  const m = src.match(re);
  if (!m) {
    console.error('✗ 无法从 issues.js 提取 ' + name + ' 函数体');
    process.exit(1);
  }
  return m[1];
}

const renderActionsBody = extractFn('_renderTaskActionsInline');
const isAdminBody = extractFn('_isRepoAdmin393');
const isOwnerBody = extractFn('_isTaskOwner1408');
const canRevertBody = extractFn('_canRevertCompleted1408');

const REPO_KEY = 'gitcode:org/repo1408';
const adminCache = { repos: [{ repo_key: REPO_KEY, full_name: 'org/repo1408', role: 'Admin', can_host: true }] };
const memberCache = { repos: [{ repo_key: REPO_KEY, full_name: 'org/repo1408', role: 'Member', can_host: false }] };

// ── mock 环境 ──
function makeSandbox(opts) {
  const o = opts || {};
  const store = {};
  if (o.repoCache !== undefined) store['repo_member_cache'] = JSON.stringify(o.repoCache);
  if (o.user !== undefined) store['user'] = JSON.stringify(o.user);
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  const I18N = { t: (k) => k };
  const windowMock = {
    stripRepoPrefix: (s) => String(s || '').replace(/^(gitcode|github|gitlab|gitee):/, ''),
    escHtml: (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
  };
  const _isRepoAdmin393 = new Function(
    'localStorage', 'window', 'return function _isRepoAdmin393(repoKey) {' + isAdminBody + '}')(
    localStorage, windowMock);
  const _isTaskOwner1408 = new Function(
    'localStorage', 'return function _isTaskOwner1408(t) {' + isOwnerBody + '}')(
    localStorage);
  const _canRevertCompleted1408 = new Function(
    '_isTaskOwner1408', '_isRepoAdmin393',
    'return function _canRevertCompleted1408(t) {' + canRevertBody + '}')(
    _isTaskOwner1408, _isRepoAdmin393);
  const render = new Function(
    'I18N', 'window', '_isRepoAdmin393', '_canRevertCompleted1408',
    'return function _renderTaskActionsInline(t) {' + renderActionsBody + '}')(
    I18N, windowMock, _isRepoAdmin393, _canRevertCompleted1408);
  return { localStorage, _isRepoAdmin393, _isTaskOwner1408, _canRevertCompleted1408, render, I18N };
}

const OWNER_USER = { id: 'uid-owner-1408', username: 'task_owner_1408' };
const OTHER_USER = { id: 'uid-other-1408', username: 'task_other_1408' };

function renderAs(user, repoCache, task) {
  const sb = makeSandbox({ user, repoCache });
  return sb.render(task);
}

// ── 1: 本人（completed）→ 渲染可点按钮 ──
{
  const html = renderAs(OWNER_USER, memberCache, {
    id: 140801, status: 'completed', repo_key: REPO_KEY, creator: 'task_owner_1408',
  });
  assert.ok(html.includes('revertTaskCompleted('), '本人应渲染可点的撤销完成按钮');
  assert.ok(!html.includes('disabled'), '本人按钮不应置灰');
  assert.ok(html.includes('tasks.revert_completed'), '按钮文案用 tasks.revert_completed');
  assert.ok(html.includes('tsk-btn-revert'), '按钮应复用任务操作区按钮样式（hover 过渡）');
  assert.ok(html.includes('tasks.revert_completed_hint'), '按钮应带 hover tooltip 功能说明');
}

// ── 2: 仓库管理员撤销他人需求 → 渲染可点按钮 ──
{
  const html = renderAs(OTHER_USER, adminCache, {
    id: 140802, status: 'completed', repo_key: REPO_KEY, creator: 'someone_else',
  });
  assert.ok(html.includes('revertTaskCompleted('), '仓库管理员应可见可点撤销完成按钮');
}

// ── 3: 非本人且非管理员 → 不渲染 ──
{
  const html = renderAs(OTHER_USER, memberCache, {
    id: 140803, status: 'completed', repo_key: REPO_KEY, creator: 'someone_else',
  });
  assert.ok(!html.includes('revertTaskCompleted('), '非本人且非管理员不应渲染撤销完成按钮');
  assert.ok(!html.includes('tasks.revert_completed'), '非授权范围不应出现文案');
}

// ── 4: 非 completed 状态一律不渲染 ──
{
  ['pending_review', 'cancelled', 'processing', 'active', 'awaiting_input'].forEach(function (st) {
    const html = renderAs(OWNER_USER, adminCache, {
      id: 140804, status: st, repo_key: REPO_KEY, creator: 'task_owner_1408',
    });
    assert.ok(!html.includes('revertTaskCompleted('), st + ' 状态不应渲染撤销完成按钮');
  });
}

// ── 5: 本人判定口径（created_by / user_id 双通道）──
{
  const sb = makeSandbox({ user: OWNER_USER, repoCache: memberCache });
  assert.strictEqual(sb._isTaskOwner1408({ creator: 'task_owner_1408' }), true, 'creator 命中 → 本人');
  assert.strictEqual(sb._isTaskOwner1408({ creator: 'nobody' }), false, 'creator 不命中 → 非本人');
  assert.strictEqual(sb._isTaskOwner1408({ user_id: 'uid-owner-1408' }), true, 'user_id 命中 → 本人');
  assert.strictEqual(sb._isTaskOwner1408(null), false, '空任务 → false');
  const sbAnon = makeSandbox({ repoCache: memberCache });
  assert.strictEqual(sbAnon._isTaskOwner1408({ creator: 'task_owner_1408' }), false, '未登录 → 非本人');
}

// ── 6/7: 撤销提交链路（二次确认 + PUT 状态迁移 + toast + 流水线刷新）──
assert.ok(/window\.revertTaskCompleted\s*=\s*function/.test(src), '应定义 window.revertTaskCompleted');
const revertStart = src.indexOf('window.revertTaskCompleted');
const revertEnd = src.indexOf('function _renderTaskActionsBar', revertStart);
const revertSrc = src.slice(revertStart, revertEnd);
assert.ok(revertSrc.includes("API.put('/issues/' + taskId + '/status', {status: 'pending_review'})"),
  '应 PUT /issues/{id}/status 且目标状态 pending_review');
assert.ok(revertSrc.includes('_grapeConfirm('), '撤销按钮应走 _grapeConfirm 二次确认');
assert.ok(revertSrc.includes("tasks.revert_completed_message"), '应使用二次确认文案 key');
assert.ok(revertSrc.includes("tasks.revert_completed_toast"), '应使用成功 toast key');
assert.ok(revertSrc.includes('_withTaskLoading('), '应复用任务操作 loading/toast 链路');
assert.ok(revertSrc.includes('_renderPipelineSection(data)'), '撤销成功后应重拉并重绘流水线区');
assert.ok(revertSrc.includes('loadTasks(') || src.includes('loadTasks();'),
  '撤销成功后应刷新列表（_withTaskLoading 内 loadTasks）');

// ── 8: 旧死代码已移除 ──
assert.ok(!/function changeTaskStatus\s*\(/.test(src), '@deprecated changeTaskStatus 应已移除');

// ── 9: 双语 locale key 齐备 ──
const zh = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../src/api/static/locales/zh/tasks.json'), 'utf8'));
const en = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../src/api/static/locales/en/tasks.json'), 'utf8'));
for (const k of ['revert_completed', 'revert_completed_message', 'revert_completed_hint', 'revert_completed_toast']) {
  assert.ok(zh[k], 'zh 缺 key: ' + k);
  assert.ok(en[k], 'en 缺 key: ' + k);
}

// ── 10: 样式与缓存版本 ──
const css = fs.readFileSync(path.resolve(__dirname, '../../../src/api/static/css/app.css'), 'utf8');
assert.ok(/\.tsk-btn-revert:hover:not\(:disabled\)\{[^}]*background:#fff/.test(css),
  '撤销完成按钮 hover 应背景变亮');
assert.ok(/\.tsk-btn-revert\{[^}]*transition:[^}]*\.15s/.test(css), 'hover 过渡应 ≥120ms（实现 150ms）');
assert.ok(/\.tsk-btn-revert:disabled\{[^}]*cursor:not-allowed/.test(css), '禁用态应 cursor:not-allowed');
const indexHtml = fs.readFileSync(path.resolve(__dirname, '../../../src/api/static/index.html'), 'utf8');
assert.ok(/issues\.js\?v=138/.test(indexHtml), 'issues.js 缓存版本应 bump 到 138');

console.log('✓ Issue #1408 revert completed entry: all assertions passed');