let _currentConvId = null;
let _chatAbort = null;
let _openConvId = null;
// v31: per-conversation 任务追踪（替代旧 _chatActiveTask 单变量）
// { conv_id: { task_id, el_id, status, finalText } }
let _chatTasks = {};
let _chatStreamAbort = null;  // 当前活跃的 SSE AbortController

// ===== 任务状态持久化（sessionStorage，对齐 Admin AI）=====
function _getStoredTask(convId) {
  try { const s = sessionStorage.getItem('chat_task_' + convId); return s ? JSON.parse(s) : null } catch(e) { return null }
}
function _setStoredTask(convId, task) {
  try { sessionStorage.setItem('chat_task_' + convId, JSON.stringify(task)) } catch(e) {}
}
function _clearStoredTask(convId) {
  try { sessionStorage.removeItem('chat_task_' + convId) } catch(e) {}
}
// v31: per-conversation 任务状态管理
function _getTask(convId) { return _chatTasks[convId] || null; }
function _setTask(convId, task) { _chatTasks[convId] = task; if (convId) _setStoredTask(convId, task); }
function _clearTask(convId) { delete _chatTasks[convId]; if (convId) _clearStoredTask(convId); }

// _NO_REPLY: 内联 I18N.t('chat.no_reply')，不缓存（模块顶层缓存会在语言切换后失效）

/**
 * Issue #293: 剥离标题行首 "Issue #N · " 前缀，返回清理后标题与编号。
 * @param {string} title - 原始标题（可能含 "Issue #N · {title}" 前缀）
 * @returns {{title: string, issueNumber: number}}
 */
