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
  if (tab === 'templates') {
    renderTemplates();
  } else {
    if (_projectsContent) {
      renderSelectionShell('projects', _projectsContent);
    } else {
      renderRepos();
    }
  }
}

// ====== 仓库选择主内容 ======
let _repoPage = 0;
const _REPO_PAGE_SIZE = 9;

function renderRepoSelection(repos) {
  const d = document.getElementById('pageContent');
  if (!d) return;
  _projectsRepos = repos || [];
  const _lu = localStorage.getItem('user') ? JSON.parse(localStorage.getItem('user')) : null;
  const user = (_lu && (_lu.nickname || _lu.username)) || 'User';
  const hostedCount = (repos || []).filter(r => r.is_hosted).length;
  const joinedCount = (repos || []).filter(r => r.is_joined && !r.is_hosted).length;
  const name = (user || I18N.t('repos.lobby.friend')).split('@')[0];

  // 已托管 > 已加入 > 未加入 排序（同等级按 full_name 稳定排序，保证分页不跳动）
  const allRepos = (repos || []).slice().sort((a, b) => {
    const pa = a.is_hosted ? 2 : a.is_joined ? 1 : 0;
    const pb = b.is_hosted ? 2 : b.is_joined ? 1 : 0;
    if (pa !== pb) return pb - pa;
    return (a.full_name || '').localeCompare(b.full_name || '');
  });
  const total = allRepos.length;
  const pages = Math.max(1, Math.ceil(total / _REPO_PAGE_SIZE));
  if (_repoPage >= pages) _repoPage = 0;
  const pageRepos = allRepos.slice(_repoPage * _REPO_PAGE_SIZE, (_repoPage + 1) * _REPO_PAGE_SIZE);

  let pagerHtml = '';
  if (pages > 1) {
    pagerHtml = `<div class="tmpl-pager">
      <button class="tmpl-page-btn" ${_repoPage === 0 ? 'disabled' : ''} onclick="goRepoPage(-1)">‹</button>
      <span class="tmpl-page-info">${_repoPage + 1} / ${pages}</span>
      <button class="tmpl-page-btn" ${_repoPage >= pages - 1 ? 'disabled' : ''} onclick="goRepoPage(1)">›</button>
    </div>`;
  }

  let inner = `<button class="lobby-refresh-btn" onclick="refreshRepos(this)" title="${I18N.t('repos.lobby.refresh_title')}">${I18N.t('repos.lobby.refresh_btn')}</button>
  <div class="lobby-hero">
    <div class="lobby-eyebrow"><i></i>WELCOME TO GRAPE<i></i></div>
    <h1 class="lobby-title">${I18N.t('repos.lobby.hello')}<span>${name}</span><span class="lobby-hello">👋</span></h1>
    <p class="lobby-sub">${I18N.t('repos.lobby.welcome_sub')}</p>
    <div class="lobby-stats-inline">
      <div class="lobby-stat"><span class="lobby-stat-ic">◧</span><b>${total}</b><span>${I18N.t('repos.lobby.stat_projects')}</span></div>
      <div class="lobby-stat"><span class="lobby-stat-ic">✓</span><b>${hostedCount}</b><span>${I18N.t('repos.lobby.stat_hosted')}</span></div>
      <div class="lobby-stat"><span class="lobby-stat-ic">◇</span><b>${joinedCount}</b><span>${I18N.t('repos.lobby.stat_joined')}</span></div>
    </div>
  </div>

  <div class="lobby-grid">
    ${pageRepos.map(r => _buildLobbyCard(r)).join('')}
  </div>
  ${pagerHtml}

  <div class="lobby-empty" id="rsEmpty" style="display:none">
    <div class="lobby-empty-ic">🔭</div>
    <h3>${I18N.t('repos.lobby.empty_title')}</h3>
    <p>${I18N.t('repos.lobby.empty_desc')}</p>
  </div>`;

  _projectsContent = inner;
  renderSelectionShell('projects', inner);
}

function goRepoPage(dir) {
  const total = (_projectsRepos || []).length;
  const pages = Math.max(1, Math.ceil(total / _REPO_PAGE_SIZE));
  _repoPage = (_repoPage + dir + pages) % pages;
  renderRepoSelection(_projectsRepos);
}

