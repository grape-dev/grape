/* Agent Runtime 管理页（M4 双视图）：Engines / Workers 两个子页，各按 scope 分区展示。
   见 runtime-admin-two-view-design.md。卡片式布局：状态徽章置顶，异常引擎优先捕捉。 */
const RUNTIME_ICON = { online: '●', recently_lost: '◐', offline: '○', about_to_gc: '⚠' };
const RUNTIME_COLOR = { online: 'var(--g)', recently_lost: 'var(--y)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };
const SCOPE_NAMES = { global: 'global', repo: 'repo', user: 'user' };
const SCOPE_ORDER = ['global', 'repo', 'user'];
const HEALTH_NAMES = { online: 'online', recently_lost: 'recently_lost', offline: 'offline', about_to_gc: 'about_to_gc' };
const HEALTH_BADGE = {
  online: { color: 'var(--g)', bg: '#dcfce7' },
  recently_lost: { color: 'var(--y)', bg: '#fef3c7' },
  offline: { color: 'var(--t3)', bg: '#eef0f3' },
  about_to_gc: { color: 'var(--r)', bg: '#fee2e2' },
};

let _runtimeTab = 'engines';
let _runtimeSearch = '';
let _rtFilterType = '';
let _rtFilterScope = '';
let _rtFilterScene = [];
let _rtFilterTag = [];
let __runtimeData = null;
let __runtimeDevices = null;
let _skillScanDirty = false;  // skills 扫描成功后标记，关闭弹窗时刷新引擎列表
let _scopeCollapse = { global: false, repo: true, user: true };  // scope 分组折叠状态：global 默认展开，repo/user 默认折叠

function _toggleScope(sc) {
  _scopeCollapse[sc] = !_scopeCollapse[sc];
  renderRuntimes(__runtimeData);
}

// 健康状态排序：在线 → 刚失联 → 离线 → 待回收
const _HEALTH_ORDER = { online: 0, recently_lost: 1, offline: 2, about_to_gc: 3 };
function _sortByHealth(list) {
  return list.sort((a, b) => {
    const ha = _HEALTH_ORDER[a.health || a.status || 'offline'] ?? 9;
    const hb = _HEALTH_ORDER[b.health || b.status || 'offline'] ?? 9;
    return ha - hb;
  });
}

// 按名称排序（字母数字从小到大）
function _sortByName(list) {
  return list.sort((a, b) => {
    const na = (a.instance_name || a.name || a.device_name || '').toLowerCase();
    const nb = (b.instance_name || b.name || b.device_name || '').toLowerCase();
    return na.localeCompare(nb);
  });
}

// 健康子分组 Tab（scope 展开后，点击切换不同状态的卡片）
const _HEALTH_SECTIONS = [
  { key: 'online', label: '在线', color: 'var(--g)', icon: '●' },
  { key: 'recently_lost', label: '刚失联', color: 'var(--y)', icon: '◐' },
  { key: 'offline', label: '离线', color: 'var(--t3)', icon: '○' },
  { key: 'about_to_gc', label: '待回收', color: 'var(--r)', icon: '⚠' },
];
let _healthTab = {};  // { scope: current_tab_key }，默认 'online'

function _healthTabBar(sc, list) {
  const counts = {};
  _HEALTH_SECTIONS.forEach(sec => { counts[sec.key] = 0; });
  list.forEach(e => { const h = e.health || e.status || 'offline'; if (counts[h] !== undefined) counts[h]++; });
  const active = _healthTab[sc] || 'all';
  const allCount = list.length;
  return `<div style="display:flex;align-items:center;gap:0;margin:8px 0 12px;border-bottom:1px solid var(--b)">
    <span onclick="_healthTab['${sc}']='all';renderRuntimes(__runtimeData)" style="display:inline-flex;align-items:center;gap:5px;padding:6px 14px;font-size:12px;font-weight:${active==='all'?'600':'400'};color:${active==='all'?'var(--t1)':'var(--t3)'};cursor:pointer;border-bottom:2px solid ${active==='all'?'var(--t1)':'transparent'};transition:all .15s;user-select:none" onmouseover="if('${active}'!=='all')this.style.color='var(--t2)'" onmouseout="if('${active}'!=='all')this.style.color='var(--t3)'">全部 <span style="font-size:10px;opacity:.7">${allCount}</span></span>
    ${_HEALTH_SECTIONS.map(sec => {
      const n = counts[sec.key] || 0;
      const isActive = active === sec.key;
      return `<span onclick="_healthTab['${sc}']='${sec.key}';renderRuntimes(__runtimeData)" style="display:inline-flex;align-items:center;gap:5px;padding:6px 14px;font-size:12px;font-weight:${isActive?'600':'400'};color:${isActive ? sec.color : 'var(--t3)'};cursor:pointer;border-bottom:2px solid ${isActive ? sec.color : 'transparent'};transition:all .15s;user-select:none" onmouseover="if(!${isActive})this.style.color='var(--t2)'" onmouseout="if(!${isActive})this.style.color='var(--t3)'">${sec.icon} ${sec.label} <span style="font-size:10px;opacity:.7">${n}</span></span>`;
    }).join('')}
  </div>`;
}

// 健康状态统计摘要（折叠时显示在标题栏）
function _healthSummary(list) {
  const counts = { online: 0, recently_lost: 0, offline: 0, about_to_gc: 0 };
  list.forEach(e => { counts[e.health || e.status || 'offline'] = (counts[e.health || e.status || 'offline'] || 0) + 1; });
  const parts = [];
  if (counts.online) parts.push(`<span style="color:var(--g)">●${counts.online}</span>`);
  if (counts.recently_lost) parts.push(`<span style="color:var(--y)">◐${counts.recently_lost}</span>`);
  if (counts.offline) parts.push(`<span style="color:var(--t3)">○${counts.offline}</span>`);
  if (counts.about_to_gc) parts.push(`<span style="color:var(--r)">⚠${counts.about_to_gc}</span>`);
  return parts.length ? parts.join(' · ') : '';
}

function _currentUser() {
  try {
    const u = JSON.parse(localStorage.getItem('user') || 'null');
    return (u && (u.nickname || u.username)) || '';
  } catch (e) { return ''; }
}

function _healthDot(h) { return `<span style="color:${RUNTIME_COLOR[h] || 'var(--t3)'}">${RUNTIME_ICON[h] || '○'}</span>`; }
function _healthLabel(h) { return I18N.t('runtimes.health.' + (HEALTH_NAMES[h] || 'offline')); }
function _scopeLabel(s) { return I18N.t('runtimes.scope.' + (SCOPE_NAMES[s] || 'global')); }

// 汇总统计栏：在过滤标签和卡片网格之间显示总体健康分布
function _summaryStatsBar(data, devices) {
  const list = _runtimeTab === 'engines' ? (data.runtimes || []) : devices;
  if (!list || !list.length) return '';
  const counts = { online: 0, recently_lost: 0, offline: 0, about_to_gc: 0 };
  list.forEach(e => { counts[e.health || e.status || 'offline'] = (counts[e.health || e.status || 'offline'] || 0) + 1; });
  const total = list.length;
  const parts = [
    `<span style="color:var(--t2);font-weight:500">共 ${total} 台</span>`,
    counts.online ? `<span style="display:inline-flex;align-items:center;gap:4px"><span style="width:7px;height:7px;border-radius:50%;background:var(--g)"></span>在线 ${counts.online}</span>` : '',
    counts.recently_lost ? `<span style="display:inline-flex;align-items:center;gap:4px"><span style="width:7px;height:7px;border-radius:50%;background:var(--y)"></span>刚失联 ${counts.recently_lost}</span>` : '',
    counts.offline ? `<span style="display:inline-flex;align-items:center;gap:4px"><span style="width:7px;height:7px;border-radius:50%;background:var(--t3)"></span>离线 ${counts.offline}</span>` : '',
    counts.about_to_gc ? `<span style="display:inline-flex;align-items:center;gap:4px"><span style="width:7px;height:7px;border-radius:50%;background:var(--r)"></span>待回收 ${counts.about_to_gc}</span>` : '',
  ].filter(Boolean);
  return `<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:7px 14px;margin-bottom:10px;font-size:12px;color:var(--t2);background:var(--bg);border-radius:8px">${parts.join('<span style="color:var(--t3)">·</span>')}</div>`;
}

// 引擎半透明底图：logo 图片水印（右下角，区分 opencode/hermes/codearts/codex）
function _engineWatermark(name) {
  const n = (name || '').toLowerCase();
  if (n.includes('opencode')) {
    return `<img src="/static/img/engines/opencode.png" style="position:absolute;right:12px;bottom:8px;width:56px;height:56px;object-fit:contain;opacity:.25;pointer-events:none" alt="">`;
  }
  if (n.includes('hermes')) {
    return `<img src="/static/img/engines/Hermes.png" style="position:absolute;right:-18px;bottom:-30px;width:130px;height:130px;object-fit:contain;opacity:.25;pointer-events:none" alt="">`;
  }
  if (n.includes('codearts')) {
    return `<img src="/static/img/engines/codearts.jpg" style="position:absolute;right:12px;bottom:8px;width:50px;height:50px;object-fit:contain;opacity:.25;pointer-events:none" alt="">`;
  }
  if (n.includes('codex')) {
    return `<img src="/static/img/engines/codex.png" style="position:absolute;right:12px;bottom:8px;width:56px;height:56px;object-fit:contain;opacity:.25;pointer-events:none" alt="">`;
  }
  return '';
}

function _healthBadge(h, suffix) {
  const st = HEALTH_BADGE[h] || HEALTH_BADGE.offline;
  return `<span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;color:${st.color};background:${st.bg};padding:3px 10px;border-radius:999px;white-space:nowrap"><span style="width:7px;height:7px;border-radius:50%;background:${st.color}"></span>${_healthLabel(h)}${suffix ? ' · ' + suffix : ''}</span>`;
}

function _scopeBar(sc, n, localN, healthSummary, collapsed) {
  const _SCOPE_BORDER = { global: '#3168e0', repo: '#8b5cf6', user: '#1ca44c' };
  const arrow = collapsed ? '▸' : '▾';
  const arrowRot = collapsed ? 'rotate(-90deg)' : 'rotate(0deg)';
  const sub = (sc === 'global' && localN) ? ` <span style="color:#0e7490;font-weight:600">· ${I18N.t('runtimes.label.local_direct')} ${localN} / ${I18N.t('runtimes.label.remote')} ${(n || 0) - localN}</span>` : '';
  const health = healthSummary ? ` <span style="font-weight:400;font-size:11px;color:var(--t2);margin-left:4px">${healthSummary}</span>` : '';
  return `<div onclick="_toggleScope('${sc}')" style="display:flex;align-items:center;gap:8px;background:#e3edfd;color:#235ec7;padding:8px 14px;border-radius:8px;font-size:13px;font-weight:600;margin:16px 0 10px;cursor:pointer;user-select:none;transition:background .15s;border-left:3px solid ${_SCOPE_BORDER[sc] || 'var(--a)'}" onmouseover="this.style.background='#d4e3fc'" onmouseout="this.style.background='#e3edfd'">
    <span style="font-size:14px;width:14px;flex-shrink:0;display:inline-block;transition:transform .25s ease;transform:${arrowRot}">${arrow}</span>
    ${_scopeLabel(sc)} <span style="background:rgba(35,94,199,.12);border-radius:10px;padding:0 8px;font-size:11px">${n}</span>${sub}${health}
  </div>`;
}

function _legendHtml() {
  return `<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:8px 0;font-size:11px;color:var(--t3);border-top:1px dashed var(--b)">
    <span style="font-weight:500;color:var(--t2)">${I18N.t('runtimes.devspace.legend_label')}</span>
    ${Object.entries(HEALTH_NAMES).map(([k, v]) =>
      `<span style="display:inline-flex;align-items:center;gap:5px"><span style="width:7px;height:7px;border-radius:50%;background:${(HEALTH_BADGE[k] || {}).color || 'var(--t3)'}"></span>${I18N.t('runtimes.health.' + v)}${k === 'recently_lost' ? '(<5min)' : k === 'about_to_gc' ? '(>6d)' : ''}</span>`).join('')}
  </div>`;
}

function renderRuntimes(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  __runtimeData = data;
  // 场景标签缓存未加载时先异步拉取，拿到后再渲染（避免卡片显示原始 name 而非 display_name）
  if (!__sceneTagsCache) {
    _loadSceneTagsCache(function() { renderRuntimes(data); });
    return;
  }
  // 设置当前用户名供权限判断
  try { window._grapeUsername = (JSON.parse(localStorage.getItem('user') || '{}').username) || ''; } catch(e) { window._grapeUsername = ''; }
  const devices = (data.devices && data.devices.length)
    ? data.devices
    : (data.runtimes || []).map(rt => ({ name: rt.worker_name || rt.name, device_name: I18N.t('runtimes.label.local'), health: rt.health || 'offline',
      scope: 'global', owner: rt.owner || '', worker_ip: rt.worker_ip || '', credential_id: 0, runtimes: [rt] }));
  __runtimeDevices = devices;

  const tabBtn = t => {
    const active = _runtimeTab === t;
    return `<button class="bt ${active ? 'bt-p' : 'bt-o'}" onclick="_runtimeTab='${t}';renderRuntimes(__runtimeData)">${I18N.t('runtimes.tab.' + t)}</button>`;
  };
  let html = `<div style="display:flex;gap:8px;margin-bottom:10px;align-items:center;flex-wrap:wrap">
      ${tabBtn('engines')}${tabBtn('workers')}
      <div class="sp"></div>
      <button class="bt bt-o" onclick="refreshRuntimes()">⟳ ${I18N.t('runtimes.refresh')}</button>
      <div style="display:flex;align-items:center;flex:1;max-width:300px">
        <input id="runtimeSearchInput" placeholder="搜索名称、设备、标签..." value="${_runtimeSearch}" onkeyup="if(event.key==='Enter'){_runtimeSearch=this.value;renderRuntimes(__runtimeData)}" onchange="_runtimeSearch=this.value;renderRuntimes(__runtimeData)" style="flex:1;padding:7px 12px 7px 14px;border:1px solid var(--b);border-radius:10px 0 0 10px;font-size:13px;background:var(--bg);color:var(--t1);transition:border-color .2s;outline:none" onfocus="this.style.borderColor='#0071e3';this.style.background='var(--c)'" onblur="this.style.borderColor='var(--b)';this.style.background='var(--bg)'">
        <button onclick="var i=document.getElementById('runtimeSearchInput');_runtimeSearch=i.value;renderRuntimes(__runtimeData)" style="padding:7px 14px;border:1px solid var(--b);border-left:none;border-radius:0 10px 10px 0;background:var(--bg);color:var(--t3);font-size:13px;cursor:pointer;flex-shrink:0;transition:all .15s" onmouseover="this.style.background='#0071e3';this.style.color='#fff'" onmouseout="this.style.background='var(--bg)';this.style.color='var(--t3)'"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:block"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg></button>
      </div>
      <button onclick="openAddDevicePanel()" style="margin-left:auto;display:inline-flex;align-items:center;gap:7px;padding:8px 18px;border:none;border-radius:10px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;font-size:13px;font-weight:600;cursor:pointer;box-shadow:0 2px 12px rgba(59,130,246,.35);transition:all .2s;position:relative;overflow:hidden" onmouseover="this.style.transform='translateY(-1px)';this.style.boxShadow='0 4px 20px rgba(59,130,246,.45)'" onmouseout="this.style.transform='translateY(0)';this.style.boxShadow='0 2px 12px rgba(59,130,246,.35)'><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0"><path d="M12 5v14M5 12h14"/></svg>${I18N.t('runtimes.action.add_device')}</button>
    </div>`;
  // 过滤标签栏
  const _availTypes = [...new Set((_runtimeTab === 'engines' ? (data.runtimes||[]) : devices).map(e => {
    const n = ((e.instance_name || e.name || '')).toLowerCase();
    if (n.includes('hermes')) return 'hermes';
    if (n.includes('opencode')) return 'opencode';
    if (n.includes('codearts')) return 'codearts';
    if (n.includes('codex')) return 'codex';
    return '';
  }).filter(Boolean))];
  const _allScopes = ['local','user','repo','global'];
  const _SCOPE_LABELS = {global:'全局', user:'个人', repo:'仓库', local:'本机'};
  const _hasScope = (sc) => {
    const list = _runtimeTab === 'engines' ? (data.runtimes||[]) : devices;
    return list.some(e => (e.scope || (e.is_local ? 'local' : 'global')) === sc || (sc === 'local' && e.is_local));
  };
  const _ENGINE_DOT = { opencode: '#3b82f6', hermes: '#8b5cf6', codearts: '#f59e0b', codex: '#10a37f' };
  // 收集可用场景和标签（从引擎数据提取）
  const _allRuntimes = (data.runtimes || []);
  const _availScenes = [...new Set(_allRuntimes.flatMap(rt => rt.scene_tags || []))].filter(Boolean);
  const _availTags = [...new Set(_allRuntimes.flatMap(rt => rt.agent_tags || []))].filter(Boolean);
  html += `<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:8px">
    <span style="font-size:11px;color:var(--t3);font-weight:500;margin-right:2px">筛选</span>
    ${_availTypes.map(t => {
      const act = _rtFilterType === t;
      return `<span onclick="_rtToggleType('${t}')" style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;font-size:11px;padding:4px 12px;border-radius:999px;border:1px solid ${act?'#0071e3':'var(--b)'};color:${act?'#fff':'var(--t1)'};background:${act?'#0071e3':'var(--c)'};font-weight:500;transition:all .2s;letter-spacing:.2px"><span style="width:7px;height:7px;border-radius:50%;background:${_ENGINE_DOT[t]||'#999'};flex-shrink:0"></span>${t}</span>`;
    }).join('')}
    ${_availTypes.length > 0 ? '<span style="width:1px;background:var(--b);margin:2px 4px;height:18px"></span>' : ''}
    ${_allScopes.filter(_hasScope).map(s => {
      const act = _rtFilterScope === s;
      return `<span onclick="_rtToggleScope('${s}')" style="cursor:pointer;font-size:11px;padding:4px 12px;border-radius:999px;border:1px solid ${act?'#0071e3':'var(--b)'};color:${act?'#fff':'var(--t1)'};background:${act?'#0071e3':'var(--c)'};font-weight:500;transition:all .2s;letter-spacing:.2px">${_SCOPE_LABELS[s]||s}</span>`;
    }).join('')}
    ${_availScenes.length > 0 && _runtimeTab === 'engines' ? '<span style="width:1px;background:var(--b);margin:2px 4px;height:18px"></span>' : ''}
    ${_runtimeTab === 'engines' ? `
    <div style="position:relative;display:inline-block">
      <span onclick="_toggleFilterDropdown('sceneDropdown')" style="cursor:pointer;font-size:11px;padding:4px 12px;border-radius:999px;border:1px solid ${_rtFilterScene.length ? '#10b981' : 'var(--b)'};color:${_rtFilterScene.length?'#fff':'var(--t1)'};background:${_rtFilterScene.length ? '#10b981' : 'var(--c)'};font-weight:500;transition:all .2s;display:inline-flex;align-items:center;gap:4px">${I18N.t('runtimes.label.scene')}${_rtFilterScene.length ? '(' + _rtFilterScene.length + ')' : ''}<span style="font-size:8px;opacity:.6">▼</span></span>
      <div id="sceneDropdown" style="display:none;position:absolute;top:calc(100% + 4px);left:0;background:var(--c);border:1px solid var(--b);border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.12);z-index:100;padding:4px;min-width:140px">
        ${_availScenes.map(s => { var c = _SCENE_TAG_COLORS[s]||'#10b981'; var sel = _rtFilterScene.indexOf(s) >= 0; return '<label style="padding:5px 10px;font-size:11px;cursor:pointer;border-radius:4px;display:flex;align-items:center;gap:6px" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'\'"><input type="checkbox" onchange="_rtToggleMulti(\'scene\',\'' + s + '\')" ' + (sel?'checked':'') + ' style="width:13px;height:13px;accent-color:' + c + '"><span style="font-size:9px;padding:1px 7px;border-radius:999px;color:#fff;background:' + c + '">' + _sceneDisplayName(s) + '</span></label>'; }).join('')}
      </div>
    </div>
    <div style="position:relative;display:inline-block">
      <span onclick="_toggleFilterDropdown('tagDropdown')" style="cursor:pointer;font-size:11px;padding:4px 12px;border-radius:999px;border:1px solid ${_rtFilterTag.length ? (_RT_TAG_COLORS[_rtFilterTag[0]]||'#999') : 'var(--b)'};color:${_rtFilterTag.length?'#fff':'var(--t1)'};background:${_rtFilterTag.length ? (_RT_TAG_COLORS[_rtFilterTag[0]]||'#999') : 'var(--c)'};font-weight:500;transition:all .2s;display:inline-flex;align-items:center;gap:4px">${I18N.t('runtimes.label.role_tags')}${_rtFilterTag.length ? '(' + _rtFilterTag.length + ')' : ''}<span style="font-size:8px;opacity:.6">▼</span></span>
      <div id="tagDropdown" style="display:none;position:absolute;top:calc(100% + 4px);left:0;background:var(--c);border:1px solid var(--b);border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.12);z-index:100;padding:4px;min-width:140px">
        ${_availTags.map(t => { var c = _RT_TAG_COLORS[t]||'#999'; var sel = _rtFilterTag.indexOf(t) >= 0; return '<label style="padding:5px 10px;font-size:11px;cursor:pointer;border-radius:4px;display:flex;align-items:center;gap:6px" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'\'"><input type="checkbox" onchange="_rtToggleMulti(\'tag\',\'' + t + '\')" ' + (sel?'checked':'') + ' style="width:13px;height:13px;accent-color:' + c + '"><span style="font-size:9px;padding:1px 7px;border-radius:999px;color:#fff;background:' + c + '">' + _rtTagLabel(t) + '</span></label>'; }).join('')}
      </div>
    </div>` : ''}
    ${(_rtFilterType || _rtFilterScope || _rtFilterScene.length || _rtFilterTag.length || _runtimeSearch) ? `<span onclick="_rtClearFilters()" style="cursor:pointer;font-size:11px;padding:4px 12px;border-radius:999px;color:#86868b;font-weight:400" onmouseover="this.style.color='#d32f2f'" onmouseout="this.style.color='#86868b'">✕ 清除</span>` : ''}
  </div>`;
  html += _summaryStatsBar(data, devices);
  html += _runtimeTab === 'engines' ? renderEngines(data, devices) : renderWorkers(data, devices);
  html += '<style>[data-tooltip]{position:relative}[data-tooltip]:hover::after{content:attr(data-tooltip);position:absolute;top:calc(100% + 8px);left:50%;transform:translateX(-50%);background:#1e2940;color:#fff;padding:6px 14px;border-radius:8px;font-size:11px;font-weight:500;white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,.15);z-index:100;pointer-events:none;opacity:0;animation:tipIn .2s ease forwards}[data-tooltip]:hover::before{content:"";position:absolute;top:calc(100% + 3px);left:50%;transform:translateX(-50%);border:5px solid transparent;border-bottom-color:#1e2940;z-index:100;pointer-events:none;opacity:0;animation:tipIn .2s ease forwards}@keyframes tipIn{to{opacity:1}}</style>';
  html += _legendHtml();
  el.innerHTML = html;
}

function renderEngines(data, devices) {
  const runtimes = data.runtimes || [];
  // 搜索 + 类型 + scope 过滤
  const q = (_runtimeSearch || '').toLowerCase().trim();
  const filtered = runtimes.filter(rt => {
    if (q) {
      const hay = [rt.alias, rt.instance_name, rt.name, rt.worker_name, rt.owner, rt.host_name, (rt.tags||[]).join(' '), (rt.local_skills||[]).join(' ')].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (_rtFilterType) {
      const ename = (rt.instance_name || rt.name || '').toLowerCase();
      if (!ename.includes(_rtFilterType)) return false;
    }
    if (_rtFilterScope) {
      const sc = rt.is_local ? 'local' : (rt.scope || 'global');
      if (sc !== _rtFilterScope) return false;
    }
    if (_rtFilterScene.length > 0) {
      const tags = rt.scene_tags || [];
      if (!_rtFilterScene.some(s => tags.includes(s))) return false;
    }
    if (_rtFilterTag.length > 0) {
      const tags = rt.agent_tags || [];
      if (!_rtFilterTag.some(t => tags.includes(t) || tags.includes('all'))) return false;
    }
    return true;
  });
  const groups = { global: [], repo: [], user: [] };
  filtered.forEach(rt => { (groups[rt.scope] || groups.global).push(rt); });
  let html = '';
  SCOPE_ORDER.forEach(sc => {
    const list = groups[sc];
    if (!list.length) return;
    // 排序：本机直连优先 → 按健康状态（在线→刚失联→离线→待回收）
    list.sort((a, b) => (b.is_local ? 1 : 0) - (a.is_local ? 1 : 0));
    _sortByHealth(list);
    const localN = list.filter(r => r.is_local).length;
    const collapsed = _scopeCollapse[sc];
    html += _scopeBar(sc, list.length, localN, _healthSummary(list), collapsed);
    if (collapsed) return;  // 折叠时不渲染卡片网格
    // 健康 Tab 切换栏
    html += _healthTabBar(sc, list);
    const activeTab = _healthTab[sc] || 'all';
    const secList = activeTab === 'all' ? list.slice() : list.filter(rt => (rt.health || rt.status || 'offline') === activeTab);
    _sortByName(secList);
    html += `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px;max-height:440px;overflow-y:auto">`;
    secList.forEach(rt => {
      const localBadge = rt.is_local
        ? `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;color:#0e7490;background:#cffafe;border-radius:999px;padding:1px 8px;white-space:nowrap;vertical-align:middle">${I18N.t('runtimes.label.local_direct')}</span>`
        : '';
      const hostTag = '';
      const _hStatus = rt.health || rt.status || 'offline';
      const _cardLeftBorder = RUNTIME_COLOR[_hStatus] || 'var(--t3)';
      html += `<div style="background:var(--c);border:1px solid var(--b);border-left:3px solid ${_cardLeftBorder};border-radius:0 var(--r2) var(--r2) 0;padding:14px 16px;box-shadow:var(--sh);cursor:pointer;position:relative;overflow:hidden;display:flex;flex-direction:column" onclick="openEngineDetail(${rt.id})">
        ${_engineWatermark(rt.name)}
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap;min-height:24px">
          ${_healthBadge(rt.health)}
          <span style="display:inline-flex;align-items:center;gap:3px;overflow:hidden;flex:1;min-width:0">
            <span style="font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${rt.alias || rt.instance_name || rt.name}</span>
            ${rt.alias ? `<span style="font-size:10px;color:var(--t3);font-weight:400">(${rt.instance_name || rt.name})</span>` : ''}
            <svg onclick="event.stopPropagation();copyText('${(rt.instance_name || rt.name || '').replace(/'/g, "\\'")}');this.style.color='var(--g)';var _t=this;setTimeout(function(){_t.style.color='var(--t3)'},1500)" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--t3)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;cursor:pointer;transition:color .2s"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            ${localBadge}${hostTag}
          </span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 16px;font-size:12px;color:var(--t2);flex:1">
          <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.worker')}</span> ${rt.worker_name || (rt.is_local ? I18N.t('runtimes.label.local') : '—')}</div>
          <div><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.skills_label')}</span> ${I18N.t('runtimes.label.skills_count', {count: (rt.local_skills || []).length})}</div>
          <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.scene')}</span> ${_sceneTagSummary(rt.scene_tags)}</div>
          <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.tags')}</span> ${_engineTagSummary(rt.agent_tags)}</div>
        </div>
      </div>`;
    });
    html += `</div>`;
  });
  return html || `<div class="ts t2" style="padding:12px">${I18N.t('runtimes.empty.no_engines')}</div>`;
}

