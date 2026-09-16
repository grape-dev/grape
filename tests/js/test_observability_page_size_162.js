/*
 * Issue #162 回归测试：概览两处分页「每页条数」选择器 + 任务页新增分页
 *
 * 运行：node tests/js/test_observability_page_size_162.js
 *
 * 验证点（概览）：
 *   1. Worker 分页默认每页 10 条：25 条仅渲染 10 张卡片，页码 1/3
 *   2. Worker 分页栏含 10/20/50 选择器，默认选中 10
 *   3. Worker 切换 20 条/页：25 条仅渲染 20 张，页码 1/2
 *   4. Runtime 分页默认每页 10 条：25 条仅渲染 10 行，页码 1/3
 *   5. Runtime 分页栏含 10/20/50 选择器，默认选中 10
 *   6. Runtime 切换 50 条/页：25 条全部渲染，页码 1/1
 *   7. Worker 空列表：无选择器无分页
 * 验证点（任务 Tab）：
 *   8. 25 个 Issue 默认仅渲染 10 个，页码 1/3
 *   9. 分页栏含 10/20/50 选择器，默认选中 10
 *   10. 切换 20 条/页：25 个仅渲染 20 个，页码 1/2
 *   11. 切换 50 条/页：25 个全部渲染，页码 1/1
 *   12. 切换每页条数后页码重置为 0
 *   13. 搜索筛选后按分页渲染（先筛选再分页）
 *   14. 页码越界自动钳制
 *   15. 空列表：显示无任务占位，无分页栏
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

function makeDom() {
  const elements = {};
  const ids = ['pageContent','obsContent','obsLastUpdate','obsAuto',
    'wk-grid','wk-pager','obs-runtime-rows','obs-runtime-count','rt-pager',
    'kpi-w-total','kpi-w-online','kpi-w-offline','kpi-r-total',
    'obs-a2a-health','obs-runtime-table','obs-timeline-issues','obs-timeline-container'
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
    '_filterTimelineIssues: typeof _filterTimelineIssues !== "undefined" ? _filterTimelineIssues : null,' +
    'renderTimelineList: typeof renderTimelineList !== "undefined" ? renderTimelineList : null,' +
    '_workerPageSize: typeof _workerPageSize !== "undefined" ? _workerPageSize : null,' +
    '_runtimePageSize: typeof _runtimePageSize !== "undefined" ? _runtimePageSize : null,' +
    '_timelinePage: typeof _timelinePage !== "undefined" ? _timelinePage : null,' +
    '_timelinePageSize: typeof _timelinePageSize !== "undefined" ? _timelinePageSize : null,' +
    '_timelineIssuesSorted: typeof _timelineIssuesSorted !== "undefined" ? _timelineIssuesSorted : null'
  + '};';
  const fn = new Function(...sKeys, code + exportsStr);
  return fn(...sVals);
}

function makeWorkers(n) {
  const list = [];
  for (let i = 0; i < n; i++) list.push({ id:i, device_name:'dev-'+String(i).padStart(3,'0'), name:'w-'+i, health:'online', last_ip:'10.0.0.'+(i+1), last_seen_at:'2026-09-12T10:00:00Z', cpu_percent:i*5, memory_percent:i*3, tmp_percent:10, disk_percent:20 });
  return { total:n, online:Math.ceil(n/3), offline:n-Math.ceil(n/3), list };
}

function makeRuntimes(n) {
  const list = [];
  for (let i = 0; i < n; i++) list.push({ id:i, name:'engine-'+i, device_name:'dev-'+String(i%3).padStart(3,'0'), version:'1.0.'+i, health:'online', running:i%4, max_concurrent:8, avg_duration:i*10, probe_status:'ok', probe_latency:0.5, probe_error:'', mode:i%2===0?'local':'remote' });
  return { total:n, online:Math.ceil(n/2), offline:n-Math.ceil(n/2), list };
}

function makeIssues(n) {
  const list = [];
  for (let i = 0; i < n; i++) {
    const num = 1000 + i;
    list.push({
      issueId: '#' + num,
      title: 'Issue ' + num,
      eventCount: 2,
      lastAt: '2026-09-12T10:00:00Z',
      repoKey: 'gitcode:org/repo',
      events: [
        { seq: 1, eventType: 'issue_opened', agentType: 'issue', skill: '', status: 'completed', duration: 2, createdAt: '2026-09-12T10:00:00Z', taskId: '' },
        { seq: 2, eventType: 'a2a_event', agentType: 'coding', skill: 'impl', status: 'completed', duration: 30, createdAt: '2026-09-12T10:01:00Z', taskId: 'task-' + i },
      ],
    });
  }
  return list;
}

function renderTimeline(m, issues, search) {
  const domExtra = `
    _timelineIssuesSorted = ${JSON.stringify(issues)};
    _timelineSearch = ${JSON.stringify(search || '')};
    _timelineGlobalRepoPath = 'org/repo';
    _expandedIssues.clear();
    _issueViewMode = {};
    _filterTimelineIssues();
  `;
  return loadObs(null, domExtra);
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch(e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

console.log('Issue #162: \u6982\u89c8\u201c\u6bcf\u9875\u6761\u6570\u201d\u9009\u62e9\u5668 + \u4efb\u52a1\u9875\u5206\u9875 \u2014 \u56de\u5f52\u6d4b\u8bd5\n');

// ===== 概览 Worker =====
test('Worker \u9ed8\u8ba4\u6bcf\u9875 10 \u6761: 25 \u6761\u4ec5\u6e32\u67d3 10 \uff0c\u9875\u7801 1/3', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: makeWorkers(25), runtimes: { total:0, online:0, offline:0, list:[] } };
  const html = m.renderOverview(ops, {});
  const cardCount = (html.match(/box-shadow:var\(--sh\)/g) || []).length;
  assert.strictEqual(cardCount, 10, '\u5e94\u6e32\u67d3 10 \u4e2a\u5361\u7247\uff0c\u5b9e\u9645 ' + cardCount);
  assert.ok(html.includes('1 / 3'), '\u5e94\u663e\u793a\u9875\u7801 1/3');
});

test('Worker \u5206\u9875\u680f\u542b 10/20/50 \u9009\u62e9\u5668\uff0c\u9ed8\u8ba4\u9009\u4e2d 10', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: makeWorkers(25), runtimes: { total:0, online:0, offline:0, list:[] } };
  const html = m.renderOverview(ops, {});
  assert.ok(html.includes('<option value="10" selected'), '10 \u5e94\u88ab\u9009\u4e2d');
  assert.ok(html.includes('<option value="20"'), '\u5e94\u542b 20 \u9009\u9879');
  assert.ok(html.includes('<option value="50"'), '\u5e94\u542b 50 \u9009\u9879');
  assert.ok(html.includes('rows_per_page'), '\u5e94\u542b\u9009\u62e9\u5668\u6807\u7b7e');
});

test('Worker \u5207\u6362 20 \u6761/\u9875: 25 \u6761\u4ec5\u6e32\u67d3 20\uff0c\u9875\u7801 1/2', () => {
  const dom = makeDom();
  const ops = { workers: makeWorkers(25), runtimes: { total:0, online:0, offline:0, list:[] } };
  const m = loadObs(dom, '; _workerPageSize = 20; var __o = renderOverview(' + JSON.stringify(ops) + ', {}); return { html: __o };');
  const cardCount = (m.html.match(/box-shadow:var\(--sh\)/g) || []).length;
  assert.strictEqual(cardCount, 20, '\u5e94\u6e32\u67d3 20 \u4e2a\uff0c\u5b9e\u9645 ' + cardCount);
  assert.ok(m.html.includes('1 / 2'), '\u5e94\u663e\u793a\u9875\u7801 1/2');
  assert.ok(m.html.includes('<option value="20" selected'), '20 \u5e94\u88ab\u9009\u4e2d');
});

test('Worker \u7a7a\u5217\u8868: \u65e0\u9009\u62e9\u5668\u65e0\u5206\u9875', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: { total:0, online:0, offline:0, list:[] } };
  const html = m.renderOverview(ops, {});
  assert.ok(html.includes('observability.no_remote_worker'), '\u5e94\u663e\u793a\u5360\u4f4d');
  assert.ok(!html.includes('rows_per_page'), '\u4e0d\u5e94\u542b\u9009\u62e9\u5668');
  assert.ok(!html.includes('prev_page'), '\u4e0d\u5e94\u6709\u5206\u9875\u6309\u94ae');
});

// ===== 概览 Runtime =====
test('Runtime \u9ed8\u8ba4\u6bcf\u9875 10 \u6761: 25 \u6761\u4ec5\u6e32\u67d3 10\uff0c\u9875\u7801 1/3', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: makeRuntimes(25) };
  const html = m.renderOverview(ops, {});
  const rowCount = (html.match(/<tr id="rt-\d+"/g) || []).length;
  assert.strictEqual(rowCount, 10, '\u5e94\u6e32\u67d3 10 \u884c\uff0c\u5b9e\u9645 ' + rowCount);
  assert.ok(html.includes('1 / 3'), '\u5e94\u663e\u793a\u9875\u7801 1/3');
});

test('Runtime \u5206\u9875\u680f\u542b 10/20/50 \u9009\u62e9\u5668\uff0c\u9ed8\u8ba4\u9009\u4e2d 10', () => {
  const dom = makeDom();
  const m = loadObs(dom);
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: makeRuntimes(25) };
  const html = m.renderOverview(ops, {});
  assert.ok(html.includes('<option value="10" selected'), '10 \u5e94\u88ab\u9009\u4e2d');
  assert.ok(html.includes('<option value="20"'), '\u5e94\u542b 20 \u9009\u9879');
  assert.ok(html.includes('<option value="50"'), '\u5e94\u542b 50 \u9009\u9879');
});

test('Runtime \u5207\u6362 50 \u6761/\u9875: 25 \u6761\u5168\u90e8\u6e32\u67d3\uff0c\u9875\u7801 1/1', () => {
  const dom = makeDom();
  const ops = { workers: { total:0, online:0, offline:0, list:[] }, runtimes: makeRuntimes(25) };
  const m = loadObs(dom, '; _runtimePageSize = 50; var __o = renderOverview(' + JSON.stringify(ops) + ', {}); return { html: __o };');
  const rowCount = (m.html.match(/<tr id="rt-\d+"/g) || []).length;
  assert.strictEqual(rowCount, 25, '\u5e94\u6e32\u67d3 25 \u884c\uff0c\u5b9e\u9645 ' + rowCount);
  assert.ok(m.html.includes('1 / 1'), '\u5e94\u663e\u793a\u9875\u7801 1/1');
  assert.ok(m.html.includes('<option value="50" selected'), '50 \u5e94\u88ab\u9009\u4e2d');
});

// ===== 任务 Tab 分页 =====
test('\u4efb\u52a1\u9875 25 \u4e2a Issue: \u9ed8\u8ba4\u4ec5\u6e32\u67d3 10\uff0c\u9875\u7801 1/3', () => {
  const dom = makeDom();
  const m = loadObs(dom, '; _timelineIssuesSorted = ' + JSON.stringify(makeIssues(25)) + '; _timelinePage = 0; _filterTimelineIssues(); return { html: document.getElementById("obs-timeline-issues").innerHTML };');
  const cardCount = (m.html.match(/<div id="tl-body-/g) || []).length;
  assert.strictEqual(cardCount, 10, '\u5e94\u6e32\u67d3 10 \u4e2a Issue\uff0c\u5b9e\u9645 ' + cardCount);
  assert.ok(m.html.includes('1 / 3'), '\u5e94\u663e\u793a\u9875\u7801 1/3');
});

test('\u4efb\u52a1\u9875\u5206\u9875\u680f\u542b 10/20/50 \u9009\u62e9\u5668\uff0c\u9ed8\u8ba4\u9009\u4e2d 10', () => {
  const dom = makeDom();
  const m = loadObs(dom, '; _timelineIssuesSorted = ' + JSON.stringify(makeIssues(25)) + '; _timelinePage = 0; _filterTimelineIssues(); return { html: document.getElementById("obs-timeline-issues").innerHTML };');
  assert.ok(m.html.includes('<option value="10" selected'), '10 \u5e94\u88ab\u9009\u4e2d');
  assert.ok(m.html.includes('<option value="20"'), '\u5e94\u542b 20 \u9009\u9879');
  assert.ok(m.html.includes('<option value="50"'), '\u5e94\u542b 50 \u9009\u9879');
  assert.ok(m.html.includes('rows_per_page'), '\u5e94\u542b\u9009\u62e9\u5668\u6807\u7b7e');
});

test('\u4efb\u52a1\u9875\u5207\u6362 20 \u6761/\u9875: 25 \u4e2a\u4ec5\u6e32\u67d3 20\uff0c\u9875\u7801 1/2', () => {
  const dom = makeDom();
  const m = loadObs(dom, '; _timelineIssuesSorted = ' + JSON.stringify(makeIssues(25)) + '; _timelinePageSize = 20; _timelinePage = 0; _filterTimelineIssues(); return { html: document.getElementById("obs-timeline-issues").innerHTML };');
  const cardCount = (m.html.match(/<div id="tl-body-/g) || []).length;
  assert.strictEqual(cardCount, 20, '\u5e94\u6e32\u67d3 20 \u4e2a\uff0c\u5b9e\u9645 ' + cardCount);
  assert.ok(m.html.includes('1 / 2'), '\u5e94\u663e\u793a\u9875\u7801 1/2');
  assert.ok(m.html.includes('<option value="20" selected'), '20 \u5e94\u88ab\u9009\u4e2d');
});

test('\u4efb\u52a1\u9875\u5207\u6362 50 \u6761/\u9875: 25 \u4e2a\u5168\u90e8\u6e32\u67d3\uff0c\u9875\u7801 1/1', () => {
  const dom = makeDom();
  const m = loadObs(dom, '; _timelineIssuesSorted = ' + JSON.stringify(makeIssues(25)) + '; _timelinePageSize = 50; _timelinePage = 0; _filterTimelineIssues(); return { html: document.getElementById("obs-timeline-issues").innerHTML };');
  const cardCount = (m.html.match(/<div id="tl-body-/g) || []).length;
  assert.strictEqual(cardCount, 25, '\u5e94\u6e32\u67d3 25 \u4e2a\uff0c\u5b9e\u9645 ' + cardCount);
  assert.ok(m.html.includes('1 / 1'), '\u5e94\u663e\u793a\u9875\u7801 1/1');
  assert.ok(m.html.includes('<option value="50" selected'), '50 \u5e94\u88ab\u9009\u4e2d');
});

test('\u4efb\u52a1\u9875 pagination onchange \u5207\u6362\u6bcf\u9875\u6761\u6570\u65f6\u91cd\u7f6e\u9875\u7801\u4e3a 0', () => {
  const dom = makeDom();
  const m = loadObs(dom, '; _timelineIssuesSorted = ' + JSON.stringify(makeIssues(25)) + '; _timelinePage = 0; _filterTimelineIssues(); return { html: document.getElementById("obs-timeline-issues").innerHTML };');
  assert.ok(m.html.includes('_timelinePageSize=parseInt(this.value)||10;_timelinePage=0;_filterTimelineIssues()'), 'onchange \u5e94\u5448\u73b0\u4e3a\u91cd\u7f6e\u9875\u7801');
});

test('\u641c\u7d22\u7b5b\u9009\u540e\u6309\u5206\u9875\u6e32\u67d3\uff08\u5148\u7b5b\u9009\u518d\u5206\u9875\uff09', () => {
  const dom = makeDom();
  const issues = makeIssues(25);
  const m = loadObs(dom, '; _timelineIssuesSorted = ' + JSON.stringify(issues) + '; _timelineSearch = "101"; _timelinePage = 0; _filterTimelineIssues(); return { html: document.getElementById("obs-timeline-issues").innerHTML };');
  const found = issues.filter(i => (i.issueId || '').toLowerCase().includes('101')).length;
  const cardCount = (m.html.match(/<div id="tl-body-/g) || []).length;
  assert.strictEqual(cardCount, found, '\u5e94\u4ec5\u6e32\u67d3\u7b5b\u9009\u540e ' + found + ' \u4e2a\uff0c\u5b9e\u9645 ' + cardCount);
});

test('\u9875\u7801\u8d8a\u754c\u81ea\u52a8\u94b3\u5236: _timelinePage=99 + 25\u4e2a \u2192 \u91cd\u7f6e\u4e3a 0', () => {
  const dom = makeDom();
  const m = loadObs(dom, '; _timelineIssuesSorted = ' + JSON.stringify(makeIssues(25)) + '; _timelinePage = 99; _filterTimelineIssues(); return { page: _timelinePage };');
  assert.strictEqual(m.page, 0, '\u9875\u7801\u5e94\u88ab\u91cd\u7f6e\u4e3a 0');
});

test('\u4efb\u52a1\u9875\u641c\u7d22\u540e\u65e0\u7ed3\u679c: \u663e\u793a\u5360\u4f4d\uff0c\u65e0\u5206\u9875\u680f', () => {
  const dom = makeDom();
  const m = loadObs(dom, '; _timelineIssuesSorted = ' + JSON.stringify(makeIssues(25)) + '; _timelineSearch = "zzz"; _timelinePage = 0; _filterTimelineIssues(); return { html: document.getElementById("obs-timeline-issues").innerHTML };');
  assert.ok(m.html.includes('observability.no_matching_tasks'), '\u5e94\u663e\u793a\u65e0\u5339\u914d\u5360\u4f4d');
  assert.ok(!m.html.includes('rows_per_page'), '\u4e0d\u5e94\u542b\u9009\u62e9\u5668');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);