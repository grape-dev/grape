/**
 * scene-market.js — 场景·技能市场（场景 tab + 场景详情 + 场景编排）
 *
 * 一级 Tab：场景 / 技能（本组件渲染场景页，技能页由 skills.js 独立渲染）
 * 二级子 Tab：场景市场(official) / 仓库场景(repo)
 *
 * API:
 *   GET  /api/scenes/market                    列表 (q / category_id / sort / scope / repo)
 *   GET  /api/scenes/market/categories         分类 tab
 *   GET  /api/scenes/market/{id}               详情（含 scope + nodes_raw）
 *   POST /api/scenes/market                    创建场景（repo）
 *   PUT  /api/scenes/market/{id}               更新场景
 *   DELETE /api/scenes/market/{id}             删除场景
 *   POST /api/scenes/market/icon-upload        图标上传
 *   GET  /api/scenes/check-name                名称重复校验
 *   GET  /api/scenes/skills/{agent_type}       skill 列表（搜索+分页+scope过滤）
 *   POST /api/scenes/market/{id}/apply         应用
 *   POST /api/scenes/market/{id}/experience    体验
 *   POST /api/scenes/market/{id}/like          点赞 toggle
 */

// Agent 图标+颜色映射
const _AGENT_META = {
  vod:    {icon:'🔍', label:'VOD',    color:'#B07A8B', tag:'tg-v'},
  issue:  {icon:'📋', label:'Issue',  color:'#B8857A', tag:'tg-i'},
  coding: {icon:'💻', label:'Coding', color:'#7A92B0', tag:'tg-c'},
  qa:     {icon:'🧪', label:'QA',     color:'#9A85A8', tag:'tg-q'},
  sre:    {icon:'⚙️', label:'SRE',    color:'#B89B6A', tag:'tg-s'},
  chat:   {icon:'💬', label:'Chat',   color:'#34c759', tag:'tg-ch'},
};
const _AGENT_ORDER = ['vod','issue','coding','qa','sre'];

// 场景分类的背景渐变
const _CAT_BG = {
  1: 'bg-default', 2: 'bg-harmony', 3: 'bg-iac',
  4: 'bg-skill',  5: 'bg-agent',    6: 'bg-blog',
};

// 状态
let _sceneMarketCats = [];
let _sceneMarketScenes = [];
let _sceneMarketFeatured = [];
let _sceneMarketActiveCat = 0;
let _sceneMarketQ = '';
let _sceneMarketSort = 'latest';
let _sceneMarketPage = 1;
let _sceneMarketPageSize = 20;
let _sceneMarketTotal = 0;
let _sceneTab = 'official';  // official / repo

// 编排组件状态
let _sceneEditorNodes = [];
let _sceneEditorMode = '';   // 'create' / 'edit'
let _sceneEditorScope = '';  // 'repo'
let _sceneEditorSceneId = '';
let _sceneEditorIconUrl = '';
let _sceneEditorIconKey = '';
let _sceneEditorFileId = 0;
let _sceneEditorCats = [];
let _sceneEditorNameCheckTimer = null;

// ── 渲染：场景市场主页（一级 Tab + 二级子 Tab + 场景内容）──
function renderSceneMarket() {
  const el = document.getElementById('pageContent');
  if (!el) return;

  // 渲染一级 Tab 栏（场景 / 技能）+ 子容器
  el.innerHTML = `<div style="margin-bottom:20px;background:rgba(255,255,255,.72);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-radius:18px;padding:24px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.04)">
    <div class="seg-bar" style="margin-top:0">
      <a class="ac">🏪 ${I18N.t('skills.scenes_tab')||'场景'}</a>
      <a onclick="app.nav('skills')">⚙ ${I18N.t('skills.skills_tab')||'技能'}</a>
    </div>
    <div style="padding:14px 0 0;font-size:11px;color:var(--t3);line-height:1.6">
      <b style="color:var(--a)">${I18N.t('skills.scenes_tab')||'场景'}</b> ${I18N.t('skills.market_intro_scene')||'是多个技能按开发流程组合的开发模式'} · <b style="color:var(--a)">${I18N.t('skills.skills_tab')||'技能'}</b> ${I18N.t('skills.market_intro_skill')||'是单个 Agent 的能力包'}
    </div>
  </div>
  <div id="marketTabContent"></div>`;

  _switchSceneTab(_sceneTab);
}

// ── 二级子 Tab 切换 ──
function _sceneScopeName(scope) {
  var names = {
    official: I18N.t('skills.scene_scope_official')||'场景市场',
    repo: I18N.t('skills.scene_scope_repo')||'仓库场景',
  };
  return names[scope] || scope;
}

function _switchSceneTab(tab) {
  _sceneTab = tab;
  _sceneMarketPage = 1;
  _sceneMarketQ = '';
  _sceneMarketActiveCat = 0;
  // Issue #63: 页面内 tab 切换——取消旧 tab 请求 + 更新标识
  if (window._tabAbortController) window._tabAbortController.abort();
  window._tabAbortController = new AbortController();
  window._tabSignal = window._tabAbortController.signal;
  window._currentView = 'scene-market:' + tab;
  const el = document.getElementById('marketTabContent');
  if (!el) return;

  // 二级子 Tab 栏
  let html = `<div style="text-align:left;margin-bottom:20px"><div class="seg-bar" style="margin-top:0">
    <a class="${tab==='official'?'ac':''}" onclick="_switchSceneTab('official')">${_sceneScopeName('official')}</a>
    <a class="${tab==='repo'?'ac':''}" onclick="_switchSceneTab('repo')">${_sceneScopeName('repo')}</a>
  </div></div>
  <style>.scn-card{position:relative}.scn-card:hover .scn-card-overlay{display:flex !important}</style>`;

  if (tab === 'official') {
    // 场景市场：渲染容器，由 _loadSceneMarket 填充
    html += `<div id="sceneMarketList" style="min-height:600px"></div>`;
    el.innerHTML = html;
    _loadSceneMarket();
  } else {
    // repo 场景：渲染容器，由 _loadCustomScenes 填充
    html += `<div id="sceneMarketList" style="min-height:600px"></div>`;
    el.innerHTML = html;
    _loadCustomScenes(tab);
  }
}

// ── 加载场景市场（official）──
function _loadSceneMarket() {
  const el = document.getElementById('sceneMarketList');
  if (!el) return;
  // Issue #196: 立即渲染页面骨架（搜索栏/分类 tab/网格容器），避免全量 loading 清空导致闪烁
  _sceneMarketScenes = [];
  _sceneMarketTotal = 0;
  _sceneMarketFeatured = [];
  _renderSceneMarketPage();
  // 网格区域显示 loading spinner（不销毁已有 DOM 结构）
  var gridEl = document.getElementById('officialSceneGrid');
  if (gridEl) gridEl.innerHTML = '<div class="scn-loading"><span class="scn-spinner"></span></div>';
  // 分类列表非阻塞加载（加载完就重新渲染分类 tab）
  if (!_sceneMarketCats.length) {
    API.get('/scenes/market/categories').then(catData => {
      _sceneMarketCats = catData.categories || [];
      // 如果当前还在 official tab，重新渲染分类 tab
      if (_sceneTab === 'official') {
        const stabs = document.querySelector('#sceneMarketList .stabs');
        if (stabs) {
          const cats = [{id:0, name: I18N.t('skills.all_categories')||'全部'}].concat(_sceneMarketCats);
          stabs.innerHTML = cats.map(c => {
            const active = c.id === _sceneMarketActiveCat ? 'ac' : '';
            const icon = c.icon ? c.icon + ' ' : '';
            return `<a class="${active}" onclick="_sceneMarketActiveCat=${c.id};_sceneMarketPage=1;_loadSceneMarketList()">${icon}${c.name}</a>`;
          }).join('');
        }
      }
    }).catch(() => {});
  }
  // 精选场景非阻塞加载（局部更新精选区，不再全量重建页面）
  API.get('/scenes/market?sort=latest&limit=4&featured_only=1&scope=official').then(featData => {
    _sceneMarketFeatured = featData.scenes || [];
    if (_sceneTab === 'official') _renderFeaturedSection();
  }).catch(() => { _sceneMarketFeatured = []; _renderFeaturedSection(); });
  // 列表直接加载（不等精选和分类）
  _loadSceneMarketList();
}

function _loadSceneMarketList() {
  const el = document.getElementById('sceneMarketList');
  if (!el) return;
  let qs = '?sort=' + _sceneMarketSort + '&page=' + _sceneMarketPage + '&page_size=' + _sceneMarketPageSize + '&scope=official';
  if (_sceneMarketQ) qs += '&q=' + encodeURIComponent(_sceneMarketQ);
  if (_sceneMarketActiveCat) qs += '&category_id=' + _sceneMarketActiveCat;
  // 局部刷新：先显示 loading spinner
  var gridEl = document.getElementById('officialSceneGrid');
  if (gridEl) gridEl.innerHTML = '<div class="scn-loading"><span class="scn-spinner"></span></div>';
  API.get('/scenes/market' + qs).then(d => {
    if (window._currentView !== 'scene-market:official') return;  // stale guard
    _sceneMarketScenes = d.scenes || [];
    _sceneMarketTotal = d.total || 0;
    // 首次渲染完整页面，后续只刷新网格
    if (gridEl) {
      _renderOfficialSceneGrid();
    } else {
      _renderSceneMarketPage();
    }
  }).catch(e => {
    if (e.name === 'AbortError' && e.silent) return;  // Issue #63: abort 静默
    if (window._currentView !== 'scene-market:official') return;  // stale guard
    // Issue #196: 错误只更新网格区域，不破坏页面结构
    if (gridEl) {
      gridEl.innerHTML = '<div class="error" style="padding:20px;text-align:center">' + I18N.t('common.load_failed') + ': ' + (e.message||'') + '</div>';
    } else {
      el.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + (e.message||'') + '</div>';
    }
  });
}

// 局部刷新：只更新 official 场景网格 + 分页器 + 计数
function _renderOfficialSceneGrid() {
  const el = document.getElementById('officialSceneGrid');
  if (!el) return;
  const all = _sceneMarketScenes;

  var countEl = document.getElementById('sceneMarketTotalCount');
  if (countEl) countEl.textContent = _sceneMarketTotal;

  // 更新分类 tab 高亮
  var stabs = document.querySelectorAll('#sceneMarketList .stabs a');
  stabs.forEach(function(a) {
    var catId = parseInt(a.getAttribute('onclick').match(/_sceneMarketActiveCat=(\d+)/)?.[1] || '0');
    if (catId === _sceneMarketActiveCat) a.classList.add('ac');
    else a.classList.remove('ac');
  });

  // 更新排序按钮高亮
  _updateSortBtns('#sceneMarketList');

  let html = '';
  if (all.length === 0) {
    html = `<div class="ts t3" style="padding:20px;text-align:center">${I18N.t('skills.no_scenes')||'暂无场景'}</div>`;
    el.innerHTML = html;
    return;
  }

  html += `<div class="rgrid">${all.map(s => _renderSceneCard(s)).join('')}</div>`;

  const totalPages = Math.max(1, Math.ceil(_sceneMarketTotal / _sceneMarketPageSize));
  const prevDisabled = _sceneMarketPage <= 1 ? ' disabled' : '';
  const nextDisabled = _sceneMarketPage >= totalPages ? ' disabled' : '';
  html += `<div class="pager" style="display:flex;align-items:center;justify-content:center;gap:12px;margin-top:16px;padding:12px 0">
    <button class="bt bt-o bt-s${prevDisabled}" ${prevDisabled ? 'disabled' : ''} onclick="_sceneMarketPage=${Math.max(1,_sceneMarketPage-1)};_loadSceneMarketList()">‹</button>
    <span style="font-size:12px;color:var(--t2)">${_sceneMarketPage} / ${totalPages} (${_sceneMarketTotal}${I18N.t('skills.items_count_suffix')||'条'})</span>
    <button class="bt bt-o bt-s${nextDisabled}" ${nextDisabled ? 'disabled' : ''} onclick="_sceneMarketPage=${Math.min(totalPages,_sceneMarketPage+1)};_loadSceneMarketList()">›</button>
    <select onchange="_sceneMarketPageSize=parseInt(this.value);_sceneMarketPage=1;_loadSceneMarketList()" style="padding:4px 8px;border-radius:999px;font-size:11px;background:rgba(255,255,255,.72);color:var(--t);outline:none">
      <option value="20" ${_sceneMarketPageSize===20?'selected':''}>20${I18N.t('skills.per_page_suffix')||'/页'}</option>
      <option value="50" ${_sceneMarketPageSize===50?'selected':''}>50${I18N.t('skills.per_page_suffix')||'/页'}</option>
      <option value="100" ${_sceneMarketPageSize===100?'selected':''}>100${I18N.t('skills.per_page_suffix')||'/页'}</option>
    </select>
  </div>`;

  el.innerHTML = html;
}

// ── 加载仓库场景列表（repo）──
function _loadCustomScenes(scope) {
  const el = document.getElementById('sceneMarketList');
  if (!el) return;

  const repo = _currentRepoFullName();
  if (!repo) {
    el.innerHTML = `<div class="apple-card"><div class="ts t3" style="padding:20px;text-align:center">${I18N.t('skills.no_repo_selected')||'请先选择仓库'}</div></div>`;
    return;
  }

  // 首次渲染才覆盖整个区域为 loading；后续局部刷新只更新网格
  var existingGrid = document.getElementById('customSceneGrid');
  if (!existingGrid) {
    el.innerHTML = '<div id="customSceneList" class="loading">' + (I18N.t('common.loading')||'加载中...') + '</div>';
  } else {
    existingGrid.innerHTML = '<div class="scn-loading"><span class="scn-spinner"></span></div>';
  }

  // 先确保分类列表已加载
  var loadCats = _sceneMarketCats.length ? Promise.resolve({categories: _sceneMarketCats}) : API.get('/scenes/market/categories');
  var loadScenes = (function() {
    let qs = '?scope=' + scope + '&sort=' + _sceneMarketSort + '&page=' + _sceneMarketPage + '&page_size=' + _sceneMarketPageSize + '&repo=' + encodeURIComponent(repo);
    if (_sceneMarketQ) qs += '&q=' + encodeURIComponent(_sceneMarketQ);
    if (_sceneMarketActiveCat) qs += '&category_id=' + _sceneMarketActiveCat;
    return API.get('/scenes/market' + qs);
  })();

  Promise.all([loadScenes, loadCats]).then(([d, catData]) => {
    if (window._currentView !== 'scene-market:' + scope) return;  // stale guard
    _sceneMarketScenes = d.scenes || [];
    _sceneMarketTotal = d.total || 0;
    _sceneMarketCats = catData.categories || _sceneMarketCats;
    // 首次渲染时构建完整页面（含工具栏），后续只刷新列表区域
    var gridEl = document.getElementById('customSceneGrid');
    if (!gridEl) {
      // 首次：渲染完整页面
      var listEl = document.getElementById('customSceneList');
      if (listEl) listEl.classList.remove('loading');
      _renderCustomSceneList(scope);
    } else {
      // 后续：只刷新网格 + 分页器 + 计数
      _renderCustomSceneGrid(scope);
    }
  }).catch(e => {
    if (e.name === 'AbortError' && e.silent) return;  // Issue #63: abort 静默
    if (window._currentView !== 'scene-market:' + scope) return;  // stale guard
    const listEl = document.getElementById('customSceneList');
    if (listEl) {
      listEl.classList.remove('loading');
      listEl.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + (e.message||'') + '</div>';
    }
  });
}

