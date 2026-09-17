const app = (() => {
  const $ = id => document.getElementById(id);
  const VIEWS = ['dash','pipe','vod','issue','coding','qa','sre','chat','im-channel','settings','skills','knowledge','runtimes','event','repos','skill','tasks','observability','scene-market','scene-detail','my-feedback','my-feedback-detail'];

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
      // Issue #82: cachedRepos 路径也需 _currentView 守卫，与 API 回调路径一致——
      // 用户已进入某页面时不应覆盖布局 DOM，只更新侧边栏下拉
      if (window._currentView && window._currentView !== '') {
        initSidebarRepos();
      } else {
        renderReposFromData(cachedRepos);
      }
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
        // Issue #63: 异步回调里调 renderReposFromData 会操作布局 DOM
        // 如果 nav 已经执行了（_currentView 已设），说明用户已进入某页面，
        // 不应覆盖——只更新缓存，不渲染
        if (window._currentView && window._currentView !== '') {
          initSidebarRepos();
        } else {
          renderReposFromData(repos);
        }
      }
    }).catch(e => {
      if (cachedRepos) return; // 有缓存时静默，避免覆盖已渲染内容
      // Issue #63: loadRepos 失败时不隐藏侧边栏、不覆盖 pageContent
      // 无缓存时侧边栏保持默认状态，用户切 tab 时 nav() 会重新加载页面内容
      console.error('[loadRepos] FAILED:', e.name, e.message);
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
      $('pageTitle').textContent = I18N.t('app.current_project');
      renderRepoSelection(repos);
      return;
    }

    $('sidebar').style.display = 'flex';
    $('tb').style.display = 'flex';
    $('mainView').style.display = 'flex';
    $('mainView').style.position = '';
    $('mainView').style.inset = '';
    $('mainView').style.zIndex = '';
    $('pageTitle').textContent = I18N.t('app.current_project');

    // Issue #82: 仅在完全无选中仓库时才默认选第一个；
    // current 存在但不在缓存列表中时保留用户选择不覆写（避免刷新后跳变为列表首位）
    if (!current) {
      const r = managedRepos[0];
      if (r) localStorage.setItem('currentRepo', JSON.stringify({fullName: r.full_name, owner: r.owner, name: r.name, repoKey: r.repo_key || ''}));
      else localStorage.removeItem('currentRepo');
    }
    nav('dash');
  }

  function initSidebarRepos() {
    // Lightweight version for after repo selection - just populate sidebar dropdown
    API.get('/repos/managed').then(d => {
      const repos = d.repos || [];
      window._managedRepoMap = {};
      repos.forEach(r => { window._managedRepoMap[r.full_name] = r.repo_key || ''; });
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
    const _rk = (window._managedRepoMap && window._managedRepoMap[val]) || '';
    localStorage.setItem('currentRepo', JSON.stringify({fullName: val, owner: parts[0], name: parts[1], repoKey: _rk}));
    // 同步侧边栏下拉选中项
    const sel = $('repoSelect');
    if (sel) sel.value = val;
    nav('dash');
  }

  // 点击 Grape Logo → 返回仓库选择页（与解除托管后跳转的页面完全一致：全屏卡片页）
  // 折中方案：先叠轻量 loading 遮罩（不改变当前布局），API 返回后一次性切换 UI，避免中间态闪烁
  function goToRepos() {
    _pushHistory('repos', null);
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    localStorage.setItem('sidebarTab', 'projects');
    window._currentView = 'repos';

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
      if (window._currentView !== 'repos') return;  // stale guard
      // 数据就绪，一次性切换 UI
      if (ov) ov.remove();
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      const mv = $('mainView');
      mv.style.display = 'flex';
      mv.style.position = 'fixed';
      mv.style.inset = '0';
      mv.style.zIndex = '100';
      $('pageTitle').textContent = I18N.t('app.current_project');
      renderRepoSelection(d.repos || []);
    }).catch(e => {
      if (window._currentView !== 'repos') return;  // stale guard
      if (ov) ov.remove();
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      const mv = $('mainView');
      mv.style.display = 'flex';
      mv.style.position = 'fixed';
      mv.style.inset = '0';
      mv.style.zIndex = '100';
      $('pageTitle').textContent = I18N.t('app.current_project');
      const pc = $('pageContent');
      if (pc) pc.innerHTML = '<div style="max-width:900px;margin:0 auto;padding:40px 0"><div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div></div>';
    });
  }

  let _refreshTimer = null;
  let _currentParam = null;
  let _hiddenSince = 0;
  // 页面从后台回到前台时，若隐藏超过 30 秒才刷新（避免短时间切换标签页频繁刷新）
  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'hidden') {
      _hiddenSince = Date.now();
    } else if (document.visibilityState === 'visible' && window._currentView && _hiddenSince) {
      // Issue #86: Tab 切回前台——重设定时器（后台 setTimeout 被浏览器节流可能没触发）
      API.scheduleProactiveRefresh();
      if (Date.now() - _hiddenSince > 60000) {
        var _v = window._currentView || '';
        var _baseView = _v.split(':')[0];
        setTimeout(function() { if (typeof app !== 'undefined' && app.nav) app.nav(_baseView, window._currentParam); }, 300);
      }
      _hiddenSince = 0;
    }
  });
  // ── Sidebar 分组折叠（Issue #66）──
  // 可折叠分组标题带 class="grp-tog" + data-key（localStorage 键）。
  // 默认收起（HTML 中预置 collapsed）；initGroupCollapse() 读取 localStorage
  // 同步真实状态；toggleGroup() 点击切换并写回。收起时 .grp-items 子项隐藏。
  function _readCollapsed(key) {
    // 默认 true（收起）；localStorage 存 '0'/'1'
    try {
      const v = localStorage.getItem(key);
      return v === null ? true : v === '1';
    } catch (e) { return true; }
  }
  function initGroupCollapse() {
    document.querySelectorAll('.grp-tog[data-key]').forEach(tog => {
      const key = tog.getAttribute('data-key');
      const collapsed = _readCollapsed(key);
      tog.classList.toggle('collapsed', collapsed);
      const items = tog.nextElementSibling;
      if (items && items.classList.contains('grp-items')) {
        items.classList.toggle('hid', collapsed);
      }
    });
  }
  function toggleGroup(tog) {
    const key = tog.getAttribute('data-key');
    const collapsed = !tog.classList.contains('collapsed');
    tog.classList.toggle('collapsed', collapsed);
    const items = tog.nextElementSibling;
    if (items && items.classList.contains('grp-items')) {
      items.classList.toggle('hid', collapsed);
    }
    try { localStorage.setItem(key, collapsed ? '1' : '0'); } catch (e) {}
  }

  function nav(view, param) {
    // Issue #67: 已在 knowledge 页面且 Vue 应用存在时，不重复执行 nav（防止多次 mount 报错）
    if (view === 'knowledge' && window._currentView === 'knowledge' && typeof _knowledgeVueApp !== 'undefined' && _knowledgeVueApp) {
      return;
    }
    const repo = currentRepo();
    if (!repo && view !== 'repos' && view !== 'settings' && view !== 'tasks' && view !== 'runtimes' && view !== 'observability' && view !== 'scene-market' && view !== 'scene-detail' && view !== 'scene-detail-local' && view !== 'my-feedback' && view !== 'my-feedback-detail') { loadRepos(); return; }

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

    // 技能详情返回：记录来源视图（pushHistory 之前 history.state 还是上一个视图）
    if (view === 'skill') {
      const _prev = window.history.state;
      window._skillDetailFrom = (_prev && _prev.view) ? {view: _prev.view, param: _prev.param || null} : null;
    }

    window._currentParam = param;
    _pushHistory(view, param);

    document.querySelectorAll('.sb-nav a').forEach(a => a.classList.remove('act'));
    const navEl = $('nav-' + view);
    if (navEl) navEl.classList.add('act');
    // 场景·技能页面内的 tab 切换（skills/skill/scene-detail）也高亮场景·技能菜单
    if (!navEl && (view === 'skills' || view === 'skill' || view === 'scene-detail')) {
      const marketNav = $('nav-scene-market');
      if (marketNav) marketNav.classList.add('act');
    }
    document.querySelectorAll('.sb-nav a').forEach(a => {
      const o = a.getAttribute('onclick') || '';
      if (param && o.includes("'" + param + "'")) a.classList.add('act');
      else if (!param && o === "nav('" + view + "')") a.classList.add('act');
    });
    // 导航到折叠分组内的项时自动展开该分组（Issue #66）
    const _actNav = document.querySelector('.sb-nav a.act');
    if (_actNav) {
      const _items = _actNav.closest('.grp-items');
      if (_items && _items.classList.contains('hid')) {
        const _tog = _items.previousElementSibling;
        if (_tog && _tog.classList.contains('grp-tog') && _tog.classList.contains('collapsed')) {
          toggleGroup(_tog);
        }
      }
    }

    const title = $('pageTitle');
    const repoTitle = $('repoTitle');
    const pc = $('pageContent');

    // Issue #67: 离开 knowledge 页面时先卸载 Vue 应用（必须在 innerHTML 替换之前）
    if (view !== 'knowledge' && typeof _knowledgeVueApp !== 'undefined' && _knowledgeVueApp) {
      try { _knowledgeVueApp.unmount(); } catch(e) {}
      _knowledgeVueApp = null;
    }

    // Issue #67: 已在 knowledge 页面且 Vue 应用存在时，不覆盖 DOM（避免 Vue 虚拟 DOM 不一致）
    if (!(view === 'knowledge' && typeof _knowledgeVueApp !== 'undefined' && _knowledgeVueApp)) {
      pc.innerHTML = '<div class="loading">' + I18N.t('common.loading') + '</div>';
    }

    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    // Issue #63: 切 tab 时主动取消旧请求 + 设全局 signal + 停定时轮询
    if (window._navAbortController) window._navAbortController.abort();
    window._navAbortController = new AbortController();
    window._navSignal = window._navAbortController.signal;
    window._tabSignal = null;  // 重置 tab signal
    window._currentView = view;
    // 离开 repos 全屏页时恢复 sidebar/tb/mainView 默认布局
    if (view !== 'repos') {
      var sb = $('sidebar'); if (sb) sb.style.display = 'flex';
      var tb = $('tb'); if (tb) tb.style.display = 'flex';
      var mv = $('mainView'); if (mv) { mv.style.position = ''; mv.style.inset = ''; mv.style.zIndex = ''; }
    }
    // 离开需求中心时停止场景识别轮询
    if (view !== 'tasks' && typeof _stopScenePoll === 'function') _stopScenePoll();
    // 离开 observability 时停止定时轮询
    if (view !== 'observability' && typeof stopObsAuto === 'function') stopObsAuto();
    // 离开 chat 时停止飞书接入轮询
    if (view !== 'chat' && typeof _stopOnboardPoll === 'function') _stopOnboardPoll();
    // 离开场景中心时清除编辑标志（防止 _dashboardEditing 泄漏导致重新进入时卡在加载中）
    window._dashboardEditing = false;

    if (repo) {
      var _rk = repo.repoKey || repo.repo_key || '';
      var _fn = repo.fullName || (repo.owner + '/' + repo.name);
      var _isGithub = _rk.indexOf('github:') === 0;
      var _repoLink = _isGithub ? 'https://github.com/' + _fn : 'https://gitcode.com/' + _fn;
      var _logo = _isGithub
        ? '<svg width="14" height="14" viewBox="0 0 1024 1024" fill="currentColor" style="vertical-align:-2px;margin-right:5px;color:var(--t2)"><path d="M512 42.666667A464.64 464.64 0 0 0 42.666667 502.186667 460.373333 460.373333 0 0 0 363.52 938.666667c23.466667 4.266667 32-9.813333 32-22.186667v-78.08c-130.56 27.733333-158.293333-61.44-158.293333-61.44a122.026667 122.026667 0 0 0-52.053334-67.413333c-42.666667-28.16 3.413333-27.733333 3.413334-27.733334a98.56 98.56 0 0 1 71.68 47.36 101.12 101.12 0 0 0 136.533333 37.973334 99.413333 99.413333 0 0 1 29.866667-61.44c-104.106667-11.52-213.333333-50.773333-213.333334-226.986667a177.066667 177.066667 0 0 1 47.36-124.16 161.28 161.28 0 0 1 4.693334-121.173333s39.68-12.373333 128 46.933333a455.68 455.68 0 0 1 234.666666 0c89.6-59.306667 128-46.933333 128-46.933333a161.28 161.28 0 0 1 4.693334 121.173333A177.066667 177.066667 0 0 1 810.666667 477.866667c0 176.64-110.08 215.466667-213.333334 226.986666a106.666667 106.666667 0 0 1 32 85.333334v125.866666c0 14.933333 8.533333 26.88 32 22.186667A460.8 460.8 0 0 0 981.333333 502.186667 464.64 464.64 0 0 0 512 42.666667"/></svg>'
        : '<svg width="14" height="14" viewBox="0 0 1024 1024" fill="currentColor" style="vertical-align:-2px;margin-right:5px;color:var(--t2)"><path d="M472.746667 896.853333c-72.533333-8.106667-151.466667-39.253333-200.106667-78.08-27.306667-21.76-72.533333-71.253333-89.6-97.706666-51.626667-80.213333-72.533333-179.626667-57.173333-269.653334 15.36-86.613333 50.773333-154.453333 113.92-216.32 109.653333-107.52 285.44-141.653333 425.813333-82.346666 46.08 19.626667 90.453333 46.933333 108.8 66.986666 35.84 39.68 22.613333 104.106667-25.173333 128-20.48 9.813333-58.453333 10.24-72.96 0.853334-14.933333-9.386667-28.16-35.84-32-62.293334-1.706667-12.8-3.84-23.04-4.266667-23.04-0.853333 0-11.946667 5.973333-23.893333 13.226667-40.106667 23.466667-51.626667 26.88-113.066667 30.72-32.426667 1.706667-67.84 4.693333-78.933333 6.4-20.053333 2.56-44.373333-0.853333-83.2-13.226667l-20.48-6.4 0.853333 42.24c0.426667 40.96 0 43.093333-17.493333 79.36-22.613333 46.933333-30.72 75.52-34.56 117.333334-8.106667 100.693333 46.506667 175.36 150.613333 205.226666 67.413333 19.2 179.2 15.36 239.36-8.533333 58.026667-23.04 108.8-76.8 108.8-114.773333 0-15.786667-20.053333-35.84-41.813333-40.96-8.96-2.56-43.946667-5.546667-77.226667-6.826667-69.973333-2.986667-119.04-9.386667-132.266667-18.346667-13.653333-8.533333-20.48-33.28-14.506666-51.2 5.973333-19.2 15.786667-27.306667 43.946666-37.973333 19.2-7.253333 33.28-8.533333 88.746667-8.533333 118.613333 0 174.08 14.933333 217.6 58.453333 49.92 50.346667 55.466667 127.146667 13.226667 194.986667-62.293333 99.84-135.68 156.586667-235.52 182.613333-33.706667 8.96-122.453333 14.933333-158.72 10.666667z"/></svg>';
      repoTitle.innerHTML = `${_logo}<a href="${_repoLink}" target="_blank" rel="noopener" style="color:var(--a);font-weight:500;text-decoration:none" title="打开 ${_fn}">${repo.fullName} ↗</a>`;
    } else {
      repoTitle.textContent = '';
    }

    if (view === 'dash') {
      title.textContent = I18N.t('app.current_project');
      // 进入场景中心时无条件清除编辑标志（上一次操作异常退出时泄漏）
      window._dashboardEditing = false;
      const loadDash = () => Promise.all([
        API.get('/dashboard/' + repo.owner + '/' + repo.name),
        API.get('/scenes?repo=' + encodeURIComponent(repo.owner + '/' + repo.name)),
      ]).then(([dashData, scenesData]) => {
        if (window._currentView !== 'dash') return;  // stale guard
        // 编辑中不覆盖 UI，避免丢失用户正在进行的修改
        if (window._dashboardEditing) return;
        dashData._scenes = scenesData.scenes || [];
        dashData._owner = repo.owner;
        dashData._name = repo.name;
        renderDashboard(dashData);
      }).catch(e => {
        if (e.name === 'AbortError' && e.silent) return;  // Issue #63: abort 静默
        if (window._currentView !== 'dash') return;  // stale guard
        // 编辑中出错只 toast 不覆盖 UI；非编辑中清 loading 显示错误
        if (window._dashboardEditing) { app.showToast && app.showToast('刷新失败: ' + e.message); return; }
        pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>';
      });
      loadDash();
      _refreshTimer = setInterval(loadDash, 30000);
    } else if (view === 'pipe') {
      title.textContent = I18N.t('app.current_project');
      API.get('/pipeline/' + repo.owner + '/' + repo.name).then(d => { if (window._currentView !== 'pipe') return; renderPipeline(d, param); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== 'pipe') return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>'; });
    } else if (['vod','issue','coding','qa','sre'].includes(view)) {
      title.textContent = I18N.t('app.current_project');
      var _agentQs = param ? '?issue_number=' + encodeURIComponent(param) : '';
      API.get('/agents/' + view + '/' + repo.owner + '/' + repo.name + _agentQs).then(d => { if (window._currentView !== view) return; renderAgent(view, d); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== view) return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>'; });
    } else if (view === 'event') {
      // 路由入口清理：非 _jumpToNodeEvent 触发的进入（如全局搜索 / 直接路由）清除残留来源标志，
      // 防止上次任务详情遗留的 _eventFromTask 导致事件页错误显示「返回需求详情」按钮。
      if (!window._eventNavFromTask) {
        window._eventFromTask = null;
      }
      // 消费瞬态标志（仅本次跳转有效，后续任何再进入都需重新标记）
      window._eventNavFromTask = false;
      title.textContent = I18N.t('app.current_project');
      API.get('/events/detail/' + param).then(d => { if (window._currentView !== 'event') return; renderEventDetail(d); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== 'event') return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>'; });
      // 切换回事件详情时恢复活跃对话查询
      if (typeof resumeConvTask === 'function') setTimeout(resumeConvTask, 500);
    } else if (view === 'chat') {
      title.textContent = I18N.t('app.current_project');
      renderChat(param || null);
      // 切换回 Chat 页面时恢复活跃查询（不中断）
      if (typeof resumeChatTask === 'function') setTimeout(resumeChatTask, 500);
    } else if (view === 'im-channel') {
      title.textContent = I18N.t('app.nav.im_channel');
      renderImChannel();
    } else if (view === 'tasks') {
      title.textContent = I18N.t('app.current_project');
      renderTasks();
    } else if (view === 'settings') {
      title.textContent = I18N.t('app.current_project');
      renderSettings(param || 'credentials');
    } else if (view === 'my-feedback') {
      title.textContent = '我的反馈';
      if (typeof renderMyFeedback === 'function') {
        renderMyFeedback();
      } else {
        pc.innerHTML = '<div class="error">反馈页面加载失败，请刷新重试</div>';
      }
    } else if (view === 'my-feedback-detail') {
      title.textContent = '反馈详情';
      if (typeof renderMyFeedbackDetail === 'function') {
        renderMyFeedbackDetail(param);
      } else {
        pc.innerHTML = '<div class="error">详情页面加载失败，请刷新重试</div>';
      }
    } else if (view === 'skills') {
      title.textContent = I18N.t('app.current_project');
      renderSkills();
    } else if (view === 'knowledge') {
      title.textContent = I18N.t('app.current_project');
      if (typeof _knowledgeVueApp === 'undefined' || !_knowledgeVueApp) {
        renderKnowledge();
      }
    } else if (view === 'runtimes') {
      title.textContent = I18N.t('app.current_project');
      if (typeof renderRuntimes !== 'function') {
        // runtimes.js failed to load (stale cache) — force reload to fetch latest
        window.location.reload();
        return;
      }
      API.get('/settings/agent-runtimes').then(d => { if (window._currentView !== 'runtimes') return; renderRuntimes(d); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== 'runtimes') return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>'; });
    } else if (view === 'observability') {
      title.textContent = I18N.t('app.current_project');
      if (typeof renderObservability === 'function') renderObservability();
    } else if (view === 'repos') {
      title.textContent = I18N.t('app.current_project');
      goToRepos();
    } else if (view === 'skill') {
      title.textContent = I18N.t('app.current_project');
      const _cr = currentRepo();
      const _qs = _cr && _cr.fullName ? '?repo=' + encodeURIComponent(_cr.fullName) : '';
      API.get('/skills/by-id/' + encodeURIComponent(param) + _qs).then(d => { if (window._currentView !== 'skill') return; renderSkillDetail(d); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== 'skill') return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>'; });
    } else if (view === 'skill-sub') {
      // 技能包子技能详情（如 "38/huawei-ecs"）
      title.textContent = I18N.t('app.current_project');
      API.get('/skills/package-sub/' + param).then(d => { if (window._currentView !== 'skill-sub') return; renderSkillDetail(d); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== 'skill-sub') return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>'; });
    } else if (view === 'scene-market') {
      title.textContent = I18N.t('app.current_project');
      renderSceneMarket();
    } else if (view === 'scene-detail') {
      title.textContent = I18N.t('app.current_project');
      renderSceneDetail(param);
    } else if (view === 'scene-detail-local') {
      title.textContent = I18N.t('app.current_project');
      renderSceneDetailLocal(param);
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
          const noRepoViews = ['repos', 'settings', 'tasks', 'runtimes', 'event', 'scene-market', 'scene-detail', 'scene-detail-local', 'my-feedback', 'my-feedback-detail'];
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
  function oauthLoginGithub() {
    API.get('/auth/login/github').then(d => { if (d.redirect_url) window.location.href = d.redirect_url; }).catch(e => showToast(I18N.t('app.login.oauth_not_configured')));
  }

  // 内测申请表单
  function openBetaApply() {
    var isZh = I18N.getLang() === 'zh';
    var overlay = document.createElement('div');
    overlay.id = 'betaApplyModal';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;overflow-y:auto;padding:20px';
    var z = isZh;
    overlay.innerHTML = `
      <div style="background:var(--c,#fff);border-radius:16px;padding:32px;width:480px;max-width:90vw;box-shadow:0 12px 48px rgba(0,0,0,0.15);max-height:90vh;overflow-y:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
          <h2 style="font-size:20px;font-weight:600">${z?'申请内测':'Beta Apply'}</h2>
          <span onclick="document.getElementById('betaApplyModal').remove()" style="cursor:pointer;font-size:20px;color:var(--t3,#86868b);line-height:1">×</span>
        </div>
        <div style="font-size:13px;font-weight:600;color:var(--t2,#5b667b);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--b,#f0f1f5)">${z?'基础信息':'Basic Info'}</div>
        <div style="display:flex;flex-direction:column;gap:12px">
          <div>
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'平台 <span style="color:var(--r,#dc2828)">*</span>':'Platform <span style="color:var(--r,#dc2828)">*</span>'}</label>
            <select id="betaPlatform" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
              <option value="gitcode">GitCode</option>
              <option value="github">GitHub</option>
            </select>
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'用户名 <span style="color:var(--r,#dc2828)">*</span>':'Username <span style="color:var(--r,#dc2828)">*</span>'}</label>
            <input id="betaAccountName" type="text" placeholder="${z?'GitCode 或 GitHub 用户名':'GitCode or GitHub username'}" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'邮箱 <span style="color:var(--r,#dc2828)">*</span>':'Email <span style="color:var(--r,#dc2828)">*</span>'}</label>
            <input id="betaEmail" type="email" placeholder="user@example.com" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'手机号':'Phone'} <span style="font-size:10px;color:var(--t3,#a0a0a0)">(${z?'选填':'optional'})</span></label>
            <input id="betaPhone" type="text" placeholder="${z?'选填':'Optional'}" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
          </div>
        </div>
        <div style="font-size:13px;font-weight:600;color:var(--t2,#5b667b);margin:16px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--b,#f0f1f5)">${z?'身份选择':'Identity'}</div>
        <div>
          <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:8px">${z?'您的身份是？ <span style="color:var(--r,#dc2828)">*</span>':'Your identity? <span style="color:var(--r,#dc2828)">*</span>'}</label>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            <div class="ba-id-card" onclick="app.baSelectIdentity('student')" data-id="student" style="display:flex;align-items:center;gap:8px;padding:10px;border:2px solid var(--b,#e3e6ee);border-radius:10px;cursor:pointer;transition:all .15s"><span style="font-size:18px">🎓</span><div><div style="font-size:12px;font-weight:600;color:var(--t,#1d1d1f)">${z?'学生':'Student'}</div><div style="font-size:10px;color:var(--t3,#86868b)">${z?'在校学生':'In school'}</div></div></div>
            <div class="ba-id-card" onclick="app.baSelectIdentity('enterprise')" data-id="enterprise" style="display:flex;align-items:center;gap:8px;padding:10px;border:2px solid var(--b,#e3e6ee);border-radius:10px;cursor:pointer;transition:all .15s"><span style="font-size:18px">🏢</span><div><div style="font-size:12px;font-weight:600;color:var(--t,#1d1d1f)">${z?'企业':'Enterprise'}</div><div style="font-size:10px;color:var(--t3,#86868b)">${z?'企业团队':'Company team'}</div></div></div>
            <div class="ba-id-card" onclick="app.baSelectIdentity('opc')" data-id="opc" style="display:flex;align-items:center;gap:8px;padding:10px;border:2px solid var(--b,#e3e6ee);border-radius:10px;cursor:pointer;transition:all .15s"><span style="font-size:18px">💼</span><div><div style="font-size:12px;font-weight:600;color:var(--t,#1d1d1f)">OPC</div><div style="font-size:10px;color:var(--t3,#86868b)">${z?'独立开发者':'Independent'}</div></div></div>
            <div class="ba-id-card" onclick="app.baSelectIdentity('other')" data-id="other" style="display:flex;align-items:center;gap:8px;padding:10px;border:2px solid var(--b,#e3e6ee);border-radius:10px;cursor:pointer;transition:all .15s"><span style="font-size:18px">📋</span><div><div style="font-size:12px;font-weight:600;color:var(--t,#1d1d1f)">${z?'其他':'Other'}</div><div style="font-size:10px;color:var(--t3,#86868b)">${z?'其他身份':'Other identity'}</div></div></div>
          </div>
        </div>
        <div id="baBranchStudent" style="overflow:hidden;max-height:0;opacity:0;transition:all .3s">
          <div style="margin-top:12px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'学历 <span style="color:var(--r,#dc2828)">*</span>':'Education <span style="color:var(--r,#dc2828)">*</span>'}</label>
            <div id="baEdu" style="display:flex;flex-direction:column;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baEdu" value="${z?'本科生':'Undergraduate'}"> ${z?'本科生':'Undergraduate'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baEdu" value="${z?'硕士研究生':'Master'}"> ${z?'硕士研究生':'Master'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baEdu" value="${z?'博士研究生':'PhD'}"> ${z?'博士研究生':'PhD'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baEdu" value="${z?'其他':'Other'}" onchange="app.baToggleOther(this)"> ${z?'其他':'Other'} <input type="text" class="ba-other-text" placeholder="${z?'请输入':'Input'}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'专业方向 <span style="color:var(--r,#dc2828)">*</span>（多选）':'Major <span style="color:var(--r,#dc2828)">*</span> (multi)'}</label>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baMajor" value="${z?'计算机/软件工程':'CS/SE'}"> ${z?'计算机/软件工程':'CS/SE'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baMajor" value="${z?'人工智能':'AI'}"> ${z?'人工智能':'AI'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baMajor" value="${z?'数据科学':'Data Science'}"> ${z?'数据科学':'Data Science'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baMajor" value="${z?'电子/自动化':'EE/Automation'}"> ${z?'电子/自动化':'EE'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baMajor" value="${z?'其他理工科':'Other STEM'}"> ${z?'其他理工科':'Other STEM'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baMajor" value="${z?'经管/商科':'Business'}"> ${z?'经管/商科':'Business'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baMajor" value="${z?'其他':'Other'}" onchange="app.baToggleOther(this)"> ${z?'其他':'Other'} <input type="text" class="ba-other-text" placeholder="${z?'请输入':'Input'}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
        </div>
        <div id="baBranchEnterprise" style="overflow:hidden;max-height:0;opacity:0;transition:all .3s">
          <div style="margin-top:12px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'企业规模 <span style="color:var(--r,#dc2828)">*</span>':'Company Size <span style="color:var(--r,#dc2828)">*</span>'}</label>
            <div id="baSize" style="display:flex;flex-direction:column;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baSize" value="1-10人"> 1-10人</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baSize" value="11-50人"> 11-50人</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baSize" value="51-200人"> 51-200人</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baSize" value="201-1000人"> 201-1000人</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baSize" value="1000人以上"> 1000人+</label>
            </div>
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'角色 <span style="color:var(--r,#dc2828)">*</span>':'Role <span style="color:var(--r,#dc2828)">*</span>'}</label>
            <div id="baRole" style="display:flex;flex-direction:column;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baRole" value="${z?'开发人员':'Developer'}"> ${z?'开发人员':'Developer'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baRole" value="${z?'技术负责人/架构师':'Tech Lead'}"> ${z?'技术负责人/架构师':'Tech Lead'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baRole" value="${z?'产品经理':'PM'}"> ${z?'产品经理':'PM'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baRole" value="${z?'项目经理':'Project Manager'}"> ${z?'项目经理':'Project Manager'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baRole" value="${z?'管理人员':'Manager'}"> ${z?'管理人员':'Manager'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baRole" value="${z?'其他':'Other'}" onchange="app.baToggleOther(this)"> ${z?'其他':'Other'} <input type="text" class="ba-other-text" placeholder="${z?'请输入':'Input'}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'使用场景 <span style="color:var(--r,#dc2828)">*</span>（多选）':'Use Cases <span style="color:var(--r,#dc2828)">*</span> (multi)'}</label>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baEntCase" value="${z?'软件开发':'Software Dev'}"> ${z?'软件开发':'Software Dev'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baEntCase" value="${z?'AI Agent / Agent 开发':'AI Agent'}"> ${z?'AI Agent / Agent 开发':'AI Agent'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baEntCase" value="${z?'自动化办公':'Automation'}"> ${z?'自动化办公':'Automation'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baEntCase" value="${z?'企业内部工具':'Internal Tools'}"> ${z?'企业内部工具':'Internal Tools'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baEntCase" value="${z?'数据分析':'Data Analysis'}"> ${z?'数据分析':'Data Analysis'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baEntCase" value="${z?'运维 / DevOps':'DevOps'}"> ${z?'运维 / DevOps':'DevOps'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baEntCase" value="${z?'其他':'Other'}" onchange="app.baToggleOther(this)"> ${z?'其他':'Other'} <input type="text" class="ba-other-text" placeholder="${z?'请输入':'Input'}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
        </div>
        <div id="baBranchOpc" style="overflow:hidden;max-height:0;opacity:0;transition:all .3s">
          <div style="margin-top:12px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'主要从事 <span style="color:var(--r,#dc2828)">*</span>':'Occupation <span style="color:var(--r,#dc2828)">*</span>'}</label>
            <div id="baOcc" style="display:flex;flex-direction:column;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baOcc" value="${z?'软件/IT':'Software/IT'}"> ${z?'软件/IT':'Software/IT'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baOcc" value="AI/Agent"> AI/Agent</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baOcc" value="${z?'电商':'E-commerce'}"> ${z?'电商':'E-commerce'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baOcc" value="${z?'内容创作':'Content Creation'}"> ${z?'内容创作':'Content'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baOcc" value="SaaS"> SaaS</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baOcc" value="${z?'咨询/服务':'Consulting'}"> ${z?'咨询/服务':'Consulting'}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input type="radio" name="baOcc" value="${z?'其他':'Other'}" onchange="app.baToggleOther(this)"> ${z?'其他':'Other'} <input type="text" class="ba-other-text" placeholder="${z?'请输入':'Input'}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'解决问题 <span style="color:var(--r,#dc2828)">*</span>（多选）':'Use Cases <span style="color:var(--r,#dc2828)">*</span> (multi)'}</label>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOpcCase" value="${z?'产品开发':'Product Dev'}"> ${z?'产品开发':'Product Dev'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOpcCase" value="${z?'编程开发':'Coding'}"> ${z?'编程开发':'Coding'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOpcCase" value="${z?'Agent 开发':'Agent Dev'}"> ${z?'Agent 开发':'Agent Dev'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOpcCase" value="${z?'自动化任务':'Automation'}"> ${z?'自动化任务':'Automation'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOpcCase" value="${z?'内容生产':'Content'}"> ${z?'内容生产':'Content'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOpcCase" value="${z?'数据处理/分析':'Data'}"> ${z?'数据处理/分析':'Data'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOpcCase" value="${z?'运营':'Operations'}"> ${z?'运营':'Operations'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOpcCase" value="${z?'其他':'Other'}" onchange="app.baToggleOther(this)"> ${z?'其他':'Other'} <input type="text" class="ba-other-text" placeholder="${z?'请输入':'Input'}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
        </div>
        <div id="baBranchOther" style="overflow:hidden;max-height:0;opacity:0;transition:all .3s">
          <div style="margin-top:12px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'身份/职业描述 <span style="color:var(--r,#dc2828)">*</span>':'Description <span style="color:var(--r,#dc2828)">*</span>'}</label>
            <input id="baOtherDesc" type="text" placeholder="${z?'如：自由职业者、研究者':'e.g. Freelancer, Researcher'}" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${z?'使用场景 <span style="color:var(--r,#dc2828)">*</span>（多选）':'Use Cases <span style="color:var(--r,#dc2828)">*</span> (multi)'}</label>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOtherCase" value="${z?'学习':'Learning'}"> ${z?'学习':'Learning'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOtherCase" value="${z?'工作':'Work'}"> ${z?'工作':'Work'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOtherCase" value="${z?'编程':'Coding'}"> ${z?'编程':'Coding'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOtherCase" value="${z?'创业':'Startup'}"> ${z?'创业':'Startup'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOtherCase" value="${z?'内容创作':'Content'}"> ${z?'内容创作':'Content'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOtherCase" value="${z?'数据分析':'Data'}"> ${z?'数据分析':'Data'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOtherCase" value="${z?'自动化':'Automation'}"> ${z?'自动化':'Automation'}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input type="checkbox" class="baOtherCase" value="${z?'其他':'Other'}" onchange="app.baToggleOther(this)"> ${z?'其他':'Other'} <input type="text" class="ba-other-text" placeholder="${z?'请输入':'Input'}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
        </div>
        <div style="font-size:13px;font-weight:600;color:var(--t2,#5b667b);margin:16px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--b,#f0f1f5)">${z?'申请理由':'Reason'} <span style="font-size:10px;color:var(--t3,#a0a0a0)">(${z?'选填':'optional'})</span></div>
        <textarea id="betaReason" rows="2" placeholder="${z?'选填':'Optional'}" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none;resize:vertical;font-family:inherit"></textarea>
        <button id="betaSubmitBtn" onclick="app.submitBetaApply()" style="width:100%;padding:12px;border:none;border-radius:10px;background:linear-gradient(135deg,#3168e0,#4f46e5);color:#fff;font-size:14px;font-weight:600;cursor:pointer;margin-top:8px">${z?'提交申请':'Submit'}</button>
      </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
  }
  var _baIdentity = '';
  function baToggleOther(el) {
    var label = el.parentElement;
    var txt = label.querySelector('.ba-other-text');
    if (!txt) return;
    if (el.type === 'radio') {
      txt.style.display = el.checked && (el.value === '其他' || el.value === 'Other') ? 'inline-block' : 'none';
      if (el.value !== '其他' && el.value !== 'Other') txt.value = '';
    } else {
      txt.style.display = el.checked ? 'inline-block' : 'none';
      if (!el.checked) txt.value = '';
    }
  }
  function baSelectIdentity(type) {
    _baIdentity = type;
    var modal = document.getElementById('betaApplyModal');
    if (!modal) return;
    modal.querySelectorAll('.ba-id-card').forEach(function(c) {
      c.style.borderColor = c.dataset.id === type ? '#3168e0' : 'var(--b,#e3e6ee)';
      c.style.background = c.dataset.id === type ? 'rgba(49,104,224,0.04)' : 'transparent';
    });
    ['Student','Enterprise','Opc','Other'].forEach(function(s) {
      var el = document.getElementById('baBranch' + s);
      if (el) { el.style.maxHeight = '0'; el.style.opacity = '0'; }
    });
    var key = type.charAt(0).toUpperCase() + type.slice(1);
    var branch = document.getElementById('baBranch' + key);
    if (branch) { branch.style.maxHeight = '600px'; branch.style.opacity = '1'; }
  }
  function _baRadio(name) {
    var c = document.querySelector('input[name="' + name + '"]:checked');
    return c ? c.value : '';
  }
  function _baChecks(cls) {
    var vals = [];
    document.querySelectorAll('.' + cls + ':checked').forEach(function(cb) {
      if (cb.value === '其他' || cb.value === 'Other') {
        var t = cb.parentElement.querySelector('.ba-other-text');
        vals.push(t && t.value.trim() ? t.value.trim() : cb.value);
      } else { vals.push(cb.value); }
    });
    return vals;
  }
  function submitBetaApply() {
    var isZh = I18N.getLang() === 'zh';
    var accountType = document.getElementById('betaPlatform').value;
    var accountName = document.getElementById('betaAccountName').value.trim();
    var email = document.getElementById('betaEmail').value.trim();
    var phone = (document.getElementById('betaPhone')||{}).value ? document.getElementById('betaPhone').value.trim() : '';
    var reason = document.getElementById('betaReason').value.trim();
    if (!accountName) { showToast(isZh ? '请填写用户名' : 'Username is required'); return; }
    if (!email) { showToast(isZh ? '请填写邮箱' : 'Email is required'); return; }
    if (!/^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(email)) { showToast(isZh ? '邮箱格式不正确' : 'Invalid email'); return; }
    if (!_baIdentity) { showToast(isZh ? '请选择身份' : 'Please select identity'); return; }
    var survey = null;
    if (_baIdentity === 'student') {
      var edu = _baRadio('baEdu'), major = _baChecks('baMajor');
      if (edu === '其他' || edu === 'Other') { var _t = document.querySelector('input[name="baEdu"]:checked'); if (_t) { var _tt = _t.parentElement.querySelector('.ba-other-text'); edu = _tt && _tt.value.trim() ? _tt.value.trim() : ''; } if (!edu) { showToast(isZh ? '请输入学历' : 'Enter education'); return; } }
      if (!edu || major.length === 0) { showToast(isZh ? '请完成学历和专业方向' : 'Complete education & major'); return; }
      survey = { identity: 'student', education: edu, major: major };
    } else if (_baIdentity === 'enterprise') {
      var size = _baRadio('baSize'), role = _baRadio('baRole'), cases = _baChecks('baEntCase');
      if (role === '其他' || role === 'Other') { var _t = document.querySelector('input[name="baRole"]:checked'); if (_t) { var _tt = _t.parentElement.querySelector('.ba-other-text'); role = _tt && _tt.value.trim() ? _tt.value.trim() : ''; } if (!role) { showToast(isZh ? '请输入角色' : 'Enter role'); return; } }
      if (!size || !role || cases.length === 0) { showToast(isZh ? '请完成企业信息' : 'Complete company info'); return; }
      survey = { identity: 'enterprise', companySize: size, role: role, useCases: cases };
    } else if (_baIdentity === 'opc') {
      var occ = _baRadio('baOcc'), cases2 = _baChecks('baOpcCase');
      if (occ === '其他' || occ === 'Other') { var _t = document.querySelector('input[name="baOcc"]:checked'); if (_t) { var _tt = _t.parentElement.querySelector('.ba-other-text'); occ = _tt && _tt.value.trim() ? _tt.value.trim() : ''; } if (!occ) { showToast(isZh ? '请输入职业' : 'Enter occupation'); return; } }
      if (!occ || cases2.length === 0) { showToast(isZh ? '请完成OPC信息' : 'Complete OPC info'); return; }
      survey = { identity: 'opc', occupation: occ, useCases: cases2 };
    } else if (_baIdentity === 'other') {
      var desc = (document.getElementById('baOtherDesc')||{}).value ? document.getElementById('baOtherDesc').value.trim() : '';
      var cases3 = _baChecks('baOtherCase');
      if (!desc || cases3.length === 0) { showToast(isZh ? '请完成描述和场景' : 'Complete description & cases'); return; }
      survey = { identity: 'other', description: desc, useCases: cases3 };
    }
    var btn = document.getElementById('betaSubmitBtn');
    btn.disabled = true; btn.textContent = isZh ? '提交中...' : 'Submitting...';
    API.post('/settings/beta-apply/submit', {
      accountType: accountType, accountName: accountName, email: email,
      identityType: _baIdentity, phone: phone, reason: reason, surveyData: survey
    }).then(function(d) {
      if (d.success) {
        showToast(d.message || (isZh ? '申请已提交' : 'Submitted'));
        document.getElementById('betaApplyModal').remove();
      } else if (d.error) {
        showToast(d.error);
        btn.disabled = false; btn.textContent = isZh ? '提交申请' : 'Submit';
      } else {
        showToast(d.message || (isZh ? '申请已提交' : 'Submitted'));
        document.getElementById('betaApplyModal').remove();
      }
    }).catch(function(e) {
      showToast((isZh ? '提交失败: ' : 'Failed: ') + (e.message || e));
      btn.disabled = false; btn.textContent = isZh ? '提交申请' : 'Submit';
    });
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
      API.scheduleProactiveRefresh();  // Issue #86: 启动主动刷新定时器
      showApp();
    }).catch(e => showToast(I18N.t('app.login.failed') + ': ' + e.message));
  }

  function logout() {
    API.post('/auth/logout', {}).catch(() => {});
    // 清除所有用户相关缓存——防止切换账号后看到上一个用户的仓库/事件
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    localStorage.removeItem('currentRepo');
    localStorage.removeItem(REPO_CACHE_KEY);
    API.scheduleProactiveRefresh();  // Issue #86: token 已清——clearTimeout + return
    // refresh_token cookie 由服务端 logout 清除
    showLogin();
  }

  function showToast(msg, duration) {
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#101724;color:#fff;padding:10px 20px;border-radius:8px;font-size:12px;z-index:10000;box-shadow:0 4px 16px rgba(0,0,0,.2);white-space:pre-line;max-width:80vw';
    t.setAttribute('role', 'status');
    t.setAttribute('aria-live', 'polite');
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), duration || 3000);
  }

  // ── 通用确认对话框（Issue #125: 替换原生 confirm）──
  // 复用 .create-repo-overlay + .confirm-modal CSS，居中显示，防滚动
  function showConfirm(message, onConfirm, opts) {
    closeConfirm();
    var o = opts || {};
    var danger = !!o.danger;
    var confirmText = o.confirmText || (I18N.t('common.confirm') || '确认');
    var cancelText = o.cancelText || (I18N.t('common.cancel') || '取消');
    var overlay = document.createElement('div');
    overlay.id = 'grapeConfirmOverlay';
    overlay.className = 'create-repo-overlay';
    overlay.innerHTML = '<div class="confirm-modal">' +
      '<div class="confirm-modal-msg">' + message + '</div>' +
      '<div class="confirm-modal-actions">' +
        '<button class="cr-btn cr-btn-ghost" id="grapeConfirmCancel">' + cancelText + '</button>' +
        '<button class="cr-btn ' + (danger ? 'cr-btn-danger' : 'cr-btn-primary') + '" id="grapeConfirmOk">' + confirmText + '</button>' +
      '</div>' +
    '</div>';
    document.body.appendChild(overlay);
    document.body.style.overflow = 'hidden';
    function _close() { overlay.remove(); document.body.style.overflow = ''; }
    overlay.onclick = function(e) { if (e.target === overlay) _close(); };
    document.getElementById('grapeConfirmOk').onclick = function() { _close(); if (onConfirm) onConfirm(); };
    document.getElementById('grapeConfirmCancel').onclick = _close;
  }
  function closeConfirm() {
    var ex = document.getElementById('grapeConfirmOverlay');
    if (ex) { ex.remove(); document.body.style.overflow = ''; }
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
      body.innerHTML = '<iframe src="/static/help.html" title="Grape 使用帮助"></iframe>';
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
    // 同步可折叠分组的持久化状态（Issue #66）
    initGroupCollapse();
    // Check for OAuth token in URL first
    const p = new URLSearchParams(window.location.search);
    // Check for OAuth error in URL (e.g. github.com timeout)
    const oauthError = p.get('oauth_error');
    if (oauthError) {
      showToast(decodeURIComponent(oauthError), 6000);
      if (window.history.replaceState) {
        window.history.replaceState(null, '', window.location.pathname);
      }
    }

    // Check for OAuth token in URL first
    const urlToken = p.get('token');
    if (urlToken) {
      localStorage.setItem('token', urlToken);
      localStorage.setItem('app_cache_ver', '29');
      // 每次 OAuth 登录都清除上一个用户的仓库缓存+选择——防止切换账号后看到旧数据
      localStorage.removeItem(REPO_CACHE_KEY);
      localStorage.removeItem('currentRepo');
      if (p.get('first_login') === '1') {
        localStorage.removeItem('currentRepo');
        // Immediately show repo selection layout, skip Dashboard flash
        $('sidebar').style.display = 'none';
        $('tb').style.display = 'none';
        $('mainView').style.display = 'flex';
        $('mainView').style.position = 'fixed';
        $('mainView').style.inset = '0';
        $('mainView').style.zIndex = '100';
        $('pageTitle').textContent = I18N.t('app.current_project');
      }
      showApp();
      API.scheduleProactiveRefresh();  // Issue #86: OAuth 登录后启动定时器
      // Remove token from URL without reload (preserve language prefix)
      if (window.history.replaceState) {
        const lang = (typeof I18N !== 'undefined') ? I18N.getLang() : (localStorage.getItem('grapeLang') || 'zh');
        const prefix = lang === 'en' ? '/en' : '';
        window.history.replaceState(null, '', prefix + '/');
      }
      return;
    }

    // Cache version check - only clear if version mismatch AND not in OAuth flow
    if (localStorage.getItem('app_cache_ver') !== '29') {
      const oldToken = localStorage.getItem('token');
      const oldUser = localStorage.getItem('user');
      localStorage.clear();
      if (oldToken) localStorage.setItem('token', oldToken);
      if (oldUser) localStorage.setItem('user', oldUser);
      localStorage.setItem('app_cache_ver', '29');
    }

    const token = localStorage.getItem('token');
    if (token) {
      showApp();
      API.scheduleProactiveRefresh();  // Issue #86: 页面恢复时启动定时器
      // Handle hash-based navigation (e.g., #runtimes, #settings) on page load
      const hash = window.location.hash;
      if (hash && hash.length > 1) {
        const parts = hash.substring(1).split('/');
        const view = parts[0];
        const param = parts[1] ? decodeURIComponent(parts[1]) : undefined;
        // Views that don't require a repo
        const noRepoViews = ['repos', 'settings', 'tasks', 'runtimes', 'event', 'scene-market', 'scene-detail', 'my-feedback', 'my-feedback-detail'];
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

  return {nav, search, oauthLogin, oauthLoginGithub, openBetaApply, submitBetaApply, baSelectIdentity, baToggleOther, logout, showToast, openHelp, closeHelp, switchRepo, goToRepos, currentRepo, showLogin, init, loadRepos, initSidebarRepos, loadProfile, getTheme, getThemeMode, setTheme, setThemeMode, toggleGroup, showConfirm, closeConfirm};
})();

/** 根据 repo_key 生成 VCS 平台 URL（仓库/Issue 等通用）。 */
function _vcsUrl(repoKey, path) {
  var rk = repoKey || '';
  var host = rk.indexOf('github:') === 0 ? 'https://github.com' : 'https://gitcode.com';
  var parts = rk.split(':');
  var repoPath = parts.length > 1 ? parts[1] : rk;
  return host + '/' + repoPath + (path || '');
}
document.addEventListener('DOMContentLoaded', () => app.init());
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') app.closeHelp(); });

// Issue #67: 全局苹果风格确认 modal（knowledge.js / skills.js 共用）
function _showAppleConfirm(message, onConfirm, onCancel) {
  var modal = document.createElement('div');
  modal.id = 'appleConfirmModal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div class="cd" style="width:360px;padding:24px;border-radius:14px;text-align:center">' +
    '<p style="font-size:14px;color:var(--t);margin-bottom:20px;line-height:1.5">' + message + '</p>' +
    '<div style="display:flex;gap:10px">' +
    '<button id="appleConfirmCancel" style="flex:1;padding:10px 16px;font-size:13px;border:1px solid #d1d1d6;border-radius:10px;background:transparent;color:#1d1d1f;cursor:pointer">' + (I18N.t('common.cancel') || '取消') + '</button>' +
    '<button id="appleConfirmOk" style="flex:1;padding:10px 16px;font-size:13px;border:none;border-radius:10px;background:#ff3b30;color:#fff;cursor:pointer">' + (I18N.t('common.confirm') || '确认') + '</button>' +
    '</div></div>';
  document.body.appendChild(modal);
  modal.onclick = function(e) { if (e.target === modal) { modal.remove(); if (onCancel) onCancel(); } };
  document.getElementById('appleConfirmOk').onclick = function() { modal.remove(); if (onConfirm) onConfirm(); };
  document.getElementById('appleConfirmCancel').onclick = function() { modal.remove(); if (onCancel) onCancel(); };
}
