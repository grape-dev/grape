/*
 * Issue #273 回归测试：/en/ 前缀下 F5 刷新保持当前视图
 *
 * 运行：node tests/js/test_issue_273_en_prefix_refresh.js
 *
 * 场景：生产 URL https://www.grape.example.com/en/app/#settings/credentials
 * F5 刷新后应恢复 settings 视图（而非回到主页）。
 *
 * 验证点：
 *   1. pathname=/en/app/ + hash=#settings/credentials → loadRepos 恢复 settings 视图
 *   2. pathname=/en/app/ + hash=#settings/credentials → init() 恢复 settings 视图（含 param）
 *   3. pathname=/en/ + hash=#coding → loadRepos 恢复 coding 视图
 *   4. pathname=/（中文）+ hash=#settings/credentials → 同样恢复（基线不回归）
 *   5. /en/ 前缀 + 无 hash → 回退默认 dash（保持原行为）
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

// ── 极简 DOM mock（复用 test_issue_278_refresh_route.js 模式）──
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

// ── 加载 app.js 源码（带 pathname 支持，模拟 /en/ 前缀）──
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
    location: { hash: opts.hash || '', pathname: opts.pathname || '/', search: '', href: 'https://www.grape.example.com' + (opts.pathname || '/') + (opts.hash || '') },
    addEventListener:()=>{},
    dispatchEvent:()=>true,
    CustomEvent: function(){},
  };
  var API = { get: () => Promise.resolve({repos:[]}), post: () => Promise.resolve({}), scheduleProactiveRefresh: () => {} };
  var I18N = { t: (k) => k, getLang: () => (opts.lang || 'zh') };
  var console_ = { log:()=>{}, error:()=>{}, warn:()=>{} };
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
    renderSceneDetailLocal: function(){},
    renderSceneDetail: function(){},
    renderSceneMarket: function(){},
    renderAutopilot: function(){},
    renderAutopilotDetail: function(){},
    renderSkillDetail: function(){},
    renderSettings: function(){},
  };
  var keys = Object.keys(sandbox);
  var vals = keys.map(function(k){ return sandbox[k]; });
  var fn = new Function(...keys, src + '\n; return (typeof app !== "undefined") ? app : null;');
  var app = fn(...vals);

  if (app && app.nav) {
    var origNav = app.nav;
    app.nav = function(view, param) {
      navLog.push({view: view, param: param});
      return origNav.call(this, view, param);
    };
  }

  return { app: app, window: win, localStorage: localStorage, navLog: navLog };
}

function seedRepo(ls) {
  ls.setItem('currentRepo', JSON.stringify({fullName:'o/r', owner:'o', name:'r'}));
  ls.setItem('repo_member_cache', JSON.stringify({
    repos: [{full_name:'o/r', owner:'o', name:'r', is_joined:true}],
    ts: Date.now()
  }));
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch(e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('Issue #273: /en/ 前缀下 F5 刷新保持当前视图 — 回归测试\n');

// ─── 测试 1：/en/app/ + #settings/credentials → loadRepos 恢复 settings 视图 ───
test('loadRepos: pathname=/en/app/ + hash=#settings/credentials → 恢复 settings 视图', () => {
  var { dom, localStorage: ls } = makeDom();
  seedRepo(ls);
  var { app, window: win } = loadApp(dom, ls, { hash: '#settings/credentials', pathname: '/en/app/', lang: 'en' });
  app.loadRepos(false);
  assert.strictEqual(win._currentView, 'settings',
    '/en/ 前缀下 hash=#settings/credentials 应恢复 settings，实际为 ' + win._currentView);
});

// ─── 测试 2：/en/app/ + #settings/credentials → init() 恢复（含 param）───
test('init: pathname=/en/app/ + hash=#settings/credentials → 恢复 settings + param=credentials', () => {
  var { dom, localStorage: ls } = makeDom();
  seedRepo(ls);
  ls.setItem('token', 'test-token');
  ls.setItem('app_cache_ver', '36');
  var { app, window: win } = loadApp(dom, ls, { hash: '#settings/credentials', pathname: '/en/app/', lang: 'en' });
  app.init();
  assert.strictEqual(win._currentView, 'settings',
    'init() 在 /en/ 前缀下应恢复 settings 视图，实际为 ' + win._currentView);
  assert.strictEqual(String(win._currentParam), 'credentials',
    '_currentParam 应为 credentials，实际为 ' + win._currentParam);
});

// ─── 测试 3：/en/ + #coding → 恢复 coding 视图 ───
test('loadRepos: pathname=/en/ + hash=#coding → 恢复 coding 视图', () => {
  var { dom, localStorage: ls } = makeDom();
  seedRepo(ls);
  var { app, window: win } = loadApp(dom, ls, { hash: '#coding', pathname: '/en/', lang: 'en' });
  app.loadRepos(false);
  assert.strictEqual(win._currentView, 'coding',
    '/en/ 前缀下 hash=#coding 应恢复 coding，实际为 ' + win._currentView);
});

// ─── 测试 4：中文路径（基线不回归）───
test('loadRepos: pathname=/ + hash=#settings/credentials → 恢复 settings 视图（基线）', () => {
  var { dom, localStorage: ls } = makeDom();
  seedRepo(ls);
  var { app, window: win } = loadApp(dom, ls, { hash: '#settings/credentials', pathname: '/' });
  app.loadRepos(false);
  assert.strictEqual(win._currentView, 'settings',
    '中文路径下 hash=#settings/credentials 应恢复 settings，实际为 ' + win._currentView);
});

// ─── 测试 5：/en/ 前缀 + 无 hash → 回退默认 dash（保持原行为）───
test('loadRepos: pathname=/en/ + 无 hash → 回退默认 dash', () => {
  var { dom, localStorage: ls } = makeDom();
  seedRepo(ls);
  var { app, window: win } = loadApp(dom, ls, { hash: '', pathname: '/en/', lang: 'en' });
  app.loadRepos(false);
  assert.strictEqual(win._currentView, 'dash',
    '/en/ 前缀无 hash 应回退 dash，实际为 ' + win._currentView);
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
