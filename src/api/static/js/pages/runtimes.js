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

// 任务列表 + 日志弹窗函数已迁移到 observability.js


function renderRuntimesLegacy(data) { renderRuntimes(data); }
