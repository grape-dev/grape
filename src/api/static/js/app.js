// ═══════════════════════════════════════════════════════════════
// repo_key 前缀统一剥离（对应后端 src/repo_key.py 的 from_repo_key）
// 外部/前端约定一律无前缀；dashboard 响应可能带 gitcode:/github: 前缀，需剥离
// ═══════════════════════════════════════════════════════════════
function stripRepoPrefix(s) {
  return String(s || '').replace(/^(gitcode|github|gitlab):/, '');
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
  'BOT_GITCODE_TOKEN','USER_ID','CONVERSATION_ID'
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
    // scope：互斥后选中即入池；什么都不选 → ["all"]（全量）
    const checked = Array.prototype.slice.call(row.querySelectorAll('.adv-kv-scope:checked'))
      .map(cb => cb.value);
    const scope = checked.length ? checked : ['all'];
    fields.push({ name: n, value: (vEl.value || '').trim(), is_secret: isSec, scope: scope, preset: isPreset });
  }
  return { fields: fields };
}

const app = (() => {
  const $ = id => document.getElementById(id);
  const VIEWS = ['dash','pipe','vod','issue','coding','qa','sre','chat','im-channel','settings','skills','knowledge','runtimes','event','repos','skill','tasks','observability','scene-market','scene-detail','my-feedback','my-feedback-detail','account'];

  // ── 浏览器前进/后退支持 ──
  // nav()/goToRepos() 把视图写入 history（#view/param），popstate 回放渲染。
  // _replaying: popstate 回放时禁止再次入栈；_histReady: 首个视图用 replaceState，
  // 避免登录后第一次导航多出一个无意义的入口。
  let _replaying = false;
  let _histReady = false;
  function _pushHistory(view, param) {
    if (_replaying) return;
    const hash = '#' + view + (param !== undefined && param !== null ? '/' + encodeURIComponent(param) : '');
    if (window.location.hash === hash) return; // 同一视图不重复入栈
    try {
      const fn = _histReady ? 'pushState' : 'replaceState';
      if (window.history[fn]) {
        window.history[fn]({view: view, param: (param !== undefined && param !== null) ? param : null}, '', hash);
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

  function showLogin() {
    document.documentElement.removeAttribute('data-logged-in');
    $('loginView').classList.remove('hid');
    $('sidebar').style.display = 'none';
    $('mainView').style.display = 'none';
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
    // 工作台已由 CSS 显示，立即展示加载中状态
    $('pageContent').innerHTML = '<div class="loading">' + I18N.t('common.loading') + '</div>';
    loadAgentTypes();
    // 串行加载：loadProfile 获取 bindings → 根据绑定状态和 hash 路由决定渲染哪个页面
    loadProfile().then(() => {
      _restoreNavAfterLogin();
    }).catch(() => {
      _restoreNavAfterLogin();  // profile 失败时也走统一路由恢复（检查 bindings → 引导页/loadRepos）
    });
  }

  // 统一路由恢复：密码登录 / OAuth 登录 / 页面刷新共用
  // 优先级：hash 路由 > 有绑定→loadRepos > 无绑定→引导页
  function _restoreNavAfterLogin() {
    // 已有视图（如 loadRepos 同步缓存路径已渲染）→ 不覆盖
    if (window._currentView && window._currentView !== '') return;

    const hash = window.location.hash;
    const user = JSON.parse(localStorage.getItem('user') || '{}');
    const bindings = user.bindings || [];
    const noRepoViews = ['repos', 'settings', 'tasks', 'runtimes', 'event', 'scene-market', 'scene-detail', 'my-feedback', 'my-feedback-detail', 'account'];

    if (hash && hash.length > 1) {
      // 有 hash 路由 → 恢复到该页面
      const parts = hash.substring(1).split('/');
      const view = parts[0];
      const param = parts[1] ? decodeURIComponent(parts[1]) : undefined;
      if (noRepoViews.includes(view) || currentRepo()) {
        nav(view, param);
      } else {
        nav('dash');
      }
    } else if (bindings.length > 0) {
      // 有绑定无 hash → 加载仓库列表
      loadRepos();
    } else if (currentRepo()) {
      // 无绑定但有当前仓库 → dashboard
      nav('dash');
    } else {
      // 无绑定无 hash → 引导页
      goToRepos();
    }
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
  function _loadTbOrgs() {
    var menu = document.getElementById('tbOrgMenu');
    if (!menu) return;
    menu.innerHTML = '<div style="padding:8px 12px;color:var(--t3);font-size:12px">' + (I18N.t("organizations.loading") || "加载中...") + '</div>';
    API.get('/orgs').then(function(orgs) {
      if (!orgs || orgs.length === 0) {
        menu.innerHTML = '<div style="padding:8px 12px;color:var(--t3);font-size:12px">' + (I18N.t("organizations.no_orgs") || "暂无组织") + '</div>';
        return;
      }
      var currentId = localStorage.getItem('current_org_id') || '';
      var html = '';
      orgs.forEach(function(o) {
        var isActive = String(o.id) === String(currentId);
        var bg = isActive ? 'var(--a)' : 'transparent';
        var fg = isActive ? '#fff' : 'var(--t1)';
        var badge = o.is_personal ? ' <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:' + (isActive ? 'rgba(255,255,255,.2)' : 'var(--bg)') + ';color:' + (isActive ? '#fff' : 'var(--t3)') + '">' + (I18N.t("organizations.badge_personal") || "默认") + '</span>' : '';
        html += '<div onclick="app.switchOrg(' + o.id + ');document.getElementById(\'tbOrgMenu\').style.display=\'none\'" style="cursor:pointer;padding:8px 12px;border-radius:8px;background:' + bg + ';color:' + fg + ';display:flex;align-items:center;gap:8px;margin:2px 0" onmouseover="if(!this.dataset.active){this.style.background=\'var(--bg)\'}" onmouseout="if(!this.dataset.active){this.style.background=\'transparent\'}" data-active="' + (isActive ? '1' : '') + '">'
          + '<span style="font-size:14px">🏢</span>'
          + '<div style="flex:1;min-width:0">'
          + '<div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + (o.name || '') + badge + '</div>'
          + '<div style="font-size:10px;opacity:.7">' + (o.member_count || 1) + ' ' + (I18N.t("organizations.members") || "成员") + ' · ' + (o.role || 'member') + '</div>'
          + '</div>'
          + (isActive ? '<span style="font-size:12px">✓</span>' : '')
          + '</div>';
      });
      html += '<div style="border-top:1px solid var(--b);margin-top:4px;padding-top:4px">'
        + '<div onclick="app.nav(\'organizations\');document.getElementById(\'tbOrgMenu\').style.display=\'none\'" style="cursor:pointer;padding:8px 12px;border-radius:8px;color:var(--a);font-size:12px;font-weight:500;display:flex;align-items:center;gap:6px">'
        + '<span>⚙</span><span>' + (I18N.t("organizations.manage_orgs") || "管理组织") + '</span></div></div>';
      menu.innerHTML = html;
    }).catch(function() {
      menu.innerHTML = '<div style="padding:8px 12px;color:var(--r);font-size:12px">' + (I18N.t("organizations.load_failed", {msg: ""}) || "加载失败") + '</div>';
    });
  }
  function _updateTbOrgName() {
    // Issue #299: 显示当前已选空间名称（而非固定文本"我的组织"）
    var nameEl = document.getElementById('tbOrgName');
    if (!nameEl) return;
    var currentId = localStorage.getItem('current_org_id') || '';
    if (!currentId) return; // 未选择组织时保留 i18n 默认文本
    API.get('/orgs').then(function(orgs) {
      if (!orgs || orgs.length === 0) return;
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
    var currentId = localStorage.getItem('current_org_id') || '';
    menu.innerHTML = '<div style="padding:8px 12px;color:var(--t3);font-size:12px">' + (I18N.t("organizations.loading") || "加载中...") + '</div>';
    API.get('/orgs').then(function(orgs) {
      if (!orgs || orgs.length === 0) {
        menu.innerHTML = '<div style="padding:8px 12px;color:var(--t3);font-size:12px">' + (I18N.t("organizations.no_orgs") || "暂无组织") + '</div>';
        return;
      }
      var html = '';
      orgs.forEach(function(o) {
        var isActive = String(o.id) === String(currentId);
        var bg = isActive ? 'var(--a)' : 'transparent';
        var fg = isActive ? '#fff' : 'var(--t1)';
        var badge = o.is_personal ? ' <span style="font-size:9px;padding:1px 4px;border-radius:3px;background:' + (isActive ? 'rgba(255,255,255,.2)' : 'var(--bg)') + ';color:' + (isActive ? '#fff' : 'var(--t3)') + '">' + (I18N.t("organizations.badge_personal") || "默认") + '</span>' : '';
        html += '<div onclick="app.switchOrg(' + o.id + ');document.getElementById(\'lobbyOrgMenu\').style.display=\'none\';app._updateLobbyOrgName()" style="cursor:pointer;padding:7px 12px;border-radius:8px;background:' + bg + ';color:' + fg + ';display:flex;align-items:center;gap:8px;margin:2px 0" onmouseover="if(!this.dataset.active){this.style.background=\'var(--bg)\'}" onmouseout="if(!this.dataset.active){this.style.background=\'transparent\'}" data-active="' + (isActive ? '1' : '') + '">'
          + '<span style="font-size:13px">🏢</span>'
          + '<div style="flex:1;min-width:0">'
          + '<div style="font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + (o.name || '') + badge + '</div>'
          + '<div style="font-size:10px;opacity:.7">' + (o.member_count || 1) + ' ' + (I18N.t("organizations.members") || "成员") + ' · ' + (o.role || 'member') + '</div>'
          + '</div>'
          + (isActive ? '<span style="font-size:11px">✓</span>' : '')
          + '</div>';
      });
      html += '<div style="border-top:1px solid var(--b);margin-top:4px;padding-top:4px">'
        + '<div onclick="app.nav(\'organizations\');document.getElementById(\'lobbyOrgMenu\').style.display=\'none\'" style="cursor:pointer;padding:8px 12px;border-radius:8px;color:var(--a);font-size:12px;font-weight:500;display:flex;align-items:center;gap:6px;margin:2px 4px" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">'
        + '<span>⚙</span><span>' + (I18N.t("organizations.manage_orgs") || "管理组织") + '</span></div></div>';
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
      var currentId = localStorage.getItem('current_org_id') || '';
      API.get('/orgs').then(function(orgs) {
        if (!orgs || orgs.length === 0) return;
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
      var label = bindSuccess === 'gitcode' ? 'GitCode' : 'GitHub';
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
      // 清除 URL 参数
      var cleanUrl = window.location.pathname + window.location.hash;
      window.history.replaceState({}, '', cleanUrl);
    }
    if (bindError) {
      var rawErr = decodeURIComponent(bindError.replace(/\+/g, ' '));
      var friendlyMsg = _translateOAuthError(rawErr);
      showToast(I18N.t('app.login.bind_failed'), friendlyMsg);
      var cleanUrl2 = window.location.pathname + window.location.hash;
      window.history.replaceState({}, '', cleanUrl2);
    }
    // OAuth 绑定命中历史绑定 → 转移确认
    var needTransfer = params.get('need_transfer');
    var transferTicket = params.get('binding_ticket');
    if (needTransfer && transferTicket) {
      // 延迟调用，确保 account.js 已加载
      setTimeout(function() {
        if (typeof _accountShowTransferConfirm === 'function') {
          _accountShowTransferConfirm(needTransfer, transferTicket);
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
    var sb = $('sidebar'); if (sb) sb.style.display = 'flex';
    // 先检查绑定状态——未绑定直接回引导页，不走 nav('dash') 避免 403
    var _u = JSON.parse(localStorage.getItem('user') || '{}');
    var _bindings = _u.bindings || [];
    if (_bindings.length === 0) {
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
    var sel = document.getElementById('orgSelect');
    if (sel) { sel.disabled = true; }
    localStorage.setItem('current_org_id', orgId);
    // 清除仓库缓存，防止切换组织后仍显示旧组织的仓库
    localStorage.removeItem(REPO_CACHE_KEY);
    // 更新组织下拉框选中项
    if (sel) {
      for (var i = 0; i < sel.options.length; i++) {
        sel.options[i].selected = (String(sel.options[i].value) === String(orgId));
      }
      sel.disabled = false;
    }
    // 保持当前页面不变，只静默刷新仓库缓存 + 侧边栏下拉
    API.get('/repos/member').then(function(d) {
      var repos = (d && d.repos) || [];
      try { localStorage.setItem(REPO_CACHE_KEY, JSON.stringify({repos: repos, ts: Date.now()})); } catch(e) {}
      // 查找该组织已托管的仓库
      var managed = repos.filter(function(r) { return r.is_hosted || r.is_joined; });
      if (managed.length > 0) {
        // 切换到第一个已托管仓库
        var first = managed[0];
        sessionStorage.setItem('currentRepo', JSON.stringify({fullName: first.full_name, owner: first.owner, name: first.name, repoKey: first.repo_key || ''}));
        // 更新侧边栏仓库下拉
        initSidebarRepos();
        // 刷新当前页面数据（切换组织后所有视图都重新加载）
        if (window._currentView === 'repos') {
          selectRepo(first.full_name, first.owner, first.name, first.repo_key || '');
        } else if (window._currentView) {
          nav(window._currentView, _currentParam);
        }
      } else {
        // 没有已托管仓库 — 清除当前仓库选择，刷新页面显示空态
        sessionStorage.removeItem('currentRepo');
        initSidebarRepos();
        showToast(I18N.t("organizations.switch_org.no_repos"));
        // 切换组织后所有视图都重新加载（显示空态或刷新数据）
        if (window._currentView === 'repos') {
          goToRepos();
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
    }).catch(function() {});
    showToast(I18N.t("organizations.switch_org.success"));
  }

  const REPO_CACHE_KEY = 'repo_member_cache';

  function loadRepos(forceRefresh) {
    console.log("[loadRepos] start");
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
      if (r) sessionStorage.setItem('currentRepo', JSON.stringify({fullName: r.full_name, owner: r.owner, name: r.name, repoKey: r.repo_key || ''}));
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
      var _validHashViews = VIEWS.concat(['skill-sub', 'scene-detail-local', 'autopilot', 'autopilot-detail']);
      if (_validHashViews.indexOf(_hv) !== -1) {
        nav(_hv, _hp[1] ? decodeURIComponent(_hp[1]) : undefined);
        _restored = true;
      }
    }
    if (!_restored) nav('dash');
  }

  function initSidebarRepos() {
    // Lightweight version for after repo selection - just populate sidebar dropdown
    API.get('/repos/managed').then(d => {
      const repos = d.repos || [];
      window._managedRepoMap = {};
      repos.forEach(r => { window._managedRepoMap[r.full_name] = r.repo_key || ''; });
      const sel = $('repoSelect');
      const current = currentRepo();
      if (repos.length === 0) {
        sel.innerHTML = '<option value="__add__">＋ ' + (I18N.t('app.repo.add_new') || '新增仓库托管') + '</option>';
        sel.onclick = function() { switchRepo('__add__'); };
      } else {
        // 按平台分组：optgroup label 为平台名称
        var gitcodeRepos = repos.filter(function(r) { return !(r.repo_key || '').startsWith('github:'); });
        var githubRepos = repos.filter(function(r) { return (r.repo_key || '').startsWith('github:'); });
        var html = '';
        if (gitcodeRepos.length > 0) {
          html += '<optgroup label="GitCode">';
          gitcodeRepos.forEach(function(r) {
            html += `<option value="${r.full_name}" ${current && current.fullName === r.full_name ? 'selected' : ''}>${r.full_name}</option>`;
          });
          html += '</optgroup>';
        }
        if (githubRepos.length > 0) {
          html += '<optgroup label="GitHub">';
          githubRepos.forEach(function(r) {
            html += `<option value="${r.full_name}" ${current && current.fullName === r.full_name ? 'selected' : ''}>${r.full_name}</option>`;
          });
          html += '</optgroup>';
        }
        html += '<option value="__add__">＋ ' + I18N.t('app.repo.add_new') + '</option>';
        sel.innerHTML = html;
        sel.onclick = null;
      }
    }).catch(() => {});
  }

  function _updateSidebarRepos(repos) {
    const sel = $('repoSelect');
    const current = currentRepo();
    sel.innerHTML = repos.map(r =>
      `<option value="${r.full_name}" ${current && current.fullName === r.full_name ? 'selected' : ''}>${r.full_name}</option>`
    ).join('');
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
    sessionStorage.setItem('currentRepo', JSON.stringify({fullName: val, owner: parts[0], name: parts[1], repoKey: _rk}));
    // 同步侧边栏下拉选中项
    const sel = $('repoSelect');
    if (sel) sel.value = val;
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
    document.body.style.paddingTop = '44px';
  }

  function _dismissOAuthCompletion() {
    const user = JSON.parse(localStorage.getItem('user') || '{}');
    const skipKey = 'oauth_completion_skipped_' + (user.username || '');
    localStorage.setItem(skipKey, '1');
    const banner = document.getElementById('oauthCompletionBanner');
    if (banner) banner.remove();
    document.body.style.paddingTop = '';
  }

  function _renderBindingGuide() {
    var isZh = I18N.getLang() === 'zh';
    var _u = JSON.parse(localStorage.getItem('user') || '{}');
    var name = (_u.nickname || _u.username || 'User').split('@')[0];
    var avatar = (name || 'U')[0].toUpperCase();

    // R7.5: 不过滤 configured——所有平台始终显示，未配 OAuth App 时点击走手动令牌绑定
    API.get('/auth/providers').then(function(d) {
      var providers = d.providers || [];
      _renderOnboardingShell(providers, isZh, name, avatar);
    }).catch(function() {
      // Fallback: show both platforms
      _renderOnboardingShell([
        {provider: 'gitcode', name: 'GitCode', brand_color: '#F54A36'},
        {provider: 'github', name: 'GitHub', brand_color: '#1F2328'}
      ], isZh, name, avatar);
    });
  }

  function _renderOnboardingShell(providers, isZh, name, avatar) {
    var d = document.getElementById('pageContent');
    if (!d) return;

    // ── 平台绑定卡片 ──
    var platformCards = '';
    providers.forEach(function(p) {
      var icon = platformIcon(p.provider, 28);
      // 品牌色前端覆盖——后端返回的原始色值不适配 Apple 设计风格
      var _brandOverride = {gitcode: '#F54A36', github: '#1F2328'};
      var _brandColor = _brandOverride[p.provider] || p.brand_color || '';
      var brandVar = '--brand:' + (_brandColor || 'var(--a)');
      platformCards += '<div class="ob-bind-card" style="' + brandVar + '" onclick="app.bindProvider(\'' + p.provider + '\')">'
        + '<div class="ob-bind-icon">' + icon + '</div>'
        + '<div class="ob-bind-name">' + _esc(p.name) + '</div>'
        + '<button class="bt bt-p ob-bind-btn" data-i18n="app.login.ob_bind_btn">'
        + I18N.t('app.login.ob_bind_btn') + '</button>'
        + '</div>';
    });

    // ── 单屏绑定页面 ──
    var html = '<div class="lobby">'
      + '<header class="lobby-head">'
      +   '<div class="lobby-brand">'
      +     '<img class="lobby-logo" src="/static/img/grape-logo.jpg?v=1" alt="Grape">'
      +     '<span class="lobby-brand-name">Grape</span>'
      +     '<span class="lobby-brand-sub" data-i18n="repos.brand_sub">' + I18N.t('repos.brand_sub') + '</span>'
      +   '</div>'
      +   '<div style="flex:1"></div>'
      +   '<div style="display:flex;align-items:center;gap:10px">'
      +     '<div class="lang-toggle" id="lobbyLangToggle" style="display:flex;align-items:center;gap:2px;padding:3px;border-radius:8px;border:1px solid var(--b);background:var(--c)">'
      +       '<button onclick="_setAppLang(\'zh\')" class="lang-btn' + (I18N.getLang() === 'zh' ? ' active' : '') + '" style="padding:3px 10px;border-radius:6px;font-size:12px;font-weight:500;border:none;cursor:pointer;white-space:nowrap;' + (I18N.getLang() === 'zh' ? 'background:var(--a);color:#fff' : 'background:none;color:var(--t3)') + '">中</button>'
      +       '<button onclick="_setAppLang(\'en\')" class="lang-btn' + (I18N.getLang() === 'en' ? ' active' : '') + '" style="padding:3px 10px;border-radius:6px;font-size:12px;font-weight:500;border:none;cursor:pointer;white-space:nowrap;' + (I18N.getLang() === 'en' ? 'background:var(--a);color:#fff' : 'background:none;color:var(--t3)') + '">EN</button>'
      +     '</div>'
      +     '<div id="lobbyOrgDropdown" style="position:relative">'
      +       '<button onclick="app._toggleLobbyOrgDropdown()" id="lobbyOrgBtn" style="display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:8px;border:1px solid var(--b);background:var(--c);color:var(--t1);font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap">'
      +         '<span style="font-size:14px">🏢</span>'
      +         '<span class="lobby-org-name" id="lobbyOrgName">' + (I18N.t('organizations.my_orgs') || '我的组织') + '</span>'
      +       '</button>'
      +       '<div id="lobbyOrgMenu" style="display:none;position:absolute;top:100%;right:0;margin-top:4px;min-width:260px;max-height:480px;overflow-y:auto;background:var(--c);border:1px solid var(--b);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.12);z-index:9999;padding:4px"></div>'
      +     '</div>'
      +     '<div id="lobbyUserDropdown" style="position:relative">'
      +       '<button onclick="app._toggleLobbyUserMenu()" style="display:flex;align-items:center;gap:8px;padding:5px 12px;border-radius:8px;border:1px solid var(--b);background:var(--c);color:var(--t1);font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap">'
      +         '<span class="lobby-avatar">' + avatar + '</span>'
      +         '<span class="lobby-user-name">' + _esc(name) + '</span>'
      +       '</button>'
      +       '<div id="lobbyUserMenu" style="display:none;position:absolute;top:100%;right:0;margin-top:4px;min-width:260px;max-height:480px;overflow-y:auto;background:var(--c);border:1px solid var(--b);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.12);z-index:9999;padding:4px"></div>'
      +     '</div>'
      +   '</div>'
      + '</header>'
      + '<main class="lobby-main">'
      +   '<div class="lobby-content ob-content">'
      +     _obHeroHtml(name, isZh, platformCards)
      +   '</div>'
      + '</main>'
      + '</div>';

    d.innerHTML = html;
    d.classList.add('ob-mode');
    _updateTbOrgName();
    _updateLobbyOrgName();
  }

  // ── 离开引导页时清理标记 class ──
  function _obExitMode() {
    var d = document.getElementById('pageContent');
    if (d) d.classList.remove('ob-mode');
  }

  // ── 欢迎区域（单屏居中）──
  function _obHeroHtml(name, isZh, platformCards) {
    return '<section class="ob-hero">'
      + '<div class="ob-hero-logo"><img src="/static/img/grape-logo.jpg?v=1" alt="Grape"></div>'
      + '<h1 class="ob-hero-title" data-i18n="app.login.ob_cta_title">' + I18N.t('app.login.ob_cta_title') + '</h1>'
      + '<p class="ob-hero-sub" data-i18n="app.login.ob_cta_sub">' + I18N.t('app.login.ob_cta_sub') + '</p>'
      + '<div class="ob-hero-bind">' + platformCards + '</div>'
      + '<p class="ob-hero-hint" data-i18n="app.login.ob_hero_hint">' + I18N.t('app.login.ob_hero_hint') + '</p>'
      + '</section>';
  }

  function _esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function goToRepos() {
    _obExitMode();
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

    // D9: 检查 bindings——未绑定时不调 /repos/member，直接显示绑定引导
    const _user = JSON.parse(localStorage.getItem('user') || '{}');
    const _bindings = _user.bindings || [];
    if (_bindings.length === 0) {
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

  let _refreshTimer = null;
  let _currentParam = null;
  let _hiddenSince = 0;
  // 语言切换时缓存 /repos/member 数据，轻量重渲染不发 API 请求
  let _cachedReposData = null;

  // _rerenderReposView: 语言切换时轻量重渲染项目列表页/引导页
  // 不发 API 请求、不闪烁 loading 遮罩——用缓存数据重新执行 HTML 拼接渲染
  function _rerenderReposView() {
    var _user = JSON.parse(localStorage.getItem('user') || '{}');
    var _bindings = _user.bindings || [];
    if (_bindings.length === 0) {
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
      if (Date.now() - _hiddenSince > 60000) {
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

  function nav(view, param) {
    // 离开引导页模式：清理 .ct.ob-mode 标记
    _obExitMode();
    // Issue #67: 已在 knowledge 页面且 Vue 应用存在时，不重复执行 nav（防止多次 mount 报错）
    if (view === 'knowledge' && window._currentView === 'knowledge' && typeof _knowledgeVueApp !== 'undefined' && _knowledgeVueApp) {
      return;
    }
    const repo = currentRepo();
    // 需求17: 未绑定平台守卫——依赖仓库的视图需检查 bindings
    var _repoDependentViews = ['dash', 'pipe', 'vod', 'issue', 'coding', 'qa', 'sre', 'chat', 'im-channel', 'skills', 'skill', 'knowledge', 'scene-market', 'scene-detail', 'scene-detail-local'];
    var _nonRepoViews = ['repos', 'settings', 'tasks', 'runtimes', 'observability', 'my-feedback', 'my-feedback-detail', 'autopilot', 'autopilot-detail', 'organizations', 'account'];
    if (!repo && _repoDependentViews.indexOf(view) >= 0) {
      // 检查是否未绑定
      var _u = JSON.parse(localStorage.getItem('user') || '{}');
      var _bindings = _u.bindings || [];
      if (_bindings.length === 0) {
        // 未绑定 → 提示用户并跳转到新手引导页
        var _isZh = I18N.getLang() === 'zh';
        showToast(I18N.t('app.login.link_platform_first'));
        goToRepos();
        return;
      }
      // 已绑定但无选中仓库 → loadRepos
      loadRepos();
      return;
    }
    if (!repo && _nonRepoViews.indexOf(view) < 0) { loadRepos(); return; }

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
    // 离开 repos 全屏页时恢复 sidebar/tb/mainView 默认布局
    // account/organizations 分支自行隐藏 sidebar，此处统一恢复 tb/mainView
    if (view !== 'repos') {
      var sb = $('sidebar'); if (sb) sb.style.display = 'flex';
      var tb = $('tb'); if (tb) tb.style.display = 'flex';
      var mv = $('mainView'); if (mv) { mv.style.position = ''; mv.style.inset = ''; mv.style.zIndex = ''; }
    }
    // 离开需求中心时停止场景识别轮询
    if (view !== 'tasks' && typeof _stopScenePoll === 'function') _stopScenePoll();
    // 离开 observability 时停止定时轮询
    if (view !== 'observability' && typeof stopObsAuto === 'function') stopObsAuto();
    // 离开 chat 时停止飞书接入轮询
    if (view !== 'chat' && typeof _stopOnboardPoll === 'function') _stopOnboardPoll();
    // 离开场景中心时清除编辑标志（防止 _dashboardEditing 泄漏导致重新进入时卡在加载中）
    window._dashboardEditing = false;

    if (repo) {
      // F5 refresh: ensure sidebar repo dropdown is populated even when
      // currentRepo() is restored from sessionStorage (skips loadRepos path)
      if (!$('repoSelect') || $('repoSelect').options.length <= 1) initSidebarRepos();
      var _rk = repo.repoKey || repo.repo_key || '';
      var _fn = repo.fullName || (repo.owner + '/' + repo.name);
      var _isGithub = _rk.indexOf('github:') === 0;
      var _repoLink = _isGithub ? 'https://github.com/' + _fn : 'https://gitcode.com/' + _fn;
      var _logo = _isGithub
        ? '<svg width="14" height="14" viewBox="0 0 1024 1024" fill="currentColor" style="vertical-align:-2px;margin-right:5px;color:var(--t2)"><path d="M512 42.666667A464.64 464.64 0 0 0 42.666667 502.186667 460.373333 460.373333 0 0 0 363.52 938.666667c23.466667 4.266667 32-9.813333 32-22.186667v-78.08c-130.56 27.733333-158.293333-61.44-158.293333-61.44a122.026667 122.026667 0 0 0-52.053334-67.413333c-42.666667-28.16 3.413333-27.733333 3.413334-27.733334a98.56 98.56 0 0 1 71.68 47.36 101.12 101.12 0 0 0 136.533333 37.973334 99.413333 99.413333 0 0 1 29.866667-61.44c-104.106667-11.52-213.333333-50.773333-213.333334-226.986667a177.066667 177.066667 0 0 1 47.36-124.16 161.28 161.28 0 0 1 4.693334-121.173333s39.68-12.373333 128 46.933333a455.68 455.68 0 0 1 234.666666 0c89.6-59.306667 128-46.933333 128-46.933333a161.28 161.28 0 0 1 4.693334 121.173333A177.066667 177.066667 0 0 1 810.666667 477.866667c0 176.64-110.08 215.466667-213.333334 226.986666a106.666667 106.666667 0 0 1 32 85.333334v125.866666c0 14.933333 8.533333 26.88 32 22.186667A460.8 460.8 0 0 0 981.333333 502.186667 464.64 464.64 0 0 0 512 42.666667"/></svg>'
        : '<svg width="14" height="14" viewBox="0 0 1024 1024" fill="currentColor" style="vertical-align:-2px;margin-right:5px;color:var(--t2)"><path d="M472.746667 896.853333c-72.533333-8.106667-151.466667-39.253333-200.106667-78.08-27.306667-21.76-72.533333-71.253333-89.6-97.706666-51.626667-80.213333-72.533333-179.626667-57.173333-269.653334 15.36-86.613333 50.773333-154.453333 113.92-216.32 109.653333-107.52 285.44-141.653333 425.813333-82.346666 46.08 19.626667 90.453333 46.933333 108.8 66.986666 35.84 39.68 22.613333 104.106667-25.173333 128-20.48 9.813333-58.453333 10.24-72.96 0.853334-14.933333-9.386667-28.16-35.84-32-62.293334-1.706667-12.8-3.84-23.04-4.266667-23.04-0.853333 0-11.946667 5.973333-23.893333 13.226667-40.106667 23.466667-51.626667 26.88-113.066667 30.72-32.426667 1.706667-67.84 4.693333-78.933333 6.4-20.053333 2.56-44.373333-0.853333-83.2-13.226667l-20.48-6.4 0.853333 42.24c0.426667 40.96 0 43.093333-17.493333 79.36-22.613333 46.933333-30.72 75.52-34.56 117.333334-8.106667 100.693333 46.506667 175.36 150.613333 205.226666 67.413333 19.2 179.2 15.36 239.36-8.533333 58.026667-23.04 108.8-76.8 108.8-114.773333 0-15.786667-20.053333-35.84-41.813333-40.96-8.96-2.56-43.946667-5.546667-77.226667-6.826667-69.973333-2.986667-119.04-9.386667-132.266667-18.346667-13.653333-8.533333-20.48-33.28-14.506666-51.2 5.973333-19.2 15.786667-27.306667 43.946666-37.973333 19.2-7.253333 33.28-8.533333 88.746667-8.533333 118.613333 0 174.08 14.933333 217.6 58.453333 49.92 50.346667 55.466667 127.146667 13.226667 194.986667-62.293333 99.84-135.68 156.586667-235.52 182.613333-33.706667 8.96-122.453333 14.933333-158.72 10.666667z"/></svg>';
      repoTitle.innerHTML = `${_logo}<a href="${_repoLink}" target="_blank" rel="noopener" style="color:var(--a);font-weight:500;text-decoration:none" title="打开 ${_fn}">${repo.fullName} ↗</a>`;
    } else {
      repoTitle.textContent = '';
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
      const loadDash = () => Promise.all([
        API.get('/dashboard/' + repo.owner + '/' + repo.name),
        API.get('/scenes?repo=' + encodeURIComponent(repo.owner + '/' + repo.name)),
      ]).then(([dashData, scenesData]) => {
        if (window._currentView !== 'dash') return;  // stale guard
        // 编辑中不覆盖 UI，避免丢失用户正在进行的修改
        if (window._dashboardEditing) return;
        // R14.17: dashboard 成功加载时清除 403 去重列表，权限恢复后可切回之前 403 的仓库
        window._tried403Repos = null;
        dashData._scenes = scenesData.scenes || [];
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
      loadDash();
      _refreshTimer = setInterval(loadDash, 30000);
    } else if (view === 'pipe') {
      title.textContent = I18N.t('app.current_project');
      if (!repo) { pc.innerHTML = _noRepoPlaceholder(); return; }
      API.get('/pipeline/' + repo.owner + '/' + repo.name).then(d => { if (window._currentView !== 'pipe') return; renderPipeline(d, param); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== 'pipe') return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div>'; });
    } else if (['vod','issue','coding','qa','sre'].includes(view)) {
      title.textContent = I18N.t('app.current_project');
      if (!repo) { pc.innerHTML = _noRepoPlaceholder(); return; }
      var _agentQs = param ? '?issue_number=' + encodeURIComponent(param) : '';
      API.get('/agents/' + view + '/' + repo.owner + '/' + repo.name + _agentQs).then(d => { if (window._currentView !== view) return; renderAgent(view, d); }).catch(e => { if (e.name === 'AbortError' && e.silent) return; if (window._currentView !== view) return; pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + errMsg(e) + '</div>'; });
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
      renderChat(param || null);
      // 切换回 Chat 页面时恢复活跃查询（不中断）
      if (typeof resumeChatTask === 'function') setTimeout(resumeChatTask, 500);
    } else if (view === 'im-channel') {
      title.textContent = I18N.t('app.nav.im_channel');
      renderImChannel();
    } else if (view === 'tasks') {
      title.textContent = I18N.t('app.current_project');
      // Issue #341: #tasks/<issue号> 深链——把 hash 中的编号透传给需求中心，
      // 纯数字 param 由 renderTasks 解析为待打开详情的 issue_number
      renderTasks(param);
    } else if (view === 'settings') {
      title.textContent = I18N.t('app.current_project');
      renderSettings(param || 'gitcode');
    } else if (view === 'account') {
      title.textContent = I18N.t('app.account.center_title');
      // R13.2: 用户设置页不展示仓库信息——清空顶栏仓库名
      repoTitle.textContent = '';
      // 用户设置：只隐藏侧边栏，保留顶栏，mainView 保持正常布局
      var _accSb = $('sidebar'); if (_accSb) _accSb.style.display = 'none';
      renderAccount(param || 'profile');
    } else if (view === 'organizations') {
      title.textContent = I18N.t('app.nav.organizations') || '组织管理';
      repoTitle.textContent = '';
      // 组织管理页面隐藏左侧菜单，全屏展示
      var _orgSb = $('sidebar'); if (_orgSb) _orgSb.style.display = 'none';
      if (typeof renderOrganizations === 'function') {
        renderOrganizations();
      } else {
        pc.innerHTML = '<div class="error">' + (I18N.t('common.load_failed') || '加载失败') + '</div>';
      }
    } else if (view === 'my-feedback') {
      title.textContent = '我的反馈';
      if (typeof renderMyFeedback === 'function') {
        renderMyFeedback();
      } else {
        pc.innerHTML = '<div class="error">反馈页面加载失败，请刷新重试</div>';
      }
    } else if (view === 'my-feedback-detail') {
      title.textContent = '反馈详情';
      if (typeof renderMyFeedbackDetail === 'function') {
        renderMyFeedbackDetail(param);
      } else {
        pc.innerHTML = '<div class="error">详情页面加载失败，请刷新重试</div>';
      }
    } else if (view === 'skills') {
      title.textContent = I18N.t('app.current_project');
      renderSkills();
    } else if (view === 'knowledge') {
      title.textContent = I18N.t('app.current_project');
      if (typeof _knowledgeVueApp === 'undefined' || !_knowledgeVueApp) {
        renderKnowledge();
      }
    } else if (view === 'runtimes') {
      title.textContent = I18N.t('app.current_project');
      if (typeof renderRuntimes !== 'function') {
        // runtimes.js failed to load (stale cache) — force reload to fetch latest
        window.location.reload();
        return;
      }
      _rtPage = 1; _rtDevPage = 1;  // Issue #185: 切入页面重置分页
      _loadRuntimesData();
    } else if (view === 'observability') {
      title.textContent = I18N.t('app.current_project');
      // param = 分区（overview/devices/inspect），由侧栏子项写入 URL，可深链
      if (typeof renderObservability === 'function') renderObservability(param);
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
      if (typeof renderAutopilot === 'function') renderAutopilot();
    } else if (view === 'autopilot-detail') {
      title.textContent = I18N.t('app.current_project');
      if (typeof renderAutopilotDetail === 'function') renderAutopilotDetail(param);
    }
  }

  // 浏览器回退/前进：按 history 状态回放视图（回放时不重复入栈）
  window.addEventListener('popstate', (e) => {
    const s = e.state;
    _replaying = true;
    try {
      if (s && s.view === 'repos') goToRepos();          // 全屏仓库选择页
      else if (s && s.view) nav(s.view, s.param);
      else {
        // No state — try hash-based navigation
        const hash = window.location.hash;
        if (hash && hash.length > 1) {
          const parts = hash.substring(1).split('/');
          const view = parts[0];
          const noRepoViews = ['repos', 'settings', 'tasks', 'runtimes', 'event', 'scene-market', 'scene-detail', 'scene-detail-local', 'my-feedback', 'my-feedback-detail', 'autopilot', 'autopilot-detail', 'account'];
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

  function search(q) {
    if (!q.trim()) return;
    const pc = $('pageContent');
    API.get('/search/?q=' + encodeURIComponent(q) + '&type=all').then(d => {
      let html = '<div class="cd"><div class="cd-h"><h2>' + I18N.t('app.search.title') + ': "' + q + '"</h2></div>';
      html += '<div class="ts t2">' + I18N.t('app.search.events') + ': ' + (d.events || []).length + ' / ' + I18N.t('app.search.repos') + ': ' + (d.repos || []).length + ' / ' + I18N.t('app.search.agents') + ': ' + (d.agents || []).length + '</div>';
      (d.events || []).forEach(ev => { html += '<div class="ai" onclick="app.nav(\'event\',' + ev.seq + ')" style="cursor:pointer"><div class="ac"><b>' + (ev.title || '') + '</b><span class="ts t3"> ' + (ev.repo_key || '') + '</span></div></div>'; });
      (d.agents || []).forEach(a => { html += '<div class="cl" onclick="app.nav(\'' + a.name + '\')"><div class="cx"><h4>' + a.display_name + '</h4></div></div>'; });
      html += '</div>';
      pc.innerHTML = html;
    }).catch(() => {});
  }

  function oauthLogin(provider) {
    // 登录场景：全页重定向（业界标准，兼容性最强）
    // 弹窗模式仅用于已登录态的绑定场景（见 account.js _accountDoOauthBind）
    API.get('/auth/login/' + provider + '?lang=' + I18N.getLang()).then(function(d) {
      if (d.redirect_url) window.location.href = d.redirect_url;
    }).catch(function() {
      // R7.5: 未配 OAuth App 时提示用户用用户名+密码注册后绑定 token
      showToast(I18N.t('app.login.oauth_not_configured_hint') || I18N.t('app.login.oauth_not_configured') || 'OAuth未配置');
    });
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
      // R7.5: 所有平台始终显示（不过滤 configured），未配 OAuth App 时点击弹提示
      if (providers.length === 0) {
        if (section) section.style.display = '';  // 确保可见
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
          var _brandOverride = {gitcode: '#F54A36', github: '#1F2328'};
          var _brandColor = _brandOverride[p.provider] || p.brand_color || '';
          var brandVar = _brandColor ? ' style="--brand:' + _brandColor + '"' : '';
          return '<button class="oauth-btn" onclick="app.oauthLogin(\'' + p.provider + '\')"' + brandVar + '>'
            + icon + '<span data-i18n="' + i18nKey + '">' + p.name + '</span></button>';
        }).join('');
        if (typeof I18N !== 'undefined' && I18N.apply) I18N.apply();
      }
    }).catch(function() { /* 预渲染按钮保留，不隐藏 */ });
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
              <option value="gitcode">GitCode</option>
              <option value="github">GitHub</option>
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
    API.post('/auth/set-password', {code: code, new_password: pwd}).then(function() {
      closeConfirm();
      showToast(I18N.t('app.login.password_set'));
      renderSettings('account');
    }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
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
      + '<button class="bt bt-p" onclick="app.doChangeEmail()">' + I18N.t('app.login.confirm_change_email') + '</button>'
      + '</div>'
      + '</div>';
    showConfirm(html, null, {hideActions: true});
  }

  function sendChangeEmailCode() {
    var email = $('ceEmail').value.trim();
    if (!email) return showToast(I18N.t('app.login.enter_email'));
    API.post('/auth/change-email/send-code', {new_email: email}).then(function() {
      showToast(I18N.t('app.login.code_sent'));
      var step1 = document.getElementById('ceStep1');
      var step2 = document.getElementById('ceStep2');
      if (step1) step1.style.display = 'none';
      if (step2) step2.style.display = 'flex';
    }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
  }

  function doChangeEmail() {
    var email = $('ceEmail').value.trim();
    var code = $('ceCode').value.trim();
    if (!email || !code) return showToast(I18N.t('app.login.fill_all_fields'));
    API.post('/auth/change-email', {new_email: email, code: code}).then(function() {
      closeConfirm();
      showToast(I18N.t('app.login.email_changed'));
      renderSettings('account');
    }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
  }

  function bindProvider(provider) {
    // 复用设置页的绑定模态框——OAuth + 手动令牌 + spinner + 转移确认 + 成功反馈
    if (typeof _accountBindProvider === 'function') {
      _accountBindProvider(provider);
    } else {
      // 兜底：account.js 未加载时直接 OAuth 跳转
      API.get('/auth/bind/' + provider).then(function(d) {
        if (d.redirect_url) window.location.href = d.redirect_url;
      }).catch(function(e) { showToast(errMsg(e) || I18N.t('app.account.error_failed')); });
    }
  }

  function deleteAccount() {
    var isZh = I18N.getLang()==='zh';
    var html = '<div style="display:flex;flex-direction:column;gap:8px">'
      + '<p style="color:var(--r);font-size:12px">' + I18N.t('app.login.delete_account_warning') + '</p>'
      + '<div id="daStep1" style="display:flex;flex-direction:column;gap:8px">'
      + '<p style="font-size:12px;color:var(--t2)">' + I18N.t('app.login.choose_verify_method') + '</p>'
      + '<button class="bt bt-p" onclick="app._daChoosePwd()">' + I18N.t('app.login.verify_with_password') + '</button>'
      + '<button class="bt bt-s" onclick="app._daChooseCode()">' + I18N.t('app.login.verify_with_code') + '</button>'
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

  function _showWhitelistGuidance(platform, username) {
    var overlay = document.createElement('div');
    overlay.id = 'whitelistModal';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,0.35);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center';
    var platName = platform === 'github' ? 'GitHub' : 'GitCode';
    overlay.innerHTML =
      '<div style="background:var(--c,#fff);border-radius:20px;padding:40px;text-align:center;width:400px;max-width:90vw;box-shadow:0 16px 64px rgba(0,0,0,0.25)">'
        + '<div style="font-size:48px;margin-bottom:16px">🔒</div>'
        + '<h2 style="font-size:18px;font-weight:700;color:var(--t,#1d1d1f);margin-bottom:8px">' + I18N.t('app.login.whitelist_title') + '</h2>'
        + '<p style="font-size:14px;color:var(--t2,#86868b);margin-bottom:6px">' + I18N.t('app.login.whitelist_desc', {platform: platName, username: username ? ' <b>' + _esc(username) + '</b> ' : ' '}) + '</p>'
        + '<p style="font-size:14px;color:var(--t2,#86868b);margin-bottom:24px">' + I18N.t('app.login.whitelist_apply_hint') + '</p>'
        + '<div style="display:flex;gap:8px;justify-content:center">'
          + '<button onclick="window.location.href=\'/static/beta-apply.html?platform=' + platform + '&username=' + encodeURIComponent(username) + '\'" style="padding:12px 32px;border:none;border-radius:12px;background:linear-gradient(135deg,#3168e0,#4f46e5);color:#fff;font-size:14px;font-weight:600;cursor:pointer">' + I18N.t('app.login.whitelist_apply_btn') + '</button>'
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

  function showToast(msg, desc, duration) {
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
      t.innerHTML = '<div style="font-weight:600">' + _esc(msg) + '</div>'
        + '<div style="font-size:11px;opacity:.8;margin-top:2px">' + _esc(desc) + '</div>';
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
  function closeHelp() {
    const overlay = document.getElementById('helpOverlay');
    const drawer = document.getElementById('helpDrawer');
    if (overlay) overlay.classList.remove('show');
    if (drawer) drawer.classList.remove('show');
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
    const oauthError = p.get('oauth_error');
    if (oauthError) {
      const decoded = decodeURIComponent(oauthError);
      if (decoded.startsWith('WHITELIST:')) {
        // 白名单不通过——显示引导弹窗
        var parts = decoded.split(':');
        var platform = parts[1] || 'gitcode';
        var username = parts[2] || '';
        _showWhitelistGuidance(platform, username);
      } else {
        // P2-i18n: 错误码走 I18N 翻译，不直接显示后端消息
        var errorKey = 'app.login.oauth_error_' + decoded;
        var translated = I18N.t(errorKey);
        showToast(translated && translated !== errorKey ? translated : decoded, 6000);
      }
      if (window.history.replaceState) {
        window.history.replaceState(null, '', window.location.pathname);
      }
    }

    // 绑定/解绑回调检测（全局，不依赖 nav('account')）
    _checkBindSuccess();

    // P0-3: Check for exchange_code in URL (new secure flow)
    const exchangeCode = p.get('exchange_code');
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
  // Theme: 'default' | 'apple'  Mode: 'light' | 'auto' (dark adapt)
  function getTheme() { return localStorage.getItem('grapeTheme') || 'apple'; }
  function getThemeMode() { return localStorage.getItem('grapeThemeMode') || 'light'; }

  function setTheme(theme) {
    localStorage.setItem('grapeTheme', theme);
    const html = document.documentElement;
    if (theme === 'apple') {
      html.setAttribute('data-theme', 'apple');
      // re-apply mode so dark-auto picks up
      setThemeMode(getThemeMode());
    } else {
      html.removeAttribute('data-theme');
      html.removeAttribute('data-theme-mode');
    }
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

  return {nav, search, oauthLogin, renderOAuthButtons, openBetaApply, submitBetaApply, baSelectIdentity, baToggleOther, logout, showToast, errMsg, openHelp, closeHelp, switchRepo, switchOrg, goToRepos, currentRepo, showLogin, init, loadRepos, _forceSyncRepos, _renderBindingGuide, _rerenderReposView, initSidebarRepos, loadProfile, getTheme, getThemeMode, setTheme, setThemeMode, toggleGroup, toggleSidebar, showConfirm, closeConfirm, showModal, closeModal, showPrompt, _toggleOrgDropdown, _updateTbOrgName, _toggleLobbyOrgDropdown, _updateLobbyOrgName, _toggleLobbyUserMenu, _loadLobbyUserMenu, _toggleUserMenu, _toggleTbUserMenu, _loadTbUserMenu, _accountGoBack, pwdLogin, _toggleLoginPwd, _clearLoginError, _blurValidateLogin, _submitValidateLogin, saveProfile, showChangePassword, doChangePassword, showSetPassword, sendSetPasswordCode, doSetPassword, showChangeEmail, sendChangeEmailCode, doChangeEmail, bindProvider, deleteAccount, _daChoosePwd, _daChooseCode, sendDeleteAccountCode, doDeleteAccount, _checkOAuthCompletion, _dismissOAuthCompletion};
})();

/** 根据 repo_key 生成 VCS 平台 URL（仓库/Issue 等通用）。 */
function _vcsUrl(repoKey, path) {
  var rk = repoKey || '';
  var host = rk.indexOf('github:') === 0 ? 'https://github.com' : 'https://gitcode.com';
  var parts = rk.split(':');
  var repoPath = parts.length > 1 ? parts[1] : rk;
  return host + '/' + repoPath + (path || '');
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
