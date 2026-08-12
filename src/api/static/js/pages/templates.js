// ====== 项目模板页（仓库选择页 Sidebar → 项目模板）======
let _tmplState = {categories: [], activeCat: 0, page: 0};

function renderTemplates() {
  const d = document.getElementById('pageContent');
  if (!d) return;
  renderSelectionShell('templates', '<div class="loading">加载中...</div>');
  API.get('/project-templates').then(data => {
    const rawCats = data.categories || [];
    const allTemplates = rawCats.flatMap(cat =>
      (cat.templates || []).map(t => ({...t, _cat_name: cat.name, _cat_icon: cat.icon || '📦'}))
    );
    _tmplState.categories = [{
      id: 'all', name: '全部', icon: '📋',
      description: '浏览所有可用项目模板，一键创建你的 DevOps 项目',
      scene_id: 'default', page_size: 9, templates: allTemplates,
    }, ...rawCats];
    _tmplState.activeCat = 0;
    _tmplState.page = 0;
    renderTemplateCategory();
  }).catch(e => {
    const c = document.getElementById('ptContent');
    if (c) c.innerHTML = '<div class="error">加载失败: ' + e.message + '</div>';
  });
}

function switchTemplateCat(idx) {
  _tmplState.activeCat = idx;
  _tmplState.page = 0;
  renderTemplateCategory();
}

function _tmplPageSize(cat) {
  const hasRandom = cat.random_mode && (cat.templates || []).length > 0;
  const extraCards = 1 + (hasRandom ? 1 : 0);
  return Math.max(1, 9 - extraCards);
}

function goTemplatePage(dir) {
  const cat = _tmplState.categories[_tmplState.activeCat];
  const pageSize = _tmplPageSize(cat);
  const total = (cat.templates || []).length;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  _tmplState.page = (_tmplState.page + dir + pages) % pages;
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
  const realCats = cats.filter(x => x.id !== 'all');
  const totalAll = realCats.reduce((n, x) => n + (x.templates || []).length, 0);
  const scCls = _sceneClass(cat.scene_id);
  const tabs = cats.map((x, i) => {
    const cnt = (x.templates || []).length;
    const act = i === _tmplState.activeCat ? ' tmpl-tab-act' : '';
    return `<button class="tmpl-tab${act}" onclick="switchTemplateCat(${i})"><span class="tmpl-tab-ic">${x.icon || '📦'}</span><span class="tmpl-tab-name">${x.name}</span><span class="tmpl-tab-cnt">${cnt}</span></button>`;
  }).join('');
  // 分页（固定卡片：创建新项目 + 随机，占用网格位置，计入每页上限 9）
  const allT = cat.templates || [];
  const pageSize = _tmplPageSize(cat);
  const pages = Math.max(1, Math.ceil(allT.length / pageSize));
  if (_tmplState.page >= pages) _tmplState.page = 0;
  const pageT = allT.slice(_tmplState.page * pageSize, (_tmplState.page + 1) * pageSize);

  const cards = pageT.map(t => {
    const tags = (t.tags || []).map(x => `<span class="tmpl-tag">${x}</span>`).join('');
    const catLine = t._cat_name ? `<div class="tmpl-card-cat">${t._cat_icon || '📦'} ${t._cat_name}</div>` : '';
    const tSc = _sceneClass(t.scene_id || cat.scene_id);
    const stats = t.stats || {};
    // 本地模板：来源标签；GitCode 模板：真实 star
    let statsHtml;
    if (t.source === 'local' || stats.show === false) {
      statsHtml = `<div class="tmpl-card-stats"><span class="tmpl-stat tmpl-stat-local"><b>◎</b>本地自写 · 简洁示例</span></div>`;
    } else {
      const stars = stats.stars || 0;
      const uses = stats.uses || Math.max(1, Math.round(stars * 3.2));
      statsHtml = `<div class="tmpl-card-stats"><span class="tmpl-stat"><b>★</b>${stars}</span><span class="tmpl-stat"><b>⬡</b>${uses}</span></div>`;
    }
    // 卡片本体：有 source_repo（GitCode 模板）点击跳转实际仓库；local 模板无可跳转，保持弹一键创建
    const tplUrl = t.source_repo ? 'https://gitcode.com/' + t.source_repo : '';
    const cardClick = tplUrl
      ? `window.open('${tplUrl}','_blank')`
      : `createFromTemplate('${t.id}','${t.default_repo_name || t.id}')`;
    return `<div class="tmpl-card tmpl-${tSc}" onclick="${cardClick}">
      <span class="tmpl-card-glow"></span>
      <div class="tmpl-card-head">
        <span class="tmpl-card-ic">${t.icon || '📦'}<i></i></span>
        <span class="tmpl-card-scene ${tSc}">${_sceneLabel(t.scene_id || cat.scene_id)}</span>
      </div>
      ${catLine}
      <h3 title="${t.name}">${t.name}</h3>
      <p>${t.description || '暂无描述'}</p>
      <div class="tmpl-tags">${tags}</div>
      ${statsHtml}
      <div class="tmpl-card-foot">
        <button class="tmpl-btn" onclick="event.stopPropagation();createFromTemplate('${t.id}','${t.default_repo_name || t.id}')">一键创建 <span class="tmpl-btn-ar">→</span></button>
      </div>
    </div>`;
  }).join('');

  // 分页控件（模板数超过每页数量时显示）
  let pagerHtml = '';
  if (pages > 1) {
    pagerHtml = `<div class="tmpl-pager">
      <button class="tmpl-page-btn" ${_tmplState.page === 0 ? 'disabled' : ''} onclick="goTemplatePage(-1)">‹</button>
      <span class="tmpl-page-info">${_tmplState.page + 1} / ${pages}</span>
      <button class="tmpl-page-btn" ${_tmplState.page >= pages - 1 ? 'disabled' : ''} onclick="goTemplatePage(1)">›</button>
    </div>`;
  }

  // 随机探索模式：面向新手的 Hello World 分类，作为网格第一个卡片，翻页时保留不动
  let randomCard = '';
  if (cat.random_mode && allT.length > 0) {
    randomCard = renderRandomExplorer(cat);
  }

  // 创建新项目卡片（从零开始，不使用模板）
  const createCard = `<div class="lobby-card lobby-card-add" onclick="openCreateRepoModal()">
    <div class="lobby-card-plus">＋</div>
    <h3>创建新项目</h3>
    <p>在系统内直接新建 GitCode 项目，从零开始搭建你的 DevOps 流水线。</p>
  </div>`;

  c.innerHTML = `
    <div class="tmpl-hero tmpl-hero-${scCls}">
      <div class="lobby-eyebrow"><i></i>TEMPLATE GALLERY<i></i></div>
      <h1 class="lobby-title">${cat.icon || '📦'} <span>${cat.name}</span></h1>
      <p class="lobby-sub">${cat.description || '选择一个模板，快速创建你的 DevOps 项目。'}</p>
      <div class="lobby-stats-inline">
        <div class="lobby-stat"><span class="lobby-stat-ic">◧</span><b>${totalAll}</b><span>模板总数</span></div>
        <div class="lobby-stat"><span class="lobby-stat-ic">▦</span><b>${allT.length}</b><span>当前分类</span></div>
        <div class="lobby-stat"><span class="lobby-stat-ic">⬡</span><b>${realCats.length}</b><span>分类</span></div>
      </div>
    </div>
    <div class="tmpl-tabs">${tabs}</div>
    <div class="lobby-grid">${createCard}${randomCard}${cards}</div>
    ${pagerHtml}`;
}

