// ═══════════════════════════════════════════════════════════════
// dashboard.js — 场景中心（卡片网格 + 场景详情页）
// 改造：下拉框 → 横向滚动场景卡片网格，点击卡片进入详情页
// ═══════════════════════════════════════════════════════════════

// Agent 元数据（与 scene-market.js 的 _AGENT_META 对齐）
const _DASH_AGENT_META = {
  vod:    {icon:'🔍', label:'VOD',    color:'#ff2d92', tag:'tg-v'},
  issue:  {icon:'📋', label:'Issue',  color:'#ff3b30', tag:'tg-i'},
  coding: {icon:'💻', label:'Coding', color:'#0071e3', tag:'tg-c'},
  qa:     {icon:'🧪', label:'QA',     color:'#af52de', tag:'tg-q'},
  sre:    {icon:'⚙️', label:'SRE',    color:'#ff9f0a', tag:'tg-s'},
  chat:   {icon:'💬', label:'Chat',   color:'#34c759', tag:'tg-ch'},
};

// 场景卡片背景渐变（与 scene-market.js 的 _CAT_BG 对齐）
const _DASH_SCENE_BG = [
  'bg-default', 'bg-harmony', 'bg-iac', 'bg-skill', 'bg-agent', 'bg-blog',
];

// 自定义场景可选图标池
const _CUSTOM_SCENE_ICONS = ['📦','🚀','🎨','🔬','🏗️','🛠️','📊','🎮','🌐','💡','🔧','⚡','🎯','🌱','🔥','⭐'];

// 场景图标映射（按 scene_id / name 匹配分类图标）
const _SCENE_ICONS = {
  'default':        '⚙',
  'harmony':        '🍃',
  'iac':            '☁',
  'cloud-native':   '☁',
  'skill-dev':      '🔧',
  'api-skill-dev':  '🔧',
  'agent-dev':      '🤖',
  'blog-publish':   '📝',
};

// 全局状态
let _dashScenePipelines = {};  // scene_id → pipeline_config（技能链用）
let _dashDetailSceneId = '';   // 当前查看详情的场景 ID

// ── 主入口：渲染场景中心（Hero + 场景卡片网格）──
function renderDashboard(data) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const scenes = data._scenes || [];
  const repo = data.repo || '';
  const role = data.role || 'Member';
  const m = data.metrics || {};
  let owner = '', name = '';
  try {
    const cr = app.currentRepo();
    if (cr) { owner = cr.owner; name = cr.name; }
  } catch(e) {}
  if (!owner || !name) {
    const parts = repo.replace(/^gitcode:/, '').split('/');
    owner = parts[0] || '';
    name = parts[1] || '';
  }
  window._dashboardData = data;
  window._dashOwner = owner;
  window._dashName = name;

  // dev_scene 为空或已删除 → 自动选中第一个场景
  const currentScene = (data.dev_scene && scenes.find(s => s.id === data.dev_scene))
    ? data.dev_scene
    : (scenes.length ? scenes[0].id : '');
  window._currentSceneId = currentScene;

  // 空状态卡片点击"创建新场景"
  window._enterCreateFromEmpty = function() {
    const tmpData = {
      agents: [], scene_nodes: [], repo: 'gitcode:' + owner + '/' + name,
      _scenes: [{ id: '__tmp__', name: I18N.t('dashboard.create_custom_scene'), scope: 'user' }],
      dev_scene: '__tmp__', agent_engines: {}, metrics: m,
    };
    renderDashboard(tmpData);
    setTimeout(function() {
      if (typeof enterCreateMode === 'function') {
        enterCreateMode();
      }
    }, 50);
  };

  // 场景列表为空 → 渲染空状态卡片
  if (!scenes.length) {
    el.innerHTML = `<style>
      .ap-hero{background:#fff;border:1px solid #d2d2d7;border-radius:18px;padding:28px 32px;margin-bottom:16px;box-shadow:0 2px 12px rgba(0,0,0,.06)}
      .ap-hero-row{display:flex;align-items:center;gap:16px}
      .ap-hero-icon{width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,#0071e3,#5ac8fa);display:flex;align-items:center;justify-content:center;color:#fff;font-size:22px;flex-shrink:0}
      .ap-hero-title{font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px}
      .ap-hero-sub{font-size:12px;color:#86868b;margin-top:3px}
      .ap-stats{display:flex;gap:8px;margin-left:auto}
      .ap-stat{text-align:center;padding:8px 18px;background:#f5f5f7;border-radius:12px;min-width:64px}
      .ap-stat .v{font-size:20px;font-weight:700;line-height:1.1}
      .ap-stat .l{font-size:10px;color:#86868b;margin-top:2px;letter-spacing:.3px}
      .ap-empty-card{background:#fff;border:1px solid #d2d2d7;border-radius:18px;padding:48px 24px;margin-bottom:16px;box-shadow:0 2px 12px rgba(0,0,0,.06);text-align:center}
      .ap-empty-icon{font-size:40px;margin-bottom:12px}
      .ap-empty-title{font-size:15px;font-weight:600;color:#1d1d1f;margin-bottom:6px}
      .ap-empty-desc{font-size:12px;color:#86868b;margin-bottom:20px}
      .ap-btn{padding:8px 20px;border-radius:10px;font-size:13px;font-weight:500;cursor:pointer;border:none;transition:all .15s}
    </style>
    <div class="ap-hero">
      <div class="ap-hero-row">
        <div class="ap-hero-icon">■</div>
        <div style="flex:1">
          <div class="ap-hero-title">${owner}/${name}</div>
          <div class="ap-hero-sub">${I18N.t('dashboard.webhook_ok')}</div>
        </div>
        <div class="ap-stats">
          <div class="ap-stat"><div class="v" style="color:#34c759">${m.active_agents || 0}</div><div class="l">${I18N.t('pipeline.stat_active')||'活跃'}</div></div>
          <div class="ap-stat"><div class="v" style="color:#ff3b30">${m.failed_events || 0}</div><div class="l">${I18N.t('pipeline.stat_failed')||'失败'}</div></div>
          <div class="ap-stat"><div class="v" style="color:#0071e3">${m.total_issues || 0}</div><div class="l">${I18N.t('dashboard.stat_total')||'总数'}</div></div>
        </div>
      </div>
    </div>
    <div class="ap-empty-card">
      <div class="ap-empty-icon">📋</div>
      <div class="ap-empty-title">${I18N.t('dashboard.no_scene_title')}</div>
      <div class="ap-empty-desc">${I18N.t('dashboard.no_scene_desc')}</div>
      <button class="ap-btn" onclick="_enterCreateFromEmpty()">➕ ${I18N.t('dashboard.no_scene_create')}</button>
    </div>`;
    return;
  }

  // 并行加载所有场景的 pipeline_config（用于卡片上的 Agent 技能链展示）
  const _loadPipelines = () => {
    API.get('/scenes/with-pipeline?repo=' + encodeURIComponent(owner + '/' + name)).then(d => {
      _dashScenePipelines = {};
      (d.scenes || []).forEach(s => {
        _dashScenePipelines[s.id] = s.pipeline || {};
      });
      _renderSceneCards();
    }).catch(() => {
      _dashScenePipelines = {};
      _renderSceneCards();
    });
  };

  // ── 渲染 Hero + 场景卡片网格 ──
  function _renderSceneCards() {
    const scenesData = scenes;
    let html = `<style>
      .ap-hero{background:rgba(255,255,255,.72);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:none;border-radius:18px;padding:24px 32px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
      .ap-hero-row{display:flex;align-items:center;gap:16px}
      .ap-hero-icon{width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,#5ac8fa,#0071e3);display:flex;align-items:center;justify-content:center;color:#fff;font-size:22px;flex-shrink:0}
      .ap-hero-title{font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px}
      .ap-hero-sub{font-size:12px;color:#86868b;margin-top:3px}
      .ap-stats{display:flex;gap:8px;margin-left:auto}
      .ap-stat{text-align:center;padding:8px 18px;background:#f5f5f7;border-radius:12px;min-width:64px}
      .ap-stat .v{font-size:20px;font-weight:700;line-height:1.1}
      .ap-stat .l{font-size:10px;color:#86868b;margin-top:2px;letter-spacing:.3px}
      .ap-btn{padding:7px 18px;border-radius:999px;font-size:12px;font-weight:500;cursor:pointer;transition:all .2s ease;background:transparent;border:1px solid rgba(0,113,227,.3);color:#0071e3}
      .ap-btn:hover{background:#0071e3;border-color:#0071e3;color:#fff}
      .ap-btn-d{border:1px solid rgba(255,59,48,.3);color:#ff3b30}
      .ap-btn-d:hover{background:#ff3b30;border-color:#ff3b30;color:#fff}
      .host-banner{display:flex;align-items:center;gap:10px;background:rgba(255,243,205,.8);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:none;border-radius:14px;padding:10px 14px;margin-bottom:12px;font-size:13px;color:#7a5b00;box-shadow:0 1px 3px rgba(0,0,0,.04)}
      .host-banner.error{background:rgba(253,236,234,.8);color:#c31f1e}
      .host-banner button{margin-left:auto;background:rgba(255,149,0,.15);color:#ff9500;border:1px solid rgba(255,149,0,.3);border-radius:999px;padding:6px 16px;cursor:pointer;font-size:12px;font-weight:500;transition:all .2s}
      .host-banner button:hover{background:#ff9500;color:#fff;border-color:#ff9500}
      .host-banner.error button{color:#ff3b30;border-color:rgba(255,59,48,.3);background:rgba(255,59,48,.1)}
      .host-banner.error button:hover{background:#ff3b30;color:#fff;border-color:#ff3b30}
      .scn-applied-badge{font-size:11px;font-weight:500;padding:5px 14px;border-radius:999px;background:rgba(52,199,89,.1);color:#34c759;display:inline-flex;align-items:center;gap:3px;border:1px solid rgba(52,199,89,.2)}
      .scn-list{display:flex;flex-direction:column;gap:12px}
      .scn-row{display:flex;align-items:center;gap:16px;padding:16px 20px;background:rgba(255,255,255,.6);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:none;border-radius:18px;cursor:pointer;transition:all .25s ease;position:relative;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.05)}
      .scn-row:hover{background:rgba(255,255,255,.8);box-shadow:0 3px 16px rgba(0,0,0,.08)}
      .scn-row-icon{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;color:#fff;font-weight:600}
      .scn-row-name{font-size:15px;font-weight:600;color:#1d1d1f;white-space:nowrap;letter-spacing:-.2px}
      .scn-row-mid{flex:0 1 30%;min-width:0;display:flex;flex-direction:column;gap:3px}
      .scn-row-desc{font-size:12px;color:#86868b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:400}
      .scn-row-chain{display:flex;align-items:center;gap:6px;flex-wrap:wrap;flex:1 1 auto;min-width:0;padding:0 16px}
      .scn-row-chain .scn-tag{font-size:12px;font-weight:500;padding:4px 14px;border-radius:999px;letter-spacing:.1px}
      .scn-row-chain .scn-arr{font-size:12px;color:#d2d2d7;font-weight:400;margin:0 1px}
      .scn-branch-tag{font-size:11px;font-weight:500;padding:3px 12px;border-radius:999px;background:rgba(142,142,147,.1);color:#8e8e93;letter-spacing:.1px;flex-shrink:0}
      .scn-row-right{display:flex;align-items:center;gap:10px;flex-shrink:0}
      .scn-card-desc{font-size:12px;color:var(--t2);line-height:1.6;margin-bottom:14px;min-height:38px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
      .scn-card-foot{display:flex;align-items:center;gap:8px;padding-top:14px;border-top:1px solid var(--b);margin-top:auto}
    </style>`;

    // Host banner
    const hs = data.host_status || '';
    if (hs) {
      if (hs === 'bot_missing') {
        html += '<div class="host-banner error">⚠️ 该项目 grape_bot 未成功加入项目，请手动添加 bot 至项目，或点击「重检修复」自动处理<bR> <button onclick="window._repairHost()">重检修复</button></div>';
      } else if (hs === 'webhook_missing') {
        html += '<div class="host-banner">⚠️ 项目 webhook 缺失，Grape 无法接收仓库事件<bR> <button onclick="window._repairHost()">补建 webhook</button></div>';
      } else {
        html += '<div class="host-banner">⚠️ 该项目托管流程未完成，Grape 部分功能不可用<bR> <button onclick="window._repairHost()">重检修复</button></div>';
      }
    }

    // Hero 区域
    html += `<div class="ap-hero">
      <div class="ap-hero-row">
        <div class="ap-hero-icon">■</div>
        <div style="flex:1">
          <div class="ap-hero-title">${owner}/${name} <span style="font-size:10px;font-weight:500;color:#0071e3;background:#e3edfd;padding:2px 8px;border-radius:6px;vertical-align:middle;margin-left:4px">${role === 'Maintainer' ? I18N.t('dashboard.hero_maintainer') : I18N.t('dashboard.hero_member')}</span></div>
          <div class="ap-hero-sub">${role === 'Maintainer' ? I18N.t('dashboard.hero_maintainer') : I18N.t('dashboard.hero_member')} · ${I18N.t('dashboard.webhook_ok')}</div>
        </div>
        <div class="ap-stats">
          <div class="ap-stat"><div class="v" style="color:#34c759">${m.active_agents || 0}</div><div class="l">${I18N.t('pipeline.stat_active')||'活跃'}</div></div>
          <div class="ap-stat"><div class="v" style="color:#ff3b30">${m.failed_events || 0}</div><div class="l">${I18N.t('pipeline.stat_failed')||'失败'}</div></div>
          <div class="ap-stat"><div class="v" style="color:#0071e3">${m.total_issues || 0}</div><div class="l">${I18N.t('dashboard.stat_total')||'总数'}</div></div>
        </div>
      </div>
    </div>`;

    // 场景中心介绍框（缩短高度）
    html += `<div class="apple-card" style="margin-bottom:16px;background:rgba(255,255,255,.6);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:none;border-radius:18px;box-shadow:0 1px 4px rgba(0,0,0,.05)">
      <div style="display:flex;align-items:center;gap:12px;padding:10px 20px">
        <div style="font-size:32px;flex-shrink:0">🏪</div>
        <div style="flex:1;min-width:0">
          <div style="font-size:14px;font-weight:600;color:#1d1d1f;margin-bottom:4px">${I18N.t('dashboard.market_intro_title')||'探索场景中心'}</div>
          <div style="font-size:12px;color:#86868b;line-height:1.5">${I18N.t('dashboard.market_intro_desc')||'场景中心提供官方场景模板，涵盖云原生、IaC、Agent 开发等多种场景，一键应用到你的项目'}</div>
        </div>
        <button class="ap-btn" onclick="app.nav('scene-market')">${I18N.t('dashboard.goto_market')||'前往场景中心'} →</button>
      </div>
    </div>`;

    // 场景列表（列表式，每行一个场景）
    html += `<div class="apple-card" style="background:rgba(255,255,255,.6);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:none;border-radius:18px;box-shadow:0 1px 4px rgba(0,0,0,.05)">
      <div class="cd-h" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <h2>${I18N.t('dashboard.scene_center_title')} <span style="font-size:10px;font-weight:400;color:var(--t2);background:var(--bg);padding:2px 8px;border-radius:8px;margin-left:4px">${scenesData.length}</span></h2>
        <div style="display:flex;gap:8px">
          <button class="ap-btn" onclick="_openSceneRecommendModal('${owner}/${name}')">${I18N.t('dashboard.scene_recommend_rules')||'智能推荐规则'}</button>
          <button class="ap-btn" onclick="enterCreateMode()">＋ ${I18N.t('dashboard.add_scene')||'添加场景'}</button>
        </div>
      </div>
      <div id="dash-scenes" class="scn-list">`;

    scenesData.forEach((s, idx) => {
      html += _renderLocalSceneCard(s, s.id === currentScene, idx);
    });

    html += `</div>
    </div>`;

    el.innerHTML = html;

  }

  // 删除模式切换
  let _deleteModeActive = false;
  window._toggleDeleteMode = function() {
    _deleteModeActive = !_deleteModeActive;
    _deleteMode = _deleteModeActive;
    const btn = document.getElementById('deleteModeBtn');
    if (_deleteModeActive) {
      // 进入删除模式：卡片上加叉号
      document.querySelectorAll('.scn-row').forEach(card => {
        if (card.querySelector('.scn-del-x')) return;
        const x = document.createElement('div');
        x.className = 'scn-del-x';
        x.style.cssText = 'position:absolute;top:10px;right:10px;width:22px;height:22px;border-radius:6px;background:rgba(255,59,48,.1);color:#ff3b30;font-size:12px;display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:10;opacity:0;transition:all .2s;border:1px solid rgba(255,59,48,.3)';
        x.textContent = '✕';
        x.onclick = function(e) {
          e.stopPropagation();
          const sid = card.getAttribute('data-scene-id');
          const sname = card.getAttribute('data-scene-name');
          _confirmDeleteScene(sid, sname, e);
        };
        card.style.position = 'relative';
        card.appendChild(x);
        x.onmouseenter = function() { this.style.background = '#ff3b30'; this.style.color = '#fff'; this.style.borderColor = '#ff3b30'; };
        x.onmouseleave = function() { this.style.background = 'rgba(255,59,48,.1)'; this.style.color = '#ff3b30'; this.style.borderColor = 'rgba(255,59,48,.3)'; };
        setTimeout(() => { x.style.opacity = '1'; }, 50);
      });
      if (btn) {
        btn.textContent = I18N.t('dashboard.finish_delete')||'结束删除';
        btn.style.color = '#ff3b30';
        btn.style.borderColor = '#ff3b30';
      }
    } else {
      // 退出删除模式：移除叉号 + 恢复卡片样式
      document.querySelectorAll('.scn-del-x').forEach(x => x.remove());
      document.querySelectorAll('.scn-row').forEach(card => {
      });
      if (btn) {
        btn.textContent = I18N.t('dashboard.delete_scene')||'删除场景';
        btn.style.color = '';
        btn.style.borderColor = '';
      }
    }
  };

  // 确认删除场景
  window._confirmDeleteScene = function(sceneId, sceneName, e) {
    if (e) e.stopPropagation();
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    overlay.innerHTML = '<div style="background:#fff;border-radius:18px;padding:28px;min-width:320px;max-width:400px;box-shadow:0 12px 48px rgba(0,0,0,.15)">' +
      '<h3 style="margin:0 0 12px;font-size:17px;font-weight:600;color:#1d1d1f">' + (I18N.t('dashboard.confirm_delete_scene')||'确认删除场景') + '</h3>' +
      '<p style="margin:0 0 20px;font-size:13px;color:#86868b;line-height:1.5">' + sceneName + '</p>' +
      '<div style="display:flex;justify-content:flex-end;gap:8px">' +
        '<button class="ap-btn" onclick="this.closest(\'[style*=fixed]\').remove()">' + (I18N.t('common.cancel')||'取消') + '</button>' +
        '<button class="ap-btn ap-btn-d" id="confirmDeleteCardBtn">' + (I18N.t('common.delete')||'删除') + '</button>' +
      '</div></div>';
    document.body.appendChild(overlay);
    document.getElementById('confirmDeleteCardBtn').onclick = function() {
      API.del('/scenes/' + encodeURIComponent(sceneId) + '?repo=' + encodeURIComponent(owner + '/' + name)).then(() => {
        overlay.remove();
        app.showToast(I18N.t('dashboard.scene_deleted')||'场景已删除');
        // 重新加载
        API.get('/dashboard/' + owner + '/' + name).then(renderDashboard).catch(() => {});
      }).catch(err => { overlay.remove(); app.showToast(I18N.t('dashboard.delete_failed', {msg: (err.detail?.message || err.message)})); });
    };
  };

  _loadPipelines();
}

