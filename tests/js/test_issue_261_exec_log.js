/*
 * Issue #261: 新版单 agent 定时任务执行历史补实时日志 — 前端测试
 *
 * 运行：node tests/js/test_issue_261_exec_log.js
 *
 * 验证点（新任务单 agent 路径 — agent_task_id 单数优先）：
 *   1. agent_task_id 非空时显示按钮（即使 agent_task_ids 为空）
 *   2. agent_task_id 优先于 agent_task_ids[0]
 *   3. inline=true 时返回含 stopPropagation 的链接
 *   4. inline=false 时返回不含 stopPropagation 的链接
 *   5. inline=true + 两者均空时返回空字符串（行内不占位）
 *   6. inline=false + 两者均空时返回 "—"（detail 占位）
 *   7. agent_task_id HTML 转义（防 XSS）
 *   8. onclick 引号逃逸（agent_task_id 含单引号不破坏 onclick）
 *   9. _apOpenTaskLog 调用 openTaskLog(agentTaskId, i18n 标题 '自动化 #execId')（Issue #274）
 * 10. 所有运行状态（running/dispatched/pending/retrying）显示"执行中"
 * 11. 终态（success/failed/skipped）显示"查看日志"
 * 12. Issue #274: 按钮文案/弹窗标题走 i18n 语言包（zh/en 均无硬编码中文）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

// ── 模拟 I18N（读取真实语言包 locales/{zh,en}/autopilot.json）──
const LOCALES_DIR = path.join(__dirname, '..', '..', 'src', 'api', 'static', 'locales');
const PACKS = {
  zh: JSON.parse(fs.readFileSync(path.join(LOCALES_DIR, 'zh', 'autopilot.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(LOCALES_DIR, 'en', 'autopilot.json'), 'utf8')),
};
let _locale = 'zh';
function _t(key) {
  const parts = key.split('.');
  let v = PACKS[_locale];
  for (const p of parts) v = (v && typeof v === 'object') ? v[p] : undefined;
  return v;
}
function _apt(key, vars) {
  let s = String(_t(key) !== undefined ? _t(key) : '');
  if (vars) Object.keys(vars).forEach(function(k) { s = s.replace(new RegExp('{{' + k + '}}', 'g'), String(vars[k])); });
  return s;
}

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
  var label = isRunning ? _apt('exec.log_btn_running') : _apt('exec.log_btn');
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
    openTaskLog(agentTaskId, _apt('exec.log_title', {id: execId}));
  }
}

// ── 测试 ──

function testSingularShowsButton() {
  var html = _apRenderLogBtn(5, 't_abc123', [], 'success', false);
  assert.ok(html.indexOf('日志') > -1, 'singular agent_task_id should show button');
  assert.ok(html.indexOf('t_abc123') > -1, 'button should contain singular agent_task_id');
  console.log('  PASS: singular agent_task_id shows button even when agent_task_ids empty');
}

function testSingularPriorityOverPlural() {
  var html = _apRenderLogBtn(5, 't_primary', ['t_secondary'], 'success', false);
  assert.ok(html.indexOf('t_primary') > -1, 'should use singular agent_task_id');
  assert.ok(html.indexOf('t_secondary') === -1, 'should not use agent_task_ids[0]');
  console.log('  PASS: singular agent_task_id takes priority over agent_task_ids[0]');
}

function testInlineHasStopPropagation() {
  var html = _apRenderLogBtn(5, 't_abc', [], 'running', true);
  assert.ok(html.indexOf('event.stopPropagation()') > -1, 'inline should have stopPropagation');
  console.log('  PASS: inline=true has event.stopPropagation()');
}

function testDetailNoStopPropagation() {
  var html = _apRenderLogBtn(5, 't_abc', [], 'running', false);
  assert.ok(html.indexOf('event.stopPropagation()') === -1, 'detail should not have stopPropagation');
  console.log('  PASS: inline=false has no event.stopPropagation()');
}

function testInlineEmptyReturnsEmptyString() {
  var html = _apRenderLogBtn(5, '', [], 'success', true);
  assert.strictEqual(html, '', 'inline empty should return empty string');
  console.log('  PASS: inline=true + both empty returns empty string');
}

function testDetailEmptyReturnsDash() {
  var html = _apRenderLogBtn(5, '', [], 'success', false);
  assert.ok(html.indexOf('—') > -1, 'detail empty should show dash');
  console.log('  PASS: inline=false + both empty returns dash');
}

function testHtmlEscape() {
  var html = _apRenderLogBtn(5, 't_<script>alert(1)</script>', [], 'success', false);
  assert.ok(html.indexOf('<script>') === -1, 'should not contain raw script tag');
  assert.ok(html.indexOf('&lt;script&gt;') > -1, 'should contain escaped script tag');
  console.log('  PASS: agent_task_id HTML-escaped (XSS prevention)');
}

function testQuoteEscapeInOnclick() {
  var evil = "t_'break";
  var html = _apRenderLogBtn(5, evil, [], 'success', false);
  assert.ok(html.indexOf(evil) === -1, 'should not contain raw single quote');
  assert.ok(html.indexOf('&#39;') > -1, 'single quote should be escaped');
  console.log('  PASS: agent_task_id single quote escaped in onclick');
}

function testOpenTaskLogCall() {
  _lastOpenCall = null;
  _apOpenTaskLog('t_xyz', 77);
  assert.ok(_lastOpenCall !== null, 'should call openTaskLog');
  assert.strictEqual(_lastOpenCall.taskId, 't_xyz');
  assert.strictEqual(_lastOpenCall.title, '自动化 #77');
  console.log('  PASS: _apOpenTaskLog calls openTaskLog with correct args (zh)');
}

function testI18nEnLocale() {
  _locale = 'en';
  try {
    var htmlRun = _apRenderLogBtn(5, 't_abc', [], 'running', false);
    assert.ok(htmlRun.indexOf('Live log (running)') > -1, 'en running should show localized label');
    assert.ok(htmlRun.indexOf('执行中') === -1, 'en running must not contain Chinese');
    var htmlDone = _apRenderLogBtn(5, 't_abc', [], 'success', false);
    assert.ok(htmlDone.indexOf('Logs') > -1, 'en terminal should show localized label');
    _lastOpenCall = null;
    _apOpenTaskLog('t_abc', 9);
    assert.strictEqual(_lastOpenCall.title, 'Automation #9', 'en dialog title should be localized');
    assert.ok(PACKS.en.exec.log_btn_running && PACKS.zh.exec.log_btn_running, 'log_btn_running key exists in both locales');
  } finally {
    _locale = 'zh';
  }
  console.log('  PASS: Issue #274 log button/title localized in en (no hardcoded Chinese)');
}

function testRunningStatuses() {
  ['running', 'dispatched', 'pending', 'retrying'].forEach(function(st) {
    var html = _apRenderLogBtn(5, 't_abc', [], st, false);
    assert.ok(html.indexOf('执行中') > -1, st + ' should show "执行中"');
  });
  console.log('  PASS: all running statuses show "执行中"');
}

function testTerminalStatuses() {
  ['success', 'failed', 'skipped'].forEach(function(st) {
    var html = _apRenderLogBtn(5, 't_abc', [], st, false);
    assert.ok(html.indexOf('日志') > -1, st + ' should show "查看日志"');
  });
  console.log('  PASS: terminal statuses show "日志"');
}

function testExecIdInOutput() {
  var html = _apRenderLogBtn(123, 't_abc', [], 'success', false);
  assert.ok(html.indexOf('123') > -1, 'execId should be in onclick');
  console.log('  PASS: execId present in onclick');
}

// ── 运行所有测试 ──
console.log('Issue #261: Autopilot Singular agent_task_id Log Button Tests\n');

let passed = 0, failed = 0;
const tests = [
  testSingularShowsButton,
  testSingularPriorityOverPlural,
  testInlineHasStopPropagation,
  testDetailNoStopPropagation,
  testInlineEmptyReturnsEmptyString,
  testDetailEmptyReturnsDash,
  testHtmlEscape,
  testQuoteEscapeInOnclick,
  testOpenTaskLogCall,
  testRunningStatuses,
  testTerminalStatuses,
  testExecIdInOutput,
  testI18nEnLocale,
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
