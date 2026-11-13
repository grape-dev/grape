/* Grape User Settings — 内嵌 SPA view 逻辑（全屏渲染到 mainView） */
/* renderAccount(tab) 由 app.nav('account') 调用，隐藏工作台侧边栏，自带顶栏 */

var _accountProfile = null;
var _accountCurrentTab = 'profile';

function renderAccount(tab) {
  var pc = document.getElementById('pageContent');
  if (!pc) return;
  // hash 路由：从 location.hash 恢复 tab（如 /#account#security）
  if (!tab && location.hash && location.hash !== '#account') {
    var hashTab = location.hash.replace('#account', '').replace('#', '');
    if (['profile', 'security', 'credentials', 'appearance', 'apikeys'].indexOf(hashTab) >= 0) {
      tab = hashTab;
    }
  }
  _accountCurrentTab = tab || 'profile';

  var isZh = (typeof I18N !== 'undefined') ? I18N.getLang() === 'zh' : true;

  var tabs = [
    {id:'profile', label: (I18N.t('app.account.nav_profile'))},
    {id:'security', label: (I18N.t('app.account.security_tab'))},
    {id:'credentials', label: (I18N.t('app.account.credentials_tab'))},
    {id:'appearance', label: (I18N.t('app.account.appearance_tab'))},
    {id:'apikeys', label: 'API Keys'},
  ];

  // 布局：cd 容器 + stabs tab 栏 + 内容（和 renderSettings 一致）
  var html = '<div class="cd"><div class="cd-h"><h2>' + (I18N.t('app.account.center_title')) + '</h2>' +
    '<button class="bt bt-o" style="margin-left:auto;font-size:12px;padding:4px 12px" onclick="app._accountGoBack()">' + (I18N.t('app.account.back_to_workspace_btn')) + '</button></div>';
  html += '<div class="stabs">';
  tabs.forEach(function(t) {
    html += '<a class="' + (t.id === _accountCurrentTab ? 'ac' : '') + '" onclick="renderAccount(\'' + t.id + '\')">' + t.label + '</a>';
  });
  html += '</div>';
  html += '<div id="accountTabContent"><div class="loading">' + (I18N.t('app.account.loading')) + '</div></div>';
  html += '</div>';
  pc.innerHTML = html;

  switch (_accountCurrentTab) {
    case 'profile': _accountLoadProfile(); break;
    case 'security': _accountLoadSecurity(); break;
    case 'credentials': _accountLoadCredentials(); break;
    case 'appearance': _accountLoadAppearance(); break;
    case 'apikeys': _accountLoadApiKeys(); break;
  }
}

