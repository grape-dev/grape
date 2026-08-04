function renderSettings(tab) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const current = tab || 'credentials';
  const tabs = [
    {id:'credentials', label:'Credentials'},
    {id:'notifications', label:'Notifications'},
  ];

  let html = `<div class="cd"><div class="cd-h"><h2>Settings</h2></div>
  <div class="stabs">${tabs.map(t => `<a class="${t.id === current ? 'ac' : ''}" onclick="renderSettings('${t.id}')">${t.label}</a>`).join('')}</div>`;

  if (current === 'credentials') {
    html += `<div id="sCredentials"><div class="loading">加载中...</div></div></div>`;
    el.innerHTML = html;
    API.get('/settings/credentials').then(d => {
      document.getElementById('sCredentials').innerHTML = `
        <div class="cd"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">GitCode API（OAuth 登录自动填充）</h3>
          <div class="srow"><label>API Token</label><input type="password" value="${d.gitcode && d.gitcode.api_token_exists ? '********' : ''}" ${d.gitcode && d.gitcode.api_token_exists ? '' : 'placeholder="未配置"'} style="${d.gitcode && d.gitcode.api_token_exists ? 'background:#f1f5f9' : ''}" ${d.gitcode && d.gitcode.api_token_exists ? 'disabled' : ''}></div>
          <div class="srow"><label>Account</label><input type="text" value="${d.gitcode && d.gitcode.account ? d.gitcode.account + ' · 登录时自动填充' : '—'}" disabled style="background:#f1f5f9;font-size:10px"></div>
        </div>
        <div class="cd mt16"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">Huawei Cloud</h3>
          <div class="srow"><label>Access Key</label><input type="password" id="huaweiAk" value="${d.huawei_cloud && d.huawei_cloud.ak_exists ? 'AK-demo-access-key-xxxx' : ''}" placeholder="Access Key"></div>
          <div class="srow"><label>Secret Key</label><input type="password" id="huaweiSk" value="${d.huawei_cloud && d.huawei_cloud.sk_exists ? 'SK-demo-secret-key-xxxx' : ''}" placeholder="Secret Key"></div>
          <p class="tx t2 mt8">Huawei Cloud CodeArts provides free cloud dev environment for Coding Agent.</p>
          <button class="bt bt-p mt8" onclick="saveCredentials()">Save</button>
        </div>`;
    }).catch(e => document.getElementById('sCredentials').innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
  } else if (current === 'notifications') {
    html += `<div class="cd"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">Notification Preferences</h3>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox" checked> Issue events</label>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox" checked> PR events</label>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox"> Build failures</label>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox" checked> Release events</label>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox"> VOD signals</label>
    </div></div>`;
    el.innerHTML = html;
  } else if (current === 'gitcode') {
    // GitCode Repo 页面 — 需要已有仓库
    const repo = app.currentRepo();
    if (!repo) {
      html += `<div class="ts t2">请先选择一个仓库</div></div>`;
      el.innerHTML = html;
      return;
    }
    html += `<div id="sGitcode"><div class="loading">加载中...</div></div></div>`;
    el.innerHTML = html;

    API.get('/settings/gitcode-repo/' + repo.owner + '/' + repo.name).then(d => {
        document.getElementById('sGitcode').innerHTML = `
        <div class="cd"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">DevOps Bot Token</h3>
          <div class="srow"><label>Bot Token</label><input type="password" id="gitcodeBotToken" value="${d.devops_bot_token_exists ? 'ghp_devopsbot_xxxxxxxxxxxxxxxxxxxx' : ''}" placeholder="不设置，将默认使用Grape官方机器人"></div>
          <div class="srow"><label>Scope</label><input type="text" value="read_repo, write_repo, manage_webhook" disabled style="background:#f1f5f9;font-size:10px"></div>
          <button class="bt bt-p mt8" onclick="saveGitcodeRepoConfig()">Save</button>
        </div>
        <div class="cd mt16" style="border-color:var(--r)">
          <div class="cd-h"><h2 style="color:var(--r)">解除托管</h2></div>
          <p class="tx t2" style="margin-bottom:8px;color:var(--r)">⚠️ 解除托管后，将删除该仓库在 Grape 上的所有资产数据（事件记录、会话、Agent 配置、仓库技能、工作区文件），且不可恢复。</p>
          <button class="bt" style="background:var(--r);color:#fff;border:none;padding:8px 16px" onclick="unhostFromSettings('${repo.fullName}')">解除托管</button>
        </div>`;
    }).catch(e => document.getElementById('sGitcode').innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
  } else {
    html += `<div class="ts t2">${current}</div></div>`;
    el.innerHTML = html;
  }
}

function saveCredentials() {
  const ak = document.getElementById('huaweiAk');
  const sk = document.getElementById('huaweiSk');
  API.put('/settings/credentials', {
    huawei_cloud_ak: ak && ak.value ? ak.value : undefined,
    huawei_cloud_sk: sk && sk.value ? sk.value : undefined,
  }).then(r => app.showToast('凭据已更新')).catch(e => app.showToast('更新失败'));
}

function saveGitcodeRepoConfig() {
  const repo = app.currentRepo();
  if (!repo) return;
  const botEl = document.getElementById('gitcodeBotToken');
  API.put('/settings/gitcode-repo/' + repo.owner + '/' + repo.name, {
    bot_token: botEl && botEl.value ? botEl.value : undefined,
  }).then(r => app.showToast('GitCode Repo 配置已更新')).catch(e => app.showToast('更新失败'));
}

function unhostFromSettings(fullName) {
  if (!confirm('⚠️ 确定要解除托管 ' + fullName + ' 吗？\n\n解除托管后，将删除该仓库在 Grape 上的所有资产数据（事件记录、会话、Agent 配置、仓库技能、工作区文件），且不可恢复。\n\n如需保留历史数据，请谨慎操作。')) return;
  API.post('/repos/remove', {full_name: fullName}).then(r => {
    app.showToast('已解除托管');
    const cur = app.currentRepo();
    const wasCurrent = cur && cur.fullName === fullName;
    if (wasCurrent) {
      localStorage.removeItem('currentRepo');
    }
    app.initSidebarRepos();
    API.get('/repos/managed').then(md => {
      const remaining = md.repos || [];
      if (wasCurrent || remaining.length === 0) {
        API.get('/repos/member').then(d => {
          const repos = d.repos || [];
          document.getElementById('sidebar').style.display = 'none';
          document.getElementById('tb').style.display = 'none';
          const mv = document.getElementById('mainView');
          mv.style.display = 'flex';
          mv.style.position = 'fixed';
          mv.style.inset = '0';
          mv.style.zIndex = '100';
          document.getElementById('pageTitle').textContent = '选择仓库';
          renderRepoSelection(repos);
        });
      } else {
        app.nav('dash');
      }
    });
  }).catch(e => app.showToast('解除托管失败: ' + e.message));
}

function applyScene(owner, repo) {
  const sceneId = document.getElementById('sceneSelect').value;
  if (!sceneId) return app.showToast('请选择一个场景');
  API.post('/scenes/apply/' + owner + '/' + repo, {scene_id: sceneId})
    .then(r => app.showToast(r.message || '场景已应用'))
    .catch(e => app.showToast('应用场景失败: ' + (e.message || e)));
}
