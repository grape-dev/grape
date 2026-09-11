// Event Detail：结构化产出 + Event Info + 与事件所属 agent 的对话（event-conversation-design.md）
let _convSeq = null;
let _convAgent = '';

function navEvent(seq) {
  if (!seq || seq < 1) return;
  // 局部刷新：直接拿数据调 renderEventDetail，不走路由避免整体闪抖
  API.get('/events/detail/' + seq).then(function(data) {
    // 仅更新标题栏 seq，不重置 pageContent
    var titleEl = document.getElementById('pageTitle');
    if (titleEl) titleEl.textContent = I18N.t('events.page_title_prefix', {seq: seq});
    renderEventDetail(data);
  }).catch(function(e) {
    // 事件不存在 → 后端返回 max_seq，跳转到最后一个事件
    var detail = (e && e.detail) || {};
    var maxSeq = detail.max_seq;
    if (maxSeq && maxSeq > 0 && maxSeq !== seq) {
      navEvent(maxSeq);
    }
  });
}

function renderEventDetail(data) {
  // 切换事件时清理接收上下文全屏状态
  if (_promptFs) {
    const _fs = document.getElementById('promptFullscreen');
    if (_fs) _fs.remove();
    _promptFs = false;
    _promptFsPlaceholder = null;
  }
  const el = document.getElementById('pageContent');
  if (!el) return;
  const agent = data.agent_type || 'vod';
  const agentName = (_AGENT_TYPES[agent] && _AGENT_TYPES[agent].display_name) || agent;
  const result = data.result || data.summary || '';
  _convSeq = data.seq;
  _convAgent = agent;

  // Issue 编号 → VCS 超链接
  const _issNum = String(data.issue_id || '').replace(/^#/, '');
  const _issueLink = (_issNum && data.repo_key)
    ? `<a href="${_vcsUrl(data.repo_key, '/issues/' + _issNum)}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">Issue ${data.issue_id}</a>`
    : `Issue ${data.issue_id || '—'}`;

  // 重新派发按钮显示条件（event-redispatch-design.md §3.1）：
  // failed / running>10min / 关联 task 卡在 dispatched
  const _ts = data.started_at ? new Date(data.started_at).getTime() : 0;
  const _redispatchVisible =
    data.status === 'failed' ||
    (data.status === 'running' && _ts && (Date.now() - _ts) > 10 * 60 * 1000) ||
    (data.task && data.task.task_status === 'dispatched');

  // M4：结构化产出（event.json archive 对应段，见 §11.4）
  // "Agent 处理记录" 作为方框外部左上角标题，与下方"Agent 答复结论"上下对齐
  let structuredHtml = '';
  const arch = data.archive || null;
  if (arch && arch.output) {
    const o = arch.output;
    const actions = Array.isArray(o.actions) ? o.actions.map(a => `<div>• ${a}</div>`).join('') : '';
    const arts = Array.isArray(o.artifacts) ? o.artifacts.map(a => `<span class="tgx" style="margin-right:6px">${a}</span>`).join('') : '';
    const _lblConclusion = I18N.t('events.label_conclusion');
    const _lblActions = I18N.t('events.label_actions');
    const _lblArtifacts = I18N.t('events.label_artifacts');
    const _lblNextStep = I18N.t('events.label_next_step');
    const _lblIssueSummary = I18N.t('events.label_issue_summary');
    structuredHtml = `${o.conclusion ? `<div style="font-size:12px;margin-bottom:3px"><b>${_lblConclusion}</b> ${o.conclusion}</div>` : ''}
      ${actions ? `<div style="font-size:12px;margin-bottom:3px"><b>${_lblActions}</b> ${actions}</div>` : ''}
      ${arts ? `<div style="font-size:12px;margin-bottom:3px"><b>${_lblArtifacts}</b> ${arts}</div>` : ''}
      ${o.next_step ? `<div style="font-size:12px;margin-bottom:3px"><b>${_lblNextStep}</b> ${o.next_step}</div>` : ''}
      ${o.issue_summary ? `<div style="font-size:12px;color:var(--a)"><b>${_lblIssueSummary}</b> ${o.issue_summary}</div>` : ''}`;
  }

  // 上下文折叠区块摘要（折叠时显示）
  const _ctxSummary = `${data.event_type || '—'} · ${data.actor || '—'}`;
  const _infoSummary = `${data.status === 'completed' ? '✅' : data.status === 'failed' ? '❌' : '⏳'} ${data.duration ? data.duration + 's' : '—'}`;

  // 返回按钮：用 history.back() 返回上一个页面
  const html = `<button class="bt bt-o bt-s" onclick="window.history.back()" style="margin-bottom:12px">← ${I18N.t('common.back')||'返回'}</button>
  <div id="evDetailGrid" style="display:grid;grid-template-columns:1fr;gap:14px;height:calc(100vh - 160px);overflow:hidden">
    <!-- 左栏大框：导航 + 可折叠区块 + 处理记录+答复结论 -->
    <div class="cd" style="padding:16px 20px;display:flex;flex-direction:column;overflow:hidden">
      <!-- 顶部导航栏 -->
      <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--b)">
        <button class="bt bt-o bt-s" onclick="navEvent(${data.prev_seq || 0})" title="${I18N.t('events.prev_event_title', {name: agentName})}" ${!data.prev_seq ? 'disabled style="opacity:.4;cursor:not-allowed"' : ''}>‹</button>
        <span class="ts t3" style="font-size:12px;font-weight:600">Event #${data.seq || '—'}</span>
        <button class="bt bt-o bt-s" onclick="navEvent(${data.next_seq || 0})" title="${I18N.t('events.next_event_title', {name: agentName})}" ${!data.next_seq ? 'disabled style="opacity:.4;cursor:not-allowed"' : ''}>›</button>
      </div>
      <!-- 区块1：Event Context & Info（可折叠，默认展开） -->
      <div class="ev-section" style="flex-shrink:0;margin-bottom:8px">
        <div class="ev-section-header" onclick="_toggleEvSection('ctxInfo', this)" style="display:flex;align-items:center;gap:6px;cursor:pointer;padding:6px 0;user-select:none">
          <span class="ev-arrow" style="font-size:10px;transition:transform .2s;display:inline-block">▼</span>
          <span style="font-size:12px;font-weight:600">${I18N.t('events.section_context_info')}</span>
          <span class="ts t3" style="font-size:11px;margin-left:auto">${_ctxSummary} · ${_infoSummary}</span>
        </div>
        <div id="ctxInfo" class="ev-section-body" style="display:grid;grid-template-columns:1fr 1fr;gap:14px;padding-bottom:10px;border-bottom:1px solid var(--b)">
          <div>
            <h3 style="font-size:12px;font-weight:600;margin-bottom:6px">${I18N.t('events.section_context')}</h3>
            <div class="tx t2" style="line-height:1.6">
              <div>Trigger: ${data.event_type || '—'}</div>
              <div>Repository: ${data.repo_key || ''}</div>
              <div>Issue: ${_issueLink}</div>
              <div>Actor: ${data.actor || '—'}</div>
              <div>Time: ${fmtLocalTime(data.created_at) || ''}</div>
            </div>
          </div>
          <div>
            <h3 style="font-size:12px;font-weight:600;margin-bottom:6px">${I18N.t('events.section_info')}</h3>
            <div style="font-size:11px;color:var(--t2);line-height:1.8">
              <div><span style="color:var(--t3)">Seq:</span> <b>#${data.seq || '—'}</b></div>
              <div><span style="color:var(--t3)">Engine:</span> <b>${data.runtime || data.backend || 'OpenCode'}</b>${data.engine_device ? `<span class="ts t3"> · ${data.engine_device}</span>` : ''}${data.engine_ip ? `<span class="ts t3"> · ${data.engine_ip}</span>` : ''}</div>
              <div><span style="color:var(--t3)">Skill:</span> ${data.skill || agent}</div>
              <div><span style="color:var(--t3)">Duration:</span> ${data.duration ? data.duration + 's' : '—'}</div>
              <div><span style="color:var(--t3)">Status:</span> <span class="${data.status === 'completed' ? 'tgx' : data.status === 'failed' ? 'tr' : 'ty'}">${data.status === 'completed' ? I18N.t('events.status_success') : data.status === 'failed' ? I18N.t('events.status_failed') : I18N.t('events.status_running_prefix') + data.status}</span>${_redispatchVisible ? ` <button class="bt bt-o bt-s" style="margin-left:8px" onclick="redispatchEvent(${data.seq})">${I18N.t('events.btn_redispatch')}</button>` : ''}${data.status === 'running' ? ` <button class="bt bt-o bt-s" style="margin-left:8px;color:var(--tr, #e74c3c);border-color:var(--tr, #e74c3c)" onclick="terminateEventTask(${data.seq}, '${(data.task && data.task.task_id) || ''}')" title="${I18N.t('events.terminate_title')}">${I18N.t('events.btn_terminate')}</button>` : ''}</div>
              ${data.task && data.task.task_id
                ? `<div style="margin-top:4px"><a href="javascript:void(0)" onclick="openTaskLog('${data.task.task_id}','seq#${data.seq}')" style="color:var(--a);text-decoration:underline">${I18N.t('events.view_realtime_log')}</a></div>`
                : `<div style="margin-top:4px"><a href="javascript:void(0)" onclick="openInlineLog(${data.seq}, 'seq#${data.seq}')" style="color:var(--a);text-decoration:underline">${I18N.t('events.view_log')}</a></div>`}
              <div id="convGate" style="display:none"></div>
            </div>
          </div>
        </div>
      </div>
      <!-- 区块2：接收上下文 prompt（可折叠，默认折叠） -->
      <div class="ev-section" style="flex-shrink:0;margin-bottom:8px">
        <div class="ev-section-header" onclick="_toggleEvSection('promptBody', this);_loadEventPrompt(${data.task && data.task.task_id ? "'" + data.task.task_id + "'" : 'null'}, ${data.seq})" style="display:flex;align-items:center;gap:6px;cursor:pointer;padding:6px 0;user-select:none">
          <span class="ev-arrow" style="font-size:10px;transition:transform .2s;display:inline-block">▶</span>
          <span style="font-size:12px;font-weight:600">${I18N.t('events.section_prompt')}</span>
          <span id="promptMeta" class="ts t3" style="font-size:11px;margin-left:auto"></span>
          <button id="promptFsBtn" onclick="event.stopPropagation();_togglePromptFullscreen()" title="${I18N.t('events.fullscreen_title')}" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;display:none">${I18N.t('events.fullscreen_btn')}</button>
        </div>
        <div id="promptBody" class="ev-section-body" style="display:none;max-height:400px;overflow-y:auto;background:#000;color:#fff;border-radius:8px;padding:14px 18px 28px;font-size:14px;font-family:JetBrains Mono,Fira Code,Cascadia Code,Consolas,Menlo,monospace;line-height:1.75;letter-spacing:0.2px;white-space:pre-wrap;word-break:break-all">
          <div id="promptContent"><span style="color:#a0aac0">${I18N.t('events.prompt_expand_hint')}</span></div>
        </div>
      </div>
      <!-- 区块3：Agent 处理记录与答复结论（统一标题，内置框+结论，整体滑动） -->
      <h3 style="font-size:13px;font-weight:600;margin-bottom:6px;flex-shrink:0">${I18N.t('events.section_processing')}</h3>
      <div style="flex:1;overflow-y:auto;background:var(--bg);color:var(--t);border-radius:8px;padding:14px;font-size:13px;line-height:1.6">
        ${structuredHtml ? `<div style="margin-bottom:12px;padding:10px 12px;background:#f0f1f4;border-radius:6px;border-left:3px solid var(--a)">${structuredHtml}</div>` : ''}
        ${result ? renderMarkdown(result) : '<div style="color:var(--t3)">' + I18N.t('events.no_conclusion') + '</div>'}
      </div>
      <div id="convOutcome" class="cd mt12" style="padding:10px;font-size:11px;flex-shrink:0;display:none"></div>
    </div>
    <!-- 右栏：对话面板（有对话/门禁开放才显示，见 loadConversation 条件渲染 Issue #35） -->
    <div id="convPanel" class="cd" style="padding:14px;display:none;flex-direction:column;gap:8px;height:100%;overflow:hidden">
      <h3 style="font-size:12px;font-weight:600;flex-shrink:0;display:flex;justify-content:space-between;align-items:center">${I18N.t('events.section_conversation', {name: agentName})} <button onclick="toggleConvFullscreen()" title="${I18N.t('events.fullscreen_title')}" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer">${I18N.t('events.fullscreen_btn')}</button></h3>
      <div id="convMessages" style="flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:8px;font-size:12px"></div>
      <div id="convInputRow" style="display:none;gap:6px;flex-shrink:0">
        <input id="convInput" placeholder="${I18N.t('events.conv_input_placeholder')}" style="flex:1;padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)" onkeydown="if(event.key==='Enter'){event.preventDefault();sendConversation();}">
        <button class="bt bt-p bt-s" onclick="sendConversation()">${I18N.t('events.btn_send')}</button>
      </div>
    </div>
  </div>`;

  el.innerHTML = html;
  loadConversation();
}

// ===== 可折叠区块交互 =====
function _toggleEvSection(bodyId, headerEl) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  const arrow = headerEl.querySelector('.ev-arrow');
  const isHidden = body.style.display === 'none';
  if (isHidden) {
    // 恢复缓存的原始 display 值（grid/block 等），无缓存则清空回退到默认
    body.style.display = body.dataset.origDisplay || '';
    if (arrow) { arrow.textContent = '▼'; arrow.style.transform = 'rotate(0deg)'; }
  } else {
    // 折叠前缓存当前 display 值（如 grid），防止展开时丢失
    if (body.style.display && body.style.display !== 'none') {
      body.dataset.origDisplay = body.style.display;
    }
    body.style.display = 'none';
    if (arrow) { arrow.textContent = '▶'; arrow.style.transform = 'rotate(0deg)'; }
  }
  // 接收上下文展开/折叠时同步显隐全屏按钮
  if (bodyId === 'promptBody') {
    const fsBtn = document.getElementById('promptFsBtn');
    if (fsBtn) {
      if (isHidden) {
        fsBtn.style.transition = 'none';
        fsBtn.style.display = 'inline-flex';
        void fsBtn.offsetHeight;  // 强制 reflush，让 transition:none 生效
        fsBtn.style.transition = '';
      } else {
        fsBtn.style.display = 'none';
      }
    }
  }
}

