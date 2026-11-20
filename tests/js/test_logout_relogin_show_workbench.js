/*
 * 退出登录 → 重新登录后工作台不可见（源码级回归断言）
 *
 * 现象（2026-11-19 现场）：在 /app/<owner>/<repo>/ 页面退出登录，再从登录页重新登录，
 * 路由直接命中 #dash（_bootRestore → _applyAppPathRepo 命中，不走 loadRepos），
 * 右侧主区一片空白，只剩左侧栏。
 *
 * 根因：showLogin() 把 sidebar / mainView 写成内联 display:none；showApp() 只设置了
 * data-logged-in 并隐藏登录页，没有摘掉这两个内联样式。CSS
 * `[data-logged-in="1"] .sb/.mn{display:flex}` 被内联样式永久压住，直到 loadRepos /
 * renderReposFromData 显式写回 flex —— 而深链直达 #dash 的路径不会经过 loadRepos。
 *
 * 约定：showApp() 是「登录后重新绘制」的唯一汇聚点，必须先清掉内联 display，
 * 交回 CSS 接管；后续大厅 / 引导页需要隐藏时各自再显式设置，互不冲突。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/shell/js/app.js'), 'utf8');

function funcBody(name) {
  const start = APP.indexOf('function ' + name + '(');
  assert.ok(start >= 0, 'app.js 缺少 ' + name);
  // 取到下一个顶层「两个空格缩进的 function」为止（app.js 内部函数同缩进）
  const rest = APP.slice(start + 1);
  const next = rest.search(/\n  function [A-Za-z_]/);
  return next >= 0 ? rest.slice(0, next) : rest;
}

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('登录页仍会把工作台收起（内联 none 的前提）', function () {
  const body = funcBody('showLogin');
  assert.ok(/\$\('sidebar'\)\.style\.display = 'none'/.test(body), 'showLogin 应收起 sidebar');
  assert.ok(/\$\('mainView'\)\.style\.display = 'none'/.test(body), 'showLogin 应收起 mainView');
});

check('showApp 清掉内联 display，交回 CSS 显示工作台', function () {
  const body = funcBody('showApp');
  assert.ok(/\$\('sidebar'\)\.style\.display = ''/.test(body),
    'showApp 必须清空 sidebar 的内联 display，否则重新登录后侧栏状态依赖 loadRepos');
  assert.ok(/\$\('mainView'\)\.style\.display = ''/.test(body),
    'showApp 必须清空 mainView 的内联 display，否则深链 #dash 直登时主区空白');
  assert.ok(/loadProfile\(\)/.test(body), 'showApp 仍应继续走 loadProfile → _bootRestore 路由');
});

console.log('\n退出→重登工作台可见性回归 全部通过 (' + passed + '/2)');