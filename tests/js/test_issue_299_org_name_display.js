/*
 * Issue #299 回归测试：工作台组织区域显示当前已选空间名称
 *
 * 运行：node tests/js/test_issue_299_org_name_display.js
 *
 * 验证点：
 *   1. _updateTbOrgName() 读取 current_org_id 并填充当前 org name 到 #tbOrgName
 *   2. 未选择组织时保留默认文本（不修改 DOM）
 *   3. 找不到匹配 org 时不修改 DOM（保留默认文本）
 *   4. switchOrg() 切换后调用 _updateTbOrgName() 更新名称
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

// ── 极简 DOM mock ──
function makeDom() {
  const elements = {};
  const ids = ['tbOrgName', 'tbOrgBtn', 'tbOrgMenu', 'orgSelect', 'pageTitle', 'repoTitle', 'toast'];
  for (const id of ids) {
    elements[id] = {
      _id: id,
      style: {},
      innerHTML: '',
      textContent: id === 'tbOrgName' ? '我的组织' : '',
      value: '',
      options: [],
      disabled: false,
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
      setAttribute: () => {},
      removeAttribute: function(attr) { this._removedAttrs = this._removedAttrs || {}; this._removedAttrs[attr] = true; },
      hasAttribute: function(attr) { return !(this._removedAttrs && this._removedAttrs[attr]); },
      getAttribute: function(attr) {
        if (attr === 'data-i18n') return this._removedAttrs && this._removedAttrs['data-i18n'] ? null : 'organizations.my_orgs';
        return null;
      },
    };
  }
  const dom = {
    getElementById(id) { return elements[id] || null; },
    querySelector: () => null,
    querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
    createElement: () => ({ style:{cssText:''}, textContent:'', innerHTML:'', classList:{add(){}}, appendChild:()=>{}, remove(){}, querySelector:()=>null, setAttribute(){}, addEventListener(){} }),
    body: { appendChild: () => {} },
    documentElement: { setAttribute(){}, removeAttribute(){} },
    addEventListener: () => {},
  };
  return { dom, elements };
}

// ── 极简 localStorage mock ──
function makeStorage() {
  const store = {};
  return {
    getItem: (k) => store[k] || null,
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { for (const k in store) delete store[k]; },
  };
}

// ── 极简 API mock ──
function makeApi(orgsData) {
  return {
    get: (url) => {
      if (url === '/orgs') return Promise.resolve(orgsData);
      if (url === '/repos/member') return Promise.resolve({ repos: [] });
      if (url === '/auth/profile') return Promise.resolve({ nickname: 'Test', username: 'test', managed_repos: [] });
      return Promise.reject(new Error('Unknown: ' + url));
    },
    scheduleProactiveRefresh: () => {},
  };
}

// ── 加载 app.js（IIFE 返回 app 对象）──
function loadApp(dom, storage, api) {
  global.document = dom;
  global.localStorage = storage;
  global.API = api;
  global.I18N = { t: (k) => { if (k === 'organizations.my_orgs') return '我的组织'; if (k === 'organizations.switch_org.success') return '已切换组织'; return k; } };
  global.window = { _currentView: '', addEventListener: () => {}, dispatchEvent: () => {} };
  global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  global.setTimeout = setTimeout;
  global.sessionStorage = makeStorage();
  global.$ = (id) => dom.getElementById(id);

  const appPath = path.resolve(__dirname, '../../src/api/static/js/app.js');
  const code = fs.readFileSync(appPath, 'utf-8');
  // app.js is an IIFE: (function() { ... return {...}; })()
  // Evaluate in global scope
  const fn = new Function(code + '\nreturn typeof app !== "undefined" ? app : null;');
  return fn();
}

async function runTests() {
  console.log('Running Issue #299 tests...\n');

  // Test 1: _updateTbOrgName fills current org name
  {
    const { dom, elements } = makeDom();
    const storage = makeStorage();
    const orgs = [
      { id: 1, name: '个人空间', is_personal: true },
      { id: 2, name: '研发团队', is_personal: false },
    ];
    const api = makeApi(orgs);
    storage.setItem('current_org_id', '2');
    const app = loadApp(dom, storage, api);

    assert.ok(typeof app._updateTbOrgName === 'function', '_updateTbOrgName should be a function');

    await app._updateTbOrgName();
    // Wait for microtask
    await new Promise(r => setTimeout(r, 50));

    assert.strictEqual(elements.tbOrgName.textContent, '研发团队', 'Should display current org name "研发团队"');
    console.log('  ✅ Test 1: _updateTbOrgName fills current org name');
  }

  // Test 2: No current_org_id → keep default text
  {
    const { dom, elements } = makeDom();
    const storage = makeStorage();
    const orgs = [{ id: 1, name: '个人空间', is_personal: true }];
    const api = makeApi(orgs);
    // No current_org_id set
    const app = loadApp(dom, storage, api);

    await app._updateTbOrgName();
    await new Promise(r => setTimeout(r, 50));

    assert.strictEqual(elements.tbOrgName.textContent, '我的组织', 'Should keep default text when no org selected');
    console.log('  ✅ Test 2: No current_org_id → keep default text');
  }

  // Test 3: current_org_id not found in orgs → keep default
  {
    const { dom, elements } = makeDom();
    const storage = makeStorage();
    const orgs = [{ id: 1, name: '个人空间', is_personal: true }];
    const api = makeApi(orgs);
    storage.setItem('current_org_id', '999');
    const app = loadApp(dom, storage, api);

    await app._updateTbOrgName();
    await new Promise(r => setTimeout(r, 50));

    assert.strictEqual(elements.tbOrgName.textContent, '我的组织', 'Should keep default when org not found');
    console.log('  ✅ Test 3: current_org_id not found → keep default');
  }

  // Test 4: switchOrg updates org name after switch
  {
    const { dom, elements } = makeDom();
    const storage = makeStorage();
    const orgs = [
      { id: 1, name: '个人空间', is_personal: true },
      { id: 2, name: '研发团队', is_personal: false },
    ];
    const api = makeApi(orgs);
    storage.setItem('current_org_id', '1');
    const app = loadApp(dom, storage, api);

    // Initial load
    await app._updateTbOrgName();
    await new Promise(r => setTimeout(r, 50));
    assert.strictEqual(elements.tbOrgName.textContent, '个人空间', 'Initial org name should be "个人空间"');

    // Switch org
    app.switchOrg(2);
    await new Promise(r => setTimeout(r, 100));

    assert.strictEqual(storage.getItem('current_org_id'), '2', 'current_org_id should be updated to 2');
    assert.strictEqual(elements.tbOrgName.textContent, '研发团队', 'After switchOrg, name should be "研发团队"');
    console.log('  ✅ Test 4: switchOrg updates org name');
  }

  // Test 5: data-i18n removed after dynamic fill (prevents language switch override)
  {
    const { dom, elements } = makeDom();
    const storage = makeStorage();
    const orgs = [{ id: 1, name: '个人空间', is_personal: true }];
    const api = makeApi(orgs);
    storage.setItem('current_org_id', '1');
    const app = loadApp(dom, storage, api);

    await app._updateTbOrgName();
    await new Promise(r => setTimeout(r, 50));

    assert.ok(elements.tbOrgName._removedAttrs && elements.tbOrgName._removedAttrs['data-i18n'],
      'data-i18n should be removed after dynamic fill to prevent language switch override');
    console.log('  ✅ Test 5: data-i18n removed after dynamic fill');
  }

  console.log('\n✅ All Issue #299 tests passed!');
}

runTests().catch(e => {
  console.error('\n❌ Test failed:', e.message);
  console.error(e.stack);
  process.exit(1);
});
