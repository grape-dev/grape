/*
 * Issue #1477 — API Key 时间显示（本地时区、人类可读）
 *
 * 2026-11-11 修复：后端 list_api_keys 返回 normalize_event_time() = UTC ISO（带 +00:00），
 * AccountView 原来直接原样渲染，用户会看到 2026-11-08T07:33:07+00:00。本次改为本地格式化。
 *   1. AccountView 用 fmtKeyTime 渲染 created_at / last_used_at
 *   2. fmtKeyTime 走 G.fmtLocalTime（本地时区），空值给 —
 *   3. 后端仍以 normalize_event_time 归一（UTC ISO），前端负责本地化（职责边界）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/AccountView.vue'), 'utf8');
const BACKEND = fs.readFileSync(path.join(ROOT, 'src/domains/mgmt/routes/settings.py'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('渲染走 fmtKeyTime（不再原样输出）', function () {
  assert.ok(/t\('created'\) \}\}: \{\{ fmtKeyTime\(k\.created_at\) \}\}/.test(VIEW), 'created_at 应走 fmtKeyTime');
  assert.ok(/fmtKeyTime\(k\.last_used_at\)/.test(VIEW), 'last_used_at 应走 fmtKeyTime');
  assert.ok(!/\{\{ k\.created_at \|\| '—' \}\}/.test(VIEW), '不应再原样渲染 created_at');
});

check('fmtKeyTime 本地化 + 空值占位', function () {
  const i = VIEW.indexOf('function fmtKeyTime(');
  assert.ok(i > 0, '应有 fmtKeyTime');
  const b = VIEW.slice(i, i + 400);
  assert.ok(/G\.fmtLocalTime/.test(b), '应使用 G.fmtLocalTime');
  assert.ok(/return '—'/.test(b), '空值应显示 —');
});

check('后端职责：归一为 UTC ISO', function () {
  assert.ok(/normalize_event_time/.test(BACKEND), '后端应继续归一化时间');
});

console.log('\n#1477 全部通过 (' + passed + '/3)');