let _currentConvId = null;
let _chatAbort = null;
let _openConvId = null;
const _NO_REPLY = I18N.t('chat.no_reply');

/**
 * 将 "Issue #N" 文本渲染为 GitCode Issue 超链接。
 * @param {string} text - 原始文本
 * @param {string} repoKey - 仓库 key（如 "songspace/hello-blog"），用于构建 URL
 * @returns {string} HTML 字符串，Issue #N 变为 <a> 标签
 */
function linkifyIssueNumbers(text, repoKey) {
  if (!text) return '';
  // repoKey 可能为空（普通对话无仓库）→ 仅当能解析出 owner/repo 时才链接化
  let ownerRepo = '';
  if (repoKey) {
    // 去掉可能的 gitcode: 前缀
    const parts = repoKey.replace(/^gitcode:/, '').split('/');
    if (parts.length === 2 && parts[0] && parts[1]) {
      ownerRepo = parts[0] + '/' + parts[1];
    }
  }
  // 匹配 "Issue #N" 或 "issue #N"（N 为数字）
  return text.replace(/(Issue\s*#(\d+))/gi, (match, full, num) => {
    if (ownerRepo) {
      const url = `https://gitcode.com/${ownerRepo}/issues/${num}`;
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
      <div class="chat-in"><input id="chatInput" placeholder="${I18N.t('chat.placeholder')}" style="flex:1"><button class="bt bt-p" onclick="sendChatStream()">${I18N.t('chat.send')}</button></div>
    </div>
    <div class="chat-side" id="chatSide"></div>
  </div>`;
  loadConversations(true);  // auto-select first
  loadSideConfig();
  setupChatStream();
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
  // 去对话指定会话时，不过滤 repo，确保任务会话可被定位
  const repo = app.currentRepo();
  // 解析 issue:N:repoKey 格式的 _openConvId（Task Center 跳转，会话可能已删除需重建）
  let _issueParam = null;
  if (_openConvId && _openConvId.startsWith('issue:')) {
    const parts = _openConvId.split(':');
    if (parts.length >= 3) _issueParam = {num: parts[1], repo: parts.slice(2).join(':')};
    _openConvId = null;
  }
  const repoKey = (_openConvId || _issueParam ? '' : (repo ? repo.fullName : ''));
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
        <span class="conv-del" onclick="event.stopPropagation();deleteConversation('${c.id}')">✕</span>
      </div>`;
    });
    if (convs.length === 0) {
      html += '<div style="padding:20px;text-align:center;color:var(--t3);font-size:12px">' + I18N.t('chat.conversation.empty_hint') + '</div>';
      document.getElementById('chatBody').innerHTML = '<div style="text-align:center;color:var(--t3);padding:40px">' + I18N.t('chat.conversation.start_hint') + '</div>';
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
    document.getElementById('chatBody').innerHTML = '';
    document.getElementById('chatInput').disabled = false;
    document.querySelector('.chat-in button').disabled = false;
    const hdr = document.querySelector('.chat-hdr h2');
    if (hdr) hdr.textContent = I18N.t('chat.header.title');
  }).catch(e => app.showToast(I18N.t('chat.conversation.create_failed', {msg: e.message})));
}

