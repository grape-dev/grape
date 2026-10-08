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
    loadStatus();
    if (pwdInitial) openPwdModal();
  }

  function loadStatus() {
    api('/status').then(function (d) {
      if (d.pwd_initial) { $2('pwdBanner').classList.remove('hid'); } else { $2('pwdBanner').classList.add('hid'); }
    }).catch(function () {});
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
    if (pwdModalMode === 'first') { $2('pwdBanner').classList.remove('hid'); }
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
    api('/config').then(function (d) {
      var c = d.config || {};
      applyToField('f_gitcode_api_token', c.gitcode_api_token_present, c.gitcode_api_token_tail);
      applyToField('f_github_bot_token', c.github_bot_token_present, c.github_bot_token_tail);
      $2('f_gitcode_oauth_client_id').value = c.gitcode_oauth_client_id_present ? '● 已配置' : '';
      $2('f_github_oauth_client_id').value = c.github_oauth_client_id_present ? '● 已配置' : '';
      $2('f_gitcode_oauth_client_secret').value = '';
      $2('f_github_oauth_client_secret').value = '';
      $2('f_gitcode_oauth_redirect_uri').value = c.gitcode_oauth_redirect_uri || '';
      $2('f_github_oauth_redirect_uri').value = c.github_oauth_redirect_uri || '';
      $2('f_bot_usernames').value = (c.bot_usernames || []).join(', ');
      if (d.pwd_initial) { $2('pwdBanner').classList.remove('hid'); } else { $2('pwdBanner').classList.add('hid'); }
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
})();