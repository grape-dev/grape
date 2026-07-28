function renderBackends(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const engines = data.backends || [];

  API.get('/settings/credentials').then(credData => {
    const creds = credData || {};
    buildHtml(engines, creds);
  }).catch(() => buildHtml(engines, {}));
}

function buildHtml(engines, creds) {
  const el = document.getElementById('pageContent');
  if (!el) return;

  const CRED_MAP = {
    codearts: 'codearts',
  };

  let html = '';
  engines.forEach(e => {
    const name = e.name || '';
    const running = e.healthy;
    const installed = e.installed;
    const credKey = CRED_MAP[name];
    const credInfo = credKey ? (creds[credKey] || {}) : {};
    const hasCreds = !!credKey;

    html += `<div class="cd" style="padding:16px 20px">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
        <span style="width:10px;height:10px;border-radius:50%;background:${running ? 'var(--g)' : 'var(--t3)'};display:inline-block"></span>
        <h3 style="font-size:14px;font-weight:600">${e.display_name || name}</h3>
        <span class="ts t2" style="margin-left:8px">${installed ? 'Version: ' + (e.version || '?') : 'Not Installed'}</span>
      </div>
      ${!installed ? `
        <div class="srow"><label></label><button class="bt bt-o bt-s" onclick="app.showToast('请运行 pip install ${e.pip_package || name}')">Install Guide</button></div>
      ` : `
        <div class="srow"><label>Installed</label><span class="tgx">✓</span> Path: ${e.cli_path || '?'}</div>
        ${hasCreds ? `
          <div class="srow"><label>AK</label><input class="cred-ak" type="password" placeholder="${credInfo.ak_exists ? '已配置' : '未配置'}" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"></div>
          <div class="srow"><label>SK</label><input class="cred-sk" type="password" placeholder="${credInfo.sk_exists ? '已配置' : '未配置'}" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--bg)"></div>
        ` : ''}
        <div class="flex gap6" style="margin-left:120px">
          <button class="bt bt-o bt-s" onclick="app.showToast('连接测试功能开发中')">Test Connection</button>
          <button class="bt bt-o bt-s" onclick="app.showToast('日志功能开发中')">View Logs</button>
          ${hasCreds ? '<button class="bt bt-p bt-s" onclick="saveCreds(this)">Save AK/SK</button>' : ''}
        </div>
      `}
    </div>`;
  });
  html += '<button class="bt bt-o" style="margin-top:8px" onclick="refreshBackends()">⟳ Refresh All</button>';
  el.innerHTML = html;
}

function refreshBackends() {
  const el = document.getElementById('pageContent');
  if (el) el.innerHTML = '<div class="loading">刷新中...</div>';
  API.post('/settings/backend-agents/refresh').then(d => {
    renderBackends(d);
    app.showToast('刷新成功');
  }).catch(e => app.showToast('刷新失败: ' + e.message));
}

function saveCreds(btn) {
  const row = btn.closest('.cd');
  const akInput = row.querySelector('.cred-ak');
  const skInput = row.querySelector('.cred-sk');
  if (!akInput || !skInput) return;
  const body = {};
  if (akInput.value) body.codearts_ak = akInput.value;
  if (skInput.value) body.codearts_sk = skInput.value;
  API.put('/settings/credentials', body).then(() => {
    app.showToast('AK/SK 已保存');
  }).catch(e => app.showToast('保存失败: ' + e.message));
}