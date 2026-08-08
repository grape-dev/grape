/* Agent Runtimes 管理页（M4）：设备列表（健康四态分组）+ 凭据管理 + 设备任务列表。
   见 agent-runtime-event-full-design.md §11.1。 */
const RUNTIME_ICON = { online: '●', recently_lost: '○', offline: '○', about_to_gc: '○' };
const RUNTIME_COLOR = { online: 'var(--g)', recently_lost: 'var(--or)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };

function renderRuntimes(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const devices = data.devices && data.devices.length ? data.devices
    : (data.runtimes || []).map(rt => ({ name: rt.name, device_name: '本机', health: rt.health || 'offline',
      runtimes: [rt], credential_id: 0 }));
  const groups = { online: [], recently_lost: [], offline: [] };
  devices.forEach(d => {
    const g = d.health === 'online' ? 'online' : (d.health === 'recently_lost' ? 'recently_lost' : 'offline');
    groups[g].push(d);
  });

  let html = `<div style="display:flex;gap:8px;margin-bottom:14px">
      <button class="bt bt-o" onclick="refreshRuntimes()">⟳ Refresh</button>
      <button class="bt bt-p" onclick="openGenCredential()">＋ 生成凭据</button>
    </div>`;

  const labels = [['online', '在线'], ['recently_lost', '刚失联'], ['offline', '离线']];
  labels.forEach(([key, label]) => {
    const list = groups[key];
    if (!list.length) return;
    html += `<div style="font-size:12px;color:var(--t3);margin:12px 0 6px">${label} (${list.length})</div>`;
    list.forEach(d => html += deviceCard(d));
  });

  html += `<div style="margin-top:18px;border-top:1px solid var(--b);padding-top:10px;font-size:12px;color:var(--t3)">凭据管理</div>`;
  html += `<div id="credList">加载中...</div>`;
  el.innerHTML = html;
  loadCredentials();
}

function deviceCard(d) {
  const clis = (d.runtimes || []).map(rt =>
    `<span class="tgx">${rt.name}${rt.version ? ' v' + rt.version : ''}${rt.healthy ? ' ✓' : ' ✗'}</span>`).join(' ');
  return `<div class="cd" style="padding:14px 18px;margin-bottom:8px">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span style="width:10px;height:10px;border-radius:50%;background:${RUNTIME_COLOR[d.health] || 'var(--t3)'};display:inline-block"></span>
      <b style="font-size:14px">${d.device_name || d.name || '?'}</b>
      <span class="ts t2">${d.name || ''}</span>
      <span class="ts t2">心跳: ${relTime(d.last_seen_at)}</span>
      <div class="sp"></div>
      ${d.credential_id ? `<button class="bt bt-o bt-s" onclick="openDeviceTasks(${d.credential_id})">查看任务</button>` : ''}
    </div>
    <div style="margin-top:6px;font-size:12px">承载: ${clis || '—'}</div>
  </div>`;
}

function relTime(iso) {
  if (!iso) return '未知';
  try {
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return Math.round(diff) + 's 前';
    if (diff < 3600) return Math.round(diff / 60) + 'm 前';
    return Math.round(diff / 3600) + 'h 前';
  } catch (e) { return iso; }
}

function loadCredentials() {
  const el = document.getElementById('credList');
  API.get('/settings/runtime-credentials').then(d => {
    const creds = d.credentials || [];
    if (!creds.length) { el.innerHTML = '<span class="ts t2">暂无凭据</span>'; return; }
    el.innerHTML = creds.map(c =>
      `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--b)">
        <span>${c.name}</span>
        <span class="ts t2">${c.device_name || ''} · ${c.status}</span>
        <span class="ts t2">${c.last_seen_at ? '心跳 ' + relTime(c.last_seen_at) : '未上线'}</span>
        <div class="sp"></div>
        ${c.status === 'active' ? `<button class="bt bt-o bt-s" onclick="revokeCredential(${c.id})">吊销</button>` : ''}
      </div>`).join('');
  }).catch(() => { el.innerHTML = ''; });
}

function openGenCredential() {
  const name = prompt('凭据名称（如 CI-Worker-01）：');
  if (!name) return;
  API.post('/settings/runtime-credentials', { name }).then(d => {
    const token = d.token || '';
    const runCmd = `RUNTIME_TOKEN=${token} python -m runtime.agent`;
    const box = `<div class="cd" style="margin-top:12px;padding:16px">
      <div style="font-size:13px;margin-bottom:8px">⚠️ token 仅展示一次，请立即保存：</div>
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
    API.get('/settings/agent-runtimes').then(renderRuntimes).catch(() => {});
  }).catch(e => app.showToast('吊销失败: ' + e.message));
}

function openDeviceTasks(credId) {
  API.get('/runtime/runtimes/' + credId + '/tasks').then(d => {
    const tasks = d.tasks || [];
    const statusIcon = { queued: '⏳', dispatched: '🔄', running: '🔄', completed: '✅', failed: '❌', cancelled: '⛔' };
    const rows = tasks.length ? tasks.map(t =>
      `<tr><td>${t.id}</td><td>${t.repo_key || ''}<br><span class="ts t2">${t.agent_type || ''} / ${t.skill || ''}</span></td>
       <td>${statusIcon[t.status] || ''} ${t.status}</td><td>${t.exit_code ?? ''}</td><td class="ts t2">${(t.result_text || '').slice(0, 60)}</td></tr>`).join('')
      : '<tr><td colspan="5" style="text-align:center;color:var(--t3)">暂无任务</td></tr>';
    const html = `<div class="cd" style="margin-top:12px">
      <h3 style="font-size:14px;margin-bottom:10px">设备 #${credId} · 任务</h3>
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
  API.post('/settings/agent-runtimes/refresh').then(d => {
    renderRuntimes(d);
    app.showToast('刷新成功');
  }).catch(e => app.showToast('刷新失败: ' + e.message));
}

function renderRuntimesLegacy(data) {
  renderRuntimes(data);
}
