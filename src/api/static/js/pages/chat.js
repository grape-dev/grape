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
      return `<a href="${url}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">${full}</a>`;
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
      <div class="chat-hdr"><h2>${I18N.t('chat.header.title')}</h2><button class="chat-cfg-btn" id="chatCfgBtn" onclick="toggleChatSide()">${I18N.t('chat.header.config_btn')}</button></div>
      <div class="chat-body" id="chatBody">
        <div style="text-align:center;color:var(--t3);padding:60px 20px">${I18N.t('common.loading')}</div>
      </div>
      <div id="chatImgPreview" style="display:none;padding:6px 12px;border-top:1px solid var(--b);flex-wrap:wrap;gap:6px"></div>
      <div class="chat-in"><label class="chat-img-btn" style="cursor:pointer;display:flex;align-items:center;padding:0 10px;border-radius:6px;color:var(--t3)" title="${I18N.t('chat.upload_image')}"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg><input type="file" accept="image/*" multiple style="display:none" onchange="_onChatImageSelect(this)"></label><label class="chat-img-btn" style="cursor:pointer;display:flex;align-items:center;padding:0 10px;border-radius:6px;color:var(--t3)" data-tooltip="${I18N.t('chat.video_tooltip')}"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg><input type="file" accept="video/*" style="display:none" onchange="_onChatVideoSelect(this)"></label><input id="chatInput" placeholder="${I18N.t('chat.placeholder')}" style="flex:1"><button class="bt bt-p" onclick="sendChatStream()">${I18N.t('chat.send')}</button></div>
    </div>
    <div class="chat-side" id="chatSide"></div>
  </div>`;
  loadConversations(true);  // auto-select first
  loadSideConfig();
  setupChatStream();
  // Issue #63: 切回来时如果有进行中的接入任务，恢复轮询
  if (typeof _onboardTaskId !== 'undefined' && _onboardTaskId) pollOnboardStatus();
}

let _chatSkillScope = 'official';

function toggleChatSide() {
  const side = document.getElementById('chatSide');
  const btn = document.getElementById('chatCfgBtn');
  if (!side) return;
  side.classList.toggle('open');
  if (btn) btn.classList.toggle('active', side.classList.contains('open'));
}

function _switchChatSkillScope(scope) {
  _chatSkillScope = scope;
  _loadChatSkillOptions();
}

function _loadChatSkillOptions() {
  const skillSelect = document.getElementById('cfgSkill');
  if (!skillSelect) return;
  const currentSkillId = skillSelect.dataset.skillId || '0';
  let repo = '';
  try {
    const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
    repo = c ? (c.fullName || '') : '';
  } catch {}
  const scope = _chatSkillScope || 'official';
  let qs = '?scope=' + scope;
  if (repo) qs += '&repo=' + encodeURIComponent(repo);
  API.get('/skills' + qs).then(d => {
    const allSkills = d.skills || [];
    const filtered = allSkills.filter(s => s.agent_type === 'chat');
    const matchCurrent = filtered.find(s => String(s.id) === currentSkillId);
    const defaultOpt = scope === 'official' ? '<option value="0">' + I18N.t('chat.skill.default_official') + '</option>' : '';
    let html = defaultOpt +
      filtered.map((s, i) => {
        const selected = matchCurrent ? (String(s.id) === currentSkillId) : (i === 0);
        return `<option value="${s.id}" ${selected ? 'selected' : ''}>${s.display_name || s.name}</option>`;
      }).join('');
    if (filtered.length === 0) {
      html = `<option value="0" disabled>${scope === 'repo' ? I18N.t('chat.skill.no_repo_skill') : I18N.t('chat.skill.no_personal_skill')}</option>`;
    }
    skillSelect.innerHTML = html;
  }).catch(() => {});
}

function loadSideConfig() {
  const _repo = app.currentRepo();
  const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
  API.get('/chat/config' + _qs).then(d => {
    const feishu = d.im?.feishu || {};
    const backend = d.backend || {};
    const backendsOpts = (d._backends || []).map(b =>
      `<option value="${b.name}" data-rid="${b.runtime_id || ''}" ${b.name === (backend.type || '').toLowerCase() || b.name.startsWith((backend.type || '').toLowerCase() + '-') ? 'selected' : ''}>${(b.display_name || b.name) + (b.scope === 'local' ? ' (' + I18N.t('chat.local_direct') + ')' : '')}</option>`
    ).join('') || '<option value="">Default</option>';
    const side = document.getElementById('chatSide');
    if (!side) return;
    // scope 初始值从已配置 skill 的 scope 推导
    _chatSkillScope = (d.skill_scope === 'repo') ? 'repo' : (d.skill_scope === 'user') ? 'user' : 'official';
    side.innerHTML = `
      <div class="cd" style="padding:12px;font-size:11px">
        <div style="font-weight:600;margin-bottom:8px">${I18N.t('chat.backend')}</div>
        <select id="cfgBackend" style="width:100%;padding:5px 8px;border:1px solid var(--b);border-radius:4px;font-size:11px;background:var(--bg)">${backendsOpts}</select>
      </div>
      <div class="cd" style="padding:12px;font-size:11px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-weight:600;flex-shrink:0">${I18N.t('chat.skill_label')}</span>
          <select id="cfgSkillScope" onchange="_switchChatSkillScope(this.value)" style="flex:0 0 70px;max-width:70px;padding:5px 6px;border:1px solid var(--b);border-radius:4px;font-size:11px;background:var(--bg);outline:none">
            <option value="official">${I18N.t('chat.scope_short.global')}</option>
            <option value="repo">${I18N.t('chat.scope_short.repo')}</option>
            <option value="user">${I18N.t('chat.scope_short.user')}</option>
          </select>
        </div>
        <select id="cfgSkill" data-skill-id="${d.skill_id || 0}" style="width:100%;padding:5px 8px;border:1px solid var(--b);border-radius:4px;font-size:11px;background:var(--bg)">
          <option value="0">${I18N.t('chat.skill.default_official')}</option>
        </select>
      </div>
      <div class="cd" style="padding:12px;font-size:11px">
        <div style="font-weight:600;margin-bottom:8px">IM</div>
        <select id="cfgImProvider" onchange="switchImProvider()" style="width:100%;padding:4px 6px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg);margin-bottom:6px">
          <option value="feishu" ${d.im?.feishu?.app_id ? 'selected' : ''}>Feishu</option>
          <option value="wecom" ${d.im?.wecom?.app_id ? 'selected' : ''}>WeCom</option>
          <option value="dingtalk" ${d.im?.dingtalk?.app_id ? 'selected' : ''}>DingTalk</option>
        </select>
        <div style="margin-bottom:4px"><span class="t3">${I18N.t('chat.app_id')}</span></div>
        <input id="cfgImAppId" placeholder="App ID" value="${feishu.app_id || ''}" style="width:100%;padding:4px 6px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg);margin-bottom:4px">
        <div style="margin-bottom:4px"><span class="t3">${I18N.t('chat.app_secret')}</span></div>
        <input id="cfgImSecret" type="password" placeholder="App Secret" value="${feishu.app_secret_exists ? '********' : ''}" style="width:100%;padding:4px 6px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg);margin-bottom:6px">
        <button class="bt bt-o bt-s" onclick="startFeishuOnboard('chat_bot')" style="width:100%;margin-bottom:6px">${I18N.t('chat.scan_config')}</button>
        <button class="bt bt-p bt-s" onclick="saveChatConfig()" style="width:100%">${I18N.t('common.save')}</button>
      </div>
      <div class="cd" style="padding:12px;font-size:11px">
        <div style="font-weight:600;margin-bottom:6px">${I18N.t('chat.agents')}</div>
        <div class="chat-agents" id="chatAgents"></div>
      </div>`;
    // 设置 scope 下拉框初始值
    const scopeSel = document.getElementById('cfgSkillScope');
    if (scopeSel) scopeSel.value = _chatSkillScope;
    // 加载 skill 下拉
    _loadChatSkillOptions();
    // Load agent stats
    const repo = app.currentRepo();
    if (repo) {
      ['vod','issue','coding','qa','sre'].forEach((at, i) => {
        const COLORS = ['var(--pk)','var(--r)','var(--a)','var(--pu)','var(--or)'];
        const LABELS = ['VOD','Issue','Coding','QA','SRE'];
        API.get('/agents/' + at + '/' + repo.owner + '/' + repo.name).then(d => {
          const m = d.metrics || {};
          const es = m.event_stats || {};
          const total = m.total_events || 0;
          const el2 = document.getElementById('chatAgents');
          if (el2) el2.innerHTML += `<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;font-size:10px"><span style="width:6px;height:6px;border-radius:50%;background:${COLORS[i]};display:inline-block"></span>${LABELS[i]}<span class="t3" style="margin-left:auto">${total}</span></div>`;
        }).catch(() => {});
      });
    }
  }).catch(() => {});
}

