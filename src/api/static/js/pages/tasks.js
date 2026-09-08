// ====== 任务中心（Account ▸ 任务中心）======
// 方案 B：列表工作流风（左列表 + 右详情面板）
let _tasksTab = 'all';        // all / active / completed
let _tasksAll = [];           // 全部任务缓存
let _taskSel = null;          // 选中的任务
let _taskQuery = '';          // 搜索关键字
let _taskPage = 1;            // 当前页码
let _taskView = 'list';       // 'list' | 'board'
const _TASKS_PER_PAGE = 15;   // 每页任务数
let _taskPollTimer = null;    // 场景识别轮询定时器
let _tasksSortOrder = 'desc'; // desc=最新在前 / asc=最早在前（Issue #42）
let _tasksMode = 'default';   // default=状态优先 / time=纯时间序（Issue #42）

function _tasksSortPrefKey(repoKey) {
  return 'task_sort_' + (repoKey || 'all');
}
function _loadTasksSortPref(repoKey) {
  try {
    const saved = JSON.parse(localStorage.getItem(_tasksSortPrefKey(repoKey)) || '{}');
    if (saved.order === 'asc' || saved.order === 'desc') _tasksSortOrder = saved.order;
    if (saved.mode === 'time' || saved.mode === 'default') _tasksMode = saved.mode;
  } catch (e) { /* ignore */ }
}
function _saveTasksSortPref(repoKey) {
  try {
    localStorage.setItem(_tasksSortPrefKey(repoKey),
      JSON.stringify({order: _tasksSortOrder, mode: _tasksMode}));
  } catch (e) { /* ignore */ }
}
function toggleTasksSort() {
  _tasksSortOrder = (_tasksSortOrder === 'desc') ? 'asc' : 'desc';
  const repo = app.currentRepo();
  _saveTasksSortPref(repo ? repo.fullName : '');
  _taskPage = 1;
  loadTasks();
}
function toggleTasksMode() {
  _tasksMode = (_tasksMode === 'default') ? 'time' : 'default';
  const repo = app.currentRepo();
  _saveTasksSortPref(repo ? repo.fullName : '');
  _taskPage = 1;
  loadTasks();
}

function renderTasks() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  _taskSel = null;   // 进入页面重置选中（避免切换 repo 后详情残留旧仓库任务）
  el.innerHTML = '<div class="loading">' + I18N.t('tasks.loading') + '</div>';
  loadTasks();
  _startScenePoll();
}

// 场景识别轮询：有待处理任务且场景未识别时，定期刷新
function _startScenePoll() {
  if (_taskPollTimer) clearInterval(_taskPollTimer);
  _taskPollTimer = setInterval(function() {
    // 只在有 active/processing 任务且场景未被 VOD 识别时才刷新
    var needRefresh = _tasksAll.some(function(t) {
      return (t.status === 'active' || t.status === 'processing') && t.scene_source !== 'vod';
    });
    if (needRefresh) {
      loadTasks();
    } else {
      // 所有任务场景已识别（或无待处理任务）→ 停止轮询
      clearInterval(_taskPollTimer);
      _taskPollTimer = null;
    }
  }, 15000);  // 15 秒轮询
}

function _stopScenePoll() {
  if (_taskPollTimer) { clearInterval(_taskPollTimer); _taskPollTimer = null; }
}

let _deployUrl = '';
let _deployEditing = false;

function _loadDeployLink() {
  const repo = app.currentRepo();
  if (!repo) return;
  const bar = document.getElementById('deployLinkBar');
  if (!bar) return;
  API.get('/repos/deploy-url/' + repo.owner + '/' + repo.name).then(d => {
    _deployUrl = (d && d.deploy_url) || '';
    _renderDeployLink();
  }).catch(() => { _deployUrl = ''; _renderDeployLink(); });
}

function _renderDeployLink() {
  const bar = document.getElementById('deployLinkBar');
  if (!bar) return;
  if (_deployEditing) {
    bar.innerHTML = `<div style="display:flex;align-items:center;gap:6px">
      <input id="deployUrlInput" value="${_deployUrl}" placeholder="输入部署访问 URL（如 http://1.2.3.4:5000）" style="flex:1;max-width:400px;padding:5px 8px;border:1px solid #ccc;border-radius:6px;font-size:12px;background:#fff">
      <button class="tsk-btn tsk-btn-primary" style="padding:4px 10px;font-size:12px" onclick="_saveDeployUrl()">保存</button>
      <button class="tsk-btn tsk-btn-ghost" style="padding:4px 10px;font-size:12px" onclick="_deployEditing=false;_renderDeployLink()">取消</button>
    </div>`;
  } else if (_deployUrl) {
    bar.innerHTML = `<div style="display:flex;align-items:center;gap:6px">
      <a href="${_deployUrl}" target="_blank" rel="noopener" style="font-size:12px;color:#34c759;text-decoration:none" title="访问部署的业务系统">🌐 访问应用 ↗</a>
      <span onclick="_deployEditing=true;_renderDeployLink()" style="cursor:pointer;font-size:12px;color:#86868b;opacity:.6" title="编辑部署 URL" onmouseover="this.style.opacity=1" onmouseout="this.style.opacity=.6">✎</span>
    </div>`;
  } else {
    bar.innerHTML = '';
  }
}