function _buildLobbyCard(r) {
  const isAdmin = r.role === 'Admin' || r.role === 'Maintainer';
  const enter = r.is_joined || r.is_hosted;
  // 卡片本体：点击跳转项目实际代码仓库（GitCode），不直接进入 Grape 工作区
  const repoUrl = 'https://gitcode.com/' + (r.full_name || '');
  const btnTxt = r.is_hosted ? I18N.t('repos.card.enter_project') : r.is_joined ? I18N.t('repos.card.enter_project') : isAdmin ? I18N.t('repos.card.join_and_host') : I18N.t('repos.card.join');
  const statusTxt = r.is_hosted ? I18N.t('repos.card.status_hosted') : r.is_joined ? I18N.t('repos.card.status_joined') : I18N.t('repos.card.status_not_joined');
  const initials = (r.name || '?').substring(0, 2).toUpperCase();

  return `<div class="lobby-card" onclick="window.open('${repoUrl}','_blank')">
    <span class="lobby-card-glow"></span>
    <div class="lobby-card-head">
      <span class="lobby-card-ic">${initials}<i></i></span>
      <span class="lobby-card-status ${enter ? 'in' : ''}"><b></b>${statusTxt}</span>
    </div>
    <h3 title="${r.full_name}">${r.name}</h3>
    <p>${r.description || I18N.t('repos.card.no_description')}</p>
    <div class="lobby-card-owner"><span class="lobby-owner-dot"></span>${r.owner} · ${r.role}</div>
    <div class="lobby-card-btn-row">
      <button class="lobby-btn ${enter ? 'lobby-btn-solid' : 'lobby-btn-ghost'}" onclick="event.stopPropagation();${enter ? `selectRepo('${r.full_name}','${r.owner}','${r.name}')` : `${isAdmin ? 'hostAndSelect' : 'joinAndSelect'}('${r.full_name}','${r.owner}','${r.name}')`}\">${btnTxt}<span class="lobby-btn-ar">→</span></button>
    </div>
  </div>`;
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
        html += `<div class="repo-item" style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--b)">
          <span class="tg tg-c" style="font-size:11px;padding:3px 8px;cursor:pointer" onclick="selectRepo('${r.full_name}','${r.owner}','${r.name}')">${r.full_name}</span>
          <span class="ts t2">${r.role}</span>
          <span class="ts t3">${r.is_hosted ? 'Webhook OK' : ''} · ★ ${r.name}</span>
          <span class="tg ${statusClass}" style="margin-left:auto">${statusText}</span>
          <button class="bt bt-o bt-s" onclick="event.stopPropagation();unhostRepo('${r.full_name}')" style="color:var(--r);border-color:var(--r)">${I18N.t('repos.managed.unhost_btn')}</button>
        </div>`;
      });
    }
    html += `</div>`;

    html += `<div class="cd"><div class="cd-h"><h2>Available to Join <span class="c">${availableRepos.length}</span></h2></div>`;
    if (availableRepos.length === 0) {
      html += '<div class="ts t2" style="padding:12px 0">' + I18N.t('repos.managed.no_available') + '</div>';
    } else {
      html += `<div class="tw"><table>
        <tr><th>${I18N.t('repos.managed.th_repository')}</th><th>${I18N.t('repos.managed.th_role')}</th><th>${I18N.t('repos.managed.th_description')}</th><th></th></tr>`;
      availableRepos.forEach(r => {
        const isAdmin = r.role === 'Admin' || r.role === 'Maintainer';
        const roleTag = r.role === 'Admin' ? `<span class="tg tg-s">${r.role}</span>` :
                        r.role === 'Maintainer' ? `<span class="tg tg-i">${r.role}</span>` : r.role;
        const btnHtml = isAdmin
          ? `<button class="bt bt-p bt-s" onclick="event.stopPropagation();hostAndSelect('${r.full_name}','${r.owner}','${r.name}')">${r.is_hosted ? I18N.t('repos.available_btn.hosted') : I18N.t('repos.available_btn.join_and_host')}</button>`
          : `<button class="bt bt-o bt-s" onclick="event.stopPropagation();joinAndSelect('${r.full_name}','${r.owner}','${r.name}')">${I18N.t('repos.available_btn.join')}</button>`;
        html += `<tr><td><b>${r.full_name}</b></td><td>${roleTag}</td><td>${r.description || ''}</td><td>${btnHtml}</td></tr>`;
      });
      html += `</table></div>`;
    }
    html += `</div>`;

    _projectsContent = html;
    const pc = document.getElementById('ptContent');
    if (pc) pc.innerHTML = html;
  }).catch(() => {
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

function hostAndSelect(fullName, owner, name) {
  API.post('/repos/host', {full_name: fullName}).then(r => {
    app.showToast(I18N.t('repos.sync.host_success'));
    app.loadProfile().then(() => selectRepo(fullName, owner, name));
  }).catch(e => app.showToast(I18N.t('repos.sync.host_failed', {msg: e.message})));
}

function joinAndSelect(fullName, owner, name) {
  API.post('/repos/join', {full_name: fullName}).then(r => {
    app.showToast(I18N.t('repos.sync.join_success'));
    app.loadProfile().then(() => selectRepo(fullName, owner, name));
  }).catch(e => app.showToast(I18N.t('repos.sync.join_failed', {msg: e.message})));
}

function selectRepo(fullName, owner, name) {
  localStorage.setItem('currentRepo', JSON.stringify({fullName, owner, name}));
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
  if (title) title.textContent = 'Dashboard';
  app.initSidebarRepos();
  app.nav('dash');
}

function unhostRepo(fullName) {
  if (!confirm(I18N.t('repos.unhost.confirm', {name: fullName}))) return;
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
      API.post('/repos/create', {name, description: desc, visibility})
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
