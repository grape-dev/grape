/**
 * skills.js — 技能市场页面（在场景·技能页面的"技能" tab 内渲染）
 * 按原型 market-prototype-v2.html 结构重写
 *
 * 二级页签：技能市场(official) / 仓库技能(repo) / 个人技能(user)
 * 精选技能横向滚动 + 全部技能(Agent tab + 搜索 + 排序 + 查找 + 上传)
 */

let _skillDetailVisible = false;
let _skillTab = 'official';  // official / repo / user
let _skillSort = 'latest';   // latest / hot
let _skillSearchQ = '';      // 搜索词（调后端查询）
let _skillPage = 1;
let _skillPageSize = 20;
let _skillTotal = 0;          // 全部技能总数（来自 agent-stats）
let _skillPageTotal = 0;      // 当前分页查询的总数（来自 list 接口，含 agent_type 过滤）
let _allSkills = [];         // 当前页技能缓存
let _featuredSkills = [];    // 精选技能（独立加载，不受搜索影响）
let _agentStats = {};        // {agent_type: count} 来自 agent-stats 接口
let _agentList = ['vod','issue','coding','qa','sre','chat'];  // 固定顺序
let _activeAgentIdx = -1;    // -1=无选中(显示全部), 0+=第N个agent
let _activeTagFilter = 'all';  // 当前标签筛选（中文 label 用于显示）
let _activeTagFilterId = '';  // 当前标签筛选英文 ID（用于 API 查询）

// 技能标签分类定义（二级筛选）—— 从接口返回的 sub_scene 动态渲染

// _getSkillTags 已废弃——sub_scene 由接口返回，直接匹配

// 精选技能卡片渐变色（按 agent_type）
const _FEAT_GRADIENT = {
  vod:    {g1:'#8FA9C4', g2:'#53688A'},
  issue:  {g1:'#A99C7C', g2:'#75684A'},
  coding: {g1:'#7EA48C', g2:'#4B7359'},
  qa:     {g1:'#A49AC0', g2:'#72638F'},
  sre:    {g1:'#C3917E', g2:'#97503D'},
  chat:   {g1:'#45B8B8', g2:'#0A7A7A'},
};

// _AGENT_META 定义在 scene-market.js 中，这里直接复用

function _scopeName(scope) {
  var names = {
    official: I18N.t('skills.scope_official')||'技能市场',
    repo: I18N.t('skills.scope_repo')||'仓库技能',
    user: I18N.t('skills.scope_user')||'个人技能',
  };
  return names[scope] || scope;
}

function _skillsContainer() {
  return document.getElementById('skillsTabContent') || document.getElementById('pageContent');
}

// 技能详情返回：根据来源决定返回目标（不 push history，避免历史栈膨胀）
function _backFromSkillDetail() {
  const from = window._skillDetailFrom;
  if (!from || !from.view) {
    app.nav('scene-market');
    return;
  }
  if (from.view === 'scene-detail' && from.param) {
    window._currentView = 'scene-detail';
    renderSceneDetail(from.param);
    // replaceState 更新当前历史记录为场景详情状态，
    // 否则 history.state 仍停留在技能详情，下次进入技能时来源判断错误
    try {
      history.replaceState({view: 'scene-detail', param: from.param}, '', '#scene-detail/' + encodeURIComponent(from.param));
    } catch(e) {}
  } else {
    _backToSkillList();
  }
}

// 华为云子技能详情返回：重新渲染华为云技能包页面（不经过 app.nav，不 push history）
function _backFromSubSkill(pkgId) {
  var el = _skillsContainer();
  if (!el) return;
  el.innerHTML = '<div class="loading">'+(I18N.t('common.loading')||'加载中...')+'</div>';
  API.get('/skills/by-id/' + pkgId).then(function(data) {
    renderHuaweiCloudPackage(el, data);
  }).catch(function(e) {
    el.innerHTML = '<div class="error">'+(I18N.t('common.load_failed')||'加载失败')+': '+(e.message||'')+'</div>';
  });
}

// 从技能详情返回技能列表（渲染完整页面含一级 Tab 栏）
function _backToSkillList() {
  window._currentView = 'skills:' + _skillTab;
  try {
    history.replaceState({view: 'skills'}, '', '#skills');
  } catch(e) {}
  renderSkills();
}

function _currentRepo() {
  try {
    const c = JSON.parse(localStorage.getItem('currentRepo') || 'null');
    return c ? (c.fullName || '') : '';
  } catch { return ''; }
}

// ── 渲染技能市场主页 ──
function renderSkills() {
  const el = document.getElementById('pageContent');
  if (!el) return;

  // 渲染一级 Tab 栏（场景 / 技能）+ 子容器
  el.innerHTML = `<div style="margin-bottom:20px;background:var(--c);border-radius:14px;padding:20px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.06)">
    <div class="seg-bar" style="margin-top:0">
      <a onclick="app.nav('scene-market')">🏪 ${I18N.t('skills.scenes_tab')||'场景'}</a>
      <a class="ac">⚙ ${I18N.t('skills.skills_tab')||'技能'}</a>
    </div>
    <div style="padding:14px 0 0;font-size:11px;color:var(--t3);line-height:1.6">
      <b style="color:var(--a)">${I18N.t('skills.scenes_tab')||'场景'}</b> ${I18N.t('skills.market_intro_scene')||'是多个技能按开发流程组合的开发模式'} · <b style="color:var(--a)">${I18N.t('skills.skills_tab')||'技能'}</b> ${I18N.t('skills.market_intro_skill')||'是单个 Agent 的能力包'}
    </div>
  </div>
  <div id="skillsTabContent"></div>`;

  // 预填搜索词（从场景详情跳转过来）
  let searchInit = '';
  if (window._skillMarketSearchInit) {
    searchInit = window._skillMarketSearchInit;
    window._skillMarketSearchInit = null;
  }

  // 并行加载：agent-stats + 技能列表 + 精选技能(official)
  _loadAgentStats();
  _loadSkills(searchInit);
  if (_skillTab === 'official') {
    API.get('/skills/featured').then(featData => {
      _featuredSkills = featData.skills || [];
      _renderSkillPage();
    }).catch(() => {
      _featuredSkills = [];
    });
  }
}

