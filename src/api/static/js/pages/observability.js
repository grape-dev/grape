// ====== 可观测性（一级分类，与「设备中心」平级）======
// 侧栏 3 个分区：总览 / 设备与引擎 / 任务与日志（见 observability-redesign-design.md §5.1）
// 分区由侧栏选中并写入 URL（#observability/<section>），页面内不再重复分区 Tab；
// 每个分区内部保留各自的子 Tab。原「设备监控」的内容归入「设备与引擎」「任务与日志」。
// 数据来源：/api/dashboard/ops/overview + /health/realtime + /metrics

// 分区 → 子 Tab 映射（总览暂无子 Tab，slice 2 填 KPI/图表）
// 异常任务是任务列表的一个视图（运行超时 + 失败），归「任务与日志」而不是设备分区
// 可观测性合并为一页（运行驾驶舱）后的**页内三个视图**：顺序即大 Tab 的顺序。
// 注意：`_OBS_SECTIONS` 仍是"视图注册表"（每个视图自己的子 Tab 定义），
// 侧栏不再按它分区，改由 `_OBS_VIEWS` 渲染页内大 Tab。
const _OBS_VIEWS = ['overview', 'devices', 'inspect'];

const _OBS_SECTIONS = {
  overview: { subs: [] },
  devices:  { subs: ['overview'] },
  inspect:  { subs: ['tasks', 'events', 'errorlogs'] },   // 异常任务并入任务 Tab 的过滤 chip（2026-10-18 拍板）
};

let _obsSection = 'overview';
let _obsTab = 'overview';
let _obsTimer = null;
let _obsData = { ops: null, realtime: null, metrics: null };
let _obsRendered = false;      // 概览/任务 Tab 是否已用 innerHTML 渲染过骨架
let _obsRenderedTab = null;    // 已渲染骨架的 tab（切换 tab 时需重新建骨架）
let _rtSearch = '';            // Runtime 引擎搜索筛选
let _rtDeviceFilter = '';      // Runtime 设备下拉筛选
let _workerPage = 0;           // Worker 卡片当前页（0 基）
let _runtimePage = 0;          // Runtime 引擎当前页（0 基）
let _workerPageSize = 10;      // Worker 每页条数
let _runtimePageSize = 10;     // Runtime 每页条数
let _timelinePage = 0;         // 任务列表当前页（0 基）
let _timelinePageSize = 10;    // 任务列表每页条数

// 页内大 Tab（2026-10-18：三页并一，明显/大一点，不用老的 .stabs 小字条）
function _obsBigTabsHtml() {
  const ic = { overview: '📊', devices: '💻', inspect: '▶' };
  return `<div style="display:flex;gap:10px;padding:14px 20px;border-bottom:1px solid var(--b)">` +
    _OBS_VIEWS.map(v => {
      const on = (_obsSection === v);
      return `<a onclick="switchObsView('${v}')" style="flex:1;cursor:pointer;text-align:center;padding:12px 10px;border-radius:10px;`
        + `border:1px solid ${on ? 'var(--a)' : 'var(--b)'};background:${on ? 'var(--a)' : 'transparent'};`
        + `color:${on ? '#fff' : 'inherit'};transition:opacity .15s" `
        + `onmouseover="this.style.opacity=0.88" onmouseout="this.style.opacity=1">`
        + `<div style="font-size:20px;line-height:1.1">${ic[v] || '•'}</div>`
        + `<div style="font-size:15px;font-weight:600;margin-top:5px">${I18N.t('observability.section.' + v)}</div>`
        + `<div style="font-size:11px;opacity:.75;margin-top:3px">${I18N.t('observability.view_hint.' + v)}</div>`
        + `</a>`;
    }).join('') + `</div>`;
}

function switchObsView(v) {
  // 复用 renderObservability 的"换分区"逻辑（重置子 Tab/分页/骨架），保持深链与轮询行为不变
  if (v === _obsSection) return;
  renderObservability(v);
}

function renderObservability(section) {
  const pc = document.getElementById('pageContent');
  if (!pc) return;
  const _sec = (section && _OBS_SECTIONS[section]) ? section : _obsSection;
  if (_sec !== _obsSection) {
    // 换分区：重置到该分区默认子 Tab 与分页，强制重建骨架
    _obsSection = _sec;
    _obsTab = (_OBS_SECTIONS[_sec].subs[0] || '');
    _obsRendered = false;
    _obsRenderedTab = null;
    _workerPage = 0; _runtimePage = 0; _timelinePage = 0;
  }
  const _cfg = _OBS_SECTIONS[_obsSection];
  // 只有一个子视图的分区（设备与引擎）不再画单条 Tab —— 分区本身就是该视图
  const _showTabs = _cfg.subs.length > 1;
  const _tabsHtml = _showTabs ? _cfg.subs.map(t =>
    `<a class="${_obsTab === t ? 'ac' : ''}" onclick="switchObsTab('${t}',event)">${I18N.t('observability.tab.' + t)}</a>`
  ).join('') : '';
  pc.innerHTML = `
    <div class="cd" style="padding:0;overflow:hidden">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--b)">
        <h2 style="font-size:14px;font-weight:600;display:flex;align-items:center;gap:7px">${I18N.t('observability.title')}
          <span class="ts t3" style="font-weight:400">/ ${I18N.t('observability.section.' + _obsSection)}</span>
        </h2>
        ${_obsSection !== 'overview' ? `<div style="display:flex;align-items:center;gap:10px">
          <span class="ts t3" id="obsLastUpdate">-</span>
          <button class="bt bt-o bt-s" onclick="refreshObs()">${I18N.t('observability.refresh')}</button>
          <label class="ts t3" style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="obsAuto" onchange="toggleObsAuto()" checked style="margin:0">${I18N.t('observability.auto_refresh')}</label>
        </div>` : `<div style="display:flex;align-items:center;gap:10px">
          <span class="ts t3" id="obsLastUpdate">-</span>
          <span class="stabs" id="obsOvBar" style="border:none;padding:0">${['1h', '6h', '24h', '7d'].map(r =>
            `<a class="${_obsOvRange === r ? 'ac' : ''}" onclick="switchObsRange('${r}',event)">${r}</a>`).join('')}</span>
          <button class="bt bt-o bt-s" onclick="_obsOvSkeleton();loadObsOverview()">${I18N.t('observability.refresh')}</button>
        </div>`}
      </div>
      ${_obsBigTabsHtml()}
      ${_showTabs ? `<div class="stabs" style="padding:0 20px;border-bottom:1px solid var(--b)">${_tabsHtml}</div>` : ''}
      <div id="obsContent" style="padding:20px"><div class="loading">${I18N.t('observability.loading')}</div></div>
      ${_obsSection === 'devices' ? `<div style="padding:0 20px 20px">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;border-top:1px solid var(--b);padding-top:14px">
          <div class="ts t3">${I18N.t('observability.res.title')}</div>
          <div style="display:flex;align-items:center;gap:10px">
            <select id="obsResRt" class="ip ip-s" style="max-width:220px" onchange="obsResPick(this.value)"></select>
            <span class="stabs" id="obsResBar" style="border:none;padding:0">${['1h', '6h', '24h', '7d'].map(r =>
              `<a class="${_obsResRange === r ? 'ac' : ''}" onclick="switchObsResRange('${r}',event)">${r}</a>`).join('')}</span>
          </div>
        </div>
        <div id="obsResChart" style="height:200px;margin-top:6px"></div>
      </div>` : ''}
    </div>
  `;
  if (_obsSection === 'overview') {
    // 总览：KPI + 图表，独立取数（不参与设备分区的 10s 轮询）
    stopObsAuto();
    _obsOvSkeleton();
    loadObsOverview();
    return;
  }
  refreshObs();
  startObsAuto();
  if (_obsSection === 'devices') {
    // 资源曲线独立取数（60s 窗口的数据，不跟着 10s 刷新重绘，避免图表闪烁）
    loadObsRuntimeHistory();
  }
}

// ===== 设备分区：资源趋势（采样表 → 折线图）=====
let _obsResRange = '6h';
let _obsResRt = '';        // '' = 全部（平均）
let _obsResData = null;

function switchObsResRange(r, ev) {
  _obsResRange = r;
  document.querySelectorAll('#obsResBar a').forEach(a => a.classList.remove('ac'));
  if (ev && ev.target) ev.target.classList.add('ac');
  loadObsRuntimeHistory();
}

function obsResPick(rtId) {
  _obsResRt = String(rtId || '');
  _renderObsResChart();
}

function loadObsRuntimeHistory() {
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const qp = ['range=' + encodeURIComponent(_obsResRange)];
  if (repo && repo.fullName) qp.push('repo=' + encodeURIComponent(repo.fullName));
  API.get('/obs/runtimes/history?' + qp.join('&')).then(d => {
    if (window._currentView !== 'observability' || _obsSection !== 'devices') return;
    _obsResData = d || null;
    _fillObsResSelect();
    _renderObsResChart();
  }).catch(e => {
    const el = document.getElementById('obsResChart');
    if (el) el.innerHTML = '<div class="ts t3" style="padding:14px 0">' + I18N.t('observability.load_failed', { msg: (e && e.message) || e }) + '</div>';
  });
}

function _fillObsResSelect() {
  const sel = document.getElementById('obsResRt');
  if (!sel) return;
  const d = _obsResData || {};
  const rts = d.runtimes || [];
  const opts = ['<option value="">' + I18N.t('observability.res.all') + '</option>']
    .concat(rts.map(r => `<option value="${r.id}">${(r.device_name || r.name || ('#' + r.id))}</option>`));
  sel.innerHTML = opts.join('');
  sel.value = rts.some(r => String(r.id) === String(_obsResRt)) ? _obsResRt : '';
  if (!rts.some(r => String(r.id) === String(_obsResRt))) _obsResRt = '';
}

function _obsResSeries(key) {
  // 全部 → 各引擎求平均（忽略空桶）；指定引擎 → 取该引擎
  const d = _obsResData || {};
  const series = d.series || {};
  const ids = Object.keys(series);
  if (!ids.length) return [];
  if (_obsResRt) return (series[_obsResRt] || {})[key] || [];
  const n = ((series[ids[0]] || {})[key] || []).length;
  const out = new Array(n).fill(null);
  for (let i = 0; i < n; i++) {
    let sum = 0, cnt = 0;
    for (const id of ids) {
      const v = ((series[id] || {})[key] || [])[i];
      if (v !== null && v !== undefined) { sum += Number(v); cnt++; }
    }
    out[i] = cnt ? Math.round(sum / cnt * 10) / 10 : null;
  }
  return out;
}

function _renderObsResChart() {
  const d = _obsResData || {};
  const labels = (d.buckets || []).map(iso => {
    const dt = new Date(iso);
    if (isNaN(dt.getTime())) return '';
    const hm = String(dt.getHours()).padStart(2, '0') + ':' + String(dt.getMinutes()).padStart(2, '0');
    return _obsResRange === '7d' ? (dt.getMonth() + 1) + '/' + dt.getDate() + ' ' + hm : hm;
  });
  const has = _obsResSeries('cpu').some(v => v !== null && v !== undefined);
  if (!has) {
    const el = document.getElementById('obsResChart');
    if (el) el.innerHTML = '<div class="ts t3" style="padding:20px 0;text-align:center">' + I18N.t('observability.res.empty') + '</div>';
    return;
  }
  const mk = (name, key, color) => ({
    name: name, type: 'line', smooth: true, showSymbol: false, connectNulls: true,
    itemStyle: { color: color }, lineStyle: { width: 2 }, data: _obsResSeries(key),
  });
  _obsChart('obsResChart', {
    tooltip: { trigger: 'axis' },
    legend: { top: 0, right: 0, itemWidth: 10, itemHeight: 8, textStyle: { fontSize: 11 } },
    grid: { left: 40, right: 16, top: 26, bottom: 22 },
    xAxis: { type: 'category', boundaryGap: false, data: labels, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', min: 0, max: 100, axisLabel: { fontSize: 10, formatter: '{value}%' } },
    series: [
      mk(I18N.t('observability.res.cpu'), 'cpu', '#4a7fd1'),
      mk(I18N.t('observability.res.mem'), 'mem', '#22a06b'),
      mk(I18N.t('observability.res.disk'), 'disk', '#8b8b8b'),
    ],
  });
}

// ===== 总览分区：真实数据（/api/obs/overview）=====
// 图表用 vendored ECharts（static/vendor/echarts.min.js，不走 CDN）；
// 库缺失时降级为只显示 KPI 数值，不留白屏。
let _obsOvRange = '24h';
let _obsOvCharts = {};

function _disposeObsCharts() {
  Object.keys(_obsOvCharts).forEach(k => {
    try { _obsOvCharts[k] && _obsOvCharts[k].dispose(); } catch (e) { /* ignore */ }
  });
  _obsOvCharts = {};
}

function _obsOvSkeleton() {
  const box = document.getElementById('obsContent');
  if (!box) return;
  _disposeObsCharts();
  if (!_obsOvBound) {
    _obsOvBound = true;
    window.addEventListener('resize', () => {
      Object.keys(_obsOvCharts).forEach(k => {
        try { _obsOvCharts[k] && _obsOvCharts[k].resize(); } catch (e) { /* ignore */ }
      });
    });
  }
  // SLO 健康结论卡片容器（P2-1）：**总览分区用 _renderObsOverview，不是 renderOverview**
  // （后者是「设备与引擎」用的）——所以容器必须挂在这里，而不是那里面（实测：挂错了位置 → 总览页空）
  box.innerHTML = `<div id="obsSloCard"></div>`
    + `<div id="obsOvBody"><div class="loading">${I18N.t('observability.loading')}</div></div>`;
}
let _obsOvBound = false;

function switchObsRange(r, ev) {
  _obsOvRange = r;
  document.querySelectorAll('#obsOvBar a').forEach(a => a.classList.remove('ac'));
  if (ev && ev.target) ev.target.classList.add('ac');
  _obsOvSkeleton();
  loadObsOverview();
}

function loadObsOverview() {
  const body = document.getElementById('obsOvBody');
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const qp = [];
  if (repo && repo.fullName) qp.push('repo=' + encodeURIComponent(repo.fullName));
  qp.push('range=' + encodeURIComponent(_obsOvRange));
  const qs = qp.join('&');
  // 总览数据与告警横幅并行取（横幅属"活数据"，与 KPI 同批刷新；趋势图不重绘）
  Promise.all([
    API.get('/obs/overview?' + qs).catch(e => { console.warn('obs/overview failed:', e); return null; }),
    API.get('/obs/alerts?' + qs).catch(e => { console.warn('obs/alerts failed:', e); return null; }),
  ]).then(([d, a]) => {
    if (window._currentView !== 'observability' || _obsSection !== 'overview') return;  // 已切走
    _obsAlerts = a || null;
    _renderObsOverview(d || {}, a || null);
    _fillObsSloCard();     // 用缓存回填（shell 重建后立刻有内容）
    loadObsSlo();          // 缓存过期（60s）才真的打接口
    const lu = document.getElementById('obsLastUpdate');
    if (lu) lu.textContent = new Date().toLocaleTimeString('zh-CN');
  }).catch(e => {
    if (body) body.innerHTML = '<div class="error">' + I18N.t('observability.load_failed', { msg: (e && e.message) || e }) + '</div>';
  });
}

// ===== 告警横幅 + 告警面板（告警不进侧栏：横幅进总览，面板从横幅进）=====
let _obsAlerts = null;
let _obsAlertPanelView = 'events';

function _shortRepo(repoKey) {
  const s = String(repoKey || '');
  const i = s.indexOf(':');
  return i >= 0 ? s.slice(i + 1) : s;
}

function _obsMetricVal(ev) {
  const m = String((ev && ev.metric) || '');
  const v = (ev && ev.value);
  if (v === null || v === undefined) return '—';
  // 数据缺失（P1-3）：value 是"多少秒没有数据"，直接说人话
  if (m.indexOf('gap_') === 0) {
    const s = Number(v);
    if (!isFinite(s)) return '—';
    if (s < 3600) return Math.round(s / 60) + I18N.t('observability.dur_min');
    if (s < 86400) return (Math.round(s / 360) / 10) + I18N.t('observability.dur_hour');
    return (Math.round(s / 8640) / 10) + I18N.t('observability.dur_day');
  }
  if (m.indexOf('_mutation') >= 0) {
    const n2 = Math.round(Number(v) * 1000) / 10;
    return (n2 > 0 ? '+' : '') + n2 + 'pp';
  }
  if (m.indexOf('rate') >= 0 || m.indexOf('usage') >= 0) return Math.round(Number(v) * 1000) / 10 + '%';
  return Number(v).toLocaleString();
}

// 事件来源标签（§6.9 统一事件视图：monitor / gap / mutation）
function _obsSourceLabel(ev) {
  const s = String((ev && ev.source) || 'monitor');
  return I18N.t('observability.source.' + s, {}) || s;
}

// 无仓库维度的事件（设备静默）用 dim_key 里的设备号做"对象"列
function _obsEventTarget(ev) {
  const dim = String((ev && ev.dim_key) || '');
  if (dim.indexOf('runtime:') === 0) return I18N.t('observability.device_short') + dim.slice(8);
  return '';
}

function _obsBannerHtml(a) {
  const s = (a && a.summary) || {};
  const n = s.firing || 0;
  if (!n) {
    return `<div class="cd" style="padding:10px 14px;margin-bottom:12px;display:flex;align-items:center;gap:8px;border-left:3px solid #22a06b">
      <span style="color:#22a06b;font-weight:600">✔</span>
      <span class="ts t2">${I18N.t('observability.banner.ok')}</span></div>`;
  }
  const crit = s.critical || 0;
  const color = crit ? '#d64545' : '#e0a02a';
  const lt = s.latest || {};
  const detail = lt.rule_name
    ? `${I18N.t('observability.rule.' + lt.rule_name, {})} · ${_shortRepo(lt.repo_key)} · ${_obsMetricVal(lt)}`
    : '';
  return `<div class="cd" style="padding:10px 14px;margin-bottom:12px;display:flex;align-items:center;gap:10px;border-left:3px solid ${color}">
    <span style="color:${color};font-weight:600;white-space:nowrap">⚠ ${I18N.t('observability.banner.firing', { n: n })}</span>
    <span class="ts t2" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${detail}</span>
    <button class="bt bt-o bt-s" style="margin-left:auto;white-space:nowrap;padding:4px 12px" onclick="openObsAlertPanel()">${I18N.t('observability.banner.view')}</button>
  </div>`;
}

function openObsAlertPanel(view) {
  const old = document.getElementById('obsAlertModal');
  if (old) old.remove();
  _obsAlertPanelView = view || 'events';
  const modal = document.createElement('div');
  modal.id = 'obsAlertModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:1001';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = '<div class="cd" style="width:820px;max-width:94vw;max-height:82vh;display:flex;flex-direction:column;padding:0;overflow:hidden">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">' +
    '<h3 style="font-size:14px;font-weight:600">' + I18N.t('observability.alert_panel') + '</h3>' +
    '<div style="display:flex;align-items:center;gap:10px">' +
    '<span id="obsAlertNotifyResult" class="ts t3"></span>' +
    '<button class="bt bt-o bt-s" style="white-space:nowrap;padding:4px 12px" onclick="obsAlertTestNotify()">' + I18N.t('observability.test_notify') + '</button>' +
    '<button class="bt bt-o bt-s" style="white-space:nowrap;padding:4px 12px" onclick="document.getElementById(\'obsAlertModal\').remove()">' + I18N.t('observability.close') + '</button>' +
    '</div>' +
    '</div>' +
    '<div class="stabs" style="padding:0 18px;border-bottom:1px solid var(--b)">' +
      ['events', 'rules', 'silences'].map(v => '<a class="' + (_obsAlertPanelView === v ? 'ac' : '') + '" onclick="switchObsAlertPanel(\'' + v + '\',event)">' + I18N.t('observability.alert_tab.' + v) + '</a>').join('') +
    '</div>' +
    '<div id="obsAlertPanelBody" style="overflow-y:auto;flex:1;padding:14px 18px"></div>' +
    '</div>';
  document.body.appendChild(modal);
  _renderObsAlertPanel();
  refreshObsAlerts();
}

function switchObsAlertPanel(view, ev) {
  _obsAlertPanelView = view;
  document.querySelectorAll('#obsAlertModal .stabs a').forEach(a => a.classList.remove('ac'));
  if (ev && ev.target) ev.target.classList.add('ac');
  _renderObsAlertPanel();
}

function refreshObsAlerts() {
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const qp = [];
  if (repo && repo.fullName) qp.push('repo=' + encodeURIComponent(repo.fullName));
  API.get('/obs/alerts' + (qp.length ? '?' + qp.join('&') : '')).then(a => {
    _obsAlerts = a || null;
    _renderObsAlertPanel();
    return null;
  }).catch(() => null);
}

function _obsAlertTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? String(iso) : d.toLocaleString('zh-CN', { hour12: false });
}

// 级别单元格：只读展示级别（P1-1：策略定义在管理台，用户面只看级别）
// 规则行额外标注"聚合/升级"标记，让用户理解为什么会收到合并/升级通知
function _obsSeverityCell(r) {
  const sev = String((r && r.severity) || 'warning');
  const label = I18N.t('observability.sev.' + sev, {}) || sev;
  const tags = [];
  if (r && r.aggregate_mode && r.aggregate_mode !== 'none') tags.push(I18N.t('observability.tag_aggregate'));
  if (r && r.escalate_json) tags.push(I18N.t('observability.tag_escalate'));
  if (r && r.escalated_at) tags.push(I18N.t('observability.tag_escalated'));
  if (r && Number(r.notify_deferred) === 1) tags.push(I18N.t('observability.tag_deferred'));
  const mark = tags.filter(Boolean).map(t => '<span class="ts t3">·' + t + '</span>').join(' ');
  return label + (mark ? ' ' + mark : '');
}

