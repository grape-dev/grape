/*
 * Issue #1077-F — 需求中心「仓库全部」看板按列独立分页（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy issues.js；Vue 迁移后为 boardCursorOf + resetBoardCursors。
 *   1. 每列独立游标：page / hasMore / loading
 *   2. 整体刷新时重置所有列游标，再逐列重拉（loadBoardAll）
 *   3. 列底部「加载更多」按该列游标追加，不触发整页重绘
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

check('每列游标含 page/hasMore/loading', function () {
  assert.ok(/function boardCursorOf\(/.test(VIEW), '应有 boardCursorOf');
  const b = fnBody('boardCursorOf');
  for (const k of ['page', 'hasMore', 'loading']) {
    assert.ok(b.indexOf(k) >= 0, '游标应含 ' + k);
  }
});

check('整体刷新重置列游标后逐列重拉', function () {
  assert.ok(/function resetBoardCursors\(/.test(VIEW), '应有 resetBoardCursors');
  const i = VIEW.indexOf('function loadTasks(');
  const b = VIEW.slice(i, i + 900);
  assert.ok(/resetBoardCursors\(\)/.test(b), '整刷应重置游标');
  assert.ok(/loadBoardAll\(\)/.test(b), '整刷应走 loadBoardAll');
});

check('列底部按游标加载更多', function () {
  assert.ok(/boardCursorOf\(col\.k\)\.hasMore/.test(VIEW), '模板应按列 hasMore 显示加载更多');
  assert.ok(/boardCursorOf\(col\.k\)\.loading/.test(VIEW), '按列 loading 态');
  assert.ok(/load_more/.test(VIEW), '应有加载更多文案');
});

console.log('\n#1077-F 全部通过 (' + passed + '/3)');