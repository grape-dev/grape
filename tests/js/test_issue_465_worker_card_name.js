/*
 * Issue #465 — 设备与引擎页 Worker 卡片名称与 runtimes 表「所属设备」列口径一致
 *
 * 迁移说明：原测试沙箱执行 legacy observability.js；Vue 迁移后统一走 workerDisplayName。
 *   1. 唯一来源 workerDisplayName（Issue #701：runtime_credentials.name 为准）
 *   2. 设备卡片、runtime 列表、runtimes 表「所属设备」列三处都调用同一函数
 *   3. 空名统一占位（不出现 undefined / null / 空串直出）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/app/src/views/ObservabilityView.vue'),
  'utf8',
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('workerDisplayName 存在且含空名占位', function () {
  assert.ok(/function workerDisplayName\(/.test(VIEW), '应有 workerDisplayName');
  const i = VIEW.indexOf('function workerDisplayName(');
  const b = VIEW.slice(i, i + 600);
  assert.ok(/unnamed_worker|未命名设备/.test(b), '空名应有统一占位文案');
  assert.ok(/return/.test(b), '应返回字符串');
});

check('三处渲染统一调用（设备卡 / runtime 行 / 所属设备列）', function () {
  const n = (VIEW.match(/workerDisplayName\(/g) || []).length;
  assert.ok(n >= 4, '除定义外至少 3 处调用，实际 ' + n);
  const tpl = VIEW.slice(0, VIEW.indexOf('<script'));
  assert.ok((tpl.match(/workerDisplayName\(/g) || []).length >= 3, '模板中应有三处渲染点');
});

check('不直接输出未保护的名称字段', function () {
  assert.ok(!/>\{\{ wk\.name \}\}</.test(VIEW), '设备卡不应直出原始 name');
  assert.ok(!/>\{\{ rti\.device_name \}\}</.test(VIEW), '所属设备列不应直出 device_name');
});

console.log('\n#465 全部通过 (' + passed + '/3)');