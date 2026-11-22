// ═══════════════════════════════════════════════════════════════
// repo_key 前缀统一剥离（对应后端 src/repo_key.py 的 from_repo_key）
// 外部/前端约定一律无前缀；dashboard 响应可能带 gitcode:/github: 前缀，需剥离
// ═══════════════════════════════════════════════════════════════
function stripRepoPrefix(s) {
  // 前缀集合以运行时平台目录为准（服务端注入 __REPO_PLATFORMS__），缺目录时回退已知四家
  // 仓库全称里不会出现 ':'，所以「开头是 [a-z0-9_-]+ 后接 : 且后面还有 / 」就是前缀段；
  // 这样不必再枚举平台（原实现写死 gitcode|github|gitlab，gitee 前缀漏剥）。
  var str = String(s || '');
  var i = str.indexOf(':');
  if (i > 0 && /^[a-z][a-z0-9_-]*$/.test(str.slice(0, i)) && str.slice(i + 1).indexOf('/') > 0) {
    return str.slice(i + 1);
  }
  return str;
}
// 平台显示名统一走 platform-icons.js（平台目录驱动，含 Gitee/GitLab）；缺则回退 key
function platformLabelOf(k) {
  return (typeof window.platformLabel === 'function' && window.platformLabel(k)) || k;
}
window.stripRepoPrefix = stripRepoPrefix;

// ═══════════════════════════════════════════════════════════════
// 场景自定义环境变量（custom_fields）前端共享校验规则（Issue #236）
// 供 dashboard.js / scene-market.js 复用：大写校验 + 保留冲突校验 + 密钥识别
// ═══════════════════════════════════════════════════════════════
const _CF_RESERVED_ENV_NAMES = [
  'PATH','HOME','USER','USERNAME','SHELL','LANG','LANGUAGE','LC_ALL','LC_CTYPE',
  'PWD','OLDPWD','HOSTNAME','HOST','TERM','TMPDIR','TMP','TEMP','EDITOR','VISUAL',
  'SHLVL','PS1','OSTYPE','MACHTYPE','LOGNAME','MAIL','WINDIR','SYSTEMROOT','COMSPEC',
  'NUMBER_OF_PROCESSORS','PROCESSOR_ARCHITECTURE','REPO_FULL_NAME','ACCOUNT',
  'BOT_USERNAME','ISSUE_NUMBER','PR_NUMBER','GIT_ASKPASS','GRAPE_SERVER_URL',
  'GRAPE_WORK_BRANCH','GRAPE_SCENE_ID','GRAPE_SESSION_TOKEN','GRAPE_CLI_MODE',
  'BOT_GITCODE_TOKEN','USER_ID','CONVERSATION_ID','RUNTIME_SERVER_URL'
];
const _CF_SECRET_SUFFIXES = ['_KEY','_SK','_SECRET','_TOKEN','_PASSWORD','_PWD','_CREDENTIALS'];

function _cfIsReservedName(name) {
  const n = (name || '').trim().toUpperCase();
  return n.startsWith('GRAPE_') || _CF_RESERVED_ENV_NAMES.indexOf(n) >= 0;
}
function _cfIsUpperName(name) {
  return /^[A-Z][A-Z0-9_]*$/.test((name || '').trim());
}
function _cfIsSecretName(name) {
  const up = (name || '').trim().toUpperCase();
  return _CF_SECRET_SUFFIXES.some(s => up.endsWith(s));
}
function _cfNameError(name) {
  const n = (name || '').trim();
  if (!n) return '自定义环境变量名不能为空';
  if (!_cfIsUpperName(n)) return '自定义环境变量名需使用大写字母（如 MY_API_KEY），当前：' + n;
  if (_cfIsReservedName(n)) return '变量名 "' + n + '" 与系统保留环境变量冲突，请更换名称';
  return '';
}
function _cfComputeIsSecret(name, isSecret) {
  if (isSecret === true) return true;
  if (isSecret === false) return false;
  return _cfIsSecretName(name);
}
const _CF_SCOPE_AGENTS = ['all', 'vod', 'issue', 'coding', 'qa', 'sre'];
// scope checkbox 互斥联动：勾选 all → 取消其它；勾选具体 agent → 取消 all
// （不做 disabled 置灰，其它选项始终可选）
function _cfScopeToggle(cb) {
  const row = cb.closest('.adv-kv');
  if (!row) return;
  const boxes = row.querySelectorAll('.adv-kv-scope');
  if (cb.value === 'all') {
    if (cb.checked) boxes.forEach(b => { if (b.value !== 'all') b.checked = false; });
  } else {
    if (cb.checked) {
      const allBox = row.querySelector('.adv-kv-scope[value="all"]');
      if (allBox) allBox.checked = false;
    }
  }
}
// 渲染 scope 多选 checkbox 组，返回 HTML；scope 为数组（默认 ['all']）
function _cfScopeHtml(scope) {
  const sel = Array.isArray(scope) ? scope : [scope || 'all'];
  const hasAll = sel.indexOf('all') >= 0;
  return _CF_SCOPE_AGENTS.map(a => {
    const checked = (a === 'all') ? hasAll : (sel.indexOf(a) >= 0 && !hasAll);
    return '<label class="adv-kv-scope-lbl" style="font-size:11px;color:#86868b;display:inline-flex;align-items:center;gap:2px;white-space:nowrap">' +
      '<input type="checkbox" class="adv-kv-scope" value="' + a + '"' + (checked ? ' checked' : '') + ' onchange="_cfScopeToggle(this)">' + a + '</label>';
  }).join('');
}
// 从 .adv-kv 行容器收集 custom_fields：{name, value, is_secret, scope}
// 密钥标记由 value 输入框类型承载：type=password（隐藏/👁）→ 密钥；type=text（明文/🙈）→ 非密钥
function _cfCollectFields(prefix) {
  const rows = document.querySelectorAll(prefix + ' .adv-kv');
  const fields = [];
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const vEl = row.querySelector('.adv-kv-val');
    if (!vEl) continue;
    const nEl = row.querySelector('.adv-kv-key');
    const staticEl = row.querySelector('.adv-kv-key-static');
    const isPreset = !!row.querySelector('.adv-kv-preset');
    const n = nEl ? (nEl.value || '').trim() : (staticEl ? (staticEl.textContent || '').trim() : '');
    if (!n) continue;
    if (!isPreset) {
      const err = _cfNameError(n);
      if (err) return { error: err + '（第 ' + (i + 1) + ' 行）' };
    }
    const isSec = vEl.type === 'password';
    // Issue #885：密钥占位行（data-keep-value）值未变更 → 提交 keep_value，服务端保留旧值
    const keepValue = vEl.getAttribute('data-keep-value') === '1';
    // scope：互斥后选中即入池；什么都不选 → ["all"]（全量）
    const checked = Array.prototype.slice.call(row.querySelectorAll('.adv-kv-scope:checked'))
      .map(cb => cb.value);
    const scope = checked.length ? checked : ['all'];
    const item = { name: n, value: (vEl.value || '').trim(), is_secret: isSec, scope: scope, preset: isPreset };
    if (keepValue) item.keep_value = true;
    fields.push(item);
  }
  return { fields: fields };
}

// ═══════════════════════════════════════════════════════════════
// Issue #885：场景密钥类自定义变量明文按需取回（对齐 #474 credentials reveal）
// GET advanced-options 对 is_secret 字段已占位返回（value:"" + has_value），
// 前端对已配置未改动的密钥行标记 data-keep-value；点击眼睛按钮时走本流程：
// 二次确认（重输 Grape 密码）→ POST reveal → 明文当次回填并清除 keep 标记。
// 上下文 window._cfAdvCtx = { sceneId, repoParam } 由加载高级选项时设置。
// ═══════════════════════════════════════════════════════════════
window._cfRevealSecretField = function(input, btn) {
  if (!input) return;
  var row = input.closest ? input.closest('.adv-kv') : null;
  var nEl = row ? row.querySelector('.adv-kv-key') : null;
  var staticEl = row ? row.querySelector('.adv-kv-key-static') : null;
  var name = nEl ? (nEl.value || '').trim() : (staticEl ? (staticEl.textContent || '').trim() : '');
  var ctx = window._cfAdvCtx || {};
  if (!name || !ctx.sceneId || !ctx.repoParam) {
    if (window.app && app.showToast) app.showToast('当前上下文不支持取回明文');
    return;
  }
  var overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center';
  overlay.innerHTML = '<div style="background:#fff;border-radius:16px;padding:24px;min-width:300px;max-width:380px;box-shadow:0 12px 48px rgba(0,0,0,.15)">' +
    '<div style="font-size:15px;font-weight:600;color:#1d1d1f;margin-bottom:8px">查看明文：' + name + '</div>' +
    '<div style="font-size:12px;color:#86868b;margin-bottom:12px">查看密钥明文需重新输入 Grape 密码确认，读取动作将记入审计日志。</div>' +
    '<input id="cfRevealPwd" type="password" placeholder="Grape 密码" style="width:100%;padding:8px 12px;border:1px solid #e8e8ed;border-radius:10px;font-size:13px;outline:none;background:#f5f5f7;margin-bottom:16px">' +
    '<div style="display:flex;justify-content:flex-end;gap:8px">' +
      '<button class="ap-btn" onclick="this.closest(\'[style*=fixed]\').remove()">取消</button>' +
      '<button class="ap-btn" id="cfRevealConfirmBtn">确认</button>' +
    '</div></div>';
  document.body.appendChild(overlay);
  var pwdInput = document.getElementById('cfRevealPwd');
  if (pwdInput) pwdInput.focus();
  document.getElementById('cfRevealConfirmBtn').onclick = function() {
    var pwd = (document.getElementById('cfRevealPwd') || {}).value || '';
    API.post('/scenes/' + encodeURIComponent(ctx.sceneId) + '/advanced-options/reveal?repo=' + encodeURIComponent(ctx.repoParam),
      { name: name, password: pwd })
      .then(function(d) {
        overlay.remove();
        input.value = (d && d.value) || '';
        input.type = 'text';
        input.removeAttribute('data-keep-value');
        if (btn) btn.textContent = '🙈';
      })
      .catch(function(e) {
        overlay.remove();
        if (window.app && app.showToast) app.showToast((e && e.detail && e.detail.message) || (e && e.message) || '取回明文失败');
      });
  };
};

