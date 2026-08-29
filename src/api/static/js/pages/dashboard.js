function renderDashboard(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const a = data.agents || [];
  const act = data.activity || [];
  const m = data.metrics || {};
  const repo = data.repo || '';
  const role = data.role || I18N.t('dashboard.member');
  const agentCounts = data.agent_event_counts || {};
  const scenes = data._scenes || [];
  const currentScene = data.dev_scene || 'default';
  const owner = data._owner || '';
  const name = data._name || '';
  const sceneOpts = scenes.map(s => `<option value="${s.id}"${s.id === currentScene ? ' selected' : ''}>${(function(){var k='dashboard.scene_'+s.id.replace(/-/g,'_');var v=I18N.t(k);return v!==k?v:s.name;})()}</option>`).join('');

  const agentOrder = ['vod','issue','coding','qa','sre'];
  const agentLabels = ['VOD Agent','Issue Agent','Coding Agent','QA Agent','SRE Agent'];
  const recentByAgent = data.agent_recent_events || {};

  let html = `<div class="cd" style="background:linear-gradient(135deg,#eef3ff,#eefdf2);border-color:#c6d9f7">
    <div class="flex fc gap12">
      <div style="font-size:26px">■</div>
      <div class="f1">
        <div style="font-size:14px;font-weight:600">${owner}/${name} <span class="tg tg-c" style="font-size:9px;vertical-align:middle">${role === 'Maintainer' ? I18N.t('dashboard.maintainer') : I18N.t('dashboard.member')}</span></div>
        <div class="ts t2">${role === 'Maintainer' ? I18N.t('dashboard.maintainer') : I18N.t('dashboard.member')} · ★ ${I18N.t('dashboard.main_branch')} · ${I18N.t('dashboard.webhook_ok')}</div>
      </div>
      <div class="stats">
        <div class="st"><div class="v" style="color:var(--g)">${m.auto_resolution != null ? m.auto_resolution + '%' : '--'}</div><div class="l">${I18N.t('dashboard.auto_resolution')}</div></div>
        <div class="st"><div class="v" style="color:var(--a)">${m.events_week != null ? m.events_week : '--'}</div><div class="l">${I18N.t('dashboard.events_week')}</div></div>
        <div class="st"><div class="v" style="color:var(--pu)">${m.avg_response != null ? m.avg_response + 's' : '--'}</div><div class="l">${I18N.t('dashboard.avg_response')}</div></div>
      </div>
    </div>
  </div>`;

  html += `<div class="cd">
    <div class="cd-h"><h2>${I18N.t('dashboard.workflow_title')}</h2>
      <div class="flex fc gap8">
        <span class="ts t2">${I18N.t('dashboard.dev_scene')}</span>
        <select id="sceneSelect" style="padding:4px 8px;border-radius:6px;border:1px solid var(--b);font-size:11px;background:var(--bg);color:var(--t);outline:none">${sceneOpts}</select>
        <button class="bt bt-p bt-s" onclick="applyScene('${owner}', '${name}')">${I18N.t('dashboard.apply')}</button>
      </div>
    </div>
    <div class="af">`;

  agentOrder.forEach((at, i) => {
    const ag = a.find(x => x.agent_type === at) || {};
    const meta = _AGENT_TYPES[at] || {};
    const st = ag.status || '';
    const hasSession = !!ag.status;
    const statusDot = st === 'failed' ? 'var(--r)' : st === 'running' ? 'var(--y)' : hasSession ? 'var(--g)' : '#ddd';
    const statusLabel = st === 'completed' ? I18N.t('dashboard.done') : st === 'running' ? I18N.t('dashboard.active') : st === 'failed' ? I18N.t('dashboard.failed') : hasSession ? I18N.t('dashboard.active') : '—';
    const backend = ag.backend || '';   // 无硬编码默认（engine-selection-design.md v2）；空 = 无 engine
    const backendHtml = backend
      ? `<span class="mv">${backend}</span>`
      : `<span class="mv" style="color:var(--r);font-weight:700">${I18N.t('dashboard.no_engine')}</span>`;
    const count = agentCounts[at] || 0;
    const nsText = count > 0 ? count + ' ' + I18N.t('dashboard.events_today') : hasSession ? (st === 'running' ? I18N.t('dashboard.processing') : I18N.t('dashboard.idle')) : '—';
    const ntCls = at === 'vod' ? 'tg-v' : at === 'issue' ? 'tg-i' : at === 'coding' ? 'tg-c' : at === 'qa' ? 'tg-q' : 'tg-s';
    const colors = {vod:'var(--pk)',issue:'var(--r)',coding:'var(--a)',qa:'var(--pu)',sre:'var(--or)'};
    const descs = {vod:I18N.t('dashboard.desc_vod'),issue:I18N.t('dashboard.desc_issue'),coding:I18N.t('dashboard.desc_coding'),qa:I18N.t('dashboard.desc_qa'),sre:I18N.t('dashboard.desc_sre')};
    const borderColor = meta.color || colors[at] || 'var(--t2)';
    const dotColor = meta.color || colors[at] || 'var(--t2)';
    const desc = descs[at] || meta.description || '';
    const enabled = ag.enable !== false;   // 默认启用；仓库级 enable

    // 该 Agent 最近 2 个事件（替代假数据）
    const evs = (recentByAgent[at] || []).slice(0, 2);
    let evHtml = '';
    if (evs.length > 0) {
      evHtml = evs.map(ev => {
        const stCls = ev.status === 'failed' ? 'tr' : ev.status === 'running' ? 'ty' : ev.status === 'completed' ? 'tgx' : 't2';
        const stTxt = ev.status === 'completed' ? I18N.t('dashboard.done') : ev.status === 'running' ? I18N.t('dashboard.in_progress') : ev.status === 'failed' ? I18N.t('dashboard.failed') : ev.status || '—';
        return `<div style="font-size:9px;padding:1px 0;border-bottom:1px dashed var(--b);display:flex;justify-content:space-between;gap:4px">
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${ev.summary || ev.title || ev.event_type}</span>
          <span class="${stCls}" style="flex-shrink:0">${stTxt}</span>
        </div>`;
      }).join('');
    } else {
      evHtml = '<div class="t2" style="font-size:9px">' + I18N.t('dashboard.no_events') + '</div>';
    }

    // Agent 启用开关（仅 Maintainer 可切换）
    const toggleBtn = `<button onclick="event.stopPropagation();toggleAgentEnable('${at}', ${!enabled})"
      style="cursor:pointer;border:1px solid var(--b);border-radius:10px;padding:1px 8px;font-size:9px;
             background:${enabled ? 'var(--g)' : 'var(--t3)'};color:#fff;margin-left:auto"
      title="${enabled ? I18N.t('dashboard.toggle_disable') : I18N.t('dashboard.toggle_enable')}">${enabled ? I18N.t('dashboard.toggle_on') : I18N.t('dashboard.toggle_off')}</button>`;

    html += `<div class="nd" style="border-top:3px solid ${borderColor};${enabled ? '' : 'opacity:0.55'}" onclick="app.nav('${at}')">
      <div class="nh"><span class="n" style="background:${dotColor}"></span><h4>${(function(){var k='agents.type.'+at+'.name';var v=I18N.t(k);return v!==k?v:(meta.display_name||agentLabels[i]);})()}</h4>${toggleBtn}</div>
      ${enabled ? `<div class="nds">${desc}</div>` : '<div class="t2" style="font-size:10px;margin:4px 0">' + I18N.t('dashboard.agent_disabled') + '</div>'}
      <div class="nm"><span class="ml">${I18N.t('dashboard.backend')}</span>${backendHtml}</div>
      <div class="ns"><span class="d" style="background:${statusDot}"></span>${nsText}</div>
      <div class="nt ${ntCls}">${evHtml}</div>
    </div>`;
  });

  // 开关交互
  window.toggleAgentEnable = function(agentType, enable) {
    const role = (data.role || '').toLowerCase();
    if (role !== 'maintainer' && role !== 'admin') { app.showToast(I18N.t('dashboard.only_maintainer_toggle')); return; }
    const repo = app.currentRepo();
    if (!repo) { app.showToast(I18N.t('dashboard.please_select_repo')); return; }
    const body = { enable: enable };
    API.put('/agents/' + agentType + '/' + repo.owner + '/' + repo.name, body).then(() => {
      app.showToast((enable ? I18N.t('dashboard.enabled_prefix') : I18N.t('dashboard.disabled_prefix')) + (agentLabels[agentOrder.indexOf(agentType)] || agentType));
      renderDashboard(data);   // 刷新卡片态（重新拉取）
      API.get('/dashboard/' + repo.owner + '/' + repo.name).then(renderDashboard).catch(() => {});
    }).catch(e => app.showToast(I18N.t('dashboard.toggle_failed', {msg: (e.detail?.message || e.message)})));
  };

  html += `</div>
    <div class="cl" onclick="app.nav('chat')">
      <div class="ci">☎</div>
      <div class="cx"><h4>${I18N.t('dashboard.chat_title')}</h4><p>${I18N.t('dashboard.chat_desc')}</p></div>
      <div class="flex gap6"><span class="tg tg-ch">↔ VOD</span><span class="tg tg-ch">↔ Issue</span><span class="tg tg-ch">↔ Coding</span><span class="tg tg-ch">↔ QA</span><span class="tg tg-ch">↔ SRE</span></div>
    </div>
  </div>`;

  window.openEngineModal = function(agentType) {
    const engines = (agentEngines[agentType] || []);
    const node = editNodes.find(n => n.id === agentType);
    if (!node) return;
    const currentIds = node.engine_runtime_ids || [];

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    const meta = _AGENT_TYPES[agentType] || {};
    const title = meta.display_name || agentType;

    const _ENG_TAG_COLORS = {all: '#6366f1', vod: '#ec4899', issue: '#ef4444', coding: '#3b82f6', qa: '#a855f7', sre: '#f59e0b'};
    const _onlineEngines = engines.filter(e => e.status === 'online');
    const _tagBadge = (tags) => (tags || ['all']).map(t => `<span style="font-size:9px;padding:2px 7px;border-radius:999px;color:#fff;background:${_ENG_TAG_COLORS[t]||'#999'};margin-left:4px;font-weight:500">${t === 'all' ? 'All' : t.charAt(0).toUpperCase()+t.slice(1)}</span>`).join('');

    overlay.innerHTML = `<div style="background:#fff;border-radius:18px;padding:28px;min-width:380px;max-width:500px;max-height:80vh;overflow-y:auto;box-shadow:0 12px 48px rgba(0,0,0,.15)">
      <h3 style="margin:0 0 4px;font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px">选择引擎 — ${title}</h3>
      <div style="font-size:12px;color:#86868b;margin-bottom:20px">${_onlineEngines.length} 个可用引擎 · 不选 = 广播竞争</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        ${_onlineEngines.length === 0 ? '<div style="padding:20px 0;text-align:center;color:#86868b;font-size:13px">无可用引擎，将使用广播</div>' : _onlineEngines.map(e => {
          const checked = currentIds.includes(e.id);
          return `<label style="display:flex;align-items:center;gap:12px;padding:12px 16px;border:1px solid ${checked ? '#0071e3' : '#e8e8ed'};border-radius:12px;cursor:pointer;background:${checked ? 'rgba(0,113,227,.04)' : '#f5f5f7'};transition:all .15s">
            <input type="checkbox" data-eid="${e.id}" ${checked ? 'checked' : ''} style="width:18px;height:18px;cursor:pointer;accent-color:#0071e3">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:13px;color:#1d1d1f;display:flex;align-items:center">${e.name}${_tagBadge(e.tags)}</div>
              <div style="font-size:10px;color:#86868b;margin-top:3px">${e.device || ''}${e.cred_name ? ' · ' + e.cred_name : ''}</div>
            </div>
            <span style="font-size:11px;font-weight:500;color:#34c759">● 在线</span>
          </label>`;
        }).join('')}
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:24px">
        <button class="ap-btn ap-btn-o" onclick="this.closest('[style*=fixed]').remove()">取消</button>
        <button class="ap-btn ap-btn-p" id="confirmEngineBtn">确认</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);

    document.getElementById('confirmEngineBtn').onclick = function() {
      const checks = overlay.querySelectorAll('input[type=checkbox][data-eid]');
      const newIds = [];
      checks.forEach(c => { if (c.checked) newIds.push(parseInt(c.dataset.eid)); });
      node.engine_runtime_ids = newIds;
      overlay.remove();
      const cardsEl = document.getElementById('agentCards');
      cardsEl.innerHTML = renderAgentCards(a, agentOrder, agentLabels, colors, descs, agentEngines, isCustomScene, editMode, editNodes, engineMode);
    };

  if (act.length > 0) {
    html += `<div class="cd"><div class="cd-h"><h2>${I18N.t('dashboard.recent_activity')}</h2><span class="ts t3">${owner}/${name} · ${I18N.t('dashboard.last_24h')}</span></div>`;
    act.forEach(ev => {
      const colorClass = ev.agent_type === 'vod' ? 'tg-v' : ev.agent_type === 'issue' ? 'tg-i' : ev.agent_type === 'coding' ? 'tg-c' : ev.agent_type === 'qa' ? 'tg-q' : ev.agent_type === 'sre' ? 'tg-s' : 'tg-d';
      html += `<div class="ai" onclick="app.nav('event',${ev.seq})" style="cursor:pointer">
        <div class="t">${fmtLocalTime(ev.created_at).substring(11,16) || '--:--'}</div>
        <div class="ac"><span class="tg ${colorClass}">${(ev.agent_type || '?').toUpperCase().substring(0,4)}</span><b>${ev.summary || ev.title}</b> · <span class="t2">${ev.event_type}</span></div>
      </div>`;
    });
    html += '</div>';
  }

  el.innerHTML = html;
}
