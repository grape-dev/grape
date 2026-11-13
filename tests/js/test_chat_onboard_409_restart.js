/*
 * 飞书扫码 409 确认重开（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy chat.js；Vue 迁移后在 startFeishuOnboard 的 catch 里。
 *   1. 409 = 已有进行中的扫码任务 → 弹确认框（onboard.confirm_restart，danger 样式）
 *   2. 确认后先取消旧任务（POST /feishu/onboard/cancel）再以 force=true 重开
 *   3. 无 task_id 时直接 force 重开；非 409 走普通失败 toast
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/ChatView.vue'), 'utf8');
const i = VIEW.indexOf("if (e && e.status === 409)");
const B = i > 0 ? VIEW.slice(i, i + 1200) : '';

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('409 分支存在且弹确认框', function () {
  assert.ok(i > 0, '应有 409 判断');
  assert.ok(/G\.app\.showConfirm\(/.test(B), '应弹确认框');
  assert.ok(/onboard\.confirm_restart/.test(B), '确认文案走 i18n');
  assert.ok(/danger: true/.test(B), '确认框应为危险样式');
});

check('确认后先取消旧任务再重开', function () {
  assert.ok(/e\.detail\.task_id/.test(B) || /e\.detail && e\.detail\.task_id/.test(B), '应从 409 detail 取旧 task_id');
  assert.ok(/\/feishu\/onboard\/cancel/.test(B), '应先取消旧任务');
  const restart = (B.match(/startFeishuOnboard\([^)]*true\)/g) || []).length;
  assert.ok(restart >= 2, '取消后与无 task_id 两条路径都应以 force=true 重开，实际 ' + restart);
});

check('非 409 走普通失败提示', function () {
  assert.ok(/onboard\.start_failed/.test(B), '非 409 应提示启动失败');
});

console.log('\n409-restart 全部通过 (' + passed + '/3)');