function _stripIssuePrefix(title) {
  if (!title) return {title: '', issueNumber: 0};
  var m = title.match(/^Issue\s*#(\d+)\s*·\s*/);
  if (m) {
    return {title: title.substring(m[0].length), issueNumber: parseInt(m[1], 10) || 0};
  }
  return {title: title, issueNumber: 0};
}

/**
 * 将 "Issue #N" 文本渲染为 GitCode Issue 超链接。
 * @param {string} text - 原始文本
 * @param {string} repoKey - 仓库 key（如 "songspace/hello-blog"），用于构建 URL
 * @returns {string} HTML 字符串，Issue #N 变为 <a> 标签
 */
function linkifyIssueNumbers(text, repoKey) {
  if (!text) return '';
  // 匹配 "Issue #N" 或 "issue #N"（N 为数字）
  return text.replace(/(Issue\s*#(\d+))/gi, (match, full, num) => {
    if (repoKey) {
      const url = _vcsUrl(repoKey, '/issues/' + num);
      return `<a href="${url}" target="_blank" rel="noopener">${full}</a>`;
    }
    return match; // 无仓库信息时保持原文
  });
}

function renderChat(openConvId) {
  _currentConvId = null;
  _openConvId = openConvId || null;
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = `<div class="chat-wrap" style="height:calc(100vh - 96px)">
    <div class="chat-convlist" id="convList"></div>
    <div class="chat-main">
      <div class="chat-hdr"><h2>${I18N.t('chat.header.title')}</h2></div>
      <div class="chat-body" id="chatBody">
        <div style="text-align:center;color:var(--t3);padding:60px 20px">${I18N.t('common.loading')}</div>
      </div>
      <div id="chatImgPreview" style="display:none;padding:6px 12px;border-top:1px solid var(--b);flex-wrap:wrap;gap:6px"></div>
      <div class="chat-in"><label class="chat-img-btn" style="cursor:pointer;display:flex;align-items:center;padding:0 10px;border-radius:6px;color:var(--t3)" title="${I18N.t('chat.upload_image')}"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg><input type="file" accept="image/*" multiple style="display:none" onchange="_onChatImageSelect(this)"></label><label class="chat-img-btn" style="cursor:pointer;display:flex;align-items:center;padding:0 10px;border-radius:6px;color:var(--t3)" data-tooltip="${I18N.t('chat.video_tooltip')}"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg><input type="file" accept="video/*" style="display:none" onchange="_onChatVideoSelect(this)"></label><textarea id="chatInput" rows="1" placeholder="${I18N.t('chat.placeholder')}" style="flex:1" oninput="_autoResizeTextarea(this)"></textarea><button class="chat-inpicker-btn" id="chatModelBtn" onclick="toggleChatPicker('model')" data-tooltip="${I18N.t('chat.engine_config_tip')}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="2" x2="9" y2="4"/><line x1="15" y1="2" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="22"/><line x1="15" y1="20" x2="15" y2="22"/><line x1="20" y1="9" x2="22" y2="9"/><line x1="20" y1="14" x2="22" y2="14"/><line x1="2" y1="9" x2="4" y2="9"/><line x1="2" y1="14" x2="4" y2="14"/></svg></button><button class="chat-inpicker-btn" id="chatSkillBtn" onclick="toggleChatPicker('skill')" data-tooltip="${I18N.t('chat.skill_config_tip')}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg></button><button class="bt bt-p" onclick="sendChatStream()">${I18N.t('chat.send')}</button></div>
    </div>
  </div>`;
  loadConversations(true);  // auto-select first
  setupChatStream();
  loadChatConfigData();
  // Issue #63: 切回来时如果有进行中的接入任务，恢复轮询
  if (typeof _onboardTaskId !== 'undefined' && _onboardTaskId) pollOnboardStatus();
}

let _chatSkillScope = 'official';  // retained for im_channel.js compatibility

function _switchChatSkillScope(scope) {
  _chatSkillScope = scope;
  _loadChatSkillOptions();
}

function _loadChatSkillOptions() {
  // 不再依赖 DOM 中的 select 元素，仅保留函数供 im_channel.js 兼容
  return;
}

// ====== Chat Agent 配置：弹出式选择器（模型 + 技能）======
let _chatConfigData = null;  // 缓存后端返回的配置数据
let _chatPickerOpen = null;  // 'model' | 'skill' | null

function loadChatConfigData() {
  const _repo = app.currentRepo();
  const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
  API.get('/chat/config' + _qs).then(d => {
    _chatConfigData = d;
    _chatSkillScope = (d.skill_scope === 'repo') ? 'repo' : (d.skill_scope === 'user') ? 'user' : 'official';
    // 更新按钮标签
    _updateModelBtnLabel(d);
    _updateSkillBtnLabel(d);
    // 加载技能选项到隐藏 select
    _loadChatSkillOptions();
  }).catch(() => {});
}

function _updateModelBtnLabel(d) {
  // 按钮已简化为纯图标，无需更新文字
}

function _updateSkillBtnLabel(d) {
  // 按钮已简化为纯图标，无需更新文字
}

function toggleChatPicker(type) {
  // 关闭已打开的弹窗
  _closeChatPicker();
  if (_chatPickerOpen === type) {
    _chatPickerOpen = null;
    return;
  }
  _chatPickerOpen = type;
  const btn = document.getElementById(type === 'model' ? 'chatModelBtn' : 'chatSkillBtn');
  if (!btn) return;
  btn.classList.add('active');

  // 创建浮层
  const overlay = document.createElement('div');
  overlay.className = 'chat-picker-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _closeChatPicker(); };

  const pop = document.createElement('div');
  pop.className = 'chat-picker-pop';

  if (type === 'model') {
    pop.innerHTML = _buildModelPicker();
  } else {
    pop.innerHTML = _buildSkillPicker();
  }

  overlay.appendChild(pop);
  document.body.appendChild(overlay);

  // 定位到按钮上方
  const rect = btn.getBoundingClientRect();
  pop.style.position = 'fixed';
  pop.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
  pop.style.left = Math.min(rect.left, window.innerWidth - 300) + 'px';

  requestAnimationFrame(function() { pop.classList.add('show'); });

  // 绑定选择事件
  if (type === 'model') {
    pop.querySelectorAll('.chat-picker-item').forEach(item => {
      item.addEventListener('click', function() {
        const backend = this.dataset.value;
        const rid = this.dataset.rid || '';
        _saveModelConfig(backend, rid);
        _closeChatPicker();
      });
    });
  } else {
    // 技能 scope 切换
    const scopeSel = pop.querySelector('#pickerSkillScope');
    if (scopeSel) {
      scopeSel.addEventListener('change', function() {
        _chatSkillScope = this.value;
        const skillList = pop.querySelector('#pickerSkillList');
        if (skillList) _renderSkillListItems(skillList);
      });
    }
    const skillList = pop.querySelector('#pickerSkillList');
    if (skillList) _renderSkillListItems(skillList);
  }
}

function _closeChatPicker() {
  const overlay = document.querySelector('.chat-picker-overlay');
  if (overlay) overlay.remove();
  const modelBtn = document.getElementById('chatModelBtn');
  if (modelBtn) modelBtn.classList.remove('active');
  const skillBtn = document.getElementById('chatSkillBtn');
  if (skillBtn) skillBtn.classList.remove('active');
  _chatPickerOpen = null;
}

function _buildModelPicker() {
  const d = _chatConfigData || {};
  const backend = d.backend || {};
  const backends = d._backends || [];
  const curType = (backend.type || '').toLowerCase();
  let items;
  if (backends.length === 0) {
    items = `<div style="padding:16px;text-align:center;color:var(--t3);font-size:12px">${I18N.t('common.loading')}</div>`;
  } else {
    items = backends.map(b => {
      const isActive = b.name === curType || b.name.startsWith(curType + '-') || curType === b.name.toLowerCase();
      const icon = b.scope === 'local' ? '🖥' : '☁';
      return `<div class="chat-picker-item${isActive ? ' active' : ''}" data-value="${b.name}" data-rid="${b.runtime_id || ''}" style="padding:10px 14px;cursor:pointer;display:flex;align-items:center;gap:10px;transition:background .15s;border-radius:8px${isActive ? ';background:var(--a)11' : ''}">
        <span style="font-size:14px">${icon}</span>
        <span style="flex:1;font-size:12px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${b.display_name || b.name}</span>
        ${isActive ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--a)" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>' : ''}
      </div>`;
    }).join('');
  }
  return `<div style="font-size:11px;font-weight:700;color:var(--t3);padding:8px 14px 6px;letter-spacing:.3px;text-transform:uppercase">${I18N.t('chat.engine_label')}</div><div style="max-height:280px;overflow-y:auto;padding:0 6px 6px">${items}</div>`;
}

function _buildSkillPicker() {
  return `<div style="padding:8px 14px 6px">
    <div style="font-size:11px;font-weight:700;color:var(--t3);margin-bottom:8px;letter-spacing:.3px;text-transform:uppercase">${I18N.t('chat.skill_label')}</div>
    <div style="display:flex;gap:4px;margin-bottom:8px">
      ${['official','repo','user'].map(s => `<button class="chat-scope-tab${_chatSkillScope === s ? ' active' : ''}" data-scope="${s}" onclick="_chatPickerScope('${s}')" style="flex:1;padding:5px 8px;border:1px solid var(--b);border-radius:6px;font-size:11px;background:${_chatSkillScope === s ? 'var(--a)' : 'var(--c)'};color:${_chatSkillScope === s ? '#fff' : 'var(--t2)'};cursor:pointer;transition:all .15s">${I18N.t('chat.scope_short.' + (s === 'official' ? 'global' : s))}</button>`).join('')}
    </div>
    <div id="pickerSkillList" style="max-height:220px;overflow-y:auto"></div>
  </div>`;
}

function _chatPickerScope(scope) {
  _chatSkillScope = scope;
  const pop = document.querySelector('.chat-picker-pop');
  if (!pop) return;
  // 更新 tab 样式
  pop.querySelectorAll('.chat-scope-tab').forEach(tab => {
    const s = tab.dataset.scope;
    tab.classList.toggle('active', s === scope);
    tab.style.background = s === scope ? 'var(--a)' : 'var(--c)';
    tab.style.color = s === scope ? '#fff' : 'var(--t2)';
  });
  const skillList = pop.querySelector('#pickerSkillList');
  if (skillList) _renderSkillListItems(skillList);
}

function _renderSkillListItems(container) {
  const d = _chatConfigData || {};
  const skillId = d.skill_id || 0;
  container.innerHTML = '<div style="padding:8px;text-align:center;color:var(--t3);font-size:11px">' + I18N.t('common.loading') + '</div>';
  let repo = '';
  try {
    const c = app.currentRepo();
    repo = c ? (c.fullName || '') : '';
  } catch {}
  const scope = _chatSkillScope || 'official';
  let qs = '?scope=' + scope;
  if (repo) qs += '&repo=' + encodeURIComponent(repo);
  API.get('/skills' + qs).then(data => {
    const allSkills = data.skills || [];
    const filtered = allSkills.filter(s => s.agent_type === 'chat');
    let html = '';
    if (scope === 'official') {
      const isActive = !skillId || skillId === 0;
      html += `<div class="chat-picker-item${isActive ? ' active' : ''}" data-value="0" style="padding:10px 14px;cursor:pointer;display:flex;align-items:center;gap:10px;transition:background .15s;border-radius:8px${isActive ? ';background:var(--a)11' : ''}">
        <span style="flex:1;font-size:12px">${I18N.t('chat.skill.default_official')}</span>
        ${isActive ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--a)" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>' : ''}
      </div>`;
    }
    if (filtered.length === 0 && scope !== 'official') {
      html = `<div style="padding:12px;text-align:center;color:var(--t3);font-size:11px">${scope === 'repo' ? I18N.t('chat.skill.no_repo_skill') : I18N.t('chat.skill.no_personal_skill')}</div>`;
    }
    html += filtered.map(s => {
      const isActive = String(s.id) === String(skillId);
      return `<div class="chat-picker-item${isActive ? ' active' : ''}" data-value="${s.id}" style="padding:10px 14px;cursor:pointer;display:flex;align-items:center;gap:10px;transition:background .15s;border-radius:8px${isActive ? ';background:var(--a)11' : ''}">
        <span style="flex:1;font-size:12px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.display_name || s.name}</span>
        ${isActive ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--a)" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>' : ''}
      </div>`;
    }).join('');
    container.innerHTML = html;
    container.querySelectorAll('.chat-picker-item').forEach(item => {
      item.addEventListener('click', function() {
        const sid = parseInt(this.dataset.value) || 0;
        _saveSkillConfig(sid);
        _closeChatPicker();
      });
    });
  }).catch(() => {
    container.innerHTML = '<div style="padding:12px;text-align:center;color:var(--t3);font-size:11px">' + I18N.t('common.load_failed') + '</div>';
  });
}

function _saveModelConfig(backend, rid) {
  const _repo = app.currentRepo();
  const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
  const _body = {backend};
  if (rid) _body.runtime_id = parseInt(rid, 10);
  API.put('/chat/config' + _qs, _body).then(() => {
    app.showToast(I18N.t('chat.config.saved'));
    if (_chatConfigData) {
      _chatConfigData.backend = {type: backend};
      _updateModelBtnLabel(_chatConfigData);
    }
  }).catch(e => app.showToast(I18N.t('chat.config.save_failed', {msg: e.message})));
}

function _saveSkillConfig(skillId) {
  if (_chatSkillScope !== 'official' && !skillId) {
    if (app && app.showToast) app.showToast(I18N.t('chat.skill.select_required'));
    return;
  }
  const _repo = app.currentRepo();
  const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
  const _body = {skill_id: skillId};
  API.put('/chat/config' + _qs, _body).then(() => {
    app.showToast(I18N.t('chat.config.saved'));
    if (_chatConfigData) {
      _chatConfigData.skill_id = skillId;
      _updateSkillBtnLabel(_chatConfigData);
    }
  }).catch(e => app.showToast(I18N.t('chat.config.save_failed', {msg: e.message})));
}

function saveChatConfig() {
  // 兼容旧调用（onboard 完成后刷新）
  loadChatConfigData();
}

function loadConversations(autoSelect) {
  // Issue #47：首渲染即按目标仓库过滤，避免"先全局后过滤"导致串库闪烁。
  //  - 需求中心跳转(issue:N:repoKey)：用任务自带仓库过滤（该仓库即目标，正确露出）
  //  - 直连 conv_id：不过滤（定位既有会话）
  //  - 普通进入：按当前仓库过滤
  const repo = app.currentRepo();
  // 解析 issue:N:repoKey 格式的 _openConvId（Issues Center 跳转，会话可能已删除需重建）
  let _issueParam = null;
  if (_openConvId && _openConvId.startsWith('issue:')) {
    const parts = _openConvId.split(':');
    if (parts.length >= 3) _issueParam = {num: parts[1], repo: parts.slice(2).join(':')};
    _openConvId = null;
  }
  const repoKey = _issueParam && _issueParam.repo
    ? _issueParam.repo
    : (_openConvId ? '' : (repo ? (repo.repoKey || repo.fullName) : ''));
  API.get('/chat/conversations' + (repoKey ? '?repo_key=' + encodeURIComponent(repoKey) : '')).then(d => {
    const convs = d.conversations || [];
    const list = document.getElementById('convList');
    if (!list) return;
    let html = '<div style="padding:10px 12px;border-bottom:1px solid var(--b)"><button class="bt bt-p bt-s" onclick="newConversation()" style="width:100%">' + I18N.t('chat.conversation.new') + '</button></div>';
    convs.forEach(c => {
      const active = c.id === _currentConvId ? ' style="background:var(--b)"' : '';
      const stripped = _stripIssuePrefix(c.title || '');
      const issueNum = (c.issue_number && c.issue_number > 0) ? c.issue_number : stripped.issueNumber;
      const cleanTitle = stripped.title || (c.title ? '' : I18N.t('chat.conversation.default_title'));
      const safeTitle = String(cleanTitle).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const metaHtml = issueNum > 0
        ? '#' + issueNum + ' · ' + fmtLocalTime(c.created_at).substring(0,16)
        : fmtLocalTime(c.created_at).substring(0,16);
      html += `<div class="conv-item" data-id="${c.id}"${active}>
        <div class="conv-title">${safeTitle}</div>
        <div class="conv-meta">${metaHtml}</div>
        <span class="conv-del" onclick="event.stopPropagation();deleteConversation('${c.id}', event)">✕</span>
      </div>`;
    });
    if (convs.length === 0) {
      html += '<div style="padding:20px;text-align:center;color:var(--t3);font-size:12px">' + I18N.t('chat.conversation.empty_hint') + '</div>';
      var _cb = document.getElementById('chatBody');
      if (_cb) _cb.innerHTML = '<div style="text-align:center;color:var(--t3);padding:40px">' + I18N.t('chat.conversation.start_hint') + '</div>';
    }
    list.innerHTML = html;
    list.querySelectorAll('.conv-item').forEach(item => {
      item.addEventListener('click', () => switchConversation(item.dataset.id));
    });
    // Auto-select: 优先 issue 重建 > 任务指定的 conversation > 第一个
    if (autoSelect && !_currentConvId) {
      if (_issueParam) {
        // Issues Center 跳转：会话可能已删除，调 ensure-issue 重建
        API.post('/chat/conversations/ensure-issue', {repo_key: _issueParam.repo, issue_number: parseInt(_issueParam.num)}).then(r => {
          loadConversations(false);
          if (r.conversation_id) switchConversation(r.conversation_id);
        }).catch(() => {
          if (convs.length > 0) switchConversation(convs[0].id);
        });
      } else if (convs.length > 0) {
        const target = _openConvId && convs.some(c => c.id === _openConvId) ? _openConvId : convs[0].id;
        _openConvId = null;
        switchConversation(target);
      }
    }
  }).catch(() => {});
}

function newConversation() {
  const repo = app.currentRepo();
  API.post('/chat/conversations', repo ? {repo_key: (repo.repoKey || repo.fullName)} : {}).then(d => {
    _currentConvId = d.conversation_id;
    loadConversations();
    var _cb = document.getElementById('chatBody');
    if (_cb) _cb.innerHTML = '';
    var _ci = document.getElementById('chatInput');
    if (_ci) _ci.disabled = false;
    var _btn = document.querySelector('.chat-in button');
    if (_btn) _btn.disabled = false;
    const hdr = document.querySelector('.chat-hdr h2');
    if (hdr) hdr.textContent = I18N.t('chat.header.title');
  }).catch(e => app.showToast(I18N.t('chat.conversation.create_failed', {msg: e.message})));
}

function deleteConversation(convId, ev) {
  // 苹果风格确认弹窗（在点击位置弹出）
  _showChatDelPopover(convId, ev);
}

function _showChatDelPopover(convId, ev) {
  // 关闭已有弹窗
  _closeChatDelPopover();
  var cx = ev.clientX, cy = ev.clientY;
  var overlay = document.createElement('div');
  overlay.className = 'chat-del-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _closeChatDelPopover(); };
  var pop = document.createElement('div');
  pop.className = 'chat-del-pop';
  // 构建弹窗内容（用 DOM API 而非 innerHTML 避免转义问题）
  var title = document.createElement('div');
  title.className = 'chat-del-title';
  title.textContent = I18N.t('chat.conversation.delete_confirm');
  pop.appendChild(title);
  var btns = document.createElement('div');
  btns.className = 'chat-del-btns';
  var cancelBtn = document.createElement('button');
  cancelBtn.className = 'chat-del-cancel';
  cancelBtn.textContent = I18N.t('common.cancel');
  cancelBtn.onclick = function() { _closeChatDelPopover(); };
  btns.appendChild(cancelBtn);
  var okBtn = document.createElement('button');
  okBtn.className = 'chat-del-ok';
  okBtn.textContent = I18N.t('common.confirm');
  okBtn.onclick = function() { _doDeleteConv(convId); };
  btns.appendChild(okBtn);
  pop.appendChild(btns);
  overlay.appendChild(pop);
  document.body.appendChild(overlay);
  // 定位到点击位置（确保不超出视口）
  pop.style.position = 'fixed';
  pop.style.left = Math.min(cx, window.innerWidth - 240) + 'px';
  pop.style.top = Math.min(cy + 8, window.innerHeight - 120) + 'px';
  // 动画
  requestAnimationFrame(function() { pop.classList.add('show'); });
}

function _closeChatDelPopover() {
  const o = document.querySelector('.chat-del-overlay');
  if (o) o.remove();
}

function _doDeleteConv(convId) {
  _closeChatDelPopover();
  API.del('/chat/conversations/' + convId).then(() => {
    if (_currentConvId === convId) {
      _currentConvId = null;
      var _cb = document.getElementById('chatBody');
      if (_cb) _cb.innerHTML = '<div style="text-align:center;color:var(--t3);padding:40px">' + I18N.t('chat.conversation.start_hint') + '</div>';
    }
    loadConversations();
    app.showToast(I18N.t('chat.conversation.deleted'));
  }).catch(e => app.showToast(I18N.t('chat.conversation.delete_failed', {msg: e.message})));
}

function switchConversation(convId) {
  _currentConvId = convId;
  loadConversations();
  const body = document.getElementById('chatBody');
  const input = document.getElementById('chatInput');
  body.innerHTML = '<div class="loading">' + I18N.t('common.loading') + '</div>';
  API.get('/chat/messages/' + convId).then(d => {
    const msgs = d.messages || [];
    const conv = d.conversation || {};
    const hdr = document.querySelector('.chat-hdr h2');
    if (hdr) {
      const titleBase = conv.title || 'Personal Assistant';
      const stripped = _stripIssuePrefix(titleBase);
      const safeTitle = String(stripped.title || titleBase).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      hdr.innerHTML = I18N.t('chat.header.title_prefix') + linkifyIssueNumbers(safeTitle, conv.repo_key || '');
    }

    // 渲染消息列表
    body.innerHTML = msgs.map(m => {
      let rendered = renderMarkdown(m.content);
      // 将 [chat_img:url] 标记替换为图片缩略图
      rendered = rendered.replace(/\[chat_img:([^\]]+)\]/g, function(match, url) {
        return '<img src="' + url + '?thumb=1" style="max-width:140px;max-height:140px;border-radius:6px;margin-top:4px;display:inline-block;cursor:pointer" onclick="window.open(\'' + url + '\',\'_blank\')">';
      });
      return `<div class="msg ${m.role === 'user' ? 'r' : 'l'}">${linkifyIssueNumbers(rendered, conv.repo_key || '')}</div>`;
    }).join('') || '<div style="text-align:center;color:var(--t3);padding:40px">' + I18N.t('chat.conversation.start_new') + '</div>';
    body.scrollTop = body.scrollHeight;

    // ★ v31: 检查最后一条消息是否有运行中任务（学习 Admin AI reconnectActiveTask）
    const lastMsg = msgs[msgs.length - 1];
    if (lastMsg && lastMsg.role === 'user' && lastMsg.task_id) {
      // 有 task_id → 查后端 status（内存优先，DB 兜底）
      fetch('/api/chat/task/' + lastMsg.task_id + '/status', {
        headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
      }).then(r => r.json()).then(st => {
        if (st.status === 'running') {
          // 任务仍在执行 → 追加 3 点动画占位 + SSE 重连
          const elId = 't' + Date.now();
          body.innerHTML += '<div class="msg l" id="' + elId + '">'
            + '<span class="chat-dots"><span></span><span></span><span></span></span>'
            + '</div>';
          body.scrollTop = body.scrollHeight;
          _setTask(convId, {task_id: lastMsg.task_id, conv_id: convId, el_id: elId, status: 'running'});
          // SSE 重连（后端重放缓冲 + 实时追加）
          streamTask(lastMsg.task_id, elId, convId);
        } else if (st.status === 'done' || st.status === 'error') {
          // 任务已完成 → 检查 DB 中 agent 消息是否已显示
          const hasAgentReply = msgs.some(m => m.role === 'agent' && m.task_id === lastMsg.task_id);
          if (!hasAgentReply) {
            // agent 消息不在列表中 → 重新拉取（刚写入 DB）
            API.get('/chat/messages/' + convId).then(d2 => {
              renderChatMessages(d2.messages || []);
              loadConversations();
            });
          }
          _clearTask(convId);
        } else {
          // not_found → 任务状态未知，不显示中断提示（可能是跨机器延迟）
          _clearTask(convId);
        }
      }).catch(() => { _clearTask(convId); });
    }

    if (input) input.disabled = false;
    const sendBtn = document.querySelector('.chat-in button');
    if (sendBtn) sendBtn.disabled = false;
  }).catch(() => {
    body.innerHTML = '<div class="error">' + I18N.t('chat.load_failed') + '</div>';
  });
}

