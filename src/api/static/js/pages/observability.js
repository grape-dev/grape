// ====== 系统监控（Settings ▸ 系统监控）======
// 仿 multica Dashboard 布局：4 Tab（概览/任务/Runtime/数据库）
// 每个 Tab 顶部 KPI 卡片行（grid 4 列），下方详情表格/图
// 数据来源：/api/dashboard/ops/overview + /health/realtime + /metrics

let _obsTab = 'overview';
let _obsTimer = null;
let _obsData = { ops: null, realtime: null, metrics: null };

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
          <label class="ts t3" style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="obsAuto" onchange="toggleObsAuto()" style="margin:0">自动(10s)</label>
        </div>
      </div>
      <div class="stabs" style="padding:0 20px;border-bottom:1px solid var(--b)">
        <a class="${_obsTab==='overview'?'ac':''}" onclick="switchObsTab('overview',event)">概览</a>
        <a class="${_obsTab==='tasks'?'ac':''}" onclick="switchObsTab('tasks',event)">任务</a>
        <a class="${_obsTab==='anomaly'?'ac':''}" onclick="switchObsTab('anomaly',event)">异常任务</a>
        <a class="${_obsTab==='errorlogs'?'ac':''}" onclick="switchObsTab('errorlogs',event)">错误日志</a>
      </div>
      <div id="obsContent" style="padding:20px"><div class="loading">加载中...</div></div>
    </div>
  `;
  refreshObs();
}

function switchObsTab(tab, ev) {
  _obsTab = tab;
  document.querySelectorAll('.stabs a').forEach(a => a.classList.remove('ac'));
  if (ev) ev.target.classList.add('ac');
  renderObsContent();
}

function toggleObsAuto() {
  const cb = document.getElementById('obsAuto');
  if (cb && cb.checked) { _obsTimer = setInterval(refreshObs, 10000); }
  else { clearInterval(_obsTimer); _obsTimer = null; }
}

function refreshObs() {
  const c = document.getElementById('obsContent');
  if (!c) return;
  c.innerHTML = '<div class="loading">加载中...</div>';
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
    renderObsContent();
  });
}

function renderObsContent() {
  const c = document.getElementById('obsContent');
  if (!c) return;
  const { ops, realtime, metricsText } = _obsData;

  // 降级：ops 401 时从 realtime 补齐
  const realOps = (ops && !ops.error) ? ops : fallbackOps(realtime);
  const rt = realtime || {};
  const mt = metricsText || '';

  if (_obsTab === 'overview') {
    c.innerHTML = renderOverview(realOps, rt, mt);
  }
  else if (_obsTab === 'tasks') c.innerHTML = renderTasksTab(realOps, rt, mt);
  else if (_obsTab === 'anomaly') renderAnomalyTab(c, realOps);
  else if (_obsTab === 'errorlogs') loadErrorLogs(c);
}

// ===== KPI 卡片行（仿 multica KpiCard grid）=====
function kpiRow(cards) {
  return `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:0;border:1px solid var(--b);border-radius:10px;overflow:hidden;margin-bottom:16px;background:var(--c)">
    ${cards.map(k => `
      <div style="display:flex;flex-direction:column;gap:4px;padding:16px 18px;border-right:1px solid var(--b);border-bottom:1px solid var(--b)">
        <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--t3)">${k.label}</div>
        <div style="font-size:22px;font-weight:700;line-height:1.2;color:${k.color||'var(--t)'};font-variant-numeric:tabular-nums">${k.value}</div>
        ${k.hint ? `<div style="font-size:10px;color:var(--t3)">${k.hint}</div>` : ''}
      </div>`).join('')}
  </div>`;
}

function section(title, subtitle) {
  return `<div style="margin-bottom:8px;margin-top:16px"><h2 style="font-size:13px;font-weight:600">${title}</h2>${subtitle ? `<span style="font-size:10px;color:var(--t3);margin-left:8px">${subtitle}</span>` : ''}</div>`;
}

function table(headers, rows) {
  return `<div class="tw" style="border:1px solid var(--b);border-radius:10px;overflow:hidden;margin-bottom:16px"><table><thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