// ===== 异步加载 event 的接收上下文（prompt） =====
let _promptLoadedSeq = 0;
function _loadEventPrompt(taskId, seq) {
  const meta = document.getElementById('promptMeta');
  function _pc(html) {
    const c = document.getElementById('promptContent');
    if (c) c.innerHTML = html;
  }
  function _pctext(text) {
    const c = document.getElementById('promptContent');
    if (c) c.textContent = text;
  }
  if (!taskId) {
    _pc('<span style="color:#a0aac0">' + I18N.t('events.no_task') + '</span>');
    if (meta) meta.textContent = '';
    return;
  }
  // 同一 event 只加载一次
  if (_promptLoadedSeq === seq) return;
  _promptLoadedSeq = seq;
  _pc('<span style="color:#a0aac0">' + I18N.t('events.loading_prompt') + '</span>');
  const token = localStorage.getItem('token') || '';
  fetch('/api/task-logs/' + encodeURIComponent(taskId) + '/info', {headers: {'Authorization': 'Bearer ' + token}})
    .then(r => r.ok ? r.json() : null)
    .then(info => {
      if (!info) { _pc('<span style="color:#ff5b5b">' + I18N.t('events.load_failed') + '</span>'); return; }
      const prompt = info.prompt || '';
      const fullLen = info.prompt_full_length || 0;
      if (meta) meta.textContent = fullLen > 0 ? I18N.t('events.char_count', {len: fullLen}) : '';
      if (!prompt) {
        _pc('<span style="color:#a0aac0">' + I18N.t('events.prompt_empty') + '</span>');
      } else {
        const truncated = fullLen > prompt.length ? '\n\n' + I18N.t('events.prompt_truncated', {len: fullLen}) : '';
        _pctext(prompt + truncated);
      }
    })
    .catch(() => { _pc('<span style="color:#ff5b5b">' + I18N.t('events.load_failed') + '</span>'); });
}

