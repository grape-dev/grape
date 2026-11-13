/*
 * Issue #442 — ChatAgent 消息撤回（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy chat.js；Vue 迁移后为 recallMessage/reEditRecalled。
 *   1. 已撤回消息：气泡加 chat-msg-recalled 类 + 显示「重新编辑」入口
 *   2. 未撤回用户消息：气泡带 data-msg-id（撤回按钮据此定位）
 *   3. 原文剥离 [chat_img:] 标记后缓存，供「重新编辑」回填
 *   4. reEditRecalled 把原文「追加」到现有输入，不覆盖用户已输入内容
 *   5. recallMessage 走二次确认 → POST /chat/messages/{id}/recall
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/ChatView.vue'), 'utf8');
const TPL = VIEW.slice(0, VIEW.indexOf('<script'));

function fnBody(name) {
  const i = VIEW.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到 ' + name);
  let d = 0,
    started = false;
  for (let j = i; j < VIEW.length; j++) {
    if (VIEW[j] === '{') {
      d++;
      started = true;
    } else if (VIEW[j] === '}') {
      d--;
      if (started && d === 0) return VIEW.slice(i, j + 1);
    }
  }
  throw new Error(name + ' 括号不闭合');
}

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('已撤回气泡标记 + 重新编辑入口', function () {
  assert.ok(/m\.recalled \? 'chat-msg-recalled'/.test(TPL), '撤回态应加类名');
  assert.ok(/@click\.prevent="reEditRecalled\(m\)"/.test(TPL), '应有「重新编辑」入口');
});

check('未撤回用户消息带 data-msg-id', function () {
  assert.ok(/:data-msg-id="m\.id \|\| ''"/.test(TPL), '应绑定消息 id');
  assert.ok(/function recallMessage\(/.test(VIEW), '应有 recallMessage');
});

check('原文剥离 [chat_img:] 标记后缓存', function () {
  assert.ok(/function stripChatImgMarkers\(/.test(VIEW), '应剥离图片标记');
  const b = VIEW.slice(VIEW.indexOf('const recallTexts'), VIEW.indexOf('const recallTexts') + 500);
  assert.ok(/recallTexts/.test(b), '应有原文缓存表');
});

check('重新编辑：追加而非覆盖', function () {
  const b = fnBody('reEditRecalled');
  assert.ok(/recallTexts\.get/.test(b), '应取回原文');
  assert.ok(/\+=|\\n\\n|draft\.value = draft\.value/.test(b), '应追加到现有输入（不覆盖）');
});

check('撤回走二次确认 + recall 端点', function () {
  const b = fnBody('recallMessage');
  assert.ok(/confirm|grapeConfirm/.test(b), '应有二次确认');
  assert.ok(/\/recall/.test(b), '应调用 recall 端点');
});

console.log('\n#442 全部通过 (' + passed + '/5)');