// ====== 首次登录/新增仓库：卡片式仓库选择页 ======
function renderRepoSelection(repos) {
  const d = document.getElementById('pageContent');
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
                    isAdmin ? '<button class="bt bt-p bt-s" onclick="event.stopPropagation();hostAndSelect(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')">加入并托管</button>' :
                    '<button class="bt bt-o bt-s" onclick="event.stopPropagation();joinAndSelect(\'' + r.full_name + '\',\'' + r.owner + '\',\'' + r.name + '\')">加入</button>';
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

// ====== 仓库管理页（侧边栏"＋ 新增仓库托管"）=======
function renderRepos() {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">加载中...</div>';

  API.get('/repos/member').then(d => {
    const allRepos = d.repos || [];
    const managedRepos = allRepos.filter(r => r.is_joined || r.is_hosted);
    const availableRepos = allRepos.filter(r => !r.is_joined && !r.is_hosted);

    let html = `<div class="flex fc jbtw" style="margin-bottom:20px">
      <h2 style="font-size:15px;font-weight:600">Repositories</h2>
      <button class="bt bt-p bt-s" onclick="renderRepos()">↻ Sync from GitCode</button>
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

    el.innerHTML = html;
  }).catch(() => {
    el.innerHTML = '<div class="error">加载失败</div>';
  });
}


// ====== 操作函数 ======
function hostAndSelect(fullName, owner, name) {
  API.post('/repos/host', {full_name: fullName}).then(r => {
    app.showToast('加入并托管成功');
    selectRepo(fullName, owner, name);
  }).catch(e => app.showToast('托管失败: ' + e.message));
}

function joinAndSelect(fullName, owner, name) {
  API.post('/repos/join', {full_name: fullName}).then(r => {
    app.showToast('已成功加入仓库');
    selectRepo(fullName, owner, name);
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
  if (!confirm('确定要解除托管 ' + fullName + ' 吗？相关的 Webhook 将被删除。')) return;
  API.post('/repos/remove', {full_name: fullName}).then(r => {
    app.showToast('已解除托管');
    const cur = app.currentRepo();
    const wasCurrent = cur && cur.fullName === fullName;
    if (wasCurrent) {
      localStorage.removeItem('currentRepo');
    }
    app.initSidebarRepos();
    // 检查是否还有剩余托管仓库
    API.get('/repos/managed').then(md => {
      const remaining = md.repos || [];
      if (wasCurrent || remaining.length === 0) {
        // 解除的是当前仓库或是最后一个 → 跳转到仓库选择页
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
        // 只是普通解除 → 刷新管理页
        document.getElementById('pageTitle').textContent = '选择仓库';
        renderRepos();
      }
    });
  }).catch(e => app.showToast('解除托管失败: ' + e.message));
}
