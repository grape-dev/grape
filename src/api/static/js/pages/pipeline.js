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

  const allPipes = active ? [active, ...history] : history;
  // 当前选中的 Issue（默认 active，点击历史行后切换）
  const displayPipe = allPipes[_pipeActiveIdx] || active;

  if (displayPipe) {
    const iss = displayPipe.issue_id || '#?';
    const num = String(iss).replace(/^#/, '');
    // GitCode Issue 链接（点击跳转对应 issue）
    const repoParts = (data.repo || '').split('/');
    const issueUrl = repoParts.length === 2
      ? `https://gitcode.com/${repoParts[0]}/${repoParts[1]}/issues/${num}`
      : '';
    const issHtml = issueUrl
      ? `<a href="${issueUrl}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">Issue ${iss}</a>`
      : `Issue ${iss}`;
    const stages = displayPipe.stages || [];
    const statuses = displayPipe.statuses || [];
    const owner = displayPipe.current_owner || '';
    html += `<div style="margin-bottom:16px"><div class="ts" style="font-weight:600;margin-bottom:8px">Active: ${issHtml}${owner ? ` &nbsp;·&nbsp; 👤 当前责任人: <span style="color:var(--a)">${owner}</span>` : ''}</div>
    <div class="af" style="min-height:auto;padding:6px 0">`;
    stages.forEach((stage, i) => {
      const sc = statuses[i];
      const ev = stage.latest_event || {};
      const evTitle = ev.title || '';
      const dotc = sc === 2 ? 'var(--g)' : sc === 1 ? 'var(--y)' : sc === 3 ? 'var(--r)' : sc === 0 ? 'var(--t3)' : '#ddd';
      // pending（未涉及任务/未到处理阶段）：整体色调调暗（灰色背景 + 弱化文字），已处理/活跃阶段保持正常色调
      const dimStyle = sc === 0
        ? 'background:#eef0f4;border-top-color:#d5d9e0;filter:grayscale(0.6)'
        : `background:${sc === 1 ? '#fff8e8' : '#fff'};border-top-color:${agentColors[i]}`;
      html += `<div class="nd" style="padding:10px;width:150px;cursor:pointer;${dimStyle}${sc === 0 ? ';opacity:.55' : ''}" onclick="app.nav('${agentIds[i]}')">
        <div class="nh"><span class="n" style="background:${sc === 0 ? '#c3c9d4' : agentColors[i]}"></span><h4 style="font-size:11px;${sc === 0 ? 'color:#9aa3b2' : ''}">${agentLabels[i]}</h4></div>
        <div class="nds" style="margin-bottom:1px;${sc === 0 ? 'color:#a8b0bd' : ''}">${ev ? evTitle || ev.event_type || '—' : '—'}</div>
        <div class="nds" style="margin-bottom:1px;font-size:9px;${sc === 0 ? 'color:#a8b0bd' : ''}">${ev && ev.actor ? '@' + ev.actor : ' '}</div>
        <div class="ns"><span class="d" style="background:${dotc}"></span>${sc === 2 ? 'Done' : sc === 1 ? 'In Progress' : sc === 3 ? 'Failed' : sc === 0 ? 'Pending' : '—'}</div>
      </div>`;
    });
    html += `</div></div>`;
  }

  const durations = ['—','28m 12s','1h 05m','42m 18s'];

  html += `<div class="tw"><table><tr><th>Issue</th><th>Duration</th><th>Agents</th><th>Result</th><th>责任人</th></tr>`;
  allPipes.forEach((p, i) => {
    const s = i === _pipeActiveIdx ? ' sel' : '';
    const agentTags = ['vod','issue','coding','qa','sre'].map((at, j) => {
      const hasAgent = p.statuses && p.statuses[j] > 0;
      const cls = ['tg-v','tg-i','tg-c','tg-q','tg-s'][j];
      return `<span class="tg ${cls}" style="${hasAgent ? '' : 'opacity:0.4'}">${agentLabels[j].substring(0,1)}</span>`;
    }).join('');

    const resultClass = p.result === 'Released' || p.result === 'completed' ? 'tgx' : p.result === 'Failed' || p.result === 'failed' ? 'tr' : 'ty';
    const dur = durations[i] || '—';
    const owner = p.current_owner || '—';
    html += `<tr class="pipe-row${s}" onclick="selPipe(${i})">
      <td><span class="ts"><b>${p.issue_id || '?'}</b></span></td>
      <td>${dur}</td>
      <td>${agentTags}</td>
      <td><span class="${resultClass}">${p.result || 'Processing'}</span></td>
      <td>${owner}</td></tr>`;
  });
  html += `</table></div>`;
  html += `</div><div class="tx t2 mt8">点击历史表中的 Issue 会切换上方工作流状态展示</div>`;

  el.innerHTML = html;
}
