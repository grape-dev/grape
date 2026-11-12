/*
 * Issue #407 — ChatAgent 图片/附件保活（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy chat.js；Vue 迁移后为 pendingImages + loadChatImages。
 *   1. 上限 MAX_IMAGES=9，超出即提示且不追加
 *   2. 选择图片追加进 pendingImages；可单项移除 / 整体清空
 *   3. 发送时以 [chat_img:<id>] 标记写入消息内容，历史渲染再还原成图片
 *   4. 切换会话按 convId 回填图片（竞态守卫：conv 已变则丢弃）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/ChatView.vue'), 'utf8');

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

check('上限 9 张，超出提示且不追加', function () {
  assert.ok(/const MAX_IMAGES = 9/.test(VIEW), 'MAX_IMAGES 应为 9');
  const b = fnBody('onImageSelect');
  assert.ok(/MAX_IMAGES - pendingImages\.value\.length/.test(b), '应计算剩余额度');
  assert.ok(/image_max_reached/.test(b), '超出应有提示');
  assert.ok(/return/.test(b), '超出应提前返回');
});

check('单项移除与整体清空', function () {
  assert.ok(/function removeImage\(/.test(VIEW), '应有 removeImage');
  assert.ok(/function clearChatImages\(/.test(VIEW), '应有 clearChatImages');
  assert.ok(/pendingImages\.value\.splice|pendingImages\.value = \[\]/.test(VIEW), '应能移除/清空');
});

check('发送用 [chat_img:<id>] 标记，渲染可还原', function () {
  assert.ok(VIEW.indexOf('chat_img:') >= 0, '应使用 [chat_img:] 标记协议');
  assert.ok(/function renderMsg\(/.test(VIEW), '应有消息渲染函数处理标记');
});

check('切会话按 convId 回填图片 + 竞态守卫', function () {
  const i = VIEW.indexOf('function loadChatImages(');
  assert.ok(i > 0, '应有 loadChatImages');
  assert.ok(/currentConvId\.value !== convId\) return/.test(VIEW), '回填应有竞态守卫');
});

console.log('\n#407-attachment 全部通过 (' + passed + '/4)');