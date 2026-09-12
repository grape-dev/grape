let _agentCfgVisible = false;
let _skillScope = 'official';

function _switchSkillScope(scope) {
  _skillScope = scope;
  loadCfgOptions();
}

function toggleAgentCfg(showCfg) {
  // showCfg=true → Configure 视图（蓝），false → Events 视图（蓝）
  if (showCfg === undefined) showCfg = !_agentCfgVisible;
  _agentCfgVisible = showCfg;
  const p = document.getElementById('agentCfgPanel');
  if (p) p.classList.toggle('hidden', !showCfg);
  const btnCfg = document.getElementById('btnConfigure');
  const btnEvt = document.getElementById('btnEvents');
  if (showCfg) {
    loadCfgOptions();
    if (btnCfg) btnCfg.className = 'bt bt-p bt-s';
    if (btnEvt) btnEvt.className = 'bt bt-o bt-s';
  } else {
    if (btnCfg) btnCfg.className = 'bt bt-o bt-s';
    if (btnEvt) btnEvt.className = 'bt bt-p bt-s';
  }
}

// ===== Backend Engines 下拉多选（引擎池，agent-cluster-design.md §4.1）=====
const _BE_COLORS = ['#4f8cff', '#22b573', '#a05ce6', '#f0a020', '#e65c8c', '#12a8c8', '#8a7cf0', '#d4752a'];
const _beColor = idx => _BE_COLORS[idx % _BE_COLORS.length];

function _beRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return `${(n >> 16) & 255},${(n >> 8) & 255},${n & 255}`;
}

function renderBackendsTrigger() {
  const box = document.querySelector('.cfg-backends');
  if (!box) return;
  const trigger = box.querySelector('.cfg-backends-trigger');
  const ph = box.querySelector('.cfg-backends-ph');
  const checked = Array.from(box.querySelectorAll('.cfg-backend-cb:checked'));
  if (ph) ph.style.display = checked.length ? 'none' : '';
  // 选中项渲染为彩色标签（不同颜色标识）
  checked.forEach(cb => {
    const color = cb.dataset.color || '#4f8cff';
    const key = 'be-chip-' + cb.value.replace(/[^a-z0-9]/gi, '-');
    if (trigger.querySelector('.' + key)) return;
    const chip = document.createElement('span');
    chip.className = key;
    chip.textContent = cb.value;
    chip.style.cssText = `display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:600;color:#fff;background:${color};line-height:18px`;
    trigger.appendChild(chip);
  });
  // 移除已取消勾选的标签
  const vals = new Set(checked.map(cb => cb.value));
  trigger.querySelectorAll('[class^="be-chip-"]').forEach(ch => {
    const v = ch.textContent;
    if (!vals.has(v)) ch.remove();
  });
}

function toggleBackendsDropdown(ev) {
  if (ev) ev.stopPropagation();
  const box = document.querySelector('.cfg-backends');
  if (!box) return;
  const panel = box.querySelector('.cfg-backends-panel');
  if (panel) panel.classList.toggle('hidden');
}

function closeBackendsDropdown() {
  const box = document.querySelector('.cfg-backends');
  if (!box) return;
  const panel = box.querySelector('.cfg-backends-panel');
  if (panel) panel.classList.add('hidden');
}

function refreshBackendsPanel() {
  // 触发区彩色标签 + 面板行选中高亮同步（勾选/取消实时更新）
  renderBackendsTrigger();
  const panel = document.querySelector('.cfg-backends-panel');
  if (!panel) return;
  panel.querySelectorAll('label').forEach(l => {
    const cb = l.querySelector('.cfg-backend-cb');
    if (!cb) return;
    const color = cb.dataset.color || '#4f8cff';
    const rgb = _beRgb(color);
    const on = cb.checked;
    l.style.background = on ? `rgba(${rgb},.12)` : 'transparent';
    l.style.boxShadow = on ? `inset 2px 0 0 ${color}` : '';
    const nameSpan = l.querySelectorAll('span')[1];  // 色点之后为引擎名
    if (nameSpan) {
      nameSpan.style.fontWeight = on ? 600 : 400;
      nameSpan.style.color = on ? color : 'var(--t2)';
    }
  });
}