function _renderCustomSceneList(scope) {
  const el = document.getElementById('customSceneList');
  if (!el) return;
  const all = _sceneMarketScenes;

  let html = `<div class="apple-card">`;
  // 标题行 + 搜索 + 排序 + 创建 + 导入
  html += `<div class="cd-h apple-search" style="justify-content:flex-start">
    <h2>${I18N.t('skills.all_scenes')||'全部场景'} <span id="sceneMarketTotalCount" style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:2px 8px;border-radius:8px;margin-left:4px">${_sceneMarketTotal}</span></h2>
    <input id="sceneMarketSearch" placeholder="${I18N.t('skills.search_placeholder')||'搜索场景名称或描述'}" value="${_sceneMarketQ}" onkeyup="if(event.key==='Enter'){_sceneMarketQ=this.value;_sceneMarketPage=1;_loadCustomScenes('${scope}')}" style="">
    <button class="bt bt-o bt-s" onclick="_sceneMarketQ=document.getElementById('sceneMarketSearch').value;_sceneMarketPage=1;_loadCustomScenes('${scope}')">🔍 ${I18N.t('skills.query')||'查询'}</button>
    <button class="bt ${_sceneMarketSort==='latest'?'bt-tg':'bt-o'} bt-s" id="sort-latest" onclick="_sceneMarketSort='latest';_sceneMarketPage=1;_loadCustomScenes('${scope}')">${I18N.t('skills.sort_latest')||'最新'}</button>
    <button class="bt bt-p bt-s" onclick="_enterSceneEditor('create','${scope}')" style="color:#fff;background:var(--g);border:none;border-radius:999px">＋ ${I18N.t('skills.create_scene')||'创建场景'}</button>
  </div>`;

  // 分类 tab
  const cats = [{id:0, name: I18N.t('skills.all_categories')||'全部'}].concat(_sceneMarketCats);
  html += `<div class="stabs" style="margin-bottom:12px">`;
  cats.forEach(c => {
    const active = c.id === _sceneMarketActiveCat ? 'ac' : '';
    const icon = c.icon ? c.icon + ' ' : '';
    html += `<a class="${active}" onclick="_sceneMarketActiveCat=${c.id};_sceneMarketPage=1;_loadCustomScenes('${scope}')">${icon}${c.name}</a>`;
  });
  html += `</div>`;

  // 场景网格 + 分页器容器（后续局部刷新只更新这里）
  html += `<div id="customSceneGrid"></div>`;
  html += `</div>`;

  el.innerHTML = html;
  // 渲染网格内容
  _renderCustomSceneGrid(scope);
}

// 局部刷新：只更新场景网格 + 分页器 + 计数（不重新渲染搜索栏/排序栏/分类 tab）
function _renderCustomSceneGrid(scope) {
  const el = document.getElementById('customSceneGrid');
  if (!el) return;
  const all = _sceneMarketScenes;

  // 更新计数
  var countEl = document.getElementById('sceneMarketTotalCount');
  if (countEl) countEl.textContent = _sceneMarketTotal;

  // 更新分类 tab 高亮
  var stabs = document.querySelectorAll('#customSceneList .stabs a');
  stabs.forEach(function(a) {
    var catId = parseInt(a.getAttribute('onclick').match(/_sceneMarketActiveCat=(\d+)/)?.[1] || '0');
    if (catId === _sceneMarketActiveCat) a.classList.add('ac');
    else a.classList.remove('ac');
  });

  // 更新排序按钮高亮
  _updateSortBtns('#customSceneList');

  let html = '';
  if (all.length === 0) {
    html = `<div class="ts t3" style="padding:20px;text-align:center">${I18N.t('skills.no_scenes_custom')||'暂无场景，点击创建'}</div>`;
    el.innerHTML = html;
    return;
  }

  html += `<div class="rgrid">${all.map(s => _renderSceneCard(s)).join('')}</div>`;

  // 分页器
  const totalPages = Math.max(1, Math.ceil(_sceneMarketTotal / _sceneMarketPageSize));
  const prevDisabled = _sceneMarketPage <= 1 ? ' disabled' : '';
  const nextDisabled = _sceneMarketPage >= totalPages ? ' disabled' : '';
  html += `<div class="pager" style="display:flex;align-items:center;justify-content:center;gap:12px;margin-top:16px;padding:12px 0">
    <button class="bt bt-o bt-s${prevDisabled}" ${prevDisabled ? 'disabled' : ''} onclick="_sceneMarketPage=${Math.max(1,_sceneMarketPage-1)};_loadCustomScenes('${scope}')">‹</button>
    <span style="font-size:12px;color:var(--t2)">${_sceneMarketPage} / ${totalPages} (${_sceneMarketTotal}${I18N.t('skills.items_count_suffix')||'条'})</span>
    <button class="bt bt-o bt-s${nextDisabled}" ${nextDisabled ? 'disabled' : ''} onclick="_sceneMarketPage=${Math.min(totalPages,_sceneMarketPage+1)};_loadCustomScenes('${scope}')">›</button>
    <select onchange="_sceneMarketPageSize=parseInt(this.value);_sceneMarketPage=1;_loadCustomScenes('${scope}')" style="padding:4px 8px;border-radius:999px;font-size:11px;background:rgba(255,255,255,.72);color:var(--t);outline:none">
      <option value="20" ${_sceneMarketPageSize===20?'selected':''}>20${I18N.t('skills.per_page_suffix')||'/页'}</option>
      <option value="50" ${_sceneMarketPageSize===50?'selected':''}>50${I18N.t('skills.per_page_suffix')||'/页'}</option>
      <option value="100" ${_sceneMarketPageSize===100?'selected':''}>100${I18N.t('skills.per_page_suffix')||'/页'}</option>
    </select>
  </div>`;

  el.innerHTML = html;
}

// ── 渲染场景市场主页（official）──
function _renderSceneMarketPage() {
  const el = document.getElementById('sceneMarketList');
  if (!el) return;
  const featured = _sceneMarketFeatured;
  const all = _sceneMarketScenes;

  let html = '';

  // 精选场景：≥4 张铺满一行，≤3 张横向滚动（可留白）
  if (featured.length > 0) {
    html += `<div id="sceneMarketFeatured" class="apple-card">${_renderFeaturedInner(featured)}</div>`;
  }

  // 全部场景
  html += `<div id="sceneMarketAllScenes" class="apple-card">`;
  html += `<div class="cd-h apple-search" style="justify-content:flex-start">
    <h2>${I18N.t('skills.all_scenes')||'全部场景'} <span id="sceneMarketTotalCount" style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:2px 8px;border-radius:8px;margin-left:4px">${_sceneMarketTotal}</span></h2>
    <input id="sceneMarketSearch" placeholder="${I18N.t('skills.search_placeholder')||'搜索场景名称或描述'}" value="${_sceneMarketQ}" onkeyup="if(event.key==='Enter'){_sceneMarketQ=this.value;_sceneMarketPage=1;_loadSceneMarketList()}">
      <button class="bt bt-p bt-s" onclick="_sceneMarketQ=document.getElementById('sceneMarketSearch').value;_sceneMarketPage=1;_loadSceneMarketList()">${I18N.t('skills.query')||'查询'}</button>
      <button class="bt ${_sceneMarketSort==='latest'?'bt-tg':'bt-o'} bt-s" onclick="_sceneMarketSort='latest';_sceneMarketPage=1;_loadSceneMarketList()">${I18N.t('skills.sort_latest')||'最新'}</button>
      <button class="bt ${_sceneMarketSort==='hot'?'bt-tg':'bt-o'} bt-s" onclick="_sceneMarketSort='hot';_sceneMarketPage=1;_loadSceneMarketList()">${I18N.t('skills.sort_hot')||'最热'}</button>
  </div>`;

  // 分类 tab
  const cats = [{id:0, name: I18N.t('skills.all_categories')||'全部'}].concat(_sceneMarketCats);
  html += `<div class="stabs" style="margin-bottom:12px">`;
  cats.forEach(c => {
    const active = c.id === _sceneMarketActiveCat ? 'ac' : '';
    const icon = c.icon ? c.icon + ' ' : '';
    html += `<a class="${active}" onclick="_sceneMarketActiveCat=${c.id};_sceneMarketPage=1;_loadSceneMarketList()">${icon}${c.name}</a>`;
  });
  html += `</div>`;

  // 场景网格容器（后续局部刷新只更新这里）
  html += `<div id="officialSceneGrid">`;

  // 场景列表网格
  if (all.length === 0) {
    html += `<div class="ts t3" style="padding:20px;text-align:center">${I18N.t('skills.no_scenes')||'暂无场景'}</div>`;
  } else {
    html += `<div class="rgrid">${all.map(s => _renderSceneCard(s)).join('')}</div>`;
  }

  // 分页器
  const totalPages = Math.max(1, Math.ceil(_sceneMarketTotal / _sceneMarketPageSize));
  const prevDisabled = _sceneMarketPage <= 1 ? ' disabled' : '';
  const nextDisabled = _sceneMarketPage >= totalPages ? ' disabled' : '';
  html += `<div class="pager" style="display:flex;align-items:center;justify-content:center;gap:12px;margin-top:16px;padding:12px 0">
    <button class="bt bt-o bt-s${prevDisabled}" ${prevDisabled ? 'disabled' : ''} onclick="_sceneMarketPage=${Math.max(1,_sceneMarketPage-1)};_loadSceneMarketList()">‹</button>
    <span style="font-size:12px;color:var(--t2)">${_sceneMarketPage} / ${totalPages} (${_sceneMarketTotal}${I18N.t('skills.items_count_suffix')||'条'})</span>
    <button class="bt bt-o bt-s${nextDisabled}" ${nextDisabled ? 'disabled' : ''} onclick="_sceneMarketPage=${Math.min(totalPages,_sceneMarketPage+1)};_loadSceneMarketList()">›</button>
    <select onchange="_sceneMarketPageSize=parseInt(this.value);_sceneMarketPage=1;_loadSceneMarketList()" style="padding:4px 8px;border-radius:999px;font-size:11px;background:rgba(255,255,255,.72);color:var(--t);outline:none">
      <option value="20" ${_sceneMarketPageSize===20?'selected':''}>20${I18N.t('skills.per_page_suffix')||'/页'}</option>
      <option value="50" ${_sceneMarketPageSize===50?'selected':''}>50${I18N.t('skills.per_page_suffix')||'/页'}</option>
      <option value="100" ${_sceneMarketPageSize===100?'selected':''}>100${I18N.t('skills.per_page_suffix')||'/页'}</option>
    </select>
  </div>`;
  html += `</div>`; // 关闭 officialSceneGrid
  html += `</div>`; // 关闭 apple-card

  el.innerHTML = html;
}

// 精选区内部 HTML：对齐精选技能 feat-row 布局
function _renderFeaturedInner(featured) {
  const cards = featured.map(s => _renderSceneCard(s)).join('');
  const head = `<div class="cd-h"><h2>🌟 ${I18N.t('skills.featured_scenes')||'精选场景'}</h2></div>`;
  return head + `<div id="featured-scenes" class="feat-row">${cards}</div>`;
}

// Issue #196: 精选区局部刷新（不重建整个页面）
function _renderFeaturedSection() {
  const featured = _sceneMarketFeatured;
  const existing = document.getElementById('sceneMarketFeatured');
  const allScenesCard = document.getElementById('sceneMarketAllScenes');
  const container = document.getElementById('sceneMarketList');
  if (!container) return;

  // 无精选数据 → 移除已有精选区
  if (featured.length === 0) {
    if (existing) existing.remove();
    return;
  }

  const innerHtml = _renderFeaturedInner(featured);

  if (existing) {
    existing.innerHTML = innerHtml;
  } else if (allScenesCard) {
    const wrapper = document.createElement('div');
    wrapper.id = 'sceneMarketFeatured';
    wrapper.className = 'apple-card';
    wrapper.innerHTML = innerHtml;
    container.insertBefore(wrapper, allScenesCard);
  }
}

// 精选区滚动按钮动态显示/隐藏
function _updateScrollBtns(trackId) {
  const track = document.getElementById(trackId);
  if (!track) return;
  const prev = document.getElementById(trackId + '-prev');
  const next = document.getElementById(trackId + '-next');
  const canScrollLeft = track.scrollLeft > 5;
  const canScrollRight = track.scrollLeft + track.clientWidth < track.scrollWidth - 5;
  if (prev) prev.classList.toggle('show', canScrollLeft);
  if (next) next.classList.toggle('show', canScrollRight);
}

window.addEventListener('resize', function() {
  ['featured-skills'].forEach(function(id) {
    if (document.getElementById(id)) _updateScrollBtns(id);
  });
});

