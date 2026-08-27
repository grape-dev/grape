/* Agent Runtime 管理页（M4 双视图）：Engines / Workers 两个子页，各按 scope 分区展示。
   见 runtime-admin-two-view-design.md。卡片式布局：状态徽章置顶，异常引擎优先捕捉。 */
const RUNTIME_ICON = { online: '●', recently_lost: '◐', offline: '○', about_to_gc: '⚠' };
const RUNTIME_COLOR = { online: 'var(--g)', recently_lost: 'var(--or)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };
const SCOPE_NAMES = { global: '全局', repo: '仓库', user: '个人' };
const SCOPE_ORDER = ['global', 'repo', 'user'];
const HEALTH_NAMES = { online: '在线', recently_lost: '刚失联', offline: '离线', about_to_gc: '待回收' };
const HEALTH_BADGE = {
  online: { color: 'var(--g)', bg: '#dcfce7' },
  recently_lost: { color: 'var(--or)', bg: '#fef3c7' },
  offline: { color: 'var(--t3)', bg: '#eef0f3' },
  about_to_gc: { color: 'var(--r)', bg: '#fee2e2' },
};

let _runtimeTab = 'engines';
let __runtimeData = null;

function _currentUser() {
  try {
    const u = JSON.parse(localStorage.getItem('user') || 'null');
    return (u && (u.nickname || u.username)) || '';
  } catch (e) { return ''; }
}

function _healthDot(h) { return `<span style="color:${RUNTIME_COLOR[h] || 'var(--t3)'}">${RUNTIME_ICON[h] || '○'}</span>`; }
function _healthLabel(h) { return HEALTH_NAMES[h] || h || '离线'; }
function _scopeLabel(s) { return SCOPE_NAMES[s] || s || '全局'; }

function _healthBadge(h, suffix) {
  const st = HEALTH_BADGE[h] || HEALTH_BADGE.offline;
  return `<span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;color:${st.color};background:${st.bg};padding:3px 10px;border-radius:999px;white-space:nowrap"><span style="width:7px;height:7px;border-radius:50%;background:${st.color}"></span>${_healthLabel(h)}${suffix ? ' · ' + suffix : ''}</span>`;
}

function _scopeBar(sc, n, localN) {
  // global 分组额外标注本机直连/远端数量（engine-selection-design.md §4.3）
  const sub = (sc === 'global' && localN) ? ` <span style="color:#0e7490;font-weight:600">· 本机直连 ${localN} / 远端 ${(n || 0) - localN}</span>` : '';
  return `<div style="display:inline-flex;align-items:center;gap:8px;background:#e3edfd;color:#235ec7;padding:5px 12px;border-radius:6px;font-size:12px;font-weight:600;margin:16px 0 10px">${_scopeLabel(sc)} <span style="background:rgba(35,94,199,.12);border-radius:10px;padding:0 8px;font-size:11px">${n}</span>${sub}</div>`;
}

function _legendHtml() {
  return `<div style="display:flex;gap:14px;flex-wrap:wrap;padding:12px 0;font-size:11px;color:var(--t3);border-top:1px dashed var(--b)">
    ${Object.entries(HEALTH_NAMES).map(([k, v]) =>
      `<span style="display:inline-flex;align-items:center;gap:5px"><span style="width:7px;height:7px;border-radius:50%;background:${(HEALTH_BADGE[k] || {}).color || 'var(--t3)'}"></span>${v}${k === 'recently_lost' ? '(<5min)' : k === 'about_to_gc' ? '(>6d)' : ''}</span>`).join('')}
  </div>`;
}

function renderRuntimes(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  __runtimeData = data;
  const devices = (data.devices && data.devices.length)
    ? data.devices
    : (data.runtimes || []).map(rt => ({ name: rt.worker_name || rt.name, device_name: '本机', health: rt.health || 'offline',
      scope: 'global', owner: rt.owner || '', worker_ip: rt.worker_ip || '', credential_id: 0, runtimes: [rt] }));
  __runtimeDevices = devices;

  const tabBtn = t => {
    const active = _runtimeTab === t;
    return `<button class="bt ${active ? 'bt-p' : 'bt-o'}" onclick="_runtimeTab='${t}';renderRuntimes(__runtimeData)">${t === 'engines' ? 'Engines 引擎' : 'Workers 设备'}</button>`;
  };
  let html = `<div style="display:flex;gap:8px;margin-bottom:12px;align-items:center;flex-wrap:wrap">
      ${tabBtn('engines')}${tabBtn('workers')}
      <div class="sp"></div>
      ${_runtimeTab === 'workers' ? '<button class="bt bt-p" onclick="openGenCredential()">＋ 生成凭据</button>' : ''}
      <button class="bt bt-o" onclick="refreshRuntimes()">⟳ Refresh</button>
    </div>`;
  html += _runtimeTab === 'engines' ? renderEngines(data, devices) : renderWorkers(data, devices);
  html += _legendHtml();
  el.innerHTML = html;
}

function renderEngines(data, devices) {
  const runtimes = data.runtimes || [];
  const groups = { global: [], repo: [], user: [] };
  runtimes.forEach(rt => { (groups[rt.scope] || groups.global).push(rt); });
  let html = '';
  SCOPE_ORDER.forEach(sc => {
    const list = groups[sc];
    if (!list.length) return;
    list.sort((a, b) => (b.is_local ? 1 : 0) - (a.is_local ? 1 : 0));
    const localN = list.filter(r => r.is_local).length;
    html += _scopeBar(sc, list.length, localN);
    html += `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px">`;
    list.forEach(rt => {
      const localBadge = rt.is_local
        ? `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;color:#0e7490;background:#cffafe;border-radius:999px;padding:1px 8px;white-space:nowrap;vertical-align:middle">本机直连</span>`
        : '';
      html += `<div class="rt-card" id="rt-${rt.id}" style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh);cursor:pointer;transition:all .15s" onclick="toggleRuntimeTasks(${rt.id}, '${(rt.instance_name || rt.name || '').replace(/'/g,"")}')">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
          ${_healthBadge(rt.health)}
          <span style="font-weight:600;font-size:14px">${rt.instance_name || rt.name} ${localBadge}</span>
          <span style="font-weight:400;color:var(--t3);font-size:12px;margin-left:auto">${rt.name} v${rt.version || ''}</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px;color:var(--t2)">
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">worker</span> ${rt.worker_name || (rt.is_local ? '本机' : '—')}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">owner</span> ${rt.owner || '—'} ${rt.is_local ? `<span style="color:var(--t3);font-size:10px">(${rt.owner_id || 'global'})</span>` : ''}</div>
        </div>
      </div>`;
    });
    html += `</div>`;
  });
  return html || '<div class="ts t2" style="padding:12px">暂无引擎</div>';
}

// 引擎卡片点击 → 弹窗显示任务列表
let _expandedRuntime = null;
function toggleRuntimeTasks(rtId, rtName) {
  openRuntimeTaskModal(rtId, rtName);
}

function openRuntimeTaskModal(rtId, rtName) {
  // 移除已有弹窗
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

// 任务行点击 → 弹窗显示运行日志
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

  // 并行加载日志和时间线
  API.get('/task-logs/' + taskId + '/messages?since=0').then(msgs => {
    renderTaskLogMessages(document.getElementById('rtLogModalBody'), msgs || [], info);
  }).catch(e => {
    document.getElementById('rtLogModalBody').innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });

  // 加载时间线（/info 端点需认证，用 API.get）
  API.get('/task-logs/' + taskId + '/info').then(info => {
    renderTaskTimeline(document.getElementById('rtLogTimeline'), info);
  }).catch(e => {
    // 时间线加载失败不影响日志查看
  });
}

function renderTaskTimeline(container, info) {
  if (!container || !info) return;
  const stages = [];
  if (info.created_at) stages.push({ label: '入队', time: info.created_at, color: 'var(--y)' });
  if (info.started_at) stages.push({ label: '开始执行', time: info.started_at, color: 'var(--a)' });
  if (info.completed_at) stages.push({ label: '完成', time: info.completed_at, color: info.status === 'failed' ? 'var(--r)' : 'var(--g)' });
  if (!stages.length) return;

  // 计算各阶段耗时
  const durations = [];
  for (let i = 1; i < stages.length; i++) {
    const prev = new Date(stages[i-1].time);
    const curr = new Date(stages[i].time);
    const diff = Math.round((curr - prev) / 1000);
    durations.push(diff);
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
      const ei = errorInfo;
      if (ei.result_text) {
        html += `<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#d49c0f">结果</span> <span style="color:#e0e6f0">${ei.result_text}</span></div>`;
      }
      if (ei.exit_code != null) {
        html += `<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#d49c0f">Exit Code</span> <span style="color:${ei.exit_code !== 0 ? '#dc2828' : '#1ca44c'}">${ei.exit_code}</span></div>`;
      }
      if (ei.errors && ei.errors.length) {
        html += '<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06)"><span style="color:#d49c0f">错误信息</span></div>';
        ei.errors.forEach(e => {
          html += `<div style="padding:4px 14px 4px 28px;color:#dc2828">${e}</div>`;
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

function renderWorkers(data, devices) {
  const groups = { global: [], repo: [], user: [] };
  devices.forEach(d => { (groups[d.scope] || groups.global).push(d); });
  let html = '';
  SCOPE_ORDER.forEach(sc => {
    const list = groups[sc];
    if (!list.length) return;
    html += _scopeBar(sc, list.length);
    html += `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px">`;
    list.forEach(d => {
      const clis = (d.runtimes || []).map(rt => `${rt.name} v${rt.version || ''}${rt.healthy ? ' ✓' : ' ✗'}`).join(' · ');
      const credColor = d.credential_status === 'revoked' ? 'var(--r)' : 'var(--g)';
      html += `<div style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
          ${_healthBadge(d.health, d.credential_id ? relTime(d.last_seen_at) : '本机直连')}
          <span style="font-weight:600;font-size:14px">${d.name || d.device_name || '?'} <span style="font-weight:400;color:var(--t3);font-size:12px">${d.worker_ip || ''}</span></span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px;color:var(--t2)">
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">owner</span> ${d.owner || '—'} ${d.credential_id === 0 ? `<span style="color:var(--t3);font-size:10px">(${d.owner_id || 'global'})</span>` : ''}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">凭据</span> <b style="color:${credColor}">${d.credential_status || ''}</b>${d.credential_id ? ` <button class="bt bt-o" onclick="renameCredential(${d.credential_id})">改名</button> <button class="bt bt-r" onclick="revokeCredential(${d.credential_id})">吊销</button>` : ''}</div>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:8px;font-size:12px;color:var(--t2)">
          <div><span style="color:var(--t3);min-width:46px;display:inline-block">承载引擎</span> ${clis || '—'}</div>
          ${d.credential_id ? `<button class="bt bt-o" onclick="openDeviceTasks(${d.credential_id})">任务</button>` : ''}
        </div>
      </div>`;
    });
    html += `</div>`;
  });
  return html || '<div class="ts t2" style="padding:12px">暂无 Worker</div>';
}

function relTime(iso) {
  if (!iso) return '未上线';
  try {
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return Math.round(diff) + 's 前';
    if (diff < 3600) return Math.round(diff / 60) + 'm 前';
    return Math.round(diff / 3600) + 'h 前';
  } catch (e) { return iso; }
}

function openGenCredential() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const cur = _currentUser();
  const html = `<div class="cd" id="genCredPanel" style="margin-bottom:12px;padding:14px 18px">
    <h3 style="font-size:14px;margin-bottom:10px">生成凭据</h3>
    <div class="srow"><label>凭据名称</label><input id="credName" placeholder="如 CI-Worker-01" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"></div>
    <div class="srow"><label>归属用户</label><input value="${cur}" disabled style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:#f1f5f9"></div>
    <div class="srow"><label>范围</label><select id="credScope" onchange="toggleRepoKey()" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"><option value="user">个人</option><option value="repo">仓库</option><option value="global">全局</option></select></div>
    <div class="srow" id="repoKeyRow" style="display:none"><label>仓库 (owner/repo)</label><input id="credRepoKey" placeholder="owner/repo" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"></div>
    <div class="flex gap6" style="margin-top:12px"><button class="bt bt-p" onclick="doCreateCredential()">生成</button><button class="bt bt-o" onclick="closeGenCredential()">取消</button></div>
  </div>`;
  el.insertAdjacentHTML('afterbegin', html);
}

function toggleRepoKey() {
  const el = document.getElementById('repoKeyRow');
  if (el) el.style.display = document.getElementById('credScope').value === 'repo' ? '' : 'none';
}

function closeGenCredential() {
  const el = document.getElementById('genCredPanel');
  if (el) el.remove();
}

function doCreateCredential() {
  const name = (document.getElementById('credName') || {}).value || '';
  if (!name) { app.showToast('请填写凭据名称'); return; }
  const scope = document.getElementById('credScope').value;
  const repo_key = (scope === 'repo' || scope === 'user') ? ((document.getElementById('credRepoKey') || {}).value || '').trim() : '';
  if (scope === 'repo' && !repo_key) { app.showToast('仓库范围需填写仓库 owner/repo'); return; }
  API.post('/settings/runtime-credentials', { name, scope, repo_key }).then(d => {
    const token = d.token || '';
    const runCmd = `RUNTIME_TOKEN=${token} python -m runtime.agent`;
    const oneCmd = `curl -fsSL ${window.location.origin}/api/runtime/install | bash -s -- --token ${token} --server ${window.location.origin}`;
    closeGenCredential();
    const box = `<div class="cd" style="margin-top:12px;padding:16px">
      <div style="font-size:13px;margin-bottom:8px">⚠️ token 仅展示一次，请立即保存（凭据名: ${name}，范围: ${_scopeLabel(scope)}）：</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px">${token}</code>
      <div style="margin-top:10px;font-size:12px">📦 在 Worker 机器（普通用户）一键安装接入：</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px;margin-top:4px">${oneCmd}</code>
      <button class="bt bt-p" style="margin-top:8px" onclick="copyText('${oneCmd.replace(/'/g, "\\'")}')">📋 复制一键命令</button>
      <div style="margin-top:8px;font-size:11px;color:var(--t2)">该命令从主控下载运行包（grape-worker.tar.gz）解压到用户家目录，自动写入凭据与主控地址并后台启动注册；免 root。重复执行可升级。</div>
      <div style="margin-top:10px;font-size:12px">或手动启动 RuntimeAgent：</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px;margin-top:4px">${runCmd}</code>
      <button class="bt bt-o" style="margin-top:8px" onclick="copyText('${token}')">📋 复制 token</button>
    </div>`;
    const el = document.getElementById('pageContent');
    el.insertAdjacentHTML('afterbegin', box);
    app.showToast('凭据已生成');
  }).catch(e => app.showToast('生成失败: ' + e.message));
}

function renameCredential(cid) {
  const nn = prompt('新的凭据名称（全局唯一）：');
  if (!nn) return;
  API.put('/settings/runtime-credentials/' + cid, { name: nn.trim() }).then(() => {
    app.showToast('已改名');
    refreshRuntimes();
  }).catch(e => app.showToast('改名失败: ' + e.message));
}

function copyText(t) {
  try {
    navigator.clipboard.writeText(t).then(() => app.showToast('已复制'));
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = t; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    app.showToast('已复制');
  }
}

function revokeCredential(cid) {
  if (!confirm('确认吊销并删除该凭据？此操作不可恢复，此后该 worker 将无法接入，凭据名可重新使用。')) return;
  API.post('/settings/runtime-credentials/' + cid + '/revoke').then(() => {
    app.showToast('已吊销并删除');
    refreshRuntimes();
  }).catch(e => app.showToast('吊销失败: ' + e.message));
}

function openDeviceTasks(credId) {
  API.get('/runtime/credentials/' + credId + '/tasks').then(d => {
    const tasks = d.tasks || [];
    const statusIcon = { queued: '⏳', dispatched: '🔄', running: '🔄', completed: '✅', failed: '❌', cancelled: '⛔' };
    const rows = tasks.length ? tasks.map(t =>
      `<tr><td>${t.id}</td><td>${t.repo_key || ''}<br><span class="ts t2">${t.agent_type || ''} / ${t.skill || ''}</span></td>
       <td>${statusIcon[t.status] || ''} ${t.status}</td><td>${t.exit_code ?? ''}</td><td class="ts t2">${(t.result_text || '').slice(0, 60)}</td></tr>`).join('')
      : '<tr><td colspan="5" style="text-align:center;color:var(--t3)">暂无任务</td></tr>';
    const html = `<div class="cd" style="margin-top:12px">
      <h3 style="font-size:14px;margin-bottom:10px">凭据 #${credId} · 任务</h3>
      <table style="width:100%;font-size:12px;border-collapse:collapse">
        <thead><tr style="color:var(--t3)"><th align="left">Task ID</th><th align="left">仓库/Agent</th><th align="left">状态</th><th align="left">Exit</th><th align="left">结果</th></tr></thead>
        <tbody>${rows}</tbody></table>
    </div>`;
    const el = document.getElementById('pageContent');
    el.insertAdjacentHTML('afterbegin', html);
  }).catch(e => app.showToast('加载任务失败: ' + e.message));
}

function refreshRuntimes() {
  const el = document.getElementById('pageContent');
  if (el) el.innerHTML = '<div class="loading">刷新中...</div>';
  API.post('/settings/agent-runtimes/refresh').then(() => {
    API.get('/settings/agent-runtimes').then(renderRuntimes).catch(() => {});
    app.showToast('刷新成功');
  }).catch(e => app.showToast('刷新失败: ' + e.message));
}

function renderRuntimesLegacy(data) { renderRuntimes(data); }
