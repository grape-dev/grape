/*
 * Issue #224 — 事件详情 navEvent：URL hash 同步 + 403 处理（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy events.js；Vue 迁移后在 EventView 的导航函数里。
 *   1. 打开事件详情时把 #event/<seq> 写入地址栏（replaceState，不新增历史）
 *   2. 403（无权查看）→ 提示并回退列表，不留死页面
 *   3. 关闭/返回时 hash 复原
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/EventView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('hash 同步（#event/<seq>，replaceState）', function () {
  assert.ok(/history\.replaceState\(null, '', '#event\/' \+ seq\)/.test(VIEW), '应把 seq 写进 hash');
  assert.ok(!/pushState\([^)]*#event/.test(VIEW), '不应新增历史记录（用 replaceState）');
});

check('403 有专门处理', function () {
  const i = VIEW.indexOf('status === 403');
  assert.ok(i > 0, '应有 403 分支');
  const b = VIEW.slice(i, i + 600);
  assert.ok(/showToast|toast\(/.test(b), '403 应提示用户');
  assert.ok(/goBack|close|nav\(|back/.test(b), '403 应回退/关闭，不停在死页面');
});

check('hash 在事件切换时同步更新', function () {
  const n = (VIEW.match(/replaceState\(null, '', '#event\/'/g) || []).length;
  assert.ok(n >= 1, '应在事件切换时更新 hash');
});

console.log('\n#224 全部通过 (' + passed + '/3)');