// ── 渲染场景卡片（按 scope 显示不同操作按钮）──
function _renderSceneCard(s) {
  const catBg = _CAT_BG[s.category_id] || 'bg-default';
  const skills = s.skills || [];
  const chain = skills.map((sk, i) => {
    const name = sk.name || sk.skill || sk;
    return `${i>0?'<span class="scn-arr">→</span>':''}<span class="scn-tag">${name}</span>`;
  }).join('');
  const likeIcon = s.liked_by_me ? '❤️' : '🤍';
  const scope = s.scope || 'official';

  // 图标处理：如果是 URL 或路径，用 img 显示；否则当 emoji 文本
  const iconVal = s.icon || '⚙';
  const iconHtml = (iconVal.startsWith('http') || iconVal.startsWith('/static/') || iconVal.startsWith('/uploads/'))
    ? `<img src="${iconVal}" style="width:100%;height:100%;border-radius:inherit;object-fit:cover" onerror="this.style.display='none';this.parentElement.innerText='${s.category_icon||'⚙'}'">`
    : iconVal;

  // 操作按钮按 scope 区分
  let btns = '';
  if (scope === 'official') {
    btns = `<div class="scn-card-btns">
      <button class="bt bt-o bt-s" onclick="event.stopPropagation();_applyScene(${s.id},'${s.name}','${(s.skills||[]).join(',')}')">${I18N.t('skills.apply_scene')||'应用'}</button>
      ${s.recommended_template ? `<button class="bt bt-o bt-s" onclick="event.stopPropagation();_experienceScene(${s.id})">${I18N.t('skills.experience')||'体验'}</button>` : ''}
    </div>`;
  } else {
    // repo: 底部只放主操作，编辑/删除移到标题行 ⋯ 菜单
    btns = `<div class="scn-card-btns">
      <button class="bt bt-o bt-s" onclick="event.stopPropagation();_applyScene(${s.id},'${s.name}','${(s.skills||[]).join(',')}')">${I18N.t('skills.apply_scene')||'应用'}</button>
      ${s.recommended_template ? `<button class="bt bt-o bt-s" onclick="event.stopPropagation();_experienceScene(${s.id})">${I18N.t('skills.experience')||'体验'}</button>` : ''}
    </div>`;
  }

  // repo 不显示点赞
  const likeHtml = scope === 'official'
    ? `<span style="cursor:pointer;font-size:11px;color:var(--t2);display:flex;align-items:center;gap:3px" onclick="event.stopPropagation();_toggleSceneLike(${s.id},this)">${likeIcon}<span style="font-size:11px">${s.like_count||0}</span></span>`
    : '';

  return `<div class="scn-card" onclick="app.nav('scene-detail','${s.id}')">
    <div class="scn-card-bar ${catBg}"></div>
    <div class="scn-card-body">
      <div class="scn-card-head">
        <div class="scn-ic ${catBg}">${iconHtml}</div>
        <div class="scn-card-title">${s.display_name || s.name}</div>
        ${scope==='official' && s.is_featured?'<span class="scn-star">🌟</span>':''}
      </div>
      <div class="scn-card-desc" title="${(s.market_description || s.description || '').replace(/"/g, '&quot;')}">${s.market_description || s.description || ''}</div>
      <div class="scn-chain">${chain}</div>
      <div class="scn-card-foot">
        <span class="scn-meta">${skills.length} ${I18N.t('skills.skills_suffix')||'技能'}${scope==='official' ? ' · ' + (s.repo_count||0) + ' ' + (I18N.t('skills.repos_in_use')||'仓库在用') : ''}</span>
        ${likeHtml}
        ${btns}
      </div>
    </div>
    ${scope!=='official' ? `<div class="scn-card-overlay" onclick="event.stopPropagation()" style="position:absolute;top:10px;right:10px;display:none;flex-direction:row;gap:1px;background:rgba(255,255,255,0.72);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-radius:14px;padding:3px;box-shadow:0 1px 3px rgba(0,0,0,.04);z-index:10">
      <button onclick="event.stopPropagation();_editScene(${s.id})" title="${I18N.t('skills.edit_scene')||'编辑'}" style="padding:6px 10px;font-size:14px;border:none;background:transparent;color:#1d1d1f;cursor:pointer;border-radius:999px;transition:background .15s" onmouseover="this.style.background='rgba(0,0,0,0.06)'" onmouseout="this.style.background='transparent'">✎</button>
      <button onclick="event.stopPropagation();_deleteScene(${s.id})" title="${I18N.t('skills.delete_scene')||'删除'}" style="padding:6px 10px;font-size:14px;border:none;background:transparent;color:#ff3b30;cursor:pointer;border-radius:999px;transition:background .15s" onmouseover="this.style.background='rgba(255,59,48,0.08)'" onmouseout="this.style.background='transparent'">🗑</button>
    </div>` : ''}
  </div>`;
}

// ── 卡片更多操作菜单（编辑/删除）──
function _showCardMenu(sceneId, el) {
  // 移除已有菜单
  var existing = document.getElementById('cardMenu');
  if (existing) { existing.remove(); return; }
  
  var rect = el.getBoundingClientRect();
  var menu = document.createElement('div');
  menu.id = 'cardMenu';
  menu.style.cssText = 'position:fixed;top:' + (rect.bottom + 4) + 'px;left:' + rect.right + 'px;transform:translateX(-100%);background:rgba(255,255,255,0.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-radius:18px;box-shadow:0 8px 30px rgba(0,0,0,.12);z-index:9999;overflow:hidden;min-width:120px';
  
  menu.innerHTML = 
    '<div onclick="_editScene(' + sceneId + ');document.getElementById(\'cardMenu\').remove()" style="padding:10px 16px;cursor:pointer;font-size:13px;color:#1d1d1f" onmouseover="this.style.background=\'rgba(0,0,0,0.04)\'" onmouseout="this.style.background=\'transparent\'">' + (I18N.t('skills.edit_scene')||'编辑') + '</div>' +
    '<div onclick="_deleteScene(' + sceneId + ');document.getElementById(\'cardMenu\').remove()" style="padding:10px 16px;cursor:pointer;font-size:13px;color:#ff3b30" onmouseover="this.style.background=\'rgba(0,0,0,0.04)\'" onmouseout="this.style.background=\'transparent\'">' + (I18N.t('skills.delete_scene')||'删除') + '</div>';
  
  document.body.appendChild(menu);
  
  // 点击外部关闭
  setTimeout(function() {
    document.addEventListener('click', function closeMenu(e) {
      if (e.target.id !== 'cardMenu' && !menu.contains(e.target)) {
        menu.remove();
        document.removeEventListener('click', closeMenu);
      }
    });
  }, 10);
}

// ── 场景点赞 ──
function _toggleSceneLike(sceneId, el) {
  API.post('/scenes/market/' + sceneId + '/like', {}).then(r => {
    if (el) {
      el.innerHTML = (r.liked ? '❤️' : '🤍') + ' ' + (r.like_count || 0);
    }
  }).catch(e => {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.like_failed')||'操作失败');
  });
}

// ── 场景应用（带高级选项弹窗） ──
function _applyScene(sceneId, sceneName, sceneSkills) {
  const repo = _currentRepoFullName();
  if (!repo) {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.select_repo_first')||'请先选择仓库');
    return;
  }
  const parts = repo.split('/');
  const owner = encodeURIComponent(parts[0]);
  const repoName = encodeURIComponent(parts[1]);
  // sceneSkills 可能是逗号分隔字符串，转回数组
  var skillsArr = typeof sceneSkills === 'string' ? (sceneSkills ? sceneSkills.split(',') : []) : (Array.isArray(sceneSkills) ? sceneSkills : []);
  // 判断是否为华为云 Skill 场景（name=skill-dev 或 skills 含 huawei-cloud）
  var isHwCloudScene = (sceneName === 'huawei-skill') || (skillsArr.indexOf('huawei-cloud') >= 0);
  // 先检查是否已应用过，然后统一弹高级选项弹窗
  API.get('/scenes/market/' + sceneId + '/applied?owner=' + owner + '&repo=' + repoName).then(check => {
    _showApplyOptionsModal(sceneId, owner, repoName, check.applied, check.scene_name, isHwCloudScene);
  }).catch(() => {
    _showApplyOptionsModal(sceneId, owner, repoName, false, '', isHwCloudScene);
  });
}

