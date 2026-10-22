// Event Detail：结构化产出 + Event Info + 与事件所属 agent 的对话（event-conversation-design.md）
let _convSeq = null;
let _convAgent = '';

function _autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// 从事件详情页返回需求详情页（路由 push 带任务 ID，非 history.back）
window._backToTaskDetail = function(taskId) {
  if (!taskId) { window.history.back(); return; }
  window._pendingTaskDetailId = taskId;
  window._eventFromTask = null;
  if (typeof app !== 'undefined' && app.nav) {
    app.nav('tasks');
  } else {
    window.history.back();
  }
};

function navEvent(seq) {
  if (!seq || seq < 1) return;
  // 局部刷新：直接拿数据调 renderEventDetail，不走路由避免整体闪抖
  API.get('/events/detail/' + seq).then(function(data) {
    // 仅更新标题栏 seq，不重置 pageContent
    var titleEl = document.getElementById('pageTitle');
    if (titleEl) titleEl.textContent = I18N.t('events.page_title_prefix', {seq: seq});
    renderEventDetail(data);
    // Issue #224: 同步更新 URL hash（replaceState 避免污染浏览器历史栈）
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, '', '#event/' + seq);
    }
  }).catch(function(e) {
    // Issue #224: 403 无权限访问该事件（可能属于其他仓库）→ 提示用户，不静默失败
    var status = (e && e.status) || 0;
    if (status === 403) {
      if (typeof app !== 'undefined' && app.showToast) {
        app.showToast(I18N.t('events.nav_forbidden', {seq: seq}));
      }
      return;
    }
    // 事件不存在 → 后端返回 max_seq，跳转到最后一个事件
    var detail = (e && e.detail) || {};
    var maxSeq = detail.max_seq;
    if (maxSeq && maxSeq > 0 && maxSeq !== seq) {
      navEvent(maxSeq);
    }
  });
}

// ===== 事件详情 running 状态自动轮询（2026-10-12 / Issue #342 局部化）=====
// 背景：A2A 补发/新事件跳转后，事件最初是 running 且引擎未认领（无 engine_instance、
// 日志空），详情页一次渲染就静止 → 用户必须手动刷新。此轮询仅对非终态事件生效，
// 5s 间隔。
// Issue #342：轮询回调不再整页重写 pageContent（原 renderEventDetail 全量 innerHTML
// 会重置折叠态/已加载 prompt/对话输入/滚动位置，造成持续闪动的"整页刷新"体验），
// 改为 _patchEventDetail 定向更新动态片段；数据指纹未变化时零 DOM 写入。
var _eventPollTimer = null;
var _eventPollSeq = 0;
var _eventPollBusy = false;

function _startEventPoll(data) {
  var _st = (data && data.status) || '';
  // 终态（completed/failed/cancelled）→ 停止轮询
  if (_st === 'completed' || _st === 'failed' || _st === 'cancelled') {
    _stopEventPoll();
    return;
  }
  var seq = data && data.seq;
  if (!seq) { _stopEventPoll(); return; }
  // 同一事件已轮询中 → 不重启（避免每次局部刷新都清重建 timer）
  if (_eventPollTimer && _eventPollSeq === seq) return;
  _stopEventPoll();
  _eventPollSeq = seq;
  _eventPollTimer = setInterval(function() {
    if (_eventPollBusy) return;
    if (window._currentView !== 'event') { _stopEventPoll(); return; }
    _eventPollBusy = true;
    API.get('/events/detail/' + _eventPollSeq).then(function(d) {
      _eventPollBusy = false;
      // 用户已切到别的事件 → 停止（新事件的 renderEventDetail 会自己重启轮询）
      if (window._currentView !== 'event' || _eventPollSeq !== d.seq) return;
      if (d.status === 'completed' || d.status === 'failed' || d.status === 'cancelled') {
        // 终态 → 停止轮询，完整渲染一次最终结论与终态按钮（此后不再刷新）
        _stopEventPoll();
        renderEventDetail(d);
        return;
      }
      // 非终态 → 定向局部更新（Issue #342：不整页重写，保留折叠/输入/滚动等交互状态）
      _patchEventDetail(d);
    }).catch(function() {
      _eventPollBusy = false;
    });
  }, 5000);
}

function _stopEventPoll() {
  if (_eventPollTimer) { clearInterval(_eventPollTimer); _eventPollTimer = null; }
  _eventPollSeq = 0;
}

// ===== 事件详情动态片段构建与定向局部更新（Issue #342）=====
// 将随事件状态变化的字段抽为片段构建函数：renderEventDetail（全量）与
// _patchEventDetail（轮询局部）复用同一构建逻辑，保证两条路径渲染结果一致。
var _evLastFp = '';

function _evResultOf(data) {
  return data.result || data.summary || '';
}

// M4：结构化产出（唯一结论）：优先 event.json archive.output，其次 events.result 自身的结构化 JSON。
function _evBuildStructuredHtml(o) {
  if (!o) return '';
  const actions = Array.isArray(o.actions) ? o.actions.map(a => `<div>• ${a}</div>`).join('') : '';
  const arts = Array.isArray(o.artifacts) ? o.artifacts.map(a => `<span class="tgx" style="margin-right:6px">${a}</span>`).join('') : '';
  return `${o.conclusion ? `<div style="font-size:12px;margin-bottom:3px"><b>${I18N.t('events.label_conclusion')}</b> ${o.conclusion}</div>` : ''}
    ${actions ? `<div style="font-size:12px;margin-bottom:3px"><b>${I18N.t('events.label_actions')}</b> ${actions}</div>` : ''}
    ${arts ? `<div style="font-size:12px;margin-bottom:3px"><b>${I18N.t('events.label_artifacts')}</b> ${arts}</div>` : ''}
    ${o.next_step ? `<div style="font-size:12px;margin-bottom:3px"><b>${I18N.t('events.label_next_step')}</b> ${o.next_step}</div>` : ''}
    ${o.issue_summary ? `<div style="font-size:12px;color:var(--a)"><b>${I18N.t('events.label_issue_summary')}</b> ${o.issue_summary}</div>` : ''}`;
}

