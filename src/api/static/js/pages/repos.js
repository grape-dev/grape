function renderRepos(repos) {
  const d = document.getElementById('repoContent');
  if (!d) return;
  if (!repos || repos.length === 0) {
    d.innerHTML = '<div style="max-width:900px;margin:0 auto;padding:40px 0"><div class="loading">没有找到仓库</div></div>';
    return;
  }
  const user = localStorage.getItem('user') ? JSON.parse(localStorage.getItem('user')).username : 'User';
  let html = `<div style="max-width:900px;margin:0 auto">
    <div style="margin-bottom:20px"><h2 style="font-size:17px;font-weight:600;margin-bottom:3px">Welcome, ${user}</h2><p class="ts t2">选择你要加入的 GitCode 仓库</p></div>
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
                    isAdmin ? '<button class="bt bt-p bt-s" onclick="event.stopPropagation();hostAndSelect(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')">Host Now</button>' :
                    '<button class="bt bt-o bt-s" onclick="event.stopPropagation();joinAndSelect(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')">+ Join</button>';
    const statusIcon = r.is_hosted ? '✅ Hosted' : r.is_joined ? '👋 Joined' : '🚧 Not added';
    html += `<div class="rc" onclick="${r.is_joined || r.is_hosted ? 'selectRepo(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')' : 'event.stopPropagation();' + (isAdmin ? 'hostAndSelect' : 'joinAndSelect') + '(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')'}">
      <div><div class="rh"><span class="rn">${r.full_name}</span><span class="tg tg-d" style="font-size:9px">${r.role}</span></div>
      <div class="rd">${r.description || ''}</div>
      <div class="ro">Owner: ${r.owner}</div></div>
      <div class="rf"><span class="s">${statusIcon}</span>${btnHtml}</div>
    </div>`;
  });
  html += `</div></div>`;
  d.innerHTML = html;
}

function joinAndSelect(fullName, owner, name) {
  API.post('/repos/join', {full_name: fullName}).then(r => {
    selectRepo(fullName, owner, name);
  }).catch(e => app.showToast('加入失败: ' + e.message));
}

function hostAndSelect(fullName, owner, name) {
  API.post('/repos/host', {full_name: fullName}).then(r => {
    selectRepo(fullName, owner, name);
  }).catch(e => app.showToast('托管失败: ' + e.message));
}

function selectRepo(fullName, owner, name) {
  localStorage.setItem('currentRepo', JSON.stringify({fullName, owner, name}));
  const sidebar = document.getElementById('sidebar');
  const mainView = document.getElementById('mainView');
  const repoView = document.getElementById('repoView');
  if (sidebar) sidebar.style.display = 'flex';
  if (mainView) mainView.style.display = 'flex';
  if (repoView) repoView.classList.remove('ac');
  // Reload sidebar repo list
  app.initSidebarRepos();
  app.nav('dash');
}