// ── 渲染单个场景行（列表式，一行一个场景）──
let _deleteMode = false;
function _renderLocalSceneCard(s, isApplied, idx) {
  const catBg = _DASH_SCENE_BG[idx % _DASH_SCENE_BG.length];
  const icon = _SCENE_ICONS[s.id] || _SCENE_ICONS[s.name] || s.icon || (s.scope === 'official' ? '⚙' : '📦');
  const pipeline = _dashScenePipelines[s.id] || {};
  const nodes = pipeline.nodes || [];
  // 莫兰迪配色（低饱和度）
  const morandiColors = ['#8B9DB0','#A8B8A0','#C4A882','#B0A0B8','#9BB0C4','#C49B9B'];
  const tagColors = {vod:'#B07A8B',issue:'#B8857A',coding:'#7A92B0',qa:'#9A85A8',sre:'#B89B6A'};
  const chain = nodes.map((n, i) => {
    const tag = n.tag || n.id || '';
    const meta = _DASH_AGENT_META[tag] || {};
    const label = meta.label || tag;
    return `${i>0?'<span class="scn-arr">→</span>':''}<span class="scn-tag" style="background:${(tagColors[tag]||'#B0B0B0')}1A;color:${tagColors[tag]||'#86868b'}">${label}</span>`;
  }).join('');
  const desc = s.description || '';
  const workBranch = s.work_branch || 'main';
  const createdBy = s.created_by || '';
  const barColor = morandiColors[idx % morandiColors.length];

  return `<div class="scn-row" data-scene-id="${s.id}" data-scene-name="${(s.display_name||s.name||'').replace(/"/g,'&quot;')}" onclick="app.nav('scene-detail-local','${s.id}')">
    <div class="scn-row-icon" style="background:${barColor}">${icon}</div>
    <div class="scn-row-mid">
      <div style="display:flex;align-items:center;gap:8px">
        <span class="scn-row-name">${s.display_name || s.name}</span>
        ${createdBy ? `<span style="font-size:10px;color:var(--t3)">@${createdBy}</span>` : ''}
      </div>
      <div style="display:flex;align-items:center;gap:8px;min-width:0">
        <span class="scn-row-desc" title="${desc.replace(/"/g, '&quot;')}">${desc || '—'}</span>
      </div>
    </div>
    <div class="scn-row-chain">${chain}</div>
    <div class="scn-row-right">
      <span class="scn-branch-tag">🌿 ${workBranch}</span>
      ${isApplied ? '' : ''}
    </div>
  </div>`;
}

// ── 滚动按钮显隐 ──
function _updateDashScrollBtns() {
  const track = document.getElementById('dash-scenes');
  if (!track) return;
  const prev = document.getElementById('dash-scenes-prev');
  const next = document.getElementById('dash-scenes-next');
  const canLeft = track.scrollLeft > 5;
  const canRight = track.scrollLeft + track.clientWidth < track.scrollWidth - 5;
  if (prev) prev.classList.toggle('show', canLeft);
  if (next) next.classList.toggle('show', canRight);
}

window.addEventListener('resize', function() {
  if (document.getElementById('dash-scenes')) _updateDashScrollBtns();
});

// ═══════════════════════════════════════════════════════════════
// 场景详情页（点击卡片后进入）
// ═══════════════════════════════════════════════════════════════
// 构建相关流水线 HTML（从 pipeData 提取当前场景的流水线统计+表格）
function _buildRelatedPipelineHTML(pipeData, sceneId, dashData) {
  const allPipesRaw = (pipeData.active ? [pipeData.active] : []).concat(pipeData.history || []);
  const scenePipes = allPipesRaw.filter(p => p.scene_id === sceneId);
  const _stageLabel = function(stage) { var map = {vod:'VOD',issue:'Issue',coding:'Coding',qa:'QA',sre:'SRE',done:I18N.t('pipeline.stage_done')||'完成'}; return map[stage] ? map[stage] : stage; };
  const sceneActiveCount = scenePipes.filter(p => p.executing).length;
  const sceneFailedCount = scenePipes.filter(p => p.statuses && p.statuses.some(s => s === 3)).length;

  let html = `<div class="det-card-wrap" id="relatedPipelineSection">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h2 style="font-size:15px;font-weight:600">${I18N.t('dashboard.related_pipeline')||'相关流水线'}</h2>
      <div style="display:flex;gap:8px">
        <div class="pl-stat"><div class="v" style="color:#34c759">${sceneActiveCount}</div><div class="l">${I18N.t('pipeline.stat_active')||'活跃'}</div></div>
        <div class="pl-stat"><div class="v" style="color:#ff3b30">${sceneFailedCount}</div><div class="l">${I18N.t('pipeline.stat_failed')||'失败'}</div></div>
        <div class="pl-stat"><div class="v" style="color:#0071e3">${scenePipes.length}</div><div class="l">${I18N.t('dashboard.stat_total')||'总数'}</div></div>
      </div>
    </div>`;

  if (scenePipes.length === 0) {
    html += `<div style="text-align:center;padding:24px;color:#86868b;font-size:13px">${I18N.t('pipeline.no_issues')||'暂无 Issue'}</div>`;
  } else {
    const _repoParts = (dashData.repo || '').replace(/^(gitcode|github):/, '').split('/');
    html += `<div class="pl-table-card" style="border:none;box-shadow:none"><div class="pl-table-wrap" style="max-height:320px"><table class="pl-table"><tr><th>${I18N.t('pipeline.th_issue')||'Issue'}</th><th>${I18N.t('pipeline.th_title')||'标题'}</th><th>${I18N.t('pipeline.th_duration')||'耗时'}</th><th>${I18N.t('pipeline.th_agents')||'Agent'}</th><th>${I18N.t('pipeline.th_stage')||'阶段'}</th><th>${I18N.t('pipeline.th_owner')||'负责人'}</th></tr>`;
    scenePipes.slice(0, 20).forEach(p => {
      const _issueNum = String(p.issue_id || '').replace(/^#/, '');
      const _issueHost = (dashData.repo || '').startsWith('github:') ? 'github.com' : 'gitcode.com';
      const _issueUrl = (_issueNum && _repoParts.length === 2) ? `https://${_issueHost}/${_repoParts[0]}/${_repoParts[1]}/issues/${_issueNum}` : '';
      const agentTags = ['vod','issue','coding','qa','sre'].map((at, j) => {
        const hasAgent = p.statuses && p.statuses[j] > 0;
        const cls = ['tg-v','tg-i','tg-c','tg-q','tg-s'][j];
        const evSeq = (p.stages && p.stages[j] && p.stages[j].latest_event && p.stages[j].latest_event.seq) || '';
        if (hasAgent && evSeq) {
          return `<span class="tg ${cls}" style="cursor:pointer;transition:transform .15s" onclick="event.stopPropagation();app.nav('event','${evSeq}')" onmouseover="this.style.transform='scale(1.25)'" onmouseout="this.style.transform='scale(1)'">${at.charAt(0).toUpperCase()}</span>`;
        }
        return `<span class="tg ${cls}" style="opacity:0.3;cursor:default">${at.charAt(0).toUpperCase()}</span>`;
      }).join('');
      const stageName = p.stage ? _stageLabel(p.stage) : '—';
      const _dur = p.duration;
      const dur = _dur ? (_dur >= 60 ? Math.floor(_dur/60) + 'm ' + Math.round(_dur%60) + 's' : _dur + 's') : '—';
      const execBadge = p.executing ? '<span class="tg tg-c" style="color:#fff;background:var(--g)">⚡</span>' : '';
      let latestSeq = '';
      if (p.stages) {
        for (let si = p.stages.length - 1; si >= 0; si--) {
          if (p.statuses && p.statuses[si] > 0) {
            const ev = p.stages[si] && p.stages[si].latest_event;
            if (ev && ev.seq) { latestSeq = ev.seq; break; }
          }
        }
      }
      html += `<tr onclick="if('${latestSeq}')app.nav('event','${latestSeq}')">
        <td><b>${p.issue_id || '?'}</b> ${execBadge}</td>
        <td><a href="${_issueUrl||'#'}" target="_blank" rel="noopener" style="color:var(--a);text-decoration:underline;font-size:11px">${(p.title || '—').substring(0, 40)}</a></td>
        <td>${dur}</td>
        <td>${agentTags}</td>
        <td><span style="font-size:11px;color:${p.stage === 'done' ? '#34c759' : '#86868b'}">${stageName}</span></td>
        <td>${p.current_owner || '—'}</td>
      </tr>`;
    });
    html += `</table></div></div>`;
  }
  html += `</div>`;
  return html;
}

// 延迟填充相关流水线区域
function _fillRelatedPipeline(pipeData, sceneId, dashData) {
  const section = document.getElementById('relatedPipelineSection');
  if (section) {
    section.outerHTML = _buildRelatedPipelineHTML(pipeData, sceneId, dashData);
  }
}

function renderSceneDetailLocal(sceneId) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  _dashDetailSceneId = sceneId;
  el.innerHTML = '<div class="loading">' + I18N.t('common.loading') + '</div>';

  const data = window._dashboardData || {};
  let owner = '', name = '';
  try {
    const cr = app.currentRepo();
    if (cr) { owner = cr.owner; name = cr.name; }
  } catch(e) {}
  if (!owner || !name) {
    const parts = (data.repo || '').replace(/^gitcode:/, '').split('/');
    owner = parts[0] || '';
    name = parts[1] || '';
  }

  // 并行加载：dashboard data + pipeline config（首屏必要）
  // pipeline(issues) 延迟加载，不阻塞首屏渲染
  const dashPromise = (data.agents ? Promise.resolve(data) :
    API.get('/dashboard/' + owner + '/' + name));
  const pipeCfgPromise = API.get('/scenes/with-pipeline?repo=' + encodeURIComponent(owner + '/' + name));

  Promise.all([dashPromise, pipeCfgPromise]).then(([dashData, scenesData]) => {
    const scenes = dashData._scenes || (scenesData.scenes || []).map(s => ({id: s.id, name: s.name, display_name: s.display_name, scope: 'repo'}));
    const scene = scenes.find(s => s.id === sceneId);
    if (!scene) {
      el.innerHTML = '<div class="error">场景不存在</div>';
      return;
    }
    const scenePipeline = (scenesData.scenes || []).find(s => s.id === sceneId);
    const pipeline = scenePipeline ? scenePipeline.pipeline : (_dashScenePipelines[sceneId] || {});
    const nodes = (pipeline && pipeline.nodes) || [];
    const agents = dashData.agents || [];
    const agentEngines = dashData.agent_engines || {};
    const currentScene = dashData.dev_scene || '';
    const isApplied = sceneId === currentScene;
    const isCustomScene = scene.scope !== 'official';
    const m = dashData.metrics || {};

    _renderLocalSceneDetailPage(scene, nodes, agents, agentEngines, isApplied, isCustomScene, null, owner, name, m, dashData);

    // 延迟加载相关流水线数据，到了再填充底部表格
    API.get('/pipeline/' + owner + '/' + name).then(pipeData => {
      _fillRelatedPipeline(pipeData, scene.id, dashData);
    }).catch(() => {});
  }).catch(e => {
    el.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + (e.message||'') + '</div>';
  });
}