// 高级选项弹窗：工作分支 + 自定义配置项
function _showApplyOptionsModal(sceneId, owner, repoName, isOverwrite, sceneName, isHwCloudScene) {
  var modal = document.createElement('div');
  modal.id = 'applyOptionsModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)';

  var title = isOverwrite
    ? (I18N.t('skills.apply_confirm_title')||'确认覆盖应用')
    : (I18N.t('skills.apply_scene')||'应用场景');
  var warnHtml = isOverwrite
    ? '<div style="display:flex;align-items:center;gap:8px;margin-bottom:16px;padding:12px 14px;background:rgba(255,59,48,.06);border-radius:12px">' +
        '<span style="font-size:16px;flex-shrink:0;line-height:1">⚠️</span>' +
        '<div style="font-size:12px;color:var(--t2);line-height:1.6">' +
          (I18N.t('skills.apply_confirm_desc')||'当前仓库已应用过此场景') + (sceneName ? '（' + sceneName + '）' : '') + '，' +
          (I18N.t('skills.apply_confirm_warning')||'再次应用将覆盖更新已有的场景配置。') +
        '</div>' +
      '</div>'
    : '';

  modal.innerHTML =
    '<style>' +
      '#applyOptionsModal .ap-btn{padding:7px 18px;border-radius:999px;font-size:12px;font-weight:500;cursor:pointer;transition:all .2s ease;background:transparent;border:1px solid rgba(0,113,227,.3);color:#0071e3}' +
      '#applyOptionsModal .ap-btn:hover{background:#0071e3;border-color:#0071e3;color:#fff}' +
      '#applyOptionsModal .ap-btn-d{border:1px solid rgba(255,59,48,.3);color:#ff3b30}' +
      '#applyOptionsModal .ap-btn-d:hover{background:#ff3b30;border-color:#ff3b30;color:#fff}' +
    '</style>' +
    '<div class="cd" style="width:600px;max-height:92vh;overflow-y:auto;padding:24px;border-radius:18px;background:rgba(255,255,255,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 8px 30px rgba(0,0,0,.12)">' +
      '<h3 style="font-size:15px;font-weight:600;margin-bottom:14px">' + title + '</h3>' +
      warnHtml +
      // 工作分支
      '<div style="margin-bottom:16px">' +
        '<div style="font-size:12px;font-weight:600;color:#86868b;margin-bottom:8px">' + (I18N.t('dashboard.work_branch')||'工作分支') + '</div>' +
        '<div style="display:flex;gap:8px;align-items:center">' +
          '<select id="applyAdvWorkBranch" style="flex:1;padding:8px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;background:#f5f5f7;color:#1d1d1f;outline:none;cursor:pointer;transition:all .15s" onfocus="this.style.borderColor=\'#0071e3\';this.style.background=\'#fff\'" onblur="this.style.borderColor=\'#e8e8ed\';this.style.background=\'#f5f5f7\'"><option value="">' + (I18N.t('common.loading')||'加载中...') + '</option></select>' +
          '<button class="ap-btn" onclick="_applyToggleNewBranch()">' + (I18N.t('dashboard.create_branch')||'新建分支') + '</button>' +
        '</div>' +
        '<div id="applyNewBranchInput" style="display:none;padding:8px 0 0">' +
          '<div style="display:flex;gap:8px">' +
            '<input id="applyNewBranchName" placeholder="' + (I18N.t('dashboard.branch_name')||'分支名') + '" style="flex:1;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;outline:none;background:#f5f5f7;transition:all .15s" onfocus="this.style.borderColor=\'#0071e3\';this.style.background=\'#fff\'" onblur="this.style.borderColor=\'#e8e8ed\';this.style.background=\'#f5f5f7\'">' +
            '<select id="applyNewBranchRef" style="width:120px;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;background:#f5f5f7;outline:none;cursor:pointer"></select>' +
            '<button class="ap-btn" onclick="_applyCreateNewBranch(\'' + owner + '\',\'' + repoName + '\')">' + (I18N.t('common.create')||'创建') + '</button>' +
            '<button class="ap-btn" onclick="_applyToggleNewBranch()">' + (I18N.t('common.cancel')||'取消') + '</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      // 自定义配置项
      '<div style="margin-bottom:16px">' +
        '<div style="font-size:12px;font-weight:600;color:#86868b;margin-bottom:8px">' + (I18N.t('dashboard.custom_config')||'自定义配置') + '</div>' +
        '<div id="applyAdvCustomFields" style="display:flex;flex-direction:column;gap:8px"></div>' +
        '<button class="ap-btn" style="margin-top:8px;font-size:11px" onclick="_applyAddCustomField()">+ ' + (I18N.t('dashboard.add_config')||'添加配置项') + '</button>' +
      '</div>' +
      // 底部按钮
      '<div style="display:flex;justify-content:flex-end;gap:8px;padding-top:8px;border-top:1px solid rgba(0,0,0,.06)">' +
        '<button class="bt bt-o bt-s" id="applyCancelBtn">' + (I18N.t('common.cancel')||'取消') + '</button>' +
        '<button class="bt bt-p bt-s" id="applyConfirmBtn">' + (isOverwrite ? (I18N.t('skills.apply_confirm_btn')||'确认覆盖') : (I18N.t('skills.apply_scene')||'应用')) + '</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(modal);

  // 关闭
  document.getElementById('applyCancelBtn').onclick = function() { modal.remove(); };
  // 点击遮罩关闭
  modal.onclick = function(e) { if (e.target === modal) modal.remove(); };

  // 新建分支切换
  window._applyToggleNewBranch = function() {
    var el = document.getElementById('applyNewBranchInput');
    if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
  };

  // 添加自定义配置项
  window._applyAddCustomField = function(key, value, readOnlyKey) {
    var container = document.getElementById('applyAdvCustomFields');
    if (!container) return;
    var row = document.createElement('div');
    row.className = 'adv-kv';
    row.style.cssText = 'display:flex;gap:8px;align-items:center';
    var keyInputAttrs = readOnlyKey ? 'readonly style="flex:1;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;outline:none;background:#eee;color:#86868b;cursor:not-allowed"' : 'style="flex:1;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;outline:none;background:#f5f5f7;transition:all .15s" onfocus="this.style.borderColor=\'#0071e3\';this.style.background=\'#fff\'" onblur="this.style.borderColor=\'#e8e8ed\';this.style.background=\'#f5f5f7\'"';
    var valId = 'advVal_' + Math.random().toString(36).slice(2,8);
    // 有值（回填/华为云注入）→ 默认隐藏；空值（手动新增）→ 默认可见方便输入
    var hasValue = !!(value && String(value).trim());
    var valType = (readOnlyKey || hasValue) ? 'password' : 'text';
    var eyeIcon = (readOnlyKey || hasValue) ? '👁' : '🙈';
    row.innerHTML =
      '<input class="adv-kv-key" placeholder="key" ' + keyInputAttrs + ' value="' + (key || '') + '">' +
      '<input class="adv-kv-val" id="' + valId + '" type="' + valType + '" placeholder="value" style="flex:1;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;outline:none;background:#f5f5f7;transition:all .15s" onfocus="this.style.borderColor=\'#0071e3\';this.style.background=\'#fff\'" onblur="this.style.borderColor=\'#e8e8ed\';this.style.background=\'#f5f5f7\'" value="' + (value || '') + '">' +
      '<button type="button" style="border:1px solid #e8e8ed;background:#f5f5f7;border-radius:8px;width:30px;height:30px;padding:0;font-size:14px;cursor:pointer;color:#86868b;flex-shrink:0" onclick="(function(b){var i=document.getElementById(\'' + valId + '\');if(i){i.type=i.type===\'password\'?\'text\':\'password\';b.textContent=i.type===\'password\'?\'👁\':\'🙈\';}})(this)">' + eyeIcon + '</button>' +
      '<button class="ap-btn ap-btn-d" style="font-size:14px;padding:4px 10px" onclick="this.parentElement.remove()">×</button>';
    container.appendChild(row);
  };

  // 华为云 Skill 场景：自动注入 AK/SK 配置项
  if (isHwCloudScene) {
    // 先添加提示
    var hwHint = document.createElement('div');
    hwHint.id = 'hwCloudCredsHint';
    hwHint.style.cssText = 'padding:10px 14px;background:rgba(0,113,227,0.06);border-radius:10px;font-size:11px;color:#86868b;line-height:1.6;margin-bottom:8px';
    hwHint.innerHTML = '☁ 华为云场景需要 AK/SK 凭证。如已配置将自动填入，也可在此直接填写。' +
      ' <a href="javascript:void(0)" onclick="app.nav(\'settings\')" style="color:#0071e3;cursor:pointer">前往设置</a>';
    var customFieldsContainer = document.getElementById('applyAdvCustomFields');
    if (customFieldsContainer) {
      customFieldsContainer.parentNode.insertBefore(hwHint, customFieldsContainer);
    }
    // 从用户设置中获取已配置的 AK/SK
    API.get('/settings/credentials').then(function(credData) {
      var hw = (credData && credData.huawei_cloud) || {};
      var ak = hw.ak || '';
      var sk = hw.sk || '';
      // 添加 AK 行（key 只读）
      window._applyAddCustomField('HUAWEICLOUD_SDK_AK', ak, true);
      // 添加 SK 行（key 只读）
      window._applyAddCustomField('HUAWEICLOUD_SDK_SK', sk, true);
    }).catch(function() {
      // 获取失败也添加空行，让用户手动填写
      window._applyAddCustomField('HUAWEICLOUD_SDK_AK', '', true);
      window._applyAddCustomField('HUAWEICLOUD_SDK_SK', '', true);
    });
  }

  // 创建新分支
  window._applyCreateNewBranch = function(o, n) {
    var branchName = (document.getElementById('applyNewBranchName') || {}).value || '';
    var refs = (document.getElementById('applyNewBranchRef') || {}).value || 'main';
    if (!branchName.trim()) { app.showToast && app.showToast(I18N.t('dashboard.branch_name_empty')||'分支名不能为空'); return; }
    API.post('/scenes/branches/' + o + '/' + n, { branch_name: branchName.trim(), refs: refs })
      .then(function() {
        app.showToast && app.showToast(I18N.t('dashboard.branch_created')||'分支已创建');
        return API.get('/scenes/branches/' + o + '/' + n);
      })
      .then(function(b) {
        var branches = (b && b.branches) || [];
        var sel = document.getElementById('applyAdvWorkBranch');
        if (!sel) return;
        sel.innerHTML = branches.map(function(br) { var s = br.name === branchName.trim() ? ' selected' : ''; return '<option value="' + br.name + '"' + s + '>' + br.name + (br.protected ? ' 🔒' : '') + '</option>'; }).join('');
        var refSel = document.getElementById('applyNewBranchRef');
        if (refSel) refSel.innerHTML = branches.map(function(br) { return '<option value="' + br.name + '">' + br.name + '</option>'; }).join('');
        document.getElementById('applyNewBranchName').value = '';
        window._applyToggleNewBranch();
      })
      .catch(function(e) { app.showToast && app.showToast((e && e.detail && e.detail.message) || (e && e.message) || '创建失败'); });
  };

  // 加载仓库分支列表
  API.get('/scenes/branches/' + owner + '/' + repoName).then(function(b) {
    var branches = (b && b.branches) || [];
    var sel = document.getElementById('applyAdvWorkBranch');
    if (!sel) return; // 弹窗可能已关闭
    var defaultBranch = 'main';
    if (branches.length) {
      sel.innerHTML = branches.map(function(br) { var s = br.name === defaultBranch ? ' selected' : ''; return '<option value="' + br.name + '"' + s + '>' + br.name + (br.protected ? ' 🔒' : '') + '</option>'; }).join('');
    } else {
      sel.innerHTML = '<option value="main">main</option>';
    }
    var refSel = document.getElementById('applyNewBranchRef');
    if (refSel) refSel.innerHTML = branches.map(function(br) { return '<option value="' + br.name + '">' + br.name + '</option>'; }).join('');
  }).catch(function() {
    var sel = document.getElementById('applyAdvWorkBranch');
    if (sel) sel.innerHTML = '<option value="main">main</option>';
  });

  // 如果是覆盖应用，加载已有的高级选项数据回填
  if (isOverwrite) {
    var repoFull = decodeURIComponent(owner) + '/' + decodeURIComponent(repoName);
    API.get('/scenes/with-pipeline?repo=' + encodeURIComponent(repoFull)).then(function(pipeData) {
      var scenes = (pipeData && pipeData.scenes) || [];
      var target = sceneName ? scenes.find(function(s) { return s.name === sceneName || s.display_name === sceneName; }) : null;
      if (!target) return;
      var sceneIdLocal = target.id;
      return API.get('/scenes/' + encodeURIComponent(sceneIdLocal) + '/advanced-options?repo=' + encodeURIComponent(repoFull));
    }).then(function(d) {
      if (!d) return;
      var sel = document.getElementById('applyAdvWorkBranch');
      if (sel && d.work_branch) {
        var opts = sel.querySelectorAll('option');
        var found = false;
        opts.forEach(function(opt) { if (opt.value === d.work_branch) { opt.selected = true; found = true; } });
        if (!found) {
          var newOpt = document.createElement('option');
          newOpt.value = d.work_branch;
          newOpt.textContent = d.work_branch;
          newOpt.selected = true;
          sel.insertBefore(newOpt, sel.firstChild);
        }
      }
      var fields = d.custom_fields || [];
      // 华为云场景已自动注入 AK/SK 行，回填时跳过这些 key 避免重复
      var hwKeys = isHwCloudScene ? ['HUAWEICLOUD_SDK_AK', 'HUAWEICLOUD_SDK_SK'] : [];
      fields.forEach(function(f) {
        if (hwKeys.indexOf(f.key) >= 0) {
          // 已有 AK/SK 行 → 只更新 value，不新增行
          var rows = document.querySelectorAll('#applyAdvCustomFields .adv-kv');
          for (var i = 0; i < rows.length; i++) {
            var k = (rows[i].querySelector('.adv-kv-key') || {}).value || '';
            if (k === f.key) {
              var valInput = rows[i].querySelector('.adv-kv-val');
              if (valInput) valInput.value = f.value || '';
              return;
            }
          }
        }
        window._applyAddCustomField(f.key, f.value);
      });
    }).catch(function() {});
  }

  // 确认按钮
  document.getElementById('applyConfirmBtn').onclick = function() {
    var workBranch = (document.getElementById('applyAdvWorkBranch') || {}).value || '';
    if (!workBranch) { app.showToast && app.showToast(I18N.t('dashboard.branch_required')||'工作分支不能为空'); return; }
    var fields = [];
    document.querySelectorAll('#applyAdvCustomFields .adv-kv').forEach(function(row) {
      var k = (row.querySelector('.adv-kv-key') || {}).value || '';
      var v = (row.querySelector('.adv-kv-val') || {}).value || '';
      if (k.trim()) fields.push({ key: k.trim(), value: v.trim() });
    });
    modal.remove();
    _doApplySceneWithOptions(sceneId, owner, repoName, workBranch, fields);
  };
}

// 应用场景 + 保存高级选项
function _doApplySceneWithOptions(sceneId, owner, repoName, workBranch, customFields) {
  var repoFull = decodeURIComponent(owner) + '/' + decodeURIComponent(repoName);
  API.post('/scenes/market/' + sceneId + '/apply?owner=' + owner + '&repo=' + repoName).then(function(r) {
    var sceneIdLocal = r.scene_id;
    if (!sceneIdLocal) {
      if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.apply_success')||'场景已应用');
      app.nav('dash');
      return;
    }
    return API.put('/scenes/' + encodeURIComponent(sceneIdLocal) + '/advanced-options?repo=' + encodeURIComponent(repoFull), {
      work_branch: workBranch,
      custom_fields: customFields
    }).then(function() {
      if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.apply_success')||'场景已应用');
      app.nav('dash');
    }).catch(function(e) {
      console.warn('Advanced options save failed:', e);
      if (app && app.showToast) app.showToast((r.message || I18N.t('skills.toast.apply_success')||'场景已应用') + '（高级选项保存失败）');
      app.nav('dash');
    });
  }).catch(function(e) {
    if (e.status === 409) {
      _showSceneNameConflict(e.message);
    } else {
      if (app && app.showToast) app.showToast((I18N.t('skills.toast.apply_failed')||'应用失败') + ': ' + (e.message||''));
    }
  });
}

// ── 场景名称冲突提示 ──
function _showSceneNameConflict(message, fullName) {
  var modal = document.createElement('div');
  modal.id = 'sceneNameConflictModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)';
  modal.innerHTML = '<div class="cd" style="width:400px;padding:24px;border-radius:18px;background:rgba(255,255,255,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 8px 30px rgba(0,0,0,.12)">' +
    '<div style="display:flex;align-items:flex-start;gap:12px;margin-bottom:14px">' +
      '<span style="font-size:22px;flex-shrink:0">⚠️</span>' +
      '<div>' +
        '<h3 style="font-size:15px;font-weight:600;margin-bottom:6px">' + (I18N.t('skills.scene_name_conflict_title')||'场景名称冲突') + '</h3>' +
        '<div style="font-size:12px;color:var(--t2);line-height:1.6">' + (message || I18N.t('skills.scene_name_conflict_desc')||'仓库中已存在同名场景，请先修改已有场景名称') + '</div>' +
      '</div>' +
    '</div>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px">' +
      '<button class="bt bt-o bt-s" id="conflictCloseBtn">' + (I18N.t('common.close')||'关闭') + '</button>' +
      '<button class="bt bt-p bt-s" id="conflictGotoBtn">' + (I18N.t('skills.goto_repo_scenes')||'去修改场景') + '</button>' +
    '</div>' +
  '</div>';
  document.body.appendChild(modal);
  document.getElementById('conflictCloseBtn').onclick = function() { modal.remove(); };
  document.getElementById('conflictGotoBtn').onclick = function() {
    modal.remove();
    if (fullName) {
      _selectRepoAndNav(fullName);
    } else {
      app.nav('dash');
    }
  };
  modal.onclick = function(e) { if (e.target === modal) modal.remove(); };
}

// ── 场景体验 ──
function _experienceScene(sceneId) {
  API.get('/scenes/market/' + sceneId + '/experience-check').then(function(check) {
    if (check.repo_exists && check.scene_applied) {
      _showExperienceConfirm(sceneId, check.full_name, check.scene_name, true);
    } else if (check.repo_exists) {
      _showExperienceConfirm(sceneId, check.full_name, '', false);
    } else {
      _doExperienceScene(sceneId);
    }
  }).catch(function() {
    _doExperienceScene(sceneId);
  });
}

function _showExperienceConfirm(sceneId, fullName, sceneName, sceneApplied) {
  var modal = document.createElement('div');
  modal.id = 'expConfirmModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)';
  var title, desc;
  if (sceneApplied) {
    title = I18N.t('skills.exp_confirm_overwrite_title')||'确认覆盖场景配置';
    desc = (I18N.t('skills.exp_confirm_overwrite_desc')||'项目') + ' ' + fullName + ' ' +
           (I18N.t('skills.exp_confirm_overwrite_warning')||'已存在且已应用过此场景，再次体验将覆盖已有的场景配置。');
  } else {
    title = I18N.t('skills.exp_confirm_exists_title')||'项目已存在';
    desc = (I18N.t('skills.exp_confirm_exists_desc')||'项目') + ' ' + fullName + ' ' +
           (I18N.t('skills.exp_confirm_exists_warning')||'已存在，体验将在此项目上应用场景配置。');
  }
  modal.innerHTML = '<div class="cd" style="width:420px;padding:24px;border-radius:18px;background:rgba(255,255,255,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 8px 30px rgba(0,0,0,.12)">' +
    '<div style="display:flex;align-items:flex-start;gap:12px;margin-bottom:14px">' +
      '<span style="font-size:22px;flex-shrink:0">⚠️</span>' +
      '<div>' +
        '<h3 style="font-size:15px;font-weight:600;margin-bottom:6px">' + title + '</h3>' +
        '<div style="font-size:12px;color:var(--t2);line-height:1.6">' + desc + '</div>' +
      '</div>' +
    '</div>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px">' +
      '<button class="bt bt-o bt-s" id="expConfirmCancelBtn">' + (I18N.t('common.cancel')||'取消') + '</button>' +
      '<button class="bt bt-p bt-s" id="expConfirmOkBtn">' + (I18N.t('skills.exp_confirm_btn')||'继续体验') + '</button>' +
    '</div>' +
  '</div>';
  document.body.appendChild(modal);
  document.getElementById('expConfirmCancelBtn').onclick = function() { modal.remove(); };
  document.getElementById('expConfirmOkBtn').onclick = function() {
    modal.remove();
    _doExperienceScene(sceneId);
  };
}

