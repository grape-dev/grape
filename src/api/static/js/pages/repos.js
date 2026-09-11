// ====== 仓库选择页：宏观大堂（Lobby）风格 ======
let _projectsContent = '';
let _projectsRepos = [];

function renderSelectionShell(tab, innerHtml) {
  const d = document.getElementById('pageContent');
  if (!d) return;
  const active = tab || localStorage.getItem('sidebarTab') || 'projects';
  const tabCls = t => 'rs-tab' + (active === t ? ' rs-tab-act' : '');
  const _lu = localStorage.getItem('user') ? JSON.parse(localStorage.getItem('user')) : null;
  const user = (_lu && (_lu.nickname || _lu.username)) || 'User';
  const avatar = (user || 'U')[0].toUpperCase();
  const html = `<div class="lobby">
    <header class="lobby-head">
      <div class="lobby-brand">
        <img class="lobby-logo" src="/static/logo.png" alt="Grape">
        <span class="lobby-brand-name">Grape</span>
        <span class="lobby-brand-sub">${I18N.t('repos.brand_sub')}</span>
      </div>
      <nav class="lobby-tabs">
        <a class="${tabCls('projects')}" onclick="switchSidebarTab('projects')">${I18N.t('repos.tab.projects')}</a>
        <a class="${tabCls('templates')}" onclick="switchSidebarTab('templates')">${I18N.t('repos.tab.templates')}</a>
        <a class="${tabCls('workspace')}" onclick="switchSidebarTab('workspace')">${I18N.t('repos.tab.workspace')}</a>
      </nav>
      <div class="lobby-user" onclick="app.logout()" title="${I18N.t('repos.logout_title')}">
        <span class="lobby-avatar">${avatar}</span>
        <span class="lobby-user-name">${user}</span>
        <span class="lobby-out">↩</span>
      </div>
    </header>
    <main class="lobby-main">
      <div class="lobby-content" id="ptContent">${innerHtml || ''}</div>
    </main>
  </div>`;
  d.innerHTML = html;
  localStorage.setItem('sidebarTab', active);
}

function switchSidebarTab(tab) {
  if (tab === 'workspace') {
    API.get('/repos/member').then(d => {
      const repos = d.repos || [];
      const hosted = repos.filter(r => r.is_hosted);
      if (hosted.length > 0) {
        const first = hosted[0];
        selectRepo(first.full_name, first.owner, first.name, first.repo_key);
      } else {
        _renderWorkspaceEmpty();
      }
    }).catch(() => {
      switchSidebarTab('projects');
    });
  } else if (tab === 'templates') {
    renderTemplates();
  } else {
    if (_projectsContent) {
      renderSelectionShell('projects', _projectsContent);
    } else {
      renderRepos();
    }
  }
}

// 无托管仓库时的工作台空态引导
function _workspaceEmptyHtml() {
  return '<div class="ws-empty">' +
    '<div class="ws-empty-ic">🧭</div>' +
    '<h2>' + I18N.t('repos.lobby.ws_empty_title') + '</h2>' +
    '<p>' + I18N.t('repos.lobby.ws_empty_desc') + '</p>' +
    '<div class="ws-empty-actions">' +
      '<button class="ws-empty-btn2" onclick="switchSidebarTab(\'projects\')">' + I18N.t('repos.lobby.ws_go_host') + '</button>' +
    '</div>' +
  '</div>';
}

function _renderWorkspaceEmpty() {
  renderSelectionShell('workspace', _workspaceEmptyHtml());
}

// ====== 仓库选择主内容 ======
let _repoPage = 0;
const _REPO_PAGE_SIZE = 9;
let _projectSearchQuery = '';

