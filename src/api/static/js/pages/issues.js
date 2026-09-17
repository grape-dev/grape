// ====== 需求中心（Account ▸ 需求中心 / Issues Center）======
// 方案 B：列表工作流风（左列表 + 右详情面板）

// 事件时间友好格式：转为本地时间，1小时内显示"X分钟前"，1天内显示"X小时前"，超过1天显示"MM-DD HH:mm"
function _fmtEventTime(utcStr) {
  if (!utcStr) return '';
  var local = fmtLocalTime(utcStr);
  if (!local) return utcStr;
  var d = new Date(local.replace(' ', 'T'));
  if (isNaN(d.getTime())) return local;
  var now = Date.now();
  var diff = now - d.getTime();
  if (diff < 0) diff = 0;
  var mins = Math.floor(diff / 60000);
  if (mins < 1) return '刚刚';
  if (mins < 60) return mins + '分钟前';
  var hours = Math.floor(mins / 60);
  if (hours < 24) return hours + '小时前';
  // 超过1天：显示 MM-DD HH:mm
  var pad = function(n) { return String(n).padStart(2, '0'); };
  return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

let _tasksAll = [];           // 全部任务缓存
let _taskSel = null;          // 选中的任务
let _taskQuery = '';          // 搜索关键字
let _taskPage = 1;            // 当前页码
let _taskView = 'board';       // 'list' | 'board'
const _TASKS_PER_PAGE = 15;   // 每页任务数
let _taskPollTimer = null;    // 场景识别轮询定时器
let _tasksSortOrder = 'desc'; // desc=最新在前 / asc=最早在前（Issue #42）
let _tasksMode = 'time';     // time=纯时间序（唯一模式）
let _taskFilterStatuses = []; // 按状态多选筛选（空=全部）
let _taskFilterAssignees = []; // 按创建人多选筛选（空=全部）
let _tasksViewMode = 'mine';  // 'mine' | 'repo_all'（Issue #72: 仓库全部视图）
let _taskFilterPopoverOpen = false;  // 筛选 popover 展开状态（跨 _render 保持）
let _filterStatusExpanded = false;   // 状态筛选区展开状态
let _filterCreatorExpanded = false;  // 创建人筛选区展开状态

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
  // 已统一为 time 模式，保留函数兼容旧调用
  _tasksMode = 'time';
  const repo = app.currentRepo();
  _saveTasksSortPref(repo ? repo.fullName : '');
  _taskPage = 1;
  loadTasks();
}
function _toggleFilterStatus(s) {
  const i = _taskFilterStatuses.indexOf(s);
  if (i >= 0) _taskFilterStatuses.splice(i, 1);
  else _taskFilterStatuses.push(s);
  _taskPage = 1;
  _render();
}
function _toggleFilterAssignee(a) {
  const i = _taskFilterAssignees.indexOf(a);
  if (i >= 0) _taskFilterAssignees.splice(i, 1);
  else _taskFilterAssignees.push(a);
  _taskPage = 1;
  loadTasks();
}
function toggleCreatedByMe() {
  const me = (window.app && app._user && app._user.username) || '';
  if (me) _toggleFilterAssignee(me);
}
function _toggleFilterPopover() {
  _taskFilterPopoverOpen = !_taskFilterPopoverOpen;
  var el = document.getElementById('taskFilterPopover');
  if (el) el.style.display = _taskFilterPopoverOpen ? 'block' : 'none';
}
function _closeFilterPopover() {
  _taskFilterPopoverOpen = false;
  var el = document.getElementById('taskFilterPopover');
  if (el) el.style.display = 'none';
}
function _toggleFilterSection(sec) {
  if (sec === 'status') _filterStatusExpanded = !_filterStatusExpanded;
  else _filterCreatorExpanded = !_filterCreatorExpanded;
  _applyFilterPopoverState();
}
function _applyFilterPopoverState() {
  var pop = document.getElementById('taskFilterPopover');
  if (pop) pop.style.display = _taskFilterPopoverOpen ? 'block' : 'none';
  var sb = document.getElementById('_filterStatusBody');
  if (sb) sb.style.display = _filterStatusExpanded ? 'block' : 'none';
  var cb = document.getElementById('_filterCreatorBody');
  if (cb) cb.style.display = _filterCreatorExpanded ? 'block' : 'none';
  var sa = document.getElementById('_filterStatusArrow');
  if (sa) sa.textContent = _filterStatusExpanded ? '▾' : '▸';
  var ca = document.getElementById('_filterCreatorArrow');
  if (ca) ca.textContent = _filterCreatorExpanded ? '▾' : '▸';
}

function renderTasks() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  _taskSel = null;   // 进入页面重置选中（避免切换 repo 后详情残留旧仓库任务）
  _taskFilterPopoverOpen = false;
  window._pendingTaskDetailId = null;  // Issue #175: 清理残留，防止意外跳详情
  el.innerHTML = '<div class="loading">' + I18N.t('tasks.loading') + '</div>';
  loadTasks();
  _startScenePoll();
}

window._switchTasksViewMode = function(mode) {
  if (_tasksViewMode === mode) return;
  _tasksViewMode = mode;
  _taskSel = null;
  _taskPage = 1;
  _taskFilterStatuses = [];
  _taskFilterAssignees = [];
  loadTasks();
};

