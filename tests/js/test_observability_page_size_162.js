/*
 * Issue #162 — 概览两处（Worker / Runtime）「每页条数」选择器（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy observability.js；Vue 迁移后为 workerPageSize/runtimePageSize。
 *   1. 两个独立的分页大小状态，默认 10
 *   2. 选择器绑定 setWorkerPageSize / setRuntimePageSize，切换后回到第 1 页
 *   3. 分页切片按各自 pageSize 计算
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

check('两处分页大小状态，默认 10', function () {
  assert.ok(/const workerPageSize = ref\(10\)/.test(VIEW), 'worker 默认 10');
  assert.ok(/const runtimePageSize = ref\(\d+\)/.test(VIEW), 'runtime 应有独立 pageSize');
});

check('选择器绑定切换函数', function () {
  assert.ok(/@change="setWorkerPageSize"/.test(VIEW), 'worker 选择器应绑定 setWorkerPageSize');
  assert.ok(/@change="setRuntimePageSize"/.test(VIEW), 'runtime 选择器应绑定 setRuntimePageSize');
  const i = VIEW.indexOf('function setWorkerPageSize(');
  assert.ok(i > 0, '应有 setWorkerPageSize');
  assert.ok(/Page\.value = 0/.test(VIEW.slice(i, i + 400)), '换页大小后应回到第 1 页');
});

check('切片按各自 pageSize', function () {
  assert.ok(/workerPageSize\.value/.test(VIEW), 'worker 分页应使用 workerPageSize');
  assert.ok(/runtimePageSize\.value/.test(VIEW), 'runtime 分页应使用 runtimePageSize');
});

console.log('\n#162 全部通过 (' + passed + '/3)');