function _convBubble(role, content, time, by) {
  if (role === 'system') {
    return `<div style="display:flex;justify-content:center"><div style="max-width:92%;padding:5px 10px;border-radius:8px;font-size:11px;color:var(--t2);background:#f1f5f9;text-align:center;line-height:1.4">${content || ''}</div></div>`;
  }
  const mine = role === 'user';
  // markdown 渲染（含 HTML 转义防 XSS，见 api.js renderMarkdown），与 chat 对话框对齐
  const rendered = renderMarkdown(content || '');
  return `<div style="display:flex;${mine ? 'justify-content:flex-end' : 'justify-content:flex-start'}">
    <div style="max-width:85%;padding:8px 10px;border-radius:8px;font-size:12px;line-height:1.5;${mine
      ? 'background:var(--a);color:#fff'
      : 'background:var(--bg);color:var(--t);border:1px solid var(--b)'}">
      ${rendered}
      <div style="font-size:10px;opacity:.7;margin-top:3px">${mine ? (by || I18N.t('events.bubble_me')) : (by || I18N.t('events.bubble_agent'))}${time ? ' · ' + fmtLocalTime(time).substring(11,16) : ''}</div>
    </div>
  </div>`;
}

function loadConversation() {
  if (!_convSeq) return;
  API.get('/events/' + _convSeq + '/thread').then(d => {
    const gate = d.gate || {};
    const hasMsgs = Array.isArray(d.messages) && d.messages.length > 0;
    const panelEl = document.getElementById('convPanel');
    const gridEl = document.getElementById('evDetailGrid');
    // Issue #35 条件渲染：有对话记录 或 门禁开放（对话可发起）才显示右栏；
    // 纯流水线事件（99%）无对话 → 隐藏右栏，左栏 1fr 满宽查阅 Agent 处理记录。
    if (gridEl) {
      gridEl.style.gridTemplateColumns = (hasMsgs || gate.open) ? '1fr 300px' : '1fr';
    }
    if (panelEl) {
      panelEl.style.display = (hasMsgs || gate.open) ? 'flex' : 'none';
    }
    const gateEl = document.getElementById('convGate');
    if (gateEl) gateEl.innerHTML = gate.open
      ? `<span class="tgx">${I18N.t('events.gate_open')}</span>`
      : `<span class="tr">${I18N.t('events.gate_closed')}</span>`;
    const messagesEl = document.getElementById('convMessages');
    if (messagesEl) {
      const msgs = (d.messages || []).map(m => _convBubble(m.role, m.content, m.created_at, m.created_by)).join('');
      messagesEl.innerHTML = msgs || '<div style="color:var(--t3);font-size:11px">' + I18N.t('events.conv_start_hint', {name: _convAgent}) + '</div>';
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    const inputRow = document.getElementById('convInputRow');
    if (inputRow) inputRow.style.display = (gate.open && panelEl) ? 'flex' : 'none';
    // 左栏：对话产出（知识沉淀 + 流程操作 + 结论）
    renderOutcome(d.knowledge || {}, d.messages || []);
  }).catch(e => {
    const messagesEl = document.getElementById('convMessages');
    if (messagesEl) messagesEl.innerHTML = '<div class="tr" style="font-size:11px">' + I18N.t('events.conv_load_failed', {msg: e.message}) + '</div>';
  });
}

function renderOutcome(knowledge, messages) {
  const el = document.getElementById('convOutcome');
  if (!el) return;
  const kRows = Object.keys(knowledge || {}).map(scope =>
    `<div style="padding:3px 0;border-bottom:1px dashed var(--b)"><b>${scope === 'project' ? I18N.t('events.scope_project') : scope === 'issue' ? I18N.t('events.scope_issue') : I18N.t('events.scope_agent')}</b> · ${knowledge[scope]}</div>`
  ).join('');
  const flowRows = (messages || []).filter(m => m.role === 'system' && (m.content || '').indexOf('🔄') === 0)
    .map(m => `<div style="padding:3px 0;border-bottom:1px dashed var(--b)">${m.content}</div>`).join('');
  if (!kRows && !flowRows) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML = `<h3 style="font-size:11px;font-weight:600;margin-bottom:4px;color:var(--a)">${I18N.t('events.outcome_title')}</h3>`
    + (kRows ? `<div style="margin-top:2px"><b>${I18N.t('events.outcome_knowledge')}</b>${kRows}</div>` : '')
    + (flowRows ? `<div style="margin-top:4px"><b>${I18N.t('events.outcome_flow')}</b>${flowRows}</div>` : '');
}

// ===== 事件对话持久化查询（对齐 Chat Agent v31）=====
let _convStreamAbort = null;
let _convTasks = {};

function _setConvTask(seq, task) {
  _convTasks[seq] = task;
  try { sessionStorage.setItem('conv_task_' + seq, JSON.stringify(task)); } catch(e) {}
}
function _getConvTask(seq) {
  if (_convTasks[seq]) return _convTasks[seq];
  try { const s = sessionStorage.getItem('conv_task_' + seq); return s ? JSON.parse(s) : null; } catch(e) { return null; }
}
function _clearConvTask(seq) {
  delete _convTasks[seq];
  try { sessionStorage.removeItem('conv_task_' + seq); } catch(e) {}
}

function sendConversation() {
  const input = document.getElementById('convInput');
  if (!input || !_convSeq) return;
  const msg = (input.value || '').trim();
  if (!msg) return;
  input.value = '';
  const messagesEl = document.getElementById('convMessages');
  if (messagesEl) messagesEl.insertAdjacentHTML('beforeend', _convBubble('user', msg));
  const replyId = 'convReply' + Date.now();
  if (messagesEl) {
    messagesEl.insertAdjacentHTML('beforeend',
      '<div style="display:flex;justify-content:flex-start"><div id="' + replyId +
      '" style="max-width:85%;padding:8px 10px;border-radius:8px;font-size:12px;line-height:1.5;white-space:pre-wrap;background:var(--bg);color:var(--t);border:1px solid var(--b)">' +
      '<span class="chat-dots"><span></span><span></span><span></span></span></div></div>');
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  // 1. submit → 立即获取 task_id
  API.post('/events/' + _convSeq + '/chat/submit', {message: msg}).then(r => {
    if (r.status === 'error' || !r.task_id) {
      const el = document.getElementById(replyId);
      if (el) el.textContent = (r.error || I18N.t('events.send_failed_paren'));
      return;
    }
    _setConvTask(_convSeq, {task_id: r.task_id, el_id: replyId, status: 'running'});
    streamConvTask(r.task_id, replyId, _convSeq);
  }).catch(e => {
    const el = document.getElementById(replyId);
    if (el) el.textContent = I18N.t('events.send_failed_paren');
  });
}

async function streamConvTask(taskId, elId, seq) {
  if (_convStreamAbort) { _convStreamAbort.abort(); _convStreamAbort = null; }
  const ac = new AbortController();
  _convStreamAbort = ac;
  const el = document.getElementById(elId);
  const messagesEl = document.getElementById('convMessages');
  let finalText = '';
  try {
    const resp = await fetch('/api/events/chat/task/' + taskId + '/stream', {
      headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
      signal: ac.signal
    });
    if (!resp.ok || !resp.body) {
      if (el) el.textContent = I18N.t('events.send_failed_paren');
      _clearConvTask(seq);
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\n');
      buf = lines.pop() || '';
      lines.forEach(line => {
        if (line.startsWith('data: ')) {
          try {
            const d = JSON.parse(line.substring(6));
            if (d.type === 'done') {
              finalText = d.text || finalText;
              if (el) el.innerHTML = renderMarkdown(finalText) || I18N.t('events.no_reply');
              _clearConvTask(seq);
              loadConversation();
            } else if (d.type === 'error') {
              if (el) el.innerHTML = renderMarkdown(d.text || '') || I18N.t('events.error_reply');
              _clearConvTask(seq);
            } else if (d.type === 'text' && d.text) {
              finalText += d.text;
              if (el) el.innerHTML = renderMarkdown(finalText);
            } else if (d.type === 'thinking') {
              if (el) el.innerHTML = '<span class="chat-dots"><span></span><span></span><span></span></span>';
            }
          } catch(e) {}
        }
      });
      if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  } catch(e) {
    if (e.name === 'AbortError') return;
    if (el) el.innerHTML = '<span class="chat-dots"><span></span><span></span><span></span></span>';
  }
}

function resumeConvTask() {
  if (!_convSeq) return;
  const task = _getConvTask(_convSeq);
  if (task && task.task_id) {
    fetch('/api/events/chat/task/' + task.task_id + '/status', {
      headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
    }).then(r => r.json()).then(st => {
      if (st.status === 'running') {
        const messagesEl = document.getElementById('convMessages');
        let el = document.getElementById(task.el_id);
        if (!el && messagesEl) {
          messagesEl.insertAdjacentHTML('beforeend',
            '<div style="display:flex;justify-content:flex-start"><div id="' + task.el_id +
            '" style="max-width:85%;padding:8px 10px;border-radius:8px;font-size:12px;line-height:1.5;background:var(--bg);border:1px solid var(--b)">' +
            '<span class="chat-dots"><span></span><span></span><span></span></span></div></div>');
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }
        streamConvTask(task.task_id, task.el_id, _convSeq);
      } else {
        _clearConvTask(_convSeq);
        loadConversation();
      }
    }).catch(() => {});
  } else {
    API.get('/events/' + _convSeq + '/thread').then(d => {
      const msgs = d.messages || [];
      const lastMsg = msgs[msgs.length - 1];
      if (lastMsg && lastMsg.role === 'user' && lastMsg.task_id) {
        _setConvTask(_convSeq, {task_id: lastMsg.task_id, el_id: 'convReply' + Date.now(), status: 'running'});
        resumeConvTask();
      }
    }).catch(() => {});
  }
}

// ===== 对话窗口全屏/还原（按钮在 convPanel 标题栏）=====
let _convFs = false;
function toggleConvFullscreen() {
  const panel = document.getElementById('convPanel');
  if (!panel) return;
  if (!_convFs) {
    // 进入全屏：消息区 + 输入行整体移入 fixed 全屏容器（DOM 移动保留 id/内联事件）
    const messages = document.getElementById('convMessages');
    const inputRow = document.getElementById('convInputRow');
    if (!messages) return;
    const fs = document.createElement('div');
    fs.id = 'convFullscreen';
    fs.style.cssText = 'position:fixed;inset:0;z-index:999;background:var(--bg);display:flex;flex-direction:column;gap:10px;padding:18px;box-sizing:border-box';
    const bar = document.createElement('div');
    bar.style.cssText = 'display:flex;justify-content:space-between;align-items:center;flex-shrink:0';
    const title = document.createElement('h3');
    title.style.cssText = 'font-size:13px;font-weight:600';
    const agentName = (_AGENT_TYPES[_convAgent] && _AGENT_TYPES[_convAgent].display_name) || _convAgent || '';
    title.textContent = I18N.t('events.section_conversation', {name: agentName});
    const btn = document.createElement('button');
    btn.textContent = I18N.t('events.fullscreen_restore_btn');
    btn.title = I18N.t('events.exit_fullscreen_title');
    btn.style.cssText = 'border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer';
    btn.onclick = toggleConvFullscreen;
    bar.appendChild(title);
    bar.appendChild(btn);
    fs.appendChild(bar);
    fs.appendChild(messages);
    fs.appendChild(inputRow);
    document.body.appendChild(fs);
    panel.style.display = 'none';
    _convFs = true;
    messages.scrollTop = messages.scrollHeight;
  } else {
    const fs = document.getElementById('convFullscreen');
    if (!fs) return;
    const messages = document.getElementById('convMessages');
    const inputRow = document.getElementById('convInputRow');
    panel.appendChild(messages);
    panel.appendChild(inputRow);
    fs.remove();
    panel.style.display = 'flex';
    _convFs = false;
    messages.scrollTop = messages.scrollHeight;
  }
}
// ===== 接收上下文全屏/还原 =====
let _promptFs = false;
let _promptFsPlaceholder = null;

function _togglePromptFullscreen() {
  const body = document.getElementById('promptBody');
  if (!body) return;
  if (!_promptFs) {
    if (body.style.display === 'none') return; // 折叠状态不响应
    _promptFsPlaceholder = document.createElement('div');
    body.parentElement.insertBefore(_promptFsPlaceholder, body);
    const fs = document.createElement('div');
    fs.id = 'promptFullscreen';
    fs.style.cssText = 'position:fixed;inset:0;z-index:999;background:#000;display:flex;flex-direction:column;box-sizing:border-box';
    const bar = document.createElement('div');
    bar.style.cssText = 'display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid #2a3140;flex-shrink:0';
    const title = document.createElement('span');
    title.style.cssText = 'font-size:13px;font-weight:600;color:#fff;margin-right:auto';
    title.textContent = I18N.t('events.section_prompt');
    bar.appendChild(title);
    const btn = document.createElement('button');
    btn.textContent = I18N.t('events.fullscreen_restore_btn');
    btn.title = I18N.t('events.exit_fullscreen_title');
    btn.style.cssText = 'border:1px solid #3a4150;background:transparent;color:#c8d0e0;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer';
    btn.onclick = _togglePromptFullscreen;
    bar.appendChild(btn);
    fs.appendChild(bar);
    body.style.maxHeight = 'none';
    body.style.flex = '1';
    body.style.borderRadius = '0';
    body.style.minHeight = '0';
    fs.appendChild(body);
    document.body.appendChild(fs);
    _promptFs = true;
  } else {
    const fs = document.getElementById('promptFullscreen');
    if (!fs) return;
    if (_promptFsPlaceholder) {
      _promptFsPlaceholder.parentElement.insertBefore(body, _promptFsPlaceholder);
      _promptFsPlaceholder.remove();
      _promptFsPlaceholder = null;
    }
    body.style.maxHeight = '400px';
    body.style.flex = '';
    body.style.borderRadius = '8px';
    body.style.minHeight = '';
    fs.remove();
    _promptFs = false;
  }
}

// ESC 键退出全屏
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') {
    if (_convFs) toggleConvFullscreen();
    if (_promptFs) _togglePromptFullscreen();
  }
});

