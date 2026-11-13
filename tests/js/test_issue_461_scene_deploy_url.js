/*
 * Issue #461 — 场景详情「复制场景」按钮前的地球仪（登记场景访问地址）
 *
 * 迁移说明：原测试沙箱执行 legacy dashboard.js；Vue 迁移后为 dbv-deployglobe 区块。
 *   1. 有地球仪图标位（🌐），已登记则渲染可点链接，未登记显示空态
 *   2. 链接目标来自场景 deploy_url（后端字段），href 用属性绑定（不走 v-html）
 *   3. 保存地址走 PUT /scenes/{id}/deploy-url（或 repos 同构接口）+ 前后端校验
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');
const TPL = VIEW.slice(0, VIEW.indexOf('<script'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('地球仪区块 + 已登记/未登记两态', function () {
  assert.ok(/dbv-deployglobe/.test(TPL), '应有地球仪区块');
  assert.ok(/dbv-globe-link/.test(TPL), '已登记渲染链接');
  assert.ok(/dbv-globe-empty/.test(TPL), '未登记渲染空态');
  assert.ok(TPL.indexOf('🌐') >= 0, '应有地球仪图标');
});

check('链接走属性绑定（非 innerHTML）', function () {
  assert.ok(/:href="[^"]*deploy/i.test(TPL), 'deploy_url 应以 :href 绑定');
  assert.ok(!/v-html[\s\S]{0,80}deploy/i.test(TPL), '不应把部署地址拼进 v-html');
});

check('保存地址有前后端校验', function () {
  assert.ok(/deploy-url/.test(VIEW), '前端应调用 deploy-url 接口');
  const BACKEND = fs.readFileSync(path.join(ROOT, 'src/domains/mgmt/routes/scenes.py'), 'utf8');
  assert.ok(/_validate_deploy_url/.test(BACKEND), '后端应复用 URL 校验');
});

console.log('\n#461 全部通过 (' + passed + '/3)');