function _evTryStructuredResult(r) {
  if (!r) return null;
  const s = String(r).trim();
  if (!(s.startsWith('{') && s.endsWith('}'))) return null;
  try {
    const o = JSON.parse(s);
    return (o && (o.conclusion || o.actions || o.artifacts || o.issue_summary)) ? o : null;
  } catch (e) { return null; }
}

// 区块1头部右侧摘要（Trigger · Actor · 状态图标 · 时长）
function _evInfoSummaryText(data) {
  return `${data.event_type || '—'} · ${data.actor || '—'} · ${data.status === 'completed' ? '✅' : data.status === 'failed' ? '❌' : '⏳'} ${data.duration ? data.duration + 's' : '—'}`;
}

function _evEngineText(data) {
  return `<span style="color:var(--t3)">Engine:</span> ${(() => { if (!data.engine_instance) return '<span class="ts t3">' + I18N.t('events.engine_undispatched') + '</span>'; const h = data.engine_health; let health = ''; if (h && h.score !== null && h.score !== undefined) { const sc = h.score === 0 ? 'var(--r)' : h.score < 50 ? 'var(--y)' : 'var(--g)'; const hb = h.heartbeat_alive === false ? ' · 失联' : ''; health = ` · <span style="color:${sc};font-weight:600">健康分 ${h.score}</span>${h.consecutive_failures > 0 ? ` <span class="ts t3" style="font-size:10px">连续失败 ${h.consecutive_failures}</span>` : ''}${hb}`; } return `<b>${data.runtime || data.backend || 'OpenCode'}</b>${data.engine_device ? `<span class="ts t3"> · ${data.engine_device}</span>` : ''}${data.engine_ip ? `<span class="ts t3"> · ${data.engine_ip}</span>` : ''}${health}`; })()}`;
}

function _evDurationText(data) {
  return `<span style="color:var(--t3)">Duration:</span> ${data.duration ? data.duration + 's' : '—'}`;
}

// 重新派发按钮显示条件（event-redispatch-design.md §3.1）：
// failed / running>10min / 关联 task 卡在 dispatched
function _evRedispatchVisible(data) {
  const _ts = data.started_at ? new Date(data.started_at).getTime() : 0;
  const _created = data.created_at ? new Date(data.created_at).getTime() : 0;
  return data.status === 'failed' ||
    ((data.status === 'running' || data.status === 'pending') &&
      ((_ts && (Date.now() - _ts) > 10 * 60 * 1000) ||
       (!_ts && _created && (Date.now() - _created) > 10 * 60 * 1000))) ||
    (data.task && data.task.task_status === 'dispatched');
}

function _evStatusText(data) {
  // A2A 重新派发按钮显示条件（a2a-redo-button-design.md §2）：
  // completed + 真实 Issue（PR 订阅伪 #pr_ 不允许 A2A）
  const _redispatchVisible = _evRedispatchVisible(data);
  const _a2aRedoVisible =
    data.status === 'completed' &&
    data.issue_id && !String(data.issue_id).startsWith('#pr_');
  const _statusLabel = data.status === 'completed' ? I18N.t('events.status_success') : data.status === 'failed' ? I18N.t('events.status_failed') : (data.status === 'pending' ? (I18N.t('events.status_pending') || '排队中') : I18N.t('events.status_running_prefix') + data.status);
  return `<span class="${data.status === 'completed' ? 'tgx' : data.status === 'failed' ? 'tr' : data.status === 'pending' ? '' : 'ty'}">${_statusLabel}</span>${_redispatchVisible ? ` <button class="bt bt-o bt-s" style="margin-left:8px" onclick="redispatchEvent(${data.seq})">${I18N.t('events.btn_redispatch')}</button> <button class="bt bt-o bt-s" style="margin-left:4px" onclick="localRetryEvent(${data.seq})">${I18N.t('events.btn_local_retry')}</button>` : ''}${_a2aRedoVisible ? ` <button class="bt bt-o bt-s" style="margin-left:8px" onclick="a2aRedoEvent(${data.seq})">${I18N.t('events.btn_a2a_redo')}</button>` : ''}${(data.status === 'running' || data.status === 'pending') ? ` <button class="bt bt-o bt-s" style="margin-left:8px;color:var(--tr, #e74c3c);border-color:var(--tr, #e74c3c)" onclick="terminateEventTask(${data.seq}, '${(data.task && data.task.task_id) || ''}')" title="${I18N.t('events.terminate_title')}">${I18N.t('events.btn_terminate')}</button>` : ''}`;
}

function _evFailReasonText(data) {
  const txt = (data.result || '').toString().trim();
  // Step 1：失败原因只取 result，不再 fallback 事件标题/摘要
  // ——否则停机失败事件（result 为空）会显示"失败原因：A2A: → coding"等无意义文案（#2337）。
  return (data.status === 'failed' && txt)
    ? `<div style="margin-top:6px;background:rgba(255,59,48,.08);border:1px solid rgba(255,59,48,.18);border-radius:10px;padding:8px 12px;font-size:12px;color:#d32f2f;line-height:1.6"><b>${I18N.t('events.fail_reason')||'失败原因'}：</b>${String(txt).substring(0, 500)}</div>`
    : (data.status === 'failed'
      ? `<div style="margin-top:6px;font-size:11px;color:var(--t3,#86868b)">${I18N.t('events.no_fail_reason')||'暂无失败原因'}</div>`
      : '');
}

