/*
 * Issue #1229 能效看板（任务与日志模块）前端源断言测试
 *
 * 运行：node tests/js/test_issue_1229_efficiency_board.js
 *
 * 验证点：
 *   T10a  inspect 分区子 Tab 注册 efficiency（能效看板入口存在）
 *   T10b  zh/en 语言包键集合一致 + 必备键存在（tab.efficiency / eff.*）
 *   T11a  渲染：KPI 汇总行 + 协作者表格行 + 占比条
 *   T11b  空态：contributors 为空时渲染空态文案，不渲染表格行
 *   T11c  安全：用户名 HTML 转义（<script> 不原样出现）
 *   T11d  取数：调用 /dashboard/efficiency 且带 repo 参数
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

const SRC = path.resolve(__dirname, '../../src/api/static/js/pages/observability.js');
const ZH = JSON.parse(fs.readFileSync(
  path.resolve(__dirname, '../../src/api/static/locales/zh/observability.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(
  path.resolve(__dirname, '../../src/api/static/locales/en/observability.json'), 'utf8'));

function makeDom() {
  const elements = {};
  for (const id of ['pageContent', 'obsContent', 'obsLastUpdate', 'obsEffBody']) {
    elements[id] = {
      _id: id, classList: {_s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, toggle() {}, contains(c) { return this._s.has(c); }},
      style: {}, innerHTML: '', textContent: '', value: '',
      querySelector: () => null, querySelectorAll: () => [], appendChild: () => {},
      remove() {}, setAttribute() {}, onclick: null,
    };
  }
  return {
    getElementById(id) {
      if (elements[id]) return elements[id];
      elements[id] = { _id: id, classList: {_s: new Set(), add() {}, remove() {}, toggle() {}, contains() { return false; }}, style: {}, innerHTML: '', textContent: '', value: '', querySelector: () => null, querySelectorAll: () => [], appendChild: () => {}, remove() {}, setAttribute() {}, onclick: null };
      return elements[id];
    },
    querySelector: () => null,
    querySelectorAll: () => { const a = []; a.forEach = Array.prototype.forEach; return a; },
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, textContent: '', innerHTML: '', appendChild: () => {}, remove() {}, querySelector: () => null, setAttribute() {} }),
    body: { appendChild: () => {} },
    documentElement: { setAttribute() {}, removeAttribute() {} },
    addEventListener: () => {},
    _elements: elements,
  };
}

function loadObs(dom, extraInit) {
  const src = fs.readFileSync(SRC, 'utf8');
  const I18N = { t: function (k, opts) {
    const kk = k.replace(/^observability\./, '');
    let v = kk.split('.').reduce((o, x) => (o || {})[x], ZH);
    if (v === undefined || typeof v === 'object') v = k;
    if (opts) { for (const [key, val] of Object.entries(opts)) v = String(v).split('{{' + key + '}}').join(val); }
    return v;
  }, getLang: () => 'zh' };
  const sandbox = {
    document: dom, window: { _currentView: '', history: { pushState() {}, replaceState() {}, state: null }, location: { hash: '' }, addEventListener: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} },
    sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    API: { get: () => Promise.resolve({}), post: () => Promise.resolve({}) }, I18N,
    console: { log: () => {}, error: () => {}, warn: () => {} }, fmtLocalTime: (s) => s || '',
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function () {}, AbortController: function () { this.abort = () => {}; this.signal = {}; },
    AbortSignal: { timeout: () => ({}) }, openRuntimeSessionModal: () => {},
  };
  const sKeys = Object.keys(sandbox);
  const sVals = sKeys.map(k => sandbox[k]);
  const exportsStr = '\n; return {' +
    '_OBS_SECTIONS: typeof _OBS_SECTIONS !== "undefined" ? _OBS_SECTIONS : null,' +
    'renderObsEfficiency: typeof renderObsEfficiency !== "undefined" ? renderObsEfficiency : null,' +
    'loadObsEfficiency: typeof loadObsEfficiency !== "undefined" ? loadObsEfficiency : null' +
  '};';
  const fn = new Function(...sKeys, src + (extraInit || '') + exportsStr);
  return fn(...sVals);
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch (e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

// ---------- T10a: 子 Tab 注册 ----------
test('T10a inspect 分区子 Tab 注册 efficiency', () => {
  const dom = makeDom();
  const obs = loadObs(dom);
  const subs = obs._OBS_SECTIONS.inspect.subs;
  assert.ok(subs.indexOf('efficiency') >= 0, 'inspect.subs 缺 efficiency，实际: ' + JSON.stringify(subs));
});

// ---------- T10b: i18n 键 ----------
test('T10b zh/en 键集合一致 + 必备键存在', () => {
  const need = ['tab.efficiency', 'eff.total_issues', 'eff.completed', 'eff.total_tokens',
    'eff.th_user', 'eff.th_issues', 'eff.th_completed', 'eff.th_tokens',
    'eff.empty', 'eff.load_failed'];
  const get = (pack, key) => key.split('.').reduce((o, k) => (o || {})[k], pack);
  for (const k of need) {
    assert.ok(get(ZH, k), 'zh 缺 ' + k);
    assert.ok(get(EN, k), 'en 缺 ' + k);
  }
});

// ---------- T11a: 渲染 ----------
test('T11a 渲染 KPI + 协作者行 + 占比条', () => {
  const dom = makeDom();
  loadObs(dom, `
; renderObsEfficiency(document.getElementById('obsContent'), {
  repo: 'gitcode:org/repo', total_issues: 5, completed_issues: 2, total_tokens: 300,
  contributors: [
    {user: 'alice', issues: 3, completed: 1, input_tokens: 100, output_tokens: 50, total_tokens: 150},
    {user: 'bob', issues: 2, completed: 1, input_tokens: 100, output_tokens: 50, total_tokens: 150},
  ],
});`);
  const html = dom._elements['obsContent'].innerHTML;
  assert.ok(html.indexOf('alice') >= 0 && html.indexOf('bob') >= 0, '协作者行缺失');
  assert.ok(html.indexOf('150') >= 0, 'token 数值缺失');
  assert.ok(html.indexOf('width:') >= 0, '占比条样式缺失');
});

// ---------- T11b: 空态 ----------
test('T11b 空数据渲染空态文案', () => {
  const dom = makeDom();
  loadObs(dom, `
; renderObsEfficiency(document.getElementById('obsContent'), {
  repo: 'gitcode:org/repo', total_issues: 0, completed_issues: 0, total_tokens: 0, contributors: [],
});`);
  const html = dom._elements['obsContent'].innerHTML;
  assert.ok(html.indexOf(ZH['eff']['empty']) >= 0, '空态文案缺失: ' + html.substring(html.length - 400));
  assert.ok(html.indexOf('<table') < 0, '空态不应渲染表格');
});

// ---------- T11c: 转义 ----------
test('T11c 用户名 HTML 转义', () => {
  const dom = makeDom();
  loadObs(dom, `
; renderObsEfficiency(document.getElementById('obsContent'), {
  repo: 'gitcode:org/repo', total_issues: 1, completed_issues: 0, total_tokens: 1,
  contributors: [{user: '<script>alert(1)</script>', issues: 1, completed: 0, input_tokens: 0, output_tokens: 1, total_tokens: 1}],
});`);
  const html = dom._elements['obsContent'].innerHTML;
  assert.ok(html.indexOf('<script>alert(1)</script>') < 0, 'script 未转义');
  assert.ok(html.indexOf('&lt;script&gt;') >= 0, '应转义为 &lt;script&gt;');
});

// ---------- T11d: 取数 ----------
test('T11d loadObsEfficiency 带 repo 参数请求端点', () => {
  const dom = makeDom();
  let called = null;
  const obs = loadObs(dom);
  // 重新以受控 API 加载，捕获请求路径
  const src = fs.readFileSync(SRC, 'utf8');
  const I18N = { t: (k) => k, getLang: () => 'zh' };
  const sandbox = {
    document: dom, window: { _currentView: '', history: { pushState() {}, replaceState() {}, state: null }, location: { hash: '' }, addEventListener: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    sessionStorage: { getItem: () => JSON.stringify({ fullName: 'org/repo' }), setItem: () => {}, removeItem: () => {} },
    app: { currentRepo: () => ({ fullName: 'org/repo' }) },
    API: { get: (p) => { called = p; return Promise.resolve({ contributors: [] }); }, post: () => Promise.resolve({}) }, I18N,
    console: { log: () => {}, error: () => {}, warn: () => {} }, fmtLocalTime: (s) => s || '',
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function () {}, AbortController: function () { this.abort = () => {}; this.signal = {}; },
    AbortSignal: { timeout: () => ({}) }, openRuntimeSessionModal: () => {},
  };
  const sKeys = Object.keys(sandbox);
  const fn = new Function(...sKeys, src + '\n; loadObsEfficiency();');
  fn(...sKeys.map(k => sandbox[k]));
  assert.ok(called && called.indexOf('/dashboard/efficiency') >= 0, '未请求 /dashboard/efficiency: ' + called);
  assert.ok(called.indexOf('repo=org%2Frepo') >= 0, 'repo 参数缺失: ' + called);
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