function _doExperienceScene(sceneId) {
  var modal = document.createElement('div');
  modal.id = 'expSceneModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)';
  modal.innerHTML = '<div class="cd" style="width:440px;padding:24px;border-radius:18px;background:rgba(255,255,255,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 8px 30px rgba(0,0,0,.12)">' +
    '<h3 style="font-size:15px;font-weight:600;margin-bottom:14px">' + (I18N.t('skills.experience')||'体验') + '</h3>' +
    '<div id="expProgress" style="margin-top:6px"></div>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:18px">' +
      '<button class="bt bt-o bt-s" id="expCancelBtn">' + (I18N.t('common.cancel')||'取消') + '</button>' +
    '</div>' +
  '</div>';
  document.body.appendChild(modal);

  var progressEl = document.getElementById('expProgress');
  var _step = 0;
  var _steps = [
    {pct: 10, text: I18N.t('templates.progress_checking')||'检查环境...'},
    {pct: 25, text: I18N.t('templates.progress_creating_repo')||'正在创建 GitCode 仓库...'},
    {pct: 50, text: I18N.t('templates.progress_downloading')||'下载模板代码...'},
    {pct: 70, text: I18N.t('templates.progress_pushing')||'推送代码到仓库...'},
    {pct: 85, text: I18N.t('templates.progress_hosting')||'托管仓库...'},
    {pct: 95, text: I18N.t('templates.progress_applying_scene')||'应用场景配置...'},
  ];
  function setProgress(pct, text) {
    progressEl.innerHTML =
      '<div style="font-size:12px;color:var(--t2);margin-bottom:6px">' + text + '</div>' +
      '<div style="height:6px;background:rgba(0,0,0,.05);border-radius:999px;overflow:hidden">' +
        '<div style="height:100%;width:' + pct + '%;background:var(--a);border-radius:999px;transition:width .4s ease"></div>' +
      '</div>' +
      '<div style="text-align:right;font-size:10px;color:var(--t3);margin-top:3px">' + pct + '%</div>';
  }
  function nextStep() {
    if (_step < _steps.length) { setProgress(_steps[_step].pct, _steps[_step].text); _step++; }
  }
  var _timer = setInterval(nextStep, 5000);
  setProgress(10, _steps[0].text);
  _step = 1;

  document.getElementById('expCancelBtn').onclick = function() {
    clearInterval(_timer);
    modal.remove();
  };

  API.post('/scenes/market/' + sceneId + '/experience', {}, 300000).then(function(r) {
    clearInterval(_timer);
    setProgress(100, I18N.t('templates.progress_done')||'完成');
    var full_name = r.full_name || '';
    if (r.message && app && app.showToast) app.showToast(r.message);
    setTimeout(function() {
      modal.remove();
      if (full_name) {
        var parts = full_name.split('/');
        _selectRepoAndNav(full_name, parts[0], parts[1]);
      }
    }, 800);
  }).catch(function(e) {
    clearInterval(_timer);
    if (e.status === 409) {
      modal.remove();
      _showSceneNameConflict(e.message, e.detail?.full_name);
    } else {
      setProgress(0, (I18N.t('templates.progress_failed', {msg: e.message})||'失败') + ': ' + (e.message||''));
      document.getElementById('expCancelBtn').textContent = I18N.t('common.close')||'关闭';
      if (app && app.showToast) app.showToast((I18N.t('skills.toast.experience_failed')||'体验失败') + ': ' + (e.message||''));
    }
  });
}

function _selectRepoAndNav(fullName, owner, name) {
  // 先刷新侧边栏仓库列表（新仓库可能刚托管还没进下拉），完成后再切换仓库
  API.get('/repos/managed').then(function(d) {
    var repos = d.repos || [];
    var sel = document.getElementById('repoSelect');
    if (sel) {
      sel.innerHTML = repos.map(function(r) {
        return '<option value="' + r.full_name + '"' + (r.full_name === fullName ? ' selected' : '') + '>' + r.full_name + '</option>';
      }).join('') + '<option value="__add__">＋ ' + (I18N.t('app.repo.add_new')||'添加仓库') + '</option>';
    }
    app.switchRepo(fullName);
  }).catch(function() {
    app.switchRepo(fullName);
  });
}

function _currentRepoFullName() {
  try {
    const c = app.currentRepo();
    return c ? (c.fullName || '') : '';
  } catch { return ''; }
}

// ══════════════════════════════════════════
// 场景详情页（按 scope 精简：repo 隐藏在用仓库/点赞/精选标记）
// ══════════════════════════════════════════
function renderSceneDetail(marketId) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  el.innerHTML = '<div class="loading">' + (I18N.t('common.loading')||'加载中...') + '</div>';
  API.get('/scenes/market/' + marketId).then(d => {
    _renderSceneDetailPage(d);
  }).catch(e => {
    el.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + (e.message||'') + '</div>';
  });
}

function _renderSceneDetailPage(s) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const catBg = _CAT_BG[s.category_id] || 'bg-default';
  const iconVal = s.icon || '⚙';
  const iconHtml = (iconVal.startsWith('http') || iconVal.startsWith('/static/') || iconVal.startsWith('/uploads/'))
    ? `<img src="${iconVal}" style="width:100%;height:100%;border-radius:inherit;object-fit:cover" onerror="this.style.display='none';this.parentElement.innerText='${s.category_icon||'⚙'}'">`
    : iconVal;
  const skills = s.skills || [];
  const likeIcon = s.liked_by_me ? '❤️' : '🤍';
  const scope = s.scope || 'official';
  const isOfficial = scope === 'official';

  let html = `<button class="bt bt-o bt-s" onclick="app.nav('scene-market')" style="margin-bottom:16px">← ${I18N.t('skills.back_to_scene_market')||'返回场景市场'}</button>`;

  // 头部卡片：图标 + 标题 + 描述 + 元数据 + 操作按钮
  html += `<div class="cd" style="padding:28px 32px;margin-bottom:20px;border-radius:18px;background:rgba(255,255,255,.72);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 1px 3px rgba(0,0,0,.04)">
    <div style="display:flex;align-items:flex-start;gap:20px;margin-bottom:20px">
      <div class="scn-ic ${catBg}" style="width:80px;height:80px;border-radius:18px;font-size:36px">${iconHtml}</div>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
          <h2 style="font-size:22px;font-weight:700;letter-spacing:-.02em">${s.display_name || s.name}</h2>
          ${s.name && s.name !== (s.display_name || s.name) ? `<span style="font-size:11px;color:#86868b;background:rgba(0,0,0,.04);padding:3px 10px;border-radius:999px;font-weight:400">${s.name}</span>` : ''}
        </div>
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:12px;flex-wrap:wrap">
          ${s.category_name ? `<span class="tg tg-d" style="font-size:10px">${s.category_name}</span>` : ''}
          ${isOfficial && s.is_featured ? `<span class="tg" style="background:rgba(184,155,106,.12);color:#B89B6A;font-size:10px">🌟 ${I18N.t('skills.featured_tag')||'精选'}</span>` : ''}
          ${s.version ? `<span class="tg tg-d" style="font-size:9px">v${s.version}</span>` : ''}
        </div>
        <div style="font-size:13px;color:var(--t2);line-height:1.6">${s.market_description || s.description || ''}</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:16px;padding-top:16px;border-top:1px solid rgba(0,0,0,.04)">
      ${isOfficial ? `<span style="cursor:pointer;font-size:13px;color:var(--t2);display:flex;align-items:center;gap:4px" onclick="_toggleSceneLike(${s.id},this)">${likeIcon} <span data-scene-like="${s.id}">${s.like_count||0}</span></span>
      <span style="font-size:11px;color:var(--t3)">${s.repo_count||0} ${I18N.t('skills.repos_in_use')||'仓库在用'}</span>` : ''}
      <div style="margin-left:auto;display:flex;align-items:center;gap:8px">
        ${!isOfficial ? `<button class="bt bt-o bt-s" onclick="_editScene(${s.id})" style="opacity:0.7">${I18N.t('skills.edit_scene')||'编辑'}</button>
        <button class="bt bt-o bt-s" onclick="_deleteScene(${s.id})" style="opacity:0.7;color:var(--r)">${I18N.t('skills.delete_scene')||'删除'}</button>` : ''}
        <button class="bt bt-o bt-s" onclick="_applyScene(${s.id},'${s.name}','${(s.skills||[]).map(function(x){return x.name||x;}).join(',')}')">${I18N.t('skills.apply_scene')||'应用'}</button>
        ${s.recommended_template ? `<button class="bt bt-o bt-s" onclick="_experienceScene(${s.id})">${I18N.t('skills.experience')||'体验'}</button>` : ''}
      </div>
    </div>
  </div>`;

  // 执行流程图
  if (skills.length > 0) {
    html += `<style>
      .det-card-wrap{background:rgba(255,255,255,.72);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-radius:18px;padding:24px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
      .det-grid{display:flex;gap:12px;overflow-x:auto;min-height:280px;scrollbar-width:none}
      .det-grid::-webkit-scrollbar{display:none}
      .det-card{flex:1 1 0;min-width:0;max-width:260px;min-height:260px;background:rgba(255,255,255,.6);border-radius:18px;padding:16px;display:flex;flex-direction:column;transition:all .2s;position:relative;box-shadow:0 1px 3px rgba(0,0,0,.04)}
      .det-card:hover{background:rgba(255,255,255,.85);box-shadow:0 2px 8px rgba(0,0,0,.06);transform:translateY(-1px)}
      .det-card-top{width:36px;height:4px;border-radius:2px;margin-bottom:12px}
      .det-card-desc{font-size:10px;color:#86868b;line-height:1.5;margin-bottom:10px;flex:1;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
      .det-card-sep{height:1px;background:rgba(0,0,0,.04);margin:8px 0}
      .det-card-box{background:rgba(255,255,255,.6);border-radius:12px;padding:8px 10px;margin-bottom:8px;display:flex;flex-direction:column;align-items:center}
      .det-card-label{font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
      .det-card-val{font-size:11px;font-weight:500;color:#1d1d1f}
      .det-card-eng{background:rgba(255,255,255,.6);border-radius:12px;padding:8px 10px;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center}
      .det-arrow{font-size:18px;color:#7A92B0;font-weight:700;align-self:center;margin:0 4px;flex-shrink:0}
    </style>`;
    html += `<div class="cd-h" style="margin-bottom:12px"><h2 style="font-size:16px;font-weight:600">${I18N.t('skills.execution_flow')||'执行流程'}</h2></div>`;
    html += `<div class="det-card-wrap"><div class="det-grid">`;
    skills.forEach((sk, i) => {
      const meta = _AGENT_META[sk.tag] || _AGENT_META[sk.agent_type] || {};
      const bgColor = meta.color || '#0071e3';
      const skIcon = meta.icon || '◈';
      const skLabel = meta.label || sk.tag || '';
      const skName = sk.display_name || sk.name || sk.skill || '';
      const roleDesc = sk.role_desc || '';
      const skDeleted = sk.skill_deleted;
      html += `<div class="det-card"${skDeleted ? ' style="border-color:var(--r);opacity:.7"' : ''}>
        <div class="det-card-top" style="background:${skDeleted ? 'var(--r)' : bgColor}"></div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="width:22px;height:22px;border-radius:999px;background:${skDeleted ? 'var(--r)22' : bgColor+'22'};color:${skDeleted ? 'var(--r)' : bgColor};font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0">${i+1}</span>
          <span style="font-size:14px;font-weight:600;color:#1d1d1f;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${skIcon} ${skLabel}</span>
          ${skDeleted ? '<span style="font-size:9px;color:var(--r);font-weight:600;flex-shrink:0">⚠ 已删除</span>' : ''}
        </div>
        ${roleDesc ? `<div class="det-card-desc" title="${roleDesc.replace(/"/g, '&quot;')}">${roleDesc}</div>` : '<div class="det-card-desc"></div>'}
        <div class="det-card-sep"></div>
        <div class="det-card-box">
          <div class="det-card-label">${I18N.t('dashboard.label_skill')||'技能'}</div>
          ${skDeleted ? `<span class="det-card-val" style="color:var(--r)">${skName}</span>` : `<span class="det-card-val" style="color:#7A92B0;cursor:pointer" onclick="app.nav('skill','${sk.skill_id||''}')">${skName}</span>`}
        </div>
        <div class="det-card-eng">
          <div class="det-card-label">${I18N.t('dashboard.label_engine')||'引擎'}</div>
          <span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.broadcast')||'广播'}</span>
        </div>
      </div>`;
      if (i < skills.length - 1) {
        html += `<span class="det-arrow">→</span>`;
      }
    });
    html += `</div></div>`;
  }

  // 技能套件
  if (skills.length > 0) {
    html += `<style>
      .det-suite-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
      .det-suite-card{background:rgba(255,255,255,.6);border-radius:18px;padding:16px;display:flex;align-items:flex-start;gap:12px;transition:all .2s;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.04)}
      .det-suite-card:hover{background:rgba(255,255,255,.85);box-shadow:0 2px 8px rgba(0,0,0,.06);transform:translateY(-1px)}
      .det-suite-num{width:28px;height:28px;border-radius:999px;color:#fff;font-size:12px;display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0}
      .det-suite-body{flex:1;min-width:0}
      .det-suite-name{font-size:14px;font-weight:600;color:#1d1d1f;margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .det-suite-role{font-size:11px;color:#86868b;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .det-suite-badge{font-size:10px;font-weight:600;padding:3px 10px;border-radius:999px;flex-shrink:0}
    </style>`;
    html += `<div class="cd-h" style="margin:24px 0 12px"><h2 style="font-size:16px;font-weight:600">${I18N.t('skills.skill_suite')||'技能套件'}</h2></div>`;
    html += `<div class="det-card-wrap"><div class="det-suite-grid">`;
    skills.forEach((sk, i) => {
      const meta = _AGENT_META[sk.tag] || _AGENT_META[sk.agent_type] || {};
      const bgColor = meta.color || '#0071e3';
      const skIcon = meta.icon || '◈';
      const skLabel = meta.label || sk.tag || '';
      const skName = sk.display_name || sk.name || sk.skill || '';
      const roleDesc = sk.role_desc || '';
      const skDeleted = sk.skill_deleted;
      html += `<div class="det-suite-card"${skDeleted ? ' style="border-color:var(--r);opacity:.7"' : ''}${skDeleted ? '' : ` onclick="app.nav('skill','${sk.skill_id||''}')"`}>
        <div class="det-suite-num" style="background:${skDeleted ? 'var(--r)' : bgColor}">${i+1}</div>
        <div class="det-suite-body">
          <div class="det-suite-name">${skIcon} ${skName}${skDeleted ? ' <span style="font-size:9px;color:var(--r);font-weight:600">⚠ 已删除</span>' : ''}</div>
          <div class="det-suite-role" title="${(roleDesc || skLabel).replace(/"/g, '&quot;')}">${roleDesc || skLabel}</div>
        </div>
        <span class="det-suite-badge" style="background:${skDeleted ? 'var(--r)14' : bgColor+'14'};color:${skDeleted ? 'var(--r)' : bgColor}">${skLabel}</span>
      </div>`;
    });
    html += `</div></div>`;
  }

  // 样例项目模板（仅 official 场景展示）
  if (s.recommended_template && isOfficial) {
    const tpl = s.template || {};
    const tplIcon = tpl.icon || '📦';
    const tplName = tpl.name || s.recommended_template_name || s.recommended_template;
    const tplDesc = tpl.description || '';
    const tplStars = tpl.stars || 0;
    const tplLang = tpl.language || '';
    const tplSource = tpl.source || '';
    const tplRepo = tpl.source_repo || '';
    const isRemote = tplSource === 'git' && tplRepo;
    const tplUrl = isRemote ? `https://gitcode.com/${tplRepo}` : '';
    const sourceBadge = isRemote
      ? `<span style="font-size:9px;font-weight:600;padding:2px 8px;border-radius:999px;background:rgba(52,199,89,.12);color:#34c759">${I18N.t('skills.gitcode_remote')||'GitCode 远程'}</span>`
      : `<span style="font-size:9px;font-weight:600;padding:2px 8px;border-radius:999px;background:rgba(0,0,0,.04);color:#86868b">${I18N.t('skills.local_template')||'本地模板'}</span>`;
    html += `<div class="cd-h" style="margin:24px 0 12px"><h2 style="font-size:16px;font-weight:600">${I18N.t('skills.sample_template')||'样例项目模板'}</h2></div>`;
    html += `<div class="det-card-wrap"${isRemote ? ` style="cursor:pointer" onclick="window.open('${tplUrl}','_blank')"` : ''}>
      <div style="display:flex;align-items:center;gap:16px;padding:8px 0">
        <span style="font-size:32px;flex-shrink:0">${tplIcon}</span>
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap">
            <span style="font-size:15px;font-weight:600;color:#1d1d1f">${tplName}</span>
            ${sourceBadge}
            ${tplLang ? `<span style="font-size:10px;font-weight:600;padding:2px 8px;border-radius:999px;background:rgba(122,146,176,.14);color:#7A92B0">${tplLang}</span>` : ''}
            ${tplStars ? `<span style="font-size:11px;color:#86868b">⭐ ${tplStars}</span>` : ''}
          </div>
          <div style="font-size:12px;color:#86868b;line-height:1.5">${tplDesc || (I18N.t('skills.template_desc')||'点击体验可基于此模板创建项目')}</div>
        </div>
        ${isRemote ? `<span style="font-size:12px;color:#0071e3;font-weight:500;flex-shrink:0;padding:6px 16px;border-radius:999px;transition:all .15s" onmouseover="this.style.background='rgba(122,146,176,.12)';this.style.color='#7A92B0'" onmouseout="this.style.background='';this.style.color='#0071e3'">${I18N.t('skills.view_project')||'查看项目'} ↗</span>` : ''}
      </div>
    </div>`;
  }

  el.innerHTML = html;
}

