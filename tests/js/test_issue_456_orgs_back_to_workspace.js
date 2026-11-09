/*
 * Issue #456 回归测试：组织管理页面增加返回工作台按钮
 *
 * 运行：node tests/js/test_issue_456_orgs_back_to_workspace.js
 *
 * 验证点：
 *   1. renderOrganizations() 头部渲染「返回工作台」按钮（bt bt-o 样式，
 *      onclick 调用 app._accountGoBack()，文案走 organizations.back_to_workspace）
 *   2. 按钮位于「创建组织」同排（同一 flex 容器），且创建按钮不回归
 *   3. zh locale 包含 back_to_workspace = "← 返回工作台"
 *   4. en locale 包含 back_to_workspace = "← Back to Workspace"
 *   5. app._accountGoBack() 守卫分流：
 *      a) 未绑定平台 → goToRepos()（引导页路径，_currentView='repos'）
 *      b) 已绑定 + 有当前仓库 → nav('dash')（_currentView='dash'）
 *      c) 已绑定 + 无当前仓库 → goToRepos()（_currentView='repos'）
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

// 防御：异步渲染中的意外 rejection 不应让进程崩溃（与断言无关的 UI 深层路径）
process.on('unhandledRejection', () => {});

// ── 极简 DOM mock ──
function makeDom() {
  const elements = {};
  const ids = ['pageContent', 'sidebar', 'tb', 'mainView', 'pageTitle', 'repoTitle', 'toast',
    'tbOrgName', 'tbOrgBtn', 'tbOrgMenu', 'orgSelect', 'repoSelect', 'gotoReposOverlay'];
  for (const id of ids) {
    elements[id] = {
      _id: id,
      style: {},
      innerHTML: '',
      textContent: '',
      value: '',
      options: [],
      disabled: false,
      classList: { add() {}, remove() {} },
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
      setAttribute: () => {},
      removeAttribute: () => {},
      getAttribute: () => null,
      remove: () => {},
      appendChild: () => {},
      onclick: null,
    };
  }
  const dom = {
    getElementById(id) { return elements[id] || null; },
    querySelector: () => null,
    querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
    createElement: () => ({ style: { cssText: '' }, textContent: '', innerHTML: '', classList: { add() {}, remove() {} }, appendChild: () => {}, remove() {}, querySelector: () => null, setAttribute: () => {}, addEventListener: () => {} }),
    body: { appendChild: () => {} },
    documentElement: { setAttribute() {}, removeAttribute() {}, classList: { add() {}, remove() {} } },
    addEventListener: () => {},
  };
  return { dom, elements };
}

// ── 极简 localStorage/sessionStorage mock ──
function makeStorage() {
  const store = {};
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { for (const k in store) delete store[k]; },
  };
}

// ── I18N mock（返回 key 便于断言语案来源）──
const I18N_MOCK = {
  getLang: () => 'zh',
  t: (k) => {
    if (k === 'organizations.back_to_workspace') return '← 返回工作台';
    if (k === 'organizations.title') return '组织管理';
    if (k === 'organizations.create_org') return '+ 创建组织';
    return k;
  },
};

// ── 极简 API mock ──
function makeApi() {
  return {
    get: (url) => {
      if (url === '/orgs') return Promise.resolve([]);
      if (url === '/repos/member') return Promise.resolve({ repos: [] });
      if (url === '/repos/managed') return Promise.resolve({ repos: [] });
      if (url === '/auth/providers') return Promise.resolve({ providers: [] });
      if (url === '/auth/profile') return Promise.resolve({ nickname: 'Test', username: 'test', managed_repos: [] });
      if (url.indexOf('/dashboard/') === 0) return Promise.resolve({});
      if (url.indexOf('/scenes') === 0) return Promise.resolve({ scenes: [] });
      return Promise.resolve({});
    },
    scheduleProactiveRefresh: () => {},
  };
}

// ── 加载 organizations.js（顶层函数声明脚本）──
function loadOrganizationsPage(dom, storage) {
  global.document = dom;
  global.localStorage = storage;
  global.I18N = I18N_MOCK;
  global.API = makeApi();
  global.window = {};
  global.app = { _accountGoBack: () => {}, nav: () => {}, showToast: () => {} };

  const code = fs.readFileSync(path.resolve(__dirname, '../../src/api/static/js/pages/organizations.js'), 'utf-8');
  const fn = new Function(code + '\nreturn renderOrganizations;');
  return fn();
}

// ── 加载 app.js（IIFE 返回 app 对象）──
function loadApp(dom, storage, session) {
  global.document = dom;
  global.localStorage = storage;
  global.sessionStorage = session;
  global.I18N = I18N_MOCK;
  global.API = makeApi();
  global.window = {
    _currentView: '',
    location: { hash: '' },
    history: { pushState() {}, replaceState() {} },
    addEventListener: () => {},
    dispatchEvent: () => {},
  };
  global.requestAnimationFrame = (fn2) => setTimeout(fn2, 0);
  global.setTimeout = setTimeout;
  global.$ = (id) => dom.getElementById(id);
  // app.js 引用的其它页面级全局函数（异步路径）——空 stub
  global.renderDashboard = () => {};
  global.renderRepoSelection = () => {};
  global.platformIcon = () => '';

  const code = fs.readFileSync(path.resolve(__dirname, '../../src/api/static/js/app.js'), 'utf-8');
  const fn = new Function(code + '\nreturn typeof app !== "undefined" ? app : null;');
  return fn();
}

async function runTests() {
  console.log('Running Issue #456 tests...\n');

  // Test 1+2: renderOrganizations 渲染返回工作台按钮（与创建组织同排）
  {
    const { dom, elements } = makeDom();
    const storage = makeStorage();
    storage.setItem('user', JSON.stringify({ nickname: 'Test', bindings: [] }));
    const renderOrganizations = loadOrganizationsPage(dom, storage);

    assert.strictEqual(typeof renderOrganizations, 'function', 'renderOrganizations should be a function');
    renderOrganizations();

    const html = elements.pageContent.innerHTML;
    assert.ok(html.indexOf('class="bt bt-o"') >= 0, 'Header should contain a bt bt-o styled button');
    assert.ok(html.indexOf('onclick="app._accountGoBack()"') >= 0, 'Back button onclick should call app._accountGoBack()');
    assert.ok(html.indexOf('← 返回工作台') >= 0, 'Back button label should use organizations.back_to_workspace i18n text');

    const backIdx = html.indexOf('app._accountGoBack()');
    const createIdx = html.indexOf('_showCreateOrgDialog()');
    assert.ok(backIdx >= 0 && createIdx >= 0, 'Both back and create buttons should render');
    assert.ok(backIdx < createIdx, 'Back button should be in the same header row before the create button');
    assert.ok(html.indexOf('+ 创建组织') >= 0, 'Create organization button should not regress');
    console.log('  ✅ Test 1-2: renderOrganizations renders back-to-workspace button (bt bt-o, app._accountGoBack) in header row');
  }

  // Test 3: zh locale key
  {
    const zh = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../src/api/static/locales/zh/organizations.json'), 'utf-8'));
    assert.strictEqual(zh.back_to_workspace, '← 返回工作台', 'zh organizations.json should have back_to_workspace');
    console.log('  ✅ Test 3: zh locale back_to_workspace = "← 返回工作台"');
  }

  // Test 4: en locale key
  {
    const en = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../src/api/static/locales/en/organizations.json'), 'utf-8'));
    assert.strictEqual(en.back_to_workspace, '← Back to Workspace', 'en organizations.json should have back_to_workspace');
    console.log('  ✅ Test 4: en locale back_to_workspace = "← Back to Workspace"');
  }

  // Test 5a: 未绑定平台 → goToRepos（引导页路径）
  {
    const { dom } = makeDom();
    const storage = makeStorage();
    const session = makeStorage();
    storage.setItem('user', JSON.stringify({ nickname: 'Test', bindings: [] }));
    const app = loadApp(dom, storage, session);

    assert.strictEqual(typeof app._accountGoBack, 'function', '_accountGoBack should be exported');
    app._accountGoBack();

    assert.strictEqual(global.window._currentView, 'repos', 'Unbound user should go to repos guide (goToRepos), not dash');
    console.log('  ✅ Test 5a: unbound user → goToRepos() guide path');
  }

  // Test 5b: 已绑定 + 有当前仓库 → nav('dash')
  {
    const { dom, elements } = makeDom();
    const storage = makeStorage();
    const session = makeStorage();
    storage.setItem('user', JSON.stringify({ nickname: 'Test', bindings: [{ provider: 'gitcode' }] }));
    session.setItem('currentRepo', JSON.stringify({ fullName: 'grape-dev/grape', owner: 'grape-dev', name: 'grape' }));
    const app = loadApp(dom, storage, session);

    app._accountGoBack();

    assert.strictEqual(global.window._currentView, 'dash', 'Bound user with current repo should nav to dash (workspace)');
    assert.strictEqual(elements.sidebar.style.display, 'flex', 'Sidebar should be restored to flex');
    console.log('  ✅ Test 5b: bound user with currentRepo → nav(\'dash\') + sidebar restored');
  }

  // Test 5c: 已绑定 + 无当前仓库 → goToRepos（仓库列表）
  {
    const { dom } = makeDom();
    const storage = makeStorage();
    const session = makeStorage();
    storage.setItem('user', JSON.stringify({ nickname: 'Test', bindings: [{ provider: 'gitcode' }] }));
    const app = loadApp(dom, storage, session);

    app._accountGoBack();

    assert.strictEqual(global.window._currentView, 'repos', 'Bound user without current repo should go back to repos list');
    console.log('  ✅ Test 5c: bound user without currentRepo → goToRepos()');
  }

  console.log('\n✅ All Issue #456 tests passed!');
  // nav('dash') 会启动 30s 仪表盘轮询 setInterval，测试完成后显式退出
  process.exit(0);
}

runTests().catch(e => {
  console.error('\n❌ Test failed:', e.message);
  console.error(e.stack);
  process.exit(1);
});
