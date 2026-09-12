let _pipeData = null;
let _pipeActiveIdx = 0;
let _pipePage = 1;
let _pipeQuery = '';
const _PIPE_PER_PAGE = 10;
const agentColors = ['var(--pk)','var(--r)','var(--a)','var(--pu)','var(--or)'];
const agentIds = ['vod','issue','coding','qa','sre'];
// agentLabels / stageLabels 不在模块顶层缓存 I18N.t()（语言切换后会失效），
// 改为函数实时取值
function _agentLabel(idx) {
  var keys = ['pipeline.agent_vod','pipeline.agent_issue','pipeline.agent_coding','pipeline.agent_qa','pipeline.agent_sre'];
  return I18N.t(keys[idx]) || agentIds[idx];
}
function _stageLabel(stage) {
  var map = {vod:'pipeline.agent_vod',issue:'pipeline.agent_issue',coding:'pipeline.agent_coding',qa:'pipeline.agent_qa',sre:'pipeline.agent_sre',done:'pipeline.stage_done'};
  return map[stage] ? (I18N.t(map[stage]) || stage) : stage;
}
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

function _filteredPipes(allPipes) {
  if (!_pipeQuery) return allPipes;
  const q = _pipeQuery.toLowerCase().trim();
  if (!q) return allPipes;
  return allPipes.filter(function(p) {
    var haystack = [
      String(p.issue_id || ''),
      (p.title || ''),
      String(p.parent_issue_id || ''),
      (p.stage || ''),
      (p.scene_id || ''),
      (p.scene_name || ''),
      (p.current_owner || '')
    ].join(' ').toLowerCase();
    return haystack.includes(q);
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
  const issueUrl = (data.repo_key || '') ? _vcsUrl(data.repo_key, '/issues/' + num) : '';
  const issHtml = issueUrl ? `<a href="${issueUrl}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">${I18N.t('pipeline.issue_prefix')} ${iss}</a>` : `${I18N.t('pipeline.issue_prefix')} ${iss}`;
  const stages = displayPipe.stages || [];
  const statuses = displayPipe.statuses || [];
  const owner = displayPipe.current_owner || '';
  const execBadge = displayPipe.executing ? ' &nbsp;⚡<span class="ts t2" style="color:var(--g)">' + I18N.t('pipeline.executing') + '</span>' : ' &nbsp;<span class="ts t2">' + I18N.t('pipeline.idle') + '</span>';

  let flowHtml = `<div class="pl-flow-hdr"><div><div class="pl-flow-title" style="font-size:15px">${I18N.t('pipeline.page_title')} · ${data.repo || ''}</div><div class="pl-flow-sub">${issHtml}${displayPipe.title ? ' &middot; <span style="color:#6e6e73;font-weight:500">' + displayPipe.title + '</span>' : ''}${owner ? ' &middot; <span style="color:#86868b">' + I18N.t('pipeline.current_owner') + ': <b style="color:#0071e3">' + owner + '</b></span>' : ''}${execBadge}</div></div><div class="pl-stats"><div class="pl-stat"><div class="v" style="color:#34c759">${_pipeData.active_count || 0}</div><div class="l">${I18N.t('pipeline.stat_active')}</div></div><div class="pl-stat"><div class="v" style="color:#0071e3">${_pipeData.today_count || 0}</div><div class="l">${I18N.t('pipeline.stat_today')}</div></div><div class="pl-stat"><div class="v" style="color:#ff3b30">${_pipeData.failed_count || 0}</div><div class="l">${I18N.t('pipeline.stat_failed')}</div></div></div></div><div class="pl-flow-grid">`;
  var pipeNodes = _getPipeNodes(data, displayPipe);
  pipeNodes.forEach(function(node, i) {
    var tag = node.tag || node.id;
    var sc = statuses[i] || 0;
    var ev = (stages[i] && stages[i].latest_event) || {};
    var dotc = sc === 2 ? '#34c759' : sc === 1 ? '#ff9f0a' : sc === 3 ? '#ff3b30' : '#86868b';
    var color = (_TAG_COLORS[tag] || agentColors[i % agentColors.length]).replace('var(--pk)','#ff2d92').replace('var(--r)','#ff3b30').replace('var(--a)','#0071e3').replace('var(--pu)','#af52de').replace('var(--or)','#ff9f0a').replace('var(--sk)','#0c92d4');
    var label = (node.id || tag).charAt(0).toUpperCase() + (node.id || tag).slice(1);
    var modeIcon = _MODE_ICONS[node.mode] || _MODE_ICONS.emit;
    var dimCls = sc === 0 ? ' dim' : sc === 1 ? ' run' : '';
    flowHtml += '<div class="pl-nd' + dimCls + (sc === 1 ? ' nd-heartbeat' : '') + '" onclick="app.nav(\'' + tag + '\')">'
      + '<div class="pl-nd-bar" style="background:' + (sc === 0 ? '#d5d9e0' : color) + '"></div>'
      + '<div class="pl-nd-name"><span class="pl-nd-dot" style="background:' + (sc === 0 ? '#c3c9d4' : color) + '"></span>' + label + ' <span style="font-size:9px;color:#86868b">' + modeIcon + '</span></div>'
      + '<div class="pl-nd-ev">' + (ev ? ev.event_type || '—' : '—') + '</div>'
      + '<div class="pl-nd-actor">' + (ev && ev.actor ? '@' + ev.actor : ' ') + '</div>'
      + '<div class="pl-nd-status"><span class="d' + (sc === 1 ? ' d-heartbeat' : '') + '" style="background:' + dotc + '"></span>' + (sc === 2 ? I18N.t('pipeline.status_done') : sc === 1 ? I18N.t('pipeline.status_in_progress') : sc === 3 ? I18N.t('pipeline.status_failed') : I18N.t('pipeline.status_pending')) + '</div>'
      + '</div>';
  });
  flowHtml += '</div></div></div>';

  // 替换流程图区域（表格不动，保持滚动位置）
  const flowEl = document.getElementById('pipeFlow');
  if (flowEl) flowEl.innerHTML = flowHtml;

  // 更新选中行高亮（不重建表格）
  document.querySelectorAll('.pl-table tr.sel').forEach(tr => tr.classList.remove('sel'));
  const selRow = document.querySelector(`.pl-table tr[data-idx="${idx}"]`);
  if (selRow) selRow.classList.add('sel');
}

function renderPipeline(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  _pipeData = data;
  const active = _pipeData.active;
  const history = _pipeData.history || [];

  let html = `<style>
    .pl-wrap{display:flex;flex-direction:column;height:calc(100vh - 120px);overflow:hidden;gap:12px}
    .pl-flow-card{background:#fff;border:1px solid #d2d2d7;border-radius:18px;box-shadow:0 2px 12px rgba(0,0,0,.06);flex-shrink:0;overflow:hidden}
    .pl-flow-hdr{display:flex;align-items:center;justify-content:space-between;padding:14px 24px 6px}
    .pl-flow-title{font-size:15px;font-weight:600;color:#1d1d1f}
    .pl-flow-title a{color:#0071e3}
    .pl-flow-sub{font-size:11px;color:#86868b;margin-top:3px}
    .pl-stats{display:flex;gap:6px}
    .pl-stat{text-align:center;padding:4px 14px;background:#f5f5f7;border-radius:10px;min-width:56px}
    .pl-stat .v{font-size:16px;font-weight:700;line-height:1.1}
    .pl-stat .l{font-size:9px;color:#86868b;margin-top:1px}
    .pl-flow-grid{display:flex;gap:8px;padding:6px 24px 12px;overflow-x:auto}
    .pl-nd{flex:0 0 145px;min-height:100px;background:#f5f5f7;border:1px solid #e8e8ed;border-radius:14px;padding:14px;display:flex;flex-direction:column;transition:all .2s;cursor:pointer;position:relative;overflow:hidden}
    .pl-nd:hover{background:#fff;box-shadow:0 4px 20px rgba(0,0,0,.08);transform:translateY(-1px)}
    .pl-nd-bar{position:absolute;top:0;left:0;right:0;height:3px;border-radius:14px 14px 0 0}
    .pl-nd-name{font-size:13px;font-weight:600;color:#1d1d1f;margin-bottom:6px;display:flex;align-items:center;gap:6px}
    .pl-nd-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
    .pl-nd-ev{font-size:10px;color:#86868b;line-height:1.4;margin-bottom:4px}
    .pl-nd-actor{font-size:9px;color:#86868b;margin-bottom:6px}
    .pl-nd-status{display:flex;align-items:center;gap:4px;font-size:10px;font-weight:500;margin-top:auto}
    .pl-nd-status .d{width:5px;height:5px;border-radius:50%}
    .pl-nd.dim{opacity:.5;filter:grayscale(.6)}
    .pl-nd.run{background:#fff8e8}
    .pl-nd.run .pl-nd-dot{animation:plPulse 1.5s ease-in-out infinite}
    @keyframes plPulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(1.3)}}
    .pl-pager{display:flex;align-items:center;gap:3px;flex-shrink:0;justify-content:flex-end;padding:0 24px 4px}
    .pl-pbtn{padding:2px 7px;border-radius:6px;border:1px solid #d2d2d7;font-size:10px;background:#fff;color:#6e6e73;cursor:pointer;transition:all .12s;line-height:1.4}
    .pl-pbtn:hover{border-color:#0071e3;color:#0071e3}
    .pl-pbtn:disabled{opacity:.4;cursor:not-allowed}
    .pl-pbtn.act{border-color:#0071e3;color:#0071e3;font-weight:600}
    .pl-table-card{background:#fff;border:1px solid #d2d2d7;border-radius:18px;box-shadow:0 2px 12px rgba(0,0,0,.06);flex:1;overflow:hidden;min-height:0}
    .pl-table-wrap{flex:1;overflow-y:auto;min-height:0}
    .pl-table{width:100%;border-collapse:collapse;font-size:12px}
    .pl-table th{text-align:left;padding:10px 14px;font-weight:500;color:#86868b;border-bottom:1px solid #e8e8ed;font-size:10px;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:0;background:#fff;z-index:1}
    .pl-table td{padding:10px 14px;border-bottom:1px solid #f2f2f7}
    .pl-table tr{cursor:pointer;transition:background .12s}
    .pl-table tr:hover td{background:#f5f5f7}
    .pl-table tr.sel td{background:#eef4ff;font-weight:500}
    .pl-table tr:last-child td{border-bottom:none}
    .pl-search-bar{display:flex;align-items:center;flex-shrink:0;padding:6px 24px}
    .pl-search-bar .pl-search-wrap{position:relative;flex:0 1 300px}
    .pl-search-bar .pl-search-wrap .pl-search-ic{position:absolute;left:10px;top:50%;transform:translateY(-50%);font-size:12px;color:#86868b;pointer-events:none}
    .pl-search-bar input{width:100%;padding:5px 10px 5px 30px;border:1px solid #d2d2d7;border-radius:8px;font-size:12px;background:#fff;color:#1d1d1f;outline:none;transition:border-color .2s,box-shadow .2s}
    .pl-search-bar input:focus{border-color:#0071e3;box-shadow:0 0 0 3px rgba(0,113,227,.08)}
    .pl-search-bar input::placeholder{color:#86868b}
  </style>

  <div class="pl-wrap">`;

  const allPipes = active ? [active, ...history] : history;
  const filteredPipes = _filteredPipes(allPipes);
  // 当前选中的 Issue（默认 active，点击历史行后切换）
  const displayPipe = allPipes[_pipeActiveIdx] || active;

  // 流程图固定区
  let flowHtml = '';
  if (displayPipe) {
    const iss = displayPipe.issue_id || '#?';
    const num = String(iss).replace(/^#/, '');
    const issueUrl = (data.repo_key || '') ? _vcsUrl(data.repo_key, '/issues/' + num) : '';
    const issHtml = issueUrl
      ? `<a href="${issueUrl}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">${I18N.t('pipeline.issue_prefix')} ${iss}</a>`
      : `${I18N.t('pipeline.issue_prefix')} ${iss}`;
    const stages = displayPipe.stages || [];
    const statuses = displayPipe.statuses || [];
    const owner = displayPipe.current_owner || '';
    const execBadge = displayPipe.executing
      ? ' &nbsp;⚡<span class="ts t2" style="color:var(--g)">' + I18N.t('pipeline.executing') + '</span>'
      : ' &nbsp;<span class="ts t2">' + I18N.t('pipeline.idle') + '</span>';
    // 触发 SRE 部署按钮：stage 非 done 时显示
    const _issueNum = String(iss).replace(/^#/, '');
    const _repoFullName = (data.repo_key || '').replace(/^(gitcode|github):/, '');
    const _stage = displayPipe.stage || '';
    const _sreBtn = '';  // 暂时隐藏触发 SRE 按钮
    flowHtml = `<div class="pl-flow-card" id="pipeFlow"><div class="pl-flow-hdr"><div><div class="pl-flow-title" style="font-size:15px">${I18N.t('pipeline.page_title')} · ${data.repo || ''}</div><div class="pl-flow-sub">${issHtml}${displayPipe.title ? ' &middot; <span style="color:#6e6e73;font-weight:500">' + displayPipe.title + '</span>' : ''}${owner ? ' &middot; <span style="color:#86868b">' + I18N.t('pipeline.current_owner') + ': <b style="color:#0071e3">' + owner + '</b></span>' : ''}${execBadge}</div></div><div class="pl-stats"><div class="pl-stat"><div class="v" style="color:#34c759">${data.active_count || 0}</div><div class="l">${I18N.t('pipeline.stat_active')}</div></div><div class="pl-stat"><div class="v" style="color:#0071e3">${data.today_count || 0}</div><div class="l">${I18N.t('pipeline.stat_today')}</div></div><div class="pl-stat"><div class="v" style="color:#ff3b30">${data.failed_count || 0}</div><div class="l">${I18N.t('pipeline.stat_failed')}</div></div></div></div><div class="pl-flow-grid">`;
    var pipeNodes2 = _getPipeNodes(data, displayPipe);
    pipeNodes2.forEach(function(node, i) {
      var tag = node.tag || node.id;
      var sc = statuses[i] || 0;
      var ev = (stages[i] && stages[i].latest_event) || {};
      var dotc = sc === 2 ? '#34c759' : sc === 1 ? '#ff9f0a' : sc === 3 ? '#ff3b30' : '#86868b';
      var color = (_TAG_COLORS[tag] || agentColors[i % agentColors.length]).replace('var(--pk)','#ff2d92').replace('var(--r)','#ff3b30').replace('var(--a)','#0071e3').replace('var(--pu)','#af52de').replace('var(--or)','#ff9f0a').replace('var(--sk)','#0c92d4');
      var label = (node.id || tag).charAt(0).toUpperCase() + (node.id || tag).slice(1);
      var modeIcon = _MODE_ICONS[node.mode] || _MODE_ICONS.emit;
      var dimCls = sc === 0 ? ' dim' : sc === 1 ? ' run' : '';
      var statusText = sc === 2 ? I18N.t('pipeline.status_done') : sc === 1 ? I18N.t('pipeline.status_in_progress') : sc === 3 ? I18N.t('pipeline.status_failed') : I18N.t('pipeline.status_pending');
      flowHtml += '<div class="pl-nd' + dimCls + (sc === 1 ? ' nd-heartbeat' : '') + '" onclick="app.nav(\'' + tag + '\')">'
        + '<div class="pl-nd-bar" style="background:' + (sc === 0 ? '#d5d9e0' : color) + '"></div>'
        + '<div class="pl-nd-name"><span class="pl-nd-dot" style="background:' + (sc === 0 ? '#c3c9d4' : color) + '"></span>' + label + ' <span style="font-size:9px;color:#86868b">' + modeIcon + '</span></div>'
        + '<div class="pl-nd-ev">' + (ev ? ev.event_type || '—' : '—') + '</div>'
        + '<div class="pl-nd-actor">' + (ev && ev.actor ? '@' + ev.actor : ' ') + '</div>'
        + '<div class="pl-nd-status"><span class="d' + (sc === 1 ? ' d-heartbeat' : '') + '" style="background:' + dotc + '"></span>' + statusText + '</div>'
        + '</div>';
    });
    flowHtml += '</div></div>';
  }

  // 分页计算（基于过滤后的列表）
  const totalPipes = filteredPipes.length;
  const totalPages = Math.ceil(totalPipes / _PIPE_PER_PAGE);
  if (_pipePage > totalPages) _pipePage = 1;
  if (_pipePage < 1) _pipePage = 1;
  const pipeStart = (_pipePage - 1) * _PIPE_PER_PAGE;
  const pagePipes = filteredPipes.slice(pipeStart, pipeStart + _PIPE_PER_PAGE);

  // 分页控件（流程图下方）
  let pagerHtml = '';
  if (totalPages > 1) {
    const pBtn = (label, page, disabled, active) =>
      `<button class="pl-pbtn${active ? ' act' : ''}" ${disabled ? 'disabled' : `onclick="_pipePage=${page};renderPipeline(_pipeData)"`}>${label}</button>`;
    let btns = '';
    let s = Math.max(1, _pipePage - 3);
    let e = Math.min(totalPages, s + 6);
    if (e - s < 6) s = Math.max(1, e - 6);
    if (s > 1) btns += pBtn('1', 1, false, _pipePage === 1) + (s > 2 ? '<span style="color:var(--t3);padding:0 2px">…</span>' : '');
    for (let i = s; i <= e; i++) btns += pBtn(String(i), i, false, _pipePage === i);
    if (e < totalPages) btns += (e < totalPages - 1 ? '<span style="color:var(--t3);padding:0 2px">…</span>' : '') + pBtn(String(totalPages), totalPages, false, _pipePage === totalPages);
    pagerHtml = `<div class="pl-pager">
      ${pBtn('‹', _pipePage - 1, _pipePage <= 1, false)}
      ${btns}
      ${pBtn('›', _pipePage + 1, _pipePage >= totalPages, false)}
      <span class="ts t3" style="margin-left:8px;font-size:10px">${I18N.t('pipeline.pagination_total', {total: totalPipes, page: _pipePage, pages: totalPages})}</span>
    </div>`;
  }

  // 拼接：流程卡片 + 分页（右对齐） + 搜索框 + 表格卡片
  html += `${flowHtml}`;
  if (pagerHtml) html += pagerHtml;

  // Issue #89: 部分任务尚未生成流水线事件提示
  if (data.has_tasks_without_events) {
    const _twe = data.tasks_without_events || [];
    const _hintItems = _twe.slice(0, 5).map(function(t) {
      return `<span class="tg tg-i" style="font-size:10px;margin:2px">${t.issue_id} ${t.title ? '· ' + t.title.substring(0, 30) : ''}</span>`;
    }).join('');
    const _more = _twe.length > 5 ? `<span class="ts t3" style="font-size:10px;margin-left:4px">+${_twe.length - 5}</span>` : '';
    html += `<div style="background:#fff8e8;border:1px solid #ff9f0a;border-radius:12px;padding:10px 16px;margin:0 0 8px;display:flex;align-items:center;gap:8px;flex-shrink:0"><span style="font-size:14px">⚠️</span><span style="font-size:12px;color:#86868b">${I18N.t('pipeline.tasks_without_events_hint')}</span><div style="margin-left:auto;display:flex;align-items:center;gap:2px;flex-wrap:wrap">${_hintItems}${_more}</div></div>`;
  }

  html += `<div class="pl-search-bar"><div class="pl-search-wrap"><span class="pl-search-ic">\ud83d\udd0d</span><input id="pipeSearchInput" placeholder="${I18N.t('pipeline.search_placeholder')}" value="${_pipeQuery}" onkeyup="if(event.key==='Enter'){_pipeQuery=this.value;_pipePage=1;renderPipeline(_pipeData)}" onchange="_pipeQuery=this.value;_pipePage=1;renderPipeline(_pipeData)"></div></div>`;
  html += `<div class="pl-table-card"><div class="pl-table-wrap"><table class="pl-table"><tr><th>${I18N.t('pipeline.th_issue')}</th><th>${I18N.t('pipeline.th_title')}</th><th>${I18N.t('pipeline.th_parent_issue')}</th><th>${I18N.t('pipeline.th_duration')}</th><th>${I18N.t('pipeline.th_agents')}</th><th>${I18N.t('pipeline.th_stage')}</th><th>${I18N.t('pipeline.th_scene')}</th><th>${I18N.t('pipeline.th_owner')}</th></tr>`;
  pagePipes.forEach((p, i) => {
    const realIdx = allPipes.indexOf(p);
    const s = realIdx === _pipeActiveIdx ? ' sel' : '';
    const agentTags = ['vod','issue','coding','qa','sre'].map((at, j) => {
      const hasAgent = p.statuses && p.statuses[j] > 0;
      const cls = ['tg-v','tg-i','tg-c','tg-q','tg-s'][j];
      const evSeq = (p.stages && p.stages[j] && p.stages[j].latest_event && p.stages[j].latest_event.seq) || '';
      if (hasAgent && evSeq) {
        return `<span class="tg ${cls}" style="cursor:pointer;transition:transform .15s" onclick="event.stopPropagation();app.nav('event','${evSeq}')" onmouseover="this.style.transform='scale(1.25)'" onmouseout="this.style.transform='scale(1)'">${_agentLabel(j).substring(0,1)}</span>`;
      }
      return `<span class="tg ${cls}" style="opacity:0.3;cursor:default">${_agentLabel(j).substring(0,1)}</span>`;
    }).join('');
    const stageName = p.stage ? (_stageLabel(p.stage) || p.stage) : '—';
    const stageClass = p.stage === 'done' ? 'tgx' : ['vod','issue','coding','qa','sre'].includes(p.stage) ? 'ty' : 't3';
    const _dur = p.duration;
    const dur = _dur ? (_dur >= 60 ? Math.floor(_dur/60) + 'm ' + Math.round(_dur%60) + 's' : _dur + 's') : '—';
    const owner = p.current_owner || '—';
    const _sceneId = p.scene_id || '';
    const _sceneLabels = {default:I18N.t('pipeline.scene_default'),'cloud-native':I18N.t('pipeline.scene_cloud_native'),'iac':I18N.t('pipeline.scene_iac'),'agent-dev':I18N.t('pipeline.scene_agent_dev'),'skill-dev':I18N.t('pipeline.scene_skill_dev'),'api-skill-dev':I18N.t('pipeline.scene_api_skill_dev'),'blog-publish':I18N.t('pipeline.scene_blog_publish')};
    const sceneLabel = _sceneId ? (_sceneLabels[_sceneId] || p.scene_name || _sceneId) : '—';
    const sceneBadge = _sceneId ? `<span class="tg tg-i" style="font-size:10px">${sceneLabel}</span>` : '<span class="ts t3">—</span>';
    const _issueNum = String(p.issue_id || '').replace(/^#/, '');
    const _issueUrl = _issueNum ? _vcsUrl(data.repo_key || '', '/issues/' + _issueNum) : '';
    const _parentIssueId = String(p.parent_issue_id || '').replace(/^#/, '');
    const _parentIssueUrl = _parentIssueId ? _vcsUrl(data.repo_key || '', '/issues/' + _parentIssueId) : '';
    const _parentIssueHtml = _parentIssueId ? `<a href="${_parentIssueUrl}" target="_blank" rel="noopener" style="color:var(--pu);text-decoration:underline;font-size:11px">#${_parentIssueId}</a>` : '<span class="ts t3">—</span>';
    const _esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const execBadge = p.executing
      ? '<span class="tg tg-c" style="color:#fff;background:var(--g)">⚡ ' + I18N.t('pipeline.executing') + '</span>'
      : '<span class="ts t2">' + I18N.t('pipeline.idle') + '</span>';
    html += `<tr class="${s ? 'sel' : ''}" data-idx="${realIdx}" onclick="selPipe(${realIdx})" ondblclick="pipeRowDblClick(${realIdx})">
      <td><span class="ts"><b>${p.issue_id || '?'}</b></span><br>${execBadge}</td>
      <td><a href="${_issueUrl || '#'}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline;font-size:11px">${_esc(p.title || '—')}</a></td>
      <td>${_parentIssueHtml}</td>
      <td>${dur}</td>
      <td>${agentTags}</td>
      <td><span class="${stageClass}">${stageName}</span></td>
      <td>${sceneBadge}</td>
      <td>${owner}</td></tr>`;
  });
  html += `</table></div></div>`;
  html += `</div>`;

  el.innerHTML = html;
}

function triggerSREDeploy(repoFullName, issueNum, stage) {
  if (!confirm(I18N.t('pipeline.sre_confirm_title', {issueNum: issueNum}) + I18N.t('pipeline.sre_confirm_body', {repoFullName: repoFullName, stage: stage})))
    return;
  const ctx = stage === 'sre'
    ? I18N.t('pipeline.sre_context_sre', {issueNum: issueNum})
    : I18N.t('pipeline.sre_context_jump', {issueNum: issueNum, stage: stage});
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
      alert(I18N.t('pipeline.sre_skipped', {skipped: d.skipped}));
    } else if (d.seq > 0) {
      alert(I18N.t('pipeline.sre_triggered', {seq: d.seq}));
      // 刷新 pipeline
      if (typeof loadPipeline === 'function') loadPipeline();
    } else {
      alert(I18N.t('pipeline.sre_result', {result: JSON.stringify(d)}));
    }
  }).catch(e => {
    alert(I18N.t('pipeline.sre_trigger_failed', {msg: (e.message || e)}));
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