function loadConversations(autoSelect) {
  // Issue #47：首渲染即按目标仓库过滤，避免"先全局后过滤"导致串库闪烁。
  //  - 任务中心跳转(issue:N:repoKey)：用任务自带仓库过滤（该仓库即目标，正确露出）
  //  - 直连 conv_id：不过滤（定位既有会话）
  //  - 普通进入：按当前仓库过滤
  const repo = app.currentRepo();
  // 解析 issue:N:repoKey 格式的 _openConvId（Task Center 跳转，会话可能已删除需重建）
  let _issueParam = null;
  if (_openConvId && _openConvId.startsWith('issue:')) {
    const parts = _openConvId.split(':');
    if (parts.length >= 3) _issueParam = {num: parts[1], repo: parts.slice(2).join(':')};
    _openConvId = null;
  }
  const repoKey = _issueParam && _issueParam.repo
    ? _issueParam.repo
    : (_openConvId ? '' : (repo ? repo.fullName : ''));
  API.get('/chat/conversations' + (repoKey ? '?repo_key=' + encodeURIComponent(repoKey) : '')).then(d => {
    const convs = d.conversations || [];
    const list = document.getElementById('convList');
    if (!list) return;
    let html = '<div style="padding:10px 12px;border-bottom:1px solid var(--b)"><button class="bt bt-p bt-s" onclick="newConversation()" style="width:100%">' + I18N.t('chat.conversation.new') + '</button></div>';
    convs.forEach(c => {
      const active = c.id === _currentConvId ? ' style="background:var(--b)"' : '';
      const titleHtml = c.title ? linkifyIssueNumbers(c.title, c.repo_key) : I18N.t('chat.conversation.default_title');
      html += `<div class="conv-item" data-id="${c.id}"${active}>
        <div class="conv-title">${titleHtml}</div>
        <div class="conv-meta">${fmtLocalTime(c.created_at).substring(0,16)}</div>
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
        // Task Center 跳转：会话可能已删除，调 ensure-issue 重建
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
  API.post('/chat/conversations', repo ? {repo_key: repo.fullName} : {}).then(d => {
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
      const safeTitle = String(titleBase).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      hdr.innerHTML = I18N.t('chat.header.title_prefix') + linkifyIssueNumbers(safeTitle, conv.repo_key || '');
    }

    // 渲染消息列表
    body.innerHTML = msgs.map(m => {
      let rendered = renderMarkdown(m.content);
      // 将 [chat_img:url] 标记替换为图片缩略图
      rendered = rendered.replace(/\[chat_img:([^\]]+)\]/g, function(match, url) {
        return '<img src="' + url + '?thumb=1" style="max-width:140px;max-height:140px;border-radius:6px;margin-top:4px;display:inline-block;cursor:pointer" onclick="window.open(\'' + url + '\',\'_blank\')">';
      });
      return `<div class="msg ${m.role === 'user' ? 'l' : 'r'}">${linkifyIssueNumbers(rendered, conv.repo_key || '')}</div>`;
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
          body.innerHTML += '<div class="msg r" id="' + elId + '">'
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

function setupChatStream() {
  const input = document.getElementById('chatInput');
  if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter') sendChatStream(); });
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
    API.post('/chat/conversations', repo ? {repo_key: repo.fullName} : {}).then(d => {
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
  body.innerHTML += '<div class="msg l">' + displayMsg + imgHtml + '</div>';
  const tid = 't' + Date.now();
  body.innerHTML += '<div class="msg r" id="' + tid + '">'
    + '<span class="chat-dots"><span></span><span></span><span></span></span>'
    + '</div>';
  body.scrollTop = body.scrollHeight;

  const repo = app.currentRepo();
  // 1. submit → 立即获取 task_id（视频理解在 agent_fn 内部异步执行，submit 不阻塞）
  API.post('/chat/submit', {
    message: msg || '请帮我分析这些图片',
    repo_key: repo ? repo.fullName : '',
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
          body.innerHTML += '<div class="msg r" id="' + elId + '">'
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
      body.innerHTML += '<div class="msg l">' + escaped + (imgHtml !== content ? imgHtml : '') + '</div>';
    } else {
      body.innerHTML += '<div class="msg r">' + renderMarkdown(content) + '</div>';
    }
  });
  body.scrollTop = body.scrollHeight;
}

function saveChatConfig() {
  const _bkSel = document.getElementById('cfgBackend');
  const backend = _bkSel?.value || '';
  const rid = _bkSel?.selectedOptions?.[0]?.dataset?.rid || '';
  const skillVal = document.getElementById('cfgSkill')?.value || '0';
  const skillId = parseInt(skillVal) || 0;
  // 项目/个人 scope 下不允许保存空值（无"默认技能"选项）
  if (_chatSkillScope !== 'official' && !skillId) {
    if (app && app.showToast) app.showToast(I18N.t('chat.skill.select_required'));
    return;
  }
  const provider = document.getElementById('cfgImProvider')?.value || 'feishu';
  const appId = document.getElementById('cfgImAppId')?.value || '';
  const rawSecret = document.getElementById('cfgImSecret')?.value || '';
  const secret = rawSecret === '********' ? '' : rawSecret;
  const im = {};
  im[provider] = {app_id: appId, app_secret: secret};
  const _repo = app.currentRepo();
  const _qs = _repo ? '?repo=' + encodeURIComponent(_repo.fullName) : '';
  const _body = {backend, skill_id: skillId, im};
  if (rid) _body.runtime_id = parseInt(rid, 10);
  API.put('/chat/config' + _qs, _body).then(() => {
    app.showToast(I18N.t('chat.config.saved'));
  }).catch(e => app.showToast(I18N.t('chat.config.save_failed', {msg: e.message})));
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
      loadSideConfig();
    }
  }
}