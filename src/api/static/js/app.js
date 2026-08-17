const app = (() => {
  const $ = id => document.getElementById(id);
  const VIEWS = ['dash','pipe','vod','issue','coding','qa','sre','chat','settings','skills','knowledge','runtimes','event','repos','skill','tasks'];

  function currentRepo() {
    try {
      const r = JSON.parse(localStorage.getItem('currentRepo'));
      if (r && r.fullName) {
        // GitCode 仓库 name 是显示名（可≠URL path，如 shelltest 显示 skillstest）——
        // 所有 API 路径/链接都用 owner/name 拼接，必须强制 URL 语义（fullName 拆分），
        // 否则旧脏 name 导致 /api/pipeline/{owner}/{name} 查空（见 repo_manager.fetch_repos_with_role）
        const parts = String(r.fullName).split('/');
        if (parts.length === 2 && r.name !== parts[1]) {
          r.name = parts[1];
          localStorage.setItem('currentRepo', JSON.stringify(r));
        }
      }
      return r;
    } catch { return null; }
  }

  function showLogin() {
    $('loginView').classList.remove('hid');
    $('sidebar').style.display = 'none';
    $('mainView').style.display = 'none';
  }

function loadAgentTypes() {
  fetch('/api/agents/types').then(r => r.json()).then(d => {
    (d.types || []).forEach(t => { _AGENT_TYPES[t.agent_type] = t; });
  }).catch(() => {});
}

  function showApp() {
    $('loginView').classList.add('hid');
    loadAgentTypes();
    loadProfile().then(() => loadRepos()).catch(() => loadRepos());
  }

  function loadProfile() {
    return API.get('/auth/profile').then(d => {
      const displayName = d.nickname || d.username || 'User';
      $('userName').textContent = displayName;
      $('userAvatar').textContent = (displayName || 'U')[0].toUpperCase();
      $('userRole').textContent = (d.managed_repos && d.managed_repos.length > 0) ? 'Admin' : 'Member';
      localStorage.setItem('user', JSON.stringify(d));
      // 如果当前在仓库选择页，更新 Welcome 消息
      const welcomeEl = document.getElementById('welcomeUser');
      if (welcomeEl) welcomeEl.textContent = displayName;
    }).catch(() => {});
  }

  const REPO_CACHE_KEY = 'repo_member_cache';

  function loadRepos(forceRefresh) {
    console.log("[loadRepos] start");
    // 先用 localStorage 缓存的仓库列表立即渲染（避免白屏），后台再刷新
    let cachedRepos = null;
    try {
      const c = JSON.parse(localStorage.getItem(REPO_CACHE_KEY) || 'null');
      if (c && c.repos && Array.isArray(c.repos)) cachedRepos = c.repos;
    } catch (e) { cachedRepos = null; }

    if (cachedRepos && !forceRefresh) {
      renderReposFromData(cachedRepos);
    }

    API.get('/repos/member').then(d => {
      const repos = d.repos || [];
      // 更新 localStorage 缓存（记录时间）
      try {
        localStorage.setItem(REPO_CACHE_KEY, JSON.stringify({repos, ts: Date.now()}));
      } catch (e) {}
      if (cachedRepos && !forceRefresh) {
        // 后台刷新后若数据变化，轻量更新侧边栏
        initSidebarRepos();
      } else {
        renderReposFromData(repos);
      }
    }).catch(() => {
      if (cachedRepos) return; // 有缓存时静默，避免覆盖已渲染内容
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

  function renderReposFromData(repos) {
    const managedRepos = repos.filter(r => r.is_joined || r.is_hosted);
    console.log("[loadRepos] repos", JSON.stringify({total: repos.length, managed: managedRepos.length}));
    initSidebarRepos();
    const current = currentRepo();

    if (managedRepos.length === 0) {
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      $('mainView').style.display = 'flex';
      $('mainView').style.position = 'fixed';
      $('mainView').style.inset = '0';
      $('mainView').style.zIndex = '100';
      $('pageTitle').textContent = '选择仓库';
      renderRepoSelection(repos);
      return;
    }

    $('sidebar').style.display = 'flex';
    $('tb').style.display = 'flex';
    $('mainView').style.display = 'flex';
    $('mainView').style.position = '';
    $('mainView').style.inset = '';
    $('mainView').style.zIndex = '';
    $('pageTitle').textContent = 'Dashboard';

    if (!current || !managedRepos.some(r => r.full_name === current.fullName)) {
      const r = managedRepos[0];
      if (r) localStorage.setItem('currentRepo', JSON.stringify({fullName: r.full_name, owner: r.owner, name: r.name}));
      else localStorage.removeItem('currentRepo');
    }
    nav('dash');
  }

  function initSidebarRepos() {
    // Lightweight version for after repo selection - just populate sidebar dropdown
    API.get('/repos/managed').then(d => {
      const repos = d.repos || [];
      const sel = $('repoSelect');
      const current = currentRepo();
      sel.innerHTML = repos.map(r =>
        `<option value="${r.full_name}" ${current && current.fullName === r.full_name ? 'selected' : ''}>${r.full_name}</option>`
      ).join('') + '<option value="__add__">＋ 新增仓库托管</option>';
    }).catch(() => {});
  }

  function _updateSidebarRepos(repos) {
    const sel = $('repoSelect');
    const current = currentRepo();
    sel.innerHTML = repos.map(r =>
      `<option value="${r.full_name}" ${current && current.fullName === r.full_name ? 'selected' : ''}>${r.full_name}</option>`
    ).join('');
  }

  function switchRepo(val) {
    if (!val) return;
    if (val === '__add__') {
      // Reset dropdown to current repo
      const cur = currentRepo();
      const sel = $('repoSelect');
      if (cur) sel.value = cur.fullName;
      // 与点击 Grape Logo「返回仓库选择大堂」一致：进入全屏仓库选择大堂（隐藏侧边栏）
      goToRepos();
      return;
    }
    const parts = val.split('/');
    localStorage.setItem('currentRepo', JSON.stringify({fullName: val, owner: parts[0], name: parts[1]}));
    nav('dash');
  }

  // 点击 Grape Logo → 返回仓库选择页（与解除托管后跳转的页面完全一致：全屏卡片页）
  function goToRepos() {
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    localStorage.setItem('sidebarTab', 'projects');
    $('sidebar').style.display = 'none';
    $('tb').style.display = 'none';
    const mv = $('mainView');
    mv.style.display = 'flex';
    mv.style.position = 'fixed';
    mv.style.inset = '0';
    mv.style.zIndex = '100';
    $('pageTitle').textContent = '选择仓库';
    API.get('/repos/member').then(d => {
      renderRepoSelection(d.repos || []);
    }).catch(e => {
      const pc = $('pageContent');
      if (pc) pc.innerHTML = '<div style="max-width:900px;margin:0 auto;padding:40px 0"><div class="error">加载失败: ' + e.message + '</div></div>';
    });
  }

  let _refreshTimer = null;
  function nav(view, param) {
    const repo = currentRepo();
    if (!repo && view !== 'repos' && view !== 'settings' && view !== 'tasks') { loadRepos(); return; }

    document.querySelectorAll('.sb-nav a').forEach(a => a.classList.remove('act'));
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

    if (repo) {
      repoTitle.innerHTML = `<a href="https://gitcode.com/${repo.fullName || (repo.owner + '/' + repo.name)}" target="_blank" rel="noopener" style="color:var(--a);font-weight:500;text-decoration:none" title="在 GitCode 打开 ${repo.fullName}">${repo.fullName} ↗</a>`;
    } else {
      repoTitle.textContent = '';
    }

    if (view === 'dash') {
      title.textContent = 'Dashboard';
      const loadDash = () => Promise.all([
        API.get('/dashboard/' + repo.owner + '/' + repo.name),
        API.get('/scenes'),
      ]).then(([dashData, scenesData]) => {
        dashData._scenes = scenesData.scenes || [];
        dashData._owner = repo.owner;
        dashData._name = repo.name;
        renderDashboard(dashData);
      }).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
      loadDash();
      _refreshTimer = setInterval(loadDash, 30000);
    } else if (view === 'pipe') {
      title.textContent = 'Pipeline';
      API.get('/pipeline/' + repo.owner + '/' + repo.name).then(renderPipeline).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (['vod','issue','coding','qa','sre'].includes(view)) {
      title.textContent = (_AGENT_TYPES[view] && _AGENT_TYPES[view].display_name) || view;
      API.get('/agents/' + view + '/' + repo.owner + '/' + repo.name).then(d => renderAgent(view, d)).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'event') {
      title.textContent = 'Event Detail';
      API.get('/events/detail/' + param).then(renderEventDetail).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'chat') {
      title.textContent = 'Chat Agent';
      renderChat(param || null);
    } else if (view === 'tasks') {
      title.textContent = 'Task Center';
      renderTasks();
    } else if (view === 'settings') {
      title.textContent = 'General Settings';
      renderSettings(param || 'credentials');
    } else if (view === 'skills') {
      title.textContent = 'DevOps Skills';
      renderSkills();
    } else if (view === 'knowledge') {
      title.textContent = 'Project Knowledge';
      renderKnowledge();
    } else if (view === 'runtimes') {
      title.textContent = 'Agent Runtimes';
      API.get('/settings/agent-runtimes').then(renderRuntimes).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'repos') {
      title.textContent = '选择仓库';
      loadRepos();
    } else if (view === 'skill') {
      title.textContent = 'Skill Detail';
      const _cr = currentRepo();
      const _qs = _cr && _cr.fullName ? '?repo=' + encodeURIComponent(_cr.fullName) : '';
      API.get('/skills/by-id/' + encodeURIComponent(param) + _qs).then(renderSkillDetail).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
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
    // 保留 currentRepo（记录上次选择的仓库），下次登录恢复；只清登录态
    localStorage.removeItem('token'); localStorage.removeItem('user');
    showLogin();
  }

  function showToast(msg) {
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#101724;color:#fff;padding:10px 20px;border-radius:8px;font-size:12px;z-index:10000;box-shadow:0 4px 16px rgba(0,0,0,.2);white-space:pre-line;max-width:80vw';
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
      localStorage.setItem('app_cache_ver', '5');
      if (p.get('first_login') === '1') {
        localStorage.removeItem('currentRepo');
        // Immediately show repo selection layout, skip Dashboard flash
        $('sidebar').style.display = 'none';
        $('tb').style.display = 'none';
        $('mainView').style.display = 'flex';
        $('mainView').style.position = 'fixed';
        $('mainView').style.inset = '0';
        $('mainView').style.zIndex = '100';
        $('pageTitle').textContent = '选择仓库';
      }
      showApp();
      // Remove token from URL without reload
      if (window.history.replaceState) {
        window.history.replaceState(null, '', '/');
      }
      return;
    }

    // Cache version check - only clear if version mismatch AND not in OAuth flow
    if (localStorage.getItem('app_cache_ver') !== '5') {
      const oldRepo = localStorage.getItem('currentRepo');
      const oldToken = localStorage.getItem('token');
      const oldUser = localStorage.getItem('user');
      localStorage.clear();
      if (oldToken) localStorage.setItem('token', oldToken);
      if (oldUser) localStorage.setItem('user', oldUser);
      if (oldRepo) localStorage.setItem('currentRepo', oldRepo);
      localStorage.setItem('app_cache_ver', '5');
    }

    const token = localStorage.getItem('token');
    if (token) { showApp(); }
    else { showLogin(); }
  }

  return {nav, search, oauthLogin, togglePwdLogin, pwdLogin, logout, showToast, switchRepo, goToRepos, currentRepo, showLogin, init, loadRepos, initSidebarRepos, loadProfile};
})();
document.addEventListener('DOMContentLoaded', () => app.init());