function _autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function _resetTextareaHeight(el) {
  el.style.height = '';
}

function setupChatStream() {
  const input = document.getElementById('chatInput');
  if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatStream(); } });
}

const _MAX_IMAGES = 9;
let _pendingImages = [];  // array of {data: dataURL, name: string}

function _renderImgPreview() {
  var pv = document.getElementById('chatImgPreview');
  if (!pv) return;
  if (_pendingImages.length === 0) {
    pv.style.display = 'none';
    pv.innerHTML = '';
    return;
  }
  pv.style.display = 'flex';
  var html = '';
  for (var i = 0; i < _pendingImages.length; i++) {
    var img = _pendingImages[i];
    html += '<div style="position:relative;display:inline-block">' +
      '<img src="' + img.data + '" style="width:56px;height:56px;border-radius:4px;object-fit:cover;cursor:pointer" onclick="window.open(this.src,\'_blank\')">' +
      '<button onclick="_removeChatImage(' + i + ')" style="position:absolute;top:-4px;right:-4px;width:18px;height:18px;border-radius:50%;background:rgba(0,0,0,.6);color:#fff;border:none;cursor:pointer;font-size:12px;line-height:18px;text-align:center;padding:0" title="' + I18N.t('chat.remove_image') + '">&#10005;</button>' +
      '</div>';
  }
  if (_pendingImages.length < _MAX_IMAGES) {
    var cnt = _pendingImages.length + '/' + _MAX_IMAGES;
    html += '<span style="font-size:11px;color:var(--t3);align-self:center">' + cnt + '</span>';
  }
  pv.innerHTML = html;
}

