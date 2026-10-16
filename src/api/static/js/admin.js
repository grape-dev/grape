/* Grape Admin — 配置中心前端逻辑 */
(function () {
  'use strict';

  var TOKEN_KEY = 'grape_admin_token';
  var COOKIE_PATH = '/api/admin';

  function $2(id) { return document.getElementById(id); }

  function getToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
  function setToken(t) { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); }

  function toast(msg, ok) {
    var wrap = document.querySelector('.toast-wrap');
    if (!wrap) { wrap = document.createElement('div'); wrap.className = 'toast-wrap'; document.body.appendChild(wrap); }
    var el = document.createElement('div');
    el.className = 'toast ' + (ok ? 'ok' : 'err');
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(function () { el.remove(); }, 3500);
  }

  var _isRefreshing = false;

  // error_code → I18N.t mapping; raw message goes to console only
  function errMsg(e) {
    var code = (e && e.error_code) || 'UNKNOWN';
    var localized = I18N.t('admin.error.' + code);
    if (e && e.message && e.error_code !== 'UNKNOWN') console.error('[admin] raw error:', e.message);
    return localized;
  }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    var tk = getToken();
    if (tk && path.indexOf('/login') < 0) opts.headers['Authorization'] = 'Bearer ' + tk;
    if (opts.body && typeof opts.body === 'object') opts.body = JSON.stringify(opts.body);
    return fetch('/api/admin' + path, opts).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) { var e = new Error((data && data.detail && data.detail.message) || I18N.t('admin.error.UNKNOWN')); e.status = r.status; e.error_code = (data && data.detail && data.detail.error_code) || 'UNKNOWN'; throw e; }
        return data;
      });
    }).catch(function (err) {
      // P1-2: 401 → refresh token → retry once, or redirect to login
      if (err && err.status === 401 && path.indexOf('/login') < 0 && !_isRefreshing) {
        _isRefreshing = true;
        return fetch('/api/admin/refresh', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
          credentials: 'same-origin'
        }).then(function (r) {
          if (!r.ok) throw new Error('refresh failed');
          return r.json();
        }).then(function (d) {
          setToken(d.token);
          _isRefreshing = false;
          // Retry original request with new token
          opts.headers['Authorization'] = 'Bearer ' + d.token;
          return fetch('/api/admin' + path, opts).then(function (r) {
            return r.json().then(function (data) {
              if (!r.ok) { var e = new Error((data && data.detail && data.detail.message) || I18N.t('admin.error.UNKNOWN')); e.status = r.status; e.error_code = (data && data.detail && data.detail.error_code) || 'UNKNOWN'; throw e; }
              return data;
            });
          });
        }).catch(function () {
          _isRefreshing = false;
          setToken(null);
          $2('mainView').classList.add('hid');
          $2('loginView').classList.remove('hid');
          // P1-4: 恢复浮动语言切换器
          var ls = document.querySelector('.lang-switch');
          if (ls) ls.style.display = '';
          throw err;
        });
      }
      throw err;
    });
  }

  // ====== 登录 ======
  function login() {
    var u = $2('loginUser').value.trim(), p = $2('loginPass').value.trim();
    if (!u || !p) { $2('loginErr').textContent = I18N.t('admin.login.enter_credentials'); return; }
    $2('loginErr').textContent = '';
    api('/login', { method: 'POST', body: { username: u, password: p } }).then(function (d) {
      setToken(d.token);
      $2('adminUser').textContent = d.user.username;
      showApp(d.pwd_initial);
    }).catch(function (e) { $2('loginErr').textContent = errMsg(e); });
  }

  // ====== 视图切换 ======
  function showApp(pwdInitial) {
    $2('loginView').classList.add('hid');
    $2('mainView').classList.remove('hid');
    // P1-4: 隐藏浮动语言切换器（登录后用侧边栏切换器）
    var ls = document.querySelector('.lang-switch');
    if (ls) ls.style.display = 'none';
    // 首次登录引导（默认密码）弹窗——仅在 pwd_initial 时弹出
    if (pwdInitial) openPwdModal();
  }

  // ====== 改密 ======
  var pwdModalMode = 'first';
  function openPwdModal(mode) {
    pwdModalMode = mode || 'first';
    $2('pwdModal').classList.remove('hid');
    $2('oldPass').value = ''; $2('newPass').value = ''; $2('confirmPass').value = '';
  }
  function closePwdModal() {
    $2('pwdModal').classList.add('hid');
  }
  function changePwd() {
    var oldP = $2('oldPass').value, newP = $2('newPass').value, confirmP = $2('confirmPass').value;
    if (!oldP || !newP) { toast(I18N.t('admin.pwd_modal.empty_fields'), false); return; }
    if (newP.length < 8) { toast(I18N.t('admin.pwd_modal.too_short'), false); return; }
    if (newP !== confirmP) { toast(I18N.t('admin.pwd_modal.mismatch'), false); return; }
    api('/change-password', { method: 'POST', body: { old_password: oldP, new_password: newP } }).then(function () {
      toast(I18N.t('admin.pwd_modal.changed'), true);
      setToken(null);
      $2('pwdModal').classList.add('hid');
      $2('mainView').classList.add('hid');
      $2('loginView').classList.remove('hid');
      // P1-4: 恢复浮动语言切换器
      var ls = document.querySelector('.lang-switch');
      if (ls) ls.style.display = '';
      $2('loginPass').value = ''; $2('loginErr').textContent = '';
    }).catch(function (e) { toast(errMsg(e), false); });
  }

  // ====== 登出 ======
  function logout() {
    api('/logout', { method: 'POST' }).then(function () { setToken(null); location.reload(); })
      .catch(function () { setToken(null); location.reload(); });
  }

  // ====== 配置中心 ======
  function showCfg() {
    // 导航高亮——与 showUsers/showWhitelist 一致
    var navItems = document.querySelectorAll('.sb-nav a');
    navItems.forEach(function(a){a.classList.remove('act')});
    var cfgNav = document.querySelector('.sb-nav a[onclick*="showCfg"]');
    if (cfgNav) cfgNav.classList.add('act');
    // 隐藏用户管理和白名单区域
    document.getElementById('usersView').style.display = 'none';
    document.getElementById('whitelistView').style.display = 'none';
    _usersViewVisible = false;
    _whitelistViewVisible = false;
    _cfgViewVisible = true;
    // 显示配置区域
    document.querySelector('.mn > div:first-child').style.display = '';
    var cfgCt = document.querySelector('.mn .ct');
    if (cfgCt) cfgCt.style.display = '';
    loadTokenHealth();
    api('/config').then(function (d) {
      _lastConfigData = d;
      _renderCfg(d);
    }).catch(function (e) { toast(I18N.t('admin.config.load_failed') + ': ' + errMsg(e), false); });
  }

  function _renderCfg(d) {
      var c = d.config || {};
      var os = c.oauth_status || {};
      applyToField('f_gitcode_api_token', c.gitcode_api_token_present, c.gitcode_api_token_tail);
      applyToField('f_github_bot_token', c.github_bot_token_present, c.github_bot_token_tail);
      // github_bot_username 非敏感，回显明文
      $2('f_github_bot_username').value = c.github_bot_username || '';
      $2('f_github_bot_username').placeholder = c.github_bot_username_present ? I18N.t('admin.config.configured') : 'GitHub bot username';
      // client_id / redirect_uri 非敏感，回显明文；secret 占位提示
      $2('f_gitcode_oauth_client_id').value = c.gitcode_oauth_client_id || '';
      $2('f_gitcode_oauth_client_id').placeholder = c.gitcode_oauth_client_id_present ? I18N.t('admin.config.configured') : 'GitCode OAuth App Client ID';
      $2('f_github_oauth_client_id').value = c.github_oauth_client_id || '';
      $2('f_github_oauth_client_id').placeholder = c.github_oauth_client_id_present ? I18N.t('admin.config.configured') : 'GitHub OAuth App Client ID';
      $2('f_gitcode_oauth_client_secret').value = '';
      $2('f_gitcode_oauth_client_secret').placeholder = os.gitcode ? I18N.t('admin.config.configured_update') : 'GitCode OAuth App Client Secret';
      $2('f_github_oauth_client_secret').value = '';
      $2('f_github_oauth_client_secret').placeholder = os.github ? I18N.t('admin.config.configured_update') : 'GitHub OAuth App Client Secret';
      $2('f_gitcode_oauth_redirect_uri').value = c.gitcode_oauth_redirect_uri || '';
      $2('f_gitcode_oauth_redirect_uri').placeholder = I18N.t('admin.config.auto_infer');
      $2('f_github_oauth_redirect_uri').value = c.github_oauth_redirect_uri || '';
      $2('f_github_oauth_redirect_uri').placeholder = I18N.t('admin.config.auto_infer');
      $2('f_bot_usernames').value = (c.bot_usernames || []).join(', ');
      // webhook_url non-sensitive, show plaintext; gitcode_webhook_token sensitive, masked
      $2('f_webhook_url').value = c.webhook_url || '';
      $2('f_webhook_url').placeholder = c.webhook_url_present ? I18N.t('admin.config.configured') : 'GitCode webhook callback URL';
      applyToField('f_gitcode_webhook_token', c.gitcode_webhook_token_present, c.gitcode_webhook_token_tail);
  }

  function applyToField(fieldId, present, tail) {
    var el = $2(fieldId);
    if (present) { el.value = ''; el.placeholder = I18N.t('admin.config.configured_tail', {tail: tail}); }
    else { el.value = ''; el.placeholder = I18N.t('admin.config.not_configured'); }
  }

  function saveConfig() {
    $2('saveBtn').disabled = true;
    var body = {};
    var gitcodeTok = $2('f_gitcode_api_token').value.trim();
    var githubTok = $2('f_github_bot_token').value.trim();
    if (gitcodeTok) body.gitcode_api_token = gitcodeTok;
    if (githubTok) body.github_bot_token = githubTok;
    var githubBotUser = $2('f_github_bot_username').value.trim();
    if (githubBotUser) body.github_bot_username = githubBotUser;
    var gcdClientId = $2('f_gitcode_oauth_client_id').value.trim();
    if (gcdClientId && gcdClientId.indexOf('●') < 0) body.gitcode_oauth_client_id = gcdClientId;
    var gcdClientSec = $2('f_gitcode_oauth_client_secret').value.trim();
    if (gcdClientSec) body.gitcode_oauth_client_secret = gcdClientSec;
    var gcdRedirect = $2('f_gitcode_oauth_redirect_uri').value.trim();
    if (gcdRedirect) body.gitcode_oauth_redirect_uri = gcdRedirect;
    var ghClientId = $2('f_github_oauth_client_id').value.trim();
    if (ghClientId && ghClientId.indexOf('●') < 0) body.github_oauth_client_id = ghClientId;
    var ghClientSec = $2('f_github_oauth_client_secret').value.trim();
    if (ghClientSec) body.github_oauth_client_secret = ghClientSec;
    var ghRedirect = $2('f_github_oauth_redirect_uri').value.trim();
    if (ghRedirect) body.github_oauth_redirect_uri = ghRedirect;
    var us = $2('f_bot_usernames').value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
    if (us.length) body.bot_usernames = us;
    var webhookUrl = $2('f_webhook_url').value.trim();
    if (webhookUrl) body.webhook_url = webhookUrl;
    var webhookToken = $2('f_gitcode_webhook_token').value.trim();
    if (webhookToken) body.gitcode_webhook_token = webhookToken;
    api('/config', { method: 'PUT', body: body }).then(function (d) {
      toast(I18N.t('admin.config.save_success'), true);
      showCfg();
    }).catch(function (e) { toast(I18N.t('admin.config.save_failed') + ': ' + errMsg(e), false); })
      .finally(function () { $2('saveBtn').disabled = false; });
  }

  function verifyToken(provider, fieldId) {
    var tok = $2(fieldId).value.trim();
    if (!tok) { toast(I18N.t('admin.config.enter_token_first'), false); return; }
    api('/config/verify-token', { method: 'POST', body: { provider: provider, token: tok } }).then(function (d) {
      var st = $2('s_' + fieldId.replace('f_', ''));
      if (d.ok) { st.textContent = '✔ ' + (d.login || d.name); toast(I18N.t('admin.config.token_valid', {login: d.login}), true); }
      else { st.textContent = I18N.t('admin.config.token_invalid_status'); toast(I18N.t('admin.config.token_invalid'), false); }
    }).catch(function (e) { toast(I18N.t('admin.config.verify_failed') + ': ' + errMsg(e), false); });
  }

  // ====== Bot Token Health (Issue #348) ======
  function tokenHealthLevel(d) {
    d = d || {};
    if (d.overall === 'unhealthy') return 'unhealthy';
    if (d.overall === 'degraded') return 'degraded';
    return 'healthy';
  }

  var TOKEN_HEALTH_META = {
    healthy:   { cls: 'tag-s', key: 'admin.token_health.healthy' },
    degraded:  { cls: 'tag-w', key: 'admin.token_health.degraded' },
    unhealthy: { cls: 'tag-d', key: 'admin.token_health.unhealthy' }
  };
  var ERROR_TYPE_CLS = { auth: 'tag-d', network: 'tag-w', config: 'tag-w' };

  function esc(s) { var d = document.createElement('div'); d.textContent = s || ''; var h = d.innerHTML; return h.replace(/"/g, '&quot;'); }

  function renderTokenHealth(d) {
    _lastHealthData = d;
    _lastHealthError = null;
    d = d || {};
    var level = tokenHealthLevel(d);
    var meta = TOKEN_HEALTH_META[level];
    var stEl = $2('tkh_status');
    if (stEl) stEl.innerHTML = '<span class="' + meta.cls + '">● ' + I18N.t(meta.key) + '</span>';
    var catEl = $2('tkh_checked_at');
    if (catEl) catEl.textContent = d.checked_at || I18N.t('admin.token_health.not_probed');

    var platforms = d.platforms || {};
    var keys = Object.keys(platforms);
    var html = '';
    for (var i = 0; i < keys.length; i++) {
      var prov = keys[i];
      var p = platforms[prov];
      if (!p) continue;
      var pLevel = p.unhealthy ? 'unhealthy' : (p.degraded ? 'degraded' : 'healthy');
      var pMeta = TOKEN_HEALTH_META[pLevel];
      var et = (p.error_type || '').toLowerCase();
      var etHtml = et ? '<span class="' + (ERROR_TYPE_CLS[et] || 'tag-w') + '">' + esc(et) + '</span>' : '—';
      var errDisplay = (p.consecutive_failures > 0) ? (esc(p.error) || '—') : '—';
      var label = I18N.t('admin.token_health.' + prov) || prov;
      html += '<div class="card" style="margin-top:8px;padding:14px 18px">'
        + '<div class="card-h" style="margin-bottom:8px"><h2 style="font-size:13px">' + esc(label) + '</h2></div>'
        + '<div class="f-row"><label>' + I18N.t('admin.token_health.status') + '</label><span><span class="' + pMeta.cls + '">● ' + I18N.t(pMeta.key) + '</span></span></div>'
        + '<div class="f-row"><label>' + I18N.t('admin.token_health.error_type') + '</label><span>' + etHtml + '</span></div>'
        + '<div class="f-row"><label>' + I18N.t('admin.token_health.error_detail') + '</label><span style="font-size:13px;color:var(--t2,#5b667b)">' + errDisplay + '</span></div>'
        + '<div class="f-row"><label>' + I18N.t('admin.token_health.failures') + '</label><span style="font-size:13px">' + String(p.consecutive_failures == null ? 0 : p.consecutive_failures) + '</span></div>'
        + '</div>';
    }
    var pEl = $2('tkh_platforms');
    if (pEl) pEl.innerHTML = html || '<div class="hint" style="margin-left:0">' + I18N.t('admin.token_health.no_platforms') + '</div>';
  }

  function renderTokenHealthError(e) {
    _lastHealthError = e;
    _lastHealthData = null;
    var stEl = $2('tkh_status');
    var unavailable = !!(e && (e.status === 404 || e.status === 405));
    if (stEl) stEl.innerHTML = '<span class="tag-w">' + (unavailable ? I18N.t('admin.token_health.unavailable') : I18N.t('admin.token_health.load_failed')) + '</span>';
    ['tkh_checked_at', 'tkh_platforms'].forEach(function (id) {
      var el = $2(id);
      if (el) el.textContent = '—';
    });
  }

  function loadTokenHealth() {
    ['tkh_status', 'tkh_checked_at'].forEach(function (id) {
      var el = $2(id);
      if (el) el.textContent = '…';
    });
    var pEl = $2('tkh_platforms');
    if (pEl) pEl.innerHTML = '';
    api('/bot-token-health').then(renderTokenHealth).catch(function(e) {
      renderTokenHealthError(e);
      if (!(e && e.status === 401)) toast(I18N.t('admin.token_health.load_health_failed') + ': ' + errMsg(e), false);
    });
  }

  // ====== 初始化 ======
  function init() {
    $2('loginBtn').onclick = login;
    document.getElementById('loginPass').addEventListener('keydown', function (e) { if (e.key === 'Enter') login(); });
    // Language switcher: sync initial state
    _updateAdminLangUI();
    // 已登录则直接进主界面
    if (getToken()) { showApp(false); showCfg(); }
  }

  // ====== Language switcher ======
  function _toggleAdminLang() {
    var newLang = I18N.getLang() === 'zh' ? 'en' : 'zh';
    I18N.setLang(newLang).then(function () { _updateAdminLangUI(); });
  }

  function _setAdminLang(lang) {
    if (lang === I18N.getLang()) return;
    I18N.setLang(lang).then(function () { _updateAdminLangUI(); });
  }

  function _updateAdminLangUI() {
    var lang = I18N.getLang();
    // Login page floating label
    var label = document.getElementById('adminLangLabel');
    if (label) label.textContent = lang === 'zh' ? 'EN' : '中';
    // Sidebar toggle active state
    var zhBtn = document.getElementById('adminLangZh');
    var enBtn = document.getElementById('adminLangEn');
    if (zhBtn) zhBtn.classList.toggle('active', lang === 'zh');
    if (enBtn) enBtn.classList.toggle('active', lang === 'en');
    // 重渲染动态 i18n 文本（不发 API 请求，用缓存数据）
    if (_cfgViewVisible && _lastConfigData) _renderCfg(_lastConfigData);
    if (_lastHealthData) renderTokenHealth(_lastHealthData);
    else if (_lastHealthError) renderTokenHealthError(_lastHealthError);
    if (_usersViewVisible && _currentPageUsers.length >= 0) _rerenderUsers();
    if (_whitelistViewVisible) {
      if (_lastSignupWhitelist) _rerenderSignupWhitelist();
      if (_lastLoginWhitelist) _rerenderLoginWhitelist();
    }
  }

  // 语言切换重渲染函数（纯 DOM 操作，无 API 请求）
  function _rerenderUsers() {
    var tbody = document.getElementById('usersTbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    var emptyEl = document.getElementById('userEmpty');
    var statusMap = {0:I18N.t('admin.users.status_disabled'),1:I18N.t('admin.users.status_active'),2:I18N.t('admin.users.status_deleted'),3:I18N.t('admin.users.status_cooldown')};
    var statusDotCls = {0:'s-off',1:'s-on',2:'s-del',3:'s-cool'};
    if (_currentPageUsers.length === 0) {
      if (emptyEl) emptyEl.style.display = '';
      var tbl = document.getElementById('usersTable');
      if (tbl) tbl.style.display = 'none';
    } else {
      if (emptyEl) emptyEl.style.display = 'none';
      var tbl2 = document.getElementById('usersTable');
      if (tbl2) tbl2.style.display = '';
    }
    _currentPageUsers.forEach(function(u) {
      var tr = document.createElement('tr');
      tr.onclick = function(e) { if (!e.target.closest('.row-actions')) editUser(u.id); };
      var menuItems = '';
      if (u.status !== 2) {
        menuItems += '<button onclick="editUser(' + u.id + ')">' + I18N.t('admin.users.edit') + '</button>';
        menuItems += '<button onclick="resetUserPassword(' + u.id + ')">' + I18N.t('admin.users.reset_password') + '</button>';
      }
      if (u.status === 1) {
        menuItems += '<button class="danger" onclick="disableUser(' + u.id + ')">' + I18N.t('admin.users.disable') + '</button>';
      } else if (u.status === 0 || u.status === 3) {
        menuItems += '<button onclick="restoreUser(' + u.id + ')">' + I18N.t('admin.users.restore') + '</button>';
      }
      var nicknameSub = u.nickname ? '<div class="user-sub">' + esc(u.nickname) + '</div>' : '';
      var dotCls = statusDotCls[u.status] || 's-del';
      tr.innerHTML =
        '<td style="padding:10px 12px"><div class="user-name">' + esc(u.username) + '</div>' + nicknameSub + '</td>' +
        '<td style="padding:10px 12px;color:var(--t2,#5b667b)">' + esc(u.email || '—') + '</td>' +
        '<td style="padding:10px 12px"><span class="status-dot ' + dotCls + '"></span>' + (statusMap[u.status]||u.status) + '</td>' +
        '<td style="padding:10px 12px;color:var(--t2,#5b667b)">' + (u.last_login_at || '—') + '</td>' +
        '<td style="padding:10px 12px" class="row-actions"><button class="row-actions-btn" onclick="event.stopPropagation();toggleRowMenu(' + u.id + ')">⋯</button>' +
        '<div class="row-menu" data-uid="' + u.id + '">' + menuItems + '</div></td>';
      tbody.appendChild(tr);
    });
    var countEl = document.getElementById('userCount');
    if (countEl) countEl.textContent = I18N.t('admin.users.total', {count: _currentPageTotal});
    var pager = document.getElementById('userPager');
    if (pager) {
      var totalPages = Math.ceil(_currentPageTotal / _PER_PAGE);
      var start = _currentPageTotal === 0 ? 0 : (_userPage - 1) * _PER_PAGE + 1;
      var end = Math.min(_userPage * _PER_PAGE, _currentPageTotal);
      pager.innerHTML = '';
      var info = document.createElement('div');
      info.className = 'pager-info';
      info.textContent = start + '-' + end + ' / ' + _currentPageTotal;
      pager.appendChild(info);
      var btns = document.createElement('div');
      btns.className = 'pager-btns';
      var prev = document.createElement('button');
      prev.className = 'pager-btn';
      prev.textContent = '‹';
      prev.disabled = _userPage <= 1;
      prev.onclick = function() { showUsers(_userPage - 1); };
      btns.appendChild(prev);
      var pageLabel = document.createElement('span');
      pageLabel.style.cssText = 'font-size:12px;color:var(--t2,#5b667b);padding:0 4px';
      pageLabel.textContent = _userPage + ' / ' + totalPages;
      btns.appendChild(pageLabel);
      var next = document.createElement('button');
      next.className = 'pager-btn';
      next.textContent = '›';
      next.disabled = _userPage >= totalPages;
      next.onclick = function() { showUsers(_userPage + 1); };
      btns.appendChild(next);
      pager.appendChild(btns);
    }
  }

  function _rerenderSignupWhitelist() {
    var tbody = document.getElementById('whitelistTbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    (_lastSignupWhitelist || []).forEach(function(w) {
      var tr = document.createElement('tr');
      tr.style.borderBottom = '1px solid var(--b,#e3e6ee)';
      tr.innerHTML =
        '<td style="padding:8px">' + w.id + '</td>' +
        '<td style="padding:8px">' + (w.type === 'email' ? I18N.t('admin.whitelist.type_email') : I18N.t('admin.whitelist.type_domain')) + '</td>' +
        '<td style="padding:8px">' + esc(w.value) + '</td>' +
        '<td style="padding:8px">' + esc(w.remark || '-') + '</td>' +
        '<td style="padding:8px;color:var(--t2)">' + (w.created_at || '-') + '</td>' +
        '<td style="padding:8px"><button class="btn" style="padding:4px 10px;font-size:12px;color:var(--r,#dc2828)" onclick="deleteWhitelist(' + w.id + ')">' + I18N.t('admin.whitelist.delete') + '</button></td>';
      tbody.appendChild(tr);
    });
  }

  function _rerenderLoginWhitelist() {
    var sel = document.getElementById('loginWlToggle');
    if (sel) sel.value = _lastLoginWhitelistEnabled ? '1' : '0';
    var tbody = document.getElementById('loginWhitelistTbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    (_lastLoginWhitelist || []).forEach(function(w) {
      var tr = document.createElement('tr');
      tr.style.borderBottom = '1px solid var(--b,#e3e6ee)';
      tr.innerHTML =
        '<td style="padding:8px">' + w.id + '</td>' +
        '<td style="padding:8px">' + esc(w.gitcode_username || '-') + '</td>' +
        '<td style="padding:8px">' + esc(w.github_username || '-') + '</td>' +
        '<td style="padding:8px">' + esc(w.remark || '-') + '</td>' +
        '<td style="padding:8px;color:var(--t2)">' + (w.created_at || '-') + '</td>' +
        '<td style="padding:8px"><button class="btn" style="padding:4px 10px;font-size:12px;color:var(--r,#dc2828)" onclick="deleteLoginWhitelist(' + w.id + ')">' + I18N.t('admin.login_whitelist.delete') + '</button></td>';
      tbody.appendChild(tr);
    });
  }

  window._toggleAdminLang = _toggleAdminLang;
  window._setAdminLang = _setAdminLang;

  window.AdminApp = { login: login, changePwd: changePwd, closePwdModal: closePwdModal, openPwdModal: openPwdModal,
                      logout: logout, showCfg: showCfg, saveConfig: saveConfig, verifyToken: verifyToken,
                      loadTokenHealth: loadTokenHealth };
  // 暴露到全局——admin.html 内联 onclick 引用裸函数名
  window.login = login;
  window.changePwd = changePwd;
  window.closePwdModal = closePwdModal;
  window.openPwdModal = openPwdModal;
  window.logout = logout;
  window.showCfg = showCfg;
  window.saveConfig = saveConfig;
  window.verifyToken = verifyToken;
  window.loadTokenHealth = loadTokenHealth;
  document.addEventListener('DOMContentLoaded', init);
  // ===== D6: 用户管理 =====
  var _userPage = 1;
  var _currentPageUsers = [];
  var _currentPageTotal = 0;  // 缓存总数用于语言切换重渲染
  var _searchTimer = null;
  var _PER_PAGE = 25;

  // 语言切换重渲染缓存
  var _lastHealthData = null;
  var _lastHealthError = null;
  var _lastSignupWhitelist = null;
  var _lastLoginWhitelist = null;
  var _lastLoginWhitelistEnabled = false;
  var _usersViewVisible = false;
  var _whitelistViewVisible = false;
  var _cfgViewVisible = false;    // config page visible
  var _lastConfigData = null;     // cached /config response for lang-switch re-render

  function searchUsersDebounced(ev) {
    if (_searchTimer) clearTimeout(_searchTimer);
    _searchTimer = setTimeout(function() { showUsers(1, ev); }, 300);
  }

  function toggleRowMenu(uid) {
    document.querySelectorAll('.row-menu').forEach(function(m) {
      if (m.getAttribute('data-uid') != uid) m.classList.remove('show');
    });
    var menu = document.querySelector('.row-menu[data-uid="' + uid + '"]');
    if (menu) menu.classList.toggle('show');
  }

  // Close menus on outside click
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.row-actions')) {
      document.querySelectorAll('.row-menu.show').forEach(function(m) { m.classList.remove('show'); });
    }
  });

  function showUsers(page, ev) {
    _userPage = page || 1;
    var navItems = document.querySelectorAll('.sb-nav a');
    navItems.forEach(function(a){a.classList.remove('act')});
    // P1-5: ev 可能来自搜索 input 而非 nav a 元素——找最近的 a 高亮
    if (ev && ev.target) {
      var navA = ev.target.closest('a');
      if (navA) navA.classList.add('act');
      else {
        // 搜索触发：高亮用户列表 nav 项
        var userNav = document.querySelector('.sb-nav a[onclick*="showUsers"]');
        if (userNav) userNav.classList.add('act');
      }
    }
    document.querySelector('.mn > div:first-child').style.display = 'none';
    var cfgCt = document.querySelector('.mn .ct');
    if (cfgCt) cfgCt.style.display = 'none';
    document.getElementById('usersView').style.display = '';
    document.getElementById('whitelistView').style.display = 'none';
    _cfgViewVisible = false;
    _whitelistViewVisible = false;
    _usersViewVisible = true;

    var q = (document.getElementById('userSearch') || {}).value || '';
    api('/users?page=' + _userPage + '&per_page=' + _PER_PAGE + '&q=' + encodeURIComponent(q)).then(function(d) {
      _currentPageUsers = d.users || [];
      _currentPageTotal = d.total;
      _usersViewVisible = true;
      var tbody = document.getElementById('usersTbody');
      tbody.innerHTML = '';
      var emptyEl = document.getElementById('userEmpty');

      var statusMap = {0:I18N.t('admin.users.status_disabled'),1:I18N.t('admin.users.status_active'),2:I18N.t('admin.users.status_deleted'),3:I18N.t('admin.users.status_cooldown')};
      var statusDotCls = {0:'s-off',1:'s-on',2:'s-del',3:'s-cool'};

      if (_currentPageUsers.length === 0) {
        emptyEl.style.display = '';
        document.getElementById('usersTable').style.display = 'none';
      } else {
        emptyEl.style.display = 'none';
        document.getElementById('usersTable').style.display = '';
      }

      _currentPageUsers.forEach(function(u) {
        var tr = document.createElement('tr');
        // Row click → edit
        tr.onclick = function(e) { if (!e.target.closest('.row-actions')) editUser(u.id); };

        // Build actions menu
        var menuItems = '';
        if (u.status !== 2) {
          menuItems += '<button onclick="editUser(' + u.id + ')">' + I18N.t('admin.users.edit') + '</button>';
          menuItems += '<button onclick="resetUserPassword(' + u.id + ')">' + I18N.t('admin.users.reset_password') + '</button>';
        }
        if (u.status === 1) {
          menuItems += '<button class="danger" onclick="disableUser(' + u.id + ')">' + I18N.t('admin.users.disable') + '</button>';
        } else if (u.status === 0 || u.status === 3) {
          menuItems += '<button onclick="restoreUser(' + u.id + ')">' + I18N.t('admin.users.restore') + '</button>';
        }

        var nicknameSub = u.nickname ? '<div class="user-sub">' + esc(u.nickname) + '</div>' : '';
        var dotCls = statusDotCls[u.status] || 's-del';

        tr.innerHTML =
          '<td style="padding:10px 12px"><div class="user-name">' + esc(u.username) + '</div>' + nicknameSub + '</td>' +
          '<td style="padding:10px 12px;color:var(--t2,#5b667b)">' + esc(u.email || '—') + '</td>' +
          '<td style="padding:10px 12px"><span class="status-dot ' + dotCls + '"></span>' + (statusMap[u.status]||u.status) + '</td>' +
          '<td style="padding:10px 12px;color:var(--t2,#5b667b)">' + (u.last_login_at || '—') + '</td>' +
          '<td style="padding:10px 12px" class="row-actions"><button class="row-actions-btn" onclick="event.stopPropagation();toggleRowMenu(' + u.id + ')">⋯</button>' +
          '<div class="row-menu" data-uid="' + u.id + '">' + menuItems + '</div></td>';
        tbody.appendChild(tr);
      });

      document.getElementById('userCount').textContent = I18N.t('admin.users.total', {count: _currentPageTotal});

      // Pager: "Showing X-Y of Z" + prev/next
      var pager = document.getElementById('userPager');
      var totalPages = Math.ceil(_currentPageTotal / _PER_PAGE);
      var start = _currentPageTotal === 0 ? 0 : (_userPage - 1) * _PER_PAGE + 1;
      var end = Math.min(_userPage * _PER_PAGE, _currentPageTotal);
      pager.innerHTML = '';
      var info = document.createElement('div');
      info.className = 'pager-info';
      info.textContent = start + '-' + end + ' / ' + _currentPageTotal;
      pager.appendChild(info);
      var btns = document.createElement('div');
      btns.className = 'pager-btns';
      var prev = document.createElement('button');
      prev.className = 'pager-btn';
      prev.textContent = '‹';
      prev.disabled = _userPage <= 1;
      prev.onclick = function() { showUsers(_userPage - 1); };
      btns.appendChild(prev);
      var pageLabel = document.createElement('span');
      pageLabel.style.cssText = 'font-size:12px;color:var(--t2,#5b667b);padding:0 4px';
      pageLabel.textContent = _userPage + ' / ' + totalPages;
      btns.appendChild(pageLabel);
      var next = document.createElement('button');
      next.className = 'pager-btn';
      next.textContent = '›';
      next.disabled = _userPage >= totalPages;
      next.onclick = function() { showUsers(_userPage + 1); };
      btns.appendChild(next);
      pager.appendChild(btns);
    }).catch(function(e) { toast(I18N.t('admin.users.load_failed') + ': ' + errMsg(e), false); });
  }

  function disableUser(uid) {
    if (!confirm(I18N.t('admin.users.disable_confirm'))) return;
    api('/users/' + uid + '/disable', {method: 'POST', body: {reason: 'admin_disabled'}}).then(function() {
      toast(I18N.t('admin.users.disabled'), true);
      showUsers(_userPage);
    }).catch(function(e) { toast(I18N.t('admin.users.disable_failed') + ': ' + errMsg(e), false); });
  }

  function restoreUser(uid) {
    api('/users/' + uid + '/restore', {method: 'POST'}).then(function() {
      toast(I18N.t('admin.users.restored'), true);
      showUsers(_userPage);
    }).catch(function(e) { toast(I18N.t('admin.users.restore_failed') + ': ' + errMsg(e), false); });
  }

  function showWhitelist(ev) {
    var navItems = document.querySelectorAll('.sb-nav a');
    navItems.forEach(function(a){a.classList.remove('act')});
    if (ev && ev.target) {
      var navA = ev.target.closest('a');
      if (navA) navA.classList.add('act');
    }
    document.querySelector('.mn > div:first-child').style.display = 'none';
    var cfgCt = document.querySelector('.mn .ct');
    if (cfgCt) cfgCt.style.display = 'none';
    document.getElementById('usersView').style.display = 'none';
    document.getElementById('whitelistView').style.display = '';
    _cfgViewVisible = false;
    _usersViewVisible = false;
    _whitelistViewVisible = true;
    loadWhitelist();
    loadAllowSignup();
    loadLoginWhitelist();
  }

  function loadWhitelist() {
    api('/signup-whitelist').then(function(d) {
      _lastSignupWhitelist = d.whitelist || [];
      _whitelistViewVisible = true;
      var tbody = document.getElementById('whitelistTbody');
      tbody.innerHTML = '';
      (d.whitelist || []).forEach(function(w) {
        var tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--b,#e3e6ee)';
        tr.innerHTML =
          '<td style="padding:8px">' + w.id + '</td>' +
          '<td style="padding:8px">' + (w.type === 'email' ? I18N.t('admin.whitelist.type_email') : I18N.t('admin.whitelist.type_domain')) + '</td>' +
          '<td style="padding:8px">' + esc(w.value) + '</td>' +
          '<td style="padding:8px">' + esc(w.remark || '-') + '</td>' +
          '<td style="padding:8px;color:var(--t2)">' + (w.created_at || '-') + '</td>' +
          '<td style="padding:8px"><button class="btn" style="padding:4px 10px;font-size:12px;color:var(--r,#dc2828)" onclick="deleteWhitelist(' + w.id + ')">' + I18N.t('admin.whitelist.delete') + '</button></td>';
        tbody.appendChild(tr);
      });
    }).catch(function(e) { toast(I18N.t('admin.whitelist.load_failed') + ': ' + errMsg(e), false); });
  }

  function addWhitelist() {
    var type = document.getElementById('wl_type').value;
    var value = document.getElementById('wl_value').value.trim();
    var remark = document.getElementById('wl_remark').value.trim();
    if (!value) { toast(I18N.t('admin.whitelist.enter_value'), false); return; }
    api('/signup-whitelist', {method: 'POST', body: {type: type, value: value, remark: remark}}).then(function() {
      toast(I18N.t('admin.whitelist.added'), true);
      document.getElementById('wl_value').value = '';
      document.getElementById('wl_remark').value = '';
      loadWhitelist();
    }).catch(function(e) { toast(I18N.t('admin.whitelist.add_failed') + ': ' + errMsg(e), false); });
  }

  function deleteWhitelist(id) {
    api('/signup-whitelist/' + id, {method: 'DELETE'}).then(function() {
      toast(I18N.t('admin.whitelist.deleted'), true);
      loadWhitelist();
    }).catch(function(e) { toast(I18N.t('admin.whitelist.delete_failed') + ': ' + errMsg(e), false); });
  }

  function loadAllowSignup() {
    api('/config').then(function(d) {
      var v = (d.config || {}).allow_signup || '0';
      document.getElementById('allow_signup').value = v === '1' ? '1' : '0';
    }).catch(function(){});
  }

  function saveAllowSignup() {
    var v = document.getElementById('allow_signup').value === '1';
    api('/allow-signup', {method: 'PUT', body: {enabled: v}}).then(function() {
      toast(I18N.t('admin.whitelist.updated'), true);
    }).catch(function(e) { toast(I18N.t('admin.whitelist.update_failed') + ': ' + errMsg(e), false); });
  }

  // ====== OAuth 登录白名单 (Issue #332) ======
  function loadLoginWhitelist() {
    // Single call: GET /whitelist returns {whitelist, enabled}
    api('/whitelist').then(function(d) {
      _lastLoginWhitelist = d.whitelist || [];
      _lastLoginWhitelistEnabled = !!(d && d.enabled);
      _whitelistViewVisible = true;
      var sel = document.getElementById('loginWlToggle');
      if (sel) sel.value = _lastLoginWhitelistEnabled ? '1' : '0';
      var tbody = document.getElementById('loginWhitelistTbody');
      if (!tbody) return;
      tbody.innerHTML = '';
      (_lastLoginWhitelist).forEach(function(w) {
        var tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--b,#e3e6ee)';
        var gitcodeUser = esc(w.gitcode_username || '-');
        var githubUser = esc(w.github_username || '-');
        tr.innerHTML =
          '<td style="padding:8px">' + w.id + '</td>' +
          '<td style="padding:8px">' + gitcodeUser + '</td>' +
          '<td style="padding:8px">' + githubUser + '</td>' +
          '<td style="padding:8px">' + esc(w.remark || '-') + '</td>' +
          '<td style="padding:8px;color:var(--t2)">' + (w.created_at || '-') + '</td>' +
          '<td style="padding:8px"><button class="btn" style="padding:4px 10px;font-size:12px;color:var(--r,#dc2828)" onclick="deleteLoginWhitelist(' + w.id + ')">' + I18N.t('admin.login_whitelist.delete') + '</button></td>';
        tbody.appendChild(tr);
      });
    }).catch(function(e) { toast(I18N.t('admin.login_whitelist.load_failed') + ': ' + errMsg(e), false); });
  }

  function addLoginWhitelist() {
    var provider = document.getElementById('lw_provider').value;
    var username = document.getElementById('lw_username').value.trim();
    var remark = document.getElementById('lw_remark').value.trim();
    if (!username) { toast(I18N.t('admin.login_whitelist.username_required'), false); return; }
    var body = { provider: provider, username: username, remark: remark };
    api('/whitelist', {method: 'POST', body: body}).then(function() {
      toast(I18N.t('admin.login_whitelist.added'), true);
      document.getElementById('lw_username').value = '';
      document.getElementById('lw_remark').value = '';
      loadLoginWhitelist();
    }).catch(function(e) { toast(I18N.t('admin.login_whitelist.add_failed') + ': ' + errMsg(e), false); });
  }

  function deleteLoginWhitelist(id) {
    api('/whitelist/' + id, {method: 'DELETE'}).then(function() {
      toast(I18N.t('admin.login_whitelist.deleted'), true);
      loadLoginWhitelist();
    }).catch(function(e) { toast(I18N.t('admin.login_whitelist.delete_failed') + ': ' + errMsg(e), false); });
  }

  function toggleLoginWhitelist() {
    var enabled = document.getElementById('loginWlToggle').value === '1';
    api('/whitelist/toggle', {method: 'PUT', body: {enabled: enabled}}).then(function() {
      toast(I18N.t('admin.login_whitelist.toggle_updated'), true);
    }).catch(function(e) { toast(I18N.t('admin.login_whitelist.toggle_failed') + ': ' + errMsg(e), false); });
  }

  // ====== 用户创建/编辑/重置密码 (Issue #332) ======
  function showUserModal(mode, user) {
    // Reuse existing modal infrastructure with a custom modal
    var existing = document.getElementById('userModal');
    if (existing) existing.remove();

    var isCreate = mode === 'create';
    var isReset = mode === 'reset';
    var title = isCreate ? I18N.t('admin.users.create_user') :
                isReset ? I18N.t('admin.users.reset_password') :
                I18N.t('admin.users.edit');
    var sub = isCreate ? '' : (isReset ? '' : I18N.t('admin.users.edit') + ': ' + (user ? user.username : ''));

    var fields = '';
    if (isCreate) {
      fields = '<input class="ipt" id="um_username" style="background:var(--bg,#f5f6f8);color:var(--t,#101724)" data-i18n-placeholder="admin.users.create_username" placeholder="Username">' +
        '<input class="ipt" id="um_email" style="background:var(--bg,#f5f6f8);color:var(--t,#101724)" data-i18n-placeholder="admin.users.create_email" placeholder="Email">' +
        '<input class="ipt" id="um_password" type="password" style="background:var(--bg,#f5f6f8);color:var(--t,#101724)" data-i18n-placeholder="admin.users.create_password" placeholder="Password">' +
        '<input class="ipt" id="um_nickname" style="background:var(--bg,#f5f6f8);color:var(--t,#101724)" data-i18n-placeholder="admin.users.create_nickname" placeholder="Nickname">';
    } else if (isReset) {
      fields = '<input class="ipt" id="um_password" type="password" style="background:var(--bg,#f5f6f8);color:var(--t,#101724)" data-i18n-placeholder="admin.users.reset_new_password" placeholder="New Password">';
    } else {
      // edit
      fields = '<input class="ipt" id="um_email" style="background:var(--bg,#f5f6f8);color:var(--t,#101724)" data-i18n-placeholder="admin.users.edit_email" placeholder="Email" value="' + esc(user ? user.email || '' : '') + '">' +
        '<input class="ipt" id="um_nickname" style="background:var(--bg,#f5f6f8);color:var(--t,#101724)" data-i18n-placeholder="admin.users.edit_nickname" placeholder="Nickname" value="' + esc(user ? user.nickname || '' : '') + '">';
    }

    var btnText = isCreate ? I18N.t('admin.users.create_btn') :
                  isReset ? I18N.t('admin.users.reset_btn') :
                  I18N.t('admin.users.edit_btn');

    var modal = document.createElement('div');
    modal.className = 'modal';
    modal.id = 'userModal';
    modal.innerHTML = '<div class="mbox">' +
      '<h3>' + esc(title) + '</h3>' +
      (sub ? '<div class="msub">' + esc(sub) + '</div>' : '') +
      fields +
      '<div class="m-actions">' +
        '<button style="background:var(--bg,#f5f6f8);color:var(--t,#101724)" onclick="closeUserModal()">' + I18N.t('admin.pwd_modal.skip') + '</button>' +
        '<button style="background:var(--a,#0071e3);color:#fff;border-radius:980px" id="um_submit">' + esc(btnText) + '</button>' +
      '</div>' +
    '</div>';
    document.body.appendChild(modal);

    document.getElementById('um_submit').onclick = function() {
      if (isCreate) {
        submitCreateUser();
      } else if (isReset) {
        submitResetPassword(user ? user.id : 0);
      } else {
        submitEditUser(user ? user.id : 0);
      }
    };
  }

  function closeUserModal() {
    var modal = document.getElementById('userModal');
    if (modal) modal.remove();
  }

  function createUser() {
    showUserModal('create', null);
  }

  function submitCreateUser() {
    var username = document.getElementById('um_username').value.trim();
    var password = document.getElementById('um_password').value.trim();
    if (!username) { toast(I18N.t('admin.users.username_required'), false); return; }
    if (!password || password.length < 8) { toast(I18N.t('admin.users.password_required'), false); return; }
    var body = {
      username: username,
      password: password,
      email: document.getElementById('um_email').value.trim(),
      nickname: document.getElementById('um_nickname').value.trim()
    };
    api('/users', {method: 'POST', body: body}).then(function() {
      toast(I18N.t('admin.users.user_created'), true);
      closeUserModal();
      showUsers(1);
    }).catch(function(e) { toast(I18N.t('admin.users.user_create_failed') + ': ' + errMsg(e), false); });
  }

  function editUser(uid) {
    // Find user from cached current page data
    var user = _currentPageUsers.find(function(u) { return u.id === uid; });
    showUserModal('edit', user || {id: uid});
  }

  function submitEditUser(uid) {
    var body = {
      email: document.getElementById('um_email').value.trim(),
      nickname: document.getElementById('um_nickname').value.trim()
    };
    api('/users/' + uid, {method: 'PUT', body: body}).then(function() {
      toast(I18N.t('admin.users.user_updated'), true);
      closeUserModal();
      showUsers(_userPage);
    }).catch(function(e) { toast(I18N.t('admin.users.user_update_failed') + ': ' + errMsg(e), false); });
  }

  function resetUserPassword(uid) {
    showUserModal('reset', {id: uid});
  }

  function submitResetPassword(uid) {
    var password = document.getElementById('um_password').value.trim();
    if (!password || password.length < 8) { toast(I18N.t('admin.users.password_required'), false); return; }
    api('/users/' + uid + '/reset-password', {method: 'POST', body: {password: password}}).then(function() {
      toast(I18N.t('admin.users.password_reset'), true);
      closeUserModal();
    }).catch(function(e) { toast(I18N.t('admin.users.password_reset_failed') + ': ' + errMsg(e), false); });
  }

  window.showUsers = showUsers;
  window.showWhitelist = showWhitelist;
  window.disableUser = disableUser;
  window.restoreUser = restoreUser;
  window.addWhitelist = addWhitelist;
  window.deleteWhitelist = deleteWhitelist;
  window.saveAllowSignup = saveAllowSignup;
  window.loadLoginWhitelist = loadLoginWhitelist;
  window.addLoginWhitelist = addLoginWhitelist;
  window.deleteLoginWhitelist = deleteLoginWhitelist;
  window.toggleLoginWhitelist = toggleLoginWhitelist;
  window.createUser = createUser;
  window.editUser = editUser;
  window.resetUserPassword = resetUserPassword;
  window.closeUserModal = closeUserModal;
  window.toggleRowMenu = toggleRowMenu;

  window.AdminApp = window.AdminApp || {};
  Object.assign(window.AdminApp, {
    loadLoginWhitelist: loadLoginWhitelist,
    addLoginWhitelist: addLoginWhitelist,
    deleteLoginWhitelist: deleteLoginWhitelist,
    toggleLoginWhitelist: toggleLoginWhitelist,
    createUser: createUser,
    editUser: editUser,
    resetUserPassword: resetUserPassword,
    closeUserModal: closeUserModal,
    searchUsersDebounced: searchUsersDebounced,
    toggleRowMenu: toggleRowMenu
  });
})();
