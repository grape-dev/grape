let _agentCfgVisible = false;

function toggleAgentCfg() {
  _agentCfgVisible = !_agentCfgVisible;
  const p = document.getElementById('agentCfgPanel');
  if (p) p.classList.toggle('hidden');
  if (_agentCfgVisible) loadCfgOptions();
}

function loadCfgOptions() {
  const backendSelect = document.querySelector('.cfg-backend');
  const skillSelect = document.querySelector('.cfg-skill');

  // Runtime 下拉：仅未加载时加载；DevOps Agent 仅可选 仓库/全局 引擎（个人引擎不出现，见 runtime-admin-two-view-design §5.1）
  if (backendSelect && !backendSelect.dataset.loaded) {
    backendSelect.dataset.loaded = '1';
    API.get('/settings/agent-runtimes').then(d => {
      const runtimes = d.runtimes || [];
      const current = backendSelect.dataset.current || 'opencode';
      const scopeLabel = sc => sc === 'global' ? '全局' : sc === 'repo' ? '仓库' : '个人';
      const ok = runtimes.filter(r => r.scope === 'repo' || r.scope === 'global');
      const online = ok.filter(r => r.health === 'online' || r.healthy);
      const offline = ok.filter(r => !(r.health === 'online' || r.healthy));
      const val = r => r.instance_name || r.name;
      const opt = (r, sel) =>
        `<option value="${val(r)}" ${(val(r) === current || r.name === current) ? 'selected' : ''}>${val(r)} · ${r.name} v${r.version || ''} · ${r.worker_name || ''} · ${scopeLabel(r.scope)}${r.health === 'online' || r.healthy ? ' ✓' : ' ✗'}</option>`;
      let html = online.map(r => opt(r, true)).join('') + offline.map(r => opt(r, false)).join('');
      if (!html) html = '<option value="opencode">opencode</option>';
      backendSelect.innerHTML = html;
    }).catch(() => {});
  }

  // Skill 下拉：仅未加载时加载（幂等），预加载后点 Configure 不再重复请求
  if (skillSelect && !skillSelect.dataset.loaded) {
    skillSelect.dataset.loaded = '1';
    let repo = '';
    try {
      const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
      repo = c ? (c.fullName || '') : '';
    } catch {}
    const qs = repo ? '?repo=' + encodeURIComponent(repo) : '';
    API.get('/skills' + qs).then(d => {
      const allSkills = d.skills || [];
      const agentType = skillSelect.dataset.agent || '';
      const currentSkill = skillSelect.dataset.current || '';
      const currentScope = skillSelect.dataset.scope || '';
      const filtered = allSkills.filter(s => s.agent_type === agentType || s.parent_agent_type === agentType);
      // 若当前配置的 skill 不在返回列表（如自定义 skill），保留该值作为一个 option
      const hasCurrent = filtered.some(s => s.name === currentSkill);
      const scopeLabel = sc => sc === 'official' ? '官方' : sc === 'repo' ? '仓库' : '个人';
      let html = '<option value="">默认官方技能（自动）</option>' +
        filtered.map(s =>
          `<option value="${s.scope}:${s.name}" ${(currentScope + ':' + currentSkill) === (s.scope + ':' + s.name) ? 'selected' : ''}>${s.display_name || s.name} (${scopeLabel(s.scope)})</option>`
        ).join('');
      if (currentSkill && !hasCurrent) {
        html += `<option value="${currentSkill}" selected>${currentSkill}（已配置）</option>`;
      }
      skillSelect.innerHTML = html;
      skillSelect.dataset.currentScope = currentScope || '';
    }).catch(() => {});
  }
}

