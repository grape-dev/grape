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
    '<option value="__create__">➕ 创建自定义场景</option>',
  ].join('');

  const agentOrder = ['vod','issue','coding','qa','sre'];
  const agentLabels = ['VOD Agent','Issue Agent','Coding Agent','QA Agent','SRE Agent'];
  const colors = {vod:'#ff2d92',issue:'#ff3b30',coding:'#0071e3',qa:'#af52de',sre:'#ff9f0a'};
  const descs = {vod:'从开发者声音中收集反馈、聚类分析共性、提炼价值、自动创建 Issue',issue:'Issue 分类、标签、指派、A2A 触发 Coding Agent',coding:'代码实现、PR 创建、自我审查',qa:'代码审查、自动化测试、功能验证、安全检查',sre:'构建部署、发布管理、监控运维、故障排查'};

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
        <div class="ap-hero-title">${owner}/${name} <span style="font-size:10px;font-weight:500;color:#0071e3;background:#e3edfd;padding:2px 8px;border-radius:6px;vertical-align:middle;margin-left:4px">${role}</span></div>
        <div class="ap-hero-sub">${role === 'Maintainer' ? 'Maintainer' : 'Member'} · ★ main · Webhook OK</div>
      </div>
      <div class="ap-stats">
        <div class="ap-stat"><div class="v" style="color:#34c759">${m.active_agents || 0}</div><div class="l">Active</div></div>
        <div class="ap-stat"><div class="v" style="color:#0071e3">${m.total_events || 0}</div><div class="l">Events</div></div>
        <div class="ap-stat"><div class="v" style="color:#ff3b30">${m.failed_events || 0}</div><div class="l">Failed</div></div>
      </div>
    </div>
  </div>

  <div class="ap-card">
    <div class="ap-card-hdr">
      <h2>Scene Center</h2>
      <div class="ap-bar">
        <select id="sceneSelect" class="ap-sel">${sceneOpts}</select>
        ${isCustomScene ? '<button class="ap-btn ap-btn-o" onclick="toggleEditMode()">编辑场景</button>' : '<button class="ap-btn ap-btn-o" onclick="toggleEngineMode()">配置引擎</button>'}
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
      app.showToast(r.message || '场景已应用');
      API.get('/dashboard/' + owner + '/' + name).then(renderDashboard).catch(() => {});
    }).catch(e => app.showToast('应用失败: ' + (e.detail?.message || e.message)));
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
    if (btn) btn.textContent = editMode ? '取消编辑' : '编辑场景';
    const eBtn = document.querySelector('button[onclick="toggleEngineMode()"]');
    if (eBtn) eBtn.textContent = '配置引擎';
    let saveBtn = document.getElementById('saveSceneBtn');
    if (editMode && !saveBtn) {
      saveBtn = document.createElement('button');
      saveBtn.id = 'saveSceneBtn';
      saveBtn.className = 'ap-btn ap-btn-p';
      saveBtn.textContent = '保存';
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
    if (eBtn) eBtn.textContent = engineMode ? '取消配置' : '配置引擎';
    const btn = document.querySelector('button[onclick="toggleEditMode()"]');
    if (btn) btn.textContent = '编辑场景';
    let saveBtn = document.getElementById('saveEngineBtn');
    if (engineMode && !saveBtn) {
      saveBtn = document.createElement('button');
      saveBtn.id = 'saveEngineBtn';
      saveBtn.className = 'ap-btn ap-btn-p';
      saveBtn.textContent = '保存引擎';
      saveBtn.onclick = saveEngineBinding;
      eBtn.parentNode.insertBefore(saveBtn, eBtn.nextSibling);
    } else if (!engineMode && saveBtn) { saveBtn.remove(); }
  };

  window.saveEngineBinding = function() {
    const repo = app.currentRepo();
    if (!repo) { app.showToast('请先选择仓库'); return; }
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
      app.showToast('引擎配置已保存');
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
      saveBtn.textContent = '保存';
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
    const _tagBadge = (tags) => (tags||['all']).map(t => `<span style="font-size:9px;padding:2px 7px;border-radius:999px;color:#fff;background:${_ENG_TAG_COLORS[t]||'#999'};margin-left:4px;font-weight:500">${t==='all'?'All':t.charAt(0).toUpperCase()+t.slice(1)}</span>`).join('');

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    const meta = _AGENT_TYPES[agentType] || {};
    const title = meta.display_name || agentType;

    overlay.innerHTML = `<div style="background:#fff;border-radius:18px;padding:28px;min-width:380px;max-width:500px;max-height:80vh;overflow-y:auto;box-shadow:0 12px 48px rgba(0,0,0,.15)">
      <h3 style="margin:0 0 4px;font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px">选择引擎 — ${title}</h3>
      <div style="font-size:12px;color:#86868b;margin-bottom:20px">${engines.length} 个可用引擎 · 不选 = 广播竞争</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        ${engines.length === 0 ? '<div style="padding:20px 0;text-align:center;color:#86868b;font-size:13px">无可用引擎，将使用广播</div>' : engines.map(e => {
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

    // 弹框设置场景名称 / 描述 / 可见范围
    const isEditingExisting = currentScene && currentScene !== 'default' && isCustomScene;
    const existingName = (currentSceneObj && currentSceneObj.name) || '';
    const existingDesc = (currentSceneObj && currentSceneObj.description) || '';
    const existingScope = (currentSceneObj && currentSceneObj.scope) || 'user';

    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    overlay.innerHTML = `<div style="background:#fff;border-radius:18px;padding:28px;min-width:360px;max-width:460px;box-shadow:0 12px 48px rgba(0,0,0,.15)">
      <h3 style="margin:0 0 20px;font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px">${isEditingExisting ? '编辑自定义场景' : '保存自定义场景'}</h3>
      <div style="margin-bottom:14px">
        <label style="font-size:11px;color:#86868b;display:block;margin-bottom:4px">场景名称</label>
        <input id="sceneNameInput" type="text" value="${(existingName||'').replace(/"/g,'&quot;')}" placeholder="如：我的开发流程" style="width:100%;padding:8px 12px;border:1px solid #d2d2d7;border-radius:10px;background:#f5f5f7;color:#1d1d1f;font-size:13px;outline:none">
      </div>
      <div style="margin-bottom:14px">
        <label style="font-size:11px;color:#86868b;display:block;margin-bottom:4px">描述（可选）</label>
        <input id="sceneDescInput" type="text" value="${(existingDesc||'').replace(/"/g,'&quot;')}" placeholder="场景描述..." style="width:100%;padding:8px 12px;border:1px solid #d2d2d7;border-radius:10px;background:#f5f5f7;color:#1d1d1f;font-size:13px;outline:none">
      </div>
      <div style="margin-bottom:20px">
        <label style="font-size:11px;color:#86868b;display:block;margin-bottom:4px">可见范围</label>
        <select id="sceneScopeSelect" style="width:100%;padding:8px 12px;border:1px solid #d2d2d7;border-radius:10px;background:#f5f5f7;color:#1d1d1f;font-size:13px;outline:none">
          <option value="user" ${existingScope==='user'?'selected':''}>个人级（所有仓库可见）</option>
          <option value="repo" ${existingScope==='repo'?'selected':''}>仓库级（仅当前仓库可见）</option>
        </select>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px">
        <button class="ap-btn ap-btn-o" onclick="this.closest('[style*=fixed]').remove()">取消</button>
        <button class="ap-btn ap-btn-p" id="confirmSaveSceneBtn">保存</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);

    document.getElementById('confirmSaveSceneBtn').onclick = function() {
      const sceneName = document.getElementById('sceneNameInput').value.trim();
      if (!sceneName) { app.showToast('请输入场景名称'); return; }
      const sceneDesc = document.getElementById('sceneDescInput').value.trim();
      const sceneScope = document.getElementById('sceneScopeSelect').value;

      if (isEditingExisting) {
        // 编辑已有自定义场景：PUT（含名称/描述更新）
        API.put('/scenes/' + currentScene, { name: sceneName, description: sceneDesc, nodes: nodesPayload }).then(() => {
          window._dashboardEditing = false;
          overlay.remove();
          app.showToast('场景已保存');
          API.get('/dashboard/' + owner + '/' + name).then(renderDashboard).catch(() => {});
        }).catch(e => { window._dashboardEditing = false; app.showToast('保存失败: ' + (e.detail?.message || e.message)); });
      } else {
        // 创建新场景：POST
        API.post('/scenes', { name: sceneName, description: sceneDesc, scope: sceneScope, nodes: nodesPayload }).then(r => {
          window._dashboardEditing = false;
          overlay.remove();
          app.showToast('场景创建成功');
          if (r.scene_id) { applyScene(owner, name, r.scene_id); }
        }).catch(e => { window._dashboardEditing = false; app.showToast('保存失败: ' + (e.detail?.message || e.message)); });
      }
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
    const desc = meta.description || descs[at] || '';
    const canToggle = at !== 'vod' && at !== 'issue';

    let toggleBtn = '';
    if (editMode && canToggle) {
      toggleBtn = `<span onclick="event.stopPropagation();toggleAgentInScene('${at}')" style="cursor:pointer;font-size:14px;color:${enabled ? '#ff3b30' : '#34c759'};margin-left:auto;padding:2px 6px" title="${enabled ? '点击禁用此 Agent' : '点击启用此 Agent'}">${enabled ? '✕' : '✓'}</span>`;
    }

    let skillHtml;
    if (editMode && enabled) {
      skillHtml = `<select onchange="selectAgentSkill('${at}', this.value)" style="width:100%;padding:4px 8px;border:1px solid #d2d2d7;border-radius:8px;font-size:11px;background:#fff;color:#1d1d1f;outline:none">
        <option value="">选择 Skill...</option>
      </select>`;
      setTimeout(() => {
        API.get('/scenes/skills/' + at).then(r => {
          const sel = document.querySelector(`select[onchange*="${at}"]`);
          if (sel && r.skills) {
            const current = editNodes.find(n => n.id === at)?.skill || '';
            sel.innerHTML = '<option value="">选择 Skill...</option>' +
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
          <span style="display:inline-flex;align-items:center;gap:4px;background:#0071e314;color:#0071e3;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${engineIds.length} 个引擎</span>
          <button class="ap-eng-btn" onclick="openEngineModal('${at}')">更改</button>
        </div>`;
      } else {
        engineHtml = `<div class="ap-nd-eng">
          <span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">广播</span>
          <button class="ap-eng-btn" onclick="openEngineModal('${at}')">选择引擎</button>
        </div>`;
      }
    } else if (enabled && engineIds.length > 0) {
      engineHtml = `<div class="ap-nd-eng"><span style="display:inline-flex;align-items:center;gap:4px;background:#0071e314;color:#0071e3;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${engineIds.length} 个引擎</span></div>`;
    } else if (enabled) {
      engineHtml = `<div class="ap-nd-eng"><span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">广播</span></div>`;
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
      ${enabled ? `<div class="ap-nd-desc">${desc}</div>` : '<div style="font-size:10px;color:#86868b;margin-bottom:8px">此场景下不需要</div>'}
      <div style="height:1px;background:#e8e8ed;margin:8px 0"></div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;margin-bottom:8px;display:flex;flex-direction:column;align-items:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">Skill</div>
        ${skillHtml}
      </div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">Engine</div>
        ${engineHtml}
      </div>
    </div>`;
  });
  return html;
}