// ══════════════════════════════════════════
// ── 更新排序按钮高亮 ──
function _updateSortBtns(container) {
  var btns = document.querySelectorAll(container + ' .cd-h button[id^="sort-"], ' + container + ' .cd-h button.bt-tg, ' + container + ' .cd-h button.bt-o');
  btns.forEach(function(btn) {
    var text = btn.textContent.trim();
    var isLatest = text.indexOf(I18N.t('skills.sort_latest')||'最新') >= 0;
    var isHot = text.indexOf(I18N.t('skills.sort_hot')||'最热') >= 0;
    if (!isLatest && !isHot) return;
    var active = (isLatest && _sceneMarketSort === 'latest') || (isHot && _sceneMarketSort === 'hot');
    if (active) {
      btn.classList.remove('bt-o');
      btn.classList.add('bt-tg');
    } else {
      btn.classList.remove('bt-tg');
      btn.classList.add('bt-o');
    }
  });
}

// ── 场景删除
// ══════════════════════════════════════════
function _deleteScene(sceneId) {
  // 移除已有弹窗
  var existing = document.getElementById('deleteSceneModal');
  if (existing) { existing.remove(); return; }

  var modal = document.createElement('div');
  modal.id = 'deleteSceneModal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center';

  modal.innerHTML = `
    <div style="background:rgba(255,255,255,0.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-radius:18px;box-shadow:0 8px 30px rgba(0,0,0,.12);width:360px;max-width:90vw;overflow:hidden">
      <div style="padding:24px 24px 20px;text-align:center">
        <div style="width:48px;height:48px;border-radius:50%;background:rgba(255,59,48,0.08);display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:22px">🗑</div>
        <h3 style="font-size:16px;font-weight:600;color:#1d1d1f;margin-bottom:6px">${I18N.t('skills.confirm_delete_scene')||'确认删除此场景？'}</h3>
        <div style="font-size:12px;color:#86868b">${I18N.t('skills.delete_scene_tip')||'删除后不可恢复，场景数据将被永久移除'}</div>
      </div>
      <div style="padding:0 24px 20px;display:flex;gap:10px">
        <button onclick="document.getElementById('deleteSceneModal').remove()" style="flex:1;padding:10px 16px;font-size:13px;border-radius:999px;background:rgba(0,0,0,.05);color:#1d1d1f;cursor:pointer">${I18N.t('skills.cancel_save')||'取消'}</button>
        <button id="deleteConfirmBtn" style="flex:1;padding:10px 16px;font-size:13px;border:none;border-radius:999px;background:#ff3b30;color:#fff;cursor:pointer">${I18N.t('skills.confirm_delete')||'确认删除'}</button>
      </div>
    </div>
  `;

  document.body.appendChild(modal);
  modal.onclick = function(e) { if (e.target === modal) modal.remove(); };

  document.getElementById('deleteConfirmBtn').onclick = function() {
    var btn = this;
    btn.disabled = true;
    btn.textContent = I18N.t('skills.deleting') || '删除中...';
    API.del('/scenes/market/' + sceneId).then(function(r) {
      if (app && app.showToast) app.showToast(I18N.t('skills.scene_deleted')||'场景已删除');
      modal.remove();
      app.nav('scene-market');
    }).catch(function(e) {
      if (app && app.showToast) app.showToast((I18N.t('skills.scene_delete_failed')||'删除失败') + ': ' + (e.message||''));
      btn.disabled = false;
      btn.textContent = I18N.t('skills.confirm_delete')||'确认删除';
    });
  };
}

// 编辑场景（跳转编排页）
function _editScene(sceneId) {
  _enterSceneEditor('edit', '', sceneId);
}

// ══════════════════════════════════════════
// 场景编排组件（创建/编辑）
// ══════════════════════════════════════════
function _enterSceneEditor(mode, scope, sceneId) {
  _sceneEditorMode = mode;
  _sceneEditorScope = scope || _sceneTab;
  _sceneEditorSceneId = sceneId || '';
  _sceneEditorIconUrl = '';
  _sceneEditorIconKey = '';
  _sceneEditorFileId = 0;

  // 初始化 5 个 agent 节点：vod/issue 默认启用，coding/qa/sre 默认禁用（需手动启用）
  _sceneEditorNodes = _AGENT_ORDER.map(at => ({
    id: at, tag: at, skill: '', skill_id: 0, skill_display: '', skill_desc: '', skill_scope: 'official', skill_deleted: false,
    mode: 'emit', next: 'done', handoff: 'a2a',
    enabled: (at === 'vod' || at === 'issue')
  }));

  if (mode === 'edit') {
    // 加载分类列表 + 模板列表（和创建模式一样）
    // 先加载 meta，等场景数据回来后 _renderSceneEditor 会传预填值
    API.get('/scenes/market/' + sceneId).then(d => {
      _sceneEditorScope = d.scope || _sceneEditorScope;
      // 用 nodes_raw 预填，用 skills_detail 补充 display_name/description
      var skillsDetail = d.skills || [];
      if (d.nodes_raw && d.nodes_raw.length > 0) {
        _sceneEditorNodes = _AGENT_ORDER.map(at => {
          const node = d.nodes_raw.find(n => (n.id || n.tag) === at);
          if (node) {
            // 从 skills_detail 找对应的 skill 信息
            var skDetail = skillsDetail.find(s => s.name === node.skill || s.skill === node.skill);
            return {
              id: at, tag: at,
              skill: node.skill || '',
              skill_id: (skDetail && skDetail.skill_id) || node.skill_id || 0,
              skill_display: (skDetail && skDetail.display_name) || node.skill || '',
              skill_desc: (skDetail && skDetail.role_desc) || '',
              skill_scope: (skDetail && skDetail.scope) || 'official',
              skill_deleted: (skDetail && skDetail.skill_deleted) || false,
              mode: node.mode || 'emit',
              next: node.next || 'done',
              handoff: node.handoff || 'a2a',
              enabled: node.enabled === true
            };
          }
          return { id: at, tag: at, skill: '', skill_id: 0, skill_display: '', skill_desc: '', skill_deleted: false, mode: 'emit', next: 'done', handoff: 'a2a', enabled: false };
        });
      }
      _sceneEditorIconUrl = d.icon || '';
      _renderSceneEditor(d.name, d.display_name || d.name, d.description || d.market_description || '',
                         d.category_id);
      // 渲染技能套件（预填已选 skill）
      _renderEditorSkillSuite();
    }).catch(e => {
      if (app && app.showToast) app.showToast(I18N.t('common.load_failed') + ': ' + (e.message||''));
    });
  } else {
    // 创建模式：_renderSceneEditor 内部会调 _loadEditorMeta
    _renderSceneEditor('', '', '', 1, '');
  }
}

function _loadEditorMeta(prefillCat) {
  // 只加载分类列表（模板已移除）
  API.get('/scenes/market/categories').then(catData => {
    _sceneEditorCats = catData.categories || [];
    // 重新渲染编排页的分类下拉
    const catSel = document.getElementById('editorCategory');
    if (catSel) {
      catSel.innerHTML = _sceneEditorCats.map(c => `<option value="${c.id}">${c.icon||''} ${c.name}</option>`).join('');
      // 编辑模式：分类列表加载完后设置选中值
      if (prefillCat) catSel.value = prefillCat;
    }
  }).catch(() => {});
}

