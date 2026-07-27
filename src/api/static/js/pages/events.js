function renderEventDetail(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const p = data.payload || {};

  let html = `<div class="cd"><div class="cd-h"><h2>Event #${data.seq}</h2>
  <span class="tg ${data.status === 'failed' ? 'tg-v' : data.status === 'completed' ? 'tg-ch' : 'tg-d'}">${data.status}</span></div>
  <div class="srow"><label>Type</label><span class="ts">${data.event_type}</span></div>
  <div class="srow"><label>Repo</label><span class="ts">${data.repo_key || ''}</span></div>
  <div class="srow"><label>Issue</label><span class="ts">${data.issue_id || '—'}</span></div>
  <div class="srow"><label>Agent</label><span class="ts">${data.agent_type || '—'}</span></div>
  <div class="srow"><label>Time</label><span class="ts">${data.created_at || ''}</span></div>
  ${data.title ? `<div class="srow"><label>Title</label><span class="ts">${data.title}</span></div>` : ''}
  ${data.body ? `<div class="srow"><label>Body</label><span class="ts">${data.body.substring(0,500)}</span></div>` : ''}
  </div>`;

  html += `<div class="cd"><div class="cd-h"><h2>Payload</h2></div>
  <pre style="font-size:11px;line-height:1.5;overflow-x:auto;max-height:400px;background:var(--bg);padding:12px;border-radius:8px">${JSON.stringify(p, null, 2).substring(0,3000)}</pre></div>`;

  html += `<div class="cd"><div class="cd-h"><h2>Feedback</h2></div>
  <div class="srow"><label>Comment</label><input id="feedbackComment" placeholder="描述您的反馈..."></div>
  <button class="bt bt-p" onclick="submitFeedback(${data.seq})">提交反馈给 VOD Agent</button></div>`;

  el.innerHTML = html;
}

function submitFeedback(seq) {
  const comment = document.getElementById('feedbackComment').value;
  API.post('/chat/feedback', {seq, feedback_type: 'create_issue', comment}).then(r => {
    app.showToast('反馈已提交');
  }).catch(e => app.showToast('提交失败: ' + e.message));
}
