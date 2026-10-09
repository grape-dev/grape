/*
 * Issue #278 回归测试：刷新后保持当前页面路由
 *
 * 运行：node tests/js/test_issue_278_refresh_route.js
 *
 * 验证点：
 *   1. renderReposFromData 在 hash 指向有效子页面时导航到该页（而非无条件跳 dash）
 *   2. renderReposFromData 在无 hash 时回退默认 dash
 *   3. renderReposFromData 在 hash 指向无效视图时回退 dash
 *   4. renderReposFromData 带 param 的 hash 路由正确恢复（如 #event/123）
 *   5. init() 在 _currentView 已设时跳过重复 hash 导航（避免二次渲染）
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

function loadApp(dom, localStorage, opts) {
  opts = opts || {};
  var navLog = [];
  var win = {
    _currentView: '',
    _currentParam: null,
    innerWidth: 1280,
    history: {
      _state: null,
      pushState(s, t, u){ this._state = s; if (u !== undefined) { try { win.location.hash = u; } catch(e){} } },
      replaceState(s, t, u){ this._state = s; if (u !== undefined) { try { win.location.hash = u; } catch(e){} } },
      get state() { return this._state; },
    },
    location: { hash: opts.hash || '' },
    addEventListener:()=>{},
    dispatchEvent:()=>true,
    CustomEvent: function(){},
  };
  var API = { get: () => Promise.resolve({repos:[]}), post: () => Promise.resolve({}), scheduleProactiveRefresh: () => {} };
  var I18N = { t: (k) => k, getLang: () => 'zh' };
  var console_ = { log:()=>{}, error:()=>{}, warn:()=>{} };
  // 本地版 app.js（Issue #231）将 currentRepo 存于 sessionStorage——远程测试未 mock，
  // 此处补独立 store，使该测试兼容本地代码
  var sessionStorageMock = {
    _s: {},
    getItem(k) { return (k in this._s) ? this._s[k] : null; },
    setItem(k,v) { this._s[k] = String(v); },
    removeItem(k) { delete this._s[k]; },
    clear() { this._s = {}; },
  };

  var sandbox = {
    document: dom,
    window: win,
    localStorage,
    sessionStorage: sessionStorageMock,
    API,
    I18N,
    console: console_,
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}) }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math, encodeURIComponent, decodeURIComponent,
    URLSearchParams,
    CustomEvent: function(){},
    AbortController: function(){ this.abort=()=>{}; this.signal={}; },
    // 外部渲染函数 mock（定义在其他 JS 文件，app.js nav() 会调用）
    renderSceneDetailLocal: function(){},
    renderSceneDetail: function(){},
    renderSceneMarket: function(){},
    renderAutopilot: function(){},
    renderAutopilotDetail: function(){},
    renderSkillDetail: function(){},
  };
  var keys = Object.keys(sandbox);
  var vals = keys.map(function(k){ return sandbox[k]; });
  var fn = new Function(...keys, src + '\n; return (typeof app !== "undefined") ? app : null;');
  var app = fn(...vals);

  // 包装 app.nav 以记录调用
  if (app && app.nav) {
    var origNav = app.nav;
    app.nav = function(view, param) {
      navLog.push({view: view, param: param});
      return origNav.call(this, view, param);
    };
  }

  return { app: app, window: win, localStorage: localStorage, navLog: navLog };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch(e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

console.log('Issue #278: 刷新后保持当前页面路由 — 回归测试\n');

// ─── 测试 1：hash 指向有效子页面 → 导航到该页，不跳 dash ───
test('renderReposFromData: hash=#coding → 导航到 coding（非 dash）', () => {
  var { dom, localStorage: ls } = makeDom();
  ls.setItem('currentRepo', JSON.stringify({fullName:'o/r', owner:'o', name:'r'}));
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [{full_name:'o/r', owner:'o', name:'r', is_joined:true}],
    ts: Date.now()
  }));
  var { app, window: win, navLog } = loadApp(dom, ls, { hash: '#coding' });
  // _currentView 为空 → loadRepos cachedRepos 路径走 renderReposFromData
  app.loadRepos(false);

  assert.strictEqual(win._currentView, 'coding',
    'hash=#coding 时 _currentView 应为 coding，实际为 ' + win._currentView);
  assert.strictEqual(win.location.hash, '#coding',
    'hash 应保持 #coding 不被覆写为 #dash，实际为 ' + win.location.hash);
});

// ─── 测试 2：无 hash → 回退默认 dash ───
test('renderReposFromData: 无 hash → 回退默认 dash', () => {
  var { dom, localStorage: ls } = makeDom();
  ls.setItem('currentRepo', JSON.stringify({fullName:'o/r', owner:'o', name:'r'}));
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [{full_name:'o/r', owner:'o', name:'r', is_joined:true}],
    ts: Date.now()
  }));
  var { app, window: win, navLog } = loadApp(dom, ls, { hash: '' });
  app.loadRepos(false);

  assert.strictEqual(win._currentView, 'dash',
    '无 hash 时应回退 dash，实际为 ' + win._currentView);
});

// ─── 测试 3：hash 指向无效视图 → 回退 dash ───
test('renderReposFromData: hash=#invalidview → 回退 dash', () => {
  var { dom, localStorage: ls } = makeDom();
  ls.setItem('currentRepo', JSON.stringify({fullName:'o/r', owner:'o', name:'r'}));
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [{full_name:'o/r', owner:'o', name:'r', is_joined:true}],
    ts: Date.now()
  }));
  var { app, window: win, navLog } = loadApp(dom, ls, { hash: '#invalidview' });
  app.loadRepos(false);

  assert.strictEqual(win._currentView, 'dash',
    'hash=#invalidview 时应回退 dash，实际为 ' + win._currentView);
});

// ─── 测试 4：带 param 的 hash 路由正确恢复 ───
test('renderReposFromData: hash=#event/123 → 导航到 event，param=123', () => {
  var { dom, localStorage: ls } = makeDom();
  ls.setItem('currentRepo', JSON.stringify({fullName:'o/r', owner:'o', name:'r'}));
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [{full_name:'o/r', owner:'o', name:'r', is_joined:true}],
    ts: Date.now()
  }));
  var { app, window: win, navLog } = loadApp(dom, ls, { hash: '#event/123' });
  app.loadRepos(false);

  assert.strictEqual(win._currentView, 'event',
    'hash=#event/123 时 _currentView 应为 event，实际为 ' + win._currentView);
  assert.strictEqual(String(win._currentParam), '123',
    '_currentParam 应为 123，实际为 ' + win._currentParam);
});

// ─── 测试 5：_currentView 已设时 init 不重复导航 ───
test('init: _currentView 已设 → 跳过重复 hash 导航', () => {
  var { dom, localStorage: ls } = makeDom();
  ls.setItem('token', 'test-token');
  ls.setItem('currentRepo', JSON.stringify({fullName:'o/r', owner:'o', name:'r'}));
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [{full_name:'o/r', owner:'o', name:'r', is_joined:true}],
    ts: Date.now()
  }));
  ls.setItem('app_cache_ver', '32');
  var { app, window: win, navLog } = loadApp(dom, ls, { hash: '#coding' });

  // 模拟 renderReposFromData 已通过 loadRepos 恢复了路由
  // （cachedRepos 同步路径 → renderReposFromData → nav('coding') → _currentView='coding'）
  app.loadRepos(false);
  var navCountAfterLoad = navLog.length;
  assert.strictEqual(win._currentView, 'coding', 'loadRepos 后 _currentView 应为 coding');

  // 调用 init()：此时 _currentView 已设，应跳过重复 hash 导航
  app.init();
  assert.strictEqual(navLog.length, navCountAfterLoad,
    'init() 在 _currentView 已设时不应再调用 nav，实际多调了 ' + (navLog.length - navCountAfterLoad) + ' 次');
  assert.strictEqual(win._currentView, 'coding',
    'init() 后 _currentView 应仍为 coding，实际为 ' + win._currentView);
});

// ─── 测试 6：刷新场景端到端——hash 不被覆写 ───
test('场景: 用户在 coding 页刷新 → URL hash 保持 #coding 不变为 #dash', () => {
  var { dom, localStorage: ls } = makeDom();
  ls.setItem('currentRepo', JSON.stringify({fullName:'o/r', owner:'o', name:'r'}));
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [{full_name:'o/r', owner:'o', name:'r', is_joined:true}],
    ts: Date.now()
  }));
  var { app, window: win, navLog } = loadApp(dom, ls, { hash: '#coding' });

  // 刷新触发 loadRepos（cachedRepos 同步路径）
  app.loadRepos(false);

  // hash 应仍为 #coding（不被 replaceState 改为 #dash）
  assert.strictEqual(win.location.hash, '#coding',
    '刷新后 hash 应保持 #coding，实际为 ' + win.location.hash);
  assert.strictEqual(win._currentView, 'coding',
    '刷新后 _currentView 应为 coding，实际为 ' + win._currentView);
});

// ─── 测试 7：不在 VIEWS 数组但在 nav() 中有渲染分支的视图也能恢复 ───
test('renderReposFromData: hash=#scene-detail-local/abc → 导航到 scene-detail-local', () => {
  var { dom, localStorage: ls } = makeDom();
  ls.setItem('currentRepo', JSON.stringify({fullName:'o/r', owner:'o', name:'r'}));
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [{full_name:'o/r', owner:'o', name:'r', is_joined:true}],
    ts: Date.now()
  }));
  var { app, window: win, navLog } = loadApp(dom, ls, { hash: '#scene-detail-local/abc' });
  app.loadRepos(false);
  assert.strictEqual(win._currentView, 'scene-detail-local',
    'hash=#scene-detail-local/abc 时 _currentView 应为 scene-detail-local，实际为 ' + win._currentView);
});

// ─── 回归：Issue #82 原有行为不受影响 ───
test('回归 #82: currentRepo 存在但不在列表中 → 保留用户选择不覆写', () => {
  var { dom, localStorage: ls } = makeDom();
  ls.setItem('currentRepo', JSON.stringify({fullName:'owner/repo-B', owner:'owner', name:'repo-B'}));
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [
      {full_name:'owner/repo-A', owner:'owner', name:'repo-A', is_joined:true},
      {full_name:'owner/repo-B', owner:'owner', name:'repo-B', is_joined:true},
    ],
    ts: Date.now()
  }));
  var { app } = loadApp(dom, ls, { hash: '' });
  app.loadRepos(false);
  var after = JSON.parse(ls.getItem('currentRepo'));
  assert.strictEqual(after.fullName, 'owner/repo-B',
    '刷新后 currentRepo 应保持 repo-B，实际为 ' + after.fullName);
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
