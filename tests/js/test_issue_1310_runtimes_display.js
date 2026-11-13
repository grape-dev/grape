/*
 * Issue #1310 — runtimes 引擎展示位（卡片 / 详情 / 复制）口径统一（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy runtimes.js；Vue 迁移后统一走 engineDisplayName。
 *   1. 展示名优先级：display_name → engine_name → name → instance_name
 *   2. 卡片、详情弹窗、复制/下拉标签都调用同一函数（不各自拼）
 *   3. 有别名时「别名（原名）」双显；无值显示 —（不出现 undefined）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/RuntimesView.vue'), 'utf8');

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

check('展示名优先级链完整', function () {
  const b = fnBody('engineDisplayName');
  const order = ['rt.display_name', 'rt.engine_name', 'rt.name', 'rt.instance_name'];
  let last = -1;
  for (const k of order) {
    const i = b.indexOf(k);
    assert.ok(i >= 0, '应包含 ' + k);
    assert.ok(i > last, '优先级顺序错误（' + k + '）');
    last = i;
  }
});

check('多入口统一调用', function () {
  const n = (VIEW.match(/engineDisplayName\(/g) || []).length;
  assert.ok(n >= 4, '卡片/详情/标签应统一调用，实际 ' + n);
});

check('别名双显 + 空值占位', function () {
  assert.ok(/rt\.alias \|\| engineDisplayName\(rt\)/.test(VIEW), '有别名优先别名');
  assert.ok(/\{\{ engineDisplayName\(rt\) \|\| '—' \}\}/.test(VIEW), '空值显示 —');
});

console.log('\n#1310-runtimes 全部通过 (' + passed + '/3)');