window._saveDeployUrl = function() {
  const input = document.getElementById('deployUrlInput');
  if (!input) return;
  const repo = app.currentRepo();
  if (!repo) return;
  const url = input.value.trim();
  API.put('/repos/deploy-url', {full_name: repo.fullName, deploy_url: url}).then(() => {
    _deployUrl = url;
    _deployEditing = false;
    _renderDeployLink();
    app.showToast('部署 URL 已保存');
  }).catch(e => { app.showToast('保存失败: ' + (e.detail?.message || e.message)); });
};

function loadTasks() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const repo = app.currentRepo();
  const repoKey = repo ? repo.fullName : '';
  _loadTasksSortPref(repoKey);
  // 方案 B：拉取全部任务，前端按 tab/搜索过滤（双栏联动需要全量数据）
  API.get('/tasks/mine?status=&repo_key=' + encodeURIComponent(repoKey) +
          '&sort_order=' + _tasksSortOrder + '&mode=' + _tasksMode).then(d => {
    if (window._currentView !== 'tasks') return;  // stale guard
    _tasksAll = d.tasks || [];
    // 选中项必须在当前过滤列表内（切换 repo 后旧选中失效）
    if (_taskSel && !_tasksAll.some(t => t.id === _taskSel.id)) _taskSel = null;
    if (!_taskSel && _tasksAll.length > 0) _taskSel = _tasksAll[0];
    _render();
  }).catch(e => {
    if (e.name === 'AbortError' && e.silent) return;  // Issue #63: abort 静默
    if (window._currentView !== 'tasks') return;  // stale guard
    el.innerHTML = '<div class="error">' + I18N.t('tasks.load_failed', {msg: e.message}) + '</div>';
  });
}

function _filteredTasks() {
  let list = _tasksAll;
  if (_tasksTab === 'active') list = list.filter(t => t.status === 'active' || t.status === 'processing');
  else if (_tasksTab === 'completed') list = list.filter(t => t.status === 'completed');
  if (_taskQuery) {
    const q = _taskQuery.toLowerCase().trim();
    if (q) {
      list = list.filter(t => {
        const _haystack = [
          String(t.id || ''),
          (t.title || ''),
          (t.task_description || ''),
          String(t.issue_number || ''),
          (t.repo_key || ''),
          (t.assignee || ''),
          (t.scene_name || ''),
          (t.status || ''),
          (t.repo_display || ''),
        ].join(' ').toLowerCase();
        return _haystack.includes(q);
      });
    }
  }
  return list;
}

window._openBoardDetail = function(taskId) {
  const t = window._tasksById[taskId];
  if (!t) return;
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = `
  <div style="padding:0">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--b)">
      <button class="tsk-btn tsk-btn-ghost" onclick="loadTasks()" style="padding:6px 14px">← ${I18N.t('tasks.back_to_board')||'返回看板'}</button>
      <h1 style="margin:0;font-size:16px;font-weight:600">${t.title || ''}</h1>
    </div>
    ${_detailHtml(t)}
  </div>`;
};

