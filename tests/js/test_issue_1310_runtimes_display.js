/*
 * Issue #1310：runtimes.js 引擎展示位（卡片/复制/排序/Workers chip/详情弹窗/alias 回写）
 * 全部改为「引擎名（凭据名）」，不再显示设备固有名称派生的 instance_name。
 *
 * 运行：node tests/js/test_issue_1310_runtimes_display.js
 *
 * 方式：沙箱真实执行 runtimes.js（worker-name.js 先行加载，与 index.html 顺序一致），
 * 调用 renderEngines / renderWorkers / _renderEngineDetail / _sortByName 断言输出。
 * 另含源码守卫：alias 编辑回写段（DOM 更新）不得再拼接 instance_name。
 *
 * 验证点：
 *   1. Engines 卡片无 alias：主标题 = 引擎名（凭据名），不出现 instance_name
 *   2. Engines 卡片有 alias：主标题 = alias，括号 = （引擎名（凭据名））
 *   3. 复制图标复制「引擎名（凭据名）」
 *   4. 凭据名为空：卡片显示「未命名设备」占位
 *   5. 排序：按「引擎名（凭据名）」排序（本机直连优先 + 健康序之后）
 *   6. Workers 承载引擎 chip 显示「引擎名（凭据名）」（非 alias || name 裸名）
 *   7. 引擎详情弹窗标题 = alias || 引擎名（凭据名）
 *   8. alias 编辑回写段不再出现 instance_name 拼接（源码守卫）
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}

const ROOT = path.resolve(__dirname, '..', '..');
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
  // 阶段 A：helper 在无 window 的作用域执行 → 落在真实 globalThis，页面裸引用 workerDisplayName/engineDisplayName 可解析
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
    '_renderEngineDetail: typeof _renderEngineDetail !== "undefined" ? _renderEngineDetail : null,' +
    '_sortByName: typeof _sortByName !== "undefined" ? _sortByName : null,' +
    'engineDisplayName: globalThis.engineDisplayName || null' +
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

console.log('Issue #1310：runtimes.js 引擎展示位统一「引擎名（凭据名）」');
const fns = loadRuntimesFns();
assert.ok(fns.renderEngines && fns.renderWorkers && fns._renderEngineDetail && fns._sortByName && fns.engineDisplayName, '沙箱加载失败');

// ── 1. Engines 卡片无 alias：主标题 = 引擎名（凭据名），不出现 instance_name ──
check('Engines 卡片无 alias：主标题=引擎名（凭据名），无设备派生名', () => {
  const html = fns.renderEngines({ runtimes: [mkEngine()] }, []);
  assert.ok(html.includes('opencode（我的工作机）'), '标题应显示「opencode（我的工作机）」, 实际: ' + html.substring(0, 400));
  assert.ok(!html.includes('opencode-worker-demo-0003'), '不得出现设备派生 instance_name');
});

// ── 2. Engines 卡片有 alias：主标题 = alias，括号 = （引擎名（凭据名）） ──
check('Engines 卡片有 alias：alias 优先，括号显「引擎名（凭据名）」', () => {
  const html = fns.renderEngines({ runtimes: [mkEngine({ alias: '我的opencode' })] }, []);
  assert.ok(html.includes('我的opencode'), '有 alias 时主标题应为 alias');
  assert.ok(html.includes('(opencode（我的工作机）)'), '括号应显示「(opencode（我的工作机）)」');
  assert.ok(!html.includes('opencode-worker-demo-0003'), '不得出现设备派生 instance_name');
});

// ── 3. 复制图标复制「引擎名（凭据名）」 ──
check('复制图标复制「引擎名（凭据名）」', () => {
  const html = fns.renderEngines({ runtimes: [mkEngine()] }, []);
  assert.ok(html.includes("copyText('opencode（我的工作机）')"), '复制值应为「opencode（我的工作机）」');
});

// ── 4. 凭据名为空：卡片显示「未命名设备」占位 ──
check('凭据名为空：显示「未命名设备」占位', () => {
  const html = fns.renderEngines({ runtimes: [mkEngine({ worker_name: '' })] }, []);
  assert.ok(html.includes('opencode（«common.unnamed_worker»）'), '空凭据名应显示占位');
  assert.ok(!html.includes('opencode-worker-demo-0003'), '不得回退显示 instance_name');
});

// ── 5. 排序：按「引擎名（凭据名）」排序 ──
check('排序按「引擎名（凭据名）」', () => {
  const list = [
    mkEngine({ id: 1, name: 'zeta', display_name: 'zeta', instance_name: 'aaa-zeta', worker_name: 'b机' }),
    mkEngine({ id: 2, name: 'alpha', display_name: 'alpha', instance_name: 'zzz-alpha', worker_name: 'a机' }),
    mkEngine({ id: 3, name: 'beta', display_name: 'beta', instance_name: 'mmm-beta', worker_name: '' }),
  ];
  // 显示名：alpha（a机）< beta（«common.unnamed_worker»） < zeta（b机）→ 2,3,1
  // 旧 instance_name 排序：aaa-zeta < mmm-beta < zzz-alpha → 1,3,2（红基线）
  const sorted = fns._sortByName(list.slice());
  assert.strictEqual(sorted.map(x => x.id).join(','), '2,3,1', '应按显示名排序');
});

// ── 6. Workers 承载引擎 chip 显示「引擎名（凭据名）」 ──
check('Workers 承载引擎 chip 显示「引擎名（凭据名）」', () => {
  const device = {
    credential_id: 5, name: 'worker-a', device_name: 'dev-worker-a', scope: 'global', owner: 'bob', health: 'online',
    worker_ip: '127.0.0.1', credential_status: 'active', last_seen_at: '',
    runtimes: [mkEngine({ id: 21, name: 'hermes', display_name: 'hermes', instance_name: 'hermes-worker-demo-0003', worker_name: '我的hermes' })],
  };
  const html = fns.renderWorkers({}, [device]);
  assert.ok(html.includes('hermes（我的hermes）'), 'chip 应显示「hermes（我的hermes）」');
  assert.ok(!html.includes('hermes-worker-demo-0003'), 'chip 不得出现设备派生 instance_name');
});

// ── 7. 引擎详情弹窗标题 ──
check('详情弹窗标题=alias || 引擎名（凭据名）', () => {
  const rt = mkEngine();
  fns._captured.length = 0;
  fns._renderEngineDetail(rt);
  const html = fns._captured.join('');
  assert.ok(html.includes('opencode（我的工作机）'), '弹窗标题应显示「opencode（我的工作机）」');
  assert.ok(!html.includes('opencode-worker-demo-0003'), '弹窗标题不得出现 instance_name');
  fns._captured.length = 0;
  fns._renderEngineDetail(mkEngine({ alias: '别名引擎' }));
  const htmlAlias = fns._captured.join('');
  assert.ok(htmlAlias.includes('别名引擎'), '有 alias 时弹窗标题应为 alias');
  assert.ok(htmlAlias.includes('(opencode（我的工作机）)'), '弹窗括号应显示「(opencode（我的工作机）)」');
});

// ── 8. alias 编辑回写段不再拼接 instance_name（源码守卫） ──
check('alias 编辑回写段不使用 instance_name（源码守卫）', () => {
  assert.ok(RUNTIMES_JS.includes('engineDisplayName'), 'runtimes.js 应引用 engineDisplayName');
  assert.ok(!RUNTIMES_JS.includes('rt.alias || rt.instance_name || rt.name'), '卡片/弹窗标题回退链不应含 instance_name');
  assert.ok(!RUNTIMES_JS.includes("parenSpan.textContent = '(' + (rt.instance_name || rt.name) + ')'"), 'alias 回写括号不得用 instance_name');
});

console.log('');
console.log('passed=' + passed + ' failed=' + failed);
if (failed > 0) process.exit(1);