// Issue #150: Autopilot 自动化管理页面
// 参照 multica AutopilotsPage 风格设计
var $ = function(id) { return document.getElementById(id); };

// ===== 状态 =====
var _apScope = 'all';           // all / enabled / disabled
var _apSortField = 'name';      // name / lastRun / nextRun / created
var _apSortDir = 'asc';         // asc / desc
var _apSearch = '';             // 搜索关键词
var _apSelected = new Set();    // 选中的需求ID
var _apEditingId = null;        // 编辑模式时的需求ID
var _apCronMode = 'at';      // daily / weekly / custom
var _apDetailId = null;         // 详情页需求ID

// ===== 模板 =====
var _AP_TEMPLATES = [
  {id:'issue_patrol', icon:'📋', title:'Issue看板巡检', desc:'每日检查stale issue，提醒更新或关闭', cron:'0 9 * * *', instruction:'1. 列出所有打开的 Issue\n2. 找出超过 7 天无更新的 stale issue\n3. 检查是否有阻塞的 Issue 及其阻塞原因\n4. 汇总巡检报告，包含：stale 数量、阻塞数量、建议关闭列表'},
  {id:'pr_review', icon:'🔀', title:'PR审查提醒', desc:'标记需要 Review 的过期 PR', cron:'0 10 * * 1-5', instruction:'1. 列出所有打开的 PR\n2. 找出超过 24 小时未审查的 PR\n3. 为每个标记作者和摘要\n4. 提醒团队审查'},
  {id:'bug_triage', icon:'🐛', title:'Bug 分拣', desc:'评估并优先排序新 Bug 报告', cron:'0 9 * * 1-5', instruction:'1. 列出所有未优先排序的 backlog Issue\n2. 评估每个的严重程度\n3. 设置优先级标签\n4. 添加评估说明'},
  {id:'weekly_report', icon:'📊', title:'周进度报告', desc:'编写团队周报', cron:'0 17 * * 1', instruction:'1. 收集过去 7 天完成的 Issue\n2. 收集进行中的 Issue\n3. 识别阻塞的 Issue\n4. 编写结构化周报'},
  {id:'dep_audit', icon:'🛡', title:'依赖审计', desc:'依赖安全漏洞审计', cron:'0 8 * * 1', instruction:'1. 运行依赖审计工具\n2. 识别有安全漏洞的包\n3. 列出过期的依赖\n4. 输出修复建议'},
  {id:'doc_check', icon:'🔍', title:'文档检查', desc:'检查文档覆盖完整性', cron:'0 14 * * 1', instruction:'1. 列出过去 7 天的代码变更\n2. 检查相关文档是否更新\n3. 识别缺失文档的新功能\n4. 输出文档补全清单'}
];

// ===== 入口 =====
function renderAutopilot() {
  var pc = $('pageContent');
  if (!pc) return;
  pc.innerHTML = _apListHTML();
  _apSelected.clear();
  _apLoadTasks();
}

// ===== 列表页 HTML =====
function _apListHTML() {
  return '<div class="cd">' +
    '<div class="cd-h" style="display:flex;justify-content:space-between;align-items:center">' +
      '<div style="display:flex;align-items:center;gap:12px">' +
        '<button class="bt bt-s bt-o" onclick="app.nav(\'dash\')" style="font-size:12px">← 返回</button>' +
        '<h2><span style="margin-right:6px">⏰</span>自动化</h2>' +
      '</div>' +
      '<button class="bt bt-p" onclick="_apShowCreate()">+ 新建需求</button>' +
    '</div>' +
    // Toolbar
    '<div style="display:flex;align-items:center;gap:8px;margin-top:12px;flex-wrap:wrap">' +
      '<div style="display:flex;gap:0">' +
        '<button class="bt bt-s ' + (_apScope==='all'?'bt-tg':'bt-o') + '" onclick="_apSetScope(\'all\')">全部 <span id="apCountAll" class="c"></span></button>' +
        '<button class="bt bt-s ' + (_apScope==='enabled'?'bt-tg':'bt-o') + '" onclick="_apSetScope(\'enabled\')">启用 <span id="apCountEnabled" class="c"></span></button>' +
        '<button class="bt bt-s ' + (_apScope==='disabled'?'bt-tg':'bt-o') + '" onclick="_apSetScope(\'disabled\')">暂停 <span id="apCountDisabled" class="c"></span></button>' +
      '</div>' +
      '<input type="text" placeholder="搜索需求名称..." value="' + _apSearch + '" oninput="_apSearch=this.value;_apRenderTable()" style="flex:1;min-width:120px;padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c);color:var(--t);outline:none">' +
      '<select onchange="_apSortField=this.value;_apRenderTable()" style="padding:6px 10px;border:1px solid var(--b);border-radius:6px;font-size:12px;background:var(--c);color:var(--t)">' +
        '<option value="name"' + (_apSortField==='name'?' selected':'')+ '>按名称</option>' +
        '<option value="lastRun"' + (_apSortField==='lastRun'?' selected':'')+ '>按上次运行</option>' +
        '<option value="nextRun"' + (_apSortField==='nextRun'?' selected':'')+ '>按下次运行</option>' +
        '<option value="created"' + (_apSortField==='created'?' selected':'')+ '>按创建时间</option>' +
      '</select>' +
      '<button class="bt bt-s bt-o" onclick="_apToggleSortDir()">' + (_apSortDir==='asc'?'↑':'↓') + '</button>' +
      '<span class="ts t3" id="apResultCount"></span>' +
    '</div>' +
    // Table container
    '<div id="apTableContainer" style="margin-top:12px"><div class="loading">加载中...</div></div>' +
    // Batch toolbar
    '<div id="apBatchBar" style="display:none;margin-top:8px;padding:8px 12px;background:var(--bg);border-radius:8px;display:flex;align-items:center;gap:8px">' +
      '<span class="ts" id="apBatchCount"></span>' +
      '<button class="bt bt-s bt-o" onclick="_apBatchToggle(1)">启用全部</button>' +
      '<button class="bt bt-s bt-o" onclick="_apBatchToggle(0)">暂停全部</button>' +
      '<button class="bt bt-s bt-r" onclick="_apBatchDelete()">删除全部</button>' +
      '<button class="bt bt-s bt-o" onclick="_apClearSelection()">清除</button>' +
    '</div>' +
  '</div>';
}

// ===== 空状态 =====
function _apEmptyHTML() {
  var cards = _AP_TEMPLATES.map(function(t) {
    return '<button onclick="_apShowCreate(\'' + t.id + '\')" style="display:flex;align-items:flex-start;gap:10px;padding:12px;border:1px solid var(--b);border-radius:10px;background:var(--c);cursor:pointer;text-align:left;transition:all .15s" onmouseover="this.style.borderColor=\'var(--a)\'" onmouseout="this.style.borderColor=\'var(--b)\'">' +
      '<span style="font-size:20px;flex-shrink:0">' + t.icon + '</span>' +
      '<div style="min-width:0"><div style="font-size:13px;font-weight:600;color:var(--t)">' + t.title + '</div>' +
      '<div style="font-size:11px;color:var(--t2);margin-top:2px">' + t.desc + '</div></div>' +
    '</button>';
  }).join('');
  return '<div style="padding:40px 0;text-align:center">' +
    '<div style="font-size:32px;margin-bottom:8px">⏰</div>' +
    '<p style="font-size:14px;color:var(--t2);margin-bottom:4px">暂无自动化</p>' +
    '<p style="font-size:12px;color:var(--t3);margin-bottom:24px">选择一个模板快速创建，或从空白开始</p>' +
    '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;max-width:720px;margin:0 auto">' + cards + '</div>' +
    '<button class="bt bt-o" style="margin-top:16px" onclick="_apShowCreate()">+ 从空白创建</button>' +
  '</div>';
}

// ===== 加载需求 =====
function _apLoadTasks() {
  var repo = (typeof app !== 'undefined' && app.currentRepo()) ? (app.currentRepo().fullName || '') : '';
  var url = '/autopilot/tasks?repo_key=gitcode:' + repo;
  API.get(url).then(function(d) {
    if (window._currentView !== 'autopilot') return;
    _apAllTasks = d.tasks || [];
    _apRenderTable();
  }).catch(function(e) {
    if (e.name === 'AbortError' && e.silent) return;
    if (window._currentView !== 'autopilot') return;
    $('apTableContainer').innerHTML = '<div class="error">加载失败: ' + _esc(e.message) + '</div>';
  });
}

var _apAllTasks = [];

