// ====== 系统监控（Settings ▸ 系统监控）======
// 仿 multica Dashboard 布局：4 Tab（概览/任务/Runtime/数据库）
// 每个 Tab 顶部 KPI 卡片行（grid 4 列），下方详情表格/图
// 数据来源：/api/dashboard/ops/overview + /health/realtime + /metrics

let _obsTab = 'overview';
let _obsTimer = null;
let _obsData = { ops: null, realtime: null, metrics: null };
let _obsRendered = false;      // 概览/任务 Tab 是否已用 innerHTML 渲染过骨架
let _obsRenderedTab = null;    // 已渲染骨架的 tab（切换 tab 时需重新建骨架）
let _rtSearch = '';            // Runtime 引擎搜索筛选
let _rtDeviceFilter = '';      // Runtime 设备下拉筛选

function renderObservability() {
  const pc = document.getElementById('pageContent');
  if (!pc) return;
  pc.innerHTML = `
    <div class="cd" style="padding:0;overflow:hidden">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--b)">
        <h2 style="font-size:14px;font-weight:600;display:flex;align-items:center;gap:7px">📊 系统监控</h2>
        <div style="display:flex;align-items:center;gap:10px">
          <span class="ts t3" id="obsLastUpdate">-</span>
          <button class="bt bt-o bt-s" onclick="refreshObs()">🔄 刷新</button>
          <label class="ts t3" style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="obsAuto" onchange="toggleObsAuto()" checked style="margin:0">自动(10s)</label>
        </div>
      </div>
      <div class="stabs" style="padding:0 20px;border-bottom:1px solid var(--b)">
        <a class="${_obsTab==='overview'?'ac':''}" onclick="switchObsTab('overview',event)">' + I18N.t('observability.tab.overview') + '</a>
        <a class="${_obsTab==='tasks'?'ac':''}" onclick="switchObsTab('tasks',event)">' + I18N.t('observability.tab.tasks') + '</a>
        <a class="${_obsTab==='anomaly'?'ac':''}" onclick="switchObsTab('anomaly',event)">' + I18N.t('observability.tab.anomaly') + '</a>
        <a class="${_obsTab==='errorlogs'?'ac':''}" onclick="switchObsTab('errorlogs',event)">' + I18N.t('observability.tab.errorlogs') + '</a>
      </div>
      <div id="obsContent" style="padding:20px">'<div class="loading">' + I18N.t('common.loading') + '</div>'</div>
    </div>
  `;
  refreshObs();
  startObsAuto();
}

function switchObsTab(tab, ev) {
  _obsTab = tab;
  document.querySelectorAll('.stabs a').forEach(a => a.classList.remove('ac'));
  if (ev) ev.target.classList.add('ac');
  // 切换 tab 时重置渲染状态，强制重建 DOM 骨架
  _obsRendered = false;
  _obsRenderedTab = null;
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
    c.innerHTML = '<div class="loading">' + I18N.t('common.loading') + '</div>';
  }
  // 手动刷新（非自动）时触发引擎探测（60s 防抖由后端控制）
  if (!isAutoRefresh) {
    API.get('/dashboard/ops/probe-now').catch(() => null);
  }
  // 获取当前选中仓库，过滤仅该仓库的任务
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '?repo=' + encodeURIComponent(repo.fullName) : '';
  Promise.all([
    API.get('/dashboard/ops/overview' + repoParam).catch(() => null),
    fetch('/health/realtime').then(r => r.json()).catch(() => null),
    fetch('/metrics').then(r => r.text()).catch(() => null),
  ]).then(([ops, realtime, metricsText]) => {
    _obsData = { ops, realtime, metricsText };
    const now = new Date().toLocaleTimeString('zh-CN');
    const lu = document.getElementById('obsLastUpdate');
    if (lu) lu.textContent = now;
    renderObsContent(isAutoRefresh);
  });
}