function _renderBoard(filtered) {
  const stages = ['vod', 'issue', 'coding', 'qa', 'sre'];
  const stageLabels = {
    vod: I18N.t('tasks.stage_vod')||'VOD',
    issue: I18N.t('tasks.stage_issue')||'ISSUE',
    coding: I18N.t('tasks.stage_coding')||'Coding',
    qa: I18N.t('tasks.stage_qa')||'QA',
    sre: I18N.t('tasks.stage_sre')||'SRE'
  };
  const stageColors = {
    vod: '#ec4899', issue: '#ef4444', coding: '#3b82f6',
    qa: '#a855f7', sre: '#f59e0b'
  };
  const grouped = {};
  stages.forEach(s => grouped[s] = []);
  filtered.forEach(t => {
    let stage = t.stage || 'vod';
    if (stage === 'done') return;
    if (!grouped[stage]) stage = 'vod';
    grouped[stage].push(t);
  });

  // 苹果风格看板：固定列宽 280px，水平滚动，列独立垂直滚动
  let html = '<div style="display:flex;gap:10px;height:100%;overflow-x:auto;padding-bottom:4px">';
  stages.forEach(s => {
    const list = grouped[s];
    const col = stageColors[s];
    html += `<div style="width:280px;flex-shrink:0;display:flex;flex-direction:column;background:rgba(0,0,0,.02);border-radius:14px;border:0.5px solid var(--b,#e8e8ed)">`;
    // 列头：颜色圆点 + 名称 + 计数（苹果风格 SF Pro 字体栈）
    html += `<div style="display:flex;align-items:center;gap:8px;padding:12px 14px 10px;flex-shrink:0">
      <span style="width:7px;height:7px;border-radius:50%;background:${col};box-shadow:0 0 6px ${col}40"></span>
      <span style="font-weight:600;font-size:13px;color:var(--t1,#1d1d1f);letter-spacing:-.2px;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif">${stageLabels[s]}</span>
      <span style="font-size:11px;color:var(--t3,#86868b);font-weight:500;background:rgba(0,0,0,.05);border-radius:999px;padding:1px 8px">${list.length}</span>
    </div>`;
    // 卡片容器：独立垂直滚动
    html += `<div style="flex:1;overflow-y:auto;padding:0 8px 8px;display:flex;flex-direction:column;gap:6px">`;
    if (list.length === 0) {
      html += `<div style="padding:24px 12px;text-align:center;color:var(--t3,#86868b);font-size:12px;font-weight:400">无任务</div>`;
    } else {
      list.forEach(t => {
        const isDone = t.status === 'completed';
        const isProc = t.status === 'processing';
        const statusDot = isDone ? '#34c759' : isProc ? '#3b82f6' : '#86868b';
        const statusText = isDone ? (I18N.t('tasks.status.tag_completed')||'完成') : isProc ? (I18N.t('tasks.status.tag_processing')||'进行中') : (I18N.t('tasks.status.tag_pending')||'待处理');
        const sel = _taskSel && _taskSel.id === t.id ? ';border-color:#3b82f6;box-shadow:0 0 0 3px rgba(59,130,246,.12)' : '';
        // 卡片：苹果风格 rounded-lg + 0.5px border + 微弱阴影
        html += `<div onclick="_openBoardDetail(${t.id})" style="background:var(--c,#fff);border:0.5px solid var(--b,#e5e5ea);border-radius:10px;padding:10px 12px;cursor:pointer;transition:all .2s cubic-bezier(.4,0,.2,1);box-shadow:0 1px 2px rgba(0,0,0,.03)${sel}" onmouseover="this.style.borderColor='rgba(59,130,246,.3)';this.style.boxShadow='0 2px 8px rgba(0,0,0,.06)'" onmouseout="this.style.borderColor='var(--b,#e5e5ea)';this.style.boxShadow='0 1px 2px rgba(0,0,0,.03)'">
          <!-- Row 1: Issue# + 状态指示器 -->
          <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:4px">
            <span style="font-size:11px;color:var(--t3,#86868b);font-weight:500;font-family:-apple-system,BlinkMacSystemFont,sans-serif">${t.issue_number ? '#'+t.issue_number : ''}</span>
            <span style="display:inline-flex;align-items:center;gap:3px;font-size:10px;color:${statusDot};font-weight:500">
              <span style="width:6px;height:6px;border-radius:50%;background:${statusDot}"></span>${statusText}
            </span>
          </div>
          <!-- Row 2: 标题（2行截断） -->
          <p style="margin:0 0 6px;font-size:13px;font-weight:600;color:var(--t1,#1d1d1f);line-height:1.35;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${t.title || ''}</p>
          <!-- Row 3: 元信息 -->
          <div style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--t3,#86868b)">
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:120px">${t.repo_key || ''}</span>
            <span style="margin-left:auto;white-space:nowrap">${(t.created_at||'').substring(5,10)}</span>
          </div>
        </div>`;
      });
    }
    html += `</div></div>`;
  });
  html += '</div>';
  return html;
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
    return `<button class="tsk-tab${act}" onclick="_tasksTab='${key}';_taskPage=1;_render()">${label} <span class="tsk-tab-cnt">${cnt}</span></button>`;
  };

  // 筛选下拉：状态 + 排序归到一个 popover（参考 multica issues-header）
  const _sortIcon = _tasksSortOrder === 'desc' ? '↓' : '↑';
  const _filterPanel = `
    <div style="position:relative;display:inline-flex;align-items:center;flex-shrink:0">
      <button class="tsk-tab" onclick="document.getElementById('taskFilterPopover').style.display=document.getElementById('taskFilterPopover').style.display==='block'?'none':'block'" style="border:1px solid var(--b);border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer;background:var(--bg);display:inline-flex;align-items:center;gap:4px">
        <span style="font-size:12px">⌥</span> ${I18N.t('tasks.filter')||'筛选'}
        ${_tasksTab!=='all'||_tasksMode!=='default' ? '<span style="width:6px;height:6px;border-radius:50%;background:#3b82f6"></span>' : ''}
      </button>
      <div id="taskFilterPopover" style="display:none;position:absolute;top:calc(100% + 4px);left:0;background:var(--c,#fff);border:0.5px solid var(--b,#e5e5ea);border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.1);padding:12px;min-width:200px;z-index:50;font-family:-apple-system,BlinkMacSystemFont,sans-serif">
        <div style="font-size:10px;font-weight:600;color:var(--t3,#86868b);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">${I18N.t('tasks.filter_status')||'状态'}</div>
        <div style="display:flex;flex-direction:column;gap:4px;margin-bottom:12px">
          ${['all','active','completed'].map(k => {
            const lbl = k==='all'?I18N.t('tasks.tab.all'):k==='active'?I18N.t('tasks.tab.active'):I18N.t('tasks.tab.completed');
            const cnt = k==='all'?cntAll:k==='active'?cntActive:cntDone;
            const act = _tasksTab===k;
            return `<label style="display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:6px;cursor:pointer;font-size:12px;${act?'background:rgba(59,130,246,.06)':''}" onclick="_tasksTab='${k}';_taskPage=1;document.getElementById('taskFilterPopover').style.display='none';_render()">
              <span style="width:14px;height:14px;border-radius:50%;border:1.5px solid ${act?'#3b82f6':'var(--b,#d0d7de)'};${act?'background:#3b82f6;box-shadow:inset 0 0 0 2px #fff':''}"></span>
              ${lbl} <span style="color:var(--t3,#86868b);font-size:11px">${cnt}</span>
            </label>`;
          }).join('')}
        </div>
        <div style="font-size:10px;font-weight:600;color:var(--t3,#86868b);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">${I18N.t('tasks.sort.mode')||'排序'}</div>
        <div style="display:flex;gap:6px;margin-bottom:8px">
          <button onclick="if(_tasksMode!=='default'){_tasksMode='default';toggleTasksMode();document.getElementById('taskFilterPopover').style.display='none'}" style="flex:1;padding:5px 8px;border:0.5px solid var(--b,#e5e5ea);border-radius:8px;font-size:11px;cursor:pointer;background:${_tasksMode==='default'?'#3b82f6':'var(--bg)'};color:${_tasksMode==='default'?'#fff':'var(--t1)'}">${I18N.t('tasks.sort.priority')||'状态优先'}</button>
          <button onclick="if(_tasksMode!=='time'){_tasksMode='time';toggleTasksMode();document.getElementById('taskFilterPopover').style.display='none'}" style="flex:1;padding:5px 8px;border:0.5px solid var(--b,#e5e5ea);border-radius:8px;font-size:11px;cursor:pointer;background:${_tasksMode==='time'?'#3b82f6':'var(--bg)'};color:${_tasksMode==='time'?'#fff':'var(--t1)'}">${I18N.t('tasks.sort.time')||'时间序'}</button>
        </div>
        <button onclick="toggleTasksSort();document.getElementById('taskFilterPopover').style.display='none'" style="width:100%;padding:5px 8px;border:0.5px solid var(--b,#e5e5ea);border-radius:8px;font-size:11px;cursor:pointer;background:var(--bg);color:var(--t1)">${I18N.t('tasks.sort.order')||'排序'} ${_sortIcon}</button>
      </div>
    </div>`;

  // 分页：后端已按 sort_order/mode 排序返回；mode=time 时纯时间序，default 时后端已状态优先
  // （不再前端状态分组拼接，避免打乱时间序——Issue #42 根因）
  const allRows = filtered;
  const totalPages = Math.ceil(allRows.length / _TASKS_PER_PAGE);
  if (_taskPage > totalPages) _taskPage = 1;
  if (_taskPage < 1) _taskPage = 1;
  const pageStart = (_taskPage - 1) * _TASKS_PER_PAGE;
  const pageRows = allRows.slice(pageStart, pageStart + _TASKS_PER_PAGE);

  // 当前页分组（用于页面内分区展示，不再拼接到全量顺序中）
  const pageActive = pageRows.filter(t => t.status === 'active');
  const pageProc = pageRows.filter(t => t.status === 'processing');
  const pageDone = pageRows.filter(t => t.status === 'completed');

  const rowHtml = (t) => {
    const isDone = t.status === 'completed';
    const isProc = t.status === 'processing';
    const sel = _taskSel && _taskSel.id === t.id ? ' tsk-row-sel' : '';
    const rIc = isDone ? 'tsk-ic-d' : isProc ? 'tsk-ic-pr' : 'tsk-ic-p';
    const icon = isDone ? '✓' : isProc ? '⏳' : '#';
    const tagCls = isDone ? 'tsk-tag-d' : isProc ? 'tsk-tag-pr' : 'tsk-tag-p';
    const tagText = isDone ? I18N.t('tasks.status.tag_completed') : isProc ? I18N.t('tasks.status.tag_processing') : I18N.t('tasks.status.tag_pending');
    return `<div class="tsk-row${sel}" onclick="_taskSel=window._tasksById[${t.id}];_render()" ondblclick="openTaskConversation(window._tasksById[${t.id}])">
      <div class="tsk-r-ic ${rIc}">${icon}</div>
      <div class="tsk-r-main">
        <div class="tsk-r-title">Issue #${t.issue_number} · ${t.title || ''}</div>
        <div class="tsk-r-meta">⏱ ${fmtLocalTime(t.created_at).substring(0,16)} · ${t.repo_key}</div>
      </div>
      <span class="tsk-tag ${tagCls}">${tagText}</span>
    </div>`;
  };

  let listHtml = '';
  if (_tasksMode === 'time') {
    // 纯时间序：不按状态分区，直接按后端顺序平铺（Issue #42）
    listHtml = pageRows.map(rowHtml).join('');
  } else {
    // default：状态优先（后端已按 active→processing→completed 排序），页内分区展示
    if (pageActive.length > 0) {
      listHtml += `<div class="tsk-list-sec">${I18N.t('tasks.section.pending')} <span class="tsk-sec-n">${pageActive.length}</span></div>` +
        pageActive.map(rowHtml).join('');
    }
    if (pageProc.length > 0) {
      listHtml += `<div class="tsk-list-sec">${I18N.t('tasks.section.processing')} <span class="tsk-sec-n">${pageProc.length}</span></div>` +
        pageProc.map(rowHtml).join('');
    }
    if (pageDone.length > 0) {
      listHtml += `<div class="tsk-list-sec">${I18N.t('tasks.section.completed')} <span class="tsk-sec-n">${pageDone.length}</span></div>` +
        pageDone.map(rowHtml).join('');
    }
  }
  if (!listHtml) {
    listHtml = `<div class="tsk-empty"><div>🗒</div><h3>${I18N.t('tasks.empty.no_tasks_title')}</h3><p>${I18N.t('tasks.empty.no_tasks_desc')}</p></div>`;
  }

  // 分页控件
  let pagerHtml = '';
  if (totalPages > 1) {
    const pBtn = (label, page, disabled, active) =>
      `<button class="tsk-btn tsk-btn-ghost" style="padding:3px 10px;font-size:11px;${disabled ? 'opacity:.4;cursor:not-allowed' : ''}${active ? ';border-color:var(--a);color:var(--a);font-weight:600' : ''}" ${disabled ? 'disabled' : `onclick="_taskPage=${page};_render()"`}>${label}</button>`;
    let btns = '';
    // 最多显示 7 个页码按钮
    let s = Math.max(1, _taskPage - 3);
    let e = Math.min(totalPages, s + 6);
    if (e - s < 6) s = Math.max(1, e - 6);
    if (s > 1) btns += pBtn('1', 1, false, _taskPage === 1) + (s > 2 ? '<span style="color:var(--t3);padding:0 2px">…</span>' : '');
    for (let i = s; i <= e; i++) btns += pBtn(String(i), i, false, _taskPage === i);
    if (e < totalPages) btns += (e < totalPages - 1 ? '<span style="color:var(--t3);padding:0 2px">…</span>' : '') + pBtn(String(totalPages), totalPages, false, _taskPage === totalPages);
    pagerHtml = `<div style="display:flex;align-items:center;gap:4px;padding:10px 16px;border-top:1px solid var(--b);background:var(--bg);flex-shrink:0">
      ${pBtn('‹', _taskPage - 1, _taskPage <= 1, false)}
      ${btns}
      ${pBtn('›', _taskPage + 1, _taskPage >= totalPages, false)}
      <span class="ts t3" style="margin-left:8px;font-size:10px">${I18N.t('tasks.pager.summary', {total: allRows.length, page: _taskPage, pages: totalPages})}</span>
    </div>`;
  }

  // 详情面板
  const detailHtml = _taskSel ? _detailHtml(_taskSel) : `<div class="tsk-empty" style="border:none"><div>🗒</div><h3>${I18N.t('tasks.empty.select_task_title')}</h3><p>${I18N.t('tasks.empty.select_task_desc')}</p></div>`;

  el.innerHTML = `
  <div class="tsk-page" style="max-width:none;height:calc(100vh - 60px);display:flex;flex-direction:column">
    <div class="tsk-top">
      <h1>${I18N.t('tasks.page_title')}</h1>
      <div id="deployLinkBar" style="margin-top:4px"></div>
      <div class="tsk-controls">
        <button class="tsk-btn tsk-btn-primary" onclick="openTaskCreateModal()" style="margin-right:8px">${I18N.t('tasks.create.btn_new')}</button>
        ${_filterPanel}
        <div style="flex:1"></div>
        <div style="position:relative;display:inline-flex;align-items:center;flex-shrink:0">
          <button class="tsk-tab" onclick="document.getElementById('taskViewPopover').style.display=document.getElementById('taskViewPopover').style.display==='block'?'none':'block'" style="border:1px solid var(--b);border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer;background:var(--bg);display:inline-flex;align-items:center;gap:4px">
            <span style="font-size:12px">${_taskView==='board'?'▦':'☰'}</span> ${_taskView==='board'?(I18N.t('tasks.view_board')||'看板'):(I18N.t('tasks.view_list')||'列表')}
            <span style="font-size:9px;color:var(--t3,#86868b)">▾</span>
          </button>
          <div id="taskViewPopover" style="display:none;position:absolute;top:calc(100% + 4px);right:0;background:var(--c,#fff);border:0.5px solid var(--b,#e5e5ea);border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.1);padding:8px;min-width:140px;z-index:50;font-family:-apple-system,BlinkMacSystemFont,sans-serif">
            ${['list','board'].map(k => {
              const lbl = k==='list'?(I18N.t('tasks.view_list')||'列表'):(I18N.t('tasks.view_board')||'看板');
              const ic = k==='list'?'☰':'▦';
              const act = _taskView===k;
              return `<label style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;cursor:pointer;font-size:12px;color:var(--t1,#1d1d1f);${act?'background:rgba(59,130,246,.06)':''}" onclick="_taskView='${k}';document.getElementById('taskViewPopover').style.display='none';_render()">
                <span style="font-size:13px">${ic}</span> ${lbl}
                ${act?'<span style="margin-left:auto;color:#3b82f6;font-size:12px">✓</span>':''}
              </label>`;
            }).join('')}
          </div>
        </div>
      </div>
    </div>
    ${_taskView === 'board' ? `
    <div style="display:flex;justify-content:center;margin:8px 0 4px;flex-shrink:0">
      <div style="position:relative;width:100%;max-width:400px">
        <span style="position:absolute;left:14px;top:50%;transform:translateY(-50%);font-size:13px;color:var(--t3,#86868b);pointer-events:none">🔍</span>
        <input id="tcSearchInput" placeholder="${I18N.t('tasks.search_placeholder')}" value="${_taskQuery}" onkeyup="if(event.key==='Enter'){_taskQuery=this.value;_taskPage=1;_render()}" onchange="_taskQuery=this.value;_taskPage=1;_render()" style="width:100%;padding:9px 14px 9px 36px;border:0.5px solid var(--b,#e5e5ea);border-radius:12px;font-size:13px;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none;transition:all .2s cubic-bezier(.4,0,.2,1);box-shadow:0 1px 2px rgba(0,0,0,.03)" onfocus="this.style.borderColor='rgba(59,130,246,.4)';this.style.boxShadow='0 0 0 3px rgba(59,130,246,.08)'" onblur="this.style.borderColor='var(--b,#e5e5ea)';this.style.boxShadow='0 1px 2px rgba(0,0,0,.03)'">
      </div>
    </div>
    <div style="flex:1;display:flex;flex-direction:column;min-height:0">
      <div style="flex:1;overflow:auto;min-height:0">${_renderBoard(filtered)}</div>
    </div>` : `
    <div style="display:flex;justify-content:center;margin:8px 0 4px;flex-shrink:0">
      <div style="position:relative;width:100%;max-width:400px">
        <span style="position:absolute;left:14px;top:50%;transform:translateY(-50%);font-size:13px;color:var(--t3,#86868b);pointer-events:none">🔍</span>
        <input id="tcSearchInput" placeholder="${I18N.t('tasks.search_placeholder')}" value="${_taskQuery}" onkeyup="if(event.key==='Enter'){_taskQuery=this.value;_taskPage=1;_render()}" onchange="_taskQuery=this.value;_taskPage=1;_render()" style="width:100%;padding:9px 14px 9px 36px;border:0.5px solid var(--b,#e5e5ea);border-radius:12px;font-size:13px;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none;transition:all .2s cubic-bezier(.4,0,.2,1);box-shadow:0 1px 2px rgba(0,0,0,.03)" onfocus="this.style.borderColor='rgba(59,130,246,.4)';this.style.boxShadow='0 0 0 3px rgba(59,130,246,.08)'" onblur="this.style.borderColor='var(--b,#e5e5ea)';this.style.boxShadow='0 1px 2px rgba(0,0,0,.03)'">
      </div>
    </div>
    <div class="tsk-layout" style="flex:1;min-height:0">
      <div class="tsk-list" style="overflow-y:auto;height:100%;display:flex;flex-direction:column">${listHtml}<div style="flex:1"></div>${pagerHtml}</div>
      <div class="tsk-detail" style="overflow-y:auto;height:100%;position:relative;top:0">${detailHtml}</div>
    </div>`}
  </div>`;

  // 加载部署 URL 到任务中心顶部
  _loadDeployLink();

  // 建立 id → task 映射供行点击
  window._tasksById = {};
  _tasksAll.forEach(t => { window._tasksById[t.id] = t; });
}

