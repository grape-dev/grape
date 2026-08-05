// ====== 仓库选择/管理页：内嵌 Sidebar 壳（我的项目 / 项目模板）======
let _projectsContent = '';
let _currentReposView = 'selection';  // 'selection' 首屏卡片视图 | 'management' 仓库管理列表视图

function renderSelectionShell(tab, innerHtml) {
  const d = document.getElementById('pageContent');
  if (!d) return;
  const active = tab || localStorage.getItem('sidebarTab') || 'projects';
  const tabCls = t => 'ptab' + (active === t ? ' ptab-act' : '');
  const html = `<div style="display:flex;gap:18px;max-width:1200px;margin:0 auto;min-height:calc(100vh - 32px)">
    <div class="ptsb">
      <div style="display:flex;align-items:center;gap:9px;padding:4px 10px;margin-bottom:16px;cursor:pointer" onclick="switchSidebarTab('projects')" title="返回我的项目">
        <img src="/static/logo.png" class="sq" style="width:30px;height:30px;border-radius:8px">
        <div><div style="font-size:14px;font-weight:700;line-height:1.1">Grape</div><div style="font-size:9px;color:#8d97a8">Multi-Agent DevOps</div></div>
      </div>
      <div style="font-size:10px;color:#8d97a8;padding:2px 10px;margin-bottom:4px;letter-spacing:.5px">NAVIGATION</div>
      <div class="${tabCls('projects')}" onclick="switchSidebarTab('projects')"><span class="ic">🗂️</span>我的项目</div>
      <div class="${tabCls('templates')}" onclick="switchSidebarTab('templates')"><span class="ic">📦</span>项目模板</div>
      <div class="ptft"><a onclick="app.logout()">↩ Logout</a></div>
    </div>
    <div style="flex:1;min-width:0" id="ptContent">${innerHtml || ''}</div>
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

// ====== 首次登录/新增仓库：卡片式仓库选择页 ======
function renderRepoSelection(repos) {
  const d = document.getElementById('pageContent');
  if (!d) return;
  _currentReposView = 'selection';
  const user = localStorage.getItem('user') ? JSON.parse(localStorage.getItem('user')).username : 'User';
  let inner = `<div style="max-width:900px;margin:0 auto">
    <div style="margin-bottom:20px;display:flex;justify-content:space-between;align-items:center">
      <div><h2 style="font-size:17px;font-weight:600;margin-bottom:3px">Welcome, <span id="welcomeUser">${user}</span></h2><p class="ts t2">选择你要加入的 GitCode 仓库</p></div>
      <div style="display:flex;gap:8px">
        <button class="bt bt-o bt-s" onclick="syncRepos()">↻ Sync from GitCode</button>
        <button class="bt bt-o bt-s" onclick="app.logout()" style="color:var(--r);border-color:var(--r)">退出登录</button>
      </div>
    </div>
    <div class="flex gap8" style="margin-bottom:24px"><input placeholder="搜索仓库..." style="flex:1;padding:8px 12px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c)"><select style="padding:8px 12px;border:1px solid var(--b);border-radius:var(--rd);font-size:11px;background:var(--c);outline:none"><option>全部</option><option>成员</option><option>管理员</option></select></div>
    <div class="rgrid">
      <div class="rc" style="background:linear-gradient(135deg,#eef3ff,#f0fdf4);border-color:var(--a);border-style:dashed" onclick="window.open('https://gitcode.com/create','_blank')">
        <div>
          <div class="rh"><span class="rn" style="color:var(--a)">＋ 新增仓库</span><span class="tg tg-c" style="font-size:9px">新项目</span></div>
          <div class="rd">创建一个新的 GitCode 项目</div>
          <div class="ro">不想基于已有项目？从零开始创建</div>
        </div>
        <div class="rf"><span class="s">🆕 New Project</span><button class="bt bt-p bt-s" onclick="event.stopPropagation();window.open('https://gitcode.com/create','_blank')">Create Now</button></div>
      </div>`;
  repos.forEach(r => {
    const isAdmin = r.role === 'Admin' || r.role === 'Maintainer';
    const btnHtml = r.is_hosted ? '<span class="ts t2">已托管</span>' :
                    r.is_joined ? '<span class="ts t2">已加入</span>' :
                    isAdmin ? '<button class="bt bt-p bt-s" onclick="event.stopPropagation();hostAndSelect(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')">加入并托管</button>' :
                    '<button class="bt bt-o bt-s" onclick="event.stopPropagation();joinAndSelect(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')">加入</button>';
    const statusIcon = r.is_hosted ? '✅ Hosted' : r.is_joined ? '👋 Joined' : '🚧 Not added';
    inner += `<div class="rc" onclick="${r.is_joined || r.is_hosted ? 'selectRepo(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')' : 'event.stopPropagation();' + (isAdmin ? 'hostAndSelect' : 'joinAndSelect') + '(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')'}">
      <div><div class="rh"><span class="rn">${r.full_name}</span><span class="tg tg-d" style="font-size:9px">${r.role}</span></div>
      <div class="rd">${r.description || ''}</div>
      <div class="ro">Owner: ${r.owner}</div></div>
      <div class="rf"><span class="s">${statusIcon}</span>${btnHtml}</div>
    </div>`;
  });
  inner += `</div></div>`;
  _projectsContent = inner;
  renderSelectionShell('projects', inner);
}

// ====== 仓库管理页（侧边栏"＋ 新增仓库托管"）=======
function renderRepos() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  _currentReposView = 'management';
  renderSelectionShell('projects', '<div class="loading">加载中...</div>');

  API.get('/repos/member').then(d => {
    const allRepos = d.repos || [];
    const managedRepos = allRepos.filter(r => r.is_joined || r.is_hosted);
    const availableRepos = allRepos.filter(r => !r.is_joined && !r.is_hosted);

    let html = `<div class="flex fc jbtw" style="margin-bottom:20px">
      <h2 style="font-size:15px;font-weight:600">Repositories</h2>
      <button class="bt bt-p bt-s" onclick="syncRepos()">↻ Sync from GitCode</button>
    </div>`;

    // Hosted repos
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

    // Available to join
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
    if (_currentReposView === 'selection') {
      // 首屏卡片视图：重新拉取并渲染卡片
      API.get('/repos/member').then(r => {
        _projectsContent = '';
        renderRepoSelection(r.repos || []);
      }).catch(() => renderRepoSelection(d.repos || []));
    } else {
      renderRepos();
    }
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
      if (wasCurrent) {
        localStorage.removeItem('currentRepo');
      }
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
