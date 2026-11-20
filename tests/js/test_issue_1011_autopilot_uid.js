// Issue #1011 前端源断言（autopilot uid 消费收尾）：静态扫描 AutopilotView.vue 关键改动点。
// 2026-11-13 [tree removal] 原断言目标 src/api/static/js/pages/autopilot.js 已随死树清理删除；
// uid 消费面移植到 Vue shell（AutopilotView.vue）后本用例恢复，断言改到 Vue 源。
// 沿用 node 源断言模式（无 DOM，不执行逻辑）。
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../../src/api/frontend/app/src/views/AutopilotView.vue'), 'utf8');

let failures = [];
let count = 0;
function assertContains(name, needle) {
  count++;
  if (src.indexOf(needle) === -1) failures.push(`missing: ${name} -> ${needle}`);
}
function section(start, end) {
  const i = src.indexOf(start);
  const j = src.indexOf(end, i + start.length);
  return i >= 0 && j >= 0 ? src.slice(i, j) : '';
}

// 1. 读侧：uids 优先回落 ids（编辑加载）
assertContains('edit-load uids-first', 't.engine_runtime_uids && t.engine_runtime_uids.length');
assertContains('uids fed as string keys', 'engineKeys.forEach((k) => selectedEngines.add(String(k)))');
// 详情展示 uid 优先计数
assertContains('detail count uid-first', 't.engine_runtime_uids && t.engine_runtime_uids.length');

// 2. 写侧升格：全部 uid 可解析时提交 engine_runtime_uids + 清空 ids
assertContains('submit engine_runtime_uids', 'body.engine_runtime_uids = uidSel');
assertContains('upgrade clears ids', 'body.engine_runtime_ids = []');
assertContains('legacy fallback ids', 'body.engine_runtime_ids = idSel');
// 姊妹字段不同时非空（回落分支内 uids 恒为空数组）
assertContains('fallback clears uids', 'body.engine_runtime_uids = []');

// 3. 选择键 uid 优先（勾选/切换/超范围匹配双轨）
assertContains('selection key uid-first', "String((e && e.engine_uid) || (e && e.id) || '')");
assertContains('detached dual-track match', "String(e.id) === k || String(e.engine_uid || '') === k");

if (failures.length) {
  console.error('FAIL tests/js/test_issue_1011_autopilot_uid.js');
  failures.forEach(f => console.error('  ' + f));
  process.exit(1);
}
console.log(`PASS tests/js/test_issue_1011_autopilot_uid.js (${count} assertions)`);