function _onChatImageSelect(fileInput) {
  var files = fileInput.files;
  if (!files || files.length === 0) return;
  var remaining = _MAX_IMAGES - _pendingImages.length;
  if (remaining <= 0) {
    app.showToast(I18N.t('chat.image_max_reached', {max: _MAX_IMAGES}));
    fileInput.value = '';
    return;
  }
  var toLoad = Math.min(files.length, remaining);
  if (files.length > remaining) {
    app.showToast(I18N.t('chat.image_max_exceeded', {max: _MAX_IMAGES}));
  }
  var loaded = 0;
  for (var i = 0; i < toLoad; i++) {
    (function(file) {
      if (file.size > 5 * 1024 * 1024) {
        app.showToast(I18N.t('chat.image_too_large') + ': ' + (file.name || ''));
        return;
      }
      var reader = new FileReader();
      reader.onload = function(e) {
        _pendingImages.push({data: e.target.result, name: file.name || '图片'});
        loaded++;
        if (loaded === toLoad || _pendingImages.length === _MAX_IMAGES) {
          _renderImgPreview();
          var input = document.getElementById('chatInput');
          if (input) input.focus();
        }
      };
      reader.readAsDataURL(file);
    })(files[i]);
  }
  fileInput.value = '';
}

function _removeChatImage(idx) {
  _pendingImages.splice(idx, 1);
  _renderImgPreview();
}