function _searchRepos() {
  const allRepos = (_projectsRepos || []).slice().sort((a, b) => {
    const pa = a.is_hosted ? 2 : a.is_joined ? 1 : 0;
    const pb = b.is_hosted ? 2 : b.is_joined ? 1 : 0;
    if (pa !== pb) return pb - pa;
    // 同级内：新建时间越近越前（created_at 倒序）；无时间则按全名兜底
    const ca = a.created_at || '';
    const cb = b.created_at || '';
    if (ca && cb && ca !== cb) return ca < cb ? 1 : -1;
    if (ca && !cb) return -1;
    if (!ca && cb) return 1;
    return (a.full_name || '').localeCompare(b.full_name || '');
  });
  const query = _projectSearchQuery.toLowerCase().trim();
  const filteredRepos = query
    ? allRepos.filter(r => (r.full_name || '').toLowerCase().includes(query) || (r.owner || '').toLowerCase().includes(query))
    : allRepos;
  const pages = Math.max(1, Math.ceil(filteredRepos.length / _REPO_PAGE_SIZE));
  if (_repoPage >= pages) _repoPage = 0;
  const pageRepos = filteredRepos.slice(_repoPage * _REPO_PAGE_SIZE, (_repoPage + 1) * _REPO_PAGE_SIZE);
  return { allRepos, filteredRepos, pages, pageRepos };
}

function _buildProjectListHtml() {
  const { filteredRepos, pages, pageRepos } = _searchRepos();

  let pagerHtml = '';
  if (pages > 1) {
    pagerHtml = '<div class="project-pager">' +
      '<button' + (_repoPage === 0 ? ' disabled' : '') + ' onclick="goRepoPage(-1)">‹</button>' +
      '<span>' + (_repoPage + 1) + ' / ' + pages + '</span>' +
      '<button' + (_repoPage >= pages - 1 ? ' disabled' : '') + ' onclick="goRepoPage(1)">›</button>' +
    '</div>';
  }

  var cardsHtml = pageRepos.map(function(r) { return _buildProjectCard(r); }).join('');
  var addCardHtml = '<div class="project-card-add" onclick="openCreateRepoModal()">' +
    '<span class="pca-ic">＋</span>' +
    '<b>' + I18N.t('repos.lobby.new_project_btn') + '</b>' +
    '<span class="pca-tip">' + I18N.t('repos.lobby.new_project_tip') + '</span>' +
  '</div>';

  return addCardHtml + cardsHtml +
    (filteredRepos.length === 0 ? '<div class="lobby-empty" style="grid-column:1/-1;margin:0;border:none;background:none"><div class="lobby-empty-ic">🔭</div><h3>' + I18N.t('repos.lobby.empty_title') + '</h3><p>' + I18N.t('repos.lobby.empty_desc') + '</p></div>' : '') +
    pagerHtml;
}

function renderRepoSelection(repos) {
  const d = document.getElementById('pageContent');
  if (!d) return;
  _projectsRepos = repos || [];
  const _lu = localStorage.getItem('user') ? JSON.parse(localStorage.getItem('user')) : null;
  const user = (_lu && (_lu.nickname || _lu.username)) || 'User';
  const name = user.split('@')[0];
  const hostedCount = (_projectsRepos).filter(r => r.is_hosted).length;
  const total = _projectsRepos.length;

  var templatesHtml = _renderProjectTemplates();

  var inner = '<div class="project-page">' +
    '<div class="project-hero">' +
      '<div class="project-hero-welcome">' +
        '<span class="pw-icon">👋</span>' +
        '<div class="pw-text">' +
          '<span class="pw-title">' + I18N.t('repos.lobby.welcome_back', {name: user}) + '</span>' +
          '<span class="pw-desc">' + I18N.t('repos.lobby.welcome_tip') + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="project-stats">' +
        '<div class="project-stat"><b>' + total + '</b><span>' + I18N.t('repos.lobby.stat_projects') + '</span></div>' +
        '<div class="project-stat"><b>' + hostedCount + '</b><span>' + I18N.t('repos.lobby.stat_hosted') + '</span></div>' +
      '</div>' +
    '</div>' +
    '<div class="project-section">' +
      '<div class="project-section-header">' +
        '<div class="project-section-header-left"><h2>' + I18N.t('repos.lobby.my_projects') + '</h2></div>' +
        '<div class="project-section-header-right">' +
          '<button class="project-refresh-btn" onclick="refreshRepos(this)" title="' + I18N.t('repos.lobby.refresh_title') + '">' + I18N.t('repos.lobby.refresh_btn') + '</button>' +
          '<input class="project-search" id="projectSearch" type="text" placeholder="🔍 ' + I18N.t('repos.lobby.search_placeholder') + '" value="' + _projectSearchQuery + '" oninput="onProjectSearch(this.value)">' +
        '</div>' +
      '</div>' +
      '<div class="project-list" id="projectList">' + _buildProjectListHtml() + '</div>' +
    '</div>' +
    templatesHtml +
  '</div>';

  _projectsContent = inner;
  renderSelectionShell('projects', inner);
}

