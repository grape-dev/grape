/*
 * Issue #1932：workers 设备页卡片「承载引擎」行去掉引擎名后的括号内容（凭据名），
 * 仅保留「引擎名 v版本 ✓/✗」，多项以 ` · ` 分隔；跳转/版本/健康标记不变；
 * Engines 子页卡片与引擎详情弹窗仍维持「引擎名（凭据名）」口径（#1310，无回归）。
 *
 * 运行：node tests/js/test_issue_1932_workers_engine_chip.js
 *
 * 方式：沙箱真实执行 runtimes.js（worker-name.js 先行加载，与页面顺序一致），
 * 调用 renderWorkers / renderEngines / _renderEngineDetail 断言输出。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}

const ROOT = path.resolve(__dirname, '..', '..', '..');
const RUNTIMES_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/runtimes.js'), 'utf8');

function makeDom() {
  const captured = [];
  return {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, textContent: '', innerHTML: '', appendChild() {}, remove() {}, setAttribute() {}, addEventListener() {} }),
    body: { appendChild() {}, insertAdjacentHTML(pos, html) { captured.push(html); }, removeChild() {} },
    head: { appendChild() {}, insertAdjacentHTML() {} },
    documentElement: { setAttribute() {}, removeAttribute() {} },
    addEventListener() {},
    execCommand() {},
    removeChild() {},
    _captured: captured,
  };
}

function loadRuntimesFns() {
  const helperSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/worker-name.js'), 'utf8');
  const src = RUNTIMES_JS;
  const I18N = { t: (k) => '«' + k + '»', getLang: () => 'zh' };
  const dom = makeDom();
  new Function('I18N', helperSrc)(I18N);
  const sandbox = {
    document: dom,
    window: { addEventListener() {}, _currentView: '', _grapeUsername: 'tester', history: { pushState() {}, replaceState() {}, state: null }, location: { hash: '', search: '' } },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    API: { get: () => Promise.resolve({}), post: () => Promise.resolve({}), put: () => Promise.resolve({}), del: () => Promise.resolve({}) },
    I18N,
    console: { log() {}, error() {}, warn() {} },
    relTime: (s) => s || '',
    fmtLocalTime: (s) => s || '',
    fmtDateTime: (s) => s || '',
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function () {}, AbortController: function () {}, AbortSignal: { timeout: () => ({}) },
    app: { showToast() {}, showConfirm() {}, showPrompt() {}, currentRepo: () => ({ fullName: 'a/b', owner: 'a', name: 'b' }) },
  };
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  const exportsStr = '\n; return {' +
    'renderEngines: typeof renderEngines !== "undefined" ? renderEngines : null,' +
    'renderWorkers: typeof renderWorkers !== "undefined" ? renderWorkers : null,' +
    '_renderEngineDetail: typeof _renderEngineDetail !== "undefined" ? _renderEngineDetail : null' +
    '};';
  const fn = new Function(...keys, src + exportsStr);
  const fns = fn(...vals);
  fns._captured = dom._captured;
  return fns;
}

function mkEngine(over) {
  return Object.assign({
    id: 1, name: 'opencode', display_name: 'opencode', version: '1.0',
    instance_name: 'opencode-worker-demo-0003', worker_name: '我的工作机',
    health: 'online', status: 'online', healthy: true, is_local: false,
    scope: 'global', local_skills: [], agent_tags: [], scene_tags: ['default'], installed: true,
  }, over || {});
}

function mkDevice(runtimes) {
  return {
    credential_id: 5, name: 'worker-a', device_name: 'dev-worker-a', scope: 'global', owner: 'bob', health: 'online',
    worker_ip: '127.0.0.1', credential_status: 'active', last_seen_at: '',
    runtimes: runtimes,
  };
}

// renderWorkers 完整输出（设备卡片；承载引擎 chip 为其中唯一引擎名渲染位）
function chipHtml(fns, device) {
  return fns.renderWorkers({}, [device]);
}

console.log('Issue #1932：workers 设备卡片「承载引擎」行去括号（仅 引擎名 v版本 健康标记）');
const fns = loadRuntimesFns();
assert.ok(fns.renderEngines && fns.renderWorkers && fns._renderEngineDetail, '沙箱加载失败');

// ── 1. 无 alias：chip 仅显示「引擎名 v版本 ✓」，无括号内容 ──
check('无 alias：chip=「hermes v1.0 ✓」，无括号', () => {
  const html = chipHtml(fns, mkDevice([mkEngine({ id: 21, name: 'hermes', display_name: 'hermes', instance_name: 'hermes-worker-demo-0003', worker_name: '我的hermes' })]));
  assert.ok(html.includes('hermes v1.0 ✓'), 'chip 应为「hermes v1.0 ✓」');
  assert.ok(!html.includes('hermes（我的hermes）'), 'chip 不得出现「引擎名（凭据名）」括号内容');
  assert.ok(!html.includes('hermes(我的hermes)'), 'chip 不得出现半角括号内容');
  assert.ok(!html.includes('hermes-worker-demo-0003'), 'chip 不得出现设备派生 instance_name');
});

// ── 2. 有 alias：主名仍为 alias（无括号），版本/健康标记保留 ──
check('有 alias：chip=「别名引擎 v1.0 ✓」', () => {
  const html = chipHtml(fns, mkDevice([mkEngine({ id: 22, alias: '别名引擎' })]));
  assert.ok(html.includes('别名引擎 v1.0 ✓'), '有 alias 时 chip 应为「别名引擎 v1.0 ✓」');
  assert.ok(!html.includes('（我的工作机）'), 'chip 不得出现括号内容');
});

// ── 3. 多引擎以 ` · ` 分隔，跳转 openEngineDetail 保留 ──
check('多引擎 ` · ` 分隔 + openEngineDetail 跳转保留', () => {
  const html = chipHtml(fns, mkDevice([
    mkEngine({ id: 21, name: 'hermes', display_name: 'hermes' }),
    mkEngine({ id: 22, name: 'codearts', display_name: 'codearts' }),
  ]));
  assert.ok(/✓<\/span> · <span/.test(html), '多项 chip 应以「 · 」分隔');
  assert.ok(html.includes('openEngineDetail(21)') && html.includes('openEngineDetail(22)'), 'chip 点击跳转应保留');
});

// ── 4. 不健康引擎显示 ✗ ──
check('不健康引擎 chip=「hermes v1.0 ✗」', () => {
  const html = chipHtml(fns, mkDevice([mkEngine({ id: 21, name: 'hermes', display_name: 'hermes', healthy: false })]));
  assert.ok(html.includes('hermes v1.0 ✗'), '不健康引擎应显示 ✗');
});

// ── 5. 引擎名取值链与 #1310 引擎名部分一致（display_name/engine_name/name 优先）──
check('engine_name 兜底（无 display_name/name）显示引擎名', () => {
  const html = chipHtml(fns, mkDevice([mkEngine({ id: 23, display_name: '', name: '', engine_name: 'claude' })]));
  assert.ok(html.includes('claude v1.0 ✓'), '应回退显示 engine_name=claude');
});

// ── 5b. 全空回退：所有引擎名来源为空时回退 engineDisplayName（无括号，审查 I-3）──
check('全空回退：chip 取 engineDisplayName（凭据名），仍无括号', () => {
  const html = chipHtml(fns, mkDevice([mkEngine({
    id: 24, alias: '', display_name: '', engine_name: '', name: '', instance_name: '',
  })]));
  assert.ok(html.includes('我的工作机 v1.0 ✓'), '全空时应回退 worker/凭据名显示');
  assert.ok(!/[（(]/.test(html.split('承载引擎')[1] || ''), '回退路径也不得出现括号');
});

// ── 6. 回归：Engines 子页卡片仍显示「引擎名（凭据名）」 ──
check('回归：Engines 卡片仍显示「引擎名（凭据名）」', () => {
  const html = fns.renderEngines({ runtimes: [mkEngine()] }, []);
  assert.ok(html.includes('opencode（我的工作机）'), 'Engines 卡片口径不变');
});

// ── 7. 回归：引擎详情弹窗标题仍为「引擎名（凭据名）」 ──
check('回归：详情弹窗标题仍为「引擎名（凭据名）」', () => {
  fns._captured.length = 0;
  fns._renderEngineDetail(mkEngine());
  const html = fns._captured.join('');
  assert.ok(html.includes('opencode（我的工作机）'), '详情弹窗口径不变');
});

console.log('');
console.log('passed=' + passed + ' failed=' + failed);
if (failed > 0) process.exit(1);