// 加载 agent 统计
function _loadAgentStats() {
  const params = ['scope=' + _skillTab];
  const repo = _currentRepo();
  if (repo) params.push('repo=' + encodeURIComponent(repo));
  API.get('/skills/agent-stats?' + params.join('&')).then(d => {
    _agentStats = {};
    (d.agents || []).forEach(a => { _agentStats[a.agent_type] = a.count; });
    _skillTotal = d.total || 0;
    _renderAgentSidebar();
  }).catch(() => {});
}

function _loadSkills(searchInit) {
  const el = _skillsContainer();
  if (!el) return;
  _reloadSkills(searchInit);
}

// 从后端加载技能（带 scope + agent_type + 搜索词 + 排序 + 分页）
function _reloadSkills(searchInit) {
  const el = _skillsContainer();
  if (!el) return;
  const repo = _currentRepo();
  const params = [];
  if (repo) params.push('repo=' + encodeURIComponent(repo));
  if (_skillTab) params.push('scope=' + _skillTab);
  if (_skillSort) params.push('sort=' + _skillSort);
  if (_skillSearchQ) params.push('q=' + encodeURIComponent(_skillSearchQ));
  // agent_type 过滤
  if (_activeAgentIdx >= 0 && _agentList[_activeAgentIdx]) {
    params.push('agent_type=' + _agentList[_activeAgentIdx]);
  }
  params.push('page=' + _skillPage);
  params.push('page_size=' + _skillPageSize);
  const qs = '?' + params.join('&');
  // 局部刷新：已有 all-skills 容器时保留旧内容，API 回调后直接覆盖（避免高度塌缩导致跳动）
  var existingGrid = document.getElementById('all-skills');
  API.get('/skills' + qs).then(d => {
    if (window._currentView !== 'skills' && window._currentView !== 'skills:' + _skillTab) return;  // stale guard
    _allSkills = d.skills || [];
    _skillPageTotal = d.total || _allSkills.length;
    if (existingGrid) {
      _renderSkillGrid();
    } else {
      _renderSkillPage();
    }
    if (searchInit) {
      setTimeout(() => {
        const input = document.getElementById('skillSearchInput');
        if (input) { input.value = searchInit; _skillSearchQ = searchInit; _skillPage = 1; _reloadSkills(); }
      }, 100);
    }
  }).catch(e => {
    if (e.name === 'AbortError' && e.silent) return;  // Issue #63: abort 静默
    if (window._currentView !== 'skills' && window._currentView !== 'skills:' + _skillTab) return;  // stale guard
    el.innerHTML = '<div class="error">' + (I18N.t('common.load_failed')||'加载失败') + ': ' + (e.message||'') + '</div>';
  });
}

function _renderSkillPage() {
  const el = _skillsContainer();
  if (!el) return;

  let html = '';

  // 二级子页签（药丸分段控件，靠左）
  html += `<div style="text-align:left;margin-bottom:20px"><div class="seg-bar" style="margin-top:0">
    <a class="${_skillTab==='official'?'ac':''}" onclick="_switchSkillTab('official')">${_scopeName('official')}</a>
    <a class="${_skillTab==='repo'?'ac':''}" onclick="_switchSkillTab('repo')">${_scopeName('repo')}</a>
    <a class="${_skillTab==='user'?'ac':''}" onclick="_switchSkillTab('user')">${_scopeName('user')}</a>
  </div></div>`;

  // 精选技能（仅 official，独立加载，不受搜索影响）— 渐变广告卡网格
  if (_skillTab === 'official' && _featuredSkills.length > 0) {
    html += `<div class="apple-card">
      <div class="cd-h"><h2>🌟 ${I18N.t('skills.featured_skills')||'精选技能'}</h2></div>
      <div class="feat-row" id="featured-skills">
        ${_featuredSkills.map(s => _renderFeaturedSkillCard(s)).join('')}
      </div>
    </div>`;
  }

  // 全部技能
  html += `<div class="apple-card">`;
  // 标题行 + 搜索 + 查找 + 排序 + 上传
  html += `<div class="cd-h apple-search" style="justify-content:space-between">
    <h2>${I18N.t('skills.all_skills')||'全部技能'} <span style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:2px 8px;border-radius:8px;margin-left:4px">${_skillTotal}</span></h2>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <button class="bt ${_skillSort==='latest'?'bt-tg':'bt-o'} bt-s" id="sort-latest" onclick="_switchSort('latest')">${I18N.t('skills.sort_latest')||'最新'}</button>${_skillTab === 'user' ? '' : `<button class="bt ${_skillSort==='hot'?'bt-tg':'bt-o'} bt-s" id="sort-hot" onclick="_switchSort('hot')">${I18N.t('skills.sort_hot')||'最热'}</button>`}
      <input id="skillSearchInput" placeholder="${I18N.t('skills.skill_search_placeholder')||'搜索技能名称或描述'}" value="${_skillSearchQ}" onkeyup="if(event.key==='Enter'){_skillSearchQ=this.value;_skillPage=1;_reloadSkills()}">
      <button class="bt bt-o bt-s" onclick="_skillSearchQ=document.getElementById('skillSearchInput').value;_skillPage=1;_reloadSkills()">${I18N.t('skills.find_skill')||'查找技能'}</button>
      <button class="bt bt-o bt-s" onclick="_resetSkillFilters()">${I18N.t('skills.reset_filters')||'重置'}</button>`;
  if (_skillTab === 'repo' || _skillTab === 'user') {
    html += `<button class="bt bt-p bt-s" onclick="showSkillUpload()" style="color:#fff;background:var(--g);border-color:var(--g)">${I18N.t('skills.action.add_skill')||'上传技能'}</button>`;
  }
  html += `</div></div>`;

  // 仓库技能需当前仓库
  if (_skillTab === 'repo' && !_currentRepo()) {
    html += `<div class="ts t2" style="padding:12px">${I18N.t('skills.empty.no_repo_selected')||'请先选择仓库'}</div>`;
    html += `</div>`;
    el.innerHTML = html;
    return;
  }

  // 左右分栏：左侧 Agent 筛选栏 + 右侧（标签筛选行 + 技能卡片网格）
  html += `<div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-top:10px">`;
  // 左侧 Agent 筛选栏（竖向排列，无"全部"）
  html += `<div id="agent-tabs" style="width:160px;flex-shrink:0;display:flex;flex-direction:column;gap:2px">`;
  _agentList.forEach((at, i) => {
    const meta = _AGENT_META[at] || {};
    const count = _agentStats[at] || 0;
    const countBadge = count > 0 ? ` <span class="c" style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:1px 6px;border-radius:8px;margin-left:2px">${count}</span>` : '';
    const isOn = _activeAgentIdx === i;
    html += `<a class="skill-agent-item${isOn?' ac':''}" href="javascript:void(0)" onclick="_switchAgentTab(${i})" style="padding:10px 14px;font-size:12px;font-weight:${isOn?'600':'500'};color:${isOn?'var(--a)':'var(--t2)'};cursor:pointer;border-radius:8px;transition:all .15s;display:flex;align-items:center;gap:4px;white-space:nowrap;background:${isOn?'rgba(49,104,224,0.08)':'transparent'}">${meta.icon?`<span style="margin-right:2px">${meta.icon}</span>`:''}${(meta.label||at)+' Agent'}${countBadge}</a>`;
  });
  html += `</div>`;
  // 右侧：标签筛选行 + 技能列表 + 分页器
  html += `<div style="flex:1;min-width:280px">`;
  // 标签筛选行（横向排列，二级筛选）—— 动态渲染，_renderTagFilters() 填充
  html += `<div id="tag-filters" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px"></div>`;
  // 技能列表
  html += `<div id="all-skills"></div>`;
  // 分页器（始终显示，只有1页时按钮disabled但选择器可用）
  const totalPages = Math.max(1, Math.ceil(_skillPageTotal / _skillPageSize));
  const prevDisabled = _skillPage <= 1 ? ' disabled' : '';
  const nextDisabled = _skillPage >= totalPages ? ' disabled' : '';
  html += `<div id="skill-pager" style="display:flex;align-items:center;justify-content:center;gap:12px;margin-top:16px;padding:12px 0">
    <button class="bt bt-o bt-s${prevDisabled}" ${prevDisabled ? 'disabled' : ''} onclick="_skillPage=${Math.max(1,_skillPage-1)};_reloadSkills()">‹</button>
    <span style="font-size:12px;color:var(--t2)">${_skillPage} / ${totalPages} (${_skillPageTotal}${I18N.t('skills.items_count_suffix')||'条'})</span>
    <button class="bt bt-o bt-s${nextDisabled}" ${nextDisabled ? 'disabled' : ''} onclick="_skillPage=${Math.min(totalPages,_skillPage+1)};_reloadSkills()">›</button>
    <select onchange="_skillPageSize=parseInt(this.value);_skillPage=1;_reloadSkills()" style="padding:4px 8px;border:1px solid var(--b);border-radius:var(--rd);font-size:11px;background:var(--c);color:var(--t);outline:none">
      <option value="20" ${_skillPageSize===20?'selected':''}>20${I18N.t('skills.per_page_suffix')||'/页'}</option>
      <option value="50" ${_skillPageSize===50?'selected':''}>50${I18N.t('skills.per_page_suffix')||'/页'}</option>
      <option value="100" ${_skillPageSize===100?'selected':''}>100${I18N.t('skills.per_page_suffix')||'/页'}</option>
    </select>
  </div>`;
  html += `</div></div></div>`;

  el.innerHTML = html;

  // 渲染全部技能
  _activeAgentIdx = -1;
  _activeTagFilter = 'all';
  _renderTagFilters();
  _renderAgentSkills();
}

