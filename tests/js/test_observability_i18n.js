/*
 * 可观测性前端 i18n 审计（真实语义）—— 固化 2026-10-17 审计脚本
 *
 * 与 test_observability_sections.js 的关键区别：那个 harness 把 I18N.t 桩成 '«key»'，
 * 永远返回**字符串**，所以测不出这两类真实事故：
 *   (1) 缺键 → 页面直接显示原始 key（曾出现 observability.tab.events / rule.failed）
 *   (2) t() 命中"命名空间对象" → 真实 i18n 原样返回对象 → 页面显示 [object Object]
 *       （曾出现 I18N.t('observability.ev_state')，ev_state 是 dict）
 *
 * 本用例用**真实语言包 + 真实 t/_lookup/_interpolate 语义**驱动真实 observability.js，
 * 渲染所有视图后扫描每个元素 innerHTML，断言：
 *   - 不出现 `observability.xxx` 形式的原始 key
 *   - 不出现 `[object Object]`
 *   - 目标元素**确实渲染出了内容**（防止入口调错导致"扫空页=零问题"的假绿）
 *
 * 运行：node tests/js/test_observability_i18n.js
 */
const path = require('path');
const fs = require('fs');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '../..');
let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}

// ── 真实 i18n 语义（与 src/api/static/js/i18n.js 的 t/_lookup/_interpolate 一致）──
function loadPack(lang, mod) {
  const p = path.join(ROOT, 'src/api/static/locales', lang, mod + '.json');
  return fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, 'utf8')) : null;
}
const PACKS = {
  zh: { observability: loadPack('zh', 'observability'), app: loadPack('zh', 'app') },
  en: { observability: loadPack('en', 'observability'), app: loadPack('en', 'app') },
};
function lookup(pack, key) {
  if (!pack || !key) return undefined;
  let cur = pack;
  for (const part of key.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[part];
  }
  return cur;
}
function interp(str, vars) {
  if (!str || typeof str !== 'string' || !vars) return str;
  return str.replace(/\{\{?(\w+)\}?\}/g, (m, n) => (Object.prototype.hasOwnProperty.call(vars, n) ? String(vars[n]) : m));
}
const requested = new Map();
function realT(lang) {
  return function t(key, vars) {
    let val = lookup(PACKS[lang], key);
    if (val === undefined) val = lookup(PACKS.zh, key);
    if (val === undefined || val === null) { if (!requested.has(key)) requested.set(key, 'missing'); return key; }
    if (typeof val !== 'string') { if (!requested.has(key)) requested.set(key, 'object'); return val; }
    if (!requested.has(key)) requested.set(key, 'ok');
    return interp(val, vars);
  };
}

// ── DOM / 沙箱脚手架 ──
function makeDom() {
  const elements = {};
  const mk = (id) => ({
    _id: id, style: {}, innerHTML: '', textContent: '', value: '',
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, toggle() {}, contains(c) { return this._s.has(c); } },
    querySelector: () => null, querySelectorAll: () => [], addEventListener: () => {}, closest: () => null,
    appendChild: () => {}, remove: () => {}, setAttribute: () => {}, onclick: null,
  });
  return {
    getElementById: (id) => elements[id] || (elements[id] = mk(id)),
    querySelector: () => null, querySelectorAll: () => [], createElement: () => mk('_new'),
    body: { appendChild: (el) => { if (el && el.id) elements[el.id] = el; } },
    documentElement: { setAttribute() {}, removeAttribute() {} }, addEventListener: () => {}, _elements: elements,
  };
}
function loadObs(dom, payloads) {
  const src = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/observability.js'), 'utf8');
  const state = { api: [] };
  const pick = (u) => {
    for (const [pfx, py] of (payloads || [])) if (String(u).indexOf(pfx) === 0) return Promise.resolve(py);
    return Promise.resolve({});
  };
  const sandbox = {
    document: dom,
    window: { _currentView: 'observability', history: { pushState() {}, replaceState() {}, state: null }, location: { hash: '' }, addEventListener: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} },
    API: { get: (u) => { state.api.push(u); return pick(u); }, post: (u) => { state.api.push(u); return pick(u); } },
    I18N: { t: realT('zh'), getLang: () => 'zh' },
    console: { log: () => {}, error: () => {}, warn: () => {} },
    fmtLocalTime: (s) => s || '',
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math, encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function () {}, AbortController: function () { this.abort = () => {}; this.signal = {}; },
    AbortSignal: { timeout: () => ({}) }, openRuntimeSessionModal: () => {}, openRuntimeTaskModal: () => {},
  };
  const ks = Object.keys(sandbox), vs = ks.map((k) => sandbox[k]);
  const ex = '\n; return { renderObservability, switchObsTab, loadErrorLogs, openObsAlertPanel, switchObsAlertPanel, openObsTimeline };';
  return { obs: new Function(...ks, src + ex)(...vs), state, dom };
}

