function renderSettings(tab) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const current = tab || 'credentials';
  const tabs = [
    {id:'credentials', label:I18N.t('settings.tab.credentials')},
    {id:'notifications', label:I18N.t('settings.tab.notifications')},
    {id:'appearance', label:I18N.t('settings.tab.appearance')},
    {id:'advanced', label:I18N.t('settings.tab.advanced')},
  ];

  let html = `<div class="cd"><div class="cd-h"><h2>${I18N.t('settings.page_title')}</h2></div>`;
  // gitcode 是独立页面，不显示通用设置的 tabs 标签栏
  if (current !== 'gitcode') {
    html += `<div class="stabs">${tabs.map(t => `<a class="${t.id === current ? 'ac' : ''}" onclick="renderSettings('${t.id}')">${t.label}</a>`).join('')}</div>`;
  }

  if (current === 'credentials') {
    html += `<div id="sCredentials"><div class="loading">${I18N.t('settings.common.loading')}</div></div></div>`;
    el.innerHTML = html;
    const curRepo = app.currentRepo();
    const repoQ = curRepo && curRepo.fullName ? '?repo_key=gitcode:' + encodeURIComponent(curRepo.fullName) : '';
    API.get('/settings/credentials' + repoQ).then(d => {
      // AK/SK 回显真实值需 HTML 转义（防引号破坏属性）
      const _esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;');
      const _akVal = d.huawei_cloud ? _esc(d.huawei_cloud.ak || '') : '';
      const _skVal = d.huawei_cloud ? _esc(d.huawei_cloud.sk || '') : '';
      document.getElementById('sCredentials').innerHTML = `
        <div class="cd"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">${I18N.t('settings.credentials.gitcode_api_title')}</h3>
          <div class="srow"><label>API Token</label><input type="password" value="${d.gitcode && d.gitcode.api_token_exists ? '********' : ''}" ${d.gitcode && d.gitcode.api_token_exists ? '' : `placeholder="${I18N.t('settings.credentials.gitcode_token_placeholder')}"`} style="${d.gitcode && d.gitcode.api_token_exists ? 'background:#f1f5f9' : ''}" ${d.gitcode && d.gitcode.api_token_exists ? 'disabled' : ''}></div>
          <div class="srow"><label>Account</label><input type="text" value="${d.gitcode && d.gitcode.account ? d.gitcode.account + I18N.t('settings.credentials.gitcode_account_suffix') : '—'}" disabled style="background:#f1f5f9;font-size:10px"></div>
        </div>
        <div class="cd mt16"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">${I18N.t('settings.credentials.huawei_cloud_title')}</h3>
          <div class="srow"><label>Access Key</label><input type="password" id="huaweiAk" value="${_akVal}" autocomplete="new-password" placeholder="Access Key"><button class="bt bt-o bt-s" id="huaweiToggleAk" onclick="toggleHuaweiSecret()" title="${I18N.t('settings.theme.show_hide')}" style="padding:2px 8px">👁</button></div>
          <div class="srow"><label>Secret Key</label><input type="password" id="huaweiSk" value="${_skVal}" autocomplete="new-password" placeholder="Secret Key"><button class="bt bt-o bt-s" id="huaweiToggleSk" onclick="toggleHuaweiSecret()" title="${I18N.t('settings.theme.show_hide')}" style="padding:2px 8px">👁</button></div>
          <p class="tx t2 mt8">${I18N.t('settings.credentials.huawei_cloud_desc')}</p>
          <button class="bt bt-p mt8" onclick="saveCredentials()">${I18N.t('settings.common.save')}</button>
        </div>


`;
    }).catch(e => document.getElementById('sCredentials').innerHTML = I18N.t('settings.common.load_failed', {msg: e.message}));
  } else if (current === 'notifications') {
    html += `<div class="cd"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">Notification Preferences</h3>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox" checked> Issue events</label>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox" checked> PR events</label>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox"> Build failures</label>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox" checked> Release events</label>
      <label style="display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px"><input type="checkbox"> VOD signals</label>
    </div></div>`;
    el.innerHTML = html;
  } else if (current === 'appearance') {
    // Appearance / Theme selection
    const curTheme = app.getTheme ? app.getTheme() : (localStorage.getItem('grapeTheme') || 'default');
    const curMode = app.getThemeMode ? app.getThemeMode() : (localStorage.getItem('grapeThemeMode') || 'light');
    const curLang = (typeof I18N !== 'undefined') ? I18N.getLang() : (localStorage.getItem('grapeLang') || 'zh');
    html += `
    <div class="cd">
      <h3 style="font-size:13px;font-weight:600;margin-bottom:16px">${I18N.t('settings.appearance.title')}</h3>
      <p class="tx t2" style="margin-bottom:20px;font-size:12px;line-height:1.6">
        ${I18N.t('settings.appearance.theme_desc')}
      </p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">
        <div id="themeCardDefault" onclick="selectTheme('default')"
          style="cursor:pointer;border:2px solid ${curTheme==='default'?'var(--a)':'var(--b)'};border-radius:14px;padding:20px;transition:all .25s;background:var(--c)">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
            <span style="font-size:14px;font-weight:600;color:var(--t)">Grape Default</span>
            ${curTheme==='default'?'<span style="color:var(--a);font-size:12px;font-weight:600">'+I18N.t('settings.appearance.current')+'</span>':''}
          </div>
          <div style="height:80px;border-radius:10px;overflow:hidden;border:1px solid var(--b);display:flex">
            <div style="width:60px;background:#0c1222;display:flex;flex-direction:column;align-items:center;padding:8px 0;gap:4px">
              <div style="width:24px;height:3px;background:#1e2940;border-radius:2px"></div>
              <div style="width:20px;height:3px;background:#1e2940;border-radius:2px"></div>
              <div style="width:20px;height:3px;background:#3168e0;border-radius:2px"></div>
              <div style="width:20px;height:3px;background:#1e2940;border-radius:2px"></div>
            </div>
            <div style="flex:1;background:#f5f6f8;padding:10px">
              <div style="height:4px;width:60%;background:#101724;border-radius:2px;margin-bottom:6px"></div>
              <div style="height:3px;width:40%;background:#5b667b;border-radius:2px;margin-bottom:8px"></div>
              <div style="height:30px;background:#fff;border-radius:6px;border:1px solid #e3e6ee"></div>
            </div>
          </div>
          <p style="font-size:11px;color:var(--t3);margin-top:10px">${I18N.t('settings.appearance.default_desc')}</p>
        </div>
        <div id="themeCardApple" onclick="selectTheme('apple')"
          style="cursor:pointer;border:2px solid ${curTheme==='apple'?'var(--a)':'var(--b)'};border-radius:14px;padding:20px;transition:all .25s;background:var(--c)">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
            <span style="font-size:14px;font-weight:600;color:var(--t)">Liquid Glass</span>
            ${curTheme==='apple'?'<span style="color:var(--a);font-size:12px;font-weight:600">'+I18N.t('settings.appearance.current')+'</span>':''}
          </div>
          <div style="height:80px;border-radius:10px;overflow:hidden;border:1px solid #d2d2d7;display:flex">
            <div style="width:60px;background:rgba(245,245,247,0.8);backdrop-filter:blur(10px);display:flex;flex-direction:column;align-items:center;padding:8px 0;gap:4px">
              <div style="width:24px;height:3px;background:#d2d2d7;border-radius:2px"></div>
              <div style="width:20px;height:3px;background:#d2d2d7;border-radius:2px"></div>
              <div style="width:20px;height:3px;background:#0071e3;border-radius:2px"></div>
              <div style="width:20px;height:3px;background:#d2d2d7;border-radius:2px"></div>
            </div>
            <div style="flex:1;background:#f5f5f7;padding:10px">
              <div style="height:4px;width:60%;background:#1d1d1f;border-radius:2px;margin-bottom:6px"></div>
              <div style="height:3px;width:40%;background:#6e6e73;border-radius:2px;margin-bottom:8px"></div>
              <div style="height:30px;background:#fff;border-radius:12px;border:1px solid #d2d2d7;box-shadow:0 2px 8px rgba(0,0,0,0.04)"></div>
            </div>
          </div>
          <p style="font-size:11px;color:var(--t3);margin-top:10px">${I18N.t('settings.appearance.apple_desc')}</p>
        </div>
      </div>
    </div>
    <div class="cd mt16" id="themeModeSection" style="${curTheme==='apple'?'':'opacity:0.4;pointer-events:none'}">
      <h3 style="font-size:13px;font-weight:600;margin-bottom:12px">${I18N.t('settings.appearance.mode_title')}</h3>
      <p class="tx t2" style="margin-bottom:14px;font-size:12px">${I18N.t('settings.appearance.mode_desc')}</p>
      <div style="display:flex;gap:10px">
        <button onclick="selectThemeMode('light')"
          style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:${curMode==='light'?'var(--a)':'var(--c)'};color:${curMode==='light'?'#fff':'var(--t2)'};transition:all .2s">
          ${I18N.t('settings.appearance.light')}
        </button>
        <button onclick="selectThemeMode('auto')"
          style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:${curMode==='auto'?'var(--a)':'var(--c)'};color:${curMode==='auto'?'#fff':'var(--t2)'};transition:all .2s">
          ${I18N.t('settings.appearance.auto')}
        </button>
      </div>
    </div>
    <div class="cd mt16">
      <h3 style="font-size:13px;font-weight:600;margin-bottom:12px">${I18N.t('settings.language.title')}</h3>
      <p class="tx t2" style="margin-bottom:14px;font-size:12px">${I18N.t('settings.language.desc')}</p>
      <div style="display:flex;gap:10px">
        <button onclick="selectLang('zh')"
          style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:${curLang==='zh'?'var(--a)':'var(--c)'};color:${curLang==='zh'?'#fff':'var(--t2)'};transition:all .2s">
          ${I18N.t('settings.language.zh')}
        </button>
        <button onclick="selectLang('en')"
          style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:${curLang==='en'?'var(--a)':'var(--c)'};color:${curLang==='en'?'#fff':'var(--t2)'};transition:all .2s">
          ${I18N.t('settings.language.en')}
        </button>
      </div>
    </div>
    </div>`;
    el.innerHTML = html;
  } else if (current === 'advanced') {
    // Advanced settings — pipeline mode toggle
    html += `<div id="sAdvanced"><div class="loading">${I18N.t('settings.common.loading')}</div></div></div>`;
    el.innerHTML = html;
    API.get('/settings/pipeline-mode').then(d => {
      const mode = d.mode || 'legacy';
      const isPlugin = mode === 'plugin';
      document.getElementById('sAdvanced').innerHTML = `
        <div class="cd">
          <h3 style="font-size:13px;font-weight:600;margin-bottom:12px">${I18N.t('settings.pipeline.title')}</h3>
          <p class="tx t2" style="margin-bottom:16px;font-size:12px;line-height:1.6">
            ${I18N.t('settings.pipeline.plugin_desc')}<br>
            ${I18N.t('settings.pipeline.traditional_desc')}
          </p>
          <div style="display:flex;align-items:center;gap:12px">
            <button id="pipelineModeToggle" onclick="togglePipelineModeSetting()"
              style="padding:10px 24px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:1px solid ${isPlugin?'var(--a)':'var(--b)'};background:${isPlugin?'var(--a)':'var(--c)'};color:${isPlugin?'#fff':'var(--t2)'};transition:all .2s">
              ${isPlugin ? I18N.t('settings.pipeline.plugin') : I18N.t('settings.pipeline.traditional')}
            </button>
            <span style="font-size:11px;color:var(--t3)">${isPlugin ? I18N.t('settings.pipeline.status_plugin') : I18N.t('settings.pipeline.status_legacy')}</span>
          </div>
          <p style="font-size:11px;color:var(--t3);margin-top:12px">${I18N.t('settings.pipeline.warning')}</p>
        </div>`;
    }).catch(e => {
      document.getElementById('sAdvanced').innerHTML = I18N.t('settings.common.load_failed', {msg: e.message});
    });
  } else if (current === 'gitcode') {
    // GitCode Repo 页面 — 需要已有仓库
    const repo = app.currentRepo();
    if (!repo) {
      html += `<div class="ts t2">${I18N.t('settings.host.select_repo_first')}</div></div>`;
      el.innerHTML = html;
      return;
    }
    html += `<div id="sGitcode"><div class="loading">${I18N.t('settings.common.loading')}</div></div></div>`;
    el.innerHTML = html;

    API.get('/settings/gitcode-repo/' + repo.owner + '/' + repo.name).then(d => {
        document.getElementById('sGitcode').innerHTML = `
        <div class="cd"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">${I18N.t('settings.credentials.devops_bot_title')}</h3>
          <div class="srow"><label>Bot Token</label><input type="password" id="gitcodeBotToken" value="" placeholder="${d.devops_bot_token_exists ? I18N.t('settings.credentials.bot_token_placeholder_configured') : I18N.t('settings.credentials.bot_token_placeholder_empty')}">${d.devops_bot_token_exists ? '<span class="ts" style="color:var(--g);font-size:10px">'+I18N.t('settings.credentials.bot_configured')+'</span>' : ''}</div>
          <div class="srow"><label>Scope</label><input type="text" value="read_repo, write_repo, manage_webhook" disabled style="background:#f1f5f9;font-size:10px"></div>
          <button class="bt bt-p mt8" onclick="saveGitcodeRepoConfig()">${I18N.t('settings.common.save')}</button>
        </div>
        <div class="cd mt16" style="border-color:var(--r)">
          <div class="cd-h"><h2 style="color:var(--r)">${I18N.t('settings.host.unhost_title')}</h2></div>
          <p class="tx t2" style="margin-bottom:8px;color:var(--r)">${I18N.t('settings.host.unhost_warning')}</p>
          <button class="bt" style="background:var(--r);color:#fff;border:none;padding:8px 16px" onclick="unhostFromSettings('${repo.fullName}')">${I18N.t('settings.host.unhost_title')}</button>
        </div>`;
    }).catch(e => document.getElementById('sGitcode').innerHTML = I18N.t('settings.common.load_failed', {msg: e.message}));
  } else {
    html += `<div class="ts t2">${current}</div></div>`;
    el.innerHTML = html;
  }
}

