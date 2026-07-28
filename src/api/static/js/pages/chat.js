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
  </div>`;
  loadConversations();
  setupChatStream();
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
    // Bind click
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
  loadConversations(); // refresh highlight
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
    // Auto-create conversation
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
  body.innerHTML += '<div class="msg l">' + msg + '</div>';
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
  }).then(resp => {
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '', finalText = '';
    function read() {
      reader.read().then(({done, value}) => {
        if (done) {
          const el = document.getElementById(tid);
          if (el) el.innerHTML = finalText || 'Done';
          body.scrollTop = body.scrollHeight;
          _chatAbort = null;
          // reload conversations to update title
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
              else if (d.text) {
                const el = document.getElementById(tid);
                if (el) el.innerHTML = d.text;
              }
            } catch(e) {}
          }
        });
        body.scrollTop = body.scrollHeight;
        read();
      }).catch(e => { if (e.name !== 'AbortError') { const el = document.getElementById(tid); if (el) el.innerHTML = 'Error: ' + e.message; } });
    }
    read();
  }).catch(e => { const el = document.getElementById(tid); if (el) el.innerHTML = 'Request failed: ' + e.message; });
}