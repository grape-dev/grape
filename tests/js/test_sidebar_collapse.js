/*
 * Issue #207 回归测试：sidebar 折叠/展开功能
 *
 * 运行：node tests/js/test_sidebar_collapse.js
 *
 * 验证点：
 *   1. toggleSidebar() 切换 .collapsed class 并写回 localStorage('grapeSidebarCollapsed')
 *   2. initSidebarState() 启动时读取 localStorage 恢复折叠状态
 *   3. 再次点击恢复展开，localStorage 写回 '0'
 *   4. toggleSidebar(true) 在桌面端仍正常切换（forceClose 仅影响窄屏短路）
 *   5. 暴露在 app 公开 API（app.toggleSidebar 可调用）
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

// ── 极简 DOM mock（仅覆盖 app.js 用到的 API）──
function makeDom(innerWidth) {
  const store = {};
  const elements = {};
  const ids = ['loginView','sidebar','mainView','tb','pageTitle','repoSelect','pageContent','repoTitle','welcomeUser','userName','userAvatar','userRole','loginLinks','pwdLogin','gitBtn','loginUser','loginPass','helpOverlay','helpDrawer','gotoReposOverlay','nav-dash','sbToggle','sbOverlay'];
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
  return { dom, elements, localStorage, innerWidth: innerWidth || 1280 };
}

// ── 加载 app.js 源码 ──
const appJsPath = path.resolve(__dirname, '../../src/api/static/js/app.js');
const src = fs.readFileSync(appJsPath, 'utf8');

function loadApp(dom, localStorage, innerWidth) {
  const win = {
    _currentView: '',
    innerWidth: innerWidth || 1280,
    history: { pushState(){}, replaceState(){}, state: null },
    location: { hash:'' },
    addEventListener:()=>{},
    dispatchEvent:()=>true,
    CustomEvent: function(){},
  };
  const API = { get: () => Promise.resolve({repos:[]}), post: () => Promise.resolve({}), scheduleProactiveRefresh: () => {} };
  const I18N = { t: (k) => k, getLang: () => 'zh' };
  const console_ = { log:()=>{}, error:()=>{}, warn:()=>{} };

  const sandbox = {
    document: dom,
    window: win,
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
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  const fn = new Function(...keys, src + '\n; return (typeof app !== "undefined") ? app : null;');
  const app = fn(...vals);
  return { app, window: win, localStorage };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch(e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('Issue #207: sidebar 折叠/展开 — 回归测试\n');

// ─── 测试 1：toggleSidebar() 切换 collapsed 并持久化 ───
test('toggleSidebar(): 桌面端首次点击 → collapsed class 添加 + localStorage 写入 "1"', () => {
  const { dom, localStorage: ls } = makeDom();
  const { app } = loadApp(dom, ls);
  assert.strictEqual(typeof app.toggleSidebar, 'function', 'app.toggleSidebar 应为函数');
  // 初始无 collapsed
  assert.strictEqual(dom.getElementById('sidebar').classList.contains('collapsed'), false, '初始不应有 collapsed');
  app.toggleSidebar();
  assert.strictEqual(dom.getElementById('sidebar').classList.contains('collapsed'), true, '点击后应有 collapsed');
  assert.strictEqual(ls.getItem('grapeSidebarCollapsed'), '1', 'localStorage 应写入 "1"');
});

// ─── 测试 2：再次点击恢复展开 ───
test('toggleSidebar(): 再次点击 → collapsed 移除 + localStorage 写入 "0"', () => {
  const { dom, localStorage: ls } = makeDom();
  const { app } = loadApp(dom, ls);
  app.toggleSidebar();  // 收起
  app.toggleSidebar();  // 展开
  assert.strictEqual(dom.getElementById('sidebar').classList.contains('collapsed'), false, '再次点击后 collapsed 应移除');
  assert.strictEqual(ls.getItem('grapeSidebarCollapsed'), '0', 'localStorage 应写入 "0"');
});

// ─── 测试 3：initSidebarState() 恢复持久化状态 ───
test('initSidebarState(): localStorage="1" → 启动时恢复 collapsed（通过 app.init 间接调用）', () => {
  const { dom, localStorage: ls } = makeDom();
  ls.setItem('grapeSidebarCollapsed', '1');
  ls.setItem('token', 'fake-token');  // 让 init 走 showApp 路径
  ls.setItem('app_cache_ver', '30');
  const { app } = loadApp(dom, ls);
  app.init();
  assert.strictEqual(dom.getElementById('sidebar').classList.contains('collapsed'), true,
    'localStorage="1" 时 init 后 sidebar 应有 collapsed');
});

// ─── 测试 4：initSidebarState() 无持久化时默认展开 ───
test('initSidebarState(): 无 localStorage 记录 → 默认展开（无 collapsed）', () => {
  const { dom, localStorage: ls } = makeDom();
  ls.setItem('token', 'fake-token');
  ls.setItem('app_cache_ver', '30');
  const { app } = loadApp(dom, ls);
  app.init();
  assert.strictEqual(dom.getElementById('sidebar').classList.contains('collapsed'), false,
    '无持久化记录时默认展开');
});

// ─── 测试 5：折叠按钮图标同步 ───
test('toggleSidebar(): 折叠时 sbToggle 添加 collapsed class（图标方向提示）', () => {
  const { dom, localStorage: ls } = makeDom();
  const { app } = loadApp(dom, ls);
  app.toggleSidebar();
  assert.strictEqual(dom.getElementById('sbToggle').classList.contains('collapsed'), true,
    '折叠后 sbToggle 应有 collapsed class');
  app.toggleSidebar();
  assert.strictEqual(dom.getElementById('sbToggle').classList.contains('collapsed'), false,
    '展开后 sbToggle collapsed 应移除');
});

// ─── 测试 6：toggleSidebar(true) 桌面端仍切换 ───
test('toggleSidebar(true): 桌面端 forceClose 不短路，仍正常切换', () => {
  const { dom, localStorage: ls } = makeDom();
  const { app } = loadApp(dom, ls);
  app.toggleSidebar(true);  // 桌面端 forceClose 不影响
  assert.strictEqual(dom.getElementById('sidebar').classList.contains('collapsed'), true,
    '桌面端 forceClose=true 仍应切换到 collapsed');
});

// ─── 测试 7：窄屏 overlay 抽屉模式 ───
test('toggleSidebar(): 窄屏(≤768px) → overlay 抽屉模式，不写持久化', () => {
  const ctx = makeDom(600);  // innerWidth=600 ≤768
  const { dom, localStorage: ls } = ctx;
  const { app } = loadApp(dom, ls, 600);
  app.toggleSidebar();
  // 窄屏应添加 sb-drawer-open 而非 collapsed
  assert.strictEqual(dom.getElementById('sidebar').classList.contains('sb-drawer-open'), true,
    '窄屏点击应添加 sb-drawer-open class');
  assert.strictEqual(dom.getElementById('sbOverlay').classList.contains('show'), true,
    '窄屏点击应显示 sbOverlay');
  // 不写持久化
  assert.strictEqual(ls.getItem('grapeSidebarCollapsed'), null,
    '窄屏模式不应写 localStorage 持久化');
  // 再次点击关闭
  app.toggleSidebar();
  assert.strictEqual(dom.getElementById('sidebar').classList.contains('sb-drawer-open'), false,
    '窄屏再次点击应移除 sb-drawer-open');
  assert.strictEqual(dom.getElementById('sbOverlay').classList.contains('show'), false,
    '窄屏再次点击应隐藏 sbOverlay');
});

// ─── 测试 8：窄屏 forceClose 关闭抽屉 ───
test('toggleSidebar(true): 窄屏抽屉已展开 → 关闭抽屉', () => {
  const ctx = makeDom(600);
  const { dom, localStorage: ls } = ctx;
  const { app } = loadApp(dom, ls, 600);
  app.toggleSidebar();  // 展开
  app.toggleSidebar(true);  // forceClose
  assert.strictEqual(dom.getElementById('sidebar').classList.contains('sb-drawer-open'), false,
    '窄屏 forceClose 应关闭抽屉');
});

// ─── 测试 9：公开 API 暴露 ───
test('API 暴露: app.toggleSidebar 在公开返回对象中', () => {
  const { dom, localStorage: ls } = makeDom();
  const { app } = loadApp(dom, ls);
  assert.strictEqual(typeof app.toggleSidebar, 'function', 'toggleSidebar 应暴露在 app 对象');
});

// ─── 测试 10：#sbOverlay 不在 #sidebar 内部（CSS 包含块修复）───
// QA Critical: transform 会为 fixed 子孙建立包含块，overlay 必须在 #sidebar 外部
test('DOM 结构: #sbOverlay 是 #sidebar 的兄弟节点而非子节点', () => {
  const htmlPath = path.resolve(__dirname, '../../src/api/static/index.html');
  const html = fs.readFileSync(htmlPath, 'utf8');
  const sidebarOpen = html.indexOf('id="sidebar"');
  const sidebarClose = html.indexOf('</div>', html.indexOf('id="sidebar"'));
  const overlayPos = html.indexOf('id="sbOverlay"');
  assert.ok(sidebarOpen > -1, '应找到 #sidebar');
  assert.ok(overlayPos > -1, '应找到 #sbOverlay');
  // sbOverlay 必须出现在 #sidebar 开标签之前（是兄弟节点，不是子节点）
  assert.ok(overlayPos < sidebarOpen,
    '#sbOverlay 应在 #sidebar 之前（兄弟节点），当前 overlayPos=' + overlayPos + ' sidebarOpen=' + sidebarOpen);
});

// ─── 测试 11：apple-theme 窄屏抽屉 transform 过渡 ───
// QA Required: apple-theme .sb{transition:width} 覆盖了窄屏 transform 过渡
test('CSS: apple-theme 窄屏 media query 内含 transform 过渡规则', () => {
  const cssPath = path.resolve(__dirname, '../../src/api/static/css/apple-theme.css');
  const css = fs.readFileSync(cssPath, 'utf8');
  // 提取最后一个 @media(max-width:768px) 块
  const mediaIdx = css.lastIndexOf('@media(max-width:768px)');
  assert.ok(mediaIdx > -1, 'apple-theme.css 应有 @media(max-width:768px) 块');
  const mediaBlock = css.substring(mediaIdx);
  // 精确匹配 [data-theme="apple"] .sb {（而非 .sb-ov / .sb-toggle 等）
  const sbRuleMatch = mediaBlock.match(/\[data-theme="apple"\]\s+\.sb\s*\{/);
  assert.ok(sbRuleMatch, 'media query 内应有 [data-theme="apple"] .sb { 规则');
  const sbRuleIdx = sbRuleMatch.index;
  const ruleBlock = mediaBlock.substring(sbRuleIdx);
  const ruleEnd = ruleBlock.indexOf('}');
  const ruleContent = ruleBlock.substring(0, ruleEnd);
  assert.ok(ruleContent.includes('transform') && ruleContent.includes('var(--apple-transition)'),
    '窄屏 apple-theme .sb 规则应包含 transition: transform var(--apple-transition)，实际: ' + ruleContent);
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
