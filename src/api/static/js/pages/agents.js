let _agentCfgVisible = false;

function toggleAgentCfg() {
  _agentCfgVisible = !_agentCfgVisible;
  const p = document.getElementById('agentCfgPanel');
  if (p) p.classList.toggle('hidden');
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
      <div class="t">${(ev.created_at || '').substring(11,16) || '--:--'}</div>
      <div class="ac">${ev.title || ev.event_type} <span class="ts ${ev.status === 'failed' ? 'tr' : ev.status === 'completed' ? 'tgx' : 'ty'}">${ev.status}</span></div>
    </div>`
  ).join('') : '<div class="tx t2">暂无事件</div>';

  const ef = es.completed || 0;
  const er = es.running || 0;
  const ep = es.pending || 0;
  const et = m.total_events || 0;

  // Fetch backends and skills for the config panel
  Promise.all([
    API.get('/settings/backend-agents').catch(() => ({backends: []})),
    API.get('/skills').catch(() => ({skills: []})),
  ]).then(([backendData, skillData]) => {
    const backends = backendData.backends || [];
    const allSkills = skillData.skills || [];
    const agentSkills = allSkills.filter(s => s.agent_type === name || s.parent_agent_type === name);

    const currentBackend = data.backend || meta.default_backend || 'opencode';
    const currentSkill = data.skill || '';

    const backendOpts = backends.length > 0
      ? backends.map(b => `<option value="${b.name}" ${b.name === currentBackend ? 'selected' : ''}>${b.name}${b.healthy ? ' ✓' : ' ✗'}</option>`).join('')
      : `<option value="${currentBackend}" selected>${currentBackend}</option>`;

    const skillOpts = agentSkills.length > 0
      ? `<option value="">默认官方技能（自动）</option>${agentSkills.map(s => `<option value="${s.name}" ${s.name === currentSkill ? 'selected' : ''}>${s.display_name || s.name}</option>`).join('')}`
      : '<option value="">默认官方技能（自动）</option>';

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
    <select class="cfg-backend" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)">${backendOpts}</select>
  </div>
  <div class="srow"><label>Skill</label>
    <select class="cfg-skill" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)">${skillOpts}</select>
  </div>
  <div style="font-size:11px;color:var(--t3);margin-top:4px">${agentSkills.length > 0 ? `可选 ${agentSkills.length} 个 ${meta.display_name || name} 技能` : '无专用技能，使用默认官方技能'}</div>
  <div class="flex gap6 mt16"><button class="bt bt-p bt-s" onclick="saveAgentConfig('${name}')">Save</button><button class="bt bt-o bt-s" onclick="toggleAgentCfg()">Cancel</button></div>
</div>
<div class="stats mt16 mb16">
  <div class="st"><div class="v" style="color:${meta.color || 'var(--t2)'}">${et}</div><div class="l">事件总量</div></div>
  <div class="st"><div class="v" style="color:${meta.color || 'var(--t2)'}">${ef}</div><div class="l">已完成</div></div>
  <div class="st"><div class="v" style="color:${meta.color || 'var(--t2)'}">${er}</div><div class="l">进行中</div></div>
</div>
<div class="cd"><div class="cd-h"><h2>Event History</h2><span class="ts t3">最近 ${events.length} 条</span></div>${evtHtml}</div>`;

    el.innerHTML = html;
  });
}

function saveAgentConfig(name) {
  const backend = document.querySelector('.cfg-backend')?.value || 'opencode';
  const skill = document.querySelector('.cfg-skill')?.value || '';
  const repo = app.currentRepo();
  if (!repo) return;
  API.put('/agents/' + name + '/' + repo.owner + '/' + repo.name, {backend, skill}).then(r => {
    app.showToast('配置已保存');
    toggleAgentCfg();
  }).catch(e => app.showToast('保存失败: ' + e.message));
}