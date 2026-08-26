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
        <a class="${_obsTab==='runtimes'?'ac':''}" onclick="switchObsTab('runtimes',event)">Runtime</a>
        <a class="${_obsTab==='database'?'ac':''}" onclick="switchObsTab('database',event)">数据库</a>
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
  Promise.all([
    API.get('/dashboard/ops/overview').catch(() => null),
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

  if (_obsTab === 'overview') c.innerHTML = renderOverview(realOps, rt, mt);
  else if (_obsTab === 'tasks') c.innerHTML = renderTasksTab(realOps, rt);
  else if (_obsTab === 'runtimes') c.innerHTML = renderRuntimesTab(realOps, rt, mt);
  else if (_obsTab === 'database') c.innerHTML = renderDatabaseTab(rt, mt);
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
  const t = ops.tasks || {};
  const sc = t.status_counts || {};
  const r = ops.runtimes || {};
  const db = parseMetrics(mt, ['grape_db_threads_connected', 'grape_db_threads_running', 'grape_db_max_used_connections']);
  const taskQueued = parseMetric(mt, 'grape_task_queued');
  const taskRunning = parseMetric(mt, 'grape_task_running');
  const taskStuck = parseMetric(mt, 'grape_task_stuck');
  const httpReq = parseMetric(mt, 'grape_http_in_flight_requests');
  const httpTotal = parseLabeledSum(mt, 'grape_http_requests_total');
  const httpDurSum = parseLabeledSum(mt, 'grape_http_request_duration_seconds_sum');
  const httpDurCount = parseLabeledSum(mt, 'grape_http_request_duration_seconds_count');
  const httpAvgMs = httpDurCount > 0 ? Math.round(httpDurSum / httpDurCount * 1000) : 0;
  const llmTokens = parseLabeledSum(mt, 'grape_llm_tokens_total');

  return `
    ${section('任务概览')}
    ${kpiRow([
      { label: '排队中', value: sc.queued || 0, color: 'var(--y)' },
      { label: '执行中', value: (t.in_progress || 0), color: 'var(--a)' },
      { label: '卡住 (>30min)', value: t.stuck_count || 0, color: (t.stuck_count||0) > 0 ? 'var(--r)' : 'var(--g)' },
      { label: '已完成', value: sc.completed || 0, color: 'var(--g)' },
    ])}
    ${section('Runtime 概览')}
    ${kpiRow([
      { label: '总引擎数', value: r.total || 0, color: 'var(--a)' },
      { label: '在线', value: r.online || 0, color: 'var(--g)' },
      { label: '离线', value: r.offline || 0, color: 'var(--t3)' },
      { label: '本机/远端', value: `${(r.by_mode||{}).local||0} / ${(r.by_mode||{}).remote||0}`, color: 'var(--pu)' },
    ])}
    ${section('数据库')}
    ${kpiRow([
      { label: 'DB 连接数', value: db['grape_db_threads_connected'] || 0, color: 'var(--a)', hint: `max ${db['grape_db_max_used_connections']||0}` },
      { label: 'DB 活跃线程', value: db['grape_db_threads_running'] || 0, color: (db['grape_db_threads_running']||0) > 5 ? 'var(--r)' : 'var(--g)' },
      { label: 'LLM Tokens', value: fmtNum(llmTokens), color: 'var(--pu)' },
    ])}
    ${section('HTTP 请求')}
    ${kpiRow([
      { label: '在途', value: httpReq || 0, color: 'var(--sk)', hint: '当前处理中' },
      { label: '累计请求', value: fmtNum(httpTotal), color: 'var(--a)', hint: '启动至今总数' },
      { label: '平均延迟', value: httpAvgMs + 'ms', color: httpAvgMs > 500 ? 'var(--r)' : httpAvgMs > 100 ? 'var(--y)' : 'var(--g)' },
    ])}
    ${section('实时队列')}
    ${kpiRow([
      { label: 'Queued (live)', value: taskQueued, color: 'var(--y)' },
      { label: 'Running (live)', value: taskRunning, color: 'var(--a)' },
      { label: 'Stuck (live)', value: taskStuck, color: (taskStuck||0) > 0 ? 'var(--r)' : 'var(--g)' },
      { label: 'IM Bots', value: rt.im ? `${rt.im.connected}/${rt.im.total_bots}` : '0/0', color: 'var(--sk)' },
    ])}
  `;
}

// ===== Tab: 任务 =====
function renderTasksTab(ops, rt) {
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

  if (Object.keys(bySource).length > 0) {
    html += section('按 Agent 类型分组', '排队 / 运行中');
    html += table(['Agent Type', 'Queued', 'Running'],
      Object.entries(bySource).map(([src, c]) => [
        `<b>${src}</b>`, c.queued || 0, c.running || 0
      ])
    );
  }

  if (stuck.length > 0) {
    html += section('⚠ 卡住的任务', 'running > 30min');
    html += table(['Task ID', 'Agent', 'Runtime', 'Started', 'Repo'],
      stuck.map(s => [
        `<span class="ts">${s.id}</span>`, s.agent_type || '-', s.runtime_id || '-',
        `<span class="ts">${s.started_at || '-'}</span>`, `<span class="ts">${s.repo_key || '-'}</span>`
      ])
    );
  }

  // realtime 补充
  if (rt.tasks) {
    html += section('实时快照', 'from /health/realtime');
    html += table(['Status', 'Count'],
      Object.entries(rt.tasks).map(([k, v]) => [`<b>${k}</b>`, v])
    );
  }

  return html || '<div class="loading">无数据</div>';
}

// ===== Tab: Runtime =====
function renderRuntimesTab(ops, rt, mt) {
  const r = ops.runtimes || {};
  const list = r.list || [];
  const onlineLocal = parseLabeledSumByLabel(mt, 'grape_runtime_online', 'mode', 'local');
  const onlineRemote = parseLabeledSumByLabel(mt, 'grape_runtime_online', 'mode', 'remote');

  let html = '';
  html += section('Runtime 概览');
  html += kpiRow([
    { label: 'Total', value: r.total || 0, color: 'var(--a)' },
    { label: 'Online', value: r.online || 0, color: 'var(--g)' },
    { label: 'Offline', value: r.offline || 0, color: 'var(--t3)' },
    { label: 'Local / Remote', value: `${onlineLocal} / ${onlineRemote}`, color: 'var(--pu)', hint: 'from /metrics' },
  ]);

  if (list.length > 0) {
    html += section('引擎列表');
    html += table(['ID', 'Name', 'Mode', 'Status', 'Health', 'Provider', 'Last Seen'],
      list.map(e => {
        const hc = e.health === 'online' ? 'var(--g)' : e.health === 'recently_lost' ? 'var(--or)' : 'var(--t3)';
        return [
          e.id,
          `<b>${e.name}</b>`,
          `<span class="tg tg-d">${e.mode}</span>`,
          e.status,
          `<b style="color:${hc}">${e.health}</b>`,
          `<span class="ts">${e.provider || '-'}</span>`,
          `<span class="ts">${e.last_seen_at || '-'}</span>`,
        ];
      })
    );
  } else {
    html += section('引擎列表');
    html += '<div class="loading">无 Runtime 详情（ops/overview 未认证时降级，仅显示汇总数）</div>';
    // 从 /metrics 补充
    if (rt.runtimes) {
      html += table(['指标', '值'],
        [['Total', rt.runtimes.total || 0], ['Online', rt.runtimes.online || 0]]
      );
    }
  }

  return html;
}

// ===== Tab: 数据库 =====
function renderDatabaseTab(rt, mt) {
  const db = parseMetrics(mt, [
    'grape_db_threads_connected', 'grape_db_threads_running', 'grape_db_threads_cached',
    'grape_db_threads_created_total', 'grape_db_max_used_connections', 'grape_db_aborted_connects_total',
  ]);

  const connected = db['grape_db_threads_connected'] || 0;
  const maxUsed = db['grape_db_max_used_connections'] || 1;
  const usagePct = maxUsed > 0 ? Math.round(connected / maxUsed * 100) : 0;
  const usageColor = usagePct > 80 ? 'var(--r)' : usagePct > 60 ? 'var(--y)' : 'var(--g)';

  let html = '';
  html += section('MySQL 连接');
  html += kpiRow([
    { label: 'Threads Connected', value: connected, color: 'var(--a)' },
    { label: 'Threads Running', value: db['grape_db_threads_running'] || 0, color: (db['grape_db_threads_running']||0) > 5 ? 'var(--r)' : 'var(--g)' },
    { label: 'Threads Cached', value: db['grape_db_threads_cached'] || 0, color: 'var(--t3)' },
    { label: 'Max Used', value: maxUsed, color: 'var(--y)' },
  ]);
  html += kpiRow([
    { label: 'Threads Created', value: fmtNum(db['grape_db_threads_created_total'] || 0), color: 'var(--pu)' },
    { label: 'Aborted Connects', value: db['grape_db_aborted_connects_total'] || 0, color: (db['grape_db_aborted_connects_total']||0) > 5 ? 'var(--r)' : 'var(--t3)' },
  ]);

  html += section('连接使用率', `${connected}/${maxUsed}`);
  html += `<div style="background:var(--bg);border-radius:8px;padding:4px;margin-bottom:16px"><div style="height:24px;border-radius:6px;background:${usageColor};width:${Math.min(usagePct,100)}%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:600;transition:width .3s">${usagePct}%</div></div>`;

  if (rt.tasks) {
    html += section('任务队列快照', 'from /health/realtime');
    html += table(['Status', 'Count'],
      Object.entries(rt.tasks).map(([k, v]) => [`<b>${k}</b>`, v])
    );
  }
  html += section('Runtime 快照');
  html += kpiRow([
    { label: 'Total', value: rt.runtimes ? rt.runtimes.total : 0, color: 'var(--a)' },
    { label: 'Online', value: rt.runtimes ? rt.runtimes.online : 0, color: 'var(--g)' },
    { label: 'In Progress', value: rt.in_progress || 0, color: 'var(--pu)' },
    { label: 'IM Connected', value: rt.im ? `${rt.im.connected}/${rt.im.total_bots}` : '0/0', color: 'var(--sk)' },
  ]);

  return html;
}

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
