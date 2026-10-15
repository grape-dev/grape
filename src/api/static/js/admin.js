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

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    var tk = getToken();
    if (tk && path.indexOf('/login') < 0) opts.headers['Authorization'] = 'Bearer ' + tk;
    if (opts.body && typeof opts.body === 'object') opts.body = JSON.stringify(opts.body);
    return fetch('/api/admin' + path, opts).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) { var e = new Error(data && data.detail && data.detail.message) || '请求失败'; e.status = r.status; throw e; }
        return data;
      });
    });
  }

  // ====== 登录 ======
  function login() {
    var u = $2('loginUser').value.trim(), p = $2('loginPass').value.trim();
    if (!u || !p) { $2('loginErr').textContent = '请输入账号和密码'; return; }
    $2('loginErr').textContent = '';
    api('/login', { method: 'POST', body: { username: u, password: p } }).then(function (d) {
      setToken(d.token);
      $2('adminUser').textContent = d.user.username;
      showApp(d.pwd_initial);
    }).catch(function (e) { $2('loginErr').textContent = e.message; });
  }

  // ====== 视图切换 ======
  function showApp(pwdInitial) {
    $2('loginView').classList.add('hid');
    $2('mainView').classList.remove('hid');
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
    if (!oldP || !newP) { toast('请填写原密码与新密码', false); return; }
    if (newP.length < 8) { toast('新密码至少 8 位', false); return; }
    if (newP !== confirmP) { toast('两次输入的新密码不一致', false); return; }
    api('/change-password', { method: 'POST', body: { old_password: oldP, new_password: newP } }).then(function () {
      toast('密码已修改，请重新登录', true);
      setToken(null);
      $2('pwdModal').classList.add('hid');
      $2('mainView').classList.add('hid');
      $2('loginView').classList.remove('hid');
      $2('loginPass').value = ''; $2('loginErr').textContent = '';
    }).catch(function (e) { toast(e.message, false); });
  }

  // ====== 登出 ======
  function logout() {
    api('/logout', { method: 'POST' }).then(function () { setToken(null); location.reload(); })
      .catch(function () { setToken(null); location.reload(); });
  }

  // ====== 配置中心 ======
  function showCfg() {
    // 隐藏用户管理和白名单区域
    document.getElementById('usersView').style.display = 'none';
    document.getElementById('whitelistView').style.display = 'none';
    // 显示配置区域
    document.querySelector('.mn > div:first-child').style.display = '';
    var cfgCt = document.querySelector('.mn .ct');
    if (cfgCt) cfgCt.style.display = '';
    api('/config').then(function (d) {
      var c = d.config || {};
      var os = c.oauth_status || {};
      applyToField('f_gitcode_api_token', c.gitcode_api_token_present, c.gitcode_api_token_tail);
      applyToField('f_github_bot_token', c.github_bot_token_present, c.github_bot_token_tail);
      // client_id / redirect_uri 非敏感，回显明文；secret 占位提示
      $2('f_gitcode_oauth_client_id').value = c.gitcode_oauth_client_id || '';
      $2('f_gitcode_oauth_client_id').placeholder = c.gitcode_oauth_client_id_present ? '● 已配置' : 'GitCode OAuth App Client ID';
      $2('f_github_oauth_client_id').value = c.github_oauth_client_id || '';
      $2('f_github_oauth_client_id').placeholder = c.github_oauth_client_id_present ? '● 已配置' : 'GitHub OAuth App Client ID';
      $2('f_gitcode_oauth_client_secret').value = '';
      $2('f_gitcode_oauth_client_secret').placeholder = os.gitcode ? '● 已配置（重新输入以更新）' : 'GitCode OAuth App Client Secret';
      $2('f_github_oauth_client_secret').value = '';
      $2('f_github_oauth_client_secret').placeholder = os.github ? '● 已配置（重新输入以更新）' : 'GitHub OAuth App Client Secret';
      $2('f_gitcode_oauth_redirect_uri').value = c.gitcode_oauth_redirect_uri || '';
      $2('f_gitcode_oauth_redirect_uri').placeholder = '留空自动推断';
      $2('f_github_oauth_redirect_uri').value = c.github_oauth_redirect_uri || '';
      $2('f_github_oauth_redirect_uri').placeholder = '留空自动推断';
      $2('f_bot_usernames').value = (c.bot_usernames || []).join(', ');
    }).catch(function (e) { toast('加载配置失败: ' + e.message, false); });
  }

  function applyToField(fieldId, present, tail) {
    var el = $2(fieldId);
    if (present) { el.value = ''; el.placeholder = '● 已配置 (…' + tail + ')'; }
    else { el.value = ''; el.placeholder = '○ 未配置'; }
  }

  function saveConfig() {
    $2('saveBtn').disabled = true;
    var body = {};
    var gitcodeTok = $2('f_gitcode_api_token').value.trim();
    var githubTok = $2('f_github_bot_token').value.trim();
    if (gitcodeTok) body.gitcode_api_token = gitcodeTok;
    if (githubTok) body.github_bot_token = githubTok;
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
    api('/config', { method: 'PUT', body: body }).then(function (d) {
      toast('配置已保存', true);
      showCfg();
    }).catch(function (e) { toast('保存失败: ' + e.message, false); })
      .finally(function () { $2('saveBtn').disabled = false; });
  }

  function verifyToken(provider, fieldId) {
    var tok = $2(fieldId).value.trim();
    if (!tok) { toast('请先填入 token 再校验', false); return; }
    api('/config/verify-token', { method: 'POST', body: { provider: provider, token: tok } }).then(function (d) {
      var st = $2('s_' + fieldId.replace('f_', ''));
      if (d.ok) { st.textContent = '✔ ' + (d.login || d.name); toast('token 有效: ' + d.login, true); }
      else { st.textContent = '✗ 无效'; toast('token 无效或不具备访问权限', false); }
    }).catch(function (e) { toast('校验失败: ' + e.message, false); });
  }

  // ====== 初始化 ======
  function init() {
    $2('loginBtn').onclick = login;
    document.getElementById('loginPass').addEventListener('keydown', function (e) { if (e.key === 'Enter') login(); });
    // 已登录则直接进主界面
    if (getToken()) { showApp(false); showCfg(); }
  }

  window.AdminApp = { login: login, changePwd: changePwd, closePwdModal: closePwdModal, openPwdModal: openPwdModal,
                      logout: logout, showCfg: showCfg, saveConfig: saveConfig, verifyToken: verifyToken };
  // 暴露到全局——admin.html 内联 onclick 引用裸函数名
  window.login = login;
  window.changePwd = changePwd;
  window.closePwdModal = closePwdModal;
  window.openPwdModal = openPwdModal;
  window.logout = logout;
  window.showCfg = showCfg;
  window.saveConfig = saveConfig;
  window.verifyToken = verifyToken;
  document.addEventListener('DOMContentLoaded', init);
  // ===== D6: 用户管理 =====
  var _userPage = 1;

  function showUsers(page, ev) {
    _userPage = page || 1;
    var navItems = document.querySelectorAll('.sb-nav a');
    navItems.forEach(function(a){a.classList.remove('act')});
    if (ev && ev.target) ev.target.classList.add('act');
    document.querySelector('.mn > div:first-child').style.display = 'none'; // hide config tb
    var cfgCt = document.querySelector('.mn .ct');
    if (cfgCt) cfgCt.style.display = 'none';
    document.getElementById('usersView').style.display = '';
    document.getElementById('whitelistView').style.display = 'none';

    var q = (document.getElementById('userSearch') || {}).value || '';
    api('/users?page=' + _userPage + '&per_page=20&q=' + encodeURIComponent(q)).then(function(d) {
      var tbody = document.getElementById('usersTbody');
      tbody.innerHTML = '';
      var statusMap = {0:'禁用',1:'正常',2:'已注销',3:'冷静期'};
      var statusColor = {0:'#dc2828',1:'#1ca44c',2:'#86868b',3:'#f5a623'};
      d.users.forEach(function(u) {
        var tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #e3e6ee';
        var actions = '';
        if (u.status === 1) {
          actions = '<button class="btn" style="padding:4px 10px;font-size:12px;color:#dc2828" onclick="disableUser(' + u.id + ')">禁用</button>';
        } else if (u.status === 0) {
          actions = '<button class="btn btn-p" style="padding:4px 10px;font-size:12px" onclick="restoreUser(' + u.id + ')">恢复</button>';
        } else if (u.status === 3) {
          actions = '<button class="btn btn-p" style="padding:4px 10px;font-size:12px" onclick="restoreUser(' + u.id + ')">恢复</button>';
        }
        tr.innerHTML =
          '<td style="padding:8px">' + u.id + '</td>' +
          '<td style="padding:8px">' + esc(u.username) + '</td>' +
          '<td style="padding:8px">' + esc(u.email || '-') + '</td>' +
          '<td style="padding:8px;color:' + (statusColor[u.status]||'#86868b') + '">' + (statusMap[u.status]||u.status) + '</td>' +
          '<td style="padding:8px">' + (u.bindings ? u.bindings.join(', ') : '-') + '</td>' +
          '<td style="padding:8px;color:var(--t2)">' + (u.last_login_at || '-') + '</td>' +
          '<td style="padding:8px">' + actions + '</td>';
        tbody.appendChild(tr);
      });
      document.getElementById('userCount').textContent = '(共 ' + d.total + ' 人)';
      // pager
      var pager = document.getElementById('userPager');
      var totalPages = Math.ceil(d.total / 20);
      pager.innerHTML = '';
      if (totalPages > 1) {
        for (var i = 1; i <= totalPages; i++) {
          var btn = document.createElement('button');
          btn.textContent = i;
          btn.className = 'btn' + (i === _userPage ? ' btn-p' : '');
          btn.style.cssText = 'padding:4px 10px;font-size:12px;margin:0 2px';
          btn.onclick = function(p){ return function(){ showUsers(p); }; }(i);
          pager.appendChild(btn);
        }
      }
    }).catch(function(e) { toast('加载用户列表失败: ' + e.message, false); });
  }

  function disableUser(uid) {
    if (!confirm('确定禁用此用户？')) return;
    api('/users/' + uid + '/disable', {method: 'POST', body: {reason: 'admin_disabled'}}).then(function() {
      toast('用户已禁用', true);
      showUsers(_userPage);
    }).catch(function(e) { toast('禁用失败: ' + e.message, false); });
  }

  function restoreUser(uid) {
    api('/users/' + uid + '/restore', {method: 'POST'}).then(function() {
      toast('用户已恢复', true);
      showUsers(_userPage);
    }).catch(function(e) { toast('恢复失败: ' + e.message, false); });
  }

  function showWhitelist(ev) {
    var navItems = document.querySelectorAll('.sb-nav a');
    navItems.forEach(function(a){a.classList.remove('act')});
    if (ev && ev.target) ev.target.classList.add('act');
    document.querySelector('.mn > div:first-child').style.display = 'none';
    var cfgCt = document.querySelector('.mn .ct');
    if (cfgCt) cfgCt.style.display = 'none';
    document.getElementById('usersView').style.display = 'none';
    document.getElementById('whitelistView').style.display = '';
    loadWhitelist();
    loadAllowSignup();
  }

  function loadWhitelist() {
    api('/signup-whitelist').then(function(d) {
      var tbody = document.getElementById('whitelistTbody');
      tbody.innerHTML = '';
      (d.whitelist || []).forEach(function(w) {
        var tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #e3e6ee';
        tr.innerHTML =
          '<td style="padding:8px">' + w.id + '</td>' +
          '<td style="padding:8px">' + (w.type === 'email' ? '邮箱' : '域名') + '</td>' +
          '<td style="padding:8px">' + esc(w.value) + '</td>' +
          '<td style="padding:8px">' + esc(w.remark || '-') + '</td>' +
          '<td style="padding:8px;color:var(--t2)">' + (w.created_at || '-') + '</td>' +
          '<td style="padding:8px"><button class="btn" style="padding:4px 10px;font-size:12px;color:#dc2828" onclick="deleteWhitelist(' + w.id + ')">删除</button></td>';
        tbody.appendChild(tr);
      });
    }).catch(function(e) { toast('加载白名单失败: ' + e.message, false); });
  }

  function addWhitelist() {
    var type = document.getElementById('wl_type').value;
    var value = document.getElementById('wl_value').value.trim();
    var remark = document.getElementById('wl_remark').value.trim();
    if (!value) { toast('请输入值', false); return; }
    api('/signup-whitelist', {method: 'POST', body: {type: type, value: value, remark: remark}}).then(function() {
      toast('已添加', true);
      document.getElementById('wl_value').value = '';
      document.getElementById('wl_remark').value = '';
      loadWhitelist();
    }).catch(function(e) { toast('添加失败: ' + e.message, false); });
  }

  function deleteWhitelist(id) {
    api('/signup-whitelist/' + id, {method: 'DELETE'}).then(function() {
      toast('已删除', true);
      loadWhitelist();
    }).catch(function(e) { toast('删除失败: ' + e.message, false); });
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
      toast('已更新', true);
    }).catch(function(e) { toast('更新失败: ' + e.message, false); });
  }

  function esc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

  window.showUsers = showUsers;
  window.showWhitelist = showWhitelist;
  window.disableUser = disableUser;
  window.restoreUser = restoreUser;
  window.addWhitelist = addWhitelist;
  window.deleteWhitelist = deleteWhitelist;
  window.saveAllowSignup = saveAllowSignup;
})();
