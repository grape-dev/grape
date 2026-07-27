function renderRepos(repos) {
  const d = document.getElementById('repoContent');
  if (!d) return;
  if (!repos || repos.length === 0) {
    d.innerHTML = '<div class="rgrid"><div class="rc" onclick="window.open(\'https://gitcode.com/create\')"><div class="rh"><span class="rn">＋ 新增仓库</span></div><div class="rd">在 GitCode 上创建新仓库后刷新此页面</div></div></div>';
    return;
  }
  let html = '<div class="rgrid">';
  repos.forEach(r => {
    const btn = r.role === 'Admin' ? '<button class="bt bt-g bt-s">Host Now</button>' :
                r.is_joined ? '<span class="ts t2">已加入</span>' : '<button class="bt bt-p bt-s">+ Join</button>';
    html += `<div class="rc" onclick="selectRepo('${r.full_name}','${r.owner}','${r.name}')">
      <div><div class="rh"><span class="rn">${r.full_name}</span></div>
      <div class="rd">${r.description || ''}</div>
      <div class="ro">${r.role}</div></div>
      <div class="rf"><div>${btn}</div></div></div>`;
  });
  html += '<div class="rc" style="display:flex;align-items:center;justify-content:center;font-size:28px;color:var(--t3);cursor:pointer" onclick="window.open(\'https://gitcode.com/create\')">＋</div>';
  html += '</div>';
  d.innerHTML = html;
}

function selectRepo(fullName, owner, name) {
  localStorage.setItem('currentRepo', JSON.stringify({fullName, owner, name}));
  app.nav('dash');
}
