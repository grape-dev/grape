const app = (() => {
  const $ = id => document.getElementById(id);
  const VIEWS = ['dash','pipe','vod','issue','coding','qa','sre','chat','settings','skills','backends','event','repos'];

  function currentRepo() {
    try { return JSON.parse(localStorage.getItem('currentRepo')); } catch { return null; }
  }

  function showLogin() {
    $('loginView').classList.remove('hid');
    $('sidebar').style.display = 'none';
    $('mainView').style.display = 'none';
  }

  function showApp() {
    $('loginView').classList.add('hid');
    // Sidebar and mainView visibility is managed by loadRepos
    loadProfile();
    loadRepos();
  }

  function loadProfile() {
    API.get('/auth/profile').then(d => {
      $('userName').textContent = d.username || 'User';
      $('userAvatar').textContent = (d.username || 'U')[0].toUpperCase();
      $('userRole').textContent = (d.managed_repos && d.managed_repos.length > 0) ? 'Admin' : 'Member';
    }).catch(() => {});
  }

  function loadRepos() {
    console.log("[loadRepos] start");
    API.get('/repos/member').then(d => {
      const repos = d.repos || [];
      console.log("[loadRepos] repos", JSON.stringify({total: repos.length, managed: repos.filter(r => r.is_joined || r.is_hosted).length}));
      _updateSidebarRepos(repos);
      const current = currentRepo();

      const managedRepos = repos.filter(r => r.is_joined || r.is_hosted);
      if (managedRepos.length === 0) {
        $('sidebar').style.display = 'none';
        $('tb').style.display = 'none';
        $('mainView').style.display = 'flex';
        $('mainView').style.position = 'fixed';
        $('mainView').style.inset = '0';
        $('mainView').style.zIndex = '100';
        $('pageTitle').textContent = '选择仓库';
        renderRepos(repos);
        return;
      }

      $('sidebar').style.display = 'flex';
      $('mainView').style.display = 'flex';
      $('repoView').classList.remove('ac');

      if (!current) {
        const r = managedRepos[0];
        localStorage.setItem('currentRepo', JSON.stringify({fullName: r.full_name, owner: r.owner, name: r.name}));
        nav('dash');
      }
    }).catch(() => {
      console.error("[loadRepos] FAILED");
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      $('mainView').style.display = 'flex';
      $('mainView').style.position = 'fixed';
      $('mainView').style.inset = '0';
      $('mainView').style.zIndex = '100';
      const el = $('pageContent');
      if (el) el.innerHTML = '<div style="max-width:900px;margin:0 auto;padding:40px 0"><div class="error">无法加载仓库列表，请确认 GitCode Token 是否有效</div></div>';
    });
  }

  function initSidebarRepos() {
    // Lightweight version for after repo selection - just populate sidebar dropdown
    API.get('/repos/managed').then(d => {
      const repos = d.repos || [];
      const sel = $('repoSelect');
      const current = currentRepo();
      sel.innerHTML = '<option value="">选择仓库...</option>' + repos.map(r =>
        `<option value="${r.full_name}" ${current && current.fullName === r.full_name ? 'selected' : ''}>${r.full_name}</option>`
      ).join('');
    }).catch(() => {});
  }

  function _updateSidebarRepos(repos) {
    const sel = $('repoSelect');
    const current = currentRepo();
    sel.innerHTML = '<option value="">选择仓库...</option>' + repos.map(r =>
      `<option value="${r.full_name}" ${current && current.fullName === r.full_name ? 'selected' : ''}>${r.full_name}</option>`
    ).join('');
  }

  function switchRepo(val) {
    if (!val) return;
    const parts = val.split('/');
    localStorage.setItem('currentRepo', JSON.stringify({fullName: val, owner: parts[0], name: parts[1]}));
    nav('dash');
  }

  let _refreshTimer = null;
  function nav(view, param) {
    const repo = currentRepo();
    if (!repo && view !== 'repos' && view !== 'settings') { loadRepos(); return; }

    VIEWS.forEach(v => { const el = $('nav-' + v); if (el) el.classList.remove('act'); });
    const navEl = $('nav-' + view);
    if (navEl) navEl.classList.add('act');
    document.querySelectorAll('.sb-nav a').forEach(a => {
      const o = a.getAttribute('onclick') || '';
      if (param && o.includes("'" + param + "'")) a.classList.add('act');
      else if (!param && o === "nav('" + view + "')") a.classList.add('act');
    });

    const title = $('pageTitle');
    const repoTitle = $('repoTitle');
    const pc = $('pageContent');
    pc.innerHTML = '<div class="loading">加载中...</div>';

    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }

    repoTitle.textContent = repo ? repo.fullName : '';

    if (view === 'dash') {
      title.textContent = 'Dashboard';
      const loadDash = () => API.get('/dashboard/' + repo.owner + '/' + repo.name).then(renderDashboard).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
      loadDash();
      _refreshTimer = setInterval(loadDash, 30000);
    } else if (view === 'pipe') {
      title.textContent = 'Pipeline';
      API.get('/pipeline/' + repo.owner + '/' + repo.name).then(renderPipeline).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (['vod','issue','coding','qa','sre'].includes(view)) {
      title.textContent = AGENT_META[view].display;
      API.get('/agents/' + view + '/' + repo.owner + '/' + repo.name).then(d => renderAgent(view, d)).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'event') {
      title.textContent = 'Event Detail';
      API.get('/events/detail/' + param).then(renderEventDetail).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'chat') {
      title.textContent = 'Chat Agent';
      renderChat();
    } else if (view === 'settings') {
      title.textContent = 'General Settings';
      renderSettings(param || 'credentials');
    } else if (view === 'skills') {
      title.textContent = 'DevOps Skills';
      renderSkills();
    } else if (view === 'backends') {
      title.textContent = 'Backend Agents';
      API.get('/settings/backend-agents').then(renderBackends).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'repos') {
      title.textContent = '选择仓库';
      loadRepos();
    }
  }

  function search(q) {
    if (!q.trim()) return;
    const pc = $('pageContent');
    API.get('/search/?q=' + encodeURIComponent(q) + '&type=all').then(d => {
      let html = '<div class="cd"><div class="cd-h"><h2>Search: "' + q + '"</h2></div>';
      html += '<div class="ts t2">Events: ' + (d.events || []).length + ' / Repos: ' + (d.repos || []).length + ' / Agents: ' + (d.agents || []).length + '</div>';
      (d.events || []).forEach(ev => { html += '<div class="ai" onclick="app.nav(\'event\',' + ev.seq + ')" style="cursor:pointer"><div class="ac"><b>' + (ev.title || '') + '</b><span class="ts t3"> ' + (ev.repo_key || '') + '</span></div></div>'; });
      (d.agents || []).forEach(a => { html += '<div class="cl" onclick="app.nav(\'' + a.name + '\')"><div class="cx"><h4>' + a.display_name + '</h4></div></div>'; });
      html += '</div>';
      pc.innerHTML = html;
    }).catch(() => {});
  }

  function oauthLogin() {
    API.get('/auth/login/gitcode').then(d => { if (d.redirect_url) window.location.href = d.redirect_url; }).catch(e => showToast('OAuth 未配置'));
  }

  let pwdVisible = false;
  function togglePwdLogin() {
    pwdVisible = !pwdVisible;
    $('loginLinks').style.display = pwdVisible ? 'none' : '';
    $('pwdLogin').style.display = pwdVisible ? 'block' : 'none';
    $('gitBtn').style.display = pwdVisible ? 'none' : '';
  }

  function pwdLogin() {
    const u = $('loginUser').value.trim();
    const p = $('loginPass').value.trim();
    if (!u || !p) return showToast('请输入用户名和密码');
    API.post('/auth/login', {username: u, password: p}).then(d => {
      localStorage.setItem('token', d.token);
      localStorage.setItem('user', JSON.stringify(d.user));
      showApp();
    }).catch(e => showToast('登录失败: ' + e.message));
  }

  function logout() {
    const t = localStorage.getItem('token');
    if (t) API.post('/auth/logout', {}).catch(() => {});
    localStorage.removeItem('token'); localStorage.removeItem('user'); localStorage.removeItem('currentRepo');
    showLogin();
  }

  function showToast(msg) {
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#101724;color:#fff;padding:10px 20px;border-radius:8px;font-size:12px;z-index:999;box-shadow:0 4px 16px rgba(0,0,0,.2)';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3000);
  }

  function init() {
    console.log("[init] start", JSON.stringify({
      hasToken: !!localStorage.getItem('token'),
      cacheVer: localStorage.getItem('app_cache_ver'),
      currentRepo: localStorage.getItem('currentRepo'),
      url: window.location.href
    }));
    // Check for OAuth token in URL first
    const p = new URLSearchParams(window.location.search);
    const urlToken = p.get('token');
    if (urlToken) {
      localStorage.setItem('token', urlToken);
      localStorage.setItem('app_cache_ver', '2');
      // Clear old repo selection on fresh OAuth login
      if (p.get('first_login')) {
        localStorage.removeItem('currentRepo');
      }
      showApp();
      // Remove token from URL without reload
      if (window.history.replaceState) {
        window.history.replaceState(null, '', '/');
      }
      return;
    }

    // Cache version check - only clear if version mismatch AND not in OAuth flow
    if (localStorage.getItem('app_cache_ver') !== '2') {
      const oldRepo = localStorage.getItem('currentRepo');
      localStorage.clear();
      if (oldRepo) localStorage.setItem('currentRepo', oldRepo); // restore repo selection
      localStorage.setItem('app_cache_ver', '2');
    }

    const token = localStorage.getItem('token');
    if (token) { showApp(); }
    else { showLogin(); }
  }

  return {nav, search, oauthLogin, togglePwdLogin, pwdLogin, logout, showToast, switchRepo, currentRepo, showLogin, init, loadRepos, initSidebarRepos};
})();
document.addEventListener('DOMContentLoaded', () => app.init());