// ====== 随机探索模式（Hello World 新手模板）======
function renderRandomExplorer(cat) {
  const tmpls = cat.templates || [];
  if (typeof _tmplState.randIdx !== 'number' || _tmplState.randIdx >= tmpls.length) {
    _tmplState.randIdx = Math.floor(Math.random() * tmpls.length);
  }
  const t = tmpls[_tmplState.randIdx];
  const tSc = _sceneClass(t.scene_id || cat.scene_id);
  const tags = (t.tags || []).map(x => `<span class="tmpl-tag">${x}</span>`).join('');
  const rndUrl = t.source_repo ? 'https://gitcode.com/' + t.source_repo : '';
  const rndClick = rndUrl ? `window.open('${rndUrl}','_blank')` : `createFromTemplate('${t.id}','${t.default_repo_name || t.id}')`;
  return `<div class="tmpl-card tmpl-random tmpl-${tSc}" id="tmplRandomCard" onclick="${rndClick}">
    <span class="tmpl-card-glow"></span>
    <div class="tmpl-card-head">
      <span class="tmpl-card-ic">${t.icon || '📦'}<i></i></span>
      <span class="tmpl-random-badge">🎲 ${_tmplState.randIdx + 1}/${tmpls.length}</span>
    </div>
    <div class="tmpl-random-eyebrow">随机小项目 · 快速上手</div>
    <h3 title="${t.name}">${t.name}</h3>
    <p>${t.description || '暂无描述'}</p>
    <div class="tmpl-tags">${tags}</div>
    <div class="tmpl-random-actions">
      <button class="tmpl-btn tmpl-btn-shuffle" onclick="event.stopPropagation();randomPickTemplate(${tmpls.length})">🎲 换一个</button>
      <button class="tmpl-btn" onclick="event.stopPropagation();createFromTemplate('${t.id}','${t.default_repo_name || t.id}')">创建 <span class="tmpl-btn-ar">→</span></button>
    </div>
  </div>`;
}

function randomPickTemplate(total) {
  if (total > 1) {
    let next = _tmplState.randIdx;
    while (next === _tmplState.randIdx) {
      next = Math.floor(Math.random() * total);
    }
    _tmplState.randIdx = next;
  } else {
    _tmplState.randIdx = 0;
  }
  const cat = _tmplState.categories[_tmplState.activeCat];
  const card = document.getElementById('tmplRandomCard');
  if (card && cat) card.outerHTML = renderRandomExplorer(cat);
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
  API.get('/repos/check-name?name=' + encodeURIComponent(repoName))
    .then(check => {
      if (check.exists) {
        btn.disabled = false;
        btn.textContent = '创建';
        app.showToast('⚠️ 仓库 ' + check.full_name + ' 已存在，请更换仓库名');
        const input = document.getElementById('ptRepoName');
        if (input) { input.focus(); input.select(); }
        return;
      }
      API.post('/project-templates/create', {template_id: templateId, repo_name: repoName}).then(r => {
        const modal = document.getElementById('ptCreateModal');
        if (modal) modal.remove();
        app.showToast('✅ ' + (r.message || '创建成功'));
        const parts = (r.full_name || '').split('/');
        selectRepo(r.full_name, parts[0], parts[1]);
      }).catch(e => {
        btn.disabled = false;
        btn.textContent = '创建';
        app.showToast('创建失败: ' + e.message);
      });
    })
    .catch(err => {
      btn.disabled = false;
      btn.textContent = '创建';
      app.showToast('检查失败: ' + (err.message || err));
    });
}
