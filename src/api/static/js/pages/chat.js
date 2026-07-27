function renderChat() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = `<div class="chat-wrap">
    <div class="chat-main">
      <div class="chat-hdr"><h2>Chat Agent</h2><div class="ts t2">Your personal DevOps assistant</div></div>
      <div class="chat-body" id="chatBody">
        <div class="msg r">你好！我是你的 DevOps 助手，有什么可以帮你的？</div>
      </div>
      <div class="chat-in">
        <input id="chatInput" placeholder="输入消息..." onkeydown="if(event.key==='Enter')sendChatStream()">
        <button class="bt bt-p" onclick="sendChatStream()">发送</button>
      </div>
    </div>
    <div class="chat-side">
      <div class="cd"><div class="cd-h"><h2>系统状态</h2></div>
      <div class="agc"><span class="ad" style="background:var(--g)"></span>VOD Agent<div class="ctc">47 signals</div></div>
      <div class="agc"><span class="ad" style="background:var(--y)"></span>Issue Agent<div class="ctc">3 active</div></div>
      <div class="agc"><span class="ad" style="background:var(--g)"></span>System<div class="ctc">Online</div></div>
      </div>
      <div class="cd" id="chatConfigPanel">
        <div class="cd-h"><h2>Backend Config</h2></div>
        <div class="ts t2">Chat 配置可在 Settings 中管理</div>
      </div>
    </div>
  </div>`;
}

let currentStreamAbort = null;

function sendChatStream() {
  const input = document.getElementById('chatInput');
  const body = document.getElementById('chatBody');
  if (!input || !input.value.trim()) return;

  const msg = input.value.trim();
  input.value = '';
  body.innerHTML += `<div class="msg l">${msg}</div>`;

  const thinkingId = 'think-' + Date.now();
  body.innerHTML += `<div class="msg r" id="${thinkingId}">思考中...</div>`;
  body.scrollTop = body.scrollHeight;

  const repo = app.currentRepo();
  const token = localStorage.getItem('token');

  if (currentStreamAbort) {
    currentStreamAbort.abort();
    currentStreamAbort = null;
  }
  const abortController = new AbortController();
  currentStreamAbort = abortController;

  fetch('/api/chat/send/stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
    body: JSON.stringify({message: msg, repo_key: repo ? repo.fullName : ''}),
    signal: abortController.signal,
  }).then(response => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finalText = '';

    function readChunk() {
      reader.read().then(({done, value}) => {
        if (done) {
          document.getElementById(thinkingId).innerHTML = finalText || '完成';
          body.scrollTop = body.scrollHeight;
          currentStreamAbort = null;
          return;
        }
        buffer += decoder.decode(value, {stream: true});
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.substring(6));
              if (data.type === 'done' || data.type === '完成') {
                finalText = data.text;
              } else if (data.text) {
                const el = document.getElementById(thinkingId);
                if (el) el.innerHTML = data.text;
              }
            } catch (e) { /* skip malformed SSE */ }
          }
        }
        body.scrollTop = body.scrollHeight;
        readChunk();
      }).catch(e => {
        if (e.name !== 'AbortError') {
          document.getElementById(thinkingId).innerHTML = 'Error: ' + e.message;
        }
      });
    }
    readChunk();
  }).catch(e => {
    document.getElementById(thinkingId).innerHTML = '请求失败: ' + e.message;
  });
}