const app = (() => {
  const $ = id => document.getElementById(id);
  // Issue #3395：插件市场前端入口开关——唯一数据源 = 服务端 shell 注入的
  // window.__FEATURES__（默认开；见 src/api/frontend_flags.py）。
  const PLUGINS_ENABLED = !(window.__FEATURES__ && window.__FEATURES__.plugins === false);
  // flag=0：从路由表过滤 'plugins'，刷新/hash 恢复不再进入插件市场。
  const VIEWS = ['dash','pipe','vod','issue','coding','qa','sre','chat','im-channel','settings','skills','knowledge','runtimes','event','repos','skill','tasks','observability','scene-market','scene-detail','my-feedback','my-feedback-detail','account','plugins','sandbox'].filter(function (view) {
    return view !== 'plugins' || PLUGINS_ENABLED;
  });

  // ── 非仓库视图清单（单一事实源）──
  // 历史上 app.js 内有 4 份内容不一致的拷贝（restore 198 / check 1125 / nav 1458 /
  // popstate 1785），漏改任意一份就是"视图守卫漂移"类 bug（如用户设置页弹引擎横幅）。
  // 收敛为一份并集常量：合法 VIEWS 之外的历史遗留视图（scene-detail-local /
  // autopilot / autopilot-detail / organizations）一并纳入，多排无害（它们都不
  // 依赖仓库上下文，横幅/守卫跳过它们语义正确）。
const NO_REPO_VIEWS = ['repos', 'settings', 'tasks', 'runtimes', 'event', 'scene-market', 'scene-detail', 'scene-detail-local', 'my-feedback', 'my-feedback-detail', 'autopilot', 'autopilot-detail', 'observability', 'organizations', 'org-pipeline', 'account', 'plugins', 'sandbox'];

  // ── 浏览器前进/后退支持 ──
  // nav()/goToRepos() 把视图写入 history（#view/param），popstate 回放渲染。
  // _replaying: popstate 回放时禁止再次入栈；_histReady: 首个视图用 replaceState，
  // 避免登录后第一次导航多出一个无意义的入口。
  let _replaying = false;
  let _histReady = false

  // 只切换仓库上下文，不触发 nav（用于「跳到目标仓的某个需求详情」：
  // 由调用方紧接着 nav('tasks', issue)，保证 history 只多一条，浏览器返回
  // 能回到来源需求，而不是先卡在目标仓 Dashboard）。
  function _applyRepoState(repo) {
    if (!repo || !repo.fullName) return null;
    const parts = String(repo.fullName).split('/');
    if (parts.length < 2) return null;
    const state = {
      fullName: String(repo.fullName),
      owner: repo.owner || parts[0],
      name: repo.name || parts[1],
      repoKey: repo.repoKey || repo.repo_key || '',
    };
    if (repo.isOrgRepo !== undefined) state.isOrgRepo = !!repo.isOrgRepo;
    if (repo.orgId !== undefined) state.orgId = repo.orgId;
    sessionStorage.setItem('currentRepo', JSON.stringify(state));
    if (state.orgId) localStorage.setItem('current_org_id', state.orgId);
    _syncAppPath(state.owner, state.name);
    const sel = $('repoSelect');
    if (sel) sel.value = state.fullName;
    _updateRepoOrgBadge();
    _updateTbOrgName();
    return state;
  }

  function _pushHistory(view, param) {
    if (_replaying) return;
    const hash = '#' + view + (param !== undefined && param !== null ? '/' + encodeURIComponent(param) : '');
    if (window.location.hash === hash) return; // 同一视图不重复入栈
    try {
      const fn = _histReady ? 'pushState' : 'replaceState';
      if (window.history[fn]) {
        // 把仓库上下文一并写入 history：跨仓跳转后按浏览器返回，先恢复来源仓库，
        // 再回放来源需求，避免「#tasks/20 在目标仓里查不到」。
        window.history[fn]({
          view: view,
          param: (param !== undefined && param !== null) ? param : null,
          repo: currentRepo(),
        }, '', hash);
        _histReady = true;
      }
    } catch (e) {}
  }

  // Issue #231: currentRepo 从 localStorage 迁移到 sessionStorage，实现页签级项目选择隔离。
  // localStorage 跨页签共享会导致页签2切换项目后页签1对话列表串扰；sessionStorage 页签级隔离，
  // 刷新保留、关闭清除，符合「每个页签独立维护当前选中项目」的期望。
  // 一次性迁移逻辑见 _migrateCurrentRepo()，在 init() 最先执行，兼容已有用户会话无感知。
  function _migrateCurrentRepo() {
    try {
      const inSession = sessionStorage.getItem('currentRepo');
      const inLocal = localStorage.getItem('currentRepo');
      if (!inSession && inLocal) {
        sessionStorage.setItem('currentRepo', inLocal);
        localStorage.removeItem('currentRepo');
      }
    } catch (e) {}
  }

  function currentRepo() {
    try {
      const r = JSON.parse(sessionStorage.getItem('currentRepo'));
      if (r && r.fullName) {
        // GitCode 仓库 name 是显示名（可≠URL path，如 shelltest 显示 skillstest）——
        // 所有 API 路径/链接都用 owner/name 拼接，必须强制 URL 语义（fullName 拆分），
        // 否则旧脏 name 导致 /api/pipeline/{owner}/{name} 查空（见 repo_manager.fetch_repos_with_role）
        const parts = String(r.fullName).split('/');
        if (parts.length === 2 && r.name !== parts[1]) {
          r.name = parts[1];
          sessionStorage.setItem('currentRepo', JSON.stringify(r));
        }
      }
      return r;
    } catch { return null; }
  }

  // ===== 组织仓库 / 组织项目 UI 判定 =====
  // /repos/managed 返回 is_org_repo；同时把 flags 缓存在内存，供刷新后
  // sessionStorage 里的 currentRepo 还没有 isOrgRepo 时兜底。
  function _isOrgRepo(repo) {
    if (!repo) return false;
    if (repo.isOrgRepo === true) return true;
    var fn = repo.fullName || '';
    return !!(fn && window._managedRepoFlags && window._managedRepoFlags[fn]);
  }

  function _currentOrgMeta() {
    try {
      var id = localStorage.getItem('current_org_id') || '';
      return (window._orgMetaById || {})[id] || null;
    } catch (e) {
      return null;
    }
  }

  // 当前是否处于「团队组织」上下文：组织仓库和关联仓库都要保留组织标识。
  function _isTeamOrgContext() {
    var meta = _currentOrgMeta();
    if (meta) return !meta.is_personal;
    var cur = currentRepo() || {};
    if (cur.orgId) {
      var byRepo = (window._orgMetaById || {})[String(cur.orgId)] || null;
      if (byRepo) return !byRepo.is_personal;
      return false; // 快照缺失先不显示，_updateRepoOrgBadge 会补拉 /orgs 后重绘
    }
    return false;
  }

  function _platformRepoLink(repo, labelOverride) {
    var _rk = repo.repoKey || repo.repo_key || '';
    var _fn = repo.fullName || (repo.owner + '/' + repo.name);
    var _prov = _rk.indexOf(':') > 0 ? _rk.slice(0, _rk.indexOf(':')) : '';
    var _dom = (typeof window.platformHost === 'function' && window.platformHost(_prov)) || 'gitcode.com';
    var _repoLink = 'https://' + _dom + '/' + _fn;
    var _logo = (typeof window.platformIcon === 'function')
      ? '<span style="margin-right:5px;color:var(--t2)">' + window.platformIcon(_prov || 'gitcode', 14) + '</span>'
      : '';
    var _label = labelOverride || _fn;
    return `${_logo}<a href="${_repoLink}" target="_blank" rel="noopener" style="color:var(--a);font-weight:500;text-decoration:none" title="打开 ${_fn}">${_label} ↗</a>`;
  }

  function _updateRepoOrgBadge() {
    var badge = document.getElementById('repoOrgBadge');
    if (!badge) return;
    var isTeam = _isTeamOrgContext();
    var _cur0 = currentRepo() || {};
    var _curIsOrgRepo = _isOrgRepo(_cur0) ||
      !!(window._managedRepoFlags && _cur0.fullName && window._managedRepoFlags[_cur0.fullName]);
    // 仅「组织上下文 + 当前仓库是组织仓库（is_org_repo）」显示紫色「组织项目」胶囊。
    // 个人空间、以及组织下关联的普通仓库都不显示。
    badge.style.display = (isTeam && _curIsOrgRepo) ? 'inline-flex' : 'none';
    var sel = document.getElementById('repoSelect');
    if (sel) {
      sel.style.borderColor = isTeam ? 'rgba(123,60,242,.7)' : '';
      sel.style.boxShadow = isTeam ? '0 0 0 2px rgba(49,104,224,.16)' : '';
    }
    // 首次进入时组织元信息可能还没缓存：补一次 /orgs 后重绘，避免组织标识闪没。
    if (!_currentOrgMeta() && (localStorage.getItem('current_org_id') || '')) {
      API.get('/orgs').then(function (orgs) {
        _cacheOrgs(orgs);
        _updateRepoOrgBadge();
      }).catch(function () {});
    }
  }

  function _orgProjectLink(orgId) {
    var _cur = currentRepo() || {};
    var id = String(orgId || _cur.orgId || localStorage.getItem('current_org_id') || '');
    var label = I18N.t('app.repo.org_project') || '组织项目';
    if (!id) return '<span style="color:var(--t2);font-weight:500">' + label + '</span>';
    var safe = id.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    var title = I18N.t('organizations.demands.title') || '组织需求';
    return '<a href="javascript:void(0)" onclick="app._openOrgDemandCenter(\'' + safe + '\')" style="color:var(--a);font-weight:600;text-decoration:none" title="' + title + '">' + label + ' ↗</a>';
  }

  // 「组织项目」→ 组织需求中心：切到该组织的组织仓库，再进需求中心（#tasks）。
  // 组织级需求（org_dispatch 父单）落在组织仓库下，因此需求中心按组织仓库过滤
  // 即可看到该组织的组织需求；组织仓库缺失时回落到组织详情页。
  function _openOrgDemandCenter(orgId) {
    var _cur = currentRepo() || {};
    var id = String(orgId || _cur.orgId || localStorage.getItem('current_org_id') || '');
    if (!id) return;
    localStorage.setItem('current_org_id', id);
    API.get('/repos/managed').then(function (md) {
      var list = (md && md.repos) || [];
      var orgRepo = list.find(function (r) { return !!r.is_org_repo; });
      if (!orgRepo || !orgRepo.full_name) {
        nav('organizations', id);
        return;
      }
      var parts = String(orgRepo.full_name).split('/');
      var owner = orgRepo.repo_owner || parts[0] || '';
      var name = orgRepo.repo_name || parts[1] || '';
      if (!owner || !name) {
        nav('organizations', id);
        return;
      }
      sessionStorage.setItem('currentRepo', JSON.stringify({
        fullName: orgRepo.full_name, owner: owner, name: name,
        repoKey: orgRepo.repo_key || '', isOrgRepo: true, orgId: id
      }));
      _syncAppPath(owner, name);
      var sel = $('repoSelect');
      if (sel) sel.value = orgRepo.full_name;
      initSidebarRepos();
      _updateRepoOrgBadge();
      nav('tasks');
    }).catch(function () {
      nav('organizations', id);
    });
  }

  function _escOpt(v) {
    return String(v == null ? '' : v)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // 仓库下拉统一渲染：组织仓库置顶且只显示「组织仓库」，普通仓库按平台分组。
  function _renderSidebarRepoOptions(repos) {
    var sel = $('repoSelect');
    if (!sel) return;
    // 仓库下拉只展示「当前组织」作用域内的仓库：个人空间不混入组织仓库/团队组织仓库，
    // 团队组织只展示该组织的仓库。组织清单未就绪时不做过滤（保持旧行为，避免误隐藏）。
    if (window.__orgsList && window.__orgsList.length) {
      var _ctx = _orgContext();
      repos = (repos || []).filter(function(r) { return _repoInOrgScope(r, _ctx); });
    }
    var current = currentRepo();
    var orgRepos = [];
    var normal = [];
    (repos || []).forEach(function(r) {
      var isOrg = !!(r.is_org_repo || (window._managedRepoFlags && window._managedRepoFlags[r.full_name]));
      if (isOrg) orgRepos.push(r);
      else normal.push(r);
    });
    var html = '';
    if (orgRepos.length) {
      html += '<optgroup label="' + _escOpt(I18N.t('app.repo.org_group') || '组织仓库') + '">';
      orgRepos.forEach(function(r) {
        var selected = current && current.fullName === r.full_name ? ' selected' : '';
        // 组织项目：隐藏具体仓库名（用户口径），但前缀优先用组织名区分多个组织；
        // 组织名缺失时回落 owner。形如：github-g-test / 组织项目
        var _owner = String(r.full_name || '').split('/')[0] || '';
        var _orgName = String(r.org_name || '').trim();
        var _prefix = _orgName || _owner;
        var _orgLabel = _prefix
          ? _prefix + ' / ' + (I18N.t('app.repo.org_project') || '组织项目')
          : (I18N.t('app.repo.org_project') || '组织项目');
        html += '<option value="' + _escOpt(r.full_name) + '"' + selected + '>'
          + _escOpt(_orgLabel) + '</option>';
      });
      html += '</optgroup>';
    }
    // 按平台分组：分组键 = repo_key 前缀（任何在册平台都成立）。
    var groups = {};
    normal.forEach(function(r) {
      var rk = r.repo_key || '';
      var p = rk.indexOf(':') > 0 ? rk.slice(0, rk.indexOf(':')) : 'gitcode';
      (groups[p] = groups[p] || []).push(r);
    });
    var order = (window.__REPO_PLATFORMS__ || []).map(function(x) { return x && x.provider; })
      .filter(function(k) { return k && groups[k]; });
    Object.keys(groups).forEach(function(k) { if (order.indexOf(k) < 0) order.push(k); });
    order.forEach(function(p) {
      var list = groups[p];
      if (!list || !list.length) return;
      html += '<optgroup label="' + _escOpt(platformLabelOf(p)) + '">';
      list.forEach(function(r) {
        var selected = current && current.fullName === r.full_name ? ' selected' : '';
        html += '<option value="' + _escOpt(r.full_name) + '"' + selected + '>'
          + _escOpt(r.full_name) + '</option>';
      });
      html += '</optgroup>';
    });
    html += '<option value="__add__">＋ ' + _escOpt(I18N.t('app.repo.add_new') || '新增仓库托管') + '</option>';
    sel.innerHTML = html;
    sel.onclick = null;
    _updateRepoOrgBadge();
    // 刷新后 currentRepo 的 isOrgRepo 可能缺失，等 /repos/managed 返回后补一次顶栏文案。
    var _repoViews = ['dash', 'pipe', 'vod', 'issue', 'coding', 'qa', 'sre', 'chat', 'knowledge', 'skills'];
    if (_isOrgRepo(current) && _repoViews.indexOf(window._currentView || '') >= 0) {
      var _rt = $('repoTitle');
      if (_rt) _rt.innerHTML = _orgProjectLink(current.orgId);
    }
  }

  // ── 需求详情深链：/app/<owner>/<repo>/#tasks/<N>（区分仓库）──
  // hash 路由只带视图与参数（#tasks/12），仓库维度放在路径里；解析器见 js/deeplink.js。
  function _appPathCtx() {
    try {
      const dl = window.GrapeDeepLink;
      return (dl && dl.parseAppPath) ? dl.parseAppPath(window.location.pathname || '') : null;
    } catch (e) { return null; }
  }

  function _setCurrentRepoFromDeeplink(repo) {
    try {
      const owner = repo.owner || String(repo.full_name || '').split('/')[0] || '';
      const name = repo.name || String(repo.full_name || '').split('/')[1] || '';
      const state = {
        fullName: repo.full_name || (owner + '/' + name),
        owner: owner,
        name: name,
        repoKey: repo.repo_key || ''
      };
      // 深链同样要带上组织归属：否则登录/刷新后顶栏与仓库下拉无法按组织过滤
      const orgId = String(repo.org_id || repo.orgId || '');
      if (orgId) state.orgId = orgId;
      if (repo.is_org_repo !== undefined || repo.isOrgRepo !== undefined) {
        state.isOrgRepo = !!(repo.is_org_repo || repo.isOrgRepo);
      }
      sessionStorage.setItem('currentRepo', JSON.stringify(state));
      if (orgId) localStorage.setItem('current_org_id', orgId);
    } catch (e) {}
    // 深链落仓后组织上下文已变：顶栏名称和仓库下拉都要按新组织重绘，
    // 否则会停留在「默认个人组织」的旧渲染上。
    _updateTbOrgName();
    initSidebarRepos();
  }

  // 把路径里的仓库落到 currentRepo（深链入口）。返回 Promise<boolean>：
  //   true = 已切到该仓库；false = 没命中/无权（已提示，路由按无仓库上下文继续）。
  function _applyAppPathRepo() {
    const ctx = _appPathCtx();
    if (!ctx) return Promise.resolve(false);
    const cur = currentRepo();
    if (cur && window.GrapeDeepLink.sameRepo(cur.fullName, ctx.full)) return Promise.resolve(true);

    function pick(list) {
      for (let i = 0; i < (list || []).length; i++) {
        const r = list[i] || {};
        const full = r.full_name || ((r.owner || '') + '/' + (r.name || ''));
        if (window.GrapeDeepLink.sameRepo(full, ctx.full)) {
          _setCurrentRepoFromDeeplink({
            full_name: full, owner: r.owner, name: r.name, repo_key: r.repo_key,
            org_id: r.org_id, orgId: r.orgId, is_org_repo: r.is_org_repo, isOrgRepo: r.isOrgRepo,
            org_name: r.org_name
          });
          return true;
        }
      }
      return false;
    }

    // 缓存先落（秒开），服务端结果为准（缓存可能已解绑/改名）
    let cachedHit = false;
    try {
      const c = JSON.parse(localStorage.getItem(REPO_CACHE_KEY) || 'null');
      cachedHit = pick(c && c.repos);
    } catch (e) {}

    return API.get('/repos/member').then(function (d) {
      if (pick((d && d.repos) || [])) return true;
      if (cachedHit) {
        // 缓存里有、服务端没有 → 已被解绑/无权限：清掉，别拿旧仓库打开别人的单
        try { sessionStorage.removeItem('currentRepo'); } catch (e) {}
      }
      // 2026-11-17 现场：只弹 toast 不动地址栏，用户会停在 /app/<owner>/<repo>/ 上——
      // 每次刷新重放同一次失败，且所有仓库维度菜单都只是「静默回大厅」，现象就是
      // 「项目场景/流水线点了没反应」。这里把路径改回大厅（保留 /en 语言前缀），
      // 并丢掉指向别人仓库的 hash，避免脏 URL 反复把用户拽回同一个坑。
      try {
        if (window.history && window.history.replaceState) {
          window.history.replaceState(window.history.state, '',
            (ctx.prefix || '') + '/' + (window.location.search || ''));
        }
      } catch (e) {}
      showToast(I18N.t('app.deeplink.repo_not_found') ||
                '无法通过该链接进入：仓库不存在、未托管或你没有权限', '', 6000);
      return false;
    }).catch(function () { return cachedHit; });
  }

  // 深链仓库就绪后再走原有「hash → 视图」恢复逻辑（顺序不能反：先有仓库才能开详情页）
  // 只有仓库、没有 hash（/app/owner/repo/）：直接进该仓库工作台，而不是弹仓库选择大堂

  // Vue 视图 chunk 是 ES module（defer 执行），hash 路由可能在模块注册 window.renderXxx
  // 之前就跑到这里 → 直接调用会抛 "renderXxx is not a function"，容器停在「加载中」。
  // 这里做有界重试（最多 ~4s），既修掉竞态，也不会把真正的加载失败伪装成永久 loading。
  function _ensureViewFn(name, cb, tries) {
    tries = tries || 0
    if (typeof window[name] === 'function') { cb(); return; }
    if (tries >= 40) {
      const pc = document.getElementById('pageContent');
      if (pc) pc.innerHTML = '<div class="error">' + (I18N.t('common.load_failed') || '加载失败') +
        '（' + name + '）</div>';
      return;
    }
    setTimeout(function () { _ensureViewFn(name, cb, tries + 1); }, 100);
  }

  function _bootRestore() {
    var _ctx0 = _appPathCtx();
    // D109 收口（2026-11-14 复核）：关系为空时不得复用任何残留仓库入口（URL 深链 /
    // sessionStorage currentRepo）——否则「清空全部 PAT 且无 OAuth」的用户刷新首页仍
    // 停在项目工作台，绕过引导门。关系空 → 先清残留，再走引导页。
    if (!_hasVcsBinding()) {
      try { sessionStorage.removeItem('currentRepo'); } catch (e) {}
      goToRepos();
      return;
    }
    _applyAppPathRepo().then(function (hit) {
      const h = window.location.hash || '';
      if (hit && h.length <= 1) { nav('dash'); return; }
      // 深链指名了仓库却没命中：多半是 boot 早于 token 落地 / 接口抖动（曾表现为
      // 「登录后第一次进 /app/owner/repo 掉回引导页，刷新一次才好」）。再试一次，
      // 仍失败才交给 _restoreNavAfterLogin —— 那里不会再用「未绑定」去夺路径。
      if (_ctx0 && !hit && !currentRepo()) {
        setTimeout(function () {
          _applyAppPathRepo().then(function (hit2) {
            if (hit2) {
              const h2 = window.location.hash || '';
              if (h2.length <= 1) { nav('dash'); return; }
            }
            _restoreNavAfterLogin();
            _resumeScenePollAfterLogin();
          });
        }, 400);
        return;
      }
      _restoreNavAfterLogin();
      _resumeScenePollAfterLogin();
    });
  }

  // 仓库切换后同步路径，避免 URL 与当前仓库脱节（深链分享出去指错仓库）
  // 注意：GitCode 的仓库 name 可能是显示名（≠URL path），统一以 currentRepo().fullName 为准
  // （与 currentRepo() 里「强制 URL 语义」同口径），否则会拼出 /app/owner/显示名/ 的死链。
  function _syncAppPath(owner, name) {
    try {
      const dl = window.GrapeDeepLink;
      if (!dl || !dl.buildAppPath) return;
      const cur = currentRepo();
      if (cur && cur.fullName) {
        const parts = String(cur.fullName).split('/');
        if (parts.length === 2 && parts[0] && parts[1]) {
          owner = parts[0];
          name = parts[1];
        }
      }
      const target = dl.buildAppPath(owner, name, _appPathPrefix());
      if ((window.location.pathname || '') === target) return;
      window.history.replaceState(window.history.state, '', target + (window.location.hash || ''));
    } catch (e) {}
  }

  function _appPathPrefix() {
    try { return /^\/en(\/|$)/.test(window.location.pathname || '') ? '/en' : ''; } catch (e) { return ''; }
  }

  // 回仓库选择大堂：把 /app/<owner>/<repo>/ 前缀还原成根路径，
  // 否则刷新/转发仍指回上一个仓库（视图已变了，路径得跟上）
  function _syncAppPathToRoot() {
    try {
      if (!_appPathCtx()) return;
      const rootPath = _appPathPrefix() + '/';
      const next = rootPath + (window.location.hash || '');
      if ((window.location.pathname || '') === rootPath && !window.location.hash) return;
      window.history.replaceState(window.history.state, '', next);
    } catch (e) {}
  }

  // ── Issue #614：会话失效时统一清理明文凭据弹窗 ──
  // showLogin 是所有登出路径（app.logout 手动登出 / doRefresh 失败 /
  // handle401 无 retryFn 踢回登录）的最终汇聚点。在此集中关闭/清空
  // 所有含明文秘钥的弹窗与面板，避免秘钥在无人值守屏幕上残留暴露。
  function _clearSensitiveModals() {
    // 1) 统一弹层：showModal/showPrompt（grapeModalOverlay）与 showConfirm（grapeConfirmOverlay）
    closeModal();
    closeConfirm();
    // 2) 已知明文凭据面板：runtime 添加设备的凭据生成区 / AI Devspace 区
    //    （runtimes.js 一次性渲染明文 token，清空 DOM 即清除秘钥）
    var panelIds = ['addDeviceCredArea', 'addDeviceDevArea', 'aiDevspacePanel'];
    for (var i = 0; i < panelIds.length; i++) {
      var panel = document.getElementById(panelIds[i]);
      if (panel) { panel.innerHTML = ''; panel.style.display = 'none'; }
    }
    // 3) 已知敏感弹窗：SRE 添加设备弹窗、侧边栏添加设备面板、
    //    devspace 环境凭据弹窗（credModal_*，直含明文 token 安装命令）
    var modalIds = ['sreAddDeviceOverlay', 'addDeviceOverlay'];
    for (var j = 0; j < modalIds.length; j++) {
      var modal = document.getElementById(modalIds[j]);
      if (modal) modal.remove();
    }
    var credModals = document.querySelectorAll('[id^="credModal_"]');
    for (var k = 0; k < credModals.length; k++) { credModals[k].remove(); }
    // 4) 兜底：移除 body 直系子节点中 position:fixed 且 z-index∈[1000,9000)
    //    的未知遮罩层（toast 9999/10000、warn 9999、帮助抽屉 9990/9991 不在
    //    区间内，不受影响）
    var overlays = document.querySelectorAll('body > div');
    for (var m = 0; m < overlays.length; m++) {
      var node = overlays[m];
      var pos = (node.style && node.style.position) || '';
      var z = parseInt((node.style && node.style.zIndex) || '', 10);
      if (pos === 'fixed' && !isNaN(z) && z >= 1000 && z < 9000) { node.remove(); }
    }
  }

  function showLogin() {
    // Issue #614：会话失效统一清理明文凭据弹窗（所有登出路径的汇聚点，
    // 与下方 UC16e 停轮询同一收口语义）
    _clearSensitiveModals();
    document.documentElement.removeAttribute('data-logged-in');
    $('loginView').classList.remove('hid');
    $('sidebar').style.display = 'none';
    $('mainView').style.display = 'none';
    // UC16e（#535 S9）：被踢回登录页时停掉后台轮询定时器——
    // 此前 doRefresh 失败清 token + showLogin 后，dashboard 30s 轮询
    // 仍在裸发 401（token 已清走「无 token」静默分支），用户只看到
    // 数据不动、得不到「请重新登录」的明确引导。showLogin 是所有
    // 踢人路径（refresh 失败/手动登出/token 失效）的统一收口，在此
    // 停轮询 = 一次修复覆盖全部入口。
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    // Issue #600：离开/被踢时停掉流水线卡片轮询
    if (typeof stopPipePolling === 'function') stopPipePolling();
    // Issue #3734：被踢时停掉需求看板轮询（此前只收口了 dashboard/pipe，tasks 仍在
    // 每 30s 裸发 401 → 单日 2.8 万条噪声）；与 _refreshTimer/pipe 同一收口语义
    if (typeof _stopScenePoll === 'function') _stopScenePoll();
    // 清除当前视图状态——退出/过期后重新登录时 _restoreNavAfterLogin 需要重新路由
    window._currentView = '';
    renderOAuthButtons();
    // 自动聚焦用户名输入
    setTimeout(function(){var el=$('loginUser');if(el)el.focus()},100);
  }

function loadAgentTypes() {
  fetch('/api/agents/types').then(r => r.json()).then(d => {
    (d.types || []).forEach(t => { _AGENT_TYPES[t.agent_type] = t; });
  }).catch(() => {});
}

  function showApp() {
    document.documentElement.setAttribute('data-logged-in', '1');
    $('loginView').classList.add('hid');
    // 退出登录（showLogin）会把 sidebar/mainView 写成内联 display:none。重新登录时
    // 必须摘掉这两个内联样式，交回 CSS（[data-logged-in="1"] .sb/.mn → display:flex）；
    // 否则登录后若路由直接命中 #dash（如 /app/<owner>/<repo>/ 深链，_bootRestore 不走
    // loadRepos）主区会一直隐藏——现象就是「只剩左侧栏，右侧一片空白」。
    $('sidebar').style.display = '';
    $('mainView').style.display = '';
    // 登出会清 current_org_id：重新登录默认落「个人」上下文——个人空间要能
    // 看到全部托管仓库（含被团队组织关联的），深链落仓时再由仓库归属覆盖。
    _fetchOrgs().then(function(orgs) {
      if (!localStorage.getItem('current_org_id') && orgs && orgs.length) {
        var _pid = _personalOrgId(orgs);
        if (_pid) localStorage.setItem('current_org_id', _pid);
      }
      _updateTbOrgName();
    }).catch(function() {});
    // 同一页内「退出→重新登录」不会清空侧栏 DOM：仓库下拉可能还留着上一个组织/
    // 账号的选项。按当前组织重新拉一次，避免残留上一个账号的仓库。
    initSidebarRepos();
    // 工作台已由 CSS 显示，立即展示加载中状态
    // 登录/刷新是「重新绘制」入口：先清空视图标记，否则下面的 loading 占位会盖掉
    // 深链 hash 已经渲染好的视图（如 /login#event/9717），而 _restoreNavAfterLogin
    // 的「已有视图不覆盖」守卫又会因标记非空直接 return —— 页面永久停在「加载中」。
    window._currentView = '';
    $('pageContent').innerHTML = '<div class="loading">' + I18N.t('common.loading') + '</div>';
    loadAgentTypes();
    // 串行加载：loadProfile 获取 bindings → 根据绑定状态和 hash 路由决定渲染哪个页面
    loadProfile().then(() => {
      _bootRestore();
    }).catch(() => {
      _bootRestore();  // profile 失败时也走统一路由恢复（深链仓库 → hash 路由 → 引导页/loadRepos）
    });
  }

  // 口径统一：前端粗判只认「仓库平台绑定」（平台卡目录内 provider）——
  // 登录渠道（华为等）不算；目录未就绪时保守视为已绑定，交由后端
  // /repos/member 的 _no_vcs_binding 权威纠正（D102-C）。
  // 注：Vue 线未搬 _appVcsCatalog 目录（旧壳的 _appLoadVcsCatalog 已随页面删除），
  // 因此目录恒为 null → 本函数当前等价于「bindings 非空」；保留它为了语义与
  // arch-weekly-11 同源，将来恢复目录即自动生效（arch 合并 d2787875）。
  // 绑定态是否已经「确知」：/auth/profile 成功返回后才置 true。
  // 未确知时绝不能按「未绑定」把带深链仓库的用户轰去引导页（那会把 /app/owner/repo/ 路径也改掉）。
  var _profileLoadedOk = false;
  // 关系快照的取回时间（ms）。工作区/我的项目/项目模板这些入口在每次进入前用它判「是否要
  // 重新取一次关系」——PAT 增删只改服务端，前端旧快照会让「已清空」的账号仍被判已接入。
  var _linksLoadedAt = 0;

  function _hasVcsBinding() {
    // D109 收口：判据 = 「当前用户是否存在仓库平台关系」（grape_t_user_repo_platform_link，
    // 未撤销行）。绑定 identities 只是登录身份，不是接入关系（huawei 纯登录渠道即为反例）。
    // 表未就绪时后端返回 source='identities'，此时沿用旧判据，避免误判。
    var _links = window.__repoPlatformLinks;
    if (_links && (_links.source === 'table' || _links.source === 'identities')) {
      // 2026-11-18：关系行在、但凭据全部失效（credential_error 非空）时仍要进项目页——
      // 项目页的凭据提示条（"已隐藏其仓库，请重新配置"）是这批用户唯一的可处置入口；
      // 判成"无绑定"会把他们送回引导页，复现 #3200 的"系统说我绑错了平台"。
      if (_links.has_any) return true;
      return ((_links.credential_broken || []).length
              || Object.keys(_links.credential_error || {}).length) > 0;
    }
    // 快照未就绪：不按 identities 猜「已接入仓库平台」——Vue 线没有 _appVcsCatalog，
    // 旧判据会把纯登录渠道（huawei 之类）当成仓库平台绑定，把无关系用户送进项目页。
    // 无快照一律按「无关系」处理，落引导页；后端 /repos/member 的 _no_vcs_binding 仍有终裁权。
    return false;
  }

  // 统一路由恢复：密码登录 / OAuth 登录 / 页面刷新共用
  // 优先级：hash 路由 > 有绑定→loadRepos > 无绑定→引导页
  function _restoreNavAfterLogin() {
    // 已有视图（如 loadRepos 同步缓存路径已渲染）→ 不覆盖。
    // 例外：视图标记有值但主区只剩 loading 占位（登录流程 showApp 刚把它盖掉），
    // 那是"未渲染"而非"已渲染"——放行按 hash 重绘，否则深链永久卡在加载中。
    if (window._currentView && window._currentView !== '') {
      var _pc0 = document.getElementById('pageContent');
      var _onlyLoading = _pc0 && _pc0.children.length === 1 && _pc0.children[0].classList &&
        _pc0.children[0].classList.contains('loading');
      if (!_onlyLoading) return;
      window._currentView = '';
    }

    const hash = window.location.hash;
    const user = JSON.parse(localStorage.getItem('user') || '{}');
    const bindings = user.bindings || [];
    const noRepoViews = NO_REPO_VIEWS;

    if (hash && hash.length > 1) {
      // 有 hash 路由 → 恢复到该页面。
      // 一键部署闭环（复现修复）：未绑定平台的新用户带 hash 进入时（注册页跳转/
      // OAuth 回调携带 hash），原逻辑直接 nav() 落在空 dashboard，绕过绑定引导页
      // ——用户首次进入看不到 GitCode/GitHub 绑定提示，只剩密码补全条。
      // 修正：未绑定且无当前仓库 → 一律引导页（hash 丢弃）；绑定引导完成后正常走路由。
      const parts = hash.substring(1).split('/');
      const view = parts[0];
      const param = parts[1] ? decodeURIComponent(parts[1]) : undefined;
      // D109 收口：先认「用户↔仓库平台关系」，再看当前仓库。
      // 进需要仓库的视图（含 #repos 本身）时，没有关系一律引导页——
      // sessionStorage 里残留的 currentRepo 不能代替关系（撤绑后它还在）。
      // 绑定态未知（_profileLoadedOk=false）时不跳，避免误判（基线口径）。
      if (!_hasVcsBinding() && !noRepoViews.includes(view) && _profileLoadedOk) {
        goToRepos();
      } else if (noRepoViews.includes(view) || currentRepo()) {
        nav(view, param);
      } else {
        nav('dash');
      }
    } else if (_appPathCtx() && !currentRepo()) {
      // 深链仓库没解析出来（同上）：保留 URL，落在 dashboard，刷新/重登后可恢复
      nav('dash');
    } else if (_hasVcsBinding()) {
      // 有仓库平台关系无 hash → 加载仓库列表（纯登录渠道绑定不算关系 → 走引导页）
      loadRepos();
    } else if (!_hasVcsBinding()) {
      // D109：先判关系，再判残留 currentRepo —— 清空全部 PAT 且无 OAuth 时关系为 0，
      // sessionStorage 里的旧仓库必须丢弃，否则会绕过引导门直接停在项目页。
      try { sessionStorage.removeItem('currentRepo'); } catch (e) {}
      goToRepos();
    } else if (currentRepo()) {
      // 有绑定且有当前仓库 → dashboard
      nav('dash');
    } else {
      // 有绑定且无 hash/仓库/引导需求 → 停留当前视图（显式空分支：语义=无需路由）
    }
  }

  // Issue #3734：登录恢复后回启 tasks 看板轮询。登出/被踢只切 DOM、不一定卸载
  // Vue 组件（onMounted 不会重跑），因此由 showApp 链路显式回启；startPoll 自带
  // 清旧 timer，重复调用不会产生双定时器。
  function _resumeScenePollAfterLogin() {
    if (window._currentView === 'tasks' && typeof _startScenePoll === 'function') _startScenePoll();
  }

  // 仓库平台关系刷新（D109 对象模型）：PAT / OAuth 绑定后关系行是服务端写的，
  // 前端 __repoPlatformLinks 是登录时的快照——不刷新就会「刚配完 PAT 仍被引导门拦回引导页」。
  function refreshRepoLinks() {
    return API.get('/auth/platform-links').then(function(d) {
      window.__repoPlatformLinks = d || null;
      _linksLoadedAt = Date.now();
      // /auth/profile 的字段同源；同步回 localStorage 的 user 快照，供其它判据使用
      try {
        var u = JSON.parse(localStorage.getItem('user') || '{}');
        u.repo_platform_links = d || null;
        localStorage.setItem('user', JSON.stringify(u));
      } catch (e) {}
      return d || null;
    }).catch(function() { return null; });
  }

  // 提示条已移除——未绑定用户直接显示引导页（goToRepos 处理）

  function _showBindBannerIfNeeded() {
    // 已移除——未绑定用户通过引导页引导绑定，不需要额外提示条
  }

  function loadProfile() {
    return API.get('/auth/profile').then(d => {
      const displayName = d.nickname || d.username || 'User';
      $('userName').textContent = displayName;
      $('userAvatar').textContent = (displayName || 'U')[0].toUpperCase();
      // 角色：仅当任一所托管仓库真实是 admin（managed_repos[].is_admin）才显示"管理员"，
      // 修复：原逻辑用 managed_repos.length>0（"是否托管过仓库"）误判为管理员。
      const _managed = d.managed_repos || [];
      const _isAnyRepoAdmin = _managed.some(function(r) { return r.is_admin; });
      $('userRole').textContent = _isAnyRepoAdmin ? I18N.t('dashboard.admin') : I18N.t('dashboard.member');
      // 同步 toolbar 用户下拉的头像和名称
      var _tbAvatar = $('tbUserAvatar'); if (_tbAvatar) _tbAvatar.textContent = (displayName || 'U')[0].toUpperCase();
      var _tbName = $('tbUserName'); if (_tbName) _tbName.textContent = displayName;
      localStorage.setItem('user', JSON.stringify(d));
      _profileLoadedOk = true;
      // D109：仓库平台关系（对象模型）。登录后进不进引导页以它为准；
      // 缺失（老后端/表未就绪回落）时 _hasVcsBinding 走 identities 旧判据。
      window.__repoPlatformLinks = d.repo_platform_links || null;
      _linksLoadedAt = Date.now();
      // 如果当前在仓库选择页，更新 Welcome 消息
      const welcomeEl = document.getElementById('welcomeUser');
      if (welcomeEl) welcomeEl.textContent = displayName;
      // 加载组织下拉列表
      _loadOrgSelect();
      // 加载右上角组织名
      _updateTbOrgName();
      // 需求 18: OAuth 首次登录补全提示条
      _checkOAuthCompletion(d);
    }).catch(() => {});
  }

  function _loadOrgSelect() {
    var sel = document.getElementById('orgSelect');
    if (!sel) return;
    API.get('/orgs').then(function(orgs) {
      if (!orgs || orgs.length === 0) {
        sel.innerHTML = I18N.t("organizations.no_orgs");
        return;
      }
      var html = '';
      var currentId = localStorage.getItem('current_org_id') || '';
      var found = false;
      orgs.forEach(function(o) {
        var selAttr = '';
        if (String(o.id) === String(currentId)) { selAttr = ' selected'; found = true; }
        var label = o.name + (o.is_personal ? '（默认）' : '') + ' · ' + o.role;
        html += '<option value="' + o.id + '"' + selAttr + '>' + label + '</option>';
      });
      // 未匹配到当前组织 → 默认选个人组织
      if (!found) {
        var personal = orgs.find(function(o) { return o.is_personal; });
        if (personal) {
          localStorage.setItem('current_org_id', personal.id);
          html = html.replace('value="' + personal.id + '"', 'value="' + personal.id + '" selected');
        }
      }
      sel.innerHTML = html;
    }).catch(function(e) {
      // 延迟重试一次（可能是 token refresh 导致的瞬时失败）
      setTimeout(function() {
        API.get('/orgs').then(function(orgs) {
          if (!orgs || orgs.length === 0) {
            sel.innerHTML = '<option value="">无组织</option>';
            return;
          }
          var html2 = '';
          var currentId2 = localStorage.getItem('current_org_id') || '';
          var found2 = false;
          orgs.forEach(function(o) {
            var selAttr2 = '';
            if (String(o.id) === String(currentId2)) { selAttr2 = ' selected'; found2 = true; }
            html2 += '<option value="' + o.id + '"' + selAttr2 + '>' + o.name + (o.is_personal ? '（默认）' : '') + ' · ' + o.role + '</option>';
          });
          if (!found2) {
            var personal2 = orgs.find(function(o) { return o.is_personal; });
            if (personal2) {
              localStorage.setItem('current_org_id', personal2.id);
              html2 = html2.replace('value="' + personal2.id + '"', 'value="' + personal2.id + '" selected');
            }
          }
          sel.innerHTML = html2;
        }).catch(function() {
          sel.innerHTML = I18N.t("organizations.loading");
        });
      }, 500);
    });
  }

  // ===== 工作台右上角组织下拉菜单 =====
  function _toggleOrgDropdown() {
    var menu = document.getElementById('tbOrgMenu');
    if (!menu) return;
    if (menu.style.display === 'none' || !menu.style.display) {
      _loadTbOrgs();
      menu.style.display = 'block';
      setTimeout(function() {
        document.addEventListener('click', _closeOrgDropdownOnOutside);
      }, 0);
    } else {
      menu.style.display = 'none';
      document.removeEventListener('click', _closeOrgDropdownOnOutside);
    }
  }
  function _closeOrgDropdownOnOutside(e) {
    var btn = document.getElementById('tbOrgBtn');
    var menu = document.getElementById('tbOrgMenu');
    if (btn && !btn.contains(e.target) && menu && !menu.contains(e.target)) {
      menu.style.display = 'none';
      document.removeEventListener('click', _closeOrgDropdownOnOutside);
    }
  }
  // 组织下拉底部统一入口：创建组织（列表页不再作为入口，创建动作放在组织切换器里）。
  function _orgCreateMenuItem(menuId) {
    // 创建组织：优先在当前页面直接弹出创建弹窗（不切页）；全局函数未就绪时才回退到组织页。
    return '<div style="border-top:1px solid var(--b);margin-top:4px;padding-top:4px">'
      + '<div onclick="(window.openCreateOrgModal?window.openCreateOrgModal():app.nav(\'organizations\',\'__create__\'));document.getElementById(\'' + menuId + '\').style.display=\'none\'"'
      + ' style="cursor:pointer;padding:8px 12px;border-radius:8px;color:var(--a);font-size:12px;font-weight:600;display:flex;align-items:center;gap:7px"'
      + ' onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">'
      + '<span>' + (I18N.t("organizations.create_org") || "＋ 创建组织") + '</span></div></div>';
  }

  function _cacheOrgs(orgs) {
    window._orgMetaById = window._orgMetaById || {};
    (orgs || []).forEach(function(o) {
      if (o && o.id != null) window._orgMetaById[String(o.id)] = o;
    });
  }

  // ===== 当前组织上下文 =====
  // 「组织」是仓库下拉的作用域：顶栏名称、仓库下拉、切换组织必须共用同一个组织 id。
  // 登出会清 current_org_id，重新登录后若没人补默认值，顶栏会一直停在 i18n 的
  // 「我的组织」，仓库下拉也会退化成「所有组织的托管仓库混在一起」。
  var _orgsFetchPromise = null;
  function _fetchOrgs(force) {
    if (!force && window.__orgsList) return Promise.resolve(window.__orgsList);
    if (_orgsFetchPromise) return _orgsFetchPromise;
    _orgsFetchPromise = API.get('/orgs').then(function(orgs) {
      window.__orgsList = orgs || [];
      _cacheOrgs(window.__orgsList);
      return window.__orgsList;
    }).catch(function() {
      return window.__orgsList || [];
    }).then(function(list) {
      _orgsFetchPromise = null;
      return list;
    });
    return _orgsFetchPromise;
  }

  function _validOrgId(orgs, id) {
    return !!(id && (orgs || []).some(function(o) { return String(o.id) === String(id); }));
  }

  function _personalOrgId(orgs) {
    var personal = (orgs || []).find(function(o) { return o.is_personal; });
    return personal ? String(personal.id) : '';
  }

  // 解析/补齐当前组织：优先跟随当前仓库的 org_id（仓库是具体的操作上下文），
  // 其次沿用已选组织，最后回落到个人组织。结果写回 localStorage.current_org_id。
  function _ensureOrgContext(orgs) {
    var list = orgs || window.__orgsList || [];
    var currentId = String(localStorage.getItem('current_org_id') || '');
    var next = '';
    // 已选组织优先：个人空间要能看到全部托管仓库（含被团队组织关联的），
    // 不能被当前仓库归属反向改写。current_org_id 缺失时默认落个人组织；
    // 深链/显式切仓会先写入仓库所属组织，再走到这里。
    if (_validOrgId(list, currentId)) next = currentId;
    else next = _personalOrgId(list);
    if (next && next !== currentId) localStorage.setItem('current_org_id', next);
    // 仓库 org_id 缺失时只用托管清单回填快照（用于「组织项目」标识），不改变组织上下文。
    var repo = currentRepo() || {};
    if (repo.fullName && !repo.orgId && window._managedRepoOrgMap &&
        Object.prototype.hasOwnProperty.call(window._managedRepoOrgMap, repo.fullName)) {
      var repoOrg = String(window._managedRepoOrgMap[repo.fullName] || '');
      if (repoOrg) {
        repo.orgId = repoOrg;
        try { sessionStorage.setItem('currentRepo', JSON.stringify(repo)); } catch (e) {}
      }
    }
    return next;
  }

  function _orgContext() {
    var list = window.__orgsList || [];
    var id = _ensureOrgContext(list);
    var meta = (list || []).find(function(o) { return String(o.id) === String(id); }) || null;
    return { id: id, meta: meta, isPersonal: !!(meta && meta.is_personal) };
  }

  // 仓库是否属于当前组织作用域：
  // - 个人组织：**托管的所有仓库都可见**（含被团队组织关联的普通仓库）；
  //   只排除组织自动仓库（下拉里显示为「组织项目」的那种）。
  // - 团队组织：只保留 org_id 等于该组织的仓库（含它的组织仓库）。
  function _repoInOrgScope(r, ctx) {
    ctx = ctx || _orgContext();
    if (!ctx.id) return true;   // 组织清单还没拿到：不做过滤，保持旧行为
    var oid = String(
      (r && (r.org_id || r.orgId)) ||
      (window._managedRepoOrgMap && r && window._managedRepoOrgMap[r.full_name]) || ''
    );
    if (ctx.isPersonal) {
      var isOrgRepo = !!(r && (r.is_org_repo || r.isOrgRepo ||
        (window._managedRepoFlags && r.full_name && window._managedRepoFlags[r.full_name])));
      return !isOrgRepo;
    }
    return oid === String(ctx.id);
  }

  // 创建组织弹窗的数据源预热：organizations 页 / 全局创建弹窗复用同一个请求
  // （window.__orgNsPromise），避免每次打开弹窗都等 1.5~2.5s 的 VCS 外部调用。
  function _warmOrgNamespaces() {
    var now = Date.now();
    if (window.__orgNsPromise && now - (window.__orgNsAt || 0) < 60000) return;
    window.__orgNsAt = now;
    window.__orgNsPromise = API.get('/orgs/repo-namespaces').catch(function () {
      window.__orgNsPromise = null;
    });
  }

  function _loadTbOrgs() {
    var menu = document.getElementById('tbOrgMenu');
    if (!menu) return;
    _warmOrgNamespaces();
    menu.innerHTML = '<div style="padding:8px 12px;color:var(--t3);font-size:12px">' + (I18N.t("organizations.loading") || "加载中...") + '</div>';
    _fetchOrgs(true).then(function(orgs) {
      if (!orgs || orgs.length === 0) {
        menu.innerHTML = '<div style="padding:8px 12px;color:var(--t3);font-size:12px">' + (I18N.t("organizations.no_orgs") || "暂无组织") + '</div>'
          + _orgCreateMenuItem('tbOrgMenu');
        return;
      }
      var currentId = _ensureOrgContext(orgs);
      var html = '';
      orgs.forEach(function(o) {
        var isActive = String(o.id) === String(currentId);
        var bg = isActive ? 'var(--a)' : 'transparent';
        var fg = isActive ? '#fff' : 'var(--t1)';
        var badge = o.is_personal ? ' <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:' + (isActive ? 'rgba(255,255,255,.2)' : 'var(--bg)') + ';color:' + (isActive ? '#fff' : 'var(--t3)') + '">' + (I18N.t("organizations.badge_personal") || "默认") + '</span>' : '';
        // 团队组织在行内提供「管理」入口；个人组织不显示，避免无意义的详情跳转。
        var manage = '';
        if (!o.is_personal) {
          manage = '<span role="button" title="' + (I18N.t("organizations.detail.open") || "打开组织管理") + '"'
            + ' onclick="event.stopPropagation();app.nav(\'organizations\',\'' + _escOpt(o.id) + '\');document.getElementById(\'tbOrgMenu\').style.display=\'none\'"'
            + ' style="flex-shrink:0;padding:3px 8px;border:1px solid ' + (isActive ? 'rgba(255,255,255,.35)' : 'var(--b)') + ';border-radius:6px;font-size:10px;font-weight:600;color:' + (isActive ? '#fff' : 'var(--a)') + ';background:' + (isActive ? 'rgba(255,255,255,.12)' : 'transparent') + ';cursor:pointer;margin-left:4px">'
            + (I18N.t("common.manage") || "管理") + '</span>';
        }
        html += '<div onclick="app.switchOrg(\'' + _escOpt(o.id) + '\');document.getElementById(\'tbOrgMenu\').style.display=\'none\'" style="cursor:pointer;padding:8px 12px;border-radius:8px;background:' + bg + ';color:' + fg + ';display:flex;align-items:center;gap:8px;margin:2px 0" onmouseover="if(!this.dataset.active){this.style.background=\'var(--bg)\'}" onmouseout="if(!this.dataset.active){this.style.background=\'transparent\'}" data-active="' + (isActive ? '1' : '') + '">'
          + '<span style="font-size:14px">🏢</span>'
          + '<div style="flex:1;min-width:0">'
          + '<div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + (o.name || '') + badge + '</div>'
          + '<div style="font-size:10px;opacity:.7">' + (o.member_count || 1) + ' ' + (I18N.t("organizations.members") || "成员") + ' · ' + (o.role || 'member') + '</div>'
          + '</div>'
          + manage
          + '</div>';
      });
      html += _orgCreateMenuItem('tbOrgMenu');
      menu.innerHTML = html;
    }).catch(function() {
      menu.innerHTML = '<div style="padding:8px 12px;color:var(--r);font-size:12px">' + (I18N.t("organizations.load_failed", {msg: ""}) || "加载失败") + '</div>';
    });
  }
  function _updateTbOrgName() {
    // Issue #299: 显示当前已选空间名称（而非固定文本"我的组织"）
    var nameEl = document.getElementById('tbOrgName');
    if (!nameEl) return;
    // 登出会清 current_org_id；重新登录后必须先补默认组织（仓库所属组织 → 个人组织），
    // 否则这里直接 return，顶栏永远停在 i18n 的「我的组织」。
    _fetchOrgs().then(function(orgs) {
      if (!orgs || orgs.length === 0) return;
      // G-3：展示层只认已选组织；缺失/失效时保留 i18n 默认文案。
      // 默认组织的回填由 initSidebarRepos() 负责（重新登录后先补 current_org_id，
      // 再由后续 _updateTbOrgName() 调用显示名称），这里不再隐式改写组织上下文。
      var currentId = String(localStorage.getItem('current_org_id') || '');
      if (!_validOrgId(orgs, currentId)) return;
      var current = orgs.find(function(o) { return String(o.id) === String(currentId); });
      if (current && current.name) {
        nameEl.textContent = current.name;
        nameEl.removeAttribute('data-i18n'); // 动态填充后移除 i18n 键，防止语言切换覆盖
      }
    }).catch(function() {});
  }

  // ===== lobby（#repos 全屏页）组织独立下拉菜单 =====
  function _toggleLobbyOrgDropdown() {
    var menu = document.getElementById('lobbyOrgMenu');
    if (!menu) return;
    if (menu.style.display === 'none' || !menu.style.display) {
      _loadLobbyOrgs();
      menu.style.display = 'block';
      setTimeout(function() {
        document.addEventListener('click', _closeLobbyOrgDropdownOnOutside);
      }, 0);
    } else {
      menu.style.display = 'none';
      document.removeEventListener('click', _closeLobbyOrgDropdownOnOutside);
    }
  }
  function _closeLobbyOrgDropdownOnOutside(e) {
    var dropdown = document.getElementById('lobbyOrgDropdown');
    var menu = document.getElementById('lobbyOrgMenu');
    if (dropdown && !dropdown.contains(e.target)) {
      if (menu) menu.style.display = 'none';
      document.removeEventListener('click', _closeLobbyOrgDropdownOnOutside);
    }
  }
  function _loadLobbyOrgs() {
    var menu = document.getElementById('lobbyOrgMenu');
    if (!menu) return;
    _warmOrgNamespaces();
    menu.innerHTML = '<div style="padding:8px 12px;color:var(--t3);font-size:12px">' + (I18N.t("organizations.loading") || "加载中...") + '</div>';
    _fetchOrgs(true).then(function(orgs) {
      if (!orgs || orgs.length === 0) {
        menu.innerHTML = '<div style="padding:8px 12px;color:var(--t3);font-size:12px">' + (I18N.t("organizations.no_orgs") || "暂无组织") + '</div>'
          + _orgCreateMenuItem('lobbyOrgMenu');
        return;
      }
      var currentId = _ensureOrgContext(orgs);
      var html = '';
      orgs.forEach(function(o) {
        var isActive = String(o.id) === String(currentId);
        var bg = isActive ? 'var(--a)' : 'transparent';
        var fg = isActive ? '#fff' : 'var(--t1)';
        var badge = o.is_personal ? ' <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:' + (isActive ? 'rgba(255,255,255,.2)' : 'var(--bg)') + ';color:' + (isActive ? '#fff' : 'var(--t3)') + '">' + (I18N.t("organizations.badge_personal") || "默认") + '</span>' : '';
        // 团队组织行内「管理」入口；个人组织不显示。
        var manage = '';
        if (!o.is_personal) {
          manage = '<span role="button" title="' + (I18N.t("organizations.detail.open") || "打开组织管理") + '"'
            + ' onclick="event.stopPropagation();app.nav(\'organizations\',\'' + _escOpt(o.id) + '\');document.getElementById(\'lobbyOrgMenu\').style.display=\'none\'"'
            + ' style="flex-shrink:0;padding:3px 8px;border:1px solid ' + (isActive ? 'rgba(255,255,255,.35)' : 'var(--b)') + ';border-radius:6px;font-size:10px;font-weight:600;color:' + (isActive ? '#fff' : 'var(--a)') + ';background:' + (isActive ? 'rgba(255,255,255,.12)' : 'transparent') + ';cursor:pointer;margin-left:4px">'
            + (I18N.t("common.manage") || "管理") + '</span>';
        }
        html += '<div onclick="app.switchOrg(\'' + _escOpt(o.id) + '\');document.getElementById(\'lobbyOrgMenu\').style.display=\'none\';app._updateLobbyOrgName()" style="cursor:pointer;padding:7px 12px;border-radius:8px;background:' + bg + ';color:' + fg + ';display:flex;align-items:center;gap:8px;margin:2px 0" onmouseover="if(!this.dataset.active){this.style.background=\'var(--bg)\'}" onmouseout="if(!this.dataset.active){this.style.background=\'transparent\'}" data-active="' + (isActive ? '1' : '') + '">'
          + '<span style="font-size:13px">🏢</span>'
          + '<div style="flex:1;min-width:0">'
          + '<div style="font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + (o.name || '') + badge + '</div>'
          + '<div style="font-size:10px;opacity:.7">' + (o.member_count || 1) + ' ' + (I18N.t("organizations.members") || "成员") + ' · ' + (o.role || 'member') + '</div>'
          + '</div>'
          + manage
          + '</div>';
      });
      html += _orgCreateMenuItem('lobbyOrgMenu');
      menu.innerHTML = html;
    }).catch(function() {
      menu.innerHTML = '<div style="padding:8px 12px;color:var(--r);font-size:12px">' + (I18N.t("organizations.load_failed", {msg: ""}) || "加载失败") + '</div>';
    });
  }
  function _updateLobbyOrgName() {
    // 延迟读取，等 switchOrg 写完 localStorage
    setTimeout(function() {
      var nameEl = document.getElementById('lobbyOrgName');
      if (!nameEl) return;
      _fetchOrgs().then(function(orgs) {
        if (!orgs || orgs.length === 0) return;
        var currentId = _ensureOrgContext(orgs);
        var current = orgs.find(function(o) { return String(o.id) === String(currentId); });
        if (current) {
          nameEl.textContent = current.name;
        }
      }).catch(function() {});
    }, 200);
  }

  // ===== lobby（#repos 全屏页）用户下拉菜单（退出登录） =====
  function _toggleLobbyUserMenu() {
    var menu = document.getElementById('lobbyUserMenu');
    if (!menu) return;
    if (menu.style.display === 'none' || !menu.style.display) {
      _loadLobbyUserMenu();
      menu.style.display = 'block';
      setTimeout(function() {
        document.addEventListener('click', _closeLobbyUserMenuOnOutside);
      }, 0);
    } else {
      menu.style.display = 'none';
      document.removeEventListener('click', _closeLobbyUserMenuOnOutside);
    }
  }

  // Sidebar user avatar dropdown (需求15: 个人资料 / 账号设置 / 登出)
  function _toggleUserMenu() {
    var menu = document.getElementById('sbUserMenu');
    if (!menu) return;
    if (menu.style.display === 'none' || !menu.style.display) {
      _loadUserMenu();
      menu.style.display = 'block';
      setTimeout(function() {
        document.addEventListener('click', _closeUserMenuOnOutside);
      }, 0);
    } else {
      menu.style.display = 'none';
      document.removeEventListener('click', _closeUserMenuOnOutside);
    }
  }

  function _closeUserMenuOnOutside(e) {
    var footer = document.getElementById('sbFooter');
    var menu = document.getElementById('sbUserMenu');
    if (footer && !footer.contains(e.target)) {
      if (menu) menu.style.display = 'none';
      document.removeEventListener('click', _closeUserMenuOnOutside);
    }
  }

  // OAuth 绑定回调检查——从 URL 参数提取 bind_success/bind_error 显示 toast
  function _checkBindSuccess() {
    var params = new URLSearchParams(window.location.search);
    var bindSuccess = params.get('bind_success');
    var bindError = params.get('bind_error');
    if (bindSuccess) {
      var label = (typeof window.platformLabel === 'function' && window.platformLabel(bindSuccess)) || bindSuccess;
      showToast(I18N.t('app.login.bind_success', {label: label}),
                I18N.t('app.login.bind_success_desc'));
      // D43: 绑定成功后清除仓库缓存，下次拉取获取含新平台的仓库
      localStorage.removeItem(REPO_CACHE_KEY);
      localStorage.removeItem('repo_member_cache');
      // 刷新 localStorage 中的 bindings
      try {
        var _u = JSON.parse(localStorage.getItem('user') || '{}');
        if (!_u.bindings) _u.bindings = [];
        if (_u.bindings.indexOf(bindSuccess) < 0) _u.bindings.push(bindSuccess);
        localStorage.setItem('user', JSON.stringify(_u));
      } catch(e) {}
      // 引导页成功反馈（苹果式完成感）：绑定的卡片原地变"已绑定 ✓"品牌态，
      // 光圈/箭头静默，1.6s 后再进仓库列表——替代"toast 一闪就跳走"的断崖
      var _card = document.querySelector('.ob-bind-card.' + bindSuccess + '-card, .ob-bind-card[data-provider="' + bindSuccess + '"]');
      if (!_card) {
        // 兜底：按卡片 onclick 里的 provider 匹配
        document.querySelectorAll('.ob-bind-card').forEach(function(c) {
          if ((c.getAttribute('onclick') || '').indexOf(bindSuccess) >= 0) _card = c;
        });
      }
      if (!_card || !document.querySelector('.ob-hero')) {
        // 时序兜底：OAuth 回跳时回调处理先于引导页渲染（init 顺序）——
        // 存 pending，_renderOnboardingShell 渲染完成后再应用成功态
        try { sessionStorage.setItem('ob_pending_bind_success', bindSuccess); } catch (e) {}
      }
      if (_card && document.querySelector('.ob-hero')) {
        try {
          document.querySelectorAll('.ob-bind-card').forEach(function(c) { c.classList.add('bound'); });
          var _h = document.querySelector('.ob-hero'); if (_h) _h.classList.add('ob-bound-done');
          _card.classList.add('just-bound');
          _card.insertAdjacentHTML('beforeend',
            '<div class="ob-bound-check" style="position:absolute;top:12px;right:12px;width:26px;height:26px;border-radius:50%;'
            + 'background:linear-gradient(135deg,#34c759,#30d158);display:flex;align-items:center;justify-content:center;'
            + 'box-shadow:0 2px 8px rgba(52,199,89,.4)">'
            + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg></div>');
          setTimeout(function() { loadRepos(); }, 1600);
          var cleanUrl0 = window.location.pathname + window.location.hash;
          window.history.replaceState({}, '', cleanUrl0);
          return;
        } catch (e) {}
      }
      // 清除 URL 参数
      var cleanUrl = window.location.pathname + window.location.hash;
      window.history.replaceState({}, '', cleanUrl);
    }
    if (bindError) {
      // P0-3：解码失败按“参数无效”处理（bind_error=100%25 会抛 URIError 中断 init）
      var rawErr = _safeDecodeParam(bindError.replace(/\+/g, ' '));
      var friendlyMsg = _translateOAuthError(rawErr || '');
      showToast(I18N.t('app.login.bind_failed'), friendlyMsg);
      var cleanUrl2 = window.location.pathname + window.location.hash;
      window.history.replaceState({}, '', cleanUrl2);
    }
    // OAuth 绑定命中历史绑定 → 转移确认
    // Issue #3097：票据不由回调 URL 传递（服务端 popup 模式故意不回传），
    // 必须调 GET /auth/transfer-context 取票；取票失败在页面内可见，不得静默。
    var needTransfer = params.get('need_transfer');
    if (needTransfer) {
      // 延迟调用，确保 account 视图已加载并注册取票函数
      setTimeout(function() {
        if (typeof window._accountFetchTransferContext === 'function') {
          window._accountFetchTransferContext(needTransfer);
        } else {
          showToast(I18N.t('app.login.bind_failed'),
                    I18N.t('app.account.transfer_context_expired'));
        }
      }, 500);
      var cleanUrl3 = window.location.pathname + window.location.hash;
      window.history.replaceState({}, '', cleanUrl3);
    }
  }

  // OAuth 错误翻译——将错误码翻译为用户可理解的语言
  function _translateOAuthError(msg) {
    if (!msg) return I18N.t('app.login.bind_failed_retry');
    // P2-i18n: 新错误码直接走 i18n key
    var errorKey = 'app.login.oauth_error_' + msg;
    var translated = I18N.t(errorKey);
    if (translated && translated !== errorKey) return translated;
    // 兼容旧的中文/英文消息
    var lower = msg.toLowerCase();
    if (lower.indexOf('access_denied') >= 0 || lower.indexOf('denied') >= 0)
      return I18N.t('app.login.oauth_cancelled');
    if (lower.indexOf('invalid_state') >= 0)
      return I18N.t('app.login.oauth_callback_error');
    if (lower.indexOf('already_bound') >= 0 || lower.indexOf('already') >= 0)
      return I18N.t('app.login.oauth_already_bound');
    return msg;
  }

  // 用户设置返回工作台
  function _accountGoBack() {
    // 优先级补位：从安全设置返回时安全横幅可能已被处理，补检被抑制的引擎引导
    setTimeout(function() { _checkEngineBanner(); }, 100);
    var sb = $('sidebar'); if (sb) sb.style.display = 'flex';
    // 未绑定仓库平台直接回引导页，不走 nav('dash') 避免 403（用户裁定 2026-11-09）
    if (!_hasVcsBinding()) {
      goToRepos();
      return;
    }
    var repo = currentRepo();
    if (repo) {
      nav('dash');
    } else {
      goToRepos();
    }
  }

  function _buildUserMenuItems(includeSecurity) {
    // 统一用户菜单项（所有页面复用）
    // 顺序：用户设置 → 账号安全（可选）→ 反馈 → 帮助 → 退出登录
    // 反馈/帮助统一在此入口，toolbar 不再放独立按钮（避免重复）
    var itemStyle = 'cursor:pointer;padding:8px 12px;border-radius:8px;font-size:13px;font-weight:500;display:flex;align-items:center;gap:6px;margin:2px 4px';
    var hoverScript = ' onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'"';
    var html = '';
    // 用户设置
    html += '<div onclick="app.nav(\'account\',\'profile\')" style="' + itemStyle + ';color:var(--t)"' + hoverScript + '>'
      + '<span>👤</span><span>' + I18N.t('app.account.center_title') + '</span></div>';
    // 账号安全
    if (includeSecurity) {
      html += '<div onclick="app.nav(\'account\',\'security\')" style="' + itemStyle + ';color:var(--a)"' + hoverScript + '>'
        + '<span>🔒</span><span>' + I18N.t('app.login.account_security_menu') + '</span></div>';
    }
    // 反馈
    html += '<div onclick="openFeedbackDialog()" style="' + itemStyle + ';color:var(--t)"' + hoverScript + '>'
      + '<span>💬</span><span>' + (I18N.t('app.feedback.link_text') || '反馈') + '</span></div>';
    // 帮助
    html += '<div onclick="app.openHelp()" style="' + itemStyle + ';color:var(--t)"' + hoverScript + '>'
      + '<span>📖</span><span>' + (I18N.t('app.help.link_text') || '帮助').replace('📖 ', '') + '</span></div>';
    // 退出登录
    html += '<div onclick="app.logout()" style="' + itemStyle + ';color:var(--r)"' + hoverScript + '>'
      + '<span>↩</span><span>' + (I18N.t('app.logout') || '退出登录') + '</span></div>';
    return html;
  }

  function _loadUserMenu() {
    var menu = document.getElementById('sbUserMenu');
    if (!menu) return;
    menu.innerHTML = _buildUserMenuItems(true);
  }
  function _closeLobbyUserMenuOnOutside(e) {
    var dropdown = document.getElementById('lobbyUserDropdown');
    var menu = document.getElementById('lobbyUserMenu');
    if (dropdown && !dropdown.contains(e.target)) {
      if (menu) menu.style.display = 'none';
      document.removeEventListener('click', _closeLobbyUserMenuOnOutside);
    }
  }
  function _loadLobbyUserMenu() {
    var menu = document.getElementById('lobbyUserMenu');
    if (!menu) return;
    menu.innerHTML = _buildUserMenuItems(true);
  }

  // Toolbar 用户下拉（工作区顶部，与侧边栏菜单内容一致）
  function _toggleTbUserMenu() {
    var menu = document.getElementById('tbUserMenu');
    if (!menu) return;
    if (menu.style.display === 'none' || !menu.style.display) {
      _loadTbUserMenu();
      menu.style.display = 'block';
      setTimeout(function() {
        document.addEventListener('click', _closeTbUserMenuOnOutside);
      }, 0);
    } else {
      menu.style.display = 'none';
      document.removeEventListener('click', _closeTbUserMenuOnOutside);
    }
  }
  function _loadTbUserMenu() {
    var menu = document.getElementById('tbUserMenu');
    if (!menu) return;
    menu.innerHTML = _buildUserMenuItems(true);
  }
  function _closeTbUserMenuOnOutside(e) {
    var btn = document.getElementById('tbUserBtn');
    var menu = document.getElementById('tbUserMenu');
    if (btn && !btn.contains(e.target)) {
      if (menu) menu.style.display = 'none';
      document.removeEventListener('click', _closeTbUserMenuOnOutside);
    }
  }

  function switchOrg(orgId) {
    if (!orgId) return;
    var _prevOrgId = localStorage.getItem('current_org_id') || '';
    var _sameOrg = String(_prevOrgId) === String(orgId);
    var sel = document.getElementById('orgSelect');
    if (sel) { sel.disabled = true; }
    localStorage.setItem('current_org_id', orgId);
    // 清除仓库缓存，防止切换组织后仍显示旧组织的仓库
    localStorage.removeItem(REPO_CACHE_KEY);
    // R5: 清除内存缓存 _cachedReposData，防止语言切换渲染旧组织仓库
    _cachedReposData = null;
    // 更新组织下拉框选中项
    if (sel) {
      for (var i = 0; i < sel.options.length; i++) {
        sel.options[i].selected = (String(sel.options[i].value) === String(orgId));
      }
      sel.disabled = false;
    }
    // 切换组织后按「目标组织」重新选仓库并重渲染当前视图。
    function _applyOrgRepoSelection(managed) {
      if (managed.length > 0) {
        // 切换到第一个已托管仓库
        var first = managed[0];
        sessionStorage.setItem('currentRepo', JSON.stringify({
          fullName: first.full_name, owner: first.owner, name: first.name,
          repoKey: first.repo_key || '', isOrgRepo: !!first.is_org_repo,
          orgId: first.org_id || ''
        }));
        // 更新侧边栏仓库下拉
        initSidebarRepos();
        // 刷新当前页面数据（切换组织后所有视图都重新加载）
        if (window._currentView === 'repos') {
          selectRepo(first.full_name, first.owner, first.name, first.repo_key || '');
        } else if (window._currentView === 'organizations' || window._currentView === 'org-pipeline') {
          // 组织管理/组织流水线跟随组织切换，不要继续显示旧组织详情
          nav(window._currentView, orgId);
        } else if (window._currentView) {
          nav(window._currentView, _currentParam);
        }
      } else {
        // 没有已托管仓库 — 清除当前仓库选择，刷新页面显示空态
        sessionStorage.removeItem('currentRepo');
        sessionStorage.removeItem('_bindReturnView');  // 绑定返回标记不跨会话残留
        initSidebarRepos();
        showToast(I18N.t("organizations.switch_org.no_repos"));
        // 切换组织后所有视图都重新加载（显示空态或刷新数据）
        if (window._currentView === 'repos') {
          goToRepos();
        } else if (window._currentView === 'organizations' || window._currentView === 'org-pipeline') {
          nav(window._currentView, orgId);
        } else if (window._currentView) {
          nav(window._currentView, _currentParam);
        }
      }
      // 刷新右上角组织名
      _updateTbOrgName();
      // 刷新首页"我的组织"区块高亮（如果在 #repos 页面）
      if (typeof _loadOrgSection === 'function') {
        _loadOrgSection();
      }
    }

    // Issue #2262：优先用组织作用域的 /repos/managed 选仓库。
    // /repos/member 是 VCS 侧全量列表、不含组织归属，按它选会把 currentRepo 指到
    // 个人/未归属仓库；Dashboard 的场景列表按 currentRepo 查询，于是切换团队组织后
    // 显示「暂无场景」（实际组织场景存在）。
    function _orgScopedRepos(md) {
      return ((md && md.repos) || []).map(function(r) {
        return {
          full_name: r.full_name,
          owner: r.repo_owner || (r.full_name || '').split('/')[0],
          name: r.repo_name || (r.full_name || '').split('/')[1],
          repo_key: r.repo_key || '',
          is_org_repo: !!r.is_org_repo,
          org_id: r.org_id || '',
          is_hosted: true,
          is_joined: true
        };
      });
    }

    function _switchToast() {
      showToast(I18N.t(_sameOrg ? "organizations.switch_org.current" : "organizations.switch_org.success"));
    }

    // 切到团队组织：进入该组织的「组织仓库」工作台（/app/<owner>/<org-repo>/#dash）。
    // 注意：只认 is_org_repo=1 的组织仓库，不自动选关联的其它仓库；组织仓库尚未
    // provisioning 完成时才回落到组织详情页。
    // 组织作用域过滤：
    // - 个人组织：全部托管仓库都可选（含被团队组织关联的普通仓库），只排除组织自动仓库；
    // - 团队组织：只保留本组织仓库。
    function _scopeRepos(list, _orgId, _isPersonal) {
      return (list || []).filter(function(r) {
        if (_isPersonal) {
          return !(r && (r.is_org_repo || r.isOrgRepo ||
            (window._managedRepoFlags && r.full_name && window._managedRepoFlags[r.full_name])));
        }
        var oid = String((r && (r.org_id || r.orgId)) || '');
        return oid === String(_orgId);
      });
    }

    function _enterTeamOrgWorkbench() {
      sessionStorage.removeItem('currentRepo');
      sessionStorage.removeItem('_bindReturnView');
      _updateTbOrgName();
      API.get('/repos/managed').then(function(md) {
        var list = _scopeRepos((md && md.repos) || [], orgId, false);
        var orgRepo = list.find(function(r) { return !!r.is_org_repo; });
        if (orgRepo && orgRepo.full_name) {
          var parts = String(orgRepo.full_name).split('/');
          var owner = orgRepo.repo_owner || parts[0] || '';
          var name = orgRepo.repo_name || parts[1] || '';
          if (owner && name) {
            sessionStorage.setItem('currentRepo', JSON.stringify({
              fullName: orgRepo.full_name, owner: owner, name: name,
              repoKey: orgRepo.repo_key || '', isOrgRepo: true, orgId: orgId
            }));
            _syncAppPath(owner, name);
            initSidebarRepos();
            _switchToast();
            nav('dash');
            return;
          }
        }
        // 组织仓库不存在（provisioning 未完成/失败）：先落到组织详情，便于查看状态。
        initSidebarRepos();
        _updateRepoOrgBadge();
        if (typeof _loadOrgSection === 'function') _loadOrgSection();
        _switchToast();
        _syncAppPathToRoot();
        nav('organizations', orgId);
      }).catch(function() {
        initSidebarRepos();
        _switchToast();
        _syncAppPathToRoot();
        nav('organizations', orgId);
      });
    }

    function _loadOrgReposForSwitch(_isPersonal) {
      API.get('/repos/member').then(function(d) {
        var repos = (d && d.repos) || [];
        try { localStorage.setItem(REPO_CACHE_KEY, JSON.stringify({repos: repos, ts: Date.now()})); } catch(e) {}
        var memberScoped = _scopeRepos(repos.filter(function(r) { return r.is_hosted || r.is_joined; }),
                                        orgId, _isPersonal);
        API.get('/repos/managed').then(function(md) {
          var orgRepos = _scopeRepos(_orgScopedRepos(md), orgId, _isPersonal);
          _applyOrgRepoSelection(orgRepos.length ? orgRepos : memberScoped);
        }).catch(function() {
          _applyOrgRepoSelection(memberScoped);
        });
      }).catch(function() {});
      _switchToast();
    }

    var _knownOrg = (window._orgMetaById || {})[String(orgId)];
    if (_knownOrg) {
      if (!_knownOrg.is_personal) { _enterTeamOrgWorkbench(); return; }
      _loadOrgReposForSwitch(true);
      return;
    }
    // 组织信息还没缓存（例如从其它页面直接切）：补一次 /orgs 再决定落点。
    API.get('/orgs').then(function(orgs) {
      _cacheOrgs(orgs);
      var target = (orgs || []).find(function(o) { return String(o.id) === String(orgId); });
      if (target && !target.is_personal) { _enterTeamOrgWorkbench(); return; }
      _loadOrgReposForSwitch(true);
    }).catch(function() { _loadOrgReposForSwitch(true); });
  }

  const REPO_CACHE_KEY = 'repo_member_cache';

  // 关系守卫（工作区 / 我的项目 / 项目模板共用）：进入任何「需要仓库」的页面之前，
  // 先取一次最新关系，没有关系就直接渲染绑定引导页。
  // 2026-11-16 实锤：refreshRepoLinks 此前**从未被调用**，快照只来自登录那一刻——
  // 用户清完 PAT 后前端仍认为已接入，于是"我的项目/项目模板"能进、进去后服务端报
  // 授权过期/空列表，两个页面互相打脸。
  function _ensureRepoLinksFresh(maxAgeMs) {
    var age = Date.now() - (_linksLoadedAt || 0);
    if (window.__repoPlatformLinks && age < (maxAgeMs || 0)) {
      return Promise.resolve(window.__repoPlatformLinks);
    }
    return refreshRepoLinks().then(function(links) {
      return links || window.__repoPlatformLinks || null;
    });
  }

  function loadRepos(forceRefresh) {
    console.log("[loadRepos] start");
    // 我的项目 / 项目模板 / 工作区入口统一守卫：关系为空 → 只渲染引导页，不拉仓库。
    // （重入保护：引导页本身不调用 loadRepos，不会递归）
    _ensureRepoLinksFresh(0).then(function(links) {
      var _broken = (((links && links.credential_broken) || []).length
                     || Object.keys((links && links.credential_error) || {}).length) > 0;
      if (!(links && links.has_any) && !_broken) {
        goToRepos();
        return;
      }
      _loadReposInner(forceRefresh);
    }).catch(function() { _loadReposInner(forceRefresh); });
    return;
  }

  function _loadReposInner(forceRefresh) {
    // 先用 localStorage 缓存的仓库列表立即渲染（避免白屏），后台再刷新
    let cachedRepos = null;
    try {
      const c = JSON.parse(localStorage.getItem(REPO_CACHE_KEY) || 'null');
      if (c && c.repos && Array.isArray(c.repos)) cachedRepos = c.repos;
    } catch (e) { cachedRepos = null; }

    if (cachedRepos && !forceRefresh) {
      // Issue #82: cachedRepos 路径也需 _currentView 守卫，与 API 回调路径一致——
      // 用户已进入某页面时不应覆盖布局 DOM，只更新侧边栏下拉
      if (window._currentView && window._currentView !== '') {
        initSidebarRepos();
      } else {
        renderReposFromData(cachedRepos);
      }
    }

    API.get('/repos/member').then(d => {
      const repos = d.repos || [];
      // 更新 localStorage 缓存（记录时间）
      try {
        localStorage.setItem(REPO_CACHE_KEY, JSON.stringify({repos, ts: Date.now()}));
      } catch (e) {}
      if (cachedRepos && !forceRefresh) {
        // 后台刷新后若数据变化，轻量更新侧边栏
        initSidebarRepos();
      } else {
        // Issue #63: 异步回调里调 renderReposFromData 会操作布局 DOM
        // 如果 nav 已经执行了（_currentView 已设），说明用户已进入某页面，
        // 不应覆盖——只更新缓存，不渲染
        if (window._currentView && window._currentView !== '') {
          initSidebarRepos();
        } else {
          renderReposFromData(repos);
        }
      }
    }).catch(e => {
      if (cachedRepos) return; // 有缓存时静默，避免覆盖已渲染内容
      console.error('[loadRepos] FAILED:', e.name, e.message);
    });
  }

  function _forceSyncRepos() {
    // 绑定新平台后强制同步仓库列表（调 /repos/sync 不走缓存）
    API.post('/repos/sync').then(function(d) {
      var repos = d.repos || [];
      try {
        localStorage.setItem(REPO_CACHE_KEY, JSON.stringify({repos: repos, ts: Date.now()}));
      } catch(e) {}
      // R7: 清除内存缓存，防止语言切换用旧数据渲染
      _cachedReposData = null;
      renderReposFromData(repos);
    }).catch(function(e) {
      // 同步失败则降级为普通加载
      loadRepos(true);
    });
  }

  function renderReposFromData(repos) {
    const managedRepos = repos.filter(r => r.is_joined || r.is_hosted);
    console.log("[loadRepos] repos", JSON.stringify({total: repos.length, managed: managedRepos.length}));
    initSidebarRepos();
    const current = currentRepo();
    // 引擎兜底引导仅服务"已进入仓库上下文（有选中仓库）"的场景（dash 渲染时
    // dashboard.js 会调 app._checkEngineBanner 补位）。此处调用只覆盖"有仓库
    // 工作台"分支的进入路径；零托管仓库分支（managedRepos.length===0，落 repos
    // 项目大厅）不弹——该场景用户的第一要务是托管仓库，引擎引导属于超前打扰
    //（历史注释曾写"两个分支都需要兜底引导"，与 _checkEngineBanner 的
    // NO_REPO_VIEWS 视图守卫矛盾，已修正——守卫本来就会拦掉 repos 视图）。
    if (managedRepos.length > 0) _checkEngineBanner();

    if (managedRepos.length === 0) {
      _pushHistory('repos', null);
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      $('mainView').style.display = 'flex';
      $('mainView').style.position = 'fixed';
      $('mainView').style.inset = '0';
      $('mainView').style.zIndex = '100';
      $('pageTitle').textContent = I18N.t('app.current_project');
      // 已绑定但无仓库 → 显示项目页空态（不是新手引导）
      renderRepoSelection(repos);
      return;
    }

    $('sidebar').style.display = 'flex';
    $('tb').style.display = 'flex';
    $('mainView').style.display = 'flex';
    $('mainView').style.position = '';
    $('mainView').style.inset = '';
    $('mainView').style.zIndex = '';
    $('pageTitle').textContent = I18N.t('app.current_project');

    // Issue #82: 仅在完全无选中仓库时才默认选第一个；
    // current 存在但不在缓存列表中时保留用户选择不覆写（避免刷新后跳变为列表首位）
    if (!current) {
      const r = managedRepos[0];
      if (r) {
        // 组织归属一并落快照：否则顶栏/仓库下拉不知道这个仓库属于哪个组织
        const _oid = String(r.org_id ||
          (window._managedRepoOrgMap && window._managedRepoOrgMap[r.full_name]) || '');
        sessionStorage.setItem('currentRepo', JSON.stringify({
          fullName: r.full_name, owner: r.owner, name: r.name, repoKey: r.repo_key || '',
          isOrgRepo: !!(r.is_org_repo ||
            (window._managedRepoFlags && window._managedRepoFlags[r.full_name])),
          orgId: _oid
        }));
        _updateTbOrgName();
      }
      else sessionStorage.removeItem('currentRepo');
    }
    // Issue #278: 刷新后保持当前 URL 路由——若 hash 已指向有效子页面则导航到该页，
    // 仅当无有效 hash 时才回退默认 Dashboard，避免刷新被无条件重定向到 dash。
    var _h = window.location.hash;
    var _restored = false;
    if (_h && _h.length > 1) {
      var _hp = _h.substring(1).split('/');
      var _hv = _hp[0];
      // 包含 VIEWS 数组 + nav() 视图开关中的额外视图（不在 VIEWS 但有对应渲染分支）
      var _validHashViews = VIEWS.concat(['skill-sub', 'scene-detail-local', 'autopilot', 'autopilot-detail', 'organizations', 'org-pipeline']);
      if (_validHashViews.indexOf(_hv) !== -1) {
        nav(_hv, _hp[1] ? decodeURIComponent(_hp[1]) : undefined);
        _restored = true;
      }
    }
    if (!_restored) nav('dash');
  }

  function initSidebarRepos() {
    // Lightweight version for after repo selection - just populate sidebar dropdown
    // 先拿组织清单：仓库下拉要按当前组织作用域过滤，且 currentRepo 可能缺 org_id
    //（深链 / 旧会话快照），需要用 /repos/managed 的 org_id 回填后再决定组织上下文。
    return _fetchOrgs().then(function(orgs) {
      return API.get('/repos/managed').then(d => {
        const repos = d.repos || [];
        window._managedRepoMap = {};
        window._managedRepoFlags = {};
        window._managedRepoOrgMap = {};
        repos.forEach(r => {
          window._managedRepoMap[r.full_name] = r.repo_key || '';
          window._managedRepoFlags[r.full_name] = !!r.is_org_repo;
          window._managedRepoOrgMap[r.full_name] = r.org_id || '';
        });
        // 回填当前仓库的组织归属，让顶栏组织名与仓库下拉作用域跟当前仓库一致
        var _cur = currentRepo();
        if (_cur && _cur.fullName && !_cur.orgId) {
          var _curOrg = window._managedRepoOrgMap[_cur.fullName] || '';
          if (_curOrg) {
            _cur.orgId = _curOrg;
            _cur.isOrgRepo = !!(window._managedRepoFlags[_cur.fullName] || _cur.isOrgRepo);
            sessionStorage.setItem('currentRepo', JSON.stringify(_cur));
          }
        }
        _ensureOrgContext(orgs);
        if (repos.length === 0) {
          const sel = $('repoSelect');
          if (sel) {
            sel.innerHTML = '<option value="__add__">＋ ' + (I18N.t('app.repo.add_new') || '新增仓库托管') + '</option>';
            sel.onclick = function() { switchRepo('__add__'); };
          }
          _updateRepoOrgBadge();
          _updateTbOrgName();
          return;
        }
        _renderSidebarRepoOptions(repos);
        _updateTbOrgName();
      });
    }).catch(() => {});
  }

  function _updateSidebarRepos(repos) {
    window._managedRepoMap = window._managedRepoMap || {};
    window._managedRepoFlags = window._managedRepoFlags || {};
    window._managedRepoOrgMap = window._managedRepoOrgMap || {};
    (repos || []).forEach(function(r) {
      window._managedRepoMap[r.full_name] = r.repo_key || window._managedRepoMap[r.full_name] || '';
      window._managedRepoFlags[r.full_name] = !!(r.is_org_repo || window._managedRepoFlags[r.full_name]);
      window._managedRepoOrgMap[r.full_name] = r.org_id || window._managedRepoOrgMap[r.full_name] || '';
    });
    _renderSidebarRepoOptions(repos || []);
  }

  function switchRepo(val) {
    if (!val) return;
    if (val === '__add__') {
      // Reset dropdown to current repo
      const cur = currentRepo();
      const sel = $('repoSelect');
      if (cur) sel.value = cur.fullName;
      // 与点击 Grape Logo「返回仓库选择大堂」一致：进入全屏仓库选择大堂（隐藏侧边栏）
      goToRepos();
      return;
    }
    const parts = val.split('/');
    const _rk = (window._managedRepoMap && window._managedRepoMap[val]) || '';
    const _isOrg = !!(window._managedRepoFlags && window._managedRepoFlags[val]);
    const _repoOrgId = (window._managedRepoOrgMap && window._managedRepoOrgMap[val]) || '';
    sessionStorage.setItem('currentRepo', JSON.stringify({
      fullName: val, owner: parts[0], name: parts[1], repoKey: _rk,
      isOrgRepo: _isOrg, orgId: _repoOrgId
    }));
    // 已有组织上下文（含个人）时不要被仓库归属反向改写：个人空间允许选择
    // 任意托管仓库（含被团队组织关联的）。仅上下文缺失时用仓库归属兜底。
    if (!localStorage.getItem('current_org_id') && _repoOrgId) {
      localStorage.setItem('current_org_id', _repoOrgId);
    }
    _syncAppPath(parts[0], parts[1]);   // 路径跟随仓库，深链分享不会指错仓
    // 同步侧边栏下拉选中项
    const sel = $('repoSelect');
    if (sel) sel.value = val;
    _updateRepoOrgBadge();
    _updateTbOrgName();
    nav('dash');
  }

  // 点击 Grape Logo → 返回仓库选择页（与解除托管后跳转的页面完全一致：全屏卡片页）
  // 折中方案：先叠轻量 loading 遮罩（不改变当前布局），API 返回后一次性切换 UI，避免中间态闪烁
  // 需求 18: OAuth 首次登录补全提示条
  function _checkOAuthCompletion(profile) {
    // P0-4: 检测 OAuth 登录（exchange_code 新流程或 token 旧流程）
    const params = new URLSearchParams(window.location.search);
    const isOAuthLogin = params.has('exchange_code') || params.has('token');
    // 也检查 profile 中的 email_verified 标记（exchange_code 流程下 URL 参数已被清除）
    const isEmailUnverified = profile.email_verified === 0 && !profile.email;

    if (!isOAuthLogin && !isEmailUnverified) return;  // 非 OAuth 登录且邮箱已验证 → 不弹

    // 检查是否已跳过
    const user = JSON.parse(localStorage.getItem('user') || '{}');
    const skipKey = 'oauth_completion_skipped_' + (user.username || '');
    if (localStorage.getItem(skipKey)) return;

    // 检查账号完整度
    const needsPassword = !user.has_password;
    const needsEmail = !user.email;
    const needsVerify = user.email_verified === 0;
    if (!needsPassword && !needsEmail && !needsVerify) return;  // 已完整

    // 显示提示条
    const existing = document.getElementById('oauthCompletionBanner');
    if (existing) return;  // 已显示

    const banner = document.createElement('div');
    banner.id = 'oauthCompletionBanner';
    banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:var(--warn-bg, rgba(255,204,0,0.12));border-bottom:1px solid var(--warn-border, rgba(255,204,0,0.3));padding:10px 20px;display:flex;align-items:center;justify-content:center;gap:12px;font-size:13px;color:var(--t)';
    var msg;
    if (needsEmail) {
      msg = I18N.t('app.login.oauth_completion_hint');
    } else if (needsVerify) {
      msg = I18N.t('app.login.email_unverified_hint') || I18N.t('app.login.oauth_completion_hint');
    } else {
      msg = I18N.t('app.login.oauth_completion_hint');
    }
    banner.innerHTML =
      '<span>' + msg + '</span>' +
      '<button class="bt bt-p" style="font-size:12px;padding:4px 12px" onclick="app.nav(\'account\',\'security\')">' + I18N.t('app.login.go_to_settings') + '</button>' +
      '<button class="bt bt-o" style="font-size:12px;padding:4px 12px" onclick="app._dismissOAuthCompletion()">' + I18N.t('app.login.skip') + '</button>';
    document.body.appendChild(banner);
    // 优先级显式化（替代隐式 z-index 遮挡）：安全横幅显示前主动摘掉引擎横幅。
    // 此前两横幅靠 9999 盖 9998 维持互斥，引擎横幅会作为"幽灵 DOM"留在场上，
    // 补位链任一环漏调即成不可见残留；显式摘除后互斥由代码保证，不靠运气。
    var _eb = document.getElementById('engineBanner');
    if (_eb) _eb.remove();
    document.body.style.paddingTop = '44px';
  }

  function _dismissOAuthCompletion() {
    const user = JSON.parse(localStorage.getItem('user') || '{}');
    const skipKey = 'oauth_completion_skipped_' + (user.username || '');
    localStorage.setItem(skipKey, '1');
    const banner = document.getElementById('oauthCompletionBanner');
    if (banner) banner.remove();
    // 对称复位：engineBanner 可能在场（本次安全横幅是补位/并存路径弹出的），
    // 无条件清 paddingTop 会把内容顶进残留横幅的遮挡区（见 _hideEngineBannerOnly）
    if (!document.getElementById('engineBanner')) {
      document.body.style.paddingTop = '';
    }
    // 优先级补位：安全横幅退出后，被抑制的引擎引导立即补检（幂等——
    // _checkEngineBanner 内部有 DOM/needs_worker/skip-key 多重守卫，不会重复弹）
    setTimeout(function() { _checkEngineBanner(); }, 100);
  }

  function _renderBindingGuide() {
    var isZh = I18N.getLang() === 'zh';
    var _u = JSON.parse(localStorage.getItem('user') || '{}');
    var name = (_u.nickname || _u.username || 'User').split('@')[0];
    var avatar = (name || 'U')[0].toUpperCase();

    // D102-K：引导页=仓库平台绑定，数据源必须是仓库平台卡目录
    //（/settings/vcs/platforms）——此前误用 /auth/providers（登录渠道列表），
    // 华为等纯登录渠道混进"绑定代码平台"卡片。
    // 并拉 /auth/providers 仅取各平台 OAuth 配置态（登录渠道轴的正确用途）。
    Promise.all([
      API.get('/settings/vcs/platforms'),
      API.get('/auth/providers').catch(function() { return { providers: [] }; })
    ]).then(function(results) {
      var _oauthCfg = {};
      ((results[1] && results[1].providers) || []).forEach(function(p) {
        _oauthCfg[p.provider] = !!p.configured;
      });
      var providers = ((results[0] && results[0].platforms) || []).map(function(p) {
        return {provider: p.provider, name: p.name, brand_color: p.brand_color || '',
                pat_create_url: p.pat_create_url,
                configured: !!_oauthCfg[p.provider]};
      });
      // #535 修复：写入共享缓存 _accountProvidersData——引导页 → bindProvider →
      // _accountBindProvider 读同一缓存判断 OAuth tab；此前只有账号安全 tab 填充它，
      // 引导路径缓存为 null 被保守猜成「未配置」（配合 account.js 的 fetch 兜底双保险）
      if (typeof _accountProvidersData !== 'undefined') _accountProvidersData = d;
      _renderOnboardingShell(providers, isZh, name, avatar);
    }).catch(function() {
      // 目录接口失败 → 空壳渲染（不做平台名兜底：硬编码双平台会让纯 PAT 部署
      // 或新接入平台出现错误候选）
      _renderOnboardingShell([], isZh, name, avatar);
    });
  }

  function _renderOnboardingShell(providers, isZh, name, avatar) {
    var d = document.getElementById('pageContent');
    if (!d) return;

    // D102-O 续：引导壳是全屏独立页，不嵌在工作区布局里（幂等，多入口行为归一）
    var _sb = document.getElementById('sidebar');
    if (_sb) _sb.style.display = 'none';
    var _tbEl = document.getElementById('tb');
    if (_tbEl) _tbEl.style.display = 'none';
    var _mv = document.getElementById('mainView');
    if (_mv) { _mv.style.position = 'fixed'; _mv.style.inset = '0'; }

    // 2026-11-14：引导页整页 Vue 化（view chunk onboarding-view.js）——
    // 卡片渲染、绑定弹层（OAuth + PAT 双模式）、回跳成功态都由组件负责，
    // 本函数只保留 shell 布局接管与挂载入口，不再字符串拼 DOM。
    d.classList.add('ob-mode');
    // view chunk 是 <script type="module">（可能晚于 defer 的 app.js 执行）——
    // 轮询等待入口就绪再挂载，避免竞态导致引导页空白/点了没响应。
    var _payload = {
      providers: providers || [],
      name: name || '',
      avatar: avatar || '',
      lang: I18N.getLang()
    };
    var _tries = 0;
    (function _mountOnboarding() {
      if (typeof window.renderOnboarding === 'function') {
        window.renderOnboarding(_payload);
        return;
      }
      if (_tries++ > 200) {   // ~10s 兜底：chunk 加载失败要让用户看见，而不是白屏
        d.innerHTML = '<div style="padding:40px;text-align:center;color:var(--t2)">'
          + (I18N.t('app.login.ob_chunk_failed') || 'onboarding view failed to load')
          + '</div>';
        return;
      }
      setTimeout(_mountOnboarding, 50);
    })();
    _updateTbOrgName();
    _updateLobbyOrgName();
  }

  function _copyEngineCmd(btn) {
    var code = btn.parentElement && btn.parentElement.querySelector('code');
    var text = code ? code.textContent : '';
    if (navigator.clipboard && text) {
      navigator.clipboard.writeText(text).then(function() {
        var old = btn.textContent; btn.textContent = I18N.t('app.login.ob_engine_copied');
        setTimeout(function() { btn.textContent = old; }, 1500);
      });
    }
  }

  // ── 引擎不可用横幅（已绑定用户兜底引导，B 场景）──
  // 引导页卡片只覆盖"无绑定新用户"；已绑定用户直接进工作台，引擎不可用时
  // 由本横幅兜底（可关闭并记忆，复用 OAuth 补全横幅的 localStorage 模式）。
  // worker 上线后 needs_worker=false → 横幅自动不再出现（每次进工作台重查）。
  function _checkEngineBanner() {
    // 时序链（最终版）：token → 仓库 → 引擎。引擎引导只在用户已进入仓库
    // 上下文（选中了具体仓库）后触发——此时 token 与仓库均就绪，缺的只是
    // 引擎。仓库列表/绑定后不弹（避免在没托管仓库前提引擎）。
    if (!_hasVcsBinding()) return;  // 无接入关系：引导页专心配 token（PAT 接入同样算已接入）
    if (!currentRepo()) return;                     // 未进入仓库：不提引擎
    // 读取侧与写入侧（_dismissEngineBanner）同口径：从 localStorage.user 取 username。
    // 曾写成 `_bu.username`，而 `_bu` 在整个前端从未定义 → ReferenceError 在这里抛出，
    // 把 renderReposFromData 后半段（含结尾的 nav('dash')）整段打断：
    // _currentView 永远是空、主区永远是 showApp 写的「加载中」占位，
    // 需求中心等所有视图都进不去（2026-11-18 定位）。
    var _skipUser = {};
    try { _skipUser = JSON.parse(localStorage.getItem('user') || '{}') || {}; } catch (e) {}
    if (localStorage.getItem('engine_banner_skipped_' + (_skipUser.username || ''))) return;
    API.get('/runtime/engine-onboarding').then(function(d) {
      if (!d || !d.needs_worker) return;
      // 视图守卫：引擎引导是仓库维度的提示，非仓库视图（用户设置/任务/事件等）
      // 不弹——与 runtimes 页（页内已有空态引导）同一类排除，复用 NO_REPO_VIEWS
      // 单一事实源。currentRepo() 读的是 localStorage 持久化的
      // "最近选中仓库"，不代表用户当前正在看仓库视图，故必须另行按 _currentView 判断。
      if (NO_REPO_VIEWS.indexOf(window._currentView) >= 0) return;
      if (document.getElementById('engineBanner')) return;  // 已显示
      // 全局横幅优先级（Adobe Spectrum 替换式，评审采纳）：安全补全 > 引擎引导。
      // 安全横幅在场时引擎引导不显示（单向抑制，不互推不偏移）；
      // 其退出路径会主动调 _checkEngineBanner() 补位（见 _dismissOAuthCompletion/
      // account 安全设置完成后的 nav 返回）。
      var _ob = document.getElementById('oauthCompletionBanner');
      if (_ob) {
        // 排队可见：安全横幅在场 → 引擎引导被抑制，但在密码条上给出轻量指示
        // （纯文本 chip，无按钮无交互）——用户处理完安全条，补位检查会自动接上。
        // 不加指示时用户对"后面还有一条提示在等"零预期，补位横幅出现像惊吓。
        if (!document.getElementById('engineBannerQueued')) {
          var _chip = document.createElement('span');
          _chip.id = 'engineBannerQueued';
          _chip.style.cssText = 'font-size:11px;color:var(--t3);opacity:.8;margin-left:8px;padding:2px 10px;border:1px solid var(--b);border-radius:999px;white-space:nowrap';
          _chip.textContent = I18N.t('app.login.engine_banner_queued_hint') || '+1';
          _ob.appendChild(_chip);
        }
        return;
      }
      var banner = document.createElement('div');
      banner.id = 'engineBanner';
      banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9998;background:rgba(255,204,0,0.12);border-bottom:1px solid rgba(255,204,0,0.3);padding:10px 20px;display:flex;align-items:center;justify-content:center;gap:12px;font-size:13px;color:var(--t)';
      banner.innerHTML =
        '<span>' + I18N.t('app.login.ob_engine_banner_hint') + '</span>'
        + '<button class="bt bt-p" style="font-size:12px;padding:4px 12px" onclick="app._gotoEnginePage()">' + I18N.t('app.login.ob_engine_banner_btn') + '</button>'
        + '<button class="bt bt-o" style="font-size:12px;padding:4px 12px" onclick="app._dismissEngineBanner()">' + I18N.t('app.login.skip') + '</button>';
      document.body.appendChild(banner);
      document.body.style.paddingTop = '44px';
    }).catch(function() { /* 静默 */ });
  }

  function _dismissEngineBanner() {
    var user = JSON.parse(localStorage.getItem('user') || '{}');
    localStorage.setItem('engine_banner_skipped_' + (user.username || ''), '1');
    var b = document.getElementById('engineBanner');
    if (b) b.remove();
    // 跳过路径同样不能无条件清 paddingTop（安全横幅可能在场，见 _hideEngineBannerOnly）
    if (!document.getElementById('oauthCompletionBanner')) {
      document.body.style.paddingTop = '';
    }
  }

  // 仅隐藏引擎横幅（不写 skip-key）：供 nav 切到非仓库视图时退场用。
  // paddingTop 复位必须带 oauthCompletionBanner 在场检查——两条横幅共用同一个
  // body padding（44px），无条件清零会把安全横幅下面的页面内容顶进遮挡区。
  // 反向同理：_dismissOAuthCompletion / _checkOAuthCompletion 各自复位前先看
  // 对方是否在场，双向闭合，杜绝"内容被横幅盖住 44px"的竞态。
  function _hideEngineBannerOnly() {
    var b = document.getElementById('engineBanner');
    if (!b) return;
    b.remove();
    if (!document.getElementById('oauthCompletionBanner')) {
      document.body.style.paddingTop = '';
    }
  }

  function _gotoEnginePage() {
    // 时序引导（需求修订）：绑定完成后直接进入引擎页面——那里有完整的
    // 接入 UI（安装命令/复制/凭据管理），不在横幅弹窗里堆命令文本。
    // 跳转后自动展开"添加设备"面板（runtimes.js 渲染完成后消费此标记）
    _dismissEngineBanner();
    window._rtOpenPanelOnEnter = true;
    nav('runtimes');
  }

  // ── 离开引导页时清理标记 class ──
  function _obExitMode() {
    var d = document.getElementById('pageContent');
    if (d) d.classList.remove('ob-mode');
  }

  // ── 欢迎区域（单屏居中）──
  function _esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // 入口守卫：关系快照过期就先刷新再决定渲染 lobby 还是引导页
  // （清 PAT 后旧快照会让 "我的项目/项目模板" 还能进来，进去后服务端又报无凭据）
  function goToRepos() {
    var stale = Date.now() - (_linksLoadedAt || 0) > 30000;
    if (stale) {
      return refreshRepoLinks().then(function() { _goToReposInner(); })
        .catch(function() { _goToReposInner(); });
    }
    return _goToReposInner();
  }

  function _goToReposInner() {
    _obExitMode();
    // 关系在、但凭据不可用（历史派生密钥解不出来 / refresh 缺失）→ 明确提示重新授权，
    // 而不是让用户以为"没有绑定"（2026-11-16 yangaiwu 现场：两处结论相反）
    try {
      var _rl = window.__repoPlatformLinks;
      if (_rl && _rl.needs_reauth && _rl.needs_reauth.length && _rl.hint) showToast(_rl.hint);
    } catch (e) {}
    _syncAppPathToRoot();
    _pushHistory('repos', null);
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    localStorage.setItem('sidebarTab', 'projects');
    window._currentView = 'repos';

    // 叠加轻量 loading 遮罩，不改动当前 DOM 布局
    let ov = document.getElementById('gotoReposOverlay');
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'gotoReposOverlay';
      ov.style.cssText = 'position:fixed;inset:0;z-index:9998;background:rgba(255,255,255,.45);display:flex;align-items:center;justify-content:center;font-size:13px;color:var(--t2)';
      ov.textContent = I18N.t('common.loading');
      document.body.appendChild(ov);
    }

    // D9（D109 收口）: 无仓库平台关系时直接显示绑定引导，不白调 /repos/member；
    // 判据 = 对象模型关系表（_hasVcsBinding），不是 identities 绑定数量。
    if (!_hasVcsBinding()) {
      // 未绑定任何平台 → 直接显示新手引导页（不渲染 lobby 壳）
      if (ov) ov.remove();
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      const mv = $('mainView');
      mv.style.display = 'flex';
      mv.style.position = 'fixed';
      mv.style.inset = '0';
      mv.style.zIndex = '100';
      $('pageTitle').textContent = I18N.t('app.current_project');
      _renderBindingGuide();
      return;
    }

    // 已绑定 → 调 /repos/member 加载仓库列表
    API.get('/repos/member').then(d => {
      if (window._currentView !== 'repos') return;  // stale guard
      // 缓存数据供语言切换时轻量重渲染使用（不发 API 请求）
      _cachedReposData = d;
      // 数据就绪，一次性切换 UI
      if (ov) ov.remove();
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      const mv = $('mainView');
      mv.style.display = 'flex';
      mv.style.position = 'fixed';
      mv.style.inset = '0';
      mv.style.zIndex = '100';
      $('pageTitle').textContent = I18N.t('app.current_project');
      renderRepoSelection(d.repos || []);
      // 加载 lobby 组织下拉名
      _updateTbOrgName();
      _updateLobbyOrgName();
    }).catch(e => {
      if (window._currentView !== 'repos') return;  // stale guard
      if (e.silent) return;  // Issue #372 §40: 外部 abort 静默，不显示错误
      if (ov) ov.remove();
      $('sidebar').style.display = 'none';
      $('tb').style.display = 'none';
      const mv = $('mainView');
      mv.style.display = 'flex';
      mv.style.position = 'fixed';
      mv.style.inset = '0';
      mv.style.zIndex = '100';
      $('pageTitle').textContent = I18N.t('app.current_project');
      // Token 未配置 → 显示新手引导页（不渲染 lobby 壳）
      if (e.message && (e.message.indexOf('Token') >= 0 || e.message.indexOf('未配置') >= 0)) {
        _renderBindingGuide();
        return;
      }
      const pc = $('pageContent');
      if (pc) pc.innerHTML = '<div style="max-width:900px;margin:0 auto;padding:40px 0"><div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div></div>';
    });
  }

  // UC16e：dashboard 轮询定时器（showLogin 统一收口清理，声明前置避免 TDZ 风险）
  let _refreshTimer = null;
  let _currentParam = null;
  let _hiddenSince = 0;
  // 语言切换时缓存 /repos/member 数据，轻量重渲染不发 API 请求
  let _cachedReposData = null;

  // R3: 清理内存缓存 _cachedReposData（供 account.js 等外部调用）
  function _clearCachedReposData() {
    _cachedReposData = null;
  }

  // _rerenderReposView: 语言切换时轻量重渲染项目列表页/引导页
  // 不发 API 请求、不闪烁 loading 遮罩——用缓存数据重新执行 HTML 拼接渲染
  function _rerenderReposView() {
    if (!_hasVcsBinding()) {
      // 未绑定 → 重新渲染引导页（引导页内部调 /auth/providers，是轻量请求）
      _renderBindingGuide();
      return;
    }
    // 已绑定 → 用缓存数据重新渲染仓库列表（不发 /repos/member）
    if (_cachedReposData) {
      $('pageTitle').textContent = I18N.t('app.current_project');
      renderRepoSelection(_cachedReposData.repos || []);
      _updateTbOrgName();
      _updateLobbyOrgName();
    } else {
      // 无缓存数据（首次加载失败等）→ fallback 到完整 goToRepos
      goToRepos();
    }
  }
  // 页面从后台回到前台时，若隐藏超过 30 秒才刷新（避免短时间切换标签页频繁刷新）
  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState === 'hidden') {
      _hiddenSince = Date.now();
    } else if (document.visibilityState === 'visible' && window._currentView && _hiddenSince) {
      // Issue #86: Tab 切回前台——重设定时器（后台 setTimeout 被浏览器节流可能没触发）
      API.scheduleProactiveRefresh();
      // Issue #407: 编辑态下不重渲染（避免未保存的表单内容丢失）
      //   - _dashboardEditing: dashboard 场景编辑/创建模式（PR #598）
      //   - _chatEditing: ChatAgent 图片/视频附件 pending（本次扩展，附件闲置返回不丢）
      if (Date.now() - _hiddenSince > 60000 && !(window._dashboardEditing || window._chatEditing)) {
        var _v = window._currentView || '';
        var _baseView = _v.split(':')[0];
        setTimeout(function() { if (typeof app !== 'undefined' && app.nav) app.nav(_baseView, window._currentParam); }, 300);
      }
      _hiddenSince = 0;
    }
  });
  // ── Sidebar 分组折叠（Issue #66）──
  // 可折叠分组标题带 class="grp-tog" + data-key（localStorage 键）。
  // 默认收起（HTML 中预置 collapsed）；initGroupCollapse() 读取 localStorage
  // 同步真实状态；toggleGroup() 点击切换并写回。收起时 .grp-items 子项隐藏。
  function _readCollapsed(key) {
    // 默认 true（收起）；localStorage 存 '0'/'1'
    try {
      const v = localStorage.getItem(key);
      return v === null ? true : v === '1';
    } catch (e) { return true; }
  }
  function initGroupCollapse() {
    document.querySelectorAll('.grp-tog[data-key]').forEach(tog => {
      const key = tog.getAttribute('data-key');
      const collapsed = _readCollapsed(key);
      tog.classList.toggle('collapsed', collapsed);
      const items = tog.nextElementSibling;
      if (items && items.classList.contains('grp-items')) {
        items.classList.toggle('hid', collapsed);
      }
    });
  }
  function toggleGroup(tog) {
    const key = tog.getAttribute('data-key');
    const collapsed = !tog.classList.contains('collapsed');
    tog.classList.toggle('collapsed', collapsed);
    const items = tog.nextElementSibling;
    if (items && items.classList.contains('grp-items')) {
      items.classList.toggle('hid', collapsed);
    }
    try { localStorage.setItem(key, collapsed ? '1' : '0'); } catch (e) {}
  }

  // ── Sidebar 折叠/展开（Issue #207）──
  // 桌面端：toggleSidebar() 切换 .sb.collapsed，宽度收为 0，主内容 .mn 自适应扩展。
  // 窄屏（≤768px，CSS 已 display:none）：toggleSidebar() 以 overlay 抽屉模式临时展开 sidebar。
  // 状态通过 localStorage('grapeSidebarCollapsed') 持久化，刷新后恢复。
  // initSidebarState() 在启动时读取持久化状态并同步 class（仅桌面端；窄屏始终临时展开不持久化）。
  const SB_COLLAPSE_KEY = 'grapeSidebarCollapsed';
  const SB_NARROW_MAX = 768;  // 与 app.css @media(max-width:768px) 断点一致

  function _isNarrowScreen() {
    return window.innerWidth <= SB_NARROW_MAX;
  }

  function initSidebarState() {
    const sb = $('sidebar');
    if (!sb) return;
    let collapsed = false;
    try { collapsed = localStorage.getItem(SB_COLLAPSE_KEY) === '1'; } catch (e) {}
    // 桌面端：恢复持久化折叠状态（窄屏下 sidebar 由 CSS display:none 控制，不持久化）
    const shouldCollapse = !_isNarrowScreen() && collapsed;
    if (shouldCollapse) {
      // 临时禁用 transition 避免首屏闪缩动画（Issue #207 QA 反馈）
      sb.classList.add('no-transition');
    }
    sb.classList.toggle('collapsed', shouldCollapse);
    // 更新按钮图标方向（收起后图标可视觉提示）
    _updateToggleIcon(shouldCollapse);
    if (shouldCollapse) {
      // 下一帧恢复 transition（双 rAF 确保浏览器已应用无过渡状态）
      const restore = () => sb.classList.remove('no-transition');
      if (window.requestAnimationFrame) {
        window.requestAnimationFrame(() => window.requestAnimationFrame(restore));
      } else {
        setTimeout(restore, 0);
      }
    }
  }

  function _updateToggleIcon(collapsed) {
    const btn = $('sbToggle');
    if (!btn) return;
    btn.classList.toggle('collapsed', collapsed);
  }

  // forceClose：窄屏 overlay 模式下点击遮罩关闭抽屉（不写回持久化）
  function toggleSidebar(forceClose) {
    const sb = $('sidebar');
    if (!sb) return;
    if (_isNarrowScreen()) {
      // 窄屏 overlay 抽屉模式：临时展开/收起，不持久化
      const open = sb.classList.contains('sb-drawer-open');
      if (forceClose && !open) return;
      sb.classList.toggle('sb-drawer-open', !open);
      const ov = $('sbOverlay');
      if (ov) ov.classList.toggle('show', !open);
      return;
    }
    // 桌面端：切换 collapsed，写回 localStorage
    const collapsed = !sb.classList.contains('collapsed');
    sb.classList.toggle('collapsed', collapsed);
    _updateToggleIcon(collapsed);
    try { localStorage.setItem(SB_COLLAPSE_KEY, collapsed ? '1' : '0'); } catch (e) {}
  }

  // 窗口尺寸跨越断点时清理窄屏 overlay 状态，避免残留 class 影响桌面布局
  let _sbWasNarrow = _isNarrowScreen();
  window.addEventListener('resize', () => {
    const nowNarrow = _isNarrowScreen();
    if (_sbWasNarrow !== nowNarrow) {
      _sbWasNarrow = nowNarrow;
      const sb = $('sidebar');
      if (sb) {
        sb.classList.remove('sb-drawer-open');
        if (!nowNarrow) {
          // 从窄屏切到桌面：恢复持久化状态
          let collapsed = false;
          try { collapsed = localStorage.getItem(SB_COLLAPSE_KEY) === '1'; } catch (e) {}
          sb.classList.toggle('collapsed', collapsed);
          _updateToggleIcon(collapsed);
        } else {
          sb.classList.remove('collapsed');
        }
      }
      const ov = $('sbOverlay');
      if (ov) ov.classList.remove('show');
    }
  });

  // 仓库维度页面拿到 403（仓库不属于当前用户 / 已解绑 / 已改名）：清掉当前仓库上下文
  // 并回仓库大厅，而不是把裸错误文本丢在内容区（2026-11-17 现场：用户从旧链接进到别人的
  // 仓库，流水线页只剩一行 "No permission to access this repository"，且 URL 仍钉着脏路径）。
  function _bailRepoForbidden() {
    try { sessionStorage.removeItem('currentRepo'); } catch (e) {}
    showToast(I18N.t('app.repo.forbidden') || '没有该仓库的访问权限，已切回仓库大厅');
    goToRepos();
  }

  function nav(view, param) {
    // Issue #3395：#plugins 深链（含 popstate 回放）在开关关闭时回退默认视图。
    if (view === 'plugins' && !PLUGINS_ENABLED) view = 'dash';
    // 组织流水线独立页面已下线：旧深链（#org-pipeline/<id>）回落到组织工作台详情。
    if (view === 'org-pipeline') view = 'organizations';
    // 离开引导页模式：清理 .ct.ob-mode 标记
    _obExitMode();
    // 引擎横幅退场（语义对齐 _gotoEnginePage）：无论从哪条路径进入引擎页
    // （侧栏菜单/直链），横幅使命已完成——移除并恢复 padding，与横幅按钮
    // 跳转后的行为一致；_checkEngineBanner 的 runtimes 守卫只防"新弹"，
    // 不退"已在场"的横幅（实测复现：侧栏进入后横幅残留）。
    if (view === 'runtimes') _dismissEngineBanner();
    // Issue #67: 已在 knowledge 页面且 Vue 应用存在时，不重复执行 nav（防止多次 mount 报错）
    if (view === 'knowledge' && window._currentView === 'knowledge' && typeof _knowledgeVueApp !== 'undefined' && _knowledgeVueApp) {
      return;
    }
    const repo = currentRepo();
    // 需求17: 未绑定平台守卫——依赖仓库的视图需检查 bindings
    var _repoDependentViews = ['dash', 'pipe', 'vod', 'issue', 'coding', 'qa', 'sre', 'chat', 'im-channel', 'skills', 'skill', 'knowledge', 'scene-market', 'scene-detail', 'scene-detail-local'];
    var _nonRepoViews = NO_REPO_VIEWS;
    if (!repo && _repoDependentViews.indexOf(view) >= 0) {
      // 检查是否未绑定
      // 判据用「用户↔仓库平台关系」（PAT 也算接入）；bindings 只有 OAuth 身份行，
      // 纯 PAT 账号会被误判成"未绑定"（2026-11-14 深度检查）
      if (!_hasVcsBinding()) {
        // 未绑定 → 提示用户并跳转到新手引导页
        var _isZh = I18N.getLang() === 'zh';
        showToast(I18N.t('app.login.link_platform_first'));
        goToRepos();
        return;
      }
      // 已绑定但无选中仓库 → loadRepos
      // 2026-11-17 现场：深链仓库无权时 currentRepo 被清空，此后每个仓库维度菜单
      // 都只是静默回仓库大厅——用户看到的现象是「点了没反应」。这里必须给一句话，
      // 否则"回到大厅"和"点了没生效"在视觉上无法区分。
      showToast(I18N.t('app.repo.pick_first') || '请先在仓库下拉里选择一个仓库');
      loadRepos();
      return;
    }
    if (!repo && _nonRepoViews.indexOf(view) < 0) {
      showToast(I18N.t('app.repo.pick_first') || '请先在仓库下拉里选择一个仓库');
      loadRepos();
      return;
    }

    // v31: 离开 Chat 页面时中断 SSE（但不清任务状态，后台任务继续跑）
    if (view !== 'chat' && typeof _chatStreamAbort !== 'undefined' && _chatStreamAbort) {
      _chatStreamAbort.abort();
      _chatStreamAbort = null;
    }
    // 离开事件详情时中断对话 SSE（后台任务继续跑）
    if (view !== 'event' && typeof _convStreamAbort !== 'undefined' && _convStreamAbort) {
      _convStreamAbort.abort();
      _convStreamAbort = null;
    }
    // Issue #342：离开事件详情立即停止 running 轮询定时器（不等下一 tick 自检，
    // 避免离开后 5s 内残留 tick 触发无效请求/局部更新）
    if (view !== 'event' && typeof _stopEventPoll === 'function') {
      _stopEventPoll();
    }

    // 技能详情返回：记录来源视图（pushHistory 之前 history.state 还是上一个视图）
    if (view === 'skill') {
      const _prev = window.history.state;
      window._skillDetailFrom = (_prev && _prev.view) ? {view: _prev.view, param: _prev.param || null} : null;
    }

    window._currentParam = param;
    _pushHistory(view, param);

    document.querySelectorAll('.sb-nav a').forEach(a => a.classList.remove('act'));
    const navEl = $('nav-' + view);
    if (navEl) navEl.classList.add('act');
    // 场景·技能页面内的 tab 切换（skills/skill/scene-detail）也高亮场景·技能菜单
    if (!navEl && (view === 'skills' || view === 'skill' || view === 'scene-detail')) {
      const marketNav = $('nav-scene-market');
      if (marketNav) marketNav.classList.add('act');
    }
    document.querySelectorAll('.sb-nav a').forEach(a => {
      const o = a.getAttribute('onclick') || '';
      if (param && o.includes("'" + param + "'")) a.classList.add('act');
      else if (!param && o === "nav('" + view + "')") a.classList.add('act');
    });
    // 导航到折叠分组内的项时自动展开该分组（Issue #66）
    const _actNav = document.querySelector('.sb-nav a.act');
    if (_actNav) {
      const _items = _actNav.closest('.grp-items');
      if (_items && _items.classList.contains('hid')) {
        const _tog = _items.previousElementSibling;
        if (_tog && _tog.classList.contains('grp-tog') && _tog.classList.contains('collapsed')) {
          toggleGroup(_tog);
        }
      }
    }

    const title = $('pageTitle');
    const repoTitle = $('repoTitle');
    const pc = $('pageContent');

    // Issue #67: 离开 knowledge 页面时先卸载 Vue 应用（必须在 innerHTML 替换之前）
    if (view !== 'knowledge' && typeof _knowledgeVueApp !== 'undefined' && _knowledgeVueApp) {
      try { _knowledgeVueApp.unmount(); } catch(e) {}
      _knowledgeVueApp = null;
    }
    // [沙箱迁移] 离开 sandbox 页面时卸载 Vue 应用（与 knowledge 一致，避免内存泄漏）
    if (view !== 'sandbox' && typeof _sandboxVueApp !== 'undefined' && _sandboxVueApp) {
      try { _sandboxVueApp.unmount(); } catch(e) {}
      _sandboxVueApp = null;
    }

    // Issue #67: 已在 knowledge 页面且 Vue 应用存在时，不覆盖 DOM（避免 Vue 虚拟 DOM 不一致）
    if (!(view === 'knowledge' && typeof _knowledgeVueApp !== 'undefined' && _knowledgeVueApp)) {
      pc.innerHTML = '<div class="loading">' + I18N.t('common.loading') + '</div>';
    }

    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    // Issue #63: 切 tab 时主动取消旧请求 + 设全局 signal + 停定时轮询
    if (window._navAbortController) window._navAbortController.abort();
    window._navAbortController = new AbortController();
    window._navSignal = window._navAbortController.signal;
    window._tabSignal = null;  // 重置 tab signal
    window._currentView = view;
    // 需求24: 进入非引导页时显示未绑定提示条（在 _currentView 设置后检查）
    _showBindBannerIfNeeded();
    // 引擎引导横幅是仓库维度的提示：进入非仓库视图时隐藏（仅摘 DOM+复位
    // paddingTop，不写 skip-key——"这里不显示"不等于"永久跳过"），返回仓库
    // 视图时既有补位调用（_accountGoBack/loadRepos）会按守卫自动重弹。
    // runtimes 页沿用既有 _dismissEngineBanner 语义（页内已有空态引导）。
    if (view !== 'runtimes' && _nonRepoViews.indexOf(view) >= 0) {
      _hideEngineBannerOnly();
      // 顺带清理过期意图：_rtOpenPanelOnEnter 是"跳转引擎页自动开面板"的一次性
      // 标记，若用户中途改道去了别的视图，标记会残留在 window 上——下次任意
      // 时刻进引擎页都会无故自动弹面板。nav 离开引导链即作废。
      window._rtOpenPanelOnEnter = false;
    }
    // 离开 repos 全屏页时恢复 sidebar/tb/mainView 默认布局
    // account/organizations 分支自行隐藏 sidebar，此处统一恢复 tb/mainView
    if (view !== 'repos') {
      var sb = $('sidebar'); if (sb) sb.style.display = 'flex';
      var tb = $('tb'); if (tb) tb.style.display = 'flex';
      var mv = $('mainView'); if (mv) { mv.style.position = ''; mv.style.inset = ''; mv.style.zIndex = ''; }
    }
    // 离开需求中心时停止场景识别轮询（该全局在 IssuesView 首次渲染后才存在，保留守卫）
    if (view !== 'tasks' && typeof _stopScenePoll === 'function') _stopScenePoll();
    // 离开 observability 时停止定时轮询（同上：渲染后才挂载）
    if (view !== 'observability' && typeof stopObsAuto === 'function') stopObsAuto();
    // 离开 chat 时停止飞书接入轮询（同上：渲染后才挂载）
    if (view !== 'chat' && typeof _stopOnboardPoll === 'function') _stopOnboardPoll();
    // 离开场景中心时清除编辑标志（防止 _dashboardEditing 泄漏导致重新进入时卡在加载中）
    window._dashboardEditing = false;
    // 同家族泄漏（D 审计，2026-10-28）：_isCreateMode 原先只在"返回按钮/创建成功/创建
    // 失败"三条路径重置，用户直接点侧栏离开场景创建流程时残留 true——下次渲染 dash
    // 智能体卡片仍按创建模式跳过 skill 名显示（幽灵行为，与 _rtOpenPanelOnEnter 同款）。
    // nav 离开即统一复位（dashboard.js 有 _resetCreateMode，但闭包外用 window 直清等效）
    window._isCreateMode = false;

    if (repo) {
      // F5 refresh: ensure sidebar repo dropdown is populated even when
      // currentRepo() is restored from sessionStorage (skips loadRepos path)
      if (!$('repoSelect') || $('repoSelect').options.length <= 1) initSidebarRepos();
      _updateRepoOrgBadge();
      if (_isTeamOrgContext()) {
        var _orgLink = _orgProjectLink();
        if (_isOrgRepo(repo)) {
          // 组织仓库：只展示组织项目（组织仓库自身就是当前工作台）。
          repoTitle.innerHTML = _orgLink;
        } else {
          // 组织下的关联仓库：组织项目在前，关联仓库 + 代码仓跳转在后。
          var _linkedLabel = I18N.t('organizations.detail.repo_linked') || '关联仓库';
          repoTitle.innerHTML = _orgLink
            + '<span style="margin:0 8px;color:var(--t3)">/</span>'
            + '<span style="color:var(--t3);font-weight:500;margin-right:5px">' + _linkedLabel + '</span>'
            + _platformRepoLink(repo);
        }
      } else if (_isOrgRepo(repo)) {
        repoTitle.innerHTML = _orgProjectLink();
      } else {
        repoTitle.innerHTML = _platformRepoLink(repo);
      }
      // 团队组织上下文：面包屑只用「组织项目 / 关联仓库」表达，不再重复“当前项目”。
      if (_isTeamOrgContext()) {
        var _pt = $('pageTitle');
        if (_pt) _pt.textContent = '';
      }
    } else {
      repoTitle.textContent = '';
      _updateRepoOrgBadge();
    }

    // 无仓库时的统一空态引导
    function _noRepoPlaceholder() {
      return '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:16px;padding:40px">' +
        '<div style="font-size:48px">📦</div>' +
        '<div style="font-size:16px;font-weight:600;color:var(--t1)">' + (I18N.t("organizations.switch_org.no_repos") || "该组织下暂无托管仓库") + '</div>' +
        '<button onclick="app.goToRepos()" style="padding:10px 24px;border:none;border-radius:10px;background:var(--a);color:#fff;cursor:pointer;font-size:13px;font-weight:600">' + (I18N.t("app.repo.back_to_select") || "返回选择仓库") + '</button>' +
        '</div>';
    }

    if (view === 'dash') {
      title.textContent = I18N.t('app.current_project');
      window._dashboardEditing = false;
      if (!repo) { pc.innerHTML = _noRepoPlaceholder(); return; }
      // Issue #501: 场景列表分页——不带 page 的老调用方不受影响，前端带 page 时后端按页切片
      // 切换仓库时页码/搜索词归位，避免带着旧仓库的页码拉新仓库（越界返回空列表）
      if (window._sceneListRepo !== (repo.fullName || '')) {
        window._sceneListRepo = repo.fullName || '';
        window._sceneListPage = 1;
        window._sceneListQ = '';
      }
      // 检索参数（Issue #501 增量）：q 为空串时不携带，保持 URL 简洁。
      // 必须在 loadDash 内部取值——闭包外取值会被 30s 自动刷新/_sceneReload 复用旧 q，静默丢搜索词
      const loadDash = () => {
        const _q = (window._sceneListQ || '').trim();
        const _qParam = _q ? '&q=' + encodeURIComponent(_q) : '';
        const _sort = window._sceneListSort || 'id';
        const _sortParam = _sort !== 'id' ? '&sort=' + encodeURIComponent(_sort) : '';
        return Promise.all([
        API.get('/dashboard/' + repo.owner + '/' + repo.name),
        // Issue #501 A 方案：卡片+技能链合并为 with-pipeline 单一来源（原 /scenes+with-pipeline
        // 双请求查同表同页数据双倍付费，30s 自动刷新每轮省 1 请求 1 次 DB 读）
        API.get('/scenes/with-pipeline?repo=' + encodeURIComponent(repo.owner + '/' + repo.name) + '&page=' + (window._sceneListPage || 1) + '&page_size=' + (window._sceneListPageSize || 20) + _qParam + _sortParam),
      ]).then(([dashData, scenesData]) => {
        if (window._currentView !== 'dash') return;  // stale guard
        // 编辑中不覆盖 UI，避免丢失用户正在进行的修改
        if (window._dashboardEditing) return;
        // Issue #501 深审优化：挂到 window 供场景分页条局部重载（替代整页 app.nav('dash')）
        window._sceneReload = loadDash;
        // R14.17: dashboard 成功加载时清除 403 去重列表，权限恢复后可切回之前 403 的仓库
        window._tried403Repos = null;
        dashData._scenes = scenesData.scenes || [];
        // 分页角标/分页条用跨页总数 total（老后端无 total 时回退当前页条数）
        dashData._scenesTotal = (typeof scenesData.total === 'number') ? scenesData.total : (scenesData.scenes || []).length;
        // A 方案：同一响应直接产出技能链映射，dashboard.js 不再二次拉 with-pipeline
        dashData._pipelines = {};
        (scenesData.scenes || []).forEach(s => { dashData._pipelines[s.id] = s.pipeline || {}; });
        dashData._owner = repo.owner;
        dashData._name = repo.name;
        renderDashboard(dashData);
        // 同步侧边栏角色为「当前选中仓库」的真实角色（随仓库切换刷新）
        const _curRole = dashData.role || 'Member';
        $('userRole').textContent = _curRole === 'Maintainer' ? I18N.t('dashboard.admin') : I18N.t('dashboard.member');
      }).catch(e => {
        if (e.name === 'AbortError' && e.silent) return;  // Issue #63: abort 静默
        if (window._currentView !== 'dash') return;  // stale guard
        // 编辑中出错只 toast 不覆盖 UI；非编辑中清 loading 显示错误
        if (window._dashboardEditing) { app.showToast && app.showToast(I18N.t('common.load_failed') + ': ' + errMsg(e)); return; }
        // R13.6: dashboard 403 = 当前仓库无权限，自动切换到下一个有权限仓库
        if (e.status === 403) {
          sessionStorage.removeItem('currentRepo');
          // P0-3: 已尝试仓库去重，避免遍历多个 403 仓库
          if (!window._tried403Repos) window._tried403Repos = [];
          window._tried403Repos.push(repo.fullName);
          // 从侧边栏仓库列表找下一个未尝试过的有权限仓库
          var _sel = $('repoSelect');
          if (_sel && _sel.options.length > 1) {
            // 跳过第一个 "__add__" 选项，选第一个未尝试过的真实仓库
            for (var i = 0; i < _sel.options.length; i++) {
              var v = _sel.options[i].value;
              if (v && v !== '__add__' && v !== repo.fullName && window._tried403Repos.indexOf(v) < 0) {
                _sel.value = v;
                switchRepo(v);
                return;
              }
            }
          }
          // 所有仓库都试过 403 → 跳到项目大厅页并提示
          window._tried403Repos = null;
          app.showToast && app.showToast(I18N.t('common.load_failed'), I18N.t('app.error.forbidden') || 'No accessible repository');
          goToRepos();
          return;
        }
        pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div>';
      });
      };
      loadDash();
      _refreshTimer = setInterval(loadDash, 30000);
    } else if (view === 'pipe') {
      title.textContent = I18N.t('app.current_project');
      if (!repo) { pc.innerHTML = _noRepoPlaceholder(); return; }
      API.get('/pipeline/' + repo.owner + '/' + repo.name).then(d => { if (window._currentView !== 'pipe') return; renderPipeline(d, param); }).catch(e => {
        if (e.name === 'AbortError' && e.silent) return;
        if (window._currentView !== 'pipe') return;
        // 403/404 = 仓库不属于当前用户 / 已解绑 / 已改名：清上下文回大厅，
        // 不再把一行裸 "No permission to access this repository" 留在内容区。
        if (e.status === 403 || e.status === 404) { _bailRepoForbidden(); return; }
        pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div>';
      });
      // Issue #600：流水线卡片轮询——重派成功/新终态到达后自动刷新，无手动刷新依赖
      if (typeof startPipePolling === 'function') startPipePolling();
    } else if (['vod','issue','coding','qa','sre'].includes(view)) {
      title.textContent = I18N.t('app.current_project');
      if (!repo) { pc.innerHTML = _noRepoPlaceholder(); return; }
      var _agentQs = param ? '?issue_number=' + encodeURIComponent(param) : '';
      API.get('/agents/' + view + '/' + repo.owner + '/' + repo.name + _agentQs).then(d => { if (window._currentView !== view) return; renderAgent(view, d); }).catch(e => {
        if (e.name === 'AbortError' && e.silent) return;
        if (window._currentView !== view) return;
        if (e.status === 403 || e.status === 404) { _bailRepoForbidden(); return; }
        pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div>';
      });
    } else if (view === 'event') {
      // 路由入口清理：非 _jumpToNodeEvent 触发的进入（如全局搜索 / 直接路由）清除残留来源标志，
      // 防止上次任务详情遗留的 _eventFromTask 导致事件页错误显示「返回需求详情」按钮。
      if (!window._eventNavFromTask) {
        window._eventFromTask = null;
      }
      // 消费瞬态标志（仅本次跳转有效，后续任何再进入都需重新标记）
      window._eventNavFromTask = false;
      title.textContent = I18N.t('app.current_project');
      API.get('/events/detail/' + param).then(d => { if (window._currentView !== 'event') return; renderEventDetail(d); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== 'event') return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div>'; });
      // 切换回事件详情时恢复活跃对话查询
      if (typeof resumeConvTask === 'function') setTimeout(resumeConvTask, 500);
    } else if (view === 'chat') {
      title.textContent = I18N.t('app.current_project');
      _ensureViewFn('renderChat', function () {
        if (window._currentView !== 'chat') return;   // 等待期间已切走 → 不覆盖新视图
        renderChat(param || null);
      });
      // 切换回 Chat 页面时恢复活跃查询（不中断）
      if (typeof resumeChatTask === 'function') setTimeout(resumeChatTask, 500);
    } else if (view === 'im-channel') {
      title.textContent = I18N.t('app.nav.im_channel');
      _ensureViewFn('renderImChannel', function () { renderImChannel(); });
    } else if (view === 'tasks') {
      title.textContent = I18N.t('app.current_project');
      // Issue #341: #tasks/<issue号> 深链——把 hash 中的编号透传给需求中心，
      // 纯数字 param 由 renderTasks 解析为待打开详情的 issue_number
      _ensureViewFn('renderTasks', function () { renderTasks(param); });
    } else if (view === 'settings') {
      title.textContent = I18N.t('app.current_project');
      _ensureViewFn('renderSettings', function () { renderSettings(param || 'gitcode'); });
    } else if (view === 'account') {
      title.textContent = I18N.t('app.account.center_title');
      // R13.2: 用户设置页不展示仓库信息——清空顶栏仓库名
      repoTitle.textContent = '';
      // 用户设置：只隐藏侧边栏，保留顶栏，mainView 保持正常布局
      var _accSb = $('sidebar'); if (_accSb) _accSb.style.display = 'none';
      _ensureViewFn('renderAccount', function () { renderAccount(param || 'profile'); });
    } else if (view === 'organizations') {
      title.textContent = param
        ? (I18N.t('organizations.workbench.title') || '组织工作台')
        : (I18N.t('app.nav.organizations') || '组织管理');
      repoTitle.textContent = '';
      // 组织管理页面隐藏左侧菜单，全屏展示。
      // 列表页不再作为入口：有 param 直接进详情；__create__ 直接开创建弹窗；
      // 无 param 时回退当前组织。
      var _orgSb = $('sidebar'); if (_orgSb) _orgSb.style.display = 'none';
      var _orgCreateOnly = param === '__create__';
      var _orgDetailId = _orgCreateOnly ? '' : (param || localStorage.getItem('current_org_id') || '');
      _ensureViewFn('renderOrganizations', function () {
        renderOrganizations(_orgDetailId, _orgCreateOnly);
      });
    } else if (view === 'my-feedback') {
      title.textContent = '我的反馈';
      _ensureViewFn('renderMyFeedback', function () { renderMyFeedback(); });
    } else if (view === 'my-feedback-detail') {
      title.textContent = '反馈详情';
      renderMyFeedbackDetail(param);
    } else if (view === 'skills') {
      title.textContent = I18N.t('app.current_project');
      renderSkills();
    } else if (view === 'plugins') {
      // Issue #3169：插件市场（Vue chunk 自管加载；无仓库上下文可浏览，repo scope 由视图内降级提示）
      title.textContent = I18N.t('app.current_project');
      _ensureViewFn('renderPlugins', function () {
        if (window._currentView !== 'plugins') return;
        renderPlugins();
      });
    } else if (view === 'knowledge') {
      title.textContent = I18N.t('app.current_project');
      if (typeof _knowledgeVueApp === 'undefined' || !_knowledgeVueApp) {
        renderKnowledge();
      }
    } else if (view === 'sandbox') {
      title.textContent = I18N.t('app.current_project');
      renderSandbox();  // B2: Vue chunk — 深链 #sandbox/provider/{id} 由组件自解析
    } else if (view === 'runtimes') {
      title.textContent = I18N.t('app.current_project');
      renderRuntimes();  // batch 11: Vue chunk 自管分页重置与数据加载
    } else if (view === 'observability') {
      title.textContent = I18N.t('app.current_project');
      // param = 分区（overview/devices/inspect），由侧栏子项写入 URL，可深链
      renderObservability(param);
    } else if (view === 'repos') {
      title.textContent = I18N.t('app.current_project');
      goToRepos();
    } else if (view === 'skill') {
      title.textContent = I18N.t('app.current_project');
      const _cr = currentRepo();
      const _qs = _cr && _cr.fullName ? '?repo=' + encodeURIComponent(_cr.fullName) : '';
      API.get('/skills/by-id/' + encodeURIComponent(param) + _qs).then(d => { if (window._currentView !== 'skill') return; renderSkillDetail(d); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== 'skill') return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div>'; });
    } else if (view === 'skill-sub') {
      // 技能包子技能详情（如 "38/huawei-ecs"）
      title.textContent = I18N.t('app.current_project');
      API.get('/skills/package-sub/' + param).then(d => { if (window._currentView !== 'skill-sub') return; renderSkillDetail(d); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== 'skill-sub') return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div>'; });
    } else if (view === 'scene-market') {
      title.textContent = I18N.t('app.current_project');
      renderSceneMarket();
    } else if (view === 'scene-detail') {
      title.textContent = I18N.t('app.current_project');
      renderSceneDetail(param);
    } else if (view === 'scene-detail-local') {
      title.textContent = I18N.t('app.current_project');
      renderSceneDetailLocal(param);
    } else if (view === 'autopilot') {
      title.textContent = I18N.t('app.current_project');
      renderAutopilot();
    } else if (view === 'autopilot-detail') {
      title.textContent = I18N.t('app.current_project');
      renderAutopilotDetail(param);
    }

    // 团队组织上下文：面包屑前段只保留「组织项目 / 关联仓库」，不再显示“当前项目”。
    // 组织管理、组织流水线、设置/账号等自身有独立标题的页面除外。
    if (repo && _isTeamOrgContext() &&
        ['organizations', 'org-pipeline', 'repos', 'account', 'settings'].indexOf(view) < 0) {
      title.textContent = '';
    }
  }

  // 浏览器回退/前进：按 history 状态回放视图（回放时不重复入栈）
  window.addEventListener('popstate', (e) => {
    const s = e.state;
    _replaying = true;
    try {
      // 跨仓跳转返回：history 里带了来源仓库，先恢复仓库上下文再回放视图。
      if (s && s.repo && s.repo.fullName) {
        const cur = currentRepo();
        if (!cur || cur.fullName !== s.repo.fullName) _applyRepoState(s.repo);
      }
      if (s && s.view === 'repos') goToRepos();          // 全屏仓库选择页
      else if (s && s.view) nav(s.view, s.param);
      else {
        // No state — try hash-based navigation
        const hash = window.location.hash;
        if (hash && hash.length > 1) {
          const parts = hash.substring(1).split('/');
          const view = parts[0];
          const noRepoViews = NO_REPO_VIEWS;
          if (noRepoViews.includes(view) || currentRepo()) {
            nav(view, parts[1] ? decodeURIComponent(parts[1]) : undefined);
          } else if (currentRepo()) {
            nav('dash');
          } else {
            goToRepos();
          }
        } else if (currentRepo()) {
          nav('dash');               // 回到入口：有仓库则默认 Dashboard
        } else {
          goToRepos();                                  // 无仓库则回到仓库选择
        }
      }
    } finally {
      _replaying = false;
    }
  });

  function oauthLogin(provider) {
    // 登录场景：全页重定向（业界标准，兼容性最强）
    // 弹窗模式仅用于已登录态的绑定场景（见 account.js _accountDoOauthBind）
    // B+（Issue #535）：配置状态已知且未配置 → 不发请求直接给出原因+文档入口；
    // 状态未知（providers 未返回/检测失败）→ 走下方 API 的 A 兜底路径
    if (_oauthConfiguredMap && _oauthConfiguredMap[provider] === false) {
      var _nm0 = platformLabelOf(provider);
      var _lt0 = I18N.t('app.login.oauth_view_config_help', null, '查看配置帮助');
      showToast(
        I18N.t('app.login.oauth_not_configured', {name: _nm0}, _nm0 + ' OAuth 未配置，' + _nm0 + ' 登录暂不可用'),
        I18N.t('app.login.oauth_not_configured_hint', {name: _nm0}, '请使用用户名+密码注册登录'),
        6000,
        {text: _lt0, url: _oauthDocUrl(provider)}
      );
      return;
    }
    API.get('/auth/login/' + provider + '?lang=' + I18N.getLang()).then(function(d) {
      if (d.redirect_url) window.location.href = d.redirect_url;
    }).catch(function(e) {
      // R7.5 + P0：区分「未配置」与其他失败（网络/限流/5xx），避免误导用户去查配置
      // - 400 且 detail 含「未配置」→ 未配置文案 + 配置帮助深链
      // - 其余（网络瞬断 / 429 限流 / 5xx）→ 通用失败提示，不走配置文案
      var _isNotConfigured = e && e.status === 400 &&
        /未配置|not configured/i.test(String(e.message || (e.detail && e.detail.message) || ''));
      if (!_isNotConfigured) {
        showToast(I18N.t('app.login.oauth_request_failed', null, errMsg(e) || '请求失败，请稍后重试'), '', 4000);
        return;
      }
      // R7.5: 未配 OAuth App 时提示用户用用户名+密码注册；附帮助文档链接（OAuth App 配置指南）
      var _nm = platformLabelOf(provider);
      var _doc = {
        gitcode: '/static/help.html#/developer/01-gitcode-integration',
        github: '/static/help.html#/developer/02-github-integration'
      }[provider] || '/static/help.html';
      var _linkText = I18N.t('app.login.oauth_view_config_help', null, '查看配置帮助');
      showToast(
        I18N.t('app.login.oauth_not_configured', {name: _nm}, _nm + ' OAuth 未配置，' + _nm + ' 登录暂不可用'),
        I18N.t('app.login.oauth_not_configured_hint', {name: _nm}, '请使用用户名+密码注册登录'),
        6000,
        {text: _linkText, url: _doc}
      );
    });
  }

  // B+（Issue #535）：provider 配置状态表——renderOAuthButtons 从 /auth/providers 填充；
  // 未加载（状态未知）时不置灰、不拦截，点击走 API 的 A 兜底路径
  var _oauthConfiguredMap = null;

  // FOUC 修复（#535）：服务端在 HTML head 注入 window.__OAUTH_CONFIGURED__（与注册页
  // __EMAIL_CONFIGURED__ 同模式）。app.js 为 defer——DOM 已就绪，此同步块在首帧渲染
  // 前执行：预渲染按钮直接带上正确的 aria-disabled + 常显说明行，不出现
  // 「先常亮 123ms、providers 返回后再置灰」的闪动。已配置场景注入值即为 true，
  // 按钮保持常亮，无中间态，行为与之前完全一致。
  // 注意：_oauthConfiguredMap 仍初始为 null——服务端未注入（如本地 file:// 打开、
  // 或未来新增入口页）时维持 A 兜底，不会误判。
  (function _applyInjectedOAuthState() {
    try {
      var inj = window.__OAUTH_CONFIGURED__;
      if (!inj || typeof inj !== 'object') return;
      var container = document.getElementById('oauthBtns');
      if (!container) return;
      var anyUnconfigured = false;
      Object.keys(inj).forEach(function(provider) {
        var key = "app.oauthLogin('" + provider + "')";
        var btns = container.querySelectorAll('.oauth-btn');
        for (var i = 0; i < btns.length; i++) {
          if ((btns[i].getAttribute('onclick') || '').indexOf(key) >= 0) {
            if (inj[provider]) btns[i].removeAttribute('aria-disabled');
            else { btns[i].setAttribute('aria-disabled', 'true'); anyUnconfigured = true; }
            break;
          }
        }
      });
      if (anyUnconfigured) {
        var providers = Object.keys(inj).map(function(k) {
          return { provider: k, name: platformLabelOf(k), configured: !!inj[k] };
        });
        _renderOAuthUnconfiguredHint(container, providers);
      }
    } catch (e) { /* 注入缺失/格式异常 → 维持预渲染常亮 + A 兜底 */ }
  })();

  function _oauthDocUrl(provider) {
    return {
      gitcode: '/static/help.html#/developer/01-gitcode-integration',
      github: '/static/help.html#/developer/02-github-integration'
    }[provider] || '/static/help.html';
  }

  // OAuth provider 图标 SVG（UX-2 修复：内置已知平台图标，未知平台用首字母降级）
  var _OAUTH_ICONS = {
    gitcode: '<svg width="16" height="16" viewBox="0 0 1024 1024" fill="currentColor"><path d="M472.746667 896.853333c-72.533333-8.106667-151.466667-39.253333-200.106667-78.08-27.306667-21.76-72.533333-71.253333-89.6-97.706666-51.626667-80.213333-72.533333-179.626667-57.173333-269.653334 15.36-86.613333 50.773333-154.453333 113.92-216.32 109.653333-107.52 285.44-141.653333 425.813333-82.346666 46.08 19.626667 90.453333 46.933333 108.8 66.986666 35.84 39.68 22.613333 104.106667-25.173333 128-20.48 9.813333-58.453333 10.24-72.96 0.853334-14.933333-9.386667-28.16-35.84-32-62.293334-1.706667-12.8-3.84-23.04-4.266667-23.04-0.853333 0-11.946667 5.973333-23.893333 13.226667-40.106667 23.466667-51.626667 26.88-113.066667 30.72-32.426667 1.706667-67.84 4.693333-78.933333 6.4-20.053333 2.56-44.373333-0.853333-83.2-13.226667l-20.48-6.4 0.853333 42.24c0.426667 40.96 0 43.093333-17.493333 79.36-22.613333 46.933333-30.72 75.52-34.56 117.333334-8.106667 100.693333 46.506667 175.36 150.613333 205.226666 67.413333 19.2 179.2 15.36 239.36-8.533333 58.026667-23.04 108.8-76.8 108.8-114.773333 0-15.786667-20.053333-35.84-41.813333-40.96-8.96-2.56-43.946667-5.546667-77.226667-6.826667-69.973333-2.986667-119.04-9.386667-132.266667-18.346667-13.653333-8.533333-20.48-33.28-14.506666-51.2 5.973333-19.2 15.786667-27.306667 43.946666-37.973333 19.2-7.253333 33.28-8.533333 88.746667-8.533333 118.613333 0 174.08 14.933333 217.6 58.453333 49.92 50.346667 55.466667 127.146667 13.226667 194.986667-62.293333 99.84-135.68 156.586667-235.52 182.613333-33.706667 8.96-122.453333 14.933333-158.72 10.666667z"/></svg>',
    github: '<svg width="16" height="16" viewBox="0 0 1024 1024" fill="currentColor"><path d="M512 42.666667A464.64 464.64 0 0 0 42.666667 502.186667 460.373333 460.373333 0 0 0 363.52 938.666667c23.466667 4.266667 32-9.813333 32-22.186667v-78.08c-130.56 27.733333-158.293333-61.44-158.293333-61.44a122.026667 122.026667 0 0 0-52.053334-67.413333c-42.666667-28.16 3.413333-27.733333 3.413334-27.733334a98.56 98.56 0 0 1 71.68 47.36 101.12 101.12 0 0 0 136.533333 37.973334 99.413333 99.413333 0 0 1 29.866667-61.44c-104.106667-11.52-213.333333-50.773333-213.333334-226.986667a177.066667 177.066667 0 0 1 47.36-124.16 161.28 161.28 0 0 1 4.693334-121.173333s39.68-12.373333 128 46.933333a455.68 455.68 0 0 1 234.666666 0c89.6-59.306667 128-46.933333 128-46.933333a161.28 161.28 0 0 1 4.693334 121.173333A177.066667 177.066667 0 0 1 810.666667 477.866667c0 176.64-110.08 215.466667-213.333334 226.986666a106.666667 106.666667 0 0 1 32 85.333334v125.866666c0 14.933333 8.533333 26.88 32 22.186667A460.8 460.8 0 0 0 981.333333 502.186667 464.64 464.64 0 0 0 512 42.666667"/></svg>'
  };

  function _oauthIcon(provider, name) {
    // 已知平台用内置 SVG，未知平台用首字母降级
    if (_OAUTH_ICONS[provider]) return _OAUTH_ICONS[provider];
    var letter = (name || provider || '?')[0].toUpperCase();
    return '<span style="display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;font-size:11px;font-weight:700;line-height:1">' + _esc(letter) + '</span>';
  }

  function renderOAuthButtons() {
    var container = document.getElementById('oauthBtns');
    var section = document.getElementById('oauthSection');
    if (!container) return;
    API.get('/auth/providers').then(function(d) {
      var providers = d.providers || [];
      // R7.5: 所有平台始终显示（不过滤 configured）
      if (providers.length === 0) {
        if (section) section.style.display = '';  // 确保可见
        return;
      }
      // B+（Issue #535）：记录配置状态表——oauthLogin 前置判断用（状态未知=null 不拦截）
      _oauthConfiguredMap = {};
      providers.forEach(function(p) {
        _oauthConfiguredMap[p.provider] = !!p.configured;
      });
      // FOUC 修复闭环：head 注入的 oauth-<provider>-not-configured 类是首帧快照，
      // providers 返回的运行时状态才是权威——不一致时按平台粒度纠正类名（例：注入时
      // gitcode 未配置、加载间隙管理员已配好 → 移除该类，按钮恢复常亮；反之亦然），
      // 避免「视觉置灰但实际可登录」/「视觉常亮但点击被拦」的错位。按平台而非
      // all-or-nothing：单平台配置时另一平台的状态变化互不干扰。
      var _de = document.documentElement;
      providers.forEach(function(p) {
        var _cls = 'oauth-' + p.provider + '-not-configured';
        if (p.configured) _de.classList.remove(_cls);
        else _de.classList.add(_cls);
      });
      // oauth-configured 类仍由整体状态决定（hint 显隐的语义锚点：全配置=隐藏）
      _de.classList.toggle('oauth-configured', providers.every(function(p) { return !!p.configured; }));
      // Vue 登录页（D109 转发后登录卡片由 src/views/LoginView.vue 接管）：目录状态与
      // FOUC 类名仍由本函数计算（oauthLogin 的前置判断、CSS 置灰都读它们），但按钮与
      // 未配置提示的 DOM 归 Vue——这里只广播状态，不再写 innerHTML、也不再命中预置按钮。
      // 保留下方旧路径 = chunk 加载失败时的降级（无 #loginVueHost 即走老逻辑）。
      if (document.getElementById('loginVueHost')) {
        try {
          window.dispatchEvent(new CustomEvent('grape:oauth-providers', { detail: { providers: providers } }));
        } catch (e) { /* 旧浏览器无 CustomEvent：Vue 侧仍可用注入快照渲染 */ }
        return;
      }
      // 预渲染按钮已在 HTML 中，只在平台列表不一致时才重新渲染
      var existingBtns = container.querySelectorAll('.oauth-btn');
      var existingNames = Array.prototype.map.call(existingBtns, function(b) {
        return b.getAttribute('onclick') || '';
      });
      var needsRender = providers.length !== existingBtns.length;
      if (!needsRender) {
        for (var i = 0; i < providers.length; i++) {
          var key = "app.oauthLogin('" + providers[i].provider + "')";
          if (existingNames[i].indexOf(key) === -1) { needsRender = true; break; }
        }
      }
      if (needsRender) {
        container.innerHTML = providers.map(function(p) {
          var icon = _oauthIcon(p.provider, p.name);
          var i18nKey = 'app.login.' + p.provider + '_login';
          // 品牌色前端覆盖——后端返回的原始色值不适配 Apple 设计风格
          // 品牌色 = platform-icons 目录值；后端给的原始色值不适配 Apple 风格仅作兜底
          var _brandColor = (typeof window.platformColor === 'function' && window.platformColor(p.provider))
            || p.brand_color || '';
          var brandVar = _brandColor ? ' style="--brand:' + _brandColor + '"' : '';
          var ariaVar = p.configured ? '' : ' aria-disabled="true"';
          return '<button class="oauth-btn" onclick="app.oauthLogin(\'' + p.provider + '\')"' + ariaVar + brandVar + '>'
            + icon + '<span data-i18n="' + i18nKey + '">' + p.name + '</span></button>';
        }).join('');
        if (typeof I18N !== 'undefined' && I18N.apply) I18N.apply();
      } else {
        // 预渲染按钮：按 configured 同步 aria-disabled（不用真 disabled，保留点击可解释）
        providers.forEach(function(p) {
          var key = "app.oauthLogin('" + p.provider + "')";
          for (var j = 0; j < existingBtns.length; j++) {
            if ((existingBtns[j].getAttribute('onclick') || '').indexOf(key) >= 0) {
              if (p.configured) existingBtns[j].removeAttribute('aria-disabled');
              else existingBtns[j].setAttribute('aria-disabled', 'true');
              break;
            }
          }
        });
      }
      _renderOAuthUnconfiguredHint(container, providers);
    }).catch(function() { /* 预渲染按钮保留，不隐藏（状态未知=A 兜底） */ });
  }

  // B+（Issue #535）：未配置平台的常显说明。FOUC 修复后说明行 DOM 预置在
  // index.html（display:none），CSS 按注入 html 类首帧显示——此函数只负责按
  // 运行时权威状态更新内容（平台名单/深链）与显隐（全部已配置时隐藏）。
  // 文本走 data-i18n + data-i18n-vars：I18N 包加载完成 / setLang 时
  // applyI18nToDOM 自动刷新（语言切换/慢网络包晚到均不残留）。
  function _renderOAuthUnconfiguredHint(container, providers) {
    var el = document.getElementById('oauthUnconfiguredHint');
    if (!el) return;
    var unconfigured = providers.filter(function(p) { return !p.configured; });
    if (unconfigured.length === 0) {
      el.style.display = 'none';
      return;
    }
    var names = unconfigured.map(function(p) {
      return (p.name || platformLabelOf(p.provider));
    }).join(' / ');
    var linkText = I18N.t('app.login.oauth_view_config_help', null, '查看配置帮助');
    var hint = I18N.t('app.login.oauth_needs_config', {name: names},
      names + ' 登录需要管理员先配置 OAuth 后才能使用');
    // 更新预置 DOM：文案 span（保 data-i18n 结构）+ 深链 href（按第一个未配置平台）
    var span = el.querySelector('span[data-i18n="app.login.oauth_needs_config"], span[data-i18n="app.login.oauth_needs_config_generic"]');
    if (span) {
      // 预置是 generic key（不点名）；拿到真实名单后切到带 {name} 的 key 并设 vars，
      // 后续语言切换时 applyI18nToDOM 会用新 key + vars 渲染精确文案
      span.setAttribute('data-i18n', 'app.login.oauth_needs_config');
      span.setAttribute('data-i18n-vars', JSON.stringify({name: names}));
      span.textContent = hint;
    }
    var a = el.querySelector('a');
    if (a) {
      a.setAttribute('href', _oauthDocUrl(unconfigured[0].provider));
      var ls = a.querySelector('span[data-i18n="app.login.oauth_view_config_help"]');
      if (ls) ls.textContent = linkText;
    }
    el.style.display = '';
  }

  // 内测申请「平台」下拉：选项 = 运行时登录渠道目录（服务端注入的 __OAUTH_PROVIDERS__，
  // 与登录页按钮同源），不再写死 gitcode/github——渠道增删（gitee/华为/将来其它）自动跟随。
  function _betaPlatformOptionsHtml() {
    // 目录缺失 = 空壳（与引导页 T4 同口径：不写死 gitcode/github 兜底），
    // 由 openBetaApply 里的 /auth/providers 对账回填。
    var list = (Array.isArray(window.__OAUTH_PROVIDERS__) && window.__OAUTH_PROVIDERS__.length)
      ? window.__OAUTH_PROVIDERS__
      : [];
    return list.filter(function(p) { return p && p.provider; }).map(function(p) {
      return '<option value="' + _esc(p.provider) + '">' + _esc(p.name || p.provider) + '</option>';
    }).join('');
  }

  // 内测申请表单
function openBetaApply() {
    var isZh = I18N.getLang() === 'zh';
    var overlay = document.createElement('div');
    overlay.id = 'betaApplyModal';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;overflow-y:auto;padding:20px';
    var z = isZh;
    overlay.innerHTML = `
      <div style="background:var(--c,#fff);border-radius:16px;padding:32px;width:480px;max-width:90vw;box-shadow:0 12px 48px rgba(0,0,0,0.15);max-height:90vh;overflow-y:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
          <h2 style="font-size:20px;font-weight:600">${I18N.t('app.login.beta_form_title')}</h2>
          <span onclick="document.getElementById('betaApplyModal').remove()" style="cursor:pointer;font-size:20px;color:var(--t3,#86868b);line-height:1">×</span>
        </div>
        <div style="font-size:13px;font-weight:600;color:var(--t2,#5b667b);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--b,#f0f1f5)">${I18N.t('app.login.beta_form_basic_info')}</div>
        <div style="display:flex;flex-direction:column;gap:12px">
          <div>
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_platform')}</label>
            <select id="betaPlatform" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
              ${_betaPlatformOptionsHtml()}
            </select>
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_username')}</label>
            <input id="betaAccountName" type="text" placeholder="${I18N.t('app.login.beta_form_username_placeholder')}" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_email')}</label>
            <input id="betaEmail" type="email" placeholder="user@example.com" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
          </div>
          <div>
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_phone')} <span style="font-size:10px;color:var(--t3,#a0a0a0)">(${I18N.t('app.login.beta_form_optional_lower')})</span></label>
            <input id="betaPhone" type="text" placeholder="${I18N.t('app.login.beta_form_optional')}" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
          </div>
        </div>
        <div style="font-size:13px;font-weight:600;color:var(--t2,#5b667b);margin:16px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--b,#f0f1f5)">${I18N.t('app.login.beta_form_identity')}</div>
        <div>
          <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:8px">${I18N.t('app.login.beta_form_identity_question')}</label>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            <div class="ba-id-card" onclick="app.baSelectIdentity('student')" data-id="student" style="display:flex;align-items:center;gap:8px;padding:10px;border:2px solid var(--b,#e3e6ee);border-radius:10px;cursor:pointer;transition:all .15s"><span style="font-size:18px">🎓</span><div><div style="font-size:12px;font-weight:600;color:var(--t,#1d1d1f)">${I18N.t('app.login.beta_form_student')}</div><div style="font-size:10px;color:var(--t3,#86868b)">${I18N.t('app.login.beta_form_student_desc')}</div></div></div>
            <div class="ba-id-card" onclick="app.baSelectIdentity('enterprise')" data-id="enterprise" style="display:flex;align-items:center;gap:8px;padding:10px;border:2px solid var(--b,#e3e6ee);border-radius:10px;cursor:pointer;transition:all .15s"><span style="font-size:18px">🏢</span><div><div style="font-size:12px;font-weight:600;color:var(--t,#1d1d1f)">${I18N.t('app.login.beta_form_enterprise')}</div><div style="font-size:10px;color:var(--t3,#86868b)">${I18N.t('app.login.beta_form_enterprise_desc')}</div></div></div>
            <div class="ba-id-card" onclick="app.baSelectIdentity('opc')" data-id="opc" style="display:flex;align-items:center;gap:8px;padding:10px;border:2px solid var(--b,#e3e6ee);border-radius:10px;cursor:pointer;transition:all .15s"><span style="font-size:18px">💼</span><div><div style="font-size:12px;font-weight:600;color:var(--t,#1d1d1f)">OPC</div><div style="font-size:10px;color:var(--t3,#86868b)">${I18N.t('app.login.beta_form_opc_desc')}</div></div></div>
            <div class="ba-id-card" onclick="app.baSelectIdentity('other')" data-id="other" style="display:flex;align-items:center;gap:8px;padding:10px;border:2px solid var(--b,#e3e6ee);border-radius:10px;cursor:pointer;transition:all .15s"><span style="font-size:18px">📋</span><div><div style="font-size:12px;font-weight:600;color:var(--t,#1d1d1f)">${I18N.t('app.login.beta_form_other')}</div><div style="font-size:10px;color:var(--t3,#86868b)">${I18N.t('app.login.beta_form_other_identity_desc')}</div></div></div>
          </div>
        </div>
        <div id="baBranchStudent" style="overflow:hidden;max-height:0;opacity:0;transition:all .3s">
          <div style="margin-top:12px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_education')}</label>
            <div id="baEdu" style="display:flex;flex-direction:column;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baEdu" value="${I18N.t('app.login.beta_form_edu_undergraduate')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_edu_undergraduate')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baEdu" value="${I18N.t('app.login.beta_form_edu_master')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_edu_master')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baEdu" value="${I18N.t('app.login.beta_form_edu_phd')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_edu_phd')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baEdu" value="${I18N.t('app.login.beta_form_other')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_other')} <input type="text" class="ba-other-text" placeholder="${I18N.t('app.login.beta_form_input_placeholder')}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_major')}</label>
            <div style="display:flex;flex-direction:column;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baMajor" value="${I18N.t('app.login.beta_form_major_cs')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_major_cs')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="radio" name="baMajor" value="${I18N.t('app.login.beta_form_major_ai')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_major_ai')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="radio" name="baMajor" value="${I18N.t('app.login.beta_form_major_data_science')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_major_data_science')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="radio" name="baMajor" value="${I18N.t('app.login.beta_form_major_ee')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_major_ee_short')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="radio" name="baMajor" value="${I18N.t('app.login.beta_form_major_other_stem')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_major_other_stem')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="radio" name="baMajor" value="${I18N.t('app.login.beta_form_major_business')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_major_business')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="radio" name="baMajor" value="${I18N.t('app.login.beta_form_other')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_other')} <input type="text" class="ba-other-text" placeholder="${I18N.t('app.login.beta_form_input_placeholder')}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
        </div>
        <div id="baBranchEnterprise" style="overflow:hidden;max-height:0;opacity:0;transition:all .3s">
          <div style="margin-top:12px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_company_size')}</label>
            <div id="baSize" style="display:flex;flex-direction:column;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baSize" value="1-10人" onchange="app.baToggleOther(this)"> 1-10人</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baSize" value="11-50人" onchange="app.baToggleOther(this)"> 11-50人</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baSize" value="51-200人" onchange="app.baToggleOther(this)"> 51-200人</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baSize" value="201-1000人" onchange="app.baToggleOther(this)"> 201-1000人</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baSize" value="1000人以上" onchange="app.baToggleOther(this)"> 1000人+</label>
            </div>
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_role')}</label>
            <div id="baRole" style="display:flex;flex-direction:column;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baRole" value="${I18N.t('app.login.beta_form_role_developer')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_role_developer')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baRole" value="${I18N.t('app.login.beta_form_role_tech_lead')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_role_tech_lead')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baRole" value="${I18N.t('app.login.beta_form_role_pm')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_role_pm')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baRole" value="${I18N.t('app.login.beta_form_role_project_manager')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_role_project_manager')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baRole" value="${I18N.t('app.login.beta_form_role_manager')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_role_manager')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baRole" value="${I18N.t('app.login.beta_form_other')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_other')} <input type="text" class="ba-other-text" placeholder="${I18N.t('app.login.beta_form_input_placeholder')}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_use_cases_multi')}</label>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baEntCase" value="${I18N.t('app.login.beta_form_case_software_dev')}"> ${I18N.t('app.login.beta_form_case_software_dev')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baEntCase" value="${I18N.t('app.login.beta_form_case_ai_agent')}"> ${I18N.t('app.login.beta_form_case_ai_agent')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baEntCase" value="${I18N.t('app.login.beta_form_case_automation_office')}"> ${I18N.t('app.login.beta_form_case_automation_office')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baEntCase" value="${I18N.t('app.login.beta_form_case_internal_tools')}"> ${I18N.t('app.login.beta_form_case_internal_tools')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baEntCase" value="${I18N.t('app.login.beta_form_case_data_analysis')}"> ${I18N.t('app.login.beta_form_case_data_analysis')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baEntCase" value="${I18N.t('app.login.beta_form_case_devops')}"> ${I18N.t('app.login.beta_form_case_devops')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baEntCase" value="${I18N.t('app.login.beta_form_other')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_other')} <input type="text" class="ba-other-text" placeholder="${I18N.t('app.login.beta_form_input_placeholder')}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
        </div>
        <div id="baBranchOpc" style="overflow:hidden;max-height:0;opacity:0;transition:all .3s">
          <div style="margin-top:12px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_occupation')}</label>
            <div id="baOcc" style="display:flex;flex-direction:column;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baOcc" value="${I18N.t('app.login.beta_form_occ_software_it')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_occ_software_it')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baOcc" value="AI/Agent" onchange="app.baToggleOther(this)"> AI/Agent</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baOcc" value="${I18N.t('app.login.beta_form_occ_ecommerce')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_occ_ecommerce')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baOcc" value="${I18N.t('app.login.beta_form_occ_content_creation')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_occ_content')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baOcc" value="SaaS" onchange="app.baToggleOther(this)"> SaaS</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baOcc" value="${I18N.t('app.login.beta_form_occ_consulting')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_occ_consulting')}</label>
              <label style="display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px"><input style="align-self:center" type="radio" name="baOcc" value="${I18N.t('app.login.beta_form_other')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_other')} <input type="text" class="ba-other-text" placeholder="${I18N.t('app.login.beta_form_input_placeholder')}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_solve_cases_multi')}</label>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOpcCase" value="${I18N.t('app.login.beta_form_case_product_dev')}"> ${I18N.t('app.login.beta_form_case_product_dev')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOpcCase" value="${I18N.t('app.login.beta_form_case_coding')}"> ${I18N.t('app.login.beta_form_case_coding')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOpcCase" value="${I18N.t('app.login.beta_form_case_agent_dev')}"> ${I18N.t('app.login.beta_form_case_agent_dev')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOpcCase" value="${I18N.t('app.login.beta_form_case_automation')}"> ${I18N.t('app.login.beta_form_case_automation')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOpcCase" value="${I18N.t('app.login.beta_form_case_content_prod')}"> ${I18N.t('app.login.beta_form_case_content_prod')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOpcCase" value="${I18N.t('app.login.beta_form_case_data')}"> ${I18N.t('app.login.beta_form_case_data')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOpcCase" value="${I18N.t('app.login.beta_form_case_operations')}"> ${I18N.t('app.login.beta_form_case_operations')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOpcCase" value="${I18N.t('app.login.beta_form_other')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_other')} <input type="text" class="ba-other-text" placeholder="${I18N.t('app.login.beta_form_input_placeholder')}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
        </div>
        <div id="baBranchOther" style="overflow:hidden;max-height:0;opacity:0;transition:all .3s">
          <div style="margin-top:12px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_desc')}</label>
            <input id="baOtherDesc" type="text" placeholder="${I18N.t('app.login.beta_form_desc_placeholder')}" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none">
          </div>
          <div style="margin-top:10px">
            <label style="display:block;font-size:12px;color:var(--t2,#5b667b);margin-bottom:4px">${I18N.t('app.login.beta_form_use_cases_multi')}</label>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOtherCase" value="${I18N.t('app.login.beta_form_case_learning')}"> ${I18N.t('app.login.beta_form_case_learning')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOtherCase" value="${I18N.t('app.login.beta_form_case_work')}"> ${I18N.t('app.login.beta_form_case_work')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOtherCase" value="${I18N.t('app.login.beta_form_case_programming')}"> ${I18N.t('app.login.beta_form_case_programming')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOtherCase" value="${I18N.t('app.login.beta_form_case_startup')}"> ${I18N.t('app.login.beta_form_case_startup')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOtherCase" value="${I18N.t('app.login.beta_form_occ_content')}"> ${I18N.t('app.login.beta_form_occ_content')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOtherCase" value="${I18N.t('app.login.beta_form_case_data_short')}"> ${I18N.t('app.login.beta_form_case_data_short')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOtherCase" value="${I18N.t('app.login.beta_form_case_automation')}"> ${I18N.t('app.login.beta_form_case_automation')}</label>
              <label style="display:flex;align-items:center;gap:4px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap"><input style="align-self:center" type="checkbox" class="baOtherCase" value="${I18N.t('app.login.beta_form_other')}" onchange="app.baToggleOther(this)"> ${I18N.t('app.login.beta_form_other')} <input type="text" class="ba-other-text" placeholder="${I18N.t('app.login.beta_form_input_placeholder')}" style="display:none;width:160px;padding:6px 10px;border:1px solid var(--b,#e3e6ee);border-radius:6px;font-size:12px;outline:none;margin-left:4px"></label>
            </div>
          </div>
        </div>
        <div style="font-size:13px;font-weight:600;color:var(--t2,#5b667b);margin:16px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--b,#f0f1f5)">${I18N.t('app.login.beta_form_reason')} <span style="font-size:10px;color:var(--t3,#a0a0a0)">(${I18N.t('app.login.beta_form_optional_lower')})</span></div>
        <textarea id="betaReason" rows="2" placeholder="${I18N.t('app.login.beta_form_optional')}" style="width:100%;padding:10px 12px;border:1px solid var(--b,#e3e6ee);border-radius:8px;font-size:13px;background:var(--c,#fff);color:var(--t1,#1d1d1f);outline:none;resize:vertical;font-family:inherit"></textarea>
        <button id="betaSubmitBtn" onclick="app.submitBetaApply()" style="width:100%;padding:12px;border:none;border-radius:10px;background:linear-gradient(135deg,#3168e0,#4f46e5);color:#fff;font-size:14px;font-weight:600;cursor:pointer;margin-top:8px">${I18N.t('app.login.beta_form_submit')}</button>
      </div>`;
    document.body.appendChild(overlay);
    // 注入缺失（非常规入口）时用接口对账一次，回填仍在打开状态的弹窗
    if (!(Array.isArray(window.__OAUTH_PROVIDERS__) && window.__OAUTH_PROVIDERS__.length)) {
      API.get('/auth/providers').then(function(d) {
        var list = (d && d.providers) || [];
        var sel = document.getElementById('betaPlatform');
        if (!sel || !list.length) return;
        var cur = sel.value;
        window.__OAUTH_PROVIDERS__ = list;
        sel.innerHTML = _betaPlatformOptionsHtml();
        if (list.some(function(p) { return p.provider === cur; })) sel.value = cur;
      }).catch(function() { /* 保留兜底两项 */ });
    }
    // Issue #205: 移除遮罩点击关闭逻辑，仅通过 × 按钮关闭；表单数据 sessionStorage 持久化
    _baRestoreForm();
    overlay.addEventListener('input', _baSaveForm);
    overlay.addEventListener('change', _baSaveForm);
  }  var _baIdentity = '';
  var _BA_FORM_KEY = 'beta_apply_form';
  function _baCollectFormData() {
    var modal = document.getElementById('betaApplyModal');
    if (!modal) return null;
    var data = {};
    ['betaPlatform','betaAccountName','betaEmail','betaPhone','betaReason'].forEach(function(id){
      var el = document.getElementById(id);
      if (el) data[id] = el.value;
    });
    data.identity = _baIdentity;
    ['baEdu','baMajor','baSize','baRole','baOcc'].forEach(function(name){
      var r = modal.querySelector('input[name="' + name + '"]:checked');
      if (r) data['r_' + name] = r.value;
    });
    ['baEdu','baMajor','baRole','baOcc'].forEach(function(name){
      var r = modal.querySelector('input[name="' + name + '"]:checked');
      if (r && (r.value === '其他' || r.value === 'Other')) {
        var t = r.parentElement.querySelector('.ba-other-text');
        if (t) data['ro_' + name] = t.value;
      }
    });
    ['baEntCase','baOpcCase','baOtherCase'].forEach(function(cls){
      var vals = [];
      modal.querySelectorAll('.' + cls + ':checked').forEach(function(cb){
        var item = { v: cb.value };
        if (cb.value === '其他' || cb.value === 'Other') {
          var t = cb.parentElement.querySelector('.ba-other-text');
          item.o = t ? t.value : '';
        }
        vals.push(item);
      });
      data['c_' + cls] = vals;
    });
    var desc = document.getElementById('baOtherDesc');
    if (desc) data.baOtherDesc = desc.value;
    return data;
  }
  function _baSaveForm() {
    var data = _baCollectFormData();
    if (data) { try { sessionStorage.setItem(_BA_FORM_KEY, JSON.stringify(data)); } catch(e) {} }
  }
  function _baRestoreForm() {
    var modal = document.getElementById('betaApplyModal');
    if (!modal) return;
    var raw;
    try { raw = sessionStorage.getItem(_BA_FORM_KEY); } catch(e) { return; }
    if (!raw) return;
    var data;
    try { data = JSON.parse(raw); } catch(e) { return; }
    ['betaPlatform','betaAccountName','betaEmail','betaPhone','betaReason'].forEach(function(id){
      var el = document.getElementById(id);
      if (el && data[id] !== undefined && data[id] !== '') el.value = data[id];
    });
    ['baEdu','baMajor','baSize','baRole','baOcc'].forEach(function(name){
      var val = data['r_' + name];
      if (val === undefined) return;
      var radios = modal.querySelectorAll('input[name="' + name + '"]');
      for (var i = 0; i < radios.length; i++) {
        if (radios[i].value === val) {
          radios[i].checked = true;
          if (radios[i].value === '其他' || radios[i].value === 'Other') {
            var t = radios[i].parentElement.querySelector('.ba-other-text');
            if (t) { t.style.display = 'inline-block'; if (data['ro_' + name] !== undefined) t.value = data['ro_' + name]; }
          }
          break;
        }
      }
    });
    ['baEntCase','baOpcCase','baOtherCase'].forEach(function(cls){
      var vals = data['c_' + cls];
      if (!Array.isArray(vals)) return;
      vals.forEach(function(item){
        var cbs = modal.querySelectorAll('.' + cls);
        for (var i = 0; i < cbs.length; i++) {
          if (cbs[i].value === item.v) {
            cbs[i].checked = true;
            if (cbs[i].value === '其他' || cbs[i].value === 'Other') {
              var t = cbs[i].parentElement.querySelector('.ba-other-text');
              if (t) { t.style.display = 'inline-block'; if (item.o) t.value = item.o; }
            }
            break;
          }
        }
      });
    });
    var desc = document.getElementById('baOtherDesc');
    if (desc && data.baOtherDesc !== undefined) desc.value = data.baOtherDesc;
    if (data.identity) { _baIdentity = data.identity; baSelectIdentity(data.identity); }
  }
  function _baClearForm() {
    try { sessionStorage.removeItem(_BA_FORM_KEY); } catch(e) {}
  }
  function baToggleOther(el) {
    if (el.type === 'radio') {
      var group = document.querySelectorAll('input[name="' + el.name + '"]');
      group.forEach(function(r) {
        var t = r.parentElement.querySelector('.ba-other-text');
        if (t) { t.style.display = 'none'; if (r.value !== '其他' && r.value !== 'Other') t.value = ''; }
      });
      if (el.checked && (el.value === '其他' || el.value === 'Other')) {
        var txt = el.parentElement.querySelector('.ba-other-text');
        if (txt) txt.style.display = 'inline-block';
      }
    } else {
      var txt = el.parentElement.querySelector('.ba-other-text');
      if (!txt) return;
      txt.style.display = el.checked ? 'inline-block' : 'none';
      if (!el.checked) txt.value = '';
    }
  }
  function baSelectIdentity(type) {
    _baIdentity = type;
    var modal = document.getElementById('betaApplyModal');
    if (!modal) return;
    modal.querySelectorAll('.ba-id-card').forEach(function(c) {
      c.style.borderColor = c.dataset.id === type ? '#3168e0' : 'var(--b,#e3e6ee)';
      c.style.background = c.dataset.id === type ? 'rgba(49,104,224,0.04)' : 'transparent';
    });
    ['Student','Enterprise','Opc','Other'].forEach(function(s) {
      var el = document.getElementById('baBranch' + s);
      if (el) { el.style.maxHeight = '0'; el.style.opacity = '0'; }
    });
    var key = type.charAt(0).toUpperCase() + type.slice(1);
    var branch = document.getElementById('baBranch' + key);
    if (branch) { branch.style.maxHeight = '1000px'; branch.style.opacity = '1'; }
    _baSaveForm();
  }
  function _baRadio(name) {
    var c = document.querySelector('input[name="' + name + '"]:checked');
    return c ? c.value : '';
  }
  function _baChecks(cls) {
    var vals = [];
    document.querySelectorAll('.' + cls + ':checked').forEach(function(cb) {
      if (cb.value === '其他' || cb.value === 'Other') {
        var t = cb.parentElement.querySelector('.ba-other-text');
        vals.push(t && t.value.trim() ? t.value.trim() : cb.value);
      } else { vals.push(cb.value); }
    });
    return vals;
  }
  function submitBetaApply() {
    var isZh = I18N.getLang() === 'zh';
    var accountType = document.getElementById('betaPlatform').value;
    var accountName = document.getElementById('betaAccountName').value.trim();
    var email = document.getElementById('betaEmail').value.trim();
    var phone = (document.getElementById('betaPhone')||{}).value ? document.getElementById('betaPhone').value.trim() : '';
    var reason = document.getElementById('betaReason').value.trim();
    if (!accountName) { showToast(I18N.t('app.login.beta_username_required')); return; }
    if (!email) { showToast(I18N.t('app.login.beta_email_required')); return; }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { showToast(I18N.t('app.login.beta_email_invalid')); return; }
    if (!_baIdentity) { showToast(I18N.t('app.login.beta_select_identity')); return; }
    var survey = null;
    if (_baIdentity === 'student') {
      var edu = _baRadio('baEdu'), major = _baRadio('baMajor');
      if (edu === '其他' || edu === 'Other') { var _t = document.querySelector('input[name="baEdu"]:checked'); if (_t) { var _tt = _t.parentElement.querySelector('.ba-other-text'); edu = _tt && _tt.value.trim() ? _tt.value.trim() : ''; } if (!edu) { showToast(I18N.t('app.login.beta_enter_education')); return; } }
      if (!edu || !major) { showToast(I18N.t('app.login.beta_complete_education')); return; }
      if (major === '其他' || major === 'Other') { var _t2 = document.querySelector('input[name="baMajor"]:checked'); if (_t2) { var _tt2 = _t2.parentElement.querySelector('.ba-other-text'); major = _tt2 && _tt2.value.trim() ? _tt2.value.trim() : ''; } if (!major) { showToast(I18N.t('app.login.beta_enter_major')); return; } }
      survey = { identity: 'student', education: edu, major: major };
    } else if (_baIdentity === 'enterprise') {
      var size = _baRadio('baSize'), role = _baRadio('baRole'), cases = _baChecks('baEntCase');
      if (role === '其他' || role === 'Other') { var _t = document.querySelector('input[name="baRole"]:checked'); if (_t) { var _tt = _t.parentElement.querySelector('.ba-other-text'); role = _tt && _tt.value.trim() ? _tt.value.trim() : ''; } if (!role) { showToast(I18N.t('app.login.beta_enter_role')); return; } }
      if (!size || !role || cases.length === 0) { showToast(I18N.t('app.login.beta_complete_company')); return; }
      survey = { identity: 'enterprise', companySize: size, role: role, useCases: cases };
    } else if (_baIdentity === 'opc') {
      var occ = _baRadio('baOcc'), cases2 = _baChecks('baOpcCase');
      if (occ === '其他' || occ === 'Other') { var _t = document.querySelector('input[name="baOcc"]:checked'); if (_t) { var _tt = _t.parentElement.querySelector('.ba-other-text'); occ = _tt && _tt.value.trim() ? _tt.value.trim() : ''; } if (!occ) { showToast(I18N.t('app.login.beta_enter_occupation')); return; } }
      if (!occ || cases2.length === 0) { showToast(I18N.t('app.login.beta_complete_opc')); return; }
      survey = { identity: 'opc', occupation: occ, useCases: cases2 };
    } else if (_baIdentity === 'other') {
      var desc = (document.getElementById('baOtherDesc')||{}).value ? document.getElementById('baOtherDesc').value.trim() : '';
      var cases3 = _baChecks('baOtherCase');
      if (!desc || cases3.length === 0) { showToast(I18N.t('app.login.beta_complete_desc')); return; }
      survey = { identity: 'other', description: desc, useCases: cases3 };
    }
    var btn = document.getElementById('betaSubmitBtn');
    btn.disabled = true; btn.textContent = I18N.t('app.login.beta_submitting');
    API.post('/settings/beta-apply/submit', {
      accountType: accountType, accountName: accountName, email: email,
      identityType: _baIdentity, phone: phone, reason: reason, surveyData: survey
    }).then(function(d) {
      if (d.success) {
        showToast(d.message || I18N.t('app.login.beta_submitted'));
        _baClearForm();
        document.getElementById('betaApplyModal').remove();
      } else if (d.error) {
        showToast(d.error);
        btn.disabled = false; btn.textContent = I18N.t('app.login.beta_submit');
      } else {
        showToast(d.message || I18N.t('app.login.beta_submitted'));
        _baClearForm();
        document.getElementById('betaApplyModal').remove();
      }
    }).catch(function(e) {
      showToast(I18N.t('app.login.beta_submit_failed_prefix') + (errMsg(e) || e));
      btn.disabled = false; btn.textContent = I18N.t('app.login.beta_submit');
    });
  }

  // ── 登录/注册/忘记密码：独立页面跳转（不再用 display:none 切换）──
  // P1-9: 删除 togglePwdLogin/toggleRegisterForm/toggleForgotPassword 死代码
  // 这些函数已被独立页面（/register, /forgot）的 <a href> 替代，从未被调用

  // P1-9: 删除 _startCooldown 死代码 — 认证页面倒计时已由 Auth.startCountdown 统一处理

  // 以下函数仅在主应用 app.js 内保留兼容引用（独立页面有自己的实现）
  // 注册/忘记密码的交互逻辑已移到 register.html / forgot.html 内联 JS

  // 按钮加载态
  function _setLoginBtnLoading(btn, loading) {
    if (!btn) return;
    if (loading) {
      btn.disabled = true;
      btn.dataset.originalHtml = btn.innerHTML;
      btn.innerHTML = '<div class="spinner"></div>';
    } else {
      btn.disabled = false;
      if (btn.dataset.originalHtml) btn.innerHTML = btn.dataset.originalHtml;
    }
  }

  // 密码可见性切换（登录页面）
  var _loginEyeOpen = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
  var _loginEyeClosed = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
  function _toggleLoginPwd(btn) {
    var el = $('loginPass');
    if (el.type === 'password') {
      el.type = 'text';
      btn.innerHTML = _loginEyeClosed;
      btn.setAttribute('aria-label', I18N.t('app.login.hide_password'));
    } else {
      el.type = 'password';
      btn.innerHTML = _loginEyeOpen;
      btn.setAttribute('aria-label', I18N.t('app.login.show_password'));
    }
  }

  // P-UX: oninput 清除错误 — 用户修改时给正反馈
  function _clearLoginError(fieldId) {
    var el = $(fieldId);
    if (!el) return;
    if (el.getAttribute('aria-invalid') === 'true') {
      var errEl = $(fieldId + 'Err');
      if (errEl) { errEl.textContent = ''; errEl.style.display = 'none'; }
      el.removeAttribute('aria-invalid');
    }
  }

  // P-UX: blur 验证 — 仅验证非空字段（空字段不报错，等 submit 统一验证）
  function _blurValidateLogin(fieldId) {
    var el = $(fieldId);
    if (!el) return true;
    var val = el.value.trim();
    if (!val) return true; // 空字段不报错
    _clearLoginError(fieldId);
    return true;
  }

  // P-UX: submit 验证 — 所有必填字段统一验证（空字段也报错）
  function _submitValidateLogin(fieldId) {
    var el = $(fieldId);
    var errEl = $(fieldId + 'Err');
    if (!el || !errEl) return true;
    var val = el.value.trim();
    var isZh = I18N.getLang() === 'zh';
    errEl.textContent = '';
    errEl.style.display = 'none';
    el.removeAttribute('aria-invalid');
    if (!val) {
      var label = fieldId === 'loginUser'
        ? I18N.t('app.login.login_user_required')
        : I18N.t('app.login.login_pass_required');
      errEl.textContent = label;
      errEl.style.display = 'block';
      el.setAttribute('aria-invalid', 'true');
      return false;
    }
    return true;
  }

  function pwdLogin() {
    // P-UX: submit 时统一验证（空字段也报错）
    var hasErr = false;
    if (!_submitValidateLogin('loginUser')) hasErr = true;
    if (!_submitValidateLogin('loginPass')) hasErr = true;
    if (hasErr) return;
    var u = $('loginUser').value.trim();
    var p = $('loginPass').value.trim();
    // 清除旧 token——避免 ensureTokenValid 尝试 refresh 旧 token 导致 20 秒等待
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    localStorage.removeItem('current_org_id');
    // 清除旧仓库选择——切换账号登录时 sessionStorage 残留的 currentRepo 会导致新账号路由恢复到 dash 但无权限
    sessionStorage.removeItem('currentRepo');
    // 按钮加载态
    var btn = $('loginBtn');
    _setLoginBtnLoading(btn, true);
    API.post('/auth/login', {identifier: u, password: p, lang: I18N.getLang()}).then(d => {
      localStorage.setItem('token', d.token);
      localStorage.setItem('user', JSON.stringify(d.user));
      _setLoginBtnLoading(btn, false);
      API.scheduleProactiveRefresh();
      showApp();
    }).catch(e => {
      _setLoginBtnLoading(btn, false);
      var msg = e.message || 'Failed';
      var code = e.error_code || '';
      // 限流反馈 — use error_code or message keyword matching (backward compat)
      if (code === 'ACCOUNT_LOCKED' || msg.includes('锁定') || msg.includes('lock')) {
        showToast(_loginErrMsg(e));
      } else if (code === 'TOO_MANY_ATTEMPTS' || msg.includes('IP') || msg.includes('频繁')) {
        showToast(_loginErrMsg(e));
      } else if (msg.includes('验证码') || msg.includes('captcha') || msg.includes('人机')) {
        showToast(msg);
      } else if (msg.includes('白名单')) {
        _showWhitelistGuidance('gitcode', u);
      } else {
        showToast(_loginErrMsg(e));   // R3: no "登录失败:" prefix; R4: error_code i18n
      }
    });
  }

  // ===== 账号安全设置页函数 =====
  function saveProfile() {
    var nick = $('accNickname') ? $('accNickname').value.trim() : '';
    API.put('/auth/profile', {nickname: nick}).then(function() {
      showToast(I18N.t('app.login.save_success'));
    }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
  }

  function showChangePassword() {
    var isZh = I18N.getLang()==='zh';
    var html = '<div style="display:flex;flex-direction:column;gap:8px">'
      + '<input class="obtn" id="cpCurr" type="password" placeholder="' + I18N.t('app.login.current_password') + '">'
      + '<input class="obtn" id="cpNew" type="password" placeholder="' + I18N.t('app.login.new_password') + '">'
      + '<input class="obtn" id="cpConfirm" type="password" placeholder="' + I18N.t('app.login.confirm_new_password') + '">'
      + '<button class="bt bt-p" onclick="app.doChangePassword()">' + I18N.t('app.login.confirm_change') + '</button>'
      + '</div>';
    showConfirm(html, null, {hideActions: true});
  }

  function doChangePassword() {
    var curr = $('cpCurr').value.trim();
    var np = $('cpNew').value.trim();
    var cp = $('cpConfirm').value.trim();
    if (!curr || !np || !cp) return showToast(I18N.t('app.login.fill_all_fields'));
    if (np !== cp) return showToast(I18N.t('app.login.password_mismatch'));
    // 弱密码黑名单前端预检（复核 #3，2026-10-28）：改密与注册同源消费——
    // 消除"前端全绿、后端拒绝"的不一致（后端 /auth/change-password 有同名单校验）
    if (window.isWeakPassword(np)) {
      return showToast(I18N.t('app.login.err_password_blocklist'));
    }
    API.post('/auth/change-password', {current_password: curr, new_password: np}).then(function(d) {
      if (d.token) { localStorage.setItem('token', d.token); API.setToken(d.token); }
      closeConfirm();
      showToast(I18N.t('app.login.password_changed'));
    }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
  }

  function showSetPassword() {
    var isZh = I18N.getLang()==='zh';
    var html = '<div style="display:flex;flex-direction:column;gap:8px">'
      + '<p style="font-size:12px;color:var(--t2)">' + I18N.t('app.login.code_sent_to_email') + '</p>'
      + '<button class="bt bt-p" id="spSendBtn" onclick="app.sendSetPasswordCode()">' + I18N.t('app.login.send_code') + '</button>'
      + '<div id="spStep2" style="display:none;flex-direction:column;gap:8px">'
      + '<input class="obtn" id="spCode" type="text" placeholder="' + I18N.t('app.login.verification_code_short') + '">'
      + '<input class="obtn" id="spPwd" type="password" placeholder="' + I18N.t('app.login.new_password') + '">'
      + '<button class="bt bt-p" onclick="app.doSetPassword()">' + I18N.t('app.login.confirm_set') + '</button>'
      + '</div>'
      + '</div>';
    showConfirm(html, null, {hideActions: true});
  }

  function sendSetPasswordCode() {
    API.post('/auth/set-password/send-code').then(function() {
      showToast(I18N.t('app.login.code_sent'));
      var step2 = document.getElementById('spStep2');
      if (step2) step2.style.display = 'flex';
      var sendBtn = document.getElementById('spSendBtn');
      if (sendBtn) sendBtn.disabled = true;
    }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
  }

  function doSetPassword() {
    var code = $('spCode').value.trim();
    var pwd = $('spPwd').value.trim();
    if (!code || !pwd) return showToast(I18N.t('app.login.fill_all_fields'));
    // 弱密码黑名单前端预检（复核 #3）：设密与注册同源消费（谓词见 pwd-blocklist.js）
    if (window.isWeakPassword(pwd)) {
      return showToast(I18N.t('app.login.err_password_blocklist'));
    }
    API.post('/auth/set-password', {code: code, new_password: pwd}).then(function() {
      closeConfirm();
      showToast(I18N.t('app.login.password_set'));
      renderSettings('account');
    }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
  }

  // Issue #3469 P：改绑链的 400 一律带 error_code（EMAIL_FORMAT / EMAIL_SAME /
  // EMAIL_TAKEN / CODE_INVALID / CODE_EXPIRED / NEED_SECOND_FACTOR / PASSWORD_INVALID）。
  // 原来弹层只 showToast(e.message)，四种失败原因被压成一句，用户分不清「邮箱被占」
  // 和「验证码过期」。命中已知码走 I18N 文案，未知码回落 errMsg（保留后端 message）。
  var CE_ERR_KEY = {
    EMAIL_FORMAT: 'err_email_format',
    EMAIL_SAME: 'err_email_same',
    EMAIL_TAKEN: 'err_email_taken',
    CODE_INVALID: 'err_code_invalid',
    CODE_EXPIRED: 'err_code_expired',
    NEED_SECOND_FACTOR: 'err_need_second_factor',
    PASSWORD_INVALID: 'err_password_invalid'
  };

  function changeEmailErrMsg(e) {
    var code = (e && e.error_code) || '';
    var key = CE_ERR_KEY[code];
    if (key) {
      var full = 'app.login.' + key;
      var localized = I18N.t(full);
      if (localized && localized !== full) return localized;
    }
    return errMsg(e) || I18N.t('app.account.error_failed');
  }

  // Issue #3469 G：改绑前必须二次认证，因子由 send-code 返回的 require_factor 决定：
  //   password      → 当前密码框
  //   old_email_code → 原邮箱验证码框（后端已把确认码发到原邮箱，scene=change_email_confirm）
  //   none          → 不渲染任何因子框（无密码且原邮箱不可投递的账号靠这条自救，
  //                   前端不得因为「没因子可填」把改绑堵死）
  // 幂等：同一因子重复调用不重复插入，按容器 data-factor 判断。
  function _ceRenderFactor(factor) {
    var host = document.getElementById('ceFactor');
    if (!host) return;
    factor = factor || 'none';
    if (host.getAttribute('data-factor') === factor) return;
    host.setAttribute('data-factor', factor);
    if (factor === 'password') {
      host.innerHTML = '<input class="obtn" id="ceCurPwd" type="password" autocomplete="current-password" placeholder="'
        + I18N.t('app.login.current_password') + '">';
    } else if (factor === 'old_email_code') {
      host.innerHTML = '<input class="obtn" id="ceOldCode" type="text" maxlength="6"'
        + ' placeholder="' + I18N.t('app.login.old_email_code') + '">';
    } else {
      host.innerHTML = '';
    }
  }

  function showChangeEmail() {
    var isZh = I18N.getLang()==='zh';
    var html = '<div style="display:flex;flex-direction:column;gap:8px">'
      + '<div id="ceStep1" style="display:flex;flex-direction:column;gap:8px">'
      + '<input class="obtn" id="ceEmail" type="text" placeholder="' + I18N.t('app.login.new_email') + '">'
      + '<button class="bt bt-p" id="ceSendBtn" onclick="app.sendChangeEmailCode()">' + I18N.t('app.login.send_code') + '</button>'
      + '</div>'
      + '<div id="ceStep2" style="display:none;flex-direction:column;gap:8px">'
      + '<p style="font-size:12px;color:var(--t2)" id="ceHint">' + I18N.t('app.login.code_sent_new_email') + '</p>'
      + '<input class="obtn" id="ceCode" type="text" placeholder="' + I18N.t('app.login.verification_code_short') + '" maxlength="6">'
      // Issue #3469 G：二次认证因子输入框的挂载点（send-code 返回 require_factor 后再渲染）
      + '<div id="ceFactor" style="display:flex;flex-direction:column;gap:8px"></div>'
      + '<button class="bt bt-p" onclick="app.doChangeEmail()">' + I18N.t('app.login.confirm_change_email') + '</button>'
      + '</div>'
      + '</div>';
    showConfirm(html, null, {hideActions: true});
  }

  function sendChangeEmailCode() {
    var email = $('ceEmail').value.trim();
    if (!email) return showToast(I18N.t('app.login.enter_email'));
    API.post('/auth/change-email/send-code', {new_email: email}).then(function(d) {
      showToast(I18N.t('app.login.code_sent'));
      var step1 = document.getElementById('ceStep1');
      var step2 = document.getElementById('ceStep2');
      if (step1) step1.style.display = 'none';
      if (step2) step2.style.display = 'flex';
      // Issue #3469 G：按后端 require_factor 渲染二次认证输入（none 时什么都不渲染）
      _ceRenderFactor(d && d.require_factor);
    }).catch(function(e) { showToast(changeEmailErrMsg(e)); });
  }

  function doChangeEmail() {
    var email = $('ceEmail').value.trim();
    var code = $('ceCode').value.trim();
    if (!email || !code) return showToast(I18N.t('app.login.fill_all_fields'));
    var body = {new_email: email, code: code};
    // Issue #3469 G：只有真正渲染出的因子框才提交对应字段（none 时 body 里两者都不出现）
    var pwdEl = document.getElementById('ceCurPwd');
    if (pwdEl) {
      var curPwd = pwdEl.value.trim();
      if (!curPwd) return showToast(I18N.t('app.login.fill_all_fields'));
      body.password = curPwd;
    }
    var oldCodeEl = document.getElementById('ceOldCode');
    if (oldCodeEl) {
      var oldCode = oldCodeEl.value.trim();
      if (!oldCode) return showToast(I18N.t('app.login.fill_all_fields'));
      body.old_email_code = oldCode;
    }
    API.post('/auth/change-email', body).then(function() {
      closeConfirm();
      showToast(I18N.t('app.login.email_changed'));
      renderSettings('account');
    }).catch(function(e) {
      // 后端回 NEED_SECOND_FACTOR 时带 require_factor：补渲染缺的因子框，让用户就地补，
      // 不用重新走一遍 send-code（也避免「因子档位与前端渲染不一致」时用户卡死）。
      if (e && e.error_code === 'NEED_SECOND_FACTOR') {
        _ceRenderFactor((e.detail && e.detail.require_factor) || e.require_factor);
      }
      showToast(changeEmailErrMsg(e));
    });
  }

  // ══ 绑定弹层：已组件化（app/src/components/BindDialog.vue）══
  // 引导页 / 账号页的绑定弹层都由 Vue 组件实现（allowPat 控制双模式或纯 OAuth），
  // shell 侧只保留一个兼容入口：点击引导页卡片 → 组件内的 openBind。
  function _openBindModal(provider, allowPat) {
    if (typeof window.__grapeOnboardingOpenBind === 'function') {
      window.__grapeOnboardingOpenBind(provider);
      return;
    }
    if (typeof window.openBindDialog === 'function') {
      window.openBindDialog({ provider: provider, allowPat: allowPat !== false });
    }
  }

  function bindProvider(provider) {
    // 引导高亮：用户点击绑定卡片后停止光圈/箭头/气泡（交互已达成）
    try {
      document.querySelectorAll('.ob-bind-card').forEach(function(c) { c.classList.add('bound'); });
      var _h = document.querySelector('.ob-hero');
      if (_h) _h.classList.add('ob-bound-done');
    } catch (e) {}
    // 引导页绑定 = Vue BindDialog（allowPat=true：OAuth 授权 + PAT 表单；未配 OAuth → PAT 直达）
    _openBindModal(provider, true);
  }


  function _shellEmailDeliverable() {
    // Issue #3469 验收 11 / N4：判据以后端为准——/auth/profile 与 /auth/me 都返回
    // email_deliverable（同一 ProviderEmailPolicy 来源）；下面的域名表只在老后端
    // 没有该字段时兜底，避免两处判据长期漂移。
    var p = (window.__grapeProfile || (window.G && G.user) || null);
    if (!p) {
      try { p = JSON.parse(localStorage.getItem('user') || 'null'); } catch (e) { p = null; }
    }
    if (p && typeof p.email_deliverable === 'boolean') return p.email_deliverable;
    var e = (p && p.email) || '';
    if (!e) return false;
    // 形态 + 私有中转（与 identity_email.PRIVATE_EMAIL_DOMAINS 对齐）
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(e).toLowerCase())) return false;
    var d = String(e).toLowerCase().split('@')[1] || '';
    var priv = ['noreply.gitcode.com','no-reply.gitcode.com','noreply.github.com','noreply.gitee.com','noreply.gitlab.com','noreply.gitee.io','privaterelay.appleid.com'];
    for (var i = 0; i < priv.length; i++) {
      if (d === priv[i] || d.slice(-(priv[i].length + 1)) === '.' + priv[i] || d.slice(-priv[i].length) === priv[i]) return false;
    }
    return true;
  }

  function deleteAccount() {
    var isZh = I18N.getLang()==='zh';
    var html = '<div style="display:flex;flex-direction:column;gap:8px">'
      + '<p style="color:var(--r);font-size:12px">' + I18N.t('app.login.delete_account_warning') + '</p>'
      + '<div id="daStep1" style="display:flex;flex-direction:column;gap:8px">'
      + '<p style="font-size:12px;color:var(--t2)">' + I18N.t('app.login.choose_verify_method') + '</p>'
      + '<button class="bt bt-p" onclick="app._daChoosePwd()">' + I18N.t('app.login.verify_with_password') + '</button>'
      // Issue #3469 验收 11：不可投递邮箱（noreply.*/私有中转）不提供"邮箱验证码"方式
      + ((_shellEmailDeliverable())
          ? '<button class="bt bt-s" onclick="app._daChooseCode()">' + I18N.t('app.login.verify_with_code') + '</button>'
          : '')
      + '</div>'
      + '<div id="daStepPwd" style="display:none;flex-direction:column;gap:8px">'
      + '<input class="obtn" id="daPwd" type="password" placeholder="' + I18N.t('app.login.enter_password') + '">'
      + '<button class="bt bt-p" style="background:var(--r)" onclick="app.doDeleteAccount()">' + I18N.t('app.login.confirm_delete_account') + '</button>'
      + '</div>'
      + '<div id="daStepCode" style="display:none;flex-direction:column;gap:8px">'
      + '<div style="display:flex;gap:8px"><button class="bt bt-s" onclick="app.sendDeleteAccountCode()">' + I18N.t('app.login.send_code') + '</button></div>'
      + '<input class="obtn" id="daCode" type="text" placeholder="' + I18N.t('app.login.verification_code_short') + '" maxlength="6">'
      + '<button class="bt bt-p" style="background:var(--r)" onclick="app.doDeleteAccount()">' + I18N.t('app.login.confirm_delete_account') + '</button>'
      + '</div>'
      + '</div>';
    showConfirm(html, null, {hideActions: true, danger: true});
  }

  function _daChoosePwd() {
    document.getElementById('daStep1').style.display = 'none';
    document.getElementById('daStepPwd').style.display = 'flex';
  }

  function _daChooseCode() {
    document.getElementById('daStep1').style.display = 'none';
    document.getElementById('daStepCode').style.display = 'flex';
  }

  function sendDeleteAccountCode() {
    API.post('/auth/delete-account/send-code').then(function() {
      showToast(I18N.t('app.login.code_sent'));
    }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
  }

  function doDeleteAccount() {
    var pwd = $('daPwd').value.trim();
    var code = $('daCode').value.trim();
    if (!pwd && !code) return showToast(I18N.t('app.login.enter_password_or_code'));
    API.post('/auth/delete-account', {password: pwd, code: code}).then(function() {
      localStorage.removeItem('token');
      localStorage.removeItem('user');
      closeConfirm();
      showLogin();
      showToast(I18N.t('app.login.account_deleted'));
    }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
  }

  // P0-1（Issue #3054）：?oauth_error= 反射型 DOM XSS 防护。
  // 静态兜底集合 = 渠道配置里的已知 key；运行时再叠加服务端注入的渠道目录
  // （window.__OAUTH_PROVIDERS__）。URL 里带的 provider 只允许取这些值，其余一律丢弃。
  var _STATIC_OAUTH_PROVIDERS = ['gitcode', 'github', 'gitee', 'gitlab', 'huawei', 'codearts'];
  // 渠道 key 形态白名单：先钉死字符集，任何 HTML/JS 元字符都不可能进 DOM。
  var _OAUTH_PROVIDER_KEY_RE = /^[a-z][a-z0-9_-]{0,31}$/;
  function _isKnownOAuthProvider(p) {
    if (!p || !_OAUTH_PROVIDER_KEY_RE.test(p)) return false;
    var list = (typeof window !== 'undefined' && Array.isArray(window.__OAUTH_PROVIDERS__))
      ? window.__OAUTH_PROVIDERS__ : [];
    for (var i = 0; i < list.length; i++) {
      var k = list[i] && (list[i].provider || list[i]);
      if (k === p) return true;
    }
    return _STATIC_OAUTH_PROVIDERS.indexOf(p) >= 0;
  }

  // P0-3（Issue #3054）：URL 参数解码兜底——decodeURIComponent('100%') 会抛 URIError，
  // 异常冒到顶层会中断 init（弹窗版则永久停在 Processing）。失败返回 null，调用方按
  // “参数无效”处理。
  function _safeDecodeParam(v) {
    if (v === null || v === undefined) return null;
    try {
      return decodeURIComponent(v);
    } catch (e) {
      return null;
    }
  }

  // P0-1/P0-3（Issue #3054）：?oauth_error= 统一处理入口（init 与测试共用）。
  //   - 解码失败（?oauth_error=100%25）按“参数无效”提示，不中断初始化；
  //   - WHITELIST:<provider>[:<username>] 走引导弹窗，provider 必须过已知 key 白名单；
  //   - 其它值按错误码走 I18N，不直接回显后端/第三方原文。
  function _handleOAuthErrorParam(params) {
    var oauthError = params && typeof params.get === 'function' ? params.get('oauth_error') : null;
    if (!oauthError) return;
    var decoded = _safeDecodeParam(oauthError);
    if (decoded === null) {
      showToast(I18N.t('app.login.oauth_error_invalid_param', null, '授权回调参数无效'), 6000);
      return;
    }
    if (decoded.indexOf('WHITELIST:') === 0) {
      var parts = decoded.split(':');
      _showWhitelistGuidance(parts[1] || 'gitcode', parts[2] || '');
      return;
    }
    var errorKey = 'app.login.oauth_error_' + decoded;
    var translated = I18N.t(errorKey);
    showToast(translated && translated !== errorKey ? translated : decoded, 6000);
  }

  function _showWhitelistGuidance(platform, prefillTicket) {
    // P0-1：platform 未转义时会被拼进 innerHTML 与 onclick 的 JS 字符串，未知 key 直接丢弃。
    if (!_isKnownOAuthProvider(platform)) return;
    var overlay = document.createElement('div');
    overlay.id = 'whitelistModal';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,0.35);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center';
    var platName = (typeof window.platformLabel === 'function' && window.platformLabel(platform)) || platform;
    // onclick 里的字符串：encodeURIComponent 不转义单引号，补 %27 防止跳出 JS 串
    var _platQ = encodeURIComponent(platform).replace(/'/g, '%27');
    // #3054：第三方用户名不进 URL（XSS 面）；改带服务端一次性票据，申请页
    // 凭票调 /auth/beta-apply/prefill 换 platform/username（自动填充恢复）。
    // token_urlsafe 字符集 [A-Za-z0-9_-] 无 HTML/JS 元字符；仍防御性补 %27。
    var _ticketQ = prefillTicket ? ('&prefill_ticket=' + encodeURIComponent(prefillTicket).replace(/'/g, '%27')) : '';
    overlay.innerHTML =
      '<div style="background:var(--c,#fff);border-radius:20px;padding:40px;text-align:center;width:400px;max-width:90vw;box-shadow:0 16px 64px rgba(0,0,0,0.25)">'
        + '<div style="font-size:48px;margin-bottom:16px">🔒</div>'
        + '<h2 style="font-size:18px;font-weight:700;color:var(--t,#1d1d1f);margin-bottom:8px">' + I18N.t('app.login.whitelist_title') + '</h2>'
        + '<p style="font-size:14px;color:var(--t2,#86868b);margin-bottom:6px">' + I18N.t('app.login.whitelist_desc', {platform: _esc(platName), username: ' '}) + '</p>'
        + '<p style="font-size:14px;color:var(--t2,#86868b);margin-bottom:24px">' + I18N.t('app.login.whitelist_apply_hint') + '</p>'
        + '<div style="display:flex;gap:8px;justify-content:center">'
          + '<button onclick="window.location.href=\'/static/beta-apply.html?platform=' + _platQ + _ticketQ + '\'" style="padding:12px 32px;border:none;border-radius:12px;background:linear-gradient(135deg,#3168e0,#4f46e5);color:#fff;font-size:14px;font-weight:600;cursor:pointer">' + I18N.t('app.login.whitelist_apply_btn') + '</button>'
          + '<button onclick="document.getElementById(\'whitelistModal\').remove()" style="padding:12px 24px;border:1px solid var(--b,#e3e6ee);border-radius:12px;background:transparent;color:var(--t,#1d1d1f);font-size:14px;font-weight:500;cursor:pointer">' + I18N.t('common.close') + '</button>'
        + '</div>'
      + '</div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
  }

  function logout() {
    // 用原生 fetch 调 logout API——不经过 ensureTokenValid（token 已清，会触发 refresh → 导航到 /login）
    const _token = localStorage.getItem('token');
    if (_token) {
      fetch('/api/auth/logout', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + _token},
        credentials: 'same-origin',
      }).catch(() => {});
    }
    // 清除所有用户相关缓存——防止切换账号后看到上一个用户的仓库/事件
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    localStorage.removeItem('currentRepo');
    localStorage.removeItem('current_org_id');
    sessionStorage.removeItem('currentRepo');
    localStorage.removeItem(REPO_CACHE_KEY);
    // 清除 URL hash——logout 不 reload 页面，残留 hash 会导致重新登录时路由恢复到错误页面
    if (window.location.hash && window.location.hash.length > 1) {
      if (window.history.replaceState) {
        window.history.replaceState(null, '', window.location.pathname);
      }
    }
    API.scheduleProactiveRefresh();  // Issue #86: token 已清——clearTimeout + return
    // refresh_token cookie 由服务端 logout 清除
    showLogin();
  }

  // R4: Map error_code to localized login error message.
  // Falls back to errMsg(e) if error_code is absent or unrecognized.
  function _loginErrMsg(e) {
    var code = (e && e.error_code) || '';
    // P41: ACCOUNT_LOCKED uses different key based on email_configured
    var key;
    if (code === 'ACCOUNT_LOCKED') {
      var emailOk = (e && e.detail && e.detail.email_configured) || (e && e.email_configured);
      key = emailOk ? 'app.login.account_locked' : 'app.login.account_locked_no_email';
    } else {
      key = {
        'CREDENTIAL_INCORRECT': 'app.login.credential_incorrect',
        'ACCOUNT_DEACTIVATED': 'app.login.account_deactivated',
        'ACCOUNT_DEACTIVATING': 'app.login.account_deactivating',
        'ACCOUNT_DELETED': 'app.login.account_deleted',
        'TOO_MANY_ATTEMPTS': 'app.login.too_many_attempts',
      }[code];
    }
    if (key) {
      var msg = I18N.t(key);
      if (msg.indexOf('{{minutes}}') >= 0) {
        var minutes = _extractMinutes(e.message || '');
        msg = msg.replace('{{minutes}}', minutes);
      }
      return msg;
    }
    return errMsg(e);  // fallback: use generic error_code mapping or raw message
  }

  // R4: Extract minutes number from backend message string.
  function _extractMinutes(msg) {
    var m = msg.match(/(\d+)\s*分钟/);
    return m ? m[1] : '15';  // default fallback
  }

// Issue #372: error_code → i18n mapping; raw message goes to console only
  function errMsg(e) {
    var code = (e && e.error_code) || '';
    if (code) {
      var localized = I18N.t('app.error.' + code);
      if (localized && localized !== ('app.error.' + code)) return localized;
    }
    if (e && e.message) {
      console.error('[app] raw error:', e.message, 'code:', code || 'NONE');
      return e.message;
    }
    return I18N.t('common.request_failed', {status: (e && e.status) || 0}) || 'Request failed';
  }

  function showToast(msg, desc, duration, link) {
    // Issue #199: 先移除已有 Toast，避免连续提示叠加
    var existing = document.querySelector('[role="status"].app-toast');
    if (existing) existing.remove();
    // desc 可以是数字（旧调用兼容：showToast(msg, 3000)）
    if (typeof desc === 'number') { duration = desc; desc = ''; }
    var ms = typeof duration === 'number' ? duration : 3000;
    var t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(10px);background:#101724;color:#fff;padding:10px 20px;border-radius:10px;font-size:13px;z-index:10000;box-shadow:0 8px 32px rgba(0,0,0,.18);white-space:pre-line;max-width:80vw;opacity:0;transition:opacity .25s ease,transform .25s cubic-bezier(.25,.1,.25,1)';
    t.setAttribute('role', 'status');
    t.classList.add('app-toast');
    // 有描述时显示主文案(粗体) + 描述(小字)
    if (desc) {
      var _linkHtml = '';
      if (link && link.url) {
        // 可选操作链接（如「查看配置帮助」）——挂在 desc 区，点击不关闭 Toast
        _linkHtml = ' <a href="' + _esc(link.url) + '" target="_blank" rel="noopener" '
          + 'style="color:#7ab8ff;text-decoration:underline;cursor:pointer" onclick="event.stopPropagation()">'
          + _esc(link.text || link.url) + '</a>';
      }
      t.innerHTML = '<div style="font-weight:600">' + _esc(msg) + '</div>'
        + '<div style="font-size:11px;opacity:.8;margin-top:2px">' + _esc(desc) + _linkHtml + '</div>';
    } else {
      t.textContent = msg;
    }
    document.body.appendChild(t);
    // Apple 风格淡入 + 上移
    requestAnimationFrame(function() {
      t.style.opacity = '1';
      t.style.transform = 'translateX(-50%) translateY(0)';
    });
    // 淡出后移除
    var removeTimer = setTimeout(function() {
      t.style.opacity = '0';
      t.style.transform = 'translateX(-50%) translateY(10px)';
      setTimeout(function() { t.remove(); }, 300);
    }, ms - 300);
    // 点击 Toast 提前关闭
    t.addEventListener('click', function() {
      clearTimeout(removeTimer);
      t.style.opacity = '0';
      t.style.transform = 'translateX(-50%) translateY(10px)';
      setTimeout(function() { t.remove(); }, 300);
    });
  }

  // ── 通用确认对话框（Issue #125: 替换原生 confirm）──
  // 复用 .create-repo-overlay + .confirm-modal CSS，居中显示，防滚动
  function showConfirm(message, onConfirm, opts) {
    closeConfirm();
    var o = opts || {};
    var danger = !!o.danger;
    var hideActions = !!o.hideActions;
    var confirmText = o.confirmText || (I18N.t('common.confirm') || '确认');
    var cancelText = o.cancelText || (I18N.t('common.cancel') || '取消');
    var overlay = document.createElement('div');
    overlay.id = 'grapeConfirmOverlay';
    overlay.className = 'create-repo-overlay';
    var actionsHtml = hideActions ? '' :
      '<div class="confirm-modal-actions">' +
        '<button class="cr-btn cr-btn-ghost" id="grapeConfirmCancel">' + cancelText + '</button>' +
        '<button class="cr-btn ' + (danger ? 'cr-btn-danger' : 'cr-btn-primary') + '" id="grapeConfirmOk">' + confirmText + '</button>' +
      '</div>';
    overlay.innerHTML = '<div class="confirm-modal">' +
      '<div class="confirm-modal-msg">' + message + '</div>' +
      actionsHtml +
    '</div>';
    document.body.appendChild(overlay);
    document.body.style.overflow = 'hidden';
    function _close() { overlay.remove(); document.body.style.overflow = ''; }
    overlay.onclick = function(e) { if (e.target === overlay) _close(); };
    if (!hideActions) {
      document.getElementById('grapeConfirmOk').onclick = function() { _close(); if (onConfirm) onConfirm(); };
      document.getElementById('grapeConfirmCancel').onclick = _close;
    }
  }
  function closeConfirm() {
    var ex = document.getElementById('grapeConfirmOverlay');
    if (ex) { ex.remove(); document.body.style.overflow = ''; }
  }
  // 跨模块确认弹窗契约：IssuesView 挂载时会覆盖为它的完整实现；但组织页/反馈页
  // 不依赖 Issues 先挂载，必须在这里先兜底。否则 window._grapeConfirm 为 undefined，
  // 组织页「解绑仓库 / 删除组织」等按钮点击直接 TypeError——弹窗不出、请求不发，
  // 表现就是“点了没反应”（2026-11-19 实测）。
  if (typeof window._grapeConfirm !== 'function') {
    window._grapeConfirm = function (opts, event) {
      var o = opts || {};
      showConfirm(o.message || o.title || '', function () {
        if (typeof o.onConfirm === 'function') o.onConfirm();
      }, { danger: !!o.danger, confirmText: o.confirmText, cancelText: o.cancelText });
    };
  }

  // ── 统一模态弹层（R13-4: z=1001，压过 .tb 1000，低于 toast 10000 / warn 9999）──
  var _modalEl = null;
  function showModal(html, opts) {
    closeModal();
    var o = opts || {};
    var overlay = document.createElement('div');
    overlay.id = 'grapeModalOverlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:1001;background:' +
      (o.bg || 'rgba(0,0,0,.35)') + ';backdrop-filter:' +
      (o.blur || 'blur(6px)') + ';-webkit-backdrop-filter:' +
      (o.blur || 'blur(6px)') + ';display:flex;align-items:center;justify-content:center;padding:20px';
    var box = document.createElement('div');
    box.style.cssText = 'width:440px;max-width:92vw;background:var(--c);border-radius:16px;padding:26px;max-height:90vh;overflow-y:auto;box-shadow:0 12px 40px rgba(0,0,0,.12)';
    box.innerHTML = html;
    overlay.appendChild(box);
    var closeOnOutside = o.closeOnOutside !== false;
    if (closeOnOutside) {
      overlay.onclick = function(e) {
        if (e.target === overlay) {
          if (o.onClickOutside) { o.onClickOutside(); } else { closeModal(); }
        }
      };
    }
    document.body.appendChild(overlay);
    document.body.style.overflow = 'hidden';
    _modalEl = overlay;
    return box;
  }
  function closeModal() {
    if (_modalEl) { _modalEl.remove(); _modalEl = null; document.body.style.overflow = ''; }
  }

  // ── 统一输入弹层（R13-4: 替代原生 prompt()）──
  function showPrompt(opts) {
    var o = opts || {};
    var box = showModal(
      '<h3>' + (o.title || '') + '</h3>' +
      '<div class="srow" style="margin-top:12px"><input type="text" id="grapePromptInput" class="kb-input" placeholder="' +
      (o.placeholder || '') + '" value="' + (o.value || '') + '"></div>' +
      '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:16px">' +
      '<button class="bt bt-o" id="grapePromptCancel">' + (o.cancelText || I18N.t('common.cancel') || '取消') + '</button>' +
      '<button class="bt bt-p" id="grapePromptOk">' + (o.confirmText || I18N.t('common.confirm') || '确认') + '</button>' +
      '</div>',
      { closeOnOutside: true }
    );
    var input = box.querySelector('#grapePromptInput');
    if (input) { input.focus(); input.select(); }
    box.querySelector('#grapePromptCancel').onclick = closeModal;
    box.querySelector('#grapePromptOk').onclick = function() {
      var val = input ? input.value.trim() : '';
      if (o.validate && !o.validate(val)) return;
      closeModal();
      if (o.onConfirm) o.onConfirm(val);
    };
    if (input) {
      input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { box.querySelector('#grapePromptOk').click(); }
      });
    }
  }

  // ── 帮助抽屉 ──
  function openHelp() {
    const overlay = document.getElementById('helpOverlay');
    const drawer = document.getElementById('helpDrawer');
    if (!overlay || !drawer) return;
    overlay.classList.add('show');
    drawer.classList.add('show');
    // 动态加载 help.html 到 iframe（避免跨域，用同路径）
    const body = drawer.querySelector('.help-drawer-body');
    if (body && !body.querySelector('iframe')) {
      body.innerHTML = '<iframe src="/static/help.html" title="Grape 使用帮助"></iframe>';
    }
  }
  // Issue #2089: 全屏/还原切换——只切换 CSS 类，不重建 iframe，保留阅读位置
  function toggleHelpFullscreen() {
    const drawer = document.getElementById('helpDrawer');
    if (!drawer) return;
    const on = drawer.classList.toggle('fullscreen');
    const btn = document.getElementById('helpFullscreenBtn');
    if (btn) {
      btn.textContent = on ? '⇲' : '⛶';
      btn.title = I18N.t(on ? 'app.help.exit_fullscreen' : 'app.help.fullscreen') || (on ? '退出全屏' : '全屏');
      btn.setAttribute('aria-label', btn.title);
    }
  }
  // Issue #2089: 新标签页打开帮助（独立全屏页面，非 iframe）
  function openHelpNewTab() {
    window.open('/static/help.html', '_blank', 'noopener');
  }
  function closeHelp() {
    const overlay = document.getElementById('helpOverlay');
    const drawer = document.getElementById('helpDrawer');
    // Issue #2089: 关闭前先退出全屏态（fullscreen 类若残留，抽屉下次打开直接铺满）
    if (drawer) drawer.classList.remove('fullscreen');
    const fsBtn = document.getElementById('helpFullscreenBtn');
    if (fsBtn) {
      fsBtn.textContent = '⛶';
      fsBtn.title = I18N.t('app.help.fullscreen') || '全屏';
    }
    if (overlay) overlay.classList.remove('show');
    if (drawer) drawer.classList.remove('show');
  }

  // P0-2（Issue #3054）：popup 结果消费方——origin + source 双校验后处理
  // oauth_result。只认本页亲自打开的 popup（window.__oauthPopupWin），
  // 同源其它窗口的伪造消息在这里被丢弃。
  //
  // 休眠说明（勿当垃圾删除）：登录场景走全页重定向（见 oauthLogin），从不打开 popup，
  // 因此本监听当前收不到 action='login' 的消息；__oauthPopupWin 也只有绑定场景
  // （AccountView.registerOAuthPopup）会赋值。它保留的理由是这份 origin+source 双校验
  // 是 P0-2 的唯一实现——将来登录若改回 popup 模式，删除后必须重写（且容易只校验
  // origin 而漏掉 source，退回被同源窗口伪造 oauth_result 的洞）。
  function _onOAuthPopupMessage(event) {
    // origin + source 双校验：同源任意窗口都能伪造 oauth_result（伪登录成功 /
    // 提交任意 exchange_code）。未记录 popup 引用时一律丢弃。
    if (event.origin !== window.location.origin) return;
    if (!window.__oauthPopupWin || event.source !== window.__oauthPopupWin) return;
    var d = event.data;
    if (!d || d.type !== 'oauth_result' || d.action !== 'login') return;
    if (d.success && d.exchange_code) {
      fetch('/api/auth/exchange', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code: d.exchange_code}),
      }).then(function(r) {
        if (!r.ok) {
          return r.json().catch(function() { return null; }).then(function(b) {
            var det = (b && b.detail) || {};
            throw new Error(det.message || det.error_code || 'exchange_failed');
          });
        }
        return r.json();
      }).then(function(t) {
        localStorage.setItem('token', t.token);
        localStorage.setItem('app_cache_ver', '46');
        localStorage.removeItem('repo_member_cache');
        localStorage.removeItem('currentRepo');
        localStorage.removeItem('current_org_id');
        sessionStorage.removeItem('currentRepo');
        // UC15g（外审发现）：保留 /en 语言前缀——硬编码 '/' 会把英文用户
        // 打回中文首页。与原 exchangeCode 分支 lang0/prefix0 同款逻辑。
        var _lg = (typeof I18N !== 'undefined') ? I18N.getLang() : (localStorage.getItem('grapeLang') || 'zh');
        location.replace((_lg === 'en' ? '/en' : '') + '/');
      }).catch(function(e) {
        showToast((e && e.message) || I18N.t('app.login.oauth_exchange_failed') || 'OAuth login failed, please retry', 6000);
      });
    } else if (d.error) {
      showToast(_safeDecodeParam((d.error + '').replace(/\+/g, ' ')) || I18N.t('app.login.oauth_exchange_failed') || 'OAuth login failed, please retry', 6000);
    }
  }

  function init() {
    // Issue #231: 最先执行 currentRepo 迁移（localStorage → sessionStorage），
    // 必须在 cache 版本检查之前，避免 localStorage.clear() 抹掉待迁移值，保证已有用户无感知。
    _migrateCurrentRepo();
    console.log("[init] start", JSON.stringify({
      hasToken: !!localStorage.getItem('token'),
      cacheVer: localStorage.getItem('app_cache_ver'),
      currentRepo: sessionStorage.getItem('currentRepo'),
      url: window.location.href
    }));
    // 同步可折叠分组的持久化状态（Issue #66）
    initGroupCollapse();
    // 同步 sidebar 折叠/展开持久化状态（Issue #207）
    initSidebarState();
    // Check for OAuth token in URL first
    const p = new URLSearchParams(window.location.search);
    // Check for OAuth error in URL (e.g. github.com timeout)
    if (p.get('oauth_error')) {
      _handleOAuthErrorParam(p);
      if (window.history.replaceState) {
        window.history.replaceState(null, '', window.location.pathname);
      }
    }

    // 绑定/解绑回调检测（全局，不依赖 nav('account')）
    _checkBindSuccess();

    // UC15d（#535）：popup 登录消息消费方——此前 /login/{provider}?popup=true 的
    // 回调经 oauth-popup-callback.html postMessage {type:'oauth_result',
    // action:'login'} 但 app.js 无监听（休眠路径）。此处补齐：成功→用
    // exchange_code 兑票、存 token、整页重载走回访用户路径；失败→toast。
    // 默认登录仍是全页重定向（oauthLogin 不带 popup），本监听只激活休眠路径，
    // 不改变默认 UX。幂等守卫防止 init 重复注册。
    if (!window.__oauthPopupLoginListener) {
      window.__oauthPopupLoginListener = true;
      window.addEventListener('message', _onOAuthPopupMessage);
    }

    // P0-3: Check for exchange_code in URL (new secure flow)
    const exchangeCode = p.get('exchange_code');
    // UC15f（#535 竞态修复）：内联兑票 fetch 进行中而 app.js init 先到——
    // URL 已被 strip、接力 flag 未落，直接走默认路径会把已入库的 token 孤立
    // 在登录页。见 pending 标记则轮询等待终态 flag（上限 5s，终态必到：
    // 内联脚本成败都会摘 pending + 置对应 flag）。
    if (!exchangeCode && sessionStorage.getItem('__oauth_inline_pending__')) {
      var _pf = null;
      var _pfDone = false;  // 双重重入守卫：interval 与 5s 超时都可能触发，只放行一次
      var _pfReenter = function () {
        if (_pfDone) return;
        _pfDone = true;
        if (_pf) clearInterval(_pf);
        init();
      };
      try { _pf = setInterval(function () {
        if (sessionStorage.getItem('__oauth_inline_login__') ||
            sessionStorage.getItem('__oauth_exchange_failed__') ||
            !sessionStorage.getItem('__oauth_inline_pending__')) {
          _pfReenter();
        }
      }, 100); } catch (e) {}
      setTimeout(_pfReenter, 5000);
      return;
    }
    // UC15（#535）：内联兑票脚本（main.py 注入，HTML 解析期执行）已抢先兑票并
    // strip 了 URL——这里到不了 exchangeCode 分支，改由 sessionStorage flag 接力：
    // token/缓存清理内联脚本已做完，本块补上它做不了的（DOM/I18N/API 模块依赖）。
    if (!exchangeCode && sessionStorage.getItem('__oauth_inline_login__')) {
      sessionStorage.removeItem('__oauth_inline_login__');
      const inlineFirstLogin = sessionStorage.getItem('__oauth_inline_first_login__');
      if (inlineFirstLogin) {
        sessionStorage.removeItem('__oauth_inline_first_login__');
        sessionStorage.removeItem('currentRepo');
        $('sidebar').style.display = 'none';
        $('tb').style.display = 'none';
        $('mainView').style.display = 'flex';
        $('mainView').style.position = 'fixed';
        $('mainView').style.inset = '0';
        $('mainView').style.zIndex = '100';
        $('pageTitle').textContent = I18N.t('app.current_project');
      }
      showApp();
      API.scheduleProactiveRefresh();
      return;
    }
    // UC15 失败兜底：内联兑票 401（过期/无效）→ 与原 app.js 兑票失败同款 UX；
    // UC15c：后端 detail（message/error_code）已由内联脚本解析存入
    // __oauth_exchange_failed_msg__，有则优先展示具体原因，无则回退通用文案
    if (!exchangeCode && sessionStorage.getItem('__oauth_exchange_failed__')) {
      sessionStorage.removeItem('__oauth_exchange_failed__');
      var _exMsg = sessionStorage.getItem('__oauth_exchange_failed_msg__') || '';
      if (_exMsg) sessionStorage.removeItem('__oauth_exchange_failed_msg__');
      showToast(_exMsg || I18N.t('app.login.oauth_exchange_failed') || 'OAuth login failed, please retry', 6000);
      if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
      return;
    }
    if (exchangeCode) {
      // 立即清除 URL 参数（不等待响应）
      if (window.history.replaceState) {
        const lang0 = (typeof I18N !== 'undefined') ? I18N.getLang() : (localStorage.getItem('grapeLang') || 'zh');
        const prefix0 = lang0 === 'en' ? '/en' : '';
        window.history.replaceState(null, '', prefix0 + '/');
      }
      // POST /auth/exchange 换 JWT
      // R5: Parse error response body to surface backend-specific message
      fetch('/api/auth/exchange', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code: exchangeCode}),
      }).then(r => {
        if (!r.ok) {
          // R5: Parse error body to surface backend-specific message
          return r.json().catch(() => null).then(errBody => {
            var detail = (errBody && errBody.detail) || {};
            var msg = detail.message || detail.error_code || (I18N.t('app.login.oauth_exchange_failed') || 'OAuth login failed, please retry');
            var e = new Error(msg);
            e.error_code = detail.error_code || '';
            e.status = r.status;
            throw e;
          });
        }
        return r.json();
      }).then(d => {
        localStorage.setItem('token', d.token);
        localStorage.setItem('app_cache_ver', '46');
        localStorage.removeItem(REPO_CACHE_KEY);
        localStorage.removeItem('currentRepo');
        localStorage.removeItem('current_org_id');
        sessionStorage.removeItem('currentRepo');
        if (d.first_login) {
          sessionStorage.removeItem('currentRepo');
          $('sidebar').style.display = 'none';
          $('tb').style.display = 'none';
          $('mainView').style.display = 'flex';
          $('mainView').style.position = 'fixed';
          $('mainView').style.inset = '0';
          $('mainView').style.zIndex = '100';
          $('pageTitle').textContent = I18N.t('app.current_project');
        }
        showApp();
        API.scheduleProactiveRefresh();
      }).catch(e => {
        showToast(I18N.t('app.login.oauth_exchange_failed') || 'OAuth login failed, please retry', 6000);
        if (typeof app !== 'undefined' && app.showLogin) app.showLogin();
      });
      return;
    }

    // Legacy: Check for OAuth token in URL (old flow, kept for transition)
    const urlToken = p.get('token');
    if (urlToken) {
      localStorage.setItem('token', urlToken);
      localStorage.setItem('app_cache_ver', '46');
      // 每次 OAuth 登录都清除上一个用户的仓库缓存+选择+组织上下文——防止切换账号后看到旧数据或 403
      localStorage.removeItem(REPO_CACHE_KEY);
      localStorage.removeItem('currentRepo');
      localStorage.removeItem('current_org_id');
      sessionStorage.removeItem('currentRepo');
      if (p.get('first_login') === '1') {
        sessionStorage.removeItem('currentRepo');
        // Immediately show repo selection layout, skip Dashboard flash
        $('sidebar').style.display = 'none';
        $('tb').style.display = 'none';
        $('mainView').style.display = 'flex';
        $('mainView').style.position = 'fixed';
        $('mainView').style.inset = '0';
        $('mainView').style.zIndex = '100';
        $('pageTitle').textContent = I18N.t('app.current_project');
      }
      showApp();
      API.scheduleProactiveRefresh();  // Issue #86: OAuth 登录后启动定时器
      // Remove token from URL without reload (preserve language prefix)
      if (window.history.replaceState) {
        const lang = (typeof I18N !== 'undefined') ? I18N.getLang() : (localStorage.getItem('grapeLang') || 'zh');
        const prefix = lang === 'en' ? '/en' : '';
        window.history.replaceState(null, '', prefix + '/');
      }
      return;
    }

    // Cache version check - only clear if version mismatch AND not in OAuth flow
    if (localStorage.getItem('app_cache_ver') !== '46') {
      const oldToken = localStorage.getItem('token');
      const oldUser = localStorage.getItem('user');
      localStorage.clear();
      if (oldToken) localStorage.setItem('token', oldToken);
      if (oldUser) localStorage.setItem('user', oldUser);
      localStorage.setItem('app_cache_ver', '46');
    }

    const token = localStorage.getItem('token');
    if (token) {
      showApp();
      API.scheduleProactiveRefresh();  // Issue #86: 页面恢复时启动定时器
      // hash 路由恢复由 showApp() → loadProfile() → _restoreNavAfterLogin() 统一处理
      // 不在这里重复路由恢复，避免和 showApp 异步链竞争
    } else {
      // 无 token = 未登录/已退出。清除可能残留的仓库选择和 URL hash，
      // 防止用户直接导航到 /login（非通过 app.logout() 退出）时状态残留
      sessionStorage.removeItem('currentRepo');
      if (window.location.hash && window.location.hash.length > 1) {
        if (window.history.replaceState) {
          window.history.replaceState(null, '', window.location.pathname);
        }
      }
      showLogin();
    }
  }

  // ── Theme Management (Apple Developer Theme) ──
  // Theme: 'apple' only（2026-11-12 默认风格退役第一步：老用户的 'default' 在读取时就地迁移）
  // Mode: 'light' | 'dark' | 'auto'
  function getTheme() {
    if (localStorage.getItem('grapeTheme') !== 'apple') {
      localStorage.setItem('grapeTheme', 'apple');
      document.documentElement.setAttribute('data-theme', 'apple');
      setThemeMode(localStorage.getItem('grapeThemeMode') || 'light');
    }
    return 'apple';
  }
  function getThemeMode() { return localStorage.getItem('grapeThemeMode') || 'light'; }

  function setTheme(theme) {
    // 只接受 'apple'：传 'default' 也落到 Liquid Glass（默认风格已退役）
    theme = 'apple';
    localStorage.setItem('grapeTheme', theme);
    const html = document.documentElement;
    html.setAttribute('data-theme', 'apple');
    // re-apply mode so dark-auto picks up
    setThemeMode(getThemeMode());
    // dispatch event so pages can react if needed
    window.dispatchEvent(new CustomEvent('themechange', {detail: {theme}}));
  }

  function setThemeMode(mode) {
    localStorage.setItem('grapeThemeMode', mode);
    const html = document.documentElement;
    if (getTheme() === 'apple') {
      html.setAttribute('data-theme-mode', mode);
    }
  }

  // 注：`search` 已随旧版顶栏搜索框一起移除（全仓无调用方），不要再挂进导出表——
  // 悬空引用会让 IIFE 求值即抛 ReferenceError，window.app 永不出现（壳整页空白）。
  return {nav, refreshRepoLinks, syncAppPath: _syncAppPath, oauthLogin, renderOAuthButtons, openBetaApply, submitBetaApply, baSelectIdentity, baToggleOther, logout, showToast, errMsg, openHelp, closeHelp, toggleHelpFullscreen, openHelpNewTab, switchRepo, setRepoContext: _applyRepoState, switchOrg, goToRepos, currentRepo, showLogin, init, loadRepos, _openOrgDemandCenter, _forceSyncRepos, _renderBindingGuide, _rerenderReposView, _clearCachedReposData, _clearSensitiveModals, initSidebarRepos, _updateSidebarRepos, loadProfile, getTheme, getThemeMode, setTheme, setThemeMode, toggleGroup, toggleSidebar, showConfirm, closeConfirm, showModal, closeModal, showPrompt, _toggleOrgDropdown, _updateTbOrgName, _toggleLobbyOrgDropdown, _updateLobbyOrgName, _toggleUserMenu, _toggleLobbyUserMenu, _toggleTbUserMenu, _loadTbUserMenu, _accountGoBack, pwdLogin, _toggleLoginPwd, _blurValidateLogin, _clearLoginError, _submitValidateLogin, saveProfile, showChangePassword, doChangePassword, showSetPassword, sendSetPasswordCode, doSetPassword, showChangeEmail, sendChangeEmailCode, doChangeEmail, bindProvider, _openBindModal, deleteAccount, _daChoosePwd, _daChooseCode, sendDeleteAccountCode, doDeleteAccount, _checkOAuthCompletion, _dismissOAuthCompletion, _copyEngineCmd, _dismissEngineBanner, _gotoEnginePage, _checkEngineBanner};
})();

