// ====== 任务中心（Account ▸ 任务中心）======
let _tasksTab = 'active';

function renderTasks() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中...</div>';
  loadTasks();
}

function loadTasks() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const repo = app.currentRepo();
  const repoKey = repo ? repo.fullName : '';
  API.get('/tasks/mine?status=' + _tasksTab + '&repo_key=' + encodeURIComponent(repoKey)).then(d => {
    const tasks = d.tasks || [];
    const tab = t => `<button class="pttab ${_tasksTab === t ? 'pttab-act' : ''}" onclick="_tasksTab='${t}';loadTasks()">${t === 'active' ? '● 待处理' : '✓ 已完成'}</button>`;
    let html = `<div style="max-width:900px;margin:0 auto">
      <div style="margin-bottom:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <h2 style="font-size:16px;font-weight:600;margin-right:12px">任务中心</h2>
        ${tab('active')}${tab('completed')}
        ${repoKey ? `<span class="ts t3" style="margin-left:auto">📦 ${repoKey}</span>` : ''}
      </div>`;
    if (tasks.length === 0) {
      html += '<div class="ts t2" style="padding:40px 0;text-align:center">暂无' + (_tasksTab === 'active' ? '待处理' : '已完成') + '任务</div>';
    } else {
      html += tasks.map(t => {
        const statusTag = t.status === 'active'
          ? '<span class="tg tg-s">● 待处理</span>'
          : '<span class="tg tg-d">✓ 已完成 · ' + (t.completed_reason === 'issue_closed' ? 'Issue 已关闭' : t.completed_reason === 'reassigned' ? '已转派' : t.completed_reason || '') + '</span>';
        return `<div class="cd" style="padding:14px 16px;margin-bottom:10px">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
            <span style="font-size:14px">📋</span>
            <b style="font-size:13px">Issue #${t.issue_number} · ${t.repo_key}</b>
            <span style="margin-left:auto">${statusTag}</span>
          </div>
          <div style="font-size:12px;font-weight:500;margin-bottom:4px">${t.title || ''}</div>
          <div class="ts t2" style="margin-bottom:8px">${t.task_description || ''}</div>
          <div class="ts t3">⏱ ${fmtLocalTime(t.created_at).substring(0,16)}</div>
          <div style="display:flex;gap:8px;margin-top:10px">
            <button class="bt bt-p bt-s" onclick="openTaskConversation('${t.conversation_id || ''}')">去对话</button>
            <button class="bt bt-o bt-s" onclick="window.open('https://gitcode.com/${t.repo_key}/issues/${t.issue_number}','_blank')">查看 Issue ↗</button>
          </div>
        </div>`;
      }).join('');
    }
    html += '</div>';
    el.innerHTML = html;
  }).catch(e => {
    el.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

function openTaskConversation(convId) {
  if (convId) {
    app.nav('chat', convId);
  } else {
    app.nav('chat');
  }
}
