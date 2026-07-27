const app = (() => {
  const $ = id => document.getElementById(id);
  const VIEWS = ['dash','pipe','vod','issue','coding','qa','sre','chat','settings','skills','backends','event','repos'];

  function currentRepo() {
    try { return JSON.parse(localStorage.getItem('currentRepo')); } catch { return null; }
  }

  function showLogin() {
    $('loginView').classList.remove('hid');
    $('sidebar').style.display = 'none';
    $('mainView').style.display = 'none';
    $('repoView').classList.remove('ac');
  }

  function showApp() {
    $('loginView').classList.add('hid');
    $('sidebar').style.display = 'flex';
    $('mainView').style.display = 'flex';
    loadProfile();
    loadRepos();
  }

  function loadProfile() {
    API.get('/auth/profile').then(d => {
      $('userName').textContent = d.username || 'User';
      $('userAvatar').textContent = (d.username || 'U')[0].toUpperCase();
    }).catch(() => {});
  }

  function loadRepos() {
    API.get('/repos/member').then(d => {
      const repos = d.repos || [];
      const sel = $('repoSelect');
      const current = currentRepo();
      sel.innerHTML = '<option value="">选择仓库...</option>' + repos.map(r =>
        `<option value="${r.full_name}" ${current && current.fullName === r.full_name ? 'selected' : ''}>${r.full_name}</option>`
      ).join('');

      if (!current && repos.length > 0) {
        const r = repos[0];
        localStorage.setItem('currentRepo', JSON.stringify({fullName: r.full_name, owner: r.owner, name: r.name}));
      }

      if (repos.length === 0) {
        showRepoSelection(repos);
      }
    }).catch(() => {});
  }

  function showRepoSelection(repos) {
    $('sidebar').style.display = 'none';
    $('mainView').style.display = 'none';
    $('repoView').classList.add('ac');
    renderRepos(repos);
  }

  function switchRepo(val) {
    if (!val) return;
    const parts = val.split('/');
    localStorage.setItem('currentRepo', JSON.stringify({fullName: val, owner: parts[0], name: parts[1]}));
    nav('dash');
  }

  function nav(view, param) {
    const repo = currentRepo();
    if (!repo && view !== 'repos' && view !== 'settings') {
      loadRepos();
      return;
    }

    VIEWS.forEach(v => {
      const el = $('nav-' + v);
      if (el) el.classList.remove('act');
    });
    const navEl = $('nav-' + view);
    if (navEl) navEl.classList.add('act');

    const bc = $('breadcrumb');
    const pc = $('pageContent');
    pc.innerHTML = '<div class="loading">加载中...</div>';

    if (view === 'dash') {
      bc.innerHTML = repo ? `<b>${repo.fullName}</b> · Dashboard` : 'Dashboard';
      API.get('/dashboard/' + repo.owner + '/' + repo.name).then(renderDashboard).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'pipe') {
      bc.innerHTML = repo ? `<b>${repo.fullName}</b> · Pipeline` : 'Pipeline';
      API.get('/pipeline/' + repo.owner + '/' + repo.name).then(renderPipeline).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (['vod','issue','coding','qa','sre'].includes(view)) {
      bc.innerHTML = repo ? `<b>${repo.fullName}</b> · ${AGENT_META[view].display}` : view;
      API.get('/agents/' + view + '/' + repo.owner + '/' + repo.name).then(d => renderAgent(view, d)).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'event') {
      bc.innerHTML = 'Event #' + param;
      API.get('/events/detail/' + param).then(renderEventDetail).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'chat') {
      bc.innerHTML = 'Chat Agent';
      renderChat();
    } else if (view === 'settings') {
      bc.innerHTML = 'Settings';
      renderSettings(param || 'credentials');
    } else if (view === 'skills') {
      bc.innerHTML = 'DevOps Skills';
      renderSkills();
    } else if (view === 'backends') {
      bc.innerHTML = 'Backend Agents';
      API.get('/settings/backend-agents').then(d => {
        const backends = d.backends || [];
        let html = '<div class="cd"><div class="cd-h"><h2>Backend Agents</h2></div>';
        backends.forEach(b => {
          html += `<div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--b)">
            <span style="font-weight:600;width:80px">${b.name}</span>
            <span class="${b.installed ? 'tgx' : 'tr'}">${b.installed ? '✓ Installed' : '✗ Not found'}</span>
            <span class="ts t3">${b.cli_path || ''}</span>
          </div>`;
        });
        html += '</div>';
        pc.innerHTML = html;
      }).catch(e => pc.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>');
    } else if (view === 'repos') {
      bc.innerHTML = '选择仓库';
      loadRepos();
    }
  }

  function search(q) {
    if (!q.trim()) return;
    API.get('/search/?q=' + encodeURIComponent(q) + '&type=all').then(d => {
      const pc = $('pageContent');
      let html = `<div class="cd"><div class="cd-h"><h2>Search: "${q}"</h2></div>`;
      html += `<div class="ts t2">Events: ${(d.events || []).length} · Repos: ${(d.repos || []).length} · Agents: ${(d.agents || []).length}</div>`;
      if (d.events && d.events.length > 0) {
        html += '<div class="cd-h" style="margin-top:12px"><h2>Events</h2></div>';
        d.events.forEach(ev => {
          html += `<div class="ai" onclick="app.nav('event',${ev.seq})" style="cursor:pointer"><div class="ac"><b>${ev.title}</b><span class="ts t3"> ${ev.repo_key}</span></div></div>`;
        });
      }
      if (d.agents && d.agents.length > 0) {
        html += '<div class="cd-h" style="margin-top:12px"><h2>Agents</h2></div>';
        d.agents.forEach(a => {
          html += `<div class="cl" onclick="app.nav('${a.name}')"><div class="cx"><h4>${a.display_name}</h4></div></div>`;
        });
      }
      html += '</div>';
      pc.innerHTML = html;
    }).catch(e => {});
  }

  function oauthLogin() {
    API.get('/auth/login/gitcode').then(d => {
      if (d.redirect_url) window.location.href = d.redirect_url;
    }).catch(e => app.showToast('OAuth 未配置'));
  }

  let pwdVisible = false;
  function togglePwdLogin() {
    pwdVisible = !pwdVisible;
    $('pwdLogin').style.display = pwdVisible ? 'block' : 'none';
  }

  function pwdLogin() {
    const username = $('loginUser').value.trim();
    const password = $('loginPass').value.trim();
    if (!username || !password) return app.showToast('请输入用户名和密码');
    API.post('/auth/login', {username, password}).then(d => {
      localStorage.setItem('token', d.token);
      localStorage.setItem('user', JSON.stringify(d.user));
      showApp();
    }).catch(e => app.showToast('登录失败: ' + e.message));
  }

  function logout() {
    const token = localStorage.getItem('token');
    if (token) {
      API.post('/auth/logout', {}).catch(() => {});
    }
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    localStorage.removeItem('currentRepo');
    showLogin();
  }

  function showToast(msg) {
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#101724;color:#fff;padding:10px 20px;border-radius:8px;font-size:12px;z-index:999;box-shadow:0 4px 16px rgba(0,0,0,.2)';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3000);
  }

  function init() {
    const token = localStorage.getItem('token');
    if (token) {
      showApp();
    } else {
      const urlParams = new URLSearchParams(window.location.search);
      const urlToken = urlParams.get('token');
      if (urlToken) {
        localStorage.setItem('token', urlToken);
        history.replaceState(null, '', '/');
        showApp();
      } else {
        showLogin();
      }
    }
  }

  return {nav, search, oauthLogin, togglePwdLogin, pwdLogin, logout, showToast, switchRepo, currentRepo, showLogin, init};
})();

document.addEventListener('DOMContentLoaded', () => app.init());
