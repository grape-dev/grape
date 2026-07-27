function renderSettings(tab) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const current = tab || 'credentials';
  const tabs = [
    {id:'credentials', label:'Credentials'},
    {id:'notifications', label:'Notifications'},
    {id:'skills', label:'Skills'},
  ];

  let html = `<div class="cd"><div class="cd-h"><h2>Settings</h2></div>
  <p class="tx t3 mt8" style="margin-bottom:12px">> Chat Agent 相关设置（Backend、IM 集成）请在 Chat Agent 页面内配置</p>
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
  } else if (current === 'skills') {
    html += `<div class="cd"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">Custom Skills</h3>
      <button class="bt bt-o bt-s">Upload Skill</button>
      <div class="flex fc gap8 mt12" style="padding:8px 0;border-top:1px solid var(--b)">
        <span class="tg tg-c">team/backend-service</span><span class="tx t2">pull_request_opened</span>
        <span class="tx f1" style="color:var(--a);text-align:right">custom-pr-review</span>
        <button class="bt bt-o bt-s">Edit</button>
      </div>
    </div></div>`;
    el.innerHTML = html;
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
