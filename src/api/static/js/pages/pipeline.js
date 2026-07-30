let _pipeData = null;
let _pipeActiveIdx = 0;
const agentColors = ['var(--pk)','var(--r)','var(--a)','var(--pu)','var(--or)'];
const agentLabels = ['VOD','Issue','Coding','QA','SRE'];
const agentIds = ['vod','issue','coding','qa','sre'];

function selPipe(idx) {
  _pipeActiveIdx = idx;
  renderPipeline(_pipeData);
}

function renderPipeline(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  _pipeData = data;
  const active = _pipeData.active;
  const history = _pipeData.history || [];

  let html = `<div class="cd">
  <div class="cd-h"><h2>Pipeline · ${data.repo || ''}</h2>
  <div class="flex gap12"><span class="ts t2">Active: <b>${data.active_count || 0}</b></span><span class="ts t2">Today: <b>${data.today_count || 0}</b></span></div></div>`;

  if (active) {
    const iss = active.issue_id || '#?';
    const stages = active.stages || [];
    const statuses = active.statuses || [];
    html += `<div style="margin-bottom:16px"><div class="ts" style="font-weight:600;margin-bottom:8px">Active: Issue ${iss}</div>
    <div class="af" style="min-height:auto;padding:6px 0">`;
    stages.forEach((stage, i) => {
      const sc = statuses[i];
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

  const durations = ['—','28m 12s','1h 05m','42m 18s'];
  const releases = ['—','v2.3.1','v2.3.0','v2.2.9'];
  const allPipes = active ? [active, ...history] : history;

  html += `<div class="tw"><table><tr><th>Pipeline</th><th>Duration</th><th>Agents</th><th>Result</th><th>Release</th></tr>`;
  allPipes.forEach((p, i) => {
    const s = i === _pipeActiveIdx ? ' sel' : '';
    const agentTags = ['vod','issue','coding','qa','sre'].map((at, j) => {
      const hasAgent = p.statuses && p.statuses[j] > 0;
      const cls = ['tg-v','tg-i','tg-c','tg-q','tg-s'][j];
      return `<span class="tg ${cls}" style="${hasAgent ? '' : 'opacity:0.4'}">${agentLabels[j].substring(0,1)}</span>`;
    }).join('');

    const resultClass = p.result === 'Released' || p.result === 'completed' ? 'tgx' : p.result === 'Failed' || p.result === 'failed' ? 'tr' : 'ty';
    const dur = durations[i] || '—';
    const rel = releases[i] || '—';
    html += `<tr class="pipe-row${s}" onclick="selPipe(${i})">
      <td><span class="ts"><b>${p.issue_id || '?'}</b></span></td>
      <td>${dur}</td>
      <td>${agentTags}</td>
      <td><span class="${resultClass}">${p.result || 'Processing'}</span></td>
      <td>${rel}</td></tr>`;
  });
  html += `</table></div>`;
  html += `</div><div class="tx t2 mt8">点击历史表中的 Issue 会切换上方工作流状态展示</div>`;

  el.innerHTML = html;
}