// ===== 统一事件视图（P1-4 / §6.9）：多来源事件流 + 下钻 =====
let _obsEventsData = null;
let _obsEventSource = '';
let _obsEventState = '';
let _obsEventQ = '';            // 关键字（服务端检索，S1）
let _obsEventCursor = null;     // 游标分页（S1 next_cursor）
let _obsEventMore = false;
let _obsEventNew = [];          // 自动刷新攒下的"新事件"（等用户点开才前插，不打断阅读）
let _obsEventLastSoft = 0;      // 上次前插检查时间（30s 一次，页面 10s 轮询不重复打）
// 异常任务：2026-10-18 起改为「任务 Tab 里的按钮 + 弹窗」（不再内联替换 Tab 内容）

function _durHuman(sec) {
  const s = Number(sec);
  if (!isFinite(s) || s <= 0) return '—';
  if (s < 60) return s + 's';
  if (s < 3600) return Math.round(s / 60) + I18N.t('observability.dur_unit_min');
  if (s < 86400) return (Math.round(s / 360) / 10) + I18N.t('observability.dur_unit_hour');
  return (Math.round(s / 8640) / 10) + I18N.t('observability.dur_unit_day');
}

function _obsIssueUrl(e) {
  const num = String(e.issue_id || '').replace(/^#/, '');
  const path = String(e.repo_key || '').split(':').pop();
  return (path && num) ? 'https://gitcode.com/' + path + '/issues/' + num : '';
}

// 注意：真实 I18N.t 在**缺键时返回 key 本身**（不是 undefined），所以 `t(k) || 兜底` 永远
// 不会走兜底分支——曾经因此把 `observability.rule.failed` 这种原始 key 直接显示给用户。
// 这里统一用「值 === key 视为未命中」的判定，缺键时回退到后端给的原值。
function _obsTOr(key, fallback) {
  const v = I18N.t(key, {});
  return (typeof v === 'string' && v !== key) ? v : fallback;
}

function _obsEventTitle(e) {
  if (e.source === 'change') {
    return (e.title || '') || I18N.t('observability.kind.change');
  }
  if (e.source === 'audit') return e.title || e.kind;
  if (e.source === 'task') {
    return _obsTOr('observability.task_state.' + e.kind, e.kind || '');
  }
  return _obsTOr('observability.rule.' + e.kind, e.kind || '');
}

// S1 起后端给结构化 target {repo_key,issue_id,device_id} → 前端不再自己拆 dim_key 字符串
function _obsEventDeviceId(e) {
  const tg = e.target || {};
  if (tg.device_id) return String(tg.device_id);
  return (String(e.dim_key || '').indexOf('runtime:') === 0) ? String(e.dim_key).slice(8) : '';
}

function _obsEventTargetLabel(e) {
  const tg = e.target || {};
  const dev = _obsEventDeviceId(e);
  if (dev) return I18N.t('observability.device_short') + dev;
  const r = _shortRepo(tg.repo_key || e.repo_key) || '';
  const num = String(tg.issue_id || e.issue_id || '').replace(/^#/, '');
  return (r + (num ? ' #' + num : '')) || '—';
}

// 状态/结果徽标：状态文案由后端 `state_label_key` 给（前端不猜词表），
// 结果码（任务/变更）与告警级别**分列**显示（S1 契约）
function _obsEventStateLabel(e) {
  const key = e.state_label_key || ('observability.state.' + e.state);
  return _obsTOr(key, _obsTOr('observability.ev_state.' + e.state, e.state || '—'));
}

function _obsEventBadge(e) {
  if (e.outcome) {
    const label = _obsTOr('observability.outcome.' + e.outcome, e.outcome);
    const bad = (e.outcome === 'failed' || e.outcome === 'stuck');
    const bg = bad ? 'rgba(220,38,38,.12)' : 'rgba(37,99,235,.12)';
    const fg = bad ? '#dc2626' : '#2563eb';
    return `<span style="padding:1px 6px;border-radius:4px;font-size:11px;background:${bg};color:${fg}">${label}</span>`;
  }
  return '<span class="ts t2">' + _obsSeverityCell({ severity: e.level }) + '</span>';
}

// 下钻动作由后端 `next_step` **数组**驱动（S1：只给键上真有的目标；前端只管渲染）
function _obsEventActions(e) {
  const steps = Array.isArray(e.next_step) ? e.next_step : (e.next_step ? [e.next_step] : []);
  const tid = e.task_id || '';
  const dev = _obsEventDeviceId(e);
  const url = _obsIssueUrl(e);
  const btns = [];
  steps.forEach(s => {
    if (s === 'timeline' && tid) {
      btns.push(`<a style="cursor:pointer;color:var(--a)" onclick="openObsTimeline('${tid}')">${I18N.t('observability.timeline')}</a>`);
    } else if (s === 'logs' && tid) {
      btns.push(`<a style="cursor:pointer;color:var(--a)" onclick="jumpObsLogsForTask('${tid}')">${I18N.t('observability.logs_short')}</a>`);
    } else if (s === 'issue' && url) {
      btns.push(`<a style="cursor:pointer;color:var(--a)" href="${url}" target="_blank" rel="noopener">${I18N.t('observability.issue_short')}</a>`);
    } else if (s === 'device' && dev) {
      btns.push(`<a style="cursor:pointer;color:var(--a)" onclick="obsShowRuntimeTasks(${dev})">${I18N.t('observability.device_short')}${dev}</a>`);
    }
  });
  if (!btns.length && url) {
    btns.push(`<a style="cursor:pointer;color:var(--a)" href="${url}" target="_blank" rel="noopener">${I18N.t('observability.issue_short')}</a>`);
  }
  return btns.join(' · ') || '—';
}

function obsShowRuntimeTasks(rtId) {
  const name = I18N.t('observability.device_short') + rtId;
  openRuntimeTaskModal(rtId, name);
}

function obsEventFilter(source) {
  _obsEventSource = (_obsEventSource === source) ? '' : source;
  obsEventReload();
}

function obsEventStateFilter(state) {
  _obsEventState = (_obsEventState === state) ? '' : state;
  obsEventReload();
}

// 关键字：**服务端检索**（S1 的 q 参数；不是前端过滤已加载页）——回车或点「检索」触发
function obsEventSearch() {
  const el = document.getElementById('obsEventQ');
  _obsEventQ = el ? String(el.value || '').trim() : '';
  obsEventReload();
}

function obsEventReload() {
  _obsEventCursor = null;
  _obsEventMore = false;
  _obsEventNew = [];
  loadObsEvents(document.getElementById('obsContent'), false);
}

function _obsEventQStr() {
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  return (repo && repo.fullName ? '&repo=' + encodeURIComponent(repo.fullName) : '')
    + (_obsEventSource ? '&sources=' + encodeURIComponent(_obsEventSource) : '')
    + (_obsEventState ? '&state=' + encodeURIComponent(_obsEventState) : '')
    + (_obsEventQ ? '&q=' + encodeURIComponent(_obsEventQ) : '');
}

// 过滤区合并成一行（来源 chips | 状态 chips | 关键字 | 计数）：少一层视觉层级
function _obsEventShellHtml() {
  const chip = (on, label, fn) => `<a style="cursor:pointer;padding:2px 8px;border-radius:10px;font-size:12px;${on ? 'background:var(--a);color:#fff' : 'color:var(--a)'}" onclick="${fn}">${label}</a>`;
  const sources = [['', I18N.t('observability.ev_all')], ['monitor', I18N.t('observability.source.monitor')],
    ['gap', I18N.t('observability.source.gap')], ['mutation', I18N.t('observability.source.mutation')],
    ['task', I18N.t('observability.source.task')], ['change', I18N.t('observability.source.change')]];
  // 状态全集（S1）：firing/resolved 之外补齐 failed/stuck/done
  const states = [['', I18N.t('observability.ev_all_state')], ['firing', I18N.t('observability.state.firing')],
    ['resolved', I18N.t('observability.state.resolved')], ['failed', I18N.t('observability.state.failed')],
    ['stuck', I18N.t('observability.state.stuck')], ['done', I18N.t('observability.state.done')]];
  const srcChips = sources.map(([k, l]) => chip(_obsEventSource === k, l, `obsEventFilter('${k}')`)).join(' ');
  const stChips = states.map(([k, l]) => chip(_obsEventState === k, l, `obsEventStateFilter('${k}')`)).join(' ');
  return `<div class="cd" style="padding:12px 14px">
    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
      <span style="font-weight:600">${I18N.t('observability.events_title')}</span>
      ${srcChips}<span class="ts t3">|</span>${stChips}
      <input id="obsEventQ" type="text" value="${_obsEventQ.replace(/"/g, '&quot;')}"
        placeholder="${I18N.t('observability.ev_filter_keyword')}"
        onkeydown="if(event.key==='Enter')obsEventSearch()"
        style="flex:1;min-width:180px;padding:4px 8px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c)">
      <button class="bt bt-o bt-s" style="white-space:nowrap;padding:4px 12px" onclick="obsEventSearch()">${I18N.t('observability.ev_search')}</button>
      <span class="ts t3" id="obsEventCount"></span>
    </div>
    <div id="obsEventNew" style="display:none;margin-top:8px"></div>
    <div id="obsEventTrunc" class="ts t3" style="margin-top:6px"></div>
    <div id="obsEventBody" style="margin-top:8px"><div class="loading">${I18N.t('observability.loading')}</div></div>
    <div id="obsEventMore" style="text-align:center;margin-top:10px"></div>
  </div>`;
}

// 一条事件 = 两段式行 + 行内展开（替代原 8 列表格）
function _obsEventRowHtml(e) {
  const point = (e.ts_kind === 'point');            // 瞬时事件（变更/审计）不显示时长
  const dur = point ? '' : (_durHuman(e.duration_sec) + (e.ongoing ? ' <span class="ts t3">…</span>' : ''));
  const did = String(e.key || '').replace(/[^A-Za-z0-9_-]/g, '_');
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/'/g, '&#39;').replace(/"/g, '&quot;');
  return `<div class="cd" style="padding:8px 10px;margin-bottom:6px;border:1px solid var(--b);border-radius:6px">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;cursor:pointer" onclick="obsEventToggle('${esc(e.key)}')">
      <span class="ts t3" style="min-width:92px">${_obsAlertTime(e.started_at)}</span>
      <span class="ts t3">${_obsSourceLabel(e)}</span>
      ${_obsEventBadge(e)}
      <span style="flex:1">${_obsEventTitle(e)}</span>
      <span class="ts t2">${_obsEventStateLabel(e)}</span>
      <span class="ts t3" style="min-width:60px;text-align:right">${dur}</span>
    </div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:4px">
      <span class="ts t2">${_obsEventTargetLabel(e)}</span>
      <span style="flex:1"></span>
      <span class="ts t2">${_obsEventActions(e)}</span>
    </div>
    <div id="obsev-d-${did}" style="display:none;margin-top:6px;border-top:1px dashed var(--b);padding-top:6px"></div>
  </div>`;
}

function _obsEventByKey(key) {
  const list = ((_obsEventsData || {}).events) || [];
  for (const e of list) if (e.key === key) return e;
  return null;
}

function _obsEventDetailHtml(e) {
  if (!e) return '';
  const rows = [];
  const add = (label, val) => { if (val !== '' && val !== null && val !== undefined && val !== '—') rows.push(`<span class="ts t2" style="display:inline-block;min-width:80px">${label}</span><span class="ts t2">${val}</span>`); };
  add(I18N.t('observability.ev_metric'), e.metric ? e.metric : '');
  add(I18N.t('observability.ev_value'), (e.value === null || e.value === undefined) ? '' : e.value);
  add(I18N.t('observability.ev_threshold'), (e.threshold === null || e.threshold === undefined) ? '' : e.threshold);
  add(I18N.t('observability.ev_detail'), e.detail ? String(e.detail) : '');
  add(I18N.t('observability.event_view_col.started'), e.started_at ? _obsAlertTime(e.started_at) : '');
  add(I18N.t('observability.event_view_col.duration'), _durHuman(e.duration_sec));
  return rows.length ? `<div style="display:grid;grid-template-columns:auto 1fr;gap:2px 10px">${rows.join('')}</div>`
    : `<div class="ts t3">${I18N.t('observability.no_data')}</div>`;
}

function obsEventToggle(key) {
  const did = String(key || '').replace(/[^A-Za-z0-9_-]/g, '_');
  const el = document.getElementById('obsev-d-' + did);
  if (!el) return;
  if (el.style.display === 'none') {
    el.innerHTML = _obsEventDetailHtml(_obsEventByKey(key));
    el.style.display = 'block';
  } else {
    el.style.display = 'none';
  }
}

function obsEventLoadMore() {
  if (!_obsEventMore) return;
  loadObsEvents(document.getElementById('obsContent'), true);
}

// 自动刷新（30s）：只把"首页里没有的新事件"攒起来 + 顶端提示，**不强制前插/不打断阅读**
function _obsEventSoftRefresh() {
  const now = Date.now();
  if (now - _obsEventLastSoft < 30000) return;
  _obsEventLastSoft = now;
  API.get('/obs/events?limit=50' + _obsEventQStr()).then(d => {
    const known = ((_obsEventsData || {}).events) || [];
    const seen = {};
    known.forEach(x => { seen[x.key] = 1; });
    const fresh = (((d || {}).events) || []).filter(x => !seen[x.key]);
    _obsEventNew = fresh;
    const box = document.getElementById('obsEventNew');
    if (!box) return;
    if (fresh.length) {
      box.style.display = 'block';
      box.innerHTML = `<a style="cursor:pointer;color:var(--a);font-size:12px" onclick="obsEventApplyNew()">${I18N.t('observability.ev_new_events', { n: fresh.length })}</a>`;
    } else {
      box.style.display = 'none';
      box.innerHTML = '';
    }
  }).catch(() => {});
}

function obsEventApplyNew() {
  const list = _obsEventNew || [];
  if (!list.length) return;
  _obsEventsData = _obsEventsData || {};
  _obsEventsData.events = list.concat(_obsEventsData.events || []);   // 顶部前插（用户主动触发）
  _obsEventNew = [];
  _renderObsEventFlow();
  const box = document.getElementById('obsEventNew');
  if (box) { box.style.display = 'none'; box.innerHTML = ''; }
}

function _renderObsEventFlow() {
  const b = document.getElementById('obsEventBody');
  if (!b) return;
  const d = _obsEventsData || {};
  const list = d.events || [];
  const n = document.getElementById('obsEventCount');
  if (n) n.textContent = I18N.t('observability.ev_count', { n: list.length });
  const tr = document.getElementById('obsEventTrunc');
  if (tr) {
    const ts = d.truncated_sources || [];
    tr.textContent = ts.length
      ? I18N.t('observability.ev_truncated', { sources: ts.map(s => I18N.t('observability.source.' + s)).join(I18N.t('observability.list_sep')) })
      : '';
  }
  const more = document.getElementById('obsEventMore');
  if (more) {
    more.innerHTML = _obsEventMore
      ? `<button class="bt bt-o bt-s" style="padding:4px 14px" onclick="obsEventLoadMore()">${I18N.t('observability.ev_load_more')}</button>`
      : '';
  }
  b.innerHTML = list.length ? list.map(e => _obsEventRowHtml(e)).join('')
    : '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_events') + '</div>';
}

function loadObsEvents(c, append) {
  if (!c) return;
  const qs = _obsEventQStr();
  const curQ = (append && _obsEventCursor) ? '&before=' + encodeURIComponent(_obsEventCursor) : '';
  if (!append) c.innerHTML = _obsEventShellHtml();
  const more = document.getElementById('obsEventMore');
  if (more) more.innerHTML = I18N.t('observability.loading');
  API.get('/obs/events?limit=50' + qs + curQ)
    .then(d => {
      d = d || {};
      if (append) {
        const cur = ((_obsEventsData || {}).events) || [];
        _obsEventsData = d;
        _obsEventsData.events = cur.concat(d.events || []);
      } else {
        _obsEventsData = d;
      }
      _obsEventCursor = d.next_cursor || null;
      _obsEventMore = !!d.next_cursor;
      _renderObsEventFlow();
    })
    .catch(e => {
      const b = document.getElementById('obsEventBody');
      if (b) b.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">'
        + I18N.t('observability.load_failed', { msg: e.message }) + '</div>';
      if (more) more.innerHTML = '';
    });
}

function _renderObsAlertPanel() {
  const box = document.getElementById('obsAlertPanelBody');
  if (!box) return;
  const a = _obsAlerts || {};
  const events = a.events || [];
  const rules = a.rules || [];
  const now = Date.now();

  if (_obsAlertPanelView === 'rules') {
    if (!rules.length) { box.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_data') + '</div>'; return; }
    box.innerHTML = '<table class="tw"><thead><tr>' +
      ['name', 'metric', 'threshold', 'for_sec', 'severity', 'enabled', 'notify'].map(k => '<th>' + I18N.t('observability.rule_col.' + k) + '</th>').join('') +
      '</tr></thead><tbody>' + rules.map(r =>
        '<tr>' +
        '<td>' + I18N.t('observability.rule.' + r.name, {}) + '</td>' +
        '<td class="ts t2">' + (r.metric || '') + '</td>' +
        '<td>' + r.threshold + ' <a style="cursor:pointer;color:var(--a)" onclick="obsAlertSetThreshold(' + r.id + ',' + r.threshold + ')">' + I18N.t('observability.edit') + '</a></td>' +
        '<td class="ts t2">' + r.for_sec + 's</td>' +
        '<td class="ts t2">' + _obsSeverityCell(r) + '</td>' +
        '<td><a style="cursor:pointer;color:var(--a)" onclick="obsAlertToggleRule(' + r.id + ',' + (Number(r.enabled) ? 'false' : 'true') + ')">' +
          (Number(r.enabled) ? I18N.t('observability.on') : I18N.t('observability.off')) + '</a></td>' +
        '<td>' + _obsChanLabel(r.notify_channels) +
          ' <a style="cursor:pointer;color:var(--a)" onclick="obsAlertSetNotify(' + r.id + ',\'' + String(r.notify_channels || '') + '\')">' + I18N.t('observability.edit') + '</a></td>' +
        '</tr>').join('') + '</tbody></table>' +
      '<div class="ts t3" style="margin-top:10px">' + I18N.t('observability.alert_rules_hint') + '</div>';
    return;
  }

  if (_obsAlertPanelView === 'silences') {
    const sil = events.filter(e => e.silenced_until && new Date(e.silenced_until).getTime() > now);
    if (!sil.length) { box.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_silence') + '</div>'; return; }
    box.innerHTML = '<table class="tw"><thead><tr>' +
      ['rule', 'repo', 'until'].map(k => '<th>' + I18N.t('observability.silence_col.' + k) + '</th>').join('') +
      '</tr></thead><tbody>' + sil.map(e =>
        '<tr><td>' + I18N.t('observability.rule.' + e.rule_name, {}) + '</td>' +
        '<td class="ts t2">' + _shortRepo(e.repo_key) + '</td>' +
        '<td class="ts t2">' + _obsAlertTime(e.silenced_until) + '</td></tr>').join('') + '</tbody></table>';
    return;
  }

  // 触发记录
  if (!events.length) {
    box.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_alerts') + '</div>';
    return;
  }
  box.innerHTML = '<table class="tw"><thead><tr>' +
    ['rule', 'severity', 'source', 'repo', 'value', 'status', 'last_seen', 'op'].map(k => '<th>' + I18N.t('observability.event_col.' + k) + '</th>').join('') +
    '</tr></thead><tbody>' + events.map(e => {
      const firing = e.status === 'firing';
      const silenced = e.silenced_until && new Date(e.silenced_until).getTime() > now;
      return '<tr>' +
        '<td>' + I18N.t('observability.rule.' + e.rule_name, {}) +
          (silenced ? ' <span class="ts t3">(' + I18N.t('observability.silenced') + ')</span>' : '') + '</td>' +
        '<td class="ts t2">' + _obsSeverityCell(e) + '</td>' +
        '<td class="ts t3">' + _obsSourceLabel(e) + '</td>' +
        '<td class="ts t2">' + (_shortRepo(e.repo_key) || _obsEventTarget(e)) + '</td>' +
        '<td>' + _obsMetricVal(e) + (String(e.metric || '').indexOf('gap_') === 0 ? '' : ' <span class="ts t3">/ ' + e.threshold + '</span>') + '</td>' +
        '<td class="ts t2">' + I18N.t('observability.status.' + (e.status || ''), {}) + '</td>' +
        '<td class="ts t2">' + _obsAlertTime(e.last_seen_at) + '</td>' +
        '<td>' + (firing ? '<a style="cursor:pointer;color:var(--a)" onclick="obsAlertSilence(' + e.id + ')">' + I18N.t('observability.silence') + '</a>' : '—') + '</td>' +
        '</tr>';
    }).join('') + '</tbody></table>';
}

function obsAlertSilence(eventId) {
  app.showPrompt({
    title: I18N.t('observability.silence_prompt'),
    value: '60',
    onConfirm: function(m) {
      if (m === null) return;
      const minutes = parseInt(m, 10);
      if (!minutes || minutes < 1) return;
      API.post('/obs/alerts/' + eventId + '/silence?minutes=' + minutes, {}).then(() => refreshObsAlerts()).catch(() => null);
    }
  });
}

function obsAlertToggleRule(ruleId, enabled) {
  API.post('/obs/alerts/rules/' + ruleId + '/toggle?enabled=' + (enabled ? 'true' : 'false'), {}).then(() => refreshObsAlerts()).catch(() => null);
}

function obsAlertSetThreshold(ruleId, cur) {
  app.showPrompt({
    title: I18N.t('observability.threshold_prompt'),
    value: String(cur),
    onConfirm: function(v) {
      if (v === null) return;
      const f = parseFloat(v);
      if (isNaN(f)) return;
      API.post('/obs/alerts/rules/' + ruleId + '/threshold?threshold=' + encodeURIComponent(String(f)), {}).then(() => refreshObsAlerts()).catch(() => null);
    }
  });
}

function _obsChanLabel(chans) {
  const s = String(chans || '').trim();
  if (!s) return '<span class="ts t3">' + I18N.t('observability.notify_none') + '</span>';
  const parts = s.split(',').map(c => {
    const k = c.trim();
    return k === 'feishu' ? I18N.t('observability.notify_feishu')
      : (k === 'webhook' ? I18N.t('observability.notify_webhook') : k);
  });
  return parts.join(' + ');
}

// 通知渠道编辑：选择 0=不推送 1=飞书 2=飞书+webhook
function obsAlertSetNotify(ruleId, cur) {
  const now = String(cur || '').trim();
  const curIdx = now === 'feishu' ? '1' : (now.indexOf('webhook') >= 0 ? '2' : '0');
  app.showPrompt({
    title: I18N.t('observability.notify_prompt'),
    value: curIdx,
    onConfirm: function(v) {
      if (v === null) return;
      const map = { '0': '', '1': 'feishu', '2': 'feishu,webhook' };
      if (map[v] === undefined) return;
      API.post('/obs/alerts/rules/' + ruleId + '/notify?channels=' + encodeURIComponent(map[v]), {})
        .then(() => refreshObsAlerts()).catch(() => null);
    }
  });
}

// 测试飞书通知：给自己发一条，结果就地显示（不依赖全局 toast）
function obsAlertTestNotify() {
  const el = document.getElementById('obsAlertNotifyResult');
  if (el) el.textContent = I18N.t('observability.test_notify_sending');
  API.post('/obs/alerts/test-notify', {}).then(r => {
    const ok = r && r.ok;
    if (el) {
      el.textContent = ok ? I18N.t('observability.test_notify_ok',
        { id: (r && r.message_id) || '' }) : ((r && r.message) || I18N.t('observability.test_notify_fail'));
      el.style.color = ok ? '#22a06b' : '#d64545';
    }
  }).catch(e => {
    if (el) { el.textContent = I18N.t('observability.test_notify_fail') + ': ' + ((e && e.message) || e); el.style.color = '#d64545'; }
  });
}

function _obsFmtAxis(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  return _obsOvRange === '7d' ? (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm : hm;
}

function _obsChart(id, option) {
  const el = document.getElementById(id);
  if (!el) return;
  if (typeof echarts === 'undefined') {
    el.innerHTML = '<div class="ts t3" style="padding:20px 0">' + I18N.t('observability.ov.charts_unavailable') + '</div>';
    return;
  }
  try {
    if (_obsOvCharts[id]) _obsOvCharts[id].dispose();
    const ch = echarts.init(el);
    ch.setOption(option);
    _obsOvCharts[id] = ch;
  } catch (e) {
    el.innerHTML = '<div class="ts t3" style="padding:20px 0">' + I18N.t('observability.ov.chart_failed') + '</div>';
  }
}

function _renderObsOverview(d, alertData) {
  const body = document.getElementById('obsOvBody');
  if (!body) return;
  const k = d.kpis || {};
  const ov = I18N.t.bind(I18N);
  const kpi = (label, value, sub) => `
    <div class="cd" style="padding:12px 14px">
      <div class="ts t3">${label}</div>
      <div style="font-size:22px;font-weight:600;line-height:1.3">${value}</div>
      ${sub ? `<div class="ts t3">${sub}</div>` : ''}
    </div>`;
  const succ = (k.success_rate === null || k.success_rate === undefined) ? '—' : k.success_rate + '%';
  const p95 = (k.p95_duration_s === null || k.p95_duration_s === undefined) ? '—'
    : (k.p95_duration_s >= 60 ? Math.floor(k.p95_duration_s / 60) + 'm' + Math.round(k.p95_duration_s % 60) + 's'
      : Math.round(k.p95_duration_s) + 's');
  const eng = (k.engines_total ? (k.engines_online + ' / ' + k.engines_total) : '—');
  const tokens = (k.tokens || 0).toLocaleString();

  body.innerHTML = `
    ${_obsBannerHtml(alertData)}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px">
      ${kpi(ov('observability.ov.kpi_queue'), k.queue_depth || 0)}
      ${kpi(ov('observability.ov.kpi_success'), succ)}
      ${kpi(ov('observability.ov.kpi_p95'), p95)}
      ${kpi(ov('observability.ov.kpi_engines'), eng)}
      ${kpi(ov('observability.ov.kpi_tokens'), tokens, ov('observability.ov.scope') + (d.scope ? d.scope.repos : 0))}
    </div>
    <div style="display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-top:12px">
      <div class="cd" style="padding:12px 14px">
        <div class="ts t3" style="margin-bottom:4px">${ov('observability.ov.chart_outcome')}</div>
        <div id="obsChart-outcome" style="height:240px"></div>
      </div>
      <div class="cd" style="padding:12px 14px">
        <div class="ts t3" style="margin-bottom:4px">${ov('observability.ov.chart_model')}</div>
        <div id="obsChart-model" style="height:240px"></div>
      </div>
    </div>
    <div class="cd" style="padding:12px 14px;margin-top:12px">
      <div class="ts t3" style="margin-bottom:4px">${ov('observability.ov.chart_token')}</div>
      <div id="obsChart-token" style="height:220px"></div>
    </div>`;

  const C = d.charts || {};
  const to = C.task_outcome || {};
  const labels = (to.buckets || []).map(_obsFmtAxis);
  const axis = { axisLabel: { fontSize: 10 }, axisLine: { lineStyle: { color: '#888' } } };
  const grid = { left: 42, right: 46, top: 28, bottom: 24 };
  const legend = { top: 0, right: 0, itemWidth: 10, itemHeight: 8, textStyle: { fontSize: 11 } };

  _obsChart('obsChart-outcome', {
    tooltip: { trigger: 'axis' },
    legend: Object.assign({ data: [ov('observability.ov.s_completed'), ov('observability.ov.s_failed'),
      ov('observability.ov.s_cancelled'), ov('observability.ov.s_rate')] }, legend),
    grid: grid,
    xAxis: Object.assign({ type: 'category', data: labels }, axis),
    yAxis: [{ type: 'value', minInterval: 1, axisLabel: { fontSize: 10 } },
            { type: 'value', min: 0, max: 100, splitLine: { show: false }, axisLabel: { fontSize: 10, formatter: '{value}%' } }],
    series: [
      { name: ov('observability.ov.s_completed'), type: 'bar', stack: 'o', barMaxWidth: 18, itemStyle: { color: '#22a06b' }, data: to.completed || [] },
      { name: ov('observability.ov.s_failed'), type: 'bar', stack: 'o', barMaxWidth: 18, itemStyle: { color: '#d64545' }, data: to.failed || [] },
      { name: ov('observability.ov.s_cancelled'), type: 'bar', stack: 'o', barMaxWidth: 18, itemStyle: { color: '#e0a02a' }, data: to.cancelled || [] },
      { name: ov('observability.ov.s_rate'), type: 'line', yAxisIndex: 1, connectNulls: true, symbolSize: 5,
        itemStyle: { color: '#4a7fd1' }, lineStyle: { width: 2 }, data: to.rate || [] }
    ]
  });

  const tt = C.token_trend || {};
  _obsChart('obsChart-token', {
    tooltip: { trigger: 'axis' },
    legend: Object.assign({ data: [ov('observability.ov.s_input'), ov('observability.ov.s_output'), ov('observability.ov.s_cache')] }, legend),
    grid: Object.assign({}, grid, { top: 26 }),
    xAxis: Object.assign({ type: 'category', data: (tt.buckets || []).map(_obsFmtAxis) }, axis),
    yAxis: [{ type: 'value', axisLabel: { fontSize: 10 } }],
    series: [
      { name: ov('observability.ov.s_input'), type: 'bar', stack: 't', barMaxWidth: 18, itemStyle: { color: '#4a7fd1' }, data: tt.input || [] },
      { name: ov('observability.ov.s_output'), type: 'bar', stack: 't', barMaxWidth: 18, itemStyle: { color: '#22a06b' }, data: tt.output || [] },
      { name: ov('observability.ov.s_cache'), type: 'bar', stack: 't', barMaxWidth: 18, itemStyle: { color: '#8b8b8b' }, data: tt.cache || [] }
    ]
  });

  const ms = (C.model_share || []).slice().reverse();   // 横向条：ECharts y 轴自下而上
  _obsChart('obsChart-model', {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: p => { const it = p[0]; const m = ms[it.dataIndex] || {}; return `${m.model || ''}<br/>${(m.tokens || 0).toLocaleString()} tokens · ${m.share || 0}%`; } },
    grid: { left: 90, right: 24, top: 10, bottom: 20 },
    xAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    yAxis: { type: 'category', data: ms.map(m => m.model), axisLabel: { fontSize: 10, width: 84, overflow: 'truncate' } },
    series: [{ type: 'bar', barMaxWidth: 16, itemStyle: { color: '#4a7fd1' }, data: ms.map(m => m.tokens) }]
  });
}

function switchObsTab(tab, ev) {
  _obsTab = tab;
  document.querySelectorAll('.stabs a').forEach(a => a.classList.remove('ac'));
  if (ev) ev.target.classList.add('ac');
  // 切换 tab 时重置渲染状态，强制重建 DOM 骨架
  _obsRendered = false;
  _obsRenderedTab = null;
  _workerPage = 0;
  _runtimePage = 0;
  _timelinePage = 0;
  // 直接用缓存数据渲染，不重新 fetch
  if (_obsData.ops) {
    renderObsContent(false);
  } else {
    refreshObs();
  }
}

function toggleObsAuto() {
  const cb = document.getElementById('obsAuto');
  if (cb && cb.checked) { startObsAuto(); }
  else { stopObsAuto(); }
}

function startObsAuto() {
  stopObsAuto();
  _obsTimer = setInterval(() => refreshObs(true), 10000);
}

function stopObsAuto() {
  clearInterval(_obsTimer);
  _obsTimer = null;
}

// isAutoRefresh=true 表示由定时器静默触发（不显示"加载中"，走局部更新路径）
function refreshObs(isAutoRefresh) {
  const c = document.getElementById('obsContent');
  if (!c) return;
  // 静默刷新（定时器触发）不显示"加载中"，保留旧数据直到新数据到达
  if (!isAutoRefresh && !_obsRendered) {
    c.innerHTML = '<div class="loading">' + I18N.t('observability.loading') + '</div>';
  }
  // 手动刷新（非自动）时触发引擎探测（60s 防抖由后端控制）
  if (!isAutoRefresh) {
    API.get('/dashboard/ops/probe-now').catch(() => null);
  }
  // 获取当前选中仓库，过滤仅该仓库的任务
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '?repo=' + encodeURIComponent(repo.fullName) : '';
  Promise.all([
    API.get('/dashboard/ops/overview' + repoParam).catch(e => {
      console.warn('ops/overview failed:', e);
      return null;
    }),
    fetch('/health/realtime', {signal: AbortSignal.timeout(10000)}).then(r => r.json()).catch(() => null),
    // fetch('/metrics').then(r => r.text()).catch(() => null),  // metrics 数据当前未使用，注释掉避免无用请求
  ]).then(([ops, realtime]) => {
    _obsData = { ops, realtime };
    const now = new Date().toLocaleTimeString('zh-CN');
    const lu = document.getElementById('obsLastUpdate');
    if (lu) lu.textContent = now;
    renderObsContent(isAutoRefresh);
  });
}

// ===== 概览分页辅助函数（参照异常任务 Tab 的 slice 分页模式）=====

function _paginationWithPageSizeHTML(page, totalPages, prevExpr, nextExpr, pageSize, pageSizeVar, rerenderExpr) {
  const prevDis = page === 0 ? 'disabled style="opacity:.4;cursor:not-allowed"' : '';
  const nextDis = page >= totalPages - 1 ? 'disabled style="opacity:.4;cursor:not-allowed"' : '';
  return `<div style="display:flex;align-items:center;justify-content:center;gap:10px;padding:12px 0;flex-wrap:wrap">
    ${totalPages > 1 ? `<button class="bt bt-o bt-s" ${prevDis} onclick="${prevExpr}">${I18N.t('observability.prev_page')}</button>` : ''}
    <span class="ts t3">${page + 1} / ${totalPages}</span>
    ${totalPages > 1 ? `<button class="bt bt-o bt-s" ${nextDis} onclick="${nextExpr}">${I18N.t('observability.next_page')}</button>` : ''}
    <label class="ts t3" style="display:flex;align-items:center;gap:4px;margin-left:8px">${I18N.t('observability.rows_per_page')}
      <select onchange="${pageSizeVar}=parseInt(this.value)||10;${rerenderExpr}" style="padding:4px 8px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c)">
        <option value="10"${pageSize===10?' selected':''}>10</option>
        <option value="20"${pageSize===20?' selected':''}>20</option>
        <option value="50"${pageSize===50?' selected':''}>50</option>
      </select>
    </label>
  </div>`;
}

function _sortedWorkers(w) {
  return (w.list || []).slice().sort((a, b) => {
    const na = (a.device_name || a.name || '').toLowerCase();
    const nb = (b.device_name || b.name || '').toLowerCase();
    return na < nb ? -1 : na > nb ? 1 : 0;
  });
}

function _sortedRuntimes(r) {
  return (r.list || []).slice().sort((a, b) => (a.device_name || '').localeCompare(b.device_name || ''));
}

function _filterRuntimes(runtimes) {
  // Issue #436：设备下拉残留失效设备名（设备失联/吊销后被过滤出列表）时，
  // select 已回显「所有设备」但变量仍持旧值 → 一切搜索 0 匹配。
  // 此处校验 _rtDeviceFilter 仍在当前数据源中，不在则视为未选设备。
  const _devSet = _rtDeviceFilter ? new Set(runtimes.map(rt => rt.device_name || '')) : null;
  return runtimes.filter(rt => {
    const _dev = rt.device_name || '';
    if (_rtDeviceFilter && _devSet.has(_rtDeviceFilter) && _dev !== _rtDeviceFilter) return false;
    if (_rtSearch) {
      // Issue #436：搜索词 trim——首尾空格（输入法/粘贴）不应导致全列表 0 匹配
      const _q = _rtSearch.trim().toLowerCase();
      if (_q &&
          !(rt.name || '').toLowerCase().includes(_q) &&
          !(rt.device_name || '').toLowerCase().includes(_q) &&
          !(rt.version || '').toLowerCase().includes(_q) &&
          !(rt.mode || '').toLowerCase().includes(_q)) return false;
    }
    return true;
  });
}

function _workerCardHTML(wk) {
  const wid = wk.id != null ? wk.id : (wk.device_name || wk.name || '').replace(/[^a-zA-Z0-9_-]/g, '_');
  const dot = _OBS_HEALTH_DOT[wk.health] || '⚫';
  const hc = _OBS_HEALTH_COLOR[wk.health] || 'var(--t3)';
  return `<div id="wk-${wid}" style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <span id="wk-${wid}-dot" style="font-size:14px">${dot}</span>
      <b style="font-size:13px">${wk.device_name || wk.name || I18N.t('observability.worker_id', {id: wk.id})}</b>
      <span id="wk-${wid}-health" style="margin-left:auto;font-size:10px;color:${hc};font-weight:600">${I18N.t("observability." + (wk.health || "offline"))}</span>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 12px;font-size:10px;color:var(--t3);margin-bottom:8px">
      <div>IP: ${wk.last_ip || '-'}</div>
      <div>${I18N.t('observability.heartbeat')}: <span id="wk-${wid}-seen">${fmtLocalTime(wk.last_seen_at||'').substring(11,19)}</span></div>
    </div>
    ${_barHTML(I18N.t('observability.cpu'), wk.cpu_percent, null, 'wk-' + wid + '-cpu')}
    ${_barHTML(I18N.t('observability.mem'), wk.memory_percent, null, 'wk-' + wid + '-mem')}
    ${_barHTML(I18N.t('observability.tmp'), wk.tmp_percent, null, 'wk-' + wid + '-tmp')}
    ${_barHTML(I18N.t('observability.disk'), wk.disk_percent, null, 'wk-' + wid + '-disk')}
    ${_barHTML(I18N.t('observability.bandwidth'), wk.bandwidth_percent, null, 'wk-' + wid + '-bw', (wk.bandwidth_mbps||0).toFixed(1) + ' Mbps')}
  </div>`;
}

function _obsHealthScoreHTML(rt) {
  const s = rt.health_score;
  if (s === null || s === undefined) return '-';
  const c = s === 0 ? 'var(--r)' : s < 50 ? 'var(--y)' : 'var(--g)';
  const cf = rt.health_consecutive_failures > 0 ? ` <span style="font-size:9px;opacity:.7">连失${rt.health_consecutive_failures}</span>` : '';
  const hb = rt.health_heartbeat_alive === false ? ' · 失联' : '';
  return `<span style="color:${c};font-weight:600">${s}</span>${cf}<span style="font-size:9px;opacity:.7">${hb}</span>`;
}

// ===== Token 消耗：表格内嵌列（引擎表 / session 列表）=====
// 同一套 engine_id = agent_runtimes.id（与引擎表同源）；tasks 按 task_id 匹配。
let _obsTokEngines = {};
let _obsTokTasks = {};
let _obsTokColAt = 0;

function _obsTokColNum(v) {
  const n = Number(v || 0);
  if (!isFinite(n) || n <= 0) return '—';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}
function _obsTokFor(engineId) {
  return _obsTokColNum(_obsTokEngines[String(engineId)]);
}
function _obsTokTaskFor(taskId) {
  return _obsTokColNum(_obsTokTasks[String(taskId)]);
}
function loadObsTokenColumns(force) {
  const now = Date.now();
  if (!force && (now - _obsTokColAt) < 60000) return;
  _obsTokColAt = now;
  const full = (typeof _obsSloRepoFull === 'function') ? _obsSloRepoFull() : '';
  const repoQ = full ? '&repo=' + encodeURIComponent(full) : '';
  API.get('/obs/tokens/engines?range=24h' + repoQ).then(d => {
    const m = {};
    ((d || {}).engines || []).forEach(e => { m[String(e.engine_id)] = e.total; });
    _obsTokEngines = m;
    if (typeof _rerenderRuntimeTable === 'function') _rerenderRuntimeTable();
  }).catch(() => {});
  API.get('/obs/tokens/tasks?range=7d&limit=500' + repoQ).then(d => {
    const m = {};
    ((d || {}).tasks || []).forEach(t => { m[String(t.task_id)] = t.total; });
    _obsTokTasks = m;
  }).catch(() => {});
}

function _runtimeRowHTML(rti) {
  const rid = rti.id;
  const dot = _OBS_HEALTH_DOT[rti.health] || '⚫';
  const hc = _OBS_HEALTH_COLOR[rti.health] || 'var(--t3)';
  const running = rti.running || 0;
  const maxC = rti.max_concurrent || 0;
  const loadColor = running >= maxC * 2 ? 'var(--r)' : running >= maxC ? 'var(--y)' : running > 0 ? 'var(--g)' : 'var(--t3)';
  const modeTag = rti.mode === 'local' ? '<span class="tg tg-ch">' + I18N.t('observability.mode_local') + '</span>' : '<span class="tg tg-c">' + I18N.t('observability.mode_remote') + '</span>';
  const avgDur = rti.avg_duration || 0;
  const avgDurStr = avgDur > 60 ? Math.round(avgDur/60) + 'm' + Math.round(avgDur%60) + 's' : avgDur + 's';
  const avgDurColor = avgDur > 300 ? 'var(--r)' : avgDur > 60 ? 'var(--y)' : 'var(--g)';
  const ps = rti.probe_status || 'pending';
  const pl = rti.probe_latency || 0;
  const pe = rti.probe_error || '';
  const probeIcon = { ok: '✅', fail: '❌', skip: '⏭', pending: '⏳' }[ps] || '⏳';
  const probeColor = ps === 'ok' ? 'var(--g)' : ps === 'fail' ? 'var(--r)' : 'var(--t3)';
  const probeStr = ps === 'ok' ? pl + 's' : ps === 'pending' ? I18N.t('observability.probe_pending') : ps === 'skip' ? I18N.t('observability.probe_skip') : I18N.t('observability.probe_fail');
  const probeTitle = pe ? ' title="' + pe.replace(/"/g, '&quot;') + '"' : '';
  return `<tr id="rt-${rid}" style="cursor:pointer" onclick="openRuntimeSessionModal(${rid}, '${(rti.name||'').replace(/'/g,"")}')">
    <td>${dot} <b>${rti.name}</b></td>
    <td><span id="rt-${rid}-version" class="ts t3">${rti.version || '-'}</span></td>
    <td><span id="rt-${rid}-health" style="color:${hc};font-weight:600">${I18N.t('observability.' + (rti.health || 'offline'))}</span></td>
    <td><span id="rt-${rid}-score" class="ts t3">${_obsHealthScoreHTML(rti)}</span></td>
    <td><span id="rt-${rid}-load" style="color:${loadColor}">${running}/${maxC}</span></td>
    <td><span id="rt-${rid}-dur" style="color:${avgDurColor}">${avgDur ? avgDurStr : '-'}</span></td>
    <td><span id="rt-${rid}-tok" class="ts t2">${_obsTokFor(rid)}</span></td>
    <td><span${probeTitle} style="color:${probeColor}">${probeIcon} ${probeStr}</span></td>
    <td>${modeTag}</td>
    <td><span id="rt-${rid}-device" class="ts t3">${rti.device_name || '-'}</span></td>
  </tr>`;
}

function _rerenderWorkerGrid() {
  const { ops, realtime } = _obsData;
  const realOps = (ops && !ops.error) ? ops : fallbackOps(realtime);
  if (!realOps) return;
  const workers = _sortedWorkers(realOps.workers || {});
  const totalPages = Math.max(1, Math.ceil(workers.length / _workerPageSize));
  if (_workerPage >= totalPages) _workerPage = 0;
  const start = _workerPage * _workerPageSize;
  const pageWorkers = workers.slice(start, start + _workerPageSize);
  const gridEl = document.getElementById('wk-grid');
  if (gridEl) {
    gridEl.innerHTML = workers.length === 0
      ? '<div class="ts t2" style="padding:12px">' + I18N.t('observability.no_remote_worker') + '</div>'
      : pageWorkers.map(_workerCardHTML).join('');
  }
  const pagerEl = document.getElementById('wk-pager');
  if (pagerEl) pagerEl.innerHTML = workers.length > 0 ? _paginationWithPageSizeHTML(_workerPage, totalPages, '_workerPage--;_rerenderWorkerGrid()', '_workerPage++;_rerenderWorkerGrid()', _workerPageSize, '_workerPageSize', '_workerPage=0;_rerenderWorkerGrid()') : '';
}

function _rerenderRuntimeTable() {
  const { ops, realtime } = _obsData;
  const realOps = (ops && !ops.error) ? ops : fallbackOps(realtime);
  if (!realOps) return;
  const r = realOps.runtimes || {};
  const runtimes = _sortedRuntimes(r);
  const _filteredRuntimes = _filterRuntimes(runtimes);
  const totalPages = Math.max(1, Math.ceil(_filteredRuntimes.length / _runtimePageSize));
  if (_runtimePage >= totalPages) _runtimePage = 0;
  const start = _runtimePage * _runtimePageSize;
  const pageRuntimes = _filteredRuntimes.slice(start, start + _runtimePageSize);
  const rowsEl = document.getElementById('obs-runtime-rows');
  const countEl = document.getElementById('obs-runtime-count');
  if (countEl) countEl.textContent = _filteredRuntimes.length + '/' + runtimes.length;
  if (rowsEl) {
    let html = '';
    if (pageRuntimes.length) {
      html += '<div class="tw"><table><thead><tr><th>' + I18N.t('observability.col_engine') + '</th><th>' + I18N.t('observability.col_version') + '</th><th>' + I18N.t('observability.col_health') + '</th><th>健康分</th><th>' + I18N.t('observability.col_busy') + '</th><th>' + I18N.t('observability.col_avg_duration') + '</th><th>' + I18N.t('observability.col_token') + '</th><th>' + I18N.t('observability.col_probe') + '</th><th>' + I18N.t('observability.col_mode') + '</th><th>' + I18N.t('observability.col_device') + '</th></tr></thead><tbody>';
      html += pageRuntimes.map(_runtimeRowHTML).join('');
      html += '</tbody></table></div>';
    } else {
      html += '<div class="ts t2" style="padding:12px">' + I18N.t('observability.no_matching_engine') + '</div>';
    }
    rowsEl.innerHTML = html;
  }
  const pagerEl = document.getElementById('rt-pager');
  if (pagerEl) pagerEl.innerHTML = _filteredRuntimes.length > 0 ? _paginationWithPageSizeHTML(_runtimePage, totalPages, '_runtimePage--;_rerenderRuntimeTable()', '_runtimePage++;_rerenderRuntimeTable()', _runtimePageSize, '_runtimePageSize', '_runtimePage=0;_rerenderRuntimeTable()') : '';
}

function renderObsContent(isAutoRefresh) {
  const c = document.getElementById('obsContent');
  if (!c) return;
  const { ops, realtime } = _obsData;

  // 降级：ops 401 时从 realtime 补齐
  const realOps = (ops && !ops.error) ? ops : fallbackOps(realtime);
  const rt = realtime || {};
  // const mt = metricsText || '';  // metrics 数据当前未使用

  // 错误日志 Tab: 独立异步加载，只首次加载 + 手动刷新，定时器不触发
  if (_obsTab === 'errorlogs') {
    if (!isAutoRefresh) loadErrorLogs(c);
    return;
  }
  // 统一事件视图 Tab: 独立异步加载；自动刷新只检查"有没有新事件"（30s，顶部提示不强制前插）
  if (_obsTab === 'events') {
    if (isAutoRefresh) _obsEventSoftRefresh();
    else obsEventReload();
    return;
  }

  // overview / tasks Tab: 首次用 innerHTML 建骨架，后续走局部更新
  const canPartialUpdate = isAutoRefresh && _obsRendered && _obsRenderedTab === _obsTab;

  if (_obsTab === 'overview') {
    if (canPartialUpdate) {
      // 尝试局部更新；元素缺失（如新增 worker）时回退到完整渲染
      if (!updateOverview(realOps, rt)) {
        c.innerHTML = renderOverview(realOps, rt);
        _obsRendered = true;
        _obsRenderedTab = _obsTab;
      }
    } else {
      c.innerHTML = renderOverview(realOps, rt);
      _obsRendered = true;
      _obsRenderedTab = _obsTab;
    }
    if (_obsSection === 'devices') loadObsTokenColumns();
  }
  else if (_obsTab === 'tasks') {
    if (canPartialUpdate) {
      if (!updateTasks(realOps, rt)) {
        c.innerHTML = renderTasksTab(realOps, rt);
        _obsRendered = true;
        _obsRenderedTab = _obsTab;
      }
    } else {
      c.innerHTML = renderTasksTab(realOps, rt);
      _obsRendered = true;
      _obsRenderedTab = _obsTab;
    }
    loadObsTokenColumns();
  }
}

// 异常任务入口（任务 Tab 内的按钮）：点击弹窗看卡住/失败明细。
// 注意：**随渲染字符串一起返回**，不要用 insertBefore 事后挂——那会在每次 10s 局部刷新时又插一根，
// 页面上就出现"每隔几秒多一行"（2026-10-18 修）。
function _obsTaskAnomalyBarHtml() {
  return `<div id="obsTaskAnomalyBar" style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
    <button class="bt bt-o bt-s" style="white-space:nowrap;padding:4px 12px" onclick="openObsAnomalyModal()">${I18N.t('observability.tab.anomaly')}</button>
    <span class="ts t3">${I18N.t('observability.anomaly_open_hint')}</span>
  </div>`;
}

// 弹窗内的重渲染（分页/每页条数用它，而不是 refreshObs——后者会重画整页、弹窗内容不动）
function _obsAnomalyRerender() {
  if (typeof document.getElementById !== 'function') return;
  const body = document.getElementById('obsAnomalyModalBody');
  if (!body) return;
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '?repo=' + encodeURIComponent(repo.fullName) : '';
  API.get('/dashboard/ops/overview' + repoParam).then(ops => {
    const b = document.getElementById('obsAnomalyModalBody');
    if (b) renderAnomalyTab(b, ops || {}, '_obsAnomalyRerender()');
  }).catch(e => {
    const b = document.getElementById('obsAnomalyModalBody');
    if (b) b.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">'
      + I18N.t('observability.load_failed', { msg: e.message }) + '</div>';
  });
}

function openObsAnomalyModal() {
  const old = (typeof document.getElementById === 'function')
    ? document.getElementById('obsAnomalyModal') : null;
  if (old && typeof old.remove === 'function') old.remove();
  const modal = document.createElement('div');
  modal.id = 'obsAnomalyModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:1001';
  modal.onclick = (e) => { if (e.target === modal && typeof modal.remove === 'function') modal.remove(); };
  modal.innerHTML = `<div class="cd" style="width:960px;max-width:94vw;max-height:84vh;display:flex;flex-direction:column;padding:0;overflow:hidden">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">
      <h3 style="font-size:14px;font-weight:600">${I18N.t('observability.tab.anomaly')}</h3>
      <button class="bt bt-o bt-s" onclick="document.getElementById('obsAnomalyModal').remove()">${I18N.t('observability.close')}</button>
    </div>
    <div id="obsAnomalyModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">${I18N.t('observability.loading_task')}</div></div>
  </div>`;
  document.body.appendChild(modal);
  _obsAnomalyRerender();
}

// ===== KPI 卡片行（仿 multica KpiCard grid）=====
// 带 id 的版本（value span 加 id 供局部更新）
function _kpiRowHTML(cards) {
  return `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:0;border:1px solid var(--b);border-radius:10px;overflow:hidden;margin-bottom:16px;background:var(--c)">
    ${cards.map(k => `
      <div style="display:flex;flex-direction:column;gap:4px;padding:16px 18px;border-right:1px solid var(--b);border-bottom:1px solid var(--b)">
        <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--t3)">${k.label}</div>
        <div ${k.id ? 'id="' + k.id + '"' : ''} style="font-size:22px;font-weight:700;line-height:1.2;color:${k.color||'var(--t)'};font-variant-numeric:tabular-nums">${k.value}</div>
        ${k.hint ? `<div style="font-size:10px;color:var(--t3)">${k.hint}</div>` : ''}
      </div>`).join('')}
  </div>`;
}

function kpiRow(cards) {
  return _kpiRowHTML(cards);
}

function section(title, subtitle) {
  return `<div style="margin-bottom:8px;margin-top:16px"><h2 style="font-size:13px;font-weight:600">${title}</h2>${subtitle ? `<span style="font-size:10px;color:var(--t3);margin-left:8px">${subtitle}</span>` : ''}</div>`;
}

function _fmtTotalDur(s) {
  if (!s || s <= 0) return '0s';
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm' + (s % 60 > 0 ? Math.round(s % 60) + 's' : '');
  return Math.floor(s / 3600) + 'h' + Math.floor((s % 3600) / 60) + 'm';
}

function table(headers, rows) {
  return `<div class="tw" style="border:1px solid var(--b);border-radius:10px;overflow:hidden;margin-bottom:16px"><table><thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

// ===== Tab: 概览 =====
// 健康度 → 圆点/颜色映射（供 renderOverview 和 updateOverview 共享）
const _OBS_HEALTH_DOT = { online: '🟢', idle: '🟢', normal: '🟢', busy: '🟡', overload: '🔴', recently_lost: '🟡', offline: '⚫', about_to_gc: '🔴' };
const _OBS_HEALTH_COLOR = { online: 'var(--g)', idle: 'var(--g)', normal: 'var(--g)', busy: 'var(--y)', overload: 'var(--r)', recently_lost: 'var(--y)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };

// 资源进度条（首次渲染时带 id，供后续局部更新）
function _barHTML(label, pct, color, idPrefix, suffix) {
  const c = pct > 80 ? 'var(--r)' : pct > 60 ? 'var(--y)' : color || 'var(--g)';
  const valText = suffix || (Math.round(pct) + '%');
  return `<div style="display:flex;align-items:center;gap:6px;font-size:10px">
    <span style="color:var(--t3);min-width:32px">${label}</span>
    <div style="flex:1;height:8px;background:var(--bg);border-radius:4px;overflow:hidden;min-width:60px">
      <div id="${idPrefix}-bar" style="height:100%;width:${Math.min(pct,100)}%;background:${c};border-radius:4px;transition:width .3s"></div>
    </div>
    <span id="${idPrefix}-val" style="color:${c};font-weight:600;min-width:50px;text-align:right">${valText}</span>
  </div>`;
}

// ===== SLO / 错误预算卡片（P2-1，§6.7）=====
// 总览顶部「健康结论」：N 条 SLO 的达成态 + 预算剩余；点卡片看燃尽曲线（实际 vs 理想）。
let _obsSlo = null;
let _obsSloAt = 0;
let _obsSloRepo = null;   // 缓存对应的仓库选择：切仓必须立刻重算（不能被 60s 节流挡住）

// 页面上的仓库选择器 → 查询串（与总览页其它 KPI/趋势图同一口径）
function _obsSloRepoFull() {
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  return (repo && repo.fullName) ? String(repo.fullName) : '';
}

function loadObsSlo(force) {
  const now = Date.now();
  const full = _obsSloRepoFull();
  if (!force && _obsSlo && full === _obsSloRepo && (now - _obsSloAt) < 60000) return;  // 60s 节流
  _obsSloAt = now;
  _obsSloRepo = full;
  API.get('/obs/slo' + (full ? '?repo=' + encodeURIComponent(full) : '')).then(d => {
    _obsSlo = d || { slos: [] };
    _fillObsSloCard();
  }).catch(() => { /* 卡片静默失败：不打扰总览其它内容 */ });
}

function _fillObsSloCard() {
  const box = document.getElementById('obsSloCard');
  if (box) box.innerHTML = _obsSloCardHtml(_obsSlo);
}

function _obsSloStateLabel(state) {
  return _obsTOr('observability.slo_state.' + state, state || '—');
}

function _obsSloCardHtml(d) {
  const list = (d || {}).slos || [];
  const cards = list.map(s => {
    const st = String(s.state || '');
    const color = st === 'breached' ? 'var(--r)'
      : (st === 'warning' ? '#d97706' : (st === 'no_data' ? 'var(--t3)' : 'var(--g)'));
    const show = (v, suffix) => (v === null || v === undefined) ? '—'
      : (Math.round(Number(v) * 10) / 10) + suffix;
    return `<div style="cursor:pointer;min-width:210px;flex:1;border-left:3px solid ${color};padding:4px 10px"
      onclick="openObsSloBurn(${s.slo_id})">
      <div class="ts t3">${_obsTOr('observability.slo_name.' + s.name, s.name)}</div>
      <div style="font-size:18px;font-weight:600;color:${color}">${show(s.value === null || s.value === undefined ? null : s.value * 100, '%')}
        <span class="ts t3" style="font-size:11px">/ ${I18N.t('observability.slo_target')} ${show((s.target || 0) * 100, '%')}</span></div>
      <div class="ts t2">${_obsSloStateLabel(st)} · ${I18N.t('observability.slo_budget_left')} ${show(s.budget_remaining_pct, '%')}</div>
    </div>`;
  }).join('');
  if (!cards) return '';
  return `<div class="cd" style="padding:10px 14px;margin-bottom:12px">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <span style="font-weight:600">${I18N.t('observability.slo_title')}</span>
      <span class="ts t3">${I18N.t('observability.slo_hint')}</span>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">${cards}</div>
  </div>`;
}

function openObsSloBurn(sloId) {
  const old = (typeof document.getElementById === 'function') ? document.getElementById('obsSloModal') : null;
  if (old && typeof old.remove === 'function') old.remove();
  const modal = document.createElement('div');
  modal.id = 'obsSloModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:1001';
  modal.onclick = (e) => { if (e.target === modal && typeof modal.remove === 'function') modal.remove(); };
  modal.innerHTML = `<div class="cd" style="width:880px;max-width:94vw;max-height:84vh;display:flex;flex-direction:column;padding:0;overflow:hidden">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">
      <h3 style="font-size:14px;font-weight:600">${I18N.t('observability.slo_burn_title')}</h3>
      <button class="bt bt-o bt-s" onclick="document.getElementById('obsSloModal').remove()">${I18N.t('observability.close')}</button>
    </div>
    <div id="obsSloModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">${I18N.t('observability.loading')}</div></div>
  </div>`;
  document.body.appendChild(modal);
  const _full = _obsSloRepoFull();
  API.get('/obs/slo/' + sloId + (_full ? '?repo=' + encodeURIComponent(_full) : '')).then(d => {
    d = d || {};
    const b = document.getElementById('obsSloModalBody');
    if (!b) return;
    const burn = d.burn || [];
    const _v = (v, suf) => (v === null || v === undefined) ? '—' : (Math.round(Number(v) * 10) / 10) + suf;
    // 标题下常驻"口径 + 状态 + 目标 + 预算"：避免把"桶少"误读成"数据丢了"
    const head = `<div class="ts t2" style="margin-bottom:4px">${I18N.t('observability.slo_window', {
      start: String(d.window_start || '').substring(0, 10),
      end: String(d.window_end || '').substring(0, 10), n: d.buckets || 0 })}</div>
      <div class="ts t2" style="margin-bottom:8px">${I18N.t('observability.slo_bucket_hint', { n: d.buckets || 0 })}
        · ${_obsSloStateLabel(String(d.state || ''))}
        · ${I18N.t('observability.slo_target')} ${_v((d.target || 0) * 100, '%')}
        · ${I18N.t('observability.slo_budget_left')} ${_v(d.budget_remaining_pct, '%')}</div>`;
    if (!burn.length) {
      // 空燃尽：给明确说明，不给白框（引擎可用率这类"数据源刚启用"就会这样）
      b.innerHTML = head + `<div class="ts t2" style="padding:22px;text-align:center;`
        + `border:1px dashed var(--b);border-radius:6px">${I18N.t('observability.slo_burn_empty')}</div>`;
      return;
    }
    // 桶太少时折线看不见（symbol:'none' 下一个点不画任何东西）→ 少于 3 个桶时把点画出来
    const _few = burn.length < 3;
    b.innerHTML = head + `<div id="obsSloBurnChart" style="width:100%;height:280px"></div>`;
    _obsChart('obsSloBurnChart', {
      tooltip: { trigger: 'axis' },
      legend: { data: [I18N.t('observability.slo_ideal'), I18N.t('observability.slo_used')],
        textStyle: { fontSize: 11 } },
      grid: { left: 44, right: 20, top: 30, bottom: 28 },
      xAxis: { type: 'category', data: burn.map(p => String(p.bucket || '').substring(5, 16)),
        axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', max: 100, axisLabel: { formatter: '{value}%', fontSize: 10 } },
      series: [
        { name: I18N.t('observability.slo_ideal'), type: 'line', smooth: true,
          symbol: _few ? 'circle' : 'none', symbolSize: _few ? 7 : 0,
          data: burn.map(p => p.ideal_pct) },
        { name: I18N.t('observability.slo_used'), type: 'line', smooth: true,
          symbol: _few ? 'circle' : 'none', symbolSize: _few ? 7 : 0,
          data: burn.map(p => p.used_pct) },
      ],
    });
  }).catch(e => {
    const b = document.getElementById('obsSloModalBody');
    if (b) b.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">'
      + I18N.t('observability.load_failed', { msg: e.message }) + '</div>';
  });
}

// ===== Token 消耗卡片（引擎 / 任务 / 阶段事件 三级；P2 归因后端 /obs/tokens/*）=====
// 设备与引擎视图 → 按引擎；任务与日志视图 → 按任务 / 按阶段（一个 issue 走 vod→issue 就是两行）
let _obsTok = { key: '', at: 0, mode: 'engines', data: null };

function _obsTokScopeKey() {
  return (_obsSection === 'devices' ? 'devices' : 'tasks') + '|' + _obsSloRepoFull();
}

function obsTokMode(m) {
  _obsTok.mode = m;
  _obsTok.data = null;
  loadObsTokens(true);
}

function loadObsTokens(force) {
  const now = Date.now();
  const key = _obsTokScopeKey();
  if (!force && _obsTok.data && key === _obsTok.key && (now - _obsTok.at) < 60000) return;
  _obsTok.key = key;
  _obsTok.at = now;
  const full = _obsSloRepoFull();
  const repoQ = full ? '&repo=' + encodeURIComponent(full) : '';
  const mode = (_obsSection === 'devices') ? 'engines'
    : (_obsTok.mode === 'engines' ? 'tasks' : _obsTok.mode);
  _obsTok.mode = mode;   // 归一化：任务视图下不留残留的 engines 模式
  const url = mode === 'engines' ? '/obs/tokens/engines?range=24h' + repoQ
    : (mode === 'stages' ? '/obs/tokens/stages?range=24h' + repoQ
      : '/obs/tokens/tasks?range=24h&limit=20' + repoQ);
  API.get(url).then(d => {
    _obsTok.data = { mode: mode, payload: d || {} };
    _fillObsTokCard();
  }).catch(() => { /* 静默：不打扰视图其它内容 */ });
}

function _fillObsTokCard() {
  const box = document.getElementById('obsTokCard');
  if (box) box.innerHTML = _obsTokCardHtml();
}

function _obsTokNum(v) {
  const n = Number(v || 0);
  if (!isFinite(n)) return '—';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}

function _obsTokCardHtml() {
  const d = _obsTok.data;
  if (!d) return '';
  const p = d.payload || {};
  const t = p.total || {};
  const inDev = (_obsSection === 'devices');
  const btn = (m, label) => `<a onclick="obsTokMode('${m}')" style="cursor:pointer;padding:2px 10px;border-radius:10px;font-size:12px;${_obsTok.mode === m && !inDev ? 'background:var(--a);color:#fff' : 'color:var(--a)'}">${label}</a>`;
  const head = `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
      <span style="font-weight:600">${I18N.t('observability.tok_title')}</span>
      <span class="ts t3">${I18N.t('observability.tok_hint', { n: _obsTokNum(t.total) })}</span>
      <span style="flex:1"></span>
      ${inDev ? '' : btn('tasks', I18N.t('observability.tok_mode_tasks')) + btn('stages', I18N.t('observability.tok_mode_stages'))}
    </div>`;
  const th = (cols) => '<tr>' + cols.map(c => `<th>${c}</th>`).join('') + '</tr>';
  const row = (cells) => '<tr>' + cells.map((c, i) => `<td class="${i === 0 ? '' : 'ts t2'}">${c}</td>`).join('') + '</tr>';
  let body = '';
  if (d.mode === 'engines') {
    const list = p.engines || [];
    body = `<table class="tw"><thead>${th([I18N.t('observability.tok_col_engine'), '入', '出', '缓存读',
      I18N.t('observability.tok_col_total'), I18N.t('observability.tok_col_tasks')])}</thead><tbody>`
      + (list.length ? list.map(e => row([`${e.name || ('engine #' + e.engine_id)}`,
        _obsTokNum(e.input), _obsTokNum(e.output), _obsTokNum(e.cache_read),
        `<b>${_obsTokNum(e.total)}</b>`, e.tasks || 0])).join('')
        : `<tr><td colspan="6" class="ts t2" style="text-align:center;padding:14px">${I18N.t('observability.tok_empty')}</td></tr>`)
      + '</tbody></table>';
  } else if (d.mode === 'stages') {
    const list = p.stages || [];
    body = `<table class="tw"><thead>${th([I18N.t('observability.tok_col_issue'),
      I18N.t('observability.tok_col_stage'), I18N.t('observability.tok_col_total'),
      I18N.t('observability.tok_col_tasks')])}</thead><tbody>`
      + (list.length ? list.slice(0, 30).map(s => row([s.issue_id || I18N.t('observability.tok_no_issue'),
        _obsTOr('observability.agent_type.' + s.stage, s.stage || '—'),
        `<b>${_obsTokNum(s.total)}</b>`, s.tasks || 0])).join('')
        : `<tr><td colspan="4" class="ts t2" style="text-align:center;padding:14px">${I18N.t('observability.tok_empty')}</td></tr>`)
      + '</tbody></table>';
  } else {
    const list = p.tasks || [];
    body = `<table class="tw"><thead>${th([I18N.t('observability.tok_col_task'),
      I18N.t('observability.tok_col_stage'), I18N.t('observability.tok_col_total')])}</thead><tbody>`
      + (list.length ? list.map(x => row([`<a style="cursor:pointer;color:var(--a)" onclick="openObsTimeline('${x.task_id}')">${x.task_id}</a>`
        + (x.issue_id ? ' <span class="ts t3">' + x.issue_id + '</span>' : ''),
        _obsTOr('observability.agent_type.' + x.stage, x.stage || '—'), `<b>${_obsTokNum(x.total)}</b>`])).join('')
        : `<tr><td colspan="3" class="ts t2" style="text-align:center;padding:14px">${I18N.t('observability.tok_empty')}</td></tr>`)
      + '</tbody></table>';
  }
  return `<div class="cd" style="padding:10px 14px;margin-bottom:12px">${head}${body}</div>`;
}

function renderOverview(ops, rt) {
  const r = ops.runtimes || {};
  const w = ops.workers || {};

  // Worker 卡片（带 id 供局部更新）— 按 device_name 排序保证位置稳定，分页渲染
  const workers = _sortedWorkers(w);
  const _wkTotalPages = Math.max(1, Math.ceil(workers.length / _workerPageSize));
  if (_workerPage >= _wkTotalPages) _workerPage = 0;
  const _wkStart = _workerPage * _workerPageSize;
  const _pageWorkers = workers.slice(_wkStart, _wkStart + _workerPageSize);
  let workerCards = workers.length === 0
    ? '<div class="ts t2" style="padding:12px">' + I18N.t('observability.no_remote_worker') + '</div>'
    : _pageWorkers.map(_workerCardHTML).join('');

  // Runtime 表格（行带 id，各 td 带 id 供局部更新）
  const runtimes = _sortedRuntimes(r);
  const _devices = [...new Set(runtimes.map(rt => rt.device_name || '-').filter(d => d && d !== '-'))];
  const _filteredRuntimes = _filterRuntimes(runtimes);
  const _rtTotalPages = Math.max(1, Math.ceil(_filteredRuntimes.length / _runtimePageSize));
  if (_runtimePage >= _rtTotalPages) _runtimePage = 0;
  const _rtStart = _runtimePage * _runtimePageSize;
  const _pageRuntimes = _filteredRuntimes.slice(_rtStart, _rtStart + _runtimePageSize);
  let runtimeTable = '<div class="ts t2" style="padding:12px">' + I18N.t('observability.no_engine') + '</div>';
  if (runtimes.length) {
    let _filterBar = '<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap">';
    _filterBar += '<input type="text" placeholder="' + I18N.t('observability.search_engine_placeholder') + '" value="' + _rtSearch.replace(/"/g,'&quot;') + '" oninput="_rtSearch=this.value;_runtimePage=0;_rerenderRuntimeTable()" style="flex:1;min-width:200px;padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c)">';
    _filterBar += '<select onchange="_rtDeviceFilter=this.value;_runtimePage=0;_rerenderRuntimeTable()" style="padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c)">';
    _filterBar += '<option value="">' + I18N.t('observability.all_devices') + '</option>';
    _devices.forEach(d => {
      _filterBar += '<option value="' + d + '"' + (_rtDeviceFilter === d ? ' selected' : '') + '>' + d + '</option>';
    });
    _filterBar += '</select>';
    _filterBar += '<span class="ts t3" id="obs-runtime-count">' + _filteredRuntimes.length + '/' + runtimes.length + '</span>';
    _filterBar += '</div>';
    runtimeTable = _filterBar;
    runtimeTable += '<div id="obs-runtime-rows">';
    if (_pageRuntimes.length) {
      runtimeTable += '<div class="tw"><table><thead><tr><th>' + I18N.t('observability.col_engine') + '</th><th>' + I18N.t('observability.col_version') + '</th><th>' + I18N.t('observability.col_health') + '</th><th>健康分</th><th>' + I18N.t('observability.col_busy') + '</th><th>' + I18N.t('observability.col_avg_duration') + '</th><th>' + I18N.t('observability.col_token') + '</th><th>' + I18N.t('observability.col_probe') + '</th><th>' + I18N.t('observability.col_mode') + '</th><th>' + I18N.t('observability.col_device') + '</th></tr></thead><tbody>';
      runtimeTable += _pageRuntimes.map(_runtimeRowHTML).join('');
      runtimeTable += '</tbody></table></div>';
    } else {
      runtimeTable += '<div class="ts t2" style="padding:12px">' + I18N.t('observability.no_matching_engine') + '</div>';
    }
    runtimeTable += '</div>';
    runtimeTable += '<div id="rt-pager">' + (_filteredRuntimes.length > 0 ? _paginationWithPageSizeHTML(_runtimePage, _rtTotalPages, '_runtimePage--;_rerenderRuntimeTable()', '_runtimePage++;_rerenderRuntimeTable()', _runtimePageSize, '_runtimePageSize', '_runtimePage=0;_rerenderRuntimeTable()') : '') + '</div>';
  }

  return `

    ${section(I18N.t('observability.worker_monitor'), I18N.t('observability.online_count', {count: (w.online || 0) + '/' + (w.total || 0)}))}
    ${_kpiRowHTML([
      { label: I18N.t('observability.worker_total'), value: w.total || 0, color: 'var(--a)', id: 'kpi-w-total' },
      { label: I18N.t('observability.online'), value: w.online || 0, color: 'var(--g)', id: 'kpi-w-online' },
      { label: I18N.t('observability.offline'), value: w.offline || 0, color: 'var(--t3)', id: 'kpi-w-offline' },
      { label: I18N.t('observability.engine_total'), value: r.total || 0, color: 'var(--pu)', id: 'kpi-r-total' },
    ])}
    <div id="wk-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-bottom:16px">
      ${workerCards}
    </div>
    <div id="wk-pager">${workers.length > 0 ? _paginationWithPageSizeHTML(_workerPage, _wkTotalPages, '_workerPage--;_rerenderWorkerGrid()', '_workerPage++;_rerenderWorkerGrid()', _workerPageSize, '_workerPageSize', '_workerPage=0;_rerenderWorkerGrid()') : ''}</div>
    ${section(I18N.t('observability.runtime_engine'), I18N.t('observability.runtime_subtitle', {online: (r.online || 0), total: (r.total || 0)}))}
    <div id="obs-runtime-table">${runtimeTable}</div>
  `;
}

// ===== 局部更新：概览 Tab =====
// 返回 true=成功局部更新, false=元素缺失需回退到完整渲染
function updateOverview(ops, rt) {
  const r = ops.runtimes || {};
  const w = ops.workers || {};

  // 1) 更新 Worker KPI 卡片数值
  _setText('kpi-w-total', w.total || 0);
  _setText('kpi-w-online', w.online || 0);
  _setText('kpi-w-offline', w.offline || 0);
  _setText('kpi-r-total', r.total || 0);

  // 2) 更新 Worker 卡片（仅当前页元素，避免分页后找不到未渲染的卡片）
  const workers = _sortedWorkers(w);
  const _wkTotalPages = Math.max(1, Math.ceil(workers.length / _workerPageSize));
  if (_workerPage >= _wkTotalPages) { _workerPage = 0; return false; }
  const _wkStart = _workerPage * _workerPageSize;
  const _pageWorkers = workers.slice(_wkStart, _wkStart + _workerPageSize);
  for (const wk of _pageWorkers) {
    const wid = wk.id != null ? wk.id : (wk.device_name || wk.name || '').replace(/[^a-zA-Z0-9_-]/g, '_');
    const card = document.getElementById('wk-' + wid);
    if (!card) return false;

    const dot = _OBS_HEALTH_DOT[wk.health] || '⚫';
    const hc = _OBS_HEALTH_COLOR[wk.health] || 'var(--t3)';

    const dotEl = document.getElementById('wk-' + wid + '-dot');
    if (dotEl) dotEl.textContent = dot;

    const healthEl = document.getElementById('wk-' + wid + '-health');
    if (healthEl) { healthEl.textContent = I18N.t("observability." + (wk.health || "offline")); healthEl.style.color = hc; }

    const seenEl = document.getElementById('wk-' + wid + '-seen');
    if (seenEl) seenEl.textContent = fmtLocalTime(wk.last_seen_at || '').substring(11, 19);

    _updateBar('wk-' + wid + '-cpu', wk.cpu_percent);
    _updateBar('wk-' + wid + '-mem', wk.memory_percent);
    _updateBar('wk-' + wid + '-tmp', wk.tmp_percent);
    _updateBar('wk-' + wid + '-disk', wk.disk_percent);
    _updateBar('wk-' + wid + '-bw', wk.bandwidth_percent, (wk.bandwidth_mbps||0).toFixed(1) + ' Mbps');
  }
  // 同步 Worker 分页控件（总页数可能变化）
  const wkPager = document.getElementById('wk-pager');
  if (wkPager) wkPager.innerHTML = workers.length > 0 ? _paginationWithPageSizeHTML(_workerPage, _wkTotalPages, '_workerPage--;_rerenderWorkerGrid()', '_workerPage++;_rerenderWorkerGrid()', _workerPageSize, '_workerPageSize', '_workerPage=0;_rerenderWorkerGrid()') : '';

  // 3) 更新 Runtime 表格行（仅当前页元素）
  const runtimes = _sortedRuntimes(r);
  const _filteredRt = _filterRuntimes(runtimes);
  const _rtTotalPages = Math.max(1, Math.ceil(_filteredRt.length / _runtimePageSize));
  if (_runtimePage >= _rtTotalPages) { _runtimePage = 0; return false; }
  const _rtStart = _runtimePage * _runtimePageSize;
  const _pageRt = _filteredRt.slice(_rtStart, _rtStart + _runtimePageSize);
  for (const rti of _pageRt) {
    const rid = rti.id;
    const tr = document.getElementById('rt-' + rid);
    if (!tr) return false;

    const hc = _OBS_HEALTH_COLOR[rti.health] || 'var(--t3)';
    const running = rti.running || 0;
    const maxC = rti.max_concurrent || 0;
    const loadColor = running >= maxC * 2 ? 'var(--r)' : running >= maxC ? 'var(--y)' : running > 0 ? 'var(--g)' : 'var(--t3)';
    const avgDur = rti.avg_duration || 0;
    const avgDurStr = avgDur > 60 ? Math.round(avgDur/60) + 'm' + Math.round(avgDur%60) + 's' : avgDur + 's';
    const avgDurColor = avgDur > 300 ? 'var(--r)' : avgDur > 60 ? 'var(--y)' : 'var(--g)';

    const healthEl = document.getElementById('rt-' + rid + '-health');
    if (healthEl) { healthEl.textContent = I18N.t("observability." + (rti.health || "offline")); healthEl.style.color = hc; }

    const scoreEl = document.getElementById('rt-' + rid + '-score');
    if (scoreEl) { scoreEl.innerHTML = _obsHealthScoreHTML(rti); }

    const loadEl = document.getElementById('rt-' + rid + '-load');
    if (loadEl) { loadEl.textContent = running + '/' + maxC; loadEl.style.color = loadColor; }

    const durEl = document.getElementById('rt-' + rid + '-dur');
    if (durEl) { durEl.textContent = avgDur ? avgDurStr : '-'; durEl.style.color = avgDurColor; }

    const versionEl = document.getElementById('rt-' + rid + '-version');
    if (versionEl) versionEl.textContent = rti.version || '-';

    const deviceEl = document.getElementById('rt-' + rid + '-device');
    if (deviceEl) deviceEl.textContent = rti.device_name || '-';
  }
  // 同步 Runtime 分页控件 + 计数
  const countEl = document.getElementById('obs-runtime-count');
  if (countEl) countEl.textContent = _filteredRt.length + '/' + runtimes.length;
  const rtPager = document.getElementById('rt-pager');
  if (rtPager) rtPager.innerHTML = _filteredRt.length > 0 ? _paginationWithPageSizeHTML(_runtimePage, _rtTotalPages, '_runtimePage--;_rerenderRuntimeTable()', '_runtimePage++;_rerenderRuntimeTable()', _runtimePageSize, '_runtimePageSize', '_runtimePage=0;_rerenderRuntimeTable()') : '';

  return true;
}

// ===== 局部更新：任务 Tab =====
// 返回 true=成功局部更新, false=需回退到完整渲染
function updateTasks(ops, rt) {
  const t = ops.tasks || {};
  const _is = t.issue_stats || {};

  _setText('kpi-t-total', _is.total || 0);
  _setText('kpi-t-completed', _is.completed || 0);
  _setText('kpi-t-inprogress', _is.in_progress || 0);
  _setText('kpi-t-failed', _is.failed || 0);

  const failedEl = document.getElementById('kpi-t-failed');
  if (failedEl) failedEl.style.color = (_is.failed || 0) > 0 ? 'var(--r)' : 'var(--t3)';

  return true;
}

// ===== 局部更新辅助函数 =====
function _setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function _updateBar(idPrefix, pct, suffix) {
  const bar = document.getElementById(idPrefix + '-bar');
  const val = document.getElementById(idPrefix + '-val');
  if (bar) bar.style.width = Math.min(pct, 100) + '%';
  const c = pct > 80 ? 'var(--r)' : pct > 60 ? 'var(--y)' : 'var(--g)';
  if (bar) bar.style.background = c;
  if (val) { val.textContent = suffix || (Math.round(pct) + '%'); val.style.color = c; }
}

// A2A 健康检查（概览 tab 内异步加载）
function loadA2aHealth() {
  const container = document.getElementById('obs-a2a-health');
  if (!container) return;
  container.innerHTML = section(I18N.t('observability.a2a_health'), I18N.t('observability.a2a_health_subtitle')) + '<div class="loading">' + I18N.t('observability.checking') + '</div>';
  fetch('/api/dashboard/ops/a2a-health', {signal: AbortSignal.timeout(10000)}).then(r => r.json()).then(d => {
    const status = d.reachable
      ? `<span style="color:var(--g)">${I18N.t('observability.a2a_reachable')}</span> (${d.latency_ms}ms)`
      : `<span style="color:var(--r)">${I18N.t('observability.a2a_unreachable')}</span> ${d.error ? '<span class="ts t3">'+d.error.substring(0,80)+'</span>' : ''}`;
    let html = section(I18N.t('observability.a2a_health'), I18N.t('observability.a2a_health_subtitle'));
    html += kpiRow([
      { label: I18N.t('observability.server_url'), value: `<span class="ts" style="font-size:10px">${d.server_base_url}</span>`, color: 'var(--a)' },
      { label: I18N.t('observability.col_status'), value: status, color: d.reachable ? 'var(--g)' : 'var(--r)' },
      { label: I18N.t('observability.col_latency'), value: d.latency_ms ? d.latency_ms + 'ms' : '-', color: 'var(--sk)' },
    ]);
    if (d.recent_a2a && d.recent_a2a.length) {
      html += table(['Seq', 'Type', 'Repo', 'Issue', 'Status', 'Time'],
        d.recent_a2a.map(a => [
          a.seq, a.event_type.substring(0,20), (a.repo_key||'').replace('gitcode:','').substring(0,15),
          a.issue_id || '-', a.status || '-', `<span class="ts t3">${fmtLocalTime(a.created_at)}</span>`
        ])
      );
    }
    container.innerHTML = html;
  }).catch(e => {
    container.innerHTML = section(I18N.t('observability.a2a_health')) + '<div class="error">' + I18N.t('observability.load_failed', {msg: e.message}) + '</div>';
  });
}

// ===== Tab: 异常任务 =====
let _anomalyPage = 0;
let anomalyPageSize = 10;

function renderAnomalyTab(container, ops, rerender) {
  // `rerender`：分页/每页条数的重渲染表达式。页面内用 refreshObs()；**弹窗内必须传
  // `_obsAnomalyRerender()`**，否则点分页会重画整页而弹窗内容不动（2026-10-18）。
  rerender = rerender || 'refreshObs()';
  const stuck = (ops.tasks || {}).stuck_tasks || [];
  const failed = (ops.tasks || {}).failed_tasks || [];
  if (!stuck.length && !failed.length) {
    container.innerHTML = '<div class="ts t2" style="padding:40px;text-align:center">' + I18N.t('observability.no_anomaly') + '</div>';
    return;
  }

  const _enc = s => encodeURIComponent(JSON.stringify({
    issue:s.issue_id, seq:s.event_seq, repo:(s.repo_key||'').replace('gitcode:',''),
    created:s.completed_at||s.started_at||'',
    errors:s.errors||[], exit_code:s.exit_code, result_text:s.result_text||''
  }));
  const _row = (s, showFailed) => {
    const is = (s.issue_id && s.event_seq) ? `${s.issue_id} #${s.event_seq}` : s.id.substring(0,12);
    const time = showFailed ? fmtLocalTime(s.completed_at||s.started_at||'') : fmtLocalTime(s.started_at||'');
    let badge;
    if (showFailed) {
      const errs = s.errors || [];
      const errText = s.result_text || (errs.length ? errs[0] : '');
      const exitLabel = s.exit_code != null ? ` [exit:${s.exit_code}]` : '';
      badge = `<span class="ts t3" style="color:var(--r)">${(errText||'').substring(0,80)}${exitLabel}</span>`;
    } else {
      badge = `<span style="color:var(--r)">🔴 STUCK</span>`;
    }
    return [`<a onclick="openTaskLogModal('${s.id}','${_enc(s)}')" style="cursor:pointer;color:var(--a);text-decoration:underline"><b>${is}</b></a>`,
      s.agent_type || '-', (s.repo_key||'').replace('gitcode:','').substring(0,20),
      `<span class="ts t3">${time.substring(0,19)}</span>`, badge];
  };

  let html = '';

  // Stuck 任务（通常很少，不分页）
  if (stuck.length) {
    html += section(I18N.t('observability.stuck_tasks'), I18N.t('observability.stuck_tasks_subtitle', {count: stuck.length}));
    html += table(['Issue/Seq', 'Agent', 'Repo', 'Started', I18N.t('observability.col_status')],
      stuck.map(s => _row(s, false))
    );
  }

  // Failed 任务（分页）
  if (failed.length) {
    const totalPages = Math.ceil(failed.length / anomalyPageSize);
    if (_anomalyPage >= totalPages) _anomalyPage = 0;
    const start = _anomalyPage * anomalyPageSize;
    const pageItems = failed.slice(start, start + anomalyPageSize);

    html += section(I18N.t('observability.failed_tasks'), I18N.t('observability.failed_tasks_subtitle', {total: failed.length, page: (_anomalyPage+1), pages: totalPages}));
    html += table([I18N.t('observability.col_issue_seq'), I18N.t('observability.col_agent'), I18N.t('observability.col_repo'), I18N.t('observability.col_failed_short'), I18N.t('observability.col_result')],
      pageItems.map(s => _row(s, true))
    );

    // 分页栏（含每页条数选择器）
    html += `<div style="display:flex;align-items:center;justify-content:center;gap:10px;padding:12px 0;flex-wrap:wrap">
      ${totalPages > 1 ? `<button class="bt bt-o bt-s" ${_anomalyPage === 0 ? 'disabled style="opacity:.4"' : ''} onclick="_anomalyPage--;${rerender}" >${I18N.t('observability.prev_page')}</button>` : ''}
      <span class="ts t3">${_anomalyPage+1} / ${totalPages}</span>
      ${totalPages > 1 ? `<button class="bt bt-o bt-s" ${_anomalyPage >= totalPages-1 ? 'disabled style="opacity:.4"' : ''} onclick="_anomalyPage++;${rerender}">${I18N.t('observability.next_page')}</button>` : ''}
      <label class="ts t3" style="display:flex;align-items:center;gap:4px;margin-left:8px">${I18N.t('observability.rows_per_page')}
        <select onchange="anomalyPageSize=parseInt(this.value)||10;_anomalyPage=0;${rerender}" style="padding:4px 8px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c)">
          <option value="10"${anomalyPageSize===10?' selected':''}>10</option>
          <option value="20"${anomalyPageSize===20?' selected':''}>20</option>
          <option value="50"${anomalyPageSize===50?' selected':''}>50</option>
        </select>
      </label>
    </div>`;
  }

  container.innerHTML = html;
}

function loadErrorLogs(container) {
  if (!container) return;
  let currentLevel = _errorLogLevel || '';
  let currentSource = _errorLogSource || '';
  container.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;flex-wrap:wrap">
      <div style="display:flex;gap:0">
        <button class="bt bt-s ${currentLevel===''?'bt-p':'bt-o'}" onclick="_errorLogLevel='';loadErrorLogs(document.getElementById('obsContent'))">${I18N.t('observability.log_all')}</button>
        <button class="bt bt-s ${currentLevel==='ERROR'?'bt-p':'bt-o'}" onclick="_errorLogLevel='ERROR';loadErrorLogs(document.getElementById('obsContent'))">${I18N.t('observability.log_only_error')}</button>
        <button class="bt bt-s ${currentLevel==='WARNING'?'bt-p':'bt-o'}" onclick="_errorLogLevel='WARNING';loadErrorLogs(document.getElementById('obsContent'))">${I18N.t('observability.log_only_warning')}</button>
      </div>
      <select id="errorLogSource" class="bt bt-s bt-o" style="padding:4px 8px" onchange="_errorLogSource=this.value;loadErrorLogs(document.getElementById('obsContent'))">
        <option value="">${I18N.t('observability.log_all_sources')}</option>
      </select>
      <input id="errorLogQ" class="ip ip-s" style="max-width:200px;padding:4px 8px" placeholder="${I18N.t('observability.log_search_placeholder')}"
             onkeydown="if(event.key==='Enter'){_errorLogQ=this.value;loadErrorLogs(document.getElementById('obsContent'))}">
      <button class="bt bt-o bt-s" style="white-space:nowrap;padding:4px 12px" onclick="_errorLogQ=(document.getElementById('errorLogQ')||{}).value||'';loadErrorLogs(document.getElementById('obsContent'))">${I18N.t('observability.search')}</button>
      <span class="ts t3" id="errorLogCount"></span>
      ${_errorLogTask ? `<span class="ts t2" style="background:rgba(74,127,209,.12);padding:2px 8px;border-radius:10px">${I18N.t('observability.log_task_filter')}: ${_errorLogTask} <a style="cursor:pointer;color:var(--a)" onclick="_errorLogTask='';loadErrorLogs(document.getElementById('obsContent'))">✕</a></span>` : ''}
    </div>
    <div class="ts t3" style="margin-bottom:10px">${I18N.t('observability.log_scope_hint')}</div>
    <div id="errorLogBody"><div class="loading">${I18N.t('observability.loading')}</div></div>
  `;
  // 来源列表从 obs_log 取（跨机聚合后的实际来源）
  fetch('/api/obs/logs/sources', {signal: AbortSignal.timeout(10000)}).then(r => r.json()).then(d => {
    const sel = document.getElementById('errorLogSource');
    if (sel) {
      (d.sources || []).forEach(s => {
        const opt = document.createElement('option');
        opt.value = s; opt.textContent = s;
        if (currentSource === s) opt.selected = true;
        sel.appendChild(opt);
      });
    }
  }).catch(() => {});
  const q = _errorLogQ || '';
  const qp = ['limit=200'];
  if (currentLevel) qp.push('level=' + currentLevel);
  if (currentSource) qp.push('source=' + encodeURIComponent(currentSource));
  if (q) qp.push('q=' + encodeURIComponent(q));
  if (_errorLogTask) qp.push('task_id=' + encodeURIComponent(_errorLogTask));
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  if (repo && repo.fullName) qp.push('repo=' + encodeURIComponent(repo.fullName));
  // 主数据源：主控 MySQL obs_log（跨机、按可见范围过滤）
  // 降级：查不到/失败时回落到文件扫描（§6.3：文件日志作为兜底归档）
  API.get('/obs/logs?' + qp.join('&')).then(d => {
    const items = d.items || [];
    const countEl = document.getElementById('errorLogCount');
    if (countEl) countEl.textContent = I18N.t('observability.log_count_db', {count: items.length, total: d.total || 0});
    const bodyEl = document.getElementById('errorLogBody');
    if (!bodyEl) return;
    if (!items.length) {
      bodyEl.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_matching_log') + '</div>';
      return;
    }
    bodyEl.innerHTML = _errorLogHtmlDb(items);
  }).catch(() => {
    // 降级到文件扫描（旧实现）
    _loadErrorLogsFileFallback(currentLevel, currentSource);
  });
}

// obs_log 渲染：短 ts + 来源 + 组件 + 仓库/任务上下文 + 级别 + 正文
function _errorLogHtmlDb(items) {
  let html = '<div style="background:#0c1222;color:#e0e6f0;font-family:Menlo,Monaco,Consolas,monospace;font-size:11px;line-height:1.5;border-radius:8px;overflow:hidden;max-height:70vh;overflow-y:auto">';
  items.forEach(e => {
    const level = e.level || '';
    const color = level === 'ERROR' || level === 'CRITICAL' ? '#dc2828' : '#d49c0f';
    const time = String(e.ts || '').replace('T', ' ').substring(0, 19);
    const srcTag = e.source ? '<span style="color:#5b9eff">[' + e.source + ']</span> ' : '';
    const comp = e.component ? '<span style="color:#8d97a8">' + e.component + '</span> ' : '';
    // 仓库/任务用短名（不显示 raw 前缀），点击可按该仓库过滤（用户偏好：入口放详情页内）
    const rk = e.repo_key ? String(e.repo_key).split(':').slice(1).join(':') : '';
    const ctx = (rk ? '<span style="color:#7f8ea3" title="' + e.repo_key + '">' + rk + '</span> ' : '')
      + (e.task_id ? '<span style="color:#7f8ea3">' + e.task_id + '</span> ' : '');
    html += '<div style="padding:4px 12px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#5b667b">' + time + '</span> ' + srcTag + comp + ctx + '<span style="color:' + color + '"><b>' + level + '</b></span> <span style="color:#e0e6f0">' + _escHtml(e.msg || '') + '</span></div>';
  });
  return html + '</div>';
}

function _escHtml(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// 文件扫描降级（保留旧实现，obs_log 不可用时仍能排障）
function _loadErrorLogsFileFallback(currentLevel, currentSource) {
  const levelParam = currentLevel ? '&level=' + currentLevel : '';
  const sourceParam = currentSource ? '&source=' + encodeURIComponent(currentSource) : '';
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '&repo=' + encodeURIComponent(repo.fullName) : '';
  fetch('/api/dashboard/ops/error-logs?limit=100' + levelParam + sourceParam + repoParam, {signal: AbortSignal.timeout(15000)}).then(r => r.json()).then(d => {
    const entries = d.entries || [];
    const countEl = document.getElementById('errorLogCount');
    const bodyEl = document.getElementById('errorLogBody');
    if (countEl) countEl.textContent = I18N.t('observability.log_count', {count: entries.length, files: (d.files ? d.files.length : 1)}) + ' ' + I18N.t('observability.log_file_fallback');
    if (!entries.length) {
      if (bodyEl) bodyEl.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_matching_log') + '</div>';
      return;
    }
    let html = '<div style="background:#0c1222;color:#e0e6f0;font-family:Menlo,Monaco,Consolas,monospace;font-size:11px;line-height:1.5;border-radius:8px;overflow:hidden;max-height:70vh;overflow-y:auto">';
    entries.forEach(e => {
      const level = e.level || '';
      const color = level === 'ERROR' ? '#dc2828' : '#d49c0f';
      const time = (e.ts || '').substring(0,19);
      const src = e.source || I18N.t('observability.source_master');
      const srcTag = src !== I18N.t('observability.source_master') ? '<span style="color:#5b9eff">[' + src + ']</span> ' : '';
      const rid = e.request_id && e.request_id !== '-' ? ' [' + e.request_id + ']' : '';
      const extra = e.method ? ' ' + e.method + ' ' + (e.path||'') + ' ' + (e.status||'') + ' ' + (e.duration_ms||0) + 'ms' : '';
      html += '<div style="padding:4px 12px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#5b667b">' + time + '</span>' + srcTag + rid + ' <span style="color:' + color + '"><b>' + level + '</b></span> <span style="color:#e0e6f0">' + _escHtml((e.message||'').substring(0,300)) + '</span><span style="color:#8d97a8">' + extra + '</span></div>';
    });
    html += '</div>';
    if (bodyEl) bodyEl.innerHTML = html;
  }).catch(e => {
    const bodyEl = document.getElementById('errorLogBody');
    if (bodyEl) bodyEl.innerHTML = '<div class="error">' + I18N.t('observability.load_failed', {msg: e.message}) + '</div>';
  });
}
// ===== 追踪时间线（§6.4）：横向时间轴 + 可展开步骤 + 失败高亮 + 与日志互跳 =====
function openObsTimeline(traceOrTaskId) {
  const old = document.getElementById('obsTraceModal');
  if (old) old.remove();
  const modal = document.createElement('div');
  modal.id = 'obsTraceModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1001';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = '<div class="cd" style="width:900px;max-width:95vw;max-height:88vh;display:flex;flex-direction:column;padding:0;overflow:hidden">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">' +
    '<h3 style="font-size:13px;font-weight:600">' + I18N.t('observability.timeline_title') + '</h3>' +
    '<div style="display:flex;align-items:center;gap:8px"><span id="obsTraceMeta" class="ts t3"></span>' +
    '<button class="bt bt-o bt-s" style="white-space:nowrap;padding:4px 12px" onclick="document.getElementById(\'obsTraceModal\').remove()">' + I18N.t('observability.close') + '</button></div>' +
    '</div>' +
    '<div id="obsTraceBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">' + I18N.t('observability.loading') + '</div></div>' +
    '</div>';
  document.body.appendChild(modal);
  API.get('/obs/trace/' + encodeURIComponent(traceOrTaskId)).then(d => {
    _renderObsTrace(d || {});
  }).catch(e => {
    const b = document.getElementById('obsTraceBody');
    if (b) b.innerHTML = '<div class="error">' + I18N.t('observability.load_failed', { msg: (e && e.message) || e }) + '</div>';
  });
}

function _fmtDur(v) {
  if (v === null || v === undefined) return '—';
  return v >= 60 ? (Math.floor(v / 60) + 'm' + Math.round(v % 60) + 's') : (Math.round(v * 10) / 10 + 's');
}

function _renderObsTrace(d) {
  const body = document.getElementById('obsTraceBody');
  if (!body) return;
  const tasks = d.tasks || [];
  const meta = document.getElementById('obsTraceMeta');
  if (meta) {
    const rk = d.repo_key ? String(d.repo_key).split(':').slice(1).join(':') : '';
    meta.textContent = [rk, d.issue_id, (d.kind === 'pipeline' ? I18N.t('observability.timeline_pipeline') : '')]
      .filter(Boolean).join(' · ');
  }
  if (!tasks.length) {
    body.innerHTML = '<div class="ts t2" style="padding:24px;text-align:center">' + I18N.t('observability.timeline_empty') + '</div>';
    return;
  }
  let html = '';
  tasks.forEach(t => {
    const bad = t.status === 'failed' || t.status === 'cancelled';
    const color = bad ? '#d64545' : (t.status === 'completed' ? '#22a06b' : '#e0a02a');
    const tk = t.tokens || {};
    html += '<div class="cd" style="padding:12px 14px;margin-bottom:12px;border-left:3px solid ' + color + '">';
    // 头部：agent 类型 + 引擎 + 状态 + 时间
    html += '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
      '<b style="font-size:13px">' + (t.agent_type || '-') + '</b>' +
      '<span class="ts t3">' + (t.engine || ('#' + t.runtime_id)) + '</span>' +
      '<span class="ts" style="color:' + color + '">' + (t.status || '') + '</span>' +
      '<span class="ts t3">' + I18N.t('observability.timeline_queue_wait') + ' ' + _fmtDur(t.queue_wait_s) +
      ' · ' + I18N.t('observability.timeline_duration') + ' ' + _fmtDur(t.duration_s) + '</span>' +
      (tk.total ? '<span class="ts t3">tokens ' + (tk.total).toLocaleString() + '</span>' : '') +
      '<a style="cursor:pointer;color:var(--a);margin-left:auto;white-space:nowrap" onclick="openTaskLogModal(\'' + t.id + '\',\'\')">' + I18N.t('observability.view_logs') + '</a>' +
      '</div>';
    // 轨道：enqueue → dispatch → terminal
    html += '<div class="ts t3" style="margin:6px 0">' +
      (t.enqueue_at || '').replace('T', ' ').substring(0, 19) + ' → ' +
      (t.dispatch_at || '').replace('T', ' ').substring(0, 19) + ' → ' +
      (t.terminal_at || '…').replace('T', ' ').substring(0, 19) +
      '</div>';
    if (bad && t.failure_reason) {
      html += '<div style="color:#d64545;font-size:12px;margin-bottom:6px">' + I18N.t('observability.timeline_failed') + ': ' + _escHtml(t.failure_reason) + '</div>';
    }
    // 步骤
    const steps = t.steps || [];
    if (!steps.length) {
      html += '<div class="ts t3">' + I18N.t('observability.timeline_no_steps') + '</div>';
    } else {
      html += '<details style="margin-top:4px"><summary style="cursor:pointer;font-size:12px" class="ts t2">' +
        I18N.t('observability.timeline_steps') + ' (' + steps.length + ')</summary>' +
        '<div style="margin-top:6px;background:#0c1222;color:#e0e6f0;border-radius:6px;padding:6px 0;font-family:Menlo,Monaco,Consolas,monospace;font-size:11px;max-height:260px;overflow-y:auto">';
      steps.forEach(s => {
        const c = s.type === 'error' ? '#dc2828' : (s.type === 'tool_use' ? '#5b9eff' : '#8d97a8');
        const extra = s.tool ? ' ' + s.tool : '';
        const io = (s.input ? ' ▸ ' + _escHtml(s.input) : '') + (s.output ? ' ⟵ ' + _escHtml(s.output) : '');
        html += '<div style="padding:3px 10px"><span style="color:#5b667b">#' + s.seq + '</span> ' +
          '<span style="color:' + c + '">' + s.type + extra + '</span> ' +
          '<span style="color:#c8d0dc">' + _escHtml(s.content || s.output || '') + '</span>' +
          '<span style="color:#6b7686">' + io + '</span></div>';
      });
      html += '</div></details>';
    }
    // 内联日志命中
    const hits = t.log_hits || [];
    if (hits.length) {
      html += '<div style="margin-top:6px">';
      hits.forEach(h => {
        html += '<div class="ts t3" style="color:' + (h.level === 'ERROR' ? '#d64545' : '#e0a02a') + '">' +
          (h.ts || '').substring(0, 19) + ' [' + (h.level || '') + '] ' + _escHtml((h.msg || '').substring(0, 160)) + '</div>';
      });
      html += '</div>';
    }
    html += '</div>';
  });
  body.innerHTML = html;
}

// 从时间线跳到日志 tab（按任务过滤；§6.4「与日志互跳」）
function jumpObsLogsForTask(taskId) {
  _errorLogTask = taskId;
  const m = document.getElementById('obsTraceModal');
  if (m) m.remove();
  renderObservability('inspect');   // 先渲染分区（会把默认子 Tab 重置为 tasks）
  switchObsTab('errorlogs');        // 再切到日志子 Tab（清缓存重渲染，带上任务过滤）
}

let _errorLogLevel = '';
let _errorLogSource = '';
let _errorLogQ = '';
let _errorLogTask = '';

// ===== Tab: 任务 =====
function renderTasksTab(ops, rt) {
  const t = ops.tasks || {};
  const sc = t.status_counts || {};
  let html = _obsTaskAnomalyBarHtml();   // 异常任务入口（按钮→弹窗）
  html += section(I18N.t('observability.task_status_dist'));
  const _is = t.issue_stats || {};
  html += _kpiRowHTML([
    { label: I18N.t('observability.task_total'), value: _is.total || 0, color: 'var(--a)', id: 'kpi-t-total' },
    { label: I18N.t('observability.completed'), value: _is.completed || 0, color: 'var(--g)', id: 'kpi-t-completed' },
    { label: I18N.t('observability.in_progress'), value: _is.in_progress || 0, color: 'var(--pu)', id: 'kpi-t-inprogress' },
    { label: I18N.t('observability.failed'), value: _is.failed || 0, color: (_is.failed||0) > 0 ? 'var(--r)' : 'var(--t3)', id: 'kpi-t-failed' },
  ]);
  html += '<div id="obs-timeline-container"><div class="loading">' + I18N.t('observability.loading_tasks') + '</div></div>';
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoFullName = repo && repo.fullName ? repo.fullName : '';
  // 使用缓存的 timeline 数据（同仓库且 30s 内）
  if (_timelineCache && _timelineCache.repo === repoFullName && (Date.now() - _timelineCache.ts) < 30000) {
    setTimeout(() => renderTimelineList(document.getElementById('obs-timeline-container'), _timelineCache.issues, _timelineCache.repoKey), 0);
  } else {
    const repoParam = repoFullName ? '?repo=' + encodeURIComponent(repoFullName) : '';
    fetch('/api/dashboard/ops/timeline' + repoParam, {signal: AbortSignal.timeout(10000)}).then(r => r.json()).then(d => {
      _timelineCache = { repo: repoFullName, ts: Date.now(), issues: d.issues || [], repoKey: d.repoKey || '' };
      _timelineIssues = d.issues || [];
      renderTimelineList(document.getElementById('obs-timeline-container'), d.issues || [], d.repoKey || '');
    }).catch(e => {
      const c = document.getElementById('obs-timeline-container');
      if (c) c.innerHTML = '<div class="error">' + I18N.t('observability.load_failed', {msg: e.message}) + '</div>';
    });
  }
  return html;
}

// ===== Issue 任务列表 + Mermaid 时序图 =====
let _mermaidEngine = null;
let _expandedIssues = new Set();

async function _getMermaid() {
  if (!_mermaidEngine) {
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js';
      s.onload = resolve; s.onerror = reject;
      document.head.appendChild(s);
    });
    _mermaidEngine = window.mermaid;
    _mermaidEngine.initialize({ startOnLoad: false, securityLevel: 'loose', theme: 'base', fontFamily: 'inherit', sequence: { mirrorActors: false, actorMargin: 60, messageMargin: 40, noteMargin: 10, boxMargin: 8 } });
  }
  return _mermaidEngine;
}

function _mermaidId(a) { return (a || 'agent').replace(/[^a-zA-Z0-9_-]/g, '_'); }

function _actionText(t) {
  if (t === 'a2a_event') return I18N.t('observability.action_a2a_event');
  return { issue_opened: I18N.t('observability.action_issue_opened'), issue_comment: I18N.t('observability.action_issue_comment'), issue_updated: I18N.t('observability.action_issue_updated'), pull_request_opened: I18N.t('observability.action_pull_request_opened'), note_hook_pr: I18N.t('observability.action_note_hook_pr'), issue_closed: I18N.t('observability.action_issue_closed'), push: I18N.t('observability.action_push') }[t] || t;
}

function _buildSequence(iss) {
  const agents = [];
  for (const ev of iss.events) { const a = ev.agentType || '—'; if (!agents.includes(a)) agents.push(a); }
  const lines = ['sequenceDiagram', 'autonumber'];
  for (const a of agents) lines.push('    participant ' + _mermaidId(a) + ' as ' + a);
  const statuses = [];
  const evTypes = [];  // 'msg' or 'note' — 用于 _bindMermaidClicks 精确映射
  let prev = null;
  for (const ev of iss.events) {
    const a = _mermaidId(ev.agentType || '—');
    const t = ev.createdAt ? fmtLocalTime(ev.createdAt).slice(11, 19) : '';
    const label = t ? t + ' ' + _actionText(ev.eventType) : _actionText(ev.eventType);
    if (prev && a !== prev) {
      // 跨 agent 交互：prev -> a
      lines.push('    ' + prev + '->>' + a + ': ' + label);
      statuses.push(ev.status);
      evTypes.push('msg');
    } else {
      // 同 agent 连续事件或首个事件：用 Note 标注（不挤在一行）
      lines.push('    Note over ' + a + ': ' + label);
      statuses.push(ev.status);
      evTypes.push('note');
    }
    prev = a;
  }
  return { text: lines.join('\n'), statuses, evTypes };
}

function _applySequenceColors(svg, statuses) {
  try {
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml');
    const root = doc.documentElement; const defs = root.querySelector('defs');
    if (!defs) return svg;
    const colorOf = s => (s === 'failed' ? '#e5484d' : s === 'pending' ? '#94a3b8' : '#2e9e5b');
    const lines = Array.from(root.querySelectorAll('line[marker-start*="sequencenumber"]'));
    lines.forEach((line, i) => {
      const markerId = root.id + '-seqnum-' + i;
      const marker = doc.createElementNS('http://www.w3.org/2000/svg', 'marker');
      marker.setAttribute('id', markerId); marker.setAttribute('refX', '15'); marker.setAttribute('refY', '15');
      marker.setAttribute('markerWidth', '60'); marker.setAttribute('markerHeight', '40'); marker.setAttribute('orient', 'auto');
      const circle = doc.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', '15'); circle.setAttribute('cy', '15'); circle.setAttribute('r', '6');
      circle.setAttribute('fill', colorOf(statuses[i] || '')); marker.appendChild(circle);
      defs.appendChild(marker); line.setAttribute('marker-start', 'url(#' + markerId + ')');
    });
    return new XMLSerializer().serializeToString(doc);
  } catch (e) { return svg; }
}

async function _renderMermaid(box, iss) {
  if (box.dataset.rendered) return;
  box.dataset.rendered = '1';
  box.innerHTML = '<span class="ts t3">' + I18N.t('observability.rendering') + '</span>';
  try {
    const mmd = await _getMermaid();
    const uid = 'mmd-' + Math.random().toString(36).slice(2, 10);
    const { text, statuses, evTypes } = _buildSequence(iss);
    // mermaid v11: 先 parse 验证语法，再 render
    try { await mmd.parse(text); } catch(pe) { /* parse 失败也尝试 render */ }
    const result = await mmd.render(uid, text);
    const svg = result.svg || result;
    box.innerHTML = _applySequenceColors(svg, statuses);
    // 给每个消息/Note 绑定点击事件 → 打开对应事件的日志弹窗
    _bindMermaidClicks(box, iss, evTypes);
  } catch (err) {
    box.innerHTML = '<div style="padding:12px;color:var(--r);font-size:12px">' + I18N.t('observability.sequence_render_failed', {msg: (err.message || err)}) + '</div>';
    // 显示原始 mermaid 文本供调试
    const { text } = _buildSequence(iss);
    box.innerHTML += '<pre style="padding:8px;background:#0c1222;color:#e0e6f0;font-size:10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap">' + text.replace(/</g,'&lt;') + '</pre>';
  }
}

function _bindMermaidClicks(box, iss, evTypes) {
  // mermaid 渲染时 message 和 Note 分组排列（不是交替的），
  // 需要按 evTypes 分别匹配 messageText 和 noteText
  // 用事件委托（svg.onclick）避免收起/展开后事件丢失
  const events = iss.events || [];
  if (!events.length) return;
  const svg = box.querySelector('svg');
  if (!svg) return;
  // 构建 device_name → IP 映射（从 ops/overview workers 数据）
  const _devIpMap = {};
  try {
    const _workers = (_obsData.ops && _obsData.ops.workers && _obsData.ops.workers.list) || [];
    _workers.forEach(w => {
      if (w.device_name) _devIpMap[w.device_name] = w.last_ip || '';
    });
  } catch(e) {}
  // 分别取 message 和 note 的 text 元素
  const msgTexts = Array.from(svg.querySelectorAll('text.messageText'));
  const noteTexts = Array.from(svg.querySelectorAll('text.noteText'));
  // evTypes 标记每个事件是 'msg' 还是 'note'，分别用独立计数器匹配
  const taskEventMap = {};
  let msgIdx = 0, noteIdx = 0;
  events.forEach((ev, i) => {
    const type = evTypes[i] || 'msg';
    let txt = null;
    if (type === 'msg') {
      txt = msgTexts[msgIdx];
      msgIdx++;
    } else {
      txt = noteTexts[noteIdx];
      noteIdx++;
    }
    if (!txt) return;
    // tooltip：引擎名 + IP（SVG 用 <title> 子元素显示原生 tooltip）
    const _eng = ev.engine || '';
    const _devName = _eng.split('-').slice(1).join('-');
    const _ip = _devIpMap[_devName] || '';
    const _tooltip = _eng + (_ip ? ' (' + _ip + ')' : '');
    if (_tooltip) {
      const _t = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      _t.textContent = _tooltip;
      txt.appendChild(_t);
    }
    if (!ev.taskId) return;
    // 用 data-task-id 属性标记，事件委托通过属性查找
    txt.setAttribute('data-task-id', ev.taskId);
    txt.style.cursor = 'pointer';
    txt.style.textDecoration = 'underline';
    txt.style.fill = 'var(--a)';
    taskEventMap[ev.taskId] = ev;
  });
  // 事件委托：在 SVG 上监听 click（不绑定到单个 text 元素，避免重绘丢失）
  const enc = (taskId) => encodeURIComponent(JSON.stringify({issue:iss.issueId, seq:taskEventMap[taskId].seq, created:taskEventMap[taskId].createdAt||''}));
  svg.onclick = function(e) {
    const target = e.target.closest('[data-task-id]');
    if (!target) return;
    const taskId = target.getAttribute('data-task-id');
    if (taskId && taskEventMap[taskId]) {
      e.stopPropagation();
      openTaskLogModal(taskId, enc(taskId));
    }
  };
}

function renderTimelineList(container, issues, repoKey) {
  if (!issues.length) { container.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_task_events') + '</div>'; return; }
  _timelineIssues = issues;
  _timelineIssuesSorted = issues.slice().sort((a, b) => {
    const na = parseInt((a.issueId || '').replace(/^#/, '')) || 0;
    const nb = parseInt((b.issueId || '').replace(/^#/, '')) || 0;
    return nb - na;
  });
  _timelineDevIpMap = {};
  try {
    const _workers = (_obsData.ops && _obsData.ops.workers && _obsData.ops.workers.list) || [];
    _workers.forEach(w => { if (w.device_name) _timelineDevIpMap[w.device_name] = w.last_ip || ''; });
  } catch(e) {}
  _timelineGlobalRepoPath = stripRepoPrefix(repoKey || '');
  let html = section(I18N.t('observability.task_list'), I18N.t('observability.task_list_subtitle', {count: issues.length}));
  html += '<div style="margin-bottom:10px;display:flex;align-items:center;gap:8px">';
  html += '<input type="text" id="timelineSearchInput" placeholder="' + I18N.t('observability.search_tasks_placeholder') + '" value="' + _timelineSearch.replace(/"/g, '&quot;') + '" oninput="_timelineSearch=this.value;_timelinePage=0;_filterTimelineIssues()" style="flex:1;padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c)">';
  html += '</div>';
  html += '<div id="obs-timeline-issues"></div>';
  container.innerHTML = html;
  _filterTimelineIssues();
}

function _filterTimelineIssues() {
  const container = document.getElementById('obs-timeline-issues');
  if (!container) return;
  const issues = _timelineIssuesSorted || [];

  let filtered = issues;
  let locateIssueId = null;
  if (_timelineSearch) {
    const q = _timelineSearch.trim();
    if (q.startsWith('#')) {
      const num = q.substring(1);
      if (num) {
        filtered = issues.filter(iss => (iss.issueId || '').replace(/^#/, '').includes(num));
        const exact = issues.find(iss => (iss.issueId || '').replace(/^#/, '') === num);
        if (exact) locateIssueId = exact.issueId;
      }
    } else {
      const ql = q.toLowerCase();
      filtered = issues.filter(iss => {
        const id = (iss.issueId || '').toLowerCase();
        const title = (iss.title || '').toLowerCase();
        return id.includes(ql) || title.includes(ql);
      });
    }
  }

  if (locateIssueId) {
    _expandedIssues.add(locateIssueId);
    if (!_issueViewMode[locateIssueId]) _issueViewMode[locateIssueId] = 'diagram';
    // 定位到目标 Issue 所在页，确保其可见
    const _idx = filtered.findIndex(iss => iss.issueId === locateIssueId);
    if (_idx >= 0) _timelinePage = Math.floor(_idx / _timelinePageSize);
  }

  if (!filtered.length) {
    container.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_matching_tasks') + '</div>';
    return;
  }

  function _engineWithIp(engine) {
    if (!engine) return '-';
    const _devName = engine.split('-').slice(1).join('-');
    const _ip = _timelineDevIpMap[_devName];
    return _ip ? engine + ' (' + _ip + ')' : engine;
  }
  function _issueUrl(issueId, issueRepoKey) {
    const num = (issueId || '').replace(/^#/, '');
    const repoPath = stripRepoPrefix(issueRepoKey || '') || _timelineGlobalRepoPath;
    return repoPath && num ? 'https://gitcode.com/' + repoPath + '/issues/' + num : '';
  }

  // 分页：先筛选，再按 _timelinePage/_timelinePageSize 切片
  const _tlTotalPages = Math.max(1, Math.ceil(filtered.length / _timelinePageSize));
  if (_timelinePage >= _tlTotalPages) _timelinePage = 0;
  const _tlStart = _timelinePage * _timelinePageSize;
  const pageItems = filtered.slice(_tlStart, _tlStart + _timelinePageSize);

  let html = '<div style="display:flex;flex-direction:column;gap:0">';
  pageItems.forEach(iss => {
    const expanded = _expandedIssues.has(iss.issueId);
    const hasFailed = iss.events.some(e => e.status === 'failed');
    const statusBadge = hasFailed ? '<span style="color:var(--r);font-size:10px;font-weight:600">' + I18N.t('observability.has_failure') + '</span>' : '<span style="color:var(--g);font-size:10px;font-weight:600">' + I18N.t('observability.normal') + '</span>';
    const _url = _issueUrl(iss.issueId, iss.repoKey);
    const _issueLink = _url
      ? '<a href="' + _url + '" target="_blank" onclick="event.stopPropagation()" style="font-size:13px;font-weight:700;color:var(--a);text-decoration:none">' + iss.issueId + ' \u2197</a>'
      : '<b style="font-size:13px">' + iss.issueId + '</b>';
    html += '<div style="border:1px solid var(--b);border-radius:8px;overflow:hidden;margin-bottom:8px">';
    html += '<div style="display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;background:var(--c)" onclick="toggleIssueTimeline(\'' + iss.issueId + '\')">';
    html += '<span style="font-size:12px">' + (expanded ? '\u25be' : '\u25b8') + '</span>';
    html += _issueLink;
    html += iss.title ? '<span class="ts t3" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + iss.title + '</span>' : '<span style="flex:1"></span>';
    const _totalDur = iss.events.reduce((s, e) => s + (e.duration || 0), 0);
    const _totalDurStr = _fmtTotalDur(_totalDur);
    html += '<span class="ts t3">' + I18N.t('observability.events_count', {count: iss.eventCount}) + '</span>' + statusBadge;
    html += '<span class="ts t3" style="color:var(--a)">' + I18N.t('observability.total_duration') + ': ' + _totalDurStr + '</span>';
    html += '<span class="ts t3">' + fmtLocalTime(iss.lastAt || '').substring(0,16) + '</span>';
    html += '</div>';
    html += '<div id="tl-body-' + iss.issueId + '" style="display:' + (expanded ? 'block' : 'none') + ';padding:10px 14px;border-top:1px solid var(--b)">';
    html += '<div style="display:flex;gap:0;margin-bottom:10px">';
    html += '<button class="bt bt-s ' + (_issueViewMode[iss.issueId] !== 'events' ? 'bt-p' : 'bt-o') + '" onclick="switchIssueView(\'' + iss.issueId + '\',\'diagram\')">' + I18N.t('observability.sequence_diagram') + '</button>';
    html += '<button class="bt bt-s ' + (_issueViewMode[iss.issueId] === 'events' ? 'bt-p' : 'bt-o') + '" onclick="switchIssueView(\'' + iss.issueId + '\',\'events\')">' + I18N.t('observability.event_list') + '</button>';
    html += '</div>';
    html += '<div id="tl-diagram-' + iss.issueId + '" class="tl-mmd" data-iid="' + iss.issueId + '" style="overflow-x:auto;' + (_issueViewMode[iss.issueId] === 'events' ? 'display:none' : '') + '"></div>';
    html += '<div id="tl-events-' + iss.issueId + '" style="' + (_issueViewMode[iss.issueId] !== 'events' ? 'display:none' : '') + '">';
    html += '<table style="width:100%;font-size:11px"><thead><tr><th>' + I18N.t('observability.col_seq') + '</th><th>' + I18N.t('observability.col_event') + '</th><th>' + I18N.t('observability.col_agent') + '</th><th>' + I18N.t('observability.col_engine') + '</th><th>' + I18N.t('observability.col_skill') + '</th><th>' + I18N.t('observability.col_status') + '</th><th>' + I18N.t('observability.col_duration') + '</th><th>' + I18N.t('observability.col_token') + '</th><th>' + I18N.t('observability.col_time') + '</th></tr></thead><tbody>';
    html += iss.events.map(ev => {
      const enc = ev.taskId ? encodeURIComponent(JSON.stringify({issue:iss.issueId, seq:ev.seq, created:ev.createdAt||''})) : '';
      const clickable = ev.taskId ? 'cursor:pointer' : '';
      const onclick = ev.taskId ? " onclick=\"openTaskLogModal('" + ev.taskId + "','" + enc + "')\"" : '';
      return '<tr style="' + clickable + '"' + onclick + '><td class="ts">' + ev.seq + '</td><td>' + _actionText(ev.eventType) + '</td><td><b>' + (ev.agentType||'-') + '</b></td><td class="ts t3">' + _engineWithIp(ev.engine) + '</td><td class="ts t3">' + (ev.skill||'-') + '</td><td style="color:' + (ev.status==='failed'?'var(--r)':ev.status==='completed'?'var(--g)':'var(--t3)') + '">' + (ev.status||'-') + '</td><td class="ts t3">' + (ev.duration||'-') + 's</td><td class="ts t2" title="' + (ev.agentType||'') + '">' + _obsTokTaskFor(ev.taskId) + '</td><td class="ts t3">' + fmtLocalTime(ev.createdAt||'').substring(0,19) + '</td></tr>';
    }).join('');
    html += '</tbody></table></div>';
    html += '</div></div>';
  });
  html += '</div>';
  // 分页栏（含每页条数选择器）
  html += _paginationWithPageSizeHTML(_timelinePage, _tlTotalPages, '_timelinePage--;_filterTimelineIssues()', '_timelinePage++;_filterTimelineIssues()', _timelinePageSize, '_timelinePageSize', '_timelinePage=0;_filterTimelineIssues()');
  container.innerHTML = html;
  pageItems.forEach(iss => {
    if (_expandedIssues.has(iss.issueId) && _issueViewMode[iss.issueId] !== 'events') {
      const box = container.querySelector('#tl-diagram-' + iss.issueId);
      if (box) _renderMermaid(box, iss);
    }
  });
  if (locateIssueId) {
    setTimeout(() => {
      const body = document.getElementById('tl-body-' + locateIssueId);
      if (body) {
        const card = body.parentElement;
        if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }, 100);
  }
}

let _timelineIssues = [];
let _timelineIssuesSorted = [];
let _timelineSearch = '';
let _timelineDevIpMap = {};
let _timelineGlobalRepoPath = '';
let _issueViewMode = {};
let _timelineCache = null;  // {repo, ts, issues} — 30s TTL 避免重复 fetch

function switchIssueView(issueId, mode) {
  _issueViewMode[issueId] = mode;
  const diagram = document.getElementById('tl-diagram-' + issueId);
  const events = document.getElementById('tl-events-' + issueId);
  if (diagram) diagram.style.display = (mode === 'events' ? 'none' : 'block');
  if (events) events.style.display = (mode === 'events' ? 'block' : 'none');
  // 更新按钮高亮
  const body = document.getElementById('tl-body-' + issueId);
  if (body) {
    const btns = body.querySelectorAll('.bt-s');
    btns.forEach(b => { b.className = b.className.replace('bt-p','bt-o'); });
    if (mode === 'events' && btns[1]) btns[1].className = btns[1].className.replace('bt-o','bt-p');
    else if (btns[0]) btns[0].className = btns[0].className.replace('bt-o','bt-p');
  }
  // 如果切换到时序图且未渲染，则渲染
  if (mode === 'diagram' && diagram && !diagram.dataset.rendered) {
    const iss = _timelineIssues.find(i => i.issueId === issueId);
    if (iss) _renderMermaid(diagram, iss);
  }
}

function toggleIssueTimeline(issueId) {
  const body = document.getElementById('tl-body-' + issueId);
  if (!body) return;
  if (body.style.display === 'none') {
    body.style.display = 'block';
    _expandedIssues.add(issueId);
    // 默认显示时序图
    if (!_issueViewMode[issueId]) _issueViewMode[issueId] = 'diagram';
    const box = document.getElementById('tl-diagram-' + issueId);
    if (box && !box.dataset.rendered && _issueViewMode[issueId] === 'diagram') {
      const iss = _timelineIssues.find(i => i.issueId === issueId);
      if (iss) _renderMermaid(box, iss);
    } else if (box && box.dataset.rendered && _issueViewMode[issueId] === 'diagram') {
      // 已渲染过：重新绑定点击事件（DOM 可能被 Vue/浏览器重绘后丢失事件委托）
      const iss = _timelineIssues.find(i => i.issueId === issueId);
      if (iss) {
        const { evTypes } = _buildSequence(iss);
        _bindMermaidClicks(box, iss, evTypes);
      }
    }
    const toggle = body.previousElementSibling;
    if (toggle) toggle.querySelector('span').textContent = '\u25be';
  } else {
    body.style.display = 'none';
    _expandedIssues.delete(issueId);
    const toggle = body.previousElementSibling;
    if (toggle) toggle.querySelector('span').textContent = '\u25b8';
  }
}

// ===== 辅助函数 =====

// ===== 降级 ops =====
function fallbackOps(realtime) {
  const rt = realtime || {};
  const tasks = rt.tasks || {};
  return {
    tasks: { status_counts: tasks, in_progress: rt.in_progress || 0, stuck_count: 0, stuck_tasks: [], by_source: {}, today: {} },
    runtimes: { total: rt.runtimes ? rt.runtimes.total : 0, online: rt.runtimes ? rt.runtimes.online : 0, offline: rt.runtimes ? (rt.runtimes.total - rt.runtimes.online) : 0, by_mode: {}, list: [] },
  };
}

// ===== Prometheus 文本解析（当前未使用，注释保留备用）=====
// function parseMetric(text, name) {
//   const m = text.match(new RegExp('^' + name + '\\s+(\\d+\\.?\\d*)', 'm'));
//   return m ? parseFloat(m[1]) : 0;
// }
// function parseMetrics(text, names) {
//   const r = {};
//   for (const n of names) r[n] = parseMetric(text, n);
//   return r;
// }
// function parseLabeledSum(text, name) {
//   let total = 0;
//   const re = new RegExp('^' + name + '\\{[^}]*\\}\\s+(\\d+\\.?\\d*)', 'gm');
//   let m;
//   while ((m = re.exec(text)) !== null) total += parseFloat(m[1]);
//   return total;
// }
// function parseLabeledSumByLabel(text, name, key, val) {
//   let total = 0;
//   const re = new RegExp('^' + name + '\\{[^}]*' + key + '="' + val + '"[^}]*\\}\\s+(\\d+\\.?\\d*)', 'gm');
//   let m;
//   while ((m = re.exec(text)) !== null) total += parseFloat(m[1]);
//   return total;
// }
// function fmtNum(n) {
//   if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
//   if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
//   return String(n);
// }
// function parseLabeledKeys(text, name, labelKey) {
//   const keys = [];
//   const re = new RegExp('^' + name + '\\\\{[^}]*' + labelKey + '="([^"]+)"[^}]*\\\\}', 'gm');
//   let m;
//   while ((m = re.exec(text)) !== null) {
//     if (!keys.includes(m[1])) keys.push(m[1]);
//   }
//   return keys;
// }

// ===== Runtime 任务列表 + 日志弹窗（从 runtimes.js 迁移）=====

// ===== Runtime Session 列表弹窗 =====
function openRuntimeSessionModal(rtId, rtName) {
  const old = document.getElementById('rtSessionModal');
  if (old) old.remove();

  const modal = document.createElement('div');
  modal.id = 'rtSessionModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:1001';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = '<div class="cd" style="width:760px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;padding:0;overflow:hidden">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">' +
    '<h3 style="font-size:14px;font-weight:600">' + rtName + ' · ' + I18N.t('observability.session_list') + '</h3>' +
    '<button class="bt bt-o bt-s" onclick="document.getElementById(\'rtSessionModal\').remove()">' + I18N.t('observability.close') + '</button>' +
    '</div>' +
    '<div id="rtSessionModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">' + I18N.t('observability.loading_session') + '</div></div>' +
    '</div>';
  document.body.appendChild(modal);

  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '?repo=' + encodeURIComponent(repo.fullName) : '';
  API.get('/runtime/runtimes/' + rtId + '/sessions' + repoParam).then(d => {
    renderSessionList(document.getElementById('rtSessionModalBody'), d.sessions || [], rtName);
  }).catch(e => {
    document.getElementById('rtSessionModalBody').innerHTML = '<div class="error">' + I18N.t('observability.load_failed', {msg: e.message}) + '</div>';
  });
}

function renderSessionList(container, sessions, rtName) {
  if (!sessions.length) {
    container.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_session') + '</div>';
    return;
  }

  // 运行中 = 有 current_task 的 session
  // 空闲 = 没有 current_task 的 session（显示在已完成区域上方）
  const running = sessions.filter(s => s.current_task);
  const idle = sessions.filter(s => !s.current_task);

  const taskStatusIcon = { completed: '✅', failed: '❌', cancelled: '⛔', running: '🔄', queued: '⏳', dispatched: '🔄' };
  const taskStatusColor = { completed: 'var(--g)', failed: 'var(--r)', cancelled: 'var(--t3)', running: 'var(--a)', queued: 'var(--y)', dispatched: 'var(--a)' };

  // 运行中 session 表格（显示 agent + 正在执行的 issue#/seq）
  function runningTable(list) {
    if (!list.length) return '<div class="ts t3" style="padding:8px">' + I18N.t('observability.no_running_session') + '</div>';
    let html = '<div class="tw"><table><thead><tr><th>Agent</th><th>Issue / Seq</th><th>Skill</th><th>' + I18N.t('observability.col_status') + '</th><th>' + I18N.t('observability.col_start_time') + '</th></tr></thead><tbody>';
    list.forEach(s => {
      const ct = s.current_task;
      const is = (ct.issue_id && ct.event_seq) ? ct.issue_id + ' #' + ct.event_seq : (ct.event_seq ? '#' + ct.event_seq : ct.id.substring(0,12));
      const enc = encodeURIComponent(JSON.stringify({issue:ct.issue_id||'', seq:ct.event_seq||0, repo:s.repo_key, created:ct.started_at||''}));
      html += '<tr style="cursor:pointer" onclick="openTaskLogModal(\'' + ct.id + '\',\'' + enc + '\')">' +
        '<td><b>' + (s.agent_type||'-') + '</b></td>' +
        '<td class="ts"><b>' + is + '</b></td>' +
        '<td class="ts t3">' + (s.skill||'-') + '</td>' +
        '<td><span style="color:' + (taskStatusColor[ct.status]||'var(--a)') + '">' + (taskStatusIcon[ct.status]||'🔄') + ' ' + ct.status + '</span></td>' +
        '<td class="ts t3">' + fmtLocalTime(ct.started_at||'').substring(0,16) + '</td>' +
        '</tr>';
    });
    html += '</tbody></table></div>';
    return html;
  }

  // 已完成 task 列表（从所有 session 的 tasks 中收集，按 seq 倒序）
  function completedTable(allSessions) {
    const seen = new Set();
    const allTasks = [];
    allSessions.forEach(s => {
      (s.tasks || []).forEach(t => {
        if (!seen.has(t.id)) { seen.add(t.id); allTasks.push(t); }
      });
    });
    allTasks.sort((a, b) => (b.event_seq || 0) - (a.event_seq || 0));
    if (!allTasks.length) return '<div class="ts t3" style="padding:8px">' + I18N.t('observability.no_completed_tasks') + '</div>';
    let html = '<div class="tw"><table><thead><tr><th>' + I18N.t('observability.col_issue_seq') + '</th><th>' + I18N.t('observability.col_agent') + '</th><th>' + I18N.t('observability.col_status') + '</th><th>' + I18N.t('observability.col_duration') + '</th><th>' + I18N.t('observability.col_complete_time') + '</th></tr></thead><tbody>';
    allTasks.forEach(t => {
      const is = (t.issue_id && t.event_seq) ? t.issue_id + ' #' + t.event_seq : (t.event_seq ? '#' + t.event_seq : t.id.substring(0,12));
      const enc = encodeURIComponent(JSON.stringify({issue:t.issue_id||'', seq:t.event_seq||0, repo:'', created:t.completed_at||''}));
      // 计算耗时
      let durStr = '-';
      let durColor = 'var(--t3)';
      if (t.started_at && t.completed_at) {
        const dur = Math.round((new Date(t.completed_at) - new Date(t.started_at)) / 1000);
        if (dur > 0) {
          durStr = dur > 60 ? Math.round(dur/60) + 'm' + Math.round(dur%60) + 's' : dur + 's';
          durColor = dur > 300 ? 'var(--r)' : dur > 60 ? 'var(--y)' : 'var(--g)';
        }
      }
      html += '<tr style="cursor:pointer" onclick="openTaskLogModal(\'' + t.id + '\',\'' + enc + '\')">' +
        '<td class="ts"><b>' + is + '</b></td>' +
        '<td><b>' + (t.agent_type||'-') + '</b></td>' +
        '<td><span style="color:' + (taskStatusColor[t.status]||'var(--t3)') + '">' + (taskStatusIcon[t.status]||'') + ' ' + t.status + '</span></td>' +
        '<td><span style="color:' + durColor + '">' + durStr + '</span></td>' +
        '<td class="ts t2">' + _obsTokTaskFor(t.id) + '</td>' +
        '<td class="ts t3">' + fmtLocalTime(t.completed_at||'').substring(0,16) + '</td>' +
        '</tr>';
    });
    html += '</tbody></table></div>';
    return html;
  }

  const completedCount = sessions.reduce((sum, s) => sum + (s.tasks ? s.tasks.length : 0), 0);

  let html = '';
  // 运行中（默认展开）
  html += '<div style="border:1px solid var(--b);border-radius:8px;overflow:hidden;margin-bottom:10px">';
  html += '<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;background:var(--c)" onclick="toggleSessionGroup(\'rtSessionRunning\')">';
  html += '<span id="rtSessionRunning-arrow" style="font-size:12px">▾</span>';
  html += '<b style="font-size:12px">' + I18N.t('observability.running') + '</b>';
  html += '<span class="ts t3" style="margin-left:auto">' + I18N.t('observability.session_count', {count: running.length}) + '</span>';
  html += '</div>';
  html += '<div id="rtSessionRunning" style="padding:8px 12px;border-top:1px solid var(--b)">' + runningTable(running) + '</div>';
  html += '</div>';

  // 已完成任务（默认收起）
  html += '<div style="border:1px solid var(--b);border-radius:8px;overflow:hidden;margin-bottom:10px">';
  html += '<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;background:var(--c)" onclick="toggleSessionGroup(\'rtSessionIdle\')">';
  html += '<span id="rtSessionIdle-arrow" style="font-size:12px">▸</span>';
  html += '<b style="font-size:12px">' + I18N.t('observability.completed_tasks') + '</b>';
  html += '<span class="ts t3" style="margin-left:auto">' + I18N.t('observability.task_count', {count: completedCount}) + '</span>';
  html += '</div>';
  html += '<div id="rtSessionIdle" style="padding:8px 12px;border-top:1px solid var(--b);display:none">' + completedTable(sessions) + '</div>';
  html += '</div>';

  container.innerHTML = html;
}

function toggleSessionGroup(groupId) {
  const body = document.getElementById(groupId);
  if (!body) return;
  const arrow = document.getElementById(groupId + '-arrow');
  if (body.style.display === 'none') {
    body.style.display = 'block';
    if (arrow) arrow.textContent = '▾';
  } else {
    body.style.display = 'none';
    if (arrow) arrow.textContent = '▸';
  }
}

// 点击 session 行 → 打开日志弹窗（复用 openTaskLogModal 样式）
// session 没有直接的 task_id，但可以通过 agent_tasks 中的 issue_id + event_seq 关联
// 这里用 session 的 agent_type + repo_key 查最近的任务日志
function openSessionLogModal(sessionId, infoEnc) {
  let info = {};
  try { info = JSON.parse(decodeURIComponent(infoEnc || '')); } catch(e) {}

  const old = document.getElementById('rtLogModal');
  if (old) old.remove();

  const titleParts = [];
  if (info.repo) titleParts.push('<span class="ts t3">' + info.repo + '</span>');
  if (info.agent) titleParts.push('<b>' + info.agent + '</b>');
  if (info.sid) titleParts.push('<span class="ts">' + info.sid + '</span>');
  if (info.last) titleParts.push('<span class="ts t3">' + info.last.substring(0,16) + '</span>');
  const titleHtml = titleParts.length ? titleParts.join(' · ') : I18N.t('observability.session_log');

  const modal = document.createElement('div');
  modal.id = 'rtLogModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1001';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = '<div class="cd" style="width:860px;max-width:94vw;max-height:85vh;display:flex;flex-direction:column;padding:0;overflow:hidden">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">' +
    '<h3 style="font-size:13px;font-weight:600">' + titleHtml + '</h3>' +
    '<button class="bt bt-o bt-s" onclick="document.getElementById(\'rtLogModal\').remove()">' + I18N.t('observability.close') + '</button>' +
    '</div>' +
    '<div id="rtLogModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">' + I18N.t('observability.loading_logs') + '</div></div>' +
    '</div>';
  document.body.appendChild(modal);

  // session 没有直接 task_id，查该 session 最近的任务
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '&repo=' + encodeURIComponent(repo.fullName) : '';
  // 先查该引擎 + 该仓库最近的任务
  // 使用 runtime tasks 端点找到最近的 task_id
  fetch('/api/runtime/runtimes/1/tasks?limit=10' + repoParam, {
    headers: { 'Authorization': 'Bearer ' + (localStorage.token || '') },
    signal: AbortSignal.timeout(10000)
  }).then(r => r.json()).then(d => {
    const tasks = d.tasks || [];
    if (!tasks.length) {
      document.getElementById('rtLogModalBody').innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_session_task_log') + '</div>';
      return;
    }
    // 取最近一个任务
    const task = tasks[0];
    const taskEnc = encodeURIComponent(JSON.stringify({issue:task.issue_id||'', seq:task.event_seq||0, repo:(task.repo_key||'').replace('gitcode:',''), created:task.created_at||''}));
    // 加载该任务的日志
    API.get('/task-logs/' + task.id + '/messages?since=0').then(msgs => {
      renderTaskLogMessages(document.getElementById('rtLogModalBody'), msgs || [], JSON.parse(decodeURIComponent(taskEnc)));
    }).catch(e => {
      document.getElementById('rtLogModalBody').innerHTML = '<div class="error">' + I18N.t('observability.load_failed', {msg: e.message}) + '</div>';
    });
  }).catch(e => {
    document.getElementById('rtLogModalBody').innerHTML = '<div class="error">' + I18N.t('observability.load_failed', {msg: e.message}) + '</div>';
  });
}

function openRuntimeTaskModal(rtId, rtName) {
  const old = document.getElementById('rtTaskModal');
  if (old) old.remove();

  const modal = document.createElement('div');
  modal.id = 'rtTaskModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:1001';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = `<div class="cd" style="width:720px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;padding:0;overflow:hidden">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">
      <h3 style="font-size:14px;font-weight:600">${I18N.t('observability.task_list_modal_title', {name: rtName})}</h3>
      <button class="bt bt-o bt-s" onclick="document.getElementById('rtTaskModal').remove()">${I18N.t('observability.close')}</button>
    </div>
    <div id="rtTaskModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">${I18N.t('observability.loading_task')}</div></div>
  </div>`;
  document.body.appendChild(modal);

  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '&repo=' + encodeURIComponent(repo.fullName) : '';
  API.get('/runtime/runtimes/' + rtId + '/tasks?limit=50' + repoParam).then(d => {
    renderRuntimeTaskList(document.getElementById('rtTaskModalBody'), d.tasks || [], rtName);
  }).catch(e => {
    document.getElementById('rtTaskModalBody').innerHTML = '<div class="error">' + I18N.t('observability.load_failed', {msg: e.message}) + '</div>';
  });
}

function renderRuntimeTaskList(container, tasks, rtName) {
  if (!tasks.length) {
    container.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_tasks') + '</div>';
    return;
  }
  const statusIcon = { queued: '⏳', dispatched: '🔄', running: '🔄', completed: '✅', failed: '❌', cancelled: '⛔' };
  const statusColor = { queued: 'var(--y)', dispatched: 'var(--a)', running: 'var(--a)', completed: 'var(--g)', failed: 'var(--r)', cancelled: 'var(--t3)' };
  let html = `<div style="font-size:11px;color:var(--t3);margin-bottom:8px">${I18N.t('observability.task_list_hint', {name: rtName, count: tasks.length})}</div>`;
  html += '<div class="tw"><table><thead><tr><th>' + I18N.t('observability.col_issue_seq') + '</th><th>' + I18N.t('observability.col_agent_skill') + '</th><th>' + I18N.t('observability.col_repo') + '</th><th>' + I18N.t('observability.col_status') + '</th><th>' + I18N.t('observability.col_token') + '</th><th>' + I18N.t('observability.col_created') + '</th></tr></thead><tbody>';
  tasks.forEach(t => {
    const issueSeq = (t.issue_id && t.event_seq) ? `${t.issue_id} #${t.event_seq}` : (t.event_seq ? `#${t.event_seq}` : t.id.substring(0,12));
    const infoEnc = encodeURIComponent(JSON.stringify({issue:t.issue_id||'', seq:t.event_seq||0, repo:(t.repo_key||'').replace('gitcode:',''), created:t.created_at||''}));
    html += `<tr style="cursor:pointer" onclick="openTaskLogModal('${t.id}', '${infoEnc}')">
      <td class="ts"><b>${issueSeq}</b></td>
      <td><b>${t.agent_type || '-'}</b><br><span class="ts t3">${t.skill || '-'}</span></td>
      <td class="ts t3">${(t.repo_key || '').replace('gitcode:','').substring(0,20)}</td>
      <td><span style="color:${statusColor[t.status] || 'var(--t3)'}">${statusIcon[t.status] || ''} ${I18N.t('common.status.' + t.status) !== ('common.status.' + t.status) ? I18N.t('common.status.' + t.status) : t.status}</span></td>
      <td class="ts t2">${_obsTokTaskFor(t.id)}</td>
      <td class="ts t3">${fmtLocalTime(t.created_at || '').substring(0,16)}</td>
    </tr>`;
  });
  html += '</tbody></table></div>';
  container.innerHTML = html;
}

