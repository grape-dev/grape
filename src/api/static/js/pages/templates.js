// ====== 项目模板页（仓库选择页 Sidebar → 项目模板）======
let _tmplState = {categories: [], activeCat: 0};

function renderTemplates() {
  const d = document.getElementById('pageContent');
  if (!d) return;
  renderSelectionShell('templates', '<div class="loading">加载中...</div>');
  API.get('/project-templates').then(data => {
    _tmplState.categories = data.categories || [];
    _tmplState.activeCat = 0;
    renderTemplateCategory();
  }).catch(e => {
    const c = document.getElementById('ptContent');
    if (c) c.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

function switchTemplateCat(idx) {
  _tmplState.activeCat = idx;
  renderTemplateCategory();
}

function renderTemplateCategory() {
  const cats = _tmplState.categories;
  const c = document.getElementById('ptContent');
  if (!c) return;
  if (!cats || cats.length === 0) {
    c.innerHTML = '<div class="ts t2" style="padding:20px 0">暂无项目模板</div>';
    return;
  }
  const cat = cats[_tmplState.activeCat] || cats[0];
  const tabs = cats.map((x, i) =>
    `<button class="pttab ${i === _tmplState.activeCat ? 'pttab-act' : ''}" onclick="switchTemplateCat(${i})">${x.icon || '📦'} ${x.name}</button>`
  ).join('');
  const cards = (cat.templates || []).map(t => {
    const tags = (t.tags || []).map(x => `<span class="tg tg-d" style="margin-right:4px">${x}</span>`).join('');
    return `<div class="rc">
      <div>
        <div class="rh"><span style="font-size:22px;line-height:1">${t.icon || '📦'}</span><span class="rn">${t.name}</span></div>
        <div class="rd">${t.description || ''}</div>
        <div class="ro">${tags}</div>
      </div>
      <div class="rf"><span class="tg tg-c">${t.scene_id || cat.scene_id || 'default'}</span><button class="bt bt-p bt-s" onclick="createFromTemplate('${t.id}','${t.default_repo_name || t.id}')">一键创建</button></div>
    </div>`;
  }).join('');

  c.innerHTML = `
    <div style="margin-bottom:14px">
      <h2 style="font-size:15px;font-weight:600;margin-bottom:12px">项目模板</h2>
      <div style="display:flex;gap:8px;flex-wrap:wrap">${tabs}</div>
    </div>
    <div style="margin-bottom:14px">
      <h3 style="font-size:13px;font-weight:600">${cat.icon || ''} ${cat.name}</h3>
      <p class="ts t2" style="margin-top:2px">${cat.description || ''}</p>
    </div>
    <div class="rgrid">${cards}</div>`;
}

function createFromTemplate(templateId, defaultName) {
  const modal = document.createElement('div');
  modal.id = 'ptCreateModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.innerHTML = `<div class="cd" style="width:440px;padding:20px;border-radius:10px">
    <h3 style="font-size:14px;font-weight:600;margin-bottom:6px">一键创建项目</h3>
    <p class="ts t2" style="margin-bottom:14px">将在你的 GitCode 个人账号下创建同名仓库，并自动加入项目托管、应用对应的开发场景。</p>
    <label class="ts t2" style="display:block;margin-bottom:4px">仓库名</label>
    <input id="ptRepoName" value="${defaultName}" style="width:100%;padding:8px 12px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c)">
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">
      <button class="bt bt-o bt-s" onclick="document.getElementById('ptCreateModal').remove()">取消</button>
      <button class="bt bt-p bt-s" id="ptCreateBtn" onclick="doCreateFromTemplate('${templateId}')">创建</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  document.getElementById('ptRepoName').focus();
}

function doCreateFromTemplate(templateId) {
  const repoName = (document.getElementById('ptRepoName').value || '').trim();
  const btn = document.getElementById('ptCreateBtn');
  btn.disabled = true;
  btn.textContent = '创建中...';
  API.post('/project-templates/create', {template_id: templateId, repo_name: repoName}).then(r => {
    const modal = document.getElementById('ptCreateModal');
    if (modal) modal.remove();
    app.showToast('✅ ' + (r.message || '创建成功'));
    const parts = (r.full_name || '').split('/');
    selectRepo(r.full_name, parts[0], parts[1]);
  }).catch(e => {
    btn.disabled = false;
    btn.textContent = '创建';
    if (e.status === 409) {
      // 同名冲突：保持弹窗打开，提示换名重试
      const input = document.getElementById('ptRepoName');
      if (input) {
        input.focus();
        input.select();
        app.showToast('⚠️ ' + (e.message || '仓库名已存在') + '，请更换仓库名后重试');
      }
    } else {
      app.showToast('创建失败: ' + e.message);
    }
  });
}
