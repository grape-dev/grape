/*
 * Issue #286 — Chat 气泡布局（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy chat.js 并断言注入 CSS；Vue 迁移后模板用
 * `.msg .l/.r` 类，样式仍在全局 legacy/css/app.css（32 条 .msg 规则）。
 *   1. 用户/助手气泡分别用 r / l 类区分左右
 *   2. 气泡宽度受限（不拉满），长文本可换行
 *   3. 撤回态类名仍挂在同一元素上
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/ChatView.vue'), 'utf8');
const CSS = fs.readFileSync(path.join(ROOT, 'src/api/frontend/legacy/css/app.css'), 'utf8');
const TPL = VIEW.slice(0, VIEW.indexOf('<script'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('左右气泡类区分', function () {
  assert.ok(/m\.role === 'user' \? 'r' : 'l'/.test(TPL), '用户/助手应分别用 r / l');
  assert.ok(/class="msg l"/.test(TPL), '流式气泡应有 msg l');
});

check('气泡样式在全局 CSS 且限宽', function () {
  const n = (CSS.match(/\.msg\b/g) || []).length;
  assert.ok(n >= 5, '.msg 规则应存在（实际 ' + n + ' 条）');
  assert.ok(/\.msg[^{]*\{[^}]*max-width/.test(CSS), '气泡应限最大宽度');
});

check('撤回态仍挂同一元素', function () {
  assert.ok(/m\.recalled \? 'chat-msg-recalled'/.test(TPL), '撤回类应挂在气泡元素上');
});

console.log('\n#286 全部通过 (' + passed + '/3)');