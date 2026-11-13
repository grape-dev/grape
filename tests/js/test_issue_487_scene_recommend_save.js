/*
 * Issue #487 — 推荐规则「添加规则」按钮初始禁用（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy dashboard.js；Vue 迁移后为 recDlg.loaded 门控。
 *   1. 规则未加载完成（!recDlg.loaded）时按钮 disabled
 *   2. 点击才 push 新规则（空 scene_id + condition 待填）
 *   3. 加载完成后可点（门条件只依赖 loaded）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('未加载完成时禁用', function () {
  assert.ok(/:disabled="!recDlg\.loaded"/.test(VIEW), '按钮应绑定 !recDlg.loaded');
  const i = VIEW.indexOf(':disabled="!recDlg.loaded"');
  const b = VIEW.slice(i, i + 400);
  assert.ok(/recommend_add_rule/.test(b), '应有「添加规则」按钮');
});

check('点击才追加规则', function () {
  const i = VIEW.indexOf(':disabled="!recDlg.loaded"');
  const b = VIEW.slice(i, i + 400);
  assert.ok(/recDlg\.rules\.push\(/.test(b), '点击应 push 新规则');
  assert.ok(/scene_id: ''/.test(b), '新规则 scene_id 应留空待选');
});

check('loaded 有初始化与置位', function () {
  assert.ok(/loaded: false/.test(VIEW), '初始应为 false');
  assert.ok(/recDlg\.loaded = true|\.loaded = true/.test(VIEW), '加载完成应置 true');
});

console.log('\n#487 全部通过 (' + passed + '/3)');