function _rerenderRuntimeTable() {
  // 只重新渲染表格行（不重建筛选栏，避免 input 失焦）
  const { ops } = _obsData;
  if (!ops || ops.error) return;
  const r = ops.runtimes || {};
  const runtimes = (r.list || []).slice().sort((a, b) => (a.device_name || '').localeCompare(b.device_name || ''));
  const _filteredRuntimes = runtimes.filter(rt => {
    const _dev = rt.device_name || '';
    if (_rtDeviceFilter && _dev !== _rtDeviceFilter) return false;
    if (_rtSearch) {
      const _q = _rtSearch.toLowerCase();
      if (!(rt.name || '').toLowerCase().includes(_q) &&
          !(rt.device_name || '').toLowerCase().includes(_q) &&
          !(rt.version || '').toLowerCase().includes(_q) &&
          !(rt.mode || '').toLowerCase().includes(_q)) return false;
    }
    return true;
  });
  // 只更新表格行容器 + 计数
  const rowsEl = document.getElementById('obs-runtime-rows');
  const countEl = document.getElementById('obs-runtime-count');
  if (countEl) countEl.textContent = _filteredRuntimes.length + '/' + runtimes.length;
  if (!rowsEl) return;
  let html = '';
  if (_filteredRuntimes.length) {
    html += '<div class="tw"><table><thead><tr><th>' + I18N.t('observability.col_engine') + '</th><th>版本</th><th>健康度</th><th>繁忙</th><th>平均耗时</th><th>探测</th><th>模式</th><th>设备</th></tr></thead><tbody>';
    _filteredRuntimes.forEach(rti => {
      const rid = rti.id;
      const dot = _OBS_HEALTH_DOT[rti.health] || '⚫';
      const hc = _OBS_HEALTH_COLOR[rti.health] || 'var(--t3)';
      const running = rti.running || 0;
      const maxC = rti.max_concurrent || 0;
      const loadColor = running >= maxC * 2 ? 'var(--r)' : running >= maxC ? 'var(--y)' : running > 0 ? 'var(--g)' : 'var(--t3)';
      const modeTag = rti.mode === 'local' ? '<span class="tg tg-ch">本机</span>' : '<span class="tg tg-c">远端</span>';
      const avgDur = rti.avg_duration || 0;
      const avgDurStr = avgDur > 60 ? Math.round(avgDur/60) + 'm' + Math.round(avgDur%60) + 's' : avgDur + 's';
      const avgDurColor = avgDur > 300 ? 'var(--r)' : avgDur > 60 ? 'var(--y)' : 'var(--g)';
      const ps = rti.probe_status || 'pending';
      const pl = rti.probe_latency || 0;
      const pe = rti.probe_error || '';
      const probeIcon = { ok: '✅', fail: '❌', skip: '⏭', pending: '⏳' }[ps] || '⏳';
      const probeColor = ps === 'ok' ? 'var(--g)' : ps === 'fail' ? 'var(--r)' : 'var(--t3)';
      const probeStr = ps === 'ok' ? pl + 's' : ps === 'pending' ? I18N.t('observability.checking') : ps === 'skip' ? I18N.t('observability.skip') : I18N.t('observability.failed');
      const probeTitle = pe ? ' title="' + pe.replace(/"/g, '&quot;') + '"' : '';
      html += `<tr id="rt-${rid}" style="cursor:pointer" onclick="openRuntimeSessionModal(${rid}, '${(rti.name||'').replace(/'/g,"")}')">
        <td>${dot} <b>${rti.name}</b></td>
        <td><span class="ts t3">${rti.version || '-'}</span></td>
        <td><span style="color:${hc};font-weight:600">${rti.health}</span></td>
        <td><span style="color:${loadColor}">${running}/${maxC}</span></td>
        <td><span style="color:${avgDurColor}">${avgDur ? avgDurStr : '-'}</span></td>
        <td><span${probeTitle} style="color:${probeColor}">${probeIcon} ${probeStr}</span></td>
        <td>${modeTag}</td>
        <td><span class="ts t3">${rti.device_name || '-'}</span></td>
      </tr>`;
    });
    html += '</tbody></table></div>';
  } else {
    html += '<div class="ts t2" style="padding:12px">' + I18N.t('observability.no_matching_log') + '</div>';
  }
  rowsEl.innerHTML = html;
}

function renderObsContent(isAutoRefresh) {
  const c = document.getElementById('obsContent');
  if (!c) return;
  const { ops, realtime, metricsText } = _obsData;

  // 降级：ops 401 时从 realtime 补齐
  const realOps = (ops && !ops.error) ? ops : fallbackOps(realtime);
  const rt = realtime || {};
  const mt = metricsText || '';

  // 错误日志 Tab: 独立异步加载，只首次加载 + 手动刷新，定时器不触发
  if (_obsTab === 'errorlogs') {
    if (!isAutoRefresh) loadErrorLogs(c);
    return;
  }
  // 异常任务 Tab: 数据量小，整体替换可接受
  if (_obsTab === 'anomaly') {
    renderAnomalyTab(c, realOps);
    _obsRendered = false;  // anomaly 不走局部更新，每次重建
    return;
  }

  // overview / tasks Tab: 首次用 innerHTML 建骨架，后续走局部更新
  const canPartialUpdate = isAutoRefresh && _obsRendered && _obsRenderedTab === _obsTab;

  if (_obsTab === 'overview') {
    if (canPartialUpdate) {
      // 尝试局部更新；元素缺失（如新增 worker）时回退到完整渲染
      if (!updateOverview(realOps, rt, mt)) {
        c.innerHTML = renderOverview(realOps, rt, mt);
        _obsRendered = true;
        _obsRenderedTab = _obsTab;
      }
    } else {
      c.innerHTML = renderOverview(realOps, rt, mt);
      _obsRendered = true;
      _obsRenderedTab = _obsTab;
    }
  }
  else if (_obsTab === 'tasks') {
    if (canPartialUpdate) {
      if (!updateTasks(realOps, rt, mt)) {
        c.innerHTML = renderTasksTab(realOps, rt, mt);
        _obsRendered = true;
        _obsRenderedTab = _obsTab;
      }
    } else {
      c.innerHTML = renderTasksTab(realOps, rt, mt);
      _obsRendered = true;
      _obsRenderedTab = _obsTab;
    }
  }
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

function table(headers, rows) {
  return `<div class="tw" style="border:1px solid var(--b);border-radius:10px;overflow:hidden;margin-bottom:16px"><table><thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

// ===== Tab: 概览 =====
// 健康度 → 圆点/颜色映射（供 renderOverview 和 updateOverview 共享）
const _OBS_HEALTH_DOT = { online: '🟢', idle: '🟢', normal: '🟢', busy: '🟡', overload: '🔴', recently_lost: '🟡', offline: '⚫', about_to_gc: '🔴' };
const _OBS_HEALTH_COLOR = { online: 'var(--g)', idle: 'var(--g)', normal: 'var(--g)', busy: 'var(--y)', overload: 'var(--r)', recently_lost: 'var(--y)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };

// 资源进度条（首次渲染时带 id，供后续局部更新）
function _barHTML(label, pct, color, idPrefix) {
  const c = pct > 80 ? 'var(--r)' : pct > 60 ? 'var(--y)' : color || 'var(--g)';
  return `<div style="display:flex;align-items:center;gap:6px;font-size:10px">
    <span style="color:var(--t3);min-width:32px">${label}</span>
    <div style="flex:1;height:8px;background:var(--bg);border-radius:4px;overflow:hidden;min-width:60px">
      <div id="${idPrefix}-bar" style="height:100%;width:${Math.min(pct,100)}%;background:${c};border-radius:4px;transition:width .3s"></div>
    </div>
    <span id="${idPrefix}-val" style="color:${c};font-weight:600;min-width:30px;text-align:right">${Math.round(pct)}%</span>
  </div>`;
}

function renderOverview(ops, rt, mt) {
  const r = ops.runtimes || {};
  const w = ops.workers || {};

  // Worker 卡片（带 id 供局部更新）— 按 device_name 排序保证位置稳定
  let workerCards = '';
  const workers = (w.list || []).slice().sort((a, b) => {
    const na = (a.device_name || a.name || '').toLowerCase();
    const nb = (b.device_name || b.name || '').toLowerCase();
    return na < nb ? -1 : na > nb ? 1 : 0;
  });
  if (workers.length === 0) {
    workerCards = '<div class="ts t2" style="padding:12px">无 Worker</div>';
  } else {
    workers.forEach(wk => {
      const wid = wk.id != null ? wk.id : (wk.device_name || wk.name || '').replace(/[^a-zA-Z0-9_-]/g, '_');
      const dot = _OBS_HEALTH_DOT[wk.health] || '⚫';
      const hc = _OBS_HEALTH_COLOR[wk.health] || 'var(--t3)';
      const isMain = wk.last_ip === '127.0.0.1' || (wk.name || '').startsWith('grape-main');
      const mainTag = isMain ? '<span style="font-size:9px;color:#0e7490;background:#cffafe;border-radius:999px;padding:1px 6px;margin-left:4px">主控</span>' : '';
      workerCards += `<div id="wk-${wid}" style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span id="wk-${wid}-dot" style="font-size:14px">${dot}</span>
          <b style="font-size:13px">${wk.device_name || wk.name || 'Worker #' + wk.id}</b>${mainTag}
          <span id="wk-${wid}-health" style="margin-left:auto;font-size:10px;color:${hc};font-weight:600">${wk.health}</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 12px;font-size:10px;color:var(--t3);margin-bottom:8px">
          <div>IP: ${wk.last_ip || '-'}</div>
          <div>心跳: <span id="wk-${wid}-seen">${(wk.last_seen_at||'-').substring(11,19)}</span></div>
        </div>
        ${_barHTML('CPU', wk.cpu_percent, null, 'wk-' + wid + '-cpu')}
        ${_barHTML('MEM', wk.memory_percent, null, 'wk-' + wid + '-mem')}
        ${_barHTML('TMP', wk.tmp_percent, null, 'wk-' + wid + '-tmp')}
        ${_barHTML('DISK', wk.disk_percent, null, 'wk-' + wid + '-disk')}
      </div>`;
    });
  }

  // Runtime 表格（行带 id，各 td 带 id 供局部更新）
  const runtimes = r.list || [];
  // 按设备名排序
  runtimes.sort((a, b) => (a.device_name || '').localeCompare(b.device_name || ''));
  // 设备列表（去重，供下拉框）
  const _devices = [...new Set(runtimes.map(rt => rt.device_name || '-').filter(d => d && d !== '-'))];
  // 应用筛选
  const _filteredRuntimes = runtimes.filter(rt => {
    const _dev = rt.device_name || '';
    if (_rtDeviceFilter && _dev !== _rtDeviceFilter) return false;
    if (_rtSearch) {
      const _q = _rtSearch.toLowerCase();
      if (!(rt.name || '').toLowerCase().includes(_q) &&
          !(rt.device_name || '').toLowerCase().includes(_q) &&
          !(rt.version || '').toLowerCase().includes(_q) &&
          !(rt.mode || '').toLowerCase().includes(_q)) return false;
    }
    return true;
  });
  let runtimeTable = '<div class="ts t2" style="padding:12px">' + I18N.t('observability.no_engine') + '</div>';
  if (runtimes.length) {
    // 筛选栏
    let _filterBar = '<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap">';
    _filterBar += '<input type="text" placeholder="搜索引擎/设备/版本..." value="' + _rtSearch.replace(/"/g,'&quot;') + '" oninput="_rtSearch=this.value;_rerenderRuntimeTable()" style="flex:1;min-width:200px;padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c)">';
    _filterBar += '<select onchange="_rtDeviceFilter=this.value;_rerenderRuntimeTable()" style="padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c)">';
    _filterBar += '<option value="">全部设备</option>';
    _devices.forEach(d => {
      _filterBar += '<option value="' + d + '"' + (_rtDeviceFilter === d ? ' selected' : '') + '>' + d + '</option>';
    });
    _filterBar += '</select>';
    _filterBar += '<span class="ts t3" id="obs-runtime-count">' + _filteredRuntimes.length + '/' + runtimes.length + '</span>';
    _filterBar += '</div>';
    runtimeTable = _filterBar;
    runtimeTable += '<div id="obs-runtime-rows">';
    if (_filteredRuntimes.length) {
    runtimeTable += '<div class="tw"><table><thead><tr><th>' + I18N.t('observability.col_engine') + '</th><th>版本</th><th>健康度</th><th>繁忙</th><th>平均耗时</th><th>探测</th><th>模式</th><th>设备</th></tr></thead><tbody>';
    _filteredRuntimes.forEach(rti => {
      const rid = rti.id;
      const dot = _OBS_HEALTH_DOT[rti.health] || '⚫';
      const hc = _OBS_HEALTH_COLOR[rti.health] || 'var(--t3)';
      const running = rti.running || 0;
      const maxC = rti.max_concurrent || 0;
      const loadColor = running >= maxC * 2 ? 'var(--r)' : running >= maxC ? 'var(--y)' : running > 0 ? 'var(--g)' : 'var(--t3)';
      const modeTag = rti.mode === 'local' ? '<span class="tg tg-ch">本机</span>' : '<span class="tg tg-c">远端</span>';
      const avgDur = rti.avg_duration || 0;
      const avgDurStr = avgDur > 60 ? Math.round(avgDur/60) + 'm' + Math.round(avgDur%60) + 's' : avgDur + 's';
      const avgDurColor = avgDur > 300 ? 'var(--r)' : avgDur > 60 ? 'var(--y)' : 'var(--g)';
      // 探测状态
      const ps = rti.probe_status || 'pending';
      const pl = rti.probe_latency || 0;
      const pe = rti.probe_error || '';
      const probeIcon = { ok: '✅', fail: '❌', skip: '⏭', pending: '⏳' }[ps] || '⏳';
      const probeColor = ps === 'ok' ? 'var(--g)' : ps === 'fail' ? 'var(--r)' : 'var(--t3)';
      const probeStr = ps === 'ok' ? pl + 's' : ps === 'pending' ? I18N.t('observability.checking') : ps === 'skip' ? I18N.t('observability.skip') : I18N.t('observability.failed');
      const probeTitle = pe ? ' title="' + pe.replace(/"/g, '&quot;') + '"' : '';
      runtimeTable += `<tr id="rt-${rid}" style="cursor:pointer" onclick="openRuntimeSessionModal(${rid}, '${(rti.name||'').replace(/'/g,"")}')">
        <td>${dot} <b>${rti.name}</b></td>
        <td><span id="rt-${rid}-version" class="ts t3">${rti.version || '-'}</span></td>
        <td><span id="rt-${rid}-health" style="color:${hc};font-weight:600">${rti.health}</span></td>
        <td><span id="rt-${rid}-load" style="color:${loadColor}">${running}/${maxC}</span></td>
        <td><span id="rt-${rid}-dur" style="color:${avgDurColor}">${avgDur ? avgDurStr : '-'}</span></td>
        <td><span${probeTitle} style="color:${probeColor}">${probeIcon} ${probeStr}</span></td>
        <td>${modeTag}</td>
        <td><span id="rt-${rid}-device" class="ts t3">${rti.device_name || '-'}</span></td>
      </tr>`;
    });
    runtimeTable += '</tbody></table></div>';
    } else {
      runtimeTable += '<div class="ts t2" style="padding:12px">' + I18N.t('observability.no_matching_log') + '</div>';
    }
    runtimeTable += '</div>';
  }

  return `
    ${section(I18N.t('observability.worker_monitor'), (w.online || 0) + '/' + (w.total || 0) + ' ' + I18N.t('observability.online'))}
    ${_kpiRowHTML([
      { label: I18N.t('observability.worker_total'), value: w.total || 0, color: 'var(--a)', id: 'kpi-w-total' },
      { label: I18N.t('observability.online'), value: w.online || 0, color: 'var(--g)', id: 'kpi-w-online' },
      { label: I18N.t('observability.offline'), value: w.offline || 0, color: 'var(--t3)', id: 'kpi-w-offline' },
      { label: I18N.t('observability.engine_total'), value: r.total || 0, color: 'var(--pu)', id: 'kpi-r-total' },
    ])}
    <div id="wk-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-bottom:16px">
      ${workerCards}
    </div>
    ${section(I18N.t('observability.runtime_engine'), I18N.t('observability.runtime_subtitle', {online: (r.online || 0), total: (r.total || 0)}))}
    <div id="obs-runtime-table">${runtimeTable}</div>
  `;
}

// ===== 局部更新：概览 Tab =====
// 返回 true=成功局部更新, false=元素缺失需回退到完整渲染
function updateOverview(ops, rt, mt) {
  const r = ops.runtimes || {};
  const w = ops.workers || {};

  // 1) 更新 Worker KPI 卡片数值
  _setText('kpi-w-total', w.total || 0);
  _setText('kpi-w-online', w.online || 0);
  _setText('kpi-w-offline', w.offline || 0);
  _setText('kpi-r-total', r.total || 0);

  // 2) 更新 Worker 卡片
  const workers = w.list || [];
  if (workers.length) {
    for (const wk of workers) {
      const wid = wk.id != null ? wk.id : (wk.device_name || wk.name || '').replace(/[^a-zA-Z0-9_-]/g, '_');
      const card = document.getElementById('wk-' + wid);
      if (!card) return false;  // 新增 worker → 回退完整渲染

      const dot = _OBS_HEALTH_DOT[wk.health] || '⚫';
      const hc = _OBS_HEALTH_COLOR[wk.health] || 'var(--t3)';

      const dotEl = document.getElementById('wk-' + wid + '-dot');
      if (dotEl) dotEl.textContent = dot;

      const healthEl = document.getElementById('wk-' + wid + '-health');
      if (healthEl) { healthEl.textContent = wk.health; healthEl.style.color = hc; }

      const seenEl = document.getElementById('wk-' + wid + '-seen');
      if (seenEl) seenEl.textContent = (wk.last_seen_at || '-').substring(11, 19);

      // 进度条
      _updateBar('wk-' + wid + '-cpu', wk.cpu_percent);
      _updateBar('wk-' + wid + '-mem', wk.memory_percent);
      _updateBar('wk-' + wid + '-tmp', wk.tmp_percent);
      _updateBar('wk-' + wid + '-disk', wk.disk_percent);
    }
  }

  // 3) 更新 Runtime 表格行
  const runtimes = r.list || [];
  if (runtimes.length) {
    for (const rti of runtimes) {
      const rid = rti.id;
      const tr = document.getElementById('rt-' + rid);
      if (!tr) return false;  // 新增 runtime → 回退完整渲染

      const hc = _OBS_HEALTH_COLOR[rti.health] || 'var(--t3)';
      const running = rti.running || 0;
      const maxC = rti.max_concurrent || 0;
      const loadColor = running >= maxC * 2 ? 'var(--r)' : running >= maxC ? 'var(--y)' : running > 0 ? 'var(--g)' : 'var(--t3)';
      const avgDur = rti.avg_duration || 0;
      const avgDurStr = avgDur > 60 ? Math.round(avgDur/60) + 'm' + Math.round(avgDur%60) + 's' : avgDur + 's';
      const avgDurColor = avgDur > 300 ? 'var(--r)' : avgDur > 60 ? 'var(--y)' : 'var(--g)';

      const healthEl = document.getElementById('rt-' + rid + '-health');
      if (healthEl) { healthEl.textContent = rti.health; healthEl.style.color = hc; }

      const loadEl = document.getElementById('rt-' + rid + '-load');
      if (loadEl) { loadEl.textContent = running + '/' + maxC; loadEl.style.color = loadColor; }

      const durEl = document.getElementById('rt-' + rid + '-dur');
      if (durEl) { durEl.textContent = avgDur ? avgDurStr : '-'; durEl.style.color = avgDurColor; }

      const versionEl = document.getElementById('rt-' + rid + '-version');
      if (versionEl) versionEl.textContent = rti.version || '-';

      const deviceEl = document.getElementById('rt-' + rid + '-device');
      if (deviceEl) deviceEl.textContent = rti.device_name || '-';
    }
  }

  return true;
}

// ===== 局部更新：任务 Tab =====
// 返回 true=成功局部更新, false=需回退到完整渲染
function updateTasks(ops, rt, mt) {
  const t = ops.tasks || {};
  const sc = t.status_counts || {};

  _setText('kpi-t-queued', sc.queued || 0);
  _setText('kpi-t-dispatched', sc.dispatched || 0);
  _setText('kpi-t-running', sc.running || 0);
  _setText('kpi-t-completed', sc.completed || 0);
  _setText('kpi-t-failed', sc.failed || 0);
  _setText('kpi-t-cancelled', sc.cancelled || 0);
  _setText('kpi-t-inprogress', t.in_progress || 0);
  _setText('kpi-t-stuck', t.stuck_count || 0);

  // 更新 Failed/Stuck 颜色（根据计数动态变色）
  const failedEl = document.getElementById('kpi-t-failed');
  if (failedEl) failedEl.style.color = (sc.failed || 0) > 0 ? 'var(--r)' : 'var(--t3)';
  const stuckEl = document.getElementById('kpi-t-stuck');
  if (stuckEl) stuckEl.style.color = (t.stuck_count || 0) > 0 ? 'var(--r)' : 'var(--g)';

  return true;
}

// ===== 局部更新辅助函数 =====
function _setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function _updateBar(idPrefix, pct) {
  const bar = document.getElementById(idPrefix + '-bar');
  const val = document.getElementById(idPrefix + '-val');
  if (bar) bar.style.width = Math.min(pct, 100) + '%';
  const c = pct > 80 ? 'var(--r)' : pct > 60 ? 'var(--y)' : 'var(--g)';
  if (bar) bar.style.background = c;
  if (val) { val.textContent = Math.round(pct) + '%'; val.style.color = c; }
}

// A2A 健康检查（概览 tab 内异步加载）
function loadA2aHealth() {
  const container = document.getElementById('obs-a2a-health');
  if (!container) return;
  container.innerHTML = section(I18N.t('observability.a2a_health'), I18N.t('observability.a2a_health_subtitle')) + '<div class="loading">检查中...</div>';
  fetch('/api/dashboard/ops/a2a-health').then(r => r.json()).then(d => {
    const status = d.reachable
      ? `<span style="color:var(--g)">✓ 可达</span> (${d.latency_ms}ms)`
      : `<span style="color:var(--r)">✗ 不可达</span> ${d.error ? '<span class="ts t3">'+d.error.substring(0,80)+'</span>' : ''}`;
    let html = section(I18N.t('observability.a2a_health'), I18N.t('observability.a2a_health_subtitle'));
    html += kpiRow([
      { label: 'Server URL', value: `<span class="ts" style="font-size:10px">${d.server_base_url}</span>`, color: 'var(--a)' },
      { label: '状态', value: status, color: d.reachable ? 'var(--g)' : 'var(--r)' },
      { label: '延迟', value: d.latency_ms ? d.latency_ms + 'ms' : '-', color: 'var(--sk)' },
    ]);
    if (d.recent_a2a && d.recent_a2a.length) {
      html += table(['Seq', 'Type', 'Repo', 'Issue', 'Status', 'Time'],
        d.recent_a2a.map(a => [
          a.seq, a.event_type.substring(0,20), (a.repo_key||'').replace('gitcode:','').substring(0,15),
          a.issue_id || '-', a.status || '-', `<span class="ts t3">${(a.created_at||'').substring(0,19)}</span>`
        ])
      );
    }
    container.innerHTML = html;
  }).catch(e => {
    container.innerHTML = section(I18N.t('observability.a2a_health')) + '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

// ===== Tab: 异常任务 =====
let _anomalyPage = 0;
const _ANOMALY_PAGE_SIZE = 10;

function renderAnomalyTab(container, ops) {
  const stuck = (ops.tasks || {}).stuck_tasks || [];
  const failed = (ops.tasks || {}).failed_tasks || [];
  if (!stuck.length && !failed.length) {
    container.innerHTML = '<div class="ts t2" style="padding:40px;text-align:center">🎉 无异常任务</div>';
    return;
  }

  const _enc = s => encodeURIComponent(JSON.stringify({
    issue:s.issue_id, seq:s.event_seq, repo:(s.repo_key||'').replace('gitcode:',''),
    created:s.completed_at||s.started_at||'',
    errors:s.errors||[], exit_code:s.exit_code, result_text:s.result_text||''
  }));
  const _row = (s, showFailed) => {
    const is = (s.issue_id && s.event_seq) ? `${s.issue_id} #${s.event_seq}` : s.id.substring(0,12);
    const time = showFailed ? (s.completed_at||'') : (s.started_at||'');
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
    html += section('🔴 卡住的任务', `${stuck.length} 个 — running > 15min`);
    html += table(['Issue/Seq', 'Agent', 'Repo', 'Started', '状态'],
      stuck.map(s => _row(s, false))
    );
  }

  // Failed 任务（分页）
  if (failed.length) {
    const totalPages = Math.ceil(failed.length / _ANOMALY_PAGE_SIZE);
    if (_anomalyPage >= totalPages) _anomalyPage = 0;
    const start = _anomalyPage * _ANOMALY_PAGE_SIZE;
    const pageItems = failed.slice(start, start + _ANOMALY_PAGE_SIZE);

    html += section('❌ 失败的任务', `共 ${failed.length} 条 — 第 ${_anomalyPage+1}/${totalPages} 页`);
    html += table(['Issue/Seq', 'Agent', 'Repo', 'Failed', '结果'],
      pageItems.map(s => _row(s, true))
    );

    // 分页栏
    if (totalPages > 1) {
      html += `<div style="display:flex;align-items:center;justify-content:center;gap:10px;padding:12px 0">
        <button class="bt bt-o bt-s" ${_anomalyPage === 0 ? 'disabled style="opacity:.4"' : ''} onclick="_anomalyPage--;refreshObs()">‹ 上一页</button>
        <span class="ts t3">${_anomalyPage+1} / ${totalPages}</span>
        <button class="bt bt-o bt-s" ${_anomalyPage >= totalPages-1 ? 'disabled style="opacity:.4"' : ''} onclick="_anomalyPage++;refreshObs()">下一页 ›</button>
      </div>`;
    }
  }

  container.innerHTML = html;
}

function loadErrorLogs(container) {
  if (!container) return;
  let currentLevel = _errorLogLevel || '';
  let currentSource = _errorLogSource || '';
  container.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">
      <div style="display:flex;gap:0">
        <button class="bt bt-s ${currentLevel===''?'bt-p':'bt-o'}" onclick="_errorLogLevel='';loadErrorLogs(document.getElementById('obsContent'))">全部</button>
        <button class="bt bt-s ${currentLevel==='ERROR'?'bt-p':'bt-o'}" onclick="_errorLogLevel='ERROR';loadErrorLogs(document.getElementById('obsContent'))">仅 ERROR</button>
        <button class="bt bt-s ${currentLevel==='WARNING'?'bt-p':'bt-o'}" onclick="_errorLogLevel='WARNING';loadErrorLogs(document.getElementById('obsContent'))">仅 WARNING</button>
      </div>
      <select id="errorLogSource" class="bt bt-s bt-o" style="padding:4px 8px" onchange="_errorLogSource=this.value;loadErrorLogs(document.getElementById('obsContent'))">
        <option value=""'+' + I18N.t('observability.log_all_sources') + '</option>
      </select>
      <span class="ts t3" id="errorLogCount"></span>
    </div>
    <div id="errorLogBody">'<div class="loading">' + I18N.t('common.loading') + '</div>'</div>
  `;
  // 加载来源列表
  fetch('/api/dashboard/ops/error-logs/sources').then(r => r.json()).then(d => {
    const sel = document.getElementById('errorLogSource');
    if (sel) {
      (d.sources || []).forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.source;
        opt.textContent = s.source;
        if (currentSource === s.source) opt.selected = true;
        sel.appendChild(opt);
      });
    }
  }).catch(() => {});
  const levelParam = currentLevel ? '&level=' + currentLevel : '';
  const sourceParam = currentSource ? '&source=' + encodeURIComponent(currentSource) : '';
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '&repo=' + encodeURIComponent(repo.fullName) : '';
  fetch('/api/dashboard/ops/error-logs?limit=100' + levelParam + sourceParam + repoParam).then(r => r.json()).then(d => {
    const entries = d.entries || [];
    const countEl = document.getElementById('errorLogCount');
    const bodyEl = document.getElementById('errorLogBody');
    if (countEl) countEl.textContent = entries.length + ' 条（来自 ' + (d.files ? d.files.length : 1) + ' 个日志文件）';
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
      html += '<div style="padding:4px 12px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#5b667b">' + time + '</span>' + srcTag + rid + ' <span style="color:' + color + '"><b>' + level + '</b></span> <span style="color:#e0e6f0">' + (e.message||'').substring(0,300) + '</span><span style="color:#8d97a8">' + extra + '</span></div>';
    });
    html += '</div>';
    if (bodyEl) bodyEl.innerHTML = html;
  }).catch(e => {
    const bodyEl = document.getElementById('errorLogBody');
    if (bodyEl) bodyEl.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}
let _errorLogLevel = '';
let _errorLogSource = '';

// ===== Tab: 任务 =====
function renderTasksTab(ops, rt, mt) {
  const t = ops.tasks || {};
  const sc = t.status_counts || {};
  let html = '';
  html += section('任务状态分布');
  html += _kpiRowHTML([
    { label: 'Queued', value: sc.queued || 0, color: 'var(--y)', id: 'kpi-t-queued' },
    { label: 'Dispatched', value: sc.dispatched || 0, color: 'var(--a)', id: 'kpi-t-dispatched' },
    { label: 'Running', value: sc.running || 0, color: 'var(--sk)', id: 'kpi-t-running' },
    { label: 'Completed', value: sc.completed || 0, color: 'var(--g)', id: 'kpi-t-completed' },
    { label: 'Failed', value: sc.failed || 0, color: (sc.failed||0) > 0 ? 'var(--r)' : 'var(--t3)', id: 'kpi-t-failed' },
    { label: 'Cancelled', value: sc.cancelled || 0, color: 'var(--t3)', id: 'kpi-t-cancelled' },
    { label: 'In Progress', value: t.in_progress || 0, color: 'var(--pu)', id: 'kpi-t-inprogress' },
    { label: 'Stuck', value: t.stuck_count || 0, color: (t.stuck_count||0) > 0 ? 'var(--r)' : 'var(--g)', id: 'kpi-t-stuck' },
  ]);
  html += '<div id="obs-timeline-container"><div class="loading">加载任务列表...</div></div>';
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoFullName = repo && repo.fullName ? repo.fullName : '';
  // 使用缓存的 timeline 数据（同仓库且 30s 内）
  if (_timelineCache && _timelineCache.repo === repoFullName && (Date.now() - _timelineCache.ts) < 30000) {
    setTimeout(() => renderTimelineList(document.getElementById('obs-timeline-container'), _timelineCache.issues, _timelineCache.repoKey), 0);
  } else {
    const repoParam = repoFullName ? '?repo=' + encodeURIComponent(repoFullName) : '';
    fetch('/api/dashboard/ops/timeline' + repoParam).then(r => r.json()).then(d => {
      _timelineCache = { repo: repoFullName, ts: Date.now(), issues: d.issues || [], repoKey: d.repoKey || '' };
      _timelineIssues = d.issues || [];
      renderTimelineList(document.getElementById('obs-timeline-container'), d.issues || [], d.repoKey || '');
    }).catch(e => {
      const c = document.getElementById('obs-timeline-container');
      if (c) c.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
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
  if (t === 'a2a_event') return 'A2A 交办';
  return { issue_opened: 'issue 打开', issue_comment: '评论', issue_updated: 'issue 更新', pull_request_opened: 'PR 打开', note_hook_pr: 'PR 评论', issue_closed: 'issue 关闭', push: '推送' }[t] || t;
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
    const t = ev.createdAt ? ev.createdAt.slice(11, 19) : '';
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
  box.innerHTML = '<span class="ts t3">渲染中…</span>';
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
    box.innerHTML = '<div style="padding:12px;color:var(--r);font-size:12px">时序渲染失败: ' + (err.message || err) + '</div>';
    // 显示原始 mermaid 文本供调试
    const { text } = _buildSequence(iss);
    box.innerHTML += '<pre style="padding:8px;background:#0c1222;color:#e0e6f0;font-size:10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap">' + text.replace(/</g,'&lt;') + '</pre>';
  }
}

function _bindMermaidClicks(box, iss, evTypes) {
  // mermaid 渲染时 message 和 Note 分组排列（不是交替的），
  // 需要按 evTypes 分别匹配 messageText 和 noteText
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
    const _devName = _eng.split('-').slice(1).join('-');  // opencode-ecs-... → ecs-...
    const _ip = _devIpMap[_devName] || '';
    const _tooltip = _eng + (_ip ? ' (' + _ip + ')' : '');
    if (_tooltip) {
      const _t = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      _t.textContent = _tooltip;
      txt.appendChild(_t);
    }
    if (!ev.taskId) return;
    txt.style.cursor = 'pointer';
    txt.style.textDecoration = 'underline';
    txt.style.fill = 'var(--a)';
    const enc = encodeURIComponent(JSON.stringify({issue:iss.issueId, seq:ev.seq, created:ev.createdAt||''}));
    txt.addEventListener('click', function(e) {
      e.stopPropagation();
      openTaskLogModal(ev.taskId, enc);
    });
  });
}

function renderTimelineList(container, issues, repoKey) {
  if (!issues.length) { container.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">' + I18N.t('observability.no_task_events') + '</div>'; return; }
  _timelineIssues = issues;
  // 构建 device_name → IP 映射（从 ops/overview workers 数据）
  const _devIpMap = {};
  try {
    const _workers = (_obsData.ops && _obsData.ops.workers && _obsData.ops.workers.list) || [];
    _workers.forEach(w => { if (w.device_name) _devIpMap[w.device_name] = w.last_ip || ''; });
  } catch(e) {}
  function _engineWithIp(engine) {
    if (!engine) return '-';
    const _devName = engine.split('-').slice(1).join('-');
    const _ip = _devIpMap[_devName];
    return _ip ? engine + ' (' + _ip + ')' : engine;
  }
  // 构建 GitCode issue URL：repoKey=gitcode:requiredsa/hello-flask → https://gitcode.com/requiredsa/hello-flask/issues/N
  const _repoPath = (repoKey || '').replace(/^gitcode:/, '');
  function _issueUrl(issueId) {
    const num = (issueId || '').replace(/^#/, '');
    return _repoPath && num ? 'https://gitcode.com/' + _repoPath + '/issues/' + num : '';
  }
  let html = section(I18N.t('observability.task_list'), I18N.t('observability.task_list_subtitle', {count: issues.length}));
  html += '<div style="display:flex;flex-direction:column;gap:0">';
  issues.forEach(iss => {
    const expanded = _expandedIssues.has(iss.issueId);
    const hasFailed = iss.events.some(e => e.status === 'failed');
    const statusBadge = hasFailed ? '<span style="color:var(--r);font-size:10px;font-weight:600">\u274c 有失败</span>' : '<span style="color:var(--g);font-size:10px;font-weight:600">\u2713 ' + I18N.t('observability.normal') + '</span>';
    const _url = _issueUrl(iss.issueId);
    const _issueLink = _url
      ? '<a href="' + _url + '" target="_blank" onclick="event.stopPropagation()" style="font-size:13px;font-weight:700;color:var(--a);text-decoration:none">' + iss.issueId + ' \u2197</a>'
      : '<b style="font-size:13px">' + iss.issueId + '</b>';
    html += '<div style="border:1px solid var(--b);border-radius:8px;overflow:hidden;margin-bottom:8px">';
    html += '<div style="display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;background:var(--c)" onclick="toggleIssueTimeline(\'' + iss.issueId + '\')">';
    html += '<span style="font-size:12px">' + (expanded ? '\u25be' : '\u25b8') + '</span>';
    html += _issueLink;
    html += iss.title ? '<span class="ts t3" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + iss.title + '</span>' : '<span style="flex:1"></span>';
    html += '<span class="ts t3">' + iss.eventCount + '' + I18N.t('observability.col_event') + '</span>' + statusBadge;
    html += '<span class="ts t3">' + (iss.lastAt || '').substring(0,16) + '</span>';
    html += '</div>';
    html += '<div id="tl-body-' + iss.issueId + '" style="display:' + (expanded ? 'block' : 'none') + ';padding:10px 14px;border-top:1px solid var(--b)">';
    // 切换按钮
    html += '<div style="display:flex;gap:0;margin-bottom:10px">';
    html += '<button class="bt bt-s ' + (_issueViewMode[iss.issueId] !== 'events' ? 'bt-p' : 'bt-o') + '" onclick="switchIssueView(\'' + iss.issueId + '\',\'diagram\')">\u{1f4ca} 时序图</button>';
    html += '<button class="bt bt-s ' + (_issueViewMode[iss.issueId] === 'events' ? 'bt-p' : 'bt-o') + '" onclick="switchIssueView(\'' + iss.issueId + '\',\'events\')">\u{1f4cb} ' + I18N.t('observability.event_list') + '</button>';
    html += '</div>';
    // 时序图容器
    html += '<div id="tl-diagram-' + iss.issueId + '" class="tl-mmd" data-iid="' + iss.issueId + '" style="overflow-x:auto;' + (_issueViewMode[iss.issueId] === 'events' ? 'display:none' : '') + '"></div>';
    // 事件列表容器
    html += '<div id="tl-events-' + iss.issueId + '" style="' + (_issueViewMode[iss.issueId] !== 'events' ? 'display:none' : '') + '">';
    html += '<table style="width:100%;font-size:11px"><thead><tr><th>Seq</th><th>事件</th><th>Agent</th><th>引擎</th><th>Skill</th><th>状态</th><th>耗时</th><th>' + I18N.t('observability.col_time') + '</th></tr></thead><tbody>';
    html += iss.events.map(ev => {
      const enc = ev.taskId ? encodeURIComponent(JSON.stringify({issue:iss.issueId, seq:ev.seq, created:ev.createdAt||''})) : '';
      const clickable = ev.taskId ? 'cursor:pointer' : '';
      const onclick = ev.taskId ? " onclick=\"openTaskLogModal('" + ev.taskId + "','" + enc + "')\"" : '';
      return '<tr style="' + clickable + '"' + onclick + '><td class="ts">' + ev.seq + '</td><td>' + _actionText(ev.eventType) + '</td><td><b>' + (ev.agentType||'-') + '</b></td><td class="ts t3">' + _engineWithIp(ev.engine) + '</td><td class="ts t3">' + (ev.skill||'-') + '</td><td style="color:' + (ev.status==='failed'?'var(--r)':ev.status==='completed'?'var(--g)':'var(--t3)') + '">' + (ev.status||'-') + '</td><td class="ts t3">' + (ev.duration||'-') + 's</td><td class="ts t3">' + (ev.createdAt||'').substring(0,19) + '</td></tr>';
    }).join('');
    html += '</tbody></table></div>';
    html += '</div></div>';
  });
  html += '</div>';
  container.innerHTML = html;
  // 渲染已展开的 mermaid
  issues.forEach(iss => {
    if (_expandedIssues.has(iss.issueId) && _issueViewMode[iss.issueId] !== 'events') {
      const box = container.querySelector('#tl-diagram-' + iss.issueId);
      if (box) _renderMermaid(box, iss);
    }
  });
}

let _timelineIssues = [];
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

// ===== Prometheus 文本解析 =====
function parseMetric(text, name) {
  const m = text.match(new RegExp('^' + name + '\\s+(\\d+\\.?\\d*)', 'm'));
  return m ? parseFloat(m[1]) : 0;
}
function parseMetrics(text, names) {
  const r = {};
  for (const n of names) r[n] = parseMetric(text, n);
  return r;
}
function parseLabeledSum(text, name) {
  let total = 0;
  const re = new RegExp('^' + name + '\\{[^}]*\\}\\s+(\\d+\\.?\\d*)', 'gm');
  let m;
  while ((m = re.exec(text)) !== null) total += parseFloat(m[1]);
  return total;
}
function parseLabeledSumByLabel(text, name, key, val) {
  let total = 0;
  const re = new RegExp('^' + name + '\\{[^}]*' + key + '="' + val + '"[^}]*\\}\\s+(\\d+\\.?\\d*)', 'gm');
  let m;
  while ((m = re.exec(text)) !== null) total += parseFloat(m[1]);
  return total;
}
function fmtNum(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}
function parseLabeledKeys(text, name, labelKey) {
  const keys = [];
  const re = new RegExp('^' + name + '\\\\{[^}]*' + labelKey + '=\"([^\"]+)\"[^}]*\\\\}', 'gm');
  let m;
  while ((m = re.exec(text)) !== null) {
    if (!keys.includes(m[1])) keys.push(m[1]);
  }
  return keys;
}

// ===== Runtime 任务列表 + 日志弹窗（从 runtimes.js 迁移）=====

// ===== Runtime Session 列表弹窗 =====
function openRuntimeSessionModal(rtId, rtName) {
  const old = document.getElementById('rtSessionModal');
  if (old) old.remove();

  const modal = document.createElement('div');
  modal.id = 'rtSessionModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = '<div class="cd" style="width:760px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;padding:0;overflow:hidden">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">' +
    '<h3 style="font-size:14px;font-weight:600">' + rtName + ' ' + I18N.t('observability.session_list') + '</h3>' +
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
    document.getElementById('rtSessionModalBody').innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

function renderSessionList(container, sessions, rtName) {
  if (!sessions.length) {
    container.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">该引擎在此仓库下暂无 session</div>';
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
    if (!list.length) return '<div class="ts t3" style="padding:8px">无运行中 session</div>';
    let html = '<div class="tw"><table><thead><tr><th>Agent</th><th>Issue / Seq</th><th>Skill</th><th>' + I18N.t('observability.col_status') + '</th><th>开始时间</th></tr></thead><tbody>';
    list.forEach(s => {
      const ct = s.current_task;
      const is = (ct.issue_id && ct.event_seq) ? ct.issue_id + ' #' + ct.event_seq : (ct.event_seq ? '#' + ct.event_seq : ct.id.substring(0,12));
      const enc = encodeURIComponent(JSON.stringify({issue:ct.issue_id||'', seq:ct.event_seq||0, repo:s.repo_key, created:ct.started_at||''}));
      html += '<tr style="cursor:pointer" onclick="openTaskLogModal(\'' + ct.id + '\',\'' + enc + '\')">' +
        '<td><b>' + (s.agent_type||'-') + '</b></td>' +
        '<td class="ts"><b>' + is + '</b></td>' +
        '<td class="ts t3">' + (s.skill||'-') + '</td>' +
        '<td><span style="color:' + (taskStatusColor[ct.status]||'var(--a)') + '">' + (taskStatusIcon[ct.status]||'🔄') + ' ' + ct.status + '</span></td>' +
        '<td class="ts t3">' + (ct.started_at||'').substring(0,16) + '</td>' +
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
    if (!allTasks.length) return '<div class="ts t3" style="padding:8px">无已完成任务</div>';
    let html = '<div class="tw"><table><thead><tr><th>Issue / Seq</th><th>Agent</th><th>状态</th><th>' + I18N.t('observability.col_duration') + '</th><th>Exit</th><th>完成时间</th></tr></thead><tbody>';
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
        '<td>' + (t.exit_code != null ? t.exit_code : '-') + '</td>' +
        '<td class="ts t3">' + (t.completed_at||'').substring(0,16) + '</td>' +
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
  html += '<b style="font-size:12px">🔄 运行中</b>';
  html += '<span class="ts t3" style="margin-left:auto">' + running.length + ' 个 session</span>';
  html += '</div>';
  html += '<div id="rtSessionRunning" style="padding:8px 12px;border-top:1px solid var(--b)">' + runningTable(running) + '</div>';
  html += '</div>';

  // 已完成任务（默认收起）
  html += '<div style="border:1px solid var(--b);border-radius:8px;overflow:hidden;margin-bottom:10px">';
  html += '<div style="display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;background:var(--c)" onclick="toggleSessionGroup(\'rtSessionIdle\')">';
  html += '<span id="rtSessionIdle-arrow" style="font-size:12px">▸</span>';
  html += '<b style="font-size:12px">📋 已完成任务</b>';
  html += '<span class="ts t3" style="margin-left:auto">' + completedCount + ' 个任务</span>';
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
  const titleHtml = titleParts.length ? titleParts.join(' · ') : 'Session 日志';

  const modal = document.createElement('div');
  modal.id = 'rtLogModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1000';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = '<div class="cd" style="width:860px;max-width:94vw;max-height:85vh;display:flex;flex-direction:column;padding:0;overflow:hidden">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">' +
    '<h3 style="font-size:13px;font-weight:600">' + titleHtml + '</h3>' +
    '<button class="bt bt-o bt-s" onclick="document.getElementById(\'rtLogModal\').remove()">' + I18N.t('observability.close') + '</button>' +
    '</div>' +
    '<div id="rtLogModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">加载日志...</div></div>' +
    '</div>';
  document.body.appendChild(modal);

  // session 没有直接 task_id，查该 session 最近的任务
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '&repo=' + encodeURIComponent(repo.fullName) : '';
  // 先查该引擎 + 该仓库最近的任务
  // 使用 runtime tasks 端点找到最近的 task_id
  fetch('/api/runtime/runtimes/1/tasks?limit=10' + repoParam, {
    headers: { 'Authorization': 'Bearer ' + (localStorage.token || '') }
  }).then(r => r.json()).then(d => {
    const tasks = d.tasks || [];
    if (!tasks.length) {
      document.getElementById('rtLogModalBody').innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">该 session 无关联任务日志</div>';
      return;
    }
    // 取最近一个任务
    const task = tasks[0];
    const taskEnc = encodeURIComponent(JSON.stringify({issue:task.issue_id||'', seq:task.event_seq||0, repo:(task.repo_key||'').replace('gitcode:',''), created:task.created_at||''}));
    // 加载该任务的日志
    API.get('/task-logs/' + task.id + '/messages?since=0').then(msgs => {
      renderTaskLogMessages(document.getElementById('rtLogModalBody'), msgs || [], JSON.parse(decodeURIComponent(taskEnc)));
    }).catch(e => {
      document.getElementById('rtLogModalBody').innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
    });
  }).catch(e => {
    document.getElementById('rtLogModalBody').innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

function openRuntimeTaskModal(rtId, rtName) {
  const old = document.getElementById('rtTaskModal');
  if (old) old.remove();

  const modal = document.createElement('div');
  modal.id = 'rtTaskModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = `<div class="cd" style="width:720px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;padding:0;overflow:hidden">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">
      <h3 style="font-size:14px;font-weight:600">${rtName} · 任务列表</h3>
      <button class="bt bt-o bt-s" onclick="document.getElementById('rtTaskModal').remove()">' + I18N.t('observability.close') + '</button>'
    </div>
    <div id="rtTaskModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">加载任务...</div></div>
  </div>`;
  document.body.appendChild(modal);

  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '&repo=' + encodeURIComponent(repo.fullName) : '';
  API.get('/runtime/runtimes/' + rtId + '/tasks?limit=50' + repoParam).then(d => {
    renderRuntimeTaskList(document.getElementById('rtTaskModalBody'), d.tasks || [], rtName);
  }).catch(e => {
    document.getElementById('rtTaskModalBody').innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

function renderRuntimeTaskList(container, tasks, rtName) {
  if (!tasks.length) {
    container.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">暂无任务</div>';
    return;
  }
  const statusIcon = { queued: '⏳', dispatched: '🔄', running: '🔄', completed: '✅', failed: '❌', cancelled: '⛔' };
  const statusColor = { queued: 'var(--y)', dispatched: 'var(--a)', running: 'var(--a)', completed: 'var(--g)', failed: 'var(--r)', cancelled: 'var(--t3)' };
  let html = `<div style="font-size:11px;color:var(--t3);margin-bottom:8px">${rtName} · ${tasks.length} 个任务（点击行查看日志）</div>`;
  html += '<div class="tw"><table><thead><tr><th>Issue / Seq</th><th>Agent / Skill</th><th>Repo</th><th>Status</th><th>Exit</th><th>Created</th></tr></thead><tbody>';
  tasks.forEach(t => {
    const issueSeq = (t.issue_id && t.event_seq) ? `${t.issue_id} #${t.event_seq}` : (t.event_seq ? `#${t.event_seq}` : t.id.substring(0,12));
    const infoEnc = encodeURIComponent(JSON.stringify({issue:t.issue_id||'', seq:t.event_seq||0, repo:(t.repo_key||'').replace('gitcode:',''), created:t.created_at||''}));
    html += `<tr style="cursor:pointer" onclick="openTaskLogModal('${t.id}', '${infoEnc}')">
      <td class="ts"><b>${issueSeq}</b></td>
      <td><b>${t.agent_type || '-'}</b><br><span class="ts t3">${t.skill || '-'}</span></td>
      <td class="ts t3">${(t.repo_key || '').replace('gitcode:','').substring(0,20)}</td>
      <td><span style="color:${statusColor[t.status] || 'var(--t3)'}">${statusIcon[t.status] || ''} ${t.status}</span></td>
      <td>${t.exit_code ?? '-'}</td>
      <td class="ts t3">${(t.created_at || '').substring(0,16)}</td>
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
  const titleHtml = titleParts.length ? titleParts.join(' · ') : '任务日志';

  const modal = document.createElement('div');
  modal.id = 'rtLogModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1000';
  modal.onclick = (e) => {
    if (e.target === modal) {
      if (_taskLogPollTimer) { clearInterval(_taskLogPollTimer); _taskLogPollTimer = null; }
      modal.remove();
    }
  };
  modal.innerHTML = '<div class="cd" style="width:860px;max-width:94vw;max-height:85vh;display:flex;flex-direction:column;padding:0;overflow:hidden">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">' +
    '<h3 style="font-size:13px;font-weight:600">' + titleHtml + '</h3>' +
    '<div style="display:flex;align-items:center;gap:8px"><span id="rtLogLiveTag" style="font-size:10px;color:var(--g);display:none">' + I18N.t('observability.live') + '</span><button class="bt bt-o bt-s" onclick="closeTaskLogModal()">' + I18N.t('observability.close') + '</button>' + '</div>' +
    '</div>' +
    '<div id="rtLogTimeline" style="padding:10px 18px;border-bottom:1px solid var(--b);display:none"></div>' +
    '<div id="rtLogModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">加载日志...</div></div>' +
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
      document.getElementById('rtLogModalBody').innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
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
  if (info.created_at) stages.push({ label: '入队', time: info.created_at, color: 'var(--y)' });
  if (info.started_at) stages.push({ label: '开始执行', time: info.started_at, color: 'var(--a)' });
  if (info.completed_at) stages.push({ label: '完成', time: info.completed_at, color: info.status === 'failed' ? 'var(--r)' : 'var(--g)' });
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
    html += '<div style="padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.06);color:#8d97a8">无执行日志</div>';
    if (errorInfo) {
      if (errorInfo.result_text) html += `<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#d49c0f">结果</span> <span style="color:#e0e6f0">${errorInfo.result_text}</span></div>`;
      if (errorInfo.exit_code != null) html += `<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#d49c0f">Exit Code</span> <span style="color:${errorInfo.exit_code !== 0 ? '#dc2828' : '#1ca44c'}">${errorInfo.exit_code}</span></div>`;
      if (errorInfo.errors && errorInfo.errors.length) {
        html += '<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#d49c0f">错误信息</span></div>';
        errorInfo.errors.forEach(e => { html += `<div style="padding:4px 14px 4px 28px;color:#dc2828">${e}</div>`; });
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
    const time = (m.created_at || '').substring(11,19);
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