function onProjectSearch(val) {
  _projectSearchQuery = val;
  _repoPage = 0;
  const list = document.getElementById('projectList');
  if (list) {
    list.innerHTML = _buildProjectListHtml();
  } else {
    renderRepoSelection(_projectsRepos);
  }
}

function goRepoPage(dir) {
  const { pages } = _searchRepos();
  _repoPage = (_repoPage + dir + pages) % pages;
  const list = document.getElementById('projectList');
  if (list) {
    list.innerHTML = _buildProjectListHtml();
  } else {
    renderRepoSelection(_projectsRepos);
  }
}

function _repoUrl(r) {
  var rk = r.repo_key || '';
  var fn = r.full_name || (r.owner ? r.owner + '/' + r.name : '');
  if (rk.indexOf('github:') === 0) return 'https://github.com/' + fn;
  return 'https://gitcode.com/' + fn;
}
function _repoPlatform(r) {
  var rk = r.repo_key || '';
  if (rk.indexOf('github:') === 0) return 'GitHub';
  return 'GitCode';
}

function _repoPlatformIcon(r) {
  var rk = r.repo_key || '';
  if (rk.indexOf('github:') === 0)
    return '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" style="vertical-align:-2px"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>';
  return '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="vertical-align:-2px"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.53-.51-1.07-.78-1.59-.59-.95-1.27-1.76-2.12-2.18-.36-.16-.65-.28-.9-.34V13c0-1.1-.9-2-2-2h-2v2h2v2H9v2h2v2.93c3.95-.49 7-3.85 7-7.93 0-.45-.05-.89-.14-1.31.79-.36 1.34-1.15 1.34-2.07 0-1.26-1.02-2.28-2.28-2.28-.42 0-.81.12-1.15.31-1.28-1.1-2.94-1.78-4.77-1.78-1.55 0-2.99.48-4.19 1.29C7.5 5.5 9.5 4 12 4c4.42 0 8 3.58 8 8 0 3.85-2.73 7.07-6.1 7.39z"/></svg>';
}

