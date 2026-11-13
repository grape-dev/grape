/*
 * Issue #456 — 组织管理页「返回工作台」按钮（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy organizations.js；Vue 迁移后为 Organizations.vue 的返回入口。
 *   1. 有返回工作台的按钮/链接（workspace 视图）
 *   2. 点击走 app().nav('dash')（工作台）而不是历史回退（避免回退到登录页）
 *   3. 文案走 i18n
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/app/src/views/Organizations.vue'),
  'utf8',
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('存在返回工作台入口', function () {
  assert.ok(/back|返回/i.test(VIEW), '应有返回入口');
  assert.ok(/workspace/i.test(VIEW), '应指向 workspace/工作台');
});

check('点击走壳的返回助手（不依赖 history.back）', function () {
  assert.ok(/_accountGoBack\(\)/.test(VIEW), '应调用 G.app._accountGoBack()');
  assert.ok(!/history\.back\(\)/.test(VIEW), '不应依赖 history.back（可能回到登录页）');
});

check('文案走 i18n', function () {
  assert.ok(/t\('organizations\.|t\('common\./.test(VIEW), '按钮文案应走 i18n');
});

console.log('\n#456 全部通过 (' + passed + '/3)');