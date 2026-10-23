function renderSettings(tab) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const current = tab || 'gitcode';

  let html = `<div class="cd"><div class="cd-h"><h2>${I18N.t('settings.host.title')}</h2></div>`;

  if (current === 'gitcode') {
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
        var _sg = document.getElementById('sGitcode');
        if (!_sg) return;
        _sg.innerHTML = `
        <div class="cd"><h3 style="font-size:13px;font-weight:600;margin-bottom:12px">${I18N.t('settings.credentials.devops_bot_title')}</h3>
          ${d.bot_token_status === 'expired' ? '<div style="padding:8px 12px;background:rgba(255,59,48,.1);border-radius:6px;margin-bottom:8px;color:#ff3b30;font-size:12px">⚠️ ' + (I18N.t('app.error.REPO_TOKEN_EXPIRED')) + '</div>' : ''}
          <div class="srow"><label>${I18N.t('settings.credentials.bot_token')}</label><input type="password" id="gitcodeBotToken" value="" placeholder="${(function(){var _lv=d.resolved_token_level||'';if(d.has_repo_bot_token||_lv==='repo_bot')return I18N.t('settings.credentials.bot_token_placeholder_configured');if(_lv==='user')return I18N.t('settings.credentials.user_token_placeholder')||I18N.t('settings.credentials.bot_token_placeholder_configured');if(_lv==='global_bot')return I18N.t('settings.credentials.global_bot_placeholder')||I18N.t('settings.credentials.bot_token_placeholder_configured');return I18N.t('settings.credentials.bot_token_placeholder_empty');})()}">${(function(){var _lv=d.resolved_token_level||'';var _has=d.has_repo_bot_token;if(_has)return '<span class="ts" style="color:var(--g);font-size:10px">'+I18N.t('settings.credentials.bot_configured')+'</span>';if(_lv==='user')return '<span class="ts" style="color:var(--a);font-size:10px">'+(I18N.t('settings.credentials.user_token_active')||'User token active')+'</span>';if(_lv==='global_bot')return '<span class="ts" style="color:var(--a);font-size:10px">'+(I18N.t('settings.credentials.global_bot_active')||'Global bot token active')+'</span>';if(_lv==='session')return '<span class="ts" style="color:var(--a);font-size:10px">'+(I18N.t('settings.credentials.session_token_active')||'Session token active')+'</span>';return '';})()}</div>
          <div class="srow"><label>${I18N.t('settings.credentials.scope')}</label><input type="text" value="read_repo, write_repo, manage_webhook" disabled style="background:#f1f5f9;font-size:10px"></div>
          <button class="bt bt-p mt8" onclick="saveGitcodeRepoConfig()">${I18N.t('settings.common.save')}</button>
        </div>
        <div class="cd mt16" style="border-color:var(--r)">
          <div class="cd-h"><h2 style="color:var(--r)">${I18N.t('settings.host.unhost_title')}</h2></div>
          <p class="tx t2" style="margin-bottom:8px;color:var(--r)">${I18N.t('settings.host.unhost_warning')}</p>
          <button class="bt" style="background:var(--r);color:#fff;border:none;padding:8px 16px" onclick="unhostFromSettings('${repo.fullName}')">${I18N.t('settings.host.unhost_title')}</button>
        </div>`;
    }).catch(e => { var _sg = document.getElementById('sGitcode'); if (_sg) _sg.innerHTML = I18N.t('settings.common.load_failed', {msg: e.message}); });
  } else {
    html += `<div class="ts t2">${current}</div></div>`;
    el.innerHTML = html;
  }
}

// 占位符常量（与后端 _is_placeholder 同步；前端双保险：旧缓存页面也可能带占位符值）
const _CRED_PLACEHOLDERS = ['AK-demo-access-key-xxxx', 'SK-demo-secret-key-xxxx', 'ghp_devopsbot_xxxxxxxxxxxxxxxxxxxx'];
function _isCredPlaceholder(v) {
  v = (v || '').trim();
  if (!v) return true;
  if (_CRED_PLACEHOLDERS.indexOf(v) >= 0) return true;
  return /^(AK-demo-|SK-demo-|ghp_devopsbot_)/.test(v);
}

function saveGitcodeRepoConfig() {
  const repo = app.currentRepo();
  if (!repo) return;
  const botEl = document.getElementById('gitcodeBotToken');
  const body = {};
  const botVal = botEl && botEl.value ? botEl.value.trim() : '';
  if (botVal && !_isCredPlaceholder(botVal)) body.bot_token = botVal;
  API.put('/settings/gitcode-repo/' + repo.owner + '/' + repo.name, body).then(r => {
    app.showToast(I18N.t('settings.credentials.repo_config_updated'));
    if (r.warning) app.showToast(r.warning, 'warning');
    if (r.scope_warnings && r.scope_warnings.length) r.scope_warnings.forEach(w => app.showToast(w, 'warning'));
  }).catch(e => app.showToast(I18N.t('settings.common.update_failed')));
}