// 占位符常量（与后端 _is_placeholder 同步；前端双保险：旧缓存页面也可能带占位符值）
const _CRED_PLACEHOLDERS = ['AK-demo-access-key-xxxx', 'SK-demo-secret-key-xxxx', 'ghp_devopsbot_xxxxxxxxxxxxxxxxxxxx'];
function _isCredPlaceholder(v) {
  v = (v || '').trim();
  if (!v) return true;                       // 空 = 无值
  if (_CRED_PLACEHOLDERS.indexOf(v) >= 0) return true;
  return /^(AK-demo-|SK-demo-|ghp_devopsbot_)/.test(v);  // 任何 demo/占位前缀一律拒绝
}

function saveCredentials() {
  const ak = document.getElementById('huaweiAk');
  const sk = document.getElementById('huaweiSk');
  const body = {};
  const akVal = ak && ak.value ? ak.value.trim() : '';

  const skVal = sk && sk.value ? sk.value.trim() : '';
  if (akVal && !_isCredPlaceholder(akVal)) body.huawei_cloud_ak = akVal;
  if (skVal && !_isCredPlaceholder(skVal)) body.huawei_cloud_sk = skVal;

  // v4.3：发布令牌按仓库（项目维度）——带当前仓库
  const cur = app.currentRepo();
  if (cur && cur.fullName) {
    const parts = cur.fullName.split('/');
    if (parts.length === 2) body.repo_key = 'gitcode:' + cur.fullName;
  }
  API.put('/settings/credentials', body).then(r => {
    // 发布目标仓库（配置）与凭据区分提示：含配置类字段提示"配置已更新"
    app.showToast(I18N.t('settings.common.cred_updated'));
  }).catch(e => app.showToast(I18N.t('settings.common.update_failed')));
}

