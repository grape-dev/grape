let _pipeData = null;
let _pipeActiveIdx = 0;
let _pipePage = 1;
const _PIPE_PER_PAGE = 15;
const agentColors = ['var(--pk)','var(--r)','var(--a)','var(--pu)','var(--or)'];
const agentLabels = ['VOD','Issue','Coding','QA','SRE'];
const agentIds = ['vod','issue','coding','qa','sre'];
const stageLabels = {vod:'VOD',issue:'Issue',coding:'Coding',qa:'QA',sre:'SRE',done:'Done'};
const _TAG_COLORS = {vod:'var(--pk)',issue:'var(--r)',coding:'var(--a)',qa:'var(--pu)',sre:'var(--or)',chat:'var(--sk)'};
const _MODE_ICONS = {emit:'\u2192',waterfall:'\u25c7',serial:'\u21bb',parallel:'\u21c4'};

function _getPipeNodes(data, pipe) {
  // 优先用 Issue 级 pipeline_config（按 scene_id 对应的节点列表）
  var cfg = pipe && pipe.pipeline_config;
  if (!cfg || !cfg.nodes || cfg.nodes.length === 0) {
    // 回退到仓库级 pipeline_config
    cfg = data && data.pipeline_config;
  }
  if (cfg && cfg.nodes && cfg.nodes.length > 0) return cfg.nodes;
  // Fallback: construct from hardcoded arrays
  return agentIds.map(function(id, i) {
    return {id: id, tag: id, skill: '', mode: 'emit', next: i < agentIds.length-1 ? agentIds[i+1] : 'done', handoff: i < agentIds.length-1 ? 'a2a' : 'complete'};
  });
}