function unhostFromSettings(fullName) {
  app.showConfirm(I18N.t('settings.host.confirm_remove', {name: fullName}), function() {
    API.post('/repos/remove', {full_name: fullName}).then(r => {
      app.showToast(I18N.t('settings.host.unhost_done'));
      // R4: 清除仓库缓存
      localStorage.removeItem('repo_member_cache');
      const cur = app.currentRepo();
      const wasCurrent = cur && cur.fullName === fullName;
      if (wasCurrent) {
        sessionStorage.removeItem('currentRepo');
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
  }, {danger: true});
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

// _langSwitching：语言切换全局互斥锁
// 已知行为：加载期间用户导航跳转，锁会继续持有直到请求完成；新页面中语言按钮会临时不可点击。
var _langSwitching = false;

function selectLang(lang) {
  // 点击当前语言：如果包已完整加载则直接退出；如果部分包失败（_loadedLangs=false）则继续触发重试
  if (lang === I18N.getLang() && I18N.isLangLoaded(lang)) return;
  if (_langSwitching) return;  // 全局锁，防并发切换

  _langSwitching = true;

  // 判断是重试（当前语言但包未完整加载）还是切换
  var isRetry = (lang === I18N.getLang() && !I18N.isLangLoaded(lang));

  // 禁用两个语言按钮 + 文案变更（UI 层反馈，非互斥手段）
  var buttons = document.querySelectorAll('[onclick^="selectLang"]');
  var origTexts = [];
  buttons.forEach(function(btn, i) {
    origTexts[i] = btn.textContent;
    btn.disabled = true;
    btn.style.opacity = '0.5';
    btn.style.cursor = 'wait';
    btn.textContent = isRetry
      ? I18N.t('app.account.reloading')
      : I18N.t('app.account.switching');
  });

  function restoreButtons() {
    buttons.forEach(function(btn, i) {
      btn.disabled = false;
      btn.style.opacity = '';
      btn.style.cursor = '';
      btn.textContent = origTexts[i];
    });
  }

  // 全部失败时 _currentLang 未变更，I18N.t() 用旧语言取文案
  // inline packs 覆盖 common.error/success（zh: '错误'/'成功', en: 'Error'/'Success'）
  // 即使语言包未加载，inline packs 保证 Toast 文案可读

  try {
    I18N.setLang(lang).then(function(result) {
      // Issue #273：语言切换是即时视觉反馈（整页语言立刻变化），成功路径一律不弹 Toast。
      // 统一 cached / 全部成功 / 部分成功 / 重试成功 各路径行为——仅全部失败时弹 error Toast。
      if (result.cached) {
        // 缓存命中：setLang 已在内部完成状态变更 + rerender，旧按钮已被替换
      } else if (result.failed === 0) {
        // 全部成功：_rerenderCurrentPage 已在 setLang 内同步完成 DOM 重建，无需额外反馈
      } else if (result.failed < result.total) {
        // 部分成功：DOM 已重建，部分文案靠 t() fallback——页面语言已切换，无需 Toast
      } else {
        // 全部失败：setLang 未提交状态变更，_currentLang 仍为旧语言
        // DOM 不重建，旧按钮仍禁用，必须手动恢复
        // Toast 文案用 I18N.t()——此时 _currentLang 是旧语言，inline packs 保证文案可读
        restoreButtons();
        if (typeof app !== 'undefined' && app.showToast) {
          app.showToast(I18N.t('common.error'));
        }
      }
    }).catch(function(e) {
      // setLang 自身代码异常（非 fetch 失败）
      restoreButtons();
      if (typeof app !== 'undefined' && app.showToast) {
        app.showToast(I18N.t('common.error') + ': ' + (e && e.message ? e.message : ''));
      }
    }).finally(function() {
      // Promise 完成后才释放全局锁——避免 fetch 飞行期间锁被提前释放
      _langSwitching = false;
    });
  } catch(e) {
    // setLang 同步异常（如 _updateUrlLang 抛出）
    restoreButtons();
    _langSwitching = false;
    if (typeof app !== 'undefined' && app.showToast) {
      app.showToast(I18N.t('common.error') + ': ' + (e && e.message ? e.message : ''));
    }
  }
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

// ── Issue #69: API Keys 管理 ──

function loadApiKeys() {
  API.get('/settings/api-keys').then(d => {
    const keys = d.keys || [];
    const container = document.getElementById('sApiKeys');
    if (!container) return;

    let rowsHtml = '';
    if (keys.length === 0) {
      rowsHtml = `<div class="tx t2" style="padding:16px;text-align:center">${I18N.t('app.account.no_api_keys')}</div>`;
    } else {
      rowsHtml = keys.map(k => `
        <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--b)">
          <div style="flex:1;min-width:0">
            <div style="font-size:12px;font-weight:600;color:var(--t)">${_escApi(k.name)}</div>
            <div style="font-size:11px;color:var(--t3);font-family:monospace;margin-top:2px">${_escApi(k.prefix)}</div>
            <div style="font-size:10px;color:var(--t3);margin-top:2px">
              ${I18N.t('app.account.created')}: ${_escApi(k.created_at || '—')}
              ${k.last_used_at ? ' · ' + I18N.t('app.account.last_used') + ': ' + _escApi(k.last_used_at) : ''}
            </div>
          </div>
          <button class="bt" style="font-size:11px;padding:5px 12px;background:var(--r);color:#fff;border:none;border-radius:6px;cursor:pointer"
            onclick="revokeApiKey(${k.id}, '${_escApi(k.name).replace(/'/g, "\\'")}')">${I18N.t('app.account.revoke')}</button>
        </div>
      `).join('');
    }

    container.innerHTML = `
      <div class="cd">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
          <h3 style="font-size:13px;font-weight:600">${I18N.t('app.account.api_keys_title')}</h3>
          <button class="bt bt-p" style="font-size:12px;padding:7px 16px" onclick="showCreateApiKeyDialog()">${I18N.t('app.account.generate_key')}</button>
        </div>
        <p class="tx t2" style="font-size:11px;line-height:1.6;margin-bottom:12px">
          ${I18N.t('app.account.api_key_desc')}
        </p>
        <div id="apiKeyList">${rowsHtml}</div>
      </div>
    `;
  }).catch(e => {
    document.getElementById('sApiKeys').innerHTML =
      `<div class="cd"><p style="color:var(--r);font-size:12px">${I18N.t('app.account.load_failed', {msg: e.message})}</p></div>`;
  });
}

function _escApi(s) {
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function showCreateApiKeyDialog() {
  app.showPrompt({
    title: I18N.t('app.account.enter_key_name'),
    placeholder: I18N.t('app.account.enter_key_name'),
    onConfirm: function(name) {
      if (!name || !name.trim()) {
        app.showToast(I18N.t('app.account.name_required'));
        return;
      }
      API.post('/settings/api-keys', {name: name.trim()}).then(d => {
        // 明文仅展示一次——弹窗显示完整 key
        const modal = document.createElement('div');
        modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1001';
        modal.innerHTML = `
          <div style="background:var(--c);border-radius:14px;padding:24px;max-width:520px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.15)">
            <h3 style="font-size:15px;font-weight:600;margin-bottom:8px">${I18N.t('app.account.api_key_created')}</h3>
            <p style="font-size:12px;color:var(--r);margin-bottom:12px">${I18N.t('app.account.api_key_warning')}</p>
            <div style="display:flex;gap:8px;margin-bottom:16px">
              <input type="text" id="newApiKeyInput" value="${d.key}" readonly
                style="flex:1;padding:8px 12px;border:1px solid var(--b);border-radius:8px;font-family:monospace;font-size:12px;background:#f8f9fa">
              <button class="bt bt-p" style="padding:8px 16px;font-size:12px" onclick="copyApiKeyToClipboard()">${I18N.t('app.account.copy')}</button>
            </div>
            <div style="display:flex;justify-content:flex-end">
              <button class="bt bt-p" style="padding:8px 20px;font-size:12px" onclick="document.querySelector('body > div[style*=fixed]').remove()">${I18N.t('app.account.saved')}</button>
            </div>
          </div>
        `;
        document.body.appendChild(modal);
        // 选中 key 文本方便复制
        setTimeout(() => { const inp = document.getElementById('newApiKeyInput'); if (inp) inp.select(); }, 100);
        loadApiKeys(); // 刷新列表
      }).catch(e => {
        app.showToast(I18N.t('app.account.generate_failed', {msg: e.message}));
      });
    }
  });
}

function copyApiKeyToClipboard() {
  const inp = document.getElementById('newApiKeyInput');
  if (!inp) return;
  inp.select();
  try { navigator.clipboard.writeText(inp.value); } catch(e) { document.execCommand('copy'); }
  app.showToast(I18N.t('app.account.copied'));
}

function revokeApiKey(id, name) {
  app.showConfirm(I18N.t('app.account.revoke_confirm', {name: name}), function() {
    API.del('/settings/api-keys/' + id).then(d => {
      app.showToast(d.message);
      loadApiKeys();
    }).catch(e => app.showToast(I18N.t('app.account.revoke_failed', {msg: (e.message || e)})));
  }, {danger: true});
}