// 局部刷新：只更新技能列表 + 分页器 + 计数 + Agent tab + 排序按钮（不重建搜索栏/排序栏）
function _renderSkillGrid() {
  // 更新计数
  var countEl = document.querySelector('#skillsTabContent .apple-card .cd-h h2 span');
  if (countEl) countEl.textContent = _skillTotal;

  // 更新 Agent sidebar（用 agent-stats 数据）
  _renderAgentSidebar();

  // 更新标签筛选
  _renderTagFilters();

  // 更新排序按钮高亮
  var sortLatest = document.getElementById('sort-latest');
  var sortHot = document.getElementById('sort-hot');
  if (sortLatest) { sortLatest.className = 'bt ' + (_skillSort==='latest'?'bt-tg':'bt-o') + ' bt-s'; }
  if (sortHot) { sortHot.className = 'bt ' + (_skillSort==='hot'?'bt-tg':'bt-o') + ' bt-s'; }

  // 更新分页器
  var pager = document.getElementById('skill-pager');
  if (pager) {
    var totalPages = Math.max(1, Math.ceil(_skillPageTotal / _skillPageSize));
    var prevDisabled = _skillPage <= 1 ? ' disabled' : '';
    var nextDisabled = _skillPage >= totalPages ? ' disabled' : '';
    pager.innerHTML =
      '<button class="bt bt-o bt-s' + prevDisabled + '" ' + (prevDisabled ? 'disabled' : '') + ' onclick="_skillPage=' + Math.max(1,_skillPage-1) + ';_reloadSkills()">‹</button>' +
      '<span style="font-size:12px;color:var(--t2)">' + _skillPage + ' / ' + totalPages + ' (' + _skillPageTotal + (I18N.t('skills.items_count_suffix')||'条') + ')</span>' +
      '<button class="bt bt-o bt-s' + nextDisabled + '" ' + (nextDisabled ? 'disabled' : '') + ' onclick="_skillPage=' + Math.min(totalPages,_skillPage+1) + ';_reloadSkills()">›</button>' +
      '<select onchange="_skillPageSize=parseInt(this.value);_skillPage=1;_reloadSkills()" style="padding:4px 8px;border:1px solid var(--b);border-radius:var(--rd);font-size:11px;background:var(--c);color:var(--t);outline:none">' +
      '<option value="20" ' + (_skillPageSize===20?'selected':'') + '>20' + (I18N.t('skills.per_page_suffix')||'/页') + '</option>' +
      '<option value="50" ' + (_skillPageSize===50?'selected':'') + '>50' + (I18N.t('skills.per_page_suffix')||'/页') + '</option>' +
      '<option value="100" ' + (_skillPageSize===100?'selected':'') + '>100' + (I18N.t('skills.per_page_suffix')||'/页') + '</option>' +
      '</select>';
  }

  // 渲染技能列表（保留当前筛选状态，不重置 agent/tag）
  _renderAgentSkills();
}

