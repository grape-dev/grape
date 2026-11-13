/*
 * Issue #407（遗留缺口）— ChatAgent 输入草稿持久化（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy chat.js；Vue 迁移后为 draftKey/saveDraft/onDraftInput。
 *   1. 草稿按会话隔离：key = chatDraft_<convId>（sessionStorage）
 *   2. 输入即存（onDraftInput → saveDraft(currentConvId, draft)）
 *   3. 切换会话时回填草稿；发送成功/清空后清除草稿
 *   4. 无会话 id 时不写草稿（避免串会话）
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

check('草稿 key 按会话隔离且用 sessionStorage', function () {
  const b = fnBody('draftKey');
  assert.ok(/return 'chatDraft_' \+ convId/.test(b), "key 应为 chatDraft_<convId>");
  const s = fnBody('saveDraft');
  assert.ok(/sessionStorage\.setItem/.test(s), '应写 sessionStorage');
  assert.ok(/if \(!convId\) return/.test(s), '无会话 id 不应写草稿');
});

check('输入即存草稿', function () {
  const b = fnBody('onDraftInput');
  assert.ok(/saveDraft\(currentConvId\.value, draft\.value\)/.test(b), '输入应保存到当前会话');
  assert.ok(/@input="onChatInput"/.test(VIEW), '输入框应绑定 @input');
  const h = fnBody('onChatInput');
  assert.ok(/onDraftInput\(\)/.test(h), 'onChatInput 应落草稿');
});

check('切换会话回填草稿', function () {
  assert.ok(/function restoreDraft\(/.test(VIEW), '应有 restoreDraft');
  assert.ok(/const _draft = restoreDraft\(convId\)/.test(VIEW), '切换会话时按 convId 取草稿');
  assert.ok(/if \(_draft\) draft\.value = _draft/.test(VIEW), '有草稿则回填输入框');
  assert.ok(/function switchConversation\(/.test(VIEW), '应有会话切换入口');
});

check('清空/发送后移除草稿', function () {
  const b = fnBody('clearDraft');
  assert.ok(/removeItem\(draftKey\(convId\)\)/.test(b), '应删除对应 key');
  assert.ok(/clearDraft\(/.test(VIEW.slice(VIEW.indexOf('function clearDraft(') + 200)), '应在发送/清理路径被调用');
});

console.log('\n#407-draft 全部通过 (' + passed + '/4)');