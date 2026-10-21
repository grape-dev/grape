/*
 * Issue #372 R13: UI 交互优化测试 — 新建项目同步 + 用户设置页仓库信息 + PAT 绑定提示
 *
 * 运行：node tests/js/test_issue_372_r13_ui_optimization.js
 *
 * 测试用例 T41-01 ~ T41-13，对应需求文档第十六章 R13.5 场景用例。
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');

function readFile(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const REPOS_JS = readFile('src/api/static/js/pages/repos.js');
const TEMPLATES_JS = readFile('src/api/static/js/pages/templates.js');
const APP_JS = readFile('src/api/static/js/app.js');
const ACCOUNT_JS = readFile('src/api/static/js/pages/account.js');
const ZH_APP_JSON = readFile('src/api/static/locales/zh/app.json');
const EN_APP_JSON = readFile('src/api/static/locales/en/app.json');

// ── T41-01: repos.js submitCreateRepo 成功后局部刷新项目列表 ──
function testSubmitCreateRepoForceSync() {
  const submitIdx = REPOS_JS.indexOf('function submitCreateRepo');
  assert.ok(submitIdx >= 0, 'submitCreateRepo 函数存在');

  const fnBody = REPOS_JS.slice(submitIdx, submitIdx + 2000);
  // R13.1: 局部刷新——调 /repos/sync 同步后直接 renderRepoSelection，不调 _forceSyncRepos（避免路由跳转）
  assert.ok(/API\.post\('\/repos\/sync'\)/.test(fnBody), 'submitCreateRepo 成功后调 /repos/sync 强制同步');
  assert.ok(/renderRepoSelection/.test(fnBody), '同步成功后调 renderRepoSelection 局部刷新');
  assert.ok(/repo_member_cache/.test(fnBody), '同步后更新 localStorage 缓存');
  // 降级路径：同步失败时调 /repos/member
  assert.ok(/API\.get\('\/repos\/member'\)/.test(fnBody), '同步失败降级为 /repos/member');

  console.log('  PASS: T41-01 repos.js submitCreateRepo 局部刷新项目列表');
}

// ── T41-02: templates.js doCreateFromTemplate 成功后调 _forceSyncRepos ──
function testDoCreateFromTemplateForceSync() {
  const fnIdx = TEMPLATES_JS.indexOf('function doCreateFromTemplate');
  assert.ok(fnIdx >= 0, 'doCreateFromTemplate 函数存在');

  // 函数体较大，取 5000 字符覆盖到成功回调
  const fnBody = TEMPLATES_JS.slice(fnIdx, fnIdx + 5000);
  assert.ok(/app\._forceSyncRepos/.test(fnBody), 'doCreateFromTemplate 成功后调 app._forceSyncRepos');
  // _forceSyncRepos 在 selectRepo 之前
  const syncIdx = fnBody.indexOf('app._forceSyncRepos');
  const selectIdx = fnBody.indexOf('selectRepo(');
  assert.ok(syncIdx >= 0 && selectIdx >= 0, '_forceSyncRepos 和 selectRepo 都存在');
  assert.ok(syncIdx < selectIdx, '_forceSyncRepos 在 selectRepo 之前');

  console.log('  PASS: T41-02 templates.js doCreateFromTemplate 调 _forceSyncRepos');
}

// ── T41-03: templates.js forkTemplate 成功后调 _forceSyncRepos ──
function testForkTemplateForceSync() {
  const fnIdx = TEMPLATES_JS.indexOf('function forkTemplate');
  assert.ok(fnIdx >= 0, 'forkTemplate 函数存在');

  const fnBody = TEMPLATES_JS.slice(fnIdx, fnIdx + 5000);
  assert.ok(/app\._forceSyncRepos/.test(fnBody), 'forkTemplate 成功后调 app._forceSyncRepos');
  const syncIdx = fnBody.indexOf('app._forceSyncRepos');
  const selectIdx = fnBody.indexOf('selectRepo(');
  assert.ok(syncIdx >= 0 && selectIdx >= 0, '_forceSyncRepos 和 selectRepo 都存在');
  assert.ok(syncIdx < selectIdx, '_forceSyncRepos 在 selectRepo 之前');

  console.log('  PASS: T41-03 templates.js forkTemplate 调 _forceSyncRepos');
}

// ── T41-04: app.js nav('account') 清空 repoTitle ──
function testNavAccountClearsRepoTitle() {
  // nav('account') 分支中应包含 repoTitle.textContent = ''
  const accountIdx = APP_JS.indexOf("view === 'account'");
  assert.ok(accountIdx >= 0, "nav('account') 分支存在");

  const branchBody = APP_JS.slice(accountIdx, accountIdx + 500);
  assert.ok(/repoTitle\.textContent\s*=\s*''/.test(branchBody), "nav('account') 清空 repoTitle.textContent");
  assert.ok(/R13\.2/.test(branchBody), '包含 R13.2 注释标记');

  console.log('  PASS: T41-04 app.js nav(account) 清空 repoTitle');
}

// ── T41-05: _accountManualBindForm 函数签名含 oauthConfigured ──
function testManualBindFormSignature() {
  const fnIdx = ACCOUNT_JS.indexOf('function _accountManualBindForm');
  assert.ok(fnIdx >= 0, '_accountManualBindForm 函数存在');

  const fnHeader = ACCOUNT_JS.slice(fnIdx, fnIdx + 100);
  assert.ok(/oauthConfigured/.test(fnHeader), '函数签名含 oauthConfigured 参数');

  console.log('  PASS: T41-05 _accountManualBindForm 含 oauthConfigured 参数');
}

// ── T41-06: 未配 OAuth 时渲染 pat_no_refresh_hint ──
function testPatHintRenderedWhenNoOAuth() {
  const fnIdx = ACCOUNT_JS.indexOf('function _accountManualBindForm');
  const fnBody = ACCOUNT_JS.slice(fnIdx, fnIdx + 1000);

  assert.ok(/if\s*\(!oauthConfigured\)/.test(fnBody), '有 !oauthConfigured 条件分支');
  assert.ok(/pat_no_refresh_hint/.test(fnBody), '分支内渲染 pat_no_refresh_hint');
  assert.ok(/ℹ️/.test(fnBody), '提示卡片含 ℹ️ 图标');

  console.log('  PASS: T41-06 未配 OAuth 时渲染 pat_no_refresh_hint');
}

// ── T41-07: _accountBindProvider 两处调用传入 oauthConfigured ──
function testBindProviderPassesOAuthConfigured() {
  const fnIdx = ACCOUNT_JS.indexOf('function _accountBindProvider');
  assert.ok(fnIdx >= 0, '_accountBindProvider 函数存在');

  const fnBody = ACCOUNT_JS.slice(fnIdx, fnIdx + 3000);
  assert.ok(fnBody.indexOf('_accountManualBindForm(provider, isZh, pName, true)') >= 0,
    '已配 OAuth 时传 oauthConfigured=true');
  // 未配 OAuth 时传 false
  assert.ok(fnBody.indexOf('_accountManualBindForm(provider, isZh, pName, false)') >= 0,
    '未配 OAuth 时传 oauthConfigured=false');

  console.log('  PASS: T41-07 _accountBindProvider 两处传入 oauthConfigured');
}

// ── T41-08: zh/app.json 包含 pat_no_refresh_hint ──
function testZhPatHintKey() {
  const zh = JSON.parse(ZH_APP_JSON);
  assert.ok(zh.account, 'zh account 节点存在');
  assert.ok(zh.account.pat_no_refresh_hint, 'zh pat_no_refresh_hint key 存在');
  assert.ok(typeof zh.account.pat_no_refresh_hint === 'string', 'zh pat_no_refresh_hint 是字符串');
  assert.ok(zh.account.pat_no_refresh_hint.length > 10, 'zh pat_no_refresh_hint 内容非空');

  console.log('  PASS: T41-08 zh/app.json 包含 pat_no_refresh_hint');
}

// ── T41-09: en/app.json 包含 pat_no_refresh_hint ──
function testEnPatHintKey() {
  const en = JSON.parse(EN_APP_JSON);
  assert.ok(en.account, 'en account 节点存在');
  assert.ok(en.account.pat_no_refresh_hint, 'en pat_no_refresh_hint key 存在');
  assert.ok(typeof en.account.pat_no_refresh_hint === 'string', 'en pat_no_refresh_hint 是字符串');
  assert.ok(en.account.pat_no_refresh_hint.length > 10, 'en pat_no_refresh_hint 内容非空');

  console.log('  PASS: T41-09 en/app.json 包含 pat_no_refresh_hint');
}

// ── T41-10: 已配 OAuth 时不渲染 pat_no_refresh_hint ──
// 逻辑验证：!oauthConfigured 才渲染，oauthConfigured=true 时跳过
// 通过源码结构验证——pat_no_refresh_hint 出现在 if (!oauthConfigured) 块内
function testNoHintWhenOAuthConfigured() {
  const fnIdx = ACCOUNT_JS.indexOf('function _accountManualBindForm');
  const fnBody = ACCOUNT_JS.slice(fnIdx, fnIdx + 1000);

  // 找 if (!oauthConfigured) 块
  const ifIdx = fnBody.indexOf('if (!oauthConfigured)');
  assert.ok(ifIdx >= 0, '存在 if (!oauthConfigured) 条件');

  // pat_no_refresh_hint 应在该 if 块内（ifIdx 之后）
  const hintIdx = fnBody.indexOf('pat_no_refresh_hint');
  assert.ok(hintIdx > ifIdx, 'pat_no_refresh_hint 在 !oauthConfigured 条件块内');

  console.log('  PASS: T41-10 已配 OAuth 时不渲染 pat_no_refresh_hint');
}

// ── T41-11: loadDash catch 中含 e.status === 403 判断 ──
function testDash403CheckExists() {
  // 定位 loadDash 函数中的 catch 块
  const dashIdx = APP_JS.indexOf("view === 'dash'");
  assert.ok(dashIdx >= 0, "nav('dash') 分支存在");

  // 取 dash 分支后 3000 字符覆盖 loadDash + catch
  const dashBody = APP_JS.slice(dashIdx, dashIdx + 3000);
  assert.ok(/e\.status\s*===\s*403/.test(dashBody), 'loadDash catch 中含 e.status === 403 判断');

  console.log('  PASS: T41-11 loadDash catch 中含 e.status === 403 判断');
}

// ── T41-12: 403 时清除 currentRepo + 调 switchRepo 或 goToRepos ──
function testDash403SwitchLogic() {
  const dashIdx = APP_JS.indexOf("view === 'dash'");
  const dashBody = APP_JS.slice(dashIdx, dashIdx + 3000);

  // 找 403 块
  const forbiddenIdx = dashBody.indexOf('e.status === 403');
  assert.ok(forbiddenIdx >= 0, '403 判断存在');

  const forbiddenBlock = dashBody.slice(forbiddenIdx, forbiddenIdx + 1200);
  assert.ok(/sessionStorage\.removeItem\('currentRepo'\)/.test(forbiddenBlock), '403 时清除 sessionStorage currentRepo');
  assert.ok(/switchRepo/.test(forbiddenBlock), '403 时调 switchRepo 切换仓库');
  assert.ok(/goToRepos/.test(forbiddenBlock), '403 无其他仓库时调 goToRepos 跳项目大厅');

  console.log('  PASS: T41-12 403 时清除 currentRepo + switchRepo/goToRepos');
}

// ── T41-13: 403 切换逻辑在 !_dashboardEditing 之后 ──
function testDash403AfterEditingGuard() {
  const dashIdx = APP_JS.indexOf("view === 'dash'");
  const dashBody = APP_JS.slice(dashIdx, dashIdx + 3000);

  const editingIdx = dashBody.indexOf('_dashboardEditing');
  const forbiddenIdx = dashBody.indexOf('e.status === 403');
  assert.ok(editingIdx >= 0 && forbiddenIdx >= 0, '_dashboardEditing 和 403 判断都存在');
  assert.ok(editingIdx < forbiddenIdx, '403 切换逻辑在 _dashboardEditing 检查之后');

  console.log('  PASS: T41-13 403 切换逻辑在 !_dashboardEditing 之后');
}

// ── 执行 ──
const tests = [
  testSubmitCreateRepoForceSync,
  testDoCreateFromTemplateForceSync,
  testForkTemplateForceSync,
  testNavAccountClearsRepoTitle,
  testManualBindFormSignature,
  testPatHintRenderedWhenNoOAuth,
  testBindProviderPassesOAuthConfigured,
  testZhPatHintKey,
  testEnPatHintKey,
  testNoHintWhenOAuthConfigured,
  testDash403CheckExists,
  testDash403SwitchLogic,
  testDash403AfterEditingGuard,
  testDash403ClearsTriedOnSuccess,
];

let failed = 0;
console.log(`Issue #372 R13 UI optimization tests (${tests.length} suites)`);
tests.forEach(t => { try { t(); } catch (e) { failed++; console.error('  FAIL:', e.message); } });
console.log(`\n${tests.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);

// ── T41-14: dashboard 成功加载时清除 _tried403Repos 去重列表 ──
function testDash403ClearsTriedOnSuccess() {
  const dashIdx = APP_JS.indexOf("view === 'dash'");
  const dashBody = APP_JS.slice(dashIdx, dashIdx + 4000);
  // 查找成功回调中的 _tried403Repos 清除
  const successClear = dashBody.indexOf('_tried403Repos = null');
  // 应该有至少 2 处：403 全部试过时清 + 成功加载时清
  const allClears = dashBody.split('_tried403Repos = null').length - 1;
  assert.ok(allClears >= 2,
    `_tried403Repos = null should appear >= 2 times (success + all-tried), found ${allClears}`);
  console.log('  PASS: T41-14 dashboard 成功加载时清除 _tried403Repos');
}