let _taskLogCache = {};  // taskId -> {msgs, info, ts} — 60s TTL

function openTaskLogModal(taskId, infoEnc) {
  const old = document.getElementById('rtLogModal');
  if (old) old.remove();
  // 清理旧的轮询定时器
  if (_taskLogPollTimer) { clearInterval(_taskLogPollTimer); _taskLogPollTimer = null; }

  let info = {};
  try { info = JSON.parse(decodeURIComponent(infoEnc || '')); } catch(e) {}
  const titleParts = [];
  if (info.repo) titleParts.push('<span class="ts t3">' + info.repo + '</span>');
  if (info.issue) titleParts.push('<b>' + info.issue + '</b>');
  if (info.seq) titleParts.push('<span class="ts">#' + info.seq + '</span>');
  if (info.created) titleParts.push('<span class="ts t3">' + info.created.substring(0,19) + '</span>');
  const titleHtml = titleParts.length ? titleParts.join(' · ') : I18N.t('observability.task_log');

  const modal = document.createElement('div');
  modal.id = 'rtLogModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1001';
  modal.onclick = (e) => {
    if (e.target === modal) {
      if (_taskLogPollTimer) { clearInterval(_taskLogPollTimer); _taskLogPollTimer = null; }
      modal.remove();
    }
  };
  modal.innerHTML = '<div class="cd" style="width:860px;max-width:94vw;max-height:85vh;display:flex;flex-direction:column;padding:0;overflow:hidden">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">' +
    '<h3 style="font-size:13px;font-weight:600">' + titleHtml + '</h3>' +
    '<div style="display:flex;align-items:center;gap:8px"><span id="rtLogLiveTag" style="font-size:10px;color:var(--g);display:none">' + I18N.t('observability.live') + '</span><button class="bt bt-o bt-s" style="white-space:nowrap;padding:4px 12px" onclick="openObsTimeline(\'' + taskId + '\')">' + I18N.t('observability.timeline') + '</button><button class="bt bt-o bt-s" onclick="closeTaskLogModal()">' + I18N.t('observability.close') + '</button></div>' +
    '</div>' +
    '<div id="rtLogTimeline" style="padding:10px 18px;border-bottom:1px solid var(--b);display:none"></div>' +
    '<div id="rtLogModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">' + I18N.t('observability.loading_logs') + '</div></div>' +
    '</div>';
  document.body.appendChild(modal);

  // 关闭按钮清理定时器
  window.closeTaskLogModal = function() {
    if (_taskLogPollTimer) { clearInterval(_taskLogPollTimer); _taskLogPollTimer = null; }
    const m = document.getElementById('rtLogModal');
    if (m) m.remove();
  };

  // 加载日志 + 启动增量轮询
  let _lastSeq = 0;
  let _allMsgs = [];

  function loadInitial() {
    API.get('/task-logs/' + taskId + '/messages?since=0').then(msgs => {
      _allMsgs = msgs || [];
      _lastSeq = _allMsgs.length ? _allMsgs[_allMsgs.length - 1].seq : 0;
      _taskLogCache[taskId] = { msgs: _allMsgs, infoData: null, ts: Date.now() };
      renderTaskLogMessages(document.getElementById('rtLogModalBody'), _allMsgs, info);
      // 检查任务是否还在运行 → 启动增量轮询
      API.get('/task-logs/' + taskId + '/info').then(infoData => {
        if (_taskLogCache[taskId]) _taskLogCache[taskId].infoData = infoData;
        renderTaskTimeline(document.getElementById('rtLogTimeline'), infoData);
        if (infoData && (infoData.status === 'running' || infoData.status === 'queued' || infoData.status === 'dispatched')) {
          startPolling();
        }
      }).catch(() => {});
    }).catch(e => {
      document.getElementById('rtLogModalBody').innerHTML = '<div class="error">' + I18N.t('observability.load_failed', {msg: e.message}) + '</div>';
    });
  }

  function startPolling() {
    const tag = document.getElementById('rtLogLiveTag');
    if (tag) tag.style.display = 'inline';
    _taskLogPollTimer = setInterval(() => {
      // 弹窗已关闭则停止
      if (!document.getElementById('rtLogModal')) {
        clearInterval(_taskLogPollTimer);
        _taskLogPollTimer = null;
        return;
      }
      API.get('/task-logs/' + taskId + '/messages?since=' + _lastSeq).then(newMsgs => {
        if (newMsgs && newMsgs.length) {
          _allMsgs = _allMsgs.concat(newMsgs);
          _lastSeq = _allMsgs[_allMsgs.length - 1].seq;
          if (_taskLogCache[taskId]) _taskLogCache[taskId].msgs = _allMsgs;
          renderTaskLogMessages(document.getElementById('rtLogModalBody'), _allMsgs, info);
          // 自动滚动到底部
          const body = document.getElementById('rtLogModalBody');
          if (body) body.scrollTop = body.scrollHeight;
        }
      }).catch(() => {});
      // 同时检查任务状态 → 终态时停止轮询
      API.get('/task-logs/' + taskId + '/info').then(infoData => {
        if (infoData && infoData.status && !['running','queued','dispatched'].includes(infoData.status)) {
          if (_taskLogPollTimer) { clearInterval(_taskLogPollTimer); _taskLogPollTimer = null; }
          const t = document.getElementById('rtLogLiveTag');
          if (t) { t.style.display = 'none'; }
          renderTaskTimeline(document.getElementById('rtLogTimeline'), infoData);
        }
      }).catch(() => {});
    }, 3000);
  }

  // 使用缓存的日志数据（60s TTL）— 但如果任务还在运行则重新加载+轮询
  const cached = _taskLogCache[taskId];
  if (cached && (Date.now() - cached.ts) < 60000) {
    // 先用缓存快速渲染
    _allMsgs = cached.msgs || [];
    _lastSeq = _allMsgs.length ? _allMsgs[_allMsgs.length - 1].seq : 0;
    renderTaskLogMessages(document.getElementById('rtLogModalBody'), _allMsgs, info);
    if (cached.infoData) renderTaskTimeline(document.getElementById('rtLogTimeline'), cached.infoData);
    // 检查是否还在运行 → 是则启动轮询
    if (cached.infoData && ['running','queued','dispatched'].includes(cached.infoData.status || '')) {
      startPolling();
    }
  } else {
    loadInitial();
  }
}

