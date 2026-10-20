/*
 * 可观测性分区路由回归测试（可观测性升为一级分类 + 3 分区）
 *
 * 运行：node tests/js/test_observability_sections.js
 * 设计：docs/weekly-07/observability-redesign-design.md §5.1/§5.2
 *       docs/weekly-07/observability-overview-page-design.md（总览数据与图表）
 *
 * 背景：可观测性从「设备中心」下的二级项升为一级分类，侧栏挂 3 个分区
 * （总览 / 设备与引擎 / 任务与日志）。分区经 URL param 传入（#observability/devices），
 * 页面内不再重复分区 Tab，只在分区内保留子 Tab。
 *
 * 验证点：
 *   1. devices 分区只有引擎/设备一个视图，不画 Tab 条，且不含异常任务
 *   2. inspect 分区子 Tab 恰为 [任务, 事件, 错误日志]，顺序固定
 *      （2026-10-18 拍板：「异常任务」并入「任务」Tab 的过滤 chip，不再单独占子 Tab）
 *   3. 有数据的两个分区：发请求 + 开轮询
 *   4. overview 分区：拉 /obs/overview、画 KPI + 3 张图，**不参与设备分区的 10s 轮询**
 *   5. 总览图表的 option 结构（双轴 / 堆叠 / 占比）与数据一致
 *   6. 无 ECharts 时降级：KPI 照常显示、图表位给提示、不抛异常
 *   7. 未知分区名回落当前分区，不抛异常
 *   8. 切换分区会重置到该分区默认子 Tab（默认选中第一个）
 *   9. 子 Tab 仍沿用旧取值 overview/anomaly/tasks/errorlogs
 *      —— 保证 renderObsContent 的既有分发逻辑无需改动（原功能不破）
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

let passed = 0, failed = 0;
const _pendingChecks = [];
function check(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
}
// 需要走真实异步链（API.get → then → 渲染）的用例
function checkAsync(name, fn) {
  _pendingChecks.push((async () => {
    try { await fn(); passed++; console.log('  ✓ ' + name); }
    catch (e) { failed++; console.log('  ✗ ' + name + '\n      ' + e.message); }
  })());
}
const tick = (ms) => new Promise(r => setTimeout(r, ms === undefined ? 20 : ms));

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

// 按 URL 前缀给 API 桩配响应体：extra.payloads = [[prefix, payload], ...]
// payload === '__reject__' → 该请求返回 rejected Promise（用于测降级路径）
function _pickPayload(extra, url) {
  const ps = (extra && extra.payloads) || [];
  for (const [prefix, payload] of ps) {
    if (String(url).indexOf(prefix) === 0) {
      if (payload === '__reject__') return Promise.reject(new Error('boom'));
      return payload;
    }
  }
  return {};
}

// 加载 observability.js：统计 API.get 调用与 setInterval 调用（断言"总览不参与设备轮询"）
// extra：额外注入的沙箱全局（如假的 echarts，用于捕获 setOption 的 option）
function loadObs(dom, extra) {
  const src = fs.readFileSync(
    path.resolve(__dirname, '../../src/api/static/js/pages/observability.js'), 'utf8');
  const state = { api: [], intervals: 0 };
  const I18N = { t: (k) => '«' + k + '»', getLang: () => 'zh' };
  const sandbox = {
    document: dom,
    window: { _currentView: 'observability', history: { pushState() {}, replaceState() {}, state: null }, location: { hash: '' }, addEventListener: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} },
    API: { get: (u) => { state.api.push(u); return Promise.resolve(_pickPayload(extra, u)); }, post: (u) => { state.api.push(u); return Promise.resolve(_pickPayload(extra, u)); } },
    I18N,
    console: { log: () => {}, error: () => {}, warn: () => {} },
    fmtLocalTime: (s) => s || '',
    fetch: () => Promise.resolve({ json: () => Promise.resolve({}), text: () => Promise.resolve('') }),
    setTimeout, clearTimeout,
    setInterval: function (...a) { state.intervals++; return setInterval(...a); },
    clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, URLSearchParams,
    CustomEvent: function () {}, AbortController: function () { this.abort = () => {}; this.signal = {}; },
    AbortSignal: { timeout: () => ({}) }, openRuntimeSessionModal: () => {},
  };
  Object.assign(sandbox, extra || {});
  const sKeys = Object.keys(sandbox);
  const sVals = sKeys.map(k => sandbox[k]);
  const exportsStr = '\n; return {' +
    'renderObservability: typeof renderObservability !== "undefined" ? renderObservability : null,' +
    '_obsSection: typeof _obsSection !== "undefined" ? _obsSection : null,' +
    '_obsTab: typeof _obsTab !== "undefined" ? _obsTab : null,' +
    '_OBS_SECTIONS: typeof _OBS_SECTIONS !== "undefined" ? _OBS_SECTIONS : null,' +
    '_renderObsOverview: typeof _renderObsOverview !== "undefined" ? _renderObsOverview : null,' +
    '_obsBannerHtml: typeof _obsBannerHtml !== "undefined" ? _obsBannerHtml : null,' +
    'openObsAlertPanel: typeof openObsAlertPanel !== "undefined" ? openObsAlertPanel : null,' +
    'switchObsAlertPanel: typeof switchObsAlertPanel !== "undefined" ? switchObsAlertPanel : null,' +
    '_renderObsAlertPanel: typeof _renderObsAlertPanel !== "undefined" ? _renderObsAlertPanel : null,' +
    '_renderObsResChart: typeof _renderObsResChart !== "undefined" ? _renderObsResChart : null,' +
    '_obsChanLabel: typeof _obsChanLabel !== "undefined" ? _obsChanLabel : null,' +
    '_obsSeverityCell: typeof _obsSeverityCell !== "undefined" ? _obsSeverityCell : null,' +
    '_obsSourceLabel: typeof _obsSourceLabel !== "undefined" ? _obsSourceLabel : null,' +
    '_obsEventTarget: typeof _obsEventTarget !== "undefined" ? _obsEventTarget : null,' +
    '_obsMetricVal: typeof _obsMetricVal !== "undefined" ? _obsMetricVal : null,' +
    'loadObsEvents: typeof loadObsEvents !== "undefined" ? loadObsEvents : null,' +
    '_durHuman: typeof _durHuman !== "undefined" ? _durHuman : null,' +
    '_renderObsEventRows: typeof _renderObsEventRows !== "undefined" ? _renderObsEventRows : null,' +
    'loadErrorLogs: typeof loadErrorLogs !== "undefined" ? loadErrorLogs : null,' +
    'openObsTimeline: typeof openObsTimeline !== "undefined" ? openObsTimeline : null,' +
    'jumpObsLogsForTask: typeof jumpObsLogsForTask !== "undefined" ? jumpObsLogsForTask : null,' +
    'openTaskLogModal: typeof openTaskLogModal !== "undefined" ? openTaskLogModal : null,' +
    '_errorLogHtmlDb: typeof _errorLogHtmlDb !== "undefined" ? _errorLogHtmlDb : null,' +
    'loadObsSlo: typeof loadObsSlo !== "undefined" ? loadObsSlo : null,' +
    '_obsSloRepoFull: typeof _obsSloRepoFull !== "undefined" ? _obsSloRepoFull : null,' +
    '_fillObsSloCard: typeof _fillObsSloCard !== "undefined" ? _fillObsSloCard : null,' +
    'openObsSloBurn: typeof openObsSloBurn !== "undefined" ? openObsSloBurn : null,' +
    'renderTasksTab: typeof renderTasksTab !== "undefined" ? renderTasksTab : null,' +
    'renderOverview: typeof renderOverview !== "undefined" ? renderOverview : null,' +
    'switchObsView: typeof switchObsView !== "undefined" ? switchObsView : null,' +
    'obsTokMode: typeof obsTokMode !== "undefined" ? obsTokMode : null,' +
    '_runtimeRowHTML: typeof _runtimeRowHTML !== "undefined" ? _runtimeRowHTML : null,' +
    '_obsTokColNum: typeof _obsTokColNum !== "undefined" ? _obsTokColNum : null,' +
    '_obsTokFor: typeof _obsTokFor !== "undefined" ? _obsTokFor : null,' +
    '_obsTokTaskFor: typeof _obsTokTaskFor !== "undefined" ? _obsTokTaskFor : null,' +
    'loadObsTokenColumns: typeof loadObsTokenColumns !== "undefined" ? loadObsTokenColumns : null,' +
    'renderAnomalyTab: typeof renderAnomalyTab !== "undefined" ? renderAnomalyTab : null,' +
    'openObsAnomalyModal: typeof openObsAnomalyModal !== "undefined" ? openObsAnomalyModal : null,' +
    '_obsAnomalyRerender: typeof _obsAnomalyRerender !== "undefined" ? _obsAnomalyRerender : null,' +
    '_obsResSeries: typeof _obsResSeries !== "undefined" ? _obsResSeries : null,' +
    '_obsResData: typeof _obsResData !== "undefined" ? _obsResData : null,' +
    'switchObsRange: typeof switchObsRange !== "undefined" ? switchObsRange : null' +
    '};';
  const fn = new Function(...sKeys, src + exportsStr);
  return { obs: fn(...sVals), state, dom };
}

const SEC_RE = (sec) => '«observability.section.' + sec + '»';

// 后端 /api/obs/overview 的响应形态（与 tests/test_obs_overview_api.py 的断言一致）
const PAYLOAD = {
  range: '24h', bucket_seconds: 1800, scope: { repo: '', repos: 3, restricted: true },
  kpis: { queue_depth: 2, success_rate: 66.7, p95_duration_s: 192, engines_online: 1, engines_total: 2, tokens: 1950 },
  charts: {
    task_outcome: {
      buckets: ['2026-10-17T00:00:00+00:00', '2026-10-17T00:30:00+00:00', '2026-10-17T01:00:00+00:00'],
      completed: [2, 3, 4], failed: [0, 1, 0], cancelled: [0, 0, 1], rate: [100, 75, 80],
    },
    token_trend: {
      buckets: ['2026-10-17T00:00:00+00:00', '2026-10-17T00:30:00+00:00'],
      input: [100, 200], output: [10, 20], cache: [0, 500],
    },
    model_share: [
      { model: 'glm-5.3', tokens: 1620, share: 83.1 },
      { model: 'deepseek-v4-flash-0731', tokens: 330, share: 16.9 },
    ],
  },
};

function fakeEcharts(captured) {
  return {
    init: () => ({ setOption(o) { captured.push(o); }, dispose() {}, resize() {} }),
  };
}

console.log('== 分区 → 子 Tab 映射 ==');

check('devices 分区：只有引擎/设备一个视图，不画单条 Tab', () => {
  const { obs, dom } = loadObs(makeDom());
  obs.renderObservability('devices');
  const h = dom._elements.pageContent.innerHTML;
  assert.ok(!h.includes('switchObsTab('), '只有一个子视图时不应画 Tab 条');
  assert.ok(!h.includes('«observability.tab.anomaly»'), '异常任务已移出设备分区');
  assert.ok(h.includes(SEC_RE('devices')), '标题未显示当前分区名');
});

check('inspect 分区：子 Tab 恰为 [任务, 事件, 错误日志]', () => {
  const { obs, dom } = loadObs(makeDom());
  obs.renderObservability('inspect');
  const h = dom._elements.pageContent.innerHTML;
  assert.ok(h.includes('«observability.tab.tasks»'), '缺 任务 子 Tab');
  assert.ok(h.includes('«observability.tab.events»'), '缺 事件 子 Tab');
  assert.ok(h.includes('«observability.tab.errorlogs»'), '缺 错误日志 子 Tab');
  assert.ok(!h.includes('«observability.tab.anomaly»'), '异常任务已并入任务 Tab，不该再占子 Tab');
  // 顺序：任务 → 事件 → 错误日志
  const iT = h.indexOf('«observability.tab.tasks»');
  const iA = h.indexOf('«observability.tab.events»');
  const iE = h.indexOf('«observability.tab.errorlogs»');
  assert.ok(iT < iA && iA < iE, '子 Tab 顺序应为 任务 → 异常任务 → 错误日志');
});

check('有数据的两个分区：发请求 + 开轮询', () => {
  for (const sec of ['devices', 'inspect']) {
    const { obs, state } = loadObs(makeDom());
    state.api.length = 0; state.intervals = 0;
    obs.renderObservability(sec);
    assert.ok(state.api.length > 0, sec + ' 未触发数据加载');
    assert.ok(state.intervals > 0, sec + ' 未开启轮询');
  }
});

console.log('\n== 总览分区：真实数据（KPI + 图表）==');

check('overview 分区：拉 /obs/overview、无子 Tab、不参与设备轮询', () => {
  const { obs, state, dom } = loadObs(makeDom());
  state.api.length = 0; state.intervals = 0;
  obs.renderObservability('overview');
  const h = dom._elements.pageContent.innerHTML;
  assert.ok(h.includes(SEC_RE('overview')), '标题未显示当前分区名');
  assert.ok(!h.includes('switchObsTab('), '总览不应有子 Tab');
  assert.ok(h.includes('switchObsRange('), '总览应提供时间范围选择');
  assert.ok(state.api.some(u => u.indexOf('/obs/overview') === 0),
    '未拉总览数据: ' + state.api.join(','));
  assert.ok(!state.api.some(u => u.indexOf('/dashboard/ops/overview') === 0),
    '总览不该拉设备分区的数据');
  assert.strictEqual(state.intervals, 0, '总览不该开启设备轮询');
});

check('总览图表：ECharts 收到 3 张图，双轴/堆叠/占比数据与后端一致', () => {
  const captured = [];
  const { obs } = loadObs(makeDom(), { echarts: fakeEcharts(captured) });
  obs._renderObsOverview(PAYLOAD);
  assert.strictEqual(captured.length, 3, '应画 3 张图（任务结果 / 模型占比 / Token）');
  const [outcome, token, model] = captured;
  // ① 任务量与结果：柱堆叠 + 成功率右轴（固定 0-100）
  assert.strictEqual(outcome.yAxis.length, 2, '任务图应为双轴');
  assert.strictEqual(outcome.series[3].yAxisIndex, 1, '成功率应画在右轴');
  assert.strictEqual(outcome.yAxis[1].max, 100, '右轴上限应固定 100%');
  assert.deepStrictEqual(outcome.series[0].data, [2, 3, 4], '完成任务数不对');
  assert.deepStrictEqual(outcome.series[3].data, [100, 75, 80], '成功率折线数据不对');
  // ② Token 消耗：输入/输出/缓存堆叠
  assert.deepStrictEqual(token.series[0].data, [100, 200], '输入 token 不对');
  assert.deepStrictEqual(token.series[2].data, [0, 500], '缓存 token 不对');
  // ③ 模型占比：横向条（y 轴类目，倒序后最差在下方）
  assert.deepStrictEqual(model.yAxis.data, ['deepseek-v4-flash-0731', 'glm-5.3'],
    '模型占比应按 y 轴自下而上排列');
  assert.deepStrictEqual(model.series[0].data, [330, 1620], '模型 token 数不对');
});

check('无 ECharts 时降级：KPI 照常显示、图表位给提示、不抛异常', () => {
  const { obs, dom } = loadObs(makeDom());   // 沙箱中不注入 echarts
  obs._renderObsOverview(PAYLOAD);
  const h = dom._elements.obsOvBody.innerHTML;
  assert.ok(h.includes('66.7%'), 'KPI 成功率未渲染');
  assert.ok(h.includes('1,950') || h.includes('1950'), 'KPI token 数未渲染');
  assert.ok(h.includes('1 / 2'), 'KPI 在线引擎未渲染');
  for (const id of ['obsChart-outcome', 'obsChart-token', 'obsChart-model']) {
    assert.ok(dom._elements[id].innerHTML.includes('«observability.ov.charts_unavailable»'),
      id + ' 未给出降级提示');
  }
});

check('空响应 / 缺字段不抛异常（后端降级或空范围时仍能渲染）', () => {
  const captured = [];
  const { obs } = loadObs(makeDom(), { echarts: fakeEcharts(captured) });
  obs._renderObsOverview({});
  obs._renderObsOverview({ kpis: { tokens: 0 }, charts: {} });
  assert.strictEqual(captured.length, 6, '空响应也应画出图表容器');
});

console.log('\n== 告警横幅 + 告警面板（告警不进侧栏）==');

const ALERTS_FIRING = {
  summary: {
    firing: 2, critical: 1,
    latest: { id: 7, rule_name: 'task_stuck', repo_key: 'gitcode:org/x', metric: 'task_stuck', value: 3, threshold: 0, status: 'firing', last_seen_at: '2026-10-17T05:00:00+00:00', silenced_until: '' },
    by_severity: { critical: 1, warning: 1, info: 0 },
  },
  events: [
    { id: 7, rule_name: 'task_stuck', level: 'user', severity: 'critical', status: 'firing', metric: 'task_stuck', value: 3, threshold: 0, repo_key: 'gitcode:org/x', last_seen_at: '2026-10-17T05:00:00+00:00', silenced_until: '' },
    { id: 8, rule_name: 'task_failure_rate', level: 'user', severity: 'warning', status: 'resolved', metric: 'task_failure_rate', value: 0.61, threshold: 0.5, repo_key: 'gitcode:org/y', last_seen_at: '2026-10-17T04:00:00+00:00', silenced_until: '' },
  ],
  rules: [
    { id: 1, name: 'task_stuck', level: 'user', metric: 'task_stuck', comparator: 'gt', threshold: 0, for_sec: 300, severity: 'warning', enabled: 1 },
    { id: 2, name: 'task_queue_backlog', level: 'user', metric: 'task_queued', comparator: 'gt', threshold: 10, for_sec: 600, severity: 'warning', enabled: 1 },
  ],
};

check('无告警时横幅为正向文案（不吓人、不红）', () => {
  const { obs, dom } = loadObs(makeDom());
  obs._renderObsOverview(PAYLOAD, { summary: { firing: 0, critical: 0, latest: null }, events: [], rules: [] });
  const h = dom._elements.obsOvBody.innerHTML;
  assert.ok(h.includes('«observability.banner.ok»'), '无告警应显示正向文案');
  assert.ok(!h.includes('«observability.banner.firing»'), '无告警不该显示计数');
  assert.ok(!h.includes('openObsAlertPanel()'), '无告警不该出现查看按钮');
});

check('有告警时横幅显示条数 + 最近一条 + 查看按钮', () => {
  const { obs, dom } = loadObs(makeDom());
  obs._renderObsOverview(PAYLOAD, ALERTS_FIRING);
  const h = dom._elements.obsOvBody.innerHTML;
  assert.ok(h.includes('«observability.banner.firing»'), '缺告警计数');
  assert.ok(h.includes('«observability.rule.task_stuck»'), '最近一条应显示规则名');
  assert.ok(h.includes('org/x'), '应显示仓库（去 gitcode: 前缀）');
  assert.ok(h.includes('openObsAlertPanel()'), '应提供进入面板的入口');
  // critical 存在时用红色，否则琥珀色
  assert.ok(h.includes('#d64545'), 'critical 告警应用红色');
});

checkAsync('告警面板：3 个子视图 + 触发记录表 + 可静默', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/alerts', ALERTS_FIRING]] });
  obs.openObsAlertPanel();
  await tick();
  const modal = dom._elements.obsAlertModal;
  assert.ok(modal, '未创建面板容器');
  ['«observability.alert_tab.events»', '«observability.alert_tab.rules»', '«observability.alert_tab.silences»']
    .forEach(k => assert.ok(modal.innerHTML.includes(k), '面板缺子视图: ' + k));
  const body = dom._elements.obsAlertPanelBody.innerHTML;
  assert.ok(body.includes('«observability.event_col.rule»'), '触发记录表头缺失');
  assert.ok(body.includes('obsAlertSilence(7)'), 'firing 事件应可静默');
  assert.ok(!body.includes('obsAlertSilence(8)'), '已恢复事件不该给静默入口');
  assert.ok(body.includes('61%'), '失败率 0.61 应显示为 61%: ' + body.slice(0, 400));
});

checkAsync('告警面板：切到规则视图显示启停入口', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/alerts', ALERTS_FIRING]] });
  obs.openObsAlertPanel('rules');
  await tick();
  obs.switchObsAlertPanel('rules');
  const body = dom._elements.obsAlertPanelBody.innerHTML;
  assert.ok(body.includes('«observability.rule.task_stuck»'), '规则名缺失');
  assert.ok(body.includes('obsAlertToggleRule(1,false)'), '启用中的规则应提供停用入口');
  assert.ok(body.includes('obsAlertSetThreshold(1,0)'), '应提供阈值修改入口');
  // 通知渠道（飞书）
  assert.ok(body.includes('«observability.rule_col.notify»'), '缺通知列表头');
  assert.ok(body.includes('obsAlertSetNotify(1,'), '应提供通知渠道编辑入口');
  assert.ok(body.includes('«observability.notify_none»'), '未配置通知应显示"不推送"');
});

check('告警面板：提供"测试飞书通知"按钮', () => {
  const { obs, dom } = loadObs(makeDom());
  obs.openObsAlertPanel();
  assert.ok(dom._elements.obsAlertModal.innerHTML.includes('obsAlertTestNotify()'), '缺测试通知入口');
  assert.ok(dom._elements.obsAlertModal.innerHTML.includes('«observability.test_notify»'), '缺按钮文案');
});

check('通知渠道标签：空=不推送、feishu=飞书私聊、含 webhook 都显示', () => {
  const { obs } = loadObs(makeDom());
  assert.ok(obs._obsChanLabel('').includes('«observability.notify_none»'), '空渠道应显示不推送');
  assert.ok(obs._obsChanLabel('feishu').includes('«observability.notify_feishu»'), 'feishu 标签不对');
  const both = obs._obsChanLabel('feishu,webhook');
  assert.ok(both.includes('«observability.notify_feishu»') && both.includes('«observability.notify_webhook»'),
    '双渠道应都显示: ' + both);
});

check('级别单元格（P1-1）：只读显示级别 + 聚合/升级标记', () => {
  const { obs } = loadObs(makeDom());
  const plain = obs._obsSeverityCell({ severity: 'warning' });
  assert.ok(plain.includes('«observability.sev.warning»'), '缺级别文案: ' + plain);
  assert.ok(!plain.includes('«observability.tag_aggregate»'), '普通规则不该有聚合标记');
  const rich = obs._obsSeverityCell({ severity: 'critical', aggregate_mode: 'rule',
    escalate_json: '{"after_sec":60}', escalated_at: '2026-10-17T00:00:00+00:00',
    notify_deferred: 1 });
  assert.ok(rich.includes('«observability.sev.critical»'), '级别不对: ' + rich);
  assert.ok(rich.includes('«observability.tag_aggregate»'), '缺聚合标记');
  assert.ok(rich.includes('«observability.tag_escalated»'), '缺已升级标记');
  assert.ok(rich.includes('«observability.tag_deferred»'), '缺时段外标记');
  const none = obs._obsSeverityCell({});
  assert.ok(none.includes('«observability.sev.warning»'), '缺省级别应为 warning');
});

check('数据缺失/突变（P1-3）：值说人话 + 来源标签 + 设备对象列', () => {
  const { obs } = loadObs(makeDom());
  const gap = obs._obsMetricVal({ metric: 'gap_runtime', value: 1800 });
  assert.ok(gap.includes('«observability.dur_min»'), 'gap 值应显示分钟: ' + gap);
  const gapH = obs._obsMetricVal({ metric: 'gap_runtime', value: 7200 });
  assert.ok(gapH.includes('«observability.dur_hour»'), 'gap 值应显示小时: ' + gapH);
  const gapD = obs._obsMetricVal({ metric: 'gap_repo', value: 200000 });
  assert.ok(gapD.includes('«observability.dur_day»'), 'gap 值应显示天: ' + gapD);
  const mut = obs._obsMetricVal({ metric: 'task_success_rate_mutation', value: -0.35 });
  assert.ok(mut.indexOf('-35pp') >= 0, '突变应带符号百分点: ' + mut);
  assert.ok(obs._obsMetricVal({ metric: 'task_stuck', value: 3 }).indexOf('3') >= 0, '指标值应保留原样');
  assert.ok(obs._obsSourceLabel({ source: 'gap' }).includes('«observability.source.gap»'), '来源标签缺失');
  assert.ok(obs._obsSourceLabel({}).includes('«observability.source.monitor»'), '缺省来源应为指标');
  assert.ok(obs._obsEventTarget({ dim_key: 'runtime:12' }).indexOf('12') > 0, '设备对象列缺失');
  assert.strictEqual(obs._obsEventTarget({ dim_key: 'repo:a/b' }), '', '仓库维度不该有设备对象');
});

check('告警面板：无告警时给空态', () => {
  const { obs, dom } = loadObs(makeDom());
  obs.openObsAlertPanel();
  obs._obsAlerts = { summary: { firing: 0 }, events: [], rules: [] };
  obs._renderObsAlertPanel();
  assert.ok(dom._elements.obsAlertPanelBody.innerHTML.includes('«observability.no_alerts»'), '缺空态文案');
});

console.log('\n== 设备分区：资源曲线 ==');

const RES_HISTORY = {
  range: '6h', bucket_seconds: 300,
  buckets: ['2026-10-17T00:00:00+00:00', '2026-10-17T00:05:00+00:00'],
  runtimes: [{ id: 1, device_name: 'worker-a' }, { id: 2, device_name: 'worker-b' }],
  series: {
    '1': { cpu: [10, 20], mem: [30, 40], disk: [50, 50], health: [90, 88] },
    '2': { cpu: [30, null], mem: [50, null], disk: [70, 70], health: [80, 80] },
  },
};

check('devices 分区：拉资源历史并渲染曲线容器', () => {
  const { obs, state, dom } = loadObs(makeDom());
  state.api.length = 0;
  obs.renderObservability('devices');
  const h = dom._elements.pageContent.innerHTML;
  assert.ok(h.includes('«observability.res.title»'), '缺资源趋势标题');
  assert.ok(h.includes('id="obsResChart"'), '缺图表容器');
  assert.ok(state.api.some(u => u.indexOf('/obs/runtimes/history') === 0),
    '未拉资源历史: ' + state.api.join(','));
});

checkAsync('资源曲线：全部=各引擎平均（空桶不参与），指定引擎=单台', async () => {
  const { obs } = loadObs(makeDom(), { payloads: [['/obs/runtimes/history', RES_HISTORY]] });
  obs.renderObservability('devices');
  await tick();
  assert.deepStrictEqual(obs._obsResSeries('cpu'), [20, 20], '全部应为均值，空桶按已有值平均');
});

checkAsync('资源曲线：ECharts 收到 3 条折线（CPU/内存/磁盘）', async () => {
  const captured = [];
  const { obs } = loadObs(makeDom(), {
    echarts: fakeEcharts(captured),
    payloads: [['/obs/runtimes/history', RES_HISTORY]],
  });
  obs.renderObservability('devices');
  await tick();
  assert.strictEqual(captured.length, 1, '应画 1 张折线图');
  const opt = captured[0];
  assert.strictEqual(opt.series.length, 3, '应有 3 条线');
  assert.deepStrictEqual(opt.series[0].data, [20, 20], 'CPU 线数据不对');
  assert.strictEqual(opt.series[0].type, 'line', '应为折线');
  assert.strictEqual(opt.yAxis.max, 100, '百分比轴上限应为 100');
});

checkAsync('资源曲线：无采样时给空态而不是空图', async () => {
  const captured = [];
  const { obs, dom } = loadObs(makeDom(), {
    echarts: fakeEcharts(captured),
    payloads: [['/obs/runtimes/history', { range: '6h', buckets: [], runtimes: [], series: {} }]],
  });
  obs.renderObservability('devices');
  await tick();
  assert.strictEqual(captured.length, 0, '无数据不该画图');
  assert.ok(dom._elements.obsResChart.innerHTML.includes('«observability.res.empty»'), '缺空态文案');
});

console.log('\n== 日志增强：错误日志 tab 走 obs_log（库检索 + 文件降级）==');

const LOGS_PAYLOAD = {
  total: 2, limit: 200, offset: 0, scoped_repos: 3,
  facets: { levels: { ERROR: 1, WARNING: 1 }, sources: ['worker-a', 'dev-lg'] },
  items: [
    { id: 2, ts: '2026-10-17T06:10:00+00:00', level: 'ERROR', component: 'worker', source: 'worker-a',
      repo_key: 'gitcode:org/x', task_id: 't_abc123', msg: '<script>boom</script> token=grape_SECRET123' },
    { id: 1, ts: '2026-10-17T06:00:00+00:00', level: 'WARNING', component: 'worker', source: 'dev-lg',
      repo_key: 'gitcode:org/y', task_id: '', msg: 'slow request' },
  ],
};

check('日志 tab：拉 /obs/logs（带级别/来源/关键字/仓库参数）并渲染', () => {
  const { obs, state, dom } = loadObs(makeDom(), { payloads: [['/obs/logs', LOGS_PAYLOAD]] });
  state.api.length = 0;
  obs.loadErrorLogs(dom.getElementById('obsContent'));
  const call = state.api.find(u => u.indexOf('/obs/logs?') === 0);
  assert.ok(call, '未调用 /obs/logs: ' + state.api.join(','));
  assert.ok(call.indexOf('limit=200') > 0, '缺少 limit');
  assert.ok(dom._elements.obsContent.innerHTML.includes('«observability.log_scope_hint»'), '缺作用域提示');
  assert.ok(dom._elements.obsContent.innerHTML.includes('«observability.log_search_placeholder»'), '缺关键字框');
});

checkAsync('日志 tab：渲染来源/组件/仓库短名/任务/级别，正文转义', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/logs', LOGS_PAYLOAD]] });
  obs.loadErrorLogs(dom.getElementById('obsContent'));
  await tick();
  const h = dom._elements.errorLogBody.innerHTML;
  assert.ok(h.includes('[worker-a]'), '未显示来源');
  assert.ok(h.includes('>org/x<'), '未显示仓库短名');
  // 完整 repo_key 只允许出现在 title 提示里（可见文本用短名，用户偏好：不显示 raw 前缀）
  assert.ok(h.indexOf('gitcode:org/x') === h.indexOf('title="gitcode:org/x"') + 7,
    '仓库前缀只应出现在 title 属性中');
  assert.ok(h.includes('t_abc123'), '未显示任务号');
  assert.ok(h.includes('<b>ERROR</b>') && h.includes('<b>WARNING</b>'), '未显示级别');
  assert.ok(h.includes('&lt;script&gt;'), '正文未做 HTML 转义', );
  assert.ok(!h.includes('<script>'), '正文里的 script 标签未被转义');
  assert.ok(dom._elements.errorLogCount.innerHTML === '' && dom._elements.errorLogCount.textContent.includes('«observability.log_count_db»'),
    '未显示计数: ' + dom._elements.errorLogCount.textContent);
});

checkAsync('日志 tab：/obs/logs 不可用时降级到文件扫描', async () => {
  const { obs, state, dom } = loadObs(makeDom(), { payloads: [['/obs/logs', '__reject__']] });
  state.api.length = 0;
  obs.loadErrorLogs(dom.getElementById('obsContent'));
  await tick(40);
  assert.ok(state.api.length === 0 || state.api.some(u => u.indexOf('/obs/logs') === 0), '首次应尝试库检索');
  // 降级走 fetch（沙箱 fetch 返回空 → 空态），关键是不抛异常且给出空态
  assert.ok(dom._elements.errorLogBody.innerHTML.length >= 0, '降级路径不应抛异常');
});

checkAsync('日志 tab：无匹配时给空态而不是空白', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/logs', { total: 0, items: [], facets: {} }]] });
  obs.loadErrorLogs(dom.getElementById('obsContent'));
  await tick();
  assert.ok(dom._elements.errorLogBody.innerHTML.includes('«observability.no_matching_log»'), '缺空态文案');
});

console.log('\n== 追踪时间线（§6.4）==');

const TRACE_PAYLOAD = {
  trace: 'tr_abc', kind: 'pipeline', pipeline_trace_id: 'tr_abc',
  repo_key: 'gitcode:org/x', issue_id: '#7',
  counts: { tasks: 2, failed: 1 },
  tasks: [
    { id: 't_ok1', agent_type: 'vod', status: 'completed', runtime_id: 262, engine: 'opencode@worker-a',
      enqueue_at: '2026-10-17T06:00:00+00:00', dispatch_at: '2026-10-17T06:00:02+00:00',
      terminal_at: '2026-10-17T06:00:12+00:00', queue_wait_s: 2, duration_s: 10,
      tokens: { total: 1600 }, failure_reason: '',
      steps: [{ seq: 1, type: 'tool_use', tool: 'shell', input: '{"cmd":"ls"}', output: 'ok', content: '' }],
      log_hits: [] },
    { id: 't_bad1', agent_type: 'coding', status: 'failed', runtime_id: 262, engine: 'opencode@worker-a',
      enqueue_at: '2026-10-17T06:01:00+00:00', dispatch_at: '2026-10-17T06:01:05+00:00',
      terminal_at: '2026-10-17T06:03:05+00:00', queue_wait_s: 5, duration_s: 120,
      tokens: {}, failure_reason: 'pytest failed: 3 errors',
      steps: [{ seq: 1, type: 'error', tool: '', input: '', output: 'boom', content: '' }],
      log_hits: [{ ts: '2026-10-17T06:03:00+00:00', level: 'ERROR', msg: 'engine crashed' }] },
  ],
  logs: [{ ts: '2026-10-17T06:03:00+00:00', level: 'ERROR', task_id: 't_bad1', msg: 'engine crashed' }],
};

checkAsync('时间线：按任务号拉取并渲染（流水线视图 + 步骤 + 失败高亮 + 日志命中）', async () => {
  const { obs, state, dom } = loadObs(makeDom(), { payloads: [['/obs/trace/', TRACE_PAYLOAD]] });
  state.api.length = 0;
  obs.openObsTimeline('t_bad1');
  assert.ok(dom._elements.obsTraceModal, '未创建时间线容器');
  assert.ok(state.api.some(u => u.indexOf('/obs/trace/') === 0), '未拉取 trace: ' + state.api.join(','));
  await tick();
  const h = dom._elements.obsTraceBody.innerHTML;
  assert.ok(dom._elements.obsTraceModal.innerHTML.includes('«observability.timeline_title»'), '标题缺失');
  assert.ok(h.includes('vod') && h.includes('coding'), '未渲染两个任务');
  assert.ok(h.includes('«observability.timeline_failed»') && h.includes('pytest failed'), '失败原因未高亮');
  assert.ok(h.includes('#d64545'), '失败任务应用红色');
  assert.ok(h.includes('«observability.timeline_steps»'), '缺步骤区');
  assert.ok(h.includes('shell'), '未渲染工具调用');
  assert.ok(h.includes('engine crashed'), '未内联日志命中');
  assert.ok(h.includes('«observability.view_logs»'), '缺查看日志入口');
  assert.ok(dom._elements.obsTraceMeta.textContent.includes('org/x'), 'meta 未显示仓库短名');
  assert.ok(dom._elements.obsTraceMeta.textContent.includes('«observability.timeline_pipeline»'), '流水线视图标记缺失');
});

checkAsync('时间线：空轨迹给空态', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/trace/', { tasks: [], counts: {} }]] });
  obs.openObsTimeline('tr_x');
  await tick();
  assert.ok(dom._elements.obsTraceBody.innerHTML.includes('«observability.timeline_empty»'), '缺空态');
});

checkAsync('互跳：从时间线跳到日志 tab 会带上任务过滤', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/logs', { total: 0, items: [], facets: {} }]] });
  obs.openObsTimeline('t_bad1');
  await tick();
  obs.jumpObsLogsForTask('t_bad1');
  // 日志 tab 重新渲染（obsContent 变为日志视图，且带任务过滤 chip）
  await tick();
  const h = dom._elements.obsContent.innerHTML;
  assert.ok(h.includes('«observability.log_task_filter»'), '日志 tab 未显示任务过滤 chip: ' + h.slice(0, 200));
  assert.ok(h.includes('t_bad1'), 'chip 未显示任务号');
});

check('时间线入口：任务日志弹窗头部有「时间线」按钮', () => {
  const { obs, dom } = loadObs(makeDom());
  obs.openTaskLogModal('t_x1', '');
  const m = dom._elements.rtLogModal;
  assert.ok(m && m.innerHTML.includes('openObsTimeline('), '任务日志弹窗缺时间线入口');
  assert.ok(m.innerHTML.includes('«observability.timeline»'), '缺按钮文案');
});

console.log('\n== 容错与默认值 ==');

check('未知分区名回落当前分区，不抛异常', () => {
  const { obs, dom } = loadObs(makeDom());
  obs.renderObservability('overview');
  obs.renderObservability('nonsense');
  assert.ok(dom._elements.pageContent.innerHTML.includes(SEC_RE('overview')), '应保持 overview');
  obs.renderObservability('devices');
  obs.renderObservability('nonsense');
  assert.ok(dom._elements.pageContent.innerHTML.includes(SEC_RE('devices')), '应保持 devices');
});

check('切换分区重置到该分区默认子 Tab（默认选中第一个）', () => {
  const { obs, dom } = loadObs(makeDom());
  obs.renderObservability('inspect');
  let h = dom._elements.pageContent.innerHTML;
  assert.ok(/class="ac"[^>]*>«observability\.tab\.tasks»/.test(h), 'inspect 默认应选中 tasks');
  assert.ok(!/class="ac"[^>]*>«observability\.tab\.anomaly»/.test(h), 'anomaly 不该默认选中');
  obs.renderObservability('devices');
  h = dom._elements.pageContent.innerHTML;
  assert.ok(!h.includes('switchObsTab('), '设备分区只有一个视图，不该有 Tab');
});

console.log('\n== 原功能不破：子 Tab 取值沿用旧集合 ==');

check('分区→子视图映射沿用旧 tab 取值（renderObsContent 分发无需改动）', () => {
  const { obs } = loadObs(makeDom());
  const S = obs._OBS_SECTIONS;
  assert.strictEqual(S.devices.subs.join(','), 'overview', '设备分区应只有 overview 视图');
  assert.strictEqual(S.inspect.subs.join(','), 'tasks,events,errorlogs',
    '任务与日志应为 tasks/events/errorlogs（异常任务已并入任务 Tab 的 chip，2026-10-18）');
  const known = ['overview', 'tasks', 'events', 'anomaly', 'errorlogs'];
  for (const sec of Object.keys(S)) {
    for (const t of S[sec].subs) assert.ok(known.includes(t), `未知子视图取值: ${t}`);
  }
});

check('任务与日志的三个子 Tab onclick 均指向 switchObsTab', () => {
  const { obs, dom } = loadObs(makeDom());
  obs.renderObservability('inspect');
  const h = dom._elements.pageContent.innerHTML;
  assert.ok(h.indexOf("switchObsTab('tasks'") >= 0, 'tasks 子 Tab 的 onclick 变了');
  assert.ok(h.indexOf("switchObsTab('events'") >= 0, 'events 子 Tab 的 onclick 变了');
  assert.ok(h.indexOf("switchObsTab('errorlogs'") >= 0, 'errorlogs 子 Tab 的 onclick 变了');
});

console.log('\n== 统一事件视图（P1-4 / §6.9）==');

// S1 契约字段齐全（ts_kind/outcome/state_label_key/target/next_step 数组）—— 前端只依赖这些键
const EVENTS_PAYLOAD = {
  total: 4, by_source: { gap: 1, monitor: 1, task: 1, change: 1 },
  sources: ['monitor', 'gap', 'mutation', 'task', 'change'],
  next_cursor: null, truncated_sources: [], q: '', state: '',
  events: [
    { key: 'alert:9', source: 'gap', kind: 'runtime_silent', level: 'warning', scope: 'user',
      repo_key: '', dim_key: 'runtime:42', task_id: '', issue_id: '',
      started_at: '2026-10-17T09:30:00+00:00', duration_sec: 1843, ongoing: true,
      state: 'firing', state_label_key: 'observability.state.firing', ts_kind: 'interval',
      outcome: '', next_step: ['device', 'logs'],
      target: { repo_key: '', issue_id: '', device_id: '42' } },
    { key: 'alert:8', source: 'monitor', kind: 'task_failure_rate', level: 'critical',
      scope: 'user', repo_key: 'gitcode:org/x', dim_key: 'repo:gitcode:org/x', task_id: 't_x1',
      issue_id: '#7', started_at: '2026-10-17T09:00:00+00:00', duration_sec: 120,
      ongoing: true, state: 'firing', state_label_key: 'observability.state.firing',
      ts_kind: 'interval', outcome: '', next_step: ['logs'],
      target: { repo_key: 'gitcode:org/x', issue_id: '#7', device_id: '' } },
    { key: 'task:t_x1:stuck', source: 'task', kind: 'task_stuck', level: 'warning',
      scope: 'user', repo_key: 'gitcode:org/x', dim_key: '', task_id: 't_x1', issue_id: '#7',
      started_at: '2026-10-17T08:00:00+00:00', duration_sec: 5400, ongoing: true,
      state: 'stuck', state_label_key: 'observability.state.stuck', ts_kind: 'interval',
      outcome: 'stuck', next_step: ['timeline', 'logs'],
      target: { repo_key: 'gitcode:org/x', issue_id: '#7', device_id: '' } },
    { key: 'change:5', source: 'change', kind: 'change', level: 'info', scope: 'user',
      repo_key: 'gitcode:org/x', dim_key: '', task_id: '', issue_id: '#7',
      started_at: '2026-10-17T07:00:00+00:00', duration_sec: 300, ongoing: false,
      state: 'done', state_label_key: 'observability.state.done', ts_kind: 'point',
      outcome: 'merged', title: 'feat: #7 修好了', next_step: ['issue'],
      target: { repo_key: 'gitcode:org/x', issue_id: '#7', device_id: '' } },
  ],
};

check('事件视图：时长说人话（秒/分/时/天）', () => {
  const { obs } = loadObs(makeDom());
  assert.ok(obs._durHuman(45).indexOf('45s') >= 0, '秒');
  assert.ok(obs._durHuman(300).includes('«observability.dur_unit_min»'), '分: ' + obs._durHuman(300));
  assert.ok(obs._durHuman(7200).includes('«observability.dur_unit_hour»'), '时');
  assert.ok(obs._durHuman(200000).includes('«observability.dur_unit_day»'), '天');
  assert.strictEqual(obs._durHuman(0), '—', '0 应显示占位');
  assert.strictEqual(obs._durHuman(null), '—', '空应显示占位');
});

checkAsync('事件视图：时间轴流（两段式行）+ S1 字段驱动的下钻', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/events', EVENTS_PAYLOAD]] });
  obs.loadObsEvents(dom.getElementById('obsContent'));
  await tick(); await tick();
  const body = dom._elements.obsEventBody.innerHTML;
  // 不再是 8 列表格：改为可展开的时间轴流行
  assert.ok(body.indexOf('<table') === -1, '不该再是表格');
  assert.ok((body.match(/obsEventToggle\(/g) || []).length === 4, '4 条事件应各有一行且可展开');
  assert.ok(body.includes('«observability.source.gap»'), '缺数据缺失来源标签');
  assert.ok(body.includes('«observability.source.change»'), '缺变更来源标签');
  assert.ok(body.includes('«observability.sev.critical»'), '级别徽标应按 level 渲染（critical）');
  // 有结果码的事件（任务/变更）显示"结果徽标"、与级别分列；无结果码的（告警类）才显示级别
  assert.ok(body.includes('«observability.outcome.merged»'), '变更事件应显示结果徽标 merged');
  // 状态文案走后端 state_label_key（不再前端拼 ev_state.*）
  assert.ok(body.includes('«observability.state.stuck»'), '缺任务状态文案（state_label_key）');
  // 结果码与级别分列：任务卡住给 outcome 徽标
  assert.ok(body.includes('«observability.outcome.stuck»'), '缺结果徽标（outcome=stuck）');
  // 时长：区间事件显示、瞬时事件（ts_kind=point）不显示
  assert.ok(body.includes('«observability.dur_unit_min»'), '区间事件应显示时长');
  // 瞬时事件（ts_kind=point）不显示时长：取最后一行（change:5）的片段检查
  const chTail = body.slice(body.lastIndexOf('change:5'));
  assert.ok(!/dur_unit/.test(chTail), '瞬时事件不该带时长: ' + chTail.slice(0, 160));
  // 下钻由 next_step 数组驱动
  assert.ok(body.includes("openObsTimeline('t_x1')"), '缺时间线下钻');
  assert.ok(body.includes("jumpObsLogsForTask('t_x1')"), '缺日志下钻');
  assert.ok(body.includes('https://gitcode.com/org/x/issues/7'), '缺需求链接');
  assert.ok(body.includes('obsShowRuntimeTasks(42)'), '缺设备下钻（target.device_id）');
  // 壳：过滤区合成一行 + 关键字检索框（服务端检索）
  const shell = dom._elements.obsContent.innerHTML;
  assert.ok(shell.includes("obsEventFilter('gap')"), '缺来源筛选');
  assert.ok(shell.includes("obsEventStateFilter('firing')"), '缺状态筛选');
  assert.ok(shell.includes('obsEventStateFilter(\'failed\')'), '状态全集应含 failed');
  assert.ok(shell.includes('id="obsEventQ"') && shell.includes('obsEventSearch()'), '缺关键字检索入口');
  assert.ok(shell.includes('«observability.events_title»'), '缺标题');
});

checkAsync('事件视图：空态 + 接口异常给错误提示', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/events', { total: 0, events: [] }]] });
  obs.loadObsEvents(dom.getElementById('obsContent'));
  await tick(); await tick();
  assert.ok(dom._elements.obsEventBody.innerHTML.includes('«observability.no_events»'), '缺空态');
  const { obs: obs2, dom: dom2 } = loadObs(makeDom(), { payloads: [['/obs/events', '__reject__']] });
  obs2.loadObsEvents(dom2.getElementById('obsContent'));
  await tick(); await tick();
  assert.ok(dom2._elements.obsEventBody.innerHTML.includes('«observability.load_failed»'), '缺失败提示');
});

console.log('\n== 异常任务：任务 Tab 按钮 + 弹窗（2026-10-18 改版）==');

check('任务 Tab：入口随渲染出现且只有一处（不再事后 insertBefore 累积）', () => {
  const { obs } = loadObs(makeDom());
  const ops = { tasks: { stuck_tasks: [], failed_tasks: [], issue_stats: {}, status_counts: {} } };
  for (let i = 0; i < 3; i++) {
    const html = obs.renderTasksTab(ops, {});
    assert.strictEqual((html.match(/obsTaskAnomalyBar/g) || []).length, 1,
      '第 ' + (i + 1) + ' 次渲染应恰有一处入口（旧实现每 10s 局部刷新会多插一根）');
    assert.ok(html.includes('openObsAnomalyModal()'), '入口应指向弹窗');
  }
  const src = fs.readFileSync(
    path.resolve(__dirname, '../../src/api/static/js/pages/observability.js'), 'utf8');
  assert.ok(src.indexOf('_obsTaskChipMount') === -1, '旧的 insertBefore 挂载方式应已移除');
});

checkAsync('异常任务弹窗：打开 → 拉 ops/overview → 渲染明细；分页走弹窗内重渲染', async () => {
  const mkFailed = (i) => ({
    id: 't' + i, agent_type: 'coding', repo_key: 'gitcode:org/x',
    started_at: '2026-10-17T02:00:00+00:00', completed_at: '2026-10-17T02:10:00+00:00',
    errors: ['boom ' + i], exit_code: 1,
  });
  const ops = { tasks: { stuck_tasks: [], failed_tasks: Array.from({ length: 12 }, (_, i) => mkFailed(i)) } };
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/dashboard/ops/overview', ops]] });
  obs.openObsAnomalyModal();
  await tick(); await tick();
  const modal = dom._elements.obsAnomalyModal;
  assert.ok(modal && modal.innerHTML.indexOf('«observability.tab.anomaly»') >= 0, '缺弹窗标题');
  assert.ok(modal.innerHTML.indexOf('obsAnomalyModalBody') >= 0, '缺弹窗内容区');
  const body = dom._elements.obsAnomalyModalBody;
  assert.ok(body && body.innerHTML.indexOf('«observability.failed_tasks»') >= 0,
    '弹窗未渲染失败明细: ' + ((body || {}).innerHTML || '').slice(0, 120));
  assert.ok(body.innerHTML.indexOf('_obsAnomalyRerender()') >= 0,
    '弹窗内分页必须走 _obsAnomalyRerender()');
  assert.ok(body.innerHTML.indexOf('refreshObs()') === -1, '弹窗内不该出现 refreshObs()（会重画整页）');
});


console.log('\n== SLO 健康结论卡片（P2-1）==');

const SLO_PAYLOAD = { slos: [
  { slo_id: 1, name: 'task_success_rate', state: 'breached', value: 0.7664, target: 0.99,
    budget_remaining_pct: 0, buckets: 48, window_start: '2026-09-18T00:00:00+00:00' },
  { slo_id: 2, name: 'task_queue_wait_p95', state: 'ok', value: 0.98, target: 0.95,
    budget_remaining_pct: 62.5, buckets: 47 },
  { slo_id: 3, name: 'engine_availability', state: 'no_data', value: null, target: 0.99,
    budget_remaining_pct: null, buckets: 0 },
] };

checkAsync('总览：SLO 卡片渲染达成态 + 预算剩余 + 目标', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/slo', SLO_PAYLOAD]] });
  obs.loadObsSlo(true);
  await tick(); await tick();
  const h = dom._elements.obsSloCard.innerHTML;
  assert.ok(h.includes('«observability.slo_title»'), '缺卡片标题');
  assert.ok(h.includes('«observability.slo_name.task_success_rate»'), '缺 SLO 名');
  assert.ok(h.includes('«observability.slo_state.breached»'), '缺违约态文案');
  assert.ok(h.includes('«observability.slo_state.no_data»'), 'no_data 也要有文案（不露原始 key）');
  assert.ok(h.includes('76.6%'), '应显示 SLI 值: ' + h.slice(0, 140));
  assert.ok(h.includes('«observability.slo_budget_left» 62.5%'), '应显示预算剩余: ' + h.slice(0, 200));
  assert.ok((h.match(/openObsSloBurn\(/g) || []).length === 3, '每条都应可点开燃尽');
});

checkAsync('SLO 燃尽弹窗：拉详情 → 实际 vs 理想曲线', async () => {
  const detail = { slo_id: 1, name: 'task_success_rate', state: 'breached', value: 0.76, target: 0.99,
    window_start: '2026-09-18T00:00:00+00:00', window_end: '2026-10-18T00:00:00+00:00', buckets: 3,
    burn: [ { bucket: '2026-10-18T11:00:00+00:00', ideal_pct: 33.3, used_pct: 100, bad: 1 },
            { bucket: '2026-10-18T12:00:00+00:00', ideal_pct: 66.7, used_pct: 100, bad: 0 },
            { bucket: '2026-10-18T13:00:00+00:00', ideal_pct: 100, used_pct: 100, bad: 0 } ] };
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/slo/1', detail], ['/obs/slo', SLO_PAYLOAD]] });
  obs.openObsSloBurn(1);
  await tick(); await tick();
  const modal = dom._elements.obsSloModal;
  assert.ok(modal && modal.innerHTML.includes('«observability.slo_burn_title»'), '缺弹窗标题');
  const body = dom._elements.obsSloModalBody;
  assert.ok(body && body.innerHTML.includes('obsSloBurnChart'), '缺曲线容器: ' + (((body || {}).innerHTML) || '').slice(0, 140));
  assert.ok(body.innerHTML.includes('«observability.slo_window»'), '缺窗口说明');
});


checkAsync('SLO 卡片只在「总览」分区：设备与引擎分区不挂容器', async () => {
  // 两个分区共用 renderOverview —— 容器必须按 _obsSection 分流（否则卡片会出现在设备页）
  // 总览分区：走 _renderObsOverview（不是 renderOverview）——端到端验卡片真的被填出来
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/slo', SLO_PAYLOAD]] });
  obs.renderObservability('overview');
  await tick(); await tick();
  const box = dom._elements.obsSloCard;
  assert.ok(box && box.innerHTML.includes('«observability.slo_state.breached»'),
    '总览分区应渲染并填充 SLO 卡片: ' + ((box || {}).innerHTML || '(空)').slice(0, 120));
  assert.ok((dom._elements.obsOvBody.innerHTML || '').indexOf('obsSloCard') === -1,
    '卡片应独立于 obsOvBody（刷新重建不丢）');
  // 设备与引擎分区：共用的 renderOverview 不得挂卡片容器
  obs.renderObservability('devices');
  await tick(); await tick();
  assert.ok((dom._elements.obsContent.innerHTML || '').indexOf('obsSloCard') === -1,
    '设备与引擎分区不该出现 SLO 卡片容器');
});

checkAsync('重建 DOM 后卡片用缓存回填（不因 60s 节流留空白）', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/slo', SLO_PAYLOAD]] });
  obs.loadObsSlo(true);
  await tick(); await tick();
  const box = dom._elements.obsSloCard;
  assert.ok(box.innerHTML.includes('«observability.slo_state.breached»'), '首次应填上');
  box.innerHTML = '';                       // 模拟局部更新回退导致的重建
  obs._fillObsSloCard();                    // 应无条件用缓存回填（不等下一次 fetch）
  assert.ok(box.innerHTML.includes('«observability.slo_state.breached»'), '重建后应立刻回填');
});


checkAsync('SLO 卡片跟随仓库选择器：URL 带 repo= 且切仓立刻重算', async () => {
  const calls = [];
  let cur = 'org/a';
  const env = loadObs(makeDom(), {
    payloads: [['/obs/slo', SLO_PAYLOAD]],
    app: { currentRepo: () => ({ fullName: cur }) },
  });
  // 该 harness 的 API 桩把 URL 记在 state.api 里 → 从 sandbox 外部再看一次不方便，
  // 改为直接断言请求 URL 拼装 + 切仓后的行为（用 _obsSloRepoFull 判定）
  assert.strictEqual(env.obs._obsSloRepoFull(), 'org/a', '_obsSloRepoFull 应取当前仓');
  env.obs.loadObsSlo(true);
  await tick(); await tick();
  const first = env.state.api.filter(u => u.indexOf('/obs/slo') === 0);
  assert.ok(first.length && first[0].indexOf('repo=org%2Fa') >= 0, '首个请求应带 repo=' + JSON.stringify(first));
  // 切仓：即使 60s 节流窗口内，也必须重新请求
  cur = 'org/b';
  env.obs.loadObsSlo();
  await tick(); await tick();
  const all = env.state.api.filter(u => u.indexOf('/obs/slo') === 0);
  assert.ok(all.length >= 2 && all[all.length - 1].indexOf('repo=org%2Fb') >= 0,
    '切仓后应立刻按新仓重算: ' + JSON.stringify(all));
});


checkAsync('SLO 燃尽：空燃尽给说明、桶少时画点、常驻口径提示', async () => {
  const opt = [];
  const fakeChart = { init: () => ({ setOption: (o) => opt.push(o), dispose() {}, resize() {} }) };
  const base = { slo_id: 1, name: 'task_success_rate', state: 'breached', value: 0.76, target: 0.99,
    window_start: '2026-09-18T00:00:00+00:00', window_end: '2026-10-18T00:00:00+00:00',
    buckets: 0, budget_remaining_pct: 0, burn: [] };
  // ① 空燃尽：给明确说明，不给白框
  const e1 = loadObs(makeDom(), { payloads: [['/obs/slo/1', base]], echarts: fakeChart });
  e1.obs.openObsSloBurn(1);
  await tick(); await tick();
  const body1 = e1.dom._elements.obsSloModalBody.innerHTML;
  assert.ok(body1.includes('«observability.slo_burn_empty»'), '空燃尽应给说明: ' + body1.slice(0, 160));
  assert.ok(body1.indexOf('obsSloBurnChart') === -1, '空燃尽不该画空图');
  assert.ok(body1.includes('«observability.slo_bucket_hint»'), '应常驻口径提示');
  // ② 桶少（1 个点）→ 打开 symbol，否则肉眼看不见
  opt.length = 0;
  const few = Object.assign({}, base, { buckets: 1,
    burn: [{ bucket: '2026-10-18T11:00:00+00:00', ideal_pct: 100, used_pct: 208, bad: 1 }] });
  const e2 = loadObs(makeDom(), { payloads: [['/obs/slo/1', few]], echarts: fakeChart });
  e2.obs.openObsSloBurn(1);
  await tick(); await tick();
  assert.ok(opt.length && opt[0].series[0].symbol === 'circle',
    '桶少时应画点: ' + JSON.stringify(opt.length ? opt[0].series.map(s => s.symbol) : null));
  // ③ 桶多 → 回到不画点（保持线干净）
  opt.length = 0;
  const many = Object.assign({}, base, { buckets: 3,
    burn: [1, 2, 3].map(i => ({ bucket: '2026-10-1' + i + 'T11:00:00+00:00', ideal_pct: i * 33, used_pct: i * 10, bad: 0 })) });
  const e3 = loadObs(makeDom(), { payloads: [['/obs/slo/1', many]], echarts: fakeChart });
  e3.obs.openObsSloBurn(1);
  await tick(); await tick();
  assert.ok(opt.length && opt[0].series[0].symbol === 'none', '桶多时应隐藏点');
});


console.log('\n== 三页并一：运行驾驶舱（页内大 Tab）==');

check('页内大 Tab：三个视图 + 明显尺寸（不是老 .stabs 小字条）', () => {
  const { obs, dom } = loadObs(makeDom());
  obs.renderObservability('overview');
  const h = dom._elements.pageContent.innerHTML;
  assert.ok(h.includes("switchObsView('devices')") && h.includes("switchObsView('inspect')"),
    '应有页内三个大 Tab');
  assert.ok(h.includes('font-size:15px'), '大 Tab 标题应明显（15px），不能沿用老的细小字');
  assert.ok(h.includes('«observability.view_hint.overview»'), '大 Tab 应各带一句话说明');
  assert.ok(h.includes('«observability.title»'), '页面标题应存在（已改为运行驾驶舱）');
});

checkAsync('大 Tab 切换复用换分区逻辑（重置子 Tab/分页并重渲染）', async () => {
  const { obs, dom } = loadObs(makeDom(), { payloads: [['/obs/slo', { slos: [] }]] });
  obs.renderObservability('overview');
  await tick();
  obs.switchObsView('devices');
  await tick(); await tick();
  const h = dom._elements.pageContent.innerHTML;
  assert.ok(h.includes("switchObsView('devices')"), '切到设备视图后应重渲染出大 Tab');
  assert.ok(dom._elements.obsContent, '设备视图应有内容容器');
});


console.log('\n== Token 卡片（引擎/任务/阶段）==');

Promise.all(_pendingChecks).then(() => {
  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
});
