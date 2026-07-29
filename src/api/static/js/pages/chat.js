let _currentConvId = null;
let _chatAbort = null;

function renderChat() {
  _currentConvId = null;
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = `<div class="chat-wrap">
    <div class="chat-convlist" id="convList"></div>
    <div class="chat-main">
      <div class="chat-hdr"><h2>Chat Agent · Personal Assistant</h2></div>
      <div class="chat-body" id="chatBody">
        <div style="text-align:center;color:var(--t3);padding:60px 20px">选择一个对话或创建新对话</div>
      </div>
      <div class="chat-in"><input id="chatInput" placeholder="Ask me anything..." style="flex:1" disabled><button class="bt bt-p" onclick="sendChatStream()" disabled>Send</button></div>
    </div>
    <div class="chat-side" id="chatSide"></div>
  </div>`;
  loadConversations();
  loadSideConfig();
  setupChatStream();
}

function loadSideConfig() {
  API.get('/chat/config').then(d => {
    const feishu = d.im?.feishu || {};
    const backend = d.backend || {};
    const backendsOpts = (d._backends || []).map(b =>
      `<option value="${b.name}" ${b.name === (backend.type || '').toLowerCase() ? 'selected' : ''}>${b.display_name || b.name}</option>`
    ).join('') || '<option>Default</option>';
    const side = document.getElementById('chatSide');
    if (!side) return;
    side.innerHTML = `
      <div class="cd" style="padding:12px;font-size:11px">
        <div style="font-weight:600;margin-bottom:8px">Backend</div>
        <select id="cfgBackend" style="width:100%;padding:5px 8px;border:1px solid var(--b);border-radius:4px;font-size:11px;background:var(--bg)">${backendsOpts}</select>
      </div>
      <div class="cd" style="padding:12px;font-size:11px">
        <div style="font-weight:600;margin-bottom:8px">IM</div>
        <select id="cfgImProvider" onchange="switchImProvider()" style="width:100%;padding:4px 6px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg);margin-bottom:6px">
          <option value="feishu" ${d.im?.feishu?.app_id ? 'selected' : ''}>Feishu</option>
          <option value="wecom" ${d.im?.wecom?.app_id ? 'selected' : ''}>WeCom</option>
          <option value="dingtalk" ${d.im?.dingtalk?.app_id ? 'selected' : ''}>DingTalk</option>
        </select>
        <div style="margin-bottom:4px"><span class="t3">App ID</span></div>
        <input id="cfgImAppId" placeholder="App ID" value="${feishu.app_id || ''}" style="width:100%;padding:4px 6px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg);margin-bottom:4px">
        <div style="margin-bottom:4px"><span class="t3">App Secret</span></div>
        <input id="cfgImSecret" type="password" placeholder="App Secret" value="${feishu.app_secret_exists ? '********' : ''}" style="width:100%;padding:4px 6px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg);margin-bottom:6px">
        <button class="bt bt-p bt-s" onclick="saveChatConfig()" style="width:100%">Save</button>
      </div>
      <div class="cd" style="padding:12px;font-size:11px">
        <div style="font-weight:600;margin-bottom:6px">Agents</div>
        <div class="chat-agents" id="chatAgents"></div>
      </div>`;
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

function loadConversations() {
  API.get('/chat/conversations').then(d => {
    const convs = d.conversations || [];
    const list = document.getElementById('convList');
    if (!list) return;
    let html = '<div style="padding:10px 12px;border-bottom:1px solid var(--b)"><button class="bt bt-p bt-s" onclick="newConversation()" style="width:100%">＋ 新对话</button></div>';
    convs.forEach(c => {
      const active = c.id === _currentConvId ? ' style="background:var(--b)"' : '';
      html += `<div class="conv-item" data-id="${c.id}"${active}>
        <div class="conv-title">${c.title || '新对话'}</div>
        <div class="conv-meta">${(c.created_at || '').substring(0,16)}</div>
      </div>`;
    });
    if (convs.length === 0) {
      html += '<div style="padding:20px;text-align:center;color:var(--t3);font-size:12px">暂无对话</div>';
    }
    list.innerHTML = html;
    list.querySelectorAll('.conv-item').forEach(item => {
      item.addEventListener('click', () => switchConversation(item.dataset.id));
    });
  }).catch(() => {});
}

function newConversation() {
  API.post('/chat/conversations', {}).then(d => {
    _currentConvId = d.conversation_id;
    loadConversations();
    document.getElementById('chatBody').innerHTML = '';
    document.getElementById('chatInput').disabled = false;
    document.querySelector('.chat-in button').disabled = false;
  }).catch(e => app.showToast('创建失败: ' + e.message));
}

function switchConversation(convId) {
  _currentConvId = convId;
  loadConversations();
  const body = document.getElementById('chatBody');
  const input = document.getElementById('chatInput');
  body.innerHTML = '<div class="loading">加载中...</div>';
  API.get('/chat/messages/' + convId).then(d => {
    const msgs = d.messages || [];
    body.innerHTML = msgs.map(m =>
      `<div class="msg ${m.role === 'user' ? 'l' : 'r'}">${m.content}</div>`
    ).join('') || '<div style="text-align:center;color:var(--t3);padding:40px">开始新对话</div>';
    body.scrollTop = body.scrollHeight;
    if (input) input.disabled = false;
    const sendBtn = document.querySelector('.chat-in button');
    if (sendBtn) sendBtn.disabled = false;
  }).catch(() => {
    body.innerHTML = '<div class="error">加载失败</div>';
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
    API.post('/chat/conversations', {}).then(d => {
      _currentConvId = d.conversation_id;
      loadConversations();
      doSend(input, body);
    }).catch(e => app.showToast('创建对话失败: ' + e.message));
    return;
  }
  doSend(input, body);
}

function doSend(input, body) {
  const msg = input.value.trim();
  input.value = '';
  body.innerHTML += '<div class="msg l">' + msg.replace(/</g,'&lt;') + '</div>';
  const tid = 't' + Date.now();
  body.innerHTML += '<div class="msg r" id="' + tid + '">Thinking...</div>';
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
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (el) el.innerHTML = 'Error: ' + (err.detail?.message || resp.statusText);
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
            if (d.text && el) el.innerHTML = d.text;
          }
        });
      } catch(e) {}
      if (el) el.innerHTML = text || 'Done';
      loadConversations();
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '', finalText = '';
    function read() {
      reader.read().then(({done, value}) => {
        if (done) {
          if (el) el.innerHTML = finalText || 'Done';
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
              else if (d.text && el) el.innerHTML = d.text;
            } catch(e) {}
          }
        });
        body.scrollTop = body.scrollHeight;
        read();
      }).catch(e => { if (e.name !== 'AbortError' && el) el.innerHTML = 'Error: ' + e.message; });
    }
    read();
  }).catch(e => { const el = document.getElementById(tid); if (el) el.innerHTML = 'Request failed: ' + e.message; });
}

function saveChatConfig() {
  const backend = document.getElementById('cfgBackend')?.value || '';
  const provider = document.getElementById('cfgImProvider')?.value || 'feishu';
  const appId = document.getElementById('cfgImAppId')?.value || '';
  const secret = document.getElementById('cfgImSecret')?.value || '';
  const im = {};
  im[provider] = {app_id: appId, app_secret: secret};
  API.put('/chat/config', {backend, im}).then(() => {
    app.showToast('配置已保存');
  }).catch(e => app.showToast('保存失败: ' + e.message));
}

function switchImProvider() {
  const sel = document.getElementById('cfgImProvider');
  if (!sel) return;
  const provider = sel.value;
  // Try to load provider-specific config from the DOM or API
  // For now just update the section header
}