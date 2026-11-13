/*
 * Issue #117 — 概览页 Worker / Runtime 列表分页（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy observability.js；Vue 迁移后为页态 + computed 切片。
 *   1. Worker 与 Runtime 各自独立页态（workerPage / runtimePage，0 基）
 *   2. 总页数由过滤后集合 × pageSize 计算，切片取 [page*size, (page+1)*size)
 *   3. 上一页/下一页有边界禁用，并显示 N / M
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

check('两处独立页态（0 基）', function () {
  assert.ok(/const workerPage = ref\(0\)/.test(VIEW), 'workerPage 应从 0 开始');
  assert.ok(/const runtimePage = ref\(0\)/.test(VIEW), 'runtimePage 应从 0 开始');
});

check('总页数与切片公式', function () {
  assert.ok(/workerTotalPages = computed|runtimeTotalPages = computed/.test(VIEW), '应有总页数 computed');
  assert.ok(/Math\.ceil\([\s\S]{0,80}PageSize\.value\)/.test(VIEW), '总页数应按 pageSize 向上取整');
  assert.ok(/slice\(\s*[\s\S]{0,60}Page\.value \* [a-zA-Z]+PageSize\.value/.test(VIEW), '切片应(page*size) 起');
});

check('边界禁用 + 页码展示', function () {
  const n = (VIEW.match(/:disabled="[a-zA-Z]+Page >= [a-zA-Z]+TotalPages - 1"/g) || []).length;
  assert.ok(n >= 1, '下一页应在末页禁用，实际 ' + n);
  assert.ok(/\{\{ [a-zA-Z]+Page \+ 1 \}\} \/ \{\{ [a-zA-Z]+TotalPages \}\}/.test(VIEW), '应显示 N / M');
});

console.log('\n#117 全部通过 (' + passed + '/3)');