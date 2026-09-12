/*
 * Issue #123 回归测试：异常任务 Tab 每页条数选择器
 *
 * 运行：node tests/js/test_anomaly_page_size.js
 *
 * 验证点：
 *   1. 默认每页 10 条：25 条 failed 任务仅渲染 10 条，页码 1/3
 *   2. 每页条数选择器存在，含 10/20/50 三个选项
 *   3. 默认选中 10
 *   4. 切换为 20 条/页：25 条仅渲染 20 条，页码 1/2
 *   5. 切换为 50 条/页：25 条全部渲染，页码 1/1
 *   6. 切换后页码重置为 0（第 1 页）
 *   7. 空列表：不渲染分页栏和选择器
 *   8. 不足一页（5 条）：选择器仍可见，无翻页按钮
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
    'renderAnomalyTab: typeof renderAnomalyTab !== "undefined" ? renderAnomalyTab : null,' +
    'anomalyPageSize: typeof anomalyPageSize !== "undefined" ? anomalyPageSize : null,' +
    '_anomalyPage: typeof _anomalyPage !== "undefined" ? _anomalyPage : null' +
  '};';
  const fn = new Function(...sKeys, code + exportsStr);
  return fn(...sVals);
}

function makeFailedTasks(n) {
  const list = [];
  for (let i = 0; i < n; i++) {
    list.push({
      id: 'task-' + i,
      issue_id: '#' + (100 + i),
      event_seq: i + 1,
      repo_key: 'gitcode:org/repo',
      agent_type: 'coding',
      started_at: '2026-09-12T10:00:00Z',
      completed_at: '2026-09-12T10:05:00Z',
      errors: ['timeout'],
      exit_code: 1,
      result_text: 'Task failed: reason ' + i,
    });
  }
  return list;
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch(e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

console.log('Issue #123: \u5f02\u5e38\u4efb\u52a1 Tab \u6bcf\u9875\u6761\u6570\u9009\u62e9\u5668 \u2014 \u56de\u5f52\u6d4b\u8bd5\n');

test('\u9ed8\u8ba4\u6bcf\u9875 10 \u6761: 25 \u6761 failed \u4ec5\u6e32\u67d3 10 \u6761\uff0c\u9875\u7801 1/3', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { tasks: { failed_tasks: makeFailedTasks(25), stuck_tasks: [] } };
  const container = dom.getElementById('obsContent');
  m.renderAnomalyTab(container, ops);
  const html = container.innerHTML;
  const rowCount = (html.match(/openTaskLogModal/g) || []).length;
  assert.strictEqual(rowCount, 10, '\u5e94\u6e32\u67d3 10 \u884c\uff0c\u5b9e\u9645 ' + rowCount);
  assert.ok(html.includes('1 / 3'), '\u5e94\u663e\u793a\u9875\u7801 1/3');
});

test('\u6bcf\u9875\u6761\u6570\u9009\u62e9\u5668\u5b58\u5728\uff0c\u542b 10/20/50 \u4e09\u4e2a\u9009\u9879', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { tasks: { failed_tasks: makeFailedTasks(25), stuck_tasks: [] } };
  const container = dom.getElementById('obsContent');
  m.renderAnomalyTab(container, ops);
  const html = container.innerHTML;
  assert.ok(html.includes('<option value="10"'), '\u5e94\u542b 10 \u9009\u9879');
  assert.ok(html.includes('<option value="20"'), '\u5e94\u542b 20 \u9009\u9879');
  assert.ok(html.includes('<option value="50"'), '\u5e94\u542b 50 \u9009\u9879');
  assert.ok(html.includes('observability.rows_per_page'), '\u5e94\u542b rows_per_page \u6807\u7b7e');
});

test('\u9ed8\u8ba4\u9009\u4e2d 10', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { tasks: { failed_tasks: makeFailedTasks(25), stuck_tasks: [] } };
  const container = dom.getElementById('obsContent');
  m.renderAnomalyTab(container, ops);
  const html = container.innerHTML;
  assert.ok(html.includes('<option value="10" selected'), '10 \u5e94\u88ab\u9009\u4e2d');
  assert.ok(!html.includes('<option value="20" selected'), '20 \u4e0d\u5e94\u88ab\u9009\u4e2d');
  assert.ok(!html.includes('<option value="50" selected'), '50 \u4e0d\u5e94\u88ab\u9009\u4e2d');
});

test('\u5207\u6362\u4e3a 20 \u6761/\u9875: 25 \u6761\u4ec5\u6e32\u67d3 20 \u6761\uff0c\u9875\u7801 1/2', () => {
  const dom = makeDom();
  const ops = { tasks: { failed_tasks: makeFailedTasks(25), stuck_tasks: [] } };
  const m = loadObs(dom, '; anomalyPageSize = 20; renderAnomalyTab(document.getElementById("obsContent"), ' + JSON.stringify(ops) + '); return { html: document.getElementById("obsContent").innerHTML };');
  const html = m.html;
  const rowCount = (html.match(/openTaskLogModal/g) || []).length;
  assert.strictEqual(rowCount, 20, '\u5e94\u6e32\u67d3 20 \u884c\uff0c\u5b9e\u9645 ' + rowCount);
  assert.ok(html.includes('1 / 2'), '\u5e94\u663e\u793a\u9875\u7801 1/2');
  assert.ok(html.includes('<option value="20" selected'), '20 \u5e94\u88ab\u9009\u4e2d');
});

test('\u5207\u6362\u4e3a 50 \u6761/\u9875: 25 \u6761\u5168\u90e8\u6e32\u67d3\uff0c\u9875\u7801 1/1', () => {
  const dom = makeDom();
  const ops = { tasks: { failed_tasks: makeFailedTasks(25), stuck_tasks: [] } };
  const m = loadObs(dom, '; anomalyPageSize = 50; renderAnomalyTab(document.getElementById("obsContent"), ' + JSON.stringify(ops) + '); return { html: document.getElementById("obsContent").innerHTML };');
  const html = m.html;
  const rowCount = (html.match(/openTaskLogModal/g) || []).length;
  assert.strictEqual(rowCount, 25, '\u5e94\u6e32\u67d3 25 \u884c\uff0c\u5b9e\u9645 ' + rowCount);
  assert.ok(html.includes('1 / 1'), '\u5e94\u663e\u793a\u9875\u7801 1/1');
  assert.ok(html.includes('<option value="50" selected'), '50 \u5e94\u88ab\u9009\u4e2d');
});

test('\u5207\u6362\u540e\u9875\u7801\u91cd\u7f6e\u4e3a 0\uff08\u7b2c 1 \u9875\uff09', () => {
  const dom = makeDom();
  const ops = { tasks: { failed_tasks: makeFailedTasks(25), stuck_tasks: [] } };
  const m = loadObs(dom, '; _anomalyPage = 2; anomalyPageSize = 20; renderAnomalyTab(document.getElementById("obsContent"), ' + JSON.stringify(ops) + '); return { _anomalyPage: _anomalyPage };');
  assert.strictEqual(m._anomalyPage, 0, '\u5207\u6362\u540e\u9875\u7801\u5e94\u91cd\u7f6e\u4e3a 0');
});

test('\u7a7a\u5217\u8868: \u4e0d\u6e32\u67d3\u5206\u9875\u680f\u548c\u9009\u62e9\u5668', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { tasks: { failed_tasks: [], stuck_tasks: [] } };
  const container = dom.getElementById('obsContent');
  m.renderAnomalyTab(container, ops);
  const html = container.innerHTML;
  assert.ok(html.includes('observability.no_anomaly'), '\u5e94\u663e\u793a\u65e0\u5f02\u5e38\u5360\u4f4d');
  assert.ok(!html.includes('rows_per_page'), '\u4e0d\u5e94\u542b\u9009\u62e9\u5668');
  assert.ok(!html.includes('prev_page'), '\u4e0d\u5e94\u542b\u5206\u9875\u6309\u94ae');
});

test('\u4e0d\u8db3\u4e00\u9875\uff085 \u6761\uff09: \u9009\u62e9\u5668\u4ecd\u53ef\u89c1\uff0c\u65e0\u7ffb\u9875\u6309\u94ae', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { tasks: { failed_tasks: makeFailedTasks(5), stuck_tasks: [] } };
  const container = dom.getElementById('obsContent');
  m.renderAnomalyTab(container, ops);
  const html = container.innerHTML;
  const rowCount = (html.match(/openTaskLogModal/g) || []).length;
  assert.strictEqual(rowCount, 5, '\u5e94\u6e32\u67d3 5 \u884c');
  assert.ok(html.includes('rows_per_page'), '\u9009\u62e9\u5668\u5e94\u53ef\u89c1');
  assert.ok(!html.includes('prev_page'), '\u4e0d\u5e94\u6709\u7ffb\u9875\u6309\u94ae');
  assert.ok(html.includes('1 / 1'), '\u5e94\u663e\u793a\u9875\u7801 1/1');
});

test('\u9875\u7801\u8d8a\u754c\u81ea\u52a8\u94b3\u5236: _anomalyPage=99 + 25\u6761 \u2192 \u91cd\u7f6e\u4e3a 0', () => {
  const dom = makeDom();
  const ops = { tasks: { failed_tasks: makeFailedTasks(25), stuck_tasks: [] } };
  const m = loadObs(dom, '; _anomalyPage = 99; renderAnomalyTab(document.getElementById("obsContent"), ' + JSON.stringify(ops) + '); return { _anomalyPage: _anomalyPage };');
  assert.strictEqual(m._anomalyPage, 0, '\u9875\u7801\u5e94\u88ab\u91cd\u7f6e\u4e3a 0');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
