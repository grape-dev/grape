/*
 * Issue #465（追加）：设备与引擎页 Worker 设备卡片名称与 runtimes 表「所属设备」列口径一致
 *
 * 运行：node tests/js/test_issue_465_worker_card_name.js
 *
 * 背景（PR #650 之后用户反馈的遗留对齐点）：
 *   - ops/overview workers 块返回 name=rc.name（凭据自定义名）、device_name=rc.device_name（边端自报原始名）
 *   - runtimes 表「所属设备」列（PR #650 已修）：rc.name 优先、空回退 rc.device_name
 *   - 但 _workerCardHTML 标题渲染 wk.device_name || wk.name —— 原始名优先，与 runtimes 列口径相反
 *
 * 修复：Worker 卡片标题改为 wk.name 优先、空回退 wk.device_name；
 *   同步对齐 _sortedWorkers 排序键（name 优先，与卡片显示名一致）。
 *
 * 验证点（renderOverview 全链路渲染出 wk-grid HTML 后断言）：
 *   1. 远端 worker：卡片标题显示 rc.name（自定义名），不显示 device_name
 *   2. rc.name 为空的存量 worker：回退显示 device_name，不出现空标题
 *   3. 本机直连 worker（id=0，name=device_name=hostname）：显示 hostname，不受影响
 *   4. 卡片排序按显示名（name 优先），与显示口径一致
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}

function makeDom() {
  const elements = {};
  function mk(id) {
    return {
      _id: id, style: {}, innerHTML: '', textContent: '', value: '',
      classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, toggle() {}, contains(c) { return this._s.has(c); } },
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener: () => {}, closest: () => null, appendChild: () => {},
      remove: () => {}, setAttribute: () => {}, onclick: null,
    };
  }
  return {
    getElementById(id) { return elements[id] || (elements[id] = mk(id)); },
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => mk('_new'),
    body: { appendChild: (el) => { if (el && el.id) elements[el.id] = el; } },
    documentElement: { setAttribute() {}, removeAttribute() {} },
    addEventListener: () => {},
    _elements: elements,
  };
}

// 直接从 observability.js 源码加载目标函数（沙箱 eval，只导出需要的符号）
function loadObsFns() {
  const src = fs.readFileSync(
    path.resolve(__dirname, '../../src/api/static/js/pages/observability.js'), 'utf8');
  const I18N = { t: (k, p) => '«' + k + '»' + (p && p.id !== undefined ? ':' + p.id : ''), getLang: () => 'zh' };
  const sandbox = {
    document: makeDom(),
    window: { _currentView: 'observability', history: { pushState() {}, replaceState() {}, state: null }, location: { hash: '' }, addEventListener: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} },
    API: { get: () => Promise.resolve({}), post: () => Promise.resolve({}) },
    I18N,
    console: { log: () => {}, error: () => {}, warn: () => {} },
    fmtLocalTime: (s) => s || '',
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function () {}, AbortController: function () { this.abort = () => {}; this.signal = {}; },
    AbortSignal: { timeout: () => ({}) }, openRuntimeSessionModal: () => {},
  };
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  const exportsStr = '\n; return {' +
    '_workerCardHTML: typeof _workerCardHTML !== "undefined" ? _workerCardHTML : null,' +
    '_sortedWorkers: typeof _sortedWorkers !== "undefined" ? _sortedWorkers : null,' +
    'renderOverview: typeof renderOverview !== "undefined" ? renderOverview : null' +
    '};';
  const fn = new Function(...keys, src + exportsStr);
  return fn(...vals);
}

// ops/overview workers 块形态（dashboard.py:340-357：name=rc.name，device_name=rc.device_name）
function _wk(id, name, device_name) {
  return {
    id, name: name || '', device_name: device_name || '',
    status: 'active', health: 'online',
    last_seen_at: '2026-10-26T08:00:00+00:00', last_ip: '192.168.0.' + id,
    cpu_percent: 10, memory_percent: 20, disk_percent: 30, tmp_percent: 5,
    bandwidth_percent: 1, bandwidth_mbps: 12.5, load_avg: '0.5', owner_name: '',
  };
}

console.log('Issue #465 追加：Worker 设备卡片名称口径（name 优先，回退 device_name）');
const fns = loadObsFns();

// --- 用例 1：远端 worker 卡片标题显示 rc.name（自定义名） ---
check('远端 worker：卡片标题显示自定义名 rc.name（不显示原始 device_name）', () => {
  const html = fns._workerCardHTML(_wk(11, '我的工作机', 'worker-os-hostname'));
  assert.ok(html.includes('我的工作机'), '卡片标题应包含自定义名「我的工作机」');
  assert.ok(!html.includes('worker-os-hostname'), '卡片标题不应显示原始设备名 device_name');
});

// --- 用例 2：rc.name 为空回退 device_name，不出现空标题 ---
check('rc.name 为空：回退显示 device_name，不出现空标题', () => {
  const html = fns._workerCardHTML(_wk(12, '', 'worker-buildnode-b'));
  assert.ok(html.includes('worker-buildnode-b'), 'name 为空应回退显示 device_name');
  // 标题 <b> 不应为空：回退链终点是 I18N 占位（id 兜底）
  assert.ok(!/<b style="font-size:13px"><\/b>/.test(html), '标题 <b> 不得为空');
});

// --- 用例 3：本机直连 worker（id=0，name=device_name=hostname）不受影响 ---
check('本机直连 worker（name=device_name=hostname）：显示 hostname 不受影响', () => {
  const html = fns._workerCardHTML(_wk(0, 'worker-node-a', 'worker-node-a'));
  assert.ok(html.includes('worker-node-a'), '本机直连应显示 hostname');
  // wid=id=0 时 id != null 成立，卡片 id 用 0，不受名称口径影响
  assert.ok(html.includes('id="wk-0"'), '卡片 DOM id 应为 wk-0（按 worker id）');
});

// --- 用例 4：排序键与显示口径一致（name 优先） ---
check('_sortedWorkers 排序键与卡片显示名口径一致（name 优先）', () => {
  const w = { list: [
    _wk(21, 'zeta-工作机', 'worker-alpha'),
    _wk(22, '', 'beta-node'),
    _wk(23, 'Alpha工作站', 'worker-gamma'),
  ] };
  const sorted = fns._sortedWorkers(w);
  // 显示名：Alpha工作站 / beta-node（回退）/ zeta-工作机 → 按显示名排序
  assert.strictEqual(sorted.map(x => x.id).join(','), '23,22,21',
    '排序应按 name 优先的显示名（alpha < beta < zeta），而不是按 device_name（alpha < beta < gamma 相同前缀不可分辨）');
});

// --- 用例 5：renderOverview 全链路——wk-grid 渲染自定义名 ---
check('renderOverview 全链路：wk-grid 内 Worker 卡片渲染自定义名（与 runtimes 列口径一致）', () => {
  const ops = {
    workers: { total: 2, online: 2, offline: 0, list: [
      _wk(31, '我的工作机', 'worker-os-hostname'),
      _wk(32, '', 'worker-buildnode-b'),
    ] },
    runtimes: { total: 0, online: 0, offline: 0, list: [] },
  };
  const html = fns.renderOverview(ops, {});
  assert.ok(html.includes('我的工作机'), 'wk-grid 应渲染自定义名「我的工作机」');
  assert.ok(!html.includes('worker-os-hostname'), 'wk-grid 不应渲染原始设备名');
  assert.ok(html.includes('worker-buildnode-b'), 'name 为空设备应回退渲染 device_name');
});

// --- 用例 6：排序稳定性——同一设备多引擎时卡片位置稳定 ---
check('排序键名优先后卡片位置稳定（渲染两次结果一致）', () => {
  const w = { list: [_wk(41, '我的工作机', 'worker-os-hostname'), _wk(42, '第二台', 'worker-2')] };
  const ops = { workers: w, runtimes: { total: 0, online: 0, offline: 0, list: [] } };
  const h1 = fns.renderOverview(ops, {});
  const h2 = fns.renderOverview(ops, {});
  assert.strictEqual(h1, h2, '相同数据两次渲染应产生相同 HTML（排序稳定）');
});

console.log('');
console.log('passed=' + passed + ' failed=' + failed);
if (failed > 0) process.exit(1);
