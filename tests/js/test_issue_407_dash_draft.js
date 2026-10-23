/*
 * Issue #407: 页签闲置返回整页重载导致编辑内容丢失 — 草稿持久化/编辑模式恢复回归测试
 *
 * 运行：node tests/js/test_issue_407_dash_draft.js
 *
 * 覆盖（QA 审查 R1/R2/Optional + 守卫条件）：
 *   A. 守卫条件
 *     1. app.js visibilitychange 处理器编辑态守卫（静态断言）
 *     2. _getDashEditMode 1 小时过期，过期即清除返回 null
 *   B. 草稿存/取/清
 *     3. _saveDashDraft 写入带 ts；_restoreDashDraft 命中返回对象
 *     4. _restoreDashDraft 无草稿返回 null；JSON 损坏返回 null 不抛错
 *     5. _clearDashDraft 清除后 _restoreDashDraft 返回 null
 *     6. 草稿键含 owner/name/mode/sceneId（页签级隔离，同仓两场景互不串）
 *   C. 过期降级
 *     7. 草稿 ts 超 1 小时 → _restoreDashDraft 返回 null 并清除键（Optional 修复）
 *     8. 草稿无 ts（老格式）→ 不按过期处理，正常返回
 *   D. 引擎选择保存草稿（R1 修复，静态断言）
 *     9. _openEngineModalImpl 声明 saveDraftCb 参数；confirm 回调内调用
 *    10. 编辑/创建两处 openEngineModal 均传入对应 _saveEditDraft/_saveCreateDraft
 *   E. 零场景分支恢复检查（R2 修复，静态断言）
 *    11. renderDashboard 空场景分支 return 前有 _autoEditEmpty 恢复检查
 *   F. 兼容性
 *    12. dashEditMode 保存时带 ts（静态断言）
 *    13. dashboard.js / app.js 无语法错误（node 解析）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const DASHBOARD_JS = path.join(ROOT, 'src/api/static/js/pages/dashboard.js');
const APP_JS = path.join(ROOT, 'src/api/static/js/app.js');

// ── 提取 dashboard.js 顶部的草稿工具函数（源码切片求值，不依赖浏览器）──
function extractDraftUtils(sessionStorageMock) {
  const src = fs.readFileSync(DASHBOARD_JS, 'utf8');
  const START = 'function _dashDraftKey';
  const END = '\n// ── 主入口：渲染场景中心';
  const i0 = src.indexOf(START);
  const i1 = src.indexOf(END, i0);
  assert.ok(i0 >= 0 && i1 > i0, 'dashboard.js 应包含草稿工具函数区块');
  const slice = src.slice(i0, i1);
  const fn = new Function('sessionStorage', 'Date', 'JSON', slice + '\nreturn { _dashDraftKey: _dashDraftKey, _saveDashDraft: _saveDashDraft, _restoreDashDraft: _restoreDashDraft, _clearDashDraft: _clearDashDraft, _saveDashEditMode: _saveDashEditMode, _getDashEditMode: _getDashEditMode, _clearDashEditMode: _clearDashEditMode };');
  return fn(sessionStorageMock, Date, JSON);
}

function makeSessionStorage() {
  return {
    _s: {},
    getItem(k) { return (k in this._s) ? this._s[k] : null; },
    setItem(k, v) { this._s[k] = String(v); },
    removeItem(k) { delete this._s[k]; },
    clear() { this._s = {}; },
  };
}

// 可注入「当前时间」的工具提取（过期语义测试用）
function extractDraftUtilsWithDate(sessionStorageMock, now) {
  const src = fs.readFileSync(DASHBOARD_JS, 'utf8');
  const START = 'function _dashDraftKey';
  const END = '\n// ── 主入口：渲染场景中心';
  const i0 = src.indexOf(START);
  const i1 = src.indexOf(END, i0);
  assert.ok(i0 >= 0 && i1 > i0, 'dashboard.js 应包含草稿工具函数区块');
  const slice = src.slice(i0, i1);
  const FakeDate = makeFakeDate(now, Date);
  const fn = new Function('sessionStorage', 'Date', 'JSON', slice + '\nreturn { _dashDraftKey: _dashDraftKey, _saveDashDraft: _saveDashDraft, _restoreDashDraft: _restoreDashDraft, _clearDashDraft: _clearDashDraft, _saveDashEditMode: _saveDashEditMode, _getDashEditMode: _getDashEditMode, _clearDashEditMode: _clearDashEditMode };');
  return fn(sessionStorageMock, FakeDate, JSON);
}

// ── 时间控制：可注入当前时间的 Date 替身 ──
function makeFakeDate(now, realDateClass) {
  function FakeDate(...args) {
    if (args.length === 0) return new realDateClass(now);
    return new realDateClass(...args);
  }
  FakeDate.now = () => now;
  FakeDate.parse = realDateClass.parse;
  FakeDate.UTC = realDateClass.UTC;
  return FakeDate;
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch (e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

function readDashboardSrc() { return fs.readFileSync(DASHBOARD_JS, 'utf8'); }
function readAppSrc() { return fs.readFileSync(APP_JS, 'utf8'); }

console.log('Issue #407: 编辑草稿持久化 / 编辑模式恢复 — 回归测试\n');

// ═══ A. 守卫条件 ═══

test('A1. app.js visibilitychange: 隐藏>60s 且非编辑态才重渲染（_dashboardEditing 守卫保留）', () => {
  const src = readAppSrc();
  assert.ok(
    /Date\.now\(\)\s*-\s*_hiddenSince\s*>\s*60000\s*&&\s*!\(window\._dashboardEditing\s*\|\|\s*window\._chatEditing\)/.test(src),
    'visibilitychange 重渲染条件应保留 _dashboardEditing 守卫（与 _chatEditing 合并，dashboard 不回归）'
  );
  assert.ok(src.includes('window._dashboardEditing'), '_dashboardEditing 守卫保留');
});

test('A2. _getDashEditMode: 1 小时内返回保存对象', () => {
  const ss = makeSessionStorage();
  const now = Date.now();
  const u = extractDraftUtilsWithDate(ss, now);
  u._saveDashEditMode('create');
  const got = u._getDashEditMode();
  assert.ok(got && got.mode === 'create', '1 小时内应返回 dashEditMode 对象');
  assert.strictEqual(got.sceneId, null, 'create 模式 sceneId 应为 null');
  assert.strictEqual(ss.getItem('dashEditMode') !== null, true, '读取不应清除未过期条目');
});

test('A3. _getDashEditMode: 超 1 小时过期 → 返回 null 并清除键', () => {
  const ss = makeSessionStorage();
  const now = Date.now();
  const u = extractDraftUtilsWithDate(ss, now);
  u._saveDashEditMode('edit', 'scn-1');
  // 推进 1 小时 + 1 秒
  const uLater = extractDraftUtilsWithDate(ss, now + 3600000 + 1000);
  const got = uLater._getDashEditMode();
  assert.strictEqual(got, null, '超 1 小时的 dashEditMode 应返回 null');
  assert.strictEqual(ss.getItem('dashEditMode'), null, '过期后 dashEditMode 键应被清除');
});

// ═══ B. 草稿存/取/清 ═══

test('B1. _saveDashDraft 写入带 ts；_restoreDashDraft 命中返回对象', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss);
  const before = Date.now();
  u._saveDashDraft('o', 'r', 'edit', 'scn-1', { nodes: [{ id: 'coding', enabled: true, engine_runtime_ids: [1, 2] }] });
  const raw = JSON.parse(ss.getItem('dashDraft_o/r_edit_scn-1'));
  assert.ok(raw.ts >= before, '草稿对象应带 ts（保存时注入）');
  const got = u._restoreDashDraft('o', 'r', 'edit', 'scn-1');
  assert.ok(got && Array.isArray(got.nodes) && got.nodes[0].engine_runtime_ids.length === 2,
    '恢复应返回完整草稿对象（含引擎选择）');
});

test('B2. _restoreDashDraft: 无草稿返回 null；JSON 损坏返回 null 不抛错', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss);
  assert.strictEqual(u._restoreDashDraft('o', 'r', 'edit', 'scn-x'), null, '无草稿应返回 null');
  ss.setItem('dashDraft_o/r_edit_scn-bad', '{not-json');
  let got = null;
  assert.doesNotThrow(() => { got = u._restoreDashDraft('o', 'r', 'edit', 'scn-bad'); },
    'JSON 损坏不应抛错（try/catch 降级）');
  assert.strictEqual(got, null, 'JSON 损坏应返回 null');
});

test('B3. _clearDashDraft: 清除后恢复返回 null', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss);
  u._saveDashDraft('o', 'r', 'create', null, { sceneName: 'n', sceneDesc: 'd' });
  assert.ok(u._restoreDashDraft('o', 'r', 'create', null), '清除前应能恢复');
  u._clearDashDraft('o', 'r', 'create', null);
  assert.strictEqual(u._restoreDashDraft('o', 'r', 'create', null), null, '清除后应返回 null');
});

test('B4. 草稿键含 owner/name/mode/sceneId — 页签级隔离', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss);
  u._saveDashDraft('o1', 'r1', 'edit', 'scn-a', { nodes: [] });
  u._saveDashDraft('o1', 'r1', 'edit', 'scn-b', { nodes: [{ id: 'qa' }] });
  u._saveDashDraft('o2', 'r2', 'create', null, { sceneName: 'x' });
  assert.strictEqual(u._dashDraftKey('o1', 'r1', 'edit', 'scn-a'), 'dashDraft_o1/r1_edit_scn-a');
  assert.strictEqual(u._dashDraftKey('o1', 'r1', 'create', ''), 'dashDraft_o1/r1_create', '创建模式（无 sceneId）键不带后缀');
  const a = u._restoreDashDraft('o1', 'r1', 'edit', 'scn-a');
  const b = u._restoreDashDraft('o1', 'r1', 'edit', 'scn-b');
  assert.ok(a && b && a !== b, '两场景草稿互不串扰');
  assert.strictEqual(a.nodes.length, 0, 'scn-a 草稿内容正确');
  assert.strictEqual(b.nodes[0].id, 'qa', 'scn-b 草稿内容正确');
});

// ═══ C. 过期降级（Optional 修复） ═══

test('C1. 草稿 ts 超 1 小时 → _restoreDashDraft 返回 null 并清除键', () => {
  const ss = makeSessionStorage();
  const now = Date.now();
  const u = extractDraftUtilsWithDate(ss, now);
  u._saveDashDraft('o', 'r', 'edit', 'scn-1', { nodes: [{ id: 'coding' }] });
  // 推进 1 小时 + 1 秒后再恢复
  const uLater = extractDraftUtilsWithDate(ss, now + 3600000 + 1000);
  const got = uLater._restoreDashDraft('o', 'r', 'edit', 'scn-1');
  assert.strictEqual(got, null, '过期草稿不应回填（返回 null）');
  assert.strictEqual(ss.getItem('dashDraft_o/r_edit_scn-1'), null, '过期草稿键应被清除（不残留）');
});

test('C2. 草稿 ts 未超 1 小时 → 正常返回', () => {
  const ss = makeSessionStorage();
  const now = Date.now();
  const u = extractDraftUtilsWithDate(ss, now);
  u._saveDashDraft('o', 'r', 'edit', 'scn-1', { nodes: [{ id: 'coding' }] });
  const uSoon = extractDraftUtilsWithDate(ss, now + 3599999);
  const got = uSoon._restoreDashDraft('o', 'r', 'edit', 'scn-1');
  assert.ok(got && got.nodes.length === 1, '1 小时内（含边界 3599999ms）草稿应正常返回');
});

test('C3. 老格式草稿（无 ts）→ 不按过期处理，正常返回', () => {
  const ss = makeSessionStorage();
  ss.setItem('dashDraft_o/r_edit_scn-legacy', JSON.stringify({ nodes: [{ id: 'coding', enabled: true }] }));
  const u = extractDraftUtils(ss);
  const got = u._restoreDashDraft('o', 'r', 'edit', 'scn-legacy');
  assert.ok(got && got.nodes.length === 1, '无 ts 的老格式草稿应正常返回（不误清）');
});

// ═══ D. 引擎选择保存草稿（R1 修复，静态断言） ═══

test('D1. _openEngineModalImpl 声明 saveDraftCb 参数（第 8 参）', () => {
  const src = readDashboardSrc();
  assert.ok(
    /function _openEngineModalImpl\(agentType,\s*editNodes,\s*agents,\s*agentEngines,\s*owner,\s*name,\s*sceneId,\s*saveDraftCb\)/.test(src),
    '_openEngineModalImpl 应声明 saveDraftCb 参数'
  );
});

test('D2. confirmEngineBtn2 确认回调内调用 saveDraftCb（try/catch 防御）', () => {
  const src = readDashboardSrc();
  assert.ok(
    /if\s*\(typeof saveDraftCb === 'function'\)\s*\{\s*try\s*\{\s*saveDraftCb\(\);.*?\}\s*catch\(e\)\s*\{\s*\}\s*\}/s.test(src),
    '引擎确认回调重渲染卡片后应调用 saveDraftCb（R1 修复）'
  );
});

test('D3. 编辑模式 openEngineModal 传入 _saveEditDraft', () => {
  const src = readDashboardSrc();
  const m = src.match(/_openEngineModalImpl\(agentType,\s*editNodes,\s*agents,\s*agentEngines,\s*owner,\s*name,\s*sceneId,\s*_saveEditDraft\)/g) || [];
  assert.strictEqual(m.length, 1, '编辑模式 openEngineModal 应传 _saveEditDraft，实际 ' + m.length + ' 处');
});

test('D4. 创建模式 openEngineModal 传入 _saveCreateDraft', () => {
  const src = readDashboardSrc();
  const m = src.match(/_openEngineModalImpl\(agentType,\s*editNodes,\s*agents,\s*agentEngines,\s*owner,\s*name,\s*'',\s*_saveCreateDraft\)/g) || [];
  assert.strictEqual(m.length, 1, '创建模式 openEngineModal 应传 _saveCreateDraft，实际 ' + m.length + ' 处');
});

// ═══ E. 零场景分支恢复检查（R2 修复，静态断言） ═══

test('E1. renderDashboard 零场景分支 return 前有恢复检查（_autoEditEmpty）', () => {
  const src = readDashboardSrc();
  assert.ok(
    /var _autoEditEmpty = _getDashEditMode\(\);\s*if \(_autoEditEmpty && _autoEditEmpty\.mode === 'create' && owner && name\) \{\s*_clearDashEditMode\(\);\s*if \(typeof enterCreateMode === 'function'\) enterCreateMode\(\);\s*\}\s*return;/.test(src),
    '零场景分支 return 前应执行 _autoEditEmpty 恢复检查（R2 修复）'
  );
});

test('E2. 有场景渲染路径保留 _autoEdit 恢复检查（不回归）', () => {
  const src = readDashboardSrc();
  assert.ok(
    /var _autoEdit = _getDashEditMode\(\);\s*if \(_autoEdit && _autoEdit\.mode === 'create' && owner && name\) \{\s*_clearDashEditMode\(\);\s*if \(typeof enterCreateMode === 'function'\) enterCreateMode\(\);\s*\}/.test(src),
    '_renderSceneCards 渲染路径的 _autoEdit 恢复检查应保留'
  );
});

// ═══ F. 兼容性 ═══

test('F1. dashEditMode 保存时带 ts（静态断言）', () => {
  const src = readDashboardSrc();
  assert.ok(
    /sessionStorage\.setItem\('dashEditMode',\s*JSON\.stringify\(\{mode: mode,\s*sceneId: sceneId \|\| null,\s*ts: Date\.now\(\)\}\)\)/.test(src),
    '_saveDashEditMode 应保存 ts 字段'
  );
});

test('F2. dashboard.js 无语法错误（node Function 解析）', () => {
  new Function(readDashboardSrc());
});

test('F3. app.js 无语法错误（node Function 解析）', () => {
  new Function(readAppSrc());
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