function _renderLocalSceneDetailPage(scene, nodes, agents, agentEngines, isApplied, isCustomScene, pipeData, owner, name, metrics, dashData) {
  const el = document.getElementById('pageContent');
  if (!el) return;
  const catBg = 'bg-default';
  const icon = _SCENE_ICONS[scene.id] || _SCENE_ICONS[scene.name] || (scene.scope === 'official' ? '⚙' : '📦');
  const agentOrder = ['vod','issue','coding','qa','sre'];
  const agentLabels = ['VOD Agent','Issue Agent','Coding Agent','QA Agent','SRE Agent'];
  const colors = {vod:'#B07A8B',issue:'#B8857A',coding:'#7A92B0',qa:'#9A85A8',sre:'#B89B6A'};
  const _detailMorandi = ['#8B9DB0','#A8B8A0','#C4A882','#B0A0B8','#9BB0C4','#C49B9B'];
  const _detailIconColor = _detailMorandi[(scene.id || '').length % _detailMorandi.length];
  const descs = {vod:I18N.t('dashboard.desc_vod'),issue:I18N.t('dashboard.desc_issue'),coding:I18N.t('dashboard.desc_coding'),qa:I18N.t('dashboard.desc_qa'),sre:I18N.t('dashboard.desc_sre')};
  const sceneName = scene.display_name || scene.name;
  const sceneDesc = scene.description || '';

  let html = `<style>
    .ap-btn{padding:7px 18px;border-radius:999px;font-size:12px;font-weight:500;cursor:pointer;transition:all .2s ease;background:transparent;border:1px solid rgba(0,113,227,.3);color:#0071e3}
    .ap-btn:hover{background:#0071e3;border-color:#0071e3;color:#fff}
    .ap-btn-d{border:1px solid rgba(255,59,48,.3);color:#ff3b30}
    .ap-btn-d:hover{background:#ff3b30;border-color:#ff3b30;color:#fff}
    .ap-grid{display:flex;gap:12px;padding:0;overflow-x:auto;min-height:280px}
    .ap-nd{flex:1 1 0;min-width:0;max-width:260px;min-height:260px;background:rgba(255,255,255,.6);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:none;border-radius:18px;padding:18px;display:flex;flex-direction:column;transition:all .25s ease;position:relative;box-shadow:0 1px 4px rgba(0,0,0,.05)}
    .ap-nd:hover{background:rgba(255,255,255,.8);box-shadow:0 3px 16px rgba(0,0,0,.08)}
    .ap-nd-top{width:36px;height:4px;border-radius:2px;margin-bottom:12px}
    .ap-nd-name{font-size:14px;font-weight:600;color:#1d1d1f;margin-bottom:6px;display:flex;align-items:center;gap:8px}
    .ap-nd-desc{font-size:11px;color:#86868b;line-height:1.5;margin-bottom:10px;font-weight:400}
    .det-card-wrap{background:rgba(255,255,255,.6);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:none;border-radius:18px;padding:24px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.05)}
    .scn-applied-badge{font-size:11px;font-weight:500;padding:5px 14px;border-radius:999px;background:rgba(52,199,89,.1);color:#34c759;display:inline-flex;align-items:center;gap:3px;border:1px solid rgba(52,199,89,.2)}
    .pl-stat{text-align:center;padding:8px 16px;background:#f5f5f7;border:none;border-radius:14px;min-width:64px}
    .pl-stat .v{font-size:18px;font-weight:700;line-height:1.1}
    .pl-stat .l{font-size:10px;color:#86868b;margin-top:2px}
    .pl-table-card{background:rgba(255,255,255,.6);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:none;border-radius:18px;box-shadow:0 1px 4px rgba(0,0,0,.05);overflow:hidden}
    .pl-table-wrap{overflow-y:auto;max-height:400px}
    .pl-table{width:100%;border-collapse:collapse;font-size:12px}
    .pl-table th{text-align:left;padding:10px 14px;font-weight:500;color:#86868b;border-bottom:1px solid rgba(0,0,0,.04);font-size:10px;text-transform:uppercase;letter-spacing:.4px;position:sticky;top:0;background:rgba(255,255,255,.9);z-index:1}
    .pl-table td{padding:10px 14px;border-bottom:1px solid rgba(0,0,0,.03)}
    .pl-table tr{cursor:pointer;transition:background .12s}
    .pl-table tr:hover td{background:rgba(0,0,0,.02)}
    .scene-edit-input{width:100%;padding:6px 10px;border:none;border-radius:8px;font-size:inherit;font-weight:inherit;background:#f5f5f7;color:#1d1d1f;outline:none}
    .adv-row{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid rgba(0,0,0,.04)}
    .adv-row:last-child{border-bottom:none}
    .adv-label{font-size:12px;font-weight:600;color:#86868b;min-width:80px;flex-shrink:0}
    .adv-select{flex:1;max-width:280px;padding:8px 12px;border:none;border-radius:12px;font-size:13px;background:#f5f5f7;color:#1d1d1f;outline:none}
    .adv-select:focus{background:#ebebed}
    .adv-kv{display:flex;gap:8px;align-items:center}
    .adv-kv input{padding:8px 12px;border:none;border-radius:12px;font-size:12px;outline:none;background:#f5f5f7;transition:background .15s}
    .adv-kv input:focus{background:#ebebed}
    .adv-kv-key{flex:0 0 120px}
    .adv-kv-val{flex:1}
    .adv-del{width:28px;height:28px;border-radius:999px;border:none;background:rgba(255,59,48,.08);color:#ff3b30;cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
    .adv-del:hover{background:rgba(255,59,48,.15)}
    select{appearance:none;-webkit-appearance:none;-moz-appearance:none;background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' fill='none'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%2386868b' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:28px!important;border-radius:8px;cursor:pointer}
    select option{padding:8px 12px;background:#fff;color:#1d1d1f;font-size:12px}
    select option:checked{background:#0071e3;color:#fff}
    select option:hover{background:#f0f4ff;color:#0071e3}
    select::-ms-expand{display:none}
    select:focus{border-color:#0071e3!important}
    /* 自定义下拉面板样式（通过 JS 动态创建） */
    .custom-dropdown{position:absolute;z-index:9999;background:#fff;border:1px solid #e8e8ed;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.12);padding:6px;max-height:240px;overflow-y:auto;min-width:160px}
    .custom-dropdown::-webkit-scrollbar{width:6px}
    .custom-dropdown::-webkit-scrollbar-thumb{background:#d2d2d7;border-radius:3px}
    .custom-dropdown::-webkit-scrollbar-track{background:transparent}
    .custom-dropdown-item{padding:8px 12px;border-radius:8px;font-size:12px;color:#1d1d1f;cursor:pointer;transition:background .1s}
    .custom-dropdown-item:hover{background:#f0f4ff}
    .custom-dropdown-item.selected{background:#0071e3;color:#fff;font-weight:500}
    .custom-dropdown-item.selected:hover{background:#0077ed}
  </style>`;

  // 返回按钮
  html += `<button class="ap-btn" onclick="app.nav('dash')" style="margin-bottom:12px">← ${I18N.t('dashboard.scene_center_title')||'项目场景'}</button>`;

  // 场景名称 + 描述（各自独立点击编辑，像文件重命名）
  html += `<div class="cd" style="padding:20px 24px;margin-bottom:16px">
    <div style="display:flex;align-items:flex-start;gap:16px">
      <div class="scn-row-icon" style="width:40px;height:40px;border-radius:10px;font-size:18px;background:${_detailIconColor};display:flex;align-items:center;justify-content:center;flex-shrink:0;color:#fff;font-weight:600">${icon}</div>
      <div style="flex:1;min-width:0">
        <div id="sceneNameDisplay" style="font-size:20px;font-weight:700;cursor:text;padding:2px 6px;margin:-2px -6px 4px;border-radius:6px;transition:background .15s" onmouseover="this.style.background='rgba(0,0,0,.04)'" onmouseout="this.style.background='transparent'" onclick="_enterInlineEdit('sceneName')">${sceneName}</div>
        <div id="sceneDescDisplay" style="font-size:13px;color:#86868b;cursor:text;padding:2px 6px;margin:-2px -6px;border-radius:6px;transition:background .15s" onmouseover="this.style.background='rgba(0,0,0,.04)'" onmouseout="this.style.background='transparent'" onclick="_enterInlineEdit('sceneDesc')">${sceneDesc || '<span style="color:#c7c7cc;font-style:italic">' + (I18N.t('dashboard.desc_optional')||'描述（可选）') + '</span>'}</div>
      </div>

      ${isCustomScene ? `<button class="ap-btn ap-btn-d" style="margin-top:4px" onclick="_confirmDeleteScene('${scene.id}','${(scene.display_name||scene.name||'').replace(/'/g,'\\\'')}',null)">${I18N.t('common.delete')||'删除'}</button>` : ''}
    </div>
  </div>`;

  // 执行流程（带编辑按钮）— 用 id 标记方便后续替换
  html += `<div class="det-card-wrap" id="flowSection">
    <div id="flowHeader" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h2 style="font-size:15px;font-weight:600">${I18N.t('skills.execution_flow')||'执行流程'}</h2>
      <div style="display:flex;gap:8px">
        ${isCustomScene ? `<button class="ap-btn" id="editFlowBtn" onclick="_toggleFlowEdit('${scene.id}','${owner}','${name}')">${I18N.t('dashboard.edit')||'编辑'}</button>` : ''}
      </div>
    </div>
    <div class="ap-grid" style="padding:0" id="flowCardsContainer">`;

  if (nodes.length > 0) {
    html += _renderFlowCards(nodes, agents, agentEngines, agentOrder, agentLabels, colors, descs, false, []);
    html += `</div></div>`;
  } else {
    html += `<div style="padding:20px;text-align:center;color:#86868b">无 Agent 节点</div></div>`;
  }

  // 高级选项（右上角保存按钮）
  html += `<div class="det-card-wrap" id="advOptionsInline">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
      <h2 style="font-size:15px;font-weight:600">${I18N.t('dashboard.advanced_options')||'高级选项'}</h2>
      <button class="ap-btn" onclick="_saveAdvancedOptionsInline('${scene.id}','${owner}/${name}')">${I18N.t('common.save')||'保存'}</button>
    </div>
    <div class="adv-row">
      <span class="adv-label">${I18N.t('dashboard.work_branch')||'工作分支'}</span>
      <select id="advWorkBranchInline" class="adv-select" style="padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;background:#f5f5f7;color:#1d1d1f;outline:none;cursor:pointer;transition:all .15s" onfocus="this.style.borderColor='#0071e3';this.style.background='#fff'" onblur="this.style.borderColor='#e8e8ed';this.style.background='#f5f5f7'"><option value="">${I18N.t('common.loading')||'加载中...'}</option></select>
      <button class="ap-btn" onclick="_toggleNewBranchInline()">${I18N.t('dashboard.create_branch')||'新建分支'}</button>
    </div>
    <div id="newBranchInputInline" style="display:none;padding:8px 0 4px 92px">
      <div style="display:flex;gap:8px">
        <input id="newBranchNameInline" placeholder="${I18N.t('dashboard.branch_name')||'分支名'}" style="flex:1;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;outline:none;background:#f5f5f7;transition:all .15s" onfocus="this.style.borderColor='#0071e3';this.style.background='#fff'" onblur="this.style.borderColor='#e8e8ed';this.style.background='#f5f5f7'">
        <select id="newBranchRefInline" style="width:120px;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;background:#f5f5f7;outline:none;cursor:pointer;transition:all .15s" onfocus="this.style.borderColor='#0071e3';this.style.background='#fff'" onblur="this.style.borderColor='#e8e8ed';this.style.background='#f5f5f7'"></select>
        <button class="ap-btn" onclick="_createNewBranchInline('${owner}','${name}')">${I18N.t('common.create')||'创建'}</button>
        <button class="ap-btn" onclick="_toggleNewBranchInline()">${I18N.t('common.cancel')||'取消'}</button>
      </div>
    </div>
    <div style="padding:12px 0 4px">
      <div style="font-size:12px;font-weight:600;color:#86868b;margin-bottom:8px">${I18N.t('dashboard.custom_config')||'自定义配置'}</div>
      <div id="advCustomFieldsInline" style="display:flex;flex-direction:column;gap:8px"></div>
      <button class="ap-btn" style="margin-top:8px;font-size:11px" onclick="_addCustomFieldInline()">+ ${I18N.t('dashboard.add_config')||'添加配置项'}</button>
    </div>
  </div>`;

  // 相关流水线（统计 + 表格，无流程图）— 只显示当前场景的流水线
  // pipeData=null 时先占位，数据到了由 _fillRelatedPipeline 填充
  if (pipeData) {
    html += _buildRelatedPipelineHTML(pipeData, scene.id, dashData);
  } else {
    html += `<div class="det-card-wrap" id="relatedPipelineSection" style="display:flex;align-items:center;justify-content:center;min-height:80px;color:#86868b;font-size:13px">${I18N.t('common.loading')||'加载中...'}</div>`;
  }

  el.innerHTML = html;

  // 将页面内所有 select 转换为自定义下拉面板（圆角风格统一）
  // 加载高级选项数据
  _loadAdvancedOptionsInline(scene.id, owner, name);

  // 点击单个字段进入编辑模式（名称或描述独立编辑）
  window._enterInlineEdit = function(field) {
    const displayId = field === 'sceneName' ? 'sceneNameDisplay' : 'sceneDescDisplay';
    const display = document.getElementById(displayId);
    if (!display) return;
    const currentVal = field === 'sceneName' ? sceneName : sceneDesc;
    const fontSize = field === 'sceneName' ? '20px' : '13px';
    const fontWeight = field === 'sceneName' ? '700' : '400';
    const color = field === 'sceneName' ? '#1d1d1f' : '#86868b';
    const inputId = field + 'EditInput';
    display.outerHTML = `<input id="${inputId}" style="width:100%;font-size:${fontSize};font-weight:${fontWeight};color:${color};padding:4px 8px;border:1px solid #d2d2d7;border-radius:6px;outline:none;background:#fff;margin:${field === 'sceneName' ? '-2px -6px 4px' : '-2px -6px'};transition:border-color .15s" onfocus="this.style.borderColor='#0071e3'" onblur="this.style.borderColor='#d2d2d7'" value="${(currentVal||'').replace(/"/g,'&quot;')}" placeholder="${field === 'sceneDesc' ? (I18N.t('dashboard.desc_optional')||'描述（可选）') : ''}">`;
    const input = document.getElementById(inputId);
    if (input) { input.focus(); input.select(); }
    // 点击外部自动保存
    setTimeout(function() {
      function _saveHandler(e) {
        if (e.target === input) return;
        document.removeEventListener('mousedown', _saveHandler);
        const newVal = (input || {}).value || '';
        const trimmed = newVal.trim();
        // 恢复显示
        const wrap = input.parentNode;
        if (wrap) {
          const newDisplay = document.createElement('div');
          newDisplay.id = displayId;
          newDisplay.style.cssText = `font-size:${fontSize};font-weight:${fontWeight};color:${color};cursor:text;padding:2px 6px;margin:${field === 'sceneName' ? '-2px -6px 4px' : '-2px -6px'};border-radius:6px;transition:background .15s`;
          newDisplay.onmouseover = function() { this.style.background='rgba(0,0,0,.04)'; };
          newDisplay.onmouseout = function() { this.style.background='transparent'; };
          newDisplay.onclick = function() { _enterInlineEdit(field); };
          newDisplay.innerHTML = trimmed || (field === 'sceneDesc' ? '<span style="color:#c7c7cc;font-style:italic">' + (I18N.t('dashboard.desc_optional')||'描述（可选）') + '</span>' : '&nbsp;');
          wrap.replaceChild(newDisplay, input);
        }
        // 如果有变化才保存
        if (trimmed !== currentVal) {
          if (field === 'sceneName' && !trimmed) { app.showToast(I18N.t('dashboard.please_input_scene_name')||'请输入场景名称'); return; }
          API.get('/scenes/with-pipeline?repo=' + encodeURIComponent(owner + '/' + name)).then(d => {
            const sc = (d.scenes || []).find(s => s.id === scene.id);
            const pipeNodes = ((sc && sc.pipeline && sc.pipeline.nodes) || []).filter(n => n.enabled !== false);
            const payload = { nodes: pipeNodes };
            if (field === 'sceneName') payload.name = trimmed;
            else payload.description = trimmed;
            API.put('/scenes/' + encodeURIComponent(scene.id) + '?repo=' + encodeURIComponent(owner + '/' + name), payload).then(() => {
              app.showToast(I18N.t('dashboard.scene_saved')||'场景已保存');
              if (field === 'sceneName') sceneName = trimmed; else sceneDesc = trimmed;
            }).catch(err => {
              // 回滚 UI 到旧值
              if (newDisplay) {
                newDisplay.innerHTML = currentVal || (field === 'sceneDesc' ? '<span style="color:#c7c7cc;font-style:italic">' + (I18N.t('dashboard.desc_optional')||'描述（可选）') + '</span>' : '&nbsp;');
              }
              app.showToast(I18N.t('dashboard.save_failed', {msg: (err.detail?.message || err.message)}));
            });
          }).catch(err => { app.showToast('加载场景数据失败: ' + (err.message||'')); });
        }
      }
      document.addEventListener('mousedown', _saveHandler);
    }, 100);
  };

  // 切换执行流程编辑模式（原地）
  window._toggleFlowEdit = function(sceneId, owner, name) {
    API.get('/dashboard/' + owner + '/' + name).then(dashData => {
      dashData._scenes = window._dashboardData?._scenes || [];
      window._dashboardData = dashData;
      window._currentSceneId = sceneId;
      // 获取当前场景的 pipeline nodes 传给编辑模式
      API.get('/scenes/with-pipeline?repo=' + encodeURIComponent(owner + '/' + name)).then(pipeData => {
        const sc = (pipeData.scenes || []).find(s => s.id === sceneId);
        const originalNodes = (sc && sc.pipeline && sc.pipeline.nodes) || [];
        _renderSceneEditMode(sceneId, dashData, owner, name, originalNodes);
      }).catch(() => {
        _renderSceneEditMode(sceneId, dashData, owner, name, null);
      });
    }).catch(() => {});
  };

  // 内联高级选项函数
  window._toggleNewBranchInline = function() {
    var el = document.getElementById('newBranchInputInline');
    if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
  };

  window._addCustomFieldInline = function(key, value) {
    var container = document.getElementById('advCustomFieldsInline');
    if (!container) return;
    var row = document.createElement('div');
    row.className = 'adv-kv';
    row.innerHTML = '<input class="adv-kv-key" placeholder="key" value="' + (key || '') + '">' +
      '<input class="adv-kv-val" placeholder="value" value="' + (value || '') + '">' +
      '<button class="adv-del" onclick="this.parentElement.remove()">×</button>';
    container.appendChild(row);
  };

  window._createNewBranchInline = function(owner, name) {
    var branchName = (document.getElementById('newBranchNameInline') || {}).value || '';
    var refs = (document.getElementById('newBranchRefInline') || {}).value || 'main';
    if (!branchName.trim()) { app.showToast && app.showToast(I18N.t('dashboard.branch_name_empty')||'分支名不能为空'); return; }
    API.post('/scenes/branches/' + owner + '/' + name, { branch_name: branchName.trim(), refs: refs })
      .then(function() { app.showToast && app.showToast(I18N.t('dashboard.branch_created')||'分支已创建'); return API.get('/scenes/branches/' + owner + '/' + name); })
      .then(function(b) {
        var branches = (b && b.branches) || [];
        var sel = document.getElementById('advWorkBranchInline');
        sel.innerHTML = branches.map(function(br) { var s = br.name === branchName.trim() ? ' selected' : ''; return '<option value="' + br.name + '"' + s + '>' + br.name + (br.protected ? ' 🔒' : '') + '</option>'; }).join('');
        var refSel = document.getElementById('newBranchRefInline');
        refSel.innerHTML = branches.map(function(br) { return '<option value="' + br.name + '">' + br.name + '</option>'; }).join('');
        document.getElementById('newBranchNameInline').value = '';
        window._toggleNewBranchInline();
      })
      .catch(function(e) { app.showToast && app.showToast((e && e.detail && e.detail.message) || (e && e.message) || '创建失败'); });
  };

  window._saveAdvancedOptionsInline = function(sceneId, repoParam) {
    var workBranch = (document.getElementById('advWorkBranchInline') || {}).value || '';
    if (!workBranch) { app.showToast && app.showToast(I18N.t('dashboard.branch_required')||'工作分支不能为空'); return; }
    var fields = [];
    document.querySelectorAll('#advCustomFieldsInline .adv-kv').forEach(function(row) {
      var k = (row.querySelector('.adv-kv-key') || {}).value || '';
      var v = (row.querySelector('.adv-kv-val') || {}).value || '';
      if (k.trim()) fields.push({ key: k.trim(), value: v.trim() });
    });
    API.put('/scenes/' + sceneId + '/advanced-options?repo=' + encodeURIComponent(repoParam), { work_branch: workBranch, custom_fields: fields })
      .then(function() { app.showToast && app.showToast(I18N.t('dashboard.adv_options_saved')||'高级选项已保存'); })
      .catch(function(e) { app.showToast && app.showToast((e && e.detail && e.detail.message) || (e && e.message) || '保存失败'); });
  };

  // 详情页操作函数
  window._deleteSceneFromDetail = function(sceneId) {
    const sn = scene.display_name || scene.name;
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    overlay.innerHTML = `<div style="background:#fff;border-radius:18px;padding:28px;min-width:320px;max-width:400px;box-shadow:0 12px 48px rgba(0,0,0,.15)">
      <h3 style="margin:0 0 12px;font-size:17px;font-weight:600;color:#1d1d1f">${I18N.t('dashboard.confirm_delete_scene')||'确认删除场景'}</h3>
      <p style="margin:0 0 20px;font-size:13px;color:#86868b;line-height:1.5">${sn}</p>
      <div style="display:flex;justify-content:flex-end;gap:8px">
        <button class="ap-btn" onclick="this.closest('[style*=fixed]').remove()">${I18N.t('common.cancel')||'取消'}</button>
        <button class="ap-btn ap-btn-d" id="confirmDeleteSceneBtn2">${I18N.t('common.delete')||'删除'}</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);
    document.getElementById('confirmDeleteSceneBtn2').onclick = function() {
      API.del('/scenes/' + encodeURIComponent(sceneId) + '?repo=' + encodeURIComponent(owner + '/' + name)).then(() => {
        window._dashboardEditing = false;
        overlay.remove();
        app.showToast(I18N.t('dashboard.scene_deleted')||'场景已删除');
        app.nav('dash');
      }).catch(e => { overlay.remove(); app.showToast(I18N.t('dashboard.delete_failed', {msg: (e.detail?.message || e.message)})); });
    };
  };
}

// 渲染执行流程卡片（查看模式只显示 enabled 的 agent）
function _renderFlowCards(nodes, agents, agentEngines, agentOrder, agentLabels, colors, descs, editMode, editNodes) {
  let html = '';
  // 查看模式：过滤掉 enabled=false 的节点
  const displayNodes = editMode ? nodes : nodes.filter(n => n.enabled !== false);
  displayNodes.forEach((node, i) => {
    const tag = node.tag || node.id || '';
    const meta = _DASH_AGENT_META[tag] || {};
    const bgColor = meta.color || colors[tag] || '#0071e3';
    const skIcon = meta.icon || '◈';
    const skLabel = meta.label || tag;
    const agent = agents.find(a => a.agent_type === tag) || {};
    const skillName = node.skill_display_name || node.skill || agent.skill_display_name || agent.skill || '';
    const engineIds = node.engine_runtime_ids || agent.engine_runtime_ids || [];
    const modeIcon = {emit:'→',waterfall:'◇',serial:'↻',parallel:'⇄'}[node.mode] || '→';
    const en = editNodes.find(n => n.id === tag);
    const enabled = editMode ? (en ? en.enabled : true) : (node.enabled !== false);

    if (editMode) {
      // 编辑模式：用 renderAgentCards 渲染
      return;
    }

    html += `<div class="ap-nd" style="${enabled ? '' : 'opacity:.35;filter:grayscale(1)'}">
      <div class="ap-nd-top" style="background:${bgColor}"></div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="width:22px;height:22px;border-radius:6px;background:${bgColor}22;color:${bgColor};font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0">${i+1}</span>
        <span style="font-size:14px;font-weight:600;color:#1d1d1f;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${skIcon} ${skLabel}</span>
      </div>
      <div class="ap-nd-desc">${descs[tag] || meta.description || ''}</div>
      <div style="height:1px;background:#e8e8ed;margin:8px 0"></div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;margin-bottom:8px;display:flex;flex-direction:column;align-items:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">${I18N.t('dashboard.label_skill')||'技能'}</div>
        ${skillName ? `<span style="font-size:11px;font-weight:500;color:#1d1d1f">${skillName}</span>` : '<span style="font-size:11px;color:#86868b">none</span>'}
      </div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">${I18N.t('dashboard.label_engine')||'引擎'}</div>
        ${engineIds.length > 0
          ? `<span style="display:inline-flex;align-items:center;gap:4px;background:#0071e314;color:#0071e3;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.engine_count', {count: engineIds.length})}</span>`
          : `<span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.broadcast')||'广播'}</span>`
        }
      </div>
    </div>`;
    if (i < displayNodes.length - 1) {
      html += `<span style="font-size:18px;color:#0071e3;font-weight:700;align-self:center;margin:0 4px;flex-shrink:0">→</span>`;
    }
  });
  return html;
}


// ── 场景编辑模式（只替换执行流程区域，保持页面其他部分不变）──
function _renderSceneEditMode(sceneId, dashData, owner, name, originalNodes) {
  window._dashboardEditing = true;
  window._currentSceneId = sceneId;
  const agents = dashData.agents || [];
  const agentEngines = dashData.agent_engines || {};
  // 用原始 nodes（来自 pipeline_config）而不是 dashData.scene_nodes
  // 这样 agent 数量和场景配置一致
  const sceneNodes = originalNodes || dashData.scene_nodes || [];

  // 按 sceneNodes 构建 editNodes，但始终包含全部5个agent（禁用的置灰不删除）
  const allAgentTypes = ['vod','issue','coding','qa','sre'];
  let editNodes = allAgentTypes.map(at => {
    const node = sceneNodes.find(n => (n.tag || n.id) === at);
    const ag = agents.find(a => a.agent_type === at) || {};
    if (node) {
      return {
        id: at, tag: at,
        skill: node.skill || ag.skill || ag.skill_display_name || '',
        mode: node.mode || 'emit',
        next: node.next || 'done',
        handoff: node.handoff || 'a2a',
        skip: node.skip || [],
        enabled: node.enabled !== false,
        engine_runtime_ids: node.engine_runtime_ids || ag.engine_runtime_ids || [],
      };
    }
    // 场景中不存在的 agent（被删除的）→ 置灰显示
    return {
      id: at, tag: at,
      skill: '', mode: 'emit', next: 'done', handoff: 'a2a',
      skip: [], enabled: false, engine_runtime_ids: [],
    };
  });

  // 只替换执行流程标题行和卡片区域
  const headerDiv = document.getElementById('flowHeader');
  if (headerDiv) {
    headerDiv.innerHTML = `<h2 style="font-size:15px;font-weight:600">${I18N.t('skills.execution_flow')||'执行流程'}</h2>
      <div style="display:flex;gap:8px">
        <button class="ap-btn" onclick="_saveSceneEdit('${sceneId}','${owner}','${name}')">${I18N.t('common.save')||'保存'}</button>
        <button class="ap-btn" onclick="_cancelSceneEdit('${sceneId}','${owner}','${name}')">${I18N.t('common.cancel')||'取消'}</button>
      </div>`;
  }

  // 替换卡片区域为编辑模式（保留箭头）
  const cardsContainer = document.getElementById('flowCardsContainer');
  if (cardsContainer) {
    cardsContainer.innerHTML = _renderEditFlowCards(editNodes, agents, agentEngines, sceneNodes);
  }

  // 编辑操作函数
  window.toggleAgentInScene = function(agentType) {
    const node = editNodes.find(n => n.id === agentType);
    if (!node) return;
    node.enabled = !node.enabled;
    if (!node.enabled) { node.skill = ''; node.engine_runtime_ids = []; }
    if (cardsContainer) {
      cardsContainer.innerHTML = _renderEditFlowCards(editNodes, agents, agentEngines, sceneNodes);
    }
  };

  window.selectAgentSkill = function(agentType, skillName) {
    const node = editNodes.find(n => n.id === agentType);
    if (node) node.skill = skillName;
  };

  window.openEngineModal = function(agentType) {
    _openEngineModalImpl(agentType, editNodes, agents, agentEngines, owner, name, sceneId);
  };

  // 保存编辑 — 只刷新执行流程区域
  window._saveSceneEdit = function(sceneId, owner, name) {
    const emptyAgents = editNodes.filter(n => n.enabled && !n.skill);
    if (emptyAgents.length) {
      const names = emptyAgents.map(n => { const meta = _AGENT_TYPES[n.id] || {}; return meta.display_name || n.id; }).join('、');
      app.showToast(I18N.t('dashboard.skill_empty_warning', {agents: names}));
      return;
    }
    const nodesPayload = editNodes.filter(n => n.enabled).map((n, i, arr) => {
      const nextAgent = i < arr.length - 1 ? arr[i + 1].id : 'done';
      return { id: n.id, tag: n.tag, skill: n.skill || '', mode: n.mode || 'emit', next: nextAgent, handoff: i < arr.length - 1 ? 'a2a' : 'complete', skip: n.skip || [], enabled: true, engine_runtime_ids: n.engine_runtime_ids || [] };
    });
    API.put('/scenes/' + encodeURIComponent(sceneId) + '?repo=' + encodeURIComponent(owner + '/' + name), { nodes: nodesPayload }).then(() => {
      window._dashboardEditing = false;
      app.showToast(I18N.t('dashboard.scene_saved')||'场景已保存');
      // 只重新加载执行流程数据，不刷新整个页面
      API.get('/scenes/with-pipeline?repo=' + encodeURIComponent(owner + '/' + name)).then(d => {
        const sc = (d.scenes || []).find(s => s.id === sceneId);
        const newNodes = (sc && sc.pipeline && sc.pipeline.nodes) || [];
        // 从编辑模式的 select options 构建 skill_name → display_name 映射
        const skillDisplayMap = {};
        document.querySelectorAll('.ap-skill-select option, select[onchange*="selectAgentSkill"] option').forEach(opt => {
          if (opt.value) skillDisplayMap[opt.value] = opt.textContent;
        });
        // 用 newNodes 的 skill + 映射构建临时 agents（保留引擎等其余字段）
        const refreshAgents = agents.map(a => {
          const node = newNodes.find(n => (n.tag || n.id) === a.agent_type);
          const sk = node ? node.skill : a.skill;
          return Object.assign({}, a, {
            skill: sk || a.skill,
            skill_display_name: (sk && skillDisplayMap[sk]) || (sk === a.skill ? a.skill_display_name : '') || sk || a.skill_display_name || a.skill
          });
        });
        // 恢复标题行
        if (headerDiv) {
          headerDiv.innerHTML = `<h2 style="font-size:15px;font-weight:600">${I18N.t('skills.execution_flow')||'执行流程'}</h2>
            <div style="display:flex;gap:8px">
              <button class="ap-btn" id="editFlowBtn" onclick="_toggleFlowEdit('${sceneId}','${owner}','${name}')">${I18N.t('dashboard.edit')||'编辑'}</button>
            </div>`;
        }
        // 恢复查看模式卡片
        if (cardsContainer) {
          const _agentOrder = newNodes.map(n => n.tag || n.id);
          const _agentLabels = _agentOrder.map(t => (_DASH_AGENT_META[t]||{}).label || t);
          const _colors = {vod:'#ff2d92',issue:'#ff3b30',coding:'#0071e3',qa:'#af52de',sre:'#ff9f0a'};
          const _descs = {vod:I18N.t('dashboard.desc_vod'),issue:I18N.t('dashboard.desc_issue'),coding:I18N.t('dashboard.desc_coding'),qa:I18N.t('dashboard.desc_qa'),sre:I18N.t('dashboard.desc_sre')};
          cardsContainer.innerHTML = _renderFlowCards(newNodes, refreshAgents, agentEngines, _agentOrder, _agentLabels, _colors, _descs, false, []);
        }
      }).catch(() => {});
    }).catch(e => { window._dashboardEditing = false; app.showToast(I18N.t('dashboard.save_failed', {msg: (e.detail?.message || e.message)})); });
  };

  // 取消编辑 — 只恢复执行流程区域
  window._cancelSceneEdit = function(sceneId, owner, name) {
    window._dashboardEditing = false;
    // 恢复标题行
    if (headerDiv) {
      headerDiv.innerHTML = `<h2 style="font-size:15px;font-weight:600">${I18N.t('skills.execution_flow')||'执行流程'}</h2>
        <div style="display:flex;gap:8px">
          <button class="ap-btn" id="editFlowBtn" onclick="_toggleFlowEdit('${sceneId}','${owner}','${name}')">${I18N.t('dashboard.edit')||'编辑'}</button>
        </div>`;
    }
    // 恢复查看模式卡片
    if (cardsContainer) {
      const _agentOrder = sceneNodes.map(n => n.tag || n.id);
      const _agentLabels = _agentOrder.map(t => (_DASH_AGENT_META[t]||{}).label || t);
      const _colors = {vod:'#ff2d92',issue:'#ff3b30',coding:'#0071e3',qa:'#af52de',sre:'#ff9f0a'};
      const _descs = {vod:I18N.t('dashboard.desc_vod'),issue:I18N.t('dashboard.desc_issue'),coding:I18N.t('dashboard.desc_coding'),qa:I18N.t('dashboard.desc_qa'),sre:I18N.t('dashboard.desc_sre')};
      cardsContainer.innerHTML = _renderFlowCards(sceneNodes, agents, agentEngines, _agentOrder, _agentLabels, _colors, _descs, false, []);
    }
  };
}

// 渲染编辑模式的流程卡片（带箭头，引擎按钮在下方）
function _renderEditFlowCards(editNodes, agents, agentEngines, sceneNodes) {
  const colors = {vod:'#B07A8B',issue:'#B8857A',coding:'#7A92B0',qa:'#9A85A8',sre:'#B89B6A'};
  const descs = {vod:I18N.t('dashboard.desc_vod'),issue:I18N.t('dashboard.desc_issue'),coding:I18N.t('dashboard.desc_coding'),qa:I18N.t('dashboard.desc_qa'),sre:I18N.t('dashboard.desc_sre')};
  const canToggle = (tag) => tag !== 'vod' && tag !== 'issue';
  let html = '';
  editNodes.forEach((node, i) => {
    const tag = node.id || node.tag;
    const meta = _DASH_AGENT_META[tag] || {};
    const bgColor = meta.color || colors[tag] || '#0071e3';
    const skIcon = meta.icon || '◈';
    const skLabel = meta.label || tag;
    const ag = agents.find(a => a.agent_type === tag) || {};
    const skillDisplay = node.skill_display_name || node.skill || ag.skill_display_name || ag.skill || '';
    const engineIds = node.engine_runtime_ids || [];
    const enabled = node.enabled !== false;

    let toggleBtn = '';
    if (canToggle(tag)) {
      toggleBtn = `<span onclick="event.stopPropagation();toggleAgentInScene('${tag}')" style="cursor:pointer;font-size:14px;color:${enabled ? '#ff3b30' : '#34c759'};margin-left:auto;padding:2px 6px" title="${enabled ? I18N.t('dashboard.disable_agent') : I18N.t('dashboard.enable_agent')}">${enabled ? '✕' : '✓'}</span>`;
    }

    // Skill 下拉框（禁用时显示已停用）
    let skillHtml;
    if (enabled) {
      skillHtml = `<select onchange="selectAgentSkill('${tag}', this.value)" class="ap-skill-select" style="width:100%;padding:6px 10px;border:1px solid #e8e8ed;border-radius:8px;font-size:11px;background:#f5f5f7;color:#1d1d1f;outline:none;cursor:pointer;transition:all .15s" onfocus="this.style.borderColor='#0071e3';this.style.background='#fff'" onblur="this.style.borderColor='#e8e8ed';this.style.background='#f5f5f7'">
        <option value="">${I18N.t('dashboard.select_skill')}</option>
      </select>`;
      setTimeout(() => {
        const cr = app.currentRepo();
        const repoParam = cr && cr.fullName ? '?repo=' + encodeURIComponent(cr.fullName) : '';
        API.get('/scenes/skills/' + tag + repoParam).then(r => {
          const sels = document.querySelectorAll(`select[onchange*="${tag}"]`);
          sels.forEach(sel => {
            if (sel && r.skills) {
              sel.innerHTML = '<option value="">' + I18N.t('dashboard.select_skill') + '</option>' +
                r.skills.map(s => `<option value="${s.skill_name}"${s.skill_name === node.skill ? ' selected' : ''}>${s.display_name}</option>`).join('');
            }
          });
        }).catch(() => {});
      }, 50);
    } else {
      skillHtml = `<span style="font-size:11px;color:#86868b">${I18N.t('dashboard.agent_disabled')||'已停用'}</span>`;
    }

    // Engine 区域 — 药丸标签在上，按钮在下
    let engineHtml;
    if (enabled) {
      const badge = engineIds.length > 0
        ? `<span style="display:inline-flex;align-items:center;gap:4px;background:#0071e314;color:#0071e3;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.engine_count', {count: engineIds.length})}</span>`
        : `<span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.broadcast')||'广播'}</span>`;
      engineHtml = `<div style="display:flex;flex-direction:column;align-items:center;gap:6px;width:100%">
        ${badge}
        <button onclick="openEngineModal('${tag}')" style="font-size:10px;padding:4px 12px;border:1px solid #e8e8ed;border-radius:8px;background:transparent;color:#6e6e73;cursor:pointer;transition:all .15s" onmouseover="this.style.borderColor='#0071e3';this.style.color='#0071e3'" onmouseout="this.style.borderColor='#e8e8ed';this.style.color='#6e6e73'">${I18N.t('dashboard.select_engine')||'选择引擎'}</button>
      </div>`;
    } else {
      engineHtml = `<span style="font-size:11px;color:#86868b">none</span>`;
    }

    html += `<div class="ap-nd" style="${enabled ? '' : 'opacity:.35;filter:grayscale(1)'}">
      <div class="ap-nd-top" style="background:${bgColor}"></div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="width:22px;height:22px;border-radius:6px;background:${bgColor}22;color:${bgColor};font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0">${i+1}</span>
        <span style="font-size:14px;font-weight:600;color:#1d1d1f;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${skIcon} ${skLabel}</span>
        ${toggleBtn}
      </div>
      ${enabled ? `<div class="ap-nd-desc">${descs[tag] || meta.description || ''}</div>` : `<div style="font-size:10px;color:#86868b;margin-bottom:8px">${I18N.t('dashboard.not_needed_in_scene')||'此场景下不需要'}</div>`}
      <div style="height:1px;background:#e8e8ed;margin:8px 0"></div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;margin-bottom:8px;display:flex;flex-direction:column;align-items:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">${I18N.t('dashboard.label_skill')||'技能'}</div>
        ${skillHtml}
      </div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">${I18N.t('dashboard.label_engine')||'引擎'}</div>
        ${engineHtml}
      </div>
    </div>`;
    // 保留箭头
    if (i < editNodes.length - 1) {
      html += `<span style="font-size:18px;color:#0071e3;font-weight:700;align-self:center;margin:0 4px;flex-shrink:0">→</span>`;
    }
  });
  return html;
}