function redispatchEvent(seq) {
  _showRedispatchGlass(seq);
}

function _showRedispatchGlass(seq) {
  // 移除已有弹窗
  _closeRedispatchGlass();
  // 遮罩（居中展示）
  const overlay = document.createElement('div');
  overlay.className = 'rdp-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.15);backdrop-filter:blur(6px) saturate(150%);-webkit-backdrop-filter:blur(6px) saturate(150%);animation:rdpFade .2s ease';
  // 弹窗 — 居中
  const modal = document.createElement('div');
  modal.className = 'rdp-glass';
  modal.style.cssText = 'width:340px;background:rgba(255,255,255,0.72);backdrop-filter:blur(28px) saturate(200%);-webkit-backdrop-filter:blur(28px) saturate(200%);border-radius:18px;border:1px solid rgba(255,255,255,0.6);box-shadow:0 12px 40px rgba(0,0,0,0.12),0 2px 8px rgba(0,0,0,0.06);overflow:hidden;animation:rdpPop .25s cubic-bezier(0.34,1.56,0.64,1)';
  modal.innerHTML = `
    <div style="padding:18px 20px 14px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <div style="width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#ff9f0a,#ff6a00);display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0;box-shadow:0 4px 12px rgba(255,159,10,0.3)">🔄</div>
        <div>
          <div style="font-size:14px;font-weight:700;color:var(--t,#1d1d1f);letter-spacing:-0.2px">${I18N.t('events.redispatch_title')}</div>
          <div style="font-size:11px;color:var(--t3,#86868b)">Seq #${seq}</div>
        </div>
      </div>
      <div style="font-size:12px;color:var(--t2,#6e6e73);line-height:1.6;margin-bottom:14px">
        ${I18N.t('events.redispatch_desc')}
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="bt bt-o bt-s" style="padding:8px 16px;font-size:12px" onclick="_closeRedispatchGlass()">${I18N.t('events.redispatch_cancel')}</button>
        <button class="bt bt-s" style="padding:8px 16px;font-size:12px;background:linear-gradient(135deg,#ff9f0a,#ff6a00);color:#fff;border:none;box-shadow:0 4px 12px rgba(255,159,10,0.35)" onclick="_doRedispatch(${seq})">${I18N.t('events.redispatch_confirm')}</button>
      </div>
    </div>`;
  overlay.appendChild(modal);
  // 点击遮罩关闭（mousedown 判断，避免 click 透传）
  overlay.addEventListener('mousedown', function(e) {
    if (e.target === overlay) _closeRedispatchGlass();
  });
  // Escape 关闭
  const _escHandler = function(e) {
    if (e.key === 'Escape') {
      _closeRedispatchGlass();
      document.removeEventListener('keydown', _escHandler);
    }
  };
  document.addEventListener('keydown', _escHandler);
  document.body.appendChild(overlay);
}

