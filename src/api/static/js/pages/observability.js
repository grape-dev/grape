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

  const HEALTH_DOT = { online: '🟢', idle: '⚪', normal: '🟢', busy: '🟡', overload: '🔴', recently_lost: '🟡', offline: '⚫', about_to_gc: '🔴' };
  const HEALTH_COLOR = { online: 'var(--g)', idle: 'var(--t3)', normal: 'var(--g)', busy: 'var(--y)', overload: 'var(--r)', recently_lost: 'var(--y)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };

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

  // Runtime 表格（引擎行可点击 → 打开任务列表）
  const runtimes = r.list || [];
  let runtimeTable = '<div class="ts t2" style="padding:12px">无引擎</div>';
  if (runtimes.length) {
    runtimeTable = '<div class="tw"><table><thead><tr><th>引擎</th><th>版本</th><th>健康度</th><th>繁忙</th><th>模式</th><th>设备</th></tr></thead><tbody>';
    runtimes.forEach(rt => {
      const dot = HEALTH_DOT[rt.health] || '⚫';
      const hc = HEALTH_COLOR[rt.health] || 'var(--t3)';
      const running = rt.running || 0;
      const maxC = rt.max_concurrent || 0;
      const loadColor = running >= maxC * 2 ? 'var(--r)' : running >= maxC ? 'var(--y)' : running > 0 ? 'var(--g)' : 'var(--t3)';
      const modeTag = rt.mode === 'local' ? '<span class="tg tg-ch">本机</span>' : '<span class="tg tg-c">远端</span>';
      runtimeTable += `<tr style="cursor:pointer" onclick="openRuntimeTaskModal(${rt.id}, '${(rt.name||'').replace(/'/g,"")}')">
        <td>${dot} <b>${rt.name}</b></td>
        <td><span class="ts t3">${rt.version || '-'}</span></td>
        <td><span style="color:${hc};font-weight:600">${rt.health}</span></td>
        <td><span style="color:${loadColor}">${running}/${maxC}</span></td>
        <td>${modeTag}</td>
        <td><span class="ts t3">${rt.device_name || '-'}</span></td>
      </tr>`;
    });
    runtimeTable += '</tbody></table></div>';
  }

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
    ${section('Runtime 引擎', `${r.online || 0}/${r.total || 0} 在线 — 点击引擎查看任务`)}
    ${runtimeTable}
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
  html += '<div id="obs-timeline-container"><div class="loading">加载任务列表...</div></div>';
  const repo = (typeof app !== 'undefined' && app.currentRepo) ? app.currentRepo() : null;
  const repoParam = repo && repo.fullName ? '?repo=' + encodeURIComponent(repo.fullName) : '';
  fetch('/api/dashboard/ops/timeline' + repoParam).then(r => r.json()).then(d => {
    renderTimelineList(document.getElementById('obs-timeline-container'), d.issues || []);
  }).catch(e => {
    const c = document.getElementById('obs-timeline-container');
    if (c) c.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
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
  let prev = null;
  for (const ev of iss.events) {
    const a = _mermaidId(ev.agentType || '—');
    const t = ev.createdAt ? ev.createdAt.slice(11, 19) : '';
    const label = t ? t + ' ' + _actionText(ev.eventType) : _actionText(ev.eventType);
    if (prev && a !== prev) {
      // 跨 agent 交互：prev -> a
      lines.push('    ' + prev + '->>' + a + ': ' + label);
      statuses.push(ev.status);
    } else {
      // 同 agent 连续事件或首个事件：用 Note 标注（不挤在一行）
      lines.push('    Note over ' + a + ': ' + label);
      statuses.push(ev.status);
    }
    prev = a;
  }
  return { text: lines.join('\n'), statuses };
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
    const { text, statuses } = _buildSequence(iss);
    // mermaid v11: 先 parse 验证语法，再 render
    try { await mmd.parse(text); } catch(pe) { /* parse 失败也尝试 render */ }
    const result = await mmd.render(uid, text);
    const svg = result.svg || result;
    box.innerHTML = _applySequenceColors(svg, statuses);
  } catch (err) {
    box.innerHTML = '<div style="padding:12px;color:var(--r);font-size:12px">时序渲染失败: ' + (err.message || err) + '</div>';
    // 显示原始 mermaid 文本供调试
    const { text } = _buildSequence(iss);
    box.innerHTML += '<pre style="padding:8px;background:#0c1222;color:#e0e6f0;font-size:10px;border-radius:6px;overflow-x:auto;white-space:pre-wrap">' + text.replace(/</g,'&lt;') + '</pre>';
  }
}

function renderTimelineList(container, issues) {
  if (!issues.length) { container.innerHTML = '<div class="ts t2" style="padding:20px;text-align:center">暂无任务事件</div>'; return; }
  _timelineIssues = issues;
  let html = section('任务列表', issues.length + ' 个 Issue \u2014 点击展开');
  html += '<div style="display:flex;flex-direction:column;gap:0">';
  issues.forEach(iss => {
    const expanded = _expandedIssues.has(iss.issueId);
    const hasFailed = iss.events.some(e => e.status === 'failed');
    const statusBadge = hasFailed ? '<span style="color:var(--r);font-size:10px;font-weight:600">\u274c 有失败</span>' : '<span style="color:var(--g);font-size:10px;font-weight:600">\u2713 正常</span>';
    html += '<div style="border:1px solid var(--b);border-radius:8px;overflow:hidden;margin-bottom:8px">';
    html += '<div style="display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer;background:var(--c)" onclick="toggleIssueTimeline(\'' + iss.issueId + '\')">';
    html += '<span style="font-size:12px">' + (expanded ? '\u25be' : '\u25b8') + '</span>';
    html += '<b style="font-size:13px">' + iss.issueId + '</b>';
    html += iss.title ? '<span class="ts t3" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + iss.title + '</span>' : '<span style="flex:1"></span>';
    html += '<span class="ts t3">' + iss.eventCount + ' 事件</span>' + statusBadge;
    html += '<span class="ts t3">' + (iss.lastAt || '').substring(0,16) + '</span>';
    html += '</div>';
    html += '<div id="tl-body-' + iss.issueId + '" style="display:' + (expanded ? 'block' : 'none') + ';padding:10px 14px;border-top:1px solid var(--b)">';
    // 切换按钮
    html += '<div style="display:flex;gap:0;margin-bottom:10px">';
    html += '<button class="bt bt-s ' + (_issueViewMode[iss.issueId] !== 'events' ? 'bt-p' : 'bt-o') + '" onclick="switchIssueView(\'' + iss.issueId + '\',\'diagram\')">\u{1f4ca} 时序图</button>';
    html += '<button class="bt bt-s ' + (_issueViewMode[iss.issueId] === 'events' ? 'bt-p' : 'bt-o') + '" onclick="switchIssueView(\'' + iss.issueId + '\',\'events\')">\u{1f4cb} 事件列表</button>';
    html += '</div>';
    // 时序图容器
    html += '<div id="tl-diagram-' + iss.issueId + '" class="tl-mmd" data-iid="' + iss.issueId + '" style="overflow-x:auto;' + (_issueViewMode[iss.issueId] === 'events' ? 'display:none' : '') + '"></div>';
    // 事件列表容器
    html += '<div id="tl-events-' + iss.issueId + '" style="' + (_issueViewMode[iss.issueId] !== 'events' ? 'display:none' : '') + '">';
    html += '<table style="width:100%;font-size:11px"><thead><tr><th>Seq</th><th>事件</th><th>Agent</th><th>Skill</th><th>状态</th><th>耗时</th><th>时间</th></tr></thead><tbody>';
    html += iss.events.map(ev => {
      const enc = ev.taskId ? encodeURIComponent(JSON.stringify({issue:iss.issueId, seq:ev.seq, created:ev.createdAt||''})) : '';
      const clickable = ev.taskId ? 'cursor:pointer' : '';
      const onclick = ev.taskId ? " onclick=\"openTaskLogModal('" + ev.taskId + "','" + enc + "')\"" : '';
      return '<tr style="' + clickable + '"' + onclick + '><td class="ts">' + ev.seq + '</td><td>' + _actionText(ev.eventType) + '</td><td><b>' + (ev.agentType||'-') + '</b></td><td class="ts t3">' + (ev.skill||'-') + '</td><td style="color:' + (ev.status==='failed'?'var(--r)':ev.status==='completed'?'var(--g)':'var(--t3)') + '">' + (ev.status||'-') + '</td><td class="ts t3">' + (ev.duration||'-') + 's</td><td class="ts t3">' + (ev.createdAt||'').substring(0,19) + '</td></tr>';
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
      <button class="bt bt-o bt-s" onclick="document.getElementById('rtTaskModal').remove()">✕ 关闭</button>
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

function openTaskLogModal(taskId, infoEnc) {
  const old = document.getElementById('rtLogModal');
  if (old) old.remove();

  let info = {};
  try { info = JSON.parse(decodeURIComponent(infoEnc || '')); } catch(e) {}
  const titleParts = [];
  if (info.repo) titleParts.push(`<span class="ts t3">${info.repo}</span>`);
  if (info.issue) titleParts.push(`<b>${info.issue}</b>`);
  if (info.seq) titleParts.push(`<span class="ts">#${info.seq}</span>`);
  if (info.created) titleParts.push(`<span class="ts t3">${info.created.substring(0,19)}</span>`);
  const titleHtml = titleParts.length ? titleParts.join(' · ') : '任务日志';

  const modal = document.createElement('div');
  modal.id = 'rtLogModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1000';
  modal.onclick = (e) => { if (e.target === modal) modal.remove(); };
  modal.innerHTML = `<div class="cd" style="width:860px;max-width:94vw;max-height:85vh;display:flex;flex-direction:column;padding:0;overflow:hidden">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--b)">
      <h3 style="font-size:13px;font-weight:600">${titleHtml}</h3>
      <button class="bt bt-o bt-s" onclick="document.getElementById('rtLogModal').remove()">✕ 关闭</button>
    </div>
    <div id="rtLogTimeline" style="padding:10px 18px;border-bottom:1px solid var(--b);display:none"></div>
    <div id="rtLogModalBody" style="overflow-y:auto;flex:1;padding:14px 18px"><div class="loading">加载日志...</div></div>
  </div>`;
  document.body.appendChild(modal);

  API.get('/task-logs/' + taskId + '/messages?since=0').then(msgs => {
    renderTaskLogMessages(document.getElementById('rtLogModalBody'), msgs || [], info);
  }).catch(e => {
    document.getElementById('rtLogModalBody').innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });

  API.get('/task-logs/' + taskId + '/info').then(info => {
    renderTaskTimeline(document.getElementById('rtLogTimeline'), info);
  }).catch(e => {});
}

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
