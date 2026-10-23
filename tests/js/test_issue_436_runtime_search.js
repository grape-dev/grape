/*
 * Issue #436 回归测试：运行驾驶舱「设备与引擎」页 runtime 列表搜索
 *
 * 运行：node tests/js/test_issue_436_runtime_search.js
 *
 * 根因（详见 docs/issue-436/spec.md）：
 *   R1 幽灵设备过滤：设备下拉残留失效设备名（设备失联/吊销后被过滤出列表），
 *      select 回显「所有设备」但 _rtDeviceFilter 仍持旧值 → 一切搜索 0 匹配
 *   R2 搜索词未 trim：首尾空格（输入法/粘贴）导致 0 匹配
 *   R3 空状态文案复用 no_matching_log（"无匹配日志"）语义错误
 *
 * 验证点（对应 Issue #436 验收标准）：
 *   1. 搜完整/部分设备名能返回匹配 runtime 项
 *   2. 搜索词带首尾空格仍命中（trim）
 *   3. 纯空格等价于无搜索（恢复全列表）
 *   4. 幽灵设备过滤（失效设备名）不再吞掉搜索结果
 *   5. 幽灵设备过滤 + 清空搜索恢复全列表
 *   6. 有效设备下拉过滤仍生效（与搜索 AND）
 *   7. 引擎名/版本/模式字段搜索仍生效
 *   8. 无匹配时空状态文案为「无匹配引擎」（no_matching_engine）
 *   9. 清空搜索恢复全列表
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

function makeDom() {
  const elements = {};
  function mkEl(id) {
    return {
      _id: id, classList: {_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},toggle(c,f){},contains(c){return this._s.has(c);}},
      style: {}, innerHTML: '', textContent: '', value: '', checked: false,
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener: () => {}, closest: () => null, appendChild: () => {},
      remove: () => {}, setAttribute: () => {}, onclick: null,
    };
  }
  const ids = ['pageContent','obsContent','obsLastUpdate','obsAuto',
    'wk-grid','wk-pager','obs-runtime-rows','obs-runtime-count','rt-pager',
    'kpi-w-total','kpi-w-online','kpi-w-offline','kpi-r-total',
    'obs-a2a-health','obs-runtime-table'
  ];
  for (const id of ids) elements[id] = mkEl(id);
  return {
    getElementById(id) { if (!elements[id]) elements[id] = mkEl(id); return elements[id]; },
    querySelector: () => null,
    querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
    createElement: () => ({ style:{}, classList:{add(){},remove(){},toggle(){},contains(){return false;}}, textContent:'', innerHTML:'', appendChild:()=>{}, remove(){}, querySelector:()=>null, setAttribute(){} }),
    body: { appendChild: () => {} },
    documentElement: { setAttribute(){}, removeAttribute(){} },
    addEventListener: () => {},
    _elements: elements,
  };
}

// 与 tests/js/test_anomaly_page_size.js 相同的 sandbox 加载模式（真实源码 + 状态操作同环境执行）
function loadObs(dom, extraInit) {
  const src = fs.readFileSync(path.resolve(__dirname, '../../src/api/static/js/pages/observability.js'), 'utf8');
  const I18N = { t: function(k, opts) { if (opts) { let s=k; for (const [key,val] of Object.entries(opts)) s=s.replace(new RegExp('{{'+key+'}}','g'),val); return s; } return k; }, getLang: () => 'zh' };
  const sandbox = {
    document: dom, window: { _currentView:'', history:{pushState(){},replaceState(){},state:null}, location:{hash:''}, addEventListener:()=>{} },
    localStorage: { getItem:()=>null, setItem:()=>{}, removeItem:()=>{}, clear:()=>{} },
    API: { get:()=>Promise.resolve({}), post:()=>Promise.resolve({}) }, I18N,
    console: { log:()=>{}, error:()=>{}, warn:()=>{} }, fmtLocalTime: (s)=>s||'',
    fetch: ()=>Promise.resolve({ json:()=>Promise.resolve({}), text:()=>Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function(){}, AbortController: function(){ this.abort=()=>{}; this.signal={}; },
    AbortSignal: { timeout: ()=>({}) }, openRuntimeSessionModal: ()=>{},
  };
  const sKeys = Object.keys(sandbox);
  const sVals = sKeys.map(k => sandbox[k]);
  const code = src + (extraInit || '');
  // 导出验证所需的内部函数与状态
  const exportsStr = '\n; return {' +
    '_filterRuntimes: typeof _filterRuntimes !== "undefined" ? _filterRuntimes : null,' +
    '_sortedRuntimes: typeof _sortedRuntimes !== "undefined" ? _sortedRuntimes : null,' +
    '_rtSearch: typeof _rtSearch !== "undefined" ? _rtSearch : null,' +
    '_rtDeviceFilter: typeof _rtDeviceFilter !== "undefined" ? _rtDeviceFilter : null,' +
    'renderOverview: typeof renderOverview !== "undefined" ? renderOverview : null,' +
    '_rerenderRuntimeTable: typeof _rerenderRuntimeTable !== "undefined" ? _rerenderRuntimeTable : null,' +
    'setSearch: function(v){ _rtSearch = v; },' +
    'setDeviceFilter: function(v){ _rtDeviceFilter = v; },' +
    'setOps: function(ops){ _obsData = { ops: ops, realtime: null }; },' +
  '};';
  const fn = new Function(...sKeys, code + exportsStr);
  return fn(...sVals);
}

function makeOps() {
  return {
    runtimes: { total: 4, online: 4, offline: 0, by_mode: {}, list: [
      { id: 1, name: 'hermes', device_name: 'worker-buildnode-a', mode: 'remote', version: 'v1', health: 'idle', running: 0, max_concurrent: 3, avg_duration: 0, probe_status: 'ok', probe_latency: 1, probe_error: '' },
      { id: 2, name: 'opencode', device_name: 'worker-buildnode-b', mode: 'remote', version: 'v2', health: 'idle', running: 0, max_concurrent: 3, avg_duration: 0, probe_status: 'ok', probe_latency: 1, probe_error: '' },
      { id: 3, name: 'codearts', device_name: 'worker-agentnode-c', mode: 'remote', version: 'v3', health: 'idle', running: 0, max_concurrent: 3, avg_duration: 0, probe_status: 'ok', probe_latency: 1, probe_error: '' },
      { id: 4, name: 'hermes', device_name: 'worker-lapnode-d', mode: 'remote', version: 'v1', health: 'idle', running: 0, max_concurrent: 3, avg_duration: 0, probe_status: 'ok', probe_latency: 1, probe_error: '' },
    ] },
    workers: { total: 2, online: 2, offline: 0, list: [] },
  };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch(e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

console.log('Issue #436: runtime 列表搜索回归\n');

test('T1 搜完整设备名命中', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  obs.setSearch('worker-agentnode-c'); obs.setDeviceFilter('');
  assert.strictEqual(obs._filterRuntimes(rts).length, 1);
});

test('T2 搜部分设备名命中', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  obs.setSearch('buildnode-b'); obs.setDeviceFilter('');
  assert.strictEqual(obs._filterRuntimes(rts).length, 1);
});

test('T3 搜索词带首尾空格仍命中（trim）', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  obs.setSearch('  worker-agentnode-c  '); obs.setDeviceFilter('');
  assert.strictEqual(obs._filterRuntimes(rts).length, 1);
});

test('T4 纯空格等价于无搜索（全列表）', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  obs.setSearch('   '); obs.setDeviceFilter('');
  assert.strictEqual(obs._filterRuntimes(rts).length, 4);
});

test('T5 幽灵设备过滤（失效设备名）不吞搜索结果', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  // 设备 worker-foo-xxx 不在当前数据源（已失联被过滤）——修复前此组合 0 匹配
  obs.setSearch('build'); obs.setDeviceFilter('worker-foo-xxx');
  assert.strictEqual(obs._filterRuntimes(rts).length, 2);
});

test('T6 幽灵设备过滤 + 清空搜索恢复全列表', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  obs.setSearch(''); obs.setDeviceFilter('worker-foo-xxx');
  assert.strictEqual(obs._filterRuntimes(rts).length, 4);
});

test('T7 有效设备下拉过滤仍生效', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  obs.setSearch(''); obs.setDeviceFilter('worker-agentnode-c');
  assert.strictEqual(obs._filterRuntimes(rts).length, 1);
});

test('T8 有效设备过滤与搜索 AND 组合', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  obs.setSearch('hermes'); obs.setDeviceFilter('worker-agentnode-c');
  assert.strictEqual(obs._filterRuntimes(rts).length, 0);
});

test('T9 引擎名/版本/模式字段搜索仍生效', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  obs.setDeviceFilter('');
  obs.setSearch('hermes');
  assert.strictEqual(obs._filterRuntimes(rts).length, 2);
  obs.setSearch('v3');
  assert.strictEqual(obs._filterRuntimes(rts).length, 1);
  obs.setSearch('remote');
  assert.strictEqual(obs._filterRuntimes(rts).length, 4);
});

test('T10 无匹配返回空数组（空状态渲染入口）', () => {
  const obs = loadObs(makeDom());
  const rts = obs._sortedRuntimes(makeOps().runtimes);
  obs.setSearch('nonexistent-device'); obs.setDeviceFilter('');
  assert.deepStrictEqual(obs._filterRuntimes(rts), []);
});

test('T11 空状态文案使用 no_matching_engine（渲染级）', () => {
  const dom = makeDom();
  const zh = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../src/api/static/locales/zh/observability.json'), 'utf8'));
  // I18N 用真实 zh locale（fake harness 默认桩返回 key 本身，验证不了文案替换）
  const src = fs.readFileSync(path.resolve(__dirname, '../../src/api/static/js/pages/observability.js'), 'utf8');
  const I18N = { t: function(k) { const kk = k.replace(/^observability\./, ''); return (zh[kk] !== undefined) ? zh[kk] : k; } };
  const sandbox = {
    document: dom, window: { history:{pushState(){},replaceState(){},state:null}, location:{hash:''}, addEventListener:()=>{} },
    localStorage: { getItem:()=>null, setItem:()=>{}, removeItem:()=>{}, clear:()=>{} },
    API: { get:()=>Promise.resolve({}), post:()=>Promise.resolve({}) }, I18N,
    console: { log:()=>{}, error:()=>{}, warn:()=>{} }, fmtLocalTime: (s)=>s||'',
    fetch: ()=>Promise.resolve({ json:()=>Promise.resolve({}), text:()=>Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function(){}, AbortController: function(){ this.abort=()=>{}; this.signal={}; },
    AbortSignal: { timeout: ()=>({}) }, openRuntimeSessionModal: ()=>{},
  };
  const sKeys = Object.keys(sandbox);
  const sVals = sKeys.map(k => sandbox[k]);
  const tail = `
; _obsData = { ops: ${JSON.stringify(makeOps())}, realtime: null };
document.getElementById('obsContent').innerHTML = renderOverview(_obsData.ops, {});
_rtSearch = 'nonexistent-device'; _runtimePage = 0; _rerenderRuntimeTable();
`;
  const fn = new Function(...sKeys, src + tail);
  fn(...sVals);
  const rows = dom._elements['obs-runtime-rows'];
  assert.ok(rows.innerHTML.indexOf(zh['no_matching_engine']) >= 0,
    'runtime 表空状态应包含 no_matching_engine 文案，实际: ' + rows.innerHTML.substring(0, 120));
  assert.ok(rows.innerHTML.indexOf(zh['no_matching_log']) < 0,
    'runtime 表空状态不应再复用 no_matching_log 文案');
});

test('T12 i18n 键 zh/en 对齐', () => {
  const zh = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../src/api/static/locales/zh/observability.json'), 'utf8'));
  const en = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../src/api/static/locales/en/observability.json'), 'utf8'));
  assert.ok(zh['no_matching_engine'], 'zh 缺 no_matching_engine');
  assert.ok(en['no_matching_engine'], 'en 缺 no_matching_engine');
  assert.strictEqual(zh['no_matching_engine'], '无匹配引擎');
  assert.strictEqual(en['no_matching_engine'], 'No matching engines');
});

test('T13 同 sandbox 全链路：初始 4 行 → 搜索 → 计数正确（renderOverview 渲染骨架 + _rerenderRuntimeTable）', () => {
  const dom = makeDom();
  const src = fs.readFileSync(path.resolve(__dirname, '../../src/api/static/js/pages/observability.js'), 'utf8');
  const I18N = { t: function(k, opts) { if (opts) { let s=k; for (const [key,val] of Object.entries(opts)) s=s.replace(new RegExp('{{'+key+'}}','g'),val); return s; } return k; } };
  const sandbox = {
    document: dom, window: { history:{pushState(){},replaceState(){},state:null}, location:{hash:''}, addEventListener:()=>{} },
    localStorage: { getItem:()=>null, setItem:()=>{}, removeItem:()=>{}, clear:()=>{} },
    API: { get:()=>Promise.resolve({}), post:()=>Promise.resolve({}) }, I18N,
    console: { log:()=>{}, error:()=>{}, warn:()=>{} }, fmtLocalTime: (s)=>s||'',
    fetch: ()=>Promise.resolve({ json:()=>Promise.resolve({}), text:()=>Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function(){}, AbortController: function(){ this.abort=()=>{}; this.signal={}; },
    AbortSignal: { timeout: ()=>({}) }, openRuntimeSessionModal: ()=>{},
  };
  const sKeys = Object.keys(sandbox);
  const sVals = sKeys.map(k => sandbox[k]);
  const tail = `
; _obsData = { ops: ${JSON.stringify(makeOps())}, realtime: null };
document.getElementById('obsContent').innerHTML = renderOverview(_obsData.ops, {});
__rowsIn = function(html){ return (html.match(/<tr id="rt-/g) || []).length; };
var __initial = __rowsIn(document.getElementById('obsContent').innerHTML);
_rtSearch = 'build'; _runtimePage = 0; _rerenderRuntimeTable();
var __searched = __rowsIn(document.getElementById('obs-runtime-rows').innerHTML);
var __cnt = document.getElementById('obs-runtime-count').textContent;
_rtSearch = ''; _rerenderRuntimeTable();
var __cleared = __rowsIn(document.getElementById('obs-runtime-rows').innerHTML);
return { initial: __initial, searched: __searched, cleared: __cleared, cnt: __cnt };
`;
  const fn = new Function(...sKeys, src + tail);
  const h = fn(...sVals);
  assert.strictEqual(h.initial, 4, 'renderOverview 初始应渲染 4 行 runtime');
  assert.strictEqual(h.searched, 2, '搜 build 应剩 2 行');
  assert.strictEqual(h.cnt, '2/4', '搜索后计数应为 2/4');
  assert.strictEqual(h.cleared, 4, '清空后应恢复 4 行');
});

test('T14 同 sandbox 全链路：搜索 → 行数 1 → 清空 → 行数 4（模拟页面内联事件序列）', () => {
  const dom = makeDom();
  const init = `
; _obsData = { ops: ${JSON.stringify(makeOps())}, realtime: null };
document.getElementById('obsContent').innerHTML = renderOverview(_obsData.ops, {});
_rtSearch = 'worker-agentnode-c'; _runtimePage = 0; _rerenderRuntimeTable();
__countRows = function(){ return (document.getElementById('obs-runtime-rows').innerHTML.match(/<tr id="rt-/g) || []).length; };
__cnt = function(){ return document.getElementById('obs-runtime-count').textContent; };
`;
  const src = fs.readFileSync(path.resolve(__dirname, '../../src/api/static/js/pages/observability.js'), 'utf8');
  const I18N = { t: function(k, opts) { if (opts) { let s=k; for (const [key,val] of Object.entries(opts)) s=s.replace(new RegExp('{{'+key+'}}','g'),val); return s; } return k; } };
  const sandbox = {
    document: dom, window: { history:{pushState(){},replaceState(){},state:null}, location:{hash:''}, addEventListener:()=>{} },
    localStorage: { getItem:()=>null, setItem:()=>{}, removeItem:()=>{}, clear:()=>{} },
    API: { get:()=>Promise.resolve({}), post:()=>Promise.resolve({}) }, I18N,
    console: { log:()=>{}, error:()=>{}, warn:()=>{} }, fmtLocalTime: (s)=>s||'',
    fetch: ()=>Promise.resolve({ json:()=>Promise.resolve({}), text:()=>Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function(){}, AbortController: function(){ this.abort=()=>{}; this.signal={}; },
    AbortSignal: { timeout: ()=>({}) }, openRuntimeSessionModal: ()=>{},
  };
  const sKeys = Object.keys(sandbox);
  const sVals = sKeys.map(k => sandbox[k]);
  const tail = init + `
; return { rows: __countRows(), cnt: __cnt(),
    clear: function(){ _rtSearch=''; _rerenderRuntimeTable(); return __countRows(); },
    cnt2: function(){ return document.getElementById('obs-runtime-count').textContent; } };
`;
  const fn = new Function(...sKeys, src + tail);
  const h = fn(...sVals);
  assert.strictEqual(h.rows, 1, '搜设备名应剩 1 行');
  assert.strictEqual(h.cnt, '1/4', '计数应为 1/4');
  assert.strictEqual(h.clear(), 4, '清空后应恢复 4 行');
  assert.strictEqual(h.cnt2(), '4/4', '清空后计数应为 4/4');
});

console.log('\n结果: ' + passed + ' 通过, ' + failed + ' 失败');
process.exit(failed ? 1 : 0);