// 引擎 modal 实现（从原有逻辑提取，参数化）
function _openEngineModalImpl(agentType, editNodes, agents, agentEngines, owner, name, sceneId) {
  const allEngines = (agentEngines[agentType] || []).filter(e => e.status === 'online' || e.status === 'recently_lost');
  const node = editNodes.find(n => n.id === agentType);
  if (!node) return;
  let currentIds = node.engine_runtime_ids || [];

  const _ENG_TAG_COLORS = {all:'#6366f1',vod:'#ec4899',issue:'#ef4444',coding:'#3b82f6',qa:'#a855f7',sre:'#f59e0b'};
  const _tagBadge = (tags) => (tags||[]).map(t => `<span style="font-size:9px;padding:2px 7px;border-radius:999px;color:#fff;background:${_ENG_TAG_COLORS[t]||'#999'};margin-left:4px;font-weight:500">${t==='all'?'All':t.charAt(0).toUpperCase()+t.slice(1)}</span>`).join('');
  const _SCOPE_COLORS = {global:'#6366f1',user:'#34c759',repo:'#f59e0b',local:'#86868b'};
  const _scopeBadge = (s) => s ? `<span style="font-size:9px;padding:2px 7px;border-radius:999px;color:#fff;background:${_SCOPE_COLORS[s]||'#999'};margin-left:4px;font-weight:500">${s==='global'?'全局':s==='user'?'个人':s==='repo'?'仓库':s==='local'?'本机':s}</span>` : '';
  const _SCOPE_ORDER = {user:0, repo:1, global:2, local:3};
  const meta = _AGENT_TYPES[agentType] || {};
  const title = meta.display_name || agentType;
  const sorted = allEngines.slice().sort((a,b) => (_SCOPE_ORDER[a.scope]||9) - (_SCOPE_ORDER[b.scope]||9));
  let _engFilterType = '';
  let _engFilterScope = '';

  function _renderEngineList(filter) {
    currentIds = (window._engCurIds || []).slice();
    const q = (filter || '').toLowerCase().trim();
    const filtered = sorted.filter(e => {
      if (q) { const hay = [e.name, e.alias, e.device, e.cred_name, e.scope_label, (e.tags||[]).join(' ')].join(' ').toLowerCase(); if (!hay.includes(q)) return false; }
      if (_engFilterType) { const ename = (e.name || '').toLowerCase(); if (!ename.includes(_engFilterType)) return false; }
      if (_engFilterScope && (e.scope || 'global') !== _engFilterScope) return false;
      return true;
    });
    const groups = {};
    filtered.forEach(e => { const s = e.scope || 'global'; if (!groups[s]) groups[s] = []; groups[s].push(e); });
    const _SCOPE_LABELS = {global:'全局', user:'个人', repo:'仓库', local:'本机'};
    const _SCOPE_ORDER2 = ['user','repo','global','local'];
    let html = '';
    if (filtered.length === 0) return `<div style="padding:20px 0;text-align:center;color:#86868b;font-size:13px">${I18N.t('dashboard.no_engine_broadcast')||'暂无可用引擎，将使用广播模式'}</div>`;
    _SCOPE_ORDER2.forEach(s => {
      if (!groups[s]) return;
      html += `<div style="margin-top:12px;margin-bottom:4px;font-size:11px;font-weight:600;color:#86868b;text-transform:uppercase;letter-spacing:.5px">${_SCOPE_LABELS[s]||s} (${groups[s].length})</div>`;
      html += groups[s].map(e => {
        const checked = currentIds.includes(e.id);
        return `<label style="display:flex;align-items:center;gap:12px;padding:12px 16px;border:none;border-radius:14px;cursor:pointer;background:${checked ? 'rgba(0,113,227,.06)' : '#f5f5f7'};transition:all .2s ease;box-shadow:0 1px 2px rgba(0,0,0,.03)">
          <input type="checkbox" data-eid="${e.id}" ${checked ? 'checked' : ''} onchange="if(this.checked){window._engCurIds.push(${e.id})}else{window._engCurIds=window._engCurIds.filter(x=>x!==${e.id})}" style="width:18px;height:18px;cursor:pointer;accent-color:#0071e3">
          <div style="flex:1;min-width:0"><div style="font-weight:600;font-size:13px;color:#1d1d1f;display:flex;align-items:center;flex-wrap:wrap">${e.alias || e.name}${e.alias ? '<span style="font-size:10px;color:#86868b;font-weight:400;margin-left:4px">(' + e.name + ')</span>' : ''}${_tagBadge(e.tags)}${_scopeBadge(e.scope)}</div><div style="font-size:10px;color:#86868b;margin-top:3px">${e.device || ''}${e.cred_name ? ' · ' + e.cred_name : ''}</div></div>
          <span style="font-size:11px;font-weight:500;color:${e.status === 'recently_lost' ? '#f59e0b' : '#34c759'}">${e.status === 'recently_lost' ? '失联' : I18N.t('dashboard.online')||'在线'}</span>
        </label>`;
      }).join('');
    });
    return html;
  }

  window._engCurIds = currentIds.slice();
  const _availTypes = [...new Set(sorted.map(e => { const n = (e.name || '').toLowerCase(); if (n.includes('hermes')) return 'hermes'; if (n.includes('opencode')) return 'opencode'; if (n.includes('codearts')) return 'codearts'; return ''; }).filter(Boolean))];
  const _availScopes = [...new Set(sorted.map(e => e.scope || 'global'))];
  const _SCOPE_LABELS_ALL = {global:'全局', user:'个人', repo:'仓库', local:'本机'};
  const _SCOPE_ORDER_ALL = ['local','user','repo','global'];

  function _renderFilterTags() {
    let html = '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;flex-shrink:0">';
    _availTypes.forEach(t => { const act = _engFilterType === t; html += `<span onclick="window._engToggleType('${t}')" style="cursor:pointer;font-size:11px;padding:3px 10px;border-radius:999px;border:1px solid ${act?'#0071e3':'#e8e8ed'};color:${act?'#fff':'#1d1d1f'};background:${act?'#0071e3':'#f5f5f7'};font-weight:500;transition:all .15s">${t}</span>`; });
    if (_availTypes.length > 0) html += '<span style="width:1px;background:#e8e8ed;margin:2px 4px"></span>';
    _SCOPE_ORDER_ALL.forEach(s => { if (!_availScopes.includes(s)) return; const act = _engFilterScope === s; html += `<span onclick="window._engToggleScope('${s}')" style="cursor:pointer;font-size:11px;padding:3px 10px;border-radius:999px;border:1px solid ${act?'#0071e3':'#e8e8ed'};color:${act?'#fff':'#1d1d1f'};background:${act?'#0071e3':'#f5f5f7'};font-weight:500;transition:all .15s">${_SCOPE_LABELS_ALL[s]||s}</span>`; });
    html += '<span style="flex:1"></span>';
    html += `<span onclick="window._engClearAll()" style="cursor:pointer;font-size:11px;padding:3px 10px;border-radius:999px;border:1px solid #e8e8ed;color:#86868b;background:#f5f5f7;font-weight:500;transition:all .15s" onmouseover="this.style.color='#d32f2f'" onmouseout="this.style.color='#86868b'">全不选</span>`;
    html += '</div>';
    return html;
  }

  window._engToggleType = function(t) { _engFilterType = (_engFilterType === t) ? '' : t; _refreshEngAll(); };
  window._engToggleScope = function(s) { _engFilterScope = (_engFilterScope === s) ? '' : s; _refreshEngAll(); };
  window._engClearAll = function() { window._engCurIds = []; _refreshEngAll(); };

  function _refreshEngAll() {
    const tagsEl = document.getElementById('engFilterTags'); if (tagsEl) tagsEl.innerHTML = _renderFilterTags();
    const c = document.getElementById('engListContainer'); if (c) c.innerHTML = window._engRenderFn(document.getElementById('engSearchInput') ? document.getElementById('engSearchInput').value : '');
  }

  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  overlay.innerHTML = `<div id="engModalBox" style="background:rgba(255,255,255,.85);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border:none;border-radius:18px;padding:28px;width:460px;height:600px;box-shadow:0 8px 32px rgba(0,0,0,.12);display:flex;flex-direction:column">
    <h3 style="margin:0 0 4px;font-size:17px;font-weight:600;color:#1d1d1f;letter-spacing:-.3px">${I18N.t('dashboard.select_engine')||'选择引擎'} — ${title}</h3>
    <div style="font-size:12px;color:#86868b;margin-bottom:12px">${I18N.t('dashboard.engines_available', {count: allEngines.length})||'可用引擎: '+allEngines.length}</div>
    <div id="engFilterTags" style="flex-shrink:0">${_renderFilterTags()}</div>
    <div style="margin-bottom:12px;flex-shrink:0"><input id="engSearchInput" placeholder="搜索引擎名称/设备/标签..." style="width:100%;padding:8px 12px;border:none;border-radius:12px;font-size:13px;background:#f5f5f7;color:#1d1d1f;outline:none"></div>
    <div id="engListContainer" style="display:flex;flex-direction:column;gap:4px;flex:1;overflow-y:auto;min-height:0">${_renderEngineList('')}</div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px;flex-shrink:0">
      <button class="ap-btn" onclick="this.closest('[style*=fixed]').remove()">${I18N.t('common.cancel')||'取消'}</button>
      <button class="ap-btn" id="confirmEngineBtn2">${I18N.t('common.confirm')||'确认'}</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);

  const _engInput = document.getElementById('engSearchInput');
  let _engComposing = false;
  _engInput.addEventListener('compositionstart', function() { _engComposing = true; });
  _engInput.addEventListener('compositionend', function() { _engComposing = false; _refreshEngAll(); });
  _engInput.addEventListener('input', function() { if (!_engComposing) _refreshEngList(); });
  function _refreshEngList() { const c = document.getElementById('engListContainer'); if (c) c.innerHTML = window._engRenderFn(_engInput.value); }
  window._engRenderFn = _renderEngineList;

  document.getElementById('confirmEngineBtn2').onclick = function() {
    node.engine_runtime_ids = window._engCurIds.slice();
    overlay.remove();
    const cardsEl = document.getElementById('flowCardsContainer');
    if (cardsEl) {
      // 重新渲染编辑卡片（用 _renderEditFlowCards 保持与编辑模式一致的结构）
      cardsEl.innerHTML = _renderEditFlowCards(editNodes, agents, agentEngines, editNodes);
    }
  };
  overlay.addEventListener('click', function(ev) { if (ev.target === overlay) overlay.remove(); });
}

// ── 创建新场景（从卡片网格的"创建"卡片触发）──
let _isCreateMode = false;
let _pendingSceneName = '';
let _pendingSceneDesc = '';

window.enterCreateMode = function() {
  const data = window._dashboardData || {};
  let owner = '', name = '';
  try { const cr = app.currentRepo(); if (cr) { owner = cr.owner; name = cr.name; } } catch(e) {}
  if (!owner || !name) { const parts = (data.repo || '').replace(/^gitcode:/, '').split('/'); owner = parts[0]||''; name = parts[1]||''; }

  const agents = data.agents || [];
  const agentEngines = data.agent_engines || {};
  const allAgentTypes = ['vod','issue','coding','qa','sre'];
  // 默认全部启用，skill 为空
  let editNodes = allAgentTypes.map(at => ({ id: at, tag: at, skill: '', mode: 'emit', next: 'done', handoff: 'a2a', skip: [], enabled: true, engine_runtime_ids: [] }));
  _isCreateMode = true;
  window._dashboardEditing = true;

  const el = document.getElementById('pageContent');
  if (!el) return;
  const catBg = 'bg-default';
  let _createSceneIcon = '📦';
  const icon = _createSceneIcon;
  const colors = {vod:'#B07A8B',issue:'#B8857A',coding:'#7A92B0',qa:'#9A85A8',sre:'#B89B6A'};
  const _detailMorandi = ['#8B9DB0','#A8B8A0','#C4A882','#B0A0B8','#9BB0C4','#C49B9B'];
  const _detailIconColor = _detailMorandi[0];
  const descs = {vod:I18N.t('dashboard.desc_vod'),issue:I18N.t('dashboard.desc_issue'),coding:I18N.t('dashboard.desc_coding'),qa:I18N.t('dashboard.desc_qa'),sre:I18N.t('dashboard.desc_sre')};
  const canToggle = (tag) => tag !== 'vod' && tag !== 'issue';

  let html = `<style>
    .ap-btn{padding:7px 18px;border-radius:999px;font-size:12px;font-weight:500;cursor:pointer;transition:all .2s ease;background:transparent;border:1px solid rgba(0,113,227,.3);color:#0071e3}
    .ap-btn:hover{background:#0071e3;border-color:#0071e3;color:#fff}
    .ap-btn-d{border:1px solid rgba(255,59,48,.3);color:#ff3b30}
    .ap-btn-d:hover{background:#ff3b30;border-color:#ff3b30;color:#fff}
    .ap-grid{display:flex;gap:12px;padding:0;overflow-x:auto;min-height:280px}
    .ap-nd{flex:1 1 0;min-width:0;max-width:260px;min-height:260px;background:rgba(255,255,255,.6);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:none;border-radius:18px;padding:18px;display:flex;flex-direction:column;transition:all .25s ease;position:relative;box-shadow:0 1px 4px rgba(0,0,0,.05)}
    .ap-nd:hover{background:rgba(255,255,255,.8);box-shadow:0 3px 16px rgba(0,0,0,.08)}
    .ap-nd-top{width:36px;height:4px;border-radius:2px;margin-bottom:12px}
    .ap-nd-name{font-size:14px;font-weight:600;color:#1d1d1f;margin-bottom:6px;display:flex;align-items:center;gap:8px}
    .ap-nd-desc{font-size:11px;color:#86868b;line-height:1.5;margin-bottom:10px;font-weight:400}
    .det-card-wrap{background:rgba(255,255,255,.6);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:none;border-radius:18px;padding:24px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.05)}
    .scn-applied-badge{font-size:11px;font-weight:500;padding:5px 14px;border-radius:999px;background:rgba(52,199,89,.1);color:#34c759;display:inline-flex;align-items:center;gap:3px;border:1px solid rgba(52,199,89,.2)}
    select{appearance:none;-webkit-appearance:none;-moz-appearance:none;background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' fill='none'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%2386868b' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 10px center;padding-right:28px!important;border-radius:8px;cursor:pointer}
    select option{padding:8px 12px;background:#fff;color:#1d1d1f;font-size:12px}
    select option:checked{background:#0071e3;color:#fff}
    select option:hover{background:#f0f4ff;color:#0071e3}
    select::-ms-expand{display:none}
    select:focus{border-color:#0071e3!important}
  </style>`;

  // 返回按钮
  html += `<button class="ap-btn" onclick="window._dashboardEditing=false;app.nav('dash')" style="margin-bottom:12px">← ${I18N.t('dashboard.scene_center_title')||'项目场景'}</button>`;

  // 场景名称 + 描述 + 图标选择（直接 input，不需要点击切换）
  const _iconPickerHtml = _CUSTOM_SCENE_ICONS.map(ic =>
    `<span class="icon-pick${ic === _createSceneIcon ? ' active' : ''}" onclick="_pickCreateIcon('${ic}')" style="font-size:20px;width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all .15s;${ic === _createSceneIcon ? 'background:#0071e3;color:#fff' : 'background:#f5f5f7'}">${ic}</span>`
  ).join('');
  html += `<div class="cd" style="padding:20px 24px;margin-bottom:16px">
    <div style="display:flex;align-items:flex-start;gap:16px">
      <div class="scn-row-icon" id="createSceneIconDisplay" style="width:40px;height:40px;border-radius:10px;font-size:18px;cursor:pointer;background:${_detailIconColor};display:flex;align-items:center;justify-content:center;flex-shrink:0;color:#fff;font-weight:600" onclick="document.getElementById('iconPickerPanel').style.display=document.getElementById('iconPickerPanel').style.display==='none'?'flex':'none'">${icon}</div>
      <div style="flex:1;min-width:0">
        <input id="createSceneName" placeholder="${I18N.t('dashboard.scene_name_placeholder')||'请输入场景名称'}" style="width:100%;font-size:20px;font-weight:700;padding:4px 8px;border:1px solid #d2d2d7;border-radius:6px;outline:none;background:#fff;margin-bottom:6px;transition:border-color .15s" onfocus="this.style.borderColor='#0071e3'" onblur="this.style.borderColor='#d2d2d7'">
        <input id="createSceneDesc" placeholder="${I18N.t('dashboard.desc_optional')||'描述（可选）'}" style="width:100%;font-size:13px;padding:4px 8px;border:1px solid #d2d2d7;border-radius:6px;outline:none;background:#fff;color:#86868b;transition:border-color .15s" onfocus="this.style.borderColor='#0071e3'" onblur="this.style.borderColor='#d2d2d7'">
        <div id="iconPickerPanel" style="display:none;flex-wrap:wrap;gap:6px;margin-top:8px;padding:8px;background:#f5f5f7;border-radius:10px">${_iconPickerHtml}</div>
      </div>
      <button class="ap-btn" style="margin-top:4px" onclick="_saveCreateScene('${owner}','${name}')">${I18N.t('common.save')||'保存'}</button>
    </div>
  </div>`;

  // 执行流程（直接编辑模式）
  html += `<div class="det-card-wrap" id="flowSection">
    <div id="flowHeader" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h2 style="font-size:15px;font-weight:600">${I18N.t('skills.execution_flow')||'执行流程'}</h2>
    </div>
    <div class="ap-grid" style="padding:0" id="flowCardsContainer">`;
  html += _renderEditFlowCards(editNodes, agents, agentEngines, editNodes);
  html += `</div></div>`;

  // 高级选项
  html += `<div class="det-card-wrap" id="advOptionsInline">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
      <h2 style="font-size:15px;font-weight:600">${I18N.t('dashboard.advanced_options')||'高级选项'}</h2>
    </div>
    <div class="adv-row" style="display:flex;align-items:center;gap:12px;padding:10px 0">
      <span style="font-size:12px;font-weight:600;color:#86868b;min-width:80px;flex-shrink:0">${I18N.t('dashboard.work_branch')||'工作分支'}</span>
      <select id="advWorkBranchInline" style="flex:1;max-width:280px;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;background:#f5f5f7;color:#1d1d1f;outline:none;cursor:pointer;transition:all .15s" onfocus="this.style.borderColor='#0071e3';this.style.background='#fff'" onblur="this.style.borderColor='#e8e8ed';this.style.background='#f5f5f7'"><option value="main">main</option></select>
      <button class="ap-btn" onclick="_toggleNewBranchInline()">${I18N.t('dashboard.create_branch')||'新建分支'}</button>
    </div>
    <div id="newBranchInputInline" style="display:none;padding:8px 0 4px 92px">
      <div style="display:flex;gap:8px">
        <input id="newBranchNameInline" placeholder="${I18N.t('dashboard.branch_name')||'分支名'}" style="flex:1;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;outline:none;background:#f5f5f7;transition:all .15s" onfocus="this.style.borderColor='#0071e3';this.style.background='#fff'" onblur="this.style.borderColor='#e8e8ed';this.style.background='#f5f5f7'">
        <select id="newBranchRefInline" style="width:120px;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;background:#f5f5f7;outline:none;cursor:pointer;transition:all .15s" onfocus="this.style.borderColor='#0071e3';this.style.background='#fff'" onblur="this.style.borderColor='#e8e8ed';this.style.background='#f5f5f7'"><option value="main">main</option></select>
        <button class="ap-btn" onclick="_createNewBranchInline('${owner}','${name}')">${I18N.t('common.create')||'创建'}</button>
        <button class="ap-btn" onclick="_toggleNewBranchInline()">${I18N.t('common.cancel')||'取消'}</button>
      </div>
    </div>
    <div style="padding:12px 0 4px">
      <div style="font-size:12px;font-weight:600;color:#86868b;margin-bottom:8px">${I18N.t('dashboard.custom_config')||'自定义配置'}</div>
      <div id="advCustomFieldsInline" style="display:flex;flex-direction:column;gap:8px"></div>
      <button class="ap-btn" style="margin-top:8px;font-size:11px" onclick="_addCustomFieldInline()">+ ${I18N.t('dashboard.add_config')||'添加配置项'}</button>
    </div>
  </div>`;

  el.innerHTML = html;

  // 图标选择
  window._pickCreateIcon = function(ic) {
    _createSceneIcon = ic;
    const display = document.getElementById('createSceneIconDisplay');
    if (display) display.textContent = ic;
    document.querySelectorAll('.icon-pick').forEach(el => {
      const isActive = el.textContent === ic;
      el.style.background = isActive ? '#0071e3' : '#f5f5f7';
      el.style.color = isActive ? '#fff' : '';
      el.classList.toggle('active', isActive);
    });
  };

  // 加载分支列表
  API.get('/scenes/branches/' + owner + '/' + name).then(function(b) {
    var branches = (b && b.branches) || [];
    var sel = document.getElementById('advWorkBranchInline');
    if (sel && branches.length) sel.innerHTML = branches.map(function(br) { return '<option value="' + br.name + '">' + br.name + (br.protected ? ' 🔒' : '') + '</option>'; }).join('');
    var refSel = document.getElementById('newBranchRefInline');
    if (refSel && branches.length) refSel.innerHTML = branches.map(function(br) { return '<option value="' + br.name + '">' + br.name + '</option>'; }).join('');
  }).catch(function() {});

  // 编辑操作函数
  window.toggleAgentInScene = function(agentType) {
    const node = editNodes.find(n => n.id === agentType);
    if (!node) return;
    node.enabled = !node.enabled;
    if (!node.enabled) { node.skill = ''; node.engine_runtime_ids = []; }
    const cardsContainer = document.getElementById('flowCardsContainer');
    if (cardsContainer) cardsContainer.innerHTML = _renderEditFlowCards(editNodes, agents, agentEngines, editNodes);
  };

  window.selectAgentSkill = function(agentType, skillName) {
    const node = editNodes.find(n => n.id === agentType);
    if (node) node.skill = skillName;
  };

  window.openEngineModal = function(agentType) {
    _openEngineModalImpl(agentType, editNodes, agents, agentEngines, owner, name, '');
  };

  window._toggleNewBranchInline = function() {
    var el2 = document.getElementById('newBranchInputInline');
    if (el2) el2.style.display = el2.style.display === 'none' ? 'block' : 'none';
  };

  window._addCustomFieldInline = function(key, value) {
    var container = document.getElementById('advCustomFieldsInline');
    if (!container) return;
    var row = document.createElement('div');
    row.className = 'adv-kv';
    row.style.cssText = 'display:flex;gap:8px;align-items:center';
    row.innerHTML = '<input class="adv-kv-key" placeholder="key" style="flex:0 0 120px;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:12px;outline:none;background:#f5f5f7;transition:all .15s" onfocus="this.style.borderColor=\'#0071e3\';this.style.background=\'#fff\'" onblur="this.style.borderColor=\'#e8e8ed\';this.style.background=\'#f5f5f7\'" value="' + (key || '') + '">' +
      '<input class="adv-kv-val" placeholder="value" style="flex:1;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:12px;outline:none;background:#f5f5f7;transition:all .15s" onfocus="this.style.borderColor=\'#0071e3\';this.style.background=\'#fff\'" onblur="this.style.borderColor=\'#e8e8ed\';this.style.background=\'#f5f5f7\'" value="' + (value || '') + '">' +
      '<button class="ap-btn" style="color:#ff3b30;border-color:#e8e8ed;width:28px;height:28px;padding:0;font-size:14px" onclick="this.parentElement.remove()">×</button>';
    container.appendChild(row);
  };

  window._createNewBranchInline = function(owner, name) {
    var branchName = (document.getElementById('newBranchNameInline') || {}).value || '';
    var refs = (document.getElementById('newBranchRefInline') || {}).value || 'main';
    if (!branchName.trim()) { app.showToast && app.showToast(I18N.t('dashboard.branch_name_empty')||'分支名不能为空'); return; }
    API.post('/scenes/branches/' + owner + '/' + name, { branch_name: branchName.trim(), refs: refs })
      .then(function() { app.showToast && app.showToast(I18N.t('dashboard.branch_created')||'分支已创建'); return API.get('/scenes/branches/' + owner + '/' + name); })
      .then(function(b) {
        var branches = (b && b.branches) || [];
        var sel = document.getElementById('advWorkBranchInline');
        if (sel) sel.innerHTML = branches.map(function(br) { var s = br.name === branchName.trim() ? ' selected' : ''; return '<option value="' + br.name + '"' + s + '>' + br.name + (br.protected ? ' 🔒' : '') + '</option>'; }).join('');
        var refSel = document.getElementById('newBranchRefInline');
        if (refSel) refSel.innerHTML = branches.map(function(br) { return '<option value="' + br.name + '">' + br.name + '</option>'; }).join('');
        document.getElementById('newBranchNameInline').value = '';
        window._toggleNewBranchInline();
      })
      .catch(function(e) { app.showToast && app.showToast((e && e.detail && e.detail.message) || (e && e.message) || '创建失败'); });
  };

  // 保存创建场景
  window._saveCreateScene = function(owner, name) {
    const sceneNameVal = (document.getElementById('createSceneName') || {}).value || '';
    if (!sceneNameVal.trim()) { app.showToast(I18N.t('dashboard.please_input_scene_name')||'请输入场景名称'); return; }
    const sceneDescVal = (document.getElementById('createSceneDesc') || {}).value || '';
    const emptyAgents = editNodes.filter(n => n.enabled && !n.skill);
    if (emptyAgents.length) {
      const names = emptyAgents.map(n => { const meta = _AGENT_TYPES[n.id] || {}; return meta.display_name || n.id; }).join('、');
      app.showToast(I18N.t('dashboard.skill_empty_warning', {agents: names}));
      return;
    }
    const nodesPayload = editNodes.filter(n => n.enabled).map((n, i, arr) => {
      const nextAgent = i < arr.length - 1 ? arr[i + 1].id : 'done';
      return { id: n.id, tag: n.tag, skill: n.skill || '', mode: 'emit', next: nextAgent, handoff: i < arr.length - 1 ? 'a2a' : 'complete', skip: [], enabled: true, engine_runtime_ids: n.engine_runtime_ids || [] };
    });
    API.post('/scenes?repo=' + encodeURIComponent(owner + '/' + name), { name: sceneNameVal.trim(), description: sceneDescVal.trim(), scope: 'repo', nodes: nodesPayload, icon: _createSceneIcon }).then(r => {
      window._dashboardEditing = false;
      // 收集高级选项
      var workBranch = (document.getElementById('advWorkBranchInline') || {}).value || 'main';
      var fields = [];
      document.querySelectorAll('#advCustomFieldsInline .adv-kv').forEach(function(row) {
        var k = (row.querySelector('.adv-kv-key') || {}).value || '';
        var v = (row.querySelector('.adv-kv-val') || {}).value || '';
        if (k.trim()) fields.push({ key: k.trim(), value: v.trim() });
      });
      // 有高级选项就一并保存，然后刷新 dashboard 跳项目场景
      var advPromise = (workBranch || fields.length) ?
        API.put('/scenes/' + encodeURIComponent(r.scene_id) + '/advanced-options?repo=' + encodeURIComponent(owner + '/' + name), { work_branch: workBranch, custom_fields: fields }) :
        Promise.resolve();
      advPromise.then(function() {
        app.showToast(I18N.t('dashboard.scene_created')||'场景创建成功');
        // 刷新 dashboard 数据后跳项目场景
        API.get('/dashboard/' + owner + '/' + name).then(dashData => {
          return API.get('/scenes?repo=' + encodeURIComponent(owner + '/' + name)).then(scenesData => {
            dashData._scenes = scenesData.scenes || [];
            dashData._owner = owner;
            dashData._name = name;
            window._dashboardData = dashData;
            app.nav('dash');
          });
        }).catch(() => { app.nav('dash'); });
      }).catch(function(e) {
        app.showToast(I18N.t('dashboard.scene_created')||'场景创建成功');
        app.nav('dash');
      });
    }).catch(e => { app.showToast(I18N.t('dashboard.save_failed', {msg: (e.detail?.message || e.message)})); });
  };
};


// 加载高级选项到详情页内联区域
function _loadAdvancedOptionsInline(sceneId, owner, name) {
  API.get('/scenes/' + sceneId + '/advanced-options?repo=' + encodeURIComponent(owner + '/' + name)).then(function(d) {
    var sel = document.getElementById('advWorkBranchInline');
    if (sel) {
      sel.innerHTML = '<option value="' + (d.work_branch || 'main') + '">' + (d.work_branch || 'main') + '</option>';
      API.get('/scenes/branches/' + owner + '/' + name).then(function(b) {
        var branches = (b && b.branches) || [];
        if (branches.length) {
          sel.innerHTML = branches.map(function(br) {
            var s = br.name === (d.work_branch || 'main') ? ' selected' : '';
            return '<option value="' + br.name + '"' + s + '>' + br.name + (br.protected ? ' 🔒' : '') + '</option>';
          }).join('');
        }
        var refSel = document.getElementById('newBranchRefInline');
        if (refSel) refSel.innerHTML = branches.map(function(br) { return '<option value="' + br.name + '">' + br.name + '</option>'; }).join('');
        if (d.work_branch && refSel) refSel.value = d.work_branch;
      }).catch(function() {});
    }
    var fields = d.custom_fields || [];
    var container = document.getElementById('advCustomFieldsInline');
    if (container) {
      container.innerHTML = '';
      fields.forEach(function(f) { window._addCustomFieldInline(f.key, f.value); });
    }
  }).catch(function() {});
}



function renderAgentCards(a, agentOrder, agentLabels, colors, descs, agentEngines, isCustom, editMode, editNodes, engineMode) {
  let html = '';
  agentOrder.forEach((at, i) => {
    const ag = a.find(x => x.agent_type === at) || {};
    const meta = _AGENT_TYPES[at] || {};
    const isEditing = editMode || engineMode;
    const node = isEditing ? editNodes.find(n => n.id === at) : null;
    const enabled = node ? node.enabled : (ag.enabled_in_scene !== false);
    const skill = node ? (node.skill || '') : (ag.skill || '');
    const skillDisplay = node ? (node.skill_display_name || node.skill || ag.skill_display_name || ag.skill || '') : (ag.skill_display_name || ag.skill || '');
    const engineIds = node ? (node.engine_runtime_ids || []) : (ag.engine_runtime_ids || []);
    const accentColor = meta.color || colors[at] || '#86868b';
    // 语言包优先于后端 description（后端返回中文时英文模式用前端翻译）
    const desc = descs[at] || meta.description || '';
    const canToggle = at !== 'vod' && at !== 'issue';

    let toggleBtn = '';
    if (editMode && canToggle) {
      toggleBtn = `<span onclick="event.stopPropagation();toggleAgentInScene('${at}')" style="cursor:pointer;font-size:14px;color:${enabled ? '#ff3b30' : '#34c759'};margin-left:auto;padding:2px 6px" title="${enabled ? I18N.t('dashboard.disable_agent') : I18N.t('dashboard.enable_agent')}">${enabled ? '✕' : '✓'}</span>`;
    }

    let skillHtml;
    if (editMode && enabled) {
      skillHtml = `<select onchange="selectAgentSkill('${at}', this.value)" style="width:100%;padding:6px 10px;border:1px solid #e8e8ed;border-radius:8px;font-size:11px;background:#f5f5f7;color:#1d1d1f;outline:none;cursor:pointer;transition:all .15s" onfocus="this.style.borderColor='#0071e3';this.style.background='#fff'" onblur="this.style.borderColor='#e8e8ed';this.style.background='#f5f5f7'">
        <option value="">${I18N.t('dashboard.select_skill')}</option>
      </select>`;
      setTimeout(() => {
        const cr = app.currentRepo();
        const repoParam = cr && cr.fullName ? '?repo=' + encodeURIComponent(cr.fullName) : '';
        API.get('/scenes/skills/' + at + repoParam).then(r => {
          const sel = document.querySelector(`select[onchange*="${at}"]`);
          if (sel && r.skills) {
            const current = editNodes.find(n => n.id === at)?.skill || '';
            sel.innerHTML = '<option value="">' + I18N.t('dashboard.select_skill') + '</option>' +
              r.skills.map(s => `<option value="${s.skill_name}"${s.skill_name === current ? ' selected' : ''}>${s.display_name}</option>`).join('');
          }
        }).catch(() => {});
      }, 50);
    } else if (enabled && skill) {
      skillHtml = `<span class="ap-nd-val">${skillDisplay}</span>`;
    } else {
      skillHtml = `<span style="font-size:11px;color:#86868b">none</span>`;
    }

    // Engine 区域 — 居中药丸标签 + 编辑按钮
    let engineHtml;
    if ((editMode || engineMode) && enabled) {
      if (engineIds.length > 0) {
        engineHtml = `<div class="ap-nd-eng" style="gap:6px">
          <span style="display:inline-flex;align-items:center;gap:4px;background:#0071e314;color:#0071e3;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.engine_count', {count: engineIds.length})}</span>
          <button onclick="openEngineModal('${at}')" style="font-size:10px;padding:3px 10px;border:1px solid #d2d2d7;border-radius:8px;background:transparent;color:#6e6e73;cursor:pointer;transition:all .15s" onmouseover="this.style.borderColor='#0071e3';this.style.color='#0071e3'" onmouseout="this.style.borderColor='#d2d2d7';this.style.color='#6e6e73'">${I18N.t('dashboard.change_engine')}</button>
        </div>`;
      } else {
        engineHtml = `<div class="ap-nd-eng" style="gap:6px">
          <span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.broadcast')}</span>
          <button onclick="openEngineModal('${at}')" style="font-size:10px;padding:3px 10px;border:1px solid #d2d2d7;border-radius:8px;background:transparent;color:#6e6e73;cursor:pointer;transition:all .15s" onmouseover="this.style.borderColor='#0071e3';this.style.color='#0071e3'" onmouseout="this.style.borderColor='#d2d2d7';this.style.color='#6e6e73'">${I18N.t('dashboard.select_engine')}</button>
        </div>`;
      }
    } else if (enabled && engineIds.length > 0) {
      engineHtml = `<div class="ap-nd-eng"><span style="display:inline-flex;align-items:center;gap:4px;background:#0071e314;color:#0071e3;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.engine_count', {count: engineIds.length})}</span></div>`;
    } else if (enabled) {
      engineHtml = `<div class="ap-nd-eng"><span style="display:inline-flex;align-items:center;gap:4px;background:#34c75914;color:#34c759;font-size:11px;font-weight:600;padding:4px 12px;border-radius:999px">${I18N.t('dashboard.broadcast')}</span></div>`;
    } else {
      engineHtml = `<div class="ap-nd-eng"><span style="font-size:11px;color:#86868b">none</span></div>`;
    }

    html += `<div class="ap-nd" style="${enabled ? '' : 'opacity:.35;filter:grayscale(1)'}">
      <div class="ap-nd-top" style="background:${accentColor}"></div>
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="width:22px;height:22px;border-radius:6px;background:${accentColor}22;color:${accentColor};font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0">${i + 1}</span>
        <span style="font-size:14px;font-weight:600;color:#1d1d1f;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${meta.display_name || agentLabels[i]}</span>
        ${toggleBtn}
      </div>
      ${enabled ? `<div class="ap-nd-desc">${desc}</div>` : `<div style="font-size:10px;color:#86868b;margin-bottom:8px">${I18N.t('dashboard.not_needed_in_scene')}</div>`}
      <div style="height:1px;background:#e8e8ed;margin:8px 0"></div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;margin-bottom:8px;display:flex;flex-direction:column;align-items:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">${I18N.t('dashboard.label_skill')}</div>
        ${skillHtml}
      </div>
      <div style="background:#fff;border:1px solid #e8e8ed;border-radius:10px;padding:8px 10px;flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center">
        <div style="font-size:8px;color:#86868b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px">${I18N.t('dashboard.label_engine')}</div>
        ${engineHtml}
      </div>
    </div>`;
  });
  return html;
}

// 方案 F-3：托管自愈（告警横幅「重检修复」按钮）
window._repairHost = function () {
  let owner = '', name = '';
  try {
    const cr = app.currentRepo();
    if (cr) { owner = cr.owner; name = cr.name; }
  } catch (e) {}
  if (!owner || !name) return;
  API.post('/repos/' + owner + '/' + name + '/repair-host', {})
    .then(function (d) {
      if (d && d.status === 'ok') {
        app.showToast && app.showToast('托管已修复');
        location.reload();
      } else {
        app.showToast && app.showToast((d && d.message) || '修复失败，请稍后手动重试');
      }
    })
    .catch(function (e) {
      app.showToast && app.showToast('修复请求失败：' + (e && (e.detail && e.detail.message || e.message) || '未知错误'));
    });
};

// ===== 智能推荐规则弹窗 =====
window._openSceneRecommendModal = function(repoParam) {
  var owner = repoParam.split('/')[0];
  var name = repoParam.split('/')[1];
  if (!owner || !name) return;

  // 移除已有弹窗
  var existing = document.getElementById('sceneRecommendOverlay');
  if (existing) existing.remove();

  // 创建 overlay
  var ov = document.createElement('div');
  ov.id = 'sceneRecommendOverlay';
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.25);z-index:9999;display:flex;align-items:center;justify-content:center;animation:fadeIn .15s';

  var modal = document.createElement('div');
  modal.className = 'rdp-glass';
  modal.style.cssText = 'width:680px;max-height:85vh;overflow-y:auto;background:rgba(255,255,255,0.92);backdrop-filter:blur(28px) saturate(200%);-webkit-backdrop-filter:blur(28px) saturate(200%);border-radius:18px;border:1px solid rgba(255,255,255,0.6);box-shadow:0 12px 40px rgba(0,0,0,0.12),0 2px 8px rgba(0,0,0,0.06);padding:28px;animation:rdpPop .25s cubic-bezier(0.34,1.56,0.64,1)';

  modal.innerHTML = '\
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">\
      <h2 style="font-size:15px;font-weight:600">' + (I18N.t('dashboard.scene_recommend_rules')||'智能推荐规则') + '</h2>\
      <button class="ap-btn" style="padding:2px 8px;font-size:16px" onclick="document.getElementById(\'sceneRecommendOverlay\').remove()">×</button>\
    </div>\
    <div style="margin-bottom:20px">\
      <div style="font-size:13px;font-weight:600;color:#1d1d1f;margin-bottom:8px">' + (I18N.t('dashboard.recommend_fallback')||'兜底场景') + '</div>\
      <select id="recFallbackScene" style="width:100%;padding:8px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;background:#f5f5f7;color:#1d1d1f;outline:none;cursor:pointer"><option value="">' + (I18N.t('common.loading')||'加载中...') + '</option></select>\
      <div style="font-size:11px;color:#86868b;margin-top:4px">' + (I18N.t('dashboard.recommend_fallback_hint')||'当任务无法精确匹配任何规则时，走此场景') + '</div>\
    </div>\
    <div style="border-top:1px solid rgba(0,0,0,.06);padding-top:16px">\
      <div style="font-size:13px;font-weight:600;color:#1d1d1f;margin-bottom:8px">' + (I18N.t('dashboard.recommend_rules')||'场景功能') + '</div>\
      <div id="recRulesContainer" style="display:flex;flex-direction:column;gap:8px"></div>\
      <button class="ap-btn" style="margin-top:8px;font-size:11px" onclick="_addRecRuleRow()">+ ' + (I18N.t('dashboard.recommend_add_rule')||'添加规则') + '</button>\
      <div style="font-size:11px;color:#86868b;margin-top:8px">' + (I18N.t('dashboard.recommend_rules_hint')||'key 从当前仓库已应用场景中选择，value 描述什么情况下推荐使用该场景') + '</div>\
    </div>\
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:20px">\
      <button class="ap-btn" onclick="document.getElementById(\'sceneRecommendOverlay\').remove()">' + (I18N.t('common.cancel')||'取消') + '</button>\
      <button class="ap-btn" onclick="_saveSceneRecommendConfig(\'' + owner + '\',\'' + name + '\')">' + (I18N.t('common.save')||'保存') + '</button>\
    </div>';

  ov.appendChild(modal);
  var _downOnOv = false;
  ov.addEventListener('mousedown', function(e) { _downOnOv = (e.target === ov); });
  ov.addEventListener('click', function(e) { if (e.target === ov && _downOnOv) ov.remove(); });
  document.body.appendChild(ov);

  // 加载场景列表 + 已有配置
  var sceneOptions = '';
  API.get('/scenes/with-pipeline?repo=' + encodeURIComponent(owner + '/' + name)).then(function(d) {
    var scenes = (d && d.scenes) || [];
    sceneOptions = scenes.map(function(s) {
      var disp = s.display_name || s.name || s.id;
      return '<option value="' + s.id + '">' + disp + '</option>';
    }).join('');
    var fallbackSel = document.getElementById('recFallbackScene');
    if (fallbackSel) fallbackSel.innerHTML = sceneOptions;
    // 加载已有配置
    return API.get('/repos/' + owner + '/' + name + '/scene-recommend-config');
  }).then(function(cfg) {
    if (!cfg) cfg = {fallback_scene: '', rules: []};
    // 设置兜底场景
    var fallbackSel = document.getElementById('recFallbackScene');
    if (fallbackSel && cfg.fallback_scene) fallbackSel.value = cfg.fallback_scene;
    // 如果没有兜底场景但有场景列表，默认选第一个
    if ((!cfg.fallback_scene || !cfg.fallback_scene) && sceneOptions) {
      var firstId = (cfg.rules && cfg.rules.length > 0) ? '' : '';
      // 从 sceneOptions 提取第一个 scene_id
      var m = sceneOptions.match(/value="([^"]+)"/);
      if (m && fallbackSel) {
        fallbackSel.value = m[1];
      }
    }
    // 填充规则
    var container = document.getElementById('recRulesContainer');
    if (container) container.innerHTML = '';
    (cfg.rules || []).forEach(function(r) {
      _addRecRuleRow(r.scene_id, r.condition);
    });
  }).catch(function(e) {
    app.showToast && app.showToast('加载推荐配置失败: ' + ((e && e.message) || ''));
  });

  // 设置全局函数
  window._addRecRuleRow = function(sceneId, condition) {
    var container = document.getElementById('recRulesContainer');
    if (!container) return;
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:8px;align-items:center';
    var selOpts = sceneOptions;
    row.innerHTML = '\
      <select class="rec-rule-scene" style="flex:0 0 200px;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;background:#f5f5f7;outline:none;cursor:pointer">' + selOpts + '</select>\
      <input class="rec-rule-cond" placeholder="' + (I18N.t('dashboard.recommend_condition_value')||'推荐条件') + '" value="' + (condition||'').replace(/"/g,'&quot;') + '" style="flex:1;padding:7px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:12px;outline:none;background:#f5f5f7;transition:all .15s" onfocus="this.style.borderColor=\'#0071e3\';this.style.background=\'#fff\'" onblur="this.style.borderColor=\'#e8e8ed\';this.style.background=\'#f5f5f7\'">\
      <button class="ap-btn ap-btn-d" style="padding:4px 8px;font-size:14px" onclick="this.parentElement.remove()">×</button>';
    container.appendChild(row);
    if (sceneId) {
      var sel = row.querySelector('.rec-rule-scene');
      if (sel) sel.value = sceneId;
    }
  };

  window._saveSceneRecommendConfig = function(owner, name) {
    var fallback = (document.getElementById('recFallbackScene') || {}).value || '';
    if (!fallback) { app.showToast && app.showToast(I18N.t('dashboard.recommend_fallback_required')||'兜底场景不能为空'); return; }
    var rules = [];
    var seen = {};
    document.querySelectorAll('#recRulesContainer > div').forEach(function(row) {
      var sid = (row.querySelector('.rec-rule-scene') || {}).value || '';
      var cond = (row.querySelector('.rec-rule-cond') || {}).value || '';
      if (sid && cond.trim()) {
        if (seen[sid]) { app.showToast && app.showToast(I18N.t('dashboard.recommend_rule_dup')||'同一场景不能有多条规则'); return; }
        seen[sid] = true;
        rules.push({scene_id: sid, condition: cond.trim()});
      }
    });
    API.put('/repos/' + owner + '/' + name + '/scene-recommend-config', {fallback_scene: fallback, rules: rules})
      .then(function() {
        app.showToast && app.showToast(I18N.t('dashboard.recommend_saved')||'智能推荐规则已保存');
        document.getElementById('sceneRecommendOverlay').remove();
      })
      .catch(function(e) { app.showToast && app.showToast((e && e.detail && e.detail.message) || (e && e.message) || '保存失败'); });
  };
};

