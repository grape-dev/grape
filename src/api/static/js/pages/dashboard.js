function renderDashboard(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const a = data.agents || [];
  const act = data.activity || [];
  const m = data.metrics || {};
  const repo = data.repo || '';
  const scenes = data._scenes || [];
  const owner = data._owner || '';
  const name = data._name || '';
  const sceneOpts = scenes.map(s => `<option value="${s.id}">${s.name}</option>`).join('');

  const agentOrder = ['vod','issue','coding','qa','sre'];
  const agentLabels = ['VOD Agent','Issue Agent','Coding Agent','QA Agent','SRE Agent'];

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
    const st = ag.status || 'idle';
    const statusDot = st === 'completed' ? 'var(--g)' : st === 'running' ? 'var(--y)' : st === 'failed' ? 'var(--r)' : '#ddd';
    const statusLabel = st === 'completed' ? 'Done' : st === 'running' ? 'Active' : st === 'failed' ? 'Failed' : 'Idle';
    const backend = ag.backend || meta.default_backend || 'OpenCode';

    html += `<div class="nd" style="border-top:3px solid ${meta.color || 'var(--t2)'}" onclick="app.nav('${at}')">
      <div class="nh"><span class="n" style="background:${meta.color || 'var(--t2)'}"></span><h4>${meta.display_name || agentLabels[i]}</h4></div>
      <div class="nds">${meta.description || ''}</div>
      <div class="nm"><span class="ml">Backend</span><span class="mv">${backend}</span></div>
      <div class="ns"><span class="d" style="background:${statusDot}"></span>${statusLabel}</div>
      <div class="nt" style="background:${meta.bg || 'var(--bg)'};color:${meta.color || 'var(--t2)'}">${statusLabel}</div>
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
    html += `<div class="cd"><div class="cd-h"><h2>Recent Activity</h2><span class="ts t3">${repo} · Last 24h</span></div>`;
    act.forEach(ev => {
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
