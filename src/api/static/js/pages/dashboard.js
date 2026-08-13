function renderDashboard(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const a = data.agents || [];
  const act = data.activity || [];
  const m = data.metrics || {};
  const repo = data.repo || '';
  const role = data.role || 'Member';
  const agentCounts = data.agent_event_counts || {};
  const scenes = data._scenes || [];
  const owner = data._owner || '';
  const name = data._name || '';
  const sceneOpts = scenes.map(s => `<option value="${s.id}">${s.name}</option>`).join('');

  const agentOrder = ['vod','issue','coding','qa','sre'];
  const agentLabels = ['VOD Agent','Issue Agent','Coding Agent','QA Agent','SRE Agent'];
  const recentByAgent = data.agent_recent_events || {};

  let html = `<div class="cd" style="background:linear-gradient(135deg,#eef3ff,#eefdf2);border-color:#c6d9f7">
    <div class="flex fc gap12">
      <div style="font-size:26px">■</div>
      <div class="f1">
        <div style="font-size:14px;font-weight:600">${owner}/${name} <span class="tg tg-c" style="font-size:9px;vertical-align:middle">${role}</span></div>
        <div class="ts t2">${role === 'Maintainer' ? 'Maintainer' : 'Member'} · ★ main · Webhook OK</div>
      </div>
      <div class="stats">
        <div class="st"><div class="v" style="color:var(--g)">${m.auto_resolution || '98.5'}%</div><div class="l">Auto-Resolution</div></div>
        <div class="st"><div class="v" style="color:var(--a)">${m.total_events || '47'}</div><div class="l">Events/Week</div></div>
        <div class="st"><div class="v" style="color:var(--pu)">${m.avg_response || '12'}s</div><div class="l">Avg Response</div></div>
      </div>
    </div>
  </div>`;

  html += `<div class="cd">
    <div class="cd-h"><h2>DevOps Agent Workflow</h2>
      <div class="flex fc gap8">
        <span class="ts t2">开发场景:</span>
        <select id="sceneSelect" style="padding:4px 8px;border-radius:6px;border:1px solid var(--b);font-size:11px;background:var(--bg);color:var(--t);outline:none">${sceneOpts}</select>
        <button class="bt bt-p bt-s" onclick="applyScene('${owner}', '${name}')">应用</button>
      </div>
    </div>
    <div class="af">`;

  agentOrder.forEach((at, i) => {
    const ag = a.find(x => x.agent_type === at) || {};
    const meta = _AGENT_TYPES[at] || {};
    const st = ag.status || '';
    const hasSession = !!ag.status;
    const statusDot = st === 'failed' ? 'var(--r)' : st === 'running' ? 'var(--y)' : hasSession ? 'var(--g)' : '#ddd';
    const statusLabel = st === 'completed' ? 'Done' : st === 'running' ? 'Active' : st === 'failed' ? 'Failed' : hasSession ? 'Active' : '—';
    const backend = ag.backend || meta.default_backend || 'opencode';
    const count = agentCounts[at] || 0;
    const nsText = count > 0 ? count + ' events today' : hasSession ? (st === 'running' ? 'Processing...' : 'Idle') : '—';
    const ntCls = at === 'vod' ? 'tg-v' : at === 'issue' ? 'tg-i' : at === 'coding' ? 'tg-c' : at === 'qa' ? 'tg-q' : 'tg-s';
    const colors = {vod:'var(--pk)',issue:'var(--r)',coding:'var(--a)',qa:'var(--pu)',sre:'var(--or)'};
    const descs = {vod:'从开发者声音中收集反馈、聚类分析共性、提炼价值、自动创建 Issue',issue:'Issue 分类、标签、指派、A2A 触发 Coding Agent',coding:'代码实现、PR 创建、自我审查',qa:'代码审查、自动化测试、功能验证、安全检查',sre:'构建部署、发布管理、监控运维、故障排查'};
    const borderColor = meta.color || colors[at] || 'var(--t2)';
    const dotColor = meta.color || colors[at] || 'var(--t2)';
    const desc = meta.description || descs[at] || '';
    const enabled = ag.enable !== false;   // 默认启用；仓库级 enable

    // 该 Agent 最近 2 个事件（替代假数据）
    const evs = (recentByAgent[at] || []).slice(0, 2);
    let evHtml = '';
    if (evs.length > 0) {
      evHtml = evs.map(ev => {
        const stCls = ev.status === 'failed' ? 'tr' : ev.status === 'running' ? 'ty' : ev.status === 'completed' ? 'tgx' : 't2';
        const stTxt = ev.status === 'completed' ? 'Done' : ev.status === 'running' ? 'In Progress' : ev.status === 'failed' ? 'Failed' : ev.status || '—';
        return `<div style="font-size:9px;padding:1px 0;border-bottom:1px dashed var(--b);display:flex;justify-content:space-between;gap:4px">
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${ev.summary || ev.title || ev.event_type}</span>
          <span class="${stCls}" style="flex-shrink:0">${stTxt}</span>
        </div>`;
      }).join('');
    } else {
      evHtml = '<div class="t2" style="font-size:9px">暂无事件</div>';
    }

    // Agent 启用开关（仅 Maintainer 可切换）
    const toggleBtn = `<button onclick="event.stopPropagation();toggleAgentEnable('${at}', ${!enabled})"
      style="cursor:pointer;border:1px solid var(--b);border-radius:10px;padding:1px 8px;font-size:9px;
             background:${enabled ? 'var(--g)' : 'var(--t3)'};color:#fff;margin-left:auto"
      title="${enabled ? '点击停用' : '点击启用'}">${enabled ? '✓ 开启' : '○ 停用'}</button>`;

    html += `<div class="nd" style="border-top:3px solid ${borderColor};${enabled ? '' : 'opacity:0.55'}" onclick="app.nav('${at}')">
      <div class="nh"><span class="n" style="background:${dotColor}"></span><h4>${meta.display_name || agentLabels[i]}</h4>${toggleBtn}</div>
      ${enabled ? `<div class="nds">${desc}</div>` : '<div class="t2" style="font-size:10px;margin:4px 0">已停用</div>'}
      <div class="nm"><span class="ml">Backend</span><span class="mv">${backend}</span></div>
      <div class="ns"><span class="d" style="background:${statusDot}"></span>${nsText}</div>
      <div class="nt ${ntCls}">${evHtml}</div>
    </div>`;
  });

  // 开关交互
  window.toggleAgentEnable = function(agentType, enable) {
    const role = (data.role || '').toLowerCase();
    if (role !== 'maintainer' && role !== 'admin') { app.showToast('仅 Maintainer 可切换 Agent 开关'); return; }
    const repo = app.currentRepo();
    if (!repo) { app.showToast('请先选择仓库'); return; }
    const body = { enable: enable };
    API.put('/agents/' + agentType + '/' + repo.owner + '/' + repo.name, body).then(() => {
      app.showToast((enable ? '已启用 ' : '已停用 ') + (agentLabels[agentOrder.indexOf(agentType)] || agentType));
      renderDashboard(data);   // 刷新卡片态（重新拉取）
      API.get('/dashboard/' + repo.owner + '/' + repo.name).then(renderDashboard).catch(() => {});
    }).catch(e => app.showToast('切换失败: ' + (e.detail?.message || e.message)));
  };

  html += `</div>
    <div class="cl" onclick="app.nav('chat')">
      <div class="ci">☎</div>
      <div class="cx"><h4>Chat Agent · Personal Assistant</h4><p>Your personal agent connected to the workflow · Submit issues · Query status · Approve</p></div>
      <div class="flex gap6"><span class="tg tg-ch">↔ VOD</span><span class="tg tg-ch">↔ Issue</span><span class="tg tg-ch">↔ Coding</span><span class="tg tg-ch">↔ QA</span><span class="tg tg-ch">↔ SRE</span></div>
    </div>
  </div>`;

  if (act.length > 0) {
    html += `<div class="cd"><div class="cd-h"><h2>Recent Activity</h2><span class="ts t3">${owner}/${name} · Last 24h</span></div>`;
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
