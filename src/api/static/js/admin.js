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
    if (window.isPwdTooShort(newP)) { toast(I18N.t('admin.pwd_modal.too_short'), false); return; }
    // A 项修复（2026-10-28 深扫）：弱口令预检与用户侧四入口同源（后端 admin
    // /change-password 有 check_password_strength，前端预检消除"前端全绿后端 400"）
    if (window.isWeakPassword(newP)) { toast(I18N.t('admin.pwd_modal.too_common'), false); return; }
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
    _hideCardMgmtViews();
    _usersViewVisible = false;
    _whitelistViewVisible = false;
    _cfgViewVisible = true;
    // 显示配置区域
    document.querySelector('.mn > div:first-child').style.display = '';
    var cfgCt = document.querySelector('.mn .ct');
    if (cfgCt) cfgCt.style.display = '';
    // D18：token health 卡已迁仓库平台页（showVcsPlatforms 加载）
    api('/config').then(function (d) {
      _lastConfigData = d;
      _renderCfg(d);
    }).catch(function (e) { toast(I18N.t('admin.config.load_failed') + ': ' + errMsg(e), false); });
  }

  function _renderCfg(d) {
      var c = d.config || {};
      var os = c.oauth_status || {};
      // D13（2026-11-12 A 层收口）：GitCode/GitHub 双卡 6 字段回显删除——凭据与
      // oauth 配置由"仓库平台"/"OAuth 登录渠道"菜单接管（同 meta key）。
      // webhook token 归仓库平台详情（{provider}_webhook_token）。
      // D18：f_bot_usernames 回显迁 showVcsPlatforms（仓库平台页）
      // webhook_url 非敏感回显明文（D13：迁运行参数卡，部署级语义）
      $2('f_webhook_url').value = c.webhook_url || '';
      $2('f_webhook_url').placeholder = c.webhook_url_present ? I18N.t('admin.config.configured') : 'https://your-host/webhook/<provider>';
      // 运行参数 (Issue #470)：回显当前值 + 生效值标注（env 覆盖时管理台值不生效）
      $2('f_runtime_task_timeout').value = c.runtime_task_timeout || '';
      var _eff_t = c.runtime_task_timeout_effective;
      var _st_t = $2('s_runtime_task_timeout');
      if (_st_t) _st_t.textContent = (_eff_t != null && String(_eff_t) !== String(c.runtime_task_timeout))
        ? I18N.t('admin.config.effective_value', {v: _eff_t}) : '';
      $2('f_devstage_template_id').value = c.devstage_template_id || '';
      $2('f_devstage_flavor_id').value = c.devstage_flavor_id || '';
      $2('f_devstage_cores').value = c.devstage_cores || '';
      $2('f_devstage_memory').value = c.devstage_memory || '';
      _loadedDevstageArch = c.devstage_arch || '';
      $2('f_devstage_arch').value = _loadedDevstageArch || 'ARM';
  }

  function applyToField(fieldId, present, tail) {
    var el = $2(fieldId);
    if (present) { el.value = ''; el.placeholder = I18N.t('admin.config.configured_tail', {tail: tail}); }
    else { el.value = ''; el.placeholder = I18N.t('admin.config.not_configured'); }
  }

  // devstage_arch 仅变化时提交（QA Suggested）：select 恒有值，若每次保存都写入
  // meta 会隐式固化该层；记录加载时的现值，保存时无变化则不提交（保持现值）。
  var _loadedDevstageArch = '';

  function saveConfig() {
    $2('saveBtn').disabled = true;
    var body = {};
    // D18（2026-11-12）：本页收敛为运行参数——webhook_url（部署级）+ DevSpace 规格；
    // bot_usernames 兜底清单已按 D22 拍板退役（self-comment 名单纯卡派生）
    var webhookUrl = $2('f_webhook_url').value.trim();
    if (webhookUrl) body.webhook_url = webhookUrl;
    // 运行参数 (Issue #470)：留空不提交（保持现值）；数值字段转 int
    var rtTimeout = parseInt($2('f_runtime_task_timeout').value, 10);
    if (rtTimeout > 0) body.runtime_task_timeout = rtTimeout;
    var dsTpl = $2('f_devstage_template_id').value.trim();
    if (dsTpl) body.devstage_template_id = dsTpl;
    var dsFlavor = $2('f_devstage_flavor_id').value.trim();
    if (dsFlavor) body.devstage_flavor_id = dsFlavor;
    var dsCores = parseInt($2('f_devstage_cores').value, 10);
    if (dsCores > 0) body.devstage_cores = dsCores;
    var dsMem = parseInt($2('f_devstage_memory').value, 10);
    if (dsMem > 0) body.devstage_memory = dsMem;
    var dsArch = $2('f_devstage_arch').value;
    if (dsArch && dsArch !== _loadedDevstageArch) body.devstage_arch = dsArch;
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

  function esc(s) {
    // 2026-11-12 修复（实弹：admin 菜单 "window.escHtml is not a function"）：
    // admin.html 只加载 i18n/admin.js，不加载定义 window.escHtml 的 app.js/auth.js，
    // 全局代理必炸。兜底本地实现（口径同 auth.js Auth.escHtml：& < > " ' 全转义）。
    if (typeof window.escHtml === 'function') return window.escHtml(s);
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

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
        menuItems += '<button onclick="editUser(\'' + u.id + '\')">' + I18N.t('admin.users.edit') + '</button>';
        menuItems += '<button onclick="resetUserPassword(\'' + u.id + '\')">' + I18N.t('admin.users.reset_password') + '</button>';
      }
      if (u.status === 1) {
        menuItems += '<button class="danger" onclick="disableUser(\'' + u.id + '\')">' + I18N.t('admin.users.disable') + '</button>';
      } else if (u.status === 0 || u.status === 3) {
        menuItems += '<button onclick="restoreUser(\'' + u.id + '\')">' + I18N.t('admin.users.restore') + '</button>';
      }
      var nicknameSub = u.nickname ? '<div class="user-sub">' + esc(u.nickname) + '</div>' : '';
      var dotCls = statusDotCls[u.status] || 's-del';
      tr.innerHTML =
        '<td style="padding:10px 12px"><div class="user-name">' + esc(u.username) + '</div>' + nicknameSub + '</td>' +
        '<td style="padding:10px 12px;color:var(--t2,#5b667b)">' + esc(u.email || '—') + '</td>' +
        '<td style="padding:10px 12px"><span class="status-dot ' + dotCls + '"></span>' + (statusMap[u.status]||u.status) + '</td>' +
        '<td style="padding:10px 12px;color:var(--t2,#5b667b)">' + (u.last_login_at || '—') + '</td>' +
        '<td style="padding:10px 12px" class="row-actions"><button class="row-actions-btn" onclick="event.stopPropagation();toggleRowMenu(\'' + u.id + '\')">⋯</button>' +
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
    _fillLoginWlProviderSelect();
    _renderLoginWhitelistRows();
    // 2026-11-12 fail-closed：开关开启 + 表空 = 拒绝所有 OAuth 登录 → 红条警示
    var warn = document.getElementById('loginWlEmptyWarn');
    if (warn) {
      var emptyLock = _lastLoginWhitelistEnabled && ((_lastLoginWhitelist || []).length === 0);
      warn.style.display = emptyLock ? '' : 'none';
    }
  }


  // ════════════ D12 卡管理视图（OAuth 登录渠道 / 仓库平台，2026-11-12） ════════════
  // 后端积木：GET/PUT /admin/auth-channels、GET/PUT /admin/vcs-platforms。
  // 详情表单按卡 dict 键动态渲染——新渠道/新卡字段自动出现，零平台字面量。

  function _hideCardMgmtViews() {
    var a = document.getElementById('authChannelsView');
    var v = document.getElementById('vcsPlatformsView');
    if (a) a.style.display = 'none';
    if (v) v.style.display = 'none';
  }

  function _switchCardMgmtView(viewId, ev) {
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
    document.getElementById('whitelistView').style.display = 'none';
    _hideCardMgmtViews();
    document.getElementById(viewId).style.display = '';
  }

  var _lastAuthChannels = [];
  var _lastVcsPlatforms = {};
  var _editingAuthChannel = '';
  var _editingVcsPlatform = '';

  function showAuthChannels(ev) {
    _switchCardMgmtView('authChannelsView', ev);
    loadAuthChannels();
  }

  function showVcsPlatforms(ev) {
    _switchCardMgmtView('vcsPlatformsView', ev);
    loadVcsPlatforms();
    // D22（2026-11-12 拍板）：兜底清单卡退役——self-comment 名单纯卡派生；
    // D19：全局探活卡退役（状态由 loadVcsPlatforms 逐行渲染）
  }

  function loadAuthChannels() {
    api('/auth-channels').then(function(d) {
      _lastAuthChannels = d.providers || [];
      var tbody = document.getElementById('authChannelsTbody');
      if (!tbody) return;
      tbody.innerHTML = '';
      _lastAuthChannels.forEach(function(p, i) {
        var tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--b,#e3e6ee)';
        var st = p.configured
          ? '<span style="color:#1a7f37">● ' + I18N.t('admin.channels.st_configured') + '</span>'
          : '<span style="color:var(--r,#dc2828)">○ ' + I18N.t('admin.channels.st_unconfigured') + '</span>';
        tr.innerHTML =
          '<td style="padding:8px">' + (i + 1) + '</td>' +
          '<td style="padding:8px">' + esc(p.name || p.provider) + '</td>' +
          '<td style="padding:8px">' + esc(p.type || 'oauth2') + '</td>' +
          '<td style="padding:8px;color:var(--t2)">' + esc(p.auth_url || '-') + '</td>' +
          '<td style="padding:8px">' + st + '</td>' +
          '<td style="padding:8px"><button class="btn" style="padding:4px 10px;font-size:12px" onclick="viewAuthChannel(\'' + p.provider + '\')">' + I18N.t('admin.channels.detail') + '</button></td>';
        tbody.appendChild(tr);
      });
      document.getElementById('authChannelDetail').style.display = 'none';
      document.querySelector('#authChannelsView .ct').style.display = '';
    }).catch(function(e) { toast(I18N.t('admin.channels.load_failed') + ': ' + errMsg(e), false); });
  }

  function viewAuthChannel(provider) {
    var p = null;
    for (var i = 0; i < _lastAuthChannels.length; i++) {
      if (_lastAuthChannels[i].provider === provider) { p = _lastAuthChannels[i]; break; }
    }
    if (!p) return;
    _editingAuthChannel = provider;
    document.getElementById('authChannelDetailTitle').textContent = p.name + '（' + provider + '）';
    document.getElementById('ac_client_id').value = p.client_id || '';
    document.getElementById('ac_client_secret').value = '';
    document.getElementById('ac_client_secret').placeholder = p.client_secret_set ? I18N.t('admin.channels.secret_set') : '';
    // 卡字段动态渲染（布尔→checkbox；其余→text input）
    _renderCardFields('ac', p.card || {});
    _switchCardTab('ac', 0);
    document.querySelector('#authChannelsView .ct').style.display = 'none';
    document.getElementById('authChannelDetail').style.display = '';
  }

  function backAuthChannels() {
    document.getElementById('authChannelDetail').style.display = 'none';
    document.querySelector('#authChannelsView .ct').style.display = '';
  }

  function saveAuthChannel() {
    var card = _collectCardFields('ac');
    var body = { providers: {} };
    card['name'] = card['name'] || _editingAuthChannel;
    card['client_id'] = document.getElementById('ac_client_id').value.trim();
    var sec = document.getElementById('ac_client_secret').value;
    if (sec) card['client_secret'] = sec;
    body.providers[_editingAuthChannel] = card;
    api('/auth-channels', {method: 'PUT', body: body}).then(function() {
      toast(I18N.t('admin.channels.saved'), true);
      loadAuthChannels();
    }).catch(function(e) { toast(I18N.t('admin.channels.save_failed') + ': ' + errMsg(e), false); });
  }

  function loadVcsPlatforms() {
    api('/vcs-platforms').then(function(d) {
      _lastVcsPlatforms = d.cards || {};
      // D19：整体探活时间挂列表标题（每卡健康状态在行内）
      var ca = document.getElementById('vcsHealthCheckedAt');
      if (ca) ca.textContent = d.bot_token_health_checked_at || '';
      var tbody = document.getElementById('vcsPlatformsTbody');
      if (!tbody) return;
      tbody.innerHTML = '';
      Object.keys(_lastVcsPlatforms).forEach(function(provider, i) {
        var c = _lastVcsPlatforms[provider];
        var tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--b,#e3e6ee)';
        var bt = c.bot_token_configured
          ? '<span style="color:#1a7f37">●</span>'
          : '<span style="color:var(--r,#dc2828)">○</span>';
        // D19：探活状态逐行展示（healthy=绿● / degraded=橙● / unhealthy=红● /
        // 无数据=灰—；title 带错误摘要，整体 checked_at 由表头小字展示）
        var h = c.bot_token_health || {};
        var hs, ht = '';
        if (h.healthy) { hs = '<span style="color:#1a7f37">●</span>'; }
        else if (h.degraded) { hs = '<span style="color:#c77c1b">●</span>'; ht = (h.error || '') ; }
        else if (h.unhealthy) { hs = '<span style="color:var(--r,#dc2828)">●</span>'; ht = (h.error || ''); }
        else { hs = '<span style="color:var(--t3)">—</span>'; }
        if (ht) hs = '<span title="' + esc(ht) + '">' + hs + '</span>';
        tr.innerHTML =
          '<td style="padding:8px">' + (i + 1) + '</td>' +
          '<td style="padding:8px">' + esc(c.name || provider) + '</td>' +
          '<td style="padding:8px;color:var(--t2)">' + esc(c.host || '-') + '</td>' +
          '<td style="padding:8px">' + esc(c.dialect || '-') + '</td>' +
          '<td style="padding:8px">' + esc(c.bot_username || '-') + '</td>' +
          '<td style="padding:8px">' + bt + '</td>' +
          '<td style="padding:8px">' + hs + '</td>' +
          '<td style="padding:8px"><button class="btn" style="padding:4px 10px;font-size:12px" onclick="viewVcsPlatform(\'' + provider + '\')">' + I18N.t('admin.platforms.detail') + '</button></td>';
        tbody.appendChild(tr);
      });
      document.getElementById('vcsPlatformDetail').style.display = 'none';
      document.querySelector('#vcsPlatformsView .ct').style.display = '';
    }).catch(function(e) { toast(I18N.t('admin.platforms.load_failed') + ': ' + errMsg(e), false); });
  }

  function viewVcsPlatform(provider) {
    var c = _lastVcsPlatforms[provider];
    if (!c) return;
    _editingVcsPlatform = provider;
    document.getElementById('vcsPlatformDetailTitle').textContent = (c.name || provider) + '（' + provider + '）';
    var bt = document.getElementById('vp_bot_token');
    bt.value = '';
    bt.placeholder = c.bot_token_configured ? I18N.t('admin.platforms.bot_set') : '';
    // D13/S20：webhook token 行——仅卡有 webhook 存储语义（webhook_token_supported）时展示
    var wtRow = document.getElementById('vp_webhook_row');
    var wtHint = document.getElementById('vp_webhook_hint');
    var wt = document.getElementById('vp_webhook_token');
    if (wt) {
      wt.value = '';
      wt.placeholder = c.webhook_token_configured ? I18N.t('admin.platforms.bot_set') : '';
    }
    if (wtRow) wtRow.style.display = c.webhook_token_supported ? '' : 'none';
    if (wtHint) wtHint.style.display = c.webhook_token_supported ? '' : 'none';
    // 卡字段动态渲染（排除只读标记位——凭据态/健康态不进表单）
    var fields = {};
    Object.keys(c).forEach(function(k) {
      if (['bot_token_configured', 'webhook_token_configured',
           'webhook_token_supported', 'bot_token_health'].indexOf(k) < 0) fields[k] = c[k];
    });
    _renderCardFields('vp', fields);
    _switchCardTab('vp', 0);
    document.querySelector('#vcsPlatformsView .ct').style.display = 'none';
    document.getElementById('vcsPlatformDetail').style.display = '';
  }

  function backVcsPlatforms() {
    document.getElementById('vcsPlatformDetail').style.display = 'none';
    document.querySelector('#vcsPlatformsView .ct').style.display = '';
  }

  function saveVcsPlatform() {
    var card = _collectCardFields('vp');
    var body = { cards: {} };
    body.cards[_editingVcsPlatform] = card;
    var tok = document.getElementById('vp_bot_token').value;
    if (tok) { body.bot_tokens = {}; body.bot_tokens[_editingVcsPlatform] = tok; }
    // D13/S20：webhook token 凭据通道（{provider}_webhook_token）
    var wtok = (document.getElementById('vp_webhook_token') || {}).value || '';
    if (wtok.trim()) { body.webhook_tokens = {}; body.webhook_tokens[_editingVcsPlatform] = wtok.trim(); }
    api('/vcs-platforms', {method: 'PUT', body: body}).then(function() {
      toast(I18N.t('admin.platforms.saved'), true);
      loadVcsPlatforms();
    }).catch(function(e) { toast(I18N.t('admin.platforms.save_failed') + ': ' + errMsg(e), false); });
  }

  // 卡字段动态渲染/收集（D12：字段由卡驱动，布尔→checkbox，其余→input）
  // 卡字段动态渲染/收集（D12 卡驱动；D21 iOS 分段 Tab 分组，2026-11-12 拍板）
  var _CARD_TAB_FIELDS = {
    basic: ['name', 'type', 'dialect', 'host', 'webhook_path', 'event_header',
            'api_base', 'api_auth', 'auth_url', 'token_url', 'user_url',
            'brand_color', 'pat_create_url', 'download_base', 'user_images_base',
            'token_env', 'legacy_token_symbol', 'clone_auth', 'webhook_verify',
            'bot_username', 'redirect_uri'],
    capability: ['scope', 'id_field', 'login_field', 'name_field', 'email_field',
                 'accept_header', 'pkce_enabled', 'issue_search',
                 'issue_write_form', 'branch_create_form', 'pr_comment_endpoint',
                 'pr_labels_via_issues', 'git_tree_family'],
    storage: ['token_storage_field', 'webhook_token_field', 'session_token_field',
              'global_bot_meta_key', 'global_bot_status_meta_key']
  };
  function _cardFieldTab(k) {
    if (_CARD_TAB_FIELDS.basic.indexOf(k) >= 0) return 1;
    if (_CARD_TAB_FIELDS.capability.indexOf(k) >= 0) return 2;
    if (_CARD_TAB_FIELDS.storage.indexOf(k) >= 0) return 3;
    return 4;
  }
  // 分段控件切换（prefix='vp'|'ac'，idx=0 凭据..4 其他）
  function _switchCardTab(prefix, idx) {
    for (var i = 0; i < 5; i++) {
      var tab = document.getElementById(prefix + 'Tab' + i);
      if (tab) tab.style.display = (i === idx) ? '' : 'none';
    }
    var seg = document.querySelectorAll('#' + prefix + 'Tabs .seg-tab');
    seg.forEach(function(t, j) { t.classList.toggle('act', j === idx); });
  }
  // 常用键白名单（2026-11-13 拍板：53 个卡字段全暴露对管理员不可用——Apple 式
  // 两级曝光）：常用=日常会改的键直接渲染；其余进"高级"折叠区默认收起
  // （details/summary 原生折叠，零 JS 状态管理），改错能力位的面收窄。
  var _COMMON_CARD_KEYS = {
    // 渠道卡（ac）：管理员日常只动凭据与展示
    name: 1, auth_url: 1, token_url: 1, user_url: 1, scope: 1,
    redirect_uri: 1, pkce_enabled: 1, brand_color: 1,
    // 平台卡（vp）：bot 身份与端点基址
    host: 1, api_base: 1, bot_username: 1, webhook_path: 1,
    pat_create_url: 1, download_base: 1
  };
  function _renderCardFields(prefix, card) {
    var groups = {1: [], 2: [], 3: [], 4: []};
    Object.keys(card).forEach(function(k) {
      var v = card[k];
      if (v !== null && typeof v === 'object') return;  // event_map/fields 等映射段不进表单
      var g = _cardFieldTab(k);
      // 高级折叠：非常用键且非"其他"桶——同 tab 内拆 common/advanced 两段
      groups[g].push([k, v, _COMMON_CARD_KEYS[k] ? 0 : 1]);
    });
    [1, 2, 3, 4].forEach(function(n) {
      var el = document.getElementById(prefix + 'CardF' + n);
      if (!el) return;
      el.innerHTML = '';
      var common = document.createElement('div');
      var adv = document.createElement('details');
      adv.className = 'adv-fold';
      adv.innerHTML = '<summary data-i18n="admin.adv_fields">' +
        (I18N.t('admin.adv_fields') !== 'admin.adv_fields' ? I18N.t('admin.adv_fields') : '高级字段') + '</summary>';
      var advBody = document.createElement('div');
      advBody.className = 'adv-body';
      adv.appendChild(advBody);
      groups[n].forEach(function(kv) {
        var k = kv[0], v = kv[1], isAdv = kv[2];
        var row = document.createElement('div');
        row.className = 'f-row';
        // 字段级配置指导（2026-11-13）：每键配 i18n hint（admin.field_docs.<key>），
        // 有词条渲染输入框下方小字——53 键的语义内置表单，不靠用户猜。
        var _docKey = 'admin.field_docs.' + k;
        var _doc = I18N.t(_docKey);
        if (_doc === _docKey) _doc = '';  // 无词条不渲染
        if (typeof v === 'boolean') {
          row.innerHTML =
            '<label>' + esc(k) + '</label>' +
            '<input type="checkbox" data-cardfield="' + esc(k) + '" data-cardtype="bool"' + (v ? ' checked' : '') + '>' +
            (_doc ? '<div class="f-doc">' + esc(_doc) + '</div>' : '');
        } else if (typeof v === 'number') {
          row.innerHTML =
            '<label>' + esc(k) + '</label>' +
            '<input type="number" class="ipt" style="background:#fff;color:#1d1d1f" data-cardfield="' + esc(k) + '" data-cardtype="num" value="' + esc(String(v)) + '">' +
            (_doc ? '<div class="f-doc">' + esc(_doc) + '</div>' : '');
        } else {
          row.innerHTML =
            '<label>' + esc(k) + '</label>' +
            '<input type="text" class="ipt" style="background:#fff;color:#1d1d1f" data-cardfield="' + esc(k) + '" data-cardtype="text" value="' + esc(String(v == null ? '' : v)) + '">' +
            (_doc ? '<div class="f-doc">' + esc(_doc) + '</div>' : '');
        }
        (isAdv ? advBody : common).appendChild(row);
      });
      el.appendChild(common);
      if (advBody.childElementCount > 0) el.appendChild(adv);
    });
    // "其他"tab 是分组兜底桶（未匹配字段零丢失保障）——空时整 tab 隐藏
    // （用户拍板 2026-11-12：空 tab 不展示，无"无未分组字段"占位）
    var otherTab = document.getElementById(prefix + 'Tab4');
    var otherSeg = document.querySelectorAll('#' + prefix + 'Tabs .seg-tab')[4];
    var hasOther = groups[4].length > 0;
    if (otherTab) otherTab.style.display = hasOther ? '' : 'none';
    if (otherSeg) otherSeg.style.display = hasOther ? '' : 'none';
  }

  function _collectCardFields(prefix) {
    var out = {};
    var els = document.querySelectorAll(
      '#' + prefix + 'CardF1 [data-cardfield], #' + prefix + 'CardF2 [data-cardfield], ' +
      '#' + prefix + 'CardF3 [data-cardfield], #' + prefix + 'CardF4 [data-cardfield]');
    els.forEach(function(inp) {
      var t = inp.getAttribute('data-cardtype');
      if (t === 'bool') out[inp.getAttribute('data-cardfield')] = inp.checked;
      else if (t === 'num') out[inp.getAttribute('data-cardfield')] = Number(inp.value || 0);
      else out[inp.getAttribute('data-cardfield')] = inp.value;
    });
    return out;
  }

  window._toggleAdminLang = _toggleAdminLang;
  window._switchCardTab = _switchCardTab;
  window.showAuthChannels = showAuthChannels;
  window.showVcsPlatforms = showVcsPlatforms;
  window.viewAuthChannel = viewAuthChannel;
  window.backAuthChannels = backAuthChannels;
  window.saveAuthChannel = saveAuthChannel;
  window.viewVcsPlatform = viewVcsPlatform;
  window.backVcsPlatforms = backVcsPlatforms;
  window.saveVcsPlatform = saveVcsPlatform;
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
  var _lastLoginWlProviders = [];  // 渠道目录元数据（GET /whitelist providers）
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
    _hideCardMgmtViews();
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
          menuItems += '<button onclick="editUser(\'' + u.id + '\')">' + I18N.t('admin.users.edit') + '</button>';
          menuItems += '<button onclick="resetUserPassword(\'' + u.id + '\')">' + I18N.t('admin.users.reset_password') + '</button>';
        }
        if (u.status === 1) {
          menuItems += '<button class="danger" onclick="disableUser(\'' + u.id + '\')">' + I18N.t('admin.users.disable') + '</button>';
        } else if (u.status === 0 || u.status === 3) {
          menuItems += '<button onclick="restoreUser(\'' + u.id + '\')">' + I18N.t('admin.users.restore') + '</button>';
        }

        var nicknameSub = u.nickname ? '<div class="user-sub">' + esc(u.nickname) + '</div>' : '';
        var dotCls = statusDotCls[u.status] || 's-del';

        tr.innerHTML =
          '<td style="padding:10px 12px"><div class="user-name">' + esc(u.username) + '</div>' + nicknameSub + '</td>' +
          '<td style="padding:10px 12px;color:var(--t2,#5b667b)">' + esc(u.email || '—') + '</td>' +
          '<td style="padding:10px 12px"><span class="status-dot ' + dotCls + '"></span>' + (statusMap[u.status]||u.status) + '</td>' +
          '<td style="padding:10px 12px;color:var(--t2,#5b667b)">' + (u.last_login_at || '—') + '</td>' +
          '<td style="padding:10px 12px" class="row-actions"><button class="row-actions-btn" onclick="event.stopPropagation();toggleRowMenu(\'' + u.id + '\')">⋯</button>' +
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
    _hideCardMgmtViews();
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
    // 2026-11-12 行式化：GET /whitelist 返回 {whitelist, enabled, providers}——
    // whitelist=行形态条目 {id, provider, username, remark, created_at, legacy}
    // providers=渠道目录元数据（provider/name），下拉动态填充，零字面量平台 option
    api('/whitelist').then(function(d) {
      _lastLoginWhitelist = d.whitelist || [];
      _lastLoginWhitelistEnabled = !!(d && d.enabled);
      _lastLoginWlProviders = d.providers || [];
      _whitelistViewVisible = true;
      _rerenderLoginWhitelist();
    }).catch(function(e) { toast(I18N.t('admin.login_whitelist.load_failed') + ': ' + errMsg(e), false); });
  }

  function _fillLoginWlProviderSelect() {
    var sel = document.getElementById('lw_provider');
    if (!sel) return;
    var prev = sel.value;
    sel.innerHTML = '';
    (_lastLoginWlProviders || []).forEach(function(p) {
      var opt = document.createElement('option');
      opt.value = p.provider;
      opt.textContent = p.name || p.provider;
      sel.appendChild(opt);
    });
    if (prev) sel.value = prev;
  }

  function _renderLoginWhitelistRows() {
    var tbody = document.getElementById('loginWhitelistTbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    (_lastLoginWhitelist || []).forEach(function(w) {
      var tr = document.createElement('tr');
      tr.style.borderBottom = '1px solid var(--b,#e3e6ee)';
      var legacyTag = w.legacy ? ' <span class="tag" data-i18n="admin.login_whitelist.legacy_tag" style="font-size:11px">存量列式</span>' : '';
      // legacy 条目带 provider 定位删除（只清对应平台列，一人多平台老行互不误伤）
      var delArg = w.legacy ? (w.id + ", '" + w.provider + "'") : ('' + w.id);
      tr.innerHTML =
        '<td style="padding:8px">' + w.id + '</td>' +
        '<td style="padding:8px">' + esc(w.provider || '-') + '</td>' +
        '<td style="padding:8px">' + esc(w.username || '-') + legacyTag + '</td>' +
        '<td style="padding:8px">' + esc(w.remark || '-') + '</td>' +
        '<td style="padding:8px;color:var(--t2)">' + (w.created_at || '-') + '</td>' +
        '<td style="padding:8px"><button class="btn" style="padding:4px 10px;font-size:12px;color:var(--r,#dc2828)" onclick="deleteLoginWhitelist(' + delArg + ')">' + I18N.t('admin.login_whitelist.delete') + '</button></td>';
      tbody.appendChild(tr);
    });
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

  function deleteLoginWhitelist(id, provider) {
    var url = '/whitelist/' + id + (provider ? ('?provider=' + encodeURIComponent(provider)) : '');
    api(url, {method: 'DELETE'}).then(function() {
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
    if (window.isPwdTooShort(password)) { toast(I18N.t('admin.users.password_required'), false); return; }
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
    if (window.isPwdTooShort(password)) { toast(I18N.t('admin.users.password_required'), false); return; }
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