// ── 负载（覆盖 6 类来源、多种 state/kind，含任务类 failed/completed）──
// S1 契约字段齐全（真实语言包 → state_label_key/outcome 必须能译出来，不能露原始 key）
const EV = (o) => Object.assign({
  source: 'monitor', kind: 'task_stuck', level: 'warning', state: 'firing',
  state_label_key: 'observability.state.firing', ts_kind: 'interval', outcome: '',
  started_at: '2026-10-17T02:00:00+00:00', duration_sec: 120, ongoing: true,
  repo_key: 'gitcode:org/demo', task_id: '34', dim_key: 'repo:gitcode:org/demo',
  next_step: ['logs'], target: { repo_key: 'gitcode:org/demo', issue_id: '', device_id: '' },
}, o);
const EVENTS = [
  EV({}),
  EV({ source: 'gap', kind: 'runtime_silent', level: 'important', dim_key: 'runtime:4', repo_key: '', task_id: '',
       next_step: ['device'], target: { repo_key: '', issue_id: '', device_id: '4' } }),
  EV({ source: 'gap', kind: 'runtime_silent', state: 'resolved', state_label_key: 'observability.state.resolved',
       ongoing: false, dim_key: 'runtime:4', repo_key: '', task_id: '',
       next_step: ['device'], target: { repo_key: '', issue_id: '', device_id: '4' } }),
  EV({ source: 'mutation', kind: 'task_success_rate_drop', level: 'severe' }),
  EV({ source: 'task', kind: 'failed', level: 'severe', state: 'failed',
       state_label_key: 'observability.state.failed', outcome: 'failed', task_id: '9',
       next_step: ['timeline', 'logs'] }),
  EV({ source: 'task', kind: 'completed', level: 'info', state: 'done',
       state_label_key: 'observability.state.done', outcome: '', ongoing: false, task_id: '10' }),
  EV({ source: 'change', kind: 'pr_merged', state: 'done', state_label_key: 'observability.state.done',
       outcome: 'merged', ts_kind: 'point', title: 'feat: x', issue_id: 3, task_id: '',
       ongoing: false, next_step: ['issue'] }),
  EV({ source: 'audit', kind: 'login', state: 'done', state_label_key: 'observability.state.done',
       ts_kind: 'point', title: '用户登录', repo_key: '', task_id: '', ongoing: false, next_step: [] }),
];
const PAYLOADS = [
  ['/obs/overview', { range: '24h', bucket_seconds: 1800, scope: { repos: 1 }, kpis: { queue_depth: 0, success_rate: 100, p95_duration_s: 1, engines_online: 1, engines_total: 1, tokens: 10 },
    charts: { task_outcome: { buckets: ['2026-10-17T00:00:00+00:00'], completed: [1], failed: [0], cancelled: [0], rate: [100] }, token_trend: { buckets: [], input: [], output: [], cache: [] }, model_share: [] } }],
  ['/obs/alerts', { summary: { firing: 1, critical: 1, latest: { id: 7, rule_name: 'task_stuck', repo_key: 'gitcode:org/demo', value: 3, threshold: 0, status: 'firing', last_seen_at: '2026-10-17T05:00:00+00:00', silenced_until: '' }, by_severity: { critical: 1 } },
    events: [{ id: 7, rule_name: 'task_stuck', level: 'user', severity: 'critical', status: 'firing', metric: 'task_stuck', value: 3, threshold: 0, repo_key: 'gitcode:org/demo', last_seen_at: '2026-10-17T05:00:00+00:00', silenced_until: '' }],
    rules: [{ id: 1, name: 'task_stuck', level: 'user', metric: 'task_stuck', threshold: 0, for_sec: 300, severity: 'warning', enabled: 1, notify_channels: 'feishu' }] }],
  ['/obs/runtimes/history', { range: '6h', bucket_seconds: 300, buckets: ['2026-10-17T00:00:00+00:00'], runtimes: [{ id: 1, device_name: 'w1' }], series: { '1': { cpu: [10], mem: [20], disk: [30], health: [90] } } }],
  ['/obs/events', { events: EVENTS, total: EVENTS.length }],
  ['/obs/logs', { total: 1, limit: 200, offset: 0, scoped_repos: 1, facets: { levels: { ERROR: 1 }, sources: ['w1'] },
    items: [{ id: 1, ts: '2026-10-17T06:10:00+00:00', level: 'ERROR', component: 'worker', source: 'w1', repo_key: 'gitcode:org/demo', task_id: '9', msg: 'boom' }] }],
  ['/obs/trace/', { trace_id: 'tr_x', task_id: '34', repo_key: 'gitcode:org/demo', issue_id: 3, steps: [{ ts: '2026-10-17T02:00:00+00:00', stage: 'queued', source: 'monitor', status: 'ok', detail: 'q', duration_sec: 1 }] }],
];