function _detailHtml(t) {
  const isDone = t.status === 'completed';
  const isProc = t.status === 'processing';
  const tagCls = isDone ? 'tsk-tag-d' : isProc ? 'tsk-tag-pr' : 'tsk-tag-p';
  const reasonText = t.completed_reason === 'issue_closed' ? I18N.t('tasks.status.reason_issue_closed') : t.completed_reason === 'reassigned' ? I18N.t('tasks.status.reason_reassigned') : t.completed_reason === 'bot_takeover' ? I18N.t('tasks.status.reason_bot_takeover') : (t.completed_reason || '');
  const tagText = isDone
    ? I18N.t('tasks.status.detail_completed') + reasonText
    : isProc ? I18N.t('tasks.status.detail_processing') : I18N.t('tasks.status.detail_pending');
  const detIc = isDone ? 'tsk-det-ic-d' : isProc ? 'tsk-det-ic-pr' : 'tsk-det-ic-p';
  const icon = isDone ? '✓' : isProc ? '⏳' : '#';
  // 场景名称
  var sceneName = t.scene_id ? (t.scene_name || t.scene_id) : '—';
  var sceneBadge = t.scene_source === 'vod' ? '' : (t.scene_id && t.scene_source === 'repo_default' ? ` <span style="font-size:10px;color:var(--t3)">${I18N.t('tasks.scene_default_badge')}</span>` : '');
  // 多任务模式
  var subtaskBadge = t.is_subtask_mode ? `<span class="tg tg-q" style="font-size:10px">${I18N.t('tasks.multi_task_badge')}</span>` : `<span class="ts t3" style="font-size:10px">${I18N.t('tasks.single_task_badge')}</span>`;
  var parentIssue = t.parent_issue_number ? ' · ' + I18N.t('tasks.parent_task') + ' #' + t.parent_issue_number : '';
  return `
    <div class="tsk-det-ic ${detIc}">${icon}</div>
    <div class="tsk-det-label">Issue #${t.issue_number}</div>
    <div class="tsk-det-title">${t.title || ''}</div>
    <div class="tsk-det-meta">
      <div class="tsk-m"><span>${I18N.t('tasks.detail.status')}</span><b class="${tagCls}">${tagText}</b></div>
      <div class="tsk-m"><span>${I18N.t('tasks.detail.task_id')}</span><b>#${t.id}</b></div>
      <div class="tsk-m"><span>${I18N.t('tasks.detail.repo')}</span><b>${t.repo_key}</b></div>
      <div class="tsk-m"><span>${I18N.t('tasks.pipeline_scene')}</span><b>${sceneName}${sceneBadge}</b></div>
      <div class="tsk-m"><span>${I18N.t('tasks.task_mode')}</span><b>${subtaskBadge}${parentIssue}</b></div>
      <div class="tsk-m"><span>${I18N.t('tasks.detail.created_at')}</span><b>${fmtLocalTime(t.created_at).substring(0,16)}</b></div>
      ${t.completed_at ? `<div class="tsk-m"><span>${I18N.t('tasks.detail.completed_at')}</span><b>${fmtLocalTime(t.completed_at).substring(0,16)}</b></div>` : ''}
    </div>
    <div class="tsk-det-desc">${t.task_description || I18N.t('tasks.detail.no_description')}</div>
    <div class="tsk-det-actions">
      <button class="tsk-btn tsk-btn-ghost" onclick="window.open('https://gitcode.com/${t.repo_key}/issues/${t.issue_number}','_blank')">${I18N.t('tasks.detail.view_issue')}</button>
      <button class="tsk-btn tsk-btn-ghost" onclick="openTaskConversation(window._tasksById[${t.id}])">${I18N.t('tasks.detail.go_conversation')}</button>
    </div>`;
}

