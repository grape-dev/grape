/*
 * Issue #12 — 添加托管仓库前确认弹窗（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy repos.js；Vue 迁移后为 showConfirm(第二个确认) 分支。
 *   1. 添加托管前先弹确认（repos.host.confirm / confirm_title / confirm_btn）
 *   2. 确认后才真正发起；取消不发请求
 *   3. 确认文案走 i18n，不硬编码
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/ReposView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('添加托管前有确认弹窗 + i18n 文案', function () {
  assert.ok(/repos\.host\.confirm_title/.test(VIEW), '应有确认标题键');
  assert.ok(/repos\.host\.confirm\b/.test(VIEW), '应有确认正文键');
  assert.ok(/repos\.host\.confirm_btn/.test(VIEW), '应有确认按钮键');
});

check('确认后才执行（showConfirm 包裹）', function () {
  const i = VIEW.indexOf('repos.host.confirm');
  assert.ok(i > 0, '应有确认调用点');
  const b = VIEW.slice(Math.max(0, i - 400), i + 400);
  assert.ok(/showConfirm|grapeConfirm/.test(b), '应通过 showConfirm 包裹后续动作');
});

check('实际执行函数由确认回调触发', function () {
  assert.ok(/const doHost = function \(/.test(VIEW), '应有 doHost 执行函数');
  const i = VIEW.indexOf('const doHost = function (');
  const b = VIEW.slice(i, i + 800);
  assert.ok(/onConfirm: doHost|showConfirm\([^)]*doHost/.test(VIEW), '必须经确认回调才执行');
  assert.ok(b.indexOf('G.API.post') >= 0, '执行体里才发请求');
});

console.log('\n#12 全部通过 (' + passed + '/3)');