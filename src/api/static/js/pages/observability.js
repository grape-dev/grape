// ====== 系统监控（Settings ▸ 系统监控）======
// 4 个 tab：任务队列 / Runtime / 数据库 / HTTP 指标
// 数据来源：/api/dashboard/ops/overview + /health/realtime + /metrics（Prometheus 文本解析）

let _obsTab = 'tasks';
let _obsRefreshTimer = null;

function renderObservability() {
  const pc = document.getElementById('pageContent') || document.querySelector('.ct');
  if (!pc) return;

  pc.innerHTML = `
    <div class="cd">
      <div class="cd-h">
        <h2>📊 系统监控 <span class="c">实时运维可观察性</span></h2>
        <div class="flex gap8 fc">
          <span class="ts t3" id="obsLastUpdate">-</span>
          <button class="bt bt-o bt-s" onclick="refreshObservability()" id="obsRefreshBtn">🔄 刷新</button>
          <label class="ts t3 flex fc gap6"><input type="checkbox" id="obsAutoRefresh" onchange="toggleObsAutoRefresh()" style="margin:0"> 自动刷新(10s)</label>
        </div>
      </div>
      <div class="stabs">
        <a class="${_obsTab==='tasks'?'ac':''}" onclick="switchObsTab('tasks')">任务队列</a>
        <a class="${_obsTab==='runtimes'?'ac':''}" onclick="switchObsTab('runtimes')">Runtime</a>
        <a class="${_obsTab==='database'?'ac':''}" onclick="switchObsTab('database')">数据库</a>
        <a class="${_obsTab==='http'?'ac':''}" onclick="switchObsTab('http')">HTTP 指标</a>
      </div>
      <div id="obsContent"><div class="loading">加载中...</div></div>
    </div>
  `;

  refreshObservability();
}

function switchObsTab(tab) {
  _obsTab = tab;
  document.querySelectorAll('.stabs a').forEach(a => a.classList.remove('ac'));
  event.target.classList.add('ac');
  refreshObservability();
}

function toggleObsAutoRefresh() {
  const cb = document.getElementById('obsAutoRefresh');
  if (cb.checked) {
    _obsRefreshTimer = setInterval(refreshObservability, 10000);
  } else {
    clearInterval(_obsRefreshTimer);
    _obsRefreshTimer = null;
  }
}

