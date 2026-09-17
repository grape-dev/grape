/*
 * Issue #187 回归测试：工作台切 tab 记忆用户上次选择的仓库
 *
 * 运行：node tests/js/test_issue_187_workspace_repo_restore.js
 *
 * 验证点（switchSidebarTab('workspace') 三条分支）：
 *   1. 命中：currentRepo 存在且在 hosted 列表中 → 恢复该仓库（非首位）
 *   2. 未命中：无 currentRepo → 降级选 hosted[0]（首次登录）
 *   3. 失效降级：currentRepo 存在但已不在 hosted 列表（解除托管）→ 降级 hosted[0]，不报错
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

function makeDom() {
  const elements = {};
  const ids = ['pageContent', 'sidebar', 'mainView', 'tb', 'pageTitle', 'ptContent'];
  for (const id of ids) {
    elements[id] = {
      _id: id,
      classList: { _set: new Set(), add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); }, toggle(c, f) { if (f === undefined) f = !this._set.has(c); f ? this._set.add(c) : this._set.delete(c); }, contains(c) { return this._set.has(c); } },
      style: {},
      innerHTML: '',
      textContent: '',
      value: '',
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
      closest: () => null,
    };
  }
  const dom = {
    getElementById(id) { return elements[id] || null; },
    querySelector: () => null,
    querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
    createElement: () => ({ style: {}, classList: elements['pageContent'].classList, textContent: '', innerHTML: '', appendChild: () => {}, remove() {}, querySelector: () => null }),
    body: { appendChild: () => {} },
    documentElement: { setAttribute() {}, removeAttribute() {} },
    addEventListener: () => {},
  };
  return { dom, elements };
}

function makeLocalStorage() {
  const store = {};
  return {
    _data: store,
    getItem(k) { return store[k] ?? null; },
    setItem(k, v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
    clear() { for (const k of Object.keys(store)) delete store[k]; },
  };
}

const reposJsPath = path.resolve(__dirname, '../../src/api/static/js/pages/repos.js');
const src = fs.readFileSync(reposJsPath, 'utf8');

function loadReposModule(dom, localStorage, apiMock) {
  const window = { _currentView: '', addEventListener: () => {} };
  const I18N = { t: (k) => k, getLang: () => 'zh' };
  const app = {
    initSidebarRepos: () => {},
    nav: () => {},
    loadProfile: () => Promise.resolve(),
    showToast: () => {},
    logout: () => {},
  };
  const console_ = { log: () => {}, error: () => {}, warn: () => {} };
  const sandbox = {
    document: dom,
    window,
    localStorage,
    API: apiMock,
    I18N,
    app,
    console: console_,
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math, encodeURIComponent, decodeURIComponent,
    URLSearchParams,
  };
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  const fn = new Function(...keys, src + '\n; return { switchSidebarTab: typeof switchSidebarTab !== "undefined" ? switchSidebarTab : null };');
  return fn(...vals);
}

const HOSTED = [
  { full_name: 'owner/repo-A', owner: 'owner', name: 'repo-A', repo_key: 'gitcode:owner/repo-A', is_hosted: true },
  { full_name: 'owner/repo-B', owner: 'owner', name: 'repo-B', repo_key: 'gitcode:owner/repo-B', is_hosted: true },
  { full_name: 'owner/repo-C', owner: 'owner', name: 'repo-C', repo_key: 'gitcode:owner/repo-C', is_hosted: true },
];

function apiReturning(repos) {
  return { get: () => Promise.resolve({ repos }) };
}

const flush = () => new Promise(r => setTimeout(r, 0));

let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + (e.stack || e.message)); }
}

console.log('Issue #187: 工作台切 tab 记忆仓库 — 回归测试\n');

(async () => {

  await test('命中：currentRepo 在 hosted 列表 → 恢复该仓库（非首位 repo-C）', async () => {
    const { dom } = makeDom();
    const ls = makeLocalStorage();
    ls.setItem('currentRepo', JSON.stringify({ fullName: 'owner/repo-C', owner: 'owner', name: 'repo-C', repoKey: 'gitcode:owner/repo-C' }));
    const mod = loadReposModule(dom, ls, apiReturning(HOSTED));
    mod.switchSidebarTab('workspace');
    await flush();
    const after = JSON.parse(ls.getItem('currentRepo'));
    assert.strictEqual(after.fullName, 'owner/repo-C',
      '切回工作台应恢复 repo-C，实际恢复为 ' + after.fullName);
  });

  await test('未命中：无 currentRepo → 降级选 hosted[0]（首次登录）', async () => {
    const { dom } = makeDom();
    const ls = makeLocalStorage();
    ls.removeItem('currentRepo');
    const mod = loadReposModule(dom, ls, apiReturning(HOSTED));
    mod.switchSidebarTab('workspace');
    await flush();
    const after = JSON.parse(ls.getItem('currentRepo'));
    assert.ok(after, '首次登录应设置 currentRepo');
    assert.strictEqual(after.fullName, 'owner/repo-A', '首次登录应选 hosted[0]=repo-A');
  });

  await test('失效降级：currentRepo 已不在 hosted 列表（解除托管）→ 降级 hosted[0]，不报错', async () => {
    const { dom } = makeDom();
    const ls = makeLocalStorage();
    ls.setItem('currentRepo', JSON.stringify({ fullName: 'owner/gone-repo', owner: 'owner', name: 'gone-repo', repoKey: 'gitcode:owner/gone-repo' }));
    const mod = loadReposModule(dom, ls, apiReturning(HOSTED));
    mod.switchSidebarTab('workspace');
    await flush();
    const after = JSON.parse(ls.getItem('currentRepo'));
    assert.strictEqual(after.fullName, 'owner/repo-A',
      '上次仓库已解除托管应降级 repo-A，实际为 ' + after.fullName);
  });

  await test('场景：选中 repo-B 后离开工作台再切回 → 恢复 repo-B', async () => {
    const { dom } = makeDom();
    const ls = makeLocalStorage();
    const mod = loadReposModule(dom, ls, apiReturning(HOSTED));
    ls.removeItem('currentRepo');
    mod.switchSidebarTab('workspace');
    await flush();
    assert.strictEqual(JSON.parse(ls.getItem('currentRepo')).fullName, 'owner/repo-A', '首次进入应选 repo-A');
    ls.setItem('currentRepo', JSON.stringify({ fullName: 'owner/repo-B', owner: 'owner', name: 'repo-B', repoKey: 'gitcode:owner/repo-B' }));
    mod.switchSidebarTab('workspace');
    await flush();
    assert.strictEqual(JSON.parse(ls.getItem('currentRepo')).fullName, 'owner/repo-B',
      '切回工作台应恢复 repo-B');
  });

  await test('空 hosted 列表 → 走空态渲染，不调用 selectRepo（currentRepo 不变）', async () => {
    const { dom } = makeDom();
    const ls = makeLocalStorage();
    ls.setItem('currentRepo', JSON.stringify({ fullName: 'owner/old', owner: 'owner', name: 'old', repoKey: '' }));
    const mod = loadReposModule(dom, ls, apiReturning([]));
    mod.switchSidebarTab('workspace');
    await flush();
    const after = JSON.parse(ls.getItem('currentRepo'));
    assert.strictEqual(after.fullName, 'owner/old', '无托管仓库时不应覆写 currentRepo');
    assert.ok(dom.getElementById('pageContent').innerHTML.indexOf('ws-empty') !== -1,
      '应渲染空态引导');
  });

  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed > 0 ? 1 : 0);
})();