function openTaskConversation(t) {
  // 跳转 chat agent：始终用 issue:N:repoKey 格式，由 chat.js 调 ensure-issue
  // 查找或新建会话（会话被删除后旧 conversation_id 仍在 task 表里，直接传 conv_id
  // 会在会话列表找不到而回退到第一个会话，不会触发重建）
  const issueNum = t && t.issue_number ? String(t.issue_number) : '';
  const repoKey = t && t.repo_key ? t.repo_key : '';
  const param = issueNum && repoKey ? 'issue:' + issueNum + ':' + repoKey : (t && t.conversation_id || undefined);
  const go = () => app.nav('chat', param);
  if (t && t.id && t.status === 'active') {
    API.post('/tasks/' + t.id + '/start').then(d => {
      if (window._tasksById[t.id] && d && d.status) { window._tasksById[t.id].status = d.status; }
      go();
    }).catch(() => go());
  } else {
    go();
  }
}

// ===== 新建任务（同步建 GitCode Issue + 既有流程建档；责任人 = General Settings Account）=====
let _taskCreateModal = null;

function openTaskCreateModal() {
  const repo = app.currentRepo();
  const defaultRepo = repo ? repo.fullName : '';
  if (_taskCreateModal) _taskCreateModal.remove();
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // 异步加载可用场景列表
  let _sceneOptions = '<option value="">' + I18N.t('tasks.create.scene_vod_auto') + '</option>';

  const ov = document.createElement('div');
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;display:flex;align-items:center;justify-content:center;';
  ov.innerHTML = `<div style="width:min(520px,92vw);background:#fff;border:1px solid #d6dae2;border-radius:10px;padding:18px 20px;box-shadow:0 10px 40px rgba(0,0,0,.3)">
    <h3 style="margin:0 0 14px;font-size:15px;color:#111">${I18N.t('tasks.create.modal_title')}</h3>
    <div style="display:flex;flex-direction:column;gap:10px;font-size:13px">
      <div style="font-size:11px;color:#555">${I18N.t('tasks.create.repo_label')}<b>${esc(defaultRepo) || I18N.t('tasks.create.repo_empty')}</b></div>
      <label style="display:flex;flex-direction:column;gap:4px;color:#333">${I18N.t('tasks.create.title_label')} <span style="color:#d32f2f">*</span> <span style="font-size:11px;color:#888">${I18N.t('tasks.create.title_hint')}</span>
        <input id="tcCreateTitle" style="padding:7px 10px;border:1px solid #ccc;border-radius:6px;background:#fff;color:#111" placeholder="${I18N.t('tasks.create.title_placeholder')}">
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;color:#333">${I18N.t('tasks.create.desc_label')} <span style="color:#d32f2f">*</span> <span style="font-size:11px;color:#888">${I18N.t('tasks.create.desc_hint')}</span>
        <textarea id="tcCreateDesc" rows="4" style="padding:7px 10px;border:1px solid #ccc;border-radius:6px;background:#fff;color:#111;resize:vertical" placeholder="${I18N.t('tasks.create.desc_placeholder')}"></textarea>
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;color:#333">${I18N.t('tasks.create.scene_label')} <span style="font-size:11px;color:#888">${I18N.t('tasks.create.scene_hint')}</span>
        <select id="tcCreateScene" style="padding:7px 10px;border:1px solid #ccc;border-radius:6px;background:#fff;color:#111;font-size:13px">${_sceneOptions}</select>
      </label>
      <div style="font-size:11px;color:#888">${I18N.t('tasks.create.assignee_hint')}</div>
      <div id="tcError" style="display:none;color:#d32f2f;font-size:12px;background:#fdecea;border:1px solid #f5c6c0;border-radius:6px;padding:7px 10px"></div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">
      <button class="tsk-btn tsk-btn-ghost" onclick="document.getElementById('taskCreateOverlay').remove()">${I18N.t('tasks.create.btn_cancel')}</button>
      <button class="tsk-btn tsk-btn-primary" id="tcSubmitBtn" onclick="createTaskSubmit()">${I18N.t('tasks.create.btn_create')}</button>
    </div>
  </div>`;
  ov.id = 'taskCreateOverlay';
  document.body.appendChild(ov);
  _taskCreateModal = ov;

  // 异步加载场景列表填充下拉框（带 repo 参数获取仓库级场景）
  API.get('/scenes/with-pipeline?repo=' + encodeURIComponent(defaultRepo)).then(function(d) {
    const sel = document.getElementById('tcCreateScene');
    if (!sel) return;
    const scenes = d.scenes || [];
    scenes.forEach(function(s) {
      const opt = document.createElement('option');
      opt.value = s.id;
      const scopeTag = s.scope === 'repo' ? '【仓库级】' : '';
      opt.textContent = (s.name || s.id) + (scopeTag ? ' ' + scopeTag : '');
      sel.appendChild(opt);
    });
  }).catch(function(){});

  // 点击遮罩空白处关闭
  let _modalDownOnOverlay = false;
  ov.addEventListener('mousedown', e => { _modalDownOnOverlay = (e.target === ov); });
  ov.addEventListener('click', e => { if (e.target === ov && _modalDownOnOverlay) ov.remove(); });
}