function refreshObservability() {
  const content = document.getElementById('obsContent');
  if (!content) return;
  content.innerHTML = '<div class="loading">加载中...</div>';

  // 并行拉取 ops/overview 和 health/realtime
  Promise.all([
    API.get('/dashboard/ops/overview').catch(e => ({error: e.message})),
    fetch('/health/realtime').then(r => r.json()).catch(e => ({error: e.message})),
  ]).then(([ops, realtime]) => {
    const now = new Date().toLocaleTimeString('zh-CN');
    const lu = document.getElementById('obsLastUpdate');
    if (lu) lu.textContent = '更新: ' + now;

    if (_obsTab === 'tasks') renderObsTasks(content, ops, realtime);
    else if (_obsTab === 'runtimes') renderObsRuntimes(content, ops, realtime);
    else if (_obsTab === 'database') renderObsDatabase(content, realtime);
    else if (_obsTab === 'http') renderObsHttp(content);
  }).catch(e => {
    content.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

// ===== Tab: 任务队列 =====
function renderObsTasks(container, ops, realtime) {
  const t = ops.tasks || {};
  const sc = t.status_counts || {};
  const stuck = t.stuck_tasks || [];
  const bySource = t.by_source || {};
  const today = t.today || {};

  let html = '';

  // 状态分布卡片
  html += '<div class="stats mb12">';
  html += obsStatCard('Queued', sc.queued || 0, 'var(--y)');
  html += obsStatCard('Dispatched', sc.dispatched || 0, 'var(--a)');
  html += obsStatCard('Running', sc.running || 0, 'var(--sk)');
  html += obsStatCard('In Progress', t.in_progress || 0, 'var(--pu)');
  html += obsStatCard('Stuck', t.stuck_count || 0, t.stuck_count > 0 ? 'var(--r)' : 'var(--g)');
  html += obsStatCard('Completed', sc.completed || 0, 'var(--g)');
  html += obsStatCard('Failed', sc.failed || 0, sc.failed > 0 ? 'var(--r)' : 'var(--t3)');
  html += '</div>';

  // 今日统计
  html += `<div class="cd-h"><h2>今日统计</h2></div>`;
  html += '<div class="stats mb12">';
  html += obsStatCard('Created', today.created || 0, 'var(--a)');
  html += obsStatCard('Completed', today.completed || 0, 'var(--g)');
  html += obsStatCard('Failed', today.failed || 0, today.failed > 0 ? 'var(--r)' : 'var(--t3)');
  html += '</div>';

  // 按来源分组
  if (Object.keys(bySource).length > 0) {
    html += `<div class="cd-h"><h2>按 Agent 类型分组 <span class="c">排队/运行中</span></h2></div>`;
    html += '<div class="tw mb12"><table><thead><tr><th>Agent Type</th><th>Queued</th><th>Running</th></tr></thead><tbody>';
    for (const [src, counts] of Object.entries(bySource)) {
      html += `<tr><td><b>${src}</b></td><td>${counts.queued || 0}</td><td>${counts.running || 0}</td></tr>`;
    }
    html += '</tbody></table></div>';
  }

  // Stuck 任务详情
  if (stuck.length > 0) {
    html += `<div class="cd-h"><h2 style="color:var(--r)">⚠ 卡住的任务 <span class="c">running > 30min</span></h2></div>`;
    html += '<div class="tw"><table><thead><tr><th>Task ID</th><th>Agent</th><th>Runtime</th><th>Started</th><th>Repo</th></tr></thead><tbody>';
    for (const s of stuck) {
      html += `<tr><td class="ts">${s.id}</td><td>${s.agent_type || '-'}</td><td>${s.runtime_id || '-'}</td><td class="ts">${s.started_at || '-'}</td><td class="ts">${s.repo_key || '-'}</td></tr>`;
    }
    html += '</tbody></table></div>';
  }

  container.innerHTML = html || '<div class="loading">无数据</div>';
}

// ===== Tab: Runtime =====
function renderObsRuntimes(container, ops, realtime) {
  const rt = ops.runtimes || {};
  const list = rt.list || [];
  const rtRt = (realtime.runtimes) || {};

  let html = '';

  // 概览卡片
  html += '<div class="stats mb12">';
  html += obsStatCard('Total', rt.total || 0, 'var(--a)');
  html += obsStatCard('Online', rt.online || 0, 'var(--g)');
  html += obsStatCard('Offline', rt.offline || 0, 'var(--t3)');
  html += '</div>';

  // by_mode
  const bm = rt.by_mode || {};
  html += '<div class="stats mb12">';
  html += obsStatCard('Local Online', bm.local || 0, 'var(--sk)');
  html += obsStatCard('Remote Online', bm.remote || 0, 'var(--pu)');
  html += obsStatCard('Unknown', bm.unknown || 0, 'var(--t3)');
  html += '</div>';

  // Runtime 列表
  if (list.length > 0) {
    html += `<div class="cd-h"><h2>引擎列表</h2></div>`;
    html += '<div class="tw"><table><thead><tr><th>ID</th><th>Name</th><th>Mode</th><th>Status</th><th>Health</th><th>Provider</th><th>Last Seen</th></tr></thead><tbody>';
    for (const r of list) {
      const hColor = r.health === 'online' ? 'var(--g)' : r.health === 'recently_lost' ? 'var(--or)' : 'var(--t3)';
      html += `<tr><td>${r.id}</td><td><b>${r.name}</b></td><td><span class="tg tg-d">${r.mode}</span></td><td>${r.status}</td><td><b style="color:${hColor}">${r.health}</b></td><td class="ts">${r.provider || '-'}</td><td class="ts">${r.last_seen_at || '-'}</td></tr>`;
    }
    html += '</tbody></table></div>';
  } else {
    html += '<div class="loading">无 Runtime 数据</div>';
  }

  container.innerHTML = html;
}

// ===== Tab: 数据库 =====
function renderObsDatabase(container, realtime) {
  let html = '';

  // 从 /metrics 拉取 MySQL 指标（解析 Prometheus 文本）
  html += '<div id="dbMetrics"><div class="loading">采集中...</div></div>';

  fetch('/metrics').then(r => r.text()).then(text => {
    const dbDiv = document.getElementById('dbMetrics');
    if (!dbDiv) return;

    const metrics = parsePromMetrics(text, [
      'grape_db_threads_connected',
      'grape_db_threads_running',
      'grape_db_threads_cached',
      'grape_db_threads_created_total',
      'grape_db_max_used_connections',
      'grape_db_aborted_connects_total',
    ]);

    let h = '<div class="stats mb12">';
    h += obsStatCard('Threads Connected', metrics['grape_db_threads_connected'] || 0, 'var(--a)');
    h += obsStatCard('Threads Running', metrics['grape_db_threads_running'] || 0, metrics['grape_db_threads_running'] > 5 ? 'var(--r)' : 'var(--g)');
    h += obsStatCard('Threads Cached', metrics['grape_db_threads_cached'] || 0, 'var(--t3)');
    h += obsStatCard('Max Used', metrics['grape_db_max_used_connections'] || 0, 'var(--y)');
    h += '</div>';

    h += '<div class="stats mb12">';
    h += obsStatCard('Threads Created', metrics['grape_db_threads_created_total'] || 0, 'var(--pu)');
    h += obsStatCard('Aborted Connects', metrics['grape_db_aborted_connects_total'] || 0, metrics['grape_db_aborted_connects_total'] > 5 ? 'var(--r)' : 'var(--t3)');
    h += '</div>';

    // 连接使用率
    const used = metrics['grape_db_threads_connected'] || 0;
    const maxUsed = metrics['grape_db_max_used_connections'] || 1;
    const usagePct = maxUsed > 0 ? Math.round(used / maxUsed * 100) : 0;
    const usageColor = usagePct > 80 ? 'var(--r)' : usagePct > 60 ? 'var(--y)' : 'var(--g)';
    h += `<div class="cd-h"><h2>连接使用率 <span class="c">${used}/${maxUsed}</span></h2></div>`;
    h += `<div style="background:var(--bg);border-radius:8px;padding:4px;margin-bottom:14px"><div style="height:24px;border-radius:6px;background:${usageColor};width:${Math.min(usagePct,100)}%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:600">${usagePct}%</div></div>`;

    // 任务队列快照（从 realtime）
    if (realtime.tasks) {
      h += `<div class="cd-h"><h2>任务队列快照 <span class="c">from /health/realtime</span></h2></div>`;
      h += '<div class="tw"><table><thead><tr><th>Status</th><th>Count</th></tr></thead><tbody>';
      for (const [status, count] of Object.entries(realtime.tasks)) {
        h += `<tr><td><b>${status}</b></td><td>${count}</td></tr>`;
      }
      h += '</tbody></table></div>';
    }

    dbDiv.innerHTML = h;
  }).catch(e => {
    const dbDiv = document.getElementById('dbMetrics');
    if (dbDiv) dbDiv.innerHTML = '<div class="error">采集失败: ' + e.message + '</div>';
  });

  container.innerHTML = html;
}

// ===== Tab: HTTP 指标 =====
function renderObsHttp(container) {
  container.innerHTML = '<div id="httpMetrics"><div class="loading">采集中...</div></div>';

  fetch('/metrics').then(r => r.text()).then(text => {
    const div = document.getElementById('httpMetrics');
    if (!div) return;

    const inFlight = parsePromMetric(text, 'grape_http_in_flight_requests');
    const reqTotal = parsePromLabeledSum(text, 'grape_http_requests_total');
    const durTotal = parsePromLabeledSum(text, 'grape_http_request_duration_seconds_sum');
    const durCount = parsePromLabeledCount(text, 'grape_http_request_duration_seconds_count');

    let html = '<div class="stats mb12">';
    html += obsStatCard('In-Flight', inFlight || 0, 'var(--a)');
    html += obsStatCard('Total Requests', reqTotal || 0, 'var(--sk)');
    html += obsStatCard('Avg Duration', durCount > 0 ? (durTotal / durCount * 1000).toFixed(0) + 'ms' : '-', 'var(--pu)');
    html += '</div>';

    // LLM token 指标
    const llmInput = parsePromLabeledSumByLabel(text, 'grape_llm_tokens_total', 'token_type', 'input');
    const llmOutput = parsePromLabeledSumByLabel(text, 'grape_llm_tokens_total', 'token_type', 'output');
    const llmCache = parsePromLabeledSumByLabel(text, 'grape_llm_tokens_total', 'token_type', 'cache');
    const llmReqs = parsePromLabeledSum(text, 'grape_llm_request_total');

    html += `<div class="cd-h"><h2>LLM Token 用量</h2></div>`;
    html += '<div class="stats mb12">';
    html += obsStatCard('Input Tokens', llmInput || 0, 'var(--a)');
    html += obsStatCard('Output Tokens', llmOutput || 0, 'var(--g)');
    html += obsStatCard('Cache Tokens', llmCache || 0, 'var(--y)');
    html += obsStatCard('LLM Requests', llmReqs || 0, 'var(--pu)');
    html += '</div>';

    // 任务指标
    const enqueued = parsePromLabeledSum(text, 'grape_agent_task_enqueued_total');
    const dispatched = parsePromLabeledSum(text, 'grape_agent_task_dispatched_total');
    const terminal = parsePromLabeledSum(text, 'grape_agent_task_terminal_total');
    const failed = parsePromLabeledSum(text, 'grape_agent_task_failed_total');
    const queued = parsePromMetric(text, 'grape_task_queued');
    const running = parsePromMetric(text, 'grape_task_running');
    const stuck = parsePromMetric(text, 'grape_task_stuck');

    html += `<div class="cd-h"><h2>任务指标</h2></div>`;
    html += '<div class="stats mb12">';
    html += obsStatCard('Enqueued', enqueued || 0, 'var(--a)');
    html += obsStatCard('Dispatched', dispatched || 0, 'var(--sk)');
    html += obsStatCard('Terminal', terminal || 0, 'var(--g)');
    html += obsStatCard('Failed', failed || 0, failed > 0 ? 'var(--r)' : 'var(--t3)');
    html += '</div>';

    html += '<div class="stats mb12">';
    html += obsStatCard('Queued (live)', queued || 0, 'var(--y)');
    html += obsStatCard('Running (live)', running || 0, 'var(--pu)');
    html += obsStatCard('Stuck (live)', stuck || 0, stuck > 0 ? 'var(--r)' : 'var(--g)');
    html += '</div>';

    div.innerHTML = html;
  }).catch(e => {
    const div = document.getElementById('httpMetrics');
    if (div) div.innerHTML = '<div class="error">采集失败: ' + e.message + '</div>';
  });
}

// ===== 辅助函数 =====

function obsStatCard(label, value, color) {
  return `<div class="st"><div class="v" style="color:${color||'var(--t)'}">${value}</div><div class="l">${label}</div></div>`;
}

// 解析 Prometheus 无标签 metric（单值）
function parsePromMetric(text, name) {
  const re = new RegExp('^' + name + '\\s+(\\d+\\.?\\d*)', 'm');
  const m = text.match(re);
  return m ? parseFloat(m[1]) : 0;
}

// 解析 Prometheus 多标签 metric 的总和（所有 label 组合的 sum）
function parsePromLabeledSum(text, name) {
  let total = 0;
  const re = new RegExp('^' + name + '\\{[^}]*\\}\\s+(\\d+\\.?\\d*)', 'gm');
  let m;
  while ((m = re.exec(text)) !== null) {
    total += parseFloat(m[1]);
  }
  return total;
}

// 解析 Prometheus histogram count 总和
function parsePromLabeledCount(text, name) {
  return parsePromLabeledSum(text, name + '_count');
}

// 按特定 label 值过滤求和
function parsePromLabeledSumByLabel(text, name, labelKey, labelValue) {
  let total = 0;
  const re = new RegExp('^' + name + '\\{[^}]*' + labelKey + '="' + labelValue + '"[^}]*\\}\\s+(\\d+\\.?\\d*)', 'gm');
  let m;
  while ((m = re.exec(text)) !== null) {
    total += parseFloat(m[1]);
  }
  return total;
}

// 批量解析无标签 metric
function parsePromMetrics(text, names) {
  const result = {};
  for (const n of names) {
    result[n] = parsePromMetric(text, n);
  }
  return result;
}
