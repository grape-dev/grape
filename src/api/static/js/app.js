const app = (() => {
  const $ = id => document.getElementById(id);
  const VIEWS = ['dash','pipe','vod','issue','coding','qa','sre','chat','settings','skills','knowledge','runtimes','event','repos','skill','tasks','observability'];

  // ── 浏览器前进/后退支持 ──
  // nav()/goToRepos() 把视图写入 history（#view/param），popstate 回放渲染。
  // _replaying: popstate 回放时禁止再次入栈；_histReady: 首个视图用 replaceState，
  // 避免登录后第一次导航多出一个无意义的入口。
  let _replaying = false;
  let _histReady = false;
  function _pushHistory(view, param) {
    if (_replaying) return;
    const hash = '#' + view + (param !== undefined && param !== null ? '/' + encodeURIComponent(param) : '');
    if (window.location.hash === hash) return; // 同一视图不重复入栈
    try {
      const fn = _histReady ? 'pushState' : 'replaceState';
      if (window.history[fn]) {
        window.history[fn]({view: view, param: (param !== undefined && param !== null) ? param : null}, '', hash);
        _histReady = true;
      }
    } catch (e) {}
  }

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
      $('userRole').textContent = (d.managed_repos && d.managed_repos.length > 0) ? I18N.t('dashboard.admin') : I18N.t('dashboard.member');
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
      if (el) el.innerHTML = '<div style="max-width:900px;margin:0 auto;padding:40px 0"><div class="error">' + I18N.t('app.repo.load_failed') + '</div></div>';
    });
  }

  function renderReposFromData(repos) {
    const managedRepos = repos.filter(r => r.is_joined || r.is_hosted);
    console.log("[loadRepos] repos", JSON.stringify({total: repos.length, managed: managedRepos.length}));
    initSidebarRepos();
    const current = currentRepo();

    if (managedRepos.length === 0) {
      _pushHistory('repos', null);
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      $('mainView').style.display = 'flex';
      $('mainView').style.position = 'fixed';
      $('mainView').style.inset = '0';
      $('mainView').style.zIndex = '100';
      $('pageTitle').textContent = I18N.t('app.select_repo');
      renderRepoSelection(repos);
      return;
    }

    $('sidebar').style.display = 'flex';
    $('tb').style.display = 'flex';
    $('mainView').style.display = 'flex';
    $('mainView').style.position = '';
    $('mainView').style.inset = '';
    $('mainView').style.zIndex = '';
    $('pageTitle').textContent = I18N.t('app.nav.scene_center');

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
      ).join('') + '<option value="__add__">＋ ' + I18N.t('app.repo.add_new') + '</option>';
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
  // 折中方案：先叠轻量 loading 遮罩（不改变当前布局），API 返回后一次性切换 UI，避免中间态闪烁
  function goToRepos() {
    _pushHistory('repos', null);
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    localStorage.setItem('sidebarTab', 'projects');

    // 叠加轻量 loading 遮罩，不改动当前 DOM 布局
    let ov = document.getElementById('gotoReposOverlay');
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'gotoReposOverlay';
      ov.style.cssText = 'position:fixed;inset:0;z-index:9998;background:rgba(255,255,255,.45);display:flex;align-items:center;justify-content:center;font-size:13px;color:var(--t2)';
      ov.textContent = I18N.t('common.loading');
      document.body.appendChild(ov);
    }

    API.get('/repos/member').then(d => {
      // 数据就绪，一次性切换 UI
      if (ov) ov.remove();
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      const mv = $('mainView');
      mv.style.display = 'flex';
      mv.style.position = 'fixed';
      mv.style.inset = '0';
      mv.style.zIndex = '100';
      $('pageTitle').textContent = I18N.t('app.select_repo');
      renderRepoSelection(d.repos || []);
    }).catch(e => {
      if (ov) ov.remove();
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      const mv = $('mainView');
      mv.style.display = 'flex';
      mv.style.position = 'fixed';
      mv.style.inset = '0';
      mv.style.zIndex = '100';
      $('pageTitle').textContent = I18N.t('app.select_repo');
      const pc = $('pageContent');
      if (pc) pc.innerHTML = '<div style="max-width:900px;margin:0 auto;padding:40px 0"><div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div></div>';
    });
  }

  let _refreshTimer = null;
  function nav(view, param) {
    const repo = currentRepo();
    if (!repo && view !== 'repos' && view !== 'settings' && view !== 'tasks' && view !== 'runtimes' && view !== 'observability') { loadRepos(); return; }

    // v31: 离开 Chat 页面时中断 SSE（但不清任务状态，后台任务继续跑）
    if (view !== 'chat' && typeof _chatStreamAbort !== 'undefined' && _chatStreamAbort) {
      _chatStreamAbort.abort();
      _chatStreamAbort = null;
    }
    // 离开事件详情时中断对话 SSE（后台任务继续跑）
    if (view !== 'event' && typeof _convStreamAbort !== 'undefined' && _convStreamAbort) {
      _convStreamAbort.abort();
      _convStreamAbort = null;
    }

    _pushHistory(view, param);

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
    pc.innerHTML = '<div class="loading">' + I18N.t('common.loading') + '</div>';

    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    // 离开任务中心时停止场景识别轮询
    if (view !== 'tasks' && typeof _stopScenePoll === 'function') _stopScenePoll();

    if (repo) {
      repoTitle.innerHTML = `<a href="https://gitcode.com/${repo.fullName || (repo.owner + '/' + repo.name)}" target="_blank" rel="noopener" style="color:var(--a);font-weight:500;text-decoration:none" title="在 GitCode 打开 ${repo.fullName}">${repo.fullName} ↗</a>`;
    } else {
      repoTitle.textContent = '';
    }

    if (view === 'dash') {
      title.textContent = I18N.t('app.nav.scene_center');
      // 进入场景中心时清除可能残留的编辑标志（上一次操作异常退出时泄漏）
      if (!window._dashboardEditing) window._dashboardEditing = false;
      const loadDash = () => Promise.all([
        API.get('/dashboard/' + repo.owner + '/' + repo.name),
        API.get('/scenes'),
      ]).then(([dashData, scenesData]) => {
        // 编辑中不覆盖 UI，避免丢失用户正在进行的修改
        if (window._dashboardEditing) return;
        dashData._scenes = scenesData.scenes || [];
        dashData._owner = repo.owner;
        dashData._name = repo.name;
        renderDashboard(dashData);
      }).catch(e => {
        // 编辑中出错只 toast 不覆盖 UI；非编辑中清 loading 显示错误
        if (window._dashboardEditing) { app.showToast && app.showToast('刷新失败: ' + e.message); return; }
        pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>';
      });
      loadDash();
      _refreshTimer = setInterval(loadDash, 30000);
    } else if (view === 'pipe') {
      title.textContent = I18N.t('app.nav.pipeline');
      API.get('/pipeline/' + repo.owner + '/' + repo.name).then(renderPipeline).catch(e => pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>');
    } else if (['vod','issue','coding','qa','sre'].includes(view)) {
      title.textContent = (_AGENT_TYPES[view] && _AGENT_TYPES[view].display_name) ? (function(){var k='agents.type.'+view+'.name';var v=I18N.t(k);return v!==k?v:_AGENT_TYPES[view].display_name;})() : view;
      API.get('/agents/' + view + '/' + repo.owner + '/' + repo.name).then(d => renderAgent(view, d)).catch(e => pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>');
    } else if (view === 'event') {
      title.textContent = I18N.t('app.nav.event_detail');
      API.get('/events/detail/' + param).then(renderEventDetail).catch(e => pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>');
      // 切换回事件详情时恢复活跃对话查询
      if (typeof resumeConvTask === 'function') setTimeout(resumeConvTask, 500);
    } else if (view === 'chat') {
      title.textContent = I18N.t('app.nav.chat');
      renderChat(param || null);
      // 切换回 Chat 页面时恢复活跃查询（不中断）
      if (typeof resumeChatTask === 'function') setTimeout(resumeChatTask, 500);
    } else if (view === 'tasks') {
      title.textContent = I18N.t('app.nav.tasks');
      renderTasks();
    } else if (view === 'settings') {
      title.textContent = I18N.t('app.nav.settings');
      renderSettings(param || 'credentials');
    } else if (view === 'skills') {
      title.textContent = I18N.t('app.nav.skills');
      renderSkills();
    } else if (view === 'knowledge') {
      title.textContent = I18N.t('app.nav.knowledge');
      renderKnowledge();
    } else if (view === 'runtimes') {
      title.textContent = I18N.t('app.nav.runtimes');
      if (typeof renderRuntimes !== 'function') {
        // runtimes.js failed to load (stale cache) — force reload to fetch latest
        window.location.reload();
        return;
      }
      API.get('/settings/agent-runtimes').then(renderRuntimes).catch(e => pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>');
    } else if (view === 'observability') {
      title.textContent = I18N.t('app.nav.observability');
      if (typeof renderObservability === 'function') renderObservability();
    } else if (view === 'repos') {
      title.textContent = I18N.t('app.select_repo');
      loadRepos();
    } else if (view === 'skill') {
      title.textContent = I18N.t('app.nav.skill_detail');
      const _cr = currentRepo();
      const _qs = _cr && _cr.fullName ? '?repo=' + encodeURIComponent(_cr.fullName) : '';
      API.get('/skills/by-id/' + encodeURIComponent(param) + _qs).then(renderSkillDetail).catch(e => pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>');
    } else if (view === 'skill-sub') {
      // 技能包子技能详情（如 "38/huawei-ecs"）
      title.textContent = I18N.t('app.nav.sub_skill_detail');
      API.get('/skills/package-sub/' + param).then(renderSkillDetail).catch(e => pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>');
    }
  }

  // 浏览器回退/前进：按 history 状态回放视图（回放时不重复入栈）
  window.addEventListener('popstate', (e) => {
    const s = e.state;
    _replaying = true;
    try {
      if (s && s.view === 'repos') goToRepos();          // 全屏仓库选择页
      else if (s && s.view) nav(s.view, s.param);
      else {
        // No state — try hash-based navigation
        const hash = window.location.hash;
        if (hash && hash.length > 1) {
          const parts = hash.substring(1).split('/');
          const view = parts[0];
          const noRepoViews = ['repos', 'settings', 'tasks', 'runtimes', 'event'];
          if (noRepoViews.includes(view) || currentRepo()) {
            nav(view, parts[1] ? decodeURIComponent(parts[1]) : undefined);
          } else if (currentRepo()) {
            nav('dash');
          } else {
            goToRepos();
          }
        } else if (currentRepo()) {
          nav('dash');               // 回到入口：有仓库则默认 Dashboard
        } else {
          goToRepos();                                  // 无仓库则回到仓库选择
        }
      }
    } finally {
      _replaying = false;
    }
  });

  function search(q) {
    if (!q.trim()) return;
    const pc = $('pageContent');
    API.get('/search/?q=' + encodeURIComponent(q) + '&type=all').then(d => {
      let html = '<div class="cd"><div class="cd-h"><h2>' + I18N.t('app.search.title') + ': "' + q + '"</h2></div>';
      html += '<div class="ts t2">' + I18N.t('app.search.events') + ': ' + (d.events || []).length + ' / ' + I18N.t('app.search.repos') + ': ' + (d.repos || []).length + ' / ' + I18N.t('app.search.agents') + ': ' + (d.agents || []).length + '</div>';
      (d.events || []).forEach(ev => { html += '<div class="ai" onclick="app.nav(\'event\',' + ev.seq + ')" style="cursor:pointer"><div class="ac"><b>' + (ev.title || '') + '</b><span class="ts t3"> ' + (ev.repo_key || '') + '</span></div></div>'; });
      (d.agents || []).forEach(a => { html += '<div class="cl" onclick="app.nav(\'' + a.name + '\')"><div class="cx"><h4>' + a.display_name + '</h4></div></div>'; });
      html += '</div>';
      pc.innerHTML = html;
    }).catch(() => {});
  }

  function oauthLogin() {
    API.get('/auth/login/gitcode').then(d => { if (d.redirect_url) window.location.href = d.redirect_url; }).catch(e => showToast(I18N.t('app.login.oauth_not_configured')));
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
    if (!u || !p) return showToast(I18N.t('app.login.empty_input'));
    API.post('/auth/login', {username: u, password: p}).then(d => {
      localStorage.setItem('token', d.token);
      // refresh_token 在 HttpOnly cookie 里——不需要存 localStorage
      localStorage.setItem('user', JSON.stringify(d.user));
      showApp();
    }).catch(e => showToast(I18N.t('app.login.failed') + ': ' + e.message));
  }

  function logout() {
    API.post('/auth/logout', {}).catch(() => {});
    // 保留 currentRepo（记录上次选择的仓库），下次登录恢复；只清登录态
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    // refresh_token cookie 由服务端 logout 清除
    showLogin();
  }

  function showToast(msg, duration) {
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#101724;color:#fff;padding:10px 20px;border-radius:8px;font-size:12px;z-index:10000;box-shadow:0 4px 16px rgba(0,0,0,.2);white-space:pre-line;max-width:80vw';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), duration || 3000);
  }

  // ── 帮助抽屉 ──
  function openHelp() {
    const overlay = document.getElementById('helpOverlay');
    const drawer = document.getElementById('helpDrawer');
    if (!overlay || !drawer) return;
    overlay.classList.add('show');
    drawer.classList.add('show');
    // 动态加载 help.html 到 iframe（避免跨域，用同路径）
    const body = drawer.querySelector('.help-drawer-body');
    if (body && !body.querySelector('iframe')) {
      body.innerHTML = '<iframe src="/static/help.html" title="' + I18N.t('app.help.iframe_title') + '"></iframe>';
    }
  }
  function closeHelp() {
    const overlay = document.getElementById('helpOverlay');
    const drawer = document.getElementById('helpDrawer');
    if (overlay) overlay.classList.remove('show');
    if (drawer) drawer.classList.remove('show');
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
      localStorage.setItem('app_cache_ver', '7');
      if (p.get('first_login') === '1') {
        localStorage.removeItem('currentRepo');
        // Immediately show repo selection layout, skip Dashboard flash
        $('sidebar').style.display = 'none';
        $('tb').style.display = 'none';
        $('mainView').style.display = 'flex';
        $('mainView').style.position = 'fixed';
        $('mainView').style.inset = '0';
        $('mainView').style.zIndex = '100';
        $('pageTitle').textContent = I18N.t('app.select_repo');
      }
      showApp();
      // Remove token from URL without reload (preserve language prefix)
      if (window.history.replaceState) {
        const lang = (typeof I18N !== 'undefined') ? I18N.getLang() : (localStorage.getItem('grapeLang') || 'zh');
        const prefix = lang === 'en' ? '/en' : '';
        window.history.replaceState(null, '', prefix + '/');
      }
      return;
    }

    // Cache version check - only clear if version mismatch AND not in OAuth flow
    if (localStorage.getItem('app_cache_ver') !== '7') {
      const oldRepo = localStorage.getItem('currentRepo');
      const oldToken = localStorage.getItem('token');
      const oldUser = localStorage.getItem('user');
      localStorage.clear();
      if (oldToken) localStorage.setItem('token', oldToken);
      if (oldUser) localStorage.setItem('user', oldUser);
      if (oldRepo) localStorage.setItem('currentRepo', oldRepo);
      localStorage.setItem('app_cache_ver', '7');
    }

    const token = localStorage.getItem('token');
    if (token) {
      showApp();
      // Handle hash-based navigation (e.g., #runtimes, #settings) on page load
      const hash = window.location.hash;
      if (hash && hash.length > 1) {
        const parts = hash.substring(1).split('/');
        const view = parts[0];
        const param = parts[1] ? decodeURIComponent(parts[1]) : undefined;
        // Views that don't require a repo
        const noRepoViews = ['repos', 'settings', 'tasks', 'runtimes', 'event'];
        if (noRepoViews.includes(view) || currentRepo()) {
          nav(view, param);
        } else {
          nav('dash');
        }
      } else if (currentRepo()) {
        nav('dash');
      } else {
        // No repo selected and no hash — go to repo selection
        goToRepos();
      }
    } else { showLogin(); }
  }

  // ── Theme Management (Apple Developer Theme) ──
  // Theme: 'default' | 'apple'  Mode: 'light' | 'auto' (dark adapt)
  function getTheme() { return localStorage.getItem('grapeTheme') || 'apple'; }
  function getThemeMode() { return localStorage.getItem('grapeThemeMode') || 'light'; }

  function setTheme(theme) {
    localStorage.setItem('grapeTheme', theme);
    const html = document.documentElement;
    if (theme === 'apple') {
      html.setAttribute('data-theme', 'apple');
      // re-apply mode so dark-auto picks up
      setThemeMode(getThemeMode());
    } else {
      html.removeAttribute('data-theme');
      html.removeAttribute('data-theme-mode');
    }
    // dispatch event so pages can react if needed
    window.dispatchEvent(new CustomEvent('themechange', {detail: {theme}}));
  }

  function setThemeMode(mode) {
    localStorage.setItem('grapeThemeMode', mode);
    const html = document.documentElement;
    if (getTheme() === 'apple') {
      html.setAttribute('data-theme-mode', mode);
    }
  }

  return {nav, search, oauthLogin, togglePwdLogin, pwdLogin, logout, showToast, openHelp, closeHelp, switchRepo, goToRepos, currentRepo, showLogin, init, loadRepos, initSidebarRepos, loadProfile, getTheme, getThemeMode, setTheme, setThemeMode};
})();
document.addEventListener('DOMContentLoaded', () => app.init());
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') app.closeHelp(); });