function renderWorkers(data, devices) {
  // 搜索 + 类型 + scope 过滤
  const q = (_runtimeSearch || '').toLowerCase().trim();
  const fDevices = devices.filter(d => {
    if (q) {
      const hay = [d.name, d.device_name, d.owner, d.worker_ip, (d.runtimes||[]).map(r=>r.name).join(' ')].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (_rtFilterType) {
      const names = (d.runtimes||[]).map(r => (r.name||'').toLowerCase()).join(' ');
      if (!names.includes(_rtFilterType)) return false;
    }
    if (_rtFilterScope) {
      const sc = d.credential_id === 0 ? 'local' : (d.scope || 'global');
      if (sc !== _rtFilterScope) return false;
    }
    return true;
  });
  const groups = { global: [], repo: [], user: [] };
  fDevices.forEach(d => { (groups[d.scope] || groups.global).push(d); });
  let html = '';
  SCOPE_ORDER.forEach(sc => {
    const list = groups[sc];
    if (!list.length) return;
    const collapsed = _scopeCollapse[sc];
    _sortByHealth(list);
    html += _scopeBar(sc, list.length, 0, _healthSummary(list), collapsed);
    if (collapsed) return;  // 折叠时不渲染卡片网格
    // 健康 Tab 切换栏
    html += _healthTabBar(sc, list);
    const activeTab = _healthTab[sc] || 'all';
    const secList = activeTab === 'all' ? list.slice() : list.filter(d => (d.health || d.status || 'offline') === activeTab);
    _sortByName(secList);
    html += `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px;max-height:440px;overflow-y:auto">`;
    secList.forEach(d => {
      const _isOwner = d.owner && window._grapeUsername && d.owner === window._grapeUsername;
      const _canManage = d.credential_id && _isOwner;
      const _curScope = d.scope || 'global';
      const clis = (d.runtimes || []).map(rt => `<span style="cursor:pointer;color:#235ec7;text-decoration:underline" onclick="event.stopPropagation();openEngineDetail(${rt.id})">${rt.alias || rt.name} v${rt.version || ''}${rt.healthy ? ' ✓' : ' ✗'}</span>`).join(' · ');
      const credColor = d.credential_status === 'revoked' ? 'var(--r)' : 'var(--g)';
      const _wStatus = d.health || 'offline';
      const _wCardLeftBorder = _wStatus === 'online' ? 'none' : (RUNTIME_COLOR[_wStatus] || 'var(--t3)');
      html += `<div style="background:var(--c);border:1px solid var(--b);border-left:${_wCardLeftBorder === 'none' ? '1px solid var(--b)' : '3px solid ' + _wCardLeftBorder};border-radius:0 var(--r2) var(--r2) 0;padding:12px 14px;box-shadow:var(--sh)">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
          ${_healthBadge(d.health, d.credential_id ? relTime(d.last_seen_at) : I18N.t('runtimes.label.local_direct'))}
          <span style="font-weight:600;font-size:14px">${d.name || d.device_name || '?'}</span>
          <span style="font-weight:400;color:var(--t3);font-size:12px">${d.worker_ip || ''}</span>
          ${_canManage ? `<button class="bt bt-o" style="font-size:11px;padding:4px 10px;border-radius:4px;margin-left:auto" onclick="openScopeSwitcher(${d.credential_id}, '${_curScope}')">${_scopeLabelShort(_curScope)}</button>` : ''}
          ${d.credential_id && _isOwner ? `<button class="bt bt-o" style="font-size:11px;padding:6px 10px;border-radius:4px" onclick="renameCredential(${d.credential_id})">${I18N.t('runtimes.action.rename')}</button>` : ''}
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;gap:16px;font-size:12px;color:var(--t2)">
          <div style="display:flex;gap:16px;flex:1;min-width:0">
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.owner')}</span> ${d.owner || '—'} ${d.credential_id === 0 ? `<span style="color:var(--t3);font-size:10px">(${I18N.t('runtimes.scope.global')})</span>` : ''}</div>
            <div><span style="color:var(--t3);display:inline-block;min-width:46px">${I18N.t('runtimes.label.credential')}</span> <b style="color:${credColor}">${d.credential_status || ''}</b></div>
          </div>
          ${d.credential_id && _isOwner ? `<button class="bt bt-r" style="font-size:11px;padding:6px 10px;border-radius:4px;flex-shrink:0" onclick="revokeCredential(${d.credential_id})">${I18N.t('runtimes.action.revoke')}</button>` : ''}
        </div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:var(--t2)">
          <span style="color:var(--t3);white-space:nowrap;flex-shrink:0">${I18N.t('runtimes.label.hosted_engines')}</span>
          <span style="flex:1;min-width:0;word-break:break-all">${clis || '—'}</span>
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

function _scopeLabelShort(s) {
  return s === 'global' ? I18N.t('runtimes.scope.global') : s === 'repo' ? I18N.t('runtimes.scope.repo') : s === 'user' ? I18N.t('runtimes.scope.user') : s;
}

function openScopeSwitcher(cid, currentScope) {
  var scopes = ['repo', 'user'];
  var existing = document.getElementById('scopeSwitcherOverlay');
  if (existing) { existing.remove(); return; }
  var overlay = document.createElement('div');
  overlay.id = 'scopeSwitcherOverlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center';
  overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
  var items = scopes.map(function(s) {
    var active = s === currentScope;
    return '<label style="display:flex;align-items:center;gap:10px;padding:10px 14px;border:1px solid ' + (active ? '#0071e3' : 'var(--b)') + ';border-radius:8px;cursor:pointer;background:' + (active ? 'rgba(0,113,227,.06)' : 'var(--bg)') + ';margin-bottom:8px">' +
      '<input type="radio" name="scopeRadio" value="' + s + '" ' + (active ? 'checked' : '') + ' style="accent-color:#0071e3;width:16px;height:16px">' +
      '<span style="font-size:13px;font-weight:' + (active ? '600' : '400') + ';color:var(--t1)">' + _scopeLabelShort(s) + '</span>' +
      '</label>';
  }).join('');
  overlay.innerHTML = '<div style="background:var(--c);border-radius:12px;box-shadow:0 8px 30px rgba(0,0,0,.2);width:320px;padding:20px" onclick="event.stopPropagation()">' +
    '<div style="font-weight:600;font-size:14px;margin-bottom:14px">' + I18N.t('runtimes.label.scope_short') + '</div>' +
    items +
    '<div id="scopeRepoKeyWrap" style="display:none;margin-bottom:12px">' +
      '<input id="scopeRepoKeyInput" placeholder="' + I18N.t('runtimes.label.repo_key_prompt') + '" style="width:100%;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg);color:var(--t1);outline:none">' +
    '</div>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:4px">' +
      '<button class="bt bt-o" style="font-size:12px;padding:6px 14px;border-radius:6px" onclick="document.getElementById(\'scopeSwitcherOverlay\').remove()">' + I18N.t('runtimes.action.close') + '</button>' +
      '<button class="bt bt-p" style="font-size:12px;padding:6px 14px;border-radius:6px" id="scopeConfirmBtn">' + I18N.t('common.confirm') + '</button>' +
    '</div></div>';
  document.body.appendChild(overlay);
  function _toggleRepoKey() {
    var sel = overlay.querySelector('input[name="scopeRadio"]:checked');
    document.getElementById('scopeRepoKeyWrap').style.display = (sel && sel.value === 'repo') ? 'block' : 'none';
  }
  overlay.querySelectorAll('input[name="scopeRadio"]').forEach(function(r) { r.onchange = _toggleRepoKey; });
  _toggleRepoKey();
  document.getElementById('scopeConfirmBtn').onclick = function() {
    var sel = overlay.querySelector('input[name="scopeRadio"]:checked');
    if (!sel) return;
    var scope = sel.value;
    var repoKey = '';
    if (scope === 'repo') {
      repoKey = (document.getElementById('scopeRepoKeyInput').value || '').trim();
      if (!repoKey) { app.showToast(I18N.t('runtimes.toast.scope_repo_required')); return; }
    }
    overlay.remove();
    API.put('/settings/runtime-credentials/' + cid + '/scope', {scope: scope, repo_key: repoKey}).then(function(d) {
      app.showToast(I18N.t('runtimes.toast.scope_changed', {scope: _scopeLabelShort(d.scope)}));
      API.get('/settings/agent-runtimes').then(renderRuntimes).catch(function(){});
    }).catch(function(e) { app.showToast(I18N.t('runtimes.toast.save_failed', {msg: e.message})); });
  };
}

// ====== 添加设备侧边栏 ======

function openAddDevicePanel() {
  var existing = document.getElementById('addDeviceOverlay');
  if (existing) { existing.remove(); return; }
  _stopDevspaceAutoRefresh();

  var overlay = document.createElement('div');
  overlay.id = 'addDeviceOverlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:9998;display:flex;justify-content:flex-end;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);background:rgba(0,0,0,.3);opacity:0;transition:opacity .3s ease';

  overlay.onclick = function(e) { if (e.target === overlay) closeAddDevicePanel(); };

  var sidebar = document.createElement('div');
  sidebar.id = 'addDeviceSidebar';
  sidebar.style.cssText = 'width:520px;max-width:92vw;height:100vh;background:var(--c);box-shadow:-12px 0 40px rgba(0,0,0,.15);overflow-y:auto;transform:translateX(100%);transition:transform .35s cubic-bezier(.22,.61,.36,1);display:flex;flex-direction:column';

  sidebar.innerHTML =
    // ── 头部 ──
    '<div style="padding:20px 24px;display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--b);position:sticky;top:0;background:var(--c);z-index:2">' +
      '<div style="width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);display:flex;align-items:center;justify-content:center;flex-shrink:0">' +
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>' +
      '</div>' +
      '<div style="flex:1;min-width:0">' +
        '<h3 style="font-size:17px;font-weight:700;margin:0;line-height:1.2">' + I18N.t('runtimes.action.add_device') + '</h3>' +
        '<p style="font-size:12px;color:var(--t3);margin:3px 0 0;line-height:1.3">' + I18N.t('runtimes.devspace.sidebar_subtitle') + '</p>' +
      '</div>' +
      '<button onclick="closeAddDevicePanel()" style="width:32px;height:32px;border-radius:8px;border:1px solid var(--b);background:var(--bg);cursor:pointer;display:flex;align-items:center;justify-content:center;color:var(--t2);font-size:16px;flex-shrink:0;transition:all .15s" onmouseover="this.style.background=\'#fee2e2\';this.style.color=\'#dc2626\';this.style.borderColor=\'#fecaca\'" onmouseout="this.style.background=\'var(--bg)\';this.style.color=\'var(--t2)\';this.style.borderColor=\'var(--b)\'">✕</button>' +
    '</div>' +

    // ── 内容区 ──
    '<div style="padding:24px;flex:1">' +

      // 选项一：生成凭据
      '<div style="margin-bottom:16px">' +
        '<div onclick="toggleAddDeviceCred()" style="display:flex;align-items:center;gap:14px;padding:16px 18px;background:var(--bg);border:1px solid var(--b);border-radius:12px;cursor:pointer;transition:all .2s" onmouseover="this.style.borderColor=\'#3b82f6\';this.style.boxShadow=\'0 2px 12px rgba(59,130,246,.08)\'" onmouseout="this.style.borderColor=\'var(--b)\';this.style.boxShadow=\'none\'">' +
          '<div style="width:42px;height:42px;border-radius:10px;background:#dbeafe;display:flex;align-items:center;justify-content:center;flex-shrink:0">' +
            '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>' +
          '</div>' +
          '<div style="flex:1;min-width:0">' +
            '<div style="font-size:15px;font-weight:600;color:var(--t1);line-height:1.3">' + I18N.t('runtimes.action.gen_credential') + '</div>' +
            '<div style="font-size:12px;color:var(--t3);margin-top:2px">' + I18N.t('runtimes.devspace.cred_hint') + '</div>' +
          '</div>' +
          '<svg id="credChevron" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--t3)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;transition:transform .3s"><path d="M6 9l6 6 6-6"/></svg>' +
        '</div>' +
        '<div id="addDeviceCredArea" style="display:none;max-height:0;overflow:hidden;transition:max-height .35s ease,margin-top .35s ease,opacity .25s ease;opacity:0;margin-top:0"></div>' +
      '</div>' +

      // 选项二：AI Devspace
      '<div style="margin-bottom:16px">' +
        '<div onclick="toggleAddDeviceDev()" style="display:flex;align-items:center;gap:14px;padding:16px 18px;background:linear-gradient(135deg,rgba(96,165,250,.08),rgba(167,139,250,.08));border:1px solid rgba(96,165,250,.2);border-radius:12px;cursor:pointer;transition:all .2s" onmouseover="this.style.borderColor=\'rgba(96,165,250,.5)\';this.style.boxShadow=\'0 2px 12px rgba(96,165,250,.12)\'" onmouseout="this.style.borderColor=\'rgba(96,165,250,.2)\';this.style.boxShadow=\'none\'">' +
          '<div style="width:42px;height:42px;border-radius:10px;background:linear-gradient(135deg,#60a5fa,#a78bfa);display:flex;align-items:center;justify-content:center;flex-shrink:0">' +
            '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 19a4.5 4.5 0 1 0-1.41-8.78 7 7 0 1 0-13.09 3.78A4.5 4.5 0 0 0 6.5 19z"/></svg>' +
          '</div>' +
          '<div style="flex:1;min-width:0">' +
            '<div style="font-size:15px;font-weight:600;color:var(--t1);line-height:1.3">☁ AI Devspace</div>' +
            '<div style="font-size:12px;color:var(--t3);margin-top:2px">' + I18N.t('runtimes.devspace.devspace_hint') + '</div>' +
          '</div>' +
          '<svg id="devChevron" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--t3)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;transition:transform .3s"><path d="M6 9l6 6 6-6"/></svg>' +
        '</div>' +
        '<div id="addDeviceDevArea" style="display:none;max-height:0;overflow:hidden;transition:max-height .35s ease,margin-top .35s ease,opacity .25s ease;opacity:0;margin-top:0"></div>' +
      '</div>' +

    '</div>';

  overlay.appendChild(sidebar);
  document.body.appendChild(overlay);

  setTimeout(function() {
    overlay.style.opacity = '1';
    sidebar.style.transform = 'translateX(0)';
  }, 10);
}

function closeAddDevicePanel() {
  _stopDevspaceAutoRefresh();
  var overlay = document.getElementById('addDeviceOverlay');
  if (!overlay) return;
  var sidebar = overlay.firstElementChild;
  if (sidebar) sidebar.style.transform = 'translateX(100%)';
  overlay.style.opacity = '0';
  setTimeout(function() {
    var el = document.getElementById('addDeviceOverlay');
    if (el) el.remove();
  }, 300);
}

function toggleAddDeviceCred() {
  var area = document.getElementById('addDeviceCredArea');
  var chevron = document.getElementById('credChevron');
  if (!area) return;
  if (area.style.display === 'none') {
    _collapseAddDeviceDev();
    if (!area.innerHTML.trim()) _renderAddDeviceCredForm();
    area.style.display = 'block';
    requestAnimationFrame(function() {
      area.style.maxHeight = '800px';
      area.style.opacity = '1';
      area.style.marginTop = '12px';
    });
    if (chevron) chevron.style.transform = 'rotate(180deg)';
  } else {
    _collapseAddDeviceCred();
  }
}

function _collapseAddDeviceCred() {
  var area = document.getElementById('addDeviceCredArea');
  var chevron = document.getElementById('credChevron');
  if (!area || area.style.display === 'none') { if (chevron) chevron.style.transform = ''; return; }
  area.style.maxHeight = '0';
  area.style.opacity = '0';
  area.style.marginTop = '0';
  if (chevron) chevron.style.transform = '';
  setTimeout(function() { if (area) area.style.display = 'none'; }, 350);
}

function _renderAddDeviceCredForm() {
  var area = document.getElementById('addDeviceCredArea');
  if (!area) return;
  var cur = _currentUser();
  area.innerHTML = '<div style="background:var(--bg);border:1px solid var(--b);border-radius:12px;padding:18px 20px">' +
    '<div style="font-size:13px;font-weight:600;margin-bottom:14px;color:var(--t1)">' + I18N.t('runtimes.credential.gen_title') + '</div>' +
    // 凭据名称
    '<div style="margin-bottom:12px">' +
      '<label style="display:block;font-size:11px;font-weight:600;color:var(--t3);margin-bottom:5px;text-transform:uppercase;letter-spacing:.4px">' + I18N.t('runtimes.label.cred_name') + '</label>' +
      '<input id="credName" placeholder="' + I18N.t('runtimes.label.cred_name_placeholder') + '" style="width:100%;box-sizing:border-box;padding:9px 12px;border:1px solid var(--b);border-radius:8px;font-size:13px;background:var(--c);color:var(--t1);transition:border-color .2s" onfocus="this.style.borderColor=\'#3b82f6\'" onblur="this.style.borderColor=\'var(--b)\'">' +
    '</div>' +
    // 归属用户
    '<div style="margin-bottom:12px">' +
      '<label style="display:block;font-size:11px;font-weight:600;color:var(--t3);margin-bottom:5px;text-transform:uppercase;letter-spacing:.4px">' + I18N.t('runtimes.label.owner_user') + '</label>' +
      '<input value="' + cur + '" disabled style="width:100%;box-sizing:border-box;padding:9px 12px;border:1px solid var(--b);border-radius:8px;font-size:13px;background:#f1f5f9;color:var(--t2)">' +
    '</div>' +
    // 范围
    '<div style="margin-bottom:12px">' +
      '<label style="display:block;font-size:11px;font-weight:600;color:var(--t3);margin-bottom:5px;text-transform:uppercase;letter-spacing:.4px">' + I18N.t('runtimes.label.scope') + '</label>' +
      '<select id="credScope" onchange="toggleRepoKey()" style="width:100%;box-sizing:border-box;padding:9px 12px;border:1px solid var(--b);border-radius:8px;font-size:13px;background:var(--c);color:var(--t1)"><option value="user">' + I18N.t('runtimes.scope.user') + '</option><option value="repo">' + I18N.t('runtimes.scope.repo') + '</option><option value="global">' + I18N.t('runtimes.scope.global') + '</option></select>' +
    '</div>' +
    // 仓库（条件显示）
    '<div id="repoKeyRow" style="display:none;margin-bottom:12px">' +
      '<label style="display:block;font-size:11px;font-weight:600;color:var(--t3);margin-bottom:5px;text-transform:uppercase;letter-spacing:.4px">' + I18N.t('runtimes.label.repo_key') + '</label>' +
      '<input id="credRepoKey" placeholder="owner/repo" style="width:100%;box-sizing:border-box;padding:9px 12px;border:1px solid var(--b);border-radius:8px;font-size:13px;background:var(--c);color:var(--t1)">' +
    '</div>' +
    // 按钮
    '<div style="display:flex;gap:8px;margin-top:16px">' +
      '<button class="bt bt-p" onclick="doCreateCredentialInPanel()" style="flex:1;justify-content:center">' + I18N.t('runtimes.action.generate') + '</button>' +
      '<button class="bt bt-o" onclick="_collapseAddDeviceCred()" style="padding:8px 18px">' + I18N.t('runtimes.action.cancel') + '</button>' +
    '</div>' +
    '</div>';
}

function doCreateCredentialInPanel() {
  var name = (document.getElementById('credName') || {}).value || '';
  if (!name) { app.showToast(I18N.t('runtimes.credential.toast_name_required')); return; }
  var scope = document.getElementById('credScope').value;
  var repo_key = (scope === 'repo' || scope === 'user') ? ((document.getElementById('credRepoKey') || {}).value || '').trim() : '';
  if (scope === 'repo' && !repo_key) { app.showToast(I18N.t('runtimes.credential.toast_repo_required')); return; }
  var area = document.getElementById('addDeviceCredArea');
  if (area) area.innerHTML = '<div style="padding:32px;text-align:center"><div style="display:inline-block;width:24px;height:24px;border:2.5px solid rgba(59,130,246,.2);border-top-color:#3b82f6;border-radius:50%;animation:spin .8s linear infinite;margin-bottom:12px"></div><div style="font-size:13px;color:var(--t3)">' + I18N.t('runtimes.toast.refreshing') + '</div><style>@keyframes spin{to{transform:rotate(360deg)}}</style></div>';
  API.post('/settings/runtime-credentials', { name, scope, repo_key }).then(d => {
    var token = d.token || '';
    var oneCmd = 'curl -fsSL ' + window.location.origin + '/api/runtime/install | bash -s -- --token ' + token + ' --server ' + window.location.origin;
    var runCmd = 'RUNTIME_TOKEN=' + token + ' python -m runtime.agent';
    if (area) area.innerHTML =
      '<div style="background:var(--bg);border:1px solid var(--b);border-radius:12px;padding:18px 20px">' +
        // 警告
        '<div style="display:flex;align-items:flex-start;gap:8px;padding:10px 12px;background:#fef3c7;border-radius:8px;margin-bottom:14px">' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#d97706" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;margin-top:1px"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
          '<span style="font-size:12px;color:#92400e;line-height:1.5">' + I18N.t('runtimes.credential.token_warning', {name: name, scope: _scopeLabel(scope)}) + '</span>' +
        '</div>' +
        // Token
        '<label style="display:block;font-size:11px;font-weight:600;color:var(--t3);margin-bottom:5px;text-transform:uppercase;letter-spacing:.4px">Token</label>' +
        '<code style="display:block;padding:10px 12px;background:var(--c);border:1px solid var(--b);border-radius:8px;word-break:break-all;font-size:12px;margin-bottom:14px">' + token + '</code>' +
        // 一键安装
        '<div style="font-size:12px;font-weight:600;color:var(--t1);margin-bottom:5px">' + I18N.t('runtimes.credential.install_hint') + '</div>' +
        '<code style="display:block;padding:10px 12px;background:var(--c);border:1px solid var(--b);border-radius:8px;word-break:break-all;font-size:11px;margin-bottom:6px">' + oneCmd + '</code>' +
        '<button class="bt bt-p" style="width:100%;justify-content:center;margin-bottom:12px" onclick="copyText(\'' + oneCmd.replace(/'/g, "\\'") + '\')">' + I18N.t('runtimes.action.copy_one_cmd') + '</button>' +
        '<div style="font-size:11px;color:var(--t2);margin-bottom:14px;line-height:1.5">' + I18N.t('runtimes.credential.install_desc') + '</div>' +
        // 手动启动
        '<div style="font-size:12px;font-weight:600;color:var(--t1);margin-bottom:5px">' + I18N.t('runtimes.credential.manual_start') + '</div>' +
        '<code style="display:block;padding:10px 12px;background:var(--c);border:1px solid var(--b);border-radius:8px;word-break:break-all;font-size:11px;margin-bottom:6px">' + runCmd + '</code>' +
        '<button class="bt bt-o" style="width:100%;justify-content:center" onclick="copyText(\'' + token + '\')">' + I18N.t('runtimes.action.copy_token') + '</button>' +
      '</div>';
    app.showToast(I18N.t('runtimes.credential.toast_generated'));
  }).catch(e => {
    app.showToast(I18N.t('runtimes.credential.toast_gen_failed', {msg: e.message}));
    _renderAddDeviceCredForm();
  });
}

function toggleAddDeviceDev() {
  var area = document.getElementById('addDeviceDevArea');
  var chevron = document.getElementById('devChevron');
  if (!area) return;
  if (area.style.display === 'none') {
    _collapseAddDeviceCred();
    if (!area.innerHTML.trim()) _renderAddDeviceDevPanel();
    area.style.display = 'block';
    requestAnimationFrame(function() {
      area.style.maxHeight = '800px';
      area.style.opacity = '1';
      area.style.marginTop = '12px';
    });
    if (chevron) chevron.style.transform = 'rotate(180deg)';
  } else {
    _collapseAddDeviceDev();
  }
}

function _collapseAddDeviceDev() {
  var area = document.getElementById('addDeviceDevArea');
  var chevron = document.getElementById('devChevron');
  if (!area || area.style.display === 'none') { if (chevron) chevron.style.transform = ''; _stopDevspaceAutoRefresh(); return; }
  area.style.maxHeight = '0';
  area.style.opacity = '0';
  area.style.marginTop = '0';
  if (chevron) chevron.style.transform = '';
  _stopDevspaceAutoRefresh();
  setTimeout(function() { if (area) area.style.display = 'none'; }, 350);
}

function _renderAddDeviceDevPanel() {
  var area = document.getElementById('addDeviceDevArea');
  if (!area) return;
  area.innerHTML =
    '<div style="border:1px solid var(--b);border-radius:12px;overflow:hidden">' +
    '<div style="padding:14px 18px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--b);background:linear-gradient(135deg,#60a5fa,#a78bfa);position:relative;overflow:hidden">' +
    '<div style="position:absolute;inset:0;background:radial-gradient(ellipse at top right,rgba(255,255,255,.2),transparent 60%);pointer-events:none"></div>' +
    '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="position:relative;flex-shrink:0"><path d="M17.5 19a4.5 4.5 0 1 0-1.41-8.78 7 7 0 1 0-13.09 3.78A4.5 4.5 0 0 0 6.5 19z"/></svg>' +
    '<span style="font-size:14px;font-weight:600;flex:1;color:#fff;position:relative">' + I18N.t('runtimes.devspace.title') + '</span>' +
    '<span id="aiDevspaceAutoTag" style="font-size:10px;color:rgba(255,255,255,.7);position:relative;display:none"></span>' +
    '<button style="background:rgba(255,255,255,.2);color:#fff;border:1px solid rgba(255,255,255,.25);border-radius:6px;padding:5px 12px;font-size:12px;font-weight:600;cursor:pointer;position:relative" onclick="_showCreateEnvForm()">' + I18N.t('runtimes.devspace.create') + '</button>' +
    '<button style="background:rgba(255,255,255,.12);color:#fff;border:1px solid rgba(255,255,255,.15);border-radius:6px;padding:5px 10px;font-size:14px;cursor:pointer;position:relative" onclick="_refreshDevspaceList()">&#8635;</button>' +
    '</div>' +
    '<div id="aiDevspaceBody" style="background:var(--bg)"><div style="padding:32px;text-align:center"><div style="display:inline-block;width:22px;height:22px;border:2.5px solid rgba(167,139,250,.2);border-top-color:#a78bfa;border-radius:50%;animation:spin .8s linear infinite;margin-bottom:10px"></div><div style="font-size:13px;color:var(--t3)">' + I18N.t('runtimes.devspace.loading') + '</div><style>@keyframes spin{to{transform:rotate(360deg)}}</style></div></div>' +
    '</div>';
  _refreshDevspaceList();
  _startDevspaceAutoRefresh();
}

function toggleRepoKey() {
  const el = document.getElementById('repoKeyRow');
  if (el) el.style.display = document.getElementById('credScope').value === 'repo' ? '' : 'none';
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

window._rtToggleType = function(t) {
  _rtFilterType = (_rtFilterType === t) ? '' : t;
  renderRuntimes(__runtimeData);
};
window._rtToggleScope = function(s) {
  _rtFilterScope = (_rtFilterScope === s) ? '' : s;
  renderRuntimes(__runtimeData);
};
window._rtClearFilters = function() {
  _rtFilterType = '';
  _rtFilterScope = '';
  _rtFilterScene = [];
  _rtFilterTag = [];
  _runtimeSearch = '';
  renderRuntimes(__runtimeData);
};
window._rtToggleMulti = function(kind, val) {
  if (kind === 'scene') {
    var i = _rtFilterScene.indexOf(val);
    if (i >= 0) _rtFilterScene.splice(i, 1); else _rtFilterScene.push(val);
    renderRuntimes(__runtimeData);
  } else if (kind === 'tag') {
    var i = _rtFilterTag.indexOf(val);
    if (i >= 0) _rtFilterTag.splice(i, 1); else _rtFilterTag.push(val);
    renderRuntimes(__runtimeData);
  }
};
window._toggleFilterDropdown = function(id) {
  var el = document.getElementById(id);
  if (!el) return;
  var open = el.style.display !== 'none';
  // 关闭所有下拉
  ['sceneDropdown', 'tagDropdown'].forEach(function(d) {
    var e = document.getElementById(d);
    if (e) e.style.display = 'none';
  });
  el.style.display = open ? 'none' : 'block';
};
// 点击外部关闭下拉
document.addEventListener('click', function(e) {
  if (!e.target || !e.target.closest || (!e.target.closest('#sceneDropdown') && !e.target.closest('#tagDropdown') && !e.target.matches('[onclick*="_toggleFilterDropdown"]'))) {
    ['sceneDropdown', 'tagDropdown'].forEach(function(d) {
      var el = document.getElementById(d);
      if (el) el.style.display = 'none';
    });
  }
});

function refreshRuntimes() {
  _stopDevspaceAutoRefresh();
  const el = document.getElementById('pageContent');
  if (el) el.innerHTML = `<div class="loading">${I18N.t('runtimes.toast.refreshing')}</div>`;
  API.post('/settings/agent-runtimes/refresh').then(() => {
    API.get('/settings/agent-runtimes').then(d => { if (window._currentView !== 'runtimes') return; renderRuntimes(d); }).catch(() => {});
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
        <span style="font-weight:700;font-size:18px">${rt.alias || rt.instance_name || rt.name}</span>
        ${rt.alias ? `<span style="font-size:12px;color:var(--t3)">(${rt.instance_name || rt.name})</span>` : ''}
        <button class="bt bt-o" style="font-size:11px;padding:3px 8px;border-radius:4px" onclick="editRuntimeAlias(${runtimeId})">${I18N.t('runtimes.action.edit_alias')}</button>
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
          <span style="font-weight:600;font-size:14px">${I18N.t('runtimes.label.scene_tags')}</span>
          <button class="bt bt-p" style="font-size:11px;padding:4px 10px;border-radius:4px" onclick="saveSceneTags(${runtimeId})">${I18N.t('runtimes.action.save')}</button>
        </div>
        <div id="sceneTagsContainer" style="display:flex;flex-wrap:wrap;gap:4px"></div>
        <div id="sceneTagsDesc" style="font-size:11px;color:var(--t3);margin-top:6px"></div>
      </div>
      <div style="border-top:1px solid var(--b);padding-top:16px;margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <span style="font-weight:600;font-size:14px">${I18N.t('runtimes.label.role_tags')}</span>
          <button class="bt bt-p" style="font-size:11px;padding:4px 10px;border-radius:4px" onclick="saveEngineTags(${runtimeId})">${I18N.t('runtimes.action.save_tags')}</button>
        </div>
        <div id="roleTagsContainer" style="display:flex;flex-wrap:wrap;gap:4px">${_engineTagCheckboxes(rt.agent_tags || [], rt.scene_tags || ['default'])}</div>
      </div>
      <div style="border-top:1px solid var(--b);padding-top:16px;margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <span style="font-weight:600;font-size:14px">${I18N.t('runtimes.label.max_concurrent')}</span>
          <button class="bt bt-p" style="font-size:11px;padding:4px 10px;border-radius:4px" onclick="saveMaxConcurrent(${runtimeId})">${I18N.t('runtimes.action.save')}</button>
        </div>
        <div style="display:flex;align-items:center;gap:10px">
          <input id="maxConcurrentInput" type="number" min="1" max="100" value="${rt.max_concurrent || 20}" style="width:80px;padding:6px 10px;border:1px solid var(--b);border-radius:8px;font-size:14px;background:var(--bg);color:var(--t1);outline:none" onfocus="this.style.borderColor='#0071e3'" onblur="this.style.borderColor='var(--b)'">
          <span style="font-size:12px;color:var(--t3)">${I18N.t('runtimes.label.max_concurrent_desc')}</span>
        </div>
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
  // 异步加载场景标签数据并渲染场景 checkbox + 角色标签联动
  _loadSceneTagsCache(function(scenes) {
    var stContainer = document.getElementById('sceneTagsContainer');
    if (stContainer) {
      stContainer.innerHTML = _renderSceneTagCheckboxes(rt.scene_tags || ['default']);
      _updateSceneTagsDesc();
    }
    // 重新渲染角色标签（带场景约束）
    var rtContainer = document.getElementById('roleTagsContainer');
    if (rtContainer) {
      rtContainer.innerHTML = _engineTagCheckboxes(rt.agent_tags || [], rt.scene_tags || ['default']);
    }
  });
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

// ===== Engine 场景标签 =====
const _SCENE_TAG_COLORS = {
  default:         '#10b981',  // 通用开发 — 翠绿
  'cloud-native':  '#3b82f6',  // 云原生开发 — 蓝
  iac:             '#f59e0b',  // IaC开发 — 琥珀
  'agent-dev':     '#8b5cf6',  // Agent开发 — 紫
  'skill-dev':     '#ec4899',  // 华为云Skill开发 — 粉
  'api-skill-dev': '#06b6d4',  // 通用Skill开发 — 青
  'blog-publish':  '#ef4444',  // 博客发布 — 红
};
var __sceneTagsCache = null;  // [{name, display_name, roles:[...]}, ...]

function _loadSceneTagsCache(cb) {
  if (__sceneTagsCache) { cb(__sceneTagsCache); return; }
  API.get('/runtime/scene-tags').then(function(d) {
    __sceneTagsCache = d.scenes || [];
    cb(__sceneTagsCache);
  }).catch(function() {
    __sceneTagsCache = [];
    cb(__sceneTagsCache);
  });
}

function _sceneDisplayName(name) {
  if (!__sceneTagsCache) return name;
  var s = __sceneTagsCache.find(function(x) { return x.name === name; });
  return s ? s.display_name : name;
}

function _sceneAvailableRoles(sceneTags) {
  // 合并所有选中场景的可用角色
  if (!__sceneTagsCache || !sceneTags || sceneTags.length === 0) return ['all','vod','issue','coding','qa','sre'];
  var roles = [];
  sceneTags.forEach(function(sn) {
    var s = __sceneTagsCache.find(function(x) { return x.name === sn; });
    if (s && s.roles) s.roles.forEach(function(r) { if (roles.indexOf(r) < 0) roles.push(r); });
  });
  return roles;
}

function _renderSceneTagCheckboxes(selectedTags) {
  if (!__sceneTagsCache) return '<span style="color:var(--t3);font-size:11px">' + I18N.t('runtimes.empty.loading') + '</span>';
  if (__sceneTagsCache.length === 0) return '<span style="color:var(--t3);font-size:11px">' + I18N.t('runtimes.empty.no_scenes') + '</span>';
  return __sceneTagsCache.map(function(s) {
    var checked = (selectedTags || []).indexOf(s.name) >= 0 ? 'checked' : '';
    var color = _SCENE_TAG_COLORS[s.name] || '#10b981';
    return '<label style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;padding:2px 6px">' +
      '<input type="checkbox" id="sceneTagCb_' + s.name + '" value="' + s.name + '" ' + checked +
      ' onchange="onSceneTagChange()" style="width:14px;height:14px;accent-color:' + color + '">' +
      '<span style="font-size:10px;padding:2px 8px;border-radius:999px;color:#fff;background:' + color + '">' + s.display_name + '</span>' +
      '</label>';
  }).join('');
}

function _updateSceneTagsDesc() {
  var desc = document.getElementById('sceneTagsDesc');
  if (!desc) return;
  var selected = [];
  if (__sceneTagsCache) {
    __sceneTagsCache.forEach(function(s) {
      var cb = document.getElementById('sceneTagCb_' + s.name);
      if (cb && cb.checked) selected.push(s);
    });
  }
  if (selected.length === 0) {
    desc.textContent = I18N.t('runtimes.label.scene_tags_empty');
    return;
  }
  var allRoles = [];
  selected.forEach(function(s) { (s.roles||[]).forEach(function(r) { if (allRoles.indexOf(r)<0) allRoles.push(r); }); });
  var roleLabels = allRoles.map(function(r) { return _rtTagLabel(r) || r; }).join(', ');
  desc.textContent = I18N.t('runtimes.label.scene_tags_roles', {roles: roleLabels});
}

function onSceneTagChange() {
  // 更新角色标签区域：只显示选中场景中包含的角色
  var selectedSceneNames = [];
  if (__sceneTagsCache) {
    __sceneTagsCache.forEach(function(s) {
      var cb = document.getElementById('sceneTagCb_' + s.name);
      if (cb && cb.checked) selectedSceneNames.push(s.name);
    });
  }
  _updateSceneTagsDesc();
  // 重新渲染角色标签 checkbox
  var container = document.getElementById('roleTagsContainer');
  if (!container) return;
  // 保留当前已选角色标签
  var currentTags = [];
  _RT_TAG_OPTIONS.forEach(function(t) {
    var cb = document.getElementById('engineTagCb_' + t);
    if (cb && cb.checked) currentTags.push(t);
  });
  container.innerHTML = _engineTagCheckboxes(currentTags, selectedSceneNames);
}

function saveSceneTags(runtimeId) {
  var tags = [];
  if (__sceneTagsCache) {
    __sceneTagsCache.forEach(function(s) {
      var cb = document.getElementById('sceneTagCb_' + s.name);
      if (cb && cb.checked) tags.push(s.name);
    });
  }
  if (tags.length === 0) {
    app.showToast(I18N.t('runtimes.toast.scene_tags_empty'));
    return;
  }
  API.put('/runtime/runtimes/' + runtimeId + '/scene-tags', {scene_tags: tags}).then(function(d) {
    var rt = (__runtimeData.runtimes || []).find(function(r) { return r.id === runtimeId; });
    if (rt) rt.scene_tags = d.scene_tags;
    if (__runtimeDevices) {
      for (var i = 0; i < __runtimeDevices.length; i++) {
        var r = (__runtimeDevices[i].runtimes || []).find(function(x) { return x.id === runtimeId; });
        if (r) r.scene_tags = d.scene_tags;
      }
    }
    app.showToast(I18N.t('runtimes.toast.scene_tags_saved'));
    // 重新渲染角色标签区域
    onSceneTagChange();
    if (__runtimeData) renderRuntimes(__runtimeData);
  }).catch(function(e) { app.showToast(I18N.t('runtimes.toast.save_failed', {msg: e.message})); });
}

// ===== 引擎别名 =====
function editRuntimeAlias(runtimeId) {
  var rt = (__runtimeData.runtimes || []).find(function(r) { return r.id === runtimeId; });
  var current = rt ? (rt.alias || '') : '';
  var name = prompt(I18N.t('runtimes.label.alias_prompt'), current);
  if (name === null) return;
  name = name.trim();
  API.put('/runtime/runtimes/' + runtimeId + '/alias', {alias: name}).then(function(d) {
    if (rt) rt.alias = d.alias;
    if (__runtimeDevices) {
      for (var i = 0; i < __runtimeDevices.length; i++) {
        var r = (__runtimeDevices[i].runtimes || []).find(function(x) { return x.id === runtimeId; });
        if (r) r.alias = d.alias;
      }
    }
    app.showToast(I18N.t('runtimes.toast.alias_saved'));
    if (__runtimeData) renderRuntimes(__runtimeData);
    // 更新详情面板标题
    var titleEl = document.querySelector('#engineDetailOverlay span[style*="font-weight:700"]');
    if (titleEl) {
      var parent = titleEl.parentElement;
      var nextSibling = titleEl.nextSibling;
      // 重建标题区域
      titleEl.textContent = d.alias || (rt ? (rt.instance_name || rt.name) : '');
      // 更新括号显示
      var parenSpan = parent.querySelector('span[style*="color:var(--t3)"][style*="font-size:12px"]');
      if (d.alias && rt) {
        if (!parenSpan) {
          var ns = document.createElement('span');
          ns.style.cssText = 'font-size:12px;color:var(--t3)';
          ns.textContent = '(' + (rt.instance_name || rt.name) + ')';
          titleEl.insertAdjacentElement('afterend', ns);
        } else {
          parenSpan.textContent = '(' + (rt.instance_name || rt.name) + ')';
        }
      } else if (parenSpan) {
        parenSpan.remove();
      }
    }
  }).catch(function(e) { app.showToast(I18N.t('runtimes.toast.save_failed', {msg: e.message})); });
}

// ===== Engine 角色标签 =====
const _RT_TAG_COLORS = {all: '#6366f1', vod: '#ec4899', issue: '#ef4444', coding: '#3b82f6', qa: '#a855f7', sre: '#f59e0b'};
function _rtTagLabel(t) { return t === 'all' ? I18N.t('runtimes.tag.all') : ({vod:'VOD',issue:'Issue',coding:'Coding',qa:'QA',sre:'SRE'})[t] || t; }
const _RT_TAG_OPTIONS = ['all', 'vod', 'issue', 'coding', 'qa', 'sre'];

function _engineTagCheckboxes(tags, sceneTags) {
  // sceneTags 约束可选角色：只显示选中场景中包含的角色 + all
  var allowedRoles = _sceneAvailableRoles(sceneTags);
  var options = sceneTags && sceneTags.length > 0
    ? _RT_TAG_OPTIONS.filter(function(t) { return t === 'all' || allowedRoles.indexOf(t) >= 0; })
    : _RT_TAG_OPTIONS;
  return options.map(function(t) {
    var checked = tags.includes(t) ? 'checked' : '';
    var desc = t === 'all' ? I18N.t('runtimes.tag.all_desc') : I18N.t('runtimes.tag.single_desc', {label: _rtTagLabel(t)});
    var disabled = (sceneTags && sceneTags.length > 0 && t !== 'all' && allowedRoles.indexOf(t) < 0) ? 'disabled style="opacity:.4;cursor:not-allowed"' : '';
    return '<label style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;padding:2px 6px">' +
      '<input type="checkbox" id="engineTagCb_' + t + '" value="' + t + '" ' + checked + ' onchange="onEngineTagChange(\'' + t + '\')" ' + disabled +
      ' style="width:14px;height:14px;accent-color:' + _RT_TAG_COLORS[t] + '">' +
      '<span style="font-size:10px;padding:2px 8px;border-radius:999px;color:#fff;background:' + _RT_TAG_COLORS[t] + '">' + _rtTagLabel(t) + '</span>' +
      '<span style="color:var(--t3);font-size:11px">' + desc + '</span>' +
      '</label>';
  }).join('');
}

function _engineTagSummary(tags) {
  if (!tags || tags.length === 0) return '<span style="color:var(--t3);font-size:10px">' + I18N.t('runtimes.empty.unassigned') + '</span>';
  return tags.map(t => `<span style="font-size:10px;padding:1px 6px;border-radius:999px;color:#fff;background:${_RT_TAG_COLORS[t]||'#999'};margin:0 2px">${_rtTagLabel(t)||t}</span>`).join('');
}

function _sceneTagSummary(tags) {
  if (!tags || tags.length === 0) return '<span style="color:var(--t3);font-size:10px">' + I18N.t('runtimes.empty.unassigned') + '</span>';
  return tags.map(function(t) {
    var dn = _sceneDisplayName(t);
    var color = _SCENE_TAG_COLORS[t] || '#10b981';
    return '<span style="font-size:10px;padding:1px 6px;border-radius:999px;color:#fff;background:' + color + ';margin:0 2px">' + dn + '</span>';
  }).join('');
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

function saveMaxConcurrent(runtimeId) {
  const input = document.getElementById('maxConcurrentInput');
  if (!input) return;
  const val = parseInt(input.value);
  if (!val || val < 1 || val > 100) {
    app.showToast(I18N.t('runtimes.toast.max_concurrent_invalid'));
    return;
  }
  API.put('/runtime/runtimes/' + runtimeId + '/max-concurrent', {max_concurrent: val}).then(d => {
    const rt = (__runtimeData.runtimes || []).find(r => r.id === runtimeId);
    if (rt) rt.max_concurrent = d.max_concurrent;
    app.showToast(I18N.t('runtimes.toast.max_concurrent_saved'));
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
  // 兼容入口：侧边栏打开时走侧边栏，否则走传统方式
  var sidebarArea = document.getElementById('addDeviceDevArea');
  if (sidebarArea) { toggleAddDeviceDev(); return; }
  // 传统入口（无侧边栏时直接在页面内展开）——保留向后兼容
  var el = document.getElementById('pageContent');
  if (!el) return;
  var existing = document.getElementById('aiDevspacePanel');
  if (existing) { existing.remove(); return; }
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
  setTimeout(function() {
    var p = document.getElementById('aiDevspacePanel');
    if (p) { p.style.maxHeight = '600px'; p.style.opacity = '1'; }
  }, 10);
  _refreshDevspaceList();
  _startDevspaceAutoRefresh();
}

function _closeDevspace() {
  // 侧边栏模式：折叠 devspace 区域
  var sidebarArea = document.getElementById('addDeviceDevArea');
  if (sidebarArea) { _collapseAddDeviceDev(); return; }
  // 传统模式
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
    // 侧边栏模式检查 addDeviceDevArea，传统模式检查 aiDevspacePanel
    var panel = document.getElementById('aiDevspacePanel') || document.getElementById('addDeviceDevArea');
    if (!panel) { _stopDevspaceAutoRefresh(); return; }
    var body = document.getElementById('aiDevspaceBody');
    if (!body || body.style.display === 'none' || !body.offsetParent) { _stopDevspaceAutoRefresh(); return; }
    _devspaceSilentRefresh();
    _devspaceAutoCountdown = _DEVSPACE_AUTO_INTERVAL;
  }, _DEVSPACE_AUTO_INTERVAL * 1000);
  // 倒计时显示
  _devspaceCountdownTimer = setInterval(function() {
    var panel = document.getElementById('aiDevspacePanel') || document.getElementById('addDeviceDevArea');
    if (!panel) { _stopDevspaceAutoRefresh(); return; }
    var tag = document.getElementById('aiDevspaceAutoTag');
    if (!tag) return;
    _devspaceAutoCountdown--;
    if (_devspaceAutoCountdown <= 0) _devspaceAutoCountdown = _DEVSPACE_AUTO_INTERVAL;
    tag.textContent = '15s auto';
    tag.style.display = '';
  }, 1000);
}

function _stopDevspaceAutoRefresh() {
  if (_devspaceAutoTimer) { clearInterval(_devspaceAutoTimer); _devspaceAutoTimer = null; }
  if (_devspaceCountdownTimer) { clearInterval(_devspaceCountdownTimer); _devspaceCountdownTimer = null; }
  var tag = document.getElementById('aiDevspaceAutoTag');
  if (tag) { tag.style.display = 'none'; tag.textContent = ''; }
}

// ====== Worker 上线进度跟踪 ======
// 后端返回 worker_online 字段表示对应 Worker 是否已真正上线（心跳<5min）。
// 环境处于"运行中"但 worker_online=false 时显示进度条，上线后自动消失。
// 进度条百分比基于"预计2-3分钟"做线性插值（仅用于视觉，不决定消失）。
var _devspaceRunningSince = {};  // { envId: timestamp_ms }

function _devspaceOnlineProgressHtml(envId, workerOnline, runningSinceMap) {
  if (workerOnline) return '';  // worker 已上线，不显示进度条
  var now = Date.now();
  if (!runningSinceMap[envId]) {
    runningSinceMap[envId] = now;
  }
  var elapsed = (now - runningSinceMap[envId]) / 1000;
  var DURATION = 180;  // 3 minutes — 仅用于进度条宽度估算
  var pct = Math.min(95, Math.round(elapsed / DURATION * 100));  // 封顶95%，只有真实上线才消失
  return '<div style="margin-top:6px;display:flex;align-items:center;gap:8px">' +
    '<div style="width:160px;height:6px;background:rgba(96,165,250,.12);border-radius:3px;overflow:hidden;flex-shrink:0">' +
    '<div style="width:' + pct + '%;height:100%;background:linear-gradient(90deg,#60a5fa,#a78bfa);border-radius:3px;animation:devspacePulse 2s ease-in-out infinite"></div>' +
    '</div>' +
    '</div>' +
    '<style>@keyframes devspacePulse{0%,100%{opacity:1}50%{opacity:.6}}</style>';
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
      if (!isRunning) delete _devspaceRunningSince[id];  // 非运行中时清除计时，下次开机重新开始
      html += '<div style="display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--b);transition:background .12s" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">' +
        '<div style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:' + dotColor + ';box-shadow:0 0 6px ' + dotColor + '50"></div>' +
        '<div style="flex:1;min-width:0">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">' +
        '<span style="font-size:13px;font-weight:600">' + (e.name || id) + '</span>' +
        _devStatusLabel(st) + '</div>' +
        '<div style="font-size:11px;color:var(--t3)">' + _specHtml(e) + ' · ' + (e.type || '') + ' · ' + ((e.image || {}).os || '') + '</div>' +
        (isRunning ? _devspaceOnlineProgressHtml(id, e.worker_online, _devspaceRunningSince) : '') + '</div>' +
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
      if (!isRunning) delete _devspaceRunningSince[id];
      html += '<div style="display:flex;align-items:center;gap:12px;padding:12px 20px;border-bottom:1px solid var(--b);transition:background .12s" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">' +
        '<div style="width:8px;height:8px;border-radius:50%;flex-shrink:0;background:' + dotColor + ';box-shadow:0 0 6px ' + dotColor + '50"></div>' +
        '<div style="flex:1;min-width:0">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">' +
        '<span style="font-size:13px;font-weight:600">' + (e.name || id) + '</span>' +
        _devStatusLabel(st) + '</div>' +
        '<div style="font-size:11px;color:var(--t3)">' + _specHtml(e) + ' · ' + (e.type || '') + ' · ' + ((e.image || {}).os || '') + '</div>' +
        (isRunning ? _devspaceOnlineProgressHtml(id, e.worker_online, _devspaceRunningSince) : '') + '</div>' +
        '<div style="display:flex;gap:4px;flex-shrink:0">' +
        ((isStopped || isReady) && !isBusy ? '<button class="bt bt-p bt-s" onclick="_devAction(\'start\',\'' + id + '\')">' + I18N.t('runtimes.devspace.start') + '</button>' : '') +
        (isRunning ? '<button class="bt bt-o bt-s" onclick="_devAction(\'stop\',\'' + id + '\')">' + I18N.t('runtimes.devspace.stop') + '</button>' : '') +
        (!(isBusy || isDeleted) ? '<button class="bt bt-r bt-s" onclick="_devAction(\'delete\',\'' + id + '\')">' + I18N.t('runtimes.devspace.delete') + '</button>' : '') +
        '</div></div>';
    }
    body.innerHTML = html;
  }).catch(function(e) {
    var msg = e.message || '';
    var isAuthErr = msg.indexOf('Unauthorized') >= 0 || msg.indexOf('APIGW.0301') >= 0;
    var isAgreementErr = msg.indexOf('83700031') >= 0 || msg.indexOf('not sign agreement') >= 0;
    body.innerHTML = '<div style="padding:32px 20px;text-align:center">' +
      '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom:10px;opacity:.5"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>' +
      '<div style="font-size:13px;color:var(--t2);margin-bottom:4px">' +
      (isAuthErr ? I18N.t('runtimes.devspace.configure_aksk') : isAgreementErr ? I18N.t('runtimes.devspace.agreement_not_signed') : I18N.t('runtimes.devspace.load_failed')) + '</div>' +
      '<div style="font-size:11px;color:var(--t3)">' +
      (isAuthErr ? I18N.t('runtimes.devspace.configure_aksk_hint') : isAgreementErr ? '' : msg) + '</div></div>';
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

  // 侧边栏模式：在 addDeviceDevArea 内显示创建中
  var devArea = document.getElementById('addDeviceDevArea');
  if (devArea) {
    var body = devArea.querySelector('#aiDevspaceBody') || devArea;
    body.innerHTML = '<div style="padding:32px;text-align:center;color:#a78bfa;font-size:13px">' +
      '<div style="display:inline-block;width:20px;height:20px;border:2px solid rgba(167,139,250,.2);border-top-color:#a78bfa;border-radius:50%;animation:spin .8s linear infinite;margin-bottom:10px"></div>' +
      '<div>' + I18N.t('runtimes.devspace.creating') + '</div><style>@keyframes spin{to{transform:rotate(360deg)}}</style></div>';
    _doCreateEnvWithRetry(envName, 0);
    return;
  }

  // 传统模式
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

    // Step 0: 预检模板可用性——模板未发布时不创建凭据，避免脏数据写入 worker 设备
    var GRAPE_TPL = '4ef59a2b4d3d4909aacd80f6007c00b2';
    API.get('/devstage/specs?template_id=' + GRAPE_TPL).then(function(specResp) {
      var specs = specResp.specs || [];
      if (!specs.length) {
        app.showToast(I18N.t('runtimes.devspace.template_not_published'));
        _refreshDevspaceList();
        return;
      }
      // 模板可用，继续创建凭据 + 环境
      _doCreateEnvWithCred(envName, credName, repoKey, grapeServer, baseName, attempt);
    }).catch(function(e) {
      var msg = e.message || '';
      // 账号未签署服务协议——精准提示
      if (msg.indexOf('83700031') >= 0 || msg.indexOf('not sign agreement') >= 0) {
        app.showToast(I18N.t('runtimes.devspace.agreement_not_signed'), 8000);
      } else {
        // 模板未发布（AK/SK 未加入白名单）：不创建凭据，直接提示
        app.showToast(I18N.t('runtimes.devspace.template_not_published'));
      }
      _refreshDevspaceList();
    });
  }

  function _doCreateEnvWithCred(envName, credName, repoKey, grapeServer, baseName, attempt) {
    // Step 1: 生成凭据（模板预检已通过）
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
        // 环境名冲突：删除凭据，换名重试（最多 _9）
        if (attempt < 9 && (msg.indexOf('98320005') >= 0 || msg.indexOf('already been used') >= 0)) {
          API.post('/settings/runtime-credentials/' + credId + '/revoke').then(function() {
            _doCreateEnvWithRetry(baseName, attempt + 1);
          }).catch(function() {
            _doCreateEnvWithRetry(baseName, attempt + 1);
          });
        } else if (msg.indexOf('98320022') >= 0 || msg.indexOf('count limit') >= 0) {
          API.post('/settings/runtime-credentials/' + credId + '/revoke');
          app.showToast(I18N.t('runtimes.devspace.count_limit'));
          _refreshDevspaceList();
        } else if (msg.indexOf('83700031') >= 0 || msg.indexOf('not sign agreement') >= 0) {
          // 账号未签署 CodeArts 开发环境服务协议
          API.post('/settings/runtime-credentials/' + credId + '/revoke');
          app.showToast(I18N.t('runtimes.devspace.agreement_not_signed'), 8000);
          _refreshDevspaceList();
        } else if (msg.indexOf('98320049') >= 0 || msg.indexOf('Template not published') >= 0) {
          // 模板未发布（预检与实际创建之间的竞态）：删除凭据，显示精准提示
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
      // 凭据名冲突（后端返回中文"凭据名称已存在"）：换名重试（最多 _9）
      if (attempt < 9 && (msg.indexOf('已存在') >= 0 || msg.indexOf('exist') >= 0)) {
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