function loadCfgOptions() {
  const backendBox = document.querySelector('.cfg-backends');
  const skillSelect = document.querySelector('.cfg-skill');

  // Runtime 下拉多选（引擎集合）：仅未加载时加载；DevOps Agent 可选 仓库级/全局/本机直连（个人引擎不出现）
  if (backendBox) {
    // 每次打开都重新加载（标签可能已变更，不复用缓存）
    backendBox.dataset.loaded = '';
    const panel = backendBox.querySelector('.cfg-backends-panel');
    const current = (backendBox.dataset.backends || '').split(',').map(s => s.trim()).filter(Boolean);
    API.get('/settings/agent-runtimes').then(d => {
      const runtimes = d.runtimes || [];
      const scopeLabel = sc => sc === 'global' ? I18N.t('agents.scope.global') : sc === 'repo' ? I18N.t('agents.scope.repo') : I18N.t('agents.scope.personal');
      const localLabel = r => r.is_local ? I18N.t('agents.scope.local_direct') : scopeLabel(r.scope);
      const agentType = backendBox.dataset.agent || '';
      // 按标签过滤：展示仓库级/全局/个人引擎（个人引擎仅 owner 自己可见，后端已过滤）
      const ok = runtimes.filter(r => {
        if (r.scope !== 'repo' && r.scope !== 'global' && r.scope !== 'user') return false;
        const tags = r.agent_tags || [];
        return tags.includes('all') || tags.includes(agentType);
      });
      const tagLabels = {all: I18N.t('agents.tag.all'), vod: 'VOD', issue: 'Issue', coding: 'Coding', qa: 'QA', sre: 'SRE'};
      const val = r => r.instance_name || r.name;
      const cb = (r, i) => {
        const v = val(r);
        const online = r.health === 'online' || r.healthy;
        const color = _beColor(i);
        const checked = current.includes(v) || current.includes(r.name) ? 'checked' : '';
        const rgb = _beRgb(color);
        const tagStr = (r.agent_tags || []).map(t => tagLabels[t]||t).join('/');
        return `<label style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;font-size:12px;cursor:pointer;user-select:none;white-space:nowrap;width:100%;flex-shrink:1;background:${checked ? `rgba(${rgb},.12)` : 'transparent'};${checked ? `box-shadow:inset 2px 0 0 ${color}` : ''};transition:background .12s">
          <input type="checkbox" class="cfg-backend-cb" value="${v}" data-color="${color}" ${checked} style="accent-color:${color};flex:0 0 auto;width:14px;height:14px;margin:0">
          <span style="width:9px;height:9px;border-radius:50%;background:${color};flex-shrink:0"></span>
          <span style="font-weight:600;color:${checked ? color : 'var(--t2)'};min-width:110px">${v}</span>
          <span class="ts t3" style="flex:1;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.name} v${r.version || ''} · ${r.worker_name || ''} · ${localLabel(r)}${online ? ' ✓' : ' ✗'} · ${tagStr}</span>
        </label>`;
      };
      panel.innerHTML = ok.map(cb).join('') || '<span class="ts t2">' + I18N.t('agents.engine.none_available') + '</span>';
      // 面板内点击不冒泡到 document（避免勾选时触发外部关闭）
      panel.addEventListener('click', e => e.stopPropagation());
      // 初始回显 + 勾选/取消 → 同步触发区彩色标签 + 面板行高亮
      refreshBackendsPanel();
      panel.addEventListener('change', refreshBackendsPanel);
    }).catch(() => {
      panel.innerHTML = '<span class="ts t2">' + I18N.t('agents.engine.load_failed') + '</span>';
    });
  }

  // Skill 下拉：按 scope 过滤加载
  if (skillSelect) {
    let repo = '';
    try {
      const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
      repo = c ? (c.fullName || '') : '';
    } catch {}
    const scope = _skillScope || 'official';
    let qs = '?scope=' + scope;
    if (repo) qs += '&repo=' + encodeURIComponent(repo);
    API.get('/skills' + qs).then(d => {
      const allSkills = d.skills || [];
      const agentType = skillSelect.dataset.agent || '';
      const currentSkillId = skillSelect.dataset.skillId || '0';
      const filtered = allSkills.filter(s => s.agent_type === agentType || s.parent_agent_type === agentType);
      // 当前配置的 skill 在当前 scope 里 → 选中它；否则默认选第一个
      const matchCurrent = filtered.find(s => String(s.id) === currentSkillId);
      // "默认官方技能（自动）"仅在全局 scope 下显示
      const defaultOpt = scope === 'official' ? '<option value="0">' + I18N.t('agents.skill.default_official') + '</option>' : '';
      let html = defaultOpt +
        filtered.map((s, i) => {
          const selected = matchCurrent ? (String(s.id) === currentSkillId) : (i === 0);
          return `<option value="${s.id}" ${selected ? 'selected' : ''}>${s.display_name || s.name}</option>`;
        }).join('');
      // 项目/个人 scope 下无 skill → 显示空提示
      if (filtered.length === 0) {
        html = `<option value="0" disabled>${scope === 'repo' ? I18N.t('agents.skill.no_repo_skill') : I18N.t('agents.skill.no_personal_skill')}</option>`;
      }
      skillSelect.innerHTML = html;
    }).catch(() => {});
  }
}

