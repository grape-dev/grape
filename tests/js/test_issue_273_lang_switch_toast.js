/*
 * Issue #273 — 语言切换成功不再弹「Success」Toast（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy settings.js；语言切换 UI 已迁到 AccountView.selectLang。
 * 2026-11-11 修复：代码里仍保留 `if (!result.cached) showToast(common.success)`，
 * 本次按 #273 验收移除成功弹窗，只保留「部分资源加载失败」提示。
 *   1. failed === 0 分支内没有任何 showToast
 *   2. 部分失败（failed < total）仍提示，避免静默降级
 *   3. 切换中防抖（langSwitching）仍在
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/AccountView.vue'), 'utf8');

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

const B = fnBody('selectLang');
let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('成功分支内不调 showToast', function () {
  const i = B.indexOf('result.failed === 0');
  assert.ok(i > 0, '应有成功分支');
  const j = B.indexOf('} else if', i);
  const seg = B.slice(i, j > i ? j : i + 260);
  assert.ok(!/showToast/.test(seg), '成功分支不应再弹 Success');
});

check('部分失败仍提示', function () {
  assert.ok(/result\.failed < result\.total/.test(B), '应区分部分失败');
  const i = B.indexOf('result.failed < result.total');
  assert.ok(/showToast/.test(B.slice(i, i + 320)), '部分失败应提示');
});

check('切换防抖仍在', function () {
  assert.ok(/langSwitching/.test(B), '应有切换中标记防重复点击');
});

console.log('\n#273 全部通过 (' + passed + '/3)');