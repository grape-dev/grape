function renderDashboard(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const a = data.agents || [];
  const currentSceneNodes = data.scene_nodes || [];
  const repo = data.repo || '';
  const role = data.role || 'Member';
  const scenes = data._scenes || [];
  const currentScene = data.dev_scene || 'default';
  let owner = '', name = '';
  try {
    const cr = app.currentRepo();
    if (cr) { owner = cr.owner; name = cr.name; }
  } catch(e) {}
  if (!owner || !name) {
    const parts = repo.replace(/^gitcode:/, '').split('/');
    owner = parts[0] || '';
    name = parts[1] || '';
  }
  const agentEngines = data.agent_engines || {};
  const m = data.metrics || {};

  const sceneOpts = [
    ...scenes.map(s => `<option value="${s.id}"${s.id === currentScene ? ' selected' : ''}>${s.name}</option>`),
    `<option value="__create__">➕ ${I18N.t('dashboard.create_custom_scene')}</option>`,
  ].join('');

  const agentOrder = ['vod','issue','coding','qa','sre'];
  const agentLabels = ['VOD Agent','Issue Agent','Coding Agent','QA Agent','SRE Agent'];
  const colors = {vod:'#ff2d92',issue:'#ff3b30',coding:'#0071e3',qa:'#af52de',sre:'#ff9f0a'};
  const descs = {vod:I18N.t('dashboard.desc_vod'),issue:I18N.t('dashboard.desc_issue'),coding:I18N.t('dashboard.desc_coding'),qa:I18N.t('dashboard.desc_qa'),sre:I18N.t('dashboard.desc_sre')};

  const currentSceneObj = scenes.find(s => s.id === currentScene);
  const isCustomScene = currentSceneObj && currentSceneObj.scope !== 'official';
  let editMode = false;
  let editNodes = [];

  // ── Apple-style scoped CSS ──
  let html = `<style>
    .ap-hero{background:#fff;border:1px solid #d2d2d7;border-radius:18px;padding:28px 32px;margin-bottom:16px;box-shadow:0 2px 12px rgba(0,0,0,.06)}
    .ap-hero-row{display:flex;align-items:center;gap:16px}
    .ap-hero-icon{width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,#0071e3,#5ac8fa);display:flex;align-items:center;justify-content:center;color:#fff;font-size:22px;flex-shrink:0}
    .ap-hero-title{font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px}
    .ap-hero-sub{font-size:12px;color:#86868b;margin-top:3px}
    .ap-stats{display:flex;gap:8px;margin-left:auto}
    .ap-stat{text-align:center;padding:8px 18px;background:#f5f5f7;border-radius:12px;min-width:64px}
    .ap-stat .v{font-size:20px;font-weight:700;line-height:1.1}
    .ap-stat .l{font-size:10px;color:#86868b;margin-top:2px;letter-spacing:.3px}

    .ap-card{background:#fff;border:1px solid #d2d2d7;border-radius:18px;padding:0;margin-bottom:16px;box-shadow:0 2px 12px rgba(0,0,0,.06);overflow:hidden}
    .ap-card-hdr{display:flex;align-items:center;justify-content:space-between;padding:20px 24px 14px}
    .ap-card-hdr h2{font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px}
    .ap-bar{display:flex;align-items:center;gap:8px}
    .ap-sel{padding:6px 12px;border-radius:10px;border:1px solid #d2d2d7;font-size:12px;background:#f5f5f7;color:#1d1d1f;outline:none;cursor:pointer}
    .ap-sel:focus{border-color:#0071e3}
    .ap-btn{padding:6px 14px;border-radius:10px;font-size:12px;font-weight:500;cursor:pointer;border:none;transition:all .15s}
    .ap-btn-p{background:#0071e3;color:#fff}
    .ap-btn-p:hover{background:#0077ed}
    .ap-btn-o{background:transparent;border:1px solid #d2d2d7;color:#6e6e73}
    .ap-btn-o:hover{border-color:#0071e3;color:#0071e3}

    .ap-grid{display:flex;gap:12px;padding:0 24px 20px;overflow-x:auto;min-height:280px}
    .ap-nd{flex:1 1 0;min-width:0;max-width:260px;min-height:260px;background:#f5f5f7;border:1px solid #e8e8ed;border-radius:14px;padding:16px;display:flex;flex-direction:column;transition:all .2s;position:relative}
    .ap-nd:hover{background:#fff;box-shadow:0 4px 20px rgba(0,0,0,.08);transform:translateY(-1px)}
    .ap-nd-top{width:36px;height:4px;border-radius:2px;margin-bottom:12px}
    .ap-nd-name{font-size:14px;font-weight:600;color:#1d1d1f;margin-bottom:6px}
    .ap-nd-desc{font-size:10px;color:#86868b;line-height:1.5;margin-bottom:10px}
    .ap-nd-row{display:flex;align-items:baseline;gap:6px;margin-bottom:8px}
    .ap-nd-label{font-size:9px;color:#86868b;text-transform:uppercase;letter-spacing:.5px;min-width:34px}
    .ap-nd-val{font-size:11px;font-weight:500;color:#1d1d1f;word-break:break-all}
    .ap-nd-eng{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:6px}
    .ap-eng-num{font-size:14px;font-weight:600}
    .ap-eng-btn{font-size:10px;padding:3px 12px;border-radius:8px;border:1px solid #d2d2d7;background:transparent;color:#6e6e73;cursor:pointer;transition:all .15s}
    .ap-eng-btn:hover{border-color:#0071e3;color:#0071e3}

    .ap-chat{display:flex;align-items:center;gap:14px;padding:16px 24px;margin:0 24px 20px;background:#f5f5f7;border:1px solid #e8e8ed;border-radius:14px;cursor:pointer;transition:all .2s}
    .ap-chat:hover{background:#fff;box-shadow:0 2px 12px rgba(0,0,0,.06)}
    .ap-chat-ic{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,#5ac8fa,#0071e3);display:flex;align-items:center;justify-content:center;color:#fff;font-size:18px;flex-shrink:0}
    .ap-chat-t{font-size:13px;font-weight:600;color:#1d1d1f}
    .ap-chat-s{font-size:11px;color:#86868b;margin-top:2px}
  </style>

  <div class="ap-hero">
    <div class="ap-hero-row">
      <div class="ap-hero-icon">■</div>
      <div style="flex:1">
        <div class="ap-hero-title">${owner}/${name} <span style="font-size:10px;font-weight:500;color:#0071e3;background:#e3edfd;padding:2px 8px;border-radius:6px;vertical-align:middle;margin-left:4px">${role === 'Maintainer' ? I18N.t('dashboard.hero_maintainer') : I18N.t('dashboard.hero_member')}</span></div>
        <div class="ap-hero-sub">${role === 'Maintainer' ? I18N.t('dashboard.hero_maintainer') : I18N.t('dashboard.hero_member')} · ★ main · ${I18N.t('dashboard.webhook_ok')}</div>
      </div>
      <div class="ap-stats">
        <div class="ap-stat"><div class="v" style="color:#34c759">${m.active_agents || 0}</div><div class="l">${I18N.t('dashboard.stat_active')}</div></div>
        <div class="ap-stat"><div class="v" style="color:#0071e3">${m.total_events || 0}</div><div class="l">${I18N.t('dashboard.stat_events')}</div></div>
        <div class="ap-stat"><div class="v" style="color:#ff3b30">${m.failed_events || 0}</div><div class="l">${I18N.t('dashboard.stat_failed')}</div></div>
      </div>
    </div>
  </div>

  <div class="ap-card">
    <div class="ap-card-hdr">
      <h2>${I18N.t('dashboard.scene_center_title')}</h2>
      <div class="ap-bar">
        <select id="sceneSelect" class="ap-sel">${sceneOpts}</select>
        ${isCustomScene ? '<button class="ap-btn ap-btn-o" onclick="toggleEditMode()">编辑场景</button>' : '<button class="ap-btn ap-btn-o" onclick="toggleEngineMode()">配置引擎</button>'}
        ${isCustomScene ? '<button class="ap-btn ap-btn-o" style="color:#ff3b30;border-color:#ff3b30" onclick="deleteCustomScene()">删除</button>' : ''}
        <button class="ap-btn ap-btn-o" onclick="applyScene('${owner}', '${name}')">应用</button>
      </div>
    </div>
    <div class="ap-grid" id="agentCards">`;

  html += renderAgentCards(a, agentOrder, agentLabels, colors, descs, agentEngines, isCustomScene, false, []);
  html += `</div>
  </div>`;

  el.innerHTML = html;

  document.getElementById('sceneSelect').onchange = function() {
    const val = this.value;
    if (val === '__create__') { enterCreateMode(); }
    else { applyScene(owner, name, val); }
  };

  window.applyScene = function(owner, name, sceneId) {
    const sid = sceneId || document.getElementById('sceneSelect').value;
    if (sid === '__create__') return;
    API.post('/scenes/apply/' + owner + '/' + name, {scene_id: sid}).then(r => {
      app.showToast(r.message || I18N.t('dashboard.scene_applied'));
      API.get('/dashboard/' + owner + '/' + name).then(renderDashboard).catch(() => {});
    }).catch(e => app.showToast(I18N.t('dashboard.apply_failed', {msg: (e.detail?.message || e.message)})));
  };

  window.toggleEditMode = function() {
    editMode = !editMode;
    engineMode = false;
    window._dashboardEditing = editMode;
    if (editMode) {
      editNodes = agentOrder.map(at => {
        const ag = a.find(x => x.agent_type === at) || {};
        const sn = currentSceneNodes.find(n => (n.tag || n.id) === at) || {};
        return {
          id: at, tag: at,
          skill: ag.skill || sn.skill || '',
          mode: sn.mode || 'emit',
          next: sn.next || 'done',
          handoff: sn.handoff || 'a2a',
          skip: sn.skip || [],
          enabled: ag.enabled_in_scene !== false,
          engine_runtime_ids: ag.engine_runtime_ids || [],
        };
      });
    }
    const cardsEl = document.getElementById('agentCards');
    cardsEl.innerHTML = renderAgentCards(a, agentOrder, agentLabels, colors, descs, agentEngines, isCustomScene, editMode, editNodes, false);
    const btn = document.querySelector('button[onclick="toggleEditMode()"]');
    if (btn) btn.textContent = editMode ? I18N.t('dashboard.cancel_edit') : I18N.t('dashboard.edit_scene');
    const eBtn = document.querySelector('button[onclick="toggleEngineMode()"]');
    if (eBtn) eBtn.textContent = I18N.t('dashboard.config_engine');
    let saveBtn = document.getElementById('saveSceneBtn');
    if (editMode && !saveBtn) {
      saveBtn = document.createElement('button');
      saveBtn.id = 'saveSceneBtn';
      saveBtn.className = 'ap-btn ap-btn-p';
      saveBtn.textContent = I18N.t('common.save');
      saveBtn.onclick = saveCustomScene;
      btn.parentNode.insertBefore(saveBtn, btn.nextSibling);
    } else if (!editMode && saveBtn) { saveBtn.remove(); }
  };

  let engineMode = false;
  window.toggleEngineMode = function() {
    engineMode = !engineMode;
    editMode = false;
    window._dashboardEditing = engineMode;
    if (engineMode) {
      editNodes = agentOrder.map(at => {
        const ag = a.find(x => x.agent_type === at) || {};
        const sn = currentSceneNodes.find(n => (n.tag || n.id) === at) || {};
        return {
          id: at, tag: at,
          skill: ag.skill || sn.skill || '',
          mode: sn.mode || 'emit',
          next: sn.next || 'done',
          handoff: sn.handoff || 'a2a',
          skip: sn.skip || [],
          enabled: ag.enabled_in_scene !== false,
          engine_runtime_ids: ag.engine_runtime_ids || [],
        };
      });
    }
    const cardsEl = document.getElementById('agentCards');
    cardsEl.innerHTML = renderAgentCards(a, agentOrder, agentLabels, colors, descs, agentEngines, isCustomScene, false, editNodes, engineMode);
    const eBtn = document.querySelector('button[onclick="toggleEngineMode()"]');
    if (eBtn) eBtn.textContent = engineMode ? I18N.t('dashboard.cancel_config') : I18N.t('dashboard.config_engine');
    const btn = document.querySelector('button[onclick="toggleEditMode()"]');
    if (btn) btn.textContent = I18N.t('dashboard.edit_scene');
    let saveBtn = document.getElementById('saveEngineBtn');
    if (engineMode && !saveBtn) {
      saveBtn = document.createElement('button');
      saveBtn.id = 'saveEngineBtn';
      saveBtn.className = 'ap-btn ap-btn-p';
      saveBtn.textContent = I18N.t('dashboard.save_engine');
      saveBtn.onclick = saveEngineBinding;
      eBtn.parentNode.insertBefore(saveBtn, eBtn.nextSibling);
    } else if (!engineMode && saveBtn) { saveBtn.remove(); }
  };

  window.saveEngineBinding = function() {
    const repo = app.currentRepo();
    if (!repo) { app.showToast(I18N.t('dashboard.please_select_repo')); return; }
    // 从 dashboard 返回的当前场景 nodes 保留结构，只覆盖 engine_runtime_ids
    const nodesPayload = currentSceneNodes.map(n => {
      const ed = editNodes.find(e => e.id === (n.id || n.tag));
      return {
        id: n.id, tag: n.tag || n.id,
        skill: n.skill || '',
        mode: n.mode || 'emit',
        next: n.next || 'done',
        handoff: n.handoff || 'a2a',
        skip: n.skip || [],
        enabled: n.enabled !== false,
        engine_runtime_ids: ed ? (ed.engine_runtime_ids || []) : (n.engine_runtime_ids || []),
      };
    });
    API.put('/scenes/' + currentScene, { nodes: nodesPayload }).then(() => {
      window._dashboardEditing = false;
      app.showToast(I18N.t('dashboard.engine_config_saved'));
      API.get('/dashboard/' + repo.owner + '/' + repo.name).then(renderDashboard).catch(() => {});
    }).catch(e => { window._dashboardEditing = false; app.showToast('保存失败: ' + (e.detail?.message || e.message)); });
  };

  window.enterCreateMode = function() {
    editMode = true;
    window._dashboardEditing = true;
    editNodes = agentOrder.map(at => ({ id: at, tag: at, skill: '', mode: 'emit', next: 'done', handoff: 'a2a', skip: [], enabled: true, engine_runtime_ids: [] }));
    const cardsEl = document.getElementById('agentCards');
    cardsEl.innerHTML = renderAgentCards(a, agentOrder, agentLabels, colors, descs, agentEngines, true, true, editNodes);
    let btn = document.querySelector('button[onclick="toggleEditMode()"]');
    if (btn) btn.remove();
    let engBtn = document.querySelector('button[onclick="toggleEngineMode()"]');
    if (engBtn) engBtn.style.display = 'none';
    const headerDiv = document.querySelector('.ap-bar');
    if (headerDiv) {
      const saveBtn = document.createElement('button');
      saveBtn.className = 'ap-btn ap-btn-p';
      saveBtn.textContent = I18N.t('common.save');
      saveBtn.onclick = saveCustomScene;
      headerDiv.appendChild(saveBtn);
    }
  };

  window.toggleAgentInScene = function(agentType) {
    const node = editNodes.find(n => n.id === agentType);
    if (!node) return;
    node.enabled = !node.enabled;
    if (!node.enabled) { node.skill = ''; node.engine_runtime_ids = []; }
    const cardsEl = document.getElementById('agentCards');
    cardsEl.innerHTML = renderAgentCards(a, agentOrder, agentLabels, colors, descs, agentEngines, true, true, editNodes, false);
  };

  window.selectAgentSkill = function(agentType, skillName) {
    const node = editNodes.find(n => n.id === agentType);
    if (node) node.skill = skillName;
  };

  window.openEngineModal = function(agentType) {
    const engines = (agentEngines[agentType] || []).filter(e => e.status === 'online');
    const node = editNodes.find(n => n.id === agentType);
    if (!node) return;
    const currentIds = node.engine_runtime_ids || [];

    const _ENG_TAG_COLORS = {all:'#6366f1',vod:'#ec4899',issue:'#ef4444',coding:'#3b82f6',qa:'#a855f7',sre:'#f59e0b'};
    const _tagBadge = (tags) => (tags||[]).map(t => `<span style="font-size:9px;padding:2px 7px;border-radius:999px;color:#fff;background:${_ENG_TAG_COLORS[t]||'#999'};margin-left:4px;font-weight:500">${t==='all'?'All':t.charAt(0).toUpperCase()+t.slice(1)}</span>`).join('');

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    const meta = _AGENT_TYPES[agentType] || {};
    const title = meta.display_name || agentType;

    overlay.innerHTML = `<div style="background:#fff;border-radius:18px;padding:28px;min-width:380px;max-width:500px;max-height:80vh;overflow-y:auto;box-shadow:0 12px 48px rgba(0,0,0,.15)">
      <h3 style="margin:0 0 4px;font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px">${I18N.t('dashboard.select_engine')} — ${title}</h3>
      <div style="font-size:12px;color:#86868b;margin-bottom:20px">${I18N.t('dashboard.engines_available', {count: engines.length})}</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        ${engines.length === 0 ? `<div style="padding:20px 0;text-align:center;color:#86868b;font-size:13px">${I18N.t('dashboard.no_engine_broadcast')}</div>` : engines.map(e => {
          const checked = currentIds.includes(e.id);
          return `<label style="display:flex;align-items:center;gap:12px;padding:12px 16px;border:1px solid ${checked ? '#0071e3' : '#e8e8ed'};border-radius:12px;cursor:pointer;background:${checked ? 'rgba(0,113,227,.04)' : '#f5f5f7'};transition:all .15s">
            <input type="checkbox" data-eid="${e.id}" ${checked ? 'checked' : ''} style="width:18px;height:18px;cursor:pointer;accent-color:#0071e3">
            <div style="flex:1;min-width:0">
              <div style="font-weight:600;font-size:13px;color:#1d1d1f;display:flex;align-items:center">${e.name}${_tagBadge(e.tags)}</div>
              <div style="font-size:10px;color:#86868b;margin-top:3px">${e.device || ''}${e.cred_name ? ' · ' + e.cred_name : ''}</div>
            </div>
            <span style="font-size:11px;font-weight:500;color:#34c759">${I18N.t('dashboard.online')}</span>
          </label>`;
        }).join('')}
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:24px">
        <button class="ap-btn ap-btn-o" onclick="this.closest('[style*=fixed]').remove()">${I18N.t('common.cancel')}</button>
        <button class="ap-btn ap-btn-p" id="confirmEngineBtn">${I18N.t('common.confirm')}</button>
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

    overlay.addEventListener('click', function(ev) {
      if (ev.target === overlay) overlay.remove();
    });
  };

  window.saveCustomScene = function() {
    const nodesPayload = agentOrder.filter(at => {
      const n = editNodes.find(e => e.id === at);
      return n && n.enabled;
    }).map((at, i, arr) => {
      const n = editNodes.find(e => e.id === at);
      const nextAgent = i < arr.length - 1 ? arr[i + 1] : 'done';
      return {
        id: at, tag: at, skill: n.skill || '', mode: 'emit',
        next: nextAgent, handoff: i < arr.length - 1 ? 'a2a' : 'complete',
        skip: [], enabled: true,
        engine_runtime_ids: n.engine_runtime_ids || [],
      };
    });

    const isEditingExisting = currentScene && currentScene !== 'default' && isCustomScene;

    // 编辑已有自定义场景：直接 PUT 保存 nodes，不弹名称/描述输入框
    if (isEditingExisting) {
      API.put('/scenes/' + currentScene, { name: '', description: '', nodes: nodesPayload }).then(() => {
        window._dashboardEditing = false;
        app.showToast('场景已保存');
        API.get('/dashboard/' + owner + '/' + name).then(renderDashboard).catch(() => {});
      }).catch(e => { window._dashboardEditing = false; app.showToast('保存失败: ' + (e.detail?.message || e.message)); });
      return;
    }

    // 创建新场景：弹框设置场景名称 / 描述 / 可见范围
    const existingName = (currentSceneObj && currentSceneObj.name) || '';
    const existingDesc = (currentSceneObj && currentSceneObj.description) || '';
    const existingScope = (currentSceneObj && currentSceneObj.scope) || 'user';

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    overlay.innerHTML = `<div style="background:#fff;border-radius:18px;padding:28px;min-width:360px;max-width:460px;box-shadow:0 12px 48px rgba(0,0,0,.15)">
      <h3 style="margin:0 0 20px;font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px">保存自定义场景</h3>
      <div style="margin-bottom:14px">
        <label style="font-size:11px;color:#86868b;display:block;margin-bottom:4px">${I18N.t('dashboard.scene_name')}</label>
        <input id="sceneNameInput" type="text" value="${(existingName||'').replace(/"/g,'&quot;')}" placeholder="${I18N.t('dashboard.scene_name_placeholder')}" style="width:100%;padding:8px 12px;border:1px solid #d2d2d7;border-radius:10px;background:#f5f5f7;color:#1d1d1f;font-size:13px;outline:none">
      </div>
      <div style="margin-bottom:14px">
        <label style="font-size:11px;color:#86868b;display:block;margin-bottom:4px">${I18N.t('dashboard.desc_optional')}</label>
        <input id="sceneDescInput" type="text" value="${(existingDesc||'').replace(/"/g,'&quot;')}" placeholder="${I18N.t('dashboard.desc_placeholder')}" style="width:100%;padding:8px 12px;border:1px solid #d2d2d7;border-radius:10px;background:#f5f5f7;color:#1d1d1f;font-size:13px;outline:none">
      </div>
      <div style="margin-bottom:20px">
        <label style="font-size:11px;color:#86868b;display:block;margin-bottom:4px">${I18N.t('dashboard.visibility')}</label>
        <select id="sceneScopeSelect" style="width:100%;padding:8px 12px;border:1px solid #d2d2d7;border-radius:10px;background:#f5f5f7;color:#1d1d1f;font-size:13px;outline:none">
          <option value="user" ${existingScope==='user'?'selected':''}>${I18N.t('dashboard.scope_user')}</option>
          <option value="repo" ${existingScope==='repo'?'selected':''}>${I18N.t('dashboard.scope_repo')}</option>
        </select>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px">
        <button class="ap-btn ap-btn-o" onclick="this.closest('[style*=fixed]').remove()">${I18N.t('common.cancel')}</button>
        <button class="ap-btn ap-btn-p" id="confirmSaveSceneBtn">${I18N.t('common.save')}</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);

    document.getElementById('confirmSaveSceneBtn').onclick = function() {
      const sceneName = document.getElementById('sceneNameInput').value.trim();
      if (!sceneName) { app.showToast(I18N.t('dashboard.please_input_scene_name')); return; }
      const sceneDesc = document.getElementById('sceneDescInput').value.trim();
      const sceneScope = document.getElementById('sceneScopeSelect').value;

      // 创建新场景：POST
      API.post('/scenes', { name: sceneName, description: sceneDesc, scope: sceneScope, nodes: nodesPayload }).then(r => {
        window._dashboardEditing = false;
        overlay.remove();
        app.showToast('场景创建成功');
        if (r.scene_id) { applyScene(owner, name, r.scene_id); }
      }).catch(e => { window._dashboardEditing = false; app.showToast('保存失败: ' + (e.detail?.message || e.message)); });
    };
  };

  // 删除自定义场景
  window.deleteCustomScene = function() {
    if (!currentScene || currentScene === 'default') { app.showToast('官方场景不可删除'); return; }
    if (!isCustomScene) { app.showToast('仅可删除自定义场景'); return; }
    const sceneName = (currentSceneObj && currentSceneObj.name) || currentScene;
    // 确认弹框
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    overlay.innerHTML = `<div style="background:#fff;border-radius:18px;padding:28px;min-width:320px;max-width:400px;box-shadow:0 12px 48px rgba(0,0,0,.15)">
      <h3 style="margin:0 0 12px;font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px">删除场景</h3>
      <p style="margin:0 0 24px;font-size:13px;color:#86868b;line-height:1.5">确定要删除场景「${sceneName}」吗？此操作不可撤销。</p>
      <div style="display:flex;justify-content:flex-end;gap:8px">
        <button class="ap-btn ap-btn-o" onclick="this.closest('[style*=fixed]').remove()">取消</button>
        <button class="ap-btn" style="background:#ff3b30;color:#fff;border:none" id="confirmDeleteSceneBtn">删除</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);
    document.getElementById('confirmDeleteSceneBtn').onclick = function() {
      API.del('/scenes/' + currentScene).then(() => {
        window._dashboardEditing = false;
        overlay.remove();
        app.showToast('场景已删除');
        // 删除后切换回 default 场景
        applyScene(owner, name, 'default');
      }).catch(e => { overlay.remove(); app.showToast('删除失败: ' + (e.detail?.message || e.message)); });
    };
  };

  window.deleteCustomScene = function() {
    if (!currentScene || !isCustomScene) return;
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    overlay.innerHTML = `<div style="background:#fff;border-radius:18px;padding:28px;min-width:320px;max-width:400px;box-shadow:0 12px 48px rgba(0,0,0,.15)">
      <h3 style="margin:0 0 12px;font-size:17px;font-weight:600;color:#1d1d1f">${I18N.t('dashboard.confirm_delete_scene')}</h3>
      <p style="margin:0 0 20px;font-size:13px;color:#86868b;line-height:1.5">${(currentSceneObj && currentSceneObj.name) || ''}</p>
      <div style="display:flex;justify-content:flex-end;gap:8px">
        <button class="ap-btn ap-btn-o" onclick="this.closest('[style*=fixed]').remove()">${I18N.t('common.cancel')}</button>
        <button class="ap-btn ap-btn-p" id="confirmDeleteSceneBtn" style="background:#ff3b30">${I18N.t('common.delete')}</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);
    document.getElementById('confirmDeleteSceneBtn').onclick = function() {
      API.delete('/scenes/' + currentScene).then(() => {
        overlay.remove();
        app.showToast(I18N.t('dashboard.scene_deleted'));
        API.get('/dashboard/' + owner + '/' + name).then(renderDashboard).catch(() => {});
      }).catch(e => { overlay.remove(); app.showToast(I18N.t('dashboard.delete_failed', {msg: (e.detail?.message || e.message)})); });
    };
  };
}

