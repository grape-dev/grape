/**
 * scene-market.js — 场景·技能市场（场景 tab + 场景详情）
 *
 * API:
 *   GET  /api/scenes/market/categories       分类 tab
 *   GET  /api/scenes/market                  列表 (q / category_id / sort)
 *   GET  /api/scenes/market/{id}             详情
 *   POST /api/scenes/market/{id}/apply       应用
 *   POST /api/scenes/market/{id}/experience  体验
 *   POST /api/scenes/market/{id}/like        点赞 toggle
 */

// Agent 图标+颜色映射
const _AGENT_META = {
  vod:    {icon:'🔍', label:'VOD',    color:'#ff2d92', tag:'tg-v'},
  issue:  {icon:'📋', label:'Issue',  color:'#ff3b30', tag:'tg-i'},
  coding: {icon:'💻', label:'Coding', color:'#0071e3', tag:'tg-c'},
  qa:     {icon:'🧪', label:'QA',     color:'#af52de', tag:'tg-q'},
  sre:    {icon:'⚙️', label:'SRE',    color:'#ff9f0a', tag:'tg-s'},
  chat:   {icon:'💬', label:'Chat',   color:'#34c759', tag:'tg-ch'},
};

// 场景分类的背景渐变
const _CAT_BG = {
  1: 'bg-default', 2: 'bg-harmony', 3: 'bg-iac',
  4: 'bg-skill',  5: 'bg-agent',    6: 'bg-blog',
};

// 状态
let _sceneMarketCats = [];
let _sceneMarketScenes = [];
let _sceneMarketFeatured = [];  // 精选场景（独立加载，不受搜索/分类影响）
let _sceneMarketActiveCat = 0;
let _sceneMarketQ = '';
let _sceneMarketSort = 'latest';
let _sceneMarketPage = 1;
let _sceneMarketPageSize = 20;
let _sceneMarketTotal = 0;
let _marketMainTab = 'scenes';

// ── 渲染：场景·技能主页（顶部 tab 切换场景和技能）──
function renderSceneMarket() {
  _renderMarketTab('scenes');
}

function _renderMarketTab(tab) {
  _marketMainTab = tab;
  const el = document.getElementById('pageContent');
  if (!el) return;
  // 如果 tab 栏已存在且只是切换 tab，不重建整个 HTML（避免闪烁）
  const existingTabs = el.querySelector('.stabs');
  if (existingTabs && el.querySelector('#marketTabContent')) {
    // 只更新 tab 高亮
    el.querySelectorAll('.stabs a').forEach(a => a.classList.remove('ac'));
    const tabs = el.querySelectorAll('.stabs a');
    if (tab === 'scenes' && tabs[0]) tabs[0].classList.add('ac');
    if (tab === 'skills' && tabs[1]) tabs[1].classList.add('ac');
  } else {
    // 首次渲染：提示框 + tab 栏 + 容器
    el.innerHTML = `<div class="cd" style="margin-bottom:16px;padding:12px 16px;font-size:12px;color:var(--t2);line-height:1.7;display:flex;align-items:flex-start;gap:10px">
      <span style="font-size:16px;flex-shrink:0">💡</span>
      <div><b style="color:var(--a)">${I18N.t('skills.scenes_tab')||'场景'}</b> ${I18N.t('skills.market_intro_scene')||'是多个技能按开发流程组合的开发模式（如 VOD→Issue→Coding→QA→SRE），'}<b style="color:var(--a)">${I18N.t('skills.skills_tab')||'技能'}</b> ${I18N.t('skills.market_intro_skill')||'是单个 Agent 的能力包，完成具体动作。点击场景卡片查看完整流程，点击技能卡片查看能力详情。'}</div>
    </div>
    <div class="stabs" style="margin-bottom:16px">
      <a class="${tab==='scenes'?'ac':''}" onclick="_renderMarketTab('scenes')">🏪 ${I18N.t('skills.scenes_tab')||'场景'}</a>
      <a class="${tab==='skills'?'ac':''}" onclick="_renderMarketTab('skills')">⚙ ${I18N.t('skills.skills_tab')||'技能'}</a>
    </div><div id="marketTabContent"></div>`;
  }
  if (tab === 'scenes') {
    _loadSceneMarket();
  } else {
    if (typeof renderSkills === 'function') {
      renderSkills();
    } else {
      document.getElementById('marketTabContent').innerHTML = '<div class="error">skills.js not loaded</div>';
    }
  }
}