function _clearAllImages() {
  _pendingImages = [];
  _renderImgPreview();
}

const _MAX_VIDEO_SIZE = 50 * 1024 * 1024; // 50MB
let _pendingVideo = null; // {data: dataURL, name: string}

function _renderVideoPreview() {
  var pv = document.getElementById('chatImgPreview');
  if (!pv) return;
  // Remove existing video preview
  var old = pv.querySelector('[data-video-preview]');
  if (old) old.remove();
  if (!_pendingVideo) return;
  if (pv.style.display === 'none') {
    pv.style.display = 'flex';
  }
  var div = document.createElement('div');
  div.setAttribute('data-video-preview', '1');
  div.style.cssText = 'position:relative;display:inline-block';
  div.innerHTML = '<div style="width:56px;height:56px;border-radius:4px;background:var(--bg);display:flex;align-items:center;justify-content:center;cursor:pointer" title="' + (_pendingVideo.name || 'video') + '"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--t2)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg></div><button onclick="_removeChatVideo()" style="position:absolute;top:-4px;right:-4px;width:18px;height:18px;border-radius:50%;background:rgba(0,0,0,.6);color:#fff;border:none;cursor:pointer;font-size:12px;line-height:18px;text-align:center;padding:0">&#10005;</button>';
  pv.appendChild(div);
}

