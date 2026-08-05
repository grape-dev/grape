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
        <span class="lobby-brand-sub">Agentic DevOps</span>
      </div>
      <nav class="lobby-tabs">
        <a class="${tabCls('projects')}" onclick="switchSidebarTab('projects')">工作空间</a>
        <a class="${tabCls('templates')}" onclick="switchSidebarTab('templates')">项目模板</a>
      </nav>
      <div class="lobby-user" onclick="app.logout()" title="退出登录">
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
function renderRepoSelection(repos) {
  const d = document.getElementById('pageContent');
  if (!d) return;
  _projectsRepos = repos || [];
  const _lu2 = localStorage.getItem('user') ? JSON.parse(localStorage.getItem('user')) : null;
  const user = (_lu2 && (_lu2.nickname || _lu2.username)) || 'User';
  const hostedCount = (repos || []).filter(r => r.is_hosted).length;
  const joinedCount = (repos || []).filter(r => r.is_joined && !r.is_hosted).length;
  const name = (user || '朋友').split('@')[0];

  let inner = `<div class="lobby-hero">
    <div class="lobby-eyebrow"><i></i>WELCOME TO GRAPE<i></i></div>
    <h1 class="lobby-title">你好，<span>${name}</span><span class="lobby-hello">👋</span></h1>
    <p class="lobby-sub">选择一个工作空间，开启由 Agent 驱动的自动化 DevOps 之旅。</p>
    <div class="lobby-stats-inline">
      <div class="lobby-stat"><span class="lobby-stat-ic">◧</span><b>${repos ? repos.length : 0}</b><span>工作空间</span></div>
      <div class="lobby-stat"><span class="lobby-stat-ic">✓</span><b>${hostedCount}</b><span>已托管</span></div>
      <div class="lobby-stat"><span class="lobby-stat-ic">◇</span><b>${joinedCount}</b><span>已加入</span></div>
    </div>
  </div>

  <div class="lobby-grid">
    <div class="lobby-card lobby-card-add" onclick="window.open('https://gitcode.com/create','_blank')">
      <div class="lobby-card-plus">＋</div>
      <h3>创建新空间</h3>
      <p>在 GitCode 上新建项目，从零开始搭建你的 DevOps 流水线。</p>
    </div>
    ${(repos || []).map(r => _buildLobbyCard(r)).join('')}
  </div>

  <div class="lobby-empty" id="rsEmpty" style="display:none">
    <div class="lobby-empty-ic">🔭</div>
    <h3>未找到匹配的空间</h3>
    <p>试试更换关键词或筛选条件。</p>
  </div>`;

  _projectsContent = inner;
  renderSelectionShell('projects', inner);
}

function _buildLobbyCard(r) {
  const isAdmin = r.role === 'Admin' || r.role === 'Maintainer';
  const enter = r.is_joined || r.is_hosted;
  const clickAction = enter
    ? `selectRepo('${r.full_name}','${r.owner}','${r.name}')`
    : `event.stopPropagation();${isAdmin ? 'hostAndSelect' : 'joinAndSelect'}('${r.full_name}','${r.owner}','${r.name}')`;
  const btnTxt = r.is_hosted ? '进入空间' : r.is_joined ? '进入空间' : isAdmin ? '加入并托管' : '加入';
  const statusTxt = r.is_hosted ? '已托管' : r.is_joined ? '已加入' : '未加入';
  const initials = (r.name || '?').substring(0, 2).toUpperCase();

  return `<div class="lobby-card" onclick="${clickAction}">
    <span class="lobby-card-glow"></span>
    <div class="lobby-card-head">
      <span class="lobby-card-ic">${initials}<i></i></span>
      <span class="lobby-card-status ${enter ? 'in' : ''}"><b></b>${statusTxt}</span>
    </div>
    <h3 title="${r.full_name}">${r.name}</h3>
    <p>${r.description || '暂无描述'}</p>
    <div class="lobby-card-owner"><span class="lobby-owner-dot"></span>${r.owner} · ${r.role}</div>
    <div class="lobby-card-btn-row">
      <button class="lobby-btn ${enter ? 'lobby-btn-solid' : 'lobby-btn-ghost'}" onclick="event.stopPropagation();${enter ? `selectRepo('${r.full_name}','${r.owner}','${r.name}')` : `${isAdmin ? 'hostAndSelect' : 'joinAndSelect'}('${r.full_name}','${r.owner}','${r.name}')`}">${btnTxt}<span class="lobby-btn-ar">→</span></button>
    </div>
  </div>`;
}

