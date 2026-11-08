/*
 * Issue #1310：agents.js / autopilot.js / dashboard.js 引擎下拉显示文本统一
 * 「引擎名（凭据名）」，option value / 标识逻辑不变。
 *
 * 运行：node tests/js/test_issue_1310_dropdown_display.js
 *
 * 方式：
 *   - agents.js：沙箱真实执行 loadCfgOptions（/settings/agent-runtimes 数据源），
 *     断言面板勾选项主文本 = 引擎名（凭据名），checkbox value 仍为 instance_name（标识不变）
 *   - autopilot.js / dashboard.js：_renderEngineList 位于闭包内，采用源码守卫 +
 *     渲染片段镜像断言（沿用 test_issue_227 的模式）
 *
 * 验证点：
 *   1. agents.js 绑定下拉：显示「opencode（我的工作机）」，value=instance_name 不变
 *   2. autopilot.js 引擎条目：主标题/括号/副行不再出现 instance_name；搜索 hay 含显示名
 *   3. dashboard.js 引擎条目：主标题显示「引擎名（凭据名）」，副行不再显示 device 固有名称
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

let passed = 0, failed = 0;
const _checkPromises = [];
function check(name, fn) {
  const p = (async () => {
    try { await fn(); passed++; console.log('  ✓ ' + name); }
    catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
  })();
  _checkPromises.push(p);
  return p;
}

const ROOT = path.resolve(__dirname, '..', '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');
const AGENTS_JS = read('src/api/static/js/pages/agents.js');
const AUTOPILOT_JS = read('src/api/static/js/pages/autopilot.js');
const DASHBOARD_JS = read('src/api/static/js/pages/dashboard.js');
const ZH = JSON.parse(read('src/api/static/locales/zh/common.json'));

// ── agents.js 沙箱真实执行 ──
function loadAgentsLoadCfgOptions() {
  const helperSrc = read('src/api/static/js/worker-name.js');
  // 阶段 A：helper 落在真实 globalThis，页面裸引用可解析
  new Function('I18N', helperSrc)({ t: (k) => '«' + k + '»' });
  const panel = { innerHTML: '', dataset: {}, addEventListener() {}, querySelector: () => null, querySelectorAll: () => [], classList: { add() {}, remove() {}, toggle() {} } };
  const trigger = { querySelector: () => null, querySelectorAll: () => [], appendChild() {}, removeChild() {} };
  const backendBox = {
    dataset: { backends: 'opencode-worker-demo-0003', agent: 'coding' },
    querySelector: (sel) => sel === '.cfg-backends-panel' ? panel : (sel === '.cfg-backends-trigger' ? trigger : null),
    querySelectorAll: () => [],
  };
  const sandbox = {
    document: {
      querySelector: (sel) => sel === '.cfg-backends' ? backendBox : null,
      querySelectorAll: () => [],
      getElementById: () => null,
      createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, appendChild() {}, setAttribute() {} }),
      body: { appendChild() {} },
      addEventListener() {},
    },
    window: { _currentView: '', history: { pushState() {}, replaceState() {}, state: null }, location: { hash: '' }, addEventListener() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {} },
    API: {
      get: () => Promise.resolve({ runtimes: [
        { id: 1, name: 'opencode', display_name: 'opencode', instance_name: 'opencode-worker-demo-0003', worker_name: '我的工作机', version: '1.0', health: 'online', healthy: true, scope: 'global', is_local: false, agent_tags: ['coding'] },
      ] }),
      post: () => Promise.resolve({}), put: () => Promise.resolve({}),
    },
    I18N: { t: (k) => '«' + k + '»' },
    console: { log() {}, error() {}, warn() {} },
    setTimeout, clearTimeout, Date, JSON, Array, Object, String, Number, Boolean, Math,
    app: { currentRepo: () => ({ fullName: 'a/b', owner: 'a', name: 'b' }) },
  };
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  const exportsStr = '\n; return { loadCfgOptions: typeof loadCfgOptions !== "undefined" ? loadCfgOptions : null };';
  const fn = new Function(...keys, AGENTS_JS + exportsStr);
  const out = fn(...vals);
  return { load: out.loadCfgOptions, panel };
}

console.log('Issue #1310：三处引擎下拉显示统一「引擎名（凭据名）」');

// ── 1. agents.js 真实执行 ──
check('agents.js 绑定下拉：显示「opencode（我的工作机）」，value=instance_name 不变', async () => {
  const { load, panel } = loadAgentsLoadCfgOptions();
  load();
  await new Promise(r => setTimeout(r, 80));
  const m = panel.innerHTML.match(/<span style="font-weight:600;color:[^"]+;min-width:110px">([^<]*)<\/span>/);
  assert.ok(m, '主显示 span 缺失, 实际: ' + panel.innerHTML.substring(0, 600));
  assert.strictEqual(m[1], 'opencode（我的工作机）', '主显示文本应为「opencode（我的工作机）」');
  assert.ok(panel.innerHTML.includes('value="opencode-worker-demo-0003"'), 'checkbox value 必须保持 instance_name（底层标识不变）');
  assert.ok(!panel.innerHTML.includes('agents.engine.load_failed'), '不应走加载失败分支');
});

check('agents.js 源码：value 仍取 instance_name（标识不变），显示改 engineDisplayName', () => {
  assert.ok(AGENTS_JS.includes('const val = r => r.instance_name || r.name;'), 'checkbox value 必须保持 instance_name（底层标识不变）');
  assert.ok(AGENTS_JS.includes('engineDisplayName(r)'), 'agents.js 应引用 engineDisplayName(r) 作为显示文本');
});

// ── 2. autopilot.js ──
check('autopilot.js 引擎条目：主/副行去 instance_name，搜索 hay 含显示名', () => {
  assert.ok(AUTOPILOT_JS.includes('engineDisplayName(e)'), 'autopilot.js 应引用 engineDisplayName(e)');
  assert.ok(!/var inst = e\.instance_name \|\| ''/.test(AUTOPILOT_JS), 'autopilot.js 副行不得再取 instance_name 显示');
  assert.ok(AUTOPILOT_JS.includes('engineDisplayName(e), e.name, e.alias'), 'autopilot.js 搜索 hay 应含显示名（保留 name/instance 兼容）');
});

// ── 3. dashboard.js ──
check('dashboard.js 引擎条目：显示「引擎名（凭据名）」，副行不再显示 device 固有名称', () => {
  assert.ok(DASHBOARD_JS.includes('engineDisplayName(e)'), 'dashboard.js 应引用 engineDisplayName(e)');
  assert.ok(!DASHBOARD_JS.includes('e.device ||'), 'dashboard.js 副行不得再显示 device 固有名称');
  assert.ok(!DASHBOARD_JS.includes("'(' + e.name + ')'"), 'dashboard.js 括号不得再显示 instance_name（e.name）');
  assert.ok(DASHBOARD_JS.includes('e.worker_name'), 'dashboard.js 搜索/显示应含 worker_name');
});

// ── 镜像断言：统一渲染片段（与各文件 _renderEngineList 一致） ──
check('统一渲染片段：alias 优先，括号显「引擎名（凭据名）」', () => {
  const rt = { name: 'opencode', display_name: 'opencode', worker_name: '我的工作机', instance_name: 'opencode-worker-demo-0003', alias: '' };
  require(path.join(ROOT, 'src/api/static/js/worker-name.js'));
  const display = globalThis.engineDisplayName(rt);
  assert.strictEqual(display, 'opencode（我的工作机）');
  assert.strictEqual(globalThis.engineDisplayName(Object.assign({}, rt, { alias: '别名' })), 'opencode（我的工作机）');
  // 无 alias → 主显示 = display；有 alias → 主显示 = alias，括号 = display
  const main = rt.alias || display;
  const bracket = rt.alias ? '(' + display + ')' : '';
  assert.strictEqual(main, 'opencode（我的工作机）');
  assert.strictEqual(bracket, '');
  const withAlias = Object.assign({}, rt, { alias: '我的opencode' });
  const mainA = withAlias.alias || display;
  const bracketA = withAlias.alias ? '(' + display + ')' : '';
  assert.strictEqual(mainA, '我的opencode');
  assert.strictEqual(bracketA, '(opencode（我的工作机）)');
});

(async () => {
  await Promise.all(_checkPromises);
  console.log('');
  console.log('passed=' + passed + ' failed=' + failed);
  if (failed > 0) process.exit(1);
})();