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
  API.get('/auth/providers').then(function(providersData) {
    window._emailConfigured = !!(providersData && providersData.email_configured);
    _accountProvidersData = providersData;
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

    // 卡片2: 邮箱管理（R7: 无邮箱配置时不显示）
    // email_configured 通过 /auth/providers 获取，此处用 window._emailConfigured 缓存
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
        var expired = !!_expiredMap[p.provider];
        return _accountRenderProviderRow(p.name, p.provider, bound, identity, expired);
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

  API.get('/settings/credentials').then(function(d) {
    var hw = d.huawei_cloud || {};
    // Bugfix（显示/隐藏都是星号）：后端本就返回真实 ak/sk，此前前端丢弃真实值、
    // 用字面量 '********' 填充输入框 → 点"显示"也只是把星号从圆点变成明文星号。
    // 现在直接填真实值（type=password 默认遮蔽，👁 切换显示真实凭据），
    // 并记录原始值供保存时比对（未改动则不回传，避免无意义写库）。
    var akVal = hw.ak_exists ? (hw.ak || '') : '';
    var skVal = hw.sk_exists ? (hw.sk || '') : '';
    window._accountCloudCredOriginal = { ak: akVal, sk: skVal };

    var html = '';

    // 卡片: Huawei Cloud AK/SK — 和原通用配置保持一致风格
    html += '<div class="cd"><div class="cd-h"><h2>Huawei Cloud</h2></div>';
    html += '<div class="srow" style="align-items:center"><label>' + (I18N.t('app.account.access_key')) + '</label>';
    html += '<div style="display:flex;align-items:center;gap:6px;flex:1">';
    html += '<input type="password" id="accHuaweiAk" value="' + _accountEsc(akVal) + '" autocomplete="new-password" placeholder="' + (I18N.t('app.account.access_key')) + '" style="flex:1">';
    html += '<button type="button" onclick="_accountToggleSecret(&apos;accHuaweiAk&apos;)" style="border:1px solid var(--b);background:var(--bg);border-radius:8px;width:30px;height:30px;padding:0;font-size:14px;cursor:pointer;color:var(--t3);flex-shrink:0">👁</button>';
    html += '</div></div>';
    html += '<div class="srow" style="align-items:center"><label>' + (I18N.t('app.account.secret_key')) + '</label>';
    html += '<div style="display:flex;align-items:center;gap:6px;flex:1">';
    html += '<input type="password" id="accHuaweiSk" value="' + _accountEsc(skVal) + '" autocomplete="new-password" placeholder="' + (I18N.t('app.account.secret_key')) + '" style="flex:1">';
    html += '<button type="button" onclick="_accountToggleSecret(&apos;accHuaweiSk&apos;)" style="border:1px solid var(--b);background:var(--bg);border-radius:8px;width:30px;height:30px;padding:0;font-size:14px;cursor:pointer;color:var(--t3);flex-shrink:0">👁</button>';
    html += '</div></div>';
    html += '<p class="tx t2 mt8">' + (I18N.t('app.account.huawei_cloud_desc')) + '</p>';
    html += '<button class="bt bt-p mt8" onclick="_accountSaveCloudCreds()">' + (I18N.t('app.account.save')) + '</button>';
    html += '</div>';

    el.innerHTML = html;
  }).catch(function(e) {
    el.innerHTML = '<div class="error">' + _accountEsc(e.message || I18N.t('app.account.load_failed')) + '</div>';
  });
}

function _accountRenderProviderRow(label, provider, bound, identity, expired) {
  var isZh = _accountIsZh();
  var icon = (typeof platformIcon === 'function') ? platformIcon(provider, 20) : '';
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';

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
        + '</div>'
        + '<div style="font-size:12px;color:var(--t2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _accountEsc(nameLine) + '</div>'
        + (timeLine ? '<div style="font-size:11px;color:var(--t3);margin-top:2px">' + _accountEsc(timeLine) + '</div>' : '')
      + '</div>'
      + '<div style="display:flex;gap:6px;flex-shrink:0">'
        + '<button class="bt bt-s bt-o" style="font-size:11px" onclick="_accountBindProvider(\'' + provider + '\')">' + (I18N.t('app.account.update')) + '</button>'
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
    + '<button class="bt bt-s bt-p" style="font-size:11px" onclick="_accountBindProvider(\'' + provider + '\')">' + (I18N.t('app.account.link_btn')) + '</button>'
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

// ===== Reset password (有密码但忘记当前密码——通过邮箱验证码重置) =====
function _accountShowResetPassword() {
  var hasEmail = _accountProfile && _accountProfile.email;
  if (!hasEmail || window._emailConfigured === false) {
    if (window._emailConfigured === false) {
      app.showToast(I18N.t('app.account.email_not_configured') || 'Email service not configured, please contact administrator');
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
    app.showToast(I18N.t('app.account.email_not_configured') || 'Email service not configured, please contact administrator');
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
    app.showToast(I18N.t('app.account.email_not_configured') || 'Email service not configured, please contact administrator');
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
function _accountBindProvider(provider) {
  var isZh = _accountIsZh();
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';
  var icon = (typeof platformIcon === 'function') ? platformIcon(provider, 24) : '';
  var bindings = (_accountProfile && _accountProfile.bindings) || [];
  var alreadyBound = bindings.indexOf(provider) >= 0;
  var titleText = alreadyBound
    ? I18N.t('app.account.update_platform_token', {platform: pName})
    : I18N.t('app.account.link_platform', {platform: pName});

  // R7.4: 检查 OAuth App 是否已配置——未配时只显示手动令牌输入，不显示 OAuth tab
  var _providersData = _accountProvidersData || { providers: [] };
  var _providerInfo = (_providersData.providers || []).find(function(p) { return p.provider === provider; });
  var _oauthConfigured = !!(_providerInfo && _providerInfo.configured);

  var html = '<h3 style="display:flex;align-items:center;gap:8px">' + icon + ' ' + titleText + '</h3>';

  if (_oauthConfigured) {
    // OAuth App 已配——显示 OAuth tab + 手动令牌 tab
    // 选项卡
    html += '<div class="stabs" style="margin-bottom:16px">';
    html += '<a class="ac" id="bindTabOauth" onclick="_accountSwitchBindTab(\'oauth\')">' + (I18N.t('app.account.oauth_auth')) + '</a>';
    html += '<a id="bindTabToken" onclick="_accountSwitchBindTab(\'token\')">' + (I18N.t('app.account.manual_token')) + '</a>';
    html += '</div>';

    // OAuth panel (默认选中)
    var oauthBtnText = alreadyBound ? (I18N.t('app.account.re_authorize')) : (I18N.t('app.account.authorize'));
    html += '<div id="bindPanelOauth">';
    var hintKey = alreadyBound ? 'app.account.oauth_rebind_hint' : 'app.account.oauth_redirect_hint';
    html += '<p class="tx t2" style="margin-bottom:16px">' + I18N.t(hintKey, {platform: pName}) + '</p>';
    html += '<button class="bt bt-p" style="width:100%" onclick="_accountDoOauthBind(\'' + provider + '\')">' + oauthBtnText + '</button>';
    html += '</div>';

    // Token panel (隐藏)
    html += '<div id="bindPanelToken" style="display:none">';
    html += _accountManualBindForm(provider, isZh, pName, true);
    html += '</div>';
  } else {
    // R7.4: OAuth App 未配——只显示手动令牌输入，不显示 tab 切换
    html += '<div id="bindPanelToken">';
    html += _accountManualBindForm(provider, isZh, pName, false);
    html += '</div>';
  }

  _accountShowModal(html);
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
  var helpSteps = provider === 'github'
    ? (I18N.t('app.account.github_token_help'))
    : (I18N.t('app.account.gitcode_token_help'));

  var html = '';
  // R13.3: 未配 OAuth App 时提示 PAT 不会自动续期
  if (!oauthConfigured) {
    html += '<div style="background:var(--bg);border:1px solid var(--b);border-radius:10px;padding:12px 14px;margin-bottom:12px;display:flex;gap:8px;align-items:flex-start">'
      + '<span style="font-size:14px;flex-shrink:0">ℹ️</span>'
      + '<div style="font-size:12px;color:var(--t2);line-height:1.6">'
      + (I18N.t('app.account.pat_no_refresh_hint'))
      + '</div></div>';
  }
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
      if (d.redirect_url) window.location.href = d.redirect_url;
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
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';
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
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';
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
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';
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

  var field = provider === 'github' ? 'github_token' : 'gitcode_token';
  var body = {};
  body[field] = token;

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
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';
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
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';
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
    renderAccount('security');
    // 刷新侧边栏仓库列表（后端 user_repos 已清理该平台数据）
    setTimeout(function() { if (typeof app !== 'undefined' && app.initSidebarRepos) app.initSidebarRepos(); }, 100);
  }).catch(function(e) {
    // 需求17: 解绑有正在使用仓库的平台时弹窗警告具体受影响的仓库列表
    if (e && e.detail && e.detail.need_confirm && e.detail.warehouses) {
      var wl = e.detail.warehouses;
      var wlist = wl.map(function(w){return '<div style="font-size:12px;color:var(--t2);padding:2px 0">• ' + String(w).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>';}).join('');
      var wmsg = (I18N.t('app.account.unbind_affected_repos_prefix')) + '<div style="margin:8px 0">' + wlist + '</div>' + (I18N.t('app.account.unbind_continue'));
      app.showConfirm(wmsg, function() {
        // 用户确认后带 confirm=true 重新调用
        API.del('/auth/bind/' + provider + '?confirm=true').then(function(d) {
          _accountShowUnbindSuccess(provider, d, isZh);
          if (d && d.bindings) {
            try {
              var _u2 = JSON.parse(localStorage.getItem('user') || '{}');
              _u2.bindings = d.bindings;
              localStorage.setItem('user', JSON.stringify(_u2));
            } catch(e2) {}
          }
          localStorage.removeItem('repo_member_cache');
          renderAccount('security');
          // 刷新侧边栏仓库列表
          setTimeout(function() { if (typeof app !== 'undefined' && app.initSidebarRepos) app.initSidebarRepos(); }, 100);
        }).catch(function(e2) {
          var code = (e2 && e2.detail && e2.detail.code) || 0;
          if (code === 400 && e2.detail && e2.detail.need_set_password) {
            app.showToast(I18N.t('app.account.keep_one_login_method'),
                          I18N.t('app.account.set_password_before_unbind'));
          } else {
            app.showToast(e2.message || I18N.t('app.account.error_failed'));
          }
        });
      }, {danger: true});
    } else if (e && e.detail && e.detail.need_set_password) {
      app.showToast(I18N.t('app.account.keep_one_login_method'),
                    I18N.t('app.account.set_password_before_unbind'));
    } else {
      app.showToast(e.message || I18N.t('app.account.error_failed'));
    }
  });
}

// R30: 解绑成功提示——含受影响仓库数量
function _accountShowUnbindSuccess(provider, d, isZh) {
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';
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
        '<div style="font-size:10px;color:var(--t3);margin-top:2px">' + (I18N.t('app.account.created')) + ': ' + _accountEsc(k.created_at || '—') +
        (k.last_used_at ? ' · ' + (I18N.t('app.account.last_used')) + ': ' + _accountEsc(k.last_used_at) : '') + '</div>' +
        '</div>' +
        '<button class="bt bt-s" style="color:var(--r)" onclick="_accountRevokeApiKey(' + k.id + ',\'' + _accountJsStr(k.name) + '\')">' + (I18N.t('app.account.revoke')) + '</button>' +
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
        var pName = p === 'github' ? 'GitHub' : 'GitCode';
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
function _accountToggleSecret(inputId) {
  var el = document.getElementById(inputId);
  if (!el) return;
  el.type = el.type === 'password' ? 'text' : 'password';
}

function _accountSaveCloudCreds() {
  var akEl = document.getElementById('accHuaweiAk');
  var skEl = document.getElementById('accHuaweiSk');
  var body = {};
  var akVal = akEl && akEl.value ? akEl.value.trim() : '';
  var skVal = skEl && skEl.value ? skEl.value.trim() : '';
  // 与加载时的原始值比对：未改动不回传（真实值已在输入框，旧的 '********'
  // 跳过逻辑不再适用；否则每次保存都会把未变的凭据重写一遍）。
  var orig = window._accountCloudCredOriginal || { ak: null, sk: null };
  if (akVal !== '********' && akVal !== orig.ak) body.huawei_cloud_ak = akVal;
  if (skVal !== '********' && skVal !== orig.sk) body.huawei_cloud_sk = skVal;
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