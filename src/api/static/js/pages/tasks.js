// ====== 任务中心（Account ▸ 任务中心）======
// 方案 B：列表工作流风（左列表 + 右详情面板）
let _tasksTab = 'all';        // all / active / completed
let _tasksAll = [];           // 全部任务缓存
let _taskSel = null;          // 选中的任务
let _taskQuery = '';          // 搜索关键字

function renderTasks() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  _taskSel = null;   // 进入页面重置选中（避免切换 repo 后详情残留旧仓库任务）
  el.innerHTML = '<div class="loading">加载中...</div>';
  loadTasks();
}

function loadTasks() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const repo = app.currentRepo();
  const repoKey = repo ? repo.fullName : '';
  // 方案 B：拉取全部任务，前端按 tab/搜索过滤（双栏联动需要全量数据）
  API.get('/tasks/mine?status=&repo_key=' + encodeURIComponent(repoKey)).then(d => {
    _tasksAll = d.tasks || [];
    // 选中项必须在当前过滤列表内（切换 repo 后旧选中失效）
    if (_taskSel && !_tasksAll.some(t => t.id === _taskSel.id)) _taskSel = null;
    if (!_taskSel && _tasksAll.length > 0) _taskSel = _tasksAll[0];
    _render();
  }).catch(e => {
    el.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

function _filteredTasks() {
  let list = _tasksAll;
  if (_tasksTab === 'active') list = list.filter(t => t.status === 'active' || t.status === 'processing');
  else if (_tasksTab === 'completed') list = list.filter(t => t.status === 'completed');
  if (_taskQuery) {
    const q = _taskQuery.toLowerCase();
    list = list.filter(t =>
      String(t.id).includes(q) ||
      (t.title || '').toLowerCase().includes(q) ||
      (t.task_description || '').toLowerCase().includes(q) ||
      String(t.issue_number).includes(q) ||
      (t.repo_key || '').toLowerCase().includes(q)
    );
  }
  return list;
}

function _render() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const filtered = _filteredTasks();
  // 选中项失效时回退到当前过滤列表第一条
  if (_taskSel && !filtered.some(t => t.id === _taskSel.id)) {
    _taskSel = filtered.length > 0 ? filtered[0] : null;
  } else if (!_taskSel && filtered.length > 0) {
    _taskSel = filtered[0];
  }
  const cntAll = _tasksAll.length;
  const cntActive = _tasksAll.filter(t => t.status === 'active' || t.status === 'processing').length;
  const cntDone = _tasksAll.filter(t => t.status === 'completed').length;

  const tab = (key, label, cnt) => {
    const act = _tasksTab === key ? ' tsk-tab-act' : '';
    return `<button class="tsk-tab${act}" onclick="_tasksTab='${key}';_render()">${label} <span class="tsk-tab-cnt">${cnt}</span></button>`;
  };

  // 分组列表（待处理/处理中在前，已完成在后）
  const activeRows = filtered.filter(t => t.status === 'active');
  const procRows = filtered.filter(t => t.status === 'processing');
  const doneRows = filtered.filter(t => t.status === 'completed');

  const rowHtml = (t) => {
    const isDone = t.status === 'completed';
    const isProc = t.status === 'processing';
    const sel = _taskSel && _taskSel.id === t.id ? ' tsk-row-sel' : '';
    const rIc = isDone ? 'tsk-ic-d' : isProc ? 'tsk-ic-pr' : 'tsk-ic-p';
    const icon = isDone ? '✓' : isProc ? '⏳' : '#';
    const tagCls = isDone ? 'tsk-tag-d' : isProc ? 'tsk-tag-pr' : 'tsk-tag-p';
    const tagText = isDone ? '<b></b>已完成' : isProc ? '<b></b>处理中' : '<b></b>待处理';
    return `<div class="tsk-row${sel}" onclick="_taskSel=window._tasksById[${t.id}];_render()">
      <div class="tsk-r-ic ${rIc}">${icon}</div>
      <div class="tsk-r-main">
        <div class="tsk-r-title">任务 #${t.id} · Issue #${t.issue_number} · ${t.title || ''}</div>
        <div class="tsk-r-meta">⏱ ${fmtLocalTime(t.created_at).substring(0,16)} · ${t.repo_key}</div>
      </div>
      <span class="tsk-tag ${tagCls}">${tagText}</span>
    </div>`;
  };

  let listHtml = '';
  if (activeRows.length > 0) {
    listHtml += `<div class="tsk-list-sec">待处理 <span class="tsk-sec-n">${activeRows.length}</span></div>` +
      activeRows.map(rowHtml).join('');
  }
  if (procRows.length > 0) {
    listHtml += `<div class="tsk-list-sec">处理中 <span class="tsk-sec-n">${procRows.length}</span></div>` +
      procRows.map(rowHtml).join('');
  }
  if (doneRows.length > 0) {
    listHtml += `<div class="tsk-list-sec">已完成 <span class="tsk-sec-n">${doneRows.length}</span></div>` +
      doneRows.map(rowHtml).join('');
  }
  if (!listHtml) {
    listHtml = `<div class="tsk-empty"><div>🗒</div><h3>暂无任务</h3><p>当前筛选下没有匹配的任务。</p></div>`;
  }

  // 详情面板
  const detailHtml = _taskSel ? _detailHtml(_taskSel) : '<div class="tsk-empty" style="border:none"><div>🗒</div><h3>选择一项任务</h3><p>从左侧列表选择任务查看详情。</p></div>';

  el.innerHTML = `
  <div class="tsk-page">
    <div class="tsk-top">
      <h1>Task Center</h1>
      <div class="tsk-controls">
        ${tab('all', '全部', cntAll)}${tab('active', '待处理', cntActive)}${tab('completed', '已完成', cntDone)}
        <div class="tsk-search"><span>🔍</span><input placeholder="搜索任务..." value="${_taskQuery}" oninput="_taskQuery=this.value;if(_taskQuery.length>1||!_taskQuery){_render()}" onkeyup="if(event.key==='Enter')_render()"></div>
      </div>
    </div>
    <div class="tsk-layout">
      <div class="tsk-list">${listHtml}</div>
      <div class="tsk-detail">${detailHtml}</div>
    </div>
  </div>`;

  // 建立 id → task 映射供行点击
  window._tasksById = {};
  _tasksAll.forEach(t => { window._tasksById[t.id] = t; });
}

function _detailHtml(t) {
  const isDone = t.status === 'completed';
  const isProc = t.status === 'processing';
  const tagCls = isDone ? 'tsk-tag-d' : isProc ? 'tsk-tag-pr' : 'tsk-tag-p';
  const tagText = isDone
    ? '✓ 已完成 · ' + (t.completed_reason === 'issue_closed' ? 'Issue 已关闭' : t.completed_reason === 'reassigned' ? '已转派' : t.completed_reason === 'bot_takeover' ? 'Bot 已接管' : t.completed_reason || '')
    : isProc ? '⏳ 处理中' : '● 待处理';
  const detIc = isDone ? 'tsk-det-ic-d' : isProc ? 'tsk-det-ic-pr' : 'tsk-det-ic-p';
  const icon = isDone ? '✓' : isProc ? '⏳' : '#';
  return `
    <div class="tsk-det-ic ${detIc}">${icon}</div>
    <div class="tsk-det-label">任务 #${t.id} · Issue #${t.issue_number}</div>
    <div class="tsk-det-title">${t.title || ''}</div>
    <div class="tsk-det-meta">
      <div class="tsk-m"><span>状态</span><b class="${tagCls}">${tagText}</b></div>
      <div class="tsk-m"><span>仓库</span><b>${t.repo_key}</b></div>
      <div class="tsk-m"><span>创建时间</span><b>${fmtLocalTime(t.created_at).substring(0,16)}</b></div>
      ${t.completed_at ? `<div class="tsk-m"><span>完成时间</span><b>${fmtLocalTime(t.completed_at).substring(0,16)}</b></div>` : ''}
    </div>
    <div class="tsk-det-desc">${t.task_description || '暂无任务描述。'}</div>
    <div class="tsk-det-actions">
      <button class="tsk-btn tsk-btn-primary" onclick="openTaskConversation(window._tasksById[${t.id}])">去对话</button>
      <button class="tsk-btn tsk-btn-ghost" onclick="window.open('https://gitcode.com/${t.repo_key}/issues/${t.issue_number}','_blank')">查看 Issue ↗</button>
    </div>`;
}

function openTaskConversation(t) {
  // 点「去对话」：active → processing（后端幂等，本地乐观更新），随后跳转 chat。
  // 跳转放 API 回调内，且不调 _render() 覆盖 pageContent（避免覆盖导航结果，见
  // task-lifecycle-design.md §4.1 修正）。
  const go = () => app.nav('chat', t && t.conversation_id ? t.conversation_id : undefined);
  if (t && t.id && t.status === 'active') {
    API.post('/tasks/' + t.id + '/start').then(d => {
      if (window._tasksById[t.id]) { window._tasksById[t.id].status = d.status || 'processing'; }
      go();
    }).catch(() => go());
  } else {
    go();
  }
}