// ── 加载场景市场 ──
function _loadSceneMarket() {
  const el = document.getElementById('marketTabContent');
  if (!el) return;
  // 并行加载：精选场景（不带搜索/分类参数，最多5条）+ 全部场景
  Promise.all([
    API.get('/scenes/market?sort=latest&limit=5&featured_only=1'),
    API.get('/scenes/market/categories')
  ]).then(([featData, catData]) => {
    _sceneMarketFeatured = featData.scenes || [];
    _sceneMarketCats = catData.categories || [];
    _loadSceneMarketList();
  }).catch(() => {
    _sceneMarketFeatured = [];
    _sceneMarketCats = [];
    _loadSceneMarketList();
  });
}

function _loadSceneMarketList() {
  const el = document.getElementById('marketTabContent');
  if (!el) return;
  let qs = '?sort=' + _sceneMarketSort + '&page=' + _sceneMarketPage + '&page_size=' + _sceneMarketPageSize;
  if (_sceneMarketQ) qs += '&q=' + encodeURIComponent(_sceneMarketQ);
  if (_sceneMarketActiveCat) qs += '&category_id=' + _sceneMarketActiveCat;
  API.get('/scenes/market' + qs).then(d => {
    _sceneMarketScenes = d.scenes || [];
    _sceneMarketTotal = d.total || 0;
    _renderSceneMarketPage();
  }).catch(e => {
    el.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + (e.message||'') + '</div>';
  });
}