function _onChatVideoSelect(fileInput) {
  var files = fileInput.files;
  if (!files || files.length === 0) return;
  var file = files[0];
  if (file.size > _MAX_VIDEO_SIZE) {
    app.showToast(I18N.t('chat.image_too_large') + ': ' + (file.name || ''));
    fileInput.value = '';
    return;
  }
  var reader = new FileReader();
  reader.onload = function(e) {
    _pendingVideo = {data: e.target.result, name: file.name || 'video'};
    _renderVideoPreview();
    var input = document.getElementById('chatInput');
    if (input) input.focus();
  };
  reader.readAsDataURL(file);
  fileInput.value = '';
}

function _removeChatVideo() {
  _pendingVideo = null;
  _renderVideoPreview();
  // Hide preview area if no images either
  if (_pendingImages.length === 0) {
    var pv = document.getElementById('chatImgPreview');
    if (pv) { pv.style.display = 'none'; pv.innerHTML = ''; }
  }
}

function _clearVideo() {
  _pendingVideo = null;
}

function sendChatStream() {
  const input = document.getElementById('chatInput');
  const body = document.getElementById('chatBody');
  if (!input) return;
  if (!input.value.trim() && _pendingImages.length === 0 && !_pendingVideo) return;
  if (!_currentConvId) {
    const repo = app.currentRepo();
    API.post('/chat/conversations', repo ? {repo_key: (repo.repoKey || repo.fullName)} : {}).then(d => {
      _currentConvId = d.conversation_id;
      loadConversations();
      doSend(input, body);
    }).catch(e => app.showToast(I18N.t('chat.conversation.create_conv_failed', {msg: e.message})));
    return;
  }
  doSend(input, body);
}

// v31: doSend — submit/stream 分离（学习 Admin AI runQueryAsync）
function doSend(input, body) {
  const msg = input.value.trim();
  const imgs = _pendingImages.slice();  // copy array
  const vid = _pendingVideo ? _pendingVideo.data : '';
  if (!msg && imgs.length === 0) return;
  input.value = '';
  _resetTextareaHeight(input);
  _clearAllImages();
  _clearVideo();
  // 显示用户消息（图片直接显示缩略图）
  var displayMsg = msg.replace(/</g,'&lt;');
  var imgHtml = '';
  if (imgs.length > 0) {
    imgHtml = '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">';
    for (var i = 0; i < imgs.length; i++) {
      imgHtml += '<img src="' + imgs[i].data + '" style="max-width:140px;max-height:140px;border-radius:6px;cursor:pointer" onclick="window.open(this.src,\'_blank\')">';
    }
    imgHtml += '</div>';
  }
  if (vid) {
    imgHtml += '<div style="margin-top:4px"><div style="display:inline-flex;align-items:center;gap:4px;padding:4px 8px;border-radius:6px;background:var(--bg);color:var(--t2);font-size:12px"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>' + (_pendingVideo ? (_pendingVideo.name || 'video') : 'video') + '</div></div>';
  }
  body.innerHTML += '<div class="msg r">' + displayMsg + imgHtml + '</div>';
  const tid = 't' + Date.now();
  body.innerHTML += '<div class="msg l" id="' + tid + '">'
    + '<span class="chat-dots"><span></span><span></span><span></span></span>'
    + '</div>';
  body.scrollTop = body.scrollHeight;

  const repo = app.currentRepo();
  // 1. submit → 立即获取 task_id（视频理解在 agent_fn 内部异步执行，submit 不阻塞）
  API.post('/chat/submit', {
    message: msg || '请帮我分析这些图片',
    repo_key: repo ? (repo.repoKey || repo.fullName) : '',
    conversation_id: _currentConvId,
    image_data: imgs.length > 0 ? JSON.stringify(imgs.map(function(i) { return i.data })) : '',
    video_data: vid || '',
  }).then(r => {
    const convId = r.conversation_id || _currentConvId;
    if (r.status === 'error') {
      const el = document.getElementById(tid);
      if (el) el.innerHTML = renderMarkdown(r.error || I18N.t('chat.no_reply'));
      _clearTask(convId);
      loadConversations();
      return;
    }
    // per-conversation 记录
    _setTask(convId, {task_id: r.task_id, conv_id: convId, el_id: tid, status: 'running'});
    // 2. SSE 订阅
    streamTask(r.task_id, tid, convId);
  }).catch(e => {
    const el = document.getElementById(tid);
    if (el) el.innerHTML = I18N.t('common.request_failed') + (e.message || '');
  });
}

// v31: streamTask — SSE 订阅（可断开重连，后端重放缓冲）
// 学习 Admin AiAssistant.vue streamTask
async function streamTask(taskId, elId, convId) {
  // 中断旧 SSE（同会话新查询 / 不同会话切换让位）
  if (_chatStreamAbort) { _chatStreamAbort.abort(); _chatStreamAbort = null; }
  const ac = new AbortController();
  _chatStreamAbort = ac;

  const body = document.getElementById('chatBody');
  const el = document.getElementById(elId);
  let finalText = '';

  try {
    const resp = await fetch('/api/chat/task/' + taskId + '/stream', {
      headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
      signal: ac.signal
    });
    if (!resp.ok || !resp.body) {
      if (el) el.innerHTML = I18N.t('common.error') + (resp.statusText || '');
      _clearTask(convId);
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let finalText = '';
    let lastStepText = '';  // 最后一条步骤文本（单行滚动显示）
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
              // 完成 — 只显示最终结果
              finalText = d.text || finalText;
              if (el) el.innerHTML = renderMarkdown(finalText) || I18N.t('chat.no_reply');
            } else if (d.type === 'error') {
              if (el) el.innerHTML = renderMarkdown(d.text || '') || I18N.t('chat.no_reply');
            } else if (d.type === '思考中') {
              // 思考阶段 — 保持三点动画占位
              if (el) el.innerHTML = '<span class="chat-dots"><span></span><span></span><span></span></span>';
            } else if (d.type === 'text' && d.text && el) {
              // 流式 text chunk — 追加到正文
              finalText += d.text;
              if (el) el.innerHTML = renderMarkdown(finalText);
            } else if (d.text && el) {
              // 中间步骤（reasoning/step_start/step_finish/tool_use 等）— 单行滚动显示
              lastStepText = d.text;
              var _prefix = '';
              if (d.type === 'step_start') _prefix = '▸ ';
              else if (d.type === 'step_finish') _prefix = '✓ ';
              var _stepLine = _prefix + d.text;
              // 单行滚动：步骤文本（转义HTML）+ 三点动画（表示还在进行中）
              var _safe = String(_stepLine).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
              el.innerHTML = '<div class="chat-ticker">' + _safe + '</div><span class="chat-dots"><span></span><span></span><span></span></span>';
            }
          } catch(e) {}
        }
      });
      // 只有当前会话才滚动（后台会话不抢滚动）
      if (_currentConvId === convId && body) body.scrollTop = body.scrollHeight;
    }
    // 完成 → 清理任务状态
    _clearTask(convId);
    // 刷新会话列表（更新最后消息预览）
    if (_currentConvId === convId) loadConversations();
  } catch(e) {
    if (e.name === 'AbortError') return;  // 主动中断（切换会话/离开页面）
    // 连接断开 → 后端继续执行，切回来时 switchConversation 会重连
    // 不清除 _chatTasks[convId] — 保留状态供切回时恢复
    if (el) el.innerHTML = '<span class="chat-dots"><span></span><span></span><span></span></span>';
  }
}