let _taskLogPollTimer = null;

function renderTaskTimeline(container, info) {
  if (!container || !info) return;
  const stages = [];
  if (info.created_at) stages.push({ label: I18N.t('observability.stage_queued'), time: info.created_at, color: 'var(--y)' });
  if (info.started_at) stages.push({ label: I18N.t('observability.stage_start'), time: info.started_at, color: 'var(--a)' });
  if (info.completed_at) stages.push({ label: I18N.t('observability.stage_complete'), time: info.completed_at, color: info.status === 'failed' ? 'var(--r)' : 'var(--g)' });
  if (!stages.length) return;
  const durations = [];
  for (let i = 1; i < stages.length; i++) {
    const prev = new Date(stages[i-1].time);
    const curr = new Date(stages[i].time);
    durations.push(Math.round((curr - prev) / 1000));
  }
  const statusColor = { queued: 'var(--y)', dispatched: 'var(--a)', running: 'var(--a)', completed: 'var(--g)', failed: 'var(--r)', cancelled: 'var(--t3)' };
  let html = '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:11px">';
  stages.forEach((s, i) => {
    html += `<div style="display:flex;align-items:center;gap:4px"><span style="width:8px;height:8px;border-radius:50%;background:${s.color}"></span><span style="color:var(--t2)">${s.label}</span><span class="ts t3">${s.time.substring(11,19)}</span></div>`;
    if (i < stages.length - 1) {
      const dur = durations[i];
      const durStr = dur > 60 ? Math.round(dur/60) + 'm' : dur + 's';
      const durColor = dur > 300 ? 'var(--r)' : dur > 60 ? 'var(--y)' : 'var(--g)';
      html += `<span style="color:${durColor};font-size:10px">→ ${durStr}</span>`;
    }
  });
  if (info.status) html += `<span style="margin-left:auto;padding:2px 8px;border-radius:4px;background:${statusColor[info.status]||'var(--t3)'};color:#fff;font-size:10px;font-weight:600">${info.status.toUpperCase()}</span>`;
  html += '</div>';
  container.innerHTML = html;
  container.style.display = 'block';
}

