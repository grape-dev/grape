/*
 * 项目列表页/引导页语言切换局部刷新测试
 *
 * 运行：node tests/js/test_i18n_repos_local_refresh.js
 *
 * 验证语言切换时不发 API 请求、不闪烁 loading 遮罩、用缓存数据重渲染。
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');

function readFile(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const APP_JS = readFile('src/api/static/js/app.js');
const I18N_JS = readFile('src/api/static/js/i18n.js');
const INDEX_HTML = readFile('src/api/static/index.html');

// ── T1: app.js 缓存 /repos/member 数据 ──
function testCachedReposDataExists() {
  assert.ok(APP_JS.indexOf('_cachedReposData') >= 0,
    'app.js should have _cachedReposData variable');
  // goToRepos 成功回调中应缓存数据
  assert.ok(APP_JS.indexOf('_cachedReposData = d') >= 0,
    'goToRepos should cache /repos/member response');
  console.log('  PASS: T1 app.js 缓存 /repos/member 数据');
}

// ── T2: _rerenderReposView 函数存在 ──
function testRerenderReposViewExists() {
  assert.ok(APP_JS.indexOf('function _rerenderReposView') >= 0,
    'app.js should have _rerenderReposView function');
  console.log('  PASS: T2 _rerenderReposView 函数存在');
}

// ── T3: _rerenderReposView 不发 API 请求 ──
function testRerenderReposViewNoAPI() {
  const funcIdx = APP_JS.indexOf('function _rerenderReposView');
  const funcEnd = APP_JS.indexOf('\n  }', funcIdx);
  const funcBody = APP_JS.slice(funcIdx, funcEnd);
  // 不应包含 API.get('/repos/member')
  assert.ok(funcBody.indexOf("API.get('/repos/member')") < 0,
    '_rerenderReposView should not call API.get /repos/member');
  // 应使用 _cachedReposData
  assert.ok(funcBody.indexOf('_cachedReposData') >= 0,
    '_rerenderReposView should use _cachedReposData');
  console.log('  PASS: T3 _rerenderReposView 不发 API 请求');
}

// ── T4: _rerenderReposView 不创建 loading 遮罩 ──
function testRerenderReposViewNoLoadingOverlay() {
  const funcIdx = APP_JS.indexOf('function _rerenderReposView');
  const funcEnd = APP_JS.indexOf('\n  }', funcIdx);
  const funcBody = APP_JS.slice(funcIdx, funcEnd);
  // 不应包含 gotoReposOverlay（loading 遮罩）
  assert.ok(funcBody.indexOf('gotoReposOverlay') < 0,
    '_rerenderReposView should not create loading overlay');
  console.log('  PASS: T4 _rerenderReposView 不创建 loading 遮罩');
}

// ── T5: _rerenderReposView 调 renderRepoSelection ──
function testRerenderReposViewCallsRender() {
  const funcIdx = APP_JS.indexOf('function _rerenderReposView');
  const funcEnd = APP_JS.indexOf('\n  }', funcIdx);
  const funcBody = APP_JS.slice(funcIdx, funcEnd);
  assert.ok(funcBody.indexOf('renderRepoSelection') >= 0,
    '_rerenderReposView should call renderRepoSelection');
  console.log('  PASS: T5 _rerenderReposView 调 renderRepoSelection');
}

// ── T6: _rerenderReposView 导出到 app 对象 ──
function testRerenderReposViewExported() {
  assert.ok(APP_JS.indexOf('_rerenderReposView') >= 0,
    '_rerenderReposView should be exported in app return');
  // 在 return 语句中
  const returnIdx = APP_JS.indexOf('return {nav, search');
  const returnLine = APP_JS.slice(returnIdx, returnIdx + 500);
  assert.ok(returnLine.indexOf('_rerenderReposView') >= 0,
    '_rerenderReposView should be in app return statement');
  console.log('  PASS: T6 _rerenderReposView 导出到 app 对象');
}

// ── T7: i18n.js _rerenderCurrentPage 对 repos 走 _rerenderReposView ──
function testI18nRerenderReposView() {
  assert.ok(I18N_JS.indexOf('_rerenderReposView') >= 0,
    'i18n.js _rerenderCurrentPage should reference _rerenderReposView');
  // 在 _rerenderCurrentPage 函数体内
  const funcIdx = I18N_JS.indexOf('function _rerenderCurrentPage');
  const funcBody = I18N_JS.slice(funcIdx);
  assert.ok(funcBody.indexOf("view === 'repos'") >= 0 && funcBody.indexOf('_rerenderReposView') >= 0,
    '_rerenderCurrentPage should have repos branch calling _rerenderReposView');
  console.log('  PASS: T7 i18n.js _rerenderCurrentPage 对 repos 走 _rerenderReposView');
}

// ── T8: i18n.js repos 分支在 app.nav fallback 之前 ──
function testReposBranchBeforeNavFallback() {
  const funcIdx = I18N_JS.indexOf('function _rerenderCurrentPage');
  const funcBody = I18N_JS.slice(funcIdx);
  const reposIdx = funcBody.indexOf("view === 'repos'");
  const navIdx = funcBody.indexOf('app.nav(view, param)');
  assert.ok(reposIdx >= 0 && navIdx >= 0 && reposIdx < navIdx,
    'repos branch should come before app.nav fallback');
  console.log('  PASS: T8 i18n.js repos 分支在 app.nav fallback 之前');
}

// ── T9: index.html 版本号 bump ──
function testVersionBumped() {
  assert.ok(INDEX_HTML.indexOf('i18n.js?v=20261020') >= 0,
    'index.html should bump i18n.js version');
  assert.ok(INDEX_HTML.indexOf('app.js?v=179') >= 0,
    'index.html should bump app.js version to 179');
  console.log('  PASS: T9 index.html 版本号 bump');
}

// ── T10: _rerenderReposView 未绑定时走引导页 ──
function testUnboundGoesToBindingGuide() {
  const funcIdx = APP_JS.indexOf('function _rerenderReposView');
  const funcEnd = APP_JS.indexOf('\n  }', funcIdx);
  const funcBody = APP_JS.slice(funcIdx, funcEnd);
  assert.ok(funcBody.indexOf('_renderBindingGuide') >= 0,
    '_rerenderReposView should call _renderBindingGuide when unbound');
  console.log('  PASS: T10 _rerenderReposView 未绑定时走引导页');
}

// ── T11: _rerenderReposView 无缓存时 fallback 到 goToRepos ──
function testFallbackToGoToRepos() {
  const funcIdx = APP_JS.indexOf('function _rerenderReposView');
  const funcEnd = APP_JS.indexOf('\n  }', funcIdx);
  const funcBody = APP_JS.slice(funcIdx, funcEnd);
  assert.ok(funcBody.indexOf('goToRepos()') >= 0,
    '_rerenderReposView should fallback to goToRepos when no cached data');
  console.log('  PASS: T11 _rerenderReposView 无缓存时 fallback 到 goToRepos');
}

// ── 运行所有测试 ──
console.log('i18n repos local refresh tests (11 suites)');
testCachedReposDataExists();
testRerenderReposViewExists();
testRerenderReposViewNoAPI();
testRerenderReposViewNoLoadingOverlay();
testRerenderReposViewCallsRender();
testRerenderReposViewExported();
testI18nRerenderReposView();
testReposBranchBeforeNavFallback();
testVersionBumped();
testUnboundGoesToBindingGuide();
testFallbackToGoToRepos();
console.log('\n11 passed, 0 failed');
