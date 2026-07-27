function renderSettings(tab) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const tabs = [
    {id:'credentials', label:'Credentials'},
    {id:'notifications', label:'Notifications'},
    {id:'gitcode', label:'GitCode Repo'},
  ];
  const current = tab || 'credentials';

  let html = `<div class="cd"><div class="cd-h"><h2>Settings</h2></div>
  <div class="stabs">${tabs.map(t => `<a class="${t.id === current ? 'ac' : ''}" onclick="renderSettings('${t.id}')">${t.label}</a>`).join('')}</div>`;

  if (current === 'credentials') {
    html += `<div id="settingsCredentials"><div class="loading">加载中...</div></div>`;
    html += `</div>`;
    el.innerHTML = html;
    API.get('/settings/credentials').then(d => {
      document.getElementById('settingsCredentials').innerHTML = `
        <div class="srow"><label>GitCode Token</label><span class="ts">${d.gitcode && d.gitcode.api_token_exists ? '已配置 ✓' : '未配置'}</span></div>
        <div class="srow"><label>华为云 AK</label><input id="huaweiAk" value="${(d.huawei_cloud && d.huawei_cloud.ak_exists) ? '***' : ''}" placeholder="Access Key"></div>
        <div class="srow"><label>华为云 SK</label><input id="huaweiSk" type="password" placeholder="Secret Key"></div>
        <button class="bt bt-p" onclick="saveCredentials()">Save</button>`;
    }).catch(e => document.getElementById('settingsCredentials').innerHTML = `<div class="error">加载失败: ${e.message}</div>`);
  } else if (current === 'notifications') {
    html += `<div id="settingsNotifications"><div class="loading">加载中...</div></div>`;
    html += `</div>`;
    el.innerHTML = html;
    const prefs = {issues: true, pr: true, build: false, release: true, vod: false};
    document.getElementById('settingsNotifications').innerHTML = `
      ${Object.entries(prefs).map(([k,v]) => `<div class="srow"><label>${k}</label><input type="checkbox" ${v ? 'checked' : ''} disabled></div>`).join('')}
      <div class="ts t2 mt8">通知偏好配置功能开发中</div>`;
  } else if (current === 'gitcode') {
    html += `<div id="settingsGitcode"><div class="loading">加载中...</div></div>`;
    html += `</div>`;
    el.innerHTML = html;
    const repo = app.currentRepo();
    if (repo) {
      API.get('/settings/gitcode-repo/' + repo.owner + '/' + repo.name).then(d => {
        document.getElementById('settingsGitcode').innerHTML = `
          <div class="srow"><label>Bot Token</label><span class="ts">${d.devops_bot_token_exists ? '已配置 ✓' : '未配置（使用默认）'}</span></div>
          <div class="srow"><label>Webhook Token</label><span class="ts">${d.webhook_token_exists ? '已配置 ✓' : '未配置'}</span></div>
          <div class="srow"><label>Webhook URL</label><span class="ts">${d.webhook_url || '—'}</span></div>
          <div class="srow"><label>Bot Username</label><span class="ts">${d.bot_username || 'devops-bot'}</span></div>
          <div class="srow"><label>New Bot Token</label><input id="newBotToken" type="password" placeholder="输入新的 Bot Token"></div>
          <div class="srow"><label>New Webhook Token</label><input id="newWebhookToken" type="password" placeholder="输入新的 Webhook Token"></div>
          <button class="bt bt-p" onclick="saveGitcodeConfig()">Save</button>`;
      }).catch(e => document.getElementById('settingsGitcode').innerHTML = `<div class="error">加载失败: ${e.message}</div>`);
    } else {
      document.getElementById('settingsGitcode').innerHTML = `<div class="ts t2">请先选择一个仓库</div>`;
    }
  } else {
    html += `<div class="ts t2">Settings tab: ${current}</div></div>`;
    el.innerHTML = html;
  }
}

function saveCredentials() {
  API.put('/settings/credentials', {
    huawei_cloud_ak: document.getElementById('huaweiAk').value || undefined,
    huawei_cloud_sk: document.getElementById('huaweiSk').value || undefined,
  }).then(r => app.showToast('凭据已更新')).catch(e => app.showToast('更新失败'));
}

function saveGitcodeConfig() {
  const repo = app.currentRepo();
  if (!repo) return;
  API.put('/settings/gitcode-repo/' + repo.owner + '/' + repo.name, {
    bot_token: document.getElementById('newBotToken').value || undefined,
    webhook_token: document.getElementById('newWebhookToken').value || undefined,
  }).then(r => app.showToast('配置已更新')).catch(e => app.showToast('更新失败'));
}
