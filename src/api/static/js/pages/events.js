function renderEventDetail(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const p = data.payload || {};
  const agent = data.agent_type || 'vod';
  const logs = data.agent_logs || [];
  const agentName = (_AGENT_TYPES[agent] && _AGENT_TYPES[agent].display_name) || agent;
  const logHtml = logs.length > 0 ? logs.map(l =>
    `<div style="${l.type === 'error' ? 'color:var(--r)' : l.type === 'done' ? 'color:var(--g)' : ''}">[${(l.time || '').substring(11,19) || '--:--'}] ${l.type}: ${l.msg}</div>`
  ).join('') : `<div>[${(data.created_at || '').substring(11,16) || '--:--'}] Agent started</div>
    <div>[${(data.created_at || '').substring(11,16) || '--:--'}] Processing ${data.event_type} event</div>
    <div style="color:var(--g)">[${(data.created_at || '').substring(11,16) || '--:--'}] ${data.status === 'completed' ? 'Completed' : data.status === 'failed' ? 'Failed' : data.status}</div>`;

  let html = `<button class="bt bt-o bt-s" onclick="app.nav('${agent}')" style="margin-bottom:12px">← 返回 ${agentName}</button>
  <div style="display:grid;grid-template-columns:1fr 300px;gap:14px;height:calc(100vh - 160px);overflow:hidden">
    <div class="cd" style="padding:16px 20px;display:flex;flex-direction:column;overflow:hidden">
      <h3 style="font-size:13px;font-weight:600;margin-bottom:8px;flex-shrink:0">Event Context</h3>
      <div class="tx t2" style="margin-bottom:12px;line-height:1.6;flex-shrink:0">
        <div>Trigger: ${data.event_type || '—'}</div>
        <div>Repository: ${data.repo_key || ''}</div>
        <div>Issue: ${data.issue_id || '—'}</div>
        <div>Actor: ${data.actor || '—'}</div>
        <div>Summary: ${data.summary || data.title || '—'}</div>
        <div>Time: ${data.created_at || ''}</div>
      </div>
      <h3 style="font-size:13px;font-weight:600;margin-bottom:8px;flex-shrink:0">Agent Execution Log</h3>
      <div style="flex:1;overflow-y:auto;background:var(--s);color:#c2cad6;border-radius:8px;padding:12px;font-size:11px;font-family:monospace;line-height:1.7">
        ${logHtml}
      </div>
      <div class="cd mt12" style="padding:10px;background:#eef2ff;font-size:11px;flex-shrink:0">
        <div style="color:var(--a);font-weight:600;margin-bottom:4px">💡 Have a question about these logs?</div>
        <div style="color:var(--t2)">Use the Feedback form on the right to submit your feedback.</div>
      </div>
    </div>
    <div style="display:flex;flex-direction:column;gap:12px;overflow-y:auto">
      <div class="cd" style="padding:16px">
        <h3 style="font-size:12px;font-weight:600;margin-bottom:10px">Event Info</h3>
        <div style="font-size:11px;color:var(--t2);line-height:1.8">
          <div><span style="color:var(--t3)">Backend:</span> <b>${data.backend || 'OpenCode'}</b></div>
          <div><span style="color:var(--t3)">Skill:</span> ${data.skill || agent}</div>
          <div><span style="color:var(--t3)">Issue:</span> ${data.issue_id || '—'}</div>
          <div><span style="color:var(--t3)">Actor:</span> @${data.actor || '—'}</div>
          <div><span style="color:var(--t3)">Agent:</span> ${agent}</div>
          <div><span style="color:var(--t3)">Duration:</span> ${data.duration ? data.duration + 's' : '—'}</div>
          <div><span style="color:var(--t3)">Status:</span> <span class="${data.status === 'completed' ? 'tgx' : data.status === 'failed' ? 'tr' : 'ty'}">${data.status === 'completed' ? '✅ Success' : data.status === 'failed' ? '❌ Failed' : '⏳ ' + data.status}</span></div>
        </div>
      </div>
      <div class="cd" style="padding:16px">
        <h3 style="font-size:12px;font-weight:600;margin-bottom:10px">💬 Feedback</h3>
        <div class="tx t2" style="margin-bottom:6px">Noticed an issue with this event?</div>
        <input type="text" placeholder="Issue title..." id="fbTitle" style="width:100%;padding:6px 8px;margin-bottom:6px;border:1px solid var(--b);border-radius:6px;font-size:11px;background:var(--bg)">
        <textarea placeholder="Describe the problem or suggestion..." id="fbBody" style="width:100%;height:60px;padding:6px 8px;margin-bottom:8px;border:1px solid var(--b);border-radius:6px;font-size:11px;background:var(--bg);resize:vertical"></textarea>
        <button class="bt bt-p bt-s" style="width:100%" onclick="submitFeedback(${data.seq})">Submit Feedback</button>
      </div>
    </div>
  </div>`;

  el.innerHTML = html;
}

function submitFeedback(seq) {
  const title = document.getElementById('fbTitle');
  const body = document.getElementById('fbBody');
  API.post('/chat/feedback', {seq, feedback_type: 'create_issue', comment: (title ? title.value : '') + '\n' + (body ? body.value : '')}).then(r => {
    app.showToast('反馈已提交');
    if (title) title.value = '';
    if (body) body.value = '';
  }).catch(e => app.showToast('提交失败: ' + e.message));
}