// v31: resumeChatTask — 切换回 Chat 页面时恢复（从 app.js nav 调用）
function resumeChatTask() {
  if (!_currentConvId) return;
  // 从 sessionStorage 恢复或查 DB 最后一条消息
  const task = _getTask(_currentConvId) || _getStoredTask(_currentConvId);
  if (task && task.task_id) {
    // 有 task_id → 查 status
    fetch('/api/chat/task/' + task.task_id + '/status', {
      headers: {'Authorization': 'Bearer ' + localStorage.getItem('token')},
    }).then(r => r.json()).then(st => {
      if (st.status === 'running') {
        // 仍在执行 → 确保 3 点动画占位 + SSE 重连
        const body = document.getElementById('chatBody');
        if (!body) return;
        // 检查是否已有占位符
        let el = document.getElementById(task.el_id);
        if (!el) {
          const elId = 't' + Date.now();
          task.el_id = elId;
          body.innerHTML += '<div class="msg l" id="' + elId + '">'
            + '<span class="chat-dots"><span></span><span></span><span></span></span>'
            + '</div>';
          body.scrollTop = body.scrollHeight;
        }
        streamTask(task.task_id, task.el_id, _currentConvId);
      } else {
        // done/error/not_found → 刷新消息
        _clearTask(_currentConvId);
        API.get('/chat/messages/' + _currentConvId).then(d => {
          renderChatMessages(d.messages || []);
          loadConversations();
        }).catch(() => {});
      }
    }).catch(() => {});
  } else {
    // 无 task_id → 查 DB 最后一条消息是否有 task_id
    API.get('/chat/messages/' + _currentConvId).then(d => {
      const msgs = d.messages || [];
      const lastMsg = msgs[msgs.length - 1];
      if (lastMsg && lastMsg.role === 'user' && lastMsg.task_id) {
        // 有 task_id → 递归调用
        _setTask(_currentConvId, {task_id: lastMsg.task_id, conv_id: _currentConvId, status: 'running'});
        resumeChatTask();
      }
    }).catch(() => {});
  }
}

function renderChatMessages(messages) {
  const body = document.getElementById('chatBody');
  if (!body) return;
  body.innerHTML = '';
  messages.forEach(m => {
    let content = m.content || '';
    // 将 [chat_img:url] 标记替换为图片缩略图
    const imgHtml = content.replace(/\[chat_img:([^\]]+)\]/g, function(match, url) {
      return '<img src="' + url + '?thumb=1" style="max-width:140px;max-height:140px;border-radius:6px;margin-top:4px;display:inline-block;cursor:pointer" onclick="window.open(\'' + url + '\',\'_blank\')">';
    });
    // 移除标记后的纯文本（用于 escaped 显示）
    const textOnly = content.replace(/\[chat_img:[^\]]+\]/g, '').trim();
    if (m.role === 'user') {
      const escaped = textOnly.replace(/</g, '&lt;');
      body.innerHTML += '<div class="msg r">' + escaped + (imgHtml !== content ? imgHtml : '') + '</div>';
    } else {
      body.innerHTML += '<div class="msg l">' + renderMarkdown(content) + '</div>';
    }
  });
  body.scrollTop = body.scrollHeight;
}

function switchImProvider() {
  const sel = document.getElementById('cfgImProvider');
  if (!sel) return;
  const provider = sel.value;
  // Try to load provider-specific config from the DOM or API
  // For now just update the section header
}

// ====== 飞书扫码配置（复用组件：Chat / VOD Agent）======

let _onboardTimer = null;
let _onboardTaskId = null;
let _onboardExpireAt = 0;
let _onboardQrRendered = false;
let _onboardBotType = 'chat_bot';
let _onboardRepoKey = '';

function startFeishuOnboard(botType, repoKey) {
  _onboardBotType = botType || 'chat_bot';
  _onboardRepoKey = repoKey || '';
  const body = {bot_type: _onboardBotType};
  if (_onboardRepoKey) body.repo_key = _onboardRepoKey;
  API.post('/feishu/onboard/start', body).then(d => {
    _onboardTaskId = d.task_id;
    showOnboardModal();
    pollOnboardStatus();
  }).catch(e => {
    // 409：已有进行中的任务
    if (e.status === 409) app.showToast(e.message || I18N.t('chat.onboard.in_progress'));
    else app.showToast(I18N.t('chat.onboard.start_failed', {msg: e.message}));
  });
}

function showOnboardModal() {
  if (document.getElementById('onboardModal')) return;
  const mask = document.createElement('div');
  mask.id = 'onboardModal';
  mask.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:998;display:flex;align-items:center;justify-content:center';
  mask.innerHTML = `
    <div style="width:340px;background:var(--bg);border:1px solid var(--b);border-radius:12px;padding:20px;box-shadow:0 8px 32px rgba(0,0,0,.25);font-size:12px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <b style="font-size:14px">${I18N.t('chat.onboard.modal_title')}</b>
        <span style="cursor:pointer;color:var(--t3)" onclick="cancelOnboard()">✕</span>
      </div>
      <div id="onboardBody" style="text-align:center;color:var(--t3);padding:30px 0">${I18N.t('chat.onboard.generating_qr')}</div>
    </div>`;
  document.body.appendChild(mask);
  mask.addEventListener('click', e => { if (e.target === mask) cancelOnboard(); });
}

