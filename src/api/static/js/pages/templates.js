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

function _sceneClass(sceneId) {
  const s = (sceneId || '').toLowerCase();
  if (s.includes('web') || s.includes('app')) return 'sc-web';
  if (s.includes('data') || s.includes('ml') || s.includes('etl')) return 'sc-data';
  if (s.includes('infra') || s.includes('devops') || s.includes('k8s')) return 'sc-infra';
  return 'sc-default';
}

function _sceneLabel(sceneId) {
  const s = (sceneId || '').toLowerCase();
  if (s.includes('web') || s.includes('app')) return 'Web';
  if (s.includes('data') || s.includes('ml') || s.includes('etl')) return 'Data';
  if (s.includes('infra') || s.includes('devops') || s.includes('k8s')) return 'Infra';
  return 'Template';
}

function renderTemplateCategory() {
  const cats = _tmplState.categories;
  const c = document.getElementById('ptContent');
  if (!c) return;
  if (!cats || cats.length === 0) {
    c.innerHTML = '<div class="lobby-empty" style="border:none;background:none;color:var(--t2)"><div class="lobby-empty-ic">📦</div><h3>暂无项目模板</h3><p>等待管理员配置项目模板。</p></div>';
    return;
  }
  const cat = cats[_tmplState.activeCat] || cats[0];
  const totalAll = cats.reduce((n, x) => n + (x.templates || []).length, 0);
  const scCls = _sceneClass(cat.scene_id);
  const tabs = cats.map((x, i) => {
    const cnt = (x.templates || []).length;
    const act = i === _tmplState.activeCat ? ' tmpl-tab-act' : '';
    return `<button class="tmpl-tab${act}" onclick="switchTemplateCat(${i})"><span class="tmpl-tab-ic">${x.icon || '📦'}</span><span class="tmpl-tab-name">${x.name}</span><span class="tmpl-tab-cnt">${cnt}</span></button>`;
  }).join('');
  const cards = (cat.templates || []).map(t => {
    const tags = (t.tags || []).map(x => `<span class="tmpl-tag">${x}</span>`).join('');
    const tSc = _sceneClass(t.scene_id || cat.scene_id);
    const stats = t.stats || {};
    const uses = stats.uses ?? Math.floor(Math.random() * 80 + 12);
    const stars = stats.stars ?? Math.floor(Math.random() * 80 + 30);
    // 卡片点击：git 模板跳转到 GitCode 对应仓库；无 source_repo（local 模板）回退打开创建弹窗
    const cardOnClick = t.source_repo
      ? `window.open('https://gitcode.com/${t.source_repo}','_blank')`
      : `createFromTemplate('${t.id}','${t.default_repo_name || t.id}')`;
    return `<div class="tmpl-card tmpl-${tSc}" onclick="${cardOnClick}">
      <span class="tmpl-card-glow"></span>
      <div class="tmpl-card-head">
        <span class="tmpl-card-ic">${t.icon || '📦'}<i></i></span>
        <span class="tmpl-card-scene ${tSc}">${_sceneLabel(t.scene_id || cat.scene_id)}</span>
      </div>
      <h3 title="${t.name}">${t.name}</h3>
      <p>${t.description || '暂无描述'}</p>
      <div class="tmpl-tags">${tags}</div>
      <div class="tmpl-card-stats">
        <span class="tmpl-stat"><b>★</b>${stars}</span>
        <span class="tmpl-stat"><b>⬡</b>${uses}</span>
      </div>
      <div class="tmpl-card-foot">
        ${t.video_url ? `<button class="tmpl-btn tmpl-btn-video" onclick="event.stopPropagation();window.open('${t.video_url}','_blank')">▶ 视频</button>` : ''}
        <button class="tmpl-btn" onclick="event.stopPropagation();createFromTemplate('${t.id}','${t.default_repo_name || t.id}')">一键创建 <span class="tmpl-btn-ar">→</span></button>
      </div>
    </div>`;
  }).join('');

  c.innerHTML = `
    <div class="tmpl-hero tmpl-hero-${scCls}">
      <div class="lobby-eyebrow"><i></i>TEMPLATE GALLERY<i></i></div>
      <h1 class="lobby-title">${cat.icon || '📦'} <span>${cat.name}</span></h1>
      <p class="lobby-sub">${cat.description || '选择一个模板，快速创建你的 DevOps 项目。'}</p>
      <div class="lobby-stats-inline">
        <div class="lobby-stat"><span class="lobby-stat-ic">◧</span><b>${totalAll}</b><span>模板总数</span></div>
        <div class="lobby-stat"><span class="lobby-stat-ic">▦</span><b>${(cat.templates || []).length}</b><span>当前分类</span></div>
        <div class="lobby-stat"><span class="lobby-stat-ic">⬡</span><b>${cats.length}</b><span>分类</span></div>
      </div>
    </div>
    <div class="tmpl-tabs">${tabs}</div>
    <div class="lobby-grid">${cards}</div>`;
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