function _buildProjectCard(r) {
  const repoUrl = _repoUrl(r);
  const initials = (r.name || '?').substring(0, 2).toUpperCase();
  const statusTxt = r.is_hosted ? I18N.t('repos.card.status_hosted') : I18N.t('repos.card.status_not_joined');
  const statusCls = r.is_hosted ? 'project-card-status-hosted' : 'project-card-status-none';
  const platformIcon = _repoPlatformIcon(r);
  const platformName = _repoPlatform(r);

  var actionHtml;
  if (r.is_hosted) {
    actionHtml = '<button class="project-card-btn project-card-btn-outline" onclick="event.stopPropagation();selectRepo(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\',\'' + (r.repo_key || '') + '\')">' + I18N.t('repos.card.enter_project') + '→</button>';
  } else if (r.can_host) {
    actionHtml = '<button class="project-card-btn project-card-btn-outline" onclick="event.stopPropagation();hostAndSelect(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\',\'' + (r.repo_key || '') + '\')">' + I18N.t('repos.card.join_and_host') + '→</button>';
  } else {
    actionHtml = '<button class="project-card-btn project-card-btn-outline" onclick="event.stopPropagation();hostNoPermission()">' + I18N.t('repos.card.join_and_host') + '→</button>';
  }

  return '<div class="project-card" onclick="window.open(\'' + repoUrl + '\',\'_blank\')">' +
    '<span class="project-card-ic">' + initials + '</span>' +
    '<div class="project-card-info">' +
      '<span class="project-card-name" title="' + (r.full_name || '').replace(/"/g, '&quot;') + '">' + (r.name || '') + '</span>' +
      '<span class="project-card-meta">' + platformIcon + ' ' + platformName + ' · ' + r.owner + ' · ' + r.role + '</span>' +
    '</div>' +
    '<span class="project-card-status ' + statusCls + '">' + statusTxt + '</span>' +
    '<div class="project-card-actions">' +
      actionHtml +
    '</div>' +
  '</div>';
}

function _renderProjectTemplates() {
  var tmpls = [
    { ic: '🚀', name: I18N.t('repos.template.hello_world'), desc: I18N.t('repos.template.hello_world_desc') },
    { ic: '🤖', name: I18N.t('repos.template.agent'), desc: I18N.t('repos.template.agent_desc') },
    { ic: '☁️', name: I18N.t('repos.template.cloud'), desc: I18N.t('repos.template.cloud_desc') },
    { ic: '🤖', name: I18N.t('repos.template.ai_office'), desc: I18N.t('repos.template.ai_office_desc') },
    { ic: '🔧', name: I18N.t('repos.template.iac'), desc: I18N.t('repos.template.iac_desc') },
  ];

  var cards = tmpls.map(function(t) {
    return '<div class="template-card" onclick="switchSidebarTab(\'templates\')">' +
      '<span class="template-card-ic">' + t.ic + '</span>' +
      '<div class="template-card-info">' +
        '<div class="template-card-name">' + t.name + '</div>' +
        '<div class="template-card-desc">' + t.desc + '</div>' +
      '</div>' +
    '</div>';
  }).join('');

  return '<div class="project-section">' +
    '<div class="project-section-header"><div class="project-section-header-left"><h2>' + I18N.t('repos.lobby.from_template') + '</h2></div></div>' +
    '<div class="template-grid">' + cards + '</div>' +
  '</div>';
}

// ====== 仓库管理页（侧边栏「＋ 新增仓库托管」）======
function renderRepos() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  renderSelectionShell('projects', '<div class="loading">' + I18N.t('repos.loading') + '</div>');

  API.get('/repos/member').then(d => {
    const allRepos = d.repos || [];
    const managedRepos = allRepos.filter(r => r.is_joined || r.is_hosted);
    const availableRepos = allRepos.filter(r => !r.is_joined && !r.is_hosted);

    let html = `<div class="flex fc jbtw" style="margin-bottom:20px">
      <h2 style="font-size:15px;font-weight:600">${I18N.t('repos.repositories')}</h2>
      <button class="bt bt-p bt-s" onclick="syncRepos()">↻ ${I18N.t('repos.sync_from_gitcode')}</button>
    </div>`;

    html += `<div class="cd"><div class="cd-h"><h2>${I18N.t('repos.hosted')} <span class="c">${managedRepos.length}</span></h2></div>`;
    if (managedRepos.length === 0) {
      html += '<div class="ts t2" style="padding:12px 0">' + I18N.t('repos.managed.no_hosted') + '</div>';
    } else {
      managedRepos.forEach(r => {
        const statusClass = r.is_hosted ? 'tg-s' : 'tg-d';
        const statusText = r.is_hosted ? 'Active' : 'Joined';
        const platformIcon = _repoPlatformIcon(r);
        const platformName = _repoPlatform(r);
        html += `<div class="repo-item" style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--b)">
          ${platformIcon} <span class="ts t3">${platformName}</span>
          <span class="tg tg-c" style="font-size:11px;padding:3px 8px;cursor:pointer" onclick="selectRepo('${r.full_name}','${r.owner}','${r.name}','${r.repo_key || ''}')">${r.full_name}</span>
          <span class="ts t2">${r.role}</span>
          <span class="ts t3">${r.is_hosted ? 'Webhook OK' : ''} · ★ ${r.name}</span>
          <span class="tg ${statusClass}" style="margin-left:auto">${statusText}</span>
          <button class="bt bt-o bt-s" onclick="event.stopPropagation();unhostRepo('${r.full_name}',event)" style="color:var(--r);border-color:var(--r)">${I18N.t('repos.managed.unhost_btn')}</button>
        </div>`;
      });
    }
    html += `</div>`;

    html += `<div class="cd"><div class="cd-h"><h2>${I18N.t('repos.unhosted_title')} <span class="c">${availableRepos.length}</span></h2></div>`;
    if (availableRepos.length === 0) {
      html += '<div class="ts t2" style="padding:12px 0">' + I18N.t('repos.managed.no_available') + '</div>';
    } else {
      html += `<div class="tw"><table>
        <tr><th>${I18N.t('repos.managed.th_repository')}</th><th>Platform</th><th>${I18N.t('repos.managed.th_role')}</th><th>${I18N.t('repos.managed.th_description')}</th><th></th></tr>`;
      availableRepos.forEach(r => {
        const roleTag = r.role === 'Admin' ? `<span class="tg tg-s">${r.role}</span>` :
                        r.role === 'Member' ? `<span class="tg tg-d">${r.role}</span>` : r.role;
        const btnHtml = r.can_host
          ? `<button class="bt bt-p bt-s" onclick="event.stopPropagation();hostAndSelect('${r.full_name}','${r.owner}','${r.name}')">${I18N.t('repos.available_btn.join_and_host')}</button>`
          : `<button class="bt bt-o bt-s" onclick="event.stopPropagation();hostNoPermission()">${I18N.t('repos.available_btn.join_and_host')}</button>`;
        html += `<tr><td><b>${r.full_name}</b></td><td>${_repoPlatformIcon(r)} ${_repoPlatform(r)}</td><td>${roleTag}</td><td>${r.description || ''}</td><td>${btnHtml}</td></tr>`;
      });
      html += `</table></div>`;
    }
    html += `</div>`;

    _projectsContent = html;
    if (localStorage.getItem('sidebarTab') !== 'projects') return;
    const pc = document.getElementById('ptContent');
    if (pc) pc.innerHTML = html;
  }).catch(() => {
    if (localStorage.getItem('sidebarTab') !== 'projects') return;
    const pc = document.getElementById('ptContent');
    if (pc) pc.innerHTML = '<div class="error">' + I18N.t('repos.load_failed') + '</div>';
  });
}