function renderAgentCards(a, agentOrder, agentLabels, colors, descs, agentEngines, isCustom, editMode, editNodes, engineMode) {
  let html = '';
  agentOrder.forEach((at, i) => {
    const ag = a.find(x => x.agent_type === at) || {};
    const meta = _AGENT_TYPES[at] || {};
    const isEditing = editMode || engineMode;
    const node = isEditing ? editNodes.find(n => n.id === at) : null;
    const enabled = node ? node.enabled : (ag.enabled_in_scene !== false);
    const skill = node ? (node.skill || '') : (ag.skill || '');
    const skillDisplay = node ? (ag.skill_display_name || ag.skill || node.skill || '') : (ag.skill_display_name || ag.skill || '');
    const engineIds = node ? (node.engine_runtime_ids || []) : (ag.engine_runtime_ids || []);
    const accentColor = meta.color || colors[at] || '#86868b';
    // 语言包优先于后端 description（后端返回中文时英文模式用前端翻译）
    const desc = descs[at] || meta.description || '';
    const canToggle = at !== 'vod' && at !== 'issue';

    let toggleBtn = '';
    if (editMode && canToggle) {
      toggleBtn = `<span onclick="event.stopPropagation();toggleAgentInScene('${at}')" style="cursor:pointer;font-size:14px;color:${enabled ? '#ff3b30' : '#34c759'};margin-left:auto;padding:2px 6px" title="${enabled ? I18N.t('dashboard.disable_agent') : I18N.t('dashboard.enable_agent')}">${enabled ? '✕' : '✓'}</span>`;
    }

    let skillHtml;
    if (editMode && enabled) {
      skillHtml = `<select onchange="selectAgentSkill('${at}', this.value)" style="width:100%;padding:4px 8px;border:1px solid #d2d2d7;border-radius:8px;font-size:11px;background:#fff;color:#1d1d1f;outline:none">
        <option value="">${I18N.t('dashboard.select_skill')}</option>
      </select>`;
      setTimeout(() => {
        API.get('/scenes/skills/' + at).then(r => {
          const sel = document.querySelector(`select[onchange*="${at}"]`);
          if (sel && r.skills) {
            const current = editNodes.find(n => n.id === at)?.skill || '';
            sel.innerHTML = '<option value="">' + I18N.t('dashboard.select_skill') + '</option>' +
              r.skills.map(s => `<option value="${s.skill_name}"${s.skill_name === current ? ' selected' : ''}>${s.display_name}</option>`).join('');
          }
        }).catch(() => {});
      }, 50);
    } else if (enabled && skill) {
      skillHtml = `<span class="ap-nd-val">${skillDisplay}</span>`;
    } else {
      skillHtml = `<span style="font-size:11px;color:#86868b">none</span>`;
    }

    // Engine 区域 — 居中药丸标签
    let engineHtml;
    if ((editMode || engineMode) && enabled) {
      if (engineIds.length > 0) {
        engineHtml = `<div class="ap-nd-eng">
          <span style="display:inline-flex;align-items:center;gap:4px;background:#0071e314;color:#0071e3;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.engine_count', {count: engineIds.length})}</span>
          <button class="ap-eng-btn" onclick="openEngineModal('${at}')">${I18N.t('dashboard.change_engine')}</button>
        </div>`;
      } else {
        engineHtml = `<div class="ap-nd-eng">
          <span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.broadcast')}</span>
          <button class="ap-eng-btn" onclick="openEngineModal('${at}')">${I18N.t('dashboard.select_engine')}</button>
        </div>`;
      }
    } else if (enabled && engineIds.length > 0) {
      engineHtml = `<div class="ap-nd-eng"><span style="display:inline-flex;align-items:center;gap:4px;background:#0071e314;color:#0071e3;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.engine_count', {count: engineIds.length})}</span></div>`;
    } else if (enabled) {
      engineHtml = `<div class="ap-nd-eng"><span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.broadcast')}</span></div>`;
    } else {
      engineHtml = `<div class="ap-nd-eng"><span style="font-size:11px;color:#86868b">none</span></div>`;
    }

    html += `<div class="ap-nd" style="${enabled ? '' : 'opacity:.35;filter:grayscale(1)'}">
      <div class="ap-nd-top" style="background:${accentColor}"></div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="width:22px;height:22px;border-radius:6px;background:${accentColor}22;color:${accentColor};font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0">${i + 1}</span>
        <span style="font-size:14px;font-weight:600;color:#1d1d1f;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${meta.display_name || agentLabels[i]}</span>
        ${toggleBtn}
      </div>
      ${enabled ? `<div class="ap-nd-desc">${desc}</div>` : `<div style="font-size:10px;color:#86868b;margin-bottom:8px">${I18N.t('dashboard.not_needed_in_scene')}</div>`}
      <div style="height:1px;background:#e8e8ed;margin:8px 0"></div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;margin-bottom:8px;display:flex;flex-direction:column;align-items:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">${I18N.t('dashboard.label_skill')}</div>
        ${skillHtml}
      </div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">${I18N.t('dashboard.label_engine')}</div>
        ${engineHtml}
      </div>
    </div>`;
  });
  return html;
}