function createTaskSubmit() {
  const repo = app.currentRepo();
  const repoKey = repo ? repo.fullName : '';
  const title = (document.getElementById('tcCreateTitle') || {}).value || '';
  const desc = (document.getElementById('tcCreateDesc') || {}).value || '';
  const sceneId = (document.getElementById('tcCreateScene') || {}).value || '';
  const errEl = document.getElementById('tcError');
  const showErr = (msg) => { if (errEl) { errEl.style.display = 'block'; errEl.textContent = msg; } };
  if (!repoKey.trim()) { showErr(I18N.t('tasks.create.err_no_repo')); return; }
  if (!title.trim()) { showErr(I18N.t('tasks.create.err_no_title')); return; }
  if (!desc.trim()) { showErr(I18N.t('tasks.create.err_no_desc')); return; }
  // 本地查重：同仓库同标题已存在 → 提示用户
  var _dup = _tasksAll.filter(function(t) { return (t.title || '').trim() === title.trim(); });
  if (_dup.length > 0) {
    showErr(I18N.t('tasks.create.err_dup_title', {issue: '#' + (_dup[0].issue_number || _dup[0].id)}));
    return;
  }
  const btn = document.getElementById('tcSubmitBtn');
  if (btn) { btn.disabled = true; btn.textContent = I18N.t('tasks.create.btn_creating'); }
  API.post('/tasks/create', {repo_key: repoKey.trim(), title: title.trim(), description: desc.trim(), scene_id: sceneId})
    .then(d => {
      const ov = document.getElementById('taskCreateOverlay');
      if (ov) ov.remove();
      if (errEl && d && d.message) { errEl.style.display = 'block'; errEl.textContent = I18N.t('tasks.create.success_prefix') + d.message; }
      loadTasks();
    })
    .catch(e => {
      if (btn) { btn.disabled = false; btn.textContent = I18N.t('tasks.create.btn_create'); }
      const msg = (e && e.detail && e.detail.message) || (e && e.message) || String(e);
      showErr(I18N.t('tasks.create.err_create_failed', {msg: msg}));
    });
}

function changeTaskStatus(taskId, status) {
  API.put('/tasks/' + taskId + '/status', {status: status})
    .then(() => loadTasks())
    .catch(e => {
      const msg = (e && e.detail && e.detail.message) || (e && e.message) || String(e);
      alert(I18N.t('tasks.change_status_failed', {msg: msg}));
    });
}