function _agentTypeDisplay(type, meta) {
  var tKey = 'agents.type.' + type + '.name';
  var tVal = I18N.t(tKey);
  return tVal !== tKey ? tVal : (meta.display_name || type);
}
function _agentTypeDesc(type, meta) {
  var tKey = 'agents.type.' + type + '.desc';
  var tVal = I18N.t(tKey);
  return tVal !== tKey ? tVal : (meta.description || '');
}

function renderAgent(name, data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const meta = _AGENT_TYPES[name] || {display_name:name,icon:'?',color:'var(--t2)',bg:'var(--bg)',description:''};
  const m = data.metrics || {};
  const es = m.event_stats || {};
  const events = data.recent_events || [];
  const highlightId = data.highlight_issue_id || '';
  const _normIss = s => String(s || '').replace(/^#/, '');
  const evtHtml = events.length > 0 ? `
    <table style="width:100%;font-size:12px;table-layout:fixed">
      <thead><tr>
        <th style="width:60px;text-align:left;padding:6px 10px;font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px">${I18N.t('agents.event_table_seq')}</th>
        <th style="width:130px;text-align:left;padding:6px 10px;font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px">${I18N.t('agents.event.table_time')}</th>
        <th style="width:auto;text-align:left;padding:6px 10px;font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px">${I18N.t('agents.event.table_content')}</th>
        <th style="width:75px;text-align:left;padding:6px 10px;font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px">Issue</th>
        <th style="width:85px;text-align:left;padding:6px 10px;font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px">${I18N.t('agents.event.table_actor')}</th>
        <th style="width:60px;text-align:center;padding:6px 10px;font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px">${I18N.t('agents.event.table_duration')}</th>
        <th style="width:160px;text-align:left;padding:6px 10px;font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px">${I18N.t('agents.event.table_engine')}</th>
        <th style="width:70px;text-align:right;padding:6px 10px;font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.5px">${I18N.t('agents.event.table_status')}</th>
      </tr></thead>
      <tbody>
        ${events.map(ev => {
          const _issNum = String(ev.issue_id || '').replace(/^#/, '');
          const _issLink = (_issNum && ev.repo_key)
            ? `<a href="${_vcsUrl(ev.repo_key, '/issues/' + _issNum)}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline" onclick="event.stopPropagation()">${ev.issue_id}</a>`
            : ev.issue_id || '—';
          const _isHl = highlightId && _normIss(ev.issue_id) === _normIss(highlightId);
          return `<tr class="pipe-row${_isHl ? ' highlight-flash' : ''}" onclick="app.nav('event',${ev.seq})" style="cursor:pointer">
          <td style="padding:8px 10px;font-size:11px;font-weight:600;color:var(--t2)">#${ev.seq || '?'}</td>
          <td style="padding:8px 10px;white-space:nowrap;font-size:10px;color:var(--t3)">${fmtLocalTime(ev.created_at).substring(5,16) || '--:--'}</td>
          <td style="padding:8px 10px;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px">${ev.summary || ev.title || ev.event_type}</td>
          <td style="padding:8px 10px;font-size:11px">${_issLink}</td>
          <td style="padding:8px 10px;font-size:10px;color:var(--t2)">${ev.actor ? '@'+ev.actor : '—'}</td>
          <td style="padding:8px 10px;text-align:center;font-size:10px;color:var(--t3)">${ev.duration ? ev.duration + 's' : '—'}</td>
          <td style="padding:8px 10px;font-size:10px;color:var(--t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${ev.backend || '—'}</td>
          <td style="padding:8px 10px;text-align:right"><span class="ts ${ev.status === 'failed' ? 'tr' : ev.status === 'completed' ? 'tgx' : 'ty'}" style="font-size:10px;font-weight:600">${I18N.t('common.status.' + ev.status) !== ('common.status.' + ev.status) ? I18N.t('common.status.' + ev.status) : ev.status}</span></td>
        </tr>`;
        }).join('')}
      </tbody>
    </table>` : '<div class="tx t2" style="padding:12px 0">' + I18N.t('agents.event.none') + '</div>';

  const ef = es.completed || 0;
  const er = es.running || 0;
  const et = m.total_events || 0;

  const engineText = data.backend || (data.backends && data.backends.length ? data.backends.join(', ') : '');
  const _ENGINE_TYPE_DOT = { opencode: '#3b82f6', hermes: '#8b5cf6', codearts: '#f59e0b', codex: '#10a37f' };
  const _engineTypeOf = n => { const l = (n || '').toLowerCase(); if (l.includes('hermes')) return 'hermes'; if (l.includes('opencode')) return 'opencode'; if (l.includes('codearts')) return 'codearts'; if (l.includes('codex')) return 'codex'; return ''; };
  const _engineDot = _ENGINE_TYPE_DOT[_engineTypeOf(engineText)] || '';
  const engineBadge = engineText
    ? `<span class="ts t3" style="font-weight:600;display:inline-flex;align-items:center;gap:5px">${_engineDot ? `<span style="width:7px;height:7px;border-radius:50%;background:${_engineDot};flex-shrink:0"></span>` : ''}${I18N.t('agents.engine.label', {text: engineText})}</span>`
    : `<span class="ts t3" style="color:var(--r);font-weight:700">${I18N.t('agents.engine.no_engine')}</span>`;

  let html = `
<div class="cd" style="background:${meta.bg || 'var(--bg)'};border-color:${meta.color || 'var(--b)'}33">
  <div class="flex fc gap12">
    <div style="width:44px;height:44px;border-radius:10px;background:${meta.color || 'var(--b)'}22;display:flex;align-items:center;justify-content:center;font-size:20px">${meta.icon || '?'}</div>
    <div class="f1"><h2 style="font-size:16px;font-weight:600;margin-bottom:2px">${_agentTypeDisplay(name, meta)}</h2><div class="ts t2">${_agentTypeDesc(name, meta)}</div></div>
  </div>
</div>
<div class="stats mt16 mb16">
  <div class="st"><div class="v" style="color:${meta.color || 'var(--t2)'}">${et}</div><div class="l">${I18N.t('agents.event.total')}</div></div>
  <div class="st"><div class="v" style="color:${meta.color || 'var(--t2)'}">${ef}</div><div class="l">${I18N.t('agents.event.completed')}</div></div>
  <div class="st"><div class="v" style="color:${meta.color || 'var(--t2)'}">${er}</div><div class="l">${I18N.t('agents.event.running')}</div></div>
</div>
<div class="cd"><div class="cd-h"><h2>${I18N.t('agents.event.history_title')}</h2><span class="ts t3">${I18N.t('agents.event.recent_n', {n: events.length})}</span></div>${evtHtml}</div>`;

  el.innerHTML = html;
  // Issue #108: 滚动定位到第一个高亮事件行
  if (highlightId) {
    const _firstHl = el.querySelector('.highlight-flash');
    if (_firstHl) {
      setTimeout(function() { _firstHl.scrollIntoView({ behavior: 'smooth', block: 'center' }); }, 120);
    }
  }
  // 根据已配置 skill 的 scope 设置初始页签（避免回显时看不到已配置的 skill）
  _skillScope = (data.skill_scope === 'repo') ? 'repo' : (data.skill_scope === 'user') ? 'user' : 'official';
  const scopeSel = document.querySelector('.cfg-skill-scope');
  if (scopeSel) scopeSel.value = _skillScope;
  // 预加载配置下拉（即使面板隐藏），确保首次打开 Configure 立即显示 skill/backend，不显示"加载中..."
  loadCfgOptions();
  // 点击面板外部关闭引擎下拉（只注册一次）
  if (!document._beOutsideCloseBound) {
    document._beOutsideCloseBound = true;
    document.addEventListener('click', closeBackendsDropdown);
  }
}

function _agentSubsSection(name, data) {
  const subs = data.subscribe_events || [];
  const prMerged = subs.filter(s => s && s.event_type === 'pull_request_merged');
  const prBranches = prMerged.map(s => (s.branch || '')).filter(b => b).join(', ');
  const prChecked = prMerged.length > 0 ? 'checked' : '';
  const displayName = (_AGENT_TYPES[name] && _AGENT_TYPES[name].display_name) || name;
  return `<div style="margin:16px 0 12px;border-top:1px solid var(--b);padding-top:12px">
    <div class="flex fc jbtw" style="margin-bottom:8px">
      <h3 style="font-size:13px;font-weight:600">${I18N.t('agents.subscription.title')}</h3>
      <span class="ts t3">${I18N.t('agents.subscription.hint')}</span>
    </div>
    <div style="font-size:12px;margin-bottom:6px">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input id="subPrMerged" type="checkbox" ${prChecked} style="accent-color:var(--a)"> ${I18N.t('agents.subscription.pr_merged')}
      </label>
    </div>
    <div class="srow"><label>${I18N.t('agents.subscription.merge_branch')}</label><input id="subPrBranches" value="${prBranches}" placeholder="${I18N.t('agents.subscription.merge_branch_placeholder')}" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg);outline:none"></div>
    <div class="ts t3" style="margin-top:6px">${I18N.t('agents.subscription.branch_hint', {name: displayName})}</div>
  </div>`;
}

function saveAgentConfig(name) {
  const backends = Array.from(document.querySelectorAll('.cfg-backend-cb:checked')).map(cb => cb.value);
  const skillVal = document.querySelector('.cfg-skill')?.value || '0';
  const skillId = parseInt(skillVal) || 0;
  // 项目/个人 scope 下不允许保存空值（无"默认技能"选项）
  if (_skillScope !== 'official' && !skillId) {
    if (app && app.showToast) app.showToast(I18N.t('agents.skill.select_required'));
    return;
  }
  const repo = app.currentRepo();
  if (!repo) return;
  const body = {backends, skill_id: skillId};
  // 事件订阅：PR 合入（勾选）+ 合入分支（逗号分隔，留空=不限）；仅 SRE Agent 开放
  const subEvents = [];
  if (name === 'sre') {
    const prCb = document.getElementById('subPrMerged');
    if (prCb && prCb.checked) {
      const rawBranches = (document.getElementById('subPrBranches')?.value || '').replace(/，/g, ',');
      subEvents.push({event_type: 'pull_request_merged', branch: rawBranches.trim()});
    }
  }
  body.subscribe_events = subEvents;
  if (name === 'vod') {
    const appId = document.getElementById('vodImAppId')?.value || '';
    const rawSecret = document.getElementById('vodImSecret')?.value || '';
    body.im_app_id = appId;
    body.im_app_secret = rawSecret === '********' ? '' : rawSecret;
  }
  API.put('/agents/' + name + '/' + repo.owner + '/' + repo.name, body).then(r => {
    app.showToast(I18N.t('agents.config.saved'));
    toggleAgentCfg(false);
  }).catch(e => app.showToast(I18N.t('agents.config.save_failed', {msg: e.message})));
}

function cleanAgentSession(name) {
  if (!confirm(I18N.t('agents.session.clean_confirm', {name: name}))) return;
  const repo = app.currentRepo();
  if (!repo) return;
  API.post('/agents/' + name + '/' + repo.owner + '/' + repo.name + '/clean').then(r => {
    app.showToast(r.message || I18N.t('agents.session.cleaned'));
  }).catch(e => app.showToast(I18N.t('agents.session.clean_failed', {msg: e.message})));
}

function startVodOnboard() {
  const repo = app.currentRepo();
  if (!repo) { app.showToast(I18N.t('agents.toast.select_repo_first')); return; }
  startFeishuOnboard('vod_bot', repo.fullName);
}

function refreshVodOnboard() {
  // VOD 扫码 onboard 完成后刷新 VOD Agent 页的 App ID/Secret 字段（对齐 chat 页 loadSideConfig，
  // 见 feishu-onboard 差异分析 2026-08-17）。agents.js 全局函数，由 chat.js closeOnboardModal 分发调用。
  const repo = app.currentRepo();
  if (!repo) return;
  API.get('/agents/vod/' + repo.owner + '/' + repo.name).then(d => {
    const im = d.im || {};
    const appIdEl = document.getElementById('vodImAppId');
    if (appIdEl) appIdEl.value = im.im_app_id || '';
    const secretEl = document.getElementById('vodImSecret');
    if (secretEl) secretEl.value = im.connected ? '********' : '';
  }).catch(() => {});
}