// ===== Helpers =====
function _accountEsc(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// JS 字符串转义——用于 onclick 属性内传参（BUG-3 修复）
// 在 onclick="fn('...')" 中，HTML 解析器先解码实体编码（&#39; → '）再传给 JS，
// 导致单引号闭合字符串。onclick 内需要 JS 层面转义（反斜杠），而非 HTML 实体编码。
function _accountJsStr(s) {
  if (!s) return '';
  return String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"');
}

function _accountIsZh() {
  return (typeof I18N !== 'undefined') ? I18N.getLang() === 'zh' : true;
}

function _accountT(key, vars) {
  if (typeof I18N !== 'undefined') return I18N.t(key, vars);
  return key;
}

// ===== Profile tab =====
function _accountLoadProfile() {
  API.get('/auth/me').then(function(d) {
    _accountProfile = d;
    var el = document.getElementById('accountTabContent');
    if (!el) return;
    var isZh = _accountIsZh();
    var html = '<div class="cd"><div class="cd-h"><h2>' + (I18N.t('app.account.basic_info')) + '</h2></div>';
    html += '<div class="srow"><label>' + (I18N.t('app.account.username')) + '</label><input type="text" value="' + _accountEsc(d.username) + '" disabled style="background:#f1f5f9"></div>';
    html += '<div class="srow"><label>' + (I18N.t('app.account.email_label')) + '</label><input type="text" value="' + _accountEsc(d.email || '') + '" disabled style="background:#f1f5f9" placeholder="' + (I18N.t('app.account.email_not_set')) + '"></div>';
    html += '<div class="srow"><label>' + (I18N.t('app.account.nickname')) + '</label><input type="text" id="accNickname" value="' + _accountEsc(d.nickname || '') + '"></div>';
    html += '<button class="bt bt-p mt8" onclick="_accountSaveProfile()">' + (I18N.t('app.account.save')) + '</button></div>';
    el.innerHTML = html;
  }).catch(function(e) {
    var el = document.getElementById('accountTabContent');
    if (el) el.innerHTML = '<div class="error">' + _accountEsc(e.message || I18N.t('app.account.load_failed')) + '</div>';
  });
}

function _accountSaveProfile() {
  var nick = document.getElementById('accNickname') ? document.getElementById('accNickname').value.trim() : '';
  API.put('/auth/profile', {nickname: nick}).then(function() {
    if (_accountProfile) _accountProfile.nickname = nick;
    // 更新侧边栏底部用户名
    var nameEl = document.getElementById('userName');
    if (nameEl) nameEl.textContent = nick || (_accountProfile ? _accountProfile.username : I18N.t('app.account.default_username'));
    app.showToast(I18N.t('app.account.profile_saved_success'));
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

// ===== Security tab (密码 + 邮箱 + 第三方账号) =====
// R7.9: _emailConfigured 初始 false（无邮箱模式默认），避免邮箱卡片先显示再隐藏闪动
var _emailConfigured = false;

function _accountLoadSecurity() {
  // R7: 先获取 email_configured 状态，再渲染安全 tab
  Promise.all([
    API.get('/auth/providers'),
    API.get('/settings/credentials').catch(function() { return {}; })
  ]).then(function(results) {
    var providersData = results[0];
    window._emailConfigured = !!(providersData && providersData.email_configured);
    _accountProvidersData = providersData;
    // 绑定来源判定（D102-M：Manual PAT > OAuth）——安全 tab 只"陈述"来源，
    // PAT 操作归凭据管理（2026-11-08 用户裁定：职责分离但模式可见）
    window._secCredSource = function(provider) {
      var c = results[1] || {};
      return !!(c[provider + '_pat'] && Object.keys(c[provider + '_pat']).length > 0) ? 'PAT' : 'OAuth';
    };
    _accountRenderSecurity();
  }).catch(function() {
    window._emailConfigured = true; // 获取失败默认显示邮箱卡片
    _accountProvidersData = { providers: [] };
    _accountRenderSecurity();
  });
}

var _accountProvidersData = null;

function _accountRenderSecurity() {
  API.get('/auth/me').then(function(d) {
    _accountProfile = d;
    var el = document.getElementById('accountTabContent');
    if (!el) return;
    var isZh = _accountIsZh();
    var hasPwd = d.has_password;
    var html = '';

    // 卡片1: 密码管理
    html += '<div class="cd"><div class="cd-h"><h2>' + (I18N.t('app.account.password')) + '</h2></div>';
    if (hasPwd) {
      html += '<p class="tx t2" style="margin-bottom:12px">' + (I18N.t('app.account.password_already_set')) + '</p>';
      html += '<button class="bt bt-p" onclick="_accountShowChangePassword()">' + (I18N.t('app.account.change_password')) + '</button>';
    } else {
      html += '<p class="tx t2" style="margin-bottom:12px">' + (I18N.t('app.account.password_not_set')) + '</p>';
      html += '<button class="bt bt-p" onclick="_accountShowSetPassword()">' + (I18N.t('app.account.set_password')) + '</button>';
    }
    html += '</div>';

    // 卡片2: 邮箱管理（R7 原设计：无邮箱配置时整卡隐藏——但用户会疑惑「为什么没有换邮箱入口」，
    // #535 用户体验收口：未配置时卡片保留但降级——说明原因 + 配置帮助链接，入口不再凭空消失）
    if (window._emailConfigured !== false) {
      html += '<div class="cd" id="accEmailCard"><div class="cd-h"><h2>' + (I18N.t('app.account.email_label')) + '</h2></div>';
      if (d.email) {
        html += '<p class="tx t2" style="margin-bottom:12px">' + _accountEsc(d.email) + '</p>';
        html += '<button class="bt bt-o" onclick="_accountShowChangeEmail()">' + (I18N.t('app.account.change_email_btn')) + '</button>';
      } else {
        html += '<p class="tx t2" style="margin-bottom:12px">' + (I18N.t('app.account.email_not_set')) + '</p>';
        html += '<button class="bt bt-o" onclick="_accountShowChangeEmail()">' + (I18N.t('app.account.set_email')) + '</button>';
      }
      html += '</div>';
    } else {
      html += '<div class="cd" id="accEmailCard"><div class="cd-h"><h2>' + (I18N.t('app.account.email_label')) + '</h2></div>';
      html += '<p class="tx t2" style="margin-bottom:4px">' + (I18N.t('app.account.email_service_not_configured_card')) + '</p>';
      html += '<p style="font-size:12px"><a href="/static/help.html#/developer/04-storage-and-email" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline">' + (I18N.t('app.account.email_view_config_help')) + '</a></p>';
      html += '</div>';
    }

    // 卡片3: 第三方账号（UX-1 修复：从 /auth/providers 动态渲染，不硬编码平台）
    var bindings = d.bindings || [];
    var identities = d.identities || [];
    var expiredProviders = d.token_expired_providers || [];
    var _expiredMap = {};
    expiredProviders.forEach(function(ep) {
      if (typeof ep === 'object') { _expiredMap[ep.provider] = ep; }
      else { _expiredMap[ep] = { provider: ep, error_code: 'TOKEN_EXPIRED' }; }
    });
    html += '<div class="cd"><div class="cd-h"><h2>' + (I18N.t('app.account.third_party_accounts')) + '</h2></div>';
    html += '<div id="accProvidersList"><div class="loading">' + (I18N.t('app.account.loading')) + '</div></div>';
    html += '</div>';

    // 卡片4: 注销账号（原"危险区域"合并到安全 tab 底部，Apple Settings 风格）
    html += '<div style="border-top:1px solid var(--b);margin-top:24px;padding-top:20px">';
    html += '<button class="bt bt-r" onclick="_accountShowDeleteAccount()">' + (I18N.t('app.account.delete_account')) + '</button>';
    html += '</div>';

    el.innerHTML = html;

    // 异步加载 provider 列表后渲染（R7: 已在 _accountLoadSecurity 中预获取）
    var providersData = _accountProvidersData || { providers: [] };
    window._emailConfigured = !!(providersData && providersData.email_configured);
    var providers = providersData.providers || [];  // R7.5: 不过滤 configured——所有平台始终显示，未配 OAuth App 时点击走手动令牌绑定
      var listEl = document.getElementById('accProvidersList');
      if (!listEl) return;
      if (providers.length === 0) {
        listEl.innerHTML = '<p class="tx t2" style="padding:8px 0">' + (I18N.t('app.account.no_third_party_platforms')) + '</p>';
        return;
      }
      var providersHtml = providers.map(function(p) {
        var bound = bindings.indexOf(p.provider) >= 0;
        var identity = identities.find(function(i) { return i.provider === p.provider; });
        // D105k（2026-11-09 用户裁定）：安全 tab 行 = 纯 OAuth 三态，PAT 彻底退出本行。
        // ①渠道已配置+OAuth 绑定（后端 has_oauth_token 声明制）→ 已绑定+解绑
        // ②渠道已配置+未绑定 → 绑定按钮
        // ③渠道未配置 → 不支持 OAuth（灰ⓘ），绑定/解绑全不渲染。
        // PAT 的一切（配置/清除/来源标签）只在凭据 tab，本行不再展示。
        var oauthBound = !!(bound && identity && identity.has_oauth_token === true);
        // D102-L：token 过期告警归凭据 tab——安全 tab 只展示绑定关系，恒 false
        var expired = false;
        var _secOauthState = (p.configured === false) ? 'unconfigured'
          : (oauthBound ? 'bound' : 'unbound');
        var _badgeTitle, _badgeBg, _badgeGlyph;
        if (_secOauthState === 'unconfigured') {
          _badgeTitle = I18N.t('app.account.sec_oauth_unconfigured_hint');
          _badgeBg = 'background:var(--bg);color:var(--t3)';
          _badgeGlyph = 'ⓘ';
        } else if (_secOauthState === 'bound') {
          _badgeTitle = I18N.t('app.account.sec_oauth_bound');
          _badgeBg = 'background:rgba(52,199,89,.15);color:#34c759';
          _badgeGlyph = '✓';
        } else {
          _badgeTitle = I18N.t('app.account.sec_oauth_unbound');
          _badgeBg = 'background:rgba(10,132,255,.12);color:#0a84ff';
          _badgeGlyph = 'ⓘ';
        }
        var _secStatusBadge = '<span class="sec-status-badge" title="' + _badgeTitle.replace(/"/g, '&quot;') + '"'
          + ' style="font-size:10px;font-weight:700;min-width:16px;height:16px;padding:0 4px;'
          + 'display:inline-flex;align-items:center;justify-content:center;border-radius:8px;cursor:help;' + _badgeBg + '">' + _badgeGlyph + '</span>';
        return _accountRenderProviderRow(p.name, p.provider, _secOauthState === 'bound', identity, expired, {
          state: _secOauthState, badge: _secStatusBadge, badgeTitle: _badgeTitle,
          oauthConfigured: p.configured });
      }).join('');
      listEl.innerHTML = providersHtml;
  }).catch(function(e) {
    var el = document.getElementById('accountTabContent');
    if (el) el.innerHTML = '<div class="error">' + _accountEsc(e.message || I18N.t('app.account.load_failed')) + '</div>';
  });
}

// ===== Credentials tab (云平台凭据) =====
function _accountLoadCredentials() {
  var el = document.getElementById('accountTabContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">' + (I18N.t('app.account.loading')) + '</div>';

  // D102-L: 第三方仓库凭据卡（凭据域——平台清单卡驱动 + 过期态）
  Promise.all([
    API.get('/settings/vcs/platforms'),
    API.get('/auth/me'),
    API.get('/settings/credentials'),
    API.get('/auth/providers').catch(function() { return { providers: [] }; })
  ]).then(function(results) {
    var _plats = (results[0] && results[0].platforms) || [];
    var _me = results[1] || {};
    var _creds = results[2] || {};
    var _expiredMap = {};
    (_me.token_expired_providers || []).forEach(function(ep) {
      _expiredMap[typeof ep === 'object' ? ep.provider : ep] = true;
    });
    // 2026-11-08 用户裁定：凭据双模式（OAuth / PAT）三态角标矩阵。
    // OAuth 状态机：unconfigured（灰，入口置灰）/ unbound（跳安全页绑定）
    // / available。PAT 独立徽章；清除=PUT 空串。
    // 平台取消 OAuth 配置后按 unconfigured 显示，绑定记录不动（不清除）。
    var _oauthCfgMap = {};
    ((results[3] && results[3].providers) || []).forEach(function(pp) { _oauthCfgMap[pp.provider] = !!pp.configured; });
    var _bindings = {};
    (_me.bindings || []).forEach(function(b) { _bindings[b] = true; });
    window._accountRepoCredSource = function(provider) {
      var hasManual = !!(_creds[provider + '_pat'] && Object.keys(_creds[provider + '_pat']).length > 0);
      if (hasManual) return {key: 'manual', label: I18N.t('app.account.cred_source_manual')};
      if (_bindings[provider]) return {key: 'oauth', label: I18N.t('app.account.cred_source_oauth')};
      return {key: 'none', label: I18N.t('app.account.cred_source_none')};
    };
    var _repoCredHtml = '';
    _plats.forEach(function(p) {
      var _oauthState = _bindings[p.provider] ? 'available'
        : (_oauthCfgMap[p.provider] === false ? 'unconfigured' : 'unbound');
      // D102-P 统一（口径 v6 终态）：全平台泛型键——后端已对存量平台做
      // 专用字段→<p>_pat 存在性映射，前端零分派。
      var _hasPat = !!(_creds[p.provider + '_pat'] && Object.keys(_creds[p.provider + '_pat']).length > 0);
      // 2026-11-08 用户裁定：凭据行=徽章即状态，不做跳转账号页的入口。
      // OAuth 徽章三态：平台未配置 OAuth（灰）/未绑定（蓝）/绑定生效中（绿）；
      // PAT 徽章两态：已配置 PAT（绿）/PAT 未配置（灰）。操作仅 PAT 配置/清除。
      var _oauthBadge, _oauthTitle;
      if (_oauthState === 'unconfigured') {
        _oauthBadge = I18N.t('app.account.oauth_badge_unconfigured');
        _oauthTitle = I18N.t('app.account.oauth_title_unconfigured');
      } else if (_oauthState === 'unbound') {
        _oauthBadge = I18N.t('app.account.oauth_badge_unbound');
        _oauthTitle = I18N.t('app.account.oauth_title_unbound');
      } else {
        _oauthBadge = I18N.t('app.account.oauth_badge_bound');
        _oauthTitle = _hasPat ? I18N.t('app.account.oauth_title_bound_pat') : I18N.t('app.account.oauth_title_bound');
      }
      var _oauthBadgeHtml = '<span class="tg" title="' + _oauthTitle + '" style="font-size:10px;padding:2px 6px;border-radius:4px;' + (_oauthState === 'unconfigured' ? 'background:var(--bg);color:var(--t3)' : _oauthState === 'unbound' ? 'background:rgba(10,132,255,.12);color:#0a84ff' : 'background:rgba(52,199,89,.15);color:#34c759') + '">' + _oauthBadge + '</span>';
      var _patBadge = _hasPat
        ? '<span class="tg" title="' + I18N.t('app.account.pat_badge_manual_title') + '" style="font-size:10px;background:rgba(52,199,89,.15);color:#34c759;padding:2px 6px;border-radius:4px">' + I18N.t('app.account.pat_badge_configured') + '</span>'
        : '<span class="tg" title="' + I18N.t('app.account.pat_badge_none_title') + '" style="font-size:10px;background:var(--bg);color:var(--t3);padding:2px 6px;border-radius:4px">' + I18N.t('app.account.pat_badge_none') + '</span>';
      var _expired = !!_expiredMap[p.provider];
      var _expiredBadge = _expired
        ? '<span class="tg" style="font-size:10px;background:var(--r);color:#fff;padding:2px 6px;border-radius:4px">' + (I18N.t('app.account.token_expired') || 'Token Expired') + '</span>'
        : '';
      var _patBtns = '<button class="bt bt-s bt-p" style="font-size:11px" onclick="_accountOpenPatForm(\'' + p.provider + '\')">' + (_hasPat ? I18N.t('app.account.pat_btn_reconfigure') : I18N.t('app.account.pat_btn_configure')) + '</button>'
        + (_hasPat ? ' <button class="bt bt-s" style="font-size:11px" title="' + I18N.t('app.account.pat_btn_clear_title') + '" onclick="_accountClearPat(\'' + p.provider + '\')">' + I18N.t('app.account.pat_btn_clear') + '</button>' : '');
      _repoCredHtml += '<div style="display:flex;align-items:center;gap:14px;padding:12px 16px;border:1px solid var(--b);border-radius:12px;background:var(--c);margin-bottom:10px">'
        + '<div style="flex:1">'
          + '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' + (typeof platformIcon === 'function' ? platformIcon(p.provider, 18) : '') + '<span style="font-size:14px;font-weight:600">' + _accountEsc(p.name) + '</span>' + _expiredBadge + _oauthBadgeHtml + _patBadge + '</div>'
        + '</div>'
        + '<div style="display:flex;align-items:center;gap:6px;flex-shrink:0">' + _patBtns + '</div>'
      + '</div>';
    });
    window._accountRepoCredHtml = _repoCredHtml;
    // 拿到仓库平台数据后再拉凭据详情渲染整个 tab
    _accountLoadCredentialsContinue();
  }).catch(function() {
    window._accountRepoCredHtml = '';
    _accountLoadCredentialsContinue();
  });
}

function _accountLoadCredentialsContinue() {
  var el = document.getElementById('accountTabContent');
  if (!el) return;

  API.get('/settings/credentials').then(function(d) {
    var hw = d.huawei_cloud || {};
    // Issue #474：接口不再回显明文 AK/SK，仅回存在性（机密只回存在性口径）。
    // 已配置 → 输入框显示 '********' 掩码占位；未配置 → 空框。
    // 记录占位符哨兵供保存时比对（未改动不回传，清空显式清除）。
    var akVal = hw.ak_exists ? '********' : '';
    var skVal = hw.sk_exists ? '********' : '';
    window._accountCloudCredOriginal = { ak: akVal, sk: skVal };
    // Issue #474 方案A：按需取回明文——已配置凭据保留 👁 查看明文（走 reveal 接口临时显示，不落缓存）
    window._accountCloudCredRevealed = { ak: null, sk: null };

    function revealBtn(field, exists) {
      if (!exists) return '';
      return '<button type="button" title="' + (I18N.t('app.account.reveal_plaintext') || '') + '" onclick="_accountToggleReveal(\'' + field + '\')" style="border:1px solid var(--b);background:var(--bg);border-radius:8px;width:30px;height:30px;padding:0;font-size:14px;cursor:pointer;color:var(--t3);flex-shrink:0">' + '👁' + '</button>';
    }

    var html = '';

    // 卡片: 第三方仓库凭据（D102-L——仓库平台 PAT 配置归凭据域）
    html += '<div class="cd"><div class="cd-h"><h2>' + I18N.t('app.account.third_party_repo_credentials') + '</h2></div>';
    html += (window._accountRepoCredHtml || '<p class="tx t2" style="padding:8px 0">' + (I18N.t('app.account.loading') || '') + '</p>');
    html += '<p class="tx t2 mt8" style="font-size:12px">' + I18N.t('app.account.repo_cred_desc') + '</p>';
    html += '</div>';

    // 卡片: Huawei Cloud AK/SK — 和原通用配置保持一致风格
    html += '<div class="cd"><div class="cd-h"><h2>Huawei Cloud</h2></div>';
    html += '<div class="srow" style="align-items:center"><label>' + (I18N.t('app.account.access_key')) + '</label>';
    html += '<div style="display:flex;align-items:center;gap:6px;flex:1">';
    html += '<input type="password" id="accHuaweiAk" value="' + _accountEsc(akVal) + '" autocomplete="new-password" placeholder="' + (I18N.t('app.account.access_key')) + '" style="flex:1">';
    html += revealBtn('ak', hw.ak_exists);
    html += '</div></div>';
    html += '<div class="srow" style="align-items:center"><label>' + (I18N.t('app.account.secret_key')) + '</label>';
    html += '<div style="display:flex;align-items:center;gap:6px;flex:1">';
    html += '<input type="password" id="accHuaweiSk" value="' + _accountEsc(skVal) + '" autocomplete="new-password" placeholder="' + (I18N.t('app.account.secret_key')) + '" style="flex:1">';
    html += revealBtn('sk', hw.sk_exists);
    html += '</div></div>';
    html += '<p class="tx t2 mt8">' + (I18N.t('app.account.huawei_cloud_desc')) + '</p>';
    html += '<button class="bt bt-p mt8" onclick="_accountSaveCloudCreds()">' + (I18N.t('app.account.save')) + '</button>';
    html += '</div>';

    el.innerHTML = html;
  }).catch(function(e) {
    el.innerHTML = '<div class="error">' + _accountEsc(e.message || I18N.t('app.account.load_failed')) + '</div>';
  });
}


// ── PlatformCatalog（F1/S24）：VCS 平台目录——后端默认卡常在（gitcode/github），
// 其他平台有卡自动出现；PAT 表单按目录渲染，前端零平台名分支。─────────────
var _vcsPlatformCatalog = null;
function _accountLoadVcsCatalog() {
  if (_vcsPlatformCatalog) return Promise.resolve(_vcsPlatformCatalog);
  return API.get('/settings/vcs/platforms').then(function(d) {
    _vcsPlatformCatalog = {};
    (d.platforms || []).forEach(function(p) { _vcsPlatformCatalog[p.provider] = p; });
    return _vcsPlatformCatalog;
  }).catch(function() { _vcsPlatformCatalog = {}; return _vcsPlatformCatalog; });
}
function _accountPlatformName(provider) {
  // 平台名来源优先级（D102-L 实弹修正 2026-11-08）：
  // /auth/providers 渠道卡（含华为账号等纯登录/未入目录渠道）→
  // 仓库平台目录 → 平台 id 原样兜底。
  // 禁止兜底硬编码 'GitCode'——旧兜底把 huawei 错标成 GitCode，
  // 第三方账号列表出现两个"GitCode"且华为账号消失（事故见
  // tests/test_account_platform_label.py）。
  var pd = (typeof _accountProvidersData !== 'undefined' && _accountProvidersData && _accountProvidersData.providers) || [];
  for (var i = 0; i < pd.length; i++) {
    if (pd[i].provider === provider && pd[i].name) return pd[i].name;
  }
  var c = _vcsPlatformCatalog || {};
  if (c[provider] && c[provider].name) return c[provider].name;
  return {github: 'GitHub', gitee: 'Gitee'}[provider] || provider;
}
function _accountPatCreateUrl(provider) {
  // 存量两平台保留官方直达链接（带 scopes/description 预选参数）；
  // 其他平台按目录卡 pat_create_url 渲染
  if (provider === 'github') return 'https://github.com/settings/tokens/new?scopes=repo&description=Grape%20DevOps';
  if (provider === 'gitcode') return 'https://gitcode.com/setting/token-classic/create';
  var c = _vcsPlatformCatalog || {};
  return (c[provider] && c[provider].pat_create_url) || '';
}

function _accountRenderProviderRow(label, provider, bound, identity, expired, opts) {
  opts = opts || {};
  var isZh = _accountIsZh();
  var icon = (typeof platformIcon === 'function') ? platformIcon(provider, 20) : '';
  if (opts.badge) icon = '<span style="position:relative;display:inline-flex">' + icon + opts.badge + '</span>';
  var pName = _accountPlatformName(provider);

  // R10: token 过期时显示过期提示 + 重新绑定按钮
  if (bound && expired) {
    return '<div style="display:flex;align-items:center;gap:14px;padding:14px 16px;border:1px solid var(--r);border-radius:12px;background:var(--c);margin-bottom:10px">'
      + '<div style="flex:1">'
        + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
          + icon
          + '<span style="font-size:14px;font-weight:600;color:var(--t)">' + _accountEsc(pName) + '</span>'
          + '<span class="tg" style="font-size:10px;background:var(--r);color:#fff;padding:2px 6px;border-radius:4px">' + (I18N.t('app.account.token_expired') || 'Token Expired') + '</span>'
        + '</div>'
        + '<div style="font-size:12px;color:var(--r);margin-bottom:8px">' + (I18N.t('app.account.token_expired_hint') || 'Your token has expired or been revoked. Please re-bind your token.') + '</div>'
      + '</div>'
      + '<button class="bt bt-s bt-p" style="font-size:11px" onclick="_accountBindProvider(\'' + provider + '\')">' + (I18N.t('app.account.re_bind') || 'Re-bind') + '</button>'
      + '</div>';
  }

  if (bound) {
    // 已绑定——卡片式布局展示完整信息
    var login = (identity && identity.external_login) || '';
    var name = (identity && identity.external_name) || '';
    var avatar = (identity && identity.external_avatar) || '';
    var createdAt = (identity && identity.created_at) || '';

    // 头像：有平台头像用头像，没有用首字母
    var avatarHtml = avatar
      ? '<img src="' + _accountEsc(avatar) + '" style="width:32px;height:32px;border-radius:50%;object-fit:cover;flex-shrink:0" onerror="this.style.display=\'none\'">'
      : '<div style="width:32px;height:32px;border-radius:50%;background:var(--bg);display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:600;color:var(--t2);flex-shrink:0">' + (login || pName)[0].toUpperCase() + '</div>';

    // 用户名行：login 必展示，name 和 login 不同时额外展示 name
    var nameLine = login;
    if (name && name !== login) nameLine += ' · ' + name;

    // 绑定时间
    var timeLine = '';
    if (createdAt) {
      var dateStr = createdAt.substring(0, 10);
      timeLine = (I18N.t('app.account.linked_on')) + dateStr;
    }

    return '<div style="display:flex;align-items:center;gap:14px;padding:14px 16px;border:1px solid var(--b);border-radius:12px;background:var(--c);margin-bottom:10px;box-shadow:var(--sh)">'
      + avatarHtml
      + '<div style="flex:1;min-width:0">'
        + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:2px">'
          + icon
          + '<span style="font-size:14px;font-weight:600;color:var(--t)">' + _accountEsc(pName) + '</span>'
          + '<span class="tg tg-d" style="font-size:10px">' + (I18N.t('app.account.linked_badge')) + '</span>'
          + (opts.sourceTag || '')
        + '</div>'
        + '<div style="font-size:12px;color:var(--t2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _accountEsc(nameLine) + '</div>'
        + (timeLine ? '<div style="font-size:11px;color:var(--t3);margin-top:2px">' + _accountEsc(timeLine) + '</div>' : '')
      + '</div>'
      + '<div style="display:flex;gap:6px;flex-shrink:0">'
        + (opts.oauthConfigured === false
          ? ''  // PAT 绑定（平台未配置 OAuth）：更新 OAuth 无意义，仅解绑（2026-11-08 用户裁定）
          : '<button class="bt bt-s bt-o" style="font-size:11px" onclick="_accountBindProvider(\'' + provider + '\')">' + (I18N.t('app.account.update')) + '</button>')
        + '<button class="bt bt-s" style="color:var(--r);border-color:var(--r);font-size:11px" onclick="_accountUnbindProvider(\'' + provider + '\')">' + (I18N.t('app.account.unbind')) + '</button>'
      + '</div>'
    + '</div>';
  }

  // 未绑定——简洁行
  return '<div style="display:flex;align-items:center;gap:14px;padding:14px 16px;border:1px solid var(--b);border-radius:12px;background:var(--c);margin-bottom:10px;box-shadow:var(--sh)">'
    + '<div style="width:32px;height:32px;border-radius:50%;background:var(--bg);display:flex;align-items:center;justify-content:center;opacity:0.5;flex-shrink:0">' + icon + '</div>'
    + '<div style="flex:1">'
      + '<div style="display:flex;align-items:center;gap:8px">'
        + '<span style="font-size:14px;font-weight:600;color:var(--t)">' + _accountEsc(pName) + '</span>'
        + '<span class="tg tg-d" style="font-size:10px">' + (I18N.t('app.account.not_linked')) + '</span>'
      + '</div>'
      + '<div style="font-size:12px;color:var(--t3);margin-top:2px">' + (I18N.t('app.account.link_to_manage_repos')) + '</div>'
    + '</div>'
    + (opts.state === 'unconfigured'
        ? '<button class="bt bt-s bt-p" aria-disabled="true" title="' + (opts.badgeTitle || I18N.t('app.account.oauth_badge_unconfigured')).replace(/"/g, '&quot;') + '" style="font-size:11px;opacity:.45;cursor:not-allowed">' + (I18N.t('app.account.link_btn')) + '</button>'
        : '<button class="bt bt-s bt-p" style="font-size:11px" onclick="_accountBindProvider(\'' + provider + '\')">' + (I18N.t('app.account.link_btn')) + '</button>')
  + '</div>';
}

// ===== Change password =====
function _accountShowChangePassword() {
  var hasEmail = _accountProfile && _accountProfile.email;
  _accountShowModal(
    '<h3>' + (I18N.t('app.account.change_password')) + '</h3>' +
    '<p class="tx t2" style="margin-bottom:12px">' + (I18N.t('app.account.change_password_logout_notice')) + '</p>' +
    '<div id="cpStep1">' +
    '<div class="srow"><label>' + (I18N.t('app.account.current_password_label')) + '</label><input type="password" id="cpCurr" class="kb-input" autocomplete="current-password" placeholder="' + (I18N.t('app.account.current_password_placeholder')) + '"></div>' +
    '<div class="srow"><label>' + (I18N.t('app.account.new_password_label')) + '</label><input type="password" id="cpNew" class="kb-input" autocomplete="new-password" placeholder="' + (I18N.t('app.account.new_password_placeholder')) + '"></div>' +
    '<div class="srow"><label>' + (I18N.t('app.account.confirm_label')) + '</label><input type="password" id="cpConfirm" class="kb-input" autocomplete="new-password" placeholder="' + (I18N.t('app.account.confirm_new_password_placeholder')) + '"></div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
    '<button class="bt bt-o" onclick="_accountCloseModal()">' + (I18N.t('app.account.cancel')) + '</button>' +
    '<button class="bt bt-p" onclick="_accountDoChangePassword()">' + (I18N.t('app.account.confirm_change')) + '</button>' +
    '</div>' +
    (hasEmail && window._emailConfigured !== false ? '<p style="text-align:center;margin-top:12px"><a style="font-size:12px;color:var(--a);cursor:pointer;text-decoration:none" onclick="_accountShowResetPassword()">' + I18N.t('app.account.forgot_password_link') + '</a></p>' : '') +
    '</div>'
  );
}

// ===== 邮箱服务未配置统一提示（#535 P1：主文案 + 可行动指引 + 配置文档链接） =====
// 三个入口（重置密码/设置密码/换邮箱）共用：主文案说明被什么挡住（contextKey 按
// 场景区分），副文案给用户可行动的下一步（普通用户=联系管理员，管理员=按文档配置），
// toast 附「查看配置帮助」深链 help.html 邮件配置章节（§5.2 SMTP/Resend）。
function _accountShowEmailNotConfiguredToast(contextKey) {
  var link = I18N.t('app.account.email_view_config_help', null, I18N.t('app.account.view_config_help'));
  app.showToast(
    I18N.t(contextKey, null, I18N.t('app.account.email_not_configured')),
    I18N.t('app.account.email_not_configured_hint'),
    6000,
    { text: link, url: '/static/help.html#/developer/04-storage-and-email' }
  );
}

// ===== Reset password (有密码但忘记当前密码——通过邮箱验证码重置) =====
function _accountShowResetPassword() {
  var hasEmail = _accountProfile && _accountProfile.email;
  if (!hasEmail || window._emailConfigured === false) {
    if (window._emailConfigured === false) {
      _accountShowEmailNotConfiguredToast('app.account.reset_pwd_blocked_by_email');
    } else {
      app.showToast(I18N.t('app.account.set_password_needs_email'));
    }
    return;
  }
  var email = _accountProfile.email;
  // Close current modal and create a new one via standard _accountShowModal
  _accountCloseModal();
  _accountShowModal(
    '<h3>' + I18N.t('app.account.reset_password') + '</h3>' +
    '<p class="tx t2" style="margin-bottom:12px">' + I18N.t('app.account.reset_password_hint') + '</p>' +
    '<div id="rpStep1"><p class="tx t2" style="margin-bottom:12px">' +
      (I18N.t('app.account.code_sent_to_email_hint')) + '：<b>' + _accountEsc(email) + '</b></p>' +
    '<button class="bt bt-p" style="width:100%" onclick="_accountSendResetPasswordCode()">' + (I18N.t('app.account.send_code')) + '</button></div>' +
    '<div id="rpStep2" style="display:none;flex-direction:column">' +
    '<p class="tx t2" style="margin-bottom:8px">' + (I18N.t('app.account.code_sent')) + '</p>' +
    '<div class="srow"><label>' + (I18N.t('app.account.verification_code')) + '</label><input type="text" id="rpCode" class="kb-input" maxlength="6" placeholder="******"></div>' +
    '<div class="srow"><label>' + (I18N.t('app.account.new_password')) + '</label><input type="password" id="rpPwd" class="kb-input" autocomplete="new-password" placeholder="' + (I18N.t('app.account.new_password_placeholder')) + '"></div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
    '<button class="bt bt-o" onclick="_accountCloseModal()">' + (I18N.t('app.account.cancel')) + '</button>' +
    '<button class="bt bt-p" onclick="_accountDoResetPassword()">' + (I18N.t('app.account.confirm_set')) + '</button>' +
    '</div></div>'
  );
}

function _accountSendResetPasswordCode() {
  var email = _accountProfile ? _accountProfile.email : '';
  if (!email) { app.showToast(I18N.t('app.account.error_failed')); return; }
  API.post('/auth/forgot-password/send-code', {email: email}).then(function() {
    app.showToast(I18N.t('app.account.code_sent'));
    document.getElementById('rpStep1').style.display = 'none';
    document.getElementById('rpStep2').style.display = 'flex';
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

function _accountDoResetPassword() {
  var email = _accountProfile ? _accountProfile.email : '';
  var code = document.getElementById('rpCode').value.trim();
  var pwd = document.getElementById('rpPwd').value.trim();
  if (!code || !pwd) { app.showToast(I18N.t('app.account.please_fill_all')); return; }
  API.post('/auth/forgot-password/reset', {email: email, code: code, new_password: pwd}).then(function(d) {
    _accountCloseModal();
    // Reset password triggers token_version+1 (all devices logged out) —
    // current session is invalidated, must redirect to login
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    app.showToast(I18N.t('app.account.password_changed'), I18N.t('app.account.other_devices_logged_out'));
    setTimeout(function() { window.location.href = '/'; }, 1500);
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

function _accountDoChangePassword() {
  var isZh = _accountIsZh();
  var curr = document.getElementById('cpCurr').value.trim();
  var np = document.getElementById('cpNew').value.trim();
  var cp = document.getElementById('cpConfirm').value.trim();
  if (!curr || !np || !cp) { app.showToast(I18N.t('app.account.please_fill_all')); return; }
  if (np !== cp) { app.showToast(I18N.t('app.account.passwords_do_not_match')); return; }
  API.post('/auth/change-password', {current_password: curr, new_password: np}).then(function(d) {
    if (d.token) { localStorage.setItem('token', d.token); if (typeof API.setToken === 'function') API.setToken(d.token); }
    _accountCloseModal();
    app.showToast(I18N.t('app.account.password_changed'), I18N.t('app.account.other_devices_logged_out'));
    renderAccount('security');
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

// ===== Set password (无密码用户) =====
function _accountShowSetPassword() {
  // 第一层：系统邮件服务配置检查
  if (window._emailConfigured === false) {
    _accountShowEmailNotConfiguredToast('app.account.set_pwd_blocked_by_email');
    return;
  }
  // 第二层：用户邮箱检查（系统已配置但用户无邮箱 → 引导绑定）
  if (!_accountProfile || !_accountProfile.email) {
    app.showToast(I18N.t('app.account.set_password_needs_email'));
    _accountShowChangeEmail();
    return;
  }
  var email = _accountProfile.email;
  _accountShowModal(
    '<h3>' + (I18N.t('app.account.set_password')) + '</h3>' +
    '<p class="tx t2" style="margin-bottom:12px;line-height:1.6">' +
      I18N.t('app.account.set_password_hint') +
    '</p>' +
    '<div id="spStep1"><p class="tx t2" style="margin-bottom:12px">' +
      (I18N.t('app.account.code_sent_to_email_hint')) + '：<b>' + _accountEsc(email) + '</b></p>' +
    '<button class="bt bt-p" style="width:100%" onclick="_accountSendSetPasswordCode()">' + (I18N.t('app.account.send_code')) + '</button></div>' +
    '<div id="spStep2" style="display:none;flex-direction:column">' +
    '<p class="tx t2" style="margin-bottom:8px">' + (I18N.t('app.account.code_sent')) + '</p>' +
    '<div class="srow"><label>' + (I18N.t('app.account.verification_code')) + '</label><input type="text" id="spCode" class="kb-input" maxlength="6" placeholder="******"></div>' +
    '<div class="srow"><label>' + (I18N.t('app.account.new_password')) + '</label><input type="password" id="spPwd" class="kb-input" autocomplete="new-password" placeholder="' + (I18N.t('app.account.new_password_placeholder')) + '"></div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
    '<button class="bt bt-o" onclick="_accountCloseModal()">' + (I18N.t('app.account.cancel')) + '</button>' +
    '<button class="bt bt-p" onclick="_accountDoSetPassword()">' + (I18N.t('app.account.confirm_set')) + '</button>' +
    '</div></div>'
  );
}

function _accountSendSetPasswordCode() {
  var isZh = _accountIsZh();
  API.post('/auth/set-password/send-code').then(function() {
    app.showToast(I18N.t('app.account.code_sent'));
    document.getElementById('spStep1').style.display = 'none';
    document.getElementById('spStep2').style.display = 'flex';
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

function _accountDoSetPassword() {
  var code = document.getElementById('spCode').value.trim();
  var pwd = document.getElementById('spPwd').value.trim();
  if (!code || !pwd) { app.showToast(I18N.t('app.account.please_fill_all')); return; }
  API.post('/auth/set-password', {code: code, new_password: pwd}).then(function() {
    _accountCloseModal();
    app.showToast(I18N.t('app.account.password_set_success'), I18N.t('app.account.can_login_with_password'));
    renderAccount('security');
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

// ===== Change email =====
function _accountShowChangeEmail() {
  if (window._emailConfigured === false) {
    _accountShowEmailNotConfiguredToast('app.account.change_email_blocked_by_email');
    return;
  }
  var isZh = _accountIsZh();
  _accountShowModal(
    '<h3>' + (I18N.t('app.account.change_email_btn')) + '</h3>' +
    '<div id="ceStep1"><p class="tx t2" style="margin-bottom:12px">' + (I18N.t('app.account.code_sent_to_new_email_hint')) + '</p>' +
    '<div class="srow"><label>' + (I18N.t('app.account.new_email')) + '</label><input type="text" id="ceEmail" class="kb-input" placeholder="email@example.com"></div>' +
    '<button class="bt bt-p" style="width:100%" onclick="_accountSendChangeEmailCode()">' + (I18N.t('app.account.send_code')) + '</button></div>' +
    '<div id="ceStep2" style="display:none;flex-direction:column">' +
    '<p class="tx t2" style="margin-bottom:8px" id="ceStep2Hint">' + (I18N.t('app.account.code_sent_to_new_email')) + '</p>' +
    '<div class="srow"><label>' + (I18N.t('app.account.verification_code')) + '</label><input type="text" id="ceCode" class="kb-input" maxlength="6" placeholder="******"></div>' +
    '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
    '<button class="bt bt-o" onclick="_accountCloseModal()">' + (I18N.t('app.account.cancel')) + '</button>' +
    '<button class="bt bt-p" onclick="_accountDoChangeEmail()">' + (I18N.t('app.account.confirm_change_email')) + '</button>' +
    '</div></div>'
  );
}

function _accountSendChangeEmailCode() {
  var email = document.getElementById('ceEmail').value.trim();
  if (!email) { app.showToast(I18N.t('app.account.enter_email')); return; }
  API.post('/auth/change-email/send-code', {new_email: email}).then(function() {
    app.showToast(I18N.t('app.account.code_sent'));
    // Update step2 hint with actual email address (delayed to avoid null ref in _accountShowModal)
    var hintEl = document.getElementById('ceStep2Hint');
    if (hintEl) hintEl.innerHTML = I18N.t('app.account.code_sent_to_new_email') + '：<b>' + _accountEsc(email) + '</b>';
    document.getElementById('ceStep1').style.display = 'none';
    document.getElementById('ceStep2').style.display = 'flex';
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

function _accountDoChangeEmail() {
  var isZh = _accountIsZh();
  var email = document.getElementById('ceEmail').value.trim();
  var code = document.getElementById('ceCode').value.trim();
  if (!email || !code) { app.showToast(I18N.t('app.account.please_fill_all')); return; }
  API.post('/auth/change-email', {new_email: email, code: code}).then(function() {
    _accountCloseModal();
    app.showToast(I18N.t('app.account.email_changed'), I18N.t('app.account.old_email_disabled'));
    renderAccount('security');
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

// ===== Bind / Unbind provider =====

// 绑定方式选择弹窗（OAuth + 手动令牌）
function _accountBindProvider(provider, allowPat) {
    // 2026-11-08 用户裁定（口径 v5 → v6 职责分域）：弹窗结构=基线形态
    // （OAuth 已配 → stabs 双页签 [OAuth 授权](默认)+[手动令牌 PAT]；
    //   未配 → PAT 表单；引导页双模式 v4 不变）。
    // allowPat=上下文分域——安全 tab 第三方账号行(缺省 false)=纯 OAuth 域
    // （仅授权/重授权，无 PAT 表单）；引导页(true)=双模式（首次打通用）。
    // PAT 的一切归 凭据 tab。
    allowPat = allowPat === true;
    _accountLoadVcsCatalog();
  var isZh = _accountIsZh();
  var pName = _accountPlatformName(provider);
  var icon = (typeof platformIcon === 'function') ? platformIcon(provider, 24) : '';
  var bindings = (_accountProfile && _accountProfile.bindings) || [];
  var alreadyBound = bindings.indexOf(provider) >= 0;
  var titleText = alreadyBound
    ? I18N.t('app.account.update_platform_token', {platform: pName})
    : I18N.t('app.account.link_platform', {platform: pName});

  // R7.4: 检查 OAuth App 是否已配置——未配时只显示手动令牌输入，不显示 OAuth tab
  // #535 修复：_accountProvidersData 只在账号安全 tab 被填充——经新手引导页等入口
  // 进来时缓存为 null，原实现保守猜「未配置」导致已配置场景也只有 token 一个页签。
  // 现改为：缓存缺失时主动 fetch /auth/providers（唯一权威源）再渲染弹窗，
  // 彻底消除「依赖某个入口恰好填过缓存」；缓存存在则沿用（省一次请求）。
  var _providersData = _accountProvidersData;
  var _renderBindModal = function() {
    var _providerInfo = (_providersData.providers || []).find(function(p) { return p.provider === provider; });
    var _oauthConfigured = !!(_providerInfo && _providerInfo.configured);

  var html = '<h3 style="display:flex;align-items:center;gap:8px">' + icon + ' ' + titleText + '</h3>';

  if (_oauthConfigured) {
    // 基线形态：经典 stabs 双页签（OAuth 默认选中 + 手动令牌 PAT）
    if (allowPat) {
      html += '<div class="stabs" style="margin-bottom:16px">';
      html += '<a class="ac" id="bindTabOauth" onclick="_accountSwitchBindTab(\'oauth\')">' + (I18N.t('app.account.oauth_auth')) + '</a>';
      html += '<a id="bindTabToken" onclick="_accountSwitchBindTab(\'token\')">' + (I18N.t('app.account.manual_token')) + '</a>';
      html += '</div>';
    }

    var oauthBtnText = alreadyBound ? (I18N.t('app.account.re_authorize')) : (I18N.t('app.account.authorize'));
    html += '<div id="bindPanelOauth">';
    var hintKey = alreadyBound ? 'app.account.oauth_rebind_hint' : 'app.account.oauth_redirect_hint';
    html += '<p class="tx t2" style="margin-bottom:16px">' + I18N.t(hintKey, {platform: pName}) + '</p>';
    html += '<button class="bt bt-p" style="width:100%" onclick="_accountDoOauthBind(\'' + provider + '\')">' + oauthBtnText + '</button>';
    html += '</div>';

    if (allowPat) {
      html += '<div id="bindPanelToken" style="display:none">';
      html += _accountManualBindForm(provider, isZh, pName, true);
      html += '</div>';
    }
  } else if (allowPat) {
    // 引导页上下文：OAuth 未配置 → PAT 表单直达（首次打通用）
    html += '<div id="bindPanelToken">';
    html += _accountManualBindForm(provider, isZh, pName, false);
    html += '</div>';
  } else {
    // 安全 tab 上下文（allowPat=false）：OAuth 未配 → 指路凭据管理（PAT 归凭据域）
    html += '<div style="background:var(--bg);border:1px solid var(--b);border-radius:10px;padding:14px;margin-bottom:12px">';
    html += '<p class="tx t2" style="margin-bottom:10px">' + (isZh
      ? I18N.t('app.account.oauth_not_enabled_prefix') + _accountEsc(pName) + I18N.t('app.account.oauth_not_enabled_suffix')
      : 'OAuth authorization is not enabled for this platform yet. Configure a repository credential (PAT) for ' + _accountEsc(pName) + ' from the Credentials tab.');
    html += '</div>';
    html += '<button class="bt bt-o" style="width:100%" onclick="_accountGoCredentialsTab()">' + (isZh ? I18N.t('app.account.go_configure_repo_cred') : 'Configure credential') + '</button>';
    html += '</div>';
  }

    _accountShowModal(html);
  };

  if (_providersData) {
    // 缓存已有（账号安全 tab 已填充 / 引导页已写入）→ 直接渲染，零额外请求
    _renderBindModal();
  } else {
    // #535 防御层：缓存缺失（新手引导页等入口）→ 主动拉权威配置再渲染，
    // 不再保守猜「未配置」；拉取失败才按未配置降级（与旧行为一致）
    API.get('/auth/providers').then(function(d) {
      _providersData = d && d.providers ? d : { providers: d && d.providers || [] };
      _accountProvidersData = _providersData;
      _renderBindModal();
    }).catch(function() {
      _providersData = { providers: [] };
      _renderBindModal();
    });
  }
}

function _accountOpenPatForm(provider) {
  // D102-L: 凭据 tab 的 PAT 配置入口（表单复用绑定模态的 manual form——
  // 写入接口 /settings/credentials 原样）
  var isZh = _accountIsZh();
  var pName = _accountPlatformName(provider);
  var icon = (typeof platformIcon === 'function') ? platformIcon(provider, 24) : '';
  var html = '<h3 style="display:flex;align-items:center;gap:8px">' + icon + ' ' + _accountEsc(pName) + ' PAT</h3>';
  html += _accountManualBindForm(provider, isZh, pName, false);
  _accountShowModal(html);
}

function _accountClearPat(provider) {
  // 清除 PAT = PUT 空串（空=显式清除），D105k 统一确认弹层（文案按四象限语义区分）
  _accountClearPatConfirmMsg(provider, function(msg) {
    app.showConfirm(msg, function() {
      _accountDoClearPat(provider);
    }, {danger: true});
  });
}

function _accountDoClearPat(provider) {
  // D102-P 统一（口径 v6 终态）：全平台清除走泛型 platform_tokens 空串；
  // D102-M：清除后回落 OAuth 供给（如已绑定）
  var body = {platform_tokens: {}};
  body.platform_tokens[provider] = '';
  API.put('/settings/credentials', body, 15000).then(function(d) {
    app.showToast(I18N.t('app.account.pat_cleared'));
    // D105k：清除可能触发平台级解绑级联（无 OAuth 时删 ei 归属行+user_repos）——
    // 失效仓库页缓存与 _projectsContent，否则项目页复用"清空期间"的空列表缓存
    try {
      if (typeof app !== 'undefined' && app._clearCachedReposData) app._clearCachedReposData();
      localStorage.removeItem('repo_member_cache');
      if (d && d.bindings) {
        var _u = JSON.parse(localStorage.getItem('user') || '{}');
        _u.bindings = d.bindings;
        localStorage.setItem('user', JSON.stringify(_u));
      }
      // R1 对齐解绑 handler：当前选中仓库属于被清平台 → 清理
      var _cr = JSON.parse(sessionStorage.getItem('currentRepo') || 'null');
      if (_cr && _cr.repoKey && _cr.repoKey.indexOf(provider + ':') === 0) {
        sessionStorage.removeItem('currentRepo');
      }
      // R2 对齐解绑 handler：角色可能变化（托管仓库归属）
      if (typeof app !== 'undefined' && app.loadProfile) app.loadProfile();
    } catch (e) {}
    if (typeof window.invalidateProjectsCache === 'function') window.invalidateProjectsCache();
    _accountLoadCredentials();
  }).catch(function(e) {
    app.showToast(e.message || I18N.t('app.account.pat_clear_failed'));
  });
}

function _accountClearPatConfirmMsg(provider, cb) {
  var pName = _accountPlatformName(provider);
  var msg = I18N.t('app.account.confirm_clear_pat_prefix') + pName + I18N.t('app.account.confirm_clear_pat_suffix');
  // 有 OAuth 绑定 → 清除后回落 OAuth 供给；无 OAuth → 平台级解绑（项目列表同步清除）
  fetch('/auth/identities', {headers: {Authorization: 'Bearer ' + (localStorage.getItem('token') || '')}})
    .then(function(r) { return r.json(); }).catch(function() { return {identities: []}; })
    .then(function(d) {
      var ident = (d.identities || []).find(function(i) { return i.provider === provider; });
      var hasOAuth = !!(ident && ident.has_oauth_token);
      msg += hasOAuth
        ? I18N.t('app.account.pat_clear_fallback_note')
        : I18N.t('app.account.pat_clear_unbound_note');
      cb(msg);
    });
}

function _accountGoSecurityTab() {
  // D102-K 续（用户纠偏）：引导页 → 用户设置安全 tab（绑定关系正式管理入口）
  try { _accountCloseModal(); } catch (e) {}
  renderAccount('security');
}

function _accountGoCredentialsTab() {
  // D102-L: 绑定弹窗 OAuth 未配置 → 指路凭据管理（PAT=显式回退正式路径）
  _accountCloseModal();
  renderAccount('credentials');
}

function _accountSwitchBindTab(tab) {
  var oauthPanel = document.getElementById('bindPanelOauth');
  var tokenPanel = document.getElementById('bindPanelToken');
  var tabOauth = document.getElementById('bindTabOauth');
  var tabToken = document.getElementById('bindTabToken');
  if (!oauthPanel || !tokenPanel) return;
  if (tab === 'oauth') {
    oauthPanel.style.display = '';
    tokenPanel.style.display = 'none';
    if (tabOauth) tabOauth.classList.add('ac');
    if (tabToken) tabToken.classList.remove('ac');
  } else {
    oauthPanel.style.display = 'none';
    tokenPanel.style.display = '';
    if (tabOauth) tabOauth.classList.remove('ac');
    if (tabToken) tabToken.classList.add('ac');
  }
}

function _accountManualBindForm(provider, isZh, pName, oauthConfigured) {
  // 令牌创建直达链接（实测 200 / 官方支持的预选参数）：
  // - GitHub: scopes=repo 预勾选仓库权限，description 预填备注
  // - GitCode: gitcode.com/settings/personal-access-tokens/new
  var tokenCreateUrl = _accountPatCreateUrl(provider);
  var tokenCreateLabel = tokenCreateUrl ? (tokenCreateUrl.split('/')[2] || '') : '';
  // 帮助文案第一步做成超链接，直达令牌创建页（新标签页打开）
  // D102：帮助文案按 provider 查表（gitee 曾错用 gitcode 文案——引导指错站点）
  // 帮助文案按 provider 查表（专用文案只给平台卡在册且已写死步骤的存量平台）；
  // 其余平台（gitlab 等）走通用文案 + 卡目录 pat_create_url 的 host——
  // 此前 gitlab 落到 gitcode_token_help，把用户导向 GitCode 站点（错站）。
  var _helpMap = {github: 'app.account.github_token_help',
                  gitee: 'app.account.gitee_token_help',
                  gitcode: 'app.account.gitcode_token_help'};
  var _helpKey = _helpMap[provider] || 'app.account.pat_token_help_generic';
  var helpSteps = _helpKey === 'app.account.pat_token_help_generic'
    ? I18N.t(_helpKey, {host: (tokenCreateUrl ? (tokenCreateUrl.split('/')[2] || '') : '') || provider})
    : I18N.t(_helpKey);
  // 单一直达入口（方案 A）：通栏按钮是唯一的令牌创建链接，帮助步骤保持纯文本
  // （步骤 2/3 是权限/复制指引有增量；第 1 步链接与按钮同目标，属重复入口，已移除）
  var createLinkBtn = '<a href="' + tokenCreateUrl + '" target="_blank" rel="noopener noreferrer" class="bt" '
    + 'style="display:block;text-align:center;text-decoration:none;margin:0 0 12px;color:var(--a);border:1px solid var(--a)">'
    + (I18N.t('app.account.open_token_page')) + ' ' + _esc(tokenCreateLabel) + '</a>';

  var html = '';
  // R13.3: 未配 OAuth App 时提示 PAT 不会自动续期
  if (!oauthConfigured) {
    html += '<div style="background:var(--bg);border:1px solid var(--b);border-radius:10px;padding:12px 14px;margin-bottom:12px;display:flex;gap:8px;align-items:flex-start">'
      + '<span style="font-size:14px;flex-shrink:0">ℹ️</span>'
      + '<div style="font-size:12px;color:var(--t2);line-height:1.6">'
      + (I18N.t('app.account.pat_no_refresh_hint'))
      + '</div></div>';
  }
  html += createLinkBtn;
  html += '<div class="srow"><label>' + (I18N.t('app.account.access_token')) + '</label>'
    + '<input type="password" id="manualTokenInput" class="kb-input" placeholder="' + (I18N.t('app.account.paste_token')) + '">'
    + '</div>'
    + '<div id="manualBindError" style="display:none;color:var(--r);font-size:12px;padding:4px 0"></div>'
    + '<details style="margin:8px 0"><summary style="cursor:pointer;font-size:12px;color:var(--a)">' + (I18N.t('app.account.how_to_get_token')) + '</summary>'
    + '<div style="font-size:12px;color:var(--t2);padding:8px 0;line-height:1.6">' + helpSteps + '</div></details>'
    + '<button class="bt bt-p" style="width:100%" id="manualBindBtn" onclick="_accountDoManualBind(\'' + provider + '\')">'
    + (I18N.t('app.account.confirm_bind')) + '</button>';
  return html;
}

function _oauthOpenCenteredPopup(url) {
  var w = 600, h = 700;
  var screenLeft = window.screenLeft != null ? window.screenLeft : window.screenX;
  var screenTop = window.screenTop != null ? window.screenTop : window.screenY;
  var parentW = window.innerWidth || document.documentElement.clientWidth || screen.width;
  var parentH = window.innerHeight || document.documentElement.clientHeight || screen.height;
  var left = Math.round(screenLeft + (parentW - w) / 2);
  var top = Math.round(screenTop + (parentH - h) / 2);
  var features = 'width=' + w + ',height=' + h + ',left=' + left + ',top=' + top +
    ',resizable=yes,scrollbars=yes,status=yes';
  return window.open(url, 'oauth_popup', features);
}

function _isMobile() {
  return /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
}

function _accountDoOauthBind(provider) {
  // 即时反馈：按钮变灰 + spinner
  var btn = event && event.target ? event.target.closest('button') : null;
  if (btn) {
    btn.disabled = true;
    var origText = btn.textContent;
    btn.innerHTML = '<span class="acct-btn-spinner"></span>' + I18N.t('app.account.verifying');
    btn.dataset.origText = origText;
  }

  // 已绑定时用 prompt=login 强制第三方平台重新认证
  var bindings = (_accountProfile && _accountProfile.bindings) || [];
  var alreadyBound = bindings.indexOf(provider) >= 0;
  var promptParam = alreadyBound ? '&prompt=login' : '';

  // 移动端：直接全页跳转，不用弹窗
  if (_isMobile()) {
    API.get('/auth/bind/' + provider + promptParam.replace('&', '?')).then(function(d) {
      // 口径 v7：标记只在真正发起 OAuth 跳转时打——弹窗打开/取消不残留
      if (d.redirect_url) {
        try { sessionStorage.setItem('_bindReturnView', 'account'); } catch (e) {}
        window.location.href = d.redirect_url;
      }
    }).catch(function(e) {
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
      app.showToast(e.message || I18N.t('app.account.error_failed'));
    });
    return;
  }

  // 关键：在用户点击的同步上下文中先打开空白弹窗，防止浏览器拦截
  // 然后异步获取 URL 后设置 popup.location.href
  var popup = _oauthOpenCenteredPopup('');

  if (!popup) {
    // 弹窗被拦截——回退到全页跳转
    API.get('/auth/bind/' + provider + (promptParam ? '?prompt=login' : '')).then(function(d2) {
      if (d2.redirect_url) window.location.href = d2.redirect_url;
    }).catch(function(e2) {
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
      app.showToast(I18N.t('app.account.popup_blocked_hint') || I18N.t('app.account.error_failed'));
    });
    return;
  }

  // 锁定模态框——授权进行中不允许点击遮罩关闭（用 dataset 属性，不重建 DOM）
  var overlay = document.getElementById('accountModal');
  if (overlay) overlay.dataset.locked = '1';

  // 异步获取授权 URL
  API.get('/auth/bind/' + provider + '?popup=true' + promptParam).then(function(d) {
    if (!d.redirect_url) throw new Error('no_redirect_url');
    popup.location.href = d.redirect_url;

    // 注册一次性 message handler
    var timeoutId = null;
    var handled = false;

    function cleanup() {
      if (timeoutId) { clearTimeout(timeoutId); timeoutId = null; }
      if (pollId) { clearInterval(pollId); pollId = null; }
      window.removeEventListener('message', handler);
    }

    function handler(event) {
      if (event.origin !== window.location.origin) return;
      var data = event.data;
      if (!data || data.type !== 'oauth_result') return;
      if (handled) return;
      handled = true;
      cleanup();

      if (data.action === 'bind' && data.success) {
        _accountConfirmBindSuccess(provider, btn, true);  // fromPostMessage=true
      } else if (data.action === 'bind' && data.need_transfer) {
        _accountFetchTransferContext(provider, btn);
      } else if (data.action === 'bind' && !data.success) {
        if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
        _accountCloseModal();
        var errMsg = _accountTranslateOAuthError(data.error, provider);
        app.showToast(errMsg);
      } else {
        if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
        _accountCloseModal();
        app.showToast(I18N.t('app.account.error_failed'));
      }
    }

    window.addEventListener('message', handler);

    // 轮询检查弹窗关闭：降级路径（window.opener=null）不会 postMessage
    // 弹窗关闭但没收到消息 → 主动调后端确认状态
    var pollId = setInterval(function() {
      if (handled) { cleanup(); return; }
      if (popup.closed) {
        handled = true;
        cleanup();
        // 弹窗关闭但没收到消息——可能走了降级路径，调后端确认
        _accountConfirmBindSuccess(provider, btn);
      }
    }, 500);

    // 超时：60 秒
    timeoutId = setTimeout(function() {
      if (!handled) {
        handled = true;
        cleanup();
        if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
        if (popup.closed) {
          _accountConfirmBindSuccess(provider, btn);
        } else {
          _accountCloseModal();
          app.showToast(I18N.t('app.account.oauth_timeout_hint') || I18N.t('app.account.error_failed'));
        }
      }
    }, 60000);
  }).catch(function(e) {
    // 获取 URL 失败——关闭空白弹窗 + 关闭"授权进行中"遮罩
    try { popup.close(); } catch(e2) {}
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
    _accountCloseModal();
    app.showToast(e.message || I18N.t('app.account.error_failed'));
  });
}

function _accountConfirmBindSuccess(provider, btn, fromPostMessage) {
  // 调后端确认真实绑定状态——不信任 postMessage 的 success
  // 记录确认前的绑定状态，用于区分"绑定确实发生了"还是"用户直接关闭了弹窗"
  var _beforeBindings = (_accountProfile && _accountProfile.bindings) || [];
  var _wasBound = _beforeBindings.indexOf(provider) >= 0;

  API.get('/auth/me').then(function(d) {
    // 检查后端返回的 bindings 是否真的包含目标 provider
    var _afterBindings = d.bindings || [];
    var _nowBound = _afterBindings.indexOf(provider) >= 0;

    if (!_nowBound) {
      // 后端确认：该 provider 未绑定
      // 可能是用户手动关闭了弹窗（没完成授权），或 OAuth 流程中断
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
      _accountCloseModal();  // 关闭"授权进行中"遮罩
      // 已绑定的用户重新授权失败不提示（状态没变），首次绑定失败才提示
      if (!_wasBound) {
        app.showToast(I18N.t('app.account.bind_cancelled') || I18N.t('app.account.error_failed'));
      }
      return;
    }

    // 后端确认已绑定——但需要区分是"刚刚绑定的"还是"本来就绑定的"
    // fromPostMessage=true 表示收到了 OAuth 回调的 postMessage，绑定确实刚完成
    // fromPostMessage=undefined/false 表示是 popup.closed 轮询触发的，用户可能手动关闭了弹窗
    if (!fromPostMessage && _wasBound) {
      // 重新授权场景：用户本来就绑定了，popup 关闭但没收到 postMessage
      // 状态没变（从绑定→绑定），说明用户没有完成重新授权
      // 静默恢复——关闭"授权进行中"遮罩，不显示成功也不显示失败
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
      _accountCloseModal();
      return;
    }

    // 绑定确实成功（从无到有，或收到了 postMessage 确认）——更新本地状态
    try {
      var _u = JSON.parse(localStorage.getItem('user') || '{}');
      _u.bindings = _afterBindings;
      localStorage.setItem('user', JSON.stringify(_u));
    } catch(e2) {}
    localStorage.removeItem('repo_member_cache');

    // 独立成功反馈层（不依赖 modal 存在性）
    _accountShowBindSuccessToast(provider);

    // 确保旧 modal 关闭
    _accountCloseModal();

    // 统一跳转项目页 + 同步仓库
    _accountNavigateToRepos();
  }).catch(function(e) {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
    // 确认失败——可能绑定没成功，显示错误
    app.showToast(I18N.t('app.account.bind_failed'));
  });
}

function _accountShowBindSuccessToast(provider) {
  // 居中成功提示层——不依赖 modal，用户始终看到明确反馈
  var pName = _accountPlatformName(provider);
  var overlay = document.createElement('div');
  overlay.id = 'bindSuccessToast';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:1001;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.15);backdrop-filter:blur(4px);animation:fadeBg .2s ease';
  overlay.innerHTML =
    '<div style="background:var(--c,#fff);border-radius:20px;padding:40px 48px;text-align:center;box-shadow:0 12px 40px rgba(0,0,0,.15);animation:popIn .3s cubic-bezier(.4,0,.2,1)">' +
      '<div style="width:56px;height:56px;border-radius:50%;background:rgba(30,191,58,.12);display:flex;align-items:center;justify-content:center;margin:0 auto 16px">' +
        '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="rgb(30,191,58)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>' +
      '</div>' +
      '<p style="font-size:16px;font-weight:600;color:var(--t,#1d1d1f);margin:0 0 4px">' + I18N.t('app.account.platform_bind_success', {platform: pName}) + '</p>' +
      '<p style="font-size:13px;color:var(--t3,#86868b);margin:0">' + (I18N.t('app.account.bind_success_hint') || '') + '</p>' +
    '</div>';
  document.body.appendChild(overlay);

  // 点击遮罩快速关闭
  overlay.addEventListener('click', function(e) {
    if (e.target === overlay) overlay.remove();
  });

  // 1.5s 后自动消失
  setTimeout(function() {
    if (overlay.parentNode) {
      overlay.style.transition = 'opacity .25s ease';
      overlay.style.opacity = '0';
      setTimeout(function() { if (overlay.parentNode) overlay.remove(); }, 250);
    }
  }, 1500);
}

function _accountNavigateToRepos() {
  // 统一跳转：成功提示显示 800ms 后过渡到项目页
  // 让用户看到反馈再跳转，避免突然切页的割裂感
  setTimeout(function() {
    if (typeof app !== 'undefined' && app.goToRepos) {
      app.goToRepos();
    }
    // Issue #372 §40: 不再并发调 _forceSyncRepos——goToRepos 内部
    // GET /repos/member 在缓存过期时会自动调 VCS API 刷新缓存，
    // 效果与 /repos/sync 一致。并发两个请求会产生 abort 竞态。
  }, 800);
}

function _accountFetchTransferContext(provider, btn) {
  // popup 模式：binding_ticket 不在 postMessage 里，用 JWT 从后端获取
  API.get('/auth/transfer-context').then(function(ctx) {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
    // 展示转移确认弹窗（ctx.binding_ticket 来自后端，与 JWT session 绑定）
    _accountShowTransferConfirm(ctx.provider || provider, ctx.binding_ticket, '');
  }).catch(function(e) {
    if (btn) { btn.disabled = false; btn.textContent = btn.dataset.origText || ''; }
    var msg = (e && e.message) || I18N.t('app.account.transfer_failed');
    app.showToast(msg);
  });
}

function _accountTranslateOAuthError(errorCode, provider) {
  if (!errorCode) return I18N.t('app.account.bind_failed');
  var pName = _accountPlatformName(provider);
  // 复用已有的错误码翻译逻辑
  var errorKey = 'app.login.oauth_error_' + errorCode;
  var translated = I18N.t(errorKey);
  if (translated && translated !== errorKey) return translated;
  // 常见错误码
  var lower = errorCode.toLowerCase();
  if (lower.indexOf('bind_conflict') >= 0) return I18N.t('app.account.already_bound_to_another');
  if (lower.indexOf('bind_failed') >= 0) return I18N.t('app.account.bind_failed');
  if (lower.indexOf('token_failed') >= 0 || lower.indexOf('user_info_failed') >= 0)
    return I18N.t('app.account.network_error_retry');
  return errorCode;
}

function _accountDoManualBind(provider) {
  var isZh = _accountIsZh();
  var pName = _accountPlatformName(provider);
  var tokenEl = document.getElementById('manualTokenInput');
  var errEl = document.getElementById('manualBindError');
  var btn = document.getElementById('manualBindBtn');

  if (!tokenEl) return;
  var token = tokenEl.value.trim();
  if (!token) {
    if (errEl) { errEl.textContent = I18N.t('app.account.please_enter_token'); errEl.style.display = 'block'; }
    return;
  }

  // loading 状态：按钮内嵌 spinner
  btn.disabled = true;
  var origText = btn.textContent;
  btn.innerHTML = '<span class="acct-btn-spinner"></span>' + I18N.t('app.account.verifying');
  btn.dataset.origText = origText;
  if (errEl) errEl.style.display = 'none';

  // D90-N5（2026-11-07）：存量双平台走专用字段路径（S52 防双路径写同一域）；
  // 新平台（gitee/gitlab/...）走 platform_tokens 泛型入口（settings.py 已就绪，
  // verify + 双写 user_credentials <p>_pat + external_identities）
  var _LEGACY_FIELDS = { github: 'github_token', gitcode: 'gitcode_token' };
  var body = {};
  if (_LEGACY_FIELDS[provider]) {
    body[_LEGACY_FIELDS[provider]] = token;
  } else {
    body.platform_tokens = {};
    body.platform_tokens[provider] = token;
  }

  API.put('/settings/credentials', body, 15000).then(function(d) {
    localStorage.removeItem('repo_member_cache');
    try {
      var _u = JSON.parse(localStorage.getItem('user') || '{}');
      if (!_u.bindings) _u.bindings = [];
      if (_u.bindings.indexOf(provider) < 0) _u.bindings.push(provider);
      localStorage.setItem('user', JSON.stringify(_u));
    } catch(e) {}
    // 统一成功反馈 + 跳转项目页
    _accountCloseModal();
    _accountShowBindSuccessToast(provider);
    _accountNavigateToRepos();
  }).catch(function(e) {
    // 失败——检查是否需要转移绑定
    var detail = (e && e.detail && typeof e.detail === 'object') ? e.detail : {};
    if (detail.need_transfer && detail.binding_ticket) {
      // 转移绑定确认——携带用户输入的 token，转移成功后直接写入
      _accountCloseModal();
      _accountShowTransferConfirm(provider, detail.binding_ticket, token);
      return;
    }
    // 普通错误——不关闭弹窗，在弹窗内显示错误
    btn.disabled = false;
    btn.textContent = btn.dataset.origText || origText;
    var msg = _accountTranslateBindError(e, provider, isZh);
    if (errEl) { errEl.textContent = msg; errEl.style.display = 'block'; }
  });
}

function _accountTranslateBindError(e, provider, isZh) {
  var pName = _accountPlatformName(provider);
  var code = 0;
  if (e && e.detail && typeof e.detail === 'object') code = e.detail.code || 0;
  else if (e && e.status) code = e.status;
  switch (code) {
    case 401:
      return I18N.t('app.account.token_invalid_expired', {platform: pName});
    case 409:
      return I18N.t('app.account.already_bound_to_another');
    case 502:
      return I18N.t('app.account.network_error_retry');
    case 400:
      return I18N.t('app.account.token_format_invalid');
    default:
      return (e && e.message) || (I18N.t('app.account.bind_failed'));
  }
}

function _accountShowTransferConfirm(provider, ticket, token) {
  var pName = _accountPlatformName(provider);
  var html = '<h3>' + I18N.t('app.account.transfer_confirm_title') + '</h3>' +
    '<p class="tx" style="margin-bottom:8px;line-height:1.5">' + I18N.t('app.account.transfer_confirm_msg', {platform: pName}) + '</p>' +
    '<p class="tx t2" style="margin-bottom:20px;line-height:1.5">' + I18N.t('app.account.transfer_confirm_desc') + '</p>' +
    '<div id="transferError" style="display:none;margin-bottom:12px;padding:8px 12px;border-radius:8px;background:rgba(220,40,40,0.08);color:var(--r);font-size:12px"></div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
    '<button class="bt bt-o" id="transferCancelBtn">' + I18N.t('app.account.transfer_cancel_btn') + '</button>' +
    '<button class="bt bt-p" id="transferConfirmBtn">' + I18N.t('app.account.transfer_confirm_btn') + '</button>' +
    '</div>';
  _accountShowModal(html);

  document.getElementById('transferCancelBtn').onclick = function() {
    _accountCloseModal();
  };
  document.getElementById('transferConfirmBtn').onclick = function() {
    var btn = this;
    var errEl = document.getElementById('transferError');
    btn.disabled = true;
    btn.innerHTML = '<span class="acct-btn-spinner"></span>' + I18N.t('app.account.transfer_confirm_btn');
    if (errEl) errEl.style.display = 'none';
    API.post('/auth/transfer-bind', {binding_ticket: ticket, confirm: true, access_token: token || ''}, 15000).then(function() {
      localStorage.removeItem('repo_member_cache');
      try {
        var _u = JSON.parse(localStorage.getItem('user') || '{}');
        if (!_u.bindings) _u.bindings = [];
        if (_u.bindings.indexOf(provider) < 0) _u.bindings.push(provider);
        localStorage.setItem('user', JSON.stringify(_u));
      } catch(e) {}
      // 统一成功反馈 + 跳转项目页
      _accountCloseModal();
      _accountShowBindSuccessToast(provider);
      _accountNavigateToRepos();
    }).catch(function(e) {
      btn.disabled = false;
      btn.textContent = I18N.t('app.account.transfer_confirm_btn');
      var code = (e && e.detail && typeof e.detail === 'object') ? (e.detail.code || 0) : (e && e.status || 0);
      var msg;
      if (code === 410) msg = I18N.t('app.account.transfer_ticket_expired');
      else if (code === 400 && e.detail && e.detail.message && e.detail.message.indexOf('\u65e0\u6cd5\u8bbf\u95ee') >= 0)
        msg = I18N.t('app.account.transfer_original_no_login');
      else msg = I18N.t('app.account.transfer_failed');
      if (errEl) { errEl.textContent = msg; errEl.style.display = 'block'; }
    });
  };
}

function _accountUnbindProvider(provider, confirmed) {
  var isZh = _accountIsZh();
  // confirmed=true 表示用户已在仓库影响警告弹窗中确认
  if (!confirmed) {
    var msg = I18N.t('app.account.unbind_confirm');
    app.showConfirm(msg, function() {
      _accountUnbindProvider(provider, true);
    }, {danger: true});
    return;
  }
  var url = '/auth/bind/' + provider;
  API.del(url).then(function(d) {
    _accountOnUnbindSuccess(provider, d, isZh);
  }).catch(function(e) {
    // 需求17: 解绑有正在使用仓库的平台时弹窗警告具体受影响的仓库列表
    if (e && e.detail && e.detail.need_confirm && e.detail.warehouses) {
      var wl = e.detail.warehouses;
      var wlist = wl.map(function(w){return '<div style="font-size:12px;color:var(--t2);padding:2px 0">• ' + String(w).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>';}).join('');
      var wmsg = (I18N.t('app.account.unbind_affected_repos_prefix')) + '<div style="margin:8px 0">' + wlist + '</div>' + (I18N.t('app.account.unbind_continue'));
      app.showConfirm(wmsg, function() {
        // 用户确认后带 confirm=true 重新调用
        API.del('/auth/bind/' + provider + '?confirm=true').then(function(d) {
          _accountOnUnbindSuccess(provider, d, isZh);
        }).catch(_accountOnUnbindError);
      }, {danger: true});
    } else {
      _accountOnUnbindError(e);
    }
  });
}

// 解绑成功统一处理：更新 bindings → 清缓存 → 清理状态 → 刷新侧边栏 → 渲染
function _accountOnUnbindSuccess(provider, d, isZh) {
  _accountShowUnbindSuccess(provider, d, isZh);
  // D42: 刷新 localStorage.user.bindings，清除仓库缓存
  if (d && d.bindings) {
    try {
      var _u = JSON.parse(localStorage.getItem('user') || '{}');
      _u.bindings = d.bindings;
      localStorage.setItem('user', JSON.stringify(_u));
    } catch(e) {}
  }
  // 清除前端仓库缓存
  localStorage.removeItem('repo_member_cache');
  // R1/R2/R3: 清理过期仓库状态 + 内存缓存 + 刷新角色 + 引导用户
  _accountCleanupAfterUnbind(provider);
  // 刷新侧边栏仓库列表（后端 user_repos 已清理该平台数据）
  setTimeout(function() { if (typeof app !== 'undefined' && app.initSidebarRepos) app.initSidebarRepos(); }, 100);
  // 解绑不跳转（需求变更：_accountCleanupAfterUnbind 不再调 goToRepos）——
  // 用户留在 account 页看到成功 toast，renderAccount 重渲染安全页反映新绑定状态
  if (window._currentView !== 'repos') {
    renderAccount('security');
  }
}

// 解绑失败统一处理（普通路径 + 确认后路径共用）
// 注意：两条路径原代码的错误条件不完全一致——
// 普通路径只检查 need_set_password，确认后路径额外检查 code === 400。
// 统一为 need_set_password 检查（更宽松），因为 need_set_password 只在后端返回 400 时设置，
// code === 400 是冗余的二次确认。行为等价。
function _accountOnUnbindError(e) {
  if (e && e.detail && e.detail.need_set_password) {
    app.showToast(I18N.t('app.account.keep_one_login_method'),
                  I18N.t('app.account.set_password_before_unbind'));
  } else {
    app.showToast(e.message || I18N.t('app.account.error_failed'));
  }
}

// R1/R2/R3: 解绑平台后统一清理仓库状态
function _accountCleanupAfterUnbind(provider) {
  // R1: 检查 currentRepo 是否属于被解绑的平台 → 属于则清理（不跳转，见函数尾）
  try {
    var cr = JSON.parse(sessionStorage.getItem('currentRepo') || 'null');
    if (cr && cr.repoKey && cr.repoKey.indexOf(provider + ':') === 0) {
      // 当前选中仓库属于被解绑的平台 → 清理（跳转已按需求移除：解绑是用户在
      // account 页的主动操作，完成后留在原地；用户下次进入工作台/仓库列表时
      // 由既有路由逻辑落到正确页面，不在解绑瞬间打断）
      sessionStorage.removeItem('currentRepo');
    }
  } catch(e) {}

  // R3: 清理内存缓存 _cachedReposData（无条件——缓存数据可能含已解绑平台仓库）
  if (typeof app !== 'undefined' && app._clearCachedReposData) {
    app._clearCachedReposData();
  }
  // D105k：项目页渲染缓存同失效——否则解绑后回项目页复用解绑前的
  // _projectsContent，"被解绑且无 OAuth 的项目"仍显示（用户实弹）
  if (typeof window.invalidateProjectsCache === 'function') {
    window.invalidateProjectsCache();
  }

  // R2: 刷新角色（无条件——解绑可能影响托管仓库列表，角色可能变化）
  if (typeof app !== 'undefined' && app.loadProfile) {
    app.loadProfile();
  }
}

// R30: 解绑成功提示——含受影响仓库数量
function _accountShowUnbindSuccess(provider, d, isZh) {
  var pName = _accountPlatformName(provider);
  var affected = (d && d.affected_repos) || 0;
  if (affected > 0) {
    app.showToast(I18N.t('app.account.platform_unbound', {platform: pName}),
                  I18N.t('app.account.unbind_affected_repos_detail', {count: affected}));
  } else {
    app.showToast(I18N.t('app.account.platform_unbound', {platform: pName}),
                  I18N.t('app.account.platform_login_disabled'));
  }
}

// ===== Appearance tab (主题 + 语言) =====
function _accountLoadAppearance() {
  var isZh = _accountIsZh();
  var el = document.getElementById('accountTabContent');
  if (!el) return;
  var curTheme = app.getTheme ? app.getTheme() : (localStorage.getItem('grapeTheme') || 'apple');
  var curMode = app.getThemeMode ? app.getThemeMode() : (localStorage.getItem('grapeThemeMode') || 'light');
  var curLang = (typeof I18N !== 'undefined') ? I18N.getLang() : (localStorage.getItem('grapeLang') || 'zh');
  var html = '<div class="cd">'
    + '<h3 style="font-size:13px;font-weight:600;margin-bottom:16px">' + (I18N.t('app.account.theme')) + '</h3>'
    + '<p class="tx t2" style="margin-bottom:20px;font-size:12px;line-height:1.6">' + (I18N.t('app.account.choose_theme')) + '</p>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">'
    // Grape Default
    + '<div id="themeCardDefault" onclick="_accountSelectTheme(\'default\')"'
    + ' style="cursor:pointer;border:2px solid ' + (curTheme==='default'?'var(--a)':'var(--b)') + ';border-radius:14px;padding:20px;transition:all .25s;background:var(--c)">'
    + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">'
    + '<span style="font-size:14px;font-weight:600;color:var(--t)">Grape Default</span>'
    + (curTheme==='default'?'<span style="color:var(--a);font-size:12px;font-weight:600">' + (I18N.t('app.account.current')) + '</span>':'')
    + '</div>'
    + '<div style="height:80px;border-radius:10px;overflow:hidden;border:1px solid var(--b);display:flex">'
    + '<div style="width:60px;background:#0c1222;display:flex;flex-direction:column;align-items:center;padding:8px 0;gap:4px">'
    + '<div style="width:24px;height:3px;background:#1e2940;border-radius:2px"></div>'
    + '<div style="width:20px;height:3px;background:#1e2940;border-radius:2px"></div>'
    + '<div style="width:20px;height:3px;background:#3168e0;border-radius:2px"></div>'
    + '<div style="width:20px;height:3px;background:#1e2940;border-radius:2px"></div>'
    + '</div>'
    + '<div style="flex:1;background:#f5f6f8;padding:10px">'
    + '<div style="height:4px;width:60%;background:#101724;border-radius:2px;margin-bottom:6px"></div>'
    + '<div style="height:3px;width:40%;background:#5b667b;border-radius:2px;margin-bottom:8px"></div>'
    + '<div style="height:30px;background:#fff;border-radius:6px;border:1px solid #e3e6ee"></div>'
    + '</div></div>'
    + '<p style="font-size:11px;color:var(--t3);margin-top:10px">' + (I18N.t('app.account.theme_default_desc')) + '</p>'
    + '</div>'
    // Liquid Glass (Apple)
    + '<div id="themeCardApple" onclick="_accountSelectTheme(\'apple\')"'
    + ' style="cursor:pointer;border:2px solid ' + (curTheme==='apple'?'var(--a)':'var(--b)') + ';border-radius:14px;padding:20px;transition:all .25s;background:var(--c)">'
    + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">'
    + '<span style="font-size:14px;font-weight:600;color:var(--t)">Liquid Glass</span>'
    + (curTheme==='apple'?'<span style="color:var(--a);font-size:12px;font-weight:600">' + (I18N.t('app.account.current')) + '</span>':'')
    + '</div>'
    + '<div style="height:80px;border-radius:10px;overflow:hidden;border:1px solid #d2d2d7;display:flex">'
    + '<div style="width:60px;background:rgba(245,245,247,0.8);backdrop-filter:blur(10px);display:flex;flex-direction:column;align-items:center;padding:8px 0;gap:4px">'
    + '<div style="width:24px;height:3px;background:#d2d2d7;border-radius:2px"></div>'
    + '<div style="width:20px;height:3px;background:#d2d2d7;border-radius:2px"></div>'
    + '<div style="width:20px;height:3px;background:#0071e3;border-radius:2px"></div>'
    + '<div style="width:20px;height:3px;background:#d2d2d7;border-radius:2px"></div>'
    + '</div>'
    + '<div style="flex:1;background:#f5f5f7;padding:10px">'
    + '<div style="height:4px;width:60%;background:#1d1d1f;border-radius:2px;margin-bottom:6px"></div>'
    + '<div style="height:3px;width:40%;background:#6e6e73;border-radius:2px;margin-bottom:8px"></div>'
    + '<div style="height:30px;background:#fff;border-radius:12px;border:1px solid #d2d2d7;box-shadow:0 2px 8px rgba(0,0,0,0.04)"></div>'
    + '</div></div>'
    + '<p style="font-size:11px;color:var(--t3);margin-top:10px">' + (I18N.t('app.account.theme_apple_desc')) + '</p>'
    + '</div>'
    + '</div>' // end grid
    + '</div>'; // end cd

  // Mode section (only for apple theme)
  html += '<div class="cd" id="themeModeSection" style="' + (curTheme==='apple'?'':'opacity:0.4;pointer-events:none') + '">'
    + '<h3 style="font-size:13px;font-weight:600;margin-bottom:12px">' + (I18N.t('app.account.display_mode')) + '</h3>'
    + '<p class="tx t2" style="margin-bottom:14px;font-size:12px">' + (I18N.t('app.account.display_mode_hint')) + '</p>'
    + '<div style="display:flex;gap:10px">'
    + '<button onclick="_accountSelectThemeMode(\'light\')" style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:' + (curMode==='light'?'var(--a)':'var(--c)') + ';color:' + (curMode==='light'?'#fff':'var(--t2)') + ';transition:all .2s">' + (I18N.t('app.account.light_mode')) + '</button>'
    + '<button onclick="_accountSelectThemeMode(\'auto\')" style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:' + (curMode==='auto'?'var(--a)':'var(--c)') + ';color:' + (curMode==='auto'?'#fff':'var(--t2)') + ';transition:all .2s">' + (I18N.t('app.account.auto_mode')) + '</button>'
    + '</div></div>';

  // Language section
  html += '<div class="cd">'
    + '<h3 style="font-size:13px;font-weight:600;margin-bottom:12px">' + (I18N.t('app.account.language')) + '</h3>'
    + '<p class="tx t2" style="margin-bottom:14px;font-size:12px">' + (I18N.t('app.account.choose_language')) + '</p>'
    + '<div style="display:flex;gap:10px">'
    + '<button onclick="_accountSelectLang(\'zh\')" style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:' + (curLang==='zh'?'var(--a)':'var(--c)') + ';color:' + (curLang==='zh'?'#fff':'var(--t2)') + ';transition:all .2s">中文</button>'
    + '<button onclick="_accountSelectLang(\'en\')" style="padding:10px 22px;border-radius:980px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--b);background:' + (curLang==='en'?'var(--a)':'var(--c)') + ';color:' + (curLang==='en'?'#fff':'var(--t2)') + ';transition:all .2s">English</button>'
    + '</div></div>';

  el.innerHTML = html;
}

function _accountSelectTheme(theme) {
  var isZh = _accountIsZh();
  if (!app.setTheme) { app.showToast(I18N.t('app.account.theme_switch_unavailable')); return; }
  app.setTheme(theme);
  renderAccount('appearance');
  app.showToast(theme === 'apple' ? (I18N.t('app.account.switched_to_liquid_glass')) : (I18N.t('app.account.switched_to_grape_default')));
}

function _accountSelectThemeMode(mode) {
  if (!app.setThemeMode) return;
  app.setThemeMode(mode);
  renderAccount('appearance');
}

var _accountLangSwitching = false;
function _accountSelectLang(lang) {
  var isZh = _accountIsZh();
  if (lang === I18N.getLang() && I18N.isLangLoaded(lang)) return;
  if (_accountLangSwitching) return;
  _accountLangSwitching = true;
  var isRetry = (lang === I18N.getLang() && !I18N.isLangLoaded(lang));
  var buttons = document.querySelectorAll('[onclick^="_accountSelectLang"]');
  var origTexts = [];
  buttons.forEach(function(btn, i) {
    origTexts[i] = btn.textContent;
    btn.disabled = true;
    btn.style.opacity = '0.5';
    btn.style.cursor = 'wait';
    btn.textContent = isRetry ? (I18N.t('app.account.reloading')) : (I18N.t('app.account.switching'));
  });
  function restoreButtons() {
    buttons.forEach(function(btn, i) { btn.disabled = false; btn.style.opacity = ''; btn.style.cursor = ''; btn.textContent = origTexts[i]; });
  }
  try {
    I18N.setLang(lang).then(function(result) {
      if (result.cached) {
        // 缓存命中，无 Toast
      } else if (result.failed === 0) {
        if (typeof app !== 'undefined' && app.showToast) {
          app.showToast(isRetry ? (I18N.t('app.account.language_packs_reloaded')) : I18N.t('common.success'));
        }
      } else if (result.failed < result.total) {
        if (typeof app !== 'undefined' && app.showToast) {
          app.showToast(I18N.t('common.success') + ' (' + I18N.t('app.account.partial_load_failed_suffix', {failed: result.failed, total: result.total}) + ')');
        }
      } else {
        restoreButtons();
        if (typeof app !== 'undefined' && app.showToast) { app.showToast(I18N.t('common.error')); }
      }
    }).catch(function(e) {
      restoreButtons();
      if (typeof app !== 'undefined' && app.showToast) { app.showToast(I18N.t('common.error') + ': ' + (e && e.message ? e.message : '')); }
    }).finally(function() {
      _accountLangSwitching = false;
    });
  } catch(e) {
    restoreButtons();
    _accountLangSwitching = false;
    if (typeof app !== 'undefined' && app.showToast) { app.showToast(I18N.t('common.error') + ': ' + (e && e.message ? e.message : '')); }
  }
}

// ===== API Keys tab =====
function _accountLoadApiKeys() {
  var isZh = _accountIsZh();
  var el = document.getElementById('accountTabContent');
  if (!el) return;
  el.innerHTML = '<div class="cd"><div class="cd-h"><h2>API Keys</h2><button class="bt bt-p" style="font-size:12px;padding:7px 16px" onclick="_accountShowCreateApiKey()">' + (I18N.t('app.account.generate_key_btn')) + '</button></div><div id="accApiKeyList"><div class="loading">' + (I18N.t('app.account.loading')) + '</div></div></div>';

  API.get('/settings/api-keys').then(function(data) {
    var keys = (data && data.keys) || data || [];
    if (!Array.isArray(keys)) keys = [];
    var listEl = document.getElementById('accApiKeyList');
    if (!listEl) return;
    if (keys.length === 0) {
      listEl.innerHTML = '<div class="tx t2" style="padding:16px;text-align:center">' + (I18N.t('app.account.no_api_keys_short')) + '</div>';
      return;
    }
    var html = keys.map(function(k) {
      return '<div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--b)">' +
        '<div style="flex:1;min-width:0">' +
        '<div style="font-size:12px;font-weight:600;color:var(--t)">' + _accountEsc(k.name) + '</div>' +
        '<div style="font-size:11px;color:var(--t3);font-family:monospace;margin-top:2px">' + _accountEsc(k.prefix) + '</div>' +
        '<div style="font-size:10px;color:var(--t3);margin-top:2px">' + (I18N.t('app.account.created')) + ': ' + _accountEsc(fmtTimestamp8(k.created_at) || '—') +
        ' · ' + (I18N.t('app.account.last_used')) + ': ' + (k.last_used_at ? _accountEsc(fmtTimestamp8(k.last_used_at)) : I18N.t('app.account.last_used_never')) + '</div>' +
        '</div>' +
        '<button class="bt bt-s" style="color:var(--r)" onclick="_accountRevokeApiKey(\'' + k.id + '\',\'' + _accountJsStr(k.name) + '\')">' + (I18N.t('app.account.revoke')) + '</button>' +
        '</div>';
    }).join('');
    listEl.innerHTML = html;
  }).catch(function(e) {
    var listEl = document.getElementById('accApiKeyList');
    if (listEl) listEl.innerHTML = '<div class="error">' + _accountEsc(e.message || I18N.t('app.account.load_failed')) + '</div>';
  });
}

function _accountShowCreateApiKey() {
  app.showPrompt({
    title: I18N.t('app.account.enter_key_name_prompt'),
    placeholder: I18N.t('app.account.enter_key_name_prompt'),
    confirmText: I18N.t('app.account.confirm_create') || I18N.t('common.confirm') || '确认',
    validate: function(val) {
      if (!val) { app.showToast(I18N.t('app.account.name_required_short')); return false; }
      return true;
    },
    onConfirm: function(name) {
      API.post('/settings/api-keys', {name: name}).then(function(d) {
        var key = d.plaintext || d.key || '';
        _accountShowModal(
          '<h3>' + (I18N.t('app.account.api_key_created_modal')) + '</h3>' +
          '<p class="tx" style="color:var(--r);margin-bottom:12px">' + (I18N.t('app.account.api_key_copy_warning')) + '</p>' +
          '<div style="background:var(--bg);border:1px solid var(--b);border-radius:8px;padding:12px;margin-bottom:12px">' +
          '<code style="font-size:12px;word-break:break-all">' + _accountEsc(key) + '</code></div>' +
          '<div style="display:flex;gap:10px;justify-content:flex-end">' +
          '<button class="bt bt-p" onclick="_accountCopyApiKey(\'' + _accountJsStr(key) + '\')">' + (I18N.t('app.account.copy')) + '</button>' +
          '<button class="bt bt-o" onclick="_accountCloseModal()">' + (I18N.t('app.account.close')) + '</button>' +
          '</div>'
        );
        _accountLoadApiKeys();
      }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
    }
  });
}

function _accountCopyApiKey(key) {
  var isZh = _accountIsZh();
  if (navigator.clipboard) {
    navigator.clipboard.writeText(key).then(function() { app.showToast(I18N.t('app.account.copied')); });
  } else {
    var ta = document.createElement('textarea');
    ta.value = key; document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); app.showToast(I18N.t('app.account.copied')); } catch(e) { app.showToast(I18N.t('app.account.error_copy_failed')); }
    ta.remove();
  }
}

function _accountRevokeApiKey(id, name) {
  var isZh = _accountIsZh();
  app.showConfirm(I18N.t('app.account.revoke_key_confirm', {name: name}), function() {
    API.del('/settings/api-keys/' + id).then(function() {
      app.showToast(I18N.t('app.account.revoked'));
      _accountLoadApiKeys();
    }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
  }, {danger: true});
}

// ===== Delete account（原"危险区域"已合并到安全 tab 底部）=====

function _accountShowDeleteAccount() {
  var hasPwd = _accountProfile ? _accountProfile.has_password : false;
  var hasEmail = _accountProfile ? !!_accountProfile.email : false;
  var canVerifyByEmail = hasEmail && window._emailConfigured !== false;
  var bindings = (_accountProfile && _accountProfile.bindings) || [];

  // 动态渲染可用验证方式
  var methods = [];
  if (hasPwd) methods.push({id: 'pwd', label: I18N.t('app.account.verify_with_password')});
  if (canVerifyByEmail) methods.push({id: 'code', label: I18N.t('app.account.verify_with_code')});
  if (bindings.length > 0) methods.push({id: 'oauth', label: I18N.t('app.account.verify_with_oauth')});

  if (methods.length === 0) {
    app.showToast(I18N.t('app.account.keep_one_login_method'));
    return;
  }

  var html = '<h3 style="color:var(--r)">' + (I18N.t('app.account.delete_account')) + '</h3>';
  html += '<p class="tx" style="color:var(--r);margin-bottom:12px">' + (I18N.t('app.account.delete_account_warning_short')) + '</p>';

  // Step 1: 选择验证方式
  html += '<div id="daStep1"><p class="tx t2" style="margin-bottom:10px">' + (I18N.t('app.account.choose_verification_method')) + '</p><div style="display:flex;flex-direction:column;gap:8px">';
  methods.forEach(function(m) {
    html += '<button class="bt bt-o" style="width:100%" onclick="_accountDaChoose(\'' + m.id + '\')">' + m.label + '</button>';
  });
  html += '</div></div>';

  // Password step
  html += '<div id="daStepPwd" style="display:none;flex-direction:column;margin-top:12px">';
  html += '<div class="srow"><label>' + (I18N.t('app.account.password')) + '</label><input type="password" id="daPwd" class="kb-input" autocomplete="current-password" placeholder="' + (I18N.t('app.account.enter_password')) + '"></div>';
  html += '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px"><button class="bt bt-o" onclick="_accountCloseModal()">' + (I18N.t('app.account.cancel')) + '</button><button class="bt bt-r" onclick="_accountDoDeleteAccount()">' + (I18N.t('app.account.confirm_delete')) + '</button></div>';
  html += '</div>';

  // Code step
  html += '<div id="daStepCode" style="display:none;flex-direction:column;margin-top:12px">';
  html += '<button class="bt bt-o" style="margin-bottom:8px" onclick="_accountSendDeleteCode()">' + (I18N.t('app.account.send_code')) + '</button>';
  html += '<div class="srow"><label>' + (I18N.t('app.account.verification_code')) + '</label><input type="text" id="daCode" class="kb-input" maxlength="6" placeholder="******"></div>';
  html += '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px"><button class="bt bt-o" onclick="_accountCloseModal()">' + (I18N.t('app.account.cancel')) + '</button><button class="bt bt-r" onclick="_accountDoDeleteAccount()">' + (I18N.t('app.account.confirm_delete')) + '</button></div>';
  html += '</div>';

  // OAuth step
  if (bindings.length > 0) {
    html += '<div id="daStepOauth" style="display:none;flex-direction:column;margin-top:12px">';
    html += '<p class="tx t2" style="margin-bottom:8px">' + I18N.t('app.account.oauth_in_progress_hint') + '</p>';
    if (bindings.length === 1) {
      html += '<button class="bt bt-r" style="width:100%" onclick="_accountDaOauth(\'' + bindings[0] + '\')">' + (I18N.t('app.account.confirm_delete')) + '</button>';
    } else {
      bindings.forEach(function(p) {
        var pName = _accountPlatformName(p);  // D102: 查表（gitee 曾显示 GitCode）
        html += '<button class="bt bt-o" style="width:100%;margin-bottom:6px" onclick="_accountDaOauth(\'' + p + '\')">' + pName + '</button>';
      });
    }
    html += '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:8px"><button class="bt bt-o" onclick="_accountCloseModal()">' + (I18N.t('app.account.cancel')) + '</button></div>';
    html += '</div>';
  }

  _accountShowModal(html);
}

function _accountDaChoose(method) {
  document.getElementById('daStep1').style.display = 'none';
  if (method === 'pwd') document.getElementById('daStepPwd').style.display = 'flex';
  else if (method === 'code') document.getElementById('daStepCode').style.display = 'flex';
  else if (method === 'oauth') document.getElementById('daStepOauth').style.display = 'flex';
}

function _accountDaOauth(provider) {
  // OAuth 重新授权注销——popup 模式
  var popup = _oauthOpenCenteredPopup('');
  if (!popup) {
    app.showToast(I18N.t('app.account.popup_blocked_hint') || I18N.t('app.account.error_failed'));
    return;
  }
  var overlay = document.getElementById('accountModal');
  if (overlay) overlay.dataset.locked = '1';

  API.get('/auth/delete-account/oauth/' + provider + '?popup=true&prompt=login').then(function(d) {
    if (!d.redirect_url) throw new Error('no_redirect_url');
    popup.location.href = d.redirect_url;

    var handled = false;
    var timeoutId = null;

    function cleanup() {
      if (timeoutId) { clearTimeout(timeoutId); timeoutId = null; }
      if (pollId) { clearInterval(pollId); pollId = null; }
      window.removeEventListener('message', handler);
    }

    function handler(event) {
      if (event.origin !== window.location.origin) return;
      var data = event.data;
      if (!data || data.type !== 'oauth_result') return;
      if (handled) return;
      handled = true;
      cleanup();

      if (data.action === 'delete' && data.delete_oauth_state) {
        // OAuth 认证成功——用 oauth_state 完成注销
        API.post('/auth/delete-account', {oauth_state: data.delete_oauth_state}).then(function() {
          _accountCloseModal();
          localStorage.removeItem('token');
          localStorage.removeItem('user');
          app.showToast(I18N.t('app.account.account_deleted'), I18N.t('app.account.cooldown_started'));
          setTimeout(function() { window.location.href = '/'; }, 2000);
        }).catch(function(e) {
          var ov = document.getElementById('accountModal');
          if (ov) delete ov.dataset.locked;
          app.showToast(e.message || I18N.t('app.account.error_failed'));
        });
      } else {
        var ov2 = document.getElementById('accountModal');
        if (ov2) delete ov2.dataset.locked;
        app.showToast(data.error || I18N.t('app.account.error_failed'));
      }
    }

    window.addEventListener('message', handler);

    var pollId = setInterval(function() {
      if (handled) { cleanup(); return; }
      if (popup.closed) {
        handled = true;
        cleanup();
        var ov3 = document.getElementById('accountModal');
        if (ov3) delete ov3.dataset.locked;
        app.showToast(I18N.t('app.account.error_failed'));
      }
    }, 500);

    timeoutId = setTimeout(function() {
      if (!handled) {
        handled = true;
        cleanup();
        var ov = document.getElementById('accountModal');
        if (ov) delete ov.dataset.locked;
        app.showToast(I18N.t('app.account.oauth_timeout_hint') || I18N.t('app.account.error_failed'));
      }
    }, 60000);
  }).catch(function(e) {
    try { popup.close(); } catch(e2) {}
    var ov = document.getElementById('accountModal');
    if (ov) delete ov.dataset.locked;
    app.showToast(e.message || I18N.t('app.account.error_failed'));
  });
}

function _accountSendDeleteCode() {
  var isZh = _accountIsZh();
  API.post('/auth/delete-account/send-code').then(function() {
    app.showToast(I18N.t('app.account.code_sent'));
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

function _accountDoDeleteAccount() {
  var isZh = _accountIsZh();
  var pwd = document.getElementById('daPwd') ? document.getElementById('daPwd').value.trim() : '';
  var code = document.getElementById('daCode') ? document.getElementById('daCode').value.trim() : '';
  if (!pwd && !code) { app.showToast(I18N.t('app.account.enter_password_or_code')); return; }
  API.post('/auth/delete-account', {password: pwd, code: code}).then(function() {
    _accountCloseModal();
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    app.showToast(I18N.t('app.account.account_deleted'), I18N.t('app.account.cooldown_started'));
    setTimeout(function() { window.location.href = '/'; }, 2000);
  }).catch(function(e) { app.showToast(e.message || I18N.t('app.account.error_failed')); });
}

// ===== Modal (复用主应用样式) =====
var _accountModalEl = null;

function _accountShowModal(html) {
  // 移除已有 modal
  _accountCloseModal();
  var overlay = document.createElement('div');
  overlay.id = 'accountModal';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:1001;background:rgba(0,0,0,.35);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center';
  var box = document.createElement('div');
  box.style.cssText = 'width:440px;max-width:92vw;background:var(--c);border-radius:16px;padding:26px;max-height:90vh;overflow-y:auto;box-shadow:0 12px 40px rgba(0,0,0,.12)';
  box.innerHTML = html;
  overlay.appendChild(box);
  // 点击遮罩关闭——检查 locked 标记（授权进行中不允许关闭）
  overlay.addEventListener('click', function(e) {
    if (e.target === overlay && !overlay.dataset.locked) _accountCloseModal();
  });
  document.body.appendChild(overlay);
  _accountModalEl = overlay;
}

function _accountCloseModal() {
  if (_accountModalEl) {
    var el = _accountModalEl;
    el.classList.add('closing');
    setTimeout(function() { el.remove(); }, 150);
    _accountModalEl = null;
  }
}

// ===== Cloud Credentials (华为云 AK/SK) =====
// Issue #474 方案A：按需取回明文——点击 👁 走 /settings/credentials/reveal（二次确认：重新输入
// 当前 Grape 密码；服务端当次返回一次明文，Cache-Control: no-store；审计 + 高频告警）。
// 明文仅临时写入输入框展示，不落任何前端缓存；再次点击恢复掩码。
function _accountToggleReveal(field) {
  var el = document.getElementById(field === 'ak' ? 'accHuaweiAk' : 'accHuaweiSk');
  if (!el) return;
  var revealed = window._accountCloudCredRevealed || {};
  if (revealed[field]) {
    // 已显示 → 隐藏（恢复掩码）
    el.value = '********';
    el.type = 'password';
    revealed[field] = null;
    return;
  }
  _accountAskPasswordForReveal(field);
}

function _accountAskPasswordForReveal(field) {
  var html = '<h3 style="color:var(--t)">' + (I18N.t('app.account.reveal_title') || '') + '</h3>';
  html += '<p class="tx t2" style="margin-bottom:12px">' + (I18N.t('app.account.reveal_desc') || '') + '</p>';
  html += '<div class="srow"><label>' + (I18N.t('app.account.password')) + '</label><input type="password" id="revealPwd" class="kb-input" autocomplete="current-password" placeholder="' + (I18N.t('app.account.enter_password')) + '"></div>';
  html += '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:14px">';
  html += '<button class="bt bt-o" onclick="_accountCloseModal()">' + (I18N.t('app.account.cancel')) + '</button>';
  html += '<button class="bt bt-p" onclick="_accountDoReveal(\'' + field + '\')">' + (I18N.t('app.account.reveal_confirm') || '') + '</button>';
  html += '</div>';
  _accountShowModal(html);
  var pwdInput = document.getElementById('revealPwd');
  if (pwdInput) { pwdInput.focus(); }
}

function _accountDoReveal(field) {
  var pwdEl = document.getElementById('revealPwd');
  var password = pwdEl ? pwdEl.value : '';
  if (!password) { app.showToast(I18N.t('app.account.enter_password')); return; }
  // 只对华为云凭据取回；provider 固定 huawei_cloud
  API.post('/settings/credentials/reveal', {provider: 'huawei_cloud', field: field, password: password}, 15000).then(function(d) {
    _accountCloseModal();
    var value = (d && d.value) || '';
    if (!value) { app.showToast(I18N.t('app.account.reveal_not_configured')); return; }
    var el = document.getElementById(field === 'ak' ? 'accHuaweiAk' : 'accHuaweiSk');
    if (!el) return;
    el.value = value;
    el.type = 'text';
    var revealed = window._accountCloudCredRevealed || {};
    revealed[field] = value;
    window._accountCloudCredRevealed = revealed;
  }).catch(function(e) {
    var msg = (e && e.message) || '';
    // 服务端返回的中文提示已可读；此处补兜底文案
    if (msg && msg.indexOf('密码') !== -1) { app.showToast(msg); return; }
    if (msg) { app.showToast(msg); return; }
    app.showToast(I18N.t('app.account.reveal_failed'));
  });
}

function _accountSaveCloudCreds() {
  var akEl = document.getElementById('accHuaweiAk');
  var skEl = document.getElementById('accHuaweiSk');
  var body = {};
  var akVal = akEl && akEl.value ? akEl.value.trim() : '';
  var skVal = skEl && skEl.value ? skEl.value.trim() : '';
  // 与加载时的占位符哨兵比对：未改动（'********' 掩码 / 空框）不回传；
  // 输入新值回传；清空已有凭据（空串且原值为掩码）走显式清除语义。
  var orig = window._accountCloudCredOriginal || { ak: null, sk: null };
  // Issue #474 方案A：取回展示的明文视为「未改动」，不与回传 PUT（避免把明文原样写回触发多余写）
  var revealed = window._accountCloudCredRevealed || {};
  if (akVal !== '********' && akVal !== orig.ak && akVal !== revealed.ak) body.huawei_cloud_ak = akVal;
  if (skVal !== '********' && skVal !== orig.sk && skVal !== revealed.sk) body.huawei_cloud_sk = skVal;
  if (Object.keys(body).length === 0) {
    app.showToast(I18N.t('app.account.no_changes'));
    return;
  }
  API.put('/settings/credentials', body, 15000).then(function() {
    app.showToast(I18N.t('app.account.credentials_saved'));
    renderAccount('credentials');  // P2-6 修复：华为云凭据在 credentials tab，不是 security
  }).catch(function(e) {
    app.showToast(I18N.t('app.account.save_failed') + ': ' + (e.message || ''));
  });
}