function pollOnboardStatus() {
  if (_onboardTimer) clearInterval(_onboardTimer);
  _onboardTimer = setInterval(async () => {
    if (!_onboardTaskId) { clearInterval(_onboardTimer); return; }
    try {
      const d = await API.get('/feishu/onboard/status?task_id=' + _onboardTaskId);
      renderOnboardStatus(d);
      if (d.status === 'connected' || d.status === 'failed') {
        clearInterval(_onboardTimer);
        _onboardTimer = null;
      }
    } catch (e) {
      clearInterval(_onboardTimer);
      _onboardTimer = null;
    }
  }, 2000);
}

// Issue #63: 导出停轮询函数供 app.nav 调用
function _stopOnboardPoll() {
  if (_onboardTimer) { clearInterval(_onboardTimer); _onboardTimer = null; }
}

function renderOnboardStatus(d) {
  const body = document.getElementById('onboardBody');
  if (!body) return;
  const map = {
    pending: () => { body.innerHTML = I18N.t('chat.onboard.generating_qr'); },
    waiting_confirm: () => {
      if (!d.qr_url) return;
      // 仅在二维码首次就绪时初始化过期时间与渲染二维码；
      // 后续轮询（每 2s）不得重置 _onboardExpireAt，否则倒计时被反复拉回满值。
      if (!_onboardQrRendered) {
        _onboardQrRendered = true;
        _onboardExpireAt = d.expire_in ? Date.now() + d.expire_in * 1000 : 0;
        body.innerHTML = `
          <div style="background:#fff;border:1px solid var(--b);border-radius:8px;padding:12px;display:inline-block;margin-bottom:10px" id="onboardQr"></div>
          <div style="color:var(--t);margin-bottom:4px">${I18N.t('chat.onboard.step1_scan')}</div>
          <div style="color:var(--t3);margin-bottom:10px">${I18N.t('chat.onboard.step2_confirm')}</div>
          <div style="color:var(--pk);font-size:11px" id="onboardCountdown">${I18N.t('chat.onboard.waiting_confirm')}</div>`;
        new QRCode(document.getElementById('onboardQr'), {text: d.qr_url, width: 200, height: 200});
        startCountdown();
      }
    },
    connected: () => {
      body.innerHTML = `
        <div style="font-size:34px;margin-bottom:8px">✅</div>
        <div style="color:var(--t);font-weight:600;margin-bottom:8px">${I18N.t('chat.onboard.success_title')}</div>
        <div style="color:var(--t2);font-size:11px;margin-bottom:2px">App ID: <span style="font-family:monospace">${d.app_id || ''}</span></div>
        <div style="color:var(--t2);font-size:11px;margin-bottom:4px">${I18N.t('chat.onboard.secret_saved')}</div>
        <div style="color:var(--pk);font-size:11px;margin-bottom:12px">${I18N.t('chat.onboard.bot_connected')}</div>
        <button class="bt bt-p bt-s" onclick="closeOnboardModal(true)" style="width:100%">${I18N.t('chat.onboard.close')}</button>`;
    },
    failed: () => {
      const msg = onboardErrorText(d.error || '');
      body.innerHTML = `
        <div style="font-size:34px;margin-bottom:8px">❌</div>
        <div style="color:var(--r);font-weight:600;margin-bottom:8px">${I18N.t('chat.onboard.failed_title')}</div>
        <div style="color:var(--t2);font-size:11px;margin-bottom:12px">${msg}</div>
        <div style="display:flex;gap:8px">
          <button class="bt bt-o bt-s" onclick="restartOnboard()" style="flex:1">${I18N.t('chat.onboard.rescan')}</button>
          <button class="bt bt-p bt-s" onclick="closeOnboardModal()" style="flex:1">${I18N.t('chat.onboard.cancel')}</button>
        </div>`;
    },
  };
  const fn = map[d.status] || map.pending;
  fn();
}

function onboardErrorText(err) {
  const t = String(err || '');
  if (t.includes('access_denied')) return I18N.t('chat.onboard.error_access_denied');
  if (t.includes('expired') || t.includes('timeout')) return I18N.t('chat.onboard.qr_expired');
  if (t.includes('已被')) return t;
  return t || I18N.t('chat.onboard.error_unknown');
}

function startCountdown() {
  const el = document.getElementById('onboardCountdown');
  if (!el) return;
  const tick = () => {
    if (!_onboardExpireAt) return;
    const remain = Math.max(0, Math.round((_onboardExpireAt - Date.now()) / 1000));
    if (remain <= 0) {
      el.textContent = I18N.t('chat.onboard.qr_expired');
      return;
    }
    const mm = String(Math.floor(remain / 60)).padStart(2, '0');
    const ss = String(remain % 60).padStart(2, '0');
    el.textContent = I18N.t('chat.onboard.waiting_countdown', {time: mm + ':' + ss});
    setTimeout(tick, 1000);
  };
  tick();
}

function restartOnboard() {
  const botType = _onboardBotType || 'chat_bot';
  const repoKey = _onboardRepoKey || '';
  _onboardTaskId = null;
  _onboardExpireAt = 0;
  _onboardQrRendered = false;
  startFeishuOnboard(botType, repoKey);
}

function cancelOnboard() {
  if (_onboardTaskId) API.post('/feishu/onboard/cancel', {task_id: _onboardTaskId}).catch(() => {});
  closeOnboardModal();
}

function closeOnboardModal(refresh) {
  _onboardTaskId = null;
  _onboardExpireAt = 0;
  _onboardQrRendered = false;
  if (_onboardTimer) { clearInterval(_onboardTimer); _onboardTimer = null; }
  const m = document.getElementById('onboardModal');
  if (m) m.remove();
  if (refresh) {
    // VOD onboard → 刷新 VOD Agent 页的 App ID/Secret（agents.js）；chat_bot → 刷新 Chat 页配置
    if (_onboardBotType === 'vod_bot' && typeof window.refreshVodOnboard === 'function') {
      window.refreshVodOnboard();
    } else {
      loadChatConfigPanel();
    }
  }
}