// ====== 操作函数 ======
function syncRepos() {
  API.post('/repos/sync').then(d => {
    app.showToast(I18N.t('repos.sync.sync_success'));
    renderRepos();
  }).catch(e => app.showToast(I18N.t('repos.sync.sync_failed', {msg: e.message})));
}

function refreshRepos(btn) {
  if (btn) { btn.disabled = true; btn.textContent = I18N.t('repos.sync.syncing'); }
  API.post('/repos/sync').then(d => {
    const repos = (d.repos || []);
    _repoPage = 0;
    renderRepoSelection(repos);
    app.showToast(I18N.t('repos.sync.refresh_success'));
  }).catch(e => {
    app.showToast(I18N.t('repos.sync.refresh_failed', {msg: (e.message || e)}));
    if (btn) { btn.disabled = false; btn.textContent = I18N.t('repos.sync.refresh'); }
  });
}

function hostAndSelect(fullName, owner, name, repoKey) {
  API.post('/repos/host', {full_name: fullName}).then(r => {
    // GitHub 仓库托管后检查 bot collaborator 状态
    if (r.bot_collaborator_needed) {
      _showBotCollaboratorPrompt(r, fullName, owner, name, repoKey);
    } else {
      app.showToast(I18N.t('repos.sync.host_success'));
      app.loadProfile().then(() => selectRepo(fullName, owner, name, repoKey));
    }
  }).catch(e => app.showToast(I18N.t('repos.sync.host_failed', {msg: e.message})));
}

