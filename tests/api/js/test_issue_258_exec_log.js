/*
 * Issue #258: 定时任务详情页执行历史添加实时日志 — 前端测试
 * Issue #261: 适配 _apRenderLogBtn 新签名（agent_task_id 单数优先 + agent_task_ids 回退 + inline 参数）
 *
 * 运行：node tests/js/test_issue_258_exec_log.js
 *
 * 验证点（老任务 issue_number 关联路径 — agent_task_ids 回退）：
 *   1. agent_task_id 为空 + agent_task_ids 为空时显示 "—"（detail）/ 空（inline）
 *   2. agent_task_id 为空 + agent_task_ids 非空时显示"查看日志"按钮（回退到 agent_task_ids[0]）
 *   3. running/dispatched/pending/retrying 状态按钮文案含"执行中"
 *   4. success/failed 状态按钮文案为"查看日志"
 *   5. 按钮 onclick 调用 _apOpenTaskLog(agentTaskId, execId)
 *   6. _apOpenTaskLog 调用 openTaskLog(agentTaskId, title)
 *   7. 多个 agent_task_ids 时取第一个
 *   8. agent_task_id HTML 转义
 */

const assert = require('assert');

// ── 模拟 _esc（与 autopilot.js 一致）──
function _esc(s) {
  s = String(s || '');
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── 提取 _apRenderLogBtn 逻辑（与 autopilot.js 一致）──
function _apRenderLogBtn(execId, agentTaskId, agentTaskIds, status, inline) {
  var tid = agentTaskId || (agentTaskIds && agentTaskIds.length > 0 ? agentTaskIds[0] : '');
  if (!tid) return inline ? '' : '<span style="color:var(--t3)">—</span>';
  var isRunning = (status === 'running' || status === 'dispatched' || status === 'pending' || status === 'retrying');
  var label = isRunning ? '实时日志（执行中）' : '查看日志';
  var stopProp = inline ? 'event.stopPropagation();' : '';
  return '<a href="javascript:void(0)" onclick="' + stopProp + '_apOpenTaskLog(\'' + _esc(tid) + '\',' + execId + ')" style="cursor:pointer;color:var(--a);text-decoration:underline;white-space:nowrap;font-size:12px;flex-shrink:0">' + label + '</a>';
}

// ── 提取 _apOpenTaskLog 逻辑（与 autopilot.js 一致）──
var _lastOpenCall = null;
function openTaskLog(taskId, title) {
  _lastOpenCall = { taskId: taskId, title: title };
}
function _apOpenTaskLog(agentTaskId, execId) {
  if (typeof openTaskLog === 'function') {
    openTaskLog(agentTaskId, '定时任务执行 #' + execId);
  }
}

// ── 测试 ──

function testEmptyBoth() {
  var html = _apRenderLogBtn(1, '', [], 'success', false);
  assert.strictEqual(html, '<span style="color:var(--t3)">—</span>', 'empty both should show dash in detail');
  var htmlInline = _apRenderLogBtn(1, '', [], 'success', true);
  assert.strictEqual(htmlInline, '', 'empty both should return empty string inline');
  console.log('  PASS: empty both shows dash (detail) / empty (inline)');
}

function testNullBoth() {
  var html = _apRenderLogBtn(1, null, null, 'success', false);
  assert.strictEqual(html, '<span style="color:var(--t3)">—</span>', 'null both should show dash');
  console.log('  PASS: null both shows dash');
}

function testFallbackToAgentTaskIds() {
  var html = _apRenderLogBtn(42, '', ['task-fb'], 'success', false);
  assert.ok(html.indexOf('查看日志') > -1, 'fallback to agent_task_ids should show button');
  assert.ok(html.indexOf('task-fb') > -1, 'should use agent_task_ids[0]');
  console.log('  PASS: fallback to agent_task_ids[0] when agent_task_id empty');
}

function testSuccessStatusShowsViewLog() {
  var html = _apRenderLogBtn(42, 'task-abc', [], 'success', false);
  assert.ok(html.indexOf('查看日志') > -1, 'success status should show "查看日志"');
  assert.ok(html.indexOf('执行中') === -1, 'success status should not show "执行中"');
  console.log('  PASS: success status shows "查看日志"');
}

function testFailedStatusShowsViewLog() {
  var html = _apRenderLogBtn(42, 'task-abc', [], 'failed', false);
  assert.ok(html.indexOf('查看日志') > -1, 'failed status should show "查看日志"');
  console.log('  PASS: failed status shows "查看日志"');
}

function testRunningStatusShowsExecuting() {
  var html = _apRenderLogBtn(42, 'task-abc', [], 'running', false);
  assert.ok(html.indexOf('执行中') > -1, 'running status should show "执行中"');
  console.log('  PASS: running status shows "执行中"');
}

function testDispatchedStatusShowsExecuting() {
  var html = _apRenderLogBtn(42, 'task-abc', [], 'dispatched', false);
  assert.ok(html.indexOf('执行中') > -1, 'dispatched status should show "执行中"');
  console.log('  PASS: dispatched status shows "执行中"');
}

function testPendingStatusShowsExecuting() {
  var html = _apRenderLogBtn(42, 'task-abc', [], 'pending', false);
  assert.ok(html.indexOf('执行中') > -1, 'pending status should show "执行中"');
  console.log('  PASS: pending status shows "执行中"');
}

function testRetryingStatusShowsExecuting() {
  var html = _apRenderLogBtn(42, 'task-abc', [], 'retrying', false);
  assert.ok(html.indexOf('执行中') > -1, 'retrying status should show "执行中"');
  console.log('  PASS: retrying status shows "执行中"');
}

function testOncallContainsApOpenTaskLog() {
  var html = _apRenderLogBtn(42, 'task-abc', [], 'success', false);
  assert.ok(html.indexOf('_apOpenTaskLog') > -1, 'button onclick should call _apOpenTaskLog');
  assert.ok(html.indexOf('task-abc') > -1, 'button should contain agentTaskId');
  assert.ok(html.indexOf('42') > -1, 'button should contain execId');
  console.log('  PASS: button onclick calls _apOpenTaskLog(agentTaskId, execId)');
}

function testApOpenTaskLogCallsOpenTaskLog() {
  _lastOpenCall = null;
  _apOpenTaskLog('task-xyz', 99);
  assert.ok(_lastOpenCall !== null, '_apOpenTaskLog should call openTaskLog');
  assert.strictEqual(_lastOpenCall.taskId, 'task-xyz', 'openTaskLog should receive agentTaskId');
  assert.strictEqual(_lastOpenCall.title, '定时任务执行 #99', 'openTaskLog should receive title with execId');
  console.log('  PASS: _apOpenTaskLog calls openTaskLog with correct args');
}

function testMultipleAgentTaskIdsTakesFirst() {
  var html = _apRenderLogBtn(10, '', ['task-first', 'task-second', 'task-third'], 'success', false);
  assert.ok(html.indexOf('task-first') > -1, 'should use first agent_task_id');
  assert.ok(html.indexOf('task-second') === -1, 'should not contain second agent_task_id');
  console.log('  PASS: multiple agent_task_ids takes first');
}

function testAgentTaskIdEscaped() {
  var html = _apRenderLogBtn(10, 'task<"evil">', [], 'success', false);
  assert.ok(html.indexOf('task<"evil">') === -1, 'agent_task_id should be HTML-escaped');
  assert.ok(html.indexOf('task&lt;') > -1, 'escaped agent_task_id should be in output');
  console.log('  PASS: agent_task_id is HTML-escaped');
}

function testInlineHasStopPropagation() {
  var html = _apRenderLogBtn(42, 'task-abc', [], 'running', true);
  assert.ok(html.indexOf('event.stopPropagation()') > -1, 'inline button should have stopPropagation');
  var htmlDetail = _apRenderLogBtn(42, 'task-abc', [], 'running', false);
  assert.ok(htmlDetail.indexOf('event.stopPropagation()') === -1, 'detail button should not have stopPropagation');
  console.log('  PASS: inline button has stopPropagation, detail does not');
}

// ── 运行所有测试 ──
console.log('Issue #258/#261: Autopilot Exec Log Button Tests\n');

let passed = 0, failed = 0;
const tests = [
  testEmptyBoth,
  testNullBoth,
  testFallbackToAgentTaskIds,
  testSuccessStatusShowsViewLog,
  testFailedStatusShowsViewLog,
  testRunningStatusShowsExecuting,
  testDispatchedStatusShowsExecuting,
  testPendingStatusShowsExecuting,
  testRetryingStatusShowsExecuting,
  testOncallContainsApOpenTaskLog,
  testApOpenTaskLogCallsOpenTaskLog,
  testMultipleAgentTaskIdsTakesFirst,
  testAgentTaskIdEscaped,
  testInlineHasStopPropagation,
];

for (const t of tests) {
  try {
    t();
    passed++;
  } catch (e) {
    console.log('  FAIL:', e.message);
    failed++;
  }
}

console.log('\n' + passed + ' passed, ' + failed + ' failed');
if (failed > 0) process.exit(1);