// 华为云 AK/SK 明文显示/隐藏切换（默认掩码，👁 查看明文；autocomplete=new-password 防浏览器自动填充覆盖）
let _hwSecretVisible = false;
function toggleHuaweiSecret() {
  _hwSecretVisible = !_hwSecretVisible;
  ['huaweiAk', 'huaweiSk'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.type = _hwSecretVisible ? 'text' : 'password';
  });
  ['huaweiToggleAk', 'huaweiToggleSk'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.textContent = _hwSecretVisible ? '🙈' : '👁';
  });
}

function saveGitcodeRepoConfig() {
  const repo = app.currentRepo();
  if (!repo) return;
  const botEl = document.getElementById('gitcodeBotToken');
  const body = {};
  const botVal = botEl && botEl.value ? botEl.value.trim() : '';
  if (botVal && !_isCredPlaceholder(botVal)) body.bot_token = botVal;
  API.put('/settings/gitcode-repo/' + repo.owner + '/' + repo.name, body).then(r => app.showToast(I18N.t('settings.credentials.repo_config_updated'))).catch(e => app.showToast(I18N.t('settings.common.update_failed')));
}

function unhostFromSettings(fullName) {
  if (!confirm(I18N.t('settings.host.confirm_remove', {name: fullName}))) return;
  API.post('/repos/remove', {full_name: fullName}).then(r => {
    app.showToast(I18N.t('settings.host.unhost_done'));
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
          document.getElementById('pageTitle').textContent = I18N.t('settings.host.select_repo');
          renderRepoSelection(repos);
        });
      } else {
        app.nav('dash');
      }
    });
  }).catch(e => app.showToast(I18N.t('settings.host.unhost_failed', {msg: e.message})));
}