function _switchSkillTab(tab) {
  _skillTab = tab;
  _skillPage = 1;
  _activeAgentIdx = -1;
  _activeTagFilter = 'all';
  _activeTagFilterId = '';
  // Issue #63: 页面内 tab 切换——取消旧 tab 请求 + 更新标识
  if (window._tabAbortController) window._tabAbortController.abort();
  window._tabAbortController = new AbortController();
  window._tabSignal = window._tabAbortController.signal;
  window._currentView = 'skills:' + tab;
  // 局部刷新：只重建列表区域（_renderSkillPage），不重建顶部 Tab 栏（renderSkills）
  _renderSkillPage();
  _loadAgentStats();
  _reloadSkills();
}

function _switchAgentTab(idx) {
  _activeAgentIdx = (_activeAgentIdx === idx) ? -1 : idx;
  _activeTagFilter = 'all';
  _activeTagFilterId = '';
  _skillPage = 1;
  _renderAgentSidebar();
  _renderTagFilters();
  _reloadSkills();
}

function _resetSkillFilters() {
  _activeAgentIdx = -1;
  _activeTagFilter = 'all';
  _activeTagFilterId = '';
  _skillSearchQ = '';
  _skillPage = 1;
  _renderAgentSidebar();
  _renderTagFilters();
  var input = document.getElementById('skillSearchInput');
  if (input) input.value = '';
  _reloadSkills();
}

function _switchSort(mode) {
  _skillSort = mode;
  _skillPage = 1;
  _reloadSkills();
}

function _switchTagFilter(label, id) {
  _activeTagFilter = label;
  _activeTagFilterId = id || '';
  _renderTagFilters();
  _renderAgentSkills();
}