function _evLogRowText(data) {
  return data.task && data.task.task_id
    ? `<div style="margin-top:4px"><a href="javascript:void(0)" onclick="openTaskLog('${data.task.task_id}','seq#${data.seq}')" style="color:var(--a);text-decoration:underline">${I18N.t('events.view_realtime_log')}</a></div>`
    : `<div style="margin-top:4px"><a href="javascript:void(0)" onclick="openInlineLog(${data.seq}, 'seq#${data.seq}')" style="color:var(--a);text-decoration:underline">${I18N.t('events.view_log')}</a></div>`;
}

function _evProcessingText(data) {
  const result = _evResultOf(data);
  const arch = data.archive || null;
  const structuredHtml = _evBuildStructuredHtml((arch && arch.output) || _evTryStructuredResult(result));
  return structuredHtml ? `<div style="margin-bottom:12px;padding:10px 12px;background:#f0f1f4;border-radius:6px;border-left:3px solid var(--a)">${structuredHtml}</div>`
    : `${result ? renderMarkdown(result) : '<div style="color:var(--t3)">' + I18N.t('events.no_conclusion') + '</div>'}`;
}

// 轮询指纹：动态字段集合不变 → 跳过一切 DOM 写入（零闪动）
function _evFingerprint(d) {
  return JSON.stringify([d.status, d.duration, d.result, d.summary, d.engine_instance, d.runtime, d.backend,
    d.engine_device, d.engine_ip, (d.engine_health && d.engine_health.score) || null,
    d.archive && d.archive.output ? JSON.stringify(d.archive.output) : null,
    (d.task && d.task.task_status) || '', (d.task && d.task.task_id) || '',
    (d.agent_logs || []).length]);
}

function _evSetFragment(id, html) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = html;
}

