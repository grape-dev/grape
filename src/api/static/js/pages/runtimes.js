/* Agent Runtime 管理页（M4 双视图）：Engines / Workers 两个子页，各按 scope 分区展示。
   见 runtime-admin-two-view-design.md。卡片式布局：状态徽章置顶，异常引擎优先捕捉。 */
const RUNTIME_ICON = { online: '●', recently_lost: '◐', offline: '○', about_to_gc: '⚠' };
const RUNTIME_COLOR = { online: 'var(--g)', recently_lost: 'var(--or)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };
const SCOPE_NAMES = { global: 'global', repo: 'repo', user: 'user' };
const SCOPE_ORDER = ['global', 'repo', 'user'];
const HEALTH_NAMES = { online: 'online', recently_lost: 'recently_lost', offline: 'offline', about_to_gc: 'about_to_gc' };
const HEALTH_BADGE = {
  online: { color: 'var(--g)', bg: '#dcfce7' },
  recently_lost: { color: 'var(--or)', bg: '#fef3c7' },
  offline: { color: 'var(--t3)', bg: '#eef0f3' },
  about_to_gc: { color: 'var(--r)', bg: '#fee2e2' },
};

let _runtimeTab = 'engines';
let __runtimeData = null;
let __runtimeDevices = null;
let _skillScanDirty = false;  // skills 扫描成功后标记，关闭弹窗时刷新引擎列表

function _currentUser() {
  try {
    const u = JSON.parse(localStorage.getItem('user') || 'null');
    return (u && (u.nickname || u.username)) || '';
  } catch (e) { return ''; }
}

function _healthDot(h) { return `<span style="color:${RUNTIME_COLOR[h] || 'var(--t3)'}">${RUNTIME_ICON[h] || '○'}</span>`; }
function _healthLabel(h) { return I18N.t('runtimes.health.' + (HEALTH_NAMES[h] || 'offline')); }
function _scopeLabel(s) { return I18N.t('runtimes.scope.' + (SCOPE_NAMES[s] || 'global')); }

function _healthBadge(h, suffix) {
  const st = HEALTH_BADGE[h] || HEALTH_BADGE.offline;
  return `<span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;color:${st.color};background:${st.bg};padding:3px 10px;border-radius:999px;white-space:nowrap"><span style="width:7px;height:7px;border-radius:50%;background:${st.color}"></span>${_healthLabel(h)}${suffix ? ' · ' + suffix : ''}</span>`;
}

function _scopeBar(sc, n, localN) {
  // global 分组额外标注本机直连/远端数量（engine-selection-design.md §4.3）
  const sub = (sc === 'global' && localN) ? ` <span style="color:#0e7490;font-weight:600">· ${I18N.t('runtimes.label.local_direct')} ${localN} / ${I18N.t('runtimes.label.remote')} ${(n || 0) - localN}</span>` : '';
  return `<div style="display:inline-flex;align-items:center;gap:8px;background:#e3edfd;color:#235ec7;padding:5px 12px;border-radius:6px;font-size:12px;font-weight:600;margin:16px 0 10px">${_scopeLabel(sc)} <span style="background:rgba(35,94,199,.12);border-radius:10px;padding:0 8px;font-size:11px">${n}</span>${sub}</div>`;
}

function _legendHtml() {
  return `<div style="display:flex;gap:14px;flex-wrap:wrap;padding:12px 0;font-size:11px;color:var(--t3);border-top:1px dashed var(--b)">
    ${Object.entries(HEALTH_NAMES).map(([k, v]) =>
      `<span style="display:inline-flex;align-items:center;gap:5px"><span style="width:7px;height:7px;border-radius:50%;background:${(HEALTH_BADGE[k] || {}).color || 'var(--t3)'}"></span>${I18N.t('runtimes.health.' + v)}${k === 'recently_lost' ? '(<5min)' : k === 'about_to_gc' ? '(>6d)' : ''}</span>`).join('')}
  </div>`;
}

function renderRuntimes(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  __runtimeData = data;
  const devices = (data.devices && data.devices.length)
    ? data.devices
    : (data.runtimes || []).map(rt => ({ name: rt.worker_name || rt.name, device_name: I18N.t('runtimes.label.local'), health: rt.health || 'offline',
      scope: 'global', owner: rt.owner || '', worker_ip: rt.worker_ip || '', credential_id: 0, runtimes: [rt] }));
  __runtimeDevices = devices;

  const tabBtn = t => {
    const active = _runtimeTab === t;
    return `<button class="bt ${active ? 'bt-p' : 'bt-o'}" onclick="_runtimeTab='${t}';renderRuntimes(__runtimeData)">${I18N.t('runtimes.tab.' + t)}</button>`;
  };
  let html = `<div style="display:flex;gap:8px;margin-bottom:12px;align-items:center;flex-wrap:wrap">
      ${tabBtn('engines')}${tabBtn('workers')}
      <div class="sp"></div>
      ${_runtimeTab === 'workers' ? `<button class="bt bt-p" onclick="openGenCredential()">${I18N.t('runtimes.action.gen_credential')}</button>` : ''}
      ${_runtimeTab === 'workers' ? `<button class="bt bt-p" onclick="openAiDevspace()" data-tooltip="${I18N.t('runtimes.devspace.ai_devspace_tooltip')}" style="background:linear-gradient(135deg,#60a5fa,#a78bfa);border:none;position:relative;box-shadow:0 2px 8px rgba(96,165,250,.3)">☁ AI Devspace</button>` : ''}
      <button class="bt bt-o" onclick="refreshRuntimes()">⟳ ${I18N.t('runtimes.refresh')}</button>
    </div>`;
  html += _runtimeTab === 'engines' ? renderEngines(data, devices) : renderWorkers(data, devices);
  html += '<style>[data-tooltip]{position:relative}[data-tooltip]:hover::after{content:attr(data-tooltip);position:absolute;top:calc(100% + 8px);left:50%;transform:translateX(-50%);background:#1e2940;color:#fff;padding:6px 14px;border-radius:8px;font-size:11px;font-weight:500;white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,.15);z-index:100;pointer-events:none;opacity:0;animation:tipIn .2s ease forwards}[data-tooltip]:hover::before{content:"";position:absolute;top:calc(100% + 3px);left:50%;transform:translateX(-50%);border:5px solid transparent;border-bottom-color:#1e2940;z-index:100;pointer-events:none;opacity:0;animation:tipIn .2s ease forwards}@keyframes tipIn{to{opacity:1}}</style>';
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
        ? `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;color:#0e7490;background:#cffafe;border-radius:999px;padding:1px 8px;white-space:nowrap;vertical-align:middle">${I18N.t('runtimes.label.local_direct')}</span>`
        : '';
      const hostTag = rt.is_local && rt.host_name ? `<span style="font-size:10px;color:var(--t3);margin-left:4px">${rt.host_name}</span>` : '';
      html += `<div style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh);cursor:pointer" onclick="openEngineDetail(${rt.id})">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
          ${_healthBadge(rt.health)}
          <span style="font-weight:600;font-size:14px">${rt.instance_name || rt.name} ${localBadge}${hostTag}</span>
          <span style="font-weight:400;color:var(--t3);font-size:12px;margin-left:auto">${rt.name} v${rt.version || ''}</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px;color:var(--t2)">
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.worker')}</span> ${rt.worker_name || (rt.is_local ? I18N.t('runtimes.label.local') : '—')}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.owner')}</span> ${rt.owner || '—'} ${rt.is_local ? `<span style="color:var(--t3);font-size:10px">(${I18N.t('runtimes.scope.global')})</span>` : ''}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.skills_label')}</span> ${I18N.t('runtimes.label.skills_count', {count: (rt.local_skills || []).length})}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.tags')}</span> ${_engineTagSummary(rt.agent_tags)}</div>
        </div>
      </div>`;
    });
    html += `</div>`;
  });
  return html || `<div class="ts t2" style="padding:12px">${I18N.t('runtimes.empty.no_engines')}</div>`;
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
      const clis = (d.runtimes || []).map(rt => `<span style="cursor:pointer;color:#235ec7;text-decoration:underline" onclick="event.stopPropagation();openEngineDetail(${rt.id})">${rt.name} v${rt.version || ''}${rt.healthy ? ' ✓' : ' ✗'}</span>`).join(' · ');
      const credColor = d.credential_status === 'revoked' ? 'var(--r)' : 'var(--g)';
      html += `<div style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
          ${_healthBadge(d.health, d.credential_id ? relTime(d.last_seen_at) : I18N.t('runtimes.label.local_direct'))}
          <span style="font-weight:600;font-size:14px">${d.name || d.device_name || '?'}</span>
          <span style="font-weight:400;color:var(--t3);font-size:12px">${d.worker_ip || ''}</span>
          ${d.credential_id ? `<button class="bt bt-o" style="font-size:11px;padding:6px 10px;border-radius:4px;margin-left:auto" onclick="renameCredential(${d.credential_id})">${I18N.t('runtimes.action.rename')}</button>` : ''}
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:16px;font-size:12px;color:var(--t2)">
          <div style="display:flex;gap:16px;flex:1;min-width:0">
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.owner')}</span> ${d.owner || '—'} ${d.credential_id === 0 ? `<span style="color:var(--t3);font-size:10px">(${I18N.t('runtimes.scope.global')})</span>` : ''}</div>
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.credential')}</span> <b style="color:${credColor}">${d.credential_status || ''}</b></div>
          </div>
          ${d.credential_id ? `<button class="bt bt-r" style="font-size:11px;padding:6px 10px;border-radius:4px;flex-shrink:0" onclick="revokeCredential(${d.credential_id})">${I18N.t('runtimes.action.revoke')}</button>` : ''}
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:var(--t2)">
          <span style="color:var(--t3);white-space:nowrap;flex-shrink:0">${I18N.t('runtimes.label.hosted_engines')}</span>
          <span style="flex:1;min-width:0;word-break:break-all">${clis || '—'}</span>
          ${d.credential_id ? `<button class="bt bt-o" style="font-size:11px;padding:6px 10px;border-radius:4px;flex-shrink:0" onclick="openDeviceTasks(${d.credential_id})">${I18N.t('runtimes.action.tasks')}</button>` : ''}
        </div>
      </div>`;
    });
    html += `</div>`;
  });
  return html || `<div class="ts t2" style="padding:12px">${I18N.t('runtimes.empty.no_workers')}</div>`;
}

