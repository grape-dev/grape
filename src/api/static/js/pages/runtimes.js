/* Agent Runtime 管理页（M4 双视图）：Engines / Workers 两个子页，各按 scope 分三张表。
   见 runtime-admin-two-view-design.md。 */
const RUNTIME_ICON = { online: '●', recently_lost: '○', offline: '○', about_to_gc: '○' };
const RUNTIME_COLOR = { online: 'var(--g)', recently_lost: 'var(--or)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };
const SCOPE_NAMES = { global: '全局', repo: '仓库', user: '个人' };
const SCOPE_ORDER = ['global', 'repo', 'user'];
const HEALTH_NAMES = { online: '在线', recently_lost: '刚失联', offline: '离线', about_to_gc: '待回收' };

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
    return `<button onclick="_runtimeTab='${t}';renderRuntimes(__runtimeData)" style="padding:7px 16px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;cursor:pointer;${active ? 'background:var(--a);color:#fff' : 'background:var(--c)'}">${t === 'engines' ? 'Engines 引擎' : 'Workers 设备'}</button>`;
  };
  let html = `<div style="display:flex;gap:8px;margin-bottom:12px;align-items:center;flex-wrap:wrap">
      ${tabBtn('engines')}${tabBtn('workers')}
      <div class="sp"></div>
      ${_runtimeTab === 'workers' ? '<button class="bt bt-p" onclick="openGenCredential()">＋ 生成凭据</button>' : ''}
      <button class="bt bt-o" onclick="refreshRuntimes()">⟳ Refresh</button>
    </div>`;
  html += _runtimeTab === 'engines' ? renderEngines(data, devices) : renderWorkers(data, devices);
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
    html += `<div style="font-size:13px;font-weight:600;color:var(--t3);margin:14px 0 8px">${_scopeLabel(sc)} (${list.length})</div>`;
    html += `<table style="width:100%;font-size:12px;border-collapse:collapse">
      <thead><tr style="color:var(--t3)"><th align="left">Engine Name</th><th align="left">Type</th><th align="left">Version</th><th align="left">Worker Name</th><th align="left">Scope</th><th align="left">Owner</th><th align="left">Status</th></tr></thead><tbody>`;
    list.forEach(rt => {
      html += `<tr style="border-bottom:1px solid var(--b)">
        <td>${rt.instance_name || rt.name}</td><td>${rt.name}</td><td>${rt.version || ''}</td>
        <td>${rt.worker_name || ''}</td><td>${_scopeLabel(rt.scope)}</td><td>${rt.owner || ''}</td>
        <td>${_healthDot(rt.health)} ${_healthLabel(rt.health)}</td></tr>`;
    });
    html += `</tbody></table>`;
  });
  return html || '<div class="ts t2" style="padding:12px">暂无引擎</div>';
}

function renderWorkers(data, devices) {
  const groups = { global: [], repo: [], user: [] };
  devices.forEach(d => { (groups[d.scope] || groups.global).push(d); });
  let html = '';
  SCOPE_ORDER.forEach(sc => {
    const list = groups[sc];
    if (!list.length) return;
    html += `<div style="font-size:13px;font-weight:600;color:var(--t3);margin:14px 0 8px">${_scopeLabel(sc)} (${list.length})</div>`;
    html += `<table style="width:100%;font-size:12px;border-collapse:collapse">
      <thead><tr style="color:var(--t3)"><th align="left">Worker Name</th><th align="left">IP</th><th align="left">Scope</th><th align="left">Owner</th><th align="left">心跳</th><th align="left">凭据</th><th align="left">承载引擎</th><th align="left"></th></tr></thead><tbody>`;
    list.forEach(d => {
      const clis = (d.runtimes || []).map(rt => `${rt.name} v${rt.version || ''}${rt.healthy ? ' ✓' : ' ✗'}`).join(' · ');
      html += `<tr style="border-bottom:1px solid var(--b)">
        <td>${d.name || d.device_name || '?'}</td><td>${d.worker_ip || ''}</td>
        <td>${_scopeLabel(d.scope)}</td><td>${d.owner || ''}</td>
        <td>${_healthDot(d.health)} ${_healthLabel(d.health)} · ${relTime(d.last_seen_at)}</td>
        <td>${d.credential_status || ''}${d.credential_id ? ` <button class="bt bt-o bt-s" onclick="renameCredential(${d.credential_id})">改名</button> <button class="bt bt-r bt-s" onclick="revokeCredential(${d.credential_id})">吊销</button>` : ''}</td>
        <td>${clis || '—'}</td>
        <td>${d.credential_id ? `<button class="bt bt-o bt-s" onclick="openDeviceTasks(${d.credential_id})">任务</button>` : ''}</td></tr>`;
    });
    html += `</tbody></table>`;
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
    <div class="flex gap6" style="margin-top:12px"><button class="bt bt-p bt-s" onclick="doCreateCredential()">生成</button><button class="bt bt-o bt-s" onclick="closeGenCredential()">取消</button></div>
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
  const repo_key = scope === 'repo' ? ((document.getElementById('credRepoKey') || {}).value || '').trim() : '';
  if (scope === 'repo' && !repo_key) { app.showToast('仓库范围需填写仓库 owner/repo'); return; }
  API.post('/settings/runtime-credentials', { name, scope, repo_key }).then(d => {
    const token = d.token || '';
    const runCmd = `RUNTIME_TOKEN=${token} python -m runtime.agent`;
    closeGenCredential();
    const box = `<div class="cd" style="margin-top:12px;padding:16px">
      <div style="font-size:13px;margin-bottom:8px">⚠️ token 仅展示一次，请立即保存（凭据名: ${name}，范围: ${_scopeLabel(scope)}）：</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px">${token}</code>
      <div style="margin-top:10px;font-size:12px">RuntimeAgent 启动命令：</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px;margin-top:4px">${runCmd}</code>
      <button class="bt bt-p bt-s" style="margin-top:10px" onclick="copyText('${token}')">📋 复制</button>
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
  if (!confirm('确认吊销该凭据？此后该 worker 将无法接入。')) return;
  API.post('/settings/runtime-credentials/' + cid + '/revoke').then(() => {
    app.showToast('已吊销');
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
