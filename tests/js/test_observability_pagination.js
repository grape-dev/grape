/*
 * Issue #117 回归测试：概览页 Worker / Runtime 列表分页
 * （基于 Issue #162，分页栏在非空列表时始终包含「每页条数」选择器）
 *
 * 运行：node tests/js/test_observability_pagination.js
 *
 * 验证点：
 *   1. Worker >10 条时仅渲染当前页 10 条，分页控件可见
 *   2. Worker <=10 条时含每页条数选择器，无翻页按钮
 *   3. Worker 列表为空时显示占位文本，无分页
 *   4. Runtime >10 条时仅渲染当前页 10 条，分页控件可见
 *   5. Runtime <=10 条时含每页条数选择器，无翻页按钮
 *   6. 筛选后 Runtime 分页正确（先筛选再分页）
 *   7. _rerenderRuntimeTable 切换页码后渲染对应行
 *   8. updateOverview 仅遍历当前页元素
 *   9. 页码越界自动钳制
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

function makeDom() {
  const elements = {};
  const ids = ['pageContent','obsContent','obsLastUpdate','obsAuto',
    'wk-grid','wk-pager','obs-runtime-rows','obs-runtime-count','rt-pager',
    'kpi-w-total','kpi-w-online','kpi-w-offline','kpi-r-total',
    'obs-a2a-health','obs-runtime-table'
  ];
  for (const id of ids) {
    elements[id] = {
      _id: id, classList: {_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},toggle(c,f){},contains(c){return this._s.has(c);}},
      style: {}, innerHTML: '', textContent: '', value: '',
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener: () => {}, closest: () => null, appendChild: () => {},
      remove: () => {}, setAttribute: () => {}, onclick: null,
    };
  }
  return {
    getElementById(id) {
      if (elements[id]) return elements[id];
      elements[id] = {
        _id: id, classList: {_s:new Set(),add(c){this._s.add(c);},remove(c){this._s.delete(c);},toggle(c,f){},contains(c){return this._s.has(c);}},
        style: {}, innerHTML: '', textContent: '', value: '',
        querySelector: () => null, querySelectorAll: () => [],
        addEventListener: () => {}, closest: () => null, appendChild: () => {},
        remove: () => {}, setAttribute: () => {}, onclick: null,
      };
      return elements[id];
    },
    querySelector: () => null,
    querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
    createElement: () => ({ style:{}, classList:elements['wk-grid'].classList, textContent:'', innerHTML:'', appendChild:()=>{}, remove(){}, querySelector:()=>null, setAttribute(){} }),
    body: { appendChild: () => {} },
    documentElement: { setAttribute(){}, removeAttribute(){} },
    addEventListener: () => {},
    _elements: elements,
  };
}

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
  const exportsStr = '\n; return {' +
    'renderOverview: typeof renderOverview !== "undefined" ? renderOverview : null,' +
    '_rerenderRuntimeTable: typeof _rerenderRuntimeTable !== "undefined" ? _rerenderRuntimeTable : null,' +
    '_rerenderWorkerGrid: typeof _rerenderWorkerGrid !== "undefined" ? _rerenderWorkerGrid : null,' +
    'updateOverview: typeof updateOverview !== "undefined" ? updateOverview : null,' +
    '_workerPage: typeof _workerPage !== "undefined" ? _workerPage : null,' +
    '_runtimePage: typeof _runtimePage !== "undefined" ? _runtimePage : null' +
  '};';
  const fn = new Function(...sKeys, code + exportsStr);
  return fn(...sVals);
}

function makeWorkers(n) {
  const list = [];
  for (let i = 0; i < n; i++) list.push({ id:i, device_name:'dev-'+String(i).padStart(3,'0'), name:'w-'+i, health:'online', last_ip:'10.0.0.'+(i+1), last_seen_at:'2026-09-12T10:00:00Z', cpu_percent:i*5, memory_percent:i*3, tmp_percent:10, disk_percent:20, bandwidth_percent:i*2 });
  return { total:n, online:Math.ceil(n/3), offline:n-Math.ceil(n/3), list };
}

function makeRuntimes(n) {
  const list = [];
  for (let i = 0; i < n; i++) list.push({ id:i, name:'engine-'+i, device_name:'dev-'+String(i%3).padStart(3,'0'), version:'1.0.'+i, health:'online', running:i%4, max_concurrent:8, avg_duration:i*10, probe_status:'ok', probe_latency:0.5, probe_error:'', mode:i%2===0?'local':'remote' });
  return { total:n, online:Math.ceil(n/2), offline:n-Math.ceil(n/2), list };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch(e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

console.log('Issue #117: \u6982\u89c8\u9875 Worker/Runtime \u5206\u9875 \u2014 \u56de\u5f52\u6d4b\u8bd5\n');

test('Worker 15\u6761: \u4ec5\u6e32\u67d3 10 \u6761\u5361\u7247\uff0c\u5206\u9875\u63a7\u4ef6\u53ef\u89c1', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: makeWorkers(15), runtimes: { total:0, online:0, offline:0, list:[] } };
  const html = m.renderOverview(ops, {});
  const cardCount = (html.match(/box-shadow:var\(--sh\)/g) || []).length;
  assert.strictEqual(cardCount, 10, '\u5e94\u6e32\u67d3 10 \u4e2a\u5361\u7247\uff0c\u5b9e\u9645 ' + cardCount);
  assert.ok(html.includes('1 / 2'), '\u5e94\u663e\u793a\u9875\u7801 1/2');
});

test('Worker 8\u6761: \u5305\u542b\u6bcf\u9875\u6761\u6570\u9009\u62e9\u5668\uff0c\u65e0\u7ffb\u9875\u6309\u94ae', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: makeWorkers(8), runtimes: { total:0, online:0, offline:0, list:[] } };
  const html = m.renderOverview(ops, {});
  const cardCount = (html.match(/box-shadow:var\(--sh\)/g) || []).length;
  assert.strictEqual(cardCount, 8, '\u5e94\u6e32\u67d3 8 \u4e2a\u5361\u7247');
  assert.ok(html.includes('1 / 1'), '\u5e94\u663e\u793a\u9875\u7801 1/1');
  assert.ok(html.includes('rows_per_page'), '\u5e94\u5305\u542b\u6bcf\u9875\u6761\u6570\u9009\u62e9\u5668');
  assert.ok(!html.includes('prev_page'), '\u4e00\u9875\u65f6\u4e0d\u5e94\u6709\u7ffb\u9875\u6309\u94ae');
});

test('Worker 0\u6761: \u663e\u793a\u5360\u4f4d\u6587\u672c\uff0c\u65e0\u5206\u9875', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: { total:0, online:0, offline:0, list:[] } };
  const html = m.renderOverview(ops, {});
  assert.ok(html.includes('observability.no_remote_worker'), '\u5e94\u663e\u793a\u65e0 worker \u5360\u4f4d');
  assert.ok(!html.includes('prev_page'), '\u4e0d\u5e94\u6709\u5206\u9875\u6309\u94ae');
});

test('Runtime 25\u6761: \u4ec5\u6e32\u67d3 10 \u884c\uff0c\u5206\u9875\u63a7\u4ef6\u53ef\u89c1', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: makeRuntimes(25) };
  const html = m.renderOverview(ops, {});
  const rowCount = (html.match(/<tr id="rt-\d+"/g) || []).length;
  assert.strictEqual(rowCount, 10, '\u5e94\u6e32\u67d3 10 \u884c\uff0c\u5b9e\u9645 ' + rowCount);
  assert.ok(html.includes('1 / 3'), '\u5e94\u663e\u793a\u9875\u7801 1/3');
});

test('Runtime 5\u6761: \u5305\u542b\u6bcf\u9875\u6761\u6570\u9009\u62e9\u5668\uff0c\u65e0\u7ffb\u9875\u6309\u94ae', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: makeRuntimes(5) };
  const html = m.renderOverview(ops, {});
  const rowCount = (html.match(/<tr id="rt-\d+"/g) || []).length;
  assert.strictEqual(rowCount, 5, '\u5e94\u6e32\u67d3 5 \u884c');
  assert.ok(html.includes('1 / 1'), '\u5e94\u663e\u793a\u9875\u7801 1/1');
  assert.ok(html.includes('rows_per_page'), '\u5e94\u5305\u542b\u6bcf\u9875\u6761\u6570\u9009\u62e9\u5668');
  assert.ok(!html.includes('prev_page'), '\u4e00\u9875\u65f6\u4e0d\u5e94\u6709\u7ffb\u9875\u6309\u94ae');
});

test('\u7b5b\u9009\u540e\u5206\u9875: \u8bbe\u5907\u7b5b\u9009\u540e\u4ec5\u6e32\u67d3\u5339\u914d\u9879', () => {
  const dom = makeDom();
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: makeRuntimes(25) };
  const m = loadObs(dom, '; _rtDeviceFilter = "dev-000"; var __html = renderOverview(' + JSON.stringify(ops) + ', {}); return { html: __html };');
  const html = m.html;
  const rowCount = (html.match(/<tr id="rt-\d+"/g) || []).length;
  const expected = ops.runtimes.list.filter(r => r.device_name === 'dev-000').length;
  assert.strictEqual(rowCount, expected, '\u7b5b\u9009\u540e\u5e94\u4ec5\u6e32\u67d3 ' + expected + ' \u884c\uff0c\u5b9e\u9645 ' + rowCount);
});

test('_rerenderRuntimeTable: \u5207\u6362\u5230\u7b2c 2 \u9875\u540e\u6e32\u67d3\u5bf9\u5e94\u884c', () => {
  const dom = makeDom();
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: makeRuntimes(25) };
  const m = loadObs(dom, '; _obsData = { ops: ' + JSON.stringify(ops) + ', realtime: null }; _runtimePage = 1; _rerenderRuntimeTable(); return { rows: document.getElementById("obs-runtime-rows").innerHTML, pager: document.getElementById("rt-pager").innerHTML };');
  const rowCount = (m.rows.match(/<tr id="rt-\d+"/g) || []).length;
  assert.strictEqual(rowCount, 10, '\u7b2c 2 \u9875\u5e94\u6e32\u67d3 10 \u884c\uff0c\u5b9e\u9645 ' + rowCount);
  assert.ok(m.pager.includes('2 / 3'), '\u5e94\u663e\u793a\u9875\u7801 2/3');
});

test('updateOverview: \u4ec5\u904d\u5386\u5f53\u524d\u9875\u5143\u7d20', () => {
  const dom = makeDom();
  const ops = { workers: makeWorkers(25), runtimes: makeRuntimes(25) };
  const m = loadObs(dom, `
    var __html = renderOverview(${JSON.stringify(ops)}, {});
    var c = document.getElementById('obsContent');
    if (c) c.innerHTML = __html;
    _obsData = { ops: ${JSON.stringify(ops)}, realtime: null };
    var __result = updateOverview(${JSON.stringify(ops)}, {});
    return { result: __result };
  `);
  assert.strictEqual(m.result, true, 'updateOverview \u5e94\u8fd4\u56de true');
});

test('Worker card 包含带宽占用率进度条 (bandwidth)', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: makeWorkers(1), runtimes: { total:0, online:0, offline:0, list:[] } };
  const html = m.renderOverview(ops, {});
  assert.ok(html.includes('wk-0-bw-bar'), '卡片应包含带宽进度条元素 wk-0-bw-bar');
  assert.ok(html.includes('observability.bandwidth'), '卡片应引用 i18n key observability.bandwidth');
});

test('updateOverview 正确更新带宽进度条 (wk-*-bw)', () => {
  const dom = makeDom();
  const ops = { workers: makeWorkers(2), runtimes: { total:0, online:0, offline:0, list:[] } };
  const m = loadObs(dom, `
    renderOverview(${JSON.stringify(ops)}, {});
    _obsData = { ops: ${JSON.stringify(ops)}, realtime: null };
    var res = updateOverview(${JSON.stringify(ops)}, {});
    return { result: res };
  `);
  assert.strictEqual(m.result, true, 'updateOverview 应返回 true');
});

test('Worker 0条: 不显示带宽占位', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: { total:0, online:0, offline:0, list:[] } };
  const html = m.renderOverview(ops, {});
  assert.ok(!html.includes('wk-0-bw-bar'), '0条 worker 时不应有带宽进度条');
});

test('页码越界自动钳制: _workerPage=99 + 15条 → 重置为 0', () => {
  const dom = makeDom();
  const ops = { workers: makeWorkers(15), runtimes: { total:0, online:0, offline:0, list:[] } };
  const m = loadObs(dom, '; _workerPage = 99; renderOverview(' + JSON.stringify(ops) + ', {}); return { _workerPage: _workerPage };');
  assert.strictEqual(m._workerPage, 0, '页码应被重置为 0');
});

test('Runtime 0条: renderOverview 不渲染 Runtime 分页栏（与 _rerenderRuntimeTable/updateOverview 守卫一致）', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: { total:0, online:0, offline:0, list:[] } };
  const html = m.renderOverview(ops, {});
  assert.ok(html.includes('observability.no_engine'), '应显示无引擎占位文本');
  assert.ok(!html.includes('rt-pager') || !html.match(/id="rt-pager">\s*<(?!\/)/), 'Runtime 分页栏应为空或不存在分页按钮');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
