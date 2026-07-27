function renderChat() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = `<div class="chat-wrap">
    <div class="chat-main">
      <div class="chat-hdr"><h2>Chat Agent · Personal Assistant</h2><div class="tx t2">Your personal agent · Backend: <b>Default</b></div></div>
      <div class="chat-body" id="chatBody">
        <div class="msg r">Hello! I'm your personal DevOps assistant. How can I help you today?</div>
        <div class="msg l">Check the status of issue #342</div>
        <div class="msg r">✅ Issue #342 "Login timeout" is with <b>QA Agent</b> (PR #1278 under review). ETA: ~15 min.</div>
        <div class="msg r">ℹ️ VOD Agent found 2 new signals. One may interest you: "API response time degraded after deploy". Want me to create an issue?</div>
      </div>
      <div class="chat-in"><input id="chatInput" placeholder="Ask me anything..." style="flex:1"><button class="bt bt-p" onclick="sendChatStream()">Send</button></div>
    </div>
    <div class="chat-side">
      <div class="ts" style="font-weight:600;color:var(--t);margin-bottom:2px">★ Agents</div>
      <div class="agc" style="background:#fde8f0"><span class="ad" style="background:var(--pk)"></span>VOD Agent<span class="ctc">47</span></div>
      <div class="agc" style="background:#fce5e5"><span class="ad" style="background:var(--r)"></span>Issue Agent<span class="ctc">3</span></div>
      <div class="agc" style="background:#e3edfd"><span class="ad" style="background:var(--a)"></span>Coding Agent<span class="ctc">1</span></div>
      <div class="agc" style="background:#f3e9fe"><span class="ad" style="background:var(--pu)"></span>QA Agent<span class="ctc">2</span></div>
      <div class="agc" style="background:#fef0e4"><span class="ad" style="background:var(--or)"></span>SRE Agent<span class="ctc">v2.3.1</span></div>

      <div class="cd" style="padding:10px;font-size:10px">
        <div class="ts" style="font-weight:600;color:var(--t);margin-bottom:8px">★ Backend</div>
        <div class="tx t3" style="margin-bottom:2px">Type</div>
        <select style="width:100%;padding:4px 6px;margin-bottom:6px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg)">
          <option>Default (Built-in)</option><option>Custom (OpenClaw)</option><option>Custom (Hermes)</option>
        </select>
        <div class="tx t3" style="margin-bottom:2px">URL</div>
        <input type="text" value="localhost:8080/chat" disabled style="width:100%;padding:4px 6px;margin-bottom:6px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg)">
        <div class="tx t3" style="margin-bottom:2px">API Key</div>
        <input type="password" value="********" style="width:100%;padding:4px 6px;margin-bottom:4px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg)">
      </div>

      <div class="cd" style="padding:10px;font-size:10px">
        <div class="ts" style="font-weight:600;color:var(--t);margin-bottom:8px">★ IM</div>
        <div style="margin-bottom:8px;padding:4px 6px;background:#f0fdf4;border-radius:4px"><span class="tgx">✓</span> Feishu <button class="bt bt-o bt-s" style="font-size:9px;padding:2px 6px;float:right">↻</button></div>
        <div class="tx t3" style="margin-bottom:2px">App ID</div>
        <input type="text" value="cli_a8d3f2e1b4c5" disabled style="width:100%;padding:4px 6px;margin-bottom:4px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg)">
        <div class="tx t3" style="margin-bottom:2px">App Secret</div>
        <input type="password" value="xxxxxxxxxxxxxxxxxxxxxxxx" disabled style="width:100%;padding:4px 6px;margin-bottom:4px;border:1px solid var(--b);border-radius:4px;font-size:10px;background:var(--bg)">
        <div class="tx t2" style="margin-bottom:6px;font-size:9px">💡 <a style="color:var(--a);text-decoration:underline;cursor:pointer">Scan QR to auto-configure</a></div>
        <div class="flex fc jbtw" style="margin-bottom:4px"><span><span class="t3">○</span> WeCom</span><button class="bt bt-o bt-s" style="font-size:9px;padding:2px 6px">+</button></div>
        <div class="flex fc jbtw" style="margin-bottom:2px"><span><span class="t3">○</span> DingTalk</span><button class="bt bt-o bt-s" style="font-size:9px;padding:2px 6px">+</button></div>
      </div>
    </div>
  </div>`;
  setupChatStream();
}

let _chatAbort = null;

function setupChatStream() {
  const input = document.getElementById('chatInput');
  if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter') sendChatStream(); });
}

function sendChatStream() {
  const input = document.getElementById('chatInput');
  const body = document.getElementById('chatBody');
  if (!input || !input.value.trim()) return;
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
    body: JSON.stringify({message: msg, repo_key: repo ? repo.fullName : ''}),
    signal: ac.signal,
  }).then(resp => {
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '', finalText = '';
    function read() {
      reader.read().then(({done, value}) => {
        if (done) { document.getElementById(tid).innerHTML = finalText || 'Done'; body.scrollTop = body.scrollHeight; _chatAbort = null; return; }
        buf += dec.decode(value, {stream: true});
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        lines.forEach(line => {
          if (line.startsWith('data: ')) {
            try {
              const d = JSON.parse(line.substring(6));
              if (d.type === 'done') finalText = d.text;
              else if (d.text) { const e = document.getElementById(tid); if (e) e.innerHTML = d.text; }
            } catch(e) {}
          }
        });
        body.scrollTop = body.scrollHeight;
        read();
      }).catch(e => { if (e.name !== 'AbortError') document.getElementById(tid).innerHTML = 'Error: ' + e.message; });
    }
    read();
  }).catch(e => document.getElementById(tid).innerHTML = 'Request failed: ' + e.message);
}