// ===== 渲染表格 =====
function _apRenderTable() {
  var container = $('apTableContainer');
  if (!container) return;
  // 筛选
  var tasks = _apAllTasks.slice();
  if (_apScope === 'enabled') tasks = tasks.filter(function(t) { return t.enabled; });
  else if (_apScope === 'disabled') tasks = tasks.filter(function(t) { return !t.enabled; });
  if (_apSearch) {
    var q = _apSearch.toLowerCase();
    tasks = tasks.filter(function(t) { return (t.name||'').toLowerCase().indexOf(q) >= 0; });
  }
  // 排序
  tasks.sort(function(a, b) {
    var dir = _apSortDir === 'asc' ? 1 : -1;
    if (_apSortField === 'name') return (a.name||'').localeCompare(b.name||'') * dir;
    if (_apSortField === 'nextRun') {
      var av = a.next_run_at ? Date.parse(a.next_run_at) : 0;
      var bv = b.next_run_at ? Date.parse(b.next_run_at) : 0;
      return (av - bv) * dir;
    }
    if (_apSortField === 'created') return (Date.parse(a.created_at) - Date.parse(b.created_at)) * dir;
    var al = a.last_run_at ? Date.parse(a.last_run_at) : 0;
    var bl = b.last_run_at ? Date.parse(b.last_run_at) : 0;
    return (al - bl) * dir;
  });
  // 计数
  var enabledCount = _apAllTasks.filter(function(t) { return t.enabled; }).length;
  var disabledCount = _apAllTasks.length - enabledCount;
  var elAll = $('apCountAll'); if (elAll) elAll.textContent = _apAllTasks.length;
  var elEn = $('apCountEnabled'); if (elEn) elEn.textContent = enabledCount;
  var elDis = $('apCountDisabled'); if (elDis) elDis.textContent = disabledCount;
  var resultEl = $('apResultCount'); if (resultEl) resultEl.textContent = tasks.length + ' / ' + _apAllTasks.length;
  // 空状态
  if (_apAllTasks.length === 0) {
    container.innerHTML = _apEmptyHTML();
    _apUpdateBatchBar();
    return;
  }
  if (tasks.length === 0) {
    container.innerHTML = '<div style="padding:24px;text-align:center;color:var(--t3)">无匹配需求</div>';
    _apUpdateBatchBar();
    return;
  }
  // 表格 — CSS Grid 布局（参照 multica ListGrid）
  var gridCols = '32px minmax(120px,1fr) 130px 110px 110px 60px 36px';
  var html = '<div style="display:grid;grid-template-columns:' + gridCols + ';gap:0;font-size:12px">' +
    // Header
    '<div style="display:contents">' +
      '<div class="ap-th" style="padding:0 8px;height:36px;display:flex;align-items:center;border-bottom:2px solid var(--b);color:var(--t2);font-size:11px;font-weight:600"></div>' +
      '<div class="ap-th" style="padding:0 8px;height:36px;display:flex;align-items:center;border-bottom:2px solid var(--b);color:var(--t2);font-size:11px;font-weight:600;cursor:pointer" onclick="_apSortField=\'name\';_apToggleSortDir()">名称 ' + (_apSortField==='name'?(_apSortDir==='asc'?'↑':'↓'):'') + '</div>' +
      '<div class="ap-th" style="padding:0 8px;height:36px;display:flex;align-items:center;border-bottom:2px solid var(--b);color:var(--t2);font-size:11px;font-weight:600">时间表</div>' +
      '<div class="ap-th" style="padding:0 8px;height:36px;display:flex;align-items:center;border-bottom:2px solid var(--b);color:var(--t2);font-size:11px;font-weight:600;cursor:pointer" onclick="_apSortField=\'lastRun\';_apToggleSortDir()">上次 ' + (_apSortField==='lastRun'?(_apSortDir==='asc'?'↑':'↓'):'') + '</div>' +
      '<div class="ap-th" style="padding:0 8px;height:36px;display:flex;align-items:center;border-bottom:2px solid var(--b);color:var(--t2);font-size:11px;font-weight:600;cursor:pointer" onclick="_apSortField=\'nextRun\';_apToggleSortDir()">下次 ' + (_apSortField==='nextRun'?(_apSortDir==='asc'?'↑':'↓'):'') + '</div>' +
      '<div class="ap-th" style="padding:0 8px;height:36px;display:flex;align-items:center;border-bottom:2px solid var(--b);color:var(--t2);font-size:11px;font-weight:600">状态</div>' +
      '<div class="ap-th" style="padding:0 4px;height:36px;display:flex;align-items:center;border-bottom:2px solid var(--b)"></div>' +
    '</div>';
  // Rows
  tasks.forEach(function(t) {
    var sel = _apSelected.has(t.id);
    var lastRun = t.last_run_at ? _apRelativeTime(t.last_run_at) : '—';
    var lastColor = t.last_run_at ? (t.last_run_status === 'success' ? '#28a745' : t.last_run_status === 'failed' ? '#dc3545' : 'var(--t3)') : 'var(--t3)';
    var nextRun = t.next_run_at ? _apFormatTime(t.next_run_at) : '—';
    var statusBadge = t.enabled ? '<span style="color:#28a745;font-size:11px">●</span>' : '<span style="color:var(--t3);font-size:11px">○</span>';
    var rowBg = sel ? ';background:rgba(49,104,224,0.06)' : '';
    html += '<div style="display:contents" class="ap-row' + (sel ? ' ap-row-sel' : '') + '" data-id="' + t.id + '">' +
      // checkbox
      '<div style="padding:0 8px;height:44px;display:flex;align-items:center;border-bottom:1px solid var(--b)' + rowBg + '">' +
        '<input type="checkbox" ' + (sel?'checked':'') + ' onclick="event.stopPropagation();_apToggleSelect(' + t.id + ')" style="cursor:pointer;accent-color:#3168e0">' +
      '</div>' +
      // name
      '<div style="padding:0 8px;height:44px;display:flex;align-items:center;border-bottom:1px solid var(--b);cursor:pointer;overflow:hidden' + rowBg + '" onclick="event.stopPropagation();_apShowDetail(' + t.id + ')">' +
        '<span style="font-weight:500;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0">' + _esc(t.name) + '</span>' +
      '</div>' +
      // cron
      '<div style="padding:0 8px;height:44px;display:flex;align-items:center;border-bottom:1px solid var(--b)' + rowBg + '">' +
        '<span style="font-family:monospace;font-size:10px;color:var(--t3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0">' + _esc(t.cron) + '</span>' +
      '</div>' +
      // last run
      '<div style="padding:0 8px;height:44px;display:flex;align-items:center;gap:4px;border-bottom:1px solid var(--b)' + rowBg + '">' +
        '<span style="width:6px;height:6px;border-radius:50%;background:' + lastColor + ';flex-shrink:0"></span>' +
        '<span style="font-size:11px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0">' + lastRun + '</span>' +
      '</div>' +
      // next run
      '<div style="padding:0 8px;height:44px;display:flex;align-items:center;border-bottom:1px solid var(--b)' + rowBg + '">' +
        '<span style="font-size:11px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0">' + nextRun + '</span>' +
      '</div>' +
      // status
      '<div style="padding:0 8px;height:44px;display:flex;align-items:center;border-bottom:1px solid var(--b)' + rowBg + '">' + statusBadge + '</div>' +
      // actions (kebab)
      '<div style="padding:0 4px;height:44px;display:flex;align-items:center;justify-content:center;border-bottom:1px solid var(--b);position:relative' + rowBg + '">' +
        '<button onclick="event.stopPropagation();_apToggleRowMenu(' + t.id + ', event)" style="border:none;background:transparent;cursor:pointer;padding:4px;border-radius:4px;color:var(--t3);font-size:16px;line-height:1">⋯</button>' +
      '</div>' +
    '</div>';
  });
  html += '</div>';
  // Row hover style
  html += '<style>.ap-row:hover > div{background:rgba(0,0,0,0.03)!important}.ap-row-sel > div{background:rgba(49,104,224,0.06)!important}</style>';
  container.innerHTML = html;
  _apUpdateBatchBar();
}

// ===== Scope/Sort =====
function _apSetScope(s) { _apScope = s; renderAutopilot(); }
function _apToggleSortDir() { _apSortDir = _apSortDir === 'asc' ? 'desc' : 'asc'; _apRenderTable(); }

// ===== 选中 =====
function _apToggleSelect(id) {
  if (_apSelected.has(id)) _apSelected.delete(id); else _apSelected.add(id);
  _apRenderTable();
}
function _apRowClick(id, e) {
  if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'BUTTON') {
    _apShowDetail(id);
  }
}