function _showBotCollaboratorPrompt(resp, fullName, owner, name, repoKey) {
  var botUser = resp.bot_username || '';
  var status = resp.bot_status || '';
  var inviteUrl = resp.bot_invite_url || '';
  var title, message;

  if (status === 'not_configured') {
    title = I18N.t('repos.bot.title') || 'Bot 未配置';
    message = I18N.t('repos.bot.not_configured') || 'GitHub Bot 未配置，请联系管理员在 global-bot-token.json 中设置 github_bot_token 和 github_bot_username。仓库已托管，但 Agent 自动操作可能受限。';
  } else if (status === 'invite_pending') {
    title = I18N.t('repos.bot.title') || '需要添加 Bot 为维护者';
    message = (I18N.t('repos.bot.invite_pending') || 'Bot {{bot}} 已被邀请加入仓库，但邀请尚未被接受。\n请前往 GitHub 仓库 Settings → Collaborators 接受邀请，或手动将 {{bot}} 添加为维护者。')
      .replace(/\{\{bot\}\}/g, botUser);
  } else if (status === 'add_failed') {
    title = I18N.t('repos.bot.title') || '需要手动添加 Bot';
    message = (I18N.t('repos.bot.add_failed') || '自动添加 Bot {{bot}} 为仓库维护者失败。\n请前往 GitHub 仓库 Settings → Collaborators 手动添加 {{bot}} 为维护者（Maintainer）。')
      .replace(/\{\{bot\}\}/g, botUser);
  } else if (status === 'insufficient_permission') {
    title = I18N.t('repos.bot.title') || 'Bot 权限不足';
    message = (I18N.t('repos.bot.insufficient') || 'Bot {{bot}} 已加入仓库但权限不足。\n请在 GitHub 仓库 Settings → Collaborators 中将 {{bot}} 的权限设为 Maintainer 或 Admin。')
      .replace(/\{\{bot\}\}/g, botUser);
  } else {
    title = I18N.t('repos.bot.title') || '需要添加 Bot 为维护者';
    message = (I18N.t('repos.bot.default') || '请前往 GitHub 仓库将 Bot {{bot}} 添加为维护者，确保 Agent 可以正常操作仓库。')
      .replace(/\{\{bot\}\}/g, botUser);
  }

  if (window._grapeConfirm) {
    window._grapeConfirm({
      title: title,
      message: message,
      confirmText: I18N.t('repos.bot.go_github') || '前往 GitHub 设置',
      cancelText: I18N.t('repos.bot.enter_repo') || '先进入仓库',
      onConfirm: function() {
        // 只打开 GitHub 设置页，不自动进入仓库
        // 用户在 GitHub 改完权限后回来重新托管或刷新
        if (inviteUrl) window.open(inviteUrl, '_blank');
        app.showToast(I18N.t('repos.bot.come_back') || '在 GitHub 设置完成后，回来重新托管或刷新页面');
      },
      onCancel: function() {
        // 用户选择先进入仓库（bot 权限后续再设置）
        app.loadProfile().then(() => selectRepo(fullName, owner, name, repoKey));
      }
    });
  } else {
    // 兜底：alert + 进入仓库
    alert(title + '\n\n' + message);
    if (inviteUrl) window.open(inviteUrl, '_blank');
    app.loadProfile().then(() => selectRepo(fullName, owner, name, repoKey));
  }
}

function hostNoPermission() {
  app.showToast(I18N.t('repos.card.no_permission_host'));
}