// ===== Tab: 概览 =====
function renderOverview(ops, rt, mt) {
  const r = ops.runtimes || {};
  const w = ops.workers || {};

  const HEALTH_DOT = { online: '🟢', recently_lost: '🟡', offline: '⚫', about_to_gc: '🔴' };
  const HEALTH_COLOR = { online: 'var(--g)', recently_lost: 'var(--y)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };

  // 资源进度条
  function bar(label, pct, color) {
    const c = pct > 80 ? 'var(--r)' : pct > 60 ? 'var(--y)' : color || 'var(--g)';
    return `<div style="display:flex;align-items:center;gap:6px;font-size:10px">
      <span style="color:var(--t3);min-width:32px">${label}</span>
      <div style="flex:1;height:8px;background:var(--bg);border-radius:4px;overflow:hidden;min-width:60px">
        <div style="height:100%;width:${Math.min(pct,100)}%;background:${c};border-radius:4px;transition:width .3s"></div>
      </div>
      <span style="color:${c};font-weight:600;min-width:30px;text-align:right">${Math.round(pct)}%</span>
    </div>`;
  }

  // Worker 卡片
  let workerCards = '';
  const workers = w.list || [];
  if (workers.length === 0) {
    workerCards = '<div class="ts t2" style="padding:12px">无远端 Worker（仅本机直连引擎）</div>';
  } else {
    workers.forEach(wk => {
      const dot = HEALTH_DOT[wk.health] || '⚫';
      const hc = HEALTH_COLOR[wk.health] || 'var(--t3)';
      workerCards += `<div style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-size:14px">${dot}</span>
          <b style="font-size:13px">${wk.device_name || wk.name || 'Worker #' + wk.id}</b>
          <span style="margin-left:auto;font-size:10px;color:${hc};font-weight:600">${wk.health}</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 12px;font-size:10px;color:var(--t3);margin-bottom:8px">
          <div>IP: ${wk.last_ip || '-'}</div>
          <div>心跳: ${(wk.last_seen_at||'-').substring(11,19)}</div>
        </div>
        ${bar('CPU', wk.cpu_percent)}
        ${bar('MEM', wk.memory_percent)}
        ${bar('DISK', wk.disk_percent)}
      </div>`;
    });
  }

  // Runtime 表格
  const runtimes = r.list || [];
  const runtimeRows = runtimes.length ? runtimes.map(rt => {
    const dot = HEALTH_DOT[rt.health] || '⚫';
    const hc = HEALTH_COLOR[rt.health] || 'var(--t3)';
    const busyColor = rt.busy_slots >= rt.max_concurrent ? 'var(--r)' : rt.busy_slots > 0 ? 'var(--y)' : 'var(--g)';
    return [`${dot} <b>${rt.name}</b>`,
      `<span class="ts t3">${rt.version || '-'}</span>`,
      `<span style="color:${hc};font-weight:600">${rt.health}</span>`,
      `<span style="color:${busyColor}">${rt.busy_slots}/${rt.max_concurrent}</span>`,
      rt.mode === 'local' ? '<span class="tg tg-ch">本机</span>' : '<span class="tg tg-c">远端</span>',
      `<span class="ts t3">${rt.device_name || '-'}</span>`,
    ];
  }) : [];

  return `
    ${section('Worker 监控', `${w.online || 0}/${w.total || 0} 在线`)}
    ${kpiRow([
      { label: 'Worker 总数', value: w.total || 0, color: 'var(--a)' },
      { label: '在线', value: w.online || 0, color: 'var(--g)' },
      { label: '离线', value: w.offline || 0, color: 'var(--t3)' },
      { label: '引擎总数', value: r.total || 0, color: 'var(--pu)' },
    ])}
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-bottom:16px">
      ${workerCards}
    </div>
    ${section('Runtime 引擎', `${r.online || 0}/${r.total || 0} 在线`)}
    ${runtimeRows.length ? table(['引擎', '版本', '健康度', '繁忙', '模式', '设备'], runtimeRows) : '<div class="ts t2" style="padding:12px">无引擎</div>'}
  `;
}

// A2A 健康检查（概览 tab 内异步加载）
function loadA2aHealth() {
  const container = document.getElementById('obs-a2a-health');
  if (!container) return;
  container.innerHTML = section('A2A 健康检查', 'server_base_url 可达性') + '<div class="loading">检查中...</div>';
  fetch('/api/dashboard/ops/a2a-health').then(r => r.json()).then(d => {
    const status = d.reachable
      ? `<span style="color:var(--g)">✓ 可达</span> (${d.latency_ms}ms)`
      : `<span style="color:var(--r)">✗ 不可达</span> ${d.error ? '<span class="ts t3">'+d.error.substring(0,80)+'</span>' : ''}`;
    let html = section('A2A 健康检查', 'server_base_url 可达性');
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
    container.innerHTML = section('A2A 健康检查') + '<div class="error">加载失败: ' + e.message + '</div>';
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
        <button class="bt bt-o bt-s" ${_anomalyPage === 0 ? 'disabled style="opacity:.4"' : ''} onclick="_anomalyPage--;refreshObservability()">‹ 上一页</button>
        <span class="ts t3">${_anomalyPage+1} / ${totalPages}</span>
        <button class="bt bt-o bt-s" ${_anomalyPage >= totalPages-1 ? 'disabled style="opacity:.4"' : ''} onclick="_anomalyPage++;refreshObservability()">下一页 ›</button>
      </div>`;
    }
  }

  container.innerHTML = html;
}

function loadErrorLogs(container) {
  if (!container) return;
  let currentLevel = _errorLogLevel || '';
  container.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
      <div style="display:flex;gap:0">
        <button class="bt bt-s ${currentLevel===''?'bt-p':'bt-o'}" onclick="_errorLogLevel='';loadErrorLogs(document.getElementById('obsContent'))">全部</button>
        <button class="bt bt-s ${currentLevel==='ERROR'?'bt-p':'bt-o'}" onclick="_errorLogLevel='ERROR';loadErrorLogs(document.getElementById('obsContent'))">仅 ERROR</button>
        <button class="bt bt-s ${currentLevel==='WARNING'?'bt-p':'bt-o'}" onclick="_errorLogLevel='WARNING';loadErrorLogs(document.getElementById('obsContent'))">仅 WARNING</button>
      </div>
      <span class="ts t3" id="errorLogCount"></span>
    </div>
    <div id="errorLogBody"><div class="loading">加载中...</div></div>
  `;
  const levelParam = currentLevel ? '&level=' + currentLevel : '';
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '&repo=' + encodeURIComponent(repo.fullName) : '';
  fetch('/api/dashboard/ops/error-logs?limit=100' + levelParam + repoParam).then(r => r.json()).then(d => {
    const entries = d.entries || [];
    const countEl = document.getElementById('errorLogCount');
    const bodyEl = document.getElementById('errorLogBody');
    if (countEl) countEl.textContent = `${entries.length} 条（来自 ${d.files ? d.files.length : 1} 个日志文件）`;
    if (!entries.length) {
      if (bodyEl) bodyEl.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">无匹配日志</div>';
      return;
    }
    let html = '<div style="background:#0c1222;color:#e0e6f0;font-family:Menlo,Monaco,Consolas,monospace;font-size:11px;line-height:1.5;border-radius:8px;overflow:hidden;max-height:70vh;overflow-y:auto">';
    entries.forEach(e => {
      const level = e.level || '';
      const color = level === 'ERROR' ? '#dc2828' : '#d49c0f';
      const time = (e.ts || '').substring(0,19);
      const rid = e.request_id && e.request_id !== '-' ? ` [${e.request_id}]` : '';
      const extra = e.method ? ` ${e.method} ${e.path||''} ${e.status||''} ${e.duration_ms||0}ms` : '';
      html += `<div style="padding:4px 12px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#5b667b">${time}</span>${rid} <span style="color:${color}"><b>${level}</b></span> <span style="color:#e0e6f0">${(e.message||'').substring(0,300)}</span><span style="color:#8d97a8">${extra}</span></div>`;
    });
    html += '</div>';
    if (bodyEl) bodyEl.innerHTML = html;
  }).catch(e => {
    const bodyEl = document.getElementById('errorLogBody');
    if (bodyEl) bodyEl.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}
let _errorLogLevel = '';

// ===== Tab: 任务 =====
function renderTasksTab(ops, rt, mt) {
  const t = ops.tasks || {};
  const sc = t.status_counts || {};
  const stuck = t.stuck_tasks || [];
  const bySource = t.by_source || {};
  const today = t.today || {};

  let html = '';
  html += section('任务状态分布');
  html += kpiRow([
    { label: 'Queued', value: sc.queued || 0, color: 'var(--y)' },
    { label: 'Dispatched', value: sc.dispatched || 0, color: 'var(--a)' },
    { label: 'Running', value: sc.running || 0, color: 'var(--sk)' },
    { label: 'Completed', value: sc.completed || 0, color: 'var(--g)' },
    { label: 'Failed', value: sc.failed || 0, color: (sc.failed||0) > 0 ? 'var(--r)' : 'var(--t3)' },
    { label: 'Cancelled', value: sc.cancelled || 0, color: 'var(--t3)' },
    { label: 'In Progress', value: t.in_progress || 0, color: 'var(--pu)' },
    { label: 'Stuck', value: t.stuck_count || 0, color: (t.stuck_count||0) > 0 ? 'var(--r)' : 'var(--g)' },
  ]);

  html += section('今日统计');
  html += kpiRow([
    { label: 'Created', value: today.created || 0, color: 'var(--a)' },
    { label: 'Completed', value: today.completed || 0, color: 'var(--g)' },
    { label: 'Failed', value: today.failed || 0, color: (today.failed||0) > 0 ? 'var(--r)' : 'var(--t3)' },
  ]);

  // 任务生命周期累计指标（从 /metrics 解析，Counter/Histogram，服务重启后归零）
  if (mt) {
    const enqueued = parseLabeledSum(mt, 'grape_agent_task_enqueued_total');
    const dispatched = parseLabeledSum(mt, 'grape_agent_task_dispatched_total');
    const terminal = parseLabeledSum(mt, 'grape_agent_task_terminal_total');
    const failed = parseLabeledSum(mt, 'grape_agent_task_failed_total');
    const qWaitSum = parseLabeledSum(mt, 'grape_agent_task_queue_wait_seconds_sum');
    const qWaitCnt = parseLabeledSum(mt, 'grape_agent_task_queue_wait_seconds_count');
    const runSum = parseLabeledSum(mt, 'grape_agent_task_run_seconds_sum');
    const runCnt = parseLabeledSum(mt, 'grape_agent_task_run_seconds_count');
    const totalSum = parseLabeledSum(mt, 'grape_agent_task_total_seconds_sum');
    const totalCnt = parseLabeledSum(mt, 'grape_agent_task_total_seconds_count');
    const avgQueueWait = qWaitCnt > 0 ? (qWaitSum / qWaitCnt).toFixed(1) + 's' : '-';
    const avgRun = runCnt > 0 ? (runSum / runCnt).toFixed(1) + 's' : '-';
    const avgTotal = totalCnt > 0 ? (totalSum / totalCnt).toFixed(1) + 's' : '-';
    const failRate = terminal > 0 ? (failed / terminal * 100).toFixed(0) + '%' : '-';

    html += section('任务生命周期累计', 'from /metrics — 服务重启后归零');
    html += kpiRow([
      { label: 'Enqueued', value: enqueued, color: 'var(--a)' },
      { label: 'Dispatched', value: dispatched, color: 'var(--sk)' },
      { label: 'Terminal', value: terminal, color: 'var(--g)' },
      { label: 'Failed', value: failed, color: failed > 0 ? 'var(--r)' : 'var(--t3)', hint: `失败率 ${failRate}` },
    ]);
    html += kpiRow([
      { label: '平均排队等待', value: avgQueueWait, color: 'var(--y)' },
      { label: '平均执行时长', value: avgRun, color: 'var(--pu)' },
      { label: '平均总时长', value: avgTotal, color: 'var(--or)' },
    ]);

    // 按来源分组的实时 + 累计合并表格
    const sources = parseLabeledKeys(mt, 'grape_agent_task_enqueued_total', 'source');
    // 合并 ops/overview 的 bySource（实时 queued/running）和 /metrics 的累计数据
    const allSources = new Set([...sources, ...Object.keys(bySource)]);
    if (allSources.size > 0) {
      html += section('按来源分组', '实时排队/运行 + 累计统计');
      html += table(['Source', 'Queued', 'Running', 'Enqueued', 'Terminal', 'Failed'],
        Array.from(allSources).sort().map(src => [
          `<b>${src}</b>`,
          (bySource[src] || {}).queued || 0,
          (bySource[src] || {}).running || 0,
          parseLabeledSumByLabel(mt, 'grape_agent_task_enqueued_total', 'source', src),
          parseLabeledSumByLabel(mt, 'grape_agent_task_terminal_total', 'source', src),
          parseLabeledSumByLabel(mt, 'grape_agent_task_failed_total', 'source', src),
        ])
      );
    }
  } else if (Object.keys(bySource).length > 0) {
    // /metrics 无数据时仅展示实时
    html += section('按来源分组', '排队 / 运行中');
    html += table(['Source', 'Queued', 'Running'],
      Object.entries(bySource).map(([src, c]) => [
        `<b>${src}</b>`, c.queued || 0, c.running || 0
      ])
    );
  }

  if (stuck.length > 0) {
    html += section('⚠ 卡住的任务', 'running > 15min（超时前 5min 预警）');
    html += table(['Task ID', 'Agent', 'Runtime', 'Started', 'Repo'],
      stuck.map(s => [
        `<span class="ts">${s.id}</span>`, s.agent_type || '-', s.runtime_id || '-',
        `<span class="ts">${s.started_at || '-'}</span>`, `<span class="ts">${s.repo_key || '-'}</span>`
      ])
    );
  }

  return html || '<div class="loading">无数据</div>';
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