function renderTaskLogMessages(container, msgs, errorInfo) {
  if (!msgs.length) {
    let html = '<div style="background:#0c1222;color:#e0e6f0;font-family:Menlo,Monaco,Consolas,monospace;font-size:12px;border-radius:8px;overflow:hidden">';
    html += '<div style="padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.06);color:#8d97a8">' + I18N.t('observability.no_exec_log') + '</div>';
    if (errorInfo) {
      if (errorInfo.result_text) html += `<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#d49c0f">${I18N.t('observability.result')}</span> <span style="color:#e0e6f0">${errorInfo.result_text}</span></div>`;
      if (errorInfo.exit_code != null) html += `<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#d49c0f">Exit Code</span> <span style="color:${errorInfo.exit_code !== 0 ? '#dc2828' : '#1ca44c'}">${errorInfo.exit_code}</span></div>`;
      if (errorInfo.errors && errorInfo.errors.length) {
        html += '<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#d49c0f">' + I18N.t('observability.error_messages') + '</span></div>';
        errorInfo.errors.forEach(e => {
          let _errHtml = '';
          if (typeof e === 'object' && e !== null) {
            // R16.2.2: error 对象含 error_code + localized_message + message
            const _msg = e.localized_message || e.message || '';
            const _code = e.error_code ? ` <span style="color:#8b9bb4;font-size:11px">[${e.error_code}]</span>` : '';
            _errHtml = `${_msg}${_code}`;
          } else {
            _errHtml = String(e);
          }
          html += `<div style="padding:4px 14px 4px 28px;color:#dc2828">${_errHtml}</div>`;
        });
      }
    }
    html += '</div>';
    container.innerHTML = html;
    return;
  }
  const typeIcon = { step_start: '▶', step_finish: '✓', text: '💬', reasoning: '🤔', tool_use: '🔧', error: '❌', done: '✅' };
  const typeColor = { step_start: '#5b9eff', step_finish: '#1ca44c', text: '#e0e6f0', reasoning: '#b388ff', tool_use: '#ea5a0c', error: '#dc2828', done: '#1ca44c' };
  let html = '<div style="background:#0c1222;color:#e0e6f0;font-family:Menlo,Monaco,Consolas,monospace;font-size:12px;line-height:1.6;border-radius:8px;overflow:hidden">';
  msgs.forEach(m => {
    const icon = typeIcon[m.type] || '•';
    const color = typeColor[m.type] || '#c2cad6';
    const time = fmtLocalTime(m.created_at || '').substring(11,19);
    let content = '';
    if (m.type === 'tool_use') {
      content = `<b style="color:${color}">${m.tool || 'tool'}</b>`;
      if (m.content) content += `<br>&nbsp;&nbsp;<span style="color:#8d97a8">${m.content.substring(0, 300)}</span>`;
    } else if (m.type === 'text' || m.type === 'reasoning' || m.type === 'error') {
      content = `<span style="color:${color}">${(m.content || '').substring(0, 800)}</span>`;
    } else {
      content = `<span style="color:${color}">${m.type}</span> <span style="color:#c2cad6">${m.content || ''}</span>`;
    }
    html += `<div style="padding:5px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#5b667b">${time}</span> ${icon} ${content}</div>`;
  });
  html += '</div>';
  container.innerHTML = html;
}