const tick = (ms) => new Promise((r) => setTimeout(r, ms === undefined ? 30 : ms));

(async () => {
  const problems = [];
  function scanAll(dom, label) {
    for (const id of Object.keys(dom._elements)) {
      const h = (dom._elements[id] && dom._elements[id].innerHTML) || '';
      if (typeof h !== 'string') { problems.push(`[${label}] #${id} innerHTML 非字符串`); continue; }
      let i = -1;
      while ((i = h.indexOf('[object Object]', i + 1)) !== -1) {
        problems.push(`[${label}] #${id} 显示 [object Object] @${i}: ${h.slice(Math.max(0, i - 90), i + 16).replace(/\s+/g, ' ')}`);
      }
      [...new Set(h.match(/observability\.[a-zA-Z0-9_.]+/g) || [])].forEach((k) => problems.push(`[${label}] #${id} 原始 key: ${k}`));
    }
  }

  // 总览走真实异步链（API.get → then → 渲染 #obsOvBody），必须等一拍再扫
  await (async () => {
    const dom = makeDom();
    loadObs(dom, PAYLOADS).obs.renderObservability('overview');
    await tick(80);
    scanAll(dom, 'overview');
    const body = (dom._elements.obsOvBody && dom._elements.obsOvBody.innerHTML) || '';
    if (body.length < 200) problems.push('[overview] 总览主体没渲染出内容（入口可能调错了）');
  })();

  // 需要走真实异步链（API.get → then → 渲染）
  await (async () => {
    for (const tab of ['tasks', 'events', 'anomaly', 'errorlogs']) {
      const dom = makeDom();
      const { obs } = loadObs(dom, PAYLOADS);
      obs.renderObservability('inspect');
      await tick(40);
      try { obs.switchObsTab(tab); } catch (e) { problems.push(`[inspect/${tab}] 切换抛错: ${e.message}`); }
      await tick(60);
      scanAll(dom, 'inspect/' + tab);
      if (tab === 'events') {
        const body = (dom._elements.obsEventBody && dom._elements.obsEventBody.innerHTML) || '';
        // S2 起是时间轴流（不再是 8 列表格）→ 用"行可展开"作渲染判据
        if (body.indexOf('obsEventToggle(') === -1) problems.push('[inspect/events] 事件时间轴没渲染出来（入口调错？）');
        else {
          // 任务来源必须显示"结果"文案，而不是 rule.failed 这种原始 key
          if (body.indexOf('任务失败') === -1) problems.push('[inspect/events] 任务来源标题未译（期望"任务失败"）');
        }
      }
      if (tab === 'events' && (dom._elements.obsContent.innerHTML || '').indexOf('事件') === -1) {
        problems.push('[inspect/events] 事件页标题缺失');
      }
    }
  })();

  await (async () => {
    for (const pv of ['events', 'rules', 'silences']) {
      const dom = makeDom();
      const { obs } = loadObs(dom, PAYLOADS);
      obs.openObsAlertPanel(pv);
      await tick(60);
      obs.switchObsAlertPanel(pv);
      await tick(30);
      scanAll(dom, 'alertPanel/' + pv);
    }
  })();

  await (async () => {
    const dom = makeDom();
    const { obs } = loadObs(dom, PAYLOADS);
    obs.openObsTimeline('34');
    await tick(70);
    scanAll(dom, 'timeline');
  })();

  check('所有视图：无原始 key、无 [object Object]', () => {
    assert.strictEqual(problems.length, 0, '\n      ' + problems.join('\n      '));
  });

  check('语言包完整：渲染过程中请求的键全部命中（zh/en）', () => {
    const miss = [...requested.entries()].filter(([, v]) => v !== 'ok').map(([k, v]) => `${k} (${v})`);
    assert.strictEqual(miss.length, 0, '缺键/非标量键: ' + miss.join(', '));
    assert.ok(requested.size >= 40, '请求到的键太少（' + requested.size + '），用例可能没真正渲染');
  });

  check('en 语言包与 zh 覆盖一致（渲染用到的键两边都在）', () => {
    const miss = [];
    for (const k of requested.keys()) {
      const zh = lookup(PACKS.zh, k), en = lookup(PACKS.en, k);
      if (typeof zh !== 'string' || typeof en !== 'string') miss.push(k);
    }
    assert.strictEqual(miss.length, 0, 'en/zh 不一致: ' + miss.join(', '));
  });

  console.log('');
  console.log(`${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();