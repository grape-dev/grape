/*
 * Issue #697 — 自动巡检疑重模板提示（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy autopilot.js；Vue 迁移后为 dedup_level/dedup_note 渲染。
 *   1. finding.dedup_level === 'suspected_duplicate' 时展示疑重标记
 *   2. dedup_note 作为悬浮说明（title），无 note 不渲染空提示
 *   3. 派单弹窗同样提示（两处入口一致）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/AutopilotView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('疑重态按 dedup_level 判定', function () {
  const n = (VIEW.match(/dedup_level === 'suspected_duplicate'/g) || []).length;
  assert.ok(n >= 1, '应按 dedup_level 判定疑重');
});

check('dedup_note 走悬浮说明', function () {
  assert.ok(/:title="[^"]*dedup_note/.test(VIEW), 'dedup_note 应绑定 title');
  assert.ok(/dedup_note \|\| ''/.test(VIEW), '无 note 应给空串（不渲染 undefined）');
});

check('列表与派单弹窗两处一致', function () {
  const n = (VIEW.match(/dedup_level/g) || []).length;
  assert.ok(n >= 2, '列表行与派单弹窗都应提示，实际 ' + n);
  assert.ok(/apv-dup-note/.test(VIEW), '应有疑重说明样式');
});

console.log('\n#697 全部通过 (' + passed + '/3)');