/*
 * 组织上下文与仓库下拉作用域（源码级回归断言）
 *
 * 现场问题（2026-11-19）：
 *   1. 退出登录→重新登录后，顶栏「组织」一直显示 i18n 默认文案「我的组织」——
 *      logout 清掉了 current_org_id，而重新登录后没有任何地方补默认组织，顶栏
 *      的 _updateTbOrgName 直接 return。
 *   2. 切到「个人」组织后，侧栏仓库下拉仍然显示团队组织的仓库（甚至组织仓库
 *      optgroup）——下拉数据源 /repos/managed 在个人组织下会放宽组织过滤，
 *      前端此前照单全收。
 *
 * 约定：
 *   - current_org_id 是唯一组织上下文：优先跟随当前仓库 org_id，其次沿用已选组织，
 *     最后回落到个人组织；
 *   - 仓库下拉只展示当前组织作用域内的仓库：个人空间不混入团队组织仓库，
 *     团队组织只展示本组织仓库；
 *   - 切仓 / 切组织 / 深链落仓后，顶栏组织名必须同步重绘。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/shell/js/app.js'), 'utf8');

function funcBody(name) {
  const start = APP.indexOf('function ' + name + '(');
  assert.ok(start >= 0, 'app.js 缺少 ' + name);
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

check('登出后重新登录会补默认组织（initSidebarRepos 回填），顶栏只展示已选组织', function () {
  const body = funcBody('_updateTbOrgName');
  assert.ok(/_fetchOrgs\(\)/.test(body), '_updateTbOrgName 应通过 _fetchOrgs 拿组织清单');
  assert.ok(/_validOrgId\(orgs, currentId\)/.test(body),
    '顶栏只展示已选组织；current_org_id 缺失/失效时保留 i18n 默认文案');
  assert.ok(!/_ensureOrgContext\(orgs\)/.test(body),
    '展示层不得隐式改写组织上下文（默认组织回填由 initSidebarRepos 负责）');
  const init = funcBody('initSidebarRepos');
  assert.ok(/_ensureOrgContext\(orgs\)/.test(init),
    '默认组织回填必须由 initSidebarRepos 负责（重新登录后补 current_org_id）');
});

check('已选组织优先，缺失时默认个人组织（不被仓库归属反向改写）', function () {
  const body = funcBody('_ensureOrgContext');
  const iCurrent = body.indexOf('_validOrgId(list, currentId)');
  const iPersonal = body.indexOf('_personalOrgId(list)');
  assert.ok(iCurrent > 0 && iPersonal > iCurrent,
    '应先认 current_org_id，缺失时回落到个人组织');
  assert.ok(body.indexOf('_validOrgId(list, repoOrg)') < 0,
    '仓库 org_id 不得反向改写已选组织（个人空间要能选任意托管仓库）');
  assert.ok(/localStorage\.setItem\('current_org_id', next\)/.test(body),
    '补齐结果应写回 current_org_id');
});

check('仓库下拉按组织作用域过滤', function () {
  const render = funcBody('_renderSidebarRepoOptions');
  assert.ok(/_repoInOrgScope\(r, _ctx\)/.test(render), '渲染前应调用 _repoInOrgScope 过滤');
  const scope = funcBody('_repoInOrgScope');
  // 个人：全部托管仓库（含被团队组织关联的普通仓库），只排除组织自动仓库
  assert.ok(/if \(ctx\.isPersonal\) \{[\s\S]*return !isOrgRepo;/.test(scope),
    '个人组织：托管仓库全部可见，仅排除组织自动仓库（组织项目）');
  assert.ok(/return oid === String\(ctx\.id\);/.test(scope),
    '团队组织：只保留该组织仓库');
});

check('切组织时按目标组织挑仓库', function () {
  const body = funcBody('switchOrg');
  assert.ok(/function _scopeRepos\(list, _orgId, _isPersonal\)/.test(body),
    'switchOrg 内部应有组织作用域过滤');
  assert.ok(/if \(_isPersonal\) \{[\s\S]*return !\(r && \(r\.is_org_repo/.test(body),
    '个人组织：排除组织自动仓库，但保留被团队组织关联的普通仓库');
  const team = body.slice(body.indexOf('function _enterTeamOrgWorkbench'));
  assert.ok(/_scopeRepos\(\(md && md\.repos\) \|\| \[\], orgId, false\)/.test(team),
    '团队组织只从本组织仓库里找组织仓库');
});

check('切仓 / 深链落仓后顶栏组织名同步重绘', function () {
  assert.ok(/localStorage\.setItem\('current_org_id', _repoOrgId\)/.test(funcBody('switchRepo')),
    'switchRepo 应把当前仓库组织写入 current_org_id');
  assert.ok(/_updateTbOrgName\(\)/.test(funcBody('switchRepo')), 'switchRepo 应刷新顶栏组织名');
  const dl = funcBody('_setCurrentRepoFromDeeplink');
  assert.ok(/orgId/.test(dl), '深链落仓应保留 org_id');
  assert.ok(/_updateTbOrgName\(\)/.test(dl) && /initSidebarRepos\(\)/.test(dl),
    '深链落仓后应重绘顶栏与仓库下拉');
});

check('紫色「组织项目」胶囊只在「组织上下文 + 组织仓库」显示', function () {
  const body = funcBody('_updateRepoOrgBadge');
  assert.ok(/badge\.style\.display = \(isTeam && _curIsOrgRepo\) \? 'inline-flex' : 'none'/.test(body),
    '必须同时满足团队组织上下文与 is_org_repo；个人空间/组织关联普通仓库都不显示');
});

console.log('\n组织上下文 / 仓库下拉作用域回归 全部通过 (' + passed + '/6)');