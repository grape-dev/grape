/*
 * D109 引导门（2026-11-14 复核）：关系为空时，任何残留仓库入口都不得把人留在项目页。
 *
 * 现象（用户实弹）：清空全部 PAT 且无 OAuth 后刷新首页，仍停在项目工作台。
 * 根因两处（源码级断言）：
 *   1. _bootRestore 先按 URL/currentRepo 恢复仓库并 nav('dash')，绕过关系判定；
 *   2. _hasVcsBinding 在 __repoPlatformLinks 缺失时回落 identities，Vue 线没有
 *      _appVcsCatalog，纯登录渠道于是被当成仓库平台绑定。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/shell/js/app.js'),
  'utf8',
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('_bootRestore 在恢复仓库前先判关系', function () {
  const i = APP.indexOf('function _bootRestore()');
  assert.ok(i > 0, '应存在 _bootRestore');
  const body = APP.slice(i, i + 700);
  const gate = body.indexOf('!_hasVcsBinding()');
  const stashClear = body.indexOf("sessionStorage.removeItem('currentRepo')");
  const restore = body.indexOf('_applyAppPathRepo()');
  assert.ok(gate > 0, '_bootRestore 必须有 _hasVcsBinding 门禁');
  assert.ok(gate < restore, '门禁必须在 _applyAppPathRepo 之前');
  assert.ok(stashClear > gate && stashClear < restore, '无关系时要先清残留 currentRepo');
  assert.ok(body.indexOf('goToRepos()') > gate, '无关系要落引导页');
});

check('_hasVcsBinding 不再回落到 identities 猜绑定', function () {
  const i = APP.indexOf('function _hasVcsBinding()');
  assert.ok(i > 0, '应存在 _hasVcsBinding');
  const body = APP.slice(i, i + 800);
  assert.ok(/source === 'table' \|\| _links\.source === 'identities'/.test(body), '两种 source 都按 has_any 判定');
  assert.ok(body.indexOf('bindings') < 0, '不得再拿 bindings 猜「已接入仓库平台」');
  assert.ok(/return false;/.test(body), '快照缺失时按无关系处理');
});

console.log('\n引导门全部通过 (' + passed + '/2)');