function selectRepo(fullName, owner, name, repoKey) {
  localStorage.setItem('currentRepo', JSON.stringify({fullName, owner, name, repoKey: repoKey || ''}));
  const sidebar = document.getElementById('sidebar');
  const mainView = document.getElementById('mainView');
  const tb = document.getElementById('tb');
  const title = document.getElementById('pageTitle');
  if (mainView) {
    mainView.style.position = '';
    mainView.style.inset = '';
    mainView.style.zIndex = '';
    mainView.style.display = 'flex';
  }
  if (sidebar) sidebar.style.display = 'flex';
  if (tb) tb.style.display = 'flex';
  if (title) title.textContent = I18N.t('app.nav.dashboard');
  app.initSidebarRepos();
  app.nav('dash');
}

function unhostRepo(fullName, event) {
  if (window._grapeConfirm) {
    _grapeConfirm({
      title: I18N.t('repos.unhost.confirm_title') || '解除托管',
      message: I18N.t('repos.unhost.confirm', {name: fullName}),
      confirmText: I18N.t('repos.unhost.confirm_btn') || '解除托管',
      danger: true,
      onConfirm: function() { _doUnhost(fullName); }
    }, event);
  } else {
    if (!confirm(I18N.t('repos.unhost.confirm', {name: fullName}))) return;
    _doUnhost(fullName);
  }
}

function _doUnhost(fullName) {
  API.post('/repos/remove', {full_name: fullName}).then(r => {
    app.showToast(I18N.t('repos.unhost.done'));
    app.loadProfile().then(() => {
      const cur = app.currentRepo();
      const wasCurrent = cur && cur.fullName === fullName;
      if (wasCurrent) localStorage.removeItem('currentRepo');
      app.initSidebarRepos();
      API.get('/repos/managed').then(md => {
        const remaining = md.repos || [];
        if (wasCurrent || remaining.length === 0) {
          // 当前仓库被解除或已无托管仓库 → 回到仓库选择页
          API.get('/repos/member').then(d => {
            const repos = d.repos || [];
            document.getElementById('sidebar').style.display = 'none';
            document.getElementById('tb').style.display = 'none';
            const mv = document.getElementById('mainView');
            mv.style.display = 'flex';
            mv.style.position = 'fixed';
            mv.style.inset = '0';
            mv.style.zIndex = '100';
            document.getElementById('pageTitle').textContent = I18N.t('repos.unhost.select_repo');
            renderRepoSelection(repos);
          });
        } else {
          // 还有其他托管仓库 → 刷新仓库管理页
          document.getElementById('pageTitle').textContent = I18N.t('repos.unhost.select_repo');
          renderRepos();
        }
      });
    });
  }).catch(e => app.showToast(I18N.t('repos.unhost.failed', {msg: e.message})));
}

