const AGENT_META = {
  vod:{display:'VOD Agent',icon:'🔍',color:'var(--pk)',bg:'#fde8f0',label:'VOD'},
  issue:{display:'Issue Agent',icon:'📋',color:'var(--r)',bg:'#fce5e5',label:'Issue'},
  coding:{display:'Coding Agent',icon:'💻',color:'var(--a)',bg:'#e3edfd',label:'Coding'},
  qa:{display:'QA Agent',icon:'🧪',color:'var(--pu)',bg:'#f3e9fe',label:'QA'},
  sre:{display:'SRE Agent',icon:'⚙️',color:'var(--or)',bg:'#fef0e4',label:'SRE'},
};

function renderAgent(name, data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const meta = AGENT_META[name] || {display:name,icon:'?',color:'var(--t2)',bg:'var(--bg)',label:name};
  const m = data.metrics || {};
  const es = m.event_stats || {};
  const events = data.recent_events || [];
  const skills = data.skills || [];

  let html = `<div class="cd"><div class="cd-h">
    <h2><span style="background:${meta.color};color:#fff;padding:2px 7px;border-radius:4px;font-size:10px">${meta.label}</span> ${meta.display}</h2>
    <span class="ts t3">${data.backend || 'opencode'} · ${data.status || 'idle'}</span></div>
  <p class="ts t2 mb8">${data.description || ''}</p>
  <div class="stats">${Object.entries({total_events:'Total',completed:'Done',running:'Active',failed:'Failed'}).map(([k,v]) =>
    `<div class="st"><div class="v">${k === 'total_events' ? m.total_events || 0 : es[k] || 0}</div><div class="l">${v}</div></div>`
  ).join('')}</div>`;

  if (data.oc_session_id) {
    html += `<div class="ts t3 mt8">Session: ${data.oc_session_id.substring(0,24)}...</div>`;
  }
  html += `</div>`;

  if (events.length > 0) {
    html += `<div class="cd"><div class="cd-h"><h2>Recent Events</h2></div>`;
    events.forEach(ev => {
      html += `<div class="ai" onclick="app.nav('event',${ev.seq})" style="cursor:pointer">
        <div class="t">${(ev.created_at || '').substring(11,16)}</div>
        <div class="ac"><b>${ev.title}</b> · <span class="${ev.status === 'failed' ? 'tr' : ev.status === 'completed' ? 'tgx' : 'ty'}">${ev.status}</span></div>
      </div>`;
    });
    html += `</div>`;
  }

  if (skills.length > 0) {
    html += `<div class="cd"><div class="cd-h"><h2>Available Skills (${skills.length})</h2></div>
    <div style="display:flex;flex-wrap:wrap;gap:6px">`;
    skills.forEach(s => {
      html += `<span class="tg tg-d">${s.name}</span>`;
    });
    html += `</div></div>`;
  }

  html += `<div class="cd"><div class="cd-h"><h2>Config</h2></div>
  <div class="srow"><label>Backend</label><select id="agentBackend">
    <option value="opencode" ${data.backend === 'opencode' ? 'selected' : ''}>OpenCode</option>
    <option value="codearts" ${data.backend === 'codearts' ? 'selected' : ''}>CodeArts</option>
  </select></div>
  <button class="bt bt-p" onclick="saveAgentConfig('${name}')">Save</button></div>`;

  el.innerHTML = html;
}

function saveAgentConfig(name) {
  const backend = document.getElementById('agentBackend').value;
  const repo = app.currentRepo();
  if (!repo) return;
  API.put('/agents/' + name + '/' + repo.owner + '/' + repo.name, {backend}).then(r => {
    app.showToast('配置已保存');
  }).catch(e => app.showToast('保存失败: ' + e.message));
}
