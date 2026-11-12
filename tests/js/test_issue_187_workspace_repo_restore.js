/*
 * Issue #187 — 工作台切 tab 记忆上次选择的仓库（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy repos.js；仓库选择与恢复在 app.js（Issue #82 语义）。
 *   1. 已有 currentRepo 时不覆写（刷新/切 tab 保持用户选择，不跳回列表首位）
 *   2. 仅当没有选中仓库时默认选 managedRepos[0]
 *   3. 无托管仓库时清空 sessionStorage.currentRepo（不留脏值）
 *   4. 选择结果写入 sessionStorage（页签级隔离）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const APP = fs.readFileSync(path.join(ROOT, 'src/api/frontend/legacy/js/app.js'), 'utf8');

function fnBody(src, name) {
  const i = src.indexOf('  function ' + name + '(') >= 0 ? src.indexOf('  function ' + name + '(') : src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到 ' + name);
  let d = 0,
    started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') {
      d++;
      started = true;
    } else if (src[j] === '}') {
      d--;
      if (started && d === 0) return src.slice(i, j + 1);
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

const LS = APP.indexOf('function loadRepos(');
const REGION = APP.slice(LS, LS + 6000); // loadRepos 体量 ~1.4k，取 6k 窗口足以覆盖守卫段

check('仅在无选中仓库时才默认首选', function () {
  assert.ok(/if \(!current\) \{/.test(REGION), '应有 !current 守卫');
  assert.ok(/managedRepos\[0\]/.test(REGION), '无选中时取首个托管仓库');
});

check('有选中仓库不覆写（保持用户选择）', function () {
  const i = REGION.indexOf('if (!current) {');
  assert.ok(i > 0, '应有守卫');
  const guard = REGION.slice(i, i + 420);
  assert.ok(/sessionStorage\.setItem\('currentRepo'/.test(guard), '仅在守卫内写 currentRepo');
  assert.ok(/Issue #82/.test(REGION), '应保留 Issue #82 决策注释（不覆写用户选择）');
});

check('无托管仓库清脏值', function () {
  assert.ok(/sessionStorage\.removeItem\('currentRepo'\)/.test(REGION), '无仓库应移除 currentRepo');
});

check('选择结果写 sessionStorage（页签级）', function () {
  assert.ok(/sessionStorage\.setItem\('currentRepo'/.test(APP), '应写 sessionStorage');
  assert.ok(/function currentRepo\(/.test(APP), '应有 currentRepo 读取函数');
});

console.log('\n#187 全部通过 (' + passed + '/4)');