function relTime(iso) {
  if (!iso) return I18N.t('runtimes.time.not_online');
  try {
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return I18N.t('runtimes.time.seconds_ago', {n: Math.round(diff)});
    if (diff < 3600) return I18N.t('runtimes.time.minutes_ago', {n: Math.round(diff / 60)});
    return I18N.t('runtimes.time.hours_ago', {n: Math.round(diff / 3600)});
  } catch (e) { return iso; }
}

function openGenCredential() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const cur = _currentUser();
  const html = `<div class="cd" id="genCredPanel" style="margin-bottom:12px;padding:14px 18px">
    <h3 style="font-size:14px;margin-bottom:10px">${I18N.t('runtimes.credential.gen_title')}</h3>
    <div class="srow"><label>${I18N.t('runtimes.label.cred_name')}</label><input id="credName" placeholder="${I18N.t('runtimes.label.cred_name_placeholder')}" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"></div>
    <div class="srow"><label>${I18N.t('runtimes.label.owner_user')}</label><input value="${cur}" disabled style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:#f1f5f9"></div>
    <div class="srow"><label>${I18N.t('runtimes.label.scope')}</label><select id="credScope" onchange="toggleRepoKey()" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"><option value="user">${I18N.t('runtimes.scope.user')}</option><option value="repo">${I18N.t('runtimes.scope.repo')}</option><option value="global">${I18N.t('runtimes.scope.global')}</option></select></div>
    <div class="srow" id="repoKeyRow" style="display:none"><label>${I18N.t('runtimes.label.repo_key')}</label><input id="credRepoKey" placeholder="owner/repo" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"></div>
    <div class="flex gap6" style="margin-top:12px"><button class="bt bt-p" onclick="doCreateCredential()">${I18N.t('runtimes.action.generate')}</button><button class="bt bt-o" onclick="closeGenCredential()">${I18N.t('runtimes.action.cancel')}</button></div>
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
  if (!name) { app.showToast(I18N.t('runtimes.credential.toast_name_required')); return; }
  const scope = document.getElementById('credScope').value;
  const repo_key = (scope === 'repo' || scope === 'user') ? ((document.getElementById('credRepoKey') || {}).value || '').trim() : '';
  if (scope === 'repo' && !repo_key) { app.showToast(I18N.t('runtimes.credential.toast_repo_required')); return; }
  API.post('/settings/runtime-credentials', { name, scope, repo_key }).then(d => {
    const token = d.token || '';
    const runCmd = `RUNTIME_TOKEN=${token} python -m runtime.agent`;
    const oneCmd = `curl -fsSL ${window.location.origin}/api/runtime/install | bash -s -- --token ${token} --server ${window.location.origin}`;
    closeGenCredential();
    const box = `<div class="cd" style="margin-top:12px;padding:16px">
      <div style="font-size:13px;margin-bottom:8px">${I18N.t('runtimes.credential.token_warning', {name: name, scope: _scopeLabel(scope)})}</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px">${token}</code>
      <div style="margin-top:10px;font-size:12px">${I18N.t('runtimes.credential.install_hint')}</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px;margin-top:4px">${oneCmd}</code>
      <button class="bt bt-p" style="margin-top:8px" onclick="copyText('${oneCmd.replace(/'/g, "\\'")}')">${I18N.t('runtimes.action.copy_one_cmd')}</button>
      <div style="margin-top:8px;font-size:11px;color:var(--t2)">${I18N.t('runtimes.credential.install_desc')}</div>
      <div style="margin-top:10px;font-size:12px">${I18N.t('runtimes.credential.manual_start')}</div>
      <code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px;margin-top:4px">${runCmd}</code>
      <button class="bt bt-o" style="margin-top:8px" onclick="copyText('${token}')">${I18N.t('runtimes.action.copy_token')}</button>
    </div>`;
    const el = document.getElementById('pageContent');
    el.insertAdjacentHTML('afterbegin', box);
    app.showToast(I18N.t('runtimes.credential.toast_generated'));
  }).catch(e => app.showToast(I18N.t('runtimes.credential.toast_gen_failed', {msg: e.message})));
}

function renameCredential(cid) {
  const nn = prompt(I18N.t('runtimes.credential.rename_prompt'));
  if (!nn) return;
  API.put('/settings/runtime-credentials/' + cid, { name: nn.trim() }).then(() => {
    app.showToast(I18N.t('runtimes.credential.toast_renamed'));
    refreshRuntimes();
  }).catch(e => app.showToast(I18N.t('runtimes.credential.toast_rename_failed', {msg: e.message})));
}

function copyText(t) {
  try {
    navigator.clipboard.writeText(t).then(() => app.showToast(I18N.t('runtimes.credential.toast_copied')));
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = t; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    app.showToast(I18N.t('runtimes.credential.toast_copied'));
  }
}

function revokeCredential(cid) {
  if (!confirm(I18N.t('runtimes.credential.revoke_confirm'))) return;
  API.post('/settings/runtime-credentials/' + cid + '/revoke').then(() => {
    app.showToast(I18N.t('runtimes.credential.toast_revoked'));
    refreshRuntimes();
  }).catch(e => app.showToast(I18N.t('runtimes.credential.toast_revoke_failed', {msg: e.message})));
}

function openDeviceTasks(credId) {
  API.get('/runtime/credentials/' + credId + '/tasks').then(d => {
    const tasks = d.tasks || [];
    const statusIcon = { queued: '⏳', dispatched: '🔄', running: '🔄', completed: '✅', failed: '❌', cancelled: '⛔' };
    const rows = tasks.length ? tasks.map(t =>
      `<tr><td>${t.id}</td><td>${t.repo_key || ''}<br><span class="ts t2">${t.agent_type || ''} / ${t.skill || ''}</span></td>
       <td>${statusIcon[t.status] || ''} ${I18N.t('common.status.' + t.status) !== ('common.status.' + t.status) ? I18N.t('common.status.' + t.status) : t.status}</td><td>${t.exit_code ?? ''}</td><td class="ts t2">${(t.result_text || '').slice(0, 60)}</td></tr>`).join('')
      : `<tr><td colspan="5" style="text-align:center;color:var(--t3)">${I18N.t('runtimes.empty.no_tasks')}</td></tr>`;
    const html = `<div class="cd" style="margin-top:12px">
      <h3 style="font-size:14px;margin-bottom:10px">${I18N.t('runtimes.credential.tasks_title', {id: credId})}</h3>
      <table style="width:100%;font-size:12px;border-collapse:collapse">
        <thead><tr style="color:var(--t3)"><th align="left">${I18N.t('runtimes.label.task_id')}</th><th align="left">${I18N.t('runtimes.label.repo_agent')}</th><th align="left">${I18N.t('runtimes.label.status')}</th><th align="left">${I18N.t('runtimes.label.exit')}</th><th align="left">${I18N.t('runtimes.label.result')}</th></tr></thead>
        <tbody>${rows}</tbody></table>
    </div>`;
    const el = document.getElementById('pageContent');
    el.insertAdjacentHTML('afterbegin', html);
  }).catch(e => app.showToast(I18N.t('runtimes.toast.load_tasks_failed', {msg: e.message})));
}

function refreshRuntimes() {
  _stopDevspaceAutoRefresh();
  const el = document.getElementById('pageContent');
  if (el) el.innerHTML = `<div class="loading">${I18N.t('runtimes.toast.refreshing')}</div>`;
  API.post('/settings/agent-runtimes/refresh').then(() => {
    API.get('/settings/agent-runtimes').then(renderRuntimes).catch(() => {});
    app.showToast(I18N.t('runtimes.toast.refresh_success'));
  }).catch(e => app.showToast(I18N.t('runtimes.toast.refresh_failed', {msg: e.message})));
}

function renderRuntimesLegacy(data) { renderRuntimes(data); }

function openEngineDetail(runtimeId) {
  const allRuntimes = (__runtimeData && __runtimeData.runtimes) || [];
  const rt = allRuntimes.find(r => r.id === runtimeId);
  if (!rt) return;

  const skills = rt.local_skills || [];
  const skillDetails = rt.skill_scan_result || [];
  const isLocal = rt.credential_id === 0 || rt.is_local;
  // 如果有详情数据，直接展示带 description 的列表；否则只展示名称
  const skillListHtml = skills.length
    ? skills.map(s => {
        const detail = skillDetails.find(d => d.name === s);
        const desc = detail ? (detail.description || I18N.t('runtimes.skill.no_desc')) : I18N.t('runtimes.skill.click_detail');
        const detailArg = detail ? JSON.stringify(detail).replace(/'/g, '&#39;').replace(/"/g, '&quot;') : '';
        return `
        <div style="padding:8px 12px;border:1px solid var(--b);border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='var(--bg)'" onmouseout="this.style.background=''" onclick="openSkillDetail('${s}', ${runtimeId}, ${detailArg})">
          <div style="font-weight:600;font-size:13px;color:var(--t1)">${s}</div>
          <div style="font-size:11px;color:var(--t3);margin-top:2px">${desc}</div>
        </div>`;
      }).join('')
    : `<div style="color:var(--t3);font-size:12px;padding:8px">${I18N.t('runtimes.empty.no_local_skill')}</div>`;

  const html = `<div id="engineDetailOverlay" style="position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center" onclick="closeEngineDetail(event)">
    <div style="background:var(--c);border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.2);max-width:680px;width:90%;max-height:85vh;overflow:auto;padding:24px" onclick="event.stopPropagation()">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
        ${_healthBadge(rt.health)}
        <span style="font-weight:700;font-size:18px">${rt.instance_name || rt.name}</span>
        ${rt.is_local ? `<span style="font-size:11px;font-weight:600;color:#0e7490;background:#cffafe;border-radius:999px;padding:1px 8px">${I18N.t('runtimes.label.local_direct')}</span>` : ''}
        <button class="bt bt-o" style="margin-left:auto" onclick="closeEngineDetail()">${I18N.t('runtimes.action.close')}</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:13px;color:var(--t2);margin-bottom:20px">
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">${I18N.t('runtimes.label.engine_type')}</span> ${rt.name}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">${I18N.t('runtimes.label.version')}</span> v${rt.version || '—'}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">${I18N.t('runtimes.label.health_status')}</span> ${rt.healthy ? I18N.t('runtimes.label.healthy') : I18N.t('runtimes.label.unhealthy')}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">${I18N.t('runtimes.label.worker')}</span> ${rt.worker_name || (rt.is_local ? I18N.t('runtimes.label.local') : '—')}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">IP</span> ${rt.worker_ip || '—'}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">${I18N.t('runtimes.label.scope_label')}</span> ${_scopeLabel(rt.scope)}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">${I18N.t('runtimes.label.owner')}</span> ${rt.owner || '—'}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">${I18N.t('runtimes.label.installed')}</span> ${rt.installed ? I18N.t('runtimes.label.yes') : I18N.t('runtimes.label.no')}</div>
      </div>
      <div style="border-top:1px solid var(--b);padding-top:16px;margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <span style="font-weight:600;font-size:14px">${I18N.t('runtimes.label.role_tags')}</span>
          <button class="bt bt-p" style="font-size:11px;padding:4px 10px;border-radius:4px" onclick="saveEngineTags(${runtimeId})">${I18N.t('runtimes.action.save_tags')}</button>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">${_engineTagCheckboxes(rt.agent_tags || ['all'])}</div>
      </div>
      <div style="border-top:1px solid var(--b);padding-top:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <span style="font-weight:600;font-size:14px">${I18N.t('runtimes.label.local_skill_list')} <span style="font-weight:400;color:var(--t3);font-size:12px">${I18N.t('runtimes.label.skill_count_paren', {count: skills.length})}</span></span>
          <button id="refreshSkillsBtn" class="bt bt-o" style="padding:4px 10px;font-size:12px" onclick="refreshEngineSkills(${runtimeId})">${I18N.t('runtimes.action.refresh_skills')}</button>
        </div>
        <div id="engineSkillList" style="display:flex;flex-direction:column;gap:6px">${skillListHtml}</div>
      </div>
    </div>
  </div>`;
  document.body.insertAdjacentHTML('beforeend', html);
}

function closeEngineDetail(event) {
  if (event && event.target && event.target.id !== 'engineDetailOverlay') return;
  const el = document.getElementById('engineDetailOverlay');
  if (el) el.remove();
  // skills 扫描成功后关闭弹窗 → 刷新引擎列表卡片（更新 skills 数量）
  if (_skillScanDirty) {
    _skillScanDirty = false;
    refreshRuntimes();
  }
}

function refreshEngineSkills(runtimeId) {
  const btn = document.getElementById('refreshSkillsBtn');
  if (!btn) return;
  btn.disabled = true;
  btn.innerHTML = '<span style="display:inline-block;animation:spin 0.8s linear infinite">⟳</span> ' + I18N.t('runtimes.toast.scanning');
  if (!document.getElementById('spinKeyframes')) {
    const style = document.createElement('style');
    style.id = 'spinKeyframes';
    style.textContent = '@keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}';
    document.head.appendChild(style);
  }
  _pollSkillScan(runtimeId, btn, 0, true);
}

function _pollSkillScan(runtimeId, btn, attempt, isFirst) {
  if (attempt > 30) {  // 最多轮询 30 次 × 2s = 60s 超时
    btn.disabled = false;
    btn.innerHTML = I18N.t('runtimes.action.refresh_skills');
    app.showToast(I18N.t('runtimes.toast.scan_timeout'));
    return;
  }
  const url = '/runtime/runtimes/' + runtimeId + '/refresh-skills' + (isFirst ? '?force=true' : '');
  API.post(url).then(d => {
    if (d.status === 'pending') {
      // 远端 worker 尚未回报，继续轮询
      setTimeout(() => _pollSkillScan(runtimeId, btn, attempt + 1, false), 2000);
      return;
    }
    btn.disabled = false;
    btn.innerHTML = I18N.t('runtimes.action.refresh_skills');
    const listEl = document.getElementById('engineSkillList');
    if (!listEl) return;
    const skills = d.skills || [];
    _skillScanDirty = true;  // 标记：关闭弹窗时刷新引擎列表卡片
    if (!skills.length) {
      listEl.innerHTML = '<div style="color:var(--t3);font-size:12px;padding:8px">' + I18N.t('runtimes.empty.no_local_skill') + '</div>';
    } else {
      listEl.innerHTML = skills.map(s => `
        <div style="padding:8px 12px;border:1px solid var(--b);border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='var(--bg)'" onmouseout="this.style.background=''" onclick="openSkillDetail('${s.name}', ${runtimeId}, ${JSON.stringify(s).replace(/'/g, '&#39;').replace(/"/g, '&quot;')})">
          <div style="font-weight:600;font-size:13px;color:var(--t1)">${s.name}</div>
          <div style="font-size:11px;color:var(--t3);margin-top:2px">${s.description || I18N.t('runtimes.skill.no_desc')}</div>
        </div>`).join('');
    }
    const titleEl = document.querySelector('#engineDetailOverlay span[style*="font-weight:400"]');
    if (titleEl) titleEl.textContent = I18N.t('runtimes.label.skill_count_paren', {count: skills.length});
    app.showToast(I18N.t('runtimes.toast.scan_complete', {count: skills.length}));
  }).catch(e => {
    btn.disabled = false;
    btn.innerHTML = I18N.t('runtimes.action.refresh_skills');
    app.showToast(I18N.t('runtimes.toast.refresh_failed', {msg: e.message}));
  });
}

function openSkillDetail(skillName, runtimeId, cachedDetail) {
  // 如果有缓存的详情数据（从刷新接口返回），直接展示
  let detail = cachedDetail || null;
  if (detail && typeof detail === 'string') {
    try { detail = JSON.parse(detail.replace(/&quot;/g, '"').replace(/&#39;/g, "'")); } catch (e) { detail = null; }
  }

  const html = `<div id="skillDetailOverlay" style="position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center" onclick="closeSkillDetail(event)">
    <div style="background:var(--c);border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.25);max-width:520px;width:90%;max-height:80vh;overflow:auto;padding:24px" onclick="event.stopPropagation()">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
        <span style="font-weight:700;font-size:16px">${skillName}</span>
        <button class="bt bt-o" style="margin-left:auto" onclick="closeSkillDetail()">${I18N.t('runtimes.action.close')}</button>
      </div>
      <div style="display:grid;gap:8px;font-size:13px;color:var(--t2)">
        ${detail ? `
          <div><span style="color:var(--t3);display:inline-block;min-width:80px">${I18N.t('runtimes.label.description')}</span> ${detail.description || '—'}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:80px">${I18N.t('runtimes.label.provider')}</span> ${detail.provider || '—'}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:80px">${I18N.t('runtimes.label.local_path')}</span> <code style="font-size:11px;background:var(--bg);padding:2px 6px;border-radius:4px;word-break:break-all">${detail.path || '—'}</code></div>
          <div><span style="color:var(--t3);display:inline-block;min-width:80px">${I18N.t('runtimes.label.file_count')}</span> ${detail.file_count || 0}</div>
        ` : '<div style="color:var(--t3)">' + I18N.t('runtimes.skill.detail_unavailable') + '</div>'}
      </div>
    </div>
  </div>`;
  document.body.insertAdjacentHTML('beforeend', html);
}

function closeSkillDetail(event) {
  if (event && event.target && event.target.id !== 'skillDetailOverlay') return;
  const el = document.getElementById('skillDetailOverlay');
  if (el) el.remove();
}

// ===== Engine 角色标签 =====
const _RT_TAG_COLORS = {all: '#6366f1', vod: '#ec4899', issue: '#ef4444', coding: '#3b82f6', qa: '#a855f7', sre: '#f59e0b'};
function _rtTagLabel(t) { return t === 'all' ? I18N.t('runtimes.tag.all') : ({vod:'VOD',issue:'Issue',coding:'Coding',qa:'QA',sre:'SRE'})[t] || t; }
const _RT_TAG_OPTIONS = ['all', 'vod', 'issue', 'coding', 'qa', 'sre'];

function _engineTagCheckboxes(tags) {
  return _RT_TAG_OPTIONS.map(t => {
    const checked = tags.includes(t) ? 'checked' : '';
    const desc = t === 'all' ? I18N.t('runtimes.tag.all_desc') : I18N.t('runtimes.tag.single_desc', {label: _rtTagLabel(t)});
    return `<label style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;padding:2px 6px">
      <input type="checkbox" id="engineTagCb_${t}" value="${t}" ${checked} onchange="onEngineTagChange('${t}')"
             style="width:14px;height:14px;accent-color:${_RT_TAG_COLORS[t]}">
      <span style="font-size:10px;padding:2px 8px;border-radius:999px;color:#fff;background:${_RT_TAG_COLORS[t]}">${_rtTagLabel(t)}</span>
      <span style="color:var(--t3);font-size:11px">${desc}</span>
    </label>`;
  }).join('');
}

function _engineTagSummary(tags) {
  if (!tags || tags.length === 0) return '<span style="color:var(--t3);font-size:10px">' + I18N.t('runtimes.empty.unassigned') + '</span>';
  return tags.map(t => `<span style="font-size:10px;padding:1px 6px;border-radius:999px;color:#fff;background:${_RT_TAG_COLORS[t]||'#999'};margin:0 2px">${_rtTagLabel(t)||t}</span>`).join('');
}

function onEngineTagChange(tag) {
  const allCb = document.getElementById('engineTagCb_all');
  const agentCbs = ['vod','issue','coding','qa','sre'].map(t => document.getElementById('engineTagCb_'+t));
  const checkedAgents = agentCbs.filter(cb => cb.checked);

  if (tag === 'all') {
    if (allCb.checked) {
      agentCbs.forEach(cb => cb.checked = false);
    }
  } else {
    if (allCb.checked) {
      allCb.checked = false;
    }
    if (checkedAgents.length === 5) {
      agentCbs.forEach(cb => cb.checked = false);
      allCb.checked = true;
    }
  }
}

function saveEngineTags(runtimeId) {
  const tags = [];
  _RT_TAG_OPTIONS.forEach(t => {
    const cb = document.getElementById('engineTagCb_'+t);
    if (cb && cb.checked) tags.push(t);
  });
  API.put('/runtime/runtimes/' + runtimeId + '/tags', {tags}).then(d => {
    const rt = (__runtimeData.runtimes || []).find(r => r.id === runtimeId);
    if (rt) rt.agent_tags = d.tags;
    // 同步更新 devices 中的 runtimes
    if (__runtimeDevices) {
      for (const dev of __runtimeDevices) {
        const r = (dev.runtimes || []).find(r => r.id === runtimeId);
        if (r) r.agent_tags = d.tags;
      }
    }
    app.showToast(I18N.t('runtimes.toast.tags_saved'));
    // 重新渲染列表页（立即生效，无需手动刷新）
    if (__runtimeData) renderRuntimes(__runtimeData);
  }).catch(e => app.showToast(I18N.t('runtimes.toast.save_failed', {msg: e.message})));
}

// ====== AI Devspace ======

function _devStatusLabel(s) {
  var map = {
    'cde.0000': { color: '#6b7280', bg: '#f3f4f6' },
    'cde.0001': { color: '#2563eb', bg: '#dbeafe' },
    'cde.0002': { color: '#16a34a', bg: '#dcfce7' },
    'cde.0003': { color: '#2563eb', bg: '#dbeafe' },
    'cde.0004': { color: '#16a34a', bg: '#dcfce7' },
    'cde.0005': { color: '#d97706', bg: '#fef3c7' },
    'cde.0006': { color: '#dc2626', bg: '#fee2e2' },
    'cde.0007': { color: '#d97706', bg: '#fef3c7' },
    'cde.0008': { color: '#d97706', bg: '#fef3c7' },
    'cde.0009': { color: '#9ca3af', bg: '#f9fafb' },
    'cde.1001': { color: '#dc2626', bg: '#fee2e2' },
    'cde.1002': { color: '#dc2626', bg: '#fee2e2' },
    'cde.1003': { color: '#dc2626', bg: '#fee2e2' },
    'cde.1004': { color: '#dc2626', bg: '#fee2e2' },
  };
  var m = map[s] || { color: '#6b7280', bg: '#f3f4f6' };
  var label = I18N.t('runtimes.devspace.status.' + (s || 'unknown').replace(/\./g, '_'));
  return '<span style="font-size:11px;font-weight:600;color:' + m.color + ';background:' + m.bg + ';padding:2px 8px;border-radius:999px">' + label + '</span>';
}

function _specHtml(e) {
  var cs = e.compute_spec || {};
  var acc = cs.accelerator || {};
  var parts = [(cs.cpu_cores || '?') + 'vCPU', cs.ram || '?', cs.cpu_arch || ''];
  if (acc.accel_type) parts.push(acc.accel_type + ' ' + acc.accel_model + 'x' + acc.card_count);
  return parts.filter(Boolean).join(' . ');
}

function openAiDevspace() {
  var el = document.getElementById('pageContent');
  if (!el) return;
  var existing = document.getElementById('aiDevspacePanel');
  if (existing) { existing.remove(); return; }
  // 滑入动画
  el.insertAdjacentHTML('afterbegin',
    '<div id="aiDevspacePanel" style="margin-bottom:16px;max-height:0;overflow:hidden;transition:max-height .3s ease,opacity .3s ease;opacity:0">' +
    '<div class="cd" style="padding:0;overflow:hidden">' +
    '<div style="padding:14px 20px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--b);background:linear-gradient(135deg,#60a5fa,#a78bfa);position:relative;overflow:hidden">' +
    '<div style="position:absolute;inset:0;background:radial-gradient(ellipse at top right,rgba(255,255,255,.2),transparent 60%);pointer-events:none"></div>' +
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="position:relative;flex-shrink:0"><path d="M17.5 19a4.5 4.5 0 1 0-1.41-8.78 7 7 0 1 0-13.09 3.78A4.5 4.5 0 0 0 6.5 19z"/></svg>' +
    '<h3 style="font-size:14px;font-weight:600;margin:0;flex:1;color:#fff;position:relative">' + I18N.t('runtimes.devspace.title') + '</h3>' +
    '<span id="aiDevspaceAutoTag" style="font-size:10px;color:rgba(255,255,255,.7);position:relative;display:none"></span>' +
    '<button class="bt bt-s" style="background:rgba(255,255,255,.2);color:#fff;border:1px solid rgba(255,255,255,.25);position:relative" onclick="_showCreateEnvForm()">' + I18N.t('runtimes.devspace.create') + '</button>' +
    '<button class="bt bt-s" style="background:rgba(255,255,255,.12);color:#fff;border:1px solid rgba(255,255,255,.15);position:relative" onclick="_refreshDevspaceList()">&#8635;</button>' +
    '<button class="bt bt-s" style="background:rgba(255,255,255,.12);color:#fff;border:1px solid rgba(255,255,255,.15);position:relative" onclick="_closeDevspace()">&#10005;</button></div>' +
    '<div id="aiDevspaceBody"><div class="loading">' + I18N.t('runtimes.devspace.loading') + '</div></div></div></div>');
  // 触发动画
  setTimeout(function() {
    var p = document.getElementById('aiDevspacePanel');
    if (p) { p.style.maxHeight = '600px'; p.style.opacity = '1'; }
  }, 10);
  _refreshDevspaceList();
  _startDevspaceAutoRefresh();
}

function _closeDevspace() {
  var el = document.getElementById('aiDevspacePanel');
  if (!el) return;
  _stopDevspaceAutoRefresh();
  el.style.maxHeight = '0';
  el.style.opacity = '0';
  setTimeout(function() { if (el) el.remove(); }, 300);
}

// ====== AI Devspace 自动刷新 ======
var _devspaceAutoTimer = null;
var _devspaceAutoCountdown = 0;
var _devspaceCountdownTimer = null;
var _DEVSPACE_AUTO_INTERVAL = 15; // 秒

function _startDevspaceAutoRefresh() {
  _stopDevspaceAutoRefresh();
  _devspaceAutoCountdown = _DEVSPACE_AUTO_INTERVAL;
  _devspaceAutoTimer = setInterval(function() {
    var panel = document.getElementById('aiDevspacePanel');
    if (!panel) { _stopDevspaceAutoRefresh(); return; }
    _devspaceSilentRefresh();
    _devspaceAutoCountdown = _DEVSPACE_AUTO_INTERVAL;
  }, _DEVSPACE_AUTO_INTERVAL * 1000);
  // 倒计时显示
  _devspaceCountdownTimer = setInterval(function() {
    var panel = document.getElementById('aiDevspacePanel');
    if (!panel) { _stopDevspaceAutoRefresh(); return; }
    var tag = document.getElementById('aiDevspaceAutoTag');
    if (!tag) return;
    _devspaceAutoCountdown--;
    if (_devspaceAutoCountdown <= 0) _devspaceAutoCountdown = _DEVSPACE_AUTO_INTERVAL;
    tag.textContent = _DEVSPACE_AUTO_INTERVAL + 's auto';
    tag.style.display = '';
  }, 1000);
}

function _stopDevspaceAutoRefresh() {
  if (_devspaceAutoTimer) { clearInterval(_devspaceAutoTimer); _devspaceAutoTimer = null; }
  if (_devspaceCountdownTimer) { clearInterval(_devspaceCountdownTimer); _devspaceCountdownTimer = null; }
  var tag = document.getElementById('aiDevspaceAutoTag');
  if (tag) { tag.style.display = 'none'; tag.textContent = ''; }
}

function _devspaceSilentRefresh() {
  var body = document.getElementById('aiDevspaceBody');
  if (!body) return;
  // 静默刷新：不显示 loading spinner，只在后台拉数据后更新列表
  API.get('/devstage/environments').then(function(d) {
    var envs = d.environments || [];
    if (envs.length === 0) {
      body.innerHTML = '<div style="padding:36px 20px;text-align:center">' +
        '<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:10px;opacity:.6"><path d="M17.5 19a4.5 4.5 0 1 0-1.41-8.78 7 7 0 1 0-13.09 3.78A4.5 4.5 0 0 0 6.5 19z"/></svg>' +
        '<div style="font-size:13px;color:var(--t2);margin-bottom:4px">' + I18N.t('runtimes.devspace.empty') + '</div>' +
        '<div style="font-size:11px;color:var(--t3)">' + I18N.t('runtimes.devspace.empty_hint') + '</div></div>';
      return;
    }
    var html = '';
    for (var i = 0; i < envs.length; i++) {
      var e = envs[i];
      var id = e.id || '';
      var st = e.status || '';
      var stMap = { 'cde.0000':'#6b7280','cde.0001':'#2563eb','cde.0002':'#16a34a','cde.0003':'#2563eb','cde.0004':'#16a34a','cde.0005':'#d97706','cde.0006':'#dc2626','cde.0007':'#d97706','cde.0008':'#d97706','cde.0009':'#9ca3af','cde.1001':'#dc2626','cde.1002':'#dc2626','cde.1003':'#dc2626','cde.1004':'#dc2626' };
      var dotColor = stMap[st] || '#8d97a8';
      var isRunning = st === 'cde.0002';
      var isReady = st === 'cde.0004';
      var isStopped = st === 'cde.0000';
      var isDeleted = st === 'cde.0009';
      var isError = ['cde.1001','cde.1002','cde.1003','cde.1004'].indexOf(st) >= 0;
      var isBusy = ['cde.0001','cde.0003','cde.0005','cde.0006','cde.0007','cde.0008'].indexOf(st) >= 0;
      html += '<div style="display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--b);transition:background .12s" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">' +
        '<div style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:' + dotColor + ';box-shadow:0 0 6px ' + dotColor + '50"></div>' +
        '<div style="flex:1;min-width:0">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">' +
        '<span style="font-size:13px;font-weight:600">' + (e.name || id) + '</span>' +
        _devStatusLabel(st) + '</div>' +
        '<div style="font-size:11px;color:var(--t3)">' + _specHtml(e) + ' · ' + (e.type || '') + ' · ' + ((e.image || {}).os || '') + '</div></div>' +
        '<div style="display:flex;gap:4px;flex-shrink:0">' +
        ((isStopped || isReady) && !isBusy ? '<button class="bt bt-p bt-s" onclick="_devAction(\'start\',\'' + id + '\')">' + I18N.t('runtimes.devspace.start') + '</button>' : '') +
        (isRunning ? '<button class="bt bt-o bt-s" onclick="_devAction(\'stop\',\'' + id + '\')">' + I18N.t('runtimes.devspace.stop') + '</button>' : '') +
        (!(isBusy || isDeleted) ? '<button class="bt bt-r bt-s" onclick="_devAction(\'delete\',\'' + id + '\')">' + I18N.t('runtimes.devspace.delete') + '</button>' : '') +
        '</div></div>';
    }
    body.innerHTML = html;
  }).catch(function() {
    // 静默失败：不打扰用户，下次定时刷新会重试
  });
}

function _refreshDevspaceList() {
  var body = document.getElementById('aiDevspaceBody');
  if (!body) return;
  body.innerHTML = '<div style="padding:32px;text-align:center;color:#a78bfa;font-size:13px"><div style="display:inline-block;width:20px;height:20px;border:2px solid rgba(167,139,250,.2);border-top-color:#a78bfa;border-radius:50%;animation:spin .8s linear infinite;margin-bottom:10px"></div><div>' + I18N.t('runtimes.devspace.loading') + '</div><style>@keyframes spin{to{transform:rotate(360deg)}}</style></div>';
  API.get('/devstage/environments').then(function(d) {
    var envs = d.environments || [];
    if (envs.length === 0) {
      body.innerHTML = '<div style="padding:36px 20px;text-align:center">' +
        '<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#a78bfa" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:10px;opacity:.6"><path d="M17.5 19a4.5 4.5 0 1 0-1.41-8.78 7 7 0 1 0-13.09 3.78A4.5 4.5 0 0 0 6.5 19z"/></svg>' +
        '<div style="font-size:13px;color:var(--t2);margin-bottom:4px">' + I18N.t('runtimes.devspace.empty') + '</div>' +
        '<div style="font-size:11px;color:var(--t3)">' + I18N.t('runtimes.devspace.empty_hint') + '</div></div>';
      return;
    }
    var html = '';
    for (var i = 0; i < envs.length; i++) {
      var e = envs[i];
      var id = e.id || '';
      var st = e.status || '';
      var stMap = { 'cde.0000':'#6b7280','cde.0001':'#2563eb','cde.0002':'#16a34a','cde.0003':'#2563eb','cde.0004':'#16a34a','cde.0005':'#d97706','cde.0006':'#dc2626','cde.0007':'#d97706','cde.0008':'#d97706','cde.0009':'#9ca3af','cde.1001':'#dc2626','cde.1002':'#dc2626','cde.1003':'#dc2626','cde.1004':'#dc2626' };
      var dotColor = stMap[st] || '#8d97a8';
      var isRunning = st === 'cde.0002';
      var isReady = st === 'cde.0004';
      var isStopped = st === 'cde.0000';
      var isDeleted = st === 'cde.0009';
      var isError = ['cde.1001','cde.1002','cde.1003','cde.1004'].indexOf(st) >= 0;
      var isBusy = ['cde.0001','cde.0003','cde.0005','cde.0006','cde.0007','cde.0008'].indexOf(st) >= 0;
      html += '<div style="display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--b);transition:background .12s" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">' +
        '<div style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:' + dotColor + ';box-shadow:0 0 6px ' + dotColor + '50"></div>' +
        '<div style="flex:1;min-width:0">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">' +
        '<span style="font-size:13px;font-weight:600">' + (e.name || id) + '</span>' +
        _devStatusLabel(st) + '</div>' +
        '<div style="font-size:11px;color:var(--t3)">' + _specHtml(e) + ' · ' + (e.type || '') + ' · ' + ((e.image || {}).os || '') + '</div></div>' +
        '<div style="display:flex;gap:4px;flex-shrink:0">' +
        ((isStopped || isReady) && !isBusy ? '<button class="bt bt-p bt-s" onclick="_devAction(\'start\',\'' + id + '\')">' + I18N.t('runtimes.devspace.start') + '</button>' : '') +
        (isRunning ? '<button class="bt bt-o bt-s" onclick="_devAction(\'stop\',\'' + id + '\')">' + I18N.t('runtimes.devspace.stop') + '</button>' : '') +
        (!(isBusy || isDeleted) ? '<button class="bt bt-r bt-s" onclick="_devAction(\'delete\',\'' + id + '\')">' + I18N.t('runtimes.devspace.delete') + '</button>' : '') +
        '</div></div>';
    }
    body.innerHTML = html;
  }).catch(function(e) {
    body.innerHTML = '<div style="padding:32px 20px;text-align:center">' +
      '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:10px;opacity:.5"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>' +
      '<div style="font-size:13px;color:var(--t2);margin-bottom:4px">' +
      (e.message.indexOf('Unauthorized') >= 0 || e.message.indexOf('APIGW.0301') >= 0 ? I18N.t('runtimes.devspace.configure_aksk') : I18N.t('runtimes.devspace.load_failed')) + '</div>' +
      '<div style="font-size:11px;color:var(--t3)">' +
      (e.message.indexOf('Unauthorized') >= 0 || e.message.indexOf('APIGW.0301') >= 0 ? I18N.t('runtimes.devspace.configure_aksk_hint') : e.message) + '</div></div>';
  });
}

function _showCreateEnvForm() {
  var repo = app.currentRepo();
  var repoName = (repo && repo.name) ? repo.name : 'grape';
  var now = new Date();
  var ts = String(now.getMonth() + 1).padStart(2, '0') +
    String(now.getDate()).padStart(2, '0') +
    String(now.getHours()).padStart(2, '0') +
    String(now.getMinutes()).padStart(2, '0');
  var maxRepo = 15 - 1 - ts.length;
  if (maxRepo < 1) maxRepo = 1;
  repoName = repoName.replace(/[^a-zA-Z0-9_]/g, '').slice(0, maxRepo);
  var envName = repoName + '_' + ts;

  var panel = document.getElementById('aiDevspacePanel');
  if (panel) panel.innerHTML = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">' +
    '<h3 style="font-size:14px;flex:1;margin:0">AI Devspace</h3>' +
    '<button class="bt bt-o" onclick="_closeDevspace()">X</button></div>' +
    '<div class="cd" style="padding:20px;text-align:center"><div class="loading">' + I18N.t('runtimes.devspace.creating') + '</div></div>';
  _doCreateEnv(envName);
}

function _genTokenForCreate() {
  var repo = app.currentRepo();
  var repoKey = repo ? repo.fullName : '';
  var name = 'auto_' + Date.now().toString(36).slice(-6);
  API.post('/settings/runtime-credentials', { name: name, scope: 'repo', repo_key: repoKey }).then(function(d) {
    var input = document.getElementById('grapeToken');
    if (input) input.value = d.token || '';
    app.showToast(I18N.t('runtimes.credential.toast_generated'));
  }).catch(function(e) {
    app.showToast(I18N.t('runtimes.credential.toast_gen_failed', {msg: e.message}));
  });
}

var _cachedTemplates = [];
var _cachedFlavors = [];

function _loadTemplates() {
  API.get('/devstage/templates').then(function(d) {
    var list = d.templates || [];
    _cachedTemplates = list;
    var sel = document.getElementById('envTemplate');
    if (!sel) return;
    sel.innerHTML = '<option value="">' + I18N.t('runtimes.devspace.select_template') + '</option>';
    for (var i = 0; i < list.length; i++) {
      sel.innerHTML += '<option value="' + list[i].uuid + '">' + list[i].name + '</option>';
    }
    sel.onchange = _onTemplateChange;
  });
}

function _onTemplateChange() {
  var tplId = document.getElementById('envTemplate').value;
  var flavorSel = document.getElementById('envFlavor');
  if (!flavorSel) return;
  flavorSel.innerHTML = '<option value="">' + I18N.t('runtimes.devspace.loading') + '</option>';
  if (!tplId) { flavorSel.innerHTML = '<option value="">' + I18N.t('runtimes.devspace.select_template_first') + '</option>'; return; }
  API.get('/devstage/specs?template_id=' + encodeURIComponent(tplId)).then(function(d) {
    var list = d.specs || [];
    _cachedFlavors = list;
    flavorSel.innerHTML = '<option value="">' + I18N.t('runtimes.devspace.select_flavor') + '</option>';
    for (var i = 0; i < list.length; i++) {
      var s = list[i];
      flavorSel.innerHTML += '<option value="' + s.flavor_id + '">' + s.env_cpu_size + 'c' + s.env_mem_size + 'g' + (s.env_npu_type ? ' NPU' : '') + '</option>';
    }
    // Auto-select if only one flavor
    if (list.length === 1) {
      flavorSel.value = list[0].flavor_id;
      _onFlavorChange();
    }
  }).catch(function() {
    var flavorSel = document.getElementById('envFlavor');
    if (flavorSel) flavorSel.innerHTML = '<option value="">' + I18N.t('runtimes.devspace.flavor_load_failed') + '</option>';
  });
}

function _onFlavorChange() {
  var sel = document.getElementById('envFlavor');
  if (!sel || !sel.value) return;
  var flavorId = sel.value;
  var flavor = null;
  for (var i = 0; i < _cachedFlavors.length; i++) {
    if (_cachedFlavors[i].flavor_id === flavorId) { flavor = _cachedFlavors[i]; break; }
  }
  if (!flavor) return;
  var coresEl = document.getElementById('envCores');
  var memEl = document.getElementById('envMemory');
  if (coresEl) coresEl.value = flavor.env_cpu_size || flavor.cores || '';
  if (memEl) memEl.value = flavor.env_mem_size || flavor.memory || '';
}

function _showCreateEnvForm() {
    var repo = app.currentRepo();
    var repoName = (repo && repo.name) ? repo.name : 'grape';
    var now = new Date();
    var ts = String(now.getMonth() + 1).padStart(2, '0') +
      String(now.getDate()).padStart(2, '0') +
      String(now.getHours()).padStart(2, '0') +
      String(now.getMinutes()).padStart(2, '0');
    var maxRepo = 15 - 1 - ts.length;
    if (maxRepo < 1) maxRepo = 1;
    repoName = repoName.replace(/[^a-zA-Z0-9_]/g, '').slice(0, maxRepo);
    var baseName = repoName + '_' + ts;

    var panel = document.getElementById('aiDevspacePanel');
    if (panel) {
      var body = panel.querySelector('#aiDevspaceBody') || panel;
      body.innerHTML = '<div style="padding:32px;text-align:center;color:#a78bfa;font-size:13px">' +
        '<div style="display:inline-block;width:20px;height:20px;border:2px solid rgba(167,139,250,.2);border-top-color:#a78bfa;border-radius:50%;animation:spin .8s linear infinite;margin-bottom:10px"></div>' +
        '<div>' + I18N.t('runtimes.devspace.creating') + '</div><style>@keyframes spin{to{transform:rotate(360deg)}}</style></div>';
    }
    _doCreateEnvWithRetry(baseName, 0);
  }

  function _doCreateEnvWithRetry(baseName, attempt) {
    var envName = attempt === 0 ? baseName : baseName + '_' + attempt;
    if (envName.length > 15) {
      // 截断仓库名部分，保留完整时间戳
      var parts = baseName.split('_');
      var repoPart = parts[0];
      var tsPart = parts.slice(1).join('_');
      var suffix = '_' + attempt;
      var maxRepo = 15 - tsPart.length - 1 - suffix.length;
      if (maxRepo < 1) maxRepo = 1;
      repoPart = repoPart.slice(0, maxRepo);
      envName = repoPart + '_' + tsPart + suffix;
    }
    var repo = app.currentRepo();
    var repoKey = repo ? repo.fullName : '';
    var grapeServer = window.location.origin;
    var credName = envName.slice(0, 20);

    // Step 1: 生成凭据
    API.post('/settings/runtime-credentials', { name: credName, scope: 'repo', repo_key: repoKey }).then(function(d) {
      var grapeToken = d.token || '';
      var credId = d.id || 0;
      if (!grapeToken) { app.showToast(I18N.t('runtimes.devspace.cred_gen_failed')); _refreshDevspaceList(); return; }

      // Step 2: 创建环境（带凭据注入）
      API.post('/devstage/environments', {
        name: envName,
        grape_server: grapeServer,
        grape_token: grapeToken,
      }).then(function(d2) {
        app.showToast(I18N.t('runtimes.devspace.create_success'));
        _refreshDevspaceList();
      }).catch(function(e) {
        var msg = e.message || '';
        // 环境名冲突：删除凭据，换名重试
        if (attempt < 10 && (msg.indexOf('98320005') >= 0 || msg.indexOf('already been used') >= 0)) {
          API.post('/settings/runtime-credentials/' + credId + '/revoke').then(function() {
            _doCreateEnvWithRetry(baseName, attempt + 1);
          }).catch(function() {
            _doCreateEnvWithRetry(baseName, attempt + 1);
          });
        } else if (msg.indexOf('98320022') >= 0 || msg.indexOf('count limit') >= 0) {
          API.post('/settings/runtime-credentials/' + credId + '/revoke');
          app.showToast(I18N.t('runtimes.devspace.count_limit'));
          _refreshDevspaceList();
        } else if (msg.indexOf('98320049') >= 0 || msg.indexOf('Template not published') >= 0) {
          // 模板未发布/未加白名单：删除凭据，提示用户联系管理员
          API.post('/settings/runtime-credentials/' + credId + '/revoke');
          app.showToast(I18N.t('runtimes.devspace.template_not_published'));
          _refreshDevspaceList();
        } else {
          // 其他失败也删除凭据，不残留无效 worker
          API.post('/settings/runtime-credentials/' + credId + '/revoke');
          app.showToast(I18N.t('runtimes.devspace.create_failed_msg', {msg: msg}));
          _refreshDevspaceList();
        }
      });
    }).catch(function(e) {
      var msg = e.message || '';
      if (attempt < 10 && (msg.indexOf('exist') >= 0 || msg.indexOf('exist') >= 0)) {
        _doCreateEnvWithRetry(baseName, attempt + 1);
      } else {
        app.showToast(I18N.t('runtimes.devspace.cred_gen_failed_msg', {msg: msg}));
        _refreshDevspaceList();
      }
    });
  }

function _devAction(action, devstageId) {
  if (action === 'delete' && !confirm(I18N.t('runtimes.devspace.confirm_delete'))) return;
  var body = document.getElementById('aiDevspaceBody');
  if (body) body.innerHTML = '<div style="padding:32px;text-align:center;color:#a78bfa;font-size:13px"><div style="display:inline-block;width:20px;height:20px;border:2px solid rgba(167,139,250,.2);border-top-color:#a78bfa;border-radius:50%;animation:spin .8s linear infinite;margin-bottom:10px"></div><div>' + I18N.t('runtimes.devspace.operating') + '</div><style>@keyframes spin{to{transform:rotate(360deg)}}</style></div>';
  if (action === 'delete') {
    API.del('/devstage/environments/' + devstageId).then(function() {
      app.showToast(I18N.t('runtimes.devspace.deleting'));
      setTimeout(_refreshDevspaceList, 1000);
    }).catch(function(e) {
      var msg = e.message || '';
      if (msg.indexOf('98320063') >= 0 || msg.indexOf('can not be deleted') >= 0) {
        app.showToast(I18N.t('runtimes.devspace.cannot_delete_running'));
      } else {
        app.showToast(I18N.t('runtimes.devspace.delete_failed', {msg: msg}));
      }
      _refreshDevspaceList();
    });
  } else {
    API.post('/devstage/environments/' + action, { devstage_id: devstageId }).then(function() {
      app.showToast(action === 'start' ? I18N.t('runtimes.devspace.starting') : I18N.t('runtimes.devspace.stopping'));
      setTimeout(_refreshDevspaceList, 1000);
    }).catch(function(e) {
      app.showToast(I18N.t('runtimes.devspace.op_failed', {msg: e.message}));
      _refreshDevspaceList();
    });
  }
}

function _genCredForEnv(envId, envName) {
  var defaultName = 'worker_' + envName.replace(/[^a-zA-Z0-9_]/g, '_').slice(0, 15);
  var html = '<div class="modal-overlay" id="credModal_' + envId + '" style="position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.4);display:flex;align-items:center;justify-content:center" onclick="if(event.target===this)_closeEnvCredPanel(\'' + envId + '\')">' +
    '<div class="cd" style="max-width:520px;width:90%;padding:20px;box-shadow:0 4px 24px rgba(0,0,0,0.15)">' +
    '<h3 style="font-size:15px;font-weight:600;margin-bottom:14px">' + I18N.t('runtimes.devspace.gen_cred_for_env', {name: envName}) + '</h3>' +
    '<div class="srow"><label>' + I18N.t('runtimes.devspace.cred_name') + '</label><input id="credName_' + envId + '" value="' + defaultName + '" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px"></div>' +
    '<div class="srow"><label>' + I18N.t('runtimes.devspace.scope') + '</label><select id="credScope_' + envId + '" onchange="_toggleCredRepoKey(\'' + envId + '\')" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px">' +
    '<option value="user">' + I18N.t('runtimes.scope.user') + '</option><option value="repo">' + I18N.t('runtimes.scope.repo') + '</option><option value="global">' + I18N.t('runtimes.scope.global') + '</option></select></div>' +
    '<div class="srow" id="credRepoRow_' + envId + '" style="display:none"><label>' + I18N.t('runtimes.devspace.repo') + '</label><input id="credRepoKey_' + envId + '" placeholder="owner/repo" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px"></div>' +
    '<div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">' +
    '<button class="bt bt-p" onclick="_doGenCredForEnv(\'' + envId + '\',\'' + envName + '\')">' + I18N.t('runtimes.action.generate') + '</button>' +
    '<button class="bt bt-o" onclick="_closeEnvCredPanel(\'' + envId + '\')">' + I18N.t('runtimes.action.cancel') + '</button></div></div></div>';
  document.body.insertAdjacentHTML('beforeend', html);
}

function _toggleCredRepoKey(envId) {
  var sel = document.getElementById('credScope_' + envId);
  var row = document.getElementById('credRepoRow_' + envId);
  if (row && sel) row.style.display = sel.value === 'repo' ? '' : 'none';
}

function _doGenCredForEnv(envId, envName) {
  var nameInput = document.getElementById('credName_' + envId);
  var scopeInput = document.getElementById('credScope_' + envId);
  var repoInput = document.getElementById('credRepoKey_' + envId);
  var credName = (nameInput ? nameInput.value : '').trim();
  var scope = scopeInput ? scopeInput.value : 'user';
  var repoKey = (scope === 'repo' && repoInput) ? repoInput.value.trim() : '';
  if (!credName) { app.showToast(I18N.t('runtimes.devspace.enter_cred_name')); return; }
  if (scope === 'repo' && !repoKey) { app.showToast(I18N.t('runtimes.devspace.repo_required')); return; }

  var modalId = 'credModal_' + envId;
  var box = document.getElementById(modalId);
  if (box) box.innerHTML = '<div class="cd" style="max-width:520px;width:90%;padding:20px;box-shadow:0 4px 24px rgba(0,0,0,0.15);margin:auto"><div class="loading">' + I18N.t('runtimes.devspace.gen_cred_loading') + '</div></div>';

  API.post('/settings/runtime-credentials', { name: credName, scope: scope, repo_key: repoKey }).then(function(d) {
    var token = d.token || '';
    var serverUrl = window.location.origin;
    var oneCmd = 'curl -fsSL ' + serverUrl + '/api/runtime/install | bash -s -- --token ' + token + ' --server ' + serverUrl;
    var box = document.getElementById(modalId);
    if (!box) return;
    var cmdEscaped = oneCmd.replace(/'/g, "\\'");
    box.innerHTML = '<div class="cd" style="max-width:520px;width:90%;padding:20px;box-shadow:0 4px 24px rgba(0,0,0,0.15);margin:auto">' +
      '<h3 style="font-size:15px;font-weight:600;margin-bottom:14px">' + I18N.t('runtimes.devspace.cred_created_for', {name: envName}) + '</h3>' +
      '<div style="font-size:12px;margin-bottom:8px;color:var(--r)">' + I18N.t('runtimes.devspace.token_warning') + '</div>' +
      '<code style="display:block;padding:10px;background:var(--bg);border-radius:6px;word-break:break-all;font-size:12px;margin-bottom:10px">' + oneCmd + '</code>' +
      '<button class="bt bt-p" onclick="copyText(\'' + cmdEscaped + '\')">' + I18N.t('runtimes.devspace.copy_cmd') + '</button>' +
      '<button class="bt bt-g" style="margin-left:6px" onclick="' + "_execCredCmd('" + cmdEscaped + "','" + envId + "')" + '">' + I18N.t('runtimes.devspace.execute') + '</button>' +
      '<p style="font-size:11px;color:var(--t3);margin-top:8px">' + I18N.t('runtimes.devspace.exec_hint') + '</p>' +
      '<button class="bt bt-o" style="margin-top:8px" onclick="' + "_closeEnvCredPanel('" + envId + "')" + '">' + I18N.t('runtimes.action.close') + '</button></div>';
  }).catch(function(e) {
    var box = document.getElementById(modalId);
    if (box) box.innerHTML = '<div class="error" style="padding:20px">' + I18N.t('runtimes.devspace.load_failed') + ': ' + e.message + '</div>';
  });
}

function _closeEnvCredPanel(envId) {
  var el = document.getElementById('credModal_' + envId);
  if (el) el.remove();
}

function _execCredCmd(cmd, envId) {
  copyText(cmd);
  app.showToast(I18N.t('runtimes.devspace.cmd_copied'));
  // 展开一个引导步骤提示
  var box = document.getElementById('credModal_' + envId);
  if (box) {
    var card = box.querySelector('.cd');
    if (card) {
      card.innerHTML = card.innerHTML + 
        '<div style="margin-top:10px;padding:12px;background:#f0fdf4;border-radius:6px;font-size:12px;border:1px solid #bbf7d0">' +
        '<div style="font-weight:600;color:#16a34a;margin-bottom:6px">' + I18N.t('runtimes.devspace.cmd_in_clipboard') + '</div>' +
        '<p style="color:#166534;line-height:1.6">1. <a href="https://devstation.myhuaweicloud.com" target="_blank" style="color:#2563eb">' + I18N.t('runtimes.devspace.open_devstation') + '</a><br>' +
        '2. ' + I18N.t('runtimes.devspace.find_env_connect') + '<br>' +
        '3. ' + I18N.t('runtimes.devspace.paste_in_terminal') + '</p></div>';
    }
  }
}