function applyScene(owner, repo) {
  const sceneId = document.getElementById('sceneSelect').value;
  if (!sceneId) return app.showToast(I18N.t('settings.scene.select_first'));
  API.post('/scenes/apply/' + owner + '/' + repo, {scene_id: sceneId})
    .then(r => app.showToast(r.message || I18N.t('settings.scene.applied')))
    .catch(e => app.showToast(I18N.t('settings.scene.apply_failed', {msg: e.message || e})));
}

// ── Theme switching ──
function selectTheme(theme) {
  if (!app.setTheme) { app.showToast(I18N.t('settings.theme.switch_unavailable')); return; }
  app.setTheme(theme);
  // re-render to update selection visual + toggle mode section
  renderSettings('appearance');
  app.showToast(theme === 'apple' ? I18N.t('settings.theme.switched_apple') : I18N.t('settings.theme.switched_default'));
}

function selectThemeMode(mode) {
  if (!app.setThemeMode) return;
  app.setThemeMode(mode);
  renderSettings('appearance');
}

function selectLang(lang) {
  if (lang === I18N.getLang()) return;
  I18N.setLang(lang);
}

// ── Pipeline mode toggle ──
function togglePipelineModeSetting() {
  const btn = document.getElementById('pipelineModeToggle');
  if (!btn) return;
  const isPlugin = btn.textContent.includes('✦');
  const newMode = isPlugin ? 'legacy' : 'plugin';
  API.put('/settings/pipeline-mode', {mode: newMode}).then(d => {
    app.showToast(d.message);
    renderSettings('advanced');
  }).catch(e => app.showToast(I18N.t('settings.pipeline.switch_failed', {msg: e.message || I18N.t('settings.pipeline.unknown_error')})));
}
