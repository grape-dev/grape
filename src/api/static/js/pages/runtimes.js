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
let __runtimeDevices = null;
let _skillScanDirty = false;  // skills 扫描成功后标记，关闭弹窗时刷新引擎列表

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
    // 本机直连引擎（credential_id=0）恒排分组最前，与远端 worker 引擎区分（engine-selection-design.md §4.3）
    list.sort((a, b) => (b.is_local ? 1 : 0) - (a.is_local ? 1 : 0));
    const localN = list.filter(r => r.is_local).length;
    html += _scopeBar(sc, list.length, localN);
    html += `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px">`;
    list.forEach(rt => {
      const localBadge = rt.is_local
        ? `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;color:#0e7490;background:#cffafe;border-radius:999px;padding:1px 8px;white-space:nowrap;vertical-align:middle">本机直连</span>`
        : '';
      const hostTag = rt.is_local && rt.host_name ? `<span style="font-size:10px;color:var(--t3);margin-left:4px">${rt.host_name}</span>` : '';
      html += `<div style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh);cursor:pointer" onclick="openEngineDetail(${rt.id})">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
          ${_healthBadge(rt.health)}
          <span style="font-weight:600;font-size:14px">${rt.instance_name || rt.name} ${localBadge}${hostTag}</span>
          <span style="font-weight:400;color:var(--t3);font-size:12px;margin-left:auto">${rt.name} v${rt.version || ''}</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;font-size:12px;color:var(--t2)">
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">worker</span> ${rt.worker_name || (rt.is_local ? '本机' : '—')}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">owner</span> ${rt.owner || '—'} ${rt.is_local ? `<span style="color:var(--t3);font-size:10px">(${rt.owner_id || 'global'})</span>` : ''}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">skills</span> ${(rt.local_skills || []).length} 个</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">标签</span> ${_engineTagSummary(rt.agent_tags)}</div>
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
      const clis = (d.runtimes || []).map(rt => `<span style="cursor:pointer;color:#235ec7;text-decoration:underline" onclick="event.stopPropagation();openEngineDetail(${rt.id})">${rt.name} v${rt.version || ''}${rt.healthy ? ' ✓' : ' ✗'}</span>`).join(' · ');
      const credColor = d.credential_status === 'revoked' ? 'var(--r)' : 'var(--g)';
      html += `<div style="background:var(--c);border:1px solid var(--b);border-radius:var(--r2);padding:12px 14px;box-shadow:var(--sh)">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
          ${_healthBadge(d.health, d.credential_id ? relTime(d.last_seen_at) : '本机直连')}
          <span style="font-weight:600;font-size:14px">${d.name || d.device_name || '?'}</span>
          <span style="font-weight:400;color:var(--t3);font-size:12px">${d.worker_ip || ''}</span>
          ${d.credential_id ? `<button class="bt bt-o" style="font-size:11px;padding:6px 10px;border-radius:4px;margin-left:auto" onclick="renameCredential(${d.credential_id})">改名</button>` : ''}
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:16px;font-size:12px;color:var(--t2)">
          <div style="display:flex;gap:16px;flex:1;min-width:0">
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">owner</span> ${d.owner || '—'} ${d.credential_id === 0 ? `<span style="color:var(--t3);font-size:10px">(${d.owner_id || 'global'})</span>` : ''}</div>
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">凭据</span> <b style="color:${credColor}">${d.credential_status || ''}</b></div>
          </div>
          ${d.credential_id ? `<button class="bt bt-r" style="font-size:11px;padding:6px 10px;border-radius:4px;flex-shrink:0" onclick="revokeCredential(${d.credential_id})">吊销</button>` : ''}
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:var(--t2)">
          <span style="color:var(--t3);white-space:nowrap;flex-shrink:0">承载引擎</span>
          <span style="flex:1;min-width:0;word-break:break-all">${clis || '—'}</span>
          ${d.credential_id ? `<button class="bt bt-o" style="font-size:11px;padding:6px 10px;border-radius:4px;flex-shrink:0" onclick="openDeviceTasks(${d.credential_id})">任务</button>` : ''}
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
        const desc = detail ? (detail.description || '无描述') : '点击查看详情';
        const detailArg = detail ? JSON.stringify(detail).replace(/'/g, '&#39;').replace(/"/g, '&quot;') : '';
        return `
        <div style="padding:8px 12px;border:1px solid var(--b);border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='var(--bg)'" onmouseout="this.style.background=''" onclick="openSkillDetail('${s}', ${runtimeId}, ${detailArg})">
          <div style="font-weight:600;font-size:13px;color:var(--t1)">${s}</div>
          <div style="font-size:11px;color:var(--t3);margin-top:2px">${desc}</div>
        </div>`;
      }).join('')
    : '<div style="color:var(--t3);font-size:12px;padding:8px">无本地 skill</div>';

  const html = `<div id="engineDetailOverlay" style="position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center" onclick="closeEngineDetail(event)">
    <div style="background:var(--c);border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.2);max-width:680px;width:90%;max-height:85vh;overflow:auto;padding:24px" onclick="event.stopPropagation()">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
        ${_healthBadge(rt.health)}
        <span style="font-weight:700;font-size:18px">${rt.instance_name || rt.name}</span>
        ${rt.is_local ? '<span style="font-size:11px;font-weight:600;color:#0e7490;background:#cffafe;border-radius:999px;padding:1px 8px">本机直连</span>' : ''}
        <button class="bt bt-o" style="margin-left:auto" onclick="closeEngineDetail()">关闭</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:13px;color:var(--t2);margin-bottom:20px">
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">引擎类型</span> ${rt.name}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">版本</span> v${rt.version || '—'}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">健康状态</span> ${rt.healthy ? '✓ 健康' : '✗ 异常'}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">Worker</span> ${rt.worker_name || (rt.is_local ? '本机' : '—')}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">IP</span> ${rt.worker_ip || '—'}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">Scope</span> ${_scopeLabel(rt.scope)}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">Owner</span> ${rt.owner || '—'}</div>
        <div><span style="color:var(--t3);display:inline-block;min-width:70px">已安装</span> ${rt.installed ? '是' : '否'}</div>
      </div>
      <div style="border-top:1px solid var(--b);padding-top:16px;margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <span style="font-weight:600;font-size:14px">角色标签</span>
          <button class="bt bt-p" style="font-size:11px;padding:4px 10px;border-radius:4px" onclick="saveEngineTags(${runtimeId})">保存标签</button>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">${_engineTagCheckboxes(rt.agent_tags || ['all'])}</div>
      </div>
      <div style="border-top:1px solid var(--b);padding-top:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <span style="font-weight:600;font-size:14px">本地 Skill 列表 <span style="font-weight:400;color:var(--t3);font-size:12px">（${skills.length} 个）</span></span>
          <button id="refreshSkillsBtn" class="bt bt-o" style="padding:4px 10px;font-size:12px" onclick="refreshEngineSkills(${runtimeId})">⟳ 刷新</button>
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
  btn.innerHTML = '<span style="display:inline-block;animation:spin 0.8s linear infinite">⟳</span> 扫描中...';
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
    btn.innerHTML = '⟳ 刷新';
    app.showToast('扫描超时，请稍后重试');
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
    btn.innerHTML = '⟳ 刷新';
    const listEl = document.getElementById('engineSkillList');
    if (!listEl) return;
    const skills = d.skills || [];
    _skillScanDirty = true;  // 标记：关闭弹窗时刷新引擎列表卡片
    if (!skills.length) {
      listEl.innerHTML = '<div style="color:var(--t3);font-size:12px;padding:8px">无本地 skill</div>';
    } else {
      listEl.innerHTML = skills.map(s => `
        <div style="padding:8px 12px;border:1px solid var(--b);border-radius:8px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='var(--bg)'" onmouseout="this.style.background=''" onclick="openSkillDetail('${s.name}', ${runtimeId}, ${JSON.stringify(s).replace(/'/g, '&#39;').replace(/"/g, '&quot;')})">
          <div style="font-weight:600;font-size:13px;color:var(--t1)">${s.name}</div>
          <div style="font-size:11px;color:var(--t3);margin-top:2px">${s.description || '无描述'}</div>
        </div>`).join('');
    }
    const titleEl = document.querySelector('#engineDetailOverlay span[style*="font-weight:400"]');
    if (titleEl) titleEl.textContent = '（' + skills.length + ' 个）';
    app.showToast('扫描完成：' + skills.length + ' 个 skill');
  }).catch(e => {
    btn.disabled = false;
    btn.innerHTML = '⟳ 刷新';
    app.showToast('刷新失败: ' + e.message);
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
        <button class="bt bt-o" style="margin-left:auto" onclick="closeSkillDetail()">关闭</button>
      </div>
      <div style="display:grid;gap:8px;font-size:13px;color:var(--t2)">
        ${detail ? `
          <div><span style="color:var(--t3);display:inline-block;min-width:80px">描述</span> ${detail.description || '—'}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:80px">提供方</span> ${detail.provider || '—'}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:80px">本地路径</span> <code style="font-size:11px;background:var(--bg);padding:2px 6px;border-radius:4px;word-break:break-all">${detail.path || '—'}</code></div>
          <div><span style="color:var(--t3);display:inline-block;min-width:80px">文件数</span> ${detail.file_count || 0}</div>
        ` : '<div style="color:var(--t3)">详情不可用（点击刷新按钮获取详情）</div>'}
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
const _RT_TAG_LABELS = {all: '全能', vod: 'VOD', issue: 'Issue', coding: 'Coding', qa: 'QA', sre: 'SRE'};
const _RT_TAG_OPTIONS = ['all', 'vod', 'issue', 'coding', 'qa', 'sre'];

function _engineTagCheckboxes(tags) {
  return _RT_TAG_OPTIONS.map(t => {
    const checked = tags.includes(t) ? 'checked' : '';
    const desc = t === 'all' ? '可作为所有角色' : '仅 ' + _RT_TAG_LABELS[t] + ' 角色';
    return `<label style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;padding:2px 6px">
      <input type="checkbox" id="engineTagCb_${t}" value="${t}" ${checked} onchange="onEngineTagChange('${t}')"
             style="width:14px;height:14px;accent-color:${_RT_TAG_COLORS[t]}">
      <span style="font-size:10px;padding:2px 8px;border-radius:999px;color:#fff;background:${_RT_TAG_COLORS[t]}">${_RT_TAG_LABELS[t]}</span>
      <span style="color:var(--t3);font-size:11px">${desc}</span>
    </label>`;
  }).join('');
}

function _engineTagSummary(tags) {
  if (!tags || tags.length === 0) return '<span style="color:var(--t3);font-size:10px">未分配</span>';
  return tags.map(t => `<span style="font-size:10px;padding:1px 6px;border-radius:999px;color:#fff;background:${_RT_TAG_COLORS[t]||'#999'};margin:0 2px">${_RT_TAG_LABELS[t]||t}</span>`).join('');
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
    app.showToast('标签已保存');
    // 重新渲染列表页（立即生效，无需手动刷新）
    if (__runtimeData) renderRuntimes(__runtimeData);
  }).catch(e => app.showToast('保存失败: ' + e.message));
}