function _renderSceneEditor(prefillSceneName, prefillName, prefillDesc, prefillCat) {
  const el = document.getElementById('pageContent');
  if (!el) return;

  // 确保 meta 数据已加载
  if (!_sceneEditorCats.length) {
    _loadEditorMeta(prefillCat);
  }

  let html = `<button class="bt bt-o bt-s" onclick="app.nav('scene-market')" style="margin-bottom:12px">← ${I18N.t('skills.back_to_scene_market')||'返回场景市场'}</button>`;

  // 顶部：图标 → 名称 → 描述 → 分类（垂直布局，苹果风格）
  html += `<div class="cd" style="padding:24px;margin-bottom:16px;border-radius:18px;background:rgba(255,255,255,.72);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);box-shadow:0 1px 3px rgba(0,0,0,.04)">
    <div style="margin-bottom:16px">
      <div style="font-size:12px;color:#86868b;margin-bottom:8px;display:flex;align-items:center;gap:6px">${I18N.t('skills.scene_icon')||'场景图标'}<span onclick="var b=document.getElementById('iconTipBubble');if(b.style.display==='none'){b.style.display='block';setTimeout(function(){document.addEventListener('click',function close(){b.style.display='none';document.removeEventListener('click',close)})},0)}else{b.style.display='none'}" style="display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:rgba(0,0,0,.05);color:#86868b;font-size:11px;cursor:pointer;position:relative">ⓘ<span id="iconTipBubble" style="display:none;position:absolute;bottom:24px;left:50%;transform:translateX(-50%);background:#1d1d1f;color:#fff;font-size:11px;padding:8px 12px;border-radius:8px;white-space:nowrap;z-index:100;box-shadow:0 4px 12px rgba(0,0,0,.15)">${I18N.t('skills.icon_format_tip')||'支持 jpg/png/webp/gif/svg，≤2MB'}<span style="position:absolute;bottom:-4px;left:50%;transform:translateX(-50%);width:0;height:0;border-left:4px solid transparent;border-right:4px solid transparent;border-top:4px solid #1d1d1f"></span></span></span></div>
      <div id="editorIconPreview" style="width:72px;height:72px;border-radius:18px;background:#f5f5f7;display:flex;align-items:center;justify-content:center;font-size:32px;cursor:pointer" onclick="document.getElementById('editorIconInput').click()">
        ${_sceneEditorIconUrl ? `<img src="${_sceneEditorIconUrl}" style="width:100%;height:100%;border-radius:18px;object-fit:cover" onerror="this.style.display='none';this.parentElement.innerText='📷'">` : '📷'}
      </div>
      <input type="file" id="editorIconInput" accept="image/jpeg,image/png,image/webp,image/gif,image/svg+xml" style="display:none" onchange="_uploadSceneIcon(this)">
    </div>
    <div style="margin-bottom:16px">
      <div style="font-size:12px;color:#86868b;margin-bottom:6px">${I18N.t('skills.scene_id')||'场景标识'}</div>
      <input id="editorSceneId" type="text" maxlength="40" placeholder="${I18N.t('skills.scene_id_placeholder')||'英文短横线，如 iac-dev'}" value="${(prefillSceneName||'').replace(/"/g,'&quot;')}" style="width:100%;padding:10px 14px;border-radius:12px;font-size:14px;background:#f5f5f7;color:#1d1d1f;outline:none;transition:background .15s" onfocus="this.style.background='#ebebed'" onblur="this.style.background='#f5f5f7'" oninput="this.value=this.value.replace(/[^a-z0-9-]/g,'').toLowerCase();_checkSceneNameDebounced()">
      <div id="editorSceneIdHint" style="font-size:11px;color:#86868b;margin-top:4px"></div>
    </div>
    <div style="margin-bottom:16px">
      <div style="font-size:12px;color:#86868b;margin-bottom:6px;display:flex;align-items:center;justify-content:space-between">
        <span>${I18N.t('skills.scene_name')||'场景名称'}</span>
        <span id="editorNameCount" style="font-size:11px;color:#86868b">0/40</span>
      </div>
      <input id="editorName" type="text" maxlength="40" placeholder="${I18N.t('skills.scene_name_placeholder')||'请输入场景名称'}" value="${(prefillName||'').replace(/"/g,'&quot;')}" style="width:100%;padding:10px 14px;border-radius:12px;font-size:14px;background:#f5f5f7;color:#1d1d1f;outline:none;transition:border-color .15s" onfocus="this.style.background='#ebebed'" onblur="this.style.background='#f5f5f7'" oninput="document.getElementById('editorNameCount').textContent=this.value.length+'/40'">
      <div id="editorNameHint" style="font-size:11px;color:#86868b;margin-top:4px"></div>
    </div>
    <div style="margin-bottom:16px">
      <div style="font-size:12px;color:#86868b;margin-bottom:6px;display:flex;align-items:center;justify-content:space-between">
        <span>${I18N.t('skills.scene_desc')||'场景描述'}</span>
        <span id="editorDescCount" style="font-size:11px;color:#86868b">0/200</span>
      </div>
      <textarea id="editorDesc" maxlength="200" placeholder="${I18N.t('skills.scene_desc_placeholder')||'请输入场景描述'}" rows="3" style="width:100%;padding:10px 14px;border-radius:12px;font-size:14px;background:#f5f5f7;color:#1d1d1f;outline:none;resize:vertical;transition:background .15s" onfocus="this.style.background='#ebebed'" onblur="this.style.background='#f5f5f7'" oninput="document.getElementById('editorDescCount').textContent=this.value.length+'/200'">${(prefillDesc||'').replace(/</g,'&lt;')}</textarea>
    </div>
    <div>
      <div style="font-size:12px;color:#86868b;margin-bottom:6px">${I18N.t('skills.scene_category')||'场景分类'}</div>
      <select id="editorCategory" style="width:100%;padding:10px 14px;border-radius:12px;font-size:14px;background:#f5f5f7;color:#1d1d1f;outline:none">
        <option value="1">${I18N.t('skills.category_general')||'通用'}</option>
      </select>
    </div>
  </div>`;

  // 执行流程（det-card 样式，和详情页一致）
  html += `<style>
    .det-card-wrap{background:rgba(255,255,255,.72);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-radius:18px;padding:24px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
    .det-grid{display:flex;gap:12px;overflow-x:auto;min-height:280px;scrollbar-width:none}
    .det-grid::-webkit-scrollbar{display:none}
    .det-card{flex:1 1 0;min-width:0;max-width:260px;min-height:260px;background:rgba(255,255,255,.6);border-radius:18px;padding:16px;display:flex;flex-direction:column;transition:all .2s;position:relative;box-shadow:0 1px 3px rgba(0,0,0,.04)}
    .det-card:hover{background:rgba(255,255,255,.85);box-shadow:0 2px 8px rgba(0,0,0,.06);transform:translateY(-1px)}
    .det-card.disabled{opacity:0.4}
    .det-card-top{width:36px;height:4px;border-radius:2px;margin-bottom:12px}
    .det-card-desc{font-size:10px;color:#86868b;line-height:1.5;margin-bottom:10px;flex:1;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
    .det-card-sep{height:1px;background:rgba(0,0,0,.04);margin:8px 0}
    .det-card-box{background:rgba(255,255,255,.6);border-radius:12px;padding:8px 10px;margin-bottom:8px;display:flex;flex-direction:column;align-items:center}
    .det-card-label{font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
    .det-card-val{font-size:11px;font-weight:500;color:#1d1d1f}
    .det-card-eng{background:rgba(255,255,255,.6);border-radius:12px;padding:8px 10px;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center}
    .det-arrow{font-size:18px;color:#7A92B0;font-weight:700;align-self:center;margin:0 4px;flex-shrink:0}
    .det-suite-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
    .det-suite-card{background:rgba(255,255,255,.6);border-radius:18px;padding:16px;display:flex;align-items:flex-start;gap:12px;transition:all .2s;box-shadow:0 1px 3px rgba(0,0,0,.04)}
    .det-suite-num{width:28px;height:28px;border-radius:999px;color:#fff;font-size:12px;display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0}
    .det-suite-body{flex:1;min-width:0}
    .det-suite-name{font-size:14px;font-weight:600;color:#1d1d1f;margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .det-suite-role{font-size:11px;color:#86868b;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .det-suite-badge{font-size:10px;font-weight:600;padding:3px 10px;border-radius:999px;flex-shrink:0}
  </style>`;
  html += `<div class="cd-h" style="margin-bottom:10px"><h2>${I18N.t('skills.execution_flow')||'执行流程'}</h2></div>`;
  html += `<div class="det-card-wrap"><div class="det-grid">`;
  _sceneEditorNodes.forEach((node, i) => {
    const meta = _AGENT_META[node.id] || {};
    const bgColor = meta.color || '#0071e3';
    const canDisable = true;
    const showToggle = canDisable && !((node.id === 'vod' || node.id === 'issue') && node.enabled);
    html += `<div class="det-card${node.enabled?'':' disabled'}" data-idx="${i}"${node.skill_deleted ? ' style="border-color:var(--r)"' : ''}>
      <div class="det-card-top" style="background:${node.skill_deleted ? 'var(--r)' : bgColor}"></div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="width:22px;height:22px;border-radius:999px;background:${node.skill_deleted ? 'var(--r)22' : bgColor+'22'};color:${node.skill_deleted ? 'var(--r)' : bgColor};font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0">${i+1}</span>
        <span style="font-size:14px;font-weight:600;color:#1d1d1f;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${meta.icon||''} ${meta.label||node.id}</span>
        ${node.skill_deleted ? '<span style="font-size:9px;color:var(--r);font-weight:600;flex-shrink:0">⚠ 技能已删除</span>' : ''}
        ${showToggle ? `<button class="bt bt-o bt-s" style="font-size:9px;padding:2px 8px" onclick="_toggleAgentEnabled(${i})">${node.enabled ? '✕' : '✓'}</button>` : ''}
      </div>
      <div class="det-card-desc" title="${(node.skill_desc||'').replace(/"/g, '&quot;')}">${node.skill_desc || ''}</div>
      <div class="det-card-sep"></div>
      <div class="det-card-box">
        <div class="det-card-label">${I18N.t('dashboard.label_skill')||'技能'}</div>
        ${node.enabled ? `<div class="ed-skill-tabs" data-idx="${i}" style="display:flex;gap:0;margin-bottom:4px;border-bottom:1px solid rgba(0,0,0,.04)">
          <a class="ed-skill-tab-opt ac" data-scope="official" data-idx="${i}" style="flex:1;padding:3px 2px;text-align:center;font-size:9px;font-weight:500;cursor:pointer;border-bottom:2px solid var(--a);color:var(--a);transition:all .15s">${I18N.t('skills.skill_tab_official')||'技能市场'}</a>
          <a class="ed-skill-tab-opt" data-scope="repo" data-idx="${i}" style="flex:1;padding:3px 2px;text-align:center;font-size:9px;font-weight:500;cursor:pointer;border-bottom:2px solid transparent;color:var(--t3);transition:all .15s">${I18N.t('skills.skill_tab_repo')||'仓库技能'}</a>
        </div>
        <select id="ed-skill-${i}" style="width:100%"></select>` : `<span class="det-card-val" style="color:#86868b">none</span>`}
      </div>
      <div class="det-card-eng">
        <div class="det-card-label">${I18N.t('dashboard.label_engine')||'引擎'}</div>
        <span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.broadcast')||'广播'}</span>
      </div>
    </div>`;
    if (i < _sceneEditorNodes.length - 1) {
      html += `<span class="det-arrow">→</span>`;
    }
  });
  html += `</div></div>`;

  // 技能套件（实时显示已选 skill）
  html += `<div id="editorSkillSuiteArea" style="display:none">
    <div class="cd-h" style="margin:20px 0 10px"><h2>${I18N.t('skills.skill_suite')||'技能套件'}</h2></div>
    <div id="editorSkillSuite"></div>
  </div>`;

  // 底部：保存 + 取消
  html += `<div style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:20px">
    <button class="bt bt-o bt-s" onclick="app.nav('scene-market')">${I18N.t('skills.cancel_save')||'取消'}</button>
    <button class="bt bt-p bt-s" onclick="_saveScene()">${I18N.t('skills.save_scene')||'保存'}</button>
  </div>`;

  el.innerHTML = html;

  // 已缓存的分类：DOM 渲染完后直接填充（不缓存的由 _loadEditorMeta 异步填充）
  if (_sceneEditorCats.length) {
    const catSel = document.getElementById('editorCategory');
    if (catSel) {
      catSel.innerHTML = _sceneEditorCats.map(c => `<option value="${c.id}">${c.icon||''} ${c.name}</option>`).join('');
      if (prefillCat) catSel.value = prefillCat;
    }
  }

  // 预填分类
  if (prefillCat) {
    const catSel = document.getElementById('editorCategory');
    if (catSel) catSel.value = prefillCat;
  }
  // 更新字数计数
  const nameInput = document.getElementById('editorName');
  if (nameInput) {
    const nameCount = document.getElementById('editorNameCount');
    if (nameCount) nameCount.textContent = nameInput.value.length + '/40';
  }
  const descInput = document.getElementById('editorDesc');
  if (descInput) {
    const descCount = document.getElementById('editorDescCount');
    if (descCount) descCount.textContent = descInput.value.length + '/200';
  }
  // 异步加载每个启用 agent 的 skill 列表填充 select
  _sceneEditorNodes.forEach((node, i) => {
    if (!node.enabled) return;
    _loadAgentSkillOptions(i);
  });
}

// ── 图标上传 ──
function _uploadSceneIcon(input) {
  const file = input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  API.upload('/scenes/market/icon-upload', formData).then(r => {
    _sceneEditorIconUrl = r.icon || '';
    _sceneEditorIconKey = r.obs_icon_key || '';
    _sceneEditorFileId = r.file_id || 0;
    const preview = document.getElementById('editorIconPreview');
    if (preview) {
      preview.innerHTML = `<img src="${_sceneEditorIconUrl}" style="width:100%;height:100%;border-radius:12px;object-fit:cover">`;
    }
  }).catch(e => {
    if (app && app.showToast) app.showToast((I18N.t('skills.icon_format_tip')||'上传失败') + ': ' + (e.message||''));
  });
}

// ── 名称重复校验（防抖）──
function _checkSceneNameDebounced() {
  if (_sceneEditorNameCheckTimer) clearTimeout(_sceneEditorNameCheckTimer);
  _sceneEditorNameCheckTimer = setTimeout(_checkSceneName, 500);
}

function _checkSceneName() {
  const sceneId = (document.getElementById('editorSceneId') || {}).value || '';
  const hint = document.getElementById('editorSceneIdHint');
  if (!hint) return;
  if (!sceneId || sceneId.length < 2) {
    hint.innerHTML = '<span style="color:#86868b">' + (I18N.t('skills.scene_id_tip')||'只允许小写英文和短横线，作为场景内部标识') + '</span>';
    return;
  }
  const repo = _currentRepoFullName();
  let qs = '?name=' + encodeURIComponent(sceneId) + '&scope=' + _sceneEditorScope + '&repo=' + encodeURIComponent(repo);
  if (_sceneEditorMode === 'edit' && _sceneEditorSceneId) qs += '&exclude_id=' + _sceneEditorSceneId;
  API.get('/scenes/check-name' + qs).then(d => {
    if (d.exists) {
      hint.innerHTML = '<span style="color:var(--r)">' + (I18N.t('skills.scene_name_duplicate')||'当前名称重复请重新命名') + '</span>';
    } else {
      hint.innerHTML = '<span style="color:#34c759">✓ ' + (I18N.t('skills.scene_name_available')||'名称可用') + '</span>';
    }
  }).catch(() => {
    hint.innerHTML = '';
  });
}

// ── Skill select 下拉（Select2 + tab 切换 scope）──
var _skillSelectScope = {};  // {idx: 'official'/'repo'}