function deleteConversation(convId) {
  if (!confirm(I18N.t('chat.conversation.delete_confirm'))) return;
  API.del('/chat/conversations/' + convId).then(() => {
    if (_currentConvId === convId) {
      _currentConvId = null;
      document.getElementById('chatBody').innerHTML = '<div style="text-align:center;color:var(--t3);padding:40px">' + I18N.t('chat.conversation.start_hint') + '</div>';
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
    // 更新会话标题（如 Issue #4）到页头，Issue #N 渲染为 GitCode 超链接
    const conv = d.conversation || {};
    const hdr = document.querySelector('.chat-hdr h2');
    if (hdr) {
      const titleBase = conv.title || 'Personal Assistant';
      const repoKey = conv.repo_key || '';
      // 先转义 HTML，再链接化 Issue #N（renderMarkdown 已转义，这里需手动处理）
      const safeTitle = String(titleBase).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const linkedTitle = linkifyIssueNumbers(safeTitle, repoKey);
      hdr.innerHTML = I18N.t('chat.header.title_prefix') + linkedTitle;
    }
    body.innerHTML = msgs.map(m => {
      const rendered = renderMarkdown(m.content);
      // 消息内容中的 "Issue #N" 渲染为 GitCode 超链接
      const repoKey = conv.repo_key || '';
      return `<div class="msg ${m.role === 'user' ? 'l' : 'r'}">${linkifyIssueNumbers(rendered, repoKey)}</div>`;
    }).join('') || '<div style="text-align:center;color:var(--t3);padding:40px">' + I18N.t('chat.conversation.start_new') + '</div>';
    body.scrollTop = body.scrollHeight;
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

function sendChatStream() {
  const input = document.getElementById('chatInput');
  const body = document.getElementById('chatBody');
  if (!input || !input.value.trim()) return;
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

function doSend(input, body) {
  const msg = input.value.trim();
  input.value = '';
  body.innerHTML += '<div class="msg l">' + msg.replace(/</g,'&lt;') + '</div>';
  const tid = 't' + Date.now();
  body.innerHTML += '<div class="msg r" id="' + tid + '">' + I18N.t('chat.thinking') + '</div>';
  body.scrollTop = body.scrollHeight;

  const repo = app.currentRepo();
  const token = localStorage.getItem('token');
  if (_chatAbort) { _chatAbort.abort(); _chatAbort = null; }
  const ac = new AbortController();
  _chatAbort = ac;

  fetch('/api/chat/send/stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    body: JSON.stringify({message: msg, repo_key: repo ? repo.fullName : '', conversation_id: _currentConvId}),
    signal: ac.signal,
  }).then(async resp => {
    const el = document.getElementById(tid);
    // M4 D5：queue 模式下 send/stream 返回 {task_id}（非 SSE）→ 轮询任务状态（打字机近似）
    const ctype = (resp.headers.get('content-type') || '');
    if (ctype.includes('application/json')) {
      const data = await resp.json().catch(() => ({}));
      if (data && data.task_id) { pollTaskStatus(data.task_id, el, body, tid); return; }
      if (el) el.innerHTML = renderMarkdown(data.reply || '') || _NO_REPLY;
      loadConversations();
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (el) el.innerHTML = I18N.t('common.error') + (err.detail?.message || resp.statusText);
      return;
    }
    if (!resp.body) {
      // Fallback: read as text
      const text = await resp.text();
      try {
        const lines = text.split('\n');
        lines.forEach(line => {
          if (line.startsWith('data: ')) {
            const d = JSON.parse(line.substring(6));
            if (d.text && el) el.innerHTML = renderMarkdown(d.text);
          }
        });
      } catch(e) {}
      if (el) el.innerHTML = renderMarkdown(text) || _NO_REPLY;
      loadConversations();
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '', finalText = '';
    function read() {
      reader.read().then(({done, value}) => {
        if (done) {
          if (el) el.innerHTML = renderMarkdown(finalText) || _NO_REPLY;
          body.scrollTop = body.scrollHeight;
          _chatAbort = null;
          loadConversations();
          return;
        }
        buf += dec.decode(value, {stream: true});
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        lines.forEach(line => {
          if (line.startsWith('data: ')) {
            try {
              const d = JSON.parse(line.substring(6));
              if (d.type === 'done') finalText = d.text;
              else if (d.text && el) el.innerHTML = renderMarkdown(d.text);
            } catch(e) {}
          }
        });
        body.scrollTop = body.scrollHeight;
        read();
      }).catch(e => { if (e.name !== 'AbortError' && el) el.innerHTML = I18N.t('common.error') + e.message; });
    }
    read();
  }).catch(e => { const el = document.getElementById(tid); if (el) el.innerHTML = I18N.t('common.request_failed') + e.message; });
}

function pollTaskStatus(taskId, el, body, tid) {
  if (el) el.innerHTML = I18N.t('chat.task.running');
  const token = localStorage.getItem('token');
  let attempts = 0;
  const timer = setInterval(() => {
    attempts++;
    fetch('/api/runtime/tasks/' + taskId + '/status', {
      headers: {'Authorization': 'Bearer ' + token},
    }).then(r => r.json()).then(d => {
      if (!d || !d.status) return;
      if (d.status === 'completed' || d.status === 'failed' || d.status === 'cancelled') {
        clearInterval(timer);
        _chatAbort = null;
        const text = d.result_text || (d.status === 'completed' ? 'Done' : I18N.t('chat.task.status_prefix', {status: d.status}));
        if (el) el.innerHTML = renderMarkdown(text);
        body.scrollTop = body.scrollHeight;
        loadConversations();
      } else if (d.status === 'running' || d.status === 'dispatched') {
        if (el) el.innerHTML = I18N.t('chat.task.running_with_time', {sec: Math.round(attempts * 1)});
      }
    }).catch(() => {});
    if (attempts > 180) { clearInterval(timer); _chatAbort = null; if (el) el.innerHTML = I18N.t('chat.task.timeout'); }
  }, 1000);
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