function _renderTagFilters() {
  var container = document.getElementById('tag-filters');
  if (!container) return;
  // 从当前页技能列表动态提取 sub_scene 去重
  var seen = {};
  var tags = [];
  _allSkills.forEach(function(s) {
    var label = s.sub_scene || '';
    var id = s.sub_scene_id || '';
    if (label && id && !seen[id]) {
      seen[id] = 1;
      tags.push({label: label, id: id});
    }
  });
  // 当前选中的 tag 不在列表中时重置为 all
  if (_activeTagFilter !== 'all' && tags.every(function(t) { return t.id !== _activeTagFilterId; })) {
    _activeTagFilter = 'all';
    _activeTagFilterId = '';
  }
  var html = '';
  // "全部" 始终第一个
  var allOn = _activeTagFilter === 'all';
  html += '<a class="skill-tag-filter' + (allOn?' ac':'') + '" href="javascript:void(0)" onclick="_switchTagFilter(\'all\',\'\')" style="padding:5px 12px;font-size:11px;color:' + (allOn?'var(--a)':'var(--t2)') + ';border:1px solid ' + (allOn?'var(--a)':'var(--b)') + ';background:' + (allOn?'rgba(49,104,224,0.06)':'var(--c)') + ';cursor:pointer;border-radius:8px;transition:all .15s;white-space:nowrap">全部</a>';
  tags.forEach(function(t) {
    var isOn = _activeTagFilterId === t.id;
    html += '<a class="skill-tag-filter' + (isOn?' ac':'') + '" href="javascript:void(0)" onclick="_switchTagFilter(\'' + t.label.replace(/'/g, "\\'") + '\',\'' + t.id + '\')" style="padding:5px 12px;font-size:11px;color:' + (isOn?'var(--a)':'var(--t2)') + ';border:1px solid ' + (isOn?'var(--a)':'var(--b)') + ';background:' + (isOn?'rgba(49,104,224,0.06)':'var(--c)') + ';cursor:pointer;border-radius:8px;transition:all .15s;white-space:nowrap">' + t.label + '</a>';
  });
  container.innerHTML = html;
}

function _renderAgentSidebar() {
  var container = document.getElementById('agent-tabs');
  if (!container) return;
  var html = '';
  _agentList.forEach(function(at, i) {
    var meta = _AGENT_META[at] || {};
    var count = _agentStats[at] || 0;
    var countBadge = count > 0 ? ' <span class="c" style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:1px 6px;border-radius:8px;margin-left:2px">' + count + '</span>' : '';
    var isOn = _activeAgentIdx === i;
    html += '<a class="skill-agent-item' + (isOn?' ac':'') + '" href="javascript:void(0)" onclick="_switchAgentTab(' + i + ')" style="padding:10px 14px;font-size:12px;font-weight:' + (isOn?'600':'500') + ';color:' + (isOn?'var(--a)':'var(--t2)') + ';cursor:pointer;border-radius:8px;transition:all .15s;display:flex;align-items:center;gap:4px;white-space:nowrap;background:' + (isOn?'rgba(49,104,224,0.08)':'transparent') + '">' + (meta.icon?'<span style="margin-right:2px">' + meta.icon + '</span>':'') + (meta.label||at) + ' Agent' + countBadge + '</a>';
  });
  container.innerHTML = html;
}

function _renderAgentSkills() {
  const container = document.getElementById('all-skills');
  if (!container) return;
  // 二级标签筛选——客户端过滤当前页数据
  let skills = _allSkills;
  if (_activeTagFilter !== 'all') {
    skills = skills.filter(s => (s.sub_scene_id || '') === _activeTagFilterId);
  }
  if (!skills.length) {
    container.innerHTML = `<div class="ts t3" style="padding:20px;text-align:center">${I18N.t('skills.empty.no_skills')||'暂无技能'}</div>`;
    return;
  }
  container.innerHTML = `<div class="rgrid">${skills.map(s => _renderSkillCard(s)).join('')}</div>`;
}

// ── 渲染精选技能卡片（渐变广告卡风格，对齐 new.html 原型）──
function _renderFeaturedSkillCard(s) {
  const at = s.agent_type || '';
  const meta = _AGENT_META[at] || {};
  const grad = _FEAT_GRADIENT[at] || {g1:'#3168e0', g2:'#2556c2'};
  const scenesArr = (s.scenes || []).map(sc => sc.display_name || sc.name || sc);
  const scenesStr = scenesArr.length > 0
    ? (scenesArr.length <= 2
        ? scenesArr.join(' · ')
        : `${scenesArr.slice(0, 2).join(' · ')}(+${scenesArr.length - 2})`)
    : '';
  const sceneCount = s.scene_count || scenesArr.length || 0;
  const repoCount = s.repo_count || 0;
  const desc = (s.description || '').replace(/"/g, '&quot;');
  return `<div class="feat-card" style="--fg1:${grad.g1};--fg2:${grad.g2}" onclick="app.nav('skill','${s.id}')">
    <span class="feat-badge">★</span>
    <div class="feat-ico">${meta.icon || '◈'}</div>
    <div class="feat-name">${s.display_name || s.name}<span class="feat-agent">${meta.label || at}</span></div>
    <div class="feat-desc">${desc}</div>
    <div class="feat-scenes">${scenesStr || ''}</div>
    <div class="feat-scenes" style="margin-top:auto;padding-top:8px">${sceneCount} 场景在用 · ${repoCount} 仓库在用</div>
  </div>`;
}

// ── 渲染技能卡片（精选和列表共用）──
function _renderSkillCard(s, fixedWidth) {
  const at = s.agent_type || '';
  const meta = _AGENT_META[at] || {};
  const tagClass = s.scope === 'official' ? 'tg-c' : s.scope === 'repo' ? 'tg-i' : 'tg-d';
  const scenesArr = (s.scenes || []).map(sc => sc.display_name || sc.name || sc);
  const sceneCount = s.scene_count || scenesArr.length || 0;
  const scenesStr = scenesArr.length > 0
    ? (scenesArr.length <= 2
        ? scenesArr.join(' · ')
        : `${scenesArr.slice(0, 2).join(' · ')}(+${scenesArr.length - 2})`)
    : '';
  const scenesTitle = scenesArr.join(' · ');
  const isUserSkill = s.scope === 'user';
  const likeIcon = s.liked_by_me ? '❤️' : '🤍';
  const likeCount = s.like_count || 0;
  const featuredBadge = (s.is_featured && !isUserSkill) ? '<span class="tg" style="background:rgba(212,156,15,.12);color:var(--y);font-size:9px">🌟</span>' : '';

  // 精选区只显示查看按钮，不显示删除/转换（fixedWidth 仅控制按钮差异，不再控制宽度）
  const actionBtns = fixedWidth ? '' : (() => {
    let btns = '';
    if (s.scope !== 'official') {
      btns = `<button class="bt bt-o bt-s" style="color:var(--r);border-color:var(--r)" onclick="event.stopPropagation();deleteSkill('${s.id}','${s.scope}')">${I18N.t('skills.action.delete')||'删除'}</button>`;
      if (s.scope === 'user' && _currentRepo()) {
        btns += `<button class="bt bt-o bt-s" onclick="event.stopPropagation();convertSkill('${s.name}')">${I18N.t('skills.action.convert_to_repo')||'转换'}</button>`;
      }
    }
    return btns;
  })();

  // 华为云技能包特殊处理
  if (s.name === 'huawei-cloud') {
    return `<div class="rc" onclick="app.nav('skill','${s.id}')">
      <div class="rh">
        <span style="font-size:20px">📁</span>
        <span class="tg ${tagClass}" style="font-size:9px">${meta.label||at}</span>
        <span class="rn">${s.display_name || s.name}</span>
      </div>
      <div class="rd">${s.description || ''}</div>
      <div class="rf">
        <span class="s">${I18N.t('skills.action.view')||'查看'} →</span>
      </div>
    </div>`;
  }

  return `<div class="rc skill-card" data-name="${s.name}" data-agent="${at}" onclick="app.nav('skill','${s.id}')">
    <div class="rh">
      <span style="font-size:14px">${meta.icon||''}</span>
      <span class="tg ${tagClass}" style="font-size:9px">${meta.label||at}</span>
      <span class="rn">${s.display_name || s.name}</span>
      ${featuredBadge}
    </div>
    <div class="rd" style="margin-bottom:8px" title="${(s.description||'').replace(/"/g,'&quot;')}">${s.description || ''}</div>
    ${scenesStr ? `<div class="ro" style="color:var(--a)" title="${scenesTitle.replace(/"/g,'&quot;')}">${I18N.t('skills.info_scenes')||'所属场景'}：${scenesStr}</div>` : ''}
    ${sceneCount > 0 || (s.repo_count||0) > 0 ? `<div class="ro">${sceneCount} ${I18N.t('skills.scenes_in_use_suffix')||'场景在用'}，${s.repo_count||0} ${I18N.t('skills.repos_in_use')||'仓库在用'}</div>` : ''}
    <div class="rf">
      ${isUserSkill ? '<span class="s"></span>' : `<span class="s" style="cursor:pointer" onclick="event.stopPropagation();toggleSkillLike(${s.id},this)">${likeIcon} ${likeCount}</span>`}
      <div class="sk-card-btns">
        <button class="bt bt-o bt-s" onclick="event.stopPropagation();app.nav('skill','${s.id}')">${I18N.t('skills.action.view')||'详情'}</button>
        ${actionBtns}
      </div>
    </div>
  </div>`;
}

// ── 点赞 toggle ──
function toggleSkillLike(skillId, el) {
  API.post('/skills/' + skillId + '/like', {}).then(r => {
    const icon = r.liked ? '❤️' : '🤍';
    const count = r.like_count || 0;
    // 更新顶部按钮
    if (el) {
      el.innerHTML = icon + ' <span id="detailLikeCount">' + count + '</span>';
    }
    // 同步右侧信息栏点赞
    const infoEl = document.getElementById('detailInfoLikes');
    if (infoEl) {
      infoEl.textContent = icon + ' ' + count;
    }
    // 更新缓存
    const s = _allSkills.find(x => x.id == skillId);
    if (s) { s.liked_by_me = r.liked; s.like_count = r.like_count; }
  }).catch(e => {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.like_failed')||'操作失败');
  });
}

// ══════════════════════════════════════════
// 技能详情页
// ══════════════════════════════════════════
function renderSkillDetail(data) {
  const el = _skillsContainer();
  if (!el) return;

  // 华为云技能包 → 展示子技能卡片页面
  if (data.name === 'huawei-cloud') {
    renderHuaweiCloudPackage(el, data);
    return;
  }

  const at = data.agent_type || '';
  const meta = _AGENT_META[at] || {};
  const grad = _FEAT_GRADIENT[at] || {g1:'#3168e0', g2:'#2556c2'};
  const isUserDetail = data.scope === 'user';
  const scenesArr = (data.scenes || []).map(sc => sc.display_name || sc.name || sc);
  const scenesStr = scenesArr.length > 0 ? scenesArr.join('、') : '—';
  const tagsStr = data.sub_scene || '—';
  const likeCount = data.like_count || 0;
  const likedIcon = data.liked_by_me ? '❤️' : '🤍';
  const rawContent = data.content || '';
  const content = rawContent.replace(/`/g, '\\`').replace(/\${/g, '\\${');
  const versionMatch = rawContent.match(/^---[\s\S]*?^version:\s*(.+?)$/m);
  const version = versionMatch ? versionMatch[1].trim() : '—';
  const descMatch = rawContent.match(/^---[\s\S]*?^description:\s*\|?\s*\n([\s\S]*?)^\w/m);
  const fnDesc = descMatch ? descMatch[1].trim() : (data.description || '—');

  el.innerHTML = `
    <button class="det-back" onclick="_backFromSkillDetail()">← ${I18N.t('common.back')||'返回'}</button>
    <div class="det">
      <div class="det-head">
        <span class="det-ico" style="--fg1:${grad.g1};--fg2:${grad.g2}">${meta.icon || '◈'}</span>
        <h1 class="det-name">${data.display_name || data.name}</h1>
        <span class="agent-badge">${meta.label || at}</span>
        <div class="det-actions">
          ${isUserDetail ? '' : `<button class="det-like ${data.liked_by_me?'liked':''}" onclick="toggleSkillLike(${data.id},this)">${likedIcon} <span id="detailLikeCount">${likeCount}</span></button>`}
          ${(data.is_obs || data.zip_url) ? `<button class="det-dl" onclick="downloadSkillZip('${data.id}')">${I18N.t('skills.download_skill')||'下载技能包'}</button>` : ''}
        </div>
      </div>
      <p class="det-sum">${data.description || ''}</p>
      <div class="fn-card">
        <div class="fn-label">${I18N.t('skills.info_function')||'功能说明'}</div>
        <div class="fn-body">${fnDesc}</div>
      </div>
      <div class="info-grid">
        <div class="info-cell"><div class="il">${I18N.t('skills.info_scenes')||'场景'}</div><div class="iv">${scenesStr}</div></div>
        <div class="info-cell"><div class="il">${I18N.t('skills.info_tags')||'子场景'}</div><div class="iv">${tagsStr}</div></div>
        <div class="info-cell"><div class="il">${I18N.t('skills.info_version')||'版本'}</div><div class="iv">${version}</div></div>
      </div>
    </div>`;
}