function _closeRedispatchGlass() {
  const ex = document.querySelector('.rdp-overlay');
  if (ex) ex.remove();
}

function _doRedispatch(seq) {
  _closeRedispatchGlass();
  API.post('/events/' + seq + '/redispatch').then(d => {
    app.showToast(I18N.t('events.redispatch_toast_success'));
    app.nav('event', seq);
  }).catch(e => {
    const msg = (e.detail && e.detail.message) || e.message || e;
    app.showToast(I18N.t('events.redispatch_toast_failed', {msg: msg}));
  });
}

// ===== 实时运行日志（task_logs SSE，grape-task-logs-analysis.md）=====
// 模态面板：fetch + ReadableStream 消费 SSE，自动跟随（滚离底部停止，回到底部恢复）
let _taskLog = {taskId: '', title: '', seq: 0, aborter: null, following: true, statusEl: null, bodyEl: null, waitTimer: null, hasMsg: false};

function _tlEsc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _tlIcon(type) {
  if (type === 'tool_use') return '🔧';
  if (type === 'step_start') return '▶';
  if (type === 'step_finish') return '✅';
  if (type === 'reasoning') return '🤔';
  if (type === 'error') return '❌';
  if (type === 'text') return '💬';
  return '·';
}

function _tlLine(payload) {
  const type = payload.type || 'log';
  const time = payload.created_at ? `<span style="color:#7a85a0;margin-right:6px;font-size:13px">${fmtLocalTime(payload.created_at).substring(11, 19)}</span>` : '';
  if (type === 'tool_use') {
    let inputHtml = '';
    try {
      inputHtml = `<div style="margin:4px 0 4px 24px;white-space:pre-wrap;word-break:break-all;color:#b8c2d6;font-size:13px">${_tlEsc(JSON.stringify(payload.input || {}, null, 2))}</div>`;
    } catch (e) {}
    return `<div style="margin-bottom:8px">${time}🔧 <span style="color:#5b9eff;font-weight:600">${_tlEsc(payload.tool || 'tool')}</span>${inputHtml}</div>`;
  }
  const color = type === 'error' ? '#ff5b5b' : type === 'reasoning' ? '#a0aac0' : '#e0e6f0';
  return `<div style="margin-bottom:8px">${time}${_tlIcon(type)} <span style="color:${color};white-space:pre-wrap;word-break:break-all">${_tlEsc(payload.content || '')}</span></div>`;
}

