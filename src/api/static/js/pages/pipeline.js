function renderPipeline(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const active = data.active;
  const history = data.history || [];
  const agentColors = ['var(--pk)','var(--r)','var(--a)','var(--pu)','var(--or)'];
  const agentLabels = ['VOD','Issue','Coding','QA','SRE'];
  const agentIds = ['vod','issue','coding','qa','sre'];

  let html = `<div class="cd">
  <div class="cd-h"><h2>Pipeline · ${data.repo || ''}</h2>
  <div class="flex gap12"><span class="ts t2">Active: <b>${data.active_count || 0}</b></span>
  <span class="ts t2">Today: <b>${data.today_count || 0}</b></span></div></div>`;

  if (active) {
    html += `<div style="margin-bottom:16px"><div class="ts" style="font-weight:600;margin-bottom:8px">Active: Issue ${active.issue_id}</div>
    <div class="af" style="min-height:auto;padding:6px 0">`;
    active.stages.forEach((stage, i) => {
      const sc = active.statuses[i];
      const dotc = sc === 2 ? 'var(--g)' : sc === 1 ? 'var(--y)' : sc === 3 ? 'var(--r)' : sc === 0 ? 'var(--t3)' : '#ddd';
      html += `<div class="nd" style="padding:10px;width:150px;border-top:3px solid ${agentColors[i]};cursor:pointer${sc === 0 ? ';opacity:.5' : ''}" onclick="app.nav('${agentIds[i]}')">
        <div class="nh"><span class="n" style="background:${agentColors[i]}"></span><h4 style="font-size:11px">${agentLabels[i]}</h4></div>
        <div class="nds" style="margin-bottom:1px">${stage[0]}</div>
        <div class="nds" style="margin-bottom:1px;font-size:9px">${stage[1]}</div>
        <div class="ns"><span class="d" style="background:${dotc}"></span>${sc === 2 ? 'Done' : sc === 1 ? 'In Progress' : sc === 3 ? 'Failed' : sc === 0 ? 'Pending' : '—'}</div>
      </div>`;
    });
    html += `</div></div>`;
  }

  if (history.length > 0) {
    html += `<div class="tw"><table><tr><th>Pipeline</th><th>Event Count</th><th>Agents</th><th>Result</th></tr>`;
    history.forEach(p => {
      const agentTags = (p.agents || []).map(a => {
        const idx = agentLabels.indexOf(a);
        const cls = ['tg-v','tg-i','tg-c','tg-q','tg-s'][idx];
        return `<span class="tg ${cls}">${a.substring(0,1)}</span>`;
      }).join('');
      html += `<tr><td><span class="ts"><b>Issue ${p.issue_id}</b></span></td>
        <td>${p.event_count || 0}</td>
        <td>${agentTags || '—'}</td>
        <td><span class="${p.result === 'Released' ? 'tgx' : p.result === 'Failed' ? 'tr' : 'ty'}">${p.result || '—'}</span></td></tr>`;
    });
    html += `</table></div>`;
  }
  html += `</div>`;
  el.innerHTML = html;
}
