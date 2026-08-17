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
    return `<button onclick="_runtimeTab='${t}';renderRuntimes(__runtimeData)" style="padding:7px 16px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;cursor:pointer;${active ? 'background:var(--a);color:#fff' : 'background:var(--c)'}">${t === 'engines' ? 'Engines 引擎' : 'Workers 设备'}</button>`;
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
    // 本机直连引擎（credential_id=0）恒排分组最前，与远端 worker 引擎区分（engine-selection-design.md §4.3）
    list.sort((a, b) => (b.is_local ? 1 : 0) - (a.is_local ? 1 : 0));
    const localN = list.filter(r => r.is_local).length;
    html += _scopeBar(sc, list.length, localN);
    html += `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px">`;
    list.forEach(rt => {
      const localBadge = rt.is_local
        ? `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;color:#0e7490;background:#cffafe;border-radius:999px;padding:1px 8px;white-space:nowrap;vertical-align:middle">本机直连</span>`
        : '';
      html += `<div style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
          ${_healthBadge(rt.health)}
          <span style="font-weight:600;font-size:14px">${rt.instance_name || rt.name} ${localBadge}</span>
          <span style="font-weight:400;color:var(--t3);font-size:12px;margin-left:auto">${rt.name} v${rt.version || ''}</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px;color:var(--t2)">
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">worker</span> ${rt.worker_name || (rt.is_local ? '本机' : '—')}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">owner</span> ${rt.owner || '—'}</div>
        </div>
      </div>`;
    });
    html += `</div>`;
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
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">owner</span> ${d.owner || '—'}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">凭据</span> <b style="color:${credColor}">${d.credential_status || ''}</b>${d.credential_id ? ` <button class="bt bt-o bt-s" onclick="renameCredential(${d.credential_id})">改名</button> <button class="bt bt-r bt-s" onclick="revokeCredential(${d.credential_id})">吊销</button>` : ''}</div>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:8px;font-size:12px;color:var(--t2)">
          <div><span style="color:var(--t3);min-width:46px;display:inline-block">承载引擎</span> ${clis || '—'}</div>
          ${d.credential_id ? `<button class="bt bt-o bt-s" onclick="openDeviceTasks(${d.credential_id})">任务</button>` : ''}
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
    const oneCmd = `curl -fsSL ${window.location.origin}/api/runtime/install | bash -s -- --token ${token} --server ${window.location.origin}`;
    closeGenCredential();
    const box = `<div class="cd" style="margin-top:12px;padding:16px">
      <div style="font-size:13px;margin-bottom:8px">⚠️ token 仅展示一次，请立即保存（凭据名: ${name}，范围: ${_scopeLabel(scope)}）：</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px">${token}</code>
      <div style="margin-top:10px;font-size:12px">📦 在 Worker 机器（普通用户）一键安装接入：</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px;margin-top:4px">${oneCmd}</code>
      <button class="bt bt-p bt-s" style="margin-top:8px" onclick="copyText('${oneCmd.replace(/'/g, "\\'")}')">📋 复制一键命令</button>
      <div style="margin-top:8px;font-size:11px;color:var(--t2)">该命令从主控下载运行包（grape-worker.tar.gz）解压到用户家目录，自动写入凭据与主控地址并后台启动注册；免 root。重复执行可升级。</div>
      <div style="margin-top:10px;font-size:12px">或手动启动 RuntimeAgent：</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px;margin-top:4px">${runCmd}</code>
      <button class="bt bt-o bt-s" style="margin-top:8px" onclick="copyText('${token}')">📋 复制 token</button>
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