/** 根据 repo_key 生成 VCS 平台 URL（仓库/Issue 等通用）。 */
function _vcsUrl(repoKey, path) {
  var rk = repoKey || '';
  var i = rk.indexOf(':');
  var prov = i > 0 ? rk.slice(0, i) : '';
  var repoPath = i > 0 ? rk.slice(i + 1) : rk;
  // 域名按平台目录取（platform-icons.js），未知平台不回退到 gitcode——宁可返空也不给错链
  var dom = (typeof window.platformHost === 'function' && window.platformHost(prov)) || '';
  if (!dom) dom = 'gitcode.com';
  return 'https://' + dom + '/' + repoPath + (path || '');
}
document.addEventListener('DOMContentLoaded', function() {
  // 等 i18n 语言包异步加载完成后初始化，避免刷新页面时 I18N.t() 返回 key
  if (typeof I18N !== 'undefined' && !I18N.isLangLoaded(I18N.getLang())) {
    // i18n.init 刚触发 loadPacks（异步），等待最多 3 秒
    var _waitI18n = function(attempts) {
      if (I18N.isLangLoaded(I18N.getLang()) || attempts <= 0) {
        app.init();
      } else {
        setTimeout(function() { _waitI18n(attempts - 1); }, 50);
      }
    };
    _waitI18n(60); // 60 * 50ms = 3s
  } else {
    app.init();
  }
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') app.closeHelp(); });

// Issue #67: 全局苹果风格确认 modal（knowledge.js / skills.js 共用）
function _showAppleConfirm(message, onConfirm, onCancel) {
  var modal = document.createElement('div');
  modal.id = 'appleConfirmModal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center';
  modal.innerHTML = '<div class="cd" style="width:360px;padding:24px;border-radius:14px;text-align:center">' +
    '<p style="font-size:14px;color:var(--t);margin-bottom:20px;line-height:1.5">' + message + '</p>' +
    '<div style="display:flex;gap:10px">' +
    '<button id="appleConfirmCancel" style="flex:1;padding:10px 16px;font-size:13px;border:1px solid #d1d1d6;border-radius:10px;background:transparent;color:#1d1d1f;cursor:pointer">' + (I18N.t('common.cancel') || '取消') + '</button>' +
    '<button id="appleConfirmOk" style="flex:1;padding:10px 16px;font-size:13px;border:none;border-radius:10px;background:#ff3b30;color:#fff;cursor:pointer">' + (I18N.t('common.confirm') || '确认') + '</button>' +
    '</div></div>';
  document.body.appendChild(modal);
  modal.onclick = function(e) { if (e.target === modal) { modal.remove(); if (onCancel) onCancel(); } };
  document.getElementById('appleConfirmOk').onclick = function() { modal.remove(); if (onConfirm) onConfirm(); };
  document.getElementById('appleConfirmCancel').onclick = function() { modal.remove(); if (onCancel) onCancel(); };
}

window.app = app; // Vue view chunks bridge (batch 2)
window.fmtLocalTime = fmtLocalTime; window._vcsUrl = _vcsUrl; // Vue view chunks bridge (batch 6)