function _tlStatus(text, color) {
  if (_taskLog.statusEl) _taskLog.statusEl.innerHTML = `<span style="color:${color || 'var(--t3)'}">${_tlEsc(text)}</span>`;
}

function openTaskLog(taskId, title) {
  closeTaskLog();
  _taskLog.taskId = taskId;
  _taskLog.title = title || taskId;
  _taskLog.seq = 0;  // task_messages seq 从 1 递增，不等于 event seq
  _taskLog.following = true;

  const ov = document.createElement('div');
  ov.id = 'taskLogOverlay';
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
  ov.innerHTML = `<div style="width:min(960px,94vw);height:min(720px,90vh);background:var(--bg,#11151c);border:1px solid var(--b,#2a3140);border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,.5)">
    <div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--b,#2a3140);flex-shrink:0">
      <span style="font-size:13px;font-weight:600">${I18N.t('events.log_title_realtime')} <span style="color:var(--t3);font-weight:400">· ${_tlEsc(title)}</span></span>
      <span id="tlStatus" style="font-size:11px;margin-left:auto"></span>
      <button id="tlCopyBtn" onclick="_copyTaskLog()" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;transition:all .2s" title="${I18N.t('events.log_copy_title')}">${I18N.t('events.log_copy')}</button>
      <button id="tlKillBtn" onclick="terminateCurrentTask()" style="border:1px solid var(--tr,#e74c3c);background:transparent;color:var(--tr,#e74c3c);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;display:none" title="${I18N.t('events.log_kill_title')}">${I18N.t('events.log_kill')}</button>
      <button id="taskLogFullscreenBtn" onclick="_toggleTaskLogFullscreen()" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer" title="${I18N.t('events.fullscreen_title')}">${I18N.t('events.fullscreen_btn')}</button>
      <button onclick="closeTaskLog()" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer">${I18N.t('events.log_close')}</button>
    </div>
    <div id="tlInfo" style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:7px 14px;border-bottom:1px solid var(--b,#2a3140);flex-shrink:0;font-size:11px;color:var(--t2,#8a94a6)"></div>
    <div id="tlBody" style="flex:1;min-height:0;overflow-y:auto;background:#000;color:#fff;padding:14px 18px;font-size:15px;font-family:'JetBrains Mono','Fira Code','Cascadia Code','Source Code Pro','SF Mono',Consolas,'Liberation Mono',Menlo,monospace;line-height:1.75;letter-spacing:0.2px"></div>
  </div>`;
  document.body.appendChild(ov);
  _taskLog.statusEl = document.getElementById('tlStatus');
  _taskLog.bodyEl = document.getElementById('tlBody');
  _tlStatus(I18N.t('events.status_connecting'));
  // 自动跟随：用户向上滚 >120px 停止跟随，回到底部边缘恢复
  _taskLog.bodyEl.addEventListener('scroll', () => {
    const el = _taskLog.bodyEl;
    const away = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (away > 120) _taskLog.following = false;
    else if (away < 30) _taskLog.following = true;
  });
  ov.addEventListener('click', e => { if (e.target === ov) closeTaskLog(); });
  // 加载执行引擎/机器信息（智能体名+版本、设备名、IP、用户名）
  _tlLoadInfo(taskId);
  _taskLogStream();
}