// ══════════════════════════════════════════
// 上传技能
// ══════════════════════════════════════════
function showSkillUpload() {
  const repo = _currentRepo();
  const scope = _skillTab;
  const AGENT_OPTS = [
    {v:'vod', label:'VOD Agent'}, {v:'issue', label:'Issue Agent'},
    {v:'coding', label:'Coding Agent'}, {v:'qa', label:'QA Agent'},
    {v:'sre', label:'SRE Agent'}, {v:'chat', label:'Chat Agent'},
  ];
  const modal = document.createElement('div');
  modal.id = 'skillUploadModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
  modal.innerHTML = `<div class="cd" style="width:480px;padding:20px;border-radius:10px">
    <h3 style="font-size:14px;font-weight:600;margin-bottom:14px">${I18N.t('skills.upload.title')||'上传技能'}</h3>
    <div class="srow" style="margin-bottom:10px"><label>${I18N.t('skills.upload.type')||'类型'}</label><span>${scope==='repo'?'仓库技能':'个人技能'}</span></div>
    ${scope==='repo'?`<div class="srow" style="margin-bottom:10px"><label>${I18N.t('skills.upload.repo')||'仓库'}</label><span>${repo}</span></div>`:''}
    <div class="srow" style="margin-bottom:10px"><label>${I18N.t('skills.upload.agent_type')||'Agent 类型'}</label>
      <select id="skillAgentType" style="flex:1;padding:7px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c);color:var(--t);outline:none">
        <option value="">${I18N.t('skills.upload.select_agent')||'请选择'}</option>
        ${AGENT_OPTS.map(o=>`<option value="${o.v}">${o.label}</option>`).join('')}
      </select>
    </div>
    <div style="margin-bottom:14px">
      <label style="font-size:11px;color:var(--t2);display:block;margin-bottom:6px">${I18N.t('skills.upload.file_label')||'技能 ZIP 包'}</label>
      <input type="file" id="skillFile" accept=".zip" style="font-size:12px">
    </div>
    <div class="srow" style="margin-bottom:12px"><label></label>
      <span style="font-size:11px;color:var(--t2)">${I18N.t('skills.upload.format_req')||'ZIP 内含 SKILL.md'} <a href="javascript:void(0)" onclick="downloadSkillTemplate()" style="color:var(--a);cursor:pointer">${I18N.t('skills.action.download_template')||'下载模板'}</a></span>
    </div>
    <div id="skillUploadProgress" style="display:none;margin-bottom:12px">
      <div style="font-size:11px;color:var(--t2);margin-bottom:4px">${I18N.t('skills.upload.uploading')||'上传中'} <span id="skillUploadPct">0%</span></div>
      <div style="background:var(--bg);border-radius:8px;height:8px;overflow:hidden"><div id="skillUploadBar" style="background:var(--a);height:100%;width:0%;transition:width .2s"></div></div>
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="bt bt-o bt-s" onclick="document.getElementById('skillUploadModal').remove()">${I18N.t('skills.action.cancel')||'取消'}</button>
      <button class="bt bt-p bt-s" onclick="doSkillUpload()">${I18N.t('skills.action.upload')||'上传'}</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
}

function downloadSkillTemplate() {
  API.get('/skills/template?token=' + encodeURIComponent(localStorage.getItem('token')||'')).then(r => {
    if (r && r.url) { window.open(r.url, '_blank'); return; }
    window.open('/api/skills/template?token=' + encodeURIComponent(localStorage.getItem('token')||''), '_blank');
  }).catch(() => {
    window.open('/api/skills/template?token=' + encodeURIComponent(localStorage.getItem('token')||''), '_blank');
  });
}

function _setSkillUploadProgress(pct, done) {
  const prog = document.getElementById('skillUploadProgress');
  const bar = document.getElementById('skillUploadBar');
  const pctEl = document.getElementById('skillUploadPct');
  if (!prog || !bar || !pctEl) return;
  prog.style.display = done ? 'none' : 'block';
  bar.style.width = pct + '%';
  pctEl.textContent = pct + '%';
}

function doSkillUpload() {
  const fileInput = document.getElementById('skillFile');
  if (!fileInput || !fileInput.files || !fileInput.files[0]) {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.select_zip')||'请选择 ZIP 文件');
    return;
  }
  const agentType = document.getElementById('skillAgentType')?.value || '';
  if (!agentType) {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.select_agent_type')||'请选择 Agent 类型');
    return;
  }
  const scope = _skillTab;
  const repo = _currentRepo();
  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  fd.append('scope', scope);
  fd.append('agent_type', agentType);
  if (scope === 'repo' && repo) fd.append('repo', repo);
  _setSkillUploadProgress(0, false);
  API.upload('/skills/upload', fd, (pct) => _setSkillUploadProgress(pct, false)).then(r => {
    _setSkillUploadProgress(100, true);
    document.getElementById('skillUploadModal')?.remove();
    if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.upload_success')||'上传成功');
    renderSkills();
  }).catch(e => {
    _setSkillUploadProgress(0, true);
    // 优先展示后端返回的具体校验错误（errors 数组）
    let msg = e.message || (I18N.t('skills.toast.upload_failed')||'上传失败');
    if (e.errors && Array.isArray(e.errors) && e.errors.length > 0) {
      msg = e.errors.join('；');
    }
    if (app && app.showToast) app.showToast(msg);
  });
}

function deleteSkill(skillId, scope) {
  // 移除已有弹窗
  var existing = document.getElementById('deleteSkillModal');
  if (existing) { existing.remove(); return; }

  var modal = document.createElement('div');
  modal.id = 'deleteSkillModal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center';

  modal.innerHTML = `
    <div style="background:rgba(255,255,255,0.98);backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,0.15);width:360px;max-width:90vw;overflow:hidden">
      <div style="padding:24px 24px 20px;text-align:center">
        <div style="width:48px;height:48px;border-radius:50%;background:rgba(255,59,48,0.1);display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:22px">🗑</div>
        <h3 style="font-size:16px;font-weight:600;color:#1d1d1f;margin-bottom:6px">${I18N.t('skills.confirm.delete_skill',{name:''})||'确认删除此技能？'}</h3>
        <div style="font-size:12px;color:#86868b">${I18N.t('skills.delete_skill_tip')||'删除后不可恢复，技能数据将被永久移除'}</div>
      </div>
      <div style="padding:0 24px 20px;display:flex;gap:10px">
        <button onclick="document.getElementById('deleteSkillModal').remove()" style="flex:1;padding:10px 16px;font-size:13px;border:1px solid #d1d1d6;border-radius:10px;background:transparent;color:#1d1d1f;cursor:pointer">${I18N.t('skills.action.cancel')||'取消'}</button>
        <button id="deleteSkillConfirmBtn" style="flex:1;padding:10px 16px;font-size:13px;border:none;border-radius:10px;background:#ff3b30;color:#fff;cursor:pointer">${I18N.t('skills.confirm_delete')||'确认删除'}</button>
      </div>
    </div>
  `;

  document.body.appendChild(modal);
  modal.onclick = function(e) { if (e.target === modal) modal.remove(); };

  document.getElementById('deleteSkillConfirmBtn').onclick = function() {
    var btn = this;
    btn.disabled = true;
    btn.textContent = I18N.t('skills.deleting') || '删除中...';
    API.del('/skills/by-id/' + encodeURIComponent(skillId)).then(r => {
      modal.remove();
      if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.deleted')||'已删除');
      renderSkills();
    }).catch(e => {
      btn.disabled = false;
      btn.textContent = I18N.t('skills.confirm_delete')||'确认删除';
      if (app && app.showToast) app.showToast(I18N.t('skills.toast.delete_failed',{msg:e.message})||'删除失败');
    });
  };
}

function convertSkill(skillName) {
  const repo = _currentRepo();
  if (!repo) {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.select_repo_first')||'请先选择仓库');
    return;
  }
  // 移除已有弹窗
  var existing = document.getElementById('convertSkillModal');
  if (existing) { existing.remove(); return; }

  var modal = document.createElement('div');
  modal.id = 'convertSkillModal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center';

  modal.innerHTML = `
    <div style="background:rgba(255,255,255,0.98);backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,0.15);width:360px;max-width:90vw;overflow:hidden">
      <div style="padding:24px 24px 20px;text-align:center">
        <div style="width:48px;height:48px;border-radius:50%;background:rgba(0,113,227,0.1);display:flex;align-items:center;justify-content:center;margin:0 auto 12px;font-size:22px">🔄</div>
        <h3 style="font-size:16px;font-weight:600;color:#1d1d1f;margin-bottom:6px">${I18N.t('skills.confirm.convert_skill',{name:skillName,repo:repo})||'确认转换？'}</h3>
        <div style="font-size:12px;color:#86868b">${I18N.t('skills.convert_skill_tip')||'将个人技能转换为此仓库的仓库技能'}</div>
      </div>
      <div style="padding:0 24px 20px;display:flex;gap:10px">
        <button onclick="document.getElementById('convertSkillModal').remove()" style="flex:1;padding:10px 16px;font-size:13px;border:1px solid #d1d1d6;border-radius:10px;background:transparent;color:#1d1d1f;cursor:pointer">${I18N.t('skills.action.cancel')||'取消'}</button>
        <button id="convertSkillConfirmBtn" style="flex:1;padding:10px 16px;font-size:13px;border:none;border-radius:10px;background:#0071e3;color:#fff;cursor:pointer">${I18N.t('skills.confirm_convert')||'确认转换'}</button>
      </div>
    </div>
  `;

  document.body.appendChild(modal);
  modal.onclick = function(e) { if (e.target === modal) modal.remove(); };

  document.getElementById('convertSkillConfirmBtn').onclick = function() {
    var btn = this;
    btn.disabled = true;
    btn.textContent = I18N.t('skills.converting') || '转换中...';
    API.post('/skills/convert', {skill_name: skillName, repo}).then(r => {
      modal.remove();
      if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.convert_success')||'转换成功');
      renderSkills();
    }).catch(e => {
      btn.disabled = false;
      btn.textContent = I18N.t('skills.confirm_convert')||'确认转换';
      if (app && app.showToast) app.showToast(I18N.t('skills.toast.convert_failed',{msg:e.message})||'转换失败');
    });
  };
}

function downloadSkillZip(id) {
  const token = localStorage.getItem('token') || '';
  fetch('/api/skills/by-id/' + encodeURIComponent(id) + '/download', {headers: {'Authorization': 'Bearer ' + token}})
    .then(r => {
      const ct = r.headers.get('content-type') || '';
      if (ct.includes('application/json')) {
        return r.json().then(d => { if (d && d.url) { window.open(d.url, '_blank'); return; } throw new Error('no url'); });
      }
      return r.blob().then(b => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(b); a.download = 'skill-' + id + '.zip';
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      });
    }).catch(() => { if (app && app.showToast) app.showToast(I18N.t('skills.toast.download_failed')||'下载失败'); });
}

// ══════════════════════════════════════════
// 华为云技能包：子技能页面
// ══════════════════════════════════════════
function renderHuaweiCloudPackage(el, data) {
  el.innerHTML = `
    <button class="bt bt-o bt-s" onclick="_backFromSkillDetail()" style="margin-bottom:12px">← ${I18N.t('skills.huawei.back')||'返回'}</button>
    <div class="cd" style="padding:16px 20px;margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:28px">📁</span>
        <div><h3 style="font-size:16px;font-weight:600">${data.display_name||data.name}</h3><div style="font-size:12px;color:var(--t2);margin-top:4px">${data.description||''}</div></div>
        <button class="bt bt-o bt-s" onclick="togglePkgSkillMd(${data.id})" style="margin-left:auto">${I18N.t('skills.action.view_skill_md')||'查看 SKILL.md'}</button>
      </div>
    </div>
    <div id="subSkillsGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px"><div style="padding:20px;text-align:center;color:var(--t3)">${I18N.t('skills.huawei.loading_sub')||'加载中...'}</div></div>`;
  API.get('/skills/' + data.id + '/sub').then(d => {
    var subs = d.sub_skills || [];
    var grid = document.getElementById('subSkillsGrid');
    if (!grid) return;
    if (!subs.length) { grid.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3)">' + (I18N.t('skills.huawei.no_sub_skill')||'暂无子技能') + '</div>'; return; }
    var html = '';
    subs.forEach(function(sub) {
      html += '<div class="cd" style="padding:14px;cursor:pointer" onclick="loadSubSkillDetail('+data.id+',\''+sub.name+'\')">'
        + '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px"><span style="font-size:16px">📄</span><span style="font-size:13px;font-weight:600">'+(sub.display_name||sub.name)+'</span></div>'
        + '<div style="font-size:10px;color:var(--t3);margin-bottom:6px">'+sub.name+'</div>'
        + '<div style="font-size:11px;color:var(--t2);line-height:1.5;flex:1">'+(sub.description||'').substring(0,200)+'</div>'
        + '</div>';
    });
    grid.innerHTML = html;
  }).catch(function() {
    var grid = document.getElementById('subSkillsGrid');
    if (grid) grid.innerHTML = '<div style="padding:20px;text-align:center;color:var(--r)">'+(I18N.t('common.load_failed')||'加载失败')+'</div>';
  });
}

function loadSubSkillDetail(pkgId, subName) {
  var el = _skillsContainer();
  if (!el) return;
  el.innerHTML = '<div class="loading">'+(I18N.t('skills.loading')||'加载中...')+'</div>';
  API.get('/skills/package-sub/'+pkgId+'/'+subName).then(function(data) {
    if (window._currentView !== 'skill' && window._currentView !== 'skills') return;  // stale guard
    var safeContent = (data.content||'').replace(/`/g,'\\`').replace(/\${/g,'\\${');
    el.innerHTML = `
      <button class="bt bt-o bt-s" onclick="_backFromSubSkill(${pkgId})" style="margin-bottom:12px">← ${I18N.t('skills.huawei.back')||'返回'}</button>
      <div style="display:grid;grid-template-columns:1fr 300px;gap:14px;min-height:500px">
        <div class="cd" style="padding:16px 20px;overflow-y:auto">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px"><span style="font-size:22px">📄</span><h3 style="font-size:15px;font-weight:600">${data.display_name||data.name}</h3></div>
          <div style="font-size:12px;color:var(--t2);margin-bottom:14px;line-height:1.5">${data.description||''}</div>
          <h3 style="font-size:13px;font-weight:600;margin-bottom:6px">SKILL.md</h3>
          <pre style="background:var(--bg);padding:14px;border-radius:var(--rd);font-size:11px;font-family:monospace;line-height:1.6;overflow-x:auto;white-space:pre-wrap">${safeContent||(I18N.t('skills.huawei.no_detail')||'无详情')}</pre>
        </div>
        <div class="cd" style="padding:16px 20px;font-size:12px">
          <h3 style="font-size:13px;font-weight:600;margin-bottom:10px">${I18N.t('skills.info_title')||'信息'}</h3>
          <div class="srow"><label>${I18N.t('skills.info_name')||'名称'}</label><span>${data.name}</span></div>
          <div class="srow"><label>${I18N.t('skills.info_description')||'描述'}</label><span class="t2">${data.description||'—'}</span></div>
        </div>
      </div>`;
  }).catch(function(e) {
    if (e.name === 'AbortError' && e.silent) return;  // Issue #63: abort 静默
    if (window._currentView !== 'skills') return;  // stale guard
    el.innerHTML = '<div class="error">'+(I18N.t('common.load_failed')||'加载失败')+': '+(e.message||'')+'</div>';
  });
}

function togglePkgSkillMd(skillId) {
  API.get('/skills/by-id/' + skillId).then(function(data) {
    var content = (data.content||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    closeSubSkillModal();
    var ov = document.createElement('div');
    ov.id = 'subSkillModal';
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center;';
    ov.innerHTML = '<div style="width:min(860px,92vw);height:min(700px,88vh);background:var(--c);border:1px solid var(--b);border-radius:10px;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,.5)">'
      + '<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--b);flex-shrink:0"><span style="font-size:13px;font-weight:600">📄 SKILL.md</span><button onclick="closeSubSkillModal()" style="margin-left:auto;border:1px solid var(--b);background:transparent;color:var(--t2);border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer">✕</button></div>'
      + '<div style="flex:1;min-height:0;overflow-y:auto;padding:14px;font-size:13px;line-height:1.7;white-space:pre-wrap;font-family:monospace;color:var(--t)">'+content+'</div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function(e) { if (e.target===ov) closeSubSkillModal(); });
  }).catch(function() {});
}

function closeSubSkillModal() {
  var ov = document.getElementById('subSkillModal');
  if (ov) ov.remove();
}
