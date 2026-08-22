// Event Detail：结构化产出 + Event Info + 与事件所属 agent 的对话（event-conversation-design.md）
let _convSeq = null;
let _convAgent = '';

function navEvent(seq) {
  if (!seq || seq < 1) return;
  // 局部刷新：直接拿数据调 renderEventDetail，不走路由避免整体闪抖
  API.get('/events/detail/' + seq).then(function(data) {
    // 仅更新标题栏 seq，不重置 pageContent
    var titleEl = document.getElementById('pageTitle');
    if (titleEl) titleEl.textContent = 'Event Detail #' + seq;
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
  const el = document.getElementById('pageContent');
  if (!el) return;
  const agent = data.agent_type || 'vod';
  const agentName = (_AGENT_TYPES[agent] && _AGENT_TYPES[agent].display_name) || agent;
  const result = data.result || data.summary || '';
  _convSeq = data.seq;
  _convAgent = agent;

  // Issue 编号 → GitCode 超链接（repo_key 已去前缀为 owner/repo，issue_id 为 #N）
  const _issNum = String(data.issue_id || '').replace(/^#/, '');
  const _repoParts = (data.repo_key || '').split('/');
  const _issueLink = (_issNum && _repoParts.length === 2)
    ? `<a href="https://gitcode.com/${_repoParts[0]}/${_repoParts[1]}/issues/${_issNum}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">Issue ${data.issue_id}</a>`
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
    const actions = (o.actions || []).map(a => `<div>• ${a}</div>`).join('');
    const arts = (o.artifacts || []).map(a => `<span class="tgx" style="margin-right:6px">${a}</span>`).join('');
    structuredHtml = `<div style="margin-bottom:8px;padding:10px 12px;background:#f0f1f4;border-radius:6px;border-left:3px solid var(--a)">
      ${o.conclusion ? `<div style="font-size:12px;margin-bottom:3px"><b>结论:</b> ${o.conclusion}</div>` : ''}
      ${actions ? `<div style="font-size:12px;margin-bottom:3px"><b>动作:</b> ${actions}</div>` : ''}
      ${arts ? `<div style="font-size:12px;margin-bottom:3px"><b>产出:</b> ${arts}</div>` : ''}
      ${o.next_step ? `<div style="font-size:12px;margin-bottom:3px"><b>下一步:</b> ${o.next_step}</div>` : ''}
      ${o.issue_summary ? `<div style="font-size:12px;color:var(--a)"><b>Issue 摘要:</b> ${o.issue_summary}</div>` : ''}
    </div>`;
  }

  const html = `<button class="bt bt-o bt-s" onclick="app.nav('${agent}')" style="margin-bottom:12px">← ${agentName}</button>
  <div style="display:grid;grid-template-columns:1fr 300px;gap:14px;height:calc(100vh - 160px);overflow:hidden">
    <!-- 左栏大框：导航 + Event Context + Event Info + 处理记录+答复结论 -->
    <div class="cd" style="padding:16px 20px;display:flex;flex-direction:column;overflow:hidden">
      <!-- 顶部导航栏 -->
      <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--b)">
        <button class="bt bt-o bt-s" onclick="navEvent(${data.prev_seq || 0})" title="上一个 ${agentName} 事件" ${!data.prev_seq ? 'disabled style="opacity:.4;cursor:not-allowed"' : ''}>‹</button>
        <span class="ts t3" style="font-size:12px;font-weight:600">Event #${data.seq || '—'}</span>
        <button class="bt bt-o bt-s" onclick="navEvent(${data.next_seq || 0})" title="下一个 ${agentName} 事件" ${!data.next_seq ? 'disabled style="opacity:.4;cursor:not-allowed"' : ''}>›</button>
      </div>
      <!-- 上半：Event Context + Event Info 左右并列 -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;flex-shrink:0;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--b)">
        <div>
          <h3 style="font-size:12px;font-weight:600;margin-bottom:6px">Event Context</h3>
          <div class="tx t2" style="line-height:1.6">
            <div>Trigger: ${data.event_type || '—'}</div>
            <div>Repository: ${data.repo_key || ''}</div>
            <div>Issue: ${_issueLink}</div>
            <div>Actor: ${data.actor || '—'}</div>
            <div>Time: ${fmtLocalTime(data.created_at) || ''}</div>
          </div>
        </div>
        <div>
          <h3 style="font-size:12px;font-weight:600;margin-bottom:6px">Event Info</h3>
          <div style="font-size:11px;color:var(--t2);line-height:1.8">
            <div><span style="color:var(--t3)">Seq:</span> <b>#${data.seq || '—'}</b></div>
            <div><span style="color:var(--t3)">Engine:</span> <b>${data.runtime || data.backend || 'OpenCode'}</b>${data.engine_device ? `<span class="ts t3"> · ${data.engine_device}</span>` : ''}${data.engine_ip ? `<span class="ts t3"> · ${data.engine_ip}</span>` : ''}</div>
            <div><span style="color:var(--t3)">Skill:</span> ${data.skill || agent}</div>
            <div><span style="color:var(--t3)">Duration:</span> ${data.duration ? data.duration + 's' : '—'}</div>
            <div><span style="color:var(--t3)">Status:</span> <span class="${data.status === 'completed' ? 'tgx' : data.status === 'failed' ? 'tr' : 'ty'}">${data.status === 'completed' ? '✅ Success' : data.status === 'failed' ? '❌ Failed' : '⏳ ' + data.status}</span>${_redispatchVisible ? ` <button class="bt bt-o bt-s" style="margin-left:8px" onclick="redispatchEvent(${data.seq})">🔄 重新派发</button>` : ''}${data.status === 'running' ? ` <button class="bt bt-o bt-s" style="margin-left:8px;color:var(--tr, #e74c3c);border-color:var(--tr, #e74c3c)" onclick="terminateEventTask(${data.seq}, '${(data.task && data.task.task_id) || ''}')" title="强行终止正在运行的 Agent">⏹ 终止</button>` : ''}</div>
            ${data.task && data.task.task_id
              ? `<div style="margin-top:4px"><a href="javascript:void(0)" onclick="openTaskLog('${data.task.task_id}','seq#${data.seq}')" style="color:var(--a);text-decoration:underline">📜 查看实时日志</a></div>`
              : `<div style="margin-top:4px"><a href="javascript:void(0)" onclick="openInlineLog(${data.seq}, 'seq#${data.seq}')" style="color:var(--a);text-decoration:underline">📜 查看日志</a></div>`}
            <div id="convGate" style="display:none"></div>
          </div>
        </div>
      </div>
      <!-- 下：Agent 处理记录 + 答复结论 合并 -->
      <h3 style="font-size:13px;font-weight:600;margin-bottom:6px;flex-shrink:0">Agent 处理记录与答复结论</h3>
      <div style="flex:1;overflow-y:auto;background:var(--s);color:#000;border-radius:8px;padding:14px;font-size:13px;line-height:1.6">
        ${structuredHtml}
        ${result ? renderMarkdown(result) : '<div style="color:var(--t3)">（无结论）</div>'}
      </div>
      <div id="convOutcome" class="cd mt12" style="padding:10px;font-size:11px;flex-shrink:0;display:none"></div>
    </div>
    <!-- 右栏：对话面板 -->
    <div id="convPanel" class="cd" style="padding:14px;display:flex;flex-direction:column;gap:8px;height:100%;overflow:hidden">
      <h3 style="font-size:12px;font-weight:600;flex-shrink:0;display:flex;justify-content:space-between;align-items:center">💬 与 ${agentName} 对话 <button class="bt bt-o bt-s" onclick="toggleConvFullscreen()" title="全屏/还原" style="padding:2px 10px">⛶ 全屏</button></h3>
      <div id="convMessages" style="flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:8px;font-size:12px"></div>
      <div id="convInputRow" style="display:none;gap:6px;flex-shrink:0">
        <input id="convInput" placeholder="问一问 agent…（Enter 发送）" style="flex:1;padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)" onkeydown="if(event.key==='Enter'){event.preventDefault();sendConversation();}">
        <button class="bt bt-p bt-s" onclick="sendConversation()">发送</button>
      </div>
    </div>
  </div>`;

  el.innerHTML = html;
  loadConversation();
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
      <div style="font-size:10px;opacity:.7;margin-top:3px">${mine ? (by || '我') : (by || 'agent')}${time ? ' · ' + fmtLocalTime(time).substring(11,16) : ''}</div>
    </div>
  </div>`;
}

function loadConversation() {
  if (!_convSeq) return;
  API.get('/events/' + _convSeq + '/thread').then(d => {
    const gate = d.gate || {};
    const gateEl = document.getElementById('convGate');
    if (gateEl) gateEl.innerHTML = gate.open
      ? `<span class="tgx">✔ 评论开放中</span>`
      : `<span class="tr">✖ 评论已关闭</span>`;
    const messagesEl = document.getElementById('convMessages');
    if (messagesEl) {
      const msgs = (d.messages || []).map(m => _convBubble(m.role, m.content, m.created_at, m.created_by)).join('');
      messagesEl.innerHTML = msgs || '<div style="color:var(--t3);font-size:11px">开始与 ' + _convAgent + ' 对话，了解这次处理。</div>';
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    const inputRow = document.getElementById('convInputRow');
    if (inputRow) inputRow.style.display = gate.open ? 'flex' : 'none';
    // 左栏：对话产出（知识沉淀 + 流程操作 + 结论）
    renderOutcome(d.knowledge || {}, d.messages || []);
  }).catch(e => {
    const messagesEl = document.getElementById('convMessages');
    if (messagesEl) messagesEl.innerHTML = '<div class="tr" style="font-size:11px">加载对话失败: ' + e.message + '</div>';
  });
}

function renderOutcome(knowledge, messages) {
  const el = document.getElementById('convOutcome');
  if (!el) return;
  const kRows = Object.keys(knowledge || {}).map(scope =>
    `<div style="padding:3px 0;border-bottom:1px dashed var(--b)"><b>${scope === 'project' ? '仓库级' : scope === 'issue' ? 'Issue 级' : 'Agent 级'}</b> · ${knowledge[scope]}</div>`
  ).join('');
  const flowRows = (messages || []).filter(m => m.role === 'system' && (m.content || '').indexOf('🔄') === 0)
    .map(m => `<div style="padding:3px 0;border-bottom:1px dashed var(--b)">${m.content}</div>`).join('');
  if (!kRows && !flowRows) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML = `<h3 style="font-size:11px;font-weight:600;margin-bottom:4px;color:var(--a)">💬 对话产出</h3>`
    + (kRows ? `<div style="margin-top:2px"><b>知识沉淀</b>${kRows}</div>` : '')
    + (flowRows ? `<div style="margin-top:4px"><b>流程操作</b>${flowRows}</div>` : '');
}

function sendConversation() {
  const input = document.getElementById('convInput');
  if (!input || !_convSeq) return;
  const msg = (input.value || '').trim();
  if (!msg) return;
  input.value = '';
  const messagesEl = document.getElementById('convMessages');
  if (messagesEl) messagesEl.insertAdjacentHTML('beforeend', _convBubble('user', msg));
  const token = localStorage.getItem('token') || '';
  const replyId = 'convReply' + Date.now();
  const holder = document.createElement('div');
  holder.style.display = 'flex';
  holder.style.justifyContent = 'flex-start';
  holder.innerHTML = `<div id="${replyId}" style="max-width:85%;padding:8px 10px;border-radius:8px;font-size:12px;line-height:1.5;white-space:pre-wrap;background:var(--bg);color:var(--t);border:1px solid var(--b)">⏳ 思考中…</div>`;
  if (messagesEl) messagesEl.appendChild(holder);
  const replyEl = document.getElementById(replyId);
  if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
  let acc = '';
  fetch('/api/events/' + _convSeq + '/chat/stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    body: JSON.stringify({message: msg}),
  }).then(resp => {
    if (!resp.ok) {
      resp.json().then(d => app.showToast(((d.detail && d.detail.message) || '发送失败'))).catch(() => app.showToast('发送失败'));
      if (replyEl) replyEl.textContent = '（发送失败）';
      return;
    }
    if (!resp.body) return loadConversation();
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    function read() {
      return reader.read().then(({done, value}) => {
        if (done) { loadConversation(); return; }
        buf += dec.decode(value, {stream: true});
        const parts = buf.split('\n\n');
        buf = parts.pop();
        for (const part of parts) {
          if (!part.startsWith('data: ')) continue;
          let evt;
          try { evt = JSON.parse(part.slice(6)); } catch (e) { continue; }
          if (evt.type === 'text') { acc += evt.text; if (replyEl) replyEl.innerHTML = renderMarkdown(acc); }
          else if (evt.type === 'done') { if (replyEl) replyEl.innerHTML = renderMarkdown(evt.reply || acc); loadConversation(); return; }
        }
        if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
        return read();
      });
    }
    return read().catch(() => loadConversation());
  }).catch(() => loadConversation());
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
    title.textContent = '💬 与 ' + agentName + ' 对话';
    const btn = document.createElement('button');
    btn.className = 'bt bt-o bt-s';
    btn.textContent = '⛶ 还原';
    btn.title = '退出全屏';
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
// ESC 键退出全屏
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && _convFs) toggleConvFullscreen();
});

function redispatchEvent(seq) {
  if (!confirm('确认重新派发该事件任务？\n将把卡住的任务重新入队执行，可能产生重复执行。')) return;
  API.post('/events/' + seq + '/redispatch').then(d => {
    app.showToast('已重新派发，等待 worker 重新执行');
    app.nav('event', seq);  // 刷新详情
  }).catch(e => {
    const msg = (e.detail && e.detail.message) || e.message || e;
    app.showToast('重派失败: ' + msg);
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
  const time = payload.created_at ? `<span style="color:var(--t3);margin-right:6px">${fmtLocalTime(payload.created_at).substring(11, 19)}</span>` : '';
  if (type === 'tool_use') {
    let inputHtml = '';
    try {
      inputHtml = `<div style="margin:4px 0 4px 24px;white-space:pre-wrap;word-break:break-all;color:var(--t2);font-size:11px">${_tlEsc(JSON.stringify(payload.input || {}, null, 2))}</div>`;
    } catch (e) {}
    return `<div style="margin-bottom:6px">${time}🔧 <span style="color:var(--a);font-weight:600">${_tlEsc(payload.tool || 'tool')}</span>${inputHtml}</div>`;
  }
  const color = type === 'error' ? 'var(--tr)' : type === 'reasoning' ? 'var(--t3)' : 'var(--t2)';
  return `<div style="margin-bottom:6px">${time}${_tlIcon(type)} <span style="color:${color};white-space:pre-wrap;word-break:break-all">${_tlEsc(payload.content || '')}</span></div>`;
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
      <span style="font-size:13px;font-weight:600">📜 运行日志 <span style="color:var(--t3);font-weight:400">· ${_tlEsc(title)}</span></span>
      <span id="tlStatus" style="font-size:11px;margin-left:auto"></span>
      <button onclick="_copyTaskLog()" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer" title="复制日志内容">📋 复制</button>
      <button id="tlKillBtn" onclick="terminateCurrentTask()" style="border:1px solid var(--tr,#e74c3c);background:transparent;color:var(--tr,#e74c3c);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;display:none" title="强行终止正在运行的任务">⏹ 终止</button>
      <button id="taskLogFullscreenBtn" onclick="_toggleTaskLogFullscreen()" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer" title="全屏/还原">⛶ 全屏</button>
      <button onclick="closeTaskLog()" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer">✕ 关闭</button>
    </div>
    <div id="tlInfo" style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:7px 14px;border-bottom:1px solid var(--b,#2a3140);flex-shrink:0;font-size:11px;color:var(--t2,#8a94a6)"></div>
    <div id="tlBody" style="flex:1;min-height:0;overflow-y:auto;background:#0d1220;color:#d3dcee;padding:12px 14px;font-size:12px;font-family:monospace;line-height:1.6"></div>
  </div>`;
  document.body.appendChild(ov);
  _taskLog.statusEl = document.getElementById('tlStatus');
  _taskLog.bodyEl = document.getElementById('tlBody');
  _tlStatus('⏳ 连接中…');
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
        '<div class="tl-waiting" style="color:var(--t3);padding:10px 0;font-size:12px">⏳ 引擎执行中，等待输出…</div>');
      _tlStatus('⏳ 引擎执行中，等待输出…', 'var(--ty, #f1c40f)');
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
    _tlStatus('🟢 已连接（实时）', 'var(--tgx, #2ecc71)');
    _taskLog.hasMsg = false;
    _tlCheckTaskStatus();
    _tlArmWaitHint();
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    function read() {
      return reader.read().then(({done, value}) => {
        if (done) {
          _tlStatus('🔚 日志流结束', 'var(--t3)');
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
            _tlStatus('🟢 已连接（实时）', 'var(--tgx, #2ecc71)');
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
    _tlStatus('⚠️ 连接断开，2s 后重连…', 'var(--tr, #e74c3c)');
    setTimeout(() => { if (_taskLog.taskId) _taskLogStream(); }, 2000);
  });
}

function _copyTaskLog() {
  if (!_taskLog.bodyEl) return;
  const txt = _taskLog.bodyEl.innerText;
  navigator.clipboard.writeText(txt).then(
    () => { const b = _taskLog.copyBtn; if (b) { const o = b.textContent; b.textContent = '✅ 已复制'; b.style.background = 'var(--ok,#1e7f3f)'; b.style.color = '#fff'; setTimeout(() => { b.textContent = o; b.style.background = ''; b.style.color = ''; }, 1200); } },
    () => {}
  );
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
    if (b) { b.textContent = '⛶ 还原'; b.title = '退出全屏'; }
  } else {
    box.dataset.fs = '';
    box.style.width = 'min(960px,94vw)';
    box.style.height = 'min(720px,90vh)';
    box.style.borderRadius = '10px';
    const b = document.getElementById('taskLogFullscreenBtn');
    if (b) { b.textContent = '⛶ 全屏'; b.title = '全屏/还原'; }
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
      _tlStatus('⏹ 正在终止…', 'var(--tr, #e74c3c)');
      _tlHideKillBtn();
    });
  } else {
    _doTerminateTask(_taskLog.taskId, function() {
      _tlStatus('⏹ 正在终止…', 'var(--tr, #e74c3c)');
      _tlHideKillBtn();
    });
  }
}

