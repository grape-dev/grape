function renderDashboard(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const a = data.agents || [];
  const act = data.activity || [];
  const m = data.metrics || {};
  const repo = data.repo || '';
  const agentColors = {vod:'var(--pk)',issue:'var(--r)',coding:'var(--a)',qa:'var(--pu)',sre:'var(--or)'};
  const agentBg = {vod:'#fde8f0',issue:'#fce5e5',coding:'#e3edfd',qa:'#f3e9fe',sre:'#fef0e4'};
  const agentBackend = {vod:'Hermes',issue:'OpenCode',coding:'CodeArts',qa:'OpenCode',sre:'OpenCode'};
  const agentTag = ['tg-v','tg-i','tg-c','tg-q','tg-s'];

  let html = `<div class="cd" style="background:linear-gradient(135deg,#eef3ff,#eefdf2);border-color:#c6d9f7">
    <div class="flex fc gap12">
      <div style="font-size:26px">■</div>
      <div class="f1">
        <div style="font-size:14px;font-weight:600">${repo} <span class="tg tg-c" style="font-size:9px;vertical-align:middle">${(a[0] && a[0].status) || 'Active'}</span></div>
        <div class="ts t2">DevOps Bot · ★ main · Webhook OK</div>
      </div>
      <div class="stats">
        <div class="st"><div class="v" style="color:var(--g)">${m.auto_resolution || '98.5'}%</div><div class="l">Auto-Resolution</div></div>
        <div class="st"><div class="v" style="color:var(--a)">${m.total_events || '47'}</div><div class="l">Events/Week</div></div>
        <div class="st"><div class="v" style="color:var(--pu)">${m.avg_response || '12'}s</div><div class="l">Avg Response</div></div>
      </div>
    </div>
  </div>`;

  html += `<div class="cd">
    <div class="cd-h"><h2>DevOps Agent Workflow</h2><span class="ts t3">VOD → Issue → Coding → QA → SRE</span></div>
    <div class="af">`;
  ['vod','issue','coding','qa','sre'].forEach((at, i) => {
    const ag = a.find(x => x.agent_type === at) || {};
    const st = ag.status || 'idle';
    const statusDot = st === 'completed' ? 'var(--g)' : st === 'running' ? 'var(--y)' : st === 'failed' ? 'var(--r)' : '#ddd';
    const label = ['VOD Agent','Issue Agent','Coding Agent','QA Agent','SRE Agent'][i];
    const desc = ['从开发者声音中收集反馈, 聚类分析, 提炼价值',
                  '处理 Issue, 分类打标签, 指派负责人',
                  '分析需求 → 设计 → 编码 → PR',
                  'Code Review, 测试验证, 质量检查',
                  '构建部署, 发布管理, 监控运维'][i];
    const tagText = ['🎤 feedback→issue','🎫 2 bugs · 1 feature','⚙ implementing...','✔ pass: 92%','🚀 uptime 99.9%'][i];
    html += `<div class="nd" style="border-top:3px solid ${agentColors[at]}" onclick="app.nav('${at}')">
      <div class="nh"><span class="n" style="background:${agentColors[at]}"></span><h4>${label}</h4></div>
      <div class="nds">${desc}</div>
      <div class="nm"><span class="ml">Backend</span><span class="mv">${agentBackend[at]}</span></div>
      <div class="ns"><span class="d" style="background:${statusDot}"></span>${ag.last_active_at ? 'Active' : 'Idle'}</div>
      <div class="nt ${agentTag[i]}">${tagText}</div>
    </div>`;
  });
  html += `</div>
    <div class="cl" onclick="app.nav('chat')">
      <div class="ci">☎</div>
      <div class="cx"><h4>Chat Agent · Personal Assistant</h4><p>Your personal agent connected to the workflow · Submit issues · Query status · Approve</p></div>
      <div class="flex gap6"><span class="tg tg-ch">↔ VOD</span><span class="tg tg-ch">↔ Issue</span><span class="tg tg-ch">↔ Coding</span><span class="tg tg-ch">↔ QA</span><span class="tg tg-ch">↔ SRE</span></div>
    </div>
  </div>`;

  if (act.length > 0) {
    let acts = act.slice(0, 5);
    html += `<div class="cd"><div class="cd-h"><h2>Recent Activity</h2><span class="ts t3">${repo} · Last 24h</span></div>`;
    acts.forEach(ev => {
      const agentChar = (ev.agent_type || 'd')[0];
      const colorClass = ev.agent_type === 'vod' ? 'tg-v' : ev.agent_type === 'issue' ? 'tg-i' : ev.agent_type === 'coding' ? 'tg-c' : ev.agent_type === 'qa' ? 'tg-q' : ev.agent_type === 'sre' ? 'tg-s' : 'tg-d';
      html += `<div class="ai" onclick="app.nav('event',${ev.seq})" style="cursor:pointer">
        <div class="t">${(ev.created_at || '').substring(11,16) || '--:--'}</div>
        <div class="ac"><span class="tg ${colorClass}">${(ev.agent_type || '?').toUpperCase().substring(0,4)}</span><b>${ev.title}</b> · <span class="t2">${ev.event_type}</span></div>
      </div>`;
    });
    html += '</div>';
  }

  el.innerHTML = html;
}
