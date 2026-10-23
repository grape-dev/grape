/**
 * Issue #337 — 需求中心列表模式「移交到其他流水线」前端回归测试。
 *
 * 运行：node tests/js/test_issue_337_list_handoff.js
 *
 * 验证点（对应 Issue #337 验收标准）：
 *   1. 列表模式详情操作区（_render L~992）包含 _renderCrossPipelineButton(_taskSel) 入口。
 *   2. 看板模式入口保留（_openBoardDetail）。
 *   3. 弹窗目标列表调用后端代理接口 GET /issues/{id}/handoff/targets（不再前端过滤）。
 *   4. 提交调用后端代理端点 POST /issues/{id}/handoff（不再直调 /a2a）。
 *   5. 提交前有 _grapeConfirm 二次确认（展示目标流水线名称）。
 *   6. 空/失败目标列表有明确提示（no_target / no_permission 词条），不静默失败。
 *   7. locales zh/en 的 cross_pipeline 新词条存在。
 *   8. index.html issues.js 版本号已提升。
 *
 * 策略：源码级断言（与 tests/js 现行范式一致）。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const issuesJsPath = path.join(ROOT, 'src/api/static/js/pages/issues.js');
const src = fs.readFileSync(issuesJsPath, 'utf8');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('Issue #337: 需求中心列表模式「移交到其他流水线」入口 — 回归测试\n');

// ── 提取函数体的辅助 ──
function fnBody(marker) {
  const idx = src.indexOf(marker);
  if (idx < 0) throw new Error(`未找到 ${marker}`);
  return src.substring(idx, idx + 5000);
}

// ─── 测试 1：列表模式详情操作区包含移交入口 ───
test('列表模式详情操作区包含 _renderCrossPipelineButton(_taskSel) 入口', () => {
  // _render 的列表模式分支：detailHtml 后的操作按钮行应以 _renderCrossPipelineButton(_taskSel) 结尾
  const listRowRe = /\$\{_renderTaskActionsInline\(_taskSel\)\}\$\{_renderCrossPipelineButton\(_taskSel\)\}/;
  assert.ok(listRowRe.test(src), '列表模式操作行应包含 ${_renderCrossPipelineButton(_taskSel)}');
});

// ─── 测试 2：看板模式入口保留 ───
test('看板模式 _openBoardDetail 入口保留', () => {
  const boardRe = /\$\{_renderCrossPipelineButton\(t\)\}/;
  assert.ok(boardRe.test(src), '_openBoardDetail 应保留 ${_renderCrossPipelineButton(t)}');
});

// ─── 测试 3：弹窗目标列表走后端代理接口 ───
test('弹窗目标列表调用 GET /issues/{id}/handoff/targets', () => {
  const body = fnBody('window.openCrossPipelineModal');
  assert.ok(body.indexOf("API.get('/issues/' + t.id + '/handoff/targets')") >= 0,
    '应调用后端代理接口 /issues/{id}/handoff/targets');
  // 不再由前端拿全量场景列表自行过滤
  assert.ok(body.indexOf('/scenes/with-pipeline') === -1,
    '弹窗不应再调用 /scenes/with-pipeline（改由后端过滤）');
});

// ─── 测试 4：空列表 / 失败有明确提示 ───
test('空列表/加载失败有明确提示（no_target / no_permission）', () => {
  const body = fnBody('window.openCrossPipelineModal');
  assert.ok(body.indexOf('no_target') >= 0, '空列表应使用 no_target 词条');
  assert.ok(body.indexOf('no_permission') >= 0, '加载失败应使用 no_permission 词条');
  assert.ok(body.indexOf('catch(function(e)') >= 0, '应有 catch 分支（不静默失败）');
});

// ─── 测试 5：提交走后端代理端点 ───
test('提交调用 POST /issues/{id}/handoff（后端代理）', () => {
  const body = fnBody('function _doCrossPipelineSubmit');
  assert.ok(body.indexOf("API.post('/issues/' + t.id + '/handoff'") >= 0,
    '提交应调用 /issues/{id}/handoff');
  const submitBody = fnBody('window.crossPipelineSubmit');
  assert.ok(submitBody.indexOf("API.post('/a2a'") === -1,
    '提交不应再直调 /a2a');
});

// ─── 测试 6：二次确认防误触 ───
test('提交前有 _grapeConfirm 二次确认（展示目标流水线名称）', () => {
  const body = fnBody('window.crossPipelineSubmit');
  assert.ok(body.indexOf('_grapeConfirm') >= 0, '应调用 _grapeConfirm 二次确认');
  assert.ok(body.indexOf('confirm_message') >= 0, '确认文案应使用 confirm_message 词条');
  assert.ok(body.indexOf("replace('{scene}', _sceneName)") >= 0, '确认文案应展示目标流水线名称');
});

// ─── 测试 7：成功后刷新列表状态 ───
test('成功后刷新 pipeline 段与列表（loadTasks）', () => {
  const body = fnBody('function _doCrossPipelineSubmit');
  assert.ok(body.indexOf('loadTasks()') >= 0, '成功后应调用 loadTasks() 刷新列表状态');
});

// ─── 测试 8：locales zh/en 新词条存在 ───
test('locales zh/en cross_pipeline 新词条存在', () => {
  const zh = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/issues.json'), 'utf8'));
  const en = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/issues.json'), 'utf8'));
  for (const k of ['confirm_title', 'confirm_message', 'no_permission', 'no_target']) {
    assert.ok(zh.cross_pipeline && zh.cross_pipeline[k], `zh cross_pipeline.${k} 应存在`);
    assert.ok(en.cross_pipeline && en.cross_pipeline[k], `en cross_pipeline.${k} 应存在`);
  }
  assert.strictEqual(zh.cross_pipeline.confirm_title, '确认移交');
});

// ─── 测试 9：index.html 版本号已提升 ───
test('index.html issues.js 版本号已提升（缓存刷新）', () => {
  const html = fs.readFileSync(path.join(ROOT, 'src/api/static/index.html'), 'utf8');
  assert.ok(html.indexOf('issues.js?v=122') >= 0, 'issues.js 版本号应为 v=114');
});

// ─── 测试 10：语法完整性 ───
test('issues.js 语法完整（vm.Script 解析通过）', () => {
  const vm = require('vm');
  new vm.Script(src, { filename: 'issues.js' });
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
