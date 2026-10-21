/*
 * 事件视图 S2：时间轴流 + 服务端关键字检索 + 游标"加载更多" + 30s 新事件提示
 *
 * 运行：node tests/js/test_observability_events_flow.js
 * 设计：docs/weekly-07/observability-events-view-redesign.md（2026-10-18 定稿：4 项决策）
 *
 * 覆盖（对应方案 §七 S2 的硬性测试要求）：
 *   1. 关键字过滤走**服务端**（URL 带 q=，不是前端过滤已加载页）
 *   2. 游标"加载更多"：不重复、不遗漏；末页按钮消失
 *   3. 自动刷新（30s）只提示"有 N 条新事件"，点击才前插；10s 轮询不重复打接口
 *   4. truncated_sources → 顶部截断提示
 *   5. 行内展开显示详情；瞬时事件（ts_kind=point）不显示时长
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

let passed = 0, failed = 0;
const pending = [];
function check(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}
function checkAsync(name, fn) {
  pending.push((async () => {
    try { await fn(); passed++; console.log('  ✓ ' + name); }
    catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
  })());
}
const tick = (ms) => new Promise(r => setTimeout(r, ms === undefined ? 25 : ms));

const SRC = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/static/js/pages/observability.js'), 'utf8');

// payloads: [[url 前缀, payload|fn(url)], ...]  fn(url) 便于按 before= 返回不同页
function makeEnv(payloads) {
  const els = {};
  const mkEl = (id) => ({
    id, innerHTML: '', textContent: '', value: '', className: '',
    style: { display: 'none' },
    appendChild() {}, insertBefore() {}, setAttribute() {}, removeAttribute() {},
    querySelector() { return null; },
  });
  const getEl = (id) => (els[id] = els[id] || mkEl(id));
  const calls = [];
  function pick(url) {
    for (const [prefix, payload] of (payloads || [])) {
      if (String(url).indexOf(prefix) === 0) {
        return typeof payload === 'function' ? payload(url) : payload;
      }
    }
    return {};
  }
  const I18N = {
    t: (k, o) => {
      let s = '«' + k + '»';
      if (o) for (const kk in o) s = s.split('{' + kk + '}').join(String(o[kk]));
      return s;
    },
    getLang: () => 'zh',
  };
  const sandbox = {
    document: {
      getElementById: getEl, createElement: () => mkEl('__new'),
      querySelector: () => null, addEventListener() {}, body: { appendChild() {} },
    },
    window: {
      _currentView: 'observability', history: { pushState() {}, replaceState() {} },
      location: { hash: '' }, addEventListener() {},
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    API: {
      get: (u) => { calls.push(u); return Promise.resolve(pick(u)); },
      post: (u) => { calls.push(u); return Promise.resolve(pick(u)); },
    },
    I18N,
    console: { log() {}, error() {}, warn() {} },
    fmtLocalTime: (s) => s || '',
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
    setTimeout, clearTimeout,
    setInterval: () => 0, clearInterval() {},
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function () {},
    AbortController: function () { this.abort = () => {}; this.signal = {}; },
    AbortSignal: { timeout: () => ({}) },
    openRuntimeSessionModal() {}, openRuntimeTaskModal() {},
  };
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  const exp = '\n; return {' +
    ' loadObsEvents: loadObsEvents, obsEventSearch: obsEventSearch,' +
    ' obsEventLoadMore: obsEventLoadMore, obsEventApplyNew: obsEventApplyNew,' +
    ' obsEventToggle: obsEventToggle, _obsEventRowHtml: _obsEventRowHtml,' +
    ' _obsEventSoftRefresh: _obsEventSoftRefresh, _renderObsEventFlow: _renderObsEventFlow,' +
    ' getCursor: () => _obsEventCursor, getMore: () => _obsEventMore,' +
    ' getNew: () => _obsEventNew, getData: () => _obsEventsData };';
  const obs = (new Function(...keys, SRC + exp))(...vals);
  return { obs, els, calls };
}

const ev = (o) => Object.assign({
  key: 'x', source: 'monitor', kind: 'task_stuck', level: 'warning', state: 'firing',
  state_label_key: 'observability.state.firing', ts_kind: 'interval', outcome: '',
  started_at: '2026-10-17T02:00:00+00:00', duration_sec: 120, ongoing: true,
  repo_key: 'gitcode:org/x', task_id: 't1', issue_id: '',
  next_step: ['logs'], target: { repo_key: 'gitcode:org/x', issue_id: '', device_id: '' },
}, o);

(async () => {
  console.log('== 关键字：服务端检索（不是前端过滤）==');
  await checkAsync('obsEventSearch → 请求 URL 带 q=', async () => {
    const { obs, els, calls } = makeEnv([['/obs/events', { events: [ev({})], next_cursor: null }]]);
    els.obsEventQ = els.obsEventQ || null;
    const inp = { value: 'task_stuck' };
    // 壳渲染时用 document.getElementById('obsEventQ') 取值
    obs.loadObsEvents({ innerHTML: '', id: 'obsContent' });
    await tick(); await tick();
    const q = { id: 'obsEventQ', value: 'task_stuck' };
    // 让 getElementById('obsEventQ') 返回带值的元素
    const env2 = makeEnv([['/obs/events', { events: [], next_cursor: null }]]);
    env2.els.obsEventQ = Object.assign(env2.els.obsEventQ || {}, q);
    env2.obs.loadObsEvents({ innerHTML: '', id: 'obsContent' });
    await tick();
    env2.els.obsEventQ.value = 'task_stuck';
    env2.obs.obsEventSearch();
    await tick(); await tick();
    const last = env2.calls[env2.calls.length - 1];
    assert.ok(last.indexOf('q=task_stuck') >= 0, '关键字应作为服务端参数发出: ' + last);
    assert.ok(calls.length >= 1, '首个环境也应发出请求');
  });

  console.log('\n== 游标分页：加载更多不重不漏 ==');
  await checkAsync('next_cursor → 加载更多 → 追加且末页按钮消失', async () => {
    const page1 = { events: [ev({ key: 'a' }), ev({ key: 'b' })], next_cursor: 'c1', truncated_sources: [] };
    const page2 = { events: [ev({ key: 'c' }), ev({ key: 'd' })], next_cursor: null, truncated_sources: [] };
    const { obs, calls } = makeEnv([
      ['/obs/events?limit=50', (url) => (url.indexOf('before=') >= 0 ? page2 : page1)],
    ]);
    obs.loadObsEvents({ innerHTML: '', id: 'obsContent' });
    await tick(); await tick();
    assert.strictEqual(obs.getData().events.length, 2, '首页 2 条');
    assert.strictEqual(obs.getMore(), true, '还有下一页');
    assert.ok(obs.getCursor() === 'c1', '游标应为 c1');
    obs.obsEventLoadMore();
    await tick(); await tick();
    const keys = obs.getData().events.map(x => x.key);
    assert.deepStrictEqual(keys, ['a', 'b', 'c', 'd'], '追加且不重复: ' + keys.join(','));
    assert.strictEqual(obs.getMore(), false, '末页后不再有"加载更多"');
    assert.ok(calls.length >= 2, '应发出两次请求');
  });

  console.log('\n== 自动刷新：30s 提示新事件（不强制前插）==');
  await checkAsync('软刷新 → 提示 N 条 → 点击前插且去重；30s 内不重复打接口', async () => {
    let n = 0;
    const { obs, els, calls } = makeEnv([
      ['/obs/events', () => {
        n += 1;
        return (n === 1)
          ? { events: [ev({ key: 'old' })], next_cursor: null, truncated_sources: [] }
          : { events: [ev({ key: 'new1' }), ev({ key: 'old' })], next_cursor: null, truncated_sources: [] };
      }],
    ]);
    obs.loadObsEvents({ innerHTML: '', id: 'obsContent' });
    await tick(); await tick();
    const before = calls.length;
    obs._obsEventSoftRefresh();              // 首次：应发一次
    await tick(); await tick();
    assert.strictEqual(calls.length, before + 1, '首次软刷新应打接口');
    assert.strictEqual(obs.getNew().length, 1, '应攒下 1 条新事件');
    assert.ok(els.obsEventNew.innerHTML.indexOf('«observability.ev_new_events»') >= 0,
      '顶部应有新事件提示: ' + els.obsEventNew.innerHTML);
    obs.obsEventApplyNew();                  // 用户点击才前插
    assert.deepStrictEqual(obs.getData().events.map(x => x.key), ['new1', 'old'], '前插到顶部且去重');
    const after = calls.length;
    obs._obsEventSoftRefresh();              // 30s 内：不应再打
    await tick();
    assert.strictEqual(calls.length, after, '30s 内应跳过软刷新（页面 10s 轮询不重复打）');
  });

  console.log('\n== 截断提示 / 行内展开 / 瞬时事件 ==');
  await checkAsync('truncated_sources → 顶部提示', async () => {
    const { obs, els } = makeEnv([
      ['/obs/events', { events: [ev({})], next_cursor: null, truncated_sources: ['task', 'audit'] }],
    ]);
    obs.loadObsEvents({ innerHTML: '', id: 'obsContent' });
    await tick(); await tick();
    const txt = els.obsEventTrunc.textContent || '';
    assert.ok(txt.indexOf('«observability.ev_truncated»') >= 0, '缺截断提示: ' + txt);
  });

  await checkAsync('行内展开：点行显示指标/阈值/详情', async () => {
    const { obs, els } = makeEnv([
      ['/obs/events', { events: [ev({ key: 'alert:1', metric: 'task_stuck', value: 3, threshold: 0, detail: 'boom' })], next_cursor: null, truncated_sources: [] }],
    ]);
    obs.loadObsEvents({ innerHTML: '', id: 'obsContent' });
    await tick(); await tick();
    obs.obsEventToggle('alert:1');
    const d = els['obsev-d-alert_1'];
    assert.ok(d && d.innerHTML.indexOf('«observability.ev_metric»') >= 0, '展开应显示指标');
    assert.ok(d.innerHTML.indexOf('task_stuck') >= 0, '展开应显示指标值');
    assert.ok(d.innerHTML.indexOf('boom') >= 0, '展开应显示详情');
  });

  check('瞬时事件（ts_kind=point）不显示时长', () => {
    const { obs } = makeEnv([]);
    const html = obs._obsEventRowHtml(ev({
      key: 'change:5', source: 'change', kind: 'change', ts_kind: 'point', outcome: 'merged',
      state: 'done', duration_sec: 300, ongoing: false, next_step: ['issue'],
    }));
    assert.ok(html.indexOf('dur_unit') === -1, '瞬时事件不该出现时长: ' + html);
    const iv = obs._obsEventRowHtml(ev({ key: 'alert:2', duration_sec: 120 }));
    assert.ok(iv.indexOf('«observability.dur_unit_min»') >= 0, '区间事件应显示时长');
  });

  await Promise.all(pending);
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();