function _doTerminateTask(taskId, onSuccess) {
  API.post('/runtime/tasks/' + encodeURIComponent(taskId) + '/cancel').then(function(d) {
    if (onSuccess) onSuccess();
  }).catch(function(e) {
    app.showToast('终止失败: ' + (e.message || e));
  });
}

function _doTerminateEvent(seq, taskId, onSuccess) {
  // 优先走 event terminate（同时支持 direct 和 worker 模式）
  API.post('/events/' + seq + '/terminate').then(function(d) {
    if (onSuccess) onSuccess();
    else {
      app.showToast('⏹ 任务终止请求已发送');
      API.get('/events/detail/' + seq).then(renderEventDetail);
    }
  }).catch(function(e) {
    // fallback: 如果有 task_id 尝试直接 cancel
    if (taskId) {
      _doTerminateTask(taskId, function() {
        if (onSuccess) onSuccess();
        else {
          app.showToast('⏹ 任务终止请求已发送');
          API.get('/events/detail/' + seq).then(renderEventDetail);
        }
      });
    } else {
      app.showToast('终止失败: ' + (e.message || e));
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
        var time = l.time ? '<span style="color:var(--t3);margin-right:6px">' + (l.time.substring(11,19) || '') + '</span>' : '';
        var msg = l.msg || '';
        if (type === 'tool_use') {
          var toolName = msg;
          var inputHtml = '';
          try {
            var parsed = JSON.parse(msg);
            if (parsed.tool) toolName = _tlEsc(parsed.tool);
            if (parsed.input) inputHtml = '<div style="margin:4px 0 4px 24px;white-space:pre-wrap;word-break:break-all;color:var(--t2);font-size:11px">' + _tlEsc(JSON.stringify(parsed.input, null, 2)) + '</div>';
          } catch(e) {}
          return '<div style="margin-bottom:6px">' + time + '🔧 <span style="color:var(--a);font-weight:600">' + toolName + '</span>' + inputHtml + '</div>';
        }
        var icon = type === 'error' ? '❌' : type === 'reasoning' ? '🤔' : type === 'step_start' ? '▶' : type === 'text' ? '💬' : '·';
        var color = type === 'error' ? 'var(--tr)' : type === 'reasoning' ? 'var(--t3)' : 'var(--t2)';
        return '<div style="margin-bottom:6px">' + time + icon + ' <span style="color:' + color + ';white-space:pre-wrap;word-break:break-all">' + _tlEsc(msg) + '</span></div>';
      }).join('');
    } else {
      var resultText = data.result || '(无执行日志 — agent 未实际运行)';
      bodyHtml = '<div style="margin-bottom:6px">❌ <span style="color:var(--tr);white-space:pre-wrap;word-break:break-all">' + _tlEsc(resultText) + '</span></div>';
    }
    var ov = document.createElement('div');
    ov.id = 'taskLogOverlay';
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    ov.innerHTML = '<div style="width:min(960px,94vw);height:min(720px,90vh);background:var(--bg,#11151c);border:1px solid var(--b,#2a3140);border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,.5)">'
      + '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--b,#2a3140);flex-shrink:0">'
      + '<span style="font-size:13px;font-weight:600">📜 执行日志 <span style="color:var(--t3);font-weight:400">· ' + _tlEsc(title) + '</span></span>'
      + '<span style="font-size:11px;margin-left:auto;color:var(--t3)">' + logs.length + ' 条记录</span>'
      + '<button onclick="closeTaskLog()" style="border:1px solid var(--b,#2a3140);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer">✕ 关闭</button>'
      + '</div>'
      + '<div style="flex:1;min-height:0;overflow-y:auto;background:#0b0e14;color:#c2cad6;padding:12px 14px;font-size:12px;font-family:monospace;line-height:1.6">' + bodyHtml + '</div>'
      + '</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function(e) { if (e.target === ov) closeTaskLog(); });
  }).catch(function(e) {
    app.showToast('加载日志失败: ' + e.message);
  });
}