// ====== 仓库管理页（侧边栏「＋ 新增仓库托管」）======
function renderRepos() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  renderSelectionShell('projects', '<div class="loading">加载中...</div>');

  API.get('/repos/member').then(d => {
    const allRepos = d.repos || [];
    const managedRepos = allRepos.filter(r => r.is_joined || r.is_hosted);
    const availableRepos = allRepos.filter(r => !r.is_joined && !r.is_hosted);

    let html = `<div class="flex fc jbtw" style="margin-bottom:20px">
      <h2 style="font-size:15px;font-weight:600">Repositories</h2>
      <button class="bt bt-p bt-s" onclick="syncRepos()">↻ Sync from GitCode</button>
    </div>`;

    html += `<div class="cd"><div class="cd-h"><h2>Hosted <span class="c">${managedRepos.length}</span></h2></div>`;
    if (managedRepos.length === 0) {
      html += '<div class="ts t2" style="padding:12px 0">暂无已托管的仓库</div>';
    } else {
      managedRepos.forEach(r => {
        const statusClass = r.is_hosted ? 'tg-s' : 'tg-d';
        const statusText = r.is_hosted ? 'Active' : 'Joined';
        html += `<div class="repo-item" style="display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--b)">
          <span class="tg tg-c" style="font-size:11px;padding:3px 8px;cursor:pointer" onclick="selectRepo('${r.full_name}','${r.owner}','${r.name}')">${r.full_name}</span>
          <span class="ts t2">${r.role}</span>
          <span class="ts t3">${r.is_hosted ? 'Webhook OK' : ''} · ★ ${r.name}</span>
          <span class="tg ${statusClass}" style="margin-left:auto">${statusText}</span>
          <button class="bt bt-o bt-s" onclick="event.stopPropagation();unhostRepo('${r.full_name}')" style="color:var(--r);border-color:var(--r)">解除托管</button>
        </div>`;
      });
    }
    html += `</div>`;

    html += `<div class="cd"><div class="cd-h"><h2>Available to Join <span class="c">${availableRepos.length}</span></h2></div>`;
    if (availableRepos.length === 0) {
      html += '<div class="ts t2" style="padding:12px 0">没有可加入的仓库</div>';
    } else {
      html += `<div class="tw"><table>
        <tr><th>Repository</th><th>Role</th><th>Description</th><th></th></tr>`;
      availableRepos.forEach(r => {
        const isAdmin = r.role === 'Admin' || r.role === 'Maintainer';
        const roleTag = r.role === 'Admin' ? `<span class="tg tg-s">${r.role}</span>` :
                        r.role === 'Maintainer' ? `<span class="tg tg-i">${r.role}</span>` : r.role;
        const btnHtml = isAdmin
          ? `<button class="bt bt-p bt-s" onclick="event.stopPropagation();hostAndSelect('${r.full_name}','${r.owner}','${r.name}')">${r.is_hosted ? '已托管' : '加入并托管'}</button>`
          : `<button class="bt bt-o bt-s" onclick="event.stopPropagation();joinAndSelect('${r.full_name}','${r.owner}','${r.name}')">加入</button>`;
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
    if (pc) pc.innerHTML = '<div class="error">加载失败</div>';
  });
}

// ====== 操作函数 ======
function syncRepos() {
  API.post('/repos/sync').then(d => {
    app.showToast('同步成功');
    renderRepos();
  }).catch(e => app.showToast('同步失败: ' + e.message));
}

function hostAndSelect(fullName, owner, name) {
  API.post('/repos/host', {full_name: fullName}).then(r => {
    app.showToast('加入并托管成功');
    app.loadProfile().then(() => selectRepo(fullName, owner, name));
  }).catch(e => app.showToast('托管失败: ' + e.message));
}

function joinAndSelect(fullName, owner, name) {
  API.post('/repos/join', {full_name: fullName}).then(r => {
    app.showToast('已成功加入仓库');
    app.loadProfile().then(() => selectRepo(fullName, owner, name));
  }).catch(e => app.showToast('加入失败: ' + e.message));
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
  if (!confirm('⚠️ 确定要解除托管 ' + fullName + ' 吗？\n\n解除托管后，将删除该仓库在 Grape 上的所有资产数据（事件记录、会话、Agent 配置、仓库技能、工作区文件），且不可恢复。')) return;
  API.post('/repos/remove', {full_name: fullName}).then(r => {
    app.showToast('已解除托管');
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
            document.getElementById('pageTitle').textContent = '选择仓库';
            renderRepoSelection(repos);
          });
        } else {
          document.getElementById('pageTitle').textContent = '选择仓库';
          renderRepos();
        }
      });
    });
  }).catch(e => app.showToast('解除托管失败: ' + e.message));
}
