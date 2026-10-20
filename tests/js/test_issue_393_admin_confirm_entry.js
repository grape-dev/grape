/*
 * Issue #393 回归测试：需求详情页「管理员确认完成」入口（admin 可用 / 非 admin 置灰）
 *
 * 运行：node tests/js/test_issue_393_admin_confirm_entry.js
 *
 * 验证点：
 *   1. _renderTaskActionsInline：非 completed/cancelled 时渲染「管理员确认完成」按钮
 *   2. _isRepoAdmin393：repo_member_cache 中 role==='Admin'（或 can_host）→ true
 *   3. _isRepoAdmin393：role==='Member' → false；无缓存/异常 → false（fail-safe）
 *   4. _isRepoAdmin393：repo_key 前缀（gitcode:）与缓存 repo_key/full_name 均可匹配
 *   5. 非 admin → 按钮 disabled + title 提示；admin → 可点（onclick=adminConfirmTaskDone）
 *   6. completed/cancelled 状态不渲染 admin 入口
 *
 * 策略：从 issues.js 源码提取函数体，在 mock 沙箱执行。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const issuesJsPath = path.resolve(__dirname, '../../src/api/static/js/pages/issues.js');
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

// ── mock 环境 ──
function makeSandbox(repoCache) {
  const store = {};
  if (repoCache !== undefined) {
    store['repo_member_cache'] = JSON.stringify(repoCache);
  }
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
  const calls = { adminConfirm: 0, confirmDone: 0 };
  const I18N = { t: (k) => k };
  const _renderTaskActionsInline = new Function(
    'I18N', '_isRepoAdmin393',
    'return function _renderTaskActionsInline(t) {' + renderActionsBody + '}')(
    I18N, null);
  const _isRepoAdmin393 = new Function(
    'localStorage', 'return function _isRepoAdmin393(repoKey) {' + isAdminBody + '}')(
    localStorage);
  return { localStorage, _isRepoAdmin393, _renderTaskActionsInline, calls, I18N };
}

// 把 _isRepoAdmin393 注入 _renderTaskActionsInline（源码里它是同文件函数，直接闭包引用）
function renderWith(cache, t) {
  const sb = makeSandbox(cache);
  const render = new Function(
    'I18N', '_isRepoAdmin393',
    'return function _renderTaskActionsInline(t) {' + renderActionsBody + '}')(
    sb.I18N, sb._isRepoAdmin393);
  return { html: render(t), sb };
}

const REPO_KEY = 'gitcode:org/repo393';
const adminCache = { repos: [{ repo_key: REPO_KEY, full_name: 'org/repo393', role: 'Admin', can_host: true }] };
const memberCache = { repos: [{ repo_key: REPO_KEY, full_name: 'org/repo393', role: 'Member', can_host: false }] };

// ── 1/5: admin 渲染可点按钮 ──
{
  const { html } = renderWith(adminCache, { id: 1, status: 'processing', repo_key: REPO_KEY });
  assert.ok(html.includes('adminConfirmTaskDone(1'), 'admin 应渲染可点的 adminConfirmTaskDone 按钮');
  assert.ok(!html.includes('disabled'), 'admin 按钮不应置灰');
  assert.ok(html.includes('tasks.admin_confirm_done'), '按钮文案用 tasks.admin_confirm_done');
}

// ── 5: 非 admin 置灰 + 提示 ──
{
  const { html } = renderWith(memberCache, { id: 2, status: 'processing', repo_key: REPO_KEY });
  assert.ok(html.includes('disabled'), '非 admin 按钮应置灰');
  assert.ok(html.includes('tasks.admin_only_hint'), '置灰按钮应带权限提示 title');
  assert.ok(!html.includes('adminConfirmTaskDone('), '非 admin 不应绑定 onclick');
}

// ── 6: completed / cancelled 不渲染 ──
{
  const { html: h1 } = renderWith(adminCache, { id: 3, status: 'completed', repo_key: REPO_KEY });
  assert.ok(!h1.includes('tasks.admin_confirm_done'), 'completed 不渲染 admin 入口');
  const { html: h2 } = renderWith(adminCache, { id: 4, status: 'cancelled', repo_key: REPO_KEY });
  assert.ok(!h2.includes('tasks.admin_confirm_done'), 'cancelled 不渲染 admin 入口');
}

// ── 2/3/4: _isRepoAdmin393 判定 ──
{
  const sb = makeSandbox(adminCache);
  assert.strictEqual(sb._isRepoAdmin393(REPO_KEY), true, 'Admin 角色 → true');
  // 前缀剥离匹配（前端 URL 无前缀 full_name）
  assert.strictEqual(sb._isRepoAdmin393('org/repo393'), true, '无前缀 repo_key 也应匹配');
}
{
  const sb = makeSandbox(memberCache);
  assert.strictEqual(sb._isRepoAdmin393(REPO_KEY), false, 'Member 角色 → false');
  assert.strictEqual(sb._isRepoAdmin393('gitcode:org/other'), false, '未匹配仓库 → false');
}
{
  const sb = makeSandbox(undefined); // 无缓存
  assert.strictEqual(sb._isRepoAdmin393(REPO_KEY), false, '无缓存 → false（fail-safe）');
}
{
  const sb = makeSandbox(null); // JSON 解析异常路径
  assert.strictEqual(sb._isRepoAdmin393(REPO_KEY), false, '缓存损坏 → false（fail-safe）');
}

// adminConfirmTaskDone 函数存在且 POST 正确端点
assert.ok(/window\.adminConfirmTaskDone\s*=\s*function/.test(src), '应定义 window.adminConfirmTaskDone');
assert.ok(src.includes("API.post('/issues/' + taskId + '/admin-confirm-done')"), '应 POST /issues/{id}/admin-confirm-done');

// i18n key（zh/en 均有）
const zh = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../src/api/static/locales/zh/tasks.json'), 'utf8'));
const en = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../src/api/static/locales/en/tasks.json'), 'utf8'));
for (const k of ['admin_confirm_done', 'admin_confirm_done_message', 'admin_only_hint']) {
  assert.ok(zh[k], 'zh 缺 key: ' + k);
  assert.ok(en[k], 'en 缺 key: ' + k);
}

console.log('✓ Issue #393 admin confirm entry: all assertions passed');