// 场景识别轮询：有待处理任务且场景未识别时，定期刷新
// Issue #126: processing 任务也需轮询刷新 per-agent 节点状态
function _startScenePoll() {
  if (_taskPollTimer) clearInterval(_taskPollTimer);
  _taskPollTimer = setInterval(function() {
    // 有 active/processing 任务且场景未识别 → 刷新
    // 或有 processing 任务（节点状态可能变更）→ 刷新
    var needRefresh = _tasksAll.some(function(t) {
      return ((t.status === 'active' || t.status === 'processing') && t.scene_source !== 'vod') ||
             (t.status === 'processing');
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
  const repoKey = repo ? (repo.repoKey || repo.fullName || '') : '';
  _loadTasksSortPref(repoKey);
  // 方案 B：拉取全部任务，前端按 tab/搜索过滤（双栏联动需要全量数据）
  // 「仓库全部」视图统一走 /issues/{owner}/{repo}（归一，见 issues-list-merge-design.md）
  let apiUrl;
  if (_tasksViewMode === 'repo_all' && repo) {
    apiUrl = '/issues/' + repo.owner + '/' + repo.name +
             '?sort_order=' + _tasksSortOrder + '&mode=' + _tasksMode;
    if (_taskFilterAssignees.length) {
      apiUrl += '&assignees=' + encodeURIComponent(_taskFilterAssignees.join(','));
    }
  } else {
    apiUrl = '/issues/mine?status=&repo_key=' + encodeURIComponent(repoKey) +
             '&sort_order=' + _tasksSortOrder + '&mode=' + _tasksMode +
             (_taskFilterAssignees.length ? '&assignees=' + encodeURIComponent(_taskFilterAssignees.join(',')) : '');
  }
  API.get(apiUrl).then(d => {
    if (window._currentView !== 'tasks') return;  // stale guard
    _tasksAll = d.issues || d.tasks || [];
    // Issue #175: 刷新 _taskSel 指向新对象（旧引用的 status 等字段已过时）
    if (_taskSel) {
      var updated = _tasksAll.find(t => t.id === _taskSel.id);
      _taskSel = updated || null;
    }
    if (!_taskSel && _tasksAll.length > 0) _taskSel = _tasksAll[0];
    // Issue #175: 有待打开详情时不先渲染列表，避免列表闪现后立刻被详情覆盖
    var _openedDetail = false;  // 跟踪本次 loadTasks 是否打开了详情页
    if (!window._pendingTaskDetailId && !window._pendingIssueNumber) {
      _render();
    } else {
      // 跳过 _render() 时仍需更新 _tasksById 供 _openBoardDetail 使用
      window._tasksById = {};
      _tasksAll.forEach(t => { window._tasksById[t.id] = t; });
    }
    // Issue #166: 从场景详情页流水线跳转来时，按 issue_number 匹配并打开需求详情
    if (window._pendingIssueNumber) {
      var _pinNum = window._pendingIssueNumber;
      window._pendingIssueNumber = null;
      var _match = _tasksAll.find(function(t) { return String(t.issue_number) === _pinNum; });
      if (_match) {
        _openBoardDetail(_match.id);
        _openedDetail = true;
      } else if (window._taskDetailFromScene && window._taskDetailFromScene.latestSeq) {
        var _seq = window._taskDetailFromScene.latestSeq;
        window._taskDetailFromScene = null;
        _render();  // 无匹配任务，渲染列表
        app.nav('event', _seq);
      } else {
        window._taskDetailFromScene = null;
        _render();  // 无匹配任务，渲染列表
      }
    }
    // 从事件详情页返回时，自动打开对应任务详情
    if (window._pendingTaskDetailId) {
      var pid = window._pendingTaskDetailId;
      window._pendingTaskDetailId = null;
      if (window._tasksById && window._tasksById[pid]) {
        _openBoardDetail(pid);
        _openedDetail = true;
      } else {
        _render();  // 任务不存在了，渲染列表
      }
    } else if (!_openedDetail) {
      // Issue #175: 普通列表刷新时清除详情页状态，防止 _withTaskLoading 误判在详情页。
      // 安全前提：_openBoardDetail 进入详情页时会 clearInterval 停止轮询（L325），
      // 所以轮询触发的 loadTasks 不会在详情页期间执行此清除。如未来改为详情页也保持轮询，需同步调整此处。
      window._currentDetailTask = null;
    }
  }).catch(e => {
    if (e.name === 'AbortError' && e.silent) return;  // Issue #63: abort 静默
    if (window._currentView !== 'tasks') return;  // stale guard
    window._pendingTaskDetailId = null;  // Issue #175: 清理待打开详情（loadTasks 失败时不残留）
    el.innerHTML = '<div class="error">' + I18N.t('tasks.load_failed', {msg: e.message}) + '</div>';
  });
}

function _mapStatusL(t) {
  if (t.status === 'cancelled') return 'cancelled';
  if (t.status === 'completed' || t.stage === 'done') return 'done';
  if (t.status === 'pending_review' || t.stage === 'pending_review') return 'pending_review';
  if (t.status === 'awaiting_input') return 'awaiting_input';
  if (['vod','issue','coding','qa','sre'].includes(t.stage)) return 'in_progress';
  if (t.status === 'processing') return 'in_progress';
  return 'pending';
}

function _filteredTasks() {
  let list = _tasksAll;
  // 状态多选过滤
  if (_taskFilterStatuses.length > 0) {
    list = list.filter(t => {
      const ms = _mapStatusL(t);
      return _taskFilterStatuses.includes(ms);
    });
  }
  // 创建人多选过滤（后端已按 assignees 过滤，前端二次过滤保持一致）
  if (_taskFilterAssignees.length > 0) {
    list = list.filter(t => _taskFilterAssignees.includes(t.assignee));
  }
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
  window._currentDetailTask = t;
  const el = document.getElementById('pageContent');
  if (!el) return;
  // 暂停轮询——防止详情页被 loadTasks() 覆盖
  if (_taskPollTimer) { clearInterval(_taskPollTimer); _taskPollTimer = null; }
  // 先渲染基本详情，然后异步加载流水线
  // Issue #166: 从场景详情页进入时，返回按钮回到场景详情页
  var _backLabel = I18N.t('tasks.back_to_board')||'返回看板';
  var _backAction = 'loadTasks()';
  if (window._taskDetailFromScene) {
    _backLabel = '← ' + (I18N.t('dashboard.scene_detail')||'场景详情');
    _backAction = "window._taskDetailFromScene=null;window.history.back()";
  }
  el.innerHTML = `
  <div style="padding:0;max-width:720px;margin:0 auto">
    <div style="display:flex;align-items:center;gap:12px;padding-bottom:6px">
      <button class="tsk-btn tsk-btn-ghost" onclick="${_backAction}" style="padding:6px 14px">${_backLabel}</button>
      <h1 style="margin:0;font-size:16px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.title || ''}</h1>
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;padding-bottom:10px;border-bottom:1px solid var(--b);margin-bottom:12px">
      <button class="tsk-btn tsk-btn-ghost" style="padding:4px 10px;border-radius:8px;font-size:11px" onclick="window.open('${_vcsUrl(t.repo_key, '/issues/' + t.issue_number)}','_blank')">${I18N.t('tasks.detail.view_issue')}</button>
      <button class="tsk-btn tsk-btn-ghost" style="padding:4px 10px;border-radius:8px;font-size:11px" onclick="openTaskConversation(window._tasksById[${t.id}])">${I18N.t('tasks.detail.go_conversation')}</button>
      ${_renderTaskActionsInline(t)}
    </div>
    <div id="_boardDetailContent">${_detailHtml(t)}</div>
    <div id="_boardPipelineSection" style="margin-top:12px">
      <div style="text-align:center;padding:20px;color:var(--t3,#86868b);font-size:13px">${I18N.t('tasks.loading')||'加载中...'}...</div>
    </div>
  </div>`;
  // 异步加载附件列表（_detailHtml 渲染后 DOM 才有 _taskAttachmentsList）
  _loadTaskAttachments(t.id);
  // 异步加载流水线信息
  API.get('/issues/' + taskId + '/pipeline').then(function(data) {
    _renderPipelineSection(data);
  }).catch(function() {
    document.getElementById('_boardPipelineSection').innerHTML = '';
  });
};

window._jumpToPipeline = function(nodeId) {
  const t = window._currentDetailTask;
  if (!t || !t.repo_key) { console.warn('jumpToPipeline: no current detail task'); return; }
  const repoKey = t.repo_key.replace(/^(github|gitcode):/, '');
  const parts = repoKey.split('/');
  if (parts.length < 2) { console.warn('jumpToPipeline: bad repo_key', t.repo_key); return; }
  // 设置当前 repo 到 localStorage（app.currentRepo 从这里读）
  const repoObj = {owner: parts[0], name: parts[1], fullName: parts[0] + '/' + parts[1]};
  localStorage.setItem('currentRepo', JSON.stringify(repoObj));
  // 导航到流水线页面
  if (typeof app !== 'undefined' && app.nav) {
    app.nav('pipe');
  } else {
    console.warn('jumpToPipeline: app.nav not available');
  }
};

// 点击流水线节点 → 跳转到该节点最新事件详情页
window._jumpToNodeEvent = function(seq) {
  if (!seq) { console.warn('jumpToNodeEvent: no seq'); return; }
  // Issue #166: 清除场景来源标记，避免从事件页返回任务详情时错误回退到场景页
  window._taskDetailFromScene = null;
  // 存储来源任务上下文，供事件详情页返回使用
  const t = window._currentDetailTask;
  // 始终赋值（无任务时置 null），避免非任务来源进入事件页时残留旧标志
  window._eventFromTask = (t && t.id) ? { taskId: t.id } : null;
  // 标记本次进入事件页来自任务详情，供 app.nav('event') 路由入口识别
  window._eventNavFromTask = true;
  // 设置当前 repo 到 localStorage（与 _jumpToPipePage 一致）
  if (t && t.repo_key) {
    const repoKey = t.repo_key.replace(/^(github|gitcode):/, '');
    const parts = repoKey.split('/');
    if (parts.length >= 2) {
      const repoObj = {owner: parts[0], name: parts[1], fullName: parts[0] + '/' + parts[1]};
      localStorage.setItem('currentRepo', JSON.stringify(repoObj));
    }
  }
  // 直接修改 hash 跳转事件详情页（#event/seq），触发 hashchange → app.nav('event', seq)
  window.location.hash = '#event/' + seq;
};

// 点击流水线卡片区域 → 跳转到流水线页面
window._jumpToPipePage = function() {
  const t = window._currentDetailTask;
  if (!t || !t.repo_key) { console.warn('jumpToPipePage: no current detail task'); return; }
  const repoKey = t.repo_key.replace(/^(github|gitcode):/, '');
  const parts = repoKey.split('/');
  if (parts.length < 2) return;
  const repoObj = {owner: parts[0], name: parts[1], fullName: parts[0] + '/' + parts[1]};
  localStorage.setItem('currentRepo', JSON.stringify(repoObj));
  if (typeof app !== 'undefined' && app.nav) {
    // 传递 issue_number 让流水线页高亮对应任务
    var issueNum = t.issue_number || '';
    app.nav('pipe', issueNum ? String(issueNum) : undefined);
  }
};

function _renderPipelineSection(data) {
  const el = document.getElementById('_boardPipelineSection');
  if (!el) return;
  // 注入脉冲动画 CSS（仅一次）
  if (!document.getElementById('_grapePulseCSS')) {
    const style = document.createElement('style');
    style.id = '_grapePulseCSS';
    style.textContent = `
      @keyframes grapePulse{0%,100%{transform:scale(1);box-shadow:0 0 0 0 rgba(59,130,246,.3)}50%{transform:scale(1.1);box-shadow:0 0 0 6px rgba(59,130,246,0)}}
      @keyframes grapeRing{0%{transform:scale(1);opacity:.6}100%{transform:scale(2.2);opacity:0}}
    `;
    document.head.appendChild(style);
  }
  const nodes = data.nodes || [];
  const events = data.events || [];
  if (nodes.length === 0 && events.length === 0) {
    el.innerHTML = '';
    return;
  }
  const _cardStyle = 'background:rgba(0,0,0,.02);border-radius:14px;border:0.5px solid var(--b,#e8e8ed);padding:14px 16px';
  const _cardTitle = 'font-size:12px;font-weight:600;color:var(--t3,#86868b);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;font-family:-apple-system,BlinkMacSystemFont,sans-serif';
  let html = `<div style="${_cardStyle};cursor:pointer" onclick="_jumpToPipePage()">`;
  // 卡片标题：流水线 + 场景名
  html += `<div style="${_cardTitle}">`;
  html += `<span>${I18N.t('tasks.pipeline_scene')||'流水线'} · ${data.scene_name || ''}</span>`;
  html += `</div>`;
  // 流水线节点进度条
  if (nodes.length > 0) {
    html += '<div style="display:flex;align-items:center;gap:0;margin-bottom:16px;padding:10px;background:rgba(0,0,0,.02);border-radius:10px">';
    nodes.forEach(function(n, i) {
      var ns = n.node_status;
      // 兼容数字状态码（0=pending,1=running,2=completed,3=failed,4=cancelled,5=skipped）和旧字符串
      var isCompleted = ns === 'completed' || ns === 2;
      var isCurrent = ns === 'current' || ns === 1;
      var isFailed = ns === 'failed' || ns === 3;
      var isCancelled = ns === 'cancelled' || ns === 4;
      var isSkipped = ns === 'skipped' || ns === 5;
      var col = isCompleted ? '#34c759' : isCurrent ? '#0071e3' : isFailed ? '#ff3b30' : isCancelled ? '#86868b' : isSkipped ? '#c7c7cc' : '#86868b';
      var bg = isCompleted ? '#34c759' : isCurrent ? '#0071e3' : isFailed ? '#ff3b30' : isCancelled ? '#86868b' : isSkipped ? '#c7c7cc' : 'rgba(0,0,0,.06)';
      var pulseStyle = isCurrent ? ';animation:grapePulse 1.2s ease-in-out infinite' : '';
      var pulseRing = isCurrent ? '<div style="position:absolute;width:24px;height:24px;border-radius:50%;border:2px solid #0071e3;opacity:0;animation:grapeRing 1.5s ease-out infinite"></div>' : '';
      var innerStyle = isCurrent ? ';position:relative' : '';
      // 节点点击：有事件 seq 则跳转事件详情，否则跳转流水线页面（阻止冒泡到卡片）
      var nodeSeq = (n.latest_event && n.latest_event.seq) || '';
      var nodeClick = (isCurrent || isCompleted) ? (nodeSeq ? ` onclick="event.stopPropagation();_jumpToNodeEvent(${nodeSeq})"` : ` onclick="event.stopPropagation();_jumpToPipePage()"`) : '';
      var nodeCursor = (isCurrent || isCompleted) ? ';cursor:pointer' : '';
      var nodeIcon = isCompleted ? '✓' : isFailed ? '✕' : isCancelled ? '⊘' : isSkipped ? '–' : (i+1);
      html += `<div style="display:flex;flex-direction:column;align-items:center;gap:4px;flex:1;min-width:0${nodeCursor}"${nodeClick}>`;
      html += `<div style="width:24px;height:24px;border-radius:50%;background:${bg};display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:600;flex-shrink:0${innerStyle}${pulseStyle}">${pulseRing}${nodeIcon}</div>`;
      html += `<span style="font-size:9px;color:${col};font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;font-family:-apple-system,BlinkMacSystemFont,sans-serif">${n.name||n.id||''}</span>`;
      html += `</div>`;
      if (i < nodes.length - 1) {
        var nextNs = nodes[i+1].node_status;
        var nextCompleted = nextNs === 'completed' || nextNs === 2;
        var lineCol = (nextCompleted || isCompleted) ? '#34c759' : 'rgba(0,0,0,.1)';
        html += `<div style="height:2px;flex:0 0 14px;background:${lineCol};border-radius:1px;margin-top:-12px"></div>`;
      }
    });
    html += '</div>';
  }
  // 事件历史（同一卡片内）
  if (events.length > 0) {
    html += `<div style="${_cardTitle};margin-top:4px">${I18N.t('tasks.event_history')||'事件历史'}</div>`;
    html += '<div style="display:flex;flex-direction:column;gap:0">';
    events.forEach(function(e, i) {
      var isLast = i === events.length - 1;
      var statusCol = e.status === 'completed' ? '#34c759' : e.status === 'failed' ? '#ef4444' : e.status === 'cancelled' ? '#86868b' : '#3b82f6';
      var statusIcon = e.status === 'completed' ? '✓' : e.status === 'failed' ? '✕' : e.status === 'cancelled' ? '○' : '●';
      var evSeq = e.seq || '';
      var evClick = evSeq ? ' onclick="event.stopPropagation();_jumpToNodeEvent(' + evSeq + ')"' : '';
      var evCursor = evSeq ? ';cursor:pointer' : '';
      html += `<div style="display:flex;gap:10px;padding-bottom:${isLast?'0':'10px'};border-radius:8px;transition:background .15s"${evClick} onmouseover="this.style.background='rgba(0,0,0,.03)'" onmouseout="this.style.background='transparent'">`;
      html += `<div style="display:flex;flex-direction:column;align-items:center;flex-shrink:0">`;
      html += `<div style="width:18px;height:18px;border-radius:50%;background:${statusCol}15;display:flex;align-items:center;justify-content:center;font-size:9px;color:${statusCol};font-weight:600">${statusIcon}</div>`;
      if (!isLast) html += `<div style="width:2px;flex:1;background:rgba(0,0,0,.08);margin-top:2px;min-height:16px"></div>`;
      html += `</div>`;
      html += `<div style="flex:1;min-width:0;padding-top:1px${evCursor}">`;
      html += `<div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px">`;
      html += `<span style="font-size:11px;font-weight:600;color:var(--t1,#1d1d1f);font-family:-apple-system,BlinkMacSystemFont,sans-serif">${e.agent_type || e.event_type || ''}</span>`;
      html += `<span style="font-size:10px;color:var(--t3,#86868b);white-space:nowrap">${_fmtEventTime(e.created_at)}</span>`;
      html += `</div>`;
      if (e.title) html += `<div style="font-size:10px;color:var(--t2,#3a3a3c);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${e.title}</div>`;
      if (e.summary) html += `<div style="font-size:9px;color:var(--t3,#86868b);margin-top:2px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">${e.summary}</div>`;
      html += `</div></div>`;
    });
    html += '</div>';
  }
  html += '</div>';
  el.innerHTML = html;
}

function _renderBoard(filtered) {
  // 看板列按 task.status 严格分组
  const statusCols = ['pending', 'in_progress', 'awaiting_input', 'pending_review', 'done', 'cancelled'];
  const statusLabels = {
    pending: I18N.t('tasks.status.tag_pending') ? I18N.t('tasks.status.tag_pending').replace(/<[^>]*>/g,'') : '待处理',
    in_progress: I18N.t('tasks.status.tag_processing') ? I18N.t('tasks.status.tag_processing').replace(/<[^>]*>/g,'') : '进行中',
    awaiting_input: I18N.t('tasks.status.tag_awaiting_input')||'待澄清',
    pending_review: I18N.t('tasks.status.tag_pending_review')||'待审核',
    done: I18N.t('tasks.status.tag_completed') ? I18N.t('tasks.status.tag_completed').replace(/<[^>]*>/g,'') : '已完成',
    cancelled: I18N.t('tasks.status.tag_cancelled')||'已取消'
  };
  const statusColors = {
    pending: '#86868b', in_progress: '#3b82f6', awaiting_input: '#f59e0b',
    pending_review: '#10b981', done: '#34c759', cancelled: '#86868b'
  };
  const stageLabels = {
    vod: 'VOD', issue: 'ISSUE', coding: 'Coding', qa: 'QA',
    sre: 'SRE', pending_review: I18N.t('tasks.status.tag_pending_review')||'待审核',
    done: I18N.t('tasks.status.tag_completed') ? I18N.t('tasks.status.tag_completed').replace(/<[^>]*>/g,'') : '已完成',
    '': '—'
  };
  const stageColors = {
    vod: '#ec4899', issue: '#ef4444', coding: '#3b82f6', qa: '#a855f7',
    sre: '#f59e0b', pending_review: '#10b981', done: '#34c759', '': '#86868b'
  };
  // task.status → 看板列映射（Issue #85: 增加 task.status=pending_review 支持）
  function _mapStatus(t) {
    if (t.status === 'cancelled') return 'cancelled';
    if (t.status === 'completed' || t.stage === 'done') return 'done';
    if (t.status === 'pending_review' || t.stage === 'pending_review') return 'pending_review';
    if (t.status === 'awaiting_input') return 'awaiting_input';
    const _activeStages = ['vod','issue','coding','qa','sre'];
    if (_activeStages.includes(t.stage)) return 'in_progress';
    if (t.status === 'processing') return 'in_progress';
    return 'pending';
  }
  const grouped = {};
  statusCols.forEach(s => grouped[s] = []);
  filtered.forEach(t => { grouped[_mapStatus(t)].push(t); });

  // 苹果风格看板：响应式列宽，水平滚动，列独立垂直滚动
  let html = '<div style="display:flex;gap:10px;height:100%;overflow-x:auto;padding-bottom:4px;flex-shrink:0">';
  statusCols.forEach(s => {
    const list = grouped[s];
    const col = statusColors[s];
    const dim = (s === 'cancelled') ? ';opacity:.65' : '';
    html += `<div style="min-width:240px;max-width:300px;flex:1 1 240px;flex-shrink:0;display:flex;flex-direction:column;background:rgba(0,0,0,.02);border-radius:14px;border:0.5px solid var(--b,#e8e8ed);height:100%;overflow:hidden${dim}">`;
    // 列头：颜色圆点 + 名称 + 计数
    html += `<div style="display:flex;align-items:center;gap:8px;padding:12px 14px 10px;flex-shrink:0">
      <span style="width:7px;height:7px;border-radius:50%;background:${col};box-shadow:0 0 6px ${col}40"></span>
      <span style="font-weight:600;font-size:13px;color:var(--t1,#1d1d1f);letter-spacing:-.2px;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',sans-serif">${statusLabels[s]}</span>
      <span style="font-size:11px;color:var(--t3,#86868b);font-weight:500;background:rgba(0,0,0,.05);border-radius:999px;padding:1px 8px">${list.length}</span>
    </div>`;
    // 卡片容器：独立垂直滚动
    html += `<div style="flex:1;overflow-y:auto;min-height:0;padding:0 8px 8px;display:flex;flex-direction:column;gap:6px">`;
    if (list.length === 0) {
      html += `<div style="padding:24px 12px;text-align:center;color:var(--t3,#86868b);font-size:12px;font-weight:400">${I18N.t('tasks.empty.no_tasks_desc')||'无需求'}</div>`;
    } else {
      list.forEach(t => {
        const _ms = _mapStatus(t);
        const _stage = t.stage || '';
        const _stageLabel = stageLabels[_stage] || _stage || '—';
        const _stageColor = stageColors[_stage] || '#86868b';
        const _isRunning = s === 'in_progress' && t.is_running && ['vod','issue','coding','qa','sre'].includes(_stage);
        const sel = _taskSel && _taskSel.id === t.id ? ';border-color:#3b82f6;box-shadow:0 0 0 3px rgba(59,130,246,.12)' : '';
        const _cardCls = _isRunning ? ' tsk-card-radar' : '';
        const _cardOverflow = _isRunning ? ';overflow:hidden' : '';
        html += `<div onclick="_openBoardDetail(${t.id})" class="${_cardCls.trim()}" style="position:relative;flex:0 0 auto;min-height:76px;background:var(--c,#fff);border:0.5px solid var(--b,#e5e5ea);border-radius:10px;padding:10px 12px;cursor:pointer;transition:all .2s cubic-bezier(.4,0,.2,1);box-shadow:0 1px 2px rgba(0,0,0,.03)${_cardOverflow}${sel}" onmouseover="this.style.borderColor='rgba(59,130,246,.3)';this.style.boxShadow='0 2px 8px rgba(0,0,0,.06)'" onmouseout="this.style.borderColor='var(--b,#e5e5ea)';this.style.boxShadow='0 1px 2px rgba(0,0,0,.03)'">`;
        if (_isRunning) {
          html += `<div class="tsk-radar-sweep" style="background:conic-gradient(from 0deg,transparent 0deg,${_stageColor}14 40deg,transparent 90deg)"></div>`;
        }
        html += `<div style="position:relative;z-index:1">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:4px">
            <span style="font-size:11px;color:var(--t3,#86868b);font-weight:500;font-family:-apple-system,BlinkMacSystemFont,sans-serif">${t.issue_number ? '#'+t.issue_number : ''}</span>
            <span style="display:inline-flex;align-items:center;gap:3px;font-size:10px;color:${_stageColor};font-weight:600;background:${_stageColor}15;border-radius:4px;padding:2px 6px${_isRunning ? `;animation:radarPulse 1.2s ease-in-out infinite` : ''}">
              ${_isRunning ? `<span style="width:5px;height:5px;border-radius:50%;background:${_stageColor};animation:radarPulse 1.2s ease-in-out infinite"></span>` : ''}
              ${_stageLabel}
            </span>
          </div>
          <p style="margin:0 0 6px;font-size:13px;font-weight:600;color:var(--t1,#1d1d1f);line-height:1.35;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${t.title || ''}</p>
          <div style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--t3,#86868b)">
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:120px">${t.assignee ? '👤 ' + t.assignee : (t.repo_key || '')}</span>
            <span style="margin-left:auto;white-space:nowrap">${(t.created_at||'').substring(5,10)}</span>
          </div>
        </div>`;
        html += `</div>`;
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

  // 筛选下拉：状态 + 排序归到一个 popover（参考 multica issues-header）
  const _sortIcon = _tasksSortOrder === 'desc' ? '↓' : '↑';
  const _filterActive = _taskFilterStatuses.length > 0 || _taskFilterAssignees.length > 0;
  // 状态选项（与看板/列表一致）
  const _statusOpts = [
    {k:'pending', lbl: I18N.t('tasks.status.tag_pending') ? I18N.t('tasks.status.tag_pending').replace(/<[^>]*>/g,'') : '待处理'},
    {k:'in_progress', lbl: I18N.t('tasks.status.tag_processing') ? I18N.t('tasks.status.tag_processing').replace(/<[^>]*>/g,'') : '处理中'},
    {k:'pending_review', lbl: I18N.t('tasks.status.tag_pending_review')||'待审核'},
    {k:'done', lbl: I18N.t('tasks.status.tag_completed') ? I18N.t('tasks.status.tag_completed').replace(/<[^>]*>/g,'') : '已完成'},
    {k:'cancelled', lbl: I18N.t('tasks.status.tag_cancelled')||'已取消'},
  ];
  const _assignees = [...new Set(_tasksAll.map(t => t.assignee).filter(Boolean))].sort();
  const _filterPanel = `
    <div style="position:relative;display:inline-flex;align-items:center;flex-shrink:0">
      <button class="tsk-tab" onclick="_toggleFilterPopover()" style="border:1px solid var(--b);border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer;background:var(--bg);display:inline-flex;align-items:center;gap:4px">
        <span style="font-size:12px">⌥</span> ${I18N.t('tasks.filter')||'筛选'}
        ${_filterActive ? '<span style="width:6px;height:6px;border-radius:50%;background:#3b82f6"></span>' : ''}
      </button>
      <div id="taskFilterPopover" style="display:none;position:absolute;top:calc(100% + 4px);left:0;background:var(--c,#fff);border:0.5px solid var(--b,#e5e5ea);border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,.1);padding:12px;min-width:220px;max-height:440px;overflow-y:auto;z-index:50;font-family:-apple-system,BlinkMacSystemFont,sans-serif">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:6px;border-bottom:0.5px solid var(--b,#e8e8ed)">
          <span style="font-size:11px;font-weight:600;color:var(--t1,#1d1d1f)">${I18N.t('tasks.filter')||'筛选'}</span>
          <button onclick="_closeFilterPopover()" style="border:none;background:none;cursor:pointer;font-size:14px;color:var(--t3,#86868b);padding:2px 6px;border-radius:6px;line-height:1" onmouseover="this.style.background='rgba(0,0,0,.05)'" onmouseout="this.style.background='none'">✕</button>
        </div>
        <div onclick="_toggleFilterSection('status')" style="font-size:10px;font-weight:600;color:var(--t3,#86868b);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;cursor:pointer;display:flex;align-items:center;justify-content:space-between">${I18N.t('tasks.filter_status')||'状态'}<span id="_filterStatusArrow" style="font-size:10px">▸</span></div>
        <div id="_filterStatusBody" style="display:none;flex-direction:column;gap:3px;margin-bottom:12px">
          <label style="display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:6px;cursor:pointer;font-size:12px;color:var(--t3,#86868b)" onclick="_taskFilterStatuses=[];_taskPage=1;_render()">
            <span style="width:14px;height:14px;border-radius:4px;border:1.5px solid var(--b,#d0d7de);display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff"></span>
            ${I18N.t('tasks.tab.all')||'全部'} <span style="font-size:11px">${cntAll}</span>
          </label>
          ${_statusOpts.map(o => {
            const cnt = _tasksAll.filter(t => _mapStatusL(t) === o.k).length;
            const chk = _taskFilterStatuses.includes(o.k);
            return `<label style="display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:6px;cursor:pointer;font-size:12px;${chk?'background:rgba(59,130,246,.06)':''}" onclick="_toggleFilterStatus('${o.k}')">
              <span style="width:14px;height:14px;border-radius:4px;border:1.5px solid ${chk?'#3b82f6':'var(--b,#d0d7de)'};${chk?'background:#3b82f6':''};display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff">${chk?'✓':''}</span>
              ${o.lbl} <span style="color:var(--t3,#86868b);font-size:11px">${cnt}</span>
            </label>`;
          }).join('')}
        </div>
        <div onclick="_toggleFilterSection('creator')" style="font-size:10px;font-weight:600;color:var(--t3,#86868b);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;cursor:pointer;display:flex;align-items:center;justify-content:space-between">${I18N.t('tasks.filter_creator')||'创建人'}<span id="_filterCreatorArrow" style="font-size:10px">▸</span></div>
        <div id="_filterCreatorBody" style="display:none;flex-direction:column;gap:3px;max-height:160px;overflow-y:auto;margin-bottom:12px">
          <label style="display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:6px;cursor:pointer;font-size:12px;color:var(--t3,#86868b)" onclick="_taskFilterAssignees=[];_taskPage=1;loadTasks()">
            <span style="width:14px;height:14px;border-radius:4px;border:1.5px solid var(--b,#d0d7de);display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff"></span>
            ${I18N.t('tasks.tab.all')||'全部'} <span style="font-size:11px">${cntAll}</span>
          </label>
          ${_assignees.map(a => {
            const cnt = _tasksAll.filter(t => t.assignee === a).length;
            const chk = _taskFilterAssignees.includes(a);
            const safeA = a.replace(/'/g, "\\'");
            return `<label style="display:flex;align-items:center;gap:8px;padding:4px 6px;border-radius:6px;cursor:pointer;font-size:12px;${chk?'background:rgba(59,130,246,.06)':''}" onclick="_toggleFilterAssignee('${safeA}')">
              <span style="width:14px;height:14px;border-radius:4px;border:1.5px solid ${chk?'#3b82f6':'var(--b,#d0d7de)'};${chk?'background:#3b82f6':''};display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff">${chk?'✓':''}</span>
              ${a} <span style="color:var(--t3,#86868b);font-size:11px">${cnt}</span>
            </label>`;
          }).join('')}
        </div>
        <button onclick="toggleTasksSort()" style="width:100%;padding:6px 8px;border:0.5px solid var(--b,#e5e5ea);border-radius:8px;font-size:11px;cursor:pointer;background:var(--bg);color:var(--t1);display:flex;align-items:center;justify-content:center;gap:4px">${I18N.t('tasks.sort.time')||'时间排序'} ${_sortIcon}</button>
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

  // 列表模式状态信息（_mapStatusL 已提为全局函数）
  const _lStatusInfo = {
    pending: {ic:'#', cls:'tsk-tag-p', rIc:'tsk-ic-p', text:I18N.t('tasks.status.tag_pending')||'<b></b>待处理'},
    in_progress: {ic:'⏳', cls:'tsk-tag-pr', rIc:'tsk-ic-pr', text:I18N.t('tasks.status.tag_processing')||'<b></b>处理中'},
    awaiting_input: {ic:'?', cls:'tsk-tag-pr', rIc:'tsk-ic-pr', text:I18N.t('tasks.status.tag_awaiting_input')||'待澄清'},
    pending_review: {ic:'◉', cls:'tsk-tag-pr', rIc:'tsk-ic-pr', text:I18N.t('tasks.status.tag_pending_review')||'待审核'},
    done: {ic:'✓', cls:'tsk-tag-d', rIc:'tsk-ic-d', text:I18N.t('tasks.status.tag_completed')||'<b></b>已完成'},
    cancelled: {ic:'✕', cls:'tsk-tag-d', rIc:'tsk-ic-d', text:I18N.t('tasks.status.tag_cancelled')||'已取消'},
  };

  const rowHtml = (t) => {
    const ms = _mapStatusL(t);
    const si = _lStatusInfo[ms] || _lStatusInfo.pending;
    const sel = _taskSel && _taskSel.id === t.id ? ' tsk-row-sel' : '';
    const _assigneeHtml = t.assignee ? ` · 👤 ${t.assignee}` : '';
    return `<div class="tsk-row${sel}" onclick="_taskSel=window._tasksById[${t.id}];_render()" ondblclick="openTaskConversation(window._tasksById[${t.id}])">
      <div class="tsk-r-ic ${si.rIc}">${si.ic}</div>
      <div class="tsk-r-main">
        <div class="tsk-r-title">Issue #${t.issue_number} · ${t.title || ''}</div>
        <div class="tsk-r-meta">⏱ ${fmtLocalTime(t.created_at).substring(0,16)} · ${t.repo_key.replace(/^(github|gitcode):/, '')}${_assigneeHtml}</div>
      </div>
      <span class="tsk-tag ${si.cls}">${si.text}</span>
    </div>`;
  };

  let listHtml = '';
  if (_tasksMode === 'time') {
    listHtml = pageRows.map(rowHtml).join('');
  } else {
    // 按新状态分区
    const pageByStatus = {};
    pageRows.forEach(t => { const ms = _mapStatusL(t); (pageByStatus[ms] = pageByStatus[ms] || []).push(t); });
    const _secLabels = {
      pending: I18N.t('tasks.section.pending')||'待处理',
      in_progress: I18N.t('tasks.section.processing')||'进行中',
      awaiting_input: I18N.t('tasks.status.tag_awaiting_input')||'待澄清',
      pending_review: I18N.t('tasks.status.tag_pending_review')||'待审核',
      done: I18N.t('tasks.section.completed')||'已完成',
      cancelled: I18N.t('tasks.status.tag_cancelled')||'已取消',
    };
    ['pending','in_progress','awaiting_input','pending_review','done','cancelled'].forEach(ms => {
      if (pageByStatus[ms] && pageByStatus[ms].length > 0) {
        listHtml += `<div class="tsk-list-sec">${_secLabels[ms]} <span class="tsk-sec-n">${pageByStatus[ms].length}</span></div>` +
          pageByStatus[ms].map(rowHtml).join('');
      }
    });
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
      <div style="display:flex;gap:0;margin-top:8px;border-bottom:0.5px solid var(--b,#e8e8ed)">
        <button class="tsk-tab${_tasksViewMode==='mine'?' tsk-tab-act':''}" onclick="_switchTasksViewMode('mine')" style="border:none;border-bottom:2px solid ${_tasksViewMode==='mine'?'#0071e3':'transparent'};border-radius:0;padding:6px 16px;font-size:13px;font-weight:600;color:${_tasksViewMode==='mine'?'#fff':'#3a3a3c'};background:${_tasksViewMode==='mine'?'#0071e3':'transparent'};cursor:pointer;font-family:-apple-system,BlinkMacSystemFont,sans-serif">${I18N.t('tasks.view_mine')||'我的需求'}</button>
        <button class="tsk-tab${_tasksViewMode==='repo_all'?' tsk-tab-act':''}" onclick="_switchTasksViewMode('repo_all')" style="border:none;border-bottom:2px solid ${_tasksViewMode==='repo_all'?'#0071e3':'transparent'};border-radius:0;padding:6px 16px;font-size:13px;font-weight:600;color:${_tasksViewMode==='repo_all'?'#fff':'#3a3a3c'};background:${_tasksViewMode==='repo_all'?'#0071e3':'transparent'};cursor:pointer;font-family:-apple-system,BlinkMacSystemFont,sans-serif">${I18N.t('tasks.view_repo_all')||'仓库全部'}</button>
      </div>
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
      <div class="tsk-detail" style="overflow-y:auto;height:100%;position:relative;top:0">${detailHtml}${_taskSel ? `<div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-start;margin-top:12px"><button class="tsk-btn tsk-btn-ghost" style="padding:4px 10px;border-radius:8px;font-size:11px" onclick="window.open('${_vcsUrl(_taskSel.repo_key, '/issues/' + _taskSel.issue_number)}','_blank')">${I18N.t('tasks.detail.view_issue')}</button><button class="tsk-btn tsk-btn-ghost" style="padding:4px 10px;border-radius:8px;font-size:11px" onclick="openTaskConversation(window._tasksById[${_taskSel.id}])">${I18N.t('tasks.detail.go_conversation')}</button>${_renderTaskActionsInline(_taskSel)}</div>` : ''}</div>
    </div>`}
  </div>`;

  if (_taskFilterPopoverOpen) _applyFilterPopoverState();

  // 加载部署 URL 到需求中心顶部
  _loadDeployLink();

  // 建立 id → task 映射供行点击
  window._tasksById = {};
  _tasksAll.forEach(t => { window._tasksById[t.id] = t; });
}

function _renderAgentStatusCard(t) {
  // Issue #126: 渲染各 Agent 节点实时状态（与菜单流水线一致，数据源为 events 表各 Agent 最新事件 status）
  var statuses = t.agent_statuses || [];
  if (statuses.length === 0) return '';
  var _cardStyle = 'background:rgba(0,0,0,.02);border-radius:14px;border:0.5px solid var(--b,#e8e8ed);padding:14px 16px;margin-bottom:12px';
  var _cardTitle = 'font-size:12px;font-weight:600;color:var(--t3,#86868b);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;font-family:-apple-system,BlinkMacSystemFont,sans-serif';
  // 注入脉冲动画 CSS（仅一次）
  if (!document.getElementById('_grapeAgentPulseCSS')) {
    var style = document.createElement('style');
    style.id = '_grapeAgentPulseCSS';
    style.textContent = '@keyframes grapeAgentPulse{0%,100%{transform:scale(1);box-shadow:0 0 0 0 rgba(59,130,246,.3)}50%{transform:scale(1.08);box-shadow:0 0 0 5px rgba(59,130,246,0)}}';
    document.head.appendChild(style);
  }
  var _statusMap = {
    pending: {color: '#86868b', bg: 'rgba(0,0,0,.06)', icon: '○', label: I18N.t('tasks.agent_status.pending')||'待执行'},
    running: {color: '#3b82f6', bg: '#3b82f6', icon: '●', label: I18N.t('tasks.agent_status.running')||'执行中'},
    completed: {color: '#34c759', bg: '#34c759', icon: '✓', label: I18N.t('tasks.agent_status.completed')||'已完成'},
    failed: {color: '#ef4444', bg: '#ef4444', icon: '✕', label: I18N.t('tasks.agent_status.failed')||'失败'},
    cancelled: {color: '#86868b', bg: 'rgba(0,0,0,.06)', icon: '⊘', label: I18N.t('tasks.agent_status.cancelled')||'已取消'},
  };
  var html = '<div style="' + _cardStyle + '">';
  html += '<div style="' + _cardTitle + '">' + (I18N.t('tasks.agent_status.title')||'Agent 节点状态') + '</div>';
  // 节点进度条
  html += '<div style="display:flex;align-items:center;gap:0;padding:10px;background:rgba(0,0,0,.02);border-radius:10px">';
  statuses.forEach(function(s, i) {
    var si = _statusMap[s.status] || _statusMap.pending;
    var isRunning = s.status === 'running';
    var isCompleted = s.status === 'completed';
    var isFailed = s.status === 'failed';
    var pulseStyle = isRunning ? ';animation:grapeAgentPulse 1.2s ease-in-out infinite' : '';
    // 节点点击：有 latest_seq 则跳转事件详情
    var seq = s.latest_seq;
    var clickAttr = (isRunning || isCompleted || isFailed) && seq ? ' onclick="window._jumpToNodeEvent(' + seq + ')" style="cursor:pointer"' : '';
    html += '<div style="display:flex;flex-direction:column;align-items:center;gap:4px;flex:1;min-width:0"' + clickAttr + '>';
    html += '<div style="width:24px;height:24px;border-radius:50%;background:' + si.bg + ';display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:600;flex-shrink:0' + pulseStyle + '">' + si.icon + '</div>';
    html += '<span style="font-size:9px;color:' + si.color + ';font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;font-family:-apple-system,BlinkMacSystemFont,sans-serif">' + (s.label || s.agent || '') + '</span>';
    html += '</div>';
    if (i < statuses.length - 1) {
      var nextCompleted = (statuses[i+1].status_num >= 2) || isCompleted;
      var lineCol = isCompleted ? '#34c759' : isFailed ? '#ef4444' : 'rgba(0,0,0,.1)';
      html += '<div style="height:2px;flex:0 0 14px;background:' + lineCol + ';border-radius:1px;margin-top:-12px"></div>';
    }
  });
  html += '</div>';
  html += '</div>';
  return html;
}

function _detailHtml(t) {
  const isDone = t.status === 'completed';
  const isProc = t.status === 'processing';
  const isCancelled = t.status === 'cancelled';
  const reasonText = t.completed_reason === 'issue_closed' ? I18N.t('tasks.status.reason_issue_closed') : t.completed_reason === 'reassigned' ? I18N.t('tasks.status.reason_reassigned') : t.completed_reason === 'bot_takeover' ? I18N.t('tasks.status.reason_bot_takeover') : t.completed_reason === 'user_cancelled' ? (I18N.t('tasks.cancel_task')||'已取消') : (t.completed_reason || '');
  const tagText = isCancelled ? (I18N.t('tasks.status.tag_cancelled')||'已取消') : isDone ? (I18N.t('tasks.status.detail_completed') + reasonText) : isProc ? I18N.t('tasks.status.detail_processing') : I18N.t('tasks.status.detail_pending');
  var sceneName = t.scene_id ? (t.scene_name || t.scene_id) : '—';
  var sceneBadge = t.scene_source === 'vod' ? '' : (t.scene_id && t.scene_source === 'repo_default' ? ` <span style="font-size:10px;color:var(--t3)">${I18N.t('tasks.scene_default_badge')}</span>` : '');
  var subtaskBadge = t.is_subtask_mode ? `<span class="tg tg-q" style="font-size:10px">${I18N.t('tasks.multi_task_badge')}</span>` : `<span class="ts t3" style="font-size:10px">${I18N.t('tasks.single_task_badge')}</span>`;
  var parentIssue = t.parent_issue_number ? ' · ' + I18N.t('tasks.parent_task') + ' #' + t.parent_issue_number : '';
  const _stage = t.stage || '';
  const _stageLabel = {vod:'VOD',issue:'ISSUE',coding:'Coding',qa:'QA',sre:'SRE',pending_review:I18N.t('tasks.status.tag_pending_review')||'待审核',done:I18N.t('tasks.status.tag_completed')||'已完成','':'—'}[_stage] || _stage || '—';

  // 苹果风格卡片容器
  const _cardStyle = 'background:rgba(0,0,0,.02);border-radius:14px;border:0.5px solid var(--b,#e8e8ed);padding:14px 16px;margin-bottom:12px';
  const _cardTitle = 'font-size:12px;font-weight:600;color:var(--t3,#86868b);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;font-family:-apple-system,BlinkMacSystemFont,sans-serif';
  const _rowStyle = 'display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:0.5px solid rgba(0,0,0,.04)';
  const _labelStyle = 'font-size:12px;color:var(--t3,#86868b);font-weight:400';
  const _valueStyle = 'font-size:13px;color:var(--t1,#1d1d1f);font-weight:500;text-align:right;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';

  return `
    <!-- 任务详情卡片（统一） -->
    <div style="${_cardStyle}position:relative">
      <div style="${_cardTitle}">${I18N.t('tasks.detail_title')||'需求详情'}</div>
      <div style="${_rowStyle}"><span style="${_labelStyle}">${I18N.t('tasks.detail.status')}</span><b style="${_valueStyle};color:${isDone?'#34c759':isCancelled?'#86868b':isProc?'#3b82f6':'#86868b'}">${tagText}</b></div>
      <div style="${_rowStyle}"><span style="${_labelStyle}">${I18N.t('tasks.detail.task_id')}</span><b style="${_valueStyle}">#${t.id}</b></div>
      <div style="${_rowStyle}"><span style="${_labelStyle}">${I18N.t('tasks.detail.assignee')||'创建人'}</span><b style="${_valueStyle}">${t.assignee || '—'}</b></div>
      <div style="${_rowStyle}"><span style="${_labelStyle}">Issue</span><b style="${_valueStyle}"><a href="https://${t.repo_key.startsWith('github:') ? 'github.com' : 'gitcode.com'}/${t.repo_key.replace(/^(github|gitcode):/, '')}/issues/${t.issue_number}" target="_blank" rel="noopener" style="color:var(--a,#0071e3);text-decoration:none;font-weight:500">#${t.issue_number} ↗</a></b></div>
      <div style="${_rowStyle}"><span style="${_labelStyle}">${I18N.t('tasks.detail.repo')}</span><b style="${_valueStyle}">${t.repo_key.replace(/^(github|gitcode):/, '')}</b></div>
      <div style="${_rowStyle}"><span style="${_labelStyle}">Stage</span><b style="${_valueStyle}">${_stageLabel}</b></div>
      <div style="${_rowStyle}"><span style="${_labelStyle}">${I18N.t('tasks.pipeline_scene')||'流水线场景'}</span><b style="${_valueStyle}">${sceneName}${sceneBadge}</b></div>
      <div style="${_rowStyle}"><span style="${_labelStyle}">${I18N.t('tasks.task_mode')}</span><b style="${_valueStyle}">${subtaskBadge}${parentIssue}</b></div>
      <div style="${_rowStyle}"><span style="${_labelStyle}">${I18N.t('tasks.detail.created_at')}</span><b style="${_valueStyle}">${fmtLocalTime(t.created_at).substring(0,16)}</b></div>
      ${t.completed_at ? `<div style="${_rowStyle}"><span style="${_labelStyle}">${I18N.t('tasks.detail.completed_at')}</span><b style="${_valueStyle}">${fmtLocalTime(t.completed_at).substring(0,16)}</b></div>` : ''}
      ${t.task_description ? `<div style="${_rowStyle};border-bottom:none;flex-direction:column;align-items:flex-start"><span style="${_labelStyle};margin-bottom:4px">${I18N.t('tasks.detail.no_description')}</span><div style="font-size:13px;color:var(--t2,#3a3a3c);line-height:1.5;white-space:pre-wrap;width:100%">${t.task_description}</div></div>` : ''}
    </div>
    <div style="${_cardStyle}">
      <div style="${_cardTitle}">附件</div>
      <div id="_taskAttachmentsList" style="min-height:30px"><div style="font-size:12px;color:var(--t3,#86868b)">加载中...</div></div>
      ${t.status === 'active' ? `<div style="display:flex;gap:6px;margin-top:10px;align-items:center">
        <label style="cursor:pointer;display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:8px;border:0.5px solid rgba(0,113,227,.3);font-size:12px;color:#0071e3" onmouseover="this.style.background='rgba(0,113,227,.06)'" onmouseout="this.style.background='transparent'">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          上传附件
          <input type="file" style="display:none" onchange="_onTaskAttachSelect(this,${t.id})">
        </label>
      </div>` : ''}
    </div>`;
}

function _loadTaskAttachments(taskId) {
  API.get('/issues/' + taskId + '/attachments').then(function(data) {
    var list = document.getElementById('_taskAttachmentsList');
    if (!list) return;
    var atts = data.attachments || [];
    if (atts.length === 0) {
      list.innerHTML = '<div style="font-size:12px;color:var(--t3,#86868b)">暂无附件</div>';
      return;
    }
    list.innerHTML = atts.map(function(a) {
      var del = '<button onclick="_deleteTaskAttach(' + taskId + ',\'' + a.filename + '\')" style="margin-left:6px;font-size:11px;color:#ff3b30;background:none;border:none;cursor:pointer;padding:2px 4px">删除</button>';
      if (a.is_image) {
        return '<div style="display:flex;align-items:center;padding:4px 0;border-bottom:0.5px solid rgba(0,0,0,.04);font-size:12px">'
          + '<img src="' + a.url + '" style="width:32px;height:32px;object-fit:cover;border-radius:6px;margin-right:8px">'
          + '<a href="' + a.url + '" target="_blank" style="color:#0071e3;text-decoration:none;flex:1">' + a.filename + '</a>'
          + '<span style="color:var(--t3,#86868b);font-size:11px">' + _formatSize(a.size) + '</span>'
          + del + '</div>';
      }
      return '<div style="display:flex;align-items:center;padding:4px 0;border-bottom:0.5px solid rgba(0,0,0,.04);font-size:12px">'
        + '<span style="margin-right:8px">📎</span>'
        + '<a href="' + a.url + '" target="_blank" style="color:#0071e3;text-decoration:none;flex:1">' + a.filename + '</a>'
        + '<span style="color:var(--t3,#86868b);font-size:11px">' + _formatSize(a.size) + '</span>'
        + del + '</div>';
    }).join('');
  }).catch(function() {
    var list = document.getElementById('_taskAttachmentsList');
    if (list) list.innerHTML = '<div style="font-size:12px;color:var(--t3,#86868b)">加载失败</div>';
  });
}

function _formatSize(bytes) {
  if (bytes < 1024) return bytes + 'B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + 'KB';
  return (bytes/1024/1024).toFixed(1) + 'MB';
}

function _onTaskAttachSelect(fileInput, taskId) {
  if (!fileInput.files || !fileInput.files[0]) return;
  var formData = new FormData();
  formData.append('file', fileInput.files[0]);
  var list = document.getElementById('_taskAttachmentsList');
  if (list) list.innerHTML = '<div style="font-size:12px;color:var(--t3,#86868b)">上传中...</div>';
  fetch('/api/issues/' + taskId + '/attachments', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + localStorage.getItem('token') },
    body: formData
  }).then(function(r) { return r.json(); }).then(function() {
    _loadTaskAttachments(taskId);
  }).catch(function(e) {
    if (list) list.innerHTML = '<div style="font-size:12px;color:#ff3b30">上传失败: ' + e.message + '</div>';
    _loadTaskAttachments(taskId);
  });
  fileInput.value = '';
}

function _deleteTaskAttach(taskId, filename) {
  if (!confirm('确认删除附件？')) return;
  // Issue #175: 走 API.js 获得超时保护
  API.del('/issues/' + taskId + '/attachments/' + filename).then(function() {
    _loadTaskAttachments(taskId);
  }).catch(function(e) {
    if (e && e.name === 'AbortError' && e.silent) return;
    app.showToast(I18N.t('tasks.operation_failed', {msg: (e && e.detail && e.detail.message || e && e.message || String(e))}));
  });
}

// ===== 创建弹窗附件上传 =====
let _createAttachments = [];

function _onCreateAttachSelect(fileInput) {
  if (!fileInput.files || !fileInput.files[0]) return;
  var repo = app.currentRepo();
  var repoKey = repo ? (repo.repoKey || repo.fullName || '') : '';
  var file = fileInput.files[0];
  var formData = new FormData();
  formData.append('file', file);
  var container = document.getElementById('tcCreateAttachments');
  var loading = document.createElement('div');
  loading.style.cssText = 'font-size:11px;color:#888;padding:4px 8px;background:rgba(0,113,227,.06);border-radius:6px';
  loading.textContent = '上传中...';
  container.appendChild(loading);
  fetch('/api/issues/attachments/pre-upload?repo_key=' + encodeURIComponent(repoKey), {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + localStorage.getItem('token') },
    body: formData
  }).then(function(r) { return r.json(); }).then(function(d) {
    loading.remove();
    if (d.error) { alert('上传失败: ' + d.error); return; }
    _createAttachments.push({filename: d.filename, safe_name: d.safe_name, size: d.size, url: d.url});
    _renderCreateAttachments();
  }).catch(function(e) {
    loading.remove();
    alert('上传失败: ' + e.message);
  });
  fileInput.value = '';
}

function _renderCreateAttachments() {
  var container = document.getElementById('tcCreateAttachments');
  if (!container) return;
  if (_createAttachments.length === 0) { container.innerHTML = ''; return; }
  container.innerHTML = _createAttachments.map(function(a, i) {
    return '<div style="display:inline-flex;align-items:center;gap:4px;padding:3px 8px;background:rgba(0,113,227,.06);border-radius:6px;font-size:11px">'
      + '<span>📎 ' + a.filename + '</span>'
      + '<span style="color:#888">' + _formatSize(a.size) + '</span>'
      + '<button onclick="_removeCreateAttach(' + i + ')" style="margin-left:2px;color:#ff3b30;background:none;border:none;cursor:pointer;font-size:11px;padding:0 2px">✕</button>'
      + '</div>';
  }).join('');
}

function _removeCreateAttach(idx) {
  var a = _createAttachments[idx];
  if (!a) return;
  var repo = app.currentRepo();
  var repoKey = repo ? (repo.repoKey || repo.fullName || '') : '';
  fetch('/api/issues/attachments/pre-upload/' + a.safe_name + '?repo_key=' + encodeURIComponent(repoKey), {
    method: 'DELETE',
    headers: { 'Authorization': 'Bearer ' + localStorage.getItem('token') }
  }).then(function() {
    _createAttachments.splice(idx, 1);
    _renderCreateAttachments();
  }).catch(function() {
    _createAttachments.splice(idx, 1);
    _renderCreateAttachments();
  });
}

function _renderTaskActionsInline(t) {
  const _status = t.status || '';
  let html = '';
  // Issue #131: 任意阶段均可确认完成（cancelled/completed 不显示）
  if (_status !== 'completed' && _status !== 'cancelled') {
    html += `<button class="tsk-btn tsk-btn-primary" onclick="confirmTaskDone(${t.id},event)" style="padding:8px 14px;border-radius:10px;font-size:12px;font-weight:600">✓ ${I18N.t('tasks.confirm_done')}</button>`;
  }
  // 取消任务
  if (_status !== 'completed' && _status !== 'cancelled') {
    html += `<button class="tsk-btn tsk-btn-ghost" onclick="cancelTask(${t.id},event)" style="padding:8px 14px;border-radius:10px;font-size:12px;color:#d32f2f">✕ ${I18N.t('tasks.cancel_task')}</button>`;
  }
  // 恢复任务
  if (_status === 'cancelled') {
    html += `<button class="tsk-btn tsk-btn-primary" onclick="resumeTask(${t.id})" style="padding:8px 14px;border-radius:10px;font-size:12px;font-weight:600">↻ ${I18N.t('tasks.resume_task')}</button>`;
  }
  return html;
}

function _renderTaskActionsBar(t) {
  // 已废弃——按钮合并到 _detailHtml 底部行
  return '';
}

window._grapeConfirm = function(opts, event) {
  const {title, message, confirmText, cancelText, onConfirm, danger} = opts;
  let posX = window.innerWidth / 2, posY = window.innerHeight / 2;
  if (event && event.clientX) { posX = event.clientX; posY = event.clientY; }
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.2);backdrop-filter:blur(3px);z-index:9999;opacity:0;transition:opacity .2s';
  const dlg = document.createElement('div');
  const dlgW = 260;
  let dlgX = posX - dlgW / 2;
  let dlgY = posY - 50;
  if (dlgX < 12) dlgX = 12;
  if (dlgX + dlgW > window.innerWidth - 12) dlgX = window.innerWidth - dlgW - 12;
  if (dlgY < 12) dlgY = 12;
  if (dlgY > window.innerHeight - 160) dlgY = window.innerHeight - 160;
  dlg.style.cssText = `position:fixed;left:${dlgX}px;top:${dlgY}px;background:var(--c,#fff);border-radius:12px;padding:14px 18px;max-width:${dlgW}px;width:90%;box-shadow:0 6px 30px rgba(0,0,0,.12);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",sans-serif;transform:scale(1.05);transition:transform .2s cubic-bezier(.4,0,.2,1)`;
  dlg.innerHTML = `
    <div style="font-size:13px;font-weight:600;color:var(--t1,#1d1d1f);margin-bottom:4px;text-align:center">${title||(I18N.t('tasks.confirm')||'确认')}</div>
    <div style="font-size:11px;color:var(--t2,#3a3a3c);line-height:1.35;text-align:center;margin-bottom:12px;white-space:pre-line">${message||''}</div>
    <div style="display:flex;gap:6px">
      <button id="_grapeCancel" style="flex:1;padding:6px 0;border-radius:8px;border:none;background:rgba(0,0,0,.06);font-size:11px;font-weight:500;color:var(--t1,#1d1d1f);cursor:pointer;font-family:inherit">${cancelText||(I18N.t('tasks.cancel')||'取消')}</button>
      <button id="_grapeOK" style="flex:1;padding:6px 0;border-radius:8px;border:none;background:${danger?'#ff3b30':'#0071e3'};font-size:11px;font-weight:600;color:#fff;cursor:pointer;font-family:inherit">${confirmText||(I18N.t('tasks.confirm')||'确认')}</button>
    </div>`;
  overlay.appendChild(dlg);
  document.body.appendChild(overlay);
  requestAnimationFrame(() => { overlay.style.opacity = '1'; dlg.style.transform = 'scale(1)'; });
  function _close() { overlay.style.opacity = '0'; dlg.style.transform = 'scale(1.05)'; setTimeout(() => overlay.remove(), 200); }
  document.getElementById('_grapeCancel').onclick = _close;
  document.getElementById('_grapeOK').onclick = function() { _close(); if (onConfirm) setTimeout(onConfirm, 100); };
  overlay.onclick = function(e) { if (e.target === overlay) _close(); };
};

// Issue #175 v3: 遮罩整个 #pageContent，成功后保持详情页视图，超时/错误自动恢复
function _withTaskLoading(taskId, apiCall, successMsg, errorFn) {
  var pc = document.getElementById('pageContent');
  var overlay = null;
  if (pc) {
    overlay = document.createElement('div');
    // position:fixed 覆盖全屏（不受可滚动容器的 overflow-y:auto 限制）
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(255,255,255,.7);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;z-index:9998';
    var spinner = document.createElement('div');
    spinner.style.cssText = 'width:24px;height:24px;border:3px solid var(--b,rgba(0,0,0,.1));border-top-color:#0071e3;border-radius:50%;animation:_grapeSpin .7s linear infinite';
    // 注入 spinner 动画（仅一次）
    if (!document.getElementById('_grapeSpinCSS')) {
      var style = document.createElement('style');
      style.id = '_grapeSpinCSS';
      style.textContent = '@keyframes _grapeSpin{to{transform:rotate(360deg)}}';
      document.head.appendChild(style);
    }
    var text = document.createElement('div');
    text.style.cssText = 'font-size:14px;font-weight:500;color:var(--t3,#86868b)';
    text.textContent = I18N.t('tasks.processing');
    overlay.appendChild(spinner);
    overlay.appendChild(text);
    pc.appendChild(overlay);
  }
  apiCall().then(function() {
    if (window._currentView !== 'tasks') { if (overlay) overlay.remove(); return; }  // 导航离开不弹 toast
    if (overlay) overlay.remove();
    app.showToast(successMsg);
    if (taskId && window._currentDetailTask) window._pendingTaskDetailId = taskId;
    loadTasks();
  }).catch(function(e) {
    if (e && e.name === 'AbortError' && e.silent) { if (overlay) overlay.remove(); return; }  // 导航离开时静默
    if (window._currentView !== 'tasks') { if (overlay) overlay.remove(); return; }  // 导航离开不弹 toast
    if (overlay) overlay.remove();
    app.showToast(errorFn(e));
    loadTasks();  // 超时或错误后刷新页面保证可用
  });
}

window.confirmTaskDone = function(taskId, event) {
  _grapeConfirm({
    title: I18N.t('tasks.confirm_done'),
    message: I18N.t('tasks.confirm_done_message'),
    confirmText: I18N.t('tasks.confirm_done'),
    onConfirm: function() {
      _withTaskLoading(
        taskId,
        function() { return API.post('/issues/' + taskId + '/confirm-done', {confirmation: 'user_confirmed'}); },
        I18N.t('tasks.task_done_toast'),
        function(e) { return I18N.t('tasks.operation_failed', {msg: (e.detail?.message || e.message)}); }
      );
    }
  }, event);
};

window.cancelTask = function(taskId, event) {
  _grapeConfirm({
    title: I18N.t('tasks.cancel_task'),
    message: I18N.t('tasks.cancel_task_message'),
    confirmText: I18N.t('tasks.cancel_task'),
    danger: true,
    onConfirm: function() {
      _withTaskLoading(
        taskId,
        function() { return API.post('/issues/' + taskId + '/cancel'); },
        I18N.t('tasks.task_cancelled_toast'),
        function(e) { return I18N.t('tasks.operation_failed', {msg: (e.detail?.message || e.message)}); }
      );
    }
  }, event);
};

window.resumeTask = function(taskId) {
  _withTaskLoading(
    taskId,
    function() { return API.post('/issues/' + taskId + '/resume'); },
    I18N.t('tasks.task_resumed_toast'),
    function(e) { return I18N.t('tasks.operation_failed', {msg: (e.detail?.message || e.message)}); }
  );
};

function openTaskConversation(t) {
  // 跳转 chat agent：始终用 issue:N:repoKey 格式，由 chat.js 调 ensure-issue
  // 查找或新建会话（会话被删除后旧 conversation_id 仍在 task 表里，直接传 conv_id
  // 会在会话列表找不到而回退到第一个会话，不会触发重建）
  const issueNum = t && t.issue_number ? String(t.issue_number) : '';
  const repoKey = t && t.repo_key ? t.repo_key : '';
  const param = issueNum && repoKey ? 'issue:' + issueNum + ':' + repoKey : (t && t.conversation_id || undefined);
  const go = () => app.nav('chat', param);
  if (t && t.id && t.status === 'active') {
    // Issue #175: 给按钮加 loading 反馈，防止用户以为没点到反复点击
    var btn = document.querySelector('[onclick*="openTaskConversation(' + t.id + ')"]');
    if (btn) { btn.disabled = true; btn.style.opacity = '0.5'; }
    var _restoreBtn = function() { if (btn) { btn.disabled = false; btn.style.opacity = '1'; } };
    API.post('/issues/' + t.id + '/start').then(d => {
      if (window._tasksById[t.id] && d && d.status) { window._tasksById[t.id].status = d.status; }
      _restoreBtn();
      go();
    }).catch(function() {
      _restoreBtn();
      go();
    });
  } else {
    go();
  }
}

// ===== 新建需求（同步建 GitCode Issue + 既有流程建档；责任人 = General Settings Account）=====
let _taskCreateModal = null;

function openTaskCreateModal() {
  const repo = app.currentRepo();
  const defaultRepo = repo ? repo.fullName : '';
  if (_taskCreateModal) _taskCreateModal.remove();
  _createAttachments = [];
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // 异步加载可用场景列表
  let _sceneOptions = '<option value="">' + I18N.t('tasks.create.scene_vod_auto') + '</option>';

  // Issue #178: 创建人只读字段——当前登录用户
  let _currentUsername = '';
  try {
    _currentUsername = (JSON.parse(localStorage.getItem('user') || '{}').username) || '';
  } catch(e) { _currentUsername = ''; }

  const ov = document.createElement('div');
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;display:flex;align-items:center;justify-content:center;';
  ov.innerHTML = `<div style="width:min(520px,92vw);background:#fff;border:1px solid #d6dae2;border-radius:10px;padding:18px 20px;box-shadow:0 10px 40px rgba(0,0,0,.3)">
    <h3 style="margin:0 0 14px;font-size:15px;color:#111">${I18N.t('tasks.create.modal_title')}</h3>
    <div style="display:flex;flex-direction:column;gap:10px;font-size:13px">
      <div style="font-size:11px;color:#555">${I18N.t('tasks.create.repo_label')}<b>${esc(defaultRepo) || I18N.t('tasks.create.repo_empty')}</b></div>
      <div style="font-size:11px;color:#555">${I18N.t('tasks.create.creator_label')}<b>${esc(_currentUsername) || '—'}</b></div>
      <div style="display:flex;flex-direction:column;gap:4px;color:#333"><div style="font-size:13px">${I18N.t('tasks.create.title_label')} <span style="color:#d32f2f">*</span></div><div style="font-size:11px;color:#888">${I18N.t('tasks.create.title_hint')}</div>
        <input id="tcCreateTitle" style="padding:7px 10px;border:1px solid #ccc;border-radius:6px;background:#fff;color:#111" placeholder="${I18N.t('tasks.create.title_placeholder')}">
      </div>
      <div style="display:flex;flex-direction:column;gap:4px;color:#333"><div style="font-size:13px">${I18N.t('tasks.create.desc_label')} <span style="color:#d32f2f">*</span></div><div style="font-size:11px;color:#888">${I18N.t('tasks.create.desc_hint')}</div>
        <textarea id="tcCreateDesc" rows="4" style="padding:7px 10px;border:1px solid #ccc;border-radius:6px;background:#fff;color:#111;resize:vertical" placeholder="${I18N.t('tasks.create.desc_placeholder')}"></textarea>
      </div>
      <label style="display:flex;flex-direction:column;gap:4px;color:#333">${I18N.t('tasks.create.scene_label')} <span style="font-size:11px;color:#888">${I18N.t('tasks.create.scene_hint')}</span>
        <select id="tcCreateScene" style="padding:7px 10px;border:1px solid #ccc;border-radius:6px;background:#fff;color:#111;font-size:13px">${_sceneOptions}</select>
      </label>
      <div style="font-size:11px;color:#888">${I18N.t('tasks.create.assignee_hint')}</div>
      <div style="display:flex;flex-direction:column;gap:4px;color:#333">
        <span style="font-size:12px">附件 <span style="font-size:11px;color:#888">（可选，创建 Issue 时同步附加）</span></span>
        <div id="tcCreateAttachments" style="display:flex;flex-wrap:wrap;gap:6px;min-height:30px"></div>
        <label style="cursor:pointer;display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:8px;border:0.5px solid rgba(0,113,227,.3);font-size:12px;color:#0071e3;width:fit-content" onmouseover="this.style.background='rgba(0,113,227,.06)'" onmouseout="this.style.background='transparent'">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          添加附件
          <input type="file" style="display:none" onchange="_onCreateAttachSelect(this)">
        </label>
      </div>
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
  const repoKey = repo ? (repo.repoKey || repo.fullName || '') : '';
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
  // C2: /api/tasks/create 已废弃 → 统一走 POST /api/issues/{owner}/{repo}（对外唯一创建入口）
  var _owner = repo ? (repo.owner || '') : '';
  var _rname = repo ? (repo.name || '') : '';
  if (!_owner || !_rname) {
    var _p = String(repoKey).split('/');
    _owner = _p[0] || '';
    _rname = _p[1] || '';
  }
  API.post('/issues/' + encodeURIComponent(_owner) + '/' + encodeURIComponent(_rname),
           {repo_key: repoKey.trim(), title: title.trim(), description: desc.trim(), scene_id: sceneId,
            attachments: _createAttachments.map(function(a) { return a.safe_name; })})
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

// @deprecated 死代码——UI 无调用点。如需启用，catch 里的 alert 应改为 app.showToast。
function changeTaskStatus(taskId, status) {
  API.put('/issues/' + taskId + '/status', {status: status})
    .then(() => loadTasks())
    .catch(e => {
      const msg = (e && e.detail && e.detail.message) || (e && e.message) || String(e);
      alert(I18N.t('tasks.change_status_failed', {msg: msg}));
    });
}