function _loadAgentSkillOptions(idx) {
  var node = _sceneEditorNodes[idx];
  if (!node || !node.enabled) return;
  var agentType = node.id;
  var sel = document.getElementById('ed-skill-' + idx);
  if (!sel) return;

  _skillSelectScope[idx] = (node.skill_scope === 'repo') ? 'repo' : 'official';

  // 根据 skill_scope 同步 tab 高亮
  var card = document.querySelector('.det-card[data-idx="' + idx + '"]');
  if (card) {
    card.querySelectorAll('.ed-skill-tab-opt').forEach(function(t) {
      var s = t.getAttribute('data-scope');
      if (s === _skillSelectScope[idx]) {
        t.classList.add('ac');
        t.style.borderBottom = '2px solid var(--a)';
        t.style.color = 'var(--a)';
      } else {
        t.classList.remove('ac');
        t.style.borderBottom = '2px solid transparent';
        t.style.color = 'var(--t3)';
      }
    });
  }

  // 先设置已有选中值（编辑模式预填）
  var current = node.skill || '';
  if (current) {
    var opt = document.createElement('option');
    opt.value = current;
    opt.textContent = node.skill_display || current;
    opt.selected = true;
    sel.appendChild(opt);
  }

  // Select2 AJAX 模式：搜索 + 分页，按 tab scope 过滤
  $(sel).select2({
    placeholder: I18N.t('skills.select_skill') || '选择技能',
    allowClear: true,
    width: '100%',
    ajax: {
      url: '/api/scenes/skills/' + agentType,
      dataType: 'json',
      delay: 300,
      headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('token') || localStorage.getItem('grape_token') || '') },
      data: function(params) {
        return {
          q: params.term || '',
          scope: _skillSelectScope[idx] || 'official',
          repo: _currentRepoFullName() || '',
          page: params.page || 1,
          page_size: 20
        };
      },
      processResults: function(data, params) {
        params.page = params.page || 1;
        var results = (data.skills || []).map(function(s) {
          return { id: s.skill_name, text: s.display_name || s.skill_name, skill_id: s.skill_id, description: s.description, scope: s.scope };
        });
        return {
          results: results,
          pagination: { more: (params.page * 20) < (data.total || 0) }
        };
      }
    },
    minimumInputLength: 0
  });

  // 选中时更新 node 数据
  $(sel).off('select2:select select2:clear').on('select2:select', function(e) {
    var d = e.params.data;
    node.skill = d.id || '';
    node.skill_display = d.text || d.id;
    node.skill_id = d.skill_id || 0;
    node.skill_desc = d.description || '';
    node.skill_scope = d.scope || _skillSelectScope[idx] || 'official';
    node.skill_deleted = false;
    _renderEditorSkillSuite();
    _updateEditorCardStyle(idx);
  });

  // 清除时清空 node
  $(sel).on('select2:clear', function() {
    node.skill = '';
    node.skill_display = '';
    node.skill_id = 0;
    node.skill_desc = '';
    node.skill_scope = 'official';
    _renderEditorSkillSuite();
    _updateEditorCardStyle(idx);
  });

  // 绑定 tab 切换
  if (card) {
    card.querySelectorAll('.ed-skill-tab-opt').forEach(function(tab) {
      tab.addEventListener('click', function(e) {
        e.stopPropagation();
        var scope = this.getAttribute('data-scope');
        _skillSelectScope[idx] = scope;
        // 更新 tab 高亮
        card.querySelectorAll('.ed-skill-tab-opt').forEach(function(t) {
          if (t.getAttribute('data-scope') === scope) {
            t.classList.add('ac');
            t.style.borderBottom = '2px solid var(--a)';
            t.style.color = 'var(--a)';
          } else {
            t.classList.remove('ac');
            t.style.borderBottom = '2px solid transparent';
            t.style.color = 'var(--t3)';
          }
        });
        // 销毁并重新初始化 Select2（刷新数据）
        $(sel).select2('destroy');
        sel.innerHTML = '';
        var cur = node.skill || '';
        if (cur) {
          var opt2 = document.createElement('option');
          opt2.value = cur;
          opt2.textContent = node.skill_display || cur;
          opt2.selected = true;
          sel.appendChild(opt2);
        }
        $(sel).select2({
          placeholder: I18N.t('skills.select_skill') || '选择技能',
          allowClear: true,
          width: '100%',
          ajax: {
            url: '/api/scenes/skills/' + agentType,
            dataType: 'json',
            delay: 300,
            headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('token') || localStorage.getItem('grape_token') || '') },
            data: function(params) {
              return {
                q: params.term || '',
                scope: scope,
                repo: _currentRepoFullName() || '',
                page: params.page || 1,
                page_size: 20
              };
            },
            processResults: function(data, params) {
              params.page = params.page || 1;
              var results = (data.skills || []).map(function(s) {
                return { id: s.skill_name, text: s.display_name || s.skill_name, skill_id: s.skill_id, description: s.description, scope: s.scope };
              });
              return {
                results: results,
                pagination: { more: (params.page * 20) < (data.total || 0) }
              };
            }
          },
          minimumInputLength: 0
        });
        $(sel).off('select2:select select2:clear').on('select2:select', function(e2) {
          var d2 = e2.params.data;
          node.skill = d2.id || '';
          node.skill_display = d2.text || d2.id;
          node.skill_id = d2.skill_id || 0;
          node.skill_desc = d2.description || '';
          node.skill_scope = d2.scope || scope;
          node.skill_deleted = false;
          _renderEditorSkillSuite();
          _updateEditorCardStyle(idx);
        });
        $(sel).on('select2:clear', function() {
          node.skill = '';
          node.skill_display = '';
          node.skill_id = 0;
          node.skill_desc = '';
          node.skill_scope = 'official';
          node.skill_deleted = false;
          _renderEditorSkillSuite();
          _updateEditorCardStyle(idx);
        });
        // 打开下拉让用户看到新数据
        $(sel).select2('open');
      });
    });
  }
}

// ── 局部更新卡片样式（选中/清除技能后，不重渲染整个编排页）──
function _updateEditorCardStyle(idx) {
  var node = _sceneEditorNodes[idx];
  if (!node) return;
  var card = document.querySelector('.det-card[data-idx="' + idx + '"]');
  if (!card) return;
  var meta = _AGENT_META[node.id] || {};
  var bgColor = meta.color || '#0071e3';
  var deleted = node.skill_deleted;
  card.style.borderColor = deleted ? 'var(--r)' : '';
  var top = card.querySelector('.det-card-top');
  if (top) top.style.background = deleted ? 'var(--r)' : bgColor;
  var numSpan = card.querySelector('span[style*="border-radius:6px"]');
  if (numSpan) {
    numSpan.style.background = deleted ? 'var(--r)22' : bgColor + '22';
    numSpan.style.color = deleted ? 'var(--r)' : bgColor;
  }
  var warn = null;
  card.querySelectorAll('span').forEach(function(s) {
    if (s.textContent.indexOf('已删除') >= 0) warn = s;
  });
  if (!deleted && warn) warn.remove();
}

// ── 重新渲染技能套件区域 ──
function _renderEditorSkillSuite() {
  var area = document.getElementById('editorSkillSuiteArea');
  var container = document.getElementById('editorSkillSuite');
  if (!area || !container) return;
  var selectedNodes = _sceneEditorNodes.filter(function(n) { return n.enabled && n.skill; });
  if (selectedNodes.length === 0) {
    area.style.display = 'none';
    container.innerHTML = '';
    return;
  }
  area.style.display = '';
  var html = '<div class="det-card-wrap"><div class="det-suite-grid">';
  selectedNodes.forEach(function(node, i) {
    var meta = _AGENT_META[node.id] || {};
    var bgColor = meta.color || '#0071e3';
    var skName = node.skill_display || node.skill || '';
    var skRole = node.skill_desc || (meta.label || node.id);
    var skDeleted = node.skill_deleted;
    var cardColor = skDeleted ? 'var(--r)' : bgColor;
    html += '<div class="det-suite-card"' + (skDeleted ? ' style="border-color:var(--r);opacity:.7"' : '') + '>' +
      '<div class="det-suite-num" style="background:' + cardColor + '">' + (i+1) + '</div>' +
      '<div class="det-suite-body">' +
        '<div class="det-suite-name">' + (meta.icon||'') + ' ' + skName + (skDeleted ? ' <span style="font-size:9px;color:var(--r);font-weight:600">⚠ 已删除</span>' : '') + '</div>' +
        '<div class="det-suite-role" title="' + skRole.replace(/"/g, '&quot;') + '">' + skRole + '</div>' +
      '</div>' +
      '<span class="det-suite-badge" style="background:' + cardColor + '14;color:' + cardColor + '">' + (meta.label||node.id) + '</span>' +
    '</div>';
  });
  html += '</div></div>';
  container.innerHTML = html;
}

// ── 启用/禁用 agent ──
function _toggleAgentEnabled(idx) {
  var node = _sceneEditorNodes[idx];
  if (!node) return;
  // VOD/ISSUE 只允许启用，不允许禁用
  if ((node.id === 'vod' || node.id === 'issue') && node.enabled) return;
  node.enabled = !node.enabled;
  if (!node.enabled) {
    node.skill = '';
    node.skill_display = '';
    node.skill_id = 0;
    node.skill_desc = '';
    node.skill_scope = 'official';
    node.skill_deleted = false;
  }
  // 更新卡片 UI
  var card = document.querySelector('.det-card[data-idx="' + idx + '"]');
  if (card) {
    if (node.enabled) {
      card.classList.remove('disabled');
      // 启用时渲染 tab + select 并加载 skill 列表
      var box = card.querySelector('.det-card-box');
      if (box) {
        var label = box.querySelector('.det-card-label');
        box.innerHTML = '';
        if (label) box.appendChild(label);
        // tab 栏
        var tabBar = document.createElement('div');
        tabBar.className = 'ed-skill-tabs';
        tabBar.style.cssText = 'display:flex;gap:0;margin-bottom:4px;border-bottom:1px solid rgba(0,0,0,.04)';
        tabBar.innerHTML =
          '<a class="ed-skill-tab-opt ac" data-scope="official" data-idx="' + idx + '" style="flex:1;padding:3px 2px;text-align:center;font-size:9px;font-weight:500;cursor:pointer;border-bottom:2px solid var(--a);color:var(--a);transition:all .15s">' + (I18N.t('skills.skill_tab_official')||'技能市场') + '</a>' +
          '<a class="ed-skill-tab-opt" data-scope="repo" data-idx="' + idx + '" style="flex:1;padding:3px 2px;text-align:center;font-size:9px;font-weight:500;cursor:pointer;border-bottom:2px solid transparent;color:var(--t3);transition:all .15s">' + (I18N.t('skills.skill_tab_repo')||'仓库技能') + '</a>';
        box.appendChild(tabBar);
        // select
        var sel = document.createElement('select');
        sel.id = 'ed-skill-' + idx;
        sel.style.width = '100%';
        box.appendChild(sel);
        _loadAgentSkillOptions(idx);
      }
    } else {
      card.classList.add('disabled');
      var box = card.querySelector('.det-card-box');
      if (box) {
        var label = box.querySelector('.det-card-label');
        box.innerHTML = '';
        if (label) box.appendChild(label);
        var span = document.createElement('span');
        span.className = 'det-card-val';
        span.style.color = '#86868b';
        span.textContent = 'none';
        box.appendChild(span);
      }
    }
    var btn = card.querySelector('button');
    if (btn) btn.textContent = node.enabled ? '✕' : '✓';
  }
  // 重新渲染技能套件
  _renderEditorSkillSuite();
}

// ── 保存场景 ──
function _saveScene() {
  var sceneId = (document.getElementById('editorSceneId') || {}).value || '';
  var displayName = (document.getElementById('editorName') || {}).value || '';
  var desc = (document.getElementById('editorDesc') || {}).value || '';
  var catId = parseInt((document.getElementById('editorCategory') || {}).value) || 1;

  if (!sceneId || sceneId.length < 2) {
    if (app && app.showToast) app.showToast(I18N.t('skills.scene_id_required')||'请输入场景标识（英文短横线）');
    return;
  }
  if (sceneId.length > 40) {
    if (app && app.showToast) app.showToast(I18N.t('skills.scene_id_too_long')||'场景标识不能超过40个字符');
    return;
  }
  if (!displayName || displayName.length < 2) {
    if (app && app.showToast) app.showToast(I18N.t('skills.scene_name_placeholder')||'请输入场景名称');
    return;
  }
  if (displayName.length > 40) {
    if (app && app.showToast) app.showToast(I18N.t('skills.scene_name_too_long')||'场景名称不能超过40个字符');
    return;
  }

  var repo = _currentRepoFullName();
  if (!repo) {
    if (app && app.showToast) app.showToast(I18N.t('skills.no_repo_selected')||'请先选择仓库');
    return;
  }

  // 校验：启用的 agent 必须配置了 skill，且 skill 未被删除
  for (var i = 0; i < _sceneEditorNodes.length; i++) {
    var n = _sceneEditorNodes[i];
    if (n.enabled && !n.skill) {
      var meta = _AGENT_META[n.id] || {};
      if (app && app.showToast) app.showToast((meta.label || n.id) + ' Agent 已启用但未配置技能，请选择技能');
      return;
    }
    if (n.enabled && n.skill_deleted) {
      var meta2 = _AGENT_META[n.id] || {};
      if (app && app.showToast) app.showToast((meta2.label || n.id) + ' Agent 引用的技能已删除，请重新选择技能');
      return;
    }
  }

  // 生成 nodes（按 agent 顺序自动生成 next 链路）
  var enabledNodes = _sceneEditorNodes.filter(function(n) { return n.enabled; });
  var nodes = enabledNodes.map(function(n, i) {
    var isLast = i === enabledNodes.length - 1;
    return {
      id: n.id, tag: n.tag, skill: n.skill || '', skill_id: n.skill_id || 0,
      mode: 'emit',
      next: isLast ? 'done' : (enabledNodes[i+1] ? enabledNodes[i+1].id : 'done'),
      handoff: isLast ? 'complete' : 'a2a',
      enabled: true
    };
  });

  if (nodes.length < 2) {
    if (app && app.showToast) app.showToast('至少启用 2 个 agent');
    return;
  }

  var body = {
    name: sceneId,
    display_name: displayName,
    description: desc,
    scope: _sceneEditorScope,
    nodes: nodes,
    icon: _sceneEditorIconUrl,
    obs_icon_key: _sceneEditorIconKey,
    file_id: _sceneEditorFileId,
    category_id: catId,
  };

  if (_sceneEditorMode === 'edit' && _sceneEditorSceneId) {
    API.put('/scenes/market/' + _sceneEditorSceneId, body).then(function(r) {
      if (app && app.showToast) app.showToast(I18N.t('skills.scene_updated')||'场景更新成功');
      app.nav('scene-market');
    }).catch(function(e) {
      if (app && app.showToast) app.showToast((I18N.t('skills.scene_update_failed')||'更新失败') + ': ' + (e.message||''));
    });
  } else {
    API.post('/scenes/market?repo=' + encodeURIComponent(repo), body).then(function(r) {
      if (app && app.showToast) app.showToast(I18N.t('skills.scene_created')||'场景创建成功');
      app.nav('scene-market');
    }).catch(function(e) {
      if (app && app.showToast) app.showToast((I18N.t('skills.scene_create_failed')||'创建失败') + ': ' + (e.message||''));
    });
  }
}


