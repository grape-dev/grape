/*
 * Issue #82 回归测试：刷新后仓库不跳变
 *
 * 运行：node tests/js/test_repo_persist_on_refresh.js
 *
 * 验证点：
 *   1. renderReposFromData 在 currentRepo 已存在但不在列表中时，不覆写为 managedRepos[0]
 *   2. renderReposFromData 在无 currentRepo 时仍默认选第一个仓库（首次登录）
 *   3. loadRepos cachedRepos 路径有 _currentView 守卫
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

// ── 极简 DOM mock（仅覆盖 app.js 用到的 API）──
function makeDom() {
  const store = {};
  const elements = {};
  const ids = ['loginView','sidebar','mainView','tb','pageTitle','repoSelect','pageContent','repoTitle','welcomeUser','userName','userAvatar','userRole','loginLinks','pwdLogin','gitBtn','loginUser','loginPass','helpOverlay','helpDrawer','gotoReposOverlay','nav-dash'];
  for (const id of ids) {
    elements[id] = {
      _id: id,
      classList: { _set:new Set(), add(c){this._set.add(c);}, remove(c){this._set.delete(c);}, toggle(c,f){ if(f===undefined) f=!this._set.has(c); f?this._set.add(c):this._set.delete(c);}, contains(c){return this._set.has(c);} },
      style: {},
      innerHTML: '',
      textContent: '',
      value: '',
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
      nextElementSibling: null,
      closest: () => null,
    };
  }
  const dom = {
    getElementById(id) { return elements[id] || null; },
    querySelector: () => null,
    querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
    createElement: () => ({ style:{}, classList:elements['loginView'].classList, textContent:'', innerHTML:'', appendChild:()=>{}, remove(){}, querySelector:()=>null }),
    body: { appendChild: () => {} },
    documentElement: { setAttribute(){}, removeAttribute(){} },
    addEventListener: () => {},
  };
  // localStorage mock
  const localStorage = {
    _data: store,
    getItem(k) { return store[k] ?? null; },
    setItem(k,v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
    clear() { for (const k of Object.keys(store)) delete store[k]; },
  };
  return { dom, elements, localStorage };
}

// ── 加载 app.js 源码 ──
const appJsPath = path.resolve(__dirname, '../../src/api/static/js/app.js');
const src = fs.readFileSync(appJsPath, 'utf8');

// app.js 是 IIFE，引用了未定义的全局（API, I18N, fetch, document, window, localStorage, console 等）
// 我们在 sandbox 里提供 mock 后 eval
function loadApp(dom, localStorage) {
  const window = { _currentView: '', history: { pushState(){}, replaceState(){}, state: null }, location: { hash:'' }, addEventListener:()=>{} };
  const API = { get: () => Promise.resolve({repos:[]}), post: () => Promise.resolve({}) };
  const I18N = { t: (k) => k, getLang: () => 'zh' };
  const console_ = { log:()=>{}, error:()=>{}, warn:()=>{} };

  // 用 Function 构造隔离作用域，把 mock 注入为全局
  const sandbox = {
    document: dom,
    window,
    localStorage,
    API,
    I18N,
    console: console_,
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math, encodeURIComponent, decodeURIComponent,
    URLSearchParams,
    CustomEvent: function(){},
    AbortController: function(){ this.abort=()=>{}; this.signal={}; },
  };
  // 把 sandbox 属性挂到 globalThis（Function 构造的函数里 with(this)）
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  // app.js 末尾有 document.addEventListener('DOMContentLoaded'...) 和 keydown，
  // 以及 visibilitychange — 我们的 document mock addEventListener 是 no-op，安全
  const fn = new Function(...keys, src + '\n; return (typeof app !== "undefined") ? app : null;');
  const app = fn(...vals);
  return { app, window, localStorage };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch(e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('Issue #82: 仓库刷新不跳变 — 回归测试\n');

// ─── 测试 1：currentRepo 存在但不在 managedRepos 列表中时，不覆写 ───
test('renderReposFromData: currentRepo 存在但不在列表中 → 保留用户选择', () => {
  const { dom, localStorage: ls } = makeDom();
  // 用户已选中 repo-B（非列表首位）
  ls.setItem('currentRepo', JSON.stringify({fullName:'owner/repo-B', owner:'owner', name:'repo-B'}));
  const { app } = loadApp(dom, ls);

  // managedRepos 列表里有 repo-A 和 repo-B
  // 注意：renderReposFromData 是闭包内私有函数，不通过 app 导出。
  // 我们通过 loadRepos → cachedRepos 路径间接触发（_currentView 为空时走 renderReposFromData）。
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [
      {full_name:'owner/repo-A', owner:'owner', name:'repo-A', is_joined:true},
      {full_name:'owner/repo-B', owner:'owner', name:'repo-B', is_joined:true},
    ],
    ts: Date.now()
  }));
  // _currentView 为空 → loadRepos cachedRepos 路径会调 renderReposFromData
  // 但 renderReposFromData 内部调 initSidebarRepos() → API.get('/repos/managed')，
  // 这里 API mock 返回 {repos:[]}，不阻塞。
  app.loadRepos(false);

  // 刷新后 currentRepo 应仍为 repo-B（未被覆写为 repo-A）
  const after = JSON.parse(ls.getItem('currentRepo'));
  assert.strictEqual(after.fullName, 'owner/repo-B',
    '刷新后 currentRepo 被覆写为 ' + after.fullName + '（预期 owner/repo-B）');
});

// ─── 测试 2：无 currentRepo 时首次登录仍默认选第一个 ───
test('renderReposFromData: 无 currentRepo → 默认选 managedRepos[0]（首次登录）', () => {
  const { dom, localStorage: ls } = makeDom();
  ls.removeItem('currentRepo');
  const { app } = loadApp(dom, ls);
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [
      {full_name:'owner/repo-A', owner:'owner', name:'repo-A', is_joined:true},
      {full_name:'owner/repo-B', owner:'owner', name:'repo-B', is_joined:true},
    ],
    ts: Date.now()
  }));
  app.loadRepos(false);
  const after = JSON.parse(ls.getItem('currentRepo'));
  assert.ok(after, '首次登录应设置 currentRepo');
  assert.strictEqual(after.fullName, 'owner/repo-A', '首次登录应选 managedRepos[0]');
});

// ─── 测试 3：cachedRepos 路径 _currentView 守卫 ───
test('loadRepos cachedRepos 路径: _currentView 已设 → 不调 renderReposFromData（走 initSidebarRepos）', () => {
  const { dom, localStorage: ls } = makeDom();
  ls.setItem('currentRepo', JSON.stringify({fullName:'owner/repo-B', owner:'owner', name:'repo-B'}));
  const ctx = loadApp(dom, ls);
  const { app, window } = ctx;
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [
      {full_name:'owner/repo-A', owner:'owner', name:'repo-A', is_joined:true},
    ],
    ts: Date.now()
  }));
  // 模拟用户已进入某页面
  window._currentView = 'dash';
  // renderReposFromData 会调 nav('dash') → 操作 pageContent 等；
  // 若走了 renderReposFromData，会触发 nav 里的 currentRepo() 检查并可能改 DOM。
  // 关键断言：currentRepo 不被覆写（因为 managedRepos 列表里只有 repo-A，
  // 旧逻辑 renderReposFromData 会因 !some(...) 把 currentRepo 改成 repo-A；
  // 新逻辑 _currentView 守卫走 initSidebarRepos，不碰 currentRepo）
  app.loadRepos(false);
  const after = JSON.parse(ls.getItem('currentRepo'));
  assert.strictEqual(after.fullName, 'owner/repo-B',
    '_currentView 守卫应阻止 renderReposFromData 覆写 currentRepo');
});

// ─── 测试 4：多仓库切换后刷新不跳变（场景级）───
test('场景: 切换到非首位仓库后刷新 → currentRepo 保持不变', () => {
  const { dom, localStorage: ls } = makeDom();
  const { app, window } = loadApp(dom, ls);
  // 缓存里有 3 个仓库
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [
      {full_name:'o/r1', owner:'o', name:'r1', is_joined:true},
      {full_name:'o/r2', owner:'o', name:'r2', is_joined:true},
      {full_name:'o/r3', owner:'o', name:'r3', is_joined:true},
    ],
    ts: Date.now()
  }));
  // 首次：无 currentRepo → 默认选 r1
  ls.removeItem('currentRepo');
  app.loadRepos(false);
  assert.strictEqual(JSON.parse(ls.getItem('currentRepo')).fullName, 'o/r1', '首次应选 r1');

  // 用户切换到 r3（模拟 switchRepo）
  ls.setItem('currentRepo', JSON.stringify({fullName:'o/r3', owner:'o', name:'r3'}));
  // 用户在 dash 页面（_currentView='dash'）→ 刷新页面触发 loadRepos
  window._currentView = 'dash';
  app.loadRepos(false);
  // 刷新后应仍为 r3
  assert.strictEqual(JSON.parse(ls.getItem('currentRepo')).fullName, 'o/r3',
    '刷新后仓库应保持 r3，不跳变为 r1');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
