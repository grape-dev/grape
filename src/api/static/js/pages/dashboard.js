function renderDashboard(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const a = data.agents || [];
  const act = data.activity || [];
  const m = data.metrics || {};
  const agentColors = {vod:'var(--pk)',issue:'var(--r)',coding:'var(--a)',qa:'var(--pu)',sre:'var(--or)'};
  const agentBg = {vod:'#fde8f0',issue:'#fce5e5',coding:'#e3edfd',qa:'#f3e9fe',sre:'#fef0e4'};

  let html = `<div class="cd"><div class="cd-h"><h2>Dashboard · ${data.repo || ''}</h2></div>
  <div class="stats">${Object.entries({total_events:'总事件',active_agents:'活跃 Agent',pending_events:'待处理',failed_events:'失败'}).map(([k,v]) =>
    `<div class="st"><div class="v">${m[k] || 0}</div><div class="l">${v}</div></div>`
  ).join('')}</div></div>`;

  html += `<div class="cd"><div class="cd-h"><h2>DevOps Agent Workflow</h2><span class="ts t3">VOD → Issue → Coding → QA → SRE</span></div>
  <div class="af">`;
  ['vod','issue','coding','qa','sre'].forEach((at, i) => {
    const ag = a.find(x => x.agent_type === at) || {};
    const st = ag.status || 'idle';
    const statusDot = st === 'completed' ? 'var(--g)' : st === 'running' ? 'var(--y)' : st === 'failed' ? 'var(--r)' : '#ddd';
    const statusLabel = st === 'completed' ? 'Done' : st === 'running' ? 'Active' : st === 'failed' ? 'Failed' : 'Idle';
    html += `<div class="nd" onclick="app.nav('${at}')" style="border-top:3px solid ${agentColors[at]}">
      <div class="nh"><span class="n" style="background:${agentColors[at]}"></span><h4>${['VOD','Issue','Coding','QA','SRE'][i]}</h4></div>
      <div class="nds">${ag.last_active_at ? 'Last: ' + ag.last_active_at.substring(0,10) : 'Not started'}</div>
      <div class="ns"><span class="d" style="background:${statusDot}"></span>${statusLabel}</div>
    </div>`;
  });
  html += `</div></div>`;

  if (act.length > 0) {
    html += `<div class="cd"><div class="cd-h"><h2>Recent Activity</h2></div>`;
    act.forEach(ev => {
      html += `<div class="ai" onclick="app.nav('event',${ev.seq})" style="cursor:pointer">
        <div class="t">${(ev.created_at || '').substring(11,16)}</div>
        <div class="ac"><span class="tg tg-${(ev.agent_type || 'd').substring(0,1)}">${ev.event_type}</span>
        <b>${ev.title}</b> · <span class="${ev.status === 'failed' ? 'tr' : ev.status === 'completed' ? 'tgx' : 'ty'}">${ev.status}</span></div>
      </div>`;
    });
    html += '</div>';
  }

  el.innerHTML = html;
}
