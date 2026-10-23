/*
 * 仓库状态一致性修复 — 回归测试
 *
 * 运行：node tests/js/test_unbind_repo_list_invisible.js
 *
 * 对应需求文档：unbind-repo-list-invisible-requirements.md
 * 对应设计文档：unbind-repo-list-invisible-design.md
 *
 * 验证点（39 个）：
 *   T01-T17: R1/R2/R3 解绑平台状态清理
 *   T18-T19: R4 解除托管清 repo_member_cache
 *   T20:     R5 组织切换清 _cachedReposData
 *   T21-T23: R6 issues.js 补全 repoKey
 *   T24:     R7 _forceSyncRepos 清 _cachedReposData
 *   T25:     R2 解绑后调 loadProfile 刷新角色
 *   T26-T27: R8 多标签页 storage 事件同步
 *   T28-T29: R9 feedback.js 修复
 *   T30:     R8 边界：repoKey 无前缀时不误清理
 *   T31-T37: R10 index.html 版本号递增（7 个改动文件）
 *   T38-T39: R10 未修改文件版本号不变（dashboard.js + pipeline.js）
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..', '..');
const ACCOUNT_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/account.js'), 'utf8');
const APP_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/app.js'), 'utf8');
const API_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/api.js'), 'utf8');
const REPOS_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/repos.js'), 'utf8');
const SETTINGS_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/settings.js'), 'utf8');
const ISSUES_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/issues.js'), 'utf8');
const FEEDBACK_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/feedback.js'), 'utf8');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('仓库状态一致性修复 — 回归测试\n');

// ── 辅助函数 ──

function extractFn(src, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{');
  const m = re.exec(src);
  if (!m) return null;
  let start = m.index + m[0].length;
  let depth = 1;
  let i = start;
  while (i < src.length && depth > 0) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(m.index, i);
}

function getUnbindProviderFn() {
  const fn = extractFn(ACCOUNT_JS, '_accountUnbindProvider');
  assert.ok(fn, '_accountUnbindProvider 函数应存在');
  return fn;
}

// ── T01: account.js 存在 _accountCleanupAfterUnbind 函数 ──
test('T01: account.js 存在 _accountCleanupAfterUnbind 函数', () => {
  const fn = extractFn(ACCOUNT_JS, '_accountCleanupAfterUnbind');
  assert.ok(fn, '_accountCleanupAfterUnbind 函数应存在于 account.js');
});

// ── T02: 普通解绑路径成功回调调用 _accountOnUnbindSuccess（内含 _accountCleanupAfterUnbind） ──
test('T02: 普通解绑路径调用了 _accountOnUnbindSuccess', () => {
  const fn = getUnbindProviderFn();
  const idx = fn.indexOf("API.del(url).then");
  assert.ok(idx >= 0);
  const body = fn.slice(idx, idx + 300);
  assert.ok(body.indexOf('_accountOnUnbindSuccess') >= 0, '普通解绑路径应调 _accountOnUnbindSuccess');
  const successFn = extractFn(ACCOUNT_JS, '_accountOnUnbindSuccess');
  assert.ok(successFn && successFn.indexOf('_accountCleanupAfterUnbind') >= 0,
    '_accountOnUnbindSuccess 内部应调 _accountCleanupAfterUnbind');
});

// ── T03: 确认后解绑路径成功回调调用 _accountOnUnbindSuccess ──
test('T03: 确认后解绑路径调用了 _accountOnUnbindSuccess', () => {
  const fn = getUnbindProviderFn();
  const idx = fn.indexOf("?confirm=true");
  assert.ok(idx >= 0);
  const body = fn.slice(idx, idx + 300);
  assert.ok(body.indexOf('_accountOnUnbindSuccess') >= 0, '确认后解绑路径应调 _accountOnUnbindSuccess');
});

// ── T04: _accountCleanupAfterUnbind 检查 repoKey 前缀匹配 ──
test('T04: _accountCleanupAfterUnbind 检查 currentRepo.repoKey 前缀匹配', () => {
  const fn = extractFn(ACCOUNT_JS, '_accountCleanupAfterUnbind');
  assert.ok(fn);
  assert.ok(/provider\s*\+\s*['"]:/.test(fn), '应使用 provider + ":" 前缀匹配');
  assert.ok(/sessionStorage.*currentRepo/.test(fn), '应读取 sessionStorage currentRepo');
});

// ── 行为模拟 ──
function runCleanup(provider, currentRepoValue) {
  const calls = { removedCurrentRepo: false, goToReposCalled: false, clearCachedCalled: false, loadProfileCalled: false };
  const sessionStorage = {
    _data: {},
    getItem(k) { return this._data[k] || null; },
    setItem(k, v) { this._data[k] = v; },
    removeItem(k) { delete this._data[k]; if (k === 'currentRepo') calls.removedCurrentRepo = true; },
  };
  if (currentRepoValue !== null) sessionStorage.setItem('currentRepo', JSON.stringify(currentRepoValue));
  const app = {
    goToRepos: () => { calls.goToReposCalled = true; },
    _clearCachedReposData: () => { calls.clearCachedCalled = true; },
    loadProfile: () => { calls.loadProfileCalled = true; return Promise.resolve(); },
  };
  const fnText = extractFn(ACCOUNT_JS, '_accountCleanupAfterUnbind');
  if (!fnText) throw new Error('_accountCleanupAfterUnbind 源码未找到');
  const bodyStart = fnText.indexOf('{') + 1;
  const bodyEnd = fnText.lastIndexOf('}');
  const fnBody = fnText.slice(bodyStart, bodyEnd);
  try { new Function('provider', 'sessionStorage', 'app', fnBody)(provider, sessionStorage, app); }
  catch(e) { throw new Error('执行出错: ' + e.message); }
  return calls;
}

// ── T05: repoKey 匹配时清除 ──
test('T05: repoKey 匹配被解绑平台时清除 sessionStorage.currentRepo', () => {
  const c1 = runCleanup('github', {repoKey: 'github:octocat/my-repo', fullName: 'octocat/my-repo'});
  assert.ok(c1.removedCurrentRepo, 'GitHub 匹配时应清除');
  const c2 = runCleanup('gitcode', {repoKey: 'gitcode:dev/project', fullName: 'dev/project'});
  assert.ok(c2.removedCurrentRepo, 'GitCode 匹配时应清除');
});

// ── T06: repoKey 不匹配时不清除（两个方向都测） ──
test('T06: repoKey 不匹配被解绑平台时不清除（GitHub↔GitCode 对称）', () => {
  // 选中 GitCode 仓库 + 解绑 github → 不清理
  const c1 = runCleanup('github', {repoKey: 'gitcode:dev/project', fullName: 'dev/project'});
  assert.ok(!c1.removedCurrentRepo, 'GitCode 仓库 + 解绑 github 不应清除');
  assert.ok(!c1.goToReposCalled, '不匹配时不应调 goToRepos');
  // 选中 GitHub 仓库 + 解绑 gitcode → 不清理
  const c2 = runCleanup('gitcode', {repoKey: 'github:octocat/my-repo', fullName: 'octocat/my-repo'});
  assert.ok(!c2.removedCurrentRepo, 'GitHub 仓库 + 解绑 gitcode 不应清除');
  assert.ok(!c2.goToReposCalled, '不匹配时不应调 goToRepos');
});

// ── T07: null 时不报错 ──
test('T07: currentRepo 为 null 时不报错', () => {
  const c = runCleanup('github', null);
  assert.ok(!c.removedCurrentRepo);
  assert.ok(!c.goToReposCalled);
});

// ── T08: 清除后调 goToRepos（两个平台都测） ──
test('T08: 清除 currentRepo 后调 goToRepos', () => {
  const c1 = runCleanup('github', {repoKey: 'github:octocat/my-repo', fullName: 'octocat/my-repo'});
  assert.ok(c1.goToReposCalled, 'GitHub 清除后应调 goToRepos');
  const c2 = runCleanup('gitcode', {repoKey: 'gitcode:dev/project', fullName: 'dev/project'});
  assert.ok(c2.goToReposCalled, 'GitCode 清除后应调 goToRepos');
});

// ── T09: 未清除时不调 goToRepos（两个方向都测） ──
test('T09: 未清除 currentRepo 时不调 goToRepos（GitHub↔GitCode 对称）', () => {
  const c1 = runCleanup('github', {repoKey: 'gitcode:dev/project', fullName: 'dev/project'});
  assert.ok(!c1.goToReposCalled, 'GitCode 仓库 + 解绑 github 不应调');
  const c2 = runCleanup('gitcode', {repoKey: 'github:octocat/my-repo', fullName: 'octocat/my-repo'});
  assert.ok(!c2.goToReposCalled, 'GitHub 仓库 + 解绑 gitcode 不应调');
});

// ── T10: 无条件调 _clearCachedReposData（两个平台 + null 都测） ──
test('T10: 无条件调 _clearCachedReposData', () => {
  assert.ok(runCleanup('github', {repoKey: 'github:o/r'}).clearCachedCalled, 'github 匹配时');
  assert.ok(runCleanup('gitcode', {repoKey: 'gitcode:d/p'}).clearCachedCalled, 'gitcode 匹配时');
  assert.ok(runCleanup('github', {repoKey: 'gitcode:d/p'}).clearCachedCalled, 'github 不匹配时');
  assert.ok(runCleanup('gitcode', {repoKey: 'github:o/r'}).clearCachedCalled, 'gitcode 不匹配时');
  assert.ok(runCleanup('github', null).clearCachedCalled, 'github null 时');
  assert.ok(runCleanup('gitcode', null).clearCachedCalled, 'gitcode null 时');
});

// ── T11: app.js 存在 _clearCachedReposData ──
test('T11: app.js 存在 _clearCachedReposData 函数', () => {
  const fn = extractFn(APP_JS, '_clearCachedReposData');
  assert.ok(fn);
  assert.ok(/_cachedReposData\s*=\s*null/.test(fn), '应置 null');
});

// ── T12: return 导出 ──
test('T12: app.js return 导出 _clearCachedReposData', () => {
  const idx = APP_JS.lastIndexOf('return {');
  assert.ok(idx >= 0);
  assert.ok(APP_JS.slice(idx, idx + 1000).indexOf('_clearCachedReposData') >= 0);
});

// ── T13: _clearCachedReposData 置 null ──
test('T13: _clearCachedReposData 将 _cachedReposData 置 null', () => {
  const fn = extractFn(APP_JS, '_clearCachedReposData');
  assert.ok(fn);
  const body = fn.slice(fn.indexOf('{') + 1, fn.lastIndexOf('}'));
  assert.ok(/_cachedReposData\s*=\s*null/.test(body));
});

// ── T14: _accountOnUnbindSuccess 中 renderAccount 有 _currentView 守卫 ──
test('T14: 普通解绑路径 renderAccount 有 _currentView 守卫', () => {
  const fn = extractFn(ACCOUNT_JS, '_accountOnUnbindSuccess');
  assert.ok(fn, '_accountOnUnbindSuccess 函数应存在');
  assert.ok(/_currentView.*!==.*['"]repos['"]/.test(fn), '应有守卫');
});

// ── T15: 确认后解绑路径共用 _accountOnUnbindSuccess，同样有守卫 ──
test('T15: 确认后解绑路径 renderAccount 有 _currentView 守卫', () => {
  const fn = getUnbindProviderFn();
  const idx = fn.indexOf("?confirm=true");
  assert.ok(idx >= 0);
  const body = fn.slice(idx, idx + 300);
  assert.ok(body.indexOf('_accountOnUnbindSuccess') >= 0, '确认后路径应调 _accountOnUnbindSuccess');
  const successFn = extractFn(ACCOUNT_JS, '_accountOnUnbindSuccess');
  assert.ok(successFn && /_currentView.*!==.*['"]repos['"]/.test(successFn), '应有守卫');
});

// ── T16: _accountOnUnbindSuccess 中 _accountCleanupAfterUnbind 在 removeItem 之后 ──
test('T16: _accountCleanupAfterUnbind 在 repo_member_cache 清除之后调用', () => {
  const fn = extractFn(ACCOUNT_JS, '_accountOnUnbindSuccess');
  assert.ok(fn);
  const cacheIdx = fn.indexOf("removeItem('repo_member_cache')");
  const cleanupIdx = fn.indexOf('_accountCleanupAfterUnbind');
  assert.ok(cacheIdx >= 0 && cleanupIdx >= 0 && cacheIdx < cleanupIdx, '应在之后调用');
});

// ── T17: _accountOnUnbindSuccess 中 _accountCleanupAfterUnbind 在 renderAccount 之前 ──
test('T17: _accountCleanupAfterUnbind 在 renderAccount 之前调用', () => {
  const fn = extractFn(ACCOUNT_JS, '_accountOnUnbindSuccess');
  assert.ok(fn);
  const cleanupIdx = fn.indexOf('_accountCleanupAfterUnbind');
  const renderIdx = fn.indexOf('renderAccount');
  assert.ok(cleanupIdx >= 0 && renderIdx >= 0 && cleanupIdx < renderIdx, '应在之前调用');
});

// ── T18: repos.js _doUnhost 清 repo_member_cache（R4） ──
test('T18: repos.js _doUnhost 清除 localStorage.repo_member_cache（R4）', () => {
  const fn = extractFn(REPOS_JS, '_doUnhost');
  assert.ok(fn);
  assert.ok(/localStorage\.removeItem\(['"]repo_member_cache['"]\)/.test(fn), '应清除 repo_member_cache');
});

// ── T19: settings.js unhostFromSettings 清 repo_member_cache（R4） ──
test('T19: settings.js unhostFromSettings 清除 repo_member_cache（R4）', () => {
  const fn = extractFn(SETTINGS_JS, 'unhostFromSettings');
  assert.ok(fn);
  assert.ok(/localStorage\.removeItem\(['"]repo_member_cache['"]\)/.test(fn), '应清除 repo_member_cache');
});

// ── T20: app.js switchOrg 清 _cachedReposData（R5） ──
test('T20: app.js switchOrg 清理 _cachedReposData（R5）', () => {
  const fn = extractFn(APP_JS, 'switchOrg');
  assert.ok(fn);
  assert.ok(/_cachedReposData\s*=\s*null/.test(fn) || /_clearCachedReposData/.test(fn), '应清理');
});

// ── T21: issues.js jumpToPipeline 设 repoKey（R6） ──
test('T21: issues.js jumpToPipeline 设置 repoKey 字段（R6）', () => {
  const idx = ISSUES_JS.indexOf('jumpToPipeline');
  assert.ok(idx >= 0);
  const body = ISSUES_JS.slice(idx, idx + 800);
  assert.ok(/repoKey\s*:\s*t\.repo_key/.test(body), '应设 repoKey: t.repo_key');
});

// ── T22: issues.js _jumpToNodeEvent 设 repoKey（R6） ──
test('T22: issues.js _jumpToNodeEvent 设置 repoKey 字段（R6）', () => {
  const idx = ISSUES_JS.indexOf('_jumpToNodeEvent');
  assert.ok(idx >= 0);
  const body = ISSUES_JS.slice(idx, idx + 1000);
  assert.ok(/repoKey\s*:\s*t\.repo_key/.test(body), '应设 repoKey: t.repo_key');
});

// ── T23: issues.js _jumpToPipePage 设 repoKey（R6） ──
test('T23: issues.js _jumpToPipePage 设置 repoKey 字段（R6）', () => {
  const idx = ISSUES_JS.indexOf('_jumpToPipePage');
  assert.ok(idx >= 0);
  const body = ISSUES_JS.slice(idx, idx + 800);
  assert.ok(/repoKey\s*:\s*t\.repo_key/.test(body), '应设 repoKey: t.repo_key');
});

// ── T24: app.js _forceSyncRepos 清 _cachedReposData（R7） ──
test('T24: app.js _forceSyncRepos 清理 _cachedReposData（R7）', () => {
  const fn = extractFn(APP_JS, '_forceSyncRepos');
  assert.ok(fn);
  assert.ok(/_cachedReposData\s*=\s*null/.test(fn) || /_clearCachedReposData/.test(fn), '应清理');
});

// ── T24b: repos.js refreshRepos 调 _clearCachedReposData（R7） ──
test('T24b: repos.js refreshRepos 调 _clearCachedReposData（R7）', () => {
  const idx = REPOS_JS.indexOf('function refreshRepos');
  assert.ok(idx >= 0, 'refreshRepos 函数应存在');
  const body = REPOS_JS.slice(idx, idx + 600);
  assert.ok(/_clearCachedReposData/.test(body), 'refreshRepos 应调 _clearCachedReposData');
});

// ── T24c: repos.js submitCreateRepo 调 _clearCachedReposData（R7） ──
test('T24c: repos.js submitCreateRepo 调 _clearCachedReposData（R7）', () => {
  const idx = REPOS_JS.indexOf('function submitCreateRepo');
  assert.ok(idx >= 0, 'submitCreateRepo 函数应存在');
  const body = REPOS_JS.slice(idx, idx + 2000);
  assert.ok(/_clearCachedReposData/.test(body), 'submitCreateRepo 应调 _clearCachedReposData');
});

// ── T25: _accountCleanupAfterUnbind 调 loadProfile（R2） ──
test('T25: _accountCleanupAfterUnbind 调 loadProfile 刷新角色（R2）', () => {
  const fn = extractFn(ACCOUNT_JS, '_accountCleanupAfterUnbind');
  assert.ok(fn);
  assert.ok(/loadProfile/.test(fn), '应调 loadProfile');
  // 验证行为模拟中也调用了（两个平台都测）
  const c1 = runCleanup('github', {repoKey: 'github:o/r', fullName: 'o/r'});
  assert.ok(c1.loadProfileCalled, 'github 匹配时应调 loadProfile');
  const c2 = runCleanup('gitcode', {repoKey: 'gitcode:d/p', fullName: 'd/p'});
  assert.ok(c2.loadProfileCalled, 'gitcode 匹配时应调 loadProfile');
  // 不跳转时也调（两个方向都测）
  const c3 = runCleanup('github', {repoKey: 'gitcode:d/p', fullName: 'd/p'});
  assert.ok(c3.loadProfileCalled, 'github 不跳转时也应调 loadProfile');
  const c4 = runCleanup('gitcode', {repoKey: 'github:o/r', fullName: 'o/r'});
  assert.ok(c4.loadProfileCalled, 'gitcode 不跳转时也应调 loadProfile');
});

// ── T26: api.js storage 事件处理 repo_member_cache（R8） ──
test('T26: api.js storage 事件处理 repo_member_cache 键（R8）', () => {
  assert.ok(/repo_member_cache/.test(API_JS), 'api.js 应提及 repo_member_cache');
  // 在 storage 事件监听附近
  const storageIdx = API_JS.indexOf("addEventListener('storage'");
  assert.ok(storageIdx >= 0, '应存在 storage 事件监听');
  const storageBody = API_JS.slice(storageIdx, storageIdx + 2000);
  assert.ok(/repo_member_cache/.test(storageBody), 'storage 事件应处理 repo_member_cache 键');
});

// ── T27: api.js storage 事件处理 user/bindings（R8） ──
test('T27: api.js storage 事件处理 user/bindings 键（R8）', () => {
  const storageIdx = API_JS.indexOf("addEventListener('storage'");
  assert.ok(storageIdx >= 0);
  const storageBody = API_JS.slice(storageIdx, storageIdx + 2000);
  // 应检查 bindings 和 currentRepo 的 provider
  assert.ok(/bindings/.test(storageBody), '应检查 bindings');
  assert.ok(/currentRepo/.test(storageBody) || /sessionStorage.*currentRepo/.test(storageBody), '应检查 currentRepo');
});

// ── T28: feedback.js 使用 app.currentRepo()（R9） ──
test('T28: feedback.js 使用 app.currentRepo()（R9）', () => {
  assert.ok(/app\.currentRepo\(\)/.test(FEEDBACK_JS) || /app\.currentRepo\b/.test(FEEDBACK_JS),
    '应使用 app.currentRepo()');
});

// ── T29: feedback.js 不再使用 window._currentRepo（R9） ──
test('T29: feedback.js 不再使用 window._currentRepo（R9）', () => {
  // 搜索 feedback.js 中是否还有 window._currentRepo 的调用（非注释）
  const lines = FEEDBACK_JS.split('\n');
  const offending = lines.filter(l => /window\._currentRepo/.test(l) && !/\/\//.test(l.trim().substring(0,2)));
  assert.ok(offending.length === 0, '不应再使用 window._currentRepo，发现: ' + offending.join('; '));
});

// ── T30: R8 边界——repoKey 无前缀时不误清理 ──
test('T30: api.js storage 事件中 repoKey 无前缀时不误清理（R8 边界）', () => {
  const storageIdx = API_JS.indexOf("addEventListener('storage'");
  assert.ok(storageIdx >= 0);
  const storageBody = API_JS.slice(storageIdx, storageIdx + 3000);
  // R8 新增的 storage 处理中应有冒号存在性检查，防止 repoKey 无前缀时 split(':')[0] 返回完整 repoKey 导致误判
  // 查找 R8 新增的 repo_member_cache 处理块
  const r8BlockIdx = storageBody.indexOf('repo_member_cache');
  if (r8BlockIdx < 0) {
    // R8 还未实现，预期失败
    assert.ok(false, 'storage 事件中应处理 repo_member_cache 键（R8 未实现）');
  }
  const r8Block = storageBody.slice(r8BlockIdx, r8BlockIdx + 1000);
  // 应有冒号/前缀存在性检查
  assert.ok(
    /indexOf\(['":\['"]\)/.test(r8Block) ||
    /split\(['":\['"]\).*length/.test(r8Block) ||
    /provider.*!==.*repoKey/.test(r8Block) ||
    /_provider.*!==/.test(r8Block),
    'R8 处理块应有冒号/前缀存在性检查防止误清理'
  );
});

// ── R10: index.html 版本号递增验证 ──
const INDEX_HTML = fs.readFileSync(path.join(ROOT, 'src/api/static/index.html'), 'utf8');

// 基线 weekly-08 的 ?v= 值
const BASELINE_V = {
  'api.js': 30,
  'app.js': 184,
  'pages/account.js': 42,
  'pages/feedback.js': 8,
  'pages/issues.js': 117,
  'pages/repos.js': 61,
  'pages/settings.js': 33,
  // 未修改的文件（用于 T38/T39 验证不变）
  'pages/dashboard.js': 98,
  'pages/pipeline.js': 46,
};

// 从 index.html 中提取指定 JS 文件的 ?v= 值
function extractVersion(html, jsPath) {
  // 匹配 src="/static/js/{jsPath}?v=N" 或 src="/static/js/{jsPath}?v=N"
  const re = new RegExp('src="[^"]*' + jsPath.replace(/\./g, '\\.') + '\\?v=(\\d+)"');
  const m = re.exec(html);
  if (!m) return null;
  return parseInt(m[1], 10);
}

// ── T31: api.js ?v= 递增 ──
test('T31: index.html 中 api.js ?v= 值大于基线 30（R10）', () => {
  const v = extractVersion(INDEX_HTML, 'api.js');
  assert.ok(v !== null, 'api.js 应在 index.html 中有 ?v= 版本号');
  assert.ok(v > BASELINE_V['api.js'], 'api.js ?v=' + v + ' 应大于基线 ' + BASELINE_V['api.js']);
});

// ── T32: app.js ?v= 递增 ──
test('T32: index.html 中 app.js ?v= 值大于基线 184（R10）', () => {
  const v = extractVersion(INDEX_HTML, 'app.js');
  assert.ok(v !== null, 'app.js 应在 index.html 中有 ?v= 版本号');
  assert.ok(v > BASELINE_V['app.js'], 'app.js ?v=' + v + ' 应大于基线 ' + BASELINE_V['app.js']);
});

// ── T33: account.js ?v= 递增 ──
test('T33: index.html 中 account.js ?v= 值大于基线 42（R10）', () => {
  const v = extractVersion(INDEX_HTML, 'pages/account.js');
  assert.ok(v !== null, 'account.js 应在 index.html 中有 ?v= 版本号');
  assert.ok(v > BASELINE_V['pages/account.js'], 'account.js ?v=' + v + ' 应大于基线 ' + BASELINE_V['pages/account.js']);
});

// ── T34: feedback.js ?v= 递增 ──
test('T34: index.html 中 feedback.js ?v= 值大于基线 8（R10）', () => {
  const v = extractVersion(INDEX_HTML, 'pages/feedback.js');
  assert.ok(v !== null, 'feedback.js 应在 index.html 中有 ?v= 版本号');
  assert.ok(v > BASELINE_V['pages/feedback.js'], 'feedback.js ?v=' + v + ' 应大于基线 ' + BASELINE_V['pages/feedback.js']);
});

// ── T35: issues.js ?v= 递增 ──
test('T35: index.html 中 issues.js ?v= 值大于基线 117（R10）', () => {
  const v = extractVersion(INDEX_HTML, 'pages/issues.js');
  assert.ok(v !== null, 'issues.js 应在 index.html 中有 ?v= 版本号');
  assert.ok(v > BASELINE_V['pages/issues.js'], 'issues.js ?v=' + v + ' 应大于基线 ' + BASELINE_V['pages/issues.js']);
});

// ── T36: repos.js ?v= 递增 ──
test('T36: index.html 中 repos.js ?v= 值大于基线 61（R10）', () => {
  const v = extractVersion(INDEX_HTML, 'pages/repos.js');
  assert.ok(v !== null, 'repos.js 应在 index.html 中有 ?v= 版本号');
  assert.ok(v > BASELINE_V['pages/repos.js'], 'repos.js ?v=' + v + ' 应大于基线 ' + BASELINE_V['pages/repos.js']);
});

// ── T37: settings.js ?v= 递增 ──
test('T37: index.html 中 settings.js ?v= 值大于基线 33（R10）', () => {
  const v = extractVersion(INDEX_HTML, 'pages/settings.js');
  assert.ok(v !== null, 'settings.js 应在 index.html 中有 ?v= 版本号');
  assert.ok(v > BASELINE_V['pages/settings.js'], 'settings.js ?v=' + v + ' 应大于基线 ' + BASELINE_V['pages/settings.js']);
});

// ── T38: 未修改的 dashboard.js ?v= 不变 ──
test('T38: 未修改的 dashboard.js ?v= 值不变（基线 98，R10 精确性）', () => {
  const v = extractVersion(INDEX_HTML, 'pages/dashboard.js');
  assert.ok(v !== null, 'dashboard.js 应在 index.html 中有 ?v= 版本号');
  assert.ok(v === BASELINE_V['pages/dashboard.js'], 'dashboard.js ?v=' + v + ' 应等于基线 ' + BASELINE_V['pages/dashboard.js'] + '（未修改不应递增）');
});

// ── T39: 未修改的 pipeline.js ?v= 不变 ──
test('T39: 未修改的 pipeline.js ?v= 值不变（基线 46，R10 精确性）', () => {
  const v = extractVersion(INDEX_HTML, 'pages/pipeline.js');
  assert.ok(v !== null, 'pipeline.js 应在 index.html 中有 ?v= 版本号');
  assert.ok(v === BASELINE_V['pages/pipeline.js'], 'pipeline.js ?v=' + v + ' 应等于基线 ' + BASELINE_V['pages/pipeline.js'] + '（未修改不应递增）');
});

// ── 汇总 ──
console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
