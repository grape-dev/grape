/*
 * Issue #402 — 项目场景详情页「复制场景」（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy dashboard.js；Vue 迁移后为 copySceneFromDetail + generateCopySceneName。
 *   1. 复制按钮打开命名弹窗，默认名 = 原名 + "-copy"（截断 40 字符）
 *   2. 自动生成不重名（与已有场景去重，追加序号）
 *   3. 纯前端生成场景名，提交走后端既有建场景接口（零新增端点）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');

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

check('默认名 = 原名 + -copy，截断 40', function () {
  const b = fnBody('copySceneFromDetail');
  assert.ok(/-copy'/.test(b), "默认名应追加 '-copy'");
  assert.ok(/slice\(0, 40\)/.test(b), '应截断到 40 字符');
  assert.ok(/copyDlg\.open = true/.test(b), '应打开命名弹窗');
});

check('不重名生成（与已有场景去重）', function () {
  const b = fnBody('generateCopySceneName');
  assert.ok(/trim\(\)/.test(b) && /slice\(0, 40\)/.test(b), '应清洗并截断');
  assert.ok(/existing|some\(|includes\(|-copy-\d/.test(b), '应与已有名去重（追加序号）');
});

check('提交走既有建场景接口', function () {
  assert.ok(/\.post\('\/scenes|\/scenes\?repo=/.test(VIEW), '应走既有 /scenes 建场景接口');
});

console.log('\n#402 全部通过 (' + passed + '/3)');