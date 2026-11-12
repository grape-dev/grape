/*
 * Issue #293 — 会话列表标题显示优化（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy chat.js；Vue 迁移后收敛为 convTitle/convMeta + 模板。
 *   1. convTitle 剥离 Issue 前缀后展示标题；无标题回退 conversation.default_title
 *   2. convMeta 展示 #issueN · 时间（无 issue 号时只给时间）
 *   3. 模板消费 convTitle/convMeta（不在模板里重复拼装）
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

check('convTitle 先剥前缀再回退默认标题', function () {
  const b = fnBody('convTitle');
  assert.ok(/stripIssuePrefix\(c\.title/.test(b), '应剥离 Issue 前缀');
  assert.ok(/conversation\.default_title/.test(b), '无标题应回退默认文案');
});

check('convMeta 拼接 #号与时间', function () {
  const b = fnBody('convMeta');
  assert.ok(/issue_number/.test(b), '应读 issue_number');
  assert.ok(/'#' \+ issueNum/.test(b), '有 issue 号应带 #');
  assert.ok(/fmtLocalTime\(c\.created_at\)/.test(b), '应格式化创建时间');
});

check('模板消费 convTitle / convMeta', function () {
  const tpl = VIEW.slice(0, VIEW.indexOf('<script'));
  assert.ok(/convTitle\(/.test(tpl), '模板应调用 convTitle');
  assert.ok(/convMeta\(/.test(tpl), '模板应调用 convMeta');
});

console.log('\n#293 全部通过 (' + passed + '/3)');