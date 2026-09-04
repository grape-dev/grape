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
let _skillTotal = 0;
let _allSkills = [];         // 全部技能缓存
let _featuredSkills = [];    // 精选技能（独立加载，不受搜索影响）
let _agentGroups = {};       // {agent_type: [skills]}
let _agentList = [];         // ['vod','issue','coding','qa','sre','chat']
let _activeAgentIdx = -1;    // -1=全部, 0+=第N个agent

// _AGENT_META 定义在 scene-market.js 中，这里直接复用

const _SCOPE_NAMES = {
  official: '技能市场',
  repo: '仓库技能',
  user: '个人技能',
};

function _skillsContainer() {
  return document.getElementById('marketTabContent') || document.getElementById('pageContent');
}

// 技能详情返回：根据来源决定返回目标（不 push history，避免历史栈膨胀）
function _backFromSkillDetail() {
  const from = window._skillDetailFrom;
  if (!from || !from.view) {
    app.nav('scene-market');
    return;
  }
  if (from.view === 'scene-detail' && from.param) {
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

// 从技能详情返回技能列表（不重新加载页面，直接渲染技能 tab）
function _backToSkillList() {
  const pc = document.getElementById('pageContent');
  if (!pc) return;
  // 重建 tab 栏 + 容器
  pc.innerHTML = `<div class="cd" style="margin-bottom:16px;padding:12px 16px;font-size:12px;color:var(--t2);line-height:1.7;display:flex;align-items:flex-start;gap:10px">
    <span style="font-size:16px;flex-shrink:0">💡</span>
    <div><b style="color:var(--a)">${I18N.t('skills.scenes_tab')||'场景'}</b> ${I18N.t('skills.market_intro_scene')||'是多个技能按开发流程组合的开发模式（如 VOD→Issue→Coding→QA→SRE），'}<b style="color:var(--a)">${I18N.t('skills.skills_tab')||'技能'}</b> ${I18N.t('skills.market_intro_skill')||'是单个 Agent 的能力包，完成具体动作。点击场景卡片查看完整流程，点击技能卡片查看能力详情。'}</div>
  </div>
  <div class="stabs" style="margin-bottom:16px">
    <a onclick="_renderMarketTab('scenes')">🏪 ${I18N.t('skills.scenes_tab')||'场景'}</a>
    <a class="ac" onclick="_renderMarketTab('skills')">⚙ ${I18N.t('skills.skills_tab')||'技能'}</a>
  </div><div id="marketTabContent"></div>`;
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
  const el = _skillsContainer();
  if (!el) return;

  // 预填搜索词（从场景详情跳转过来）
  let searchInit = '';
  if (window._skillMarketSearchInit) {
    searchInit = window._skillMarketSearchInit;
    window._skillMarketSearchInit = null;
  }

  // official 页签：并行加载精选技能（独立请求，最多5条）+ 全部技能
  if (_skillTab === 'official') {
    Promise.all([
      API.post('/skills/sync-official', {}).catch(() => ({})),
      API.get('/skills/featured')
    ]).then(([_, featData]) => {
      _featuredSkills = featData.skills || [];
      _loadSkills(searchInit);
    }).catch(() => {
      _featuredSkills = [];
      _loadSkills(searchInit);
    });
  } else {
    _loadSkills(searchInit);
  }
}

function _loadSkills(searchInit) {
  const el = _skillsContainer();
  if (!el) return;
  _reloadSkills(searchInit);
}

// 从后端加载技能（带 scope + 搜索词 + 排序 + 分页）
function _reloadSkills(searchInit) {
  const el = _skillsContainer();
  if (!el) return;
  const repo = _currentRepo();
  const params = [];
  if (repo) params.push('repo=' + encodeURIComponent(repo));
  if (_skillTab) params.push('scope=' + _skillTab);
  if (_skillSort) params.push('sort=' + _skillSort);
  if (_skillSearchQ) params.push('q=' + encodeURIComponent(_skillSearchQ));
  params.push('page=' + _skillPage);
  params.push('page_size=' + _skillPageSize);
  const qs = params.length ? '?' + params.join('&') : '';
  API.get('/skills' + qs).then(d => {
    _allSkills = d.skills || [];
    _skillTotal = d.total || _allSkills.length;
    _renderSkillPage();
    if (searchInit) {
      setTimeout(() => {
        const input = document.getElementById('skillSearchInput');
        if (input) { input.value = searchInit; _skillSearchQ = searchInit; _skillPage = 1; _reloadSkills(); }
      }, 100);
    }
  }).catch(e => {
    el.innerHTML = '<div class="error">' + (I18N.t('common.load_failed')||'加载失败') + ': ' + (e.message||'') + '</div>';
  });
}

function _renderSkillPage() {
  const el = _skillsContainer();
  if (!el) return;

  let html = '';

  // 二级子页签
  html += `<div class="sub-tabs">
    <div class="sub-tab ${_skillTab==='official'?'act':''}" onclick="_switchSkillTab('official')">${_SCOPE_NAMES.official}</div>
    <div class="sub-tab ${_skillTab==='repo'?'act':''}" onclick="_switchSkillTab('repo')">${_SCOPE_NAMES.repo}</div>
    <div class="sub-tab ${_skillTab==='user'?'act':''}" onclick="_switchSkillTab('user')">${_SCOPE_NAMES.user}</div>
  </div>`;

  // 精选技能（仅 official，独立加载，不受搜索影响）
  if (_skillTab === 'official' && _featuredSkills.length > 0) {
    html += `<div id="featured-skills-section" style="margin-bottom:20px">
      <div class="cd-h" style="margin-bottom:10px"><h2>🌟 ${I18N.t('skills.featured_skills')||'精选技能'}</h2></div>
      <div class="scn-scroll-wrap">
        <div id="featured-skills" class="scn-scroll-track">
          ${_featuredSkills.map(s => _renderSkillCard(s, true)).join('')}
        </div>
        <button class="scn-scroll-btn scn-scroll-prev" onclick="document.getElementById('featured-skills').scrollBy({left:-314,behavior:'smooth'})">◀</button>
        <button class="scn-scroll-btn scn-scroll-next" onclick="document.getElementById('featured-skills').scrollBy({left:314,behavior:'smooth'})">▶</button>
      </div>
    </div>`;
  }

  // 全部技能 — 标题行 + 搜索 + 查找 + 排序 + 上传
  const scoped = _allSkills.filter(s => s.scope === _skillTab);
  html += `<div class="cd-h" style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
    <h2>${I18N.t('skills.all_skills')||'全部技能'} <span style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:2px 8px;border-radius:8px;margin-left:4px">${_skillTotal}</span></h2>
    <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">
      <input id="skillSearchInput" placeholder="${I18N.t('skills.search_placeholder')||'搜索技能或描述任务...'}" value="${_skillSearchQ}" onkeyup="if(event.key==='Enter'){_skillSearchQ=this.value;_skillPage=1;_reloadSkills()}" style="padding:7px 12px;border:1px solid var(--b);border-radius:var(--rd);font-size:12px;outline:none;background:var(--c);color:var(--t);width:200px">
      <button class="bt bt-o bt-s" onclick="_skillSearchQ=document.getElementById('skillSearchInput').value;_skillPage=1;_reloadSkills()">${I18N.t('skills.find_skill')||'查找技能'}</button>
      <button class="bt ${_skillSort==='latest'?'bt-p':'bt-o'} bt-s" id="sort-latest" onclick="_switchSort('latest')">${I18N.t('skills.sort_latest')||'最新'}</button>
      <button class="bt ${_skillSort==='hot'?'bt-p':'bt-o'} bt-s" id="sort-hot" onclick="_switchSort('hot')">${I18N.t('skills.sort_hot')||'最热'}</button>`;
  if (_skillTab === 'repo' || _skillTab === 'user') {
    html += `<button class="bt bt-p bt-s" onclick="showSkillUpload()" style="margin-left:8px;color:#fff;background:var(--g);border-color:var(--g)">⬆ ${I18N.t('skills.action.add_skill')||'上传技能'}</button>`;
  }
  html += `</div></div>`;

  // 仓库技能需当前仓库
  if (_skillTab === 'repo' && !_currentRepo()) {
    html += `<div class="ts t2" style="padding:12px">${I18N.t('skills.empty.no_repo_selected')||'请先选择仓库'}</div>`;
    el.innerHTML = html;
    return;
  }

  // Agent tab（带图标和计数）
  _buildAgentGroups(scoped);
  if (_agentList.length === 0) {
    html += `<div class="ts t3" style="padding:20px;text-align:center">${I18N.t('skills.empty.no_skills')||'此范围暂无技能'}</div>`;
    el.innerHTML = html;
    return;
  }

  html += `<div class="stabs" id="agent-tabs" style="margin-top:10px">`;
  // "全部" tab
  html += `<a class="ac" onclick="_switchAgentTab(-1)">全部 <span class="c" style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:1px 6px;border-radius:8px;margin-left:2px">${scoped.length}</span></a>`;
  _agentList.forEach((at, i) => {
    const meta = _AGENT_META[at] || {};
    const count = (_agentGroups[at] || []).length;
    const countBadge = count > 0 ? ` <span class="c" style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:1px 6px;border-radius:8px;margin-left:2px">${count}</span>` : '';
    html += `<a onclick="_switchAgentTab(${i})">${meta.icon?`<span style="margin-right:4px">${meta.icon}</span>`:''}${(meta.label||at)+' Agent'}${countBadge}</a>`;
  });
  html += `</div>`;

  // 技能列表
  html += `<div id="all-skills"></div>`;

  // 分页器（始终显示，只有1页时按钮disabled但选择器可用）
  const totalPages = Math.max(1, Math.ceil(_skillTotal / _skillPageSize));
  const prevDisabled = _skillPage <= 1 ? ' disabled' : '';
  const nextDisabled = _skillPage >= totalPages ? ' disabled' : '';
  html += `<div style="display:flex;align-items:center;justify-content:center;gap:12px;margin-top:16px;padding:12px 0">
    <button class="bt bt-o bt-s${prevDisabled}" ${prevDisabled ? 'disabled' : ''} onclick="_skillPage=${Math.max(1,_skillPage-1)};_reloadSkills()">‹</button>
    <span style="font-size:12px;color:var(--t2)">${_skillPage} / ${totalPages} (${_skillTotal}条)</span>
    <button class="bt bt-o bt-s${nextDisabled}" ${nextDisabled ? 'disabled' : ''} onclick="_skillPage=${Math.min(totalPages,_skillPage+1)};_reloadSkills()">›</button>
    <select onchange="_skillPageSize=parseInt(this.value);_skillPage=1;_reloadSkills()" style="padding:4px 8px;border:1px solid var(--b);border-radius:var(--rd);font-size:11px;background:var(--c);color:var(--t);outline:none">
      <option value="20" ${_skillPageSize===20?'selected':''}>20/页</option>
      <option value="50" ${_skillPageSize===50?'selected':''}>50/页</option>
      <option value="100" ${_skillPageSize===100?'selected':''}>100/页</option>
    </select>
  </div>`;

  el.innerHTML = html;

  // 渲染全部技能
  _activeAgentIdx = -1;
  _renderAgentSkills();
}

function _buildAgentGroups(scoped) {
  _agentGroups = {};
  _agentList = [];
  const order = ['vod','issue','coding','qa','sre','chat'];
  scoped.forEach(s => {
    const at = s.agent_type || '__other__';
    if (!_agentGroups[at]) _agentGroups[at] = [];
    _agentGroups[at].push(s);
  });
  // 固定展示全部 6 个 agent tab，没有技能的也显示（count=0 时不显示数字）
  _agentList = order;
}

function _switchSkillTab(tab) {
  _skillTab = tab;
  _skillPage = 1;
  renderSkills();
}

function _switchAgentTab(idx) {
  _activeAgentIdx = idx;
  _renderAgentSkills();
  // 更新 tab 高亮
  document.querySelectorAll('#agent-tabs a').forEach((a, i) => {
    a.classList.toggle('ac', i === idx + 1);
  });
}

function _switchSort(mode) {
  _skillSort = mode;
  _skillPage = 1;
  _reloadSkills();
}

function _renderAgentSkills() {
  const container = document.getElementById('all-skills');
  if (!container) return;
  let skills;
  if (_activeAgentIdx === -1) {
    skills = _allSkills.filter(s => s.scope === _skillTab);
  } else {
    const at = _agentList[_activeAgentIdx];
    skills = (_agentGroups[at] || []).filter(s => s.scope === _skillTab);
  }
  if (skills.length === 0) {
    container.innerHTML = `<div class="ts t3" style="padding:20px;text-align:center">${I18N.t('skills.empty.no_skills')||'暂无技能'}</div>`;
    return;
  }
  container.innerHTML = `<div class="rgrid">${skills.map(s => _renderSkillCard(s)).join('')}</div>`;
}

// ── 渲染技能卡片（精选和列表共用）──
function _renderSkillCard(s, fixedWidth) {
  const at = s.agent_type || '';
  const meta = _AGENT_META[at] || {};
  const tagClass = s.scope === 'official' ? 'tg-c' : s.scope === 'repo' ? 'tg-i' : 'tg-d';
  const scenesArr = (s.scenes || []).map(sc => sc.display_name || sc.name || sc);
  const scenesStr = scenesArr.length ? scenesArr.join(' · ') : '';
  const likeIcon = s.liked_by_me ? '❤️' : '🤍';
  const likeCount = s.like_count || 0;
  const featuredBadge = s.is_featured ? '<span class="tg" style="background:rgba(212,156,15,.12);color:var(--y);font-size:9px">🌟</span>' : '';
  const widthStyle = fixedWidth ? 'style="flex-shrink:0;width:300px"' : '';

  // 精选区只显示查看按钮，不显示删除/转换
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
    return `<div class="rc" ${widthStyle} onclick="app.nav('skill','${s.id}')">
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

  return `<div class="rc skill-card" ${widthStyle} data-name="${s.name}" data-agent="${at}" onclick="app.nav('skill','${s.id}')">
    <div class="rh">
      <span style="font-size:14px">${meta.icon||''}</span>
      <span class="tg ${tagClass}" style="font-size:9px">${meta.label||at}</span>
      <span class="rn">${s.display_name || s.name}</span>
      ${featuredBadge}
    </div>
    <div class="rd" style="margin-bottom:8px">${s.description || ''}</div>
    ${scenesStr ? `<div class="ro" style="color:var(--a)">${I18N.t('skills.info_scenes')||'所属场景'}：${scenesStr}</div>` : ''}
    <div class="ro">${s.scene_count||0} ${I18N.t('skills.scenes_in_use_suffix')||'场景在用'}，${s.repo_count||0} ${I18N.t('skills.repos_in_use')||'仓库在用'}</div>
    <div class="rf">
      <span class="s" style="cursor:pointer" onclick="event.stopPropagation();toggleSkillLike(${s.id},this)">${likeIcon} ${likeCount}</span>
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
    if (el) {
      el.innerHTML = (r.liked ? '❤️' : '🤍') + ' ' + (r.like_count || 0);
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
  const tagClass = data.scope === 'official' ? 'tg-c' : data.scope === 'repo' ? 'tg-i' : 'tg-d';
  const featuredBadge = data.is_featured ? `<span class="tg" style="background:rgba(212,156,15,.12);color:var(--y);font-size:9px">🌟 ${I18N.t('skills.featured')||'精选'}</span>` : '';
  const scenesArr = (data.scenes || []).map(sc => sc.display_name || sc.name || sc);
  const scenesHtml = scenesArr.length ? scenesArr.map(n => `<span class="tg" style="font-size:10px;margin:2px">${n}</span>`).join('') : '—';
  const likeCount = data.like_count || 0;
  const likedIcon = data.liked_by_me ? '❤️' : '🤍';
  const content = (data.content || '').replace(/`/g, '\\`').replace(/\${/g, '\\${');

  el.innerHTML = `
    <button class="bt bt-o bt-s" onclick="_backFromSkillDetail()" style="margin-bottom:12px">← ${I18N.t('common.back')||'返回'}</button>
    <div style="display:grid;grid-template-columns:1fr 300px;gap:14px;min-height:500px">
      <div class="cd" style="padding:16px 20px;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px">
          <div style="display:flex;align-items:center;gap:8px;min-width:0">
            <span style="font-size:22px">${meta.icon||'◈'}</span>
            <div>
              <div style="display:flex;align-items:center;gap:6px">
                <h3 style="font-size:15px;font-weight:600">${data.display_name || data.name}</h3>
                ${featuredBadge}
              </div>
              <span class="tg ${tagClass}" style="font-size:9px">${(meta.label||at)+' Agent'} · ${_SCOPE_NAMES[data.scope]||data.scope}</span>
            </div>
          </div>
          <div style="display:flex;gap:6px;flex-shrink:0;align-items:center">
            <button class="bt bt-o bt-s" onclick="toggleSkillLike(${data.id},this)" style="display:flex;align-items:center;gap:4px">${likedIcon} <span id="detailLikeCount">${likeCount}</span></button>
            ${(data.is_obs || data.zip_url) ? `<button class="bt bt-o bt-s" onclick="downloadSkillZip('${data.id}')">${I18N.t('skills.download_skill')||'下载技能包'}</button>` : ''}
          </div>
        </div>
        <div style="font-size:12px;color:var(--t2);margin-bottom:14px;line-height:1.5">${data.description || ''}</div>
        <h3 style="font-size:13px;font-weight:600;margin-bottom:6px">SKILL.md</h3>
        <pre style="background:var(--bg);padding:14px;border-radius:var(--rd);font-size:11px;font-family:monospace;line-height:1.6;overflow-x:auto;white-space:pre-wrap">${content || I18N.t('skills.empty.no_detail')||'无详情'}</pre>
      </div>
      <div class="cd" style="padding:16px 20px;font-size:12px">
        <h3 style="font-size:13px;font-weight:600;margin-bottom:10px">${I18N.t('skills.info_title')||'技能信息'}</h3>
        <div class="srow"><label>${I18N.t('skills.info_name')||'名称'}</label><span>${data.name}</span></div>
        <div class="srow"><label>${I18N.t('skills.info_agent_type')||'Agent'}</label><span>${(meta.label||at)+' Agent'}</span></div>
        <div class="srow"><label>${I18N.t('skills.info_type')||'类型'}</label><span>${_SCOPE_NAMES[data.scope]||data.scope}</span></div>
        <div class="srow"><label>${I18N.t('skills.info_featured')||'精选'}</label><span>${data.is_featured ? I18N.t('skills.featured')||'是' : '—'}</span></div>
        <div class="srow"><label>${I18N.t('skills.info_likes')||'点赞'}</label><span>${likedIcon} ${likeCount}</span></div>
        <div class="srow"><label>${I18N.t('skills.info_scenes')||'场景'}</label><span class="t2">${scenesHtml}</span></div>
        <div class="srow"><label>${I18N.t('skills.info_description')||'描述'}</label><span class="t2">${data.description || '—'}</span></div>
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
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.upload_failed',{msg:e.message})||'上传失败');
  });
}

function deleteSkill(skillId, scope) {
  if (!confirm(I18N.t('skills.confirm.delete_skill',{name:''})||'确认删除？')) return;
  API.del('/skills/by-id/' + encodeURIComponent(skillId)).then(r => {
    if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.deleted')||'已删除');
    renderSkills();
  }).catch(e => {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.delete_failed',{msg:e.message})||'删除失败');
  });
}

function convertSkill(skillName) {
  const repo = _currentRepo();
  if (!repo) {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.select_repo_first')||'请先选择仓库');
    return;
  }
  if (!confirm(I18N.t('skills.confirm.convert_skill',{name:skillName,repo:repo})||'确认转换？')) return;
  API.post('/skills/convert', {skill_name: skillName, repo}).then(r => {
    if (app && app.showToast) app.showToast(r.message || I18N.t('skills.toast.convert_success')||'转换成功');
    renderSkills();
  }).catch(e => {
    if (app && app.showToast) app.showToast(I18N.t('skills.toast.convert_failed',{msg:e.message})||'转换失败');
  });
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