function selPipe(idx) {
  _pipeActiveIdx = idx;
  // 只更新流程图区域 + 高亮选中行，不重建表格（保持滚动位置）
  const data = _pipeData;
  if (!data) return;
  const active = data.active;
  const history = data.history || [];
  const allPipes = active ? [active, ...history] : history;
  const displayPipe = allPipes[idx] || active;
  if (!displayPipe) return;

  // 重建流程图 HTML
  const iss = displayPipe.issue_id || '#?';
  const num = String(iss).replace(/^#/, '');
  const repoParts = (data.repo || '').replace(/^gitcode:/, '').split('/');
  const issueUrl = repoParts.length === 2 ? `https://gitcode.com/${repoParts[0]}/${repoParts[1]}/issues/${num}` : '';
  const issHtml = issueUrl ? `<a href="${issueUrl}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">Issue ${iss}</a>` : `Issue ${iss}`;
  const stages = displayPipe.stages || [];
  const statuses = displayPipe.statuses || [];
  const owner = displayPipe.current_owner || '';
  const execBadge = displayPipe.executing ? ' &nbsp;⚡<span class="ts t2" style="color:var(--g)">执行中</span>' : ' &nbsp;<span class="ts t2">空闲</span>';

  let flowHtml = `<div class="ts" style="font-weight:600;margin-bottom:8px">Active: ${issHtml}${displayPipe.title ? ` &nbsp;·&nbsp; <span style="color:var(--t2);font-weight:500">${displayPipe.title}</span>` : ''}${owner ? ` &nbsp;\u00b7&nbsp; \ud83d\udc64 \u5f53\u524d\u8d23\u4efb\u4eba: <span style="color:var(--a)">${owner}</span>` : ''}${execBadge}</div>
    <div class="af" style="min-height:auto;padding:6px 0">`;
  // Dynamic pipeline nodes (event-dispatch-redesign-v2.md Phase 2)
  var pipeNodes = _getPipeNodes(data, displayPipe);
  pipeNodes.forEach(function(node, i) {
    var tag = node.tag || node.id;
    var sc = statuses[i] || 0;
    var ev = (stages[i] && stages[i].latest_event) || {};
    var evTitle = ev.title || '';
    var dotc = sc === 2 ? 'var(--g)' : sc === 1 ? 'var(--y)' : sc === 3 ? 'var(--r)' : 'var(--t3)';
    var color = _TAG_COLORS[tag] || agentColors[i % agentColors.length];
    var label = (node.id || tag).charAt(0).toUpperCase() + (node.id || tag).slice(1);
    var modeIcon = _MODE_ICONS[node.mode] || _MODE_ICONS.emit;
    var dimStyle = sc === 0 ? 'background:#eef0f4;border-top-color:#d5d9e0;filter:grayscale(0.6)' : 'background:' + (sc === 1 ? '#fff8e8' : '#fff') + ';border-top-color:' + color;
    flowHtml += `<div class="nd${sc === 1 ? ' nd-radar' : ''}" style="padding:10px;width:150px;cursor:pointer;${dimStyle}${sc === 0 ? ';opacity:.55' : ''}" onclick="app.nav('${tag}')">
      <div class="nh"><span class="n" style="background:${sc === 0 ? '#c3c9d4' : color}"></span><h4 style="font-size:11px;${sc === 0 ? 'color:#9aa3b2' : ''}">${label} <span style="font-size:9px;color:var(--t3)">${modeIcon}</span></h4></div>
      <div class="nds" style="margin-bottom:1px;${sc === 0 ? 'color:#a8b0bd' : ''}">${ev ? evTitle || ev.event_type || '\u2014' : '\u2014'}</div>
      <div class="nds" style="margin-bottom:1px;font-size:9px;${sc === 0 ? 'color:#a8b0bd' : ''}">${ev && ev.actor ? '@' + ev.actor : ' '}</div>
      <div class="ns"><span class="d${sc === 1 ? ' d-pulse' : ''}" style="background:${dotc}"></span>${sc === 2 ? 'Done' : sc === 1 ? 'In Progress' : sc === 3 ? 'Failed' : 'Pending'}</div>
    </div>`;
  });
  flowHtml += `</div>`;

  // 替换流程图区域（表格不动，保持滚动位置）
  const flowEl = document.getElementById('pipeFlow');
  if (flowEl) flowEl.innerHTML = flowHtml;

  // 更新选中行高亮（不重建表格）
  document.querySelectorAll('.pipe-row').forEach(tr => tr.classList.remove('sel'));
  const selRow = document.querySelector(`.pipe-row[data-idx="${idx}"]`);
  if (selRow) selRow.classList.add('sel');
}

function renderPipeline(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  _pipeData = data;
  const active = _pipeData.active;
  const history = _pipeData.history || [];

  let html = `<div class="cd" style="display:flex;flex-direction:column;height:calc(100vh - 120px);overflow:hidden">
  <div class="cd-h" style="flex-shrink:0"><h2>Pipeline · ${data.repo || ''}</h2>
  <div class="flex gap12"><span class="ts t2">Active: <b>${data.active_count || 0}</b></span><span class="ts t2">Today: <b>${data.today_count || 0}</b></span></div></div>`;

  const allPipes = active ? [active, ...history] : history;
  // 当前选中的 Issue（默认 active，点击历史行后切换）
  const displayPipe = allPipes[_pipeActiveIdx] || active;

  // 流程图固定区
  let flowHtml = '';
  if (displayPipe) {
    const iss = displayPipe.issue_id || '#?';
    const num = String(iss).replace(/^#/, '');
    const repoParts = (data.repo || '').replace(/^gitcode:/, '').split('/');
    const issueUrl = repoParts.length === 2
      ? `https://gitcode.com/${repoParts[0]}/${repoParts[1]}/issues/${num}`
      : '';
    const issHtml = issueUrl
      ? `<a href="${issueUrl}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">Issue ${iss}</a>`
      : `Issue ${iss}`;
    const stages = displayPipe.stages || [];
    const statuses = displayPipe.statuses || [];
    const owner = displayPipe.current_owner || '';
    const execBadge = displayPipe.executing
      ? ' &nbsp;⚡<span class="ts t2" style="color:var(--g)">执行中</span>'
      : ' &nbsp;<span class="ts t2">空闲</span>';
    // 触发 SRE 部署按钮：stage 非 done 时显示
    const _issueNum = String(iss).replace(/^#/, '');
    const _repoFullName = (data.repo || '').replace(/^gitcode:/, '');
    const _stage = displayPipe.stage || '';
    const _sreBtn = '';  // 暂时隐藏触发 SRE 按钮
    flowHtml = `<div id="pipeFlow" style="flex-shrink:0;padding:12px 0 8px;border-bottom:1px solid var(--b)"><div class="ts" style="font-weight:600;margin-bottom:8px">Active: ${issHtml}${displayPipe.title ? ` &nbsp;·&nbsp; <span style="color:var(--t2);font-weight:500">${displayPipe.title}</span>` : ''}${owner ? ` &nbsp;\u00b7&nbsp; \ud83d\udc64 \u5f53\u524d\u8d23\u4efb\u4eba: <span style="color:var(--a)">${owner}</span>` : ''}${execBadge}${_sreBtn}</div>
    <div class="af" style="min-height:auto;padding:6px 0">`;
    var pipeNodes2 = _getPipeNodes(data, displayPipe);
    pipeNodes2.forEach(function(node, i) {
      var tag = node.tag || node.id;
      var sc = statuses[i] || 0;
      var ev = (stages[i] && stages[i].latest_event) || {};
      var evTitle = ev.title || '';
      var dotc = sc === 2 ? 'var(--g)' : sc === 1 ? 'var(--y)' : sc === 3 ? 'var(--r)' : 'var(--t3)';
      var color = _TAG_COLORS[tag] || agentColors[i % agentColors.length];
      var label = (node.id || tag).charAt(0).toUpperCase() + (node.id || tag).slice(1);
      var modeIcon = _MODE_ICONS[node.mode] || _MODE_ICONS.emit;
      var dimStyle = sc === 0
        ? 'background:#eef0f4;border-top-color:#d5d9e0;filter:grayscale(0.6)'
        : 'background:' + (sc === 1 ? '#fff8e8' : '#fff') + ';border-top-color:' + color;
      flowHtml += `<div class="nd${sc === 1 ? ' nd-radar' : ''}" style="padding:10px;width:150px;cursor:pointer;${dimStyle}${sc === 0 ? ';opacity:.55' : ''}" onclick="app.nav('${tag}')">
        <div class="nh"><span class="n" style="background:${sc === 0 ? '#c3c9d4' : color}"></span><h4 style="font-size:11px;${sc === 0 ? 'color:#9aa3b2' : ''}">${label} <span style="font-size:9px;color:var(--t3)">${modeIcon}</span></h4></div>
        <div class="nds" style="margin-bottom:1px;${sc === 0 ? 'color:#a8b0bd' : ''}">${ev ? evTitle || ev.event_type || '\u2014' : '\u2014'}</div>
        <div class="nds" style="margin-bottom:1px;font-size:9px;${sc === 0 ? 'color:#a8b0bd' : ''}">${ev && ev.actor ? '@' + ev.actor : ' '}</div>
        <div class="ns"><span class="d${sc === 1 ? ' d-pulse' : ''}" style="background:${dotc}"></span>${sc === 2 ? 'Done' : sc === 1 ? 'In Progress' : sc === 3 ? 'Failed' : 'Pending'}</div>
      </div>`;
    });
    flowHtml += `</div></div>`;
  }

  // 分页计算
  const totalPipes = allPipes.length;
  const totalPages = Math.ceil(totalPipes / _PIPE_PER_PAGE);
  if (_pipePage > totalPages) _pipePage = 1;
  if (_pipePage < 1) _pipePage = 1;
  const pipeStart = (_pipePage - 1) * _PIPE_PER_PAGE;
  const pagePipes = allPipes.slice(pipeStart, pipeStart + _PIPE_PER_PAGE);

  // 分页控件（流程图下方）
  let pagerHtml = '';
  if (totalPages > 1) {
    const pBtn = (label, page, disabled, active) =>
      `<button class="bt bt-o bt-s" style="padding:3px 10px;font-size:11px;${disabled ? 'opacity:.4;cursor:not-allowed' : ''}${active ? ';border-color:var(--a);color:var(--a);font-weight:600' : ''}" ${disabled ? 'disabled' : `onclick="_pipePage=${page};renderPipeline(_pipeData)"`}>${label}</button>`;
    let btns = '';
    let s = Math.max(1, _pipePage - 3);
    let e = Math.min(totalPages, s + 6);
    if (e - s < 6) s = Math.max(1, e - 6);
    if (s > 1) btns += pBtn('1', 1, false, _pipePage === 1) + (s > 2 ? '<span style="color:var(--t3);padding:0 2px">…</span>' : '');
    for (let i = s; i <= e; i++) btns += pBtn(String(i), i, false, _pipePage === i);
    if (e < totalPages) btns += (e < totalPages - 1 ? '<span style="color:var(--t3);padding:0 2px">…</span>' : '') + pBtn(String(totalPages), totalPages, false, _pipePage === totalPages);
    pagerHtml = `<div style="display:flex;align-items:center;gap:4px;padding:8px 0;flex-shrink:0">
      ${pBtn('‹', _pipePage - 1, _pipePage <= 1, false)}
      ${btns}
      ${pBtn('›', _pipePage + 1, _pipePage >= totalPages, false)}
      <span class="ts t3" style="margin-left:8px;font-size:10px">共 ${totalPipes} 条 · ${_pipePage}/${totalPages} 页</span>
    </div>`;
  }

  html += `${flowHtml}${pagerHtml}`;

  // 表格区域（独立滚动）
  html += `<div style="flex:1;overflow-y:auto;min-height:0"><div class="tw"><table><tr><th>Issue</th><th>标题</th><th>Duration</th><th>Agents</th><th>阶段</th><th>场景</th><th>责任人</th></tr>`;
  pagePipes.forEach((p, i) => {
    const realIdx = pipeStart + i;
    const s = realIdx === _pipeActiveIdx ? ' sel' : '';
    const agentTags = ['vod','issue','coding','qa','sre'].map((at, j) => {
      const hasAgent = p.statuses && p.statuses[j] > 0;
      const cls = ['tg-v','tg-i','tg-c','tg-q','tg-s'][j];
      return `<span class="tg ${cls}" style="${hasAgent ? '' : 'opacity:0.4'}">${agentLabels[j].substring(0,1)}</span>`;
    }).join('');
    const stageName = p.stage ? (stageLabels[p.stage] || p.stage) : '—';
    const stageClass = p.stage === 'done' ? 'tgx' : ['vod','issue','coding','qa','sre'].includes(p.stage) ? 'ty' : 't3';
    const _dur = p.duration;
    const dur = _dur ? (_dur >= 60 ? Math.floor(_dur/60) + 'm ' + Math.round(_dur%60) + 's' : _dur + 's') : '—';
    const owner = p.current_owner || '—';
    const _sceneId = p.scene_id || '';
    const _sceneLabels = {default:'通用开发','cloud-native':'云原生','iac':'IaC','agent-dev':'Agent开发','skill-dev':'Skill开发','api-skill-dev':'API Skill','blog-publish':'博客发布'};
    const sceneLabel = _sceneId ? (p.scene_name || _sceneLabels[_sceneId] || _sceneId) : '—';
    const sceneBadge = _sceneId ? `<span class="tg tg-i" style="font-size:10px">${sceneLabel}</span>` : '<span class="ts t3">—</span>';
    const _issueNum = String(p.issue_id || '').replace(/^#/, '');
    const _repoParts2 = (data.repo || '').replace(/^gitcode:/, '').split('/');
    const _issueUrl = (_issueNum && _repoParts2.length === 2) ? `https://gitcode.com/${_repoParts2[0]}/${_repoParts2[1]}/issues/${_issueNum}` : '';
    const _esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const execBadge = p.executing
      ? '<span class="tg tg-c" style="color:#fff;background:var(--g)">⚡ 执行中</span>'
      : '<span class="ts t2">空闲</span>';
    html += `<tr class="pipe-row${s}" data-idx="${realIdx}" onclick="selPipe(${realIdx})" ondblclick="pipeRowDblClick(${realIdx})">
      <td><span class="ts"><b>${p.issue_id || '?'}</b></span><br>${execBadge}</td>
      <td><a href="${_issueUrl || '#'}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline;font-size:11px">${_esc(p.title || '—')}</a></td>
      <td>${dur}</td>
      <td>${agentTags}</td>
      <td><span class="${stageClass}">${stageName}</span></td>
      <td>${sceneBadge}</td>
      <td>${owner}</td></tr>`;
  });
  html += `</table></div></div>`;
  html += `</div><div class="tx t2 mt8">点击历史表中的 Issue 会切换上方工作流状态展示</div>`;

  el.innerHTML = html;
}

function triggerSREDeploy(repoFullName, issueNum, stage) {
  if (!confirm(`确认触发 SRE Agent 处理 Issue #${issueNum}？\n\n仓库：${repoFullName}\n当前阶段：${stage}\n\nSRE Agent 将根据 Issue 内容自动判断部署或资源释放。`))
    return;
  const ctx = stage === 'sre'
    ? `手动触发 SRE 处理 Issue #${issueNum}（当前阶段 sre，可能需要部署或资源释放）`
    : `手动触发 SRE 部署 Issue #${issueNum}（从 ${stage} 阶段跳转）`;
  API.post('/a2a', {
    agent: 'sre',
    payload: {
      repository: repoFullName,
      account: repoFullName,
      issue_number: parseInt(issueNum) || issueNum,
      context: ctx
    }
  }).then(d => {
    if (d.skipped) {
      alert(`SRE 任务被跳过：${d.skipped}\n\n可能原因：SRE 已配置订阅且本次请求被判定为重复。请检查 SRE Agent 订阅配置。`);
    } else if (d.seq > 0) {
      alert(`✅ SRE Agent 已触发！\n\n事件编号：seq=${d.seq}\n可在 Events 页面查看执行进度。`);
      // 刷新 pipeline
      if (typeof loadPipeline === 'function') loadPipeline();
    } else {
      alert(`SRE 触发结果：${JSON.stringify(d)}`);
    }
  }).catch(e => {
    alert('触发 SRE 失败：' + (e.message || e));
  });
}

function pipeRowDblClick(idx) {
  if (!_pipeData) return;
  const active = _pipeData.active;
  const history = _pipeData.history || [];
  const allPipes = active ? [active, ...history] : history;
  const p = allPipes[idx];
  if (!p) return;
  // 找到正在执行的 agent（status=1 running），跳转到其 Event Detail
  const statuses = p.statuses || [];
  const stages = p.stages || [];
  for (let i = 0; i < statuses.length; i++) {
    if (statuses[i] === 1) {
      const ev = stages[i] && stages[i].latest_event;
      if (ev && ev.seq) {
        app.nav('event', ev.seq);
        return;
      }
    }
  }
  // 没有 running 的 → 跳到最新完成的 agent 事件（最后一个 status>0 的）
  for (let i = statuses.length - 1; i >= 0; i--) {
    if (statuses[i] > 0) {
      const ev = stages[i] && stages[i].latest_event;
      if (ev && ev.seq) {
        app.nav('event', ev.seq);
        return;
      }
    }
  }
  // 全都没有 → 跳到 issue 对应的最新事件
  const agentIds = ['vod','issue','coding','qa','sre'];
  const issNum = String(p.issue_id || '').replace(/^#/, '');
  const repo = (_pipeData.repo || '').replace(/^gitcode:/, '');
  if (issNum && repo) {
    API.get('/events/' + encodeURIComponent(repo) + '?page=1&page_size=50').then(function(d) {
      const ev = (d.events || []).find(function(e) {
        return String(e.issue_id || '').replace(/^#/,'') === issNum;
      });
      if (ev) app.nav('event', ev.seq);
    });
  }
}
