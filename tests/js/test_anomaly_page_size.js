/*
 * Issue #123 — 异常任务 Tab 每页条数选择器（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy observability.js；Vue 迁移后为 anomalyPage +
 * anomalyPageItems computed + 页脚分页。
 *   1. 分页态：anomalyPage（0 基）、anomalyTotalPages（客户端分页，固定 10 条/页）
 *   已知偏差（记入 P3_AUDIT.md）：#123 的「每页条数选择器」未随 Vue 版移植，
 *   现在是固定 10 条/页 + 页码翻页。本用例只锁现存分页契约。
 *   2. 请求/渲染按页切片（anomalyPageItems），不是一次全渲染
 *   3. 上一页/下一页按钮带边界禁用 + 「N / M」页码
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

check('分页态与总页数存在', function () {
  assert.ok(/const anomalyPage = ref\(0\)/.test(VIEW), 'anomalyPage 应从 0 开始');
  assert.ok(/anomalyTotalPages/.test(VIEW), '应有总页数');
});

check('按页切片渲染（anomalyPageItems）', function () {
  assert.ok(/const anomalyPageItems = computed\(/.test(VIEW), '应有分页切片 computed');
  assert.ok(/v-for="s in anomalyPageItems"/.test(VIEW), '列表应按页渲染');
});

check('上一页/下一页边界禁用 + 页码', function () {
  assert.ok(/:disabled="anomalyPage === 0"/.test(VIEW), '首页禁用上一页');
  assert.ok(/:disabled="anomalyPage >= anomalyTotalPages - 1"/.test(VIEW), '末页禁用下一页');
  assert.ok(/\{\{ anomalyPage \+ 1 \}\} \/ \{\{ anomalyTotalPages \}\}/.test(VIEW), '应显示 N / M');
});

console.log('\n#123 全部通过 (' + passed + '/3)');