// ── 渲染场景市场主页 ──
function _renderSceneMarketPage() {
  const el = document.getElementById('marketTabContent');
  if (!el) return;
  const featured = _sceneMarketFeatured;
  const all = _sceneMarketScenes;

  let html = '';

  // 精选场景横向滚动
  if (featured.length > 0) {
    html += `<div class="cd-h" style="margin-bottom:10px"><h2>🌟 ${I18N.t('skills.featured_scenes')||'精选场景'}</h2></div>`;
    html += `<div class="scn-scroll-wrap" style="margin-bottom:20px">
      <div id="featured-scenes" class="scn-scroll-track" onscroll="_updateScrollBtns('featured-scenes')">
        ${featured.map(s => _renderSceneCard(s)).join('')}
      </div>
      <button class="scn-scroll-btn scn-scroll-prev" id="featured-scenes-prev" onclick="document.getElementById('featured-scenes').scrollBy({left:-314,behavior:'smooth'})">◀</button>
      <button class="scn-scroll-btn scn-scroll-next" id="featured-scenes-next" onclick="document.getElementById('featured-scenes').scrollBy({left:314,behavior:'smooth'})">▶</button>
    </div>`;
    setTimeout(() => _updateScrollBtns('featured-scenes'), 100);
  }

  // 全部场景 — 标题行 + 搜索 + 排序
  html += `<div class="cd-h" style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
    <h2>${I18N.t('skills.all_scenes')||'全部场景'} <span style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:2px 8px;border-radius:8px;margin-left:4px">${_sceneMarketTotal}</span></h2>
    <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
      <input id="sceneMarketSearch" placeholder="${I18N.t('skills.search_placeholder')||'搜索场景名称或描述'}" value="${_sceneMarketQ}" onkeyup="if(event.key==='Enter'){_sceneMarketQ=this.value;_sceneMarketPage=1;_loadSceneMarketList()}" style="padding:7px 12px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c);color:var(--t);width:180px">
      <button class="bt bt-p bt-s" onclick="_sceneMarketQ=document.getElementById('sceneMarketSearch').value;_sceneMarketPage=1;_loadSceneMarketList()">${I18N.t('skills.query')||'查询'}</button>
      <button class="bt ${_sceneMarketSort==='latest'?'bt-p':'bt-o'} bt-s" onclick="_sceneMarketSort='latest';_sceneMarketPage=1;_loadSceneMarketList()">${I18N.t('skills.sort_latest')||'最新'}</button>
      <button class="bt ${_sceneMarketSort==='hot'?'bt-p':'bt-o'} bt-s" onclick="_sceneMarketSort='hot';_sceneMarketPage=1;_loadSceneMarketList()">${I18N.t('skills.sort_hot')||'最热'}</button>
    </div>
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

  // 场景列表网格
  if (all.length === 0) {
    html += `<div class="ts t3" style="padding:20px;text-align:center">${I18N.t('skills.no_scenes')||'暂无场景'}</div>`;
  } else {
    html += `<div class="rgrid">${all.map(s => _renderSceneCard(s)).join('')}</div>`;
  }

  // 分页器（始终显示，只有1页时按钮disabled但选择器可用）
  const totalPages = Math.max(1, Math.ceil(_sceneMarketTotal / _sceneMarketPageSize));
  const prevDisabled = _sceneMarketPage <= 1 ? ' disabled' : '';
  const nextDisabled = _sceneMarketPage >= totalPages ? ' disabled' : '';
  html += `<div class="pager" style="display:flex;align-items:center;justify-content:center;gap:12px;margin-top:16px;padding:12px 0">
    <button class="bt bt-o bt-s${prevDisabled}" ${prevDisabled ? 'disabled' : ''} onclick="_sceneMarketPage=${Math.max(1,_sceneMarketPage-1)};_loadSceneMarketList()">‹</button>
    <span style="font-size:12px;color:var(--t2)">${_sceneMarketPage} / ${totalPages} (${_sceneMarketTotal}条)</span>
    <button class="bt bt-o bt-s${nextDisabled}" ${nextDisabled ? 'disabled' : ''} onclick="_sceneMarketPage=${Math.min(totalPages,_sceneMarketPage+1)};_loadSceneMarketList()">›</button>
    <select onchange="_sceneMarketPageSize=parseInt(this.value);_sceneMarketPage=1;_loadSceneMarketList()" style="padding:4px 8px;border:1px solid var(--b);border-radius:var(--rd);font-size:11px;background:var(--c);color:var(--t);outline:none">
      <option value="20" ${_sceneMarketPageSize===20?'selected':''}>20/页</option>
      <option value="50" ${_sceneMarketPageSize===50?'selected':''}>50/页</option>
      <option value="100" ${_sceneMarketPageSize===100?'selected':''}>100/页</option>
    </select>
  </div>`;

  el.innerHTML = html;
}

// 精选区滚动按钮动态显示/隐藏：根据 scrollLeft 判断左右是否可滚动
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

// 窗口缩放时重新计算所有精选区滚动按钮
window.addEventListener('resize', function() {
  ['featured-scenes', 'featured-skills'].forEach(function(id) {
    if (document.getElementById(id)) _updateScrollBtns(id);
  });
});

// ── 渲染场景卡片（精选和列表共用）──
function _renderSceneCard(s) {
  const catBg = _CAT_BG[s.category_id] || 'bg-default';
  const icon = s.icon || '⚙';
  const skills = s.skills || [];
  const chain = skills.map((sk, i) => {
    const name = sk.name || sk.skill || sk;
    return `${i>0?'<span class="scn-arr">→</span>':''}<span class="scn-tag">${name}</span>`;
  }).join('');
  const likeIcon = s.liked_by_me ? '❤️' : '🤍';
  return `<div class="scn-card" onclick="app.nav('scene-detail','${s.id}')">
    <div class="scn-card-bar ${catBg}"></div>
    <div class="scn-card-body">
      <div class="scn-card-head">
        <div class="scn-ic ${catBg}">${icon}</div>
        <div class="scn-card-title">${s.display_name || s.name}</div>
        ${s.is_featured?'<span class="scn-star">🌟</span>':''}
      </div>
      <div class="scn-card-desc" title="${(s.market_description || s.description || '').replace(/"/g, '&quot;')}">${s.market_description || s.description || ''}</div>
      <div class="scn-chain">${chain}</div>
      <div class="scn-card-foot">
        <span class="scn-meta">${skills.length} ${I18N.t('skills.skills_suffix')||'技能'} · ${s.repo_count||0} ${I18N.t('skills.repos_in_use')||'仓库在用'}</span>
        <span style="cursor:pointer;font-size:11px;color:var(--t2);display:flex;align-items:center;gap:3px" onclick="event.stopPropagation();_toggleSceneLike(${s.id},this)">${likeIcon}<span style="font-size:11px">${s.like_count||0}</span></span>
        <div class="scn-card-btns">
          <button class="bt bt-o bt-s" onclick="event.stopPropagation();_applyScene(${s.id})">${I18N.t('skills.apply_scene')||'应用'}</button>
          ${s.recommended_template ? `<button class="bt bt-p bt-s" onclick="event.stopPropagation();_experienceScene(${s.id})">${I18N.t('skills.experience')||'体验'}</button>` : ''}
        </div>
      </div>
    </div>
  </div>`;
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

// ── 场景应用 ──
function _applyScene(sceneId) {
  const repo = _currentRepoFullName();
  if (!repo) {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.select_repo_first')||'请先选择仓库');
    return;
  }
  const parts = repo.split('/');
  API.post('/scenes/market/' + sceneId + '/apply?owner=' + encodeURIComponent(parts[0]) + '&repo=' + encodeURIComponent(parts[1])).then(r => {
    if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.scene_applied')||'场景已应用');
  }).catch(e => {
    if (app && app.showToast) app.showToast((I18N.t('skills.toast.apply_failed')||'应用失败') + ': ' + (e.message||''));
  });
}

// ── 场景体验 ──
function _experienceScene(sceneId) {
  // 进度弹框
  var modal = document.createElement('div');
  modal.id = 'expSceneModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(4px)';
  modal.innerHTML = '<div class="cd" style="width:440px;padding:24px;border-radius:14px">' +
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
      '<div style="height:6px;background:var(--bg);border-radius:3px;overflow:hidden">' +
        '<div style="height:100%;width:' + pct + '%;background:var(--a);border-radius:3px;transition:width .4s ease"></div>' +
      '</div>' +
      '<div style="text-align:right;font-size:10px;color:var(--t3);margin-top:3px">' + pct + '%</div>';
  }
  function nextStep() {
    if (_step < _steps.length) { setProgress(_steps[_step].pct, _steps[_step].text); _step++; }
  }
  var _timer = setInterval(nextStep, 5000);
  setProgress(10, _steps[0].text);
  _step = 1;

  // 取消按钮：只关闭弹框（API 仍在后台跑，但不跳转）
  document.getElementById('expCancelBtn').onclick = function() {
    clearInterval(_timer);
    modal.remove();
  };

  API.post('/scenes/market/' + sceneId + '/experience', {}, 300000).then(function(r) {
    clearInterval(_timer);
    // 无论新建还是复用已有仓库，都跳转到项目
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
    setProgress(0, (I18N.t('templates.progress_failed', {msg: e.message})||'失败') + ': ' + (e.message||''));
    document.getElementById('expCancelBtn').textContent = I18N.t('common.close')||'关闭';
    if (app && app.showToast) app.showToast((I18N.t('skills.toast.experience_failed')||'体验失败') + ': ' + (e.message||''));
  });
}

// 选中仓库并跳转到场景中心
function _selectRepoAndNav(fullName, owner, name) {
  try {
    if (typeof selectRepo === 'function') {
      selectRepo(fullName, owner, name);
    } else if (typeof app !== 'undefined' && app.switchRepo) {
      app.switchRepo(fullName);
    }
  } catch(e) {}
}

function _currentRepoFullName() {
  try {
    const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
    return c ? (c.fullName || '') : '';
  } catch { return ''; }
}

// ══════════════════════════════════════════
// 场景详情页
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
  const icon = s.icon || '⚙';
  const skills = s.skills || [];
  const likeIcon = s.liked_by_me ? '❤️' : '🤍';

  let html = `<button class="bt bt-o bt-s" onclick="app.nav('scene-market')" style="margin-bottom:12px">← ${I18N.t('skills.back_to_scene_market')||'返回场景市场'}</button>`;

  // 头部：大图标 + 名称 + 精选 + 描述 + 点赞/应用/体验
  html += `<div class="cd" style="padding:20px 24px;margin-bottom:16px">
    <div style="display:flex;align-items:flex-start;gap:16px;margin-bottom:16px">
      <div class="scn-ic scn-ic-lg ${catBg}">${icon}</div>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
          <h2 style="font-size:20px;font-weight:700">${s.display_name || s.name}</h2>
          ${s.is_featured?'<span class="tg" style="background:rgba(212,156,15,.12);color:var(--y);font-size:10px">🌟 精选</span>':''}
          ${s.version?'<span class="tg tg-d" style="font-size:9px">v'+s.version+'</span>':''}
        </div>
        <div style="font-size:13px;color:var(--t2);line-height:1.6">${s.market_description || s.description || ''}</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px;padding-top:14px;border-top:1px solid var(--b)">
      <span style="cursor:pointer;font-size:13px;color:var(--t2);display:flex;align-items:center;gap:4px" onclick="_toggleSceneLike(${s.id},this)">${likeIcon} <span data-scene-like="${s.id}">${s.like_count||0}</span></span>
      <span style="font-size:11px;color:var(--t3)">${s.repo_count||0} ${I18N.t('skills.repos_in_use')||'仓库在用'}</span>
      <div style="margin-left:auto;display:flex;gap:8px">
        <button class="bt bt-o bt-s" onclick="_applyScene(${s.id})">${I18N.t('skills.apply_scene')||'应用'}</button>
        ${s.recommended_template ? `<button class="bt bt-p bt-s" onclick="_experienceScene(${s.id})">${I18N.t('skills.experience')||'体验'}</button>` : ''}
      </div>
    </div>
  </div>`;

  // 执行流程图（参考场景中心卡片风格）
  if (skills.length > 0) {
    html += `<style>
      .det-card-wrap{background:#fff;border:1px solid #e8e8ed;border-radius:18px;padding:24px;margin-bottom:16px}
      .det-grid{display:flex;gap:12px;overflow-x:auto;min-height:280px;scrollbar-width:none}
      .det-grid::-webkit-scrollbar{display:none}
      .det-card{flex:1 1 0;min-width:0;max-width:260px;min-height:260px;background:#f5f5f7;border:1px solid #e8e8ed;border-radius:14px;padding:16px;display:flex;flex-direction:column;transition:all .2s;position:relative}
      .det-card:hover{background:#fff;box-shadow:0 4px 20px rgba(0,0,0,.08);transform:translateY(-1px)}
      .det-card-top{width:36px;height:4px;border-radius:2px;margin-bottom:12px}
      .det-card-desc{font-size:10px;color:#86868b;line-height:1.5;margin-bottom:10px;flex:1}
      .det-card-sep{height:1px;background:#e8e8ed;margin:8px 0}
      .det-card-box{background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;margin-bottom:8px;display:flex;flex-direction:column;align-items:center}
      .det-card-label{font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
      .det-card-val{font-size:11px;font-weight:500;color:#1d1d1f}
      .det-card-eng{background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center}
      .det-arrow{font-size:18px;color:#0071e3;font-weight:700;align-self:center;margin:0 4px;flex-shrink:0}
    </style>`;
    html += `<div class="cd-h" style="margin-bottom:10px"><h2>${I18N.t('skills.execution_flow')||'执行流程'}</h2></div>`;
    html += `<div class="det-card-wrap"><div class="det-grid">`;
    skills.forEach((sk, i) => {
      const meta = _AGENT_META[sk.tag] || _AGENT_META[sk.agent_type] || {};
      const bgColor = meta.color || '#0071e3';
      const skIcon = meta.icon || '◈';
      const skLabel = meta.label || sk.tag || '';
      const skName = sk.display_name || sk.name || sk.skill || '';
      const roleDesc = sk.role_desc || '';
      html += `<div class="det-card">
        <div class="det-card-top" style="background:${bgColor}"></div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="width:22px;height:22px;border-radius:6px;background:${bgColor}22;color:${bgColor};font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0">${i+1}</span>
          <span style="font-size:14px;font-weight:600;color:#1d1d1f;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${skIcon} ${skLabel}</span>
        </div>
        ${roleDesc ? `<div class="det-card-desc">${roleDesc}</div>` : '<div class="det-card-desc"></div>'}
        <div class="det-card-sep"></div>
        <div class="det-card-box">
          <div class="det-card-label">${I18N.t('dashboard.label_skill')||'技能'}</div>
          <span class="det-card-val" style="color:#0071e3;cursor:pointer" onclick="app.nav('skill','${sk.skill_id||''}')">${skName}</span>
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

  // 技能套件（卡片网格）
  if (skills.length > 0) {
    html += `<style>
      .det-suite-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
      .det-suite-card{background:#f5f5f7;border:1px solid #e8e8ed;border-radius:14px;padding:16px;display:flex;align-items:flex-start;gap:12px;transition:all .2s;cursor:pointer}
      .det-suite-card:hover{background:#fff;box-shadow:0 4px 20px rgba(0,0,0,.08);transform:translateY(-1px)}
      .det-suite-num{width:28px;height:28px;border-radius:8px;color:#fff;font-size:12px;display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0}
      .det-suite-body{flex:1;min-width:0}
      .det-suite-name{font-size:14px;font-weight:600;color:#1d1d1f;margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .det-suite-role{font-size:11px;color:#86868b;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .det-suite-badge{font-size:10px;font-weight:600;padding:3px 10px;border-radius:999px;flex-shrink:0}
    </style>`;
    html += `<div class="cd-h" style="margin:20px 0 10px"><h2>${I18N.t('skills.skill_suite')||'技能套件'}</h2></div>`;
    html += `<div class="det-card-wrap"><div class="det-suite-grid">`;
    skills.forEach((sk, i) => {
      const meta = _AGENT_META[sk.tag] || _AGENT_META[sk.agent_type] || {};
      const bgColor = meta.color || '#0071e3';
      const skIcon = meta.icon || '◈';
      const skLabel = meta.label || sk.tag || '';
      const skName = sk.display_name || sk.name || sk.skill || '';
      const roleDesc = sk.role_desc || '';
      html += `<div class="det-suite-card" onclick="app.nav('skill','${sk.skill_id||''}')">
        <div class="det-suite-num" style="background:${bgColor}">${i+1}</div>
        <div class="det-suite-body">
          <div class="det-suite-name">${skIcon} ${skName}</div>
          <div class="det-suite-role">${roleDesc || skLabel}</div>
        </div>
        <span class="det-suite-badge" style="background:${bgColor}14;color:${bgColor}">${skLabel}</span>
      </div>`;
    });
    html += `</div></div>`;
  }

  // 样例项目模板
  if (s.recommended_template) {
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
      ? '<span style="font-size:9px;font-weight:600;padding:2px 8px;border-radius:999px;background:#34c75914;color:#34c759">GitCode 远程</span>'
      : '<span style="font-size:9px;font-weight:600;padding:2px 8px;border-radius:999px;background:var(--bg);color:var(--t3)">本地模板</span>';
    html += `<div class="cd-h" style="margin:20px 0 10px"><h2>${I18N.t('skills.sample_template')||'样例项目模板'}</h2></div>`;
    html += `<div class="det-card-wrap"${isRemote ? ` style="cursor:pointer" onclick="window.open('${tplUrl}','_blank')"` : ''}>
      <div style="display:flex;align-items:center;gap:16px;padding:8px 0">
        <span style="font-size:32px;flex-shrink:0">${tplIcon}</span>
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap">
            <span style="font-size:15px;font-weight:600;color:#1d1d1f">${tplName}</span>
            ${sourceBadge}
            ${tplLang ? `<span style="font-size:10px;font-weight:600;padding:2px 8px;border-radius:999px;background:#0071e314;color:#0071e3">${tplLang}</span>` : ''}
            ${tplStars ? `<span style="font-size:11px;color:#86868b">⭐ ${tplStars}</span>` : ''}
          </div>
          <div style="font-size:12px;color:#86868b;line-height:1.5">${tplDesc || (I18N.t('skills.template_desc')||'点击体验可基于此模板创建项目')}</div>
        </div>
        ${isRemote ? `<span style="font-size:12px;color:#0071e3;font-weight:500;flex-shrink:0;padding:6px 14px;border:1px solid #0071e3;border-radius:8px;transition:all .15s" onmouseover="this.style.background='#0071e3';this.style.color='#fff'" onmouseout="this.style.background='';this.style.color='#0071e3'">查看项目 ↗</span>` : ''}
      </div>
    </div>`;
  }

  // 在用仓库列表
  const repos = s.repos || [];
  if (repos.length > 0) {
    const REPO_PREVIEW = 6;
    const hasMore = repos.length > REPO_PREVIEW;
    const renderRepoCard = (r, idx) => {
      const fullName = r.full_name || r.repo_key || '';
      const accessible = r.accessible;
      const cardStyle = accessible ? 'cursor:pointer' : 'cursor:default;opacity:0.6';
      const clickAction = accessible ? ` onclick="window.open('https://gitcode.com/${fullName}','_blank')"` : '';
      const badge = accessible
        ? '<span class="det-suite-badge" style="background:#34c75914;color:#34c759">查看 ↗</span>'
        : '<span class="det-suite-badge" style="background:var(--bg);color:var(--t3)">无权限</span>';
      return `<div class="det-suite-card" data-repo-idx="${idx}"${clickAction} style="${cardStyle}">
        <div class="det-suite-num" style="background:#34c759">📂</div>
        <div class="det-suite-body">
          <div class="det-suite-name">${fullName}</div>
          <div class="det-suite-role">GitCode 仓库</div>
        </div>
        ${badge}
      </div>`;
    };
    html += `<div class="cd-h" style="margin:20px 0 10px"><h2>${I18N.t('skills.repos_in_use_title')||'在用仓库'} <span style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:2px 8px;border-radius:8px;margin-left:4px">${repos.length}</span></h2></div>`;
    html += `<div class="det-card-wrap"><div class="det-suite-grid" id="repoGrid">`;
    repos.slice(0, REPO_PREVIEW).forEach((r, i) => { html += renderRepoCard(r, i); });
    html += `</div></div>`;
    if (hasMore) {
      const restHtml = repos.slice(REPO_PREVIEW).map((r, i) => renderRepoCard(r, i + REPO_PREVIEW)).join('');
      html += `<div id="repoRest" style="display:none"><div class="det-card-wrap" style="margin-top:8px"><div class="det-suite-grid">${restHtml}</div></div></div>`;
      html += `<div style="text-align:center;margin-top:10px"><a onclick="(function(){document.getElementById('repoRest').style.display='block';document.getElementById('repoMore').style.display='none';})()" id="repoMore" style="font-size:12px;color:var(--a);cursor:pointer">查看全部 (${repos.length})</a></div>`;
    }
  }

  el.innerHTML = html;
}