function renderAgent(name, data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const meta = _AGENT_TYPES[name] || {display_name:name,icon:'?',color:'var(--t2)',bg:'var(--bg)',description:''};
  const m = data.metrics || {};
  const es = m.event_stats || {};
  const events = data.recent_events || [];
  const evtHtml = events.length > 0 ? events.map(ev =>
    `<div class="ai" onclick="app.nav('event',${ev.seq})" style="cursor:pointer">
      <div class="t">${fmtLocalTime(ev.created_at).substring(11,16) || '--:--'}</div>
      <div class="ac">${ev.summary || ev.title || ev.event_type}
        ${ev.actor ? ` <span class="ts t2">@${ev.actor}</span>` : ''}
        <span class="ts t3">Seq #${ev.seq || '?'}</span>
        ${ev.issue_id ? ` <span class="tg tg-d">${ev.issue_id}</span>` : ''}
        ${ev.duration ? ` <span class="ts t3">${ev.duration}s</span>` : ''}
        ${ev.backend ? ` <span class="tg tg-d">${ev.backend}</span>` : ''}
        <span class="ts ${ev.status === 'failed' ? 'tr' : ev.status === 'completed' ? 'tgx' : 'ty'}">${ev.status}</span>
      </div>
    </div>`
  ).join('') : '<div class="tx t2">暂无事件</div>';

  const ef = es.completed || 0;
  const er = es.running || 0;
  const et = m.total_events || 0;

  const currentBackend = data.backend || meta.default_backend || 'opencode';

  let html = `
<div class="cd" style="background:${meta.bg || 'var(--bg)'};border-color:${meta.color || 'var(--b)'}33">
  <div class="flex fc gap12">
    <div style="width:44px;height:44px;border-radius:10px;background:${meta.color || 'var(--b)'}22;display:flex;align-items:center;justify-content:center;font-size:20px">${meta.icon || '?'}</div>
    <div class="f1"><h2 style="font-size:16px;font-weight:600;margin-bottom:2px">${meta.display_name || name}</h2><div class="ts t2">${meta.description || ''}</div></div>
    <div class="flex gap6"><button class="bt bt-o bt-s" onclick="toggleAgentCfg()">Configure</button><button class="bt bt-p bt-s">Events</button></div>
  </div>
</div>
<div id="agentCfgPanel" class="cd hidden" style="padding:16px 20px;margin-bottom:14px">
  <h3 style="font-size:13px;font-weight:600;margin-bottom:12px">Agent Configuration</h3>
  <div class="srow"><label>Backend Engine</label>
    <select class="cfg-backend" data-current="${currentBackend}" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)">
      <option value="${currentBackend}" selected>${currentBackend} ...</option>
    </select>
  </div>
    <div class="srow"><label>Skill</label>
    <select class="cfg-skill" data-agent="${name}" data-current="${(data.skill || '')}" data-scope="${(data.skill_scope || '')}" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)">
      <option value="${data.skill || ''}" ${data.skill ? 'selected' : ''}>${data.skill ? '加载中...' : '默认官方技能（自动）'}</option>
    </select>
  </div>
  ${name === 'sre' ? _agentSubsSection(name, data) : ''}
  ${name === 'vod' ? `
  <div style="margin:16px 0 12px;border-top:1px solid var(--b);padding-top:12px">
    <div class="flex fc jbtw" style="margin-bottom:8px">
      <h3 style="font-size:13px;font-weight:600">IM 配置</h3>
      <span class="ts t3">${data.im && data.im.connected ? '🟢 已配置' : ''}</span>
    </div>
    <div class="srow"><label>Bot App ID</label><input id="vodImAppId" value="${(data.im && data.im.im_app_id) || ''}" placeholder="cli_xxxxxxxx" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg);outline:none"></div>
    <div class="srow"><label>Bot Secret</label><input id="vodImSecret" type="password" value="${data.im && data.im.connected ? '********' : ''}" placeholder="留空不修改" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg);outline:none"></div>
    <div class="srow"><label></label><button class="bt bt-o bt-s" onclick="startVodOnboard()" style="padding:7px 14px">扫码配置</button></div>
    ${data.im && data.im.conversations && data.im.conversations.length > 0 ? `
    <div style="margin-top:8px">
      <span class="ts t3">已绑定的群 (${data.im.conversations.length})</span>
      ${data.im.conversations.map(c => `<div style="font-size:11px;padding:4px 0;border-bottom:1px solid var(--b)">${c.chat_name || c.chat_id} <span class="ts t3">${c.status}</span></div>`).join('')}
    </div>` : ''}
  </div>` : ''}
  <div class="flex gap6 mt16"><button class="bt bt-p bt-s" onclick="saveAgentConfig('${name}')">Save</button><button class="bt bt-o bt-s" onclick="toggleAgentCfg()">Cancel</button></div>
  <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--b)">
    <div class="ts t3" style="margin-bottom:6px">清空会话后，下一次事件将创建全新的 Agent 上下文（不延续历史对话）。</div>
    <button class="bt bt-r bt-s" onclick="cleanAgentSession('${name}')">🧹 Clean Session</button>
  </div>
</div>
<div class="stats mt16 mb16">
  <div class="st"><div class="v" style="color:${meta.color || 'var(--t2)'}">${et}</div><div class="l">事件总量</div></div>
  <div class="st"><div class="v" style="color:${meta.color || 'var(--t2)'}">${ef}</div><div class="l">已完成</div></div>
  <div class="st"><div class="v" style="color:${meta.color || 'var(--t2)'}">${er}</div><div class="l">进行中</div></div>
</div>
<div class="cd"><div class="cd-h"><h2>Event History</h2><span class="ts t3">最近 ${events.length} 条</span></div>${evtHtml}</div>`;

  el.innerHTML = html;
  // 预加载配置下拉（即使面板隐藏），确保首次打开 Configure 立即显示 skill/backend，不显示"加载中..."
  loadCfgOptions();
}

