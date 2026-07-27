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
        <input id="chatInput" placeholder="输入消息..." onkeydown="if(event.key==='Enter')sendChat()">
        <button class="bt bt-p" onclick="sendChat()">发送</button>
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

function sendChat() {
  const input = document.getElementById('chatInput');
  const body = document.getElementById('chatBody');
  if (!input.value.trim()) return;
  body.innerHTML += `<div class="msg l">${input.value}</div>`;
  const msg = input.value;
  input.value = '';
  body.innerHTML += `<div class="msg r" style="color:var(--t3)">思考中...</div>`;
  body.scrollTop = body.scrollHeight;

  const repo = app.currentRepo();
  API.post('/chat/send', {message: msg, repo_key: repo ? repo.fullName : ''}).then(r => {
    body.removeChild(body.lastChild);
    body.innerHTML += `<div class="msg r">${r.reply}</div>`;
    body.scrollTop = body.scrollHeight;
  }).catch(e => {
    body.removeChild(body.lastChild);
    body.innerHTML += `<div class="msg r">Error: ${e.message}</div>`;
  });
}