function _tlLoadInfo(taskId) {
  const token = localStorage.getItem('token') || '';
  fetch('/api/task-logs/' + encodeURIComponent(taskId) + '/info', {headers: {'Authorization': 'Bearer ' + token}})
    .then(r => r.ok ? r.json() : null)
    .then(info => {
      if (!info) return;
      const el = document.getElementById('tlInfo');
      if (!el) return;
      const bits = [];
      if (info.agent_name) bits.push('🤖 ' + _tlEsc(info.agent_name) + (info.agent_version ? ' <span style="color:var(--t3)">v' + _tlEsc(info.agent_version) + '</span>' : ''));
      if (info.device_name) bits.push('🖥 ' + _tlEsc(info.device_name));
      if (info.last_ip) bits.push('🌐 ' + _tlEsc(info.last_ip));
      if (info.owner_name) bits.push('👤 ' + _tlEsc(info.owner_name));
      el.innerHTML = bits.map(b => '<span>' + b + '</span>').join('');
    })
    .catch(() => {});
}

function _tlArmWaitHint() {
  // hermes 类引擎 CLI 一次性输出（推理期间无 stdout），执行中长时间无消息属正常——
  // 8s 无消息给等待提示，避免误以为面板未生效
  _taskLog.waitTimer = setTimeout(() => {
    if (!_taskLog.taskId) return;
    if (_taskLog.bodyEl && !_taskLog.hasMsg && _taskLog.bodyEl.childElementCount === 0) {
      _taskLog.bodyEl.insertAdjacentHTML('beforeend',
        '<div class="tl-waiting" style="color:#a0aac0;padding:10px 0;font-size:15px">' + I18N.t('events.status_engine_waiting') + '</div>');
      _tlStatus(I18N.t('events.status_engine_waiting'), 'var(--ty, #f1c40f)');
    }
  }, 8000);
}

function _taskLogStream() {
  const token = localStorage.getItem('token') || '';
  const ac = new AbortController();
  _taskLog.aborter = ac;
  fetch('/api/task-logs/' + encodeURIComponent(_taskLog.taskId) + '/stream?since=' + _taskLog.seq, {
    headers: {'Authorization': 'Bearer ' + token},
    signal: ac.signal,
  }).then(resp => {
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    _tlStatus(I18N.t('events.status_connected'), 'var(--tgx, #2ecc71)');
    _taskLog.hasMsg = false;
    _tlCheckTaskStatus();
    _tlArmWaitHint();
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    function read() {
      return reader.read().then(({done, value}) => {
        if (done) {
          _tlStatus(I18N.t('events.status_stream_end'), 'var(--t3)');
          _tlHideKillBtn();
          if (_taskLog.waitTimer) { clearTimeout(_taskLog.waitTimer); _taskLog.waitTimer = null; }
          return;
        }
        buf += dec.decode(value, {stream: true});
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const part of parts) {
          if (!part.startsWith('data: ')) continue;
          let evt;
          try { evt = JSON.parse(part.slice(6)); } catch (e) { continue; }
          if (typeof evt.seq === 'number' && evt.seq <= _taskLog.seq) continue; // 断线重连去重
          _taskLog.seq = (typeof evt.seq === 'number' && evt.seq > _taskLog.seq) ? evt.seq : _taskLog.seq;
          if (!_taskLog.hasMsg) {
            _taskLog.hasMsg = true;
            if (_taskLog.waitTimer) { clearTimeout(_taskLog.waitTimer); _taskLog.waitTimer = null; }
            const ph = _taskLog.bodyEl && _taskLog.bodyEl.querySelector('.tl-waiting');
            if (ph) ph.remove();
            _tlStatus(I18N.t('events.status_connected'), 'var(--tgx, #2ecc71)');
          }
          _taskLog.bodyEl.insertAdjacentHTML('beforeend', _tlLine(evt));
          if (_taskLog.following) _taskLog.bodyEl.scrollTop = _taskLog.bodyEl.scrollHeight;
        }
        return read();
      });
    }
    return read();
  }).catch(err => {
    if (err.name === 'AbortError') return;
    if (_taskLog.waitTimer) { clearTimeout(_taskLog.waitTimer); _taskLog.waitTimer = null; }
    _tlStatus(I18N.t('events.status_disconnected'), 'var(--tr, #e74c3c)');
    setTimeout(() => { if (_taskLog.taskId) _taskLogStream(); }, 2000);
  });
}

function _copyTaskLog() {
  if (!_taskLog.bodyEl) return;
  var txt = _taskLog.bodyEl.innerText;
  var btn = document.getElementById('tlCopyBtn');
  if (!btn) return;
  var orig = btn.textContent;
  var origBg = btn.style.background;
  var origColor = btn.style.color;
  function _flashOk() {
    btn.textContent = I18N.t('events.log_copied');
    btn.style.background = 'var(--g,#1ca44c)';
    btn.style.color = '#fff';
    btn.style.borderColor = 'var(--g,#1ca44c)';
    setTimeout(function() {
      btn.textContent = orig;
      btn.style.background = origBg;
      btn.style.color = origColor;
      btn.style.borderColor = '';
    }, 1500);
  }
  function _flashFail() {
    btn.textContent = I18N.t('events.log_copy_failed');
    btn.style.background = 'var(--r,#dc2828)';
    btn.style.color = '#fff';
    btn.style.borderColor = 'var(--r,#dc2828)';
    setTimeout(function() {
      btn.textContent = orig;
      btn.style.background = origBg;
      btn.style.color = origColor;
      btn.style.borderColor = '';
    }, 1500);
  }
  // navigator.clipboard API（HTTPS / localhost）
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(_flashOk, function() {
      _fallbackCopy(txt, _flashOk, _flashFail);
    });
  } else {
    _fallbackCopy(txt, _flashOk, _flashFail);
  }
}

function _fallbackCopy(text, onOk, onFail) {
  try {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    var ok = document.execCommand('copy');
    document.body.removeChild(ta);
    if (ok) { onOk(); } else { onFail(); }
  } catch (e) {
    onFail();
  }
}

function _toggleTaskLogFullscreen() {
  const ov = document.getElementById('taskLogOverlay');
  if (!ov) return;
  const box = ov.firstElementChild;
  if (!box) return;
  const isFs = box.dataset.fs === '1';
  if (!isFs) {
    box.dataset.fs = '1';
    box.style.width = '100vw';
    box.style.height = '100vh';
    box.style.borderRadius = '0';
    ov.style.padding = '0';
    const b = document.getElementById('taskLogFullscreenBtn');
    if (b) { b.textContent = I18N.t('events.fullscreen_restore_btn'); b.title = I18N.t('events.exit_fullscreen_title'); }
  } else {
    box.dataset.fs = '';
    box.style.width = 'min(960px,94vw)';
    box.style.height = 'min(720px,90vh)';
    box.style.borderRadius = '10px';
    const b = document.getElementById('taskLogFullscreenBtn');
    if (b) { b.textContent = I18N.t('events.fullscreen_btn'); b.title = I18N.t('events.fullscreen_title'); }
  }
}

