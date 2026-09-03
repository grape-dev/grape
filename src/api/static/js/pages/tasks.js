// ====== 任务中心（Account ▸ 任务中心）======
// 方案 B：列表工作流风（左列表 + 右详情面板）
let _tasksTab = 'all';        // all / active / completed
let _tasksAll = [];           // 全部任务缓存
let _taskSel = null;          // 选中的任务
let _taskQuery = '';          // 搜索关键字
let _taskPage = 1;            // 当前页码
const _TASKS_PER_PAGE = 15;   // 每页任务数
let _taskPollTimer = null;    // 场景识别轮询定时器

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
    bar.innerHTML = `<button class="tsk-btn tsk-btn-ghost" style="padding:3px 10px;font-size:11px" onclick="_deployEditing=true;_renderDeployLink()">+ 登记部署 URL</button>`;
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
  // 方案 B：拉取全部任务，前端按 tab/搜索过滤（双栏联动需要全量数据）
  API.get('/tasks/mine?status=&repo_key=' + encodeURIComponent(repoKey)).then(d => {
    _tasksAll = d.tasks || [];
    // 选中项必须在当前过滤列表内（切换 repo 后旧选中失效）
    if (_taskSel && !_tasksAll.some(t => t.id === _taskSel.id)) _taskSel = null;
    if (!_taskSel && _tasksAll.length > 0) _taskSel = _tasksAll[0];
    _render();
  }).catch(e => {
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

  // 分组列表（待处理/处理中在前，已完成在后）
  const activeRows = filtered.filter(t => t.status === 'active');
  const procRows = filtered.filter(t => t.status === 'processing');
  const doneRows = filtered.filter(t => t.status === 'completed');

  // 分页：合并分组后按页切片
  const allRows = [...activeRows, ...procRows, ...doneRows];
  const totalPages = Math.ceil(allRows.length / _TASKS_PER_PAGE);
  if (_taskPage > totalPages) _taskPage = 1;
  if (_taskPage < 1) _taskPage = 1;
  const pageStart = (_taskPage - 1) * _TASKS_PER_PAGE;
  const pageRows = allRows.slice(pageStart, pageStart + _TASKS_PER_PAGE);

  // 重新按分组拆分当前页
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
  if (pageActive.length > 0) {
    listHtml += `<div class="tsk-list-sec">${I18N.t('tasks.section.pending')} <span class="tsk-sec-n">${activeRows.length}</span></div>` +
      pageActive.map(rowHtml).join('');
  }
  if (pageProc.length > 0) {
    listHtml += `<div class="tsk-list-sec">${I18N.t('tasks.section.processing')} <span class="tsk-sec-n">${procRows.length}</span></div>` +
      pageProc.map(rowHtml).join('');
  }
  if (pageDone.length > 0) {
    listHtml += `<div class="tsk-list-sec">${I18N.t('tasks.section.completed')} <span class="tsk-sec-n">${doneRows.length}</span></div>` +
      pageDone.map(rowHtml).join('');
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
  <div class="tsk-page">
    <div class="tsk-top">
      <h1>${I18N.t('tasks.page_title')}</h1>
      <div id="deployLinkBar" style="margin-top:4px"></div>
      <div class="tsk-controls">
        <button class="tsk-btn tsk-btn-primary" onclick="openTaskCreateModal()" style="margin-right:10px">${I18N.t('tasks.create.btn_new')}</button>
        ${tab('all', I18N.t('tasks.tab.all'), cntAll)}${tab('active', I18N.t('tasks.tab.active'), cntActive)}${tab('completed', I18N.t('tasks.tab.completed'), cntDone)}
        <div class="tsk-search"><span>🔍</span><input id="tcSearchInput" placeholder="${I18N.t('tasks.search_placeholder')}" value="${_taskQuery}" onkeyup="if(event.key==='Enter'){_taskQuery=this.value;_taskPage=1;_render()}" onchange="_taskQuery=this.value;_taskPage=1;_render()"></div>
      </div>
    </div>
    <div class="tsk-layout" style="height:calc(100vh - 200px)">
      <div class="tsk-list" style="overflow-y:auto;height:100%;display:flex;flex-direction:column">${listHtml}<div style="flex:1"></div>${pagerHtml}</div>
      <div class="tsk-detail" style="overflow-y:auto;height:100%;position:relative;top:0">${detailHtml}</div>
    </div>
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
  let _sceneOptions = '<option value="">' + I18N.t('tasks.create.scene_none') + '</option>';

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
      const scopeTag = s.scope === 'repo' ? '【仓库级】' : s.scope === 'user' ? '【用户级】' : '';
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