function _agentSubsSection(name, data) {
  const subs = data.subscribe_events || [];
  const prMerged = subs.filter(s => s && s.event_type === 'pull_request_merged');
  const prBranches = prMerged.map(s => (s.branch || '')).filter(b => b).join(', ');
  const prChecked = prMerged.length > 0 ? 'checked' : '';
  const displayName = (_AGENT_TYPES[name] && _AGENT_TYPES[name].display_name) || name;
  return `<div style="margin:16px 0 12px;border-top:1px solid var(--b);padding-top:12px">
    <div class="flex fc jbtw" style="margin-bottom:8px">
      <h3 style="font-size:13px;font-weight:600">事件订阅</h3>
      <span class="ts t3">默认不配置；勾选后触发即收到任务</span>
    </div>
    <div style="font-size:12px;margin-bottom:6px">
      <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input id="subPrMerged" type="checkbox" ${prChecked} style="accent-color:var(--a)"> PR 合入（pull_request_merged）
      </label>
    </div>
    <div class="srow"><label>合入分支</label><input id="subPrBranches" value="${prBranches}" placeholder="main, dev…（留空=不限分支）" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg);outline:none"></div>
    <div class="ts t3" style="margin-top:6px">当 PR 合入到上述分支时，${displayName} 自动收到任务；留空则任意分支合入都触发。</div>
  </div>`;
}

function saveAgentConfig(name) {
  const backend = document.querySelector('.cfg-backend')?.value || 'opencode';
  const skillVal = document.querySelector('.cfg-skill')?.value || '';
  let skill = skillVal, skillScope = '';
  if (skillVal.indexOf(':') > 0) {
    const _sp = skillVal.indexOf(':');
    skillScope = skillVal.slice(0, _sp);
    skill = skillVal.slice(_sp + 1);
  }
  const repo = app.currentRepo();
  if (!repo) return;
  const body = {backend, skill};
  if (skillScope) body.skill_scope = skillScope;
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
    app.showToast('配置已保存');
    toggleAgentCfg();
  }).catch(e => app.showToast('保存失败: ' + e.message));
}

function cleanAgentSession(name) {
  if (!confirm(`确认清理 ${name} Agent 的会话？\n\n将清除 backend_session_id，下一次事件到来时会创建全新会话上下文（不延续历史对话）。`)) return;
  const repo = app.currentRepo();
  if (!repo) return;
  API.post('/agents/' + name + '/' + repo.owner + '/' + repo.name + '/clean').then(r => {
    app.showToast(r.message || '会话已清理');
  }).catch(e => app.showToast('清理失败: ' + e.message));
}

function startVodOnboard() {
  const repo = app.currentRepo();
  if (!repo) { app.showToast('请先选择仓库'); return; }
  startFeishuOnboard('vod_bot', repo.fullName);
}