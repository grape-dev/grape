/*
 * Issue #163 / PR #175 — 事件详情页「返回需求详情」状态流（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy issues.js；Vue 迁移后为 _taskDetailFromScene 标记 + backFromDetail。
 *   1. 从场景页进入详情：置 _taskDetailFromScene
 *   2. 返回时：有标记 → 清标记并 history.back()（回到场景页）；无标记 → 关闭详情（回列表）
 *   3. 标记一次性（消费后清空，不污染后续进入）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/IssuesView.vue'), 'utf8');

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

check('从场景进入时的标记契约（fromScene 对象 + latestSeq）', function () {
  assert.ok(/fromScene && fromScene\.latestSeq/.test(VIEW), '事件页返回应带 latestSeq 回场景');
  assert.ok((VIEW.match(/_taskDetailFromScene = null/g) || []).length >= 3, '多个分支都应清标记（一次性）');
});

check('返回：有标记 → 清标记 + history.back', function () {
  const b = fnBody('backFromDetail');
  assert.ok(/_taskDetailFromScene/.test(b), '应读取标记');
  assert.ok(/_taskDetailFromScene = null/.test(b), '消费后应清标记（一次性）');
  assert.ok(/history\.back\(\)/.test(b), '应回退到场景页');
  assert.ok(/closeDetail\(\)/.test(b), '无标记时应关闭详情');
});

console.log('\n#163 全部通过 (' + passed + '/2)');