// Kebab dropdown menu
var _apOpenMenuId = null;
function _apToggleRowMenu(id, e) {
  // Close existing menu
  var existing = document.getElementById('apRowMenu');
  if (existing) { existing.remove(); _apOpenMenuId = null; }
  if (_apOpenMenuId === id) { _apOpenMenuId = null; return; }

  var btn = e.currentTarget;
  var rect = btn.getBoundingClientRect();
  var menu = document.createElement('div');
  menu.id = 'apRowMenu';
  menu.style.cssText = 'position:fixed;z-index:10001;top:' + (rect.bottom + 2) + 'px;left:' + (rect.right - 140) + 'px;width:140px;background:var(--c,#fff);border:1px solid var(--b);border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,0.12);padding:4px';
  menu.innerHTML =
    '<div onclick="event.stopPropagation();_apTrigger(' + id + ');_apCloseRowMenu()" style="padding:8px 10px;font-size:12px;cursor:pointer;border-radius:4px;color:var(--t);white-space:nowrap" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">▶ 立即触发</div>' +
    '<div onclick="event.stopPropagation();_apToggleTask(' + id + ');_apCloseRowMenu()" style="padding:8px 10px;font-size:12px;cursor:pointer;border-radius:4px;color:var(--t);white-space:nowrap" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">' + (_apTaskEnabled(id) ? '⏸ 暂停' : '▶ 启用') + '</div>' +
    '<div onclick="event.stopPropagation();_apEdit(' + id + ');_apCloseRowMenu()" style="padding:8px 10px;font-size:12px;cursor:pointer;border-radius:4px;color:var(--t);white-space:nowrap" onmouseover="this.style.background=\'var(--bg)\'" onmouseout="this.style.background=\'transparent\'">✎ 编辑</div>' +
    '<div onclick="event.stopPropagation();_apDelete(' + id + ');_apCloseRowMenu()" style="padding:8px 10px;font-size:12px;cursor:pointer;border-radius:4px;color:#dc3545;white-space:nowrap" onmouseover="this.style.background=\'rgba(220,53,69,0.08)\'" onmouseout="this.style.background=\'transparent\'">🗑 删除</div>';
  document.body.appendChild(menu);
  _apOpenMenuId = id;

  // Close on outside click
  setTimeout(function() {
    document.addEventListener('click', _apCloseRowMenu, { once: true });
  }, 0);
}

function _apCloseRowMenu() {
  var menu = document.getElementById('apRowMenu');
  if (menu) menu.remove();
  _apOpenMenuId = null;
}
function _apClearSelection() { _apSelected.clear(); _apRenderTable(); }
function _apUpdateBatchBar() {
  var bar = $('apBatchBar');
  if (!bar) return;
  if (_apSelected.size > 0) {
    bar.style.display = 'flex';
    var c = $('apBatchCount');
    if (c) c.textContent = _apSelected.size + ' selected';
  } else {
    bar.style.display = 'none';
  }
}

// ===== 批量操作 =====
function _apBatchToggle(enabled) {
  _apSelected.forEach(function(id) {
    API.put('/autopilot/tasks/' + id, {enabled: !!enabled}).catch(function(){});
  });
  setTimeout(_apLoadTasks, 300);
}

function _apTaskEnabled(id) {
  var t = _apAllTasks.find(function(x) { return x.id === id; });
  return t ? t.enabled : false;
}

function _apToggleTask(id) {
  var enabled = !_apTaskEnabled(id);
  API.put('/autopilot/tasks/' + id, {enabled: enabled}).then(function() {
    _apLoadTasks();
  }).catch(function(e) {
    if (app && app.showToast) app.showToast('操作失败: ' + (e.message||e));
  });
}
function _apBatchDelete() {
  if (!confirm('确定删除选中的 ' + _apSelected.size + ' 个需求吗？')) return;
  var ids = Array.from(_apSelected);
  ids.forEach(function(id) {
    API.del('/autopilot/tasks/' + id).catch(function(){});
  });
  _apSelected.clear();
  setTimeout(_apLoadTasks, 300);
}

// ===== 单个操作 =====
function _apTrigger(id) {
  API.post('/autopilot/tasks/' + id + '/trigger').then(function(d) {
    if (app && app.showToast) app.showToast(d.message || '已触发');
    _apLoadTasks();
  }).catch(function(e) {
    if (app && app.showToast) app.showToast('触发失败: ' + (e.message||e));
  });
}
function _apDelete(id) {
  if (!confirm('确定删除此需求吗？')) return;
  API.del('/autopilot/tasks/' + id).then(function() {
    if (app && app.showToast) app.showToast('已删除');
    _apLoadTasks();
  }).catch(function(e) {
    if (app && app.showToast) app.showToast('删除失败: ' + (e.message||e));
  });
}

// ===== 创建/编辑对话框 =====
function _apShowCreate(templateId) {
  _apEditingId = null;
  var tpl = templateId ? _AP_TEMPLATES.find(function(t) { return t.id === templateId; }) : null;
  _apShowDialog(tpl);
}

function _apEdit(id) {
  _apEditingId = id;
  API.get('/autopilot/tasks/' + id).then(function(t) {
    _apShowDialog(null, t);
  }).catch(function(e) {
    if (app && app.showToast) app.showToast('加载失败: ' + (e.message||e));
  });
}

function _apShowDialog(tpl, existing) {
  var name = existing ? existing.name : (tpl ? tpl.title : '');
  var skillName = existing ? (existing.skill_name || '') : '';
  var instruction = existing ? (existing.instruction || '') : (tpl ? tpl.instruction : '');
  var cron = existing ? existing.cron : (tpl ? tpl.cron : '0 9 * * *');
  var maxRetry = existing ? (existing.max_retry || 3) : 3;
  var title = existing ? '编辑自动化' : '新建自动化';
  var btnText = existing ? '保存' : '创建';

  // Shared input style
  var _inp = 'width:100%;padding:8px 12px;border:1px solid var(--b);border-radius:8px;font-size:13px;background:var(--c);color:var(--t);outline:none;min-width:0';
  var _lbl2 = 'display:block;font-size:12px;font-weight:600;color:var(--t2);margin-bottom:4px';
  var _hint = 'font-size:11px;color:var(--t3);margin-top:3px';
  var _sectionLbl = 'font-size:13px;font-weight:600;color:var(--t2);margin-bottom:8px;display:block';

  var overlay = document.createElement('div');
  overlay.id = 'apDialog';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center';
  overlay.innerHTML =
    '<div id="apDialogContent" style="background:var(--c,#fff);border-radius:12px;width:900px;max-width:calc(100vw - 32px);height:min(680px,calc(100vh - 64px));box-shadow:0 12px 48px rgba(0,0,0,0.15);display:flex;flex-direction:column;overflow:hidden;transition:all .3s ease">' +
      // Header
      '<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 20px;border-bottom:1px solid var(--b);flex-shrink:0">' +
        '<div style="display:flex;align-items:center;gap:8px">' +
          '<span style="display:inline-flex;width:20px;height:20px;align-items:center;justify-content:center;border-radius:4px;background:rgba(49,104,224,0.15);color:#3168e0;font-size:11px">⚡</span>' +
          '<span style="font-size:13px;font-weight:600">' + title + '</span>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:2px">' +
          '<button type="button" onclick="_apToggleExpand()" id="apExpandBtn" style="padding:4px 8px;border:none;background:transparent;cursor:pointer;color:var(--t3);border-radius:4px" title="放大">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>' +
          '</button>' +
          '<span onclick="document.getElementById(\'apDialog\').remove()" style="cursor:pointer;font-size:18px;color:var(--t3);padding:4px 8px;border-radius:4px;line-height:1">×</span>' +
        '</div>' +
      '</div>' +
      // Body: two columns (left=content, right=config)
      '<div style="flex:1;min-height:0;display:flex;flex-direction:row;overflow:hidden">' +
        // Left column: name + skill + instruction
        '<div style="flex:1;min-width:0;display:flex;flex-direction:column;border-right:1px solid var(--b);overflow-y:auto">' +
          '<div style="padding:16px 20px 8px">' +
            '<label style="' + _lbl2 + '">任务名称 <span style="color:var(--r)">*</span></label>' +
            '<input id="apFormName" type="text" value="' + _esc(name) + '" placeholder="输入任务名称" style="' + _inp + ';font-size:14px;font-weight:500">' +
            '<div style="' + _hint + '">定时任务的名称，用于列表展示</div>' +
          '</div>' +
          '<div style="padding:8px 20px">' +
            '<label style="' + _lbl2 + '">Skill（可选）</label>' +
            '<input id="apFormSkill" type="text" value="' + _esc(skillName) + '" placeholder="留空=纯指令模式" style="' + _inp + '">' +
            '<div style="' + _hint + '">填写 skill 名可加载 SKILL.md（如 issue-advance），留空则按巡检指令直接执行</div>' +
          '</div>' +
          '<div style="padding:8px 20px 16px;flex:1;display:flex;flex-direction:column;min-height:0">' +
            '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">' +
              '<label style="' + _lbl2 + ';margin-bottom:0">巡检指令 <span style="color:var(--r)">*</span></label>' +
              '<button type="button" class="bt bt-s bt-o" onclick="_apPolishInstruction(this)" id="apPolishBtn" style="font-size:11px;padding:4px 10px">✨ 润色</button>' +
            '</div>' +
            '<textarea id="apFormInstruction" rows="6" placeholder="描述巡检的具体步骤和要求" style="' + _inp + ';resize:none;flex:1;min-height:120px;font-family:inherit">' + _esc(instruction) + '</textarea>' +
            '<div style="' + _hint + '">写个大概，点润色可让 AI 优化指令内容</div>' +
          '</div>' +
        '</div>' +
        // Right column: schedule + retry
        '<aside style="flex:1;min-width:0;overflow-y:auto;padding:16px 20px;background:var(--bg,rgba(0,0,0,0.02))">' +
          '<div style="margin-bottom:16px">' +
            '<div style="' + _sectionLbl + '">时间表 <span style="color:var(--r)">*</span></div>' +
            '<div id="apScheduleEditor" style="border:1px solid var(--b);border-radius:8px;padding:12px;background:var(--c,#fff)"></div>' +
            '<div id="apCronPreview" style="font-size:12px;color:var(--t2);margin-top:6px;padding:8px 10px;background:var(--c,#fff);border-radius:6px"></div>' +
          '</div>' +
          '<div>' +
            '<div style="' + _sectionLbl + '">执行设置</div>' +
            '<div style="display:flex;align-items:center;gap:8px">' +
              '<label style="font-size:13px;color:var(--t3);white-space:nowrap">最大重试</label>' +
              '<input id="apFormRetry" type="number" min="0" max="10" value="' + maxRetry + '" style="width:50px;padding:6px 8px;border:1px solid var(--b);border-radius:6px;font-size:13px;background:var(--c,#fff);color:var(--t);outline:none;text-align:center">' +
              '<span style="font-size:11px;color:var(--t3)">0-10，默认3</span>' +
            '</div>' +
          '</div>' +
        '</aside>' +
      '</div>' +
      // Footer
      '<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 20px;border-top:1px solid var(--b);flex-shrink:0;background:var(--c,#fff)">' +
        '<span style="font-size:10px;color:var(--t3)">带 * 为必填项</span>' +
        '<div style="display:flex;gap:8px">' +
          '<button class="bt bt-o" onclick="document.getElementById(\'apDialog\').remove()">取消</button>' +
          '<button class="bt bt-p" onclick="_apSubmitForm()">' + btnText + '</button>' +
        '</div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);
  overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
  // 存储初始cron
  _apFormCron = cron;
  _apRenderScheduleEditor();
}

var _apExpanded = false;
function _apToggleExpand() {
  _apExpanded = !_apExpanded;
  var dlg = $('apDialogContent');
  if (!dlg) return;
  var btn = $('apExpandBtn');
  if (_apExpanded) {
    dlg.style.width = 'calc(100vw - 32px)';
    dlg.style.height = 'calc(100vh - 32px)';
    if (btn) {
      btn.title = '缩小';
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>';
    }
  } else {
    dlg.style.width = '900px';
    dlg.style.height = 'min(680px,calc(100vh - 64px))';
    if (btn) {
      btn.title = '放大';
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>';
    }
  }
}

// ===== Schedule Editor (multica-style three-dimension) =====
var _apFormCron = '0 9 * * *';

// Structured schedule model
var _apTimeKind = 'at';       // 'at' | 'every'
var _apTimeAt = '09:00';      // for 'at' mode
var _apEveryInterval = 30;    // for 'every' mode
var _apEveryUnit = 'minutes'; // 'minutes' | 'hours'
var _apEveryMinute = 0;       // firing minute for hours unit
var _apWindowFrom = '';       // '' = all day, 'HH:MM'
var _apWindowTo = '';
var _apDayKind = 'every';     // 'every' | 'weekly' | 'monthly'
var _apWeeklyDays = [1];      // 0=Sun..6=Sat
var _apMonthlyDay = 1;        // 1-31
var _apAdvanced = false;      // raw advanced mode

function _p2(n) { return String(n).padStart(2, '0'); }

// Parse cron into structured model
function _apParseCron(cron) {
  var parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) { _apAdvanced = true; return; }
  var m = parts[0], h = parts[1], dom = parts[2], mon = parts[3], dow = parts[4];
  if (mon !== '*') { _apAdvanced = true; return; }
  _apAdvanced = false;

  // Parse day pattern
  if (dom !== '*' && dow === '*') {
    _apDayKind = 'monthly';
    _apMonthlyDay = parseInt(dom) || 1;
  } else if (dom === '*' && dow !== '*') {
    _apDayKind = 'weekly';
    _apWeeklyDays = _apExpandDow(dow);
  } else {
    _apDayKind = 'every';
  }

  // Parse time pattern
  var mi = parseInt(m) || 0, hi = parseInt(h) || 0;
  if (m.indexOf('*/') === 0) {
    // every N minutes
    _apTimeKind = 'every';
    _apEveryUnit = 'minutes';
    _apEveryInterval = parseInt(m.slice(2)) || 30;
    _apEveryMinute = 0;
    if (h !== '*' && h.indexOf(',') >= 0) {
      var hs = h.split(',');
      _apWindowFrom = _p2(parseInt(hs[0])) + ':00';
      _apWindowTo = _p2(parseInt(hs[hs.length-1])) + ':59';
    } else {
      _apWindowFrom = ''; _apWindowTo = '';
    }
  } else if (h.indexOf('*/') === 0) {
    // every N hours
    _apTimeKind = 'every';
    _apEveryUnit = 'hours';
    _apEveryInterval = parseInt(h.slice(2)) || 1;
    _apEveryMinute = mi;
    _apWindowFrom = ''; _apWindowTo = '';
  } else if (h.indexOf(',') >= 0 && mi === 0) {
    // hourly window (0 H,H,H * * *)
    _apTimeKind = 'every';
    _apEveryUnit = 'hours';
    _apEveryInterval = 1;
    _apEveryMinute = 0;
    var hs2 = h.split(',');
    _apWindowFrom = _p2(parseInt(hs2[0])) + ':00';
    _apWindowTo = _p2(parseInt(hs2[hs2.length-1])) + ':59';
  } else if (h.indexOf('-') >= 0) {
    // hour range with step (M fromH-toH/N * * *)
    var range = h.split('-');
    var step = 1;
    if (range[1].indexOf('/') >= 0) { var sp = range[1].split('/'); range[1] = sp[0]; step = parseInt(sp[1]) || 1; }
    _apTimeKind = 'every';
    _apEveryUnit = 'hours';
    _apEveryInterval = step;
    _apEveryMinute = mi;
    _apWindowFrom = _p2(parseInt(range[0])) + ':' + _p2(mi);
    _apWindowTo = _p2(parseInt(range[1])) + ':' + _p2(mi);
  } else {
    // fixed time
    _apTimeKind = 'at';
    _apTimeAt = _p2(hi) + ':' + _p2(mi);
  }
}

function _apExpandDow(dow) {
  var days = [];
  if (dow === '*') return [0,1,2,3,4,5,6];
  dow.split(',').forEach(function(part) {
    if (part.indexOf('-') >= 0) {
      var r = part.split('-');
      for (var d = parseInt(r[0]); d <= parseInt(r[1]); d++) days.push(d);
    } else {
      days.push(parseInt(part));
    }
  });
  return days.length > 0 ? days : [1];
}

// Serialize day set as maximal consecutive runs: [1,2,3,4,5] → "1-5"
function _apDowFromDays(days) {
  var sorted = days.slice().sort(function(a,b){return a-b;});
  var parts = [];
  var i = 0;
  while (i < sorted.length) {
    var j = i;
    while (j + 1 < sorted.length && sorted[j+1] === sorted[j] + 1) j++;
    parts.push(j > i ? sorted[i] + '-' + sorted[j] : String(sorted[i]));
    i = j + 1;
  }
  return parts.join(',');
}

// Structured model → cron
function _apToCron() {
  if (_apAdvanced) return _apFormCron;
  var m, h, dom = '*', dow = '*';

  // Day pattern
  if (_apDayKind === 'monthly') {
    dom = String(_apMonthlyDay);
  } else if (_apDayKind === 'weekly') {
    if (_apWeeklyDays.length === 0 || _apWeeklyDays.length === 7) {
      dow = '*';
    } else {
      dow = _apDowFromDays(_apWeeklyDays);
    }
  }

  // Time pattern
  if (_apTimeKind === 'at') {
    var parts = _apTimeAt.split(':');
    m = String(parseInt(parts[1]) || 0);
    h = String(parseInt(parts[0]) || 0);
  } else if (_apEveryUnit === 'minutes') {
    m = '*/' + _apEveryInterval;
    if (_apWindowFrom && _apWindowTo) {
      var fh = parseInt(_apWindowFrom.split(':')[0]);
      var th = parseInt(_apWindowTo.split(':')[0]);
      var hours = [];
      for (var hh = fh; hh <= th; hh++) hours.push(hh);
      h = hours.join(',');
    } else {
      h = '*';
    }
  } else {
    // hours
    m = String(_apEveryMinute);
    if (_apWindowFrom && _apWindowTo) {
      var wfh = parseInt(_apWindowFrom.split(':')[0]);
      var wth = parseInt(_apWindowTo.split(':')[0]);
      h = _apEveryInterval === 1 ? wfh + '-' + wth : wfh + '-' + wth + '/' + _apEveryInterval;
    } else {
      h = _apEveryInterval === 1 ? '*' : '*/' + _apEveryInterval;
    }
  }
  return m + ' ' + h + ' ' + dom + ' * ' + dow;
}

// Human-readable description
function _apDescribeSchedule() {
  if (_apAdvanced) return null;
  var desc = '';
  // Time
  if (_apTimeKind === 'at') {
    desc += _apTimeAt;
  } else if (_apEveryUnit === 'minutes') {
    desc += '每' + _apEveryInterval + '分钟';
  } else {
    desc += '每' + _apEveryInterval + '小时';
    if (_apEveryMinute > 0) desc += ' ' + _p2(_apEveryMinute) + '分';
  }
  if (_apTimeKind === 'every' && _apWindowFrom && _apWindowTo) {
    desc += '（' + _apWindowFrom + '-' + _apWindowTo + '）';
  }
  desc += ' · ';
  // Days
  if (_apDayKind === 'every') {
    desc += '每天';
  } else if (_apDayKind === 'weekly') {
    var names = ['日','一','二','三','四','五','六'];
    desc += '每周' + _apWeeklyDays.slice().sort(function(a,b){return a-b;}).map(function(d){return names[d];}).join('、');
  } else {
    desc += '每月' + _apMonthlyDay + '日';
  }
  return desc;
}

// Compute next N runs (local timezone)
function _apComputeNextRuns(cron, count) {
  try {
    var parts = cron.trim().split(/\s+/);
    if (parts.length !== 5) return [];
    var m = parts[0], h = parts[1], dom = parts[2], mon = parts[3], dow = parts[4];
    var now = new Date();
    var results = [];
    var maxIter = 20000;
    for (var iter = 0; iter < maxIter && results.length < count; iter++) {
      now = new Date(now.getTime() + 60000);
      if (!_apCronMatch(m, now.getMinutes())) continue;
      if (!_apCronMatch(h, now.getHours())) continue;
      if (!_apCronMatch(dom, now.getDate())) continue;
      if (!_apCronMatch(dow, now.getDay())) continue;
      results.push(now.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}));
    }
    return results;
  } catch(e) { return []; }
}

function _apCronMatch(pattern, value) {
  if (pattern === '*') return true;
  if (pattern.indexOf('*/') === 0) {
    var step = parseInt(pattern.slice(2));
    return step > 0 && value % step === 0;
  }
  if (pattern.indexOf(',') >= 0) {
    return pattern.split(',').some(function(p) { return parseInt(p) === value; });
  }
  if (pattern.indexOf('-') >= 0) {
    var r = pattern.split('-');
    var hi2 = r[1];
    if (hi2.indexOf('/') >= 0) { var sp = hi2.split('/'); hi2 = sp[0]; }
    return value >= parseInt(r[0]) && value <= parseInt(hi2);
  }
  return parseInt(pattern) === value;
}

// Describe cron for detail page (static, no model)
function _apDescribeCron(cron) {
  var parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  var m = parts[0], h = parts[1], dom = parts[2], mon = parts[3], dow = parts[4];
  var mi = parseInt(m) || 0, hi = parseInt(h) || 0;
  var time = _p2(hi) + ':' + _p2(mi);
  var dayNames = ['日','一','二','三','四','五','六'];
  if (m.indexOf('*/') === 0 && h.indexOf(',') >= 0) {
    var hs = h.split(',');
    return '每天 ' + _p2(parseInt(hs[0])) + ':00-' + (parseInt(hs[hs.length-1])+1) + ':00 每' + m.slice(2) + '分钟';
  }
  if (m.indexOf('*/') === 0) return '每' + m.slice(2) + '分钟执行';
  if (h.indexOf('*/') === 0 && mi === 0) return '每' + h.slice(2) + '小时执行';
  if (mi === 0 && h.indexOf(',') >= 0) {
    var hs2 = h.split(',');
    return '每天 ' + _p2(parseInt(hs2[0])) + ':00-' + (parseInt(hs2[hs2.length-1])+1) + ':00 每小时';
  }
  if (dom === '*' && mon === '*' && dow === '*') return '每天 ' + time;
  if (dom === '*' && mon === '*' && dow !== '*') {
    if (dow === '1-5') return '工作日 ' + time;
    var days = dow.split(',').map(function(d) { return dayNames[parseInt(d)] || d; });
    return '每周' + days.join('、') + ' ' + time;
  }
  if (dom !== '*' && mon === '*' && dow === '*') return '每月' + parseInt(dom) + '日 ' + time;
  return cron;
}

// ===== Render schedule editor =====
function _apRenderScheduleEditor() {
  _apParseCron(_apFormCron);
  var container = $('apScheduleEditor');
  if (!container) return;

  // Shared styles (multica-inspired)
  var _fs = 'font-size:13px';
  var _cap = 'font-size:12px;font-weight:600;color:var(--t2);margin-bottom:6px;display:block';
  var _input = 'padding:6px 8px;border:1px solid var(--b);border-radius:6px;' + _fs + ';background:var(--c);color:var(--t);outline:none;min-width:0';
  var _select = _input + ';cursor:pointer';
  var _numInput = 'width:44px;text:center;padding:6px 4px;border:1px solid var(--b);border-radius:6px;' + _fs + ';background:var(--c);color:var(--t);outline:none;-moz-appearance:textfield';
  var _timeInput = 'padding:6px 6px;border:1px solid var(--b);border-radius:6px;' + _fs + ';background:var(--c);color:var(--t);outline:none;min-width:0';

  if (_apAdvanced) {
    container.innerHTML =
      '<div style="' + _cap + '">高级模式</div>' +
      '<input type="text" id="apCronRaw" value="' + _esc(_apFormCron) + '" oninput="_apOnRawInput(this.value)" placeholder="分 时 日 月 周" style="width:100%;' + _input + ';font-family:monospace">' +
      '<div style="font-size:10px;color:var(--t3);margin-top:4px">输入 5 段制 cron 表达式（分 时 日 月 周）</div>' +
      '<button class="bt bt-s bt-o" style="margin-top:6px" onclick="_apExitAdvanced()">返回结构化模式</button>';
    _apUpdatePreview();
    return;
  }

  // ===== SegmentedToggle helper =====
  function segToggle(opts, current, onclickFn) {
    return '<div style="display:inline-grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:2px;padding:2px;background:var(--bg);border-radius:6px">' +
      opts.map(function(o) {
        var sel = o[0] === current;
        return '<button type="button" onclick="' + onclickFn + '(\'' + o[0] + '\')" style="padding:4px 12px;border:none;border-radius:4px;font-size:11px;font-weight:500;cursor:pointer;white-space:nowrap;transition:all .15s;' +
          (sel ? 'background:var(--c);color:var(--t);box-shadow:0 1px 2px rgba(0,0,0,0.08)' : 'background:transparent;color:var(--t3)') + '">' + o[1] + '</button>';
      }).join('') + '</div>';
  }

  // ===== Time fieldset =====
  var timeHTML = '<fieldset style="border:none;padding:0;margin:0 0 16px 0">' +
    '<legend style="' + _cap + '">时间</legend>' +
    '<div style="margin-bottom:8px">' + segToggle([['at','固定时间'],['every','间隔执行']], _apTimeKind, '_apSetTimeKind') + '</div>';

  if (_apTimeKind === 'at') {
    timeHTML += '<input type="time" value="' + _apTimeAt + '" oninput="_apOnTimeAt(this.value)" style="' + _timeInput + '">';
  } else {
    // Every mode: frequency + window on one row (multica 7:4 grid)
    var maxInt = _apEveryUnit === 'hours' ? 23 : 59;
    var wFrom = _apWindowFrom || '00:00';
    var wTo = _apWindowTo || '23:59';
    var hourOnly = _apEveryUnit === 'minutes';

    // Frequency box (InputGroup style)
    var freqHTML = '<div style="display:flex;align-items:center;border:1px solid var(--b);border-radius:6px;overflow:hidden;min-width:0;flex:1">' +
      '<span style="padding:6px 8px;font-size:11px;color:var(--t3);white-space:nowrap">每</span>' +
      '<input type="number" min="1" max="' + maxInt + '" value="' + _apEveryInterval + '" oninput="_apOnEveryInterval(this.value)" style="' + _numInput + ';border:none;border-radius:0;flex:1">' +
      '<select oninput="_apOnEveryUnit(this.value)" style="border:none;border-left:1px solid var(--b);padding:6px 8px;font-size:11px;background:var(--c);color:var(--t);outline:none;cursor:pointer">' +
        '<option value="minutes"' + (_apEveryUnit==='minutes'?' selected':'') + '>分钟</option>' +
        '<option value="hours"' + (_apEveryUnit==='hours'?' selected':'') + '>小时</option>' +
      '</select>' +
    '</div>';

    // Window box
    var winHTML;
    if (hourOnly) {
      // Hour-only selects for minutes unit
      var fromOpts = '', toOpts = '';
      for (var ho = 0; ho <= 23; ho++) {
        fromOpts += '<option value="' + _p2(ho) + ':00"' + (wFrom === _p2(ho)+':00' ? ' selected':'') + '>' + _p2(ho) + ':00</option>';
        toOpts += '<option value="' + _p2(ho) + ':59"' + (wTo === _p2(ho)+':59' ? ' selected':'') + '>' + _p2(ho) + ':59</option>';
      }
      winHTML = '<div style="display:flex;align-items:center;border:1px solid var(--b);border-radius:6px;overflow:hidden;min-width:0;flex:1">' +
        '<span style="padding:6px 6px;font-size:11px;color:var(--t3)">🕒</span>' +
        '<select id="apWinFrom" oninput="_apOnWindowFrom(this.value)" style="border:none;flex:1;padding:6px 4px;font-size:11px;background:var(--c);color:var(--t);outline:none;cursor:pointer;min-width:0">' + fromOpts + '</select>' +
        '<span style="padding:0 2px;font-size:10px;color:var(--t3)">~</span>' +
        '<select id="apWinTo" oninput="_apOnWindowTo(this.value)" style="border:none;flex:1;padding:6px 4px;font-size:11px;background:var(--c);color:var(--t);outline:none;cursor:pointer;min-width:0">' + toOpts + '</select>' +
      '</div>';
    } else {
      // Time inputs for hours unit + firing minute
      winHTML = '<div style="display:flex;align-items:center;border:1px solid var(--b);border-radius:6px;overflow:hidden;min-width:0;flex:1">' +
        '<span style="padding:6px 6px;font-size:11px;color:var(--t3)">🕒</span>' +
        '<input type="time" id="apWinFrom" value="' + wFrom + '" oninput="_apOnWindowFrom(this.value)" style="border:none;flex:1;padding:6px 4px;font-size:11px;background:var(--c);color:var(--t);outline:none;min-width:0">' +
        '<span style="padding:0 2px;font-size:10px;color:var(--t3)">~</span>' +
        '<input type="time" id="apWinTo" value="' + wTo + '" oninput="_apOnWindowTo(this.value)" style="border:none;flex:1;padding:6px 4px;font-size:11px;background:var(--c);color:var(--t);outline:none;min-width:0">' +
      '</div>';
    }

    timeHTML += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;align-items:center">' + freqHTML + winHTML + '</div>';

    // Firing minute for hours unit (below the row)
    if (!hourOnly) {
      timeHTML += '<div style="display:flex;align-items:center;gap:6px;margin-top:4px">' +
        '<span style="font-size:10px;color:var(--t3)">第</span>' +
        '<input type="number" min="0" max="59" value="' + _apEveryMinute + '" oninput="_apOnEveryMinute(this.value)" style="' + _numInput + ';width:36px">' +
        '<span style="font-size:10px;color:var(--t3)">分钟执行</span>' +
      '</div>';
    }
  }
  timeHTML += '</fieldset>';

  // ===== Day fieldset =====
  var dayHTML = '<fieldset style="border:none;padding:0;margin:0 0 12px 0">' +
    '<legend style="' + _cap + '">日期</legend>';

  // Day kind select + optional monthly number on same row (7:4 grid)
  var daySelectSpan = _apDayKind !== 'monthly' ? ';grid-column:span 2' : '';
  dayHTML += '<div style="display:grid;grid-template-columns:7fr 4fr;gap:6px;align-items:center">' +
    '<select oninput="_apOnDayKind(this.value)" style="' + _select + ';' + daySelectSpan + '">' +
      '<option value="every"' + (_apDayKind==='every'?' selected':'') + '>每天</option>' +
      '<option value="weekly"' + (_apDayKind==='weekly'?' selected':'') + '>每周</option>' +
      '<option value="monthly"' + (_apDayKind==='monthly'?' selected':'') + '>每月</option>' +
    '</select>';
  if (_apDayKind === 'monthly') {
    dayHTML += '<input type="number" min="1" max="31" value="' + _apMonthlyDay + '" oninput="_apSetMonthlyDay(parseInt(this.value)||1)" style="' + _numInput + ';width:100%;padding:6px 8px">';
  }
  dayHTML += '</div>';

  if (_apDayKind === 'weekly') {
    var dn = ['日','一','二','三','四','五','六'];
    dayHTML += '<div style="display:flex;gap:2px;margin-top:6px">' +
      dn.map(function(name, i) {
        var sel = _apWeeklyDays.indexOf(i) >= 0;
        return '<button type="button" onclick="_apToggleDay(' + i + ')" style="flex:1;min-width:0;height:26px;border:none;border-radius:4px;font-size:11px;font-weight:500;cursor:pointer;transition:all .15s;' +
          (sel ? 'background:var(--t);color:var(--c)' : 'background:var(--bg);color:var(--t3)') + '">' + name + '</button>';
      }).join('') + '</div>';
  } else if (_apDayKind === 'monthly') {
    if (_apMonthlyDay >= 29) {
      dayHTML += '<div style="font-size:10px;color:var(--t3);margin-top:4px">提示：2月等短月可能不触发</div>';
    }
  }
  dayHTML += '</fieldset>';

  // Advanced entry
  var advHTML = '<div style="font-size:10px;margin-top:2px">' +
    '<span style="cursor:pointer;color:var(--a)" onclick="_apEnterAdvanced()">高级模式：直接输入 cron 表达式 →</span>' +
  '</div>';

  container.innerHTML = timeHTML + dayHTML + advHTML;
  _apUpdatePreview();
}

// ===== Event handlers =====
function _apSetTimeKind(kind) {
  _apTimeKind = kind;
  _apFormCron = _apToCron();
  _apRenderScheduleEditor();
}

function _apOnTimeAt(val) {
  _apTimeAt = val || '09:00';
  _apFormCron = _apToCron();
  _apUpdatePreview();
}

function _apOnEveryInterval(val) {
  _apEveryInterval = Math.max(1, Math.min(_apEveryUnit === 'hours' ? 23 : 59, parseInt(val) || 1));
  _apFormCron = _apToCron();
  _apUpdatePreview();
}

function _apOnEveryUnit(val) {
  _apEveryUnit = val;
  _apEveryInterval = Math.min(_apEveryInterval, val === 'hours' ? 23 : 59);
  _apFormCron = _apToCron();
  _apRenderScheduleEditor();
}

function _apOnEveryMinute(val) {
  _apEveryMinute = Math.max(0, Math.min(59, parseInt(val) || 0));
  _apFormCron = _apToCron();
  _apUpdatePreview();
}

function _apOnWindowFrom(val) {
  _apWindowFrom = val;
  if (_apWindowFrom && _apWindowTo) {
    var fh = parseInt(_apWindowFrom.split(':')[0]);
    var th = parseInt(_apWindowTo.split(':')[0]);
    if (th < fh) _apWindowTo = _apWindowFrom;
  }
  // All day check
  if (_apWindowFrom === '00:00' && (_apWindowTo === '23:59' || _apWindowTo === '24:00')) {
    _apWindowFrom = ''; _apWindowTo = '';
  }
  _apFormCron = _apToCron();
  _apUpdatePreview();
}

function _apOnWindowTo(val) {
  _apWindowTo = val;
  if (_apWindowFrom && _apWindowTo) {
    var fh = parseInt(_apWindowFrom.split(':')[0]);
    var th = parseInt(_apWindowTo.split(':')[0]);
    if (th < fh) _apWindowFrom = _apWindowTo;
  }
  if (_apWindowFrom === '00:00' && (_apWindowTo === '23:59' || _apWindowTo === '24:00')) {
    _apWindowFrom = ''; _apWindowTo = '';
  }
  _apFormCron = _apToCron();
  _apUpdatePreview();
}

function _apOnDayKind(val) {
  _apDayKind = val;
  _apFormCron = _apToCron();
  _apRenderScheduleEditor();
}

function _apToggleDay(day) {
  var idx = _apWeeklyDays.indexOf(day);
  if (idx >= 0) {
    if (_apWeeklyDays.length > 1) _apWeeklyDays.splice(idx, 1);
  } else {
    _apWeeklyDays.push(day);
  }
  _apFormCron = _apToCron();
  _apRenderScheduleEditor();
}

function _apSetMonthlyDay(day) {
  _apMonthlyDay = day;
  _apFormCron = _apToCron();
  _apRenderScheduleEditor();
}

function _apEnterAdvanced() {
  _apAdvanced = true;
  // 不调 _apParseCron——它会根据当前 cron 重置 _apAdvanced
  var container = $('apScheduleEditor');
  if (!container) return;
  var _cap = 'font-size:12px;font-weight:600;color:var(--t2);margin-bottom:6px;display:block';
  var _input = 'padding:6px 8px;border:1px solid var(--b);border-radius:6px;font-size:13px;background:var(--c);color:var(--t);outline:none;min-width:0';
  container.innerHTML =
    '<div style="' + _cap + '">高级模式</div>' +
    '<input type="text" id="apCronRaw" value="' + _esc(_apFormCron) + '" oninput="_apOnRawInput(this.value)" placeholder="分 时 日 月 周" style="width:100%;' + _input + ';font-family:monospace">' +
    '<div style="font-size:11px;color:var(--t3);margin-top:4px">输入 5 段制 cron 表达式（分 时 日 月 周）</div>' +
    '<button class="bt bt-s bt-o" style="margin-top:6px" onclick="_apExitAdvanced()">返回结构化模式</button>';
  _apUpdatePreview();
}

function _apExitAdvanced() {
  _apAdvanced = false;
  _apParseCron(_apFormCron || '0 9 * * *');
  _apFormCron = _apToCron();
  _apRenderScheduleEditor();
}

function _apOnRawInput(val) {
  _apFormCron = val.trim();
  _apUpdatePreview();
}

// ===== Preview =====
function _apUpdatePreview() {
  var el = $('apCronPreview');
  if (!el) return;
  var desc = _apAdvanced ? null : _apDescribeSchedule();
  var next3 = _apComputeNextRuns(_apFormCron, 3);
  var html = '<div style="font-family:monospace;font-size:11px;color:var(--t)">' + _esc(_apFormCron) + '</div>';
  if (desc) {
    html += '<div style="font-size:12px;color:var(--t2);margin-top:2px">🕒 ' + desc + '</div>';
  } else {
    html += '<div style="font-size:12px;color:var(--t3);margin-top:2px">高级表达式</div>';
  }
  if (next3.length > 0) {
    html += '<div style="font-size:10px;color:var(--t3);margin-top:4px">下次运行:</div>';
    next3.forEach(function(t) {
      html += '<div style="font-size:10px;color:var(--t3)">' + t + '</div>';
    });
  } else {
    html += '<div style="font-size:10px;color:var(--t3);margin-top:4px">（未计算出下次运行时间，表达式可能无效或触发时间较远）</div>';
  }
  el.innerHTML = html;
}

// ===== 润色指令 =====
function _apPolishInstruction(btn) {
  var name = $('apFormName').value.trim();
  var instruction = $('apFormInstruction').value.trim();
  if (!instruction) { if (app&&app.showToast) app.showToast('请先填写巡检指令'); return; }
  if (btn) { btn.disabled = true; btn.textContent = '润色中...'; }
  API.post('/autopilot/polish', {name: name, instruction: instruction}, 20000).then(function(d) {
    if (d.instruction) {
      $('apFormInstruction').value = d.instruction;
      if (app&&app.showToast) app.showToast('润色完成');
    }
    if (btn) { btn.disabled = false; btn.textContent = '✨ 润色'; }
  }).catch(function(e) {
    if (app&&app.showToast) app.showToast('润色失败: ' + (e.message||e));
    if (btn) { btn.disabled = false; btn.textContent = '✨ 润色'; }
  });
}

// ===== 提交表单 =====
function _apSubmitForm() {
  var name = $('apFormName').value.trim();
  var skillName = $('apFormSkill').value.trim();
  var instruction = $('apFormInstruction').value.trim();
  var retry = parseInt($('apFormRetry').value) || 3;
  if (!name) { if (app&&app.showToast) app.showToast('请填写任务名称'); return; }
  if (!instruction) { if (app&&app.showToast) app.showToast('请填写巡检指令'); return; }
  var repo = (typeof app !== 'undefined' && app.currentRepo()) ? (app.currentRepo().fullName || '') : '';
  var body = {
    name: name,
    repo_key: 'gitcode:' + repo,
    instruction: instruction,
    skill_name: skillName,
    cron: _apFormCron,
    max_retry: retry,
    enabled: true
  };
  var btn = $('apDialog').querySelector('.bt-p');
  if (btn) { btn.disabled = true; btn.textContent = '提交中...'; }
  if (_apEditingId) {
    API.put('/autopilot/tasks/' + _apEditingId, body).then(function() {
      if (app&&app.showToast) app.showToast('已保存');
      $('apDialog').remove();
      _apLoadTasks();
    }).catch(function(e) {
      if (app&&app.showToast) app.showToast('保存失败: ' + (e.message||e));
      if (btn) { btn.disabled = false; btn.textContent = '保存'; }
    });
  } else {
    API.post('/autopilot/tasks', body).then(function() {
      if (app&&app.showToast) app.showToast('已创建');
      $('apDialog').remove();
      _apLoadTasks();
    }).catch(function(e) {
      if (app&&app.showToast) app.showToast('创建失败: ' + (e.message||e));
      if (btn) { btn.disabled = false; btn.textContent = '创建'; }
    });
  }
}

// ===== 详情页 =====
function renderAutopilotDetail(id) {
  _apDetailId = id;
  var pc = $('pageContent');
  if (!pc) return;
  pc.innerHTML = '<div class="loading">加载中...</div>';
  API.get('/autopilot/tasks/' + id).then(function(t) {
    if (window._currentView !== 'autopilot-detail') return;
    pc.innerHTML = _apDetailHTML(t);
    _apLoadExecutions(id);
  }).catch(function(e) {
    pc.innerHTML = '<div class="error">加载失败: ' + _esc(e.message||e) + '</div>';
  });
}

function _apDetailHTML(t) {
  // Toggle switch moved to title bar (multica-style)
  return '<div>' +
    // Breadcrumb
    '<div style="display:flex;align-items:center;gap:8px;margin-bottom:16px">' +
      '<a onclick="app.nav(\'autopilot\')" style="cursor:pointer;color:var(--a);font-size:13px">自动化</a>' +
      '<span style="color:var(--t3)">/</span>' +
      '<span style="font-size:13px;font-weight:600">' + _esc(t.name) + '</span>' +
      '<div style="flex:1"></div>' +
      // Toggle switch (multica-style)
      '<label style="display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none">' +
        '<span style="font-size:12px;color:var(--t2)">' + (t.enabled ? '启用' : '暂停') + '</span>' +
        '<span data-ap-toggle="' + t.id + '" onclick="event.stopPropagation();_apToggleEnabled(' + t.id + ', ' + (!t.enabled) + ')" style="position:relative;width:36px;height:20px;border-radius:10px;background:' + (t.enabled ? '#28a745' : 'var(--b)') + ';transition:background .2s;cursor:pointer;flex-shrink:0">' +
          '<span style="position:absolute;top:2px;left:' + (t.enabled ? '18px' : '2px') + ';width:16px;height:16px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,0.2);transition:left .2s"></span>' +
        '</span>' +
      '</label>' +
      '<button class="bt bt-o" onclick="_apEdit(' + t.id + ')">编辑</button>' +
      '<button class="bt bt-p" onclick="_apTrigger(' + t.id + ')">立即触发</button>' +
    '</div>' +
    // 需求信息
    '<div class="cd" style="margin-bottom:16px">' +
      '<div class="cd-h"><h2>需求信息</h2></div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px">' +
        '<div><span style="color:var(--t2)">名称:</span> <b>' + _esc(t.name) + '</b></div>' +
        '<div><span style="color:var(--t2)">仓库:</span> ' + _esc(t.repo_key) + '</div>' +
        '<div><span style="color:var(--t2)">Skill:</span> ' + _esc(t.skill_name||'—') + '</div>' +
        '<div><span style="color:var(--t2)">最大重试:</span> ' + (t.max_retry||3) + '</div>' +
        '<div><span style="color:var(--t2)">创建时间:</span> ' + _apFormatTime(t.created_at) + '</div>' +
      '</div>' +
      '<div style="margin-top:12px"><span style="color:var(--t2);font-size:12px">巡检指令:</span><pre style="white-space:pre-wrap;font-size:13px;margin-top:4px;padding:10px;background:var(--bg);border-radius:6px;font-family:inherit">' + _esc(t.instruction||'') + '</pre></div>' +
    '</div>' +
    // 时间表
    '<div class="cd" style="margin-bottom:16px">' +
      '<div class="cd-h"><h2>时间表</h2></div>' +
      '<div style="display:flex;align-items:center;gap:12px;font-size:13px">' +
        '<span style="font-size:16px">🕒</span>' +
        '<code style="font-family:monospace">' + _esc(t.cron) + '</code>' +
        '<span style="color:var(--t2)">→ ' + _apDescribeCron(t.cron) + '</span>' +
      '</div>' +
      (t.next_run_at ? '<div style="margin-top:8px;font-size:12px;color:var(--t2)">下次运行: ' + _apFormatTime(t.next_run_at) + '</div>' : '') +
      (t.last_run_at ? '<div style="margin-top:4px;font-size:12px;color:var(--t2)">上次运行: ' + _apFormatTime(t.last_run_at) + '</div>' : '') +
    '</div>' +
    // 执行历史
    '<div class="cd">' +
      '<div class="cd-h"><h2>执行历史</h2></div>' +
      '<div id="apExecList"><div class="loading">加载中...</div></div>' +
    '</div>' +
  '</div>';
}

function _apToggleEnabled(id, enabled) {
  API.put('/autopilot/tasks/' + id, {enabled: !!enabled}).then(function() {
    if (app&&app.showToast) app.showToast(enabled ? '已启用' : '已暂停');
    // 更新本地数据
    var t = _apAllTasks.find(function(x) { return x.id === id; });
    if (t) t.enabled = enabled;
    // 直接操作DOM更新开关样式（不重新渲染整个页面）
    var labels = document.querySelectorAll('.cd-h label, [data-toggle-id]');
    // 通过onclick属性找到对应的开关元素
    var switchEl = document.querySelector('[data-ap-toggle="' + id + '"]');
    if (switchEl) {
      // 更新背景色
      switchEl.style.background = enabled ? '#28a745' : 'var(--b)';
      // 更新圆点位置
      var dot = switchEl.querySelector('span');
      if (dot) dot.style.left = enabled ? '18px' : '2px';
      // 更新文字
      var label = switchEl.parentElement.querySelector('span:first-child');
      if (label) label.textContent = enabled ? '启用' : '暂停';
      // 更新onclick为新值
      switchEl.setAttribute('onclick', 'event.stopPropagation();_apToggleEnabled(' + id + ', ' + !enabled + ')');
    }
  }).catch(function(e) {
    if (app&&app.showToast) app.showToast('操作失败: ' + (e.message||e));
  });
}

// ===== 执行历史 =====
function _apLoadExecutions(taskId) {
  API.get('/autopilot/executions?task_id=' + taskId + '&limit=50').then(function(d) {
    var el = $('apExecList');
    if (!el) return;
    var execs = d.executions || [];
    if (execs.length === 0) {
      el.innerHTML = '<div style="padding:24px;text-align:center;color:var(--t3)">暂无执行记录</div>';
      return;
    }
    var statusIcons = {success:'✅', failed:'❌', running:'🔄', pending:'⏳', retrying:'🔁', skipped:'⏭'};
    var html = '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
      '<thead><tr style="border-bottom:2px solid var(--b);text-align:left;color:var(--t2)">' +
        '<th style="padding:8px">状态</th>' +
        '<th style="padding:8px">重试</th>' +
        '<th style="padding:8px">开始时间</th>' +
        '<th style="padding:8px">结果/错误</th>' +
        '<th style="padding:8px">操作</th>' +
      '</tr></thead><tbody>';
    execs.forEach(function(e) {
      var icon = statusIcons[e.status] || '•';
      var result = e.error ? '<span style="color:var(--r)">' + _esc(e.error.substring(0,80)) + '</span>' : _esc((e.result||'').substring(0,80));
      var findingsBtn = e.has_findings
        ? '<button class="bt bt-s bt-o" onclick="_apShowFindings(' + e.id + ')" style="font-size:11px">查看问题</button>'
        : '—';
      html += '<tr style="border-bottom:1px solid var(--b)">' +
        '<td style="padding:8px">' + icon + ' ' + _esc(e.status) + '</td>' +
        '<td style="padding:8px">' + (e.retry_count||0) + '</td>' +
        '<td style="padding:8px;color:var(--t2);white-space:nowrap">' + _apFormatTime(e.started_at || e.created_at) + '</td>' +
        '<td style="padding:8px">' + result + '</td>' +
        '<td style="padding:8px">' + findingsBtn + '</td>' +
      '</tr>';
    });
    html += '</tbody></table>';
    el.innerHTML = html;
  }).catch(function(e) {
    var el = $('apExecList');
    if (el) el.innerHTML = '<div class="error">加载失败: ' + _esc(e.message||e) + '</div>';
  });
}

// ===== 问题列表弹窗 =====
function _apShowFindings(execId) {
  API.get('/autopilot/executions/' + execId + '/findings').then(function(d) {
    var findings = d.findings || [];
    if (findings.length === 0) {
      if (app && app.showToast) app.showToast('暂无问题记录');
      return;
    }
    var sevIcons = {high:'🔴', medium:'🟡', low:'🔵'};
    var rows = findings.map(function(f) {
      var icon = sevIcons[f.severity] || '•';
      var dispatchBtn = f.dispatched
        ? '<span style="font-size:11px;color:var(--t3)">已派发</span>'
        : '<button class="bt bt-s bt-p" onclick="_apDispatchFinding(' + execId + ', ' + f.id + ', this)" style="font-size:11px">派发</button>';
      return '<div style="display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid var(--b)">' +
        '<span style="font-size:14px;flex-shrink:0">' + icon + '</span>' +
        '<span style="flex:1;font-size:12px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(f.title) + '</span>' +
        '<span style="flex-shrink:0">' + dispatchBtn + '</span>' +
      '</div>';
    }).join('');

    var overlay = document.createElement('div');
    overlay.id = 'apFindingsDialog';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.25);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center';
    overlay.innerHTML =
      '<div style="background:var(--c,#fff);border-radius:12px;width:560px;max-width:calc(100vw - 32px);max-height:calc(100vh - 64px);display:flex;flex-direction:column;overflow:hidden;box-shadow:0 12px 48px rgba(0,0,0,0.15)">' +
        '<div style="display:flex;align-items:center;justify-content:space-between;padding:12px 20px;border-bottom:1px solid var(--b);flex-shrink:0">' +
          '<span style="font-size:14px;font-weight:600">问题列表 (执行 #' + execId + ')</span>' +
          '<span onclick="document.getElementById(\'apFindingsDialog\').remove()" style="cursor:pointer;font-size:18px;color:var(--t3);padding:4px 8px;border-radius:4px;line-height:1">×</span>' +
        '</div>' +
        '<div style="flex:1;overflow-y:auto;padding:0 20px">' + rows + '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
  }).catch(function(e) {
    if (app && app.showToast) app.showToast('加载问题失败: ' + (e.message||e));
  });
}

// ===== 派发问题 =====
function _apDispatchFinding(execId, findingId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '派发中...'; }
  API.post('/autopilot/executions/' + execId + '/dispatch', {finding_id: findingId}).then(function(d) {
    var msg = '已创建 Issue #' + (d.issue_number || '');
    if (app && app.showToast) app.showToast(msg);
    if (btn) {
      btn.disabled = true;
      btn.textContent = '已派发';
      btn.classList.remove('bt-p');
      btn.classList.add('bt-o');
      btn.style.opacity = '0.6';
      btn.style.cursor = 'default';
    }
  }).catch(function(e) {
    if (app && app.showToast) app.showToast('派发失败: ' + (e.message||e));
    if (btn) { btn.disabled = false; btn.textContent = '派发'; }
  });
}

// ===== 详情页跳转 =====
function _apShowDetail(id) {
  if (typeof app !== 'undefined' && app.nav) app.nav('autopilot-detail', String(id));
}

// ===== 工具函数 =====
function _esc(s) {
  s = String(s || '');
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function _apRelativeTime(utcStr) {
  if (!utcStr) return '—';
  var ts = Date.parse(utcStr);
  if (!ts) return utcStr;
  var diff = Date.now() - ts;
  if (diff < 60000) return 'just now';
  if (diff < 3600000) return Math.floor(diff/60000) + 'm ago';
  if (diff < 86400000) return Math.floor(diff/3600000) + 'h ago';
  if (diff < 604800000) return Math.floor(diff/86400000) + 'd ago';
  return Math.floor(diff/604800000) + 'w ago';
}

function _apFormatTime(utcStr) {
  if (!utcStr) return '—';
  var d = new Date(utcStr);
  if (isNaN(d)) return utcStr;
  return d.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}