// 定向局部更新：仅替换动态片段容器内容，保留折叠态/prompt/对话输入/滚动等交互状态。
// 页面结构不存在（如事件切换竞态）→ 退回全量渲染兜底。
function _patchEventDetail(data) {
  if (_convSeq !== data.seq || !document.getElementById('evDetailGrid')) { renderEventDetail(data); return; }
  const fp = _evFingerprint(data);
  if (fp === _evLastFp) return;
  _evLastFp = fp;
  _convSeq = data.seq;
  if (data.agent_type) _convAgent = data.agent_type;
  _evSetFragment('evInfoSummary', _evInfoSummaryText(data));
  _evSetFragment('evEngineRow', _evEngineText(data));
  _evSetFragment('evDurationRow', _evDurationText(data));
  _evSetFragment('evStatusRow', _evStatusText(data));
  _evSetFragment('evFailReason', _evFailReasonText(data));
  _evSetFragment('evLogRow', _evLogRowText(data));
  _evSetFragment('evProcessingBody', _evProcessingText(data));
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
  // 2026-10-12：running 事件自动轮询刷新（A2A 补发/新事件跳转后无需手动刷新即可
  // 看到引擎认领与日志）。终态或离开 event 页即停止；renderEventDetail 复入时重启。
  _startEventPoll(data);
  // Issue #342：记录数据指纹，轮询据此判断是否需要局部更新（未变化零 DOM 写入）
  _evLastFp = _evFingerprint(data);
  const agent = data.agent_type || 'vod';
  const agentName = (_AGENT_TYPES[agent] && _AGENT_TYPES[agent].display_name) || agent;
  _convSeq = data.seq;
  _convAgent = agent;

  // Issue/PR 编号 → VCS 超链接
  const _rawIssueId = String(data.issue_id || '');
  const _isPR = _rawIssueId.startsWith('#pr_');
  // PR 订阅伪 issue_id 格式 #pr_{pr_num}_{scene_id}，只取 pr_num 部分
  const _num = _isPR
    ? _rawIssueId.replace(/^#pr_/, '').split('_')[0]
    : _rawIssueId.replace(/^#/, '');
  const _vcsPath = _isPR ? '/pull/' + _num : '/issues/' + _num;
  const _label = _isPR ? 'PR #' + _num : 'Issue ' + data.issue_id;
  const _issueLink = (_num && data.repo_key)
    ? `<a href="${_vcsUrl(data.repo_key, _vcsPath)}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">${_label}</a>`
    : `${_label === 'Issue ' ? 'Issue —' : _label}`;

  // 返回按钮：优先返回需求详情页（携带需求上下文），否则 history.back()
  var _backBtn = '';
  if (window._eventFromTask && window._eventFromTask.taskId) {
    _backBtn = `<button class="bt bt-o bt-s" onclick="_backToTaskDetail(${window._eventFromTask.taskId})" style="margin-bottom:12px">← ${I18N.t('tasks.back_to_requirement')||'返回需求详情'}</button>`;
  } else {
    _backBtn = `<button class="bt bt-o bt-s" onclick="window.history.back()" style="margin-bottom:12px">← ${I18N.t('common.back')||'返回'}</button>`;
  }
  // 动态区块（Issue #342）：加 id 的片段容器由 _patchEventDetail 轮询定向更新，
  // 全量渲染与局部更新共用 _evXxxText 构建函数，保证两条路径渲染一致。
  const html = `${_backBtn}
  <div id="evDetailGrid" style="display:grid;grid-template-columns:1fr;gap:14px;height:calc(100vh - 160px);overflow:hidden">
    <!-- 左栏大框：导航 + 可折叠区块 + 处理记录+答复结论 -->
    <div class="cd" style="padding:16px 20px;display:flex;flex-direction:column;overflow:hidden">
      <!-- 顶部导航栏 -->
      <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--b)">
        <button class="bt bt-o bt-s" onclick="navEvent(${data.prev_seq || 0})" title="${I18N.t('events.prev_event_title', {name: agentName})}" ${!data.prev_seq ? 'disabled style="opacity:.4;cursor:not-allowed"' : ''}>‹</button>
        <span class="ts t3" style="font-size:12px;font-weight:600">Event #${data.seq || '—'}</span>
        <button class="bt bt-o bt-s" onclick="navEvent(${data.next_seq || 0})" title="${I18N.t('events.next_event_title', {name: agentName})}" ${!data.next_seq ? 'disabled style="opacity:.4;cursor:not-allowed"' : ''}>›</button>
        ${data.status === 'failed' ? `<button class="bt bt-o bt-s" style="margin-left:auto" onclick="openFeedbackDialog({event_seq:${data.seq}})" title="${I18N.t('events.btn_feedback_title') || '针对该失败事件提交问题反馈（自动携带仓库/任务/引擎上下文）'}">${I18N.t('events.btn_feedback') || '反馈'}</button>` : ''}
      </div>
      <!-- 区块1：Event Context & Info（可折叠，默认展开） -->
      <div class="ev-section" style="flex-shrink:0;margin-bottom:8px">
        <div class="ev-section-header" onclick="_toggleEvSection('ctxInfo', this)" style="display:flex;align-items:center;gap:6px;cursor:pointer;padding:6px 0;user-select:none">
          <span class="ev-arrow" style="font-size:10px;transition:transform .2s;display:inline-block">▼</span>
          <span style="font-size:12px;font-weight:600">${I18N.t('events.section_context_info')}</span>
          <span id="evInfoSummary" class="ts t3" style="font-size:11px;margin-left:auto">${_evInfoSummaryText(data)}</span>
        </div>
        <div id="ctxInfo" class="ev-section-body" style="display:grid;grid-template-columns:1fr 1fr;gap:14px;padding-bottom:10px;border-bottom:1px solid var(--b)">
          <div>
            <h3 style="font-size:12px;font-weight:600;margin-bottom:6px">${I18N.t('events.section_context')}</h3>
            <div class="tx t2" style="line-height:1.6">
              <div>Trigger: ${data.event_type || '—'}</div>
              <div>Repository: ${data.repo_key || ''}</div>
              <div>${_isPR ? 'PR' : 'Issue'}: ${_issueLink}</div>
              <div>Actor: ${data.actor || '—'}</div>
              <div>Time: ${fmtLocalTime(data.created_at) || ''}</div>
            </div>
          </div>
          <div>
            <h3 style="font-size:12px;font-weight:600;margin-bottom:6px">${I18N.t('events.section_info')}</h3>
            <div style="font-size:11px;color:var(--t2);line-height:1.8">
              <div><span style="color:var(--t3)">Seq:</span> <b>#${data.seq || '—'}</b></div>
              <div id="evEngineRow">${_evEngineText(data)}</div>
              <div><span style="color:var(--t3)">Skill:</span> ${data.skill || agent}</div>
              <div id="evDurationRow">${_evDurationText(data)}</div>
              <div id="evStatusRow">${_evStatusText(data)}</div>
              <div id="evFailReason">${_evFailReasonText(data)}</div>
              <div id="evLogRow">${_evLogRowText(data)}</div>
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
      <div id="evProcessingBody" style="flex:1;overflow-y:auto;background:var(--bg);color:var(--t);border-radius:8px;padding:14px;font-size:13px;line-height:1.6">${_evProcessingText(data)}</div>
      <div id="convOutcome" class="cd mt12" style="padding:10px;font-size:11px;flex-shrink:0;display:none"></div>
    </div>
    <!-- 右栏：对话面板（有对话/门禁开放才显示，见 loadConversation 条件渲染 Issue #35） -->
    <div id="convPanel" class="cd" style="padding:14px;display:none;flex-direction:column;gap:8px;height:100%;overflow:hidden">
      <div style="flex-shrink:0;display:flex;justify-content:space-between;align-items:center;gap:8px">
        <h3 style="font-size:12px;font-weight:600;margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">${I18N.t('events.section_conversation', {name: agentName})}</h3>
        <div style="display:flex;gap:6px;align-items:center;flex-shrink:0">
          <button onclick="clearConversation(event, ${_convSeq})" title="${I18N.t('events.clear_conv_title')}" style="border:1px solid var(--b);background:transparent;color:#ff3b30;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;white-space:nowrap">${I18N.t('events.clear_conv_btn')}</button>
          <button onclick="toggleConvFullscreen()" title="${I18N.t('events.fullscreen_title')}" style="border:1px solid var(--b);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;white-space:nowrap">${I18N.t('events.fullscreen_btn')}</button>
        </div>
      </div>
      <div id="convMessages" style="flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:8px;font-size:12px"></div>
      <div id="convInputRow" style="display:none;gap:6px;flex-shrink:0">
        <textarea id="convInput" rows="1" placeholder="${I18N.t('events.conv_input_placeholder')}" style="flex:1;padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg);resize:vertical;max-height:120px;overflow-y:auto" oninput="_autoResizeTextarea(this)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendConversation()}"></textarea>
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
  API.get('/task-logs/' + encodeURIComponent(taskId) + '/info')
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
    .catch(e => { if (e.name === 'AbortError' && e.silent) return; _pc('<span style="color:#ff5b5b">' + I18N.t('events.load_failed') + '</span>'); });
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
  _convKnowledge = knowledge || {};
  var scopeLabels = {project: I18N.t('events.scope_project'), issue: I18N.t('events.scope_issue'), issue_agent: I18N.t('events.scope_agent_solo')};
  var scopeIcons = {project: '📦', issue: '📋', issue_agent: '🤖'};
  var kCards = Object.keys(knowledge || {}).map(function(scope) {
    if (!knowledge[scope]) return '';
    var content = knowledge[scope];
    var summary = content.substring(0, 30).replace(/\n/g, ' ').replace(/- \[seq[^\]]*\] /g, '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    var len = content.length;
    return '<div class="k-card" onclick="showKnowledgeDetail(\'' + scope + '\')">'
      + '<span class="k-card-label">' + scopeIcons[scope] + ' ' + scopeLabels[scope] + '</span>'
      + '<span class="k-card-summary">' + summary + '… (' + len + '字)</span>'
      + '<button class="k-card-x" onclick="event.stopPropagation();quickDeleteKnowledge(\'' + scope + '\')">&times;</button>'
      + '</div>';
  }).join('');
  const flowRows = (messages || []).filter(m => m.role === 'system' && (m.content || '').indexOf('🔄') === 0)
    .map(m => `<div style="padding:3px 0;border-bottom:1px dashed var(--b)">${m.content}</div>`).join('');
  if (!kCards && !flowRows) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML = `<h3 style="font-size:11px;font-weight:600;margin-bottom:4px;color:var(--a)">${I18N.t('events.outcome_title')}</h3>`
    + (kCards ? `<div style="margin-top:2px"><b>${I18N.t('events.outcome_knowledge')}</b><div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:4px">${kCards}</div></div>` : '')
    + (flowRows ? `<div style="margin-top:4px"><b>${I18N.t('events.outcome_flow')}</b>${flowRows}</div>` : '');
}

// Issue #100: 知识缓存
var _convKnowledge = {};

// Issue #100: 知识详情弹窗
function showKnowledgeDetail(scope) {
  var content = (_convKnowledge || {})[scope] || '';
  var scopeLabels = {project: I18N.t('events.scope_project'), issue: I18N.t('events.scope_issue'), issue_agent: I18N.t('events.scope_agent_solo')};
  _closeKnowledgeModal();
  var overlay = document.createElement('div');
  overlay.className = 'km-overlay show';
  overlay.id = 'kmOverlay';
  overlay.onclick = function(e) { if (e.target === overlay) _closeKnowledgeModal(); };
  var modal = document.createElement('div');
  modal.className = 'km-modal';
  var hdr = document.createElement('div');
  hdr.className = 'km-hdr';
  hdr.textContent = scopeLabels[scope] + I18N.t('events.outcome_knowledge');
  modal.appendChild(hdr);
  var body = document.createElement('div');
  body.className = 'km-body';
  body.innerHTML = renderMarkdown(content);
  modal.appendChild(body);
  var footer = document.createElement('div');
  footer.className = 'km-footer';
  var closeBtn = document.createElement('button');
  closeBtn.className = 'km-close';
  closeBtn.textContent = I18N.t('events.knowledge_modal_close');
  closeBtn.onclick = _closeKnowledgeModal;
  footer.appendChild(closeBtn);
  var delBtn = document.createElement('button');
  delBtn.className = 'km-del';
  delBtn.textContent = I18N.t('events.delete_knowledge_btn');
  delBtn.onclick = function() { _closeKnowledgeModal(); _deleteKnowledge(scope); };
  footer.appendChild(delBtn);
  modal.appendChild(footer);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
}

function _closeKnowledgeModal() {
  var el = document.getElementById('kmOverlay');
  if (el) el.remove();
}

// Issue #100: 快速删除知识（卡片 X 按钮）
function quickDeleteKnowledge(scope) {
  if (scope === 'project') {
    _showProjectDeleteConfirm(scope);
  } else {
    _deleteKnowledge(scope);
  }
}

// Issue #100: project 级二次确认
function _showProjectDeleteConfirm(scope) {
  _closeClearConvPopover();
  var overlay = document.createElement('div');
  overlay.className = 'chat-del-overlay';
  overlay.id = 'kmConfirmOverlay';
  overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
  var pop = document.createElement('div');
  pop.className = 'chat-del-pop';
  pop.style.left = (window.innerWidth / 2 - 110) + 'px';
  pop.style.top = (window.innerHeight / 2 - 70) + 'px';
  var title = document.createElement('div');
  title.className = 'chat-del-title';
  title.style.color = '#ff3b30';
  title.innerHTML = I18N.t('events.knowledge_delete_confirm_project');
  pop.appendChild(title);
  var btns = document.createElement('div');
  btns.className = 'chat-del-btns';
  var cancelBtn = document.createElement('button');
  cancelBtn.className = 'chat-del-cancel';
  cancelBtn.textContent = I18N.t('common.cancel');
  cancelBtn.onclick = function() { overlay.remove(); };
  btns.appendChild(cancelBtn);
  var okBtn = document.createElement('button');
  okBtn.className = 'chat-del-ok';
  okBtn.textContent = I18N.t('common.confirm');
  okBtn.onclick = function() { overlay.remove(); _deleteKnowledge(scope); };
  btns.appendChild(okBtn);
  pop.appendChild(btns);
  overlay.appendChild(pop);
  document.body.appendChild(overlay);
  requestAnimationFrame(function() { pop.classList.add('show'); });
}

// Issue #100: 删除知识（防重复点击）
var _delKnowBusy = false;
function _deleteKnowledge(scope) {
  if (_delKnowBusy) return;
  _delKnowBusy = true;
  API.del('/events/' + _convSeq + '/knowledge/' + scope).then(function(d) {
    _delKnowBusy = false;
    if (d.ok) {
      app.showToast(I18N.t('events.knowledge_deleted'));
      loadConversation();
    } else {
      app.showToast(I18N.t('events.knowledge_delete_failed', {msg: (d.detail || {}).message || ''}));
    }
  }).catch(function(e) {
    _delKnowBusy = false;
    if (e.name === 'AbortError' && e.silent) return;
    app.showToast(I18N.t('events.knowledge_delete_failed', {msg: e.message}));
  });
}

// Issue #100: 清理对话
function clearConversation(ev, seq) {
  _showClearConvPopover(ev, seq);
}

function _showClearConvPopover(ev, seq) {
  _closeClearConvPopover();
  var overlay = document.createElement('div');
  overlay.className = 'chat-del-overlay';
  overlay.id = 'clearConvOverlay';
  overlay.onclick = function(e) { if (e.target === overlay) _closeClearConvPopover(); };
  var pop = document.createElement('div');
  pop.className = 'chat-del-pop';
  pop.style.left = Math.min(ev.clientX, window.innerWidth - 240) + 'px';
  pop.style.top = Math.min(ev.clientY + 10, window.innerHeight - 140) + 'px';
  var title = document.createElement('div');
  title.className = 'chat-del-title';
  title.textContent = I18N.t('events.clear_conv_confirm');
  pop.appendChild(title);
  var btns = document.createElement('div');
  btns.className = 'chat-del-btns';
  var cancelBtn = document.createElement('button');
  cancelBtn.className = 'chat-del-cancel';
  cancelBtn.textContent = I18N.t('common.cancel');
  cancelBtn.onclick = _closeClearConvPopover;
  btns.appendChild(cancelBtn);
  var okBtn = document.createElement('button');
  okBtn.className = 'chat-del-ok';
  okBtn.textContent = I18N.t('common.confirm');
  okBtn.onclick = function() { _closeClearConvPopover(); _doClearConv(seq); };
  btns.appendChild(okBtn);
  pop.appendChild(btns);
  overlay.appendChild(pop);
  document.body.appendChild(overlay);
  requestAnimationFrame(function() { pop.classList.add('show'); });
}

function _closeClearConvPopover() {
  var el = document.getElementById('clearConvOverlay');
  if (el) el.remove();
}

// Issue #100: 防重复点击
var _clearBusy = false;

function _doClearConv(seq) {
  if (_clearBusy) return;
  _clearBusy = true;
  API.del('/events/' + seq + '/chat').then(function(d) {
    _clearBusy = false;
    if (d.ok) {
      app.showToast(I18N.t('events.clear_conv_done'));
      loadConversation();
    } else {
      app.showToast(I18N.t('events.clear_conv_failed', {msg: (d.detail || {}).message || ''}));
    }
  }).catch(function(e) {
    _clearBusy = false;
    if (e.name === 'AbortError' && e.silent) return;
    app.showToast(I18N.t('events.clear_conv_failed', {msg: e.message}));
  });
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
  input.style.height = '';
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
    const token = await API.ensureTokenValid() || '';
    const resp = await fetch('/api/events/chat/task/' + taskId + '/stream', {
      headers: {'Authorization': 'Bearer ' + token},
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
    API.get('/events/chat/task/' + task.task_id + '/status').then(st => {
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
    // Issue #100: 全屏时也显示清理对话按钮
    var btnsDiv = document.createElement('div');
    btnsDiv.style.cssText = 'display:flex;gap:6px;align-items:center';
    var clearBtn = document.createElement('button');
    clearBtn.textContent = I18N.t('events.clear_conv_btn');
    clearBtn.title = I18N.t('events.clear_conv_title');
    clearBtn.style.cssText = 'border:1px solid var(--b);background:transparent;color:#ff3b30;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;white-space:nowrap';
    clearBtn.onclick = function(ev) { clearConversation(ev, _convSeq); };
    btnsDiv.appendChild(clearBtn);
    btnsDiv.appendChild(btn);
    bar.appendChild(btnsDiv);
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

// ===== A2A 重新派发（a2a-redo-button-design.md weekly-06）=====
// completed 事件未实际交办下游 → 用户手动补发（方案B：弹窗自选目标 Agent）
function a2aRedoEvent(seq) {
  API.get('/events/' + seq + '/a2a-redo/scene').then(s => {
    _showA2aRedoGlass(seq, s || {});
  }).catch(() => {
    _showA2aRedoGlass(seq, {});
  });
}

function _showA2aRedoGlass(seq, sceneInfo) {
  _closeA2aRedoGlass();
  const _agents = ['vod', 'issue', 'coding', 'qa', 'sre'];
  const _names = { vod: 'VOD Agent', issue: 'Issue Agent', coding: 'Coding Agent', qa: 'QA Agent', sre: 'SRE Agent' };
  const _icons = { vod: '🔍', issue: '📝', coding: '🤖', qa: '✅', sre: '🔧' };
  const _inScene = sceneInfo.agents_in_scene || [];
  const _upstream = sceneInfo.upstream_agents || [];
  const _next = sceneInfo.next_agent || '';
  const _hasSceneInfo = _inScene.length > 0;
  window._a2aRedoSel = _next && _agents.includes(_next) ? _next : '';
  window._a2aRedoDoneAgents = _upstream;  // 供 _doA2aRedo 二次确认用
  const _btn = (a) => {
    const _ok = !_hasSceneInfo || _inScene.includes(a);
    const _sel = window._a2aRedoSel === a;
    const _done = _upstream.includes(a);  // 该 Issue 此前已 completed 过的 agent
    return `<div data-a2a-agent="${a}" data-ok="${_ok ? 1 : 0}" onclick="_pickA2aRedo('${a}')" style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:12px;cursor:${_ok ? 'pointer' : 'not-allowed'};border:1px solid ${_sel ? 'rgba(0,113,227,.45)' : 'rgba(0,0,0,.06)'};background:${_sel ? 'rgba(0,113,227,.08)' : _ok ? 'rgba(255,255,255,.55)' : 'rgba(0,0,0,.03)'};opacity:${_ok ? 1 : .4};transition:all .15s">
      <span style="font-size:15px">${_icons[a]}</span>
      <span style="font-size:12px;font-weight:600;color:var(--t,#1d1d1f)">${_names[a]}</span>
      ${!_ok ? '<span style="font-size:10px;color:var(--t3,#86868b);margin-left:auto">不在场景</span>' : ''}
      ${_ok && _done ? '<span style="font-size:10px;color:#c77700;margin-left:auto">⚠ 已执行过，补发将重跑</span>' : ''}
      ${_sel ? '<span style="font-size:11px;color:var(--a,#0071e3);margin-left:auto">已选</span>' : ''}
    </div>`;
  };
  const overlay = document.createElement('div');
  overlay.className = 'a2r-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.15);backdrop-filter:blur(6px) saturate(150%);-webkit-backdrop-filter:blur(6px) saturate(150%);animation:rdpFade .2s ease';
  const modal = document.createElement('div');
  modal.className = 'a2r-glass';
  modal.style.cssText = 'width:340px;background:rgba(255,255,255,0.72);backdrop-filter:blur(28px) saturate(200%);-webkit-backdrop-filter:blur(28px) saturate(200%);border-radius:18px;border:1px solid rgba(255,255,255,0.6);box-shadow:0 12px 40px rgba(0,0,0,0.12),0 2px 8px rgba(0,0,0,0.06);overflow:hidden;animation:rdpPop .25s cubic-bezier(0.34,1.56,0.64,1)';
  modal.innerHTML = `
    <div style="padding:18px 20px 14px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <div style="width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#0a84ff,#0071e3);display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0;box-shadow:0 4px 12px rgba(10,132,255,0.3)">🤝</div>
        <div>
          <div style="font-size:14px;font-weight:700;color:var(--t,#1d1d1f);letter-spacing:-0.2px">A2A 重新派发</div>
          <div style="font-size:11px;color:var(--t3,#86868b)">Seq #${seq}</div>
        </div>
      </div>
      <div style="font-size:12px;color:var(--t2,#6e6e73);line-height:1.6;margin-bottom:12px">
        该任务已完成但未交办下游 Agent。选择目标 Agent 补发一次 A2A 交办（不影响原任务结果）：
      </div>
      <div style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px">
        ${_agents.map(_btn).join('')}
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="bt bt-o bt-s" style="padding:8px 16px;font-size:12px" onclick="_closeA2aRedoGlass()">取消</button>
        <button class="bt bt-s" id="_a2aRedoConfirmBtn" style="padding:8px 16px;font-size:12px;background:linear-gradient(135deg,#0a84ff,#0071e3);color:#fff;border:none;box-shadow:0 4px 12px rgba(10,132,255,0.35);${window._a2aRedoSel ? '' : 'opacity:.5'}" onclick="_doA2aRedo(${seq})">补发</button>
      </div>
    </div>`;
  overlay.appendChild(modal);
  overlay.addEventListener('mousedown', function(e) {
    if (e.target === overlay) _closeA2aRedoGlass();
  });
  const _escHandler = function(e) {
    if (e.key === 'Escape') {
      _closeA2aRedoGlass();
      document.removeEventListener('keydown', _escHandler);
    }
  };
  document.addEventListener('keydown', _escHandler);
  document.body.appendChild(overlay);
}

function _pickA2aRedo(a) {
  const el = document.querySelector(`[data-a2a-agent="${a}"]`);
  if (!el || el.dataset.ok === '0') return;
  window._a2aRedoSel = a;
  document.querySelectorAll('[data-a2a-agent]').forEach(n => {
    const _sel = n.dataset.a2aAgent === a;
    n.style.borderColor = _sel ? 'rgba(0,113,227,.45)' : 'rgba(0,0,0,.06)';
    n.style.background = _sel ? 'rgba(0,113,227,.08)' : (n.dataset.ok === '1' ? 'rgba(255,255,255,.55)' : 'rgba(0,0,0,.03)');
  });
  const btn = document.getElementById('_a2aRedoConfirmBtn');
  if (btn) btn.style.opacity = '';
}

function _closeA2aRedoGlass() {
  const ex = document.querySelector('.a2r-overlay');
  if (ex) ex.remove();
  window._a2aRedoSel = '';
  window._a2aRedoDoneAgents = [];
}

function _doA2aRedo(seq) {
  const agent = window._a2aRedoSel || '';
  if (!agent) { app.showToast('请先选择目标 Agent'); return; }
  // 防手误二次确认（2026-10-12）：目标 Agent 已在此前流程 completed 过 → 重跑确认
  const _doneAgents = window._a2aRedoDoneAgents || [];
  if (_doneAgents.includes(agent)) {
    if (!window.confirm('该 Agent 已在此前流程中执行完成。\n补发将重新执行整个阶段（消耗资源与时间），且不会影响已有结果。\n确定继续吗？')) return;
  }
  _closeA2aRedoGlass();
  API.post('/events/' + seq + '/a2a-redo', { agent: agent }).then(d => {
    if (d && d.skipped) {
      const _reasons = { not_in_scene: '目标 Agent 不在当前场景内', duplicate_a2a: '该 Agent 已有进行中的任务', sre_subscribed: 'SRE 已订阅，无需补发', task_cancelled: '任务已取消' };
      app.showToast('A2A 补发未执行：' + (_reasons[d.skipped] || d.skipped));
    } else if (d && d.seq) {
      app.showToast('A2A 补发成功，新事件 #' + d.seq);
      app.nav('event', d.seq);
    } else {
      app.showToast('A2A 补发失败：' + ((d && d.error) || (d && d.message) || '未知原因'));
    }
  }).catch(e => {
    const msg = (e.detail && e.detail.message) || e.message || e;
    app.showToast('A2A 补发失败：' + msg);
  });
}

// ===== 本机重试（Issue #128）=====
function localRetryEvent(seq) {
  _showLocalRetryGlass(seq);
}

function _showLocalRetryGlass(seq) {
  _closeLocalRetryGlass();
  const overlay = document.createElement('div');
  overlay.className = 'lr-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.15);backdrop-filter:blur(6px) saturate(150%);-webkit-backdrop-filter:blur(6px) saturate(150%);animation:rdpFade .2s ease';
  const modal = document.createElement('div');
  modal.className = 'lr-glass';
  modal.style.cssText = 'width:340px;background:rgba(255,255,255,0.72);backdrop-filter:blur(28px) saturate(200%);-webkit-backdrop-filter:blur(28px) saturate(200%);border-radius:18px;border:1px solid rgba(255,255,255,0.6);box-shadow:0 12px 40px rgba(0,0,0,0.12),0 2px 8px rgba(0,0,0,0.06);overflow:hidden;animation:rdpPop .25s cubic-bezier(0.34,1.56,0.64,1)';
  modal.innerHTML = `
    <div style="padding:18px 20px 14px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <div style="width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,#34c759,#30d158);display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0;box-shadow:0 4px 12px rgba(52,199,89,0.3)">🖥</div>
        <div>
          <div style="font-size:14px;font-weight:700;color:var(--t,#1d1d1f);letter-spacing:-0.2px">${I18N.t('events.local_retry_title')}</div>
          <div style="font-size:11px;color:var(--t3,#86868b)">Seq #${seq}</div>
        </div>
      </div>
      <div style="font-size:12px;color:var(--t2,#6e6e73);line-height:1.6;margin-bottom:14px">
        ${I18N.t('events.local_retry_desc')}
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="bt bt-o bt-s" style="padding:8px 16px;font-size:12px" onclick="_closeLocalRetryGlass()">${I18N.t('events.local_retry_cancel')}</button>
        <button class="bt bt-s" style="padding:8px 16px;font-size:12px;background:linear-gradient(135deg,#34c759,#30d158);color:#fff;border:none;box-shadow:0 4px 12px rgba(52,199,89,0.35)" onclick="_doLocalRetry(${seq})">${I18N.t('events.local_retry_confirm')}</button>
      </div>
    </div>`;
  overlay.appendChild(modal);
  overlay.addEventListener('mousedown', function(e) {
    if (e.target === overlay) _closeLocalRetryGlass();
  });
  const _escHandler = function(e) {
    if (e.key === 'Escape') {
      _closeLocalRetryGlass();
      document.removeEventListener('keydown', _escHandler);
    }
  };
  document.addEventListener('keydown', _escHandler);
  document.body.appendChild(overlay);
}

function _closeLocalRetryGlass() {
  const ex = document.querySelector('.lr-overlay');
  if (ex) ex.remove();
}

function _doLocalRetry(seq) {
  _closeLocalRetryGlass();
  API.post('/events/' + seq + '/redispatch?local_only=true').then(d => {
    app.showToast(I18N.t('events.local_retry_toast_success'));
    app.nav('event', seq);
  }).catch(e => {
    const msg = (e.detail && e.detail.message) || e.message || e;
    app.showToast(I18N.t('events.local_retry_toast_failed', {msg: msg}));
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
  API.get('/task-logs/' + encodeURIComponent(taskId) + '/info')
    .then(info => {
      if (!info) return;
      const el = document.getElementById('tlInfo');
      if (!el) return;
      const bits = [];
      if (info.agent_name) bits.push('🤖 ' + _tlEsc(info.agent_name) + (info.agent_version ? ' <span style="color:var(--t3)">v' + _tlEsc(info.agent_version) + '</span>' : ''));
      if (info.device_name) bits.push('🖥 ' + _tlEsc(info.device_name));
      if (info.last_ip) bits.push('🌐 ' + _tlEsc(info.last_ip));
      if (info.owner_name) bits.push('👤 ' + _tlEsc(info.owner_name));
      if (info.spec) bits.push('⚙️ ' + _tlEsc(info.spec));
      if (info.cpu_percent) bits.push('📊 CPU ' + Number(info.cpu_percent).toFixed(1) + '%');
      if (info.memory_percent) bits.push('💾 MEM ' + Number(info.memory_percent).toFixed(1) + '%');
      el.innerHTML = bits.map(b => '<span>' + b + '</span>').join('');
    })
    .catch(e => { if (e.name === 'AbortError' && e.silent) return; });
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

async function _taskLogStream() {
  const token = await API.ensureTokenValid() || '';
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
  API.get('/runtime/tasks/' + encodeURIComponent(_taskLog.taskId) + '/status').then(d => {
    if (!d || !d.status) return;
    if (d.status === 'running' || d.status === 'dispatched') {
      const btn = document.getElementById('tlKillBtn');
      if (btn) btn.style.display = '';
    } else {
      _tlHideKillBtn();
    }
  }).catch(e => { if (e.name === 'AbortError' && e.silent) return; });
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