// ====== 创建新项目弹窗 ======
function openCreateRepoModal() {
  closeCreateRepoModal();
  const modal = document.createElement('div');
  modal.id = 'createRepoModal';
  modal.className = 'create-repo-overlay';
  modal.innerHTML = `<div class="create-repo-modal">
    <div class="create-repo-head">
      <div class="create-repo-title">
        <span class="create-repo-ic">＋</span>
        <div>
          <h3>${I18N.t('repos.create.title')}</h3>
          <p>${I18N.t('repos.create.desc')}</p>
        </div>
      </div>
      <button class="create-repo-x" onclick="closeCreateRepoModal()">✕</button>
    </div>
    <form class="create-repo-form" onsubmit="submitCreateRepo(event)">
      <div class="create-repo-field">
        <label for="crName">${I18N.t('repos.create.name_label')} <span class="cr-req">*</span></label>
        <input id="crName" type="text" placeholder="${I18N.t('repos.create.name_placeholder')}" autocomplete="off" maxlength="64" required>
        <p class="cr-hint">${I18N.t('repos.create.name_hint')}</p>
      </div>
      <div class="create-repo-field">
        <label for="crDesc">${I18N.t('repos.create.desc_label')}</label>
        <textarea id="crDesc" rows="3" placeholder="${I18N.t('repos.create.desc_placeholder')}" maxlength="255"></textarea>
      </div>
      <div class="create-repo-field">
        <label>${I18N.t('repos.create.vis_label')}</label>
        <div class="create-repo-vis">
          <button type="button" class="cr-vis-btn cr-vis-act" data-vis="private" onclick="selectVisibility(this)"><span class="cr-vis-ic">🔒</span><div><b>${I18N.t('repos.create.vis_private')}</b><em>${I18N.t('repos.create.vis_private_desc')}</em></div></button>
          <button type="button" class="cr-vis-btn" data-vis="public" onclick="selectVisibility(this)"><span class="cr-vis-ic">🌐</span><div><b>${I18N.t('repos.create.vis_public')}</b><em>${I18N.t('repos.create.vis_public_desc')}</em></div></button>
        </div>
      </div>
      <div class="create-repo-actions">
        <button type="button" class="cr-btn cr-btn-ghost" onclick="closeCreateRepoModal()">${I18N.t('repos.create.btn_cancel')}</button>
        <button type="submit" class="cr-btn cr-btn-primary" id="crSubmit">
          <span class="cr-btn-txt">${I18N.t('repos.create.btn_create')}</span>
          <span class="cr-btn-loading" style="display:none">${I18N.t('repos.create.btn_creating')}</span>
        </button>
      </div>
    </form>
  </div>`;
  document.body.appendChild(modal);
  document.body.style.overflow = 'hidden';
  const input = document.getElementById('crName');
  if (input) setTimeout(() => input.focus(), 60);
}

function closeCreateRepoModal() {
  const m = document.getElementById('createRepoModal');
  if (m) m.remove();
  document.body.style.overflow = '';
}

function selectVisibility(btn) {
  document.querySelectorAll('.cr-vis-btn').forEach(b => b.classList.remove('cr-vis-act'));
  btn.classList.add('cr-vis-act');
}

function submitCreateRepo(e) {
  e.preventDefault();
  const name = (document.getElementById('crName').value || '').trim();
  if (!name) return app.showToast(I18N.t('repos.create.err_no_name'));
  const desc = document.getElementById('crDesc').value.trim();
  const visBtn = document.querySelector('.cr-vis-btn.cr-vis-act');
  const visibility = visBtn ? visBtn.getAttribute('data-vis') : 'private';

  const btn = document.getElementById('crSubmit');
  const txt = btn.querySelector('.cr-btn-txt');
  const ld = btn.querySelector('.cr-btn-loading');
  if (btn.disabled) return;
  btn.disabled = true;
  if (txt) txt.style.display = 'none';
  if (ld) ld.style.display = '';

  API.get('/repos/check-name?name=' + encodeURIComponent(name))
    .then(check => {
      if (check.exists) {
        btn.disabled = false;
        if (txt) txt.style.display = '';
        if (ld) ld.style.display = 'none';
        app.showToast(I18N.t('repos.create.err_exists', {name: check.full_name}));
        return;
      }
      API.post('/repos/create', {name, description: desc, visibility}, 120000)
        .then(r => {
          app.showToast(I18N.t('repos.create.success', {name: (r.full_name || name)}));
          closeCreateRepoModal();
          if (typeof _projectsRepos !== 'undefined') {
            API.get('/repos/member').then(d => {
              const repos = d.repos || [];
              renderRepoSelection(repos);
            }).catch(() => renderRepos());
          } else {
            renderRepos();
          }
        })
        .catch(err => {
          app.showToast(I18N.t('repos.create.err_create_failed', {msg: (err.message || err)}));
          btn.disabled = false;
          if (txt) txt.style.display = '';
          if (ld) ld.style.display = 'none';
        });
    })
    .catch(err => {
      btn.disabled = false;
      if (txt) txt.style.display = '';
      if (ld) ld.style.display = 'none';
      app.showToast(I18N.t('repos.create.err_check_failed', {msg: (err.message || err)}));
    });
}