function _tlHideKillBtn() {
  const btn = document.getElementById('tlKillBtn');
  if (btn) btn.style.display = 'none';
}

function _tlCheckTaskStatus() {
  if (!_taskLog.taskId) return;
  const token = localStorage.getItem('token') || '';
  fetch('/api/runtime/tasks/' + encodeURIComponent(_taskLog.taskId) + '/status', {
    headers: {'Authorization': 'Bearer ' + token},
  }).then(r => r.ok ? r.json() : null).then(d => {
    if (!d || !d.status) return;
    if (d.status === 'running' || d.status === 'dispatched') {
      const btn = document.getElementById('tlKillBtn');
      if (btn) btn.style.display = '';
    } else {
      _tlHideKillBtn();
    }
  }).catch(() => {});
}

function terminateEventTask(seq, taskId) {
  _doTerminateEvent(seq, taskId);
}

function terminateCurrentTask() {
  if (!_taskLog.taskId && !_taskLog.seq) return;
  // 优先走 event terminate（同时更新 task + event 状态）
  if (_taskLog.seq) {
    _doTerminateEvent(_taskLog.seq, _taskLog.taskId, function() {
      _tlStatus(I18N.t('events.status_terminating'), 'var(--tr, #e74c3c)');
      _tlHideKillBtn();
    });
  } else {
    _doTerminateTask(_taskLog.taskId, function() {
      _tlStatus(I18N.t('events.status_terminating'), 'var(--tr, #e74c3c)');
      _tlHideKillBtn();
    });
  }
}

function _doTerminateTask(taskId, onSuccess) {
  API.post('/runtime/tasks/' + encodeURIComponent(taskId) + '/cancel').then(function(d) {
    if (onSuccess) onSuccess();
  }).catch(function(e) {
    app.showToast(I18N.t('events.terminate_failed', {msg: (e.message || e)}));
  });
}

function _doTerminateEvent(seq, taskId, onSuccess) {
  // 优先走 event terminate（同时支持 direct 和 worker 模式）
  API.post('/events/' + seq + '/terminate').then(function(d) {
    if (onSuccess) onSuccess();
    else {
      app.showToast(I18N.t('events.task_terminated_sent'));
      API.get('/events/detail/' + seq).then(renderEventDetail);
    }
  }).catch(function(e) {
    // fallback: 如果有 task_id 尝试直接 cancel
    if (taskId) {
      _doTerminateTask(taskId, function() {
        if (onSuccess) onSuccess();
        else {
          app.showToast(I18N.t('events.task_terminated_sent'));
          API.get('/events/detail/' + seq).then(renderEventDetail);
        }
      });
    } else {
      app.showToast(I18N.t('events.terminate_failed', {msg: (e.message || e)}));
    }
  });
}

function closeTaskLog() {
  if (_taskLog.aborter) _taskLog.aborter.abort();
  if (_taskLog.waitTimer) { clearTimeout(_taskLog.waitTimer); _taskLog.waitTimer = null; }
  _taskLog.aborter = null;
  _taskLog.taskId = '';
  const ov = document.getElementById('taskLogOverlay');
  if (ov) ov.remove();
}

// ===== 内联 agent_logs 显示（direct 模式无 task_id，从 event detail agent_logs 字段展示）=====
function openInlineLog(seq, title) {
  closeTaskLog();
  if (!seq) return;
  API.get('/events/detail/' + seq).then(function(data) {
    var logs = data.agent_logs || [];
    var bodyHtml;
    if (logs.length) {
      bodyHtml = logs.map(function(l) {
        if (!l) return '';
        var type = l.type || 'log';
        var time = l.time ? '<span style="color:#7a85a0;margin-right:6px">' + (l.time.substring(11,19) || '') + '</span>' : '';
        var msg = l.msg || '';
        if (type === 'tool_use') {
          var toolName = msg;
          var inputHtml = '';
          try {
            var parsed = JSON.parse(msg);
            if (parsed.tool) toolName = _tlEsc(parsed.tool);
            if (parsed.input) inputHtml = '<div style="margin:4px 0 4px 24px;white-space:pre-wrap;word-break:break-all;color:#b8c2d6;font-size:13px">' + _tlEsc(JSON.stringify(parsed.input, null, 2)) + '</div>';
          } catch(e) {}
          return '<div style="margin-bottom:8px">' + time + '🔧 <span style="color:#5b9eff;font-weight:600">' + toolName + '</span>' + inputHtml + '</div>';
        }
        var icon = type === 'error' ? '❌' : type === 'reasoning' ? '🤔' : type === 'step_start' ? '▶' : type === 'text' ? '💬' : '·';
        var color = type === 'error' ? '#ff5b5b' : type === 'reasoning' ? '#a0aac0' : '#e0e6f0';
        return '<div style="margin-bottom:8px">' + time + icon + ' <span style="color:' + color + ';white-space:pre-wrap;word-break:break-all">' + _tlEsc(msg) + '</span></div>';
      }).join('');
    } else {
      var resultText = data.result || I18N.t('events.no_inline_log');
      bodyHtml = '<div style="margin-bottom:8px">❌ <span style="color:#ff5b5b;white-space:pre-wrap;word-break:break-all">' + _tlEsc(resultText) + '</span></div>';
    }
    var ov = document.createElement('div');
    ov.id = 'taskLogOverlay';
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    ov.innerHTML = '<div style="width:min(960px,94vw);height:min(720px,90vh);background:var(--bg,#11151c);border:1px solid var(--b,#2a3140);border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,.5)">'
      + '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--b,#2a3140);flex-shrink:0">'
      + '<span style="font-size:13px;font-weight:600">' + I18N.t('events.log_title_inline') + ' <span style="color:var(--t3);font-weight:400">· ' + _tlEsc(title) + '</span></span>'
      + '<span style="font-size:11px;margin-left:auto;color:var(--t3)">' + logs.length + I18N.t('events.log_count_suffix') + '</span>'
      + '<button onclick="closeTaskLog()" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer">' + I18N.t('events.log_close') + '</button>'
      + '</div>'
      + '<div style="flex:1;min-height:0;overflow-y:auto;background:#000;color:#fff;padding:14px 18px;font-size:15px;font-family:JetBrains Mono,Fira Code,Cascadia Code,Source Code Pro,SF Mono,Consolas,Liberation Mono,Menlo,monospace;line-height:1.75;letter-spacing:0.2px">' + bodyHtml + '</div>'
      + '</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function(e) { if (e.target === ov) closeTaskLog(); });
  }).catch(function(e) {
    app.showToast(I18N.t('events.load_log_failed', {msg: e.message}));
  });
}
