// 组织流水线页面（Issue #2261：从组织详情页拆出的独立页面）
// 需求与下派 / 下发看板 / 组织上下文 / 关联仓库 只在此页展示；
// 组织详情页（organizations.js）保持原版，仅保留关联仓库区块与入口按钮。

var _orgWorkbench = {
  org: null,
  members: [],
  repos: [],
  issues: [],
  dispatches: [],
  dispatch: null,
  activeTab: 'overview',
  initialTab: 'overview',
  pendingIssueIndex: -1,
  transferRepoIndex: -1,
  // Issue #2236：上下文 / 知识库双 Tab 状态
  contexts: [],
  contextsError: '',
  knowledge: [],
  knowledgeError: '',
  contentTab: 'context',
  knowledgeFilters: {kind: '', status: '', tag: '', q: ''},
  knowledgeContentCache: {},
  // 正文读取失败的知识 id：大小无法校验，预览标注「大小未知」并阻断提交（避免按 0 字节绕过 64KB 上限）
  knowledgeContentFailed: {},
  // Issue #2236：下派向导选择与预览
  dispatchContexts: [],
  dispatchKnowledge: [],
  dispatchContextMeta: {},
  dispatchKnowledgeMeta: {},
  dispatchKnowledgeFilters: {kind: '', tag: '', q: ''},
  dispatchSelection: {ctxIds: [], knowledgeIds: []}
};

// Issue #2236：下派携带内容客户端估算上限。与 #2235 服务端预览接口契约对齐前，
// 先按 64KB 阻断，避免超限下派失败。
var ORG_DISPATCH_CONTEXT_LIMIT_BYTES = 64 * 1024;
// 与后端 knowledge.MAX_CONTENT_BYTES 默认值一致，仅用于表单本地预校验提示。
var ORG_CONTENT_LIMIT_BYTES = 5 * 1024 * 1024;

function _orgpEsc(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _orgCanWrite(role) {
  return ['owner', 'admin'].indexOf(String(role || '').toLowerCase()) >= 0;
}

function _orgStatusClass(status) {
  var s = String(status || '').toLowerCase();
  if (s === 'failed' || s === 'timeout_failed') return 'org-status-failed';
  if (s === 'partial_failed') return 'org-status-partial_failed';
  if (s === 'completed') return 'org-status-completed';
  if (s === 'cancelled') return 'org-status-cancelled';
  if (s === 'running' || s === 'dispatching' || s === 'creating') return 'org-status-running';
  return 'org-status-pending';
}

function _orgStatusBadge(status) {
  var s = String(status || 'pending');
  var palette = {
    failed: 'background:#fdecec;color:#c62828;border-color:#f3b8b8',
    timeout_failed: 'background:#fdecec;color:#c62828;border-color:#f3b8b8',
    partial_failed: 'background:#fff4e5;color:#b45309;border-color:#f4ce8e',
    completed: 'background:#e8f5e9;color:#2e7d32;border-color:#b7dfb9',
    cancelled: 'background:#eef1f5;color:#5f6b7a;border-color:#d4dae2',
    running: 'background:#e8f1ff;color:#1d5fb8;border-color:#b9d1f6',
    dispatching: 'background:#e8f1ff;color:#1d5fb8;border-color:#b9d1f6',
    creating: 'background:#e8f1ff;color:#1d5fb8;border-color:#b9d1f6',
    pending: 'background:#f3f4f6;color:#5f6b7a;border-color:#d8dde5'
  };
  var labelKey = 'organizations.workbench.status.' + s;
  var label = I18N.t(labelKey);
  if (!label || label === labelKey) label = s;
  return '<span class="' + _orgStatusClass(s) + '" style="display:inline-block;padding:2px 8px;border:1px solid;border-radius:999px;font-size:11px;font-weight:600;' +
    (palette[s] || palette.pending) + '">' + _orgpEsc(label) + '</span>';
}

function _orgErrorText(e) {
  if (API.localizeOrgError) return API.localizeOrgError(e);
  return (e && e.message) || String(e || '');
}

function _orgRepoParts(repoKey) {
  var raw = String(repoKey || '');
  if (raw.indexOf(':') >= 0) raw = raw.split(':').slice(1).join(':');
  var parts = raw.split('/');
  return {owner: parts[0] || '', name: parts[1] || '', full: raw};
}

function _orgPanel(html) {
  var panel = document.getElementById('orgTabPanel');
  if (panel) panel.innerHTML = html;
}

function _orgCardShell(title, body, actionHtml) {
  return '<div style="border:1px solid var(--b);border-radius:12px;padding:14px;background:var(--c);min-width:0">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px">' +
    '<div style="font-size:13px;font-weight:600;color:var(--t1)">' + _orgpEsc(title) + '</div>' +
    (actionHtml || '') + '</div>' + body + '</div>';
}

function _orgProvisionCard(org) {
  var status = org.provision_status || 'none';
  var canRetry = _orgCanWrite(org.role) && !org.is_personal && status === 'failed';
  var action = canRetry
    ? '<button id="orgProvisionRetry" onclick="_orgRetryProvision()" style="padding:5px 10px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--a);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.provision.retry") + '</button>'
    : '';
  var error = org.provision_error
    ? '<div style="font-size:11px;color:var(--r);margin-top:8px;word-break:break-word">' + _orgpEsc(org.provision_error) + '</div>' : '';
  return _orgCardShell(I18N.t("organizations.workbench.cards.provision"), _orgStatusBadge(status) + error, action);
}

function _orgSettingsCard(org) {
  var action = _orgCanWrite(org.role)
    ? '<button onclick="_orgOpenSettings()" style="padding:5px 10px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--a);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.settings.open") + '</button>'
    : '<span style="font-size:11px;color:var(--t3)">' + I18N.t("organizations.workbench.readonly") + '</span>';
  return _orgCardShell(I18N.t("organizations.workbench.cards.settings"),
    '<div style="font-size:12px;color:var(--t2);line-height:1.8">' +
    I18N.t("organizations.detail.role_label") + ': <b>' + _orgpEsc(org.role || 'member') + '</b><br>' +
    I18N.t("organizations.detail.members_title") + ': ' + _orgWorkbench.members.length + '</div>', action);
}

function _orgRenderCards() {
  var org = _orgWorkbench.org || {};
  var repoKey = org.org_repo_key || '';
  var sceneId = org.org_scene_id || '';
  var repoBody = '<div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--t2);word-break:break-all">' +
    _orgpEsc(repoKey || I18N.t("organizations.workbench.not_generated")) + '</div>' +
    '<div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap">' +
    (repoKey ? '<button onclick="_orgOpenOrgRepo()" style="padding:5px 9px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--a);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.open_repo") + '</button>' +
      '<button onclick="_orgCopy(&quot;org_repo_key&quot;)" style="padding:5px 9px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--t2);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.copy") + '</button>' : '') +
    '</div>';
  var sceneBody = '<div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--t2);word-break:break-all">' +
    _orgpEsc(sceneId || I18N.t("organizations.workbench.not_generated")) + '</div>' +
    '<div style="font-size:11px;color:var(--t3);margin-top:8px">intake → dispatch → aggregate</div>' +
    (sceneId ? '<div style="display:flex;gap:6px;margin-top:10px"><button onclick="_orgOpenScene()" style="padding:5px 9px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--a);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.open_scene") + '</button>' +
      '<button onclick="_orgCopy(&quot;org_scene_id&quot;)" style="padding:5px 9px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--t2);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.copy") + '</button></div>' : '');
  return '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px">' +
    _orgCardShell(I18N.t("organizations.workbench.org_repo_key"), repoBody) +
    _orgCardShell(I18N.t("organizations.workbench.org_scene_id"), sceneBody) +
    _orgProvisionCard(org) + _orgSettingsCard(org) + '</div>';
}

function _orgSetTab(tab, dispatchPreset) {
  var allowed = ['overview', 'repos', 'demands', 'dispatches', 'context'];
  tab = allowed.indexOf(tab) >= 0 ? tab : 'overview';
  var prevTab = _orgWorkbench.activeTab;
  _orgWorkbench.activeTab = tab;
  // Issue #2236：离开 context Tab 时清掉子页深链，避免刷新又回到上下文页
  if (prevTab === 'context' && tab !== 'context') _orgClearContentTabHash();
  var tabs = document.getElementById('orgTabs');
  if (!tabs) return;
  var labels = {
    overview: 'organizations.workbench.tabs.overview',
    repos: 'organizations.workbench.tabs.repos',
    demands: 'organizations.workbench.tabs.demands',
    dispatches: 'organizations.workbench.tabs.dispatches',
    context: 'organizations.workbench.tabs.context'
  };
  tabs.innerHTML = allowed.map(function(name) {
    var active = name === tab;
    return '<button onclick="_orgSetTab(\'' + name + '\')" style="padding:9px 14px;border:none;border-bottom:2px solid ' +
      (active ? 'var(--a)' : 'transparent') + ';background:transparent;color:' + (active ? 'var(--a)' : 'var(--t2)') +
      ';cursor:pointer;font-size:13px;font-weight:600;white-space:nowrap">' + I18N.t(labels[name]) + '</button>';
  }).join('');
  if (tab === 'overview') _orgPanel(_orgOverviewPanel());
  else if (tab === 'repos') { _orgPanel('<div style="padding:24px;color:var(--t3)">' + I18N.t("organizations.loading") + '</div>'); _orgpLoadRepos(); }
  else if (tab === 'demands') { _orgPanel('<div style="padding:24px;color:var(--t3)">' + I18N.t("organizations.loading") + '</div>'); _loadOrgDemands(); }
  else if (tab === 'dispatches') {
    if (dispatchPreset) {
      _orgWorkbench.dispatch = dispatchPreset;
      _orgPanel(_renderOrgDispatchDetail(dispatchPreset));
    } else {
      _orgPanel('<div style="padding:24px;color:var(--t3)">' + I18N.t("organizations.loading") + '</div>');
      _loadOrgDispatches();
    }
  }
  else if (tab === 'context') {
    // Issue #2236：context 占位页替换为「组织上下文 + 知识库」双子页
    _loadOrgContext();
  }
  else _orgPanel('<div style="border:1px dashed var(--b);border-radius:12px;padding:44px 20px;text-align:center;color:var(--t3)">' +
    '<div style="font-size:15px;font-weight:600;color:var(--t2);margin-bottom:6px">' + I18N.t("organizations.workbench.context_placeholder") + '</div>' +
    '<div style="font-size:12px">' + I18N.t("organizations.workbench.context_hint") + '</div></div>');
}

// ===== Issue #2236：组织上下文 + 知识库 =====

function _orgCanEditContent(org) {
  // org 详情仅返回 owner/admin/member；组织仓库 repo admin（maintainer）写权限由后端
  // `_require_writer` 兜底，前端 403 时降级只读（不新增 capability 接口）。
  return _orgCanWrite((org || {}).role);
}

function _orgCanPublish(org) {
  // 发布/废弃/归档 = org owner/admin（与 #2150 路由 `_require_admin` 一致）
  return _orgCanWrite((org || {}).role);
}

function _orgLifecycle(status) {
  var s = String(status || 'draft').toLowerCase();
  return ['draft', 'active', 'deprecated', 'archived'].indexOf(s) >= 0 ? s : 'draft';
}

function _orgLifecycleBadge(status) {
  var s = _orgLifecycle(status);
  var palette = {
    draft: 'background:#f3f4f6;color:#5f6b7a;border-color:#d8dde5',
    active: 'background:#e8f5e9;color:#2e7d32;border-color:#b7dfb9',
    deprecated: 'background:#fff4e5;color:#b45309;border-color:#f4ce8e',
    archived: 'background:#eef1f5;color:#5f6b7a;border-color:#d4dae2'
  };
  var key = 'organizations.lifecycle.' + s;
  var label = I18N.t(key);
  if (!label || label === key) label = s;
  return '<span class="org-life-' + s + '" style="display:inline-block;padding:1px 8px;border:1px solid;border-radius:999px;font-size:11px;font-weight:600;' +
    palette[s] + '">' + _orgpEsc(label) + '</span>';
}

function _orgExpiredBadge(labelKey) {
  return '<span style="display:inline-block;margin-left:6px;padding:1px 8px;border:1px solid #f4ce8e;border-radius:999px;background:#fff4e5;color:#b45309;font-size:11px">' +
    I18N.t(labelKey || 'organizations.knowledge.expired') + '</span>';
}

function _orgToday(date) {
  var d = date instanceof Date ? date : new Date();
  var pad = function(n) { return n < 10 ? '0' + n : String(n); };
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

function _orgIsExpired(validUntil, today) {
  var value = String(validUntil || '').slice(0, 10);
  if (!value) return false;
  return value < (today || _orgToday());
}

function _orgUtf8Bytes(text) {
  var s = text === null || text === undefined ? '' : String(text);
  try {
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(s).length;
  } catch (e) { /* 降级到手工统计 */ }
  var bytes = 0;
  for (var i = 0; i < s.length; i++) {
    var c = s.charCodeAt(i);
    if (c < 0x80) bytes += 1;
    else if (c < 0x800) bytes += 2;
    else if (c >= 0xD800 && c <= 0xDBFF) { bytes += 4; i++; }
    else bytes += 3;
  }
  return bytes;
}

function _orgFormatBytes(n) {
  var bytes = Number(n || 0);
  if (!isFinite(bytes) || bytes < 0) bytes = 0;
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (Math.round(bytes / 102.4) / 10) + ' KB';
  return (Math.round(bytes / (1024 * 1024) * 10) / 10) + ' MB';
}

function _orgFmtTime(value) {
  if (!value) return '—';
  var s = String(value);
  // 后端 org_context/org_knowledge 时间为服务器本地时间（无时区），仅在 ISO/带偏移时转换，
  // 避免把本地时间当 UTC 再 +8 显示。
  if (s.indexOf('T') >= 0 || /Z$|[+-]\d{2}:\d{2}$/.test(s)) {
    try {
      if (typeof fmtLocalTime === 'function') return fmtLocalTime(value);
    } catch (e) { /* 保持原值 */ }
  }
  return s;
}

function _orgUserName(ownerId) {
  // D105：user id 为 uuid7 字符串，不能用 Number() 比较（否则 UUID 全变 0/NaN）。
  var id = String(ownerId === null || ownerId === undefined ? '' : ownerId).trim();
  if (!id || id === '0') return '—';
  var members = _orgWorkbench.members || [];
  for (var i = 0; i < members.length; i++) {
    var memberId = String(members[i].user_id === null || members[i].user_id === undefined
      ? '' : members[i].user_id).trim();
    if (memberId === id) {
      return members[i].nickname || members[i].username || ('#' + id);
    }
  }
  return '#' + id;
}

function _orgFindContext(ctxId) {
  var id = String(ctxId || '');
  var items = _orgWorkbench.contexts || [];
  for (var i = 0; i < items.length; i++) {
    if (String(items[i].id || '') === id) return items[i];
  }
  return null;
}

function _orgFindKnowledge(knowledgeId) {
  var id = Number(knowledgeId || 0);
  var items = _orgWorkbench.knowledge || [];
  for (var i = 0; i < items.length; i++) {
    if (Number(items[i].id || 0) === id) return items[i];
  }
  return null;
}

function _orgSmallBtn(label, onclick, color) {
  return '<button onclick="' + onclick + '" style="padding:4px 9px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:' +
    (color || 'var(--a)') + ';cursor:pointer;font-size:11px;margin-right:6px">' + _orgpEsc(label) + '</button>';
}

function _orgReadonlyHint(key) {
  return '<span style="font-size:11px;color:var(--t3)">' + I18N.t(key) + '</span>';
}

function _orgContextActions(ctx, org) {
  var status = _orgLifecycle(ctx.lifecycle_status);
  var id = String(ctx.id || '');
  var arg = "'" + id + "'";
  var html = '';
  if (_orgCanEditContent(org) && status !== 'deprecated') {
    html += _orgSmallBtn(I18N.t('common.edit'), '_orgOpenContextEditor(' + arg + ')');
  }
  if (_orgCanPublish(org) && status === 'draft') {
    html += _orgSmallBtn(I18N.t('organizations.context.publish'), '_orgConfirmPublishContext(' + arg + ')', 'var(--g)');
  }
  if (_orgCanPublish(org) && status === 'active') {
    html += _orgSmallBtn(I18N.t('organizations.context.deprecate'), '_orgConfirmDeprecateContext(' + arg + ')', 'var(--r)');
  }
  return html || _orgReadonlyHint('organizations.context.readonly');
}

function _orgKnowledgeActions(item, org) {
  var status = _orgLifecycle(item.lifecycle_status);
  var id = Number(item.id || 0);
  var html = '';
  var editable = status !== 'deprecated' && status !== 'archived';
  if (_orgCanEditContent(org) && editable) {
    html += _orgSmallBtn(I18N.t('common.edit'), '_orgOpenKnowledgeEditor(' + id + ')');
  }
  if (_orgCanPublish(org) && status === 'draft') {
    html += _orgSmallBtn(I18N.t('organizations.knowledge.publish'), '_orgConfirmPublishKnowledge(' + id + ')', 'var(--g)');
  }
  if (_orgCanPublish(org) && status === 'active') {
    html += _orgSmallBtn(I18N.t('organizations.knowledge.deprecate'), '_orgConfirmDeprecateKnowledge(' + id + ')', 'var(--r)');
  }
  if (_orgCanPublish(org) && (status === 'draft' || status === 'active')) {
    html += _orgSmallBtn(I18N.t('organizations.knowledge.archive'), '_orgConfirmArchiveKnowledge(' + id + ')', 'var(--t2)');
  }
  return html || _orgReadonlyHint('organizations.knowledge.readonly');
}

function _orgParseContentTab(hash) {
  var raw = String(hash || '');
  var queryMatch = raw.match(/[?&]tab=([A-Za-z]+)/);
  if (queryMatch) {
    var value = queryMatch[1].toLowerCase();
    if (value === 'context' || value === 'knowledge') return value;
  }
  var parts = raw.replace(/^#/, '').split('/');
  if (parts.length >= 3) {
    var sub = String(parts[2] || '').toLowerCase();
    if (sub === 'context' || sub === 'knowledge') return sub;
  }
  return '';
}

function _orgHashForContentTab(orgId, sub) {
  var tab = sub === 'knowledge' ? 'knowledge' : 'context';
  return '#organizations/' + encodeURIComponent(String(orgId)) + '/' + tab;
}

function _orgSyncContentTabHash(sub) {
  var org = _orgWorkbench.org || {};
  if (!org.id) return;
  try {
    if (window.history && window.history.replaceState) {
      window.history.replaceState(window.history.state, '', _orgHashForContentTab(org.id, sub));
    }
  } catch (e) { /* 深链同步失败不影响页面 */ }
}

function _orgClearContentTabHash() {
  var org = _orgWorkbench.org || {};
  if (!org.id) return;
  try {
    if (window.history && window.history.replaceState) {
      window.history.replaceState(window.history.state, '', '#organizations/' + encodeURIComponent(String(org.id)));
    }
  } catch (e) { /* ignore */ }
}

function _orgContentShell(loading) {
  var sub = _orgWorkbench.contentTab === 'knowledge' ? 'knowledge' : 'context';
  var buttons = [
    ['context', 'organizations.context.subtab_context'],
    ['knowledge', 'organizations.context.subtab_knowledge']
  ].map(function(pair) {
    var active = pair[0] === sub;
    return '<button onclick="_orgSetContentTab(\'' + pair[0] + '\')" style="padding:7px 12px;border:1px solid ' +
      (active ? 'var(--a)' : 'var(--b)') + ';border-radius:8px;background:' + (active ? 'rgba(59,130,246,.08)' : 'var(--c)') +
      ';color:' + (active ? 'var(--a)' : 'var(--t2)') + ';cursor:pointer;font-size:12px;font-weight:600">' +
      I18N.t(pair[1]) + '</button>';
  }).join('');
  return '<div style="display:flex;gap:8px;margin-bottom:12px">' + buttons + '</div>' +
    '<div id="orgContentBody">' + (loading ? '<div style="padding:24px;color:var(--t3)">' +
      I18N.t('organizations.loading') + '</div>' : '') + '</div>';
}

function _orgSetContentTab(sub) {
  _orgWorkbench.contentTab = sub === 'knowledge' ? 'knowledge' : 'context';
  _orgWorkbench.activeTab = 'context';
  _orgSyncContentTabHash(_orgWorkbench.contentTab);
  _orgPanel(_orgContentShell(false));
  _renderContentTab();
}

function _loadOrgContext() {
  var org = _orgWorkbench.org || {};
  if (!org.id) return;
  _orgWorkbench.activeTab = 'context';
  _orgPanel(_orgContentShell(true));
  var contextsReq = API.orgContext ? API.orgContext.list(org.id) : API.get('/orgs/' + org.id + '/context');
  Promise.all([
    contextsReq.then(function(d) {
      _orgWorkbench.contexts = (d && d.contexts) || [];
      _orgWorkbench.contextsError = '';
    }).catch(function(e) {
      _orgWorkbench.contexts = [];
      _orgWorkbench.contextsError = _orgErrorText(e);
    }),
    _orgFetchOrgKnowledge()
  ]).then(function() {
    _renderContentTab();
  });
}

function _renderContentTab() {
  var body = document.getElementById('orgContentBody');
  if (!body) return;
  body.innerHTML = _orgWorkbench.contentTab === 'knowledge' ? _renderKnowledgeBody() : _renderContextBody();
}

function _renderContextBody() {
  var org = _orgWorkbench.org || {};
  var html = '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap">' +
    '<div><div style="font-size:15px;font-weight:600;color:var(--t1)">' + I18N.t('organizations.context.title') + '</div>' +
    '<div style="font-size:12px;color:var(--t3);margin-top:3px">' + I18N.t('organizations.context.subtitle') + '</div></div>';
  if (_orgCanEditContent(org)) {
    html += '<button onclick="_orgOpenContextEditor(0)" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' +
      I18N.t('organizations.context.create') + '</button>';
  }
  html += '</div>';
  if (_orgWorkbench.contextsError) {
    return html + '<div style="color:var(--r);padding:16px">' + _orgpEsc(_orgWorkbench.contextsError) + '</div>';
  }
  ['brief', 'policy', 'decision'].forEach(function(kind) {
    html += _orgContextSection(kind);
  });
  return html;
}

function _orgContextSection(kind) {
  var org = _orgWorkbench.org || {};
  var items = (_orgWorkbench.contexts || []).filter(function(item) {
    return String(item.kind || '') === kind;
  });
  var label = I18N.t('organizations.context.kind_' + kind);
  var html = '<div style="border:1px solid var(--b);border-radius:12px;padding:14px;margin-bottom:12px">' +
    '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px">' +
    '<div style="font-size:13px;font-weight:600;color:var(--t1)">' + label +
    ' <span style="font-size:11px;color:var(--t3);font-weight:400">(' + items.length + ')</span></div>';
  if (_orgCanEditContent(org)) {
    html += '<button onclick="_orgOpenContextEditor(0,\'' + kind + '\')" style="padding:5px 10px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--a);cursor:pointer;font-size:11px">' +
      I18N.t('organizations.context.create') + '</button>';
  }
  html += '</div>';
  if (!items.length) {
    html += '<div style="padding:16px;text-align:center;color:var(--t3);font-size:12px">' +
      I18N.t('organizations.context.empty', {kind: label}) + '</div>';
  } else {
    html += items.map(_orgContextRow).join('');
  }
  return html + '</div>';
}

function _orgContextRow(ctx) {
  var org = _orgWorkbench.org || {};
  var status = _orgLifecycle(ctx.lifecycle_status);
  var expired = _orgIsExpired(ctx.valid_until);
  return '<div style="border-top:1px solid var(--b);padding:10px 0">' +
    '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px;flex-wrap:wrap">' +
    '<div style="min-width:0;flex:1">' +
    '<div style="font-size:13px;font-weight:600;color:var(--t1);word-break:break-word">' +
    _orgpEsc(ctx.title || '') + ' ' + _orgLifecycleBadge(status) +
    (expired ? _orgExpiredBadge('organizations.context.expired') : '') + '</div>' +
    '<div style="font-size:11px;color:var(--t3);margin-top:4px;display:flex;gap:12px;flex-wrap:wrap">' +
    '<span>' + I18N.t('organizations.context.version') + ' v' + Number(ctx.version || 1) + '</span>' +
    '<span>' + I18N.t('organizations.context.owner') + ': ' + _orgpEsc(_orgUserName(ctx.owner_id)) + '</span>' +
    '<span>' + I18N.t('organizations.context.updated_at') + ': ' + _orgpEsc(_orgFmtTime(ctx.updated_at)) + '</span>' +
    (ctx.valid_until ? '<span>' + I18N.t('organizations.context.valid_until_label') + ': ' +
      _orgpEsc(String(ctx.valid_until).slice(0, 10)) + '</span>' : '') +
    '</div></div>' +
    '<div style="display:flex;flex-wrap:wrap">' + _orgContextActions(ctx, org) + '</div>' +
    '</div></div>';
}

function _renderKnowledgeBody() {
  var org = _orgWorkbench.org || {};
  var filters = _orgWorkbench.knowledgeFilters || {};
  var statusOptions = ['draft', 'active', 'deprecated', 'archived'].map(function(status) {
    return '<option value="' + status + '"' + (filters.status === status ? ' selected' : '') + '>' +
      I18N.t('organizations.lifecycle.' + status) + '</option>';
  }).join('');
  var inputStyle = 'padding:7px 9px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:12px;box-sizing:border-box';
  var html = '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap">' +
    '<div><div style="font-size:15px;font-weight:600;color:var(--t1)">' + I18N.t('organizations.knowledge.title') + '</div>' +
    '<div style="font-size:12px;color:var(--t3);margin-top:3px">' + I18N.t('organizations.knowledge.subtitle') + '</div></div>';
  if (_orgCanEditContent(org)) {
    html += '<button onclick="_orgOpenKnowledgeEditor(0)" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' +
      I18N.t('organizations.knowledge.create') + '</button>';
  }
  html += '</div>' +
    '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px">' +
    '<input id="orgKnowledgeKindFilter" type="text" value="' + _orgpEsc(filters.kind || '') + '" placeholder="' +
    _orgpEsc(I18N.t('organizations.knowledge.filter_kind')) + '" onchange="_orgApplyKnowledgeFilters()" style="width:120px;' + inputStyle + '">' +
    '<select id="orgKnowledgeStatusFilter" onchange="_orgApplyKnowledgeFilters()" style="' + inputStyle + '">' +
    '<option value=""' + (filters.status ? '' : ' selected') + '>' + I18N.t('organizations.knowledge.filter_all') + '</option>' +
    statusOptions + '</select>' +
    '<input id="orgKnowledgeTagFilter" type="text" value="' + _orgpEsc(filters.tag || '') + '" placeholder="' +
    _orgpEsc(I18N.t('organizations.knowledge.filter_tag_placeholder')) + '" onchange="_orgApplyKnowledgeFilters()" style="width:150px;' + inputStyle + '">' +
    '<input id="orgKnowledgeQueryFilter" type="text" value="' + _orgpEsc(filters.q || '') + '" placeholder="' +
    _orgpEsc(I18N.t('organizations.knowledge.filter_q_placeholder')) + '" oninput="_orgOnKnowledgeQueryInput()" style="width:200px;' + inputStyle + '">' +
    '<button onclick="_orgApplyKnowledgeFilters()" style="padding:7px 12px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' +
    I18N.t('common.refresh') + '</button></div>' +
    '<div id="orgKnowledgeList">' + _orgKnowledgeListHtml() + '</div>';
  return html;
}

function _orgKnowledgeListHtml() {
  if (_orgWorkbench.knowledgeError) {
    return '<div style="color:var(--r);padding:16px">' + _orgpEsc(_orgWorkbench.knowledgeError) + '</div>';
  }
  var items = _orgWorkbench.knowledge || [];
  if (!items.length) {
    return '<div style="border:1px dashed var(--b);border-radius:12px;padding:34px;text-align:center;color:var(--t3)">' +
      I18N.t('organizations.knowledge.empty') + '</div>';
  }
  return items.map(_orgKnowledgeRow).join('');
}

function _orgKnowledgeRow(item) {
  var org = _orgWorkbench.org || {};
  var status = _orgLifecycle(item.lifecycle_status);
  var expired = _orgIsExpired(item.valid_until);
  var tags = (item.tags || []).map(function(tag) {
    return '<span style="padding:1px 7px;border-radius:999px;background:var(--bg);color:var(--t2);font-size:10px">' + _orgpEsc(tag) + '</span>';
  }).join('');
  return '<div style="border:1px solid ' + (expired ? '#f4ce8e' : 'var(--b)') + ';border-radius:12px;padding:12px;margin-bottom:10px;background:' +
    (expired ? '#fffdf7' : 'var(--c)') + '">' +
    '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px;flex-wrap:wrap">' +
    '<div style="min-width:0;flex:1">' +
    '<div style="font-size:13px;font-weight:600;color:var(--t1);word-break:break-word">' + _orgpEsc(item.name || '') + ' ' +
    _orgLifecycleBadge(status) + (expired ? _orgExpiredBadge('organizations.knowledge.expired') : '') + '</div>' +
    (item.description ? '<div style="font-size:11px;color:var(--t3);margin-top:3px;word-break:break-word">' +
      _orgpEsc(item.description) + '</div>' : '') +
    '<div style="font-size:11px;color:var(--t3);margin-top:5px;display:flex;gap:12px;flex-wrap:wrap">' +
    '<span>' + I18N.t('organizations.knowledge.kind_label') + ': ' + _orgpEsc(item.kind || '—') + '</span>' +
    '<span>' + I18N.t('organizations.knowledge.owner') + ': ' + _orgpEsc(_orgUserName(item.owner_id)) + '</span>' +
    '<span>' + I18N.t('organizations.knowledge.valid_until_label') + ': ' +
    _orgpEsc(String(item.valid_until || '').slice(0, 10) || '—') + '</span>' +
    '<span>' + I18N.t('organizations.knowledge.last_reviewed_at') + ': ' + _orgpEsc(_orgFmtTime(item.last_reviewed_at)) + '</span>' +
    '<span>' + I18N.t('organizations.context.version') + ' v' + Number(item.version || 1) + '</span>' +
    '</div>' +
    (tags ? '<div style="margin-top:6px;display:flex;gap:5px;flex-wrap:wrap">' + tags + '</div>' : '') +
    '</div>' +
    '<div style="display:flex;flex-wrap:wrap">' + _orgKnowledgeActions(item, org) + '</div>' +
    '</div></div>';
}

function _orgFetchOrgKnowledge() {
  var org = _orgWorkbench.org || {};
  var request = API.orgKnowledge
    ? API.orgKnowledge.list(org.id, _orgWorkbench.knowledgeFilters)
    : API.get('/orgs/' + org.id + '/knowledge');
  return request.then(function(d) {
    _orgWorkbench.knowledge = (d && d.knowledge) || [];
    _orgWorkbench.knowledgeError = '';
  }).catch(function(e) {
    _orgWorkbench.knowledge = [];
    _orgWorkbench.knowledgeError = _orgErrorText(e);
  });
}

function _orgRenderKnowledgeListOnly() {
  var list = document.getElementById('orgKnowledgeList');
  if (list) list.innerHTML = _orgKnowledgeListHtml();
}

var _orgKnowledgeQueryTimer = null;
function _orgOnKnowledgeQueryInput() {
  clearTimeout(_orgKnowledgeQueryTimer);
  _orgKnowledgeQueryTimer = setTimeout(function() {
    _orgApplyKnowledgeFilters();
  }, 300);
}

function _orgApplyKnowledgeFilters() {
  function read(id) {
    var el = document.getElementById(id);
    return el ? String(el.value || '').trim() : '';
  }
  _orgWorkbench.knowledgeFilters = {
    kind: read('orgKnowledgeKindFilter'),
    status: read('orgKnowledgeStatusFilter'),
    tag: read('orgKnowledgeTagFilter'),
    q: read('orgKnowledgeQueryFilter')
  };
  _orgFetchOrgKnowledge().then(_orgRenderKnowledgeListOnly);
}

function _orgReloadContexts() {
  var org = _orgWorkbench.org || {};
  var request = API.orgContext ? API.orgContext.list(org.id) : API.get('/orgs/' + org.id + '/context');
  return request.then(function(d) {
    _orgWorkbench.contexts = (d && d.contexts) || [];
    _orgWorkbench.contextsError = '';
  }).catch(function(e) {
    _orgWorkbench.contextsError = _orgErrorText(e);
  });
}

function _orgReloadKnowledge() {
  return _orgFetchOrgKnowledge().then(_orgRenderKnowledgeListOnly);
}

function _orgMemberOptions(selectedId) {
  var members = _orgWorkbench.members || [];
  var current = String(selectedId === null || selectedId === undefined ? '' : selectedId).trim();
  var html = '<option value=""' + (current && current !== '0' ? '' : ' selected') + '>' +
    I18N.t('organizations.context.owner_any') + '</option>';
  members.forEach(function(member) {
    var id = String(member.user_id === null || member.user_id === undefined
      ? '' : member.user_id).trim();
    if (!id || id === '0') return;
    html += '<option value="' + _orgpEsc(id) + '"' + (id === current ? ' selected' : '') + '>' +
      _orgpEsc(member.nickname || member.username || ('#' + id)) + '</option>';
  });
  return html;
}

function _orgUpdateContentSize(inputId, labelId) {
  var input = document.getElementById(inputId);
  var label = document.getElementById(labelId);
  if (!input || !label) return;
  var bytes = _orgUtf8Bytes(input.value || '');
  label.textContent = I18N.t('organizations.context.content_size', {
    size: _orgFormatBytes(bytes), limit: _orgFormatBytes(ORG_CONTENT_LIMIT_BYTES)
  });
}

function _orgOpenContextEditor(ctxId, kind) {
  var org = _orgWorkbench.org || {};
  if (!_orgCanEditContent(org)) {
    app.showToast(I18N.t('organizations.context.readonly'));
    return;
  }
  var ctx = ctxId ? _orgFindContext(ctxId) : null;
  var currentKind = (ctx && ctx.kind) || kind || 'brief';
  var isEdit = !!ctx;
  var inputStyle = 'width:100%;padding:8px 10px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:12px;box-sizing:border-box';
  var kindOptions = ['brief', 'policy', 'decision'].map(function(k) {
    return '<option value="' + k + '"' + (k === currentKind ? ' selected' : '') + '>' +
      I18N.t('organizations.context.kind_' + k) + '</option>';
  }).join('');
  _orgModal('orgContextEditorOverlay',
    '<div style="font-size:15px;font-weight:600;color:var(--t1);margin-bottom:14px">' +
    I18N.t(isEdit ? 'organizations.context.edit_title' : 'organizations.context.create_title') + '</div>' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t('organizations.context.kind_label') + '</label>' +
    '<select id="orgCtxKind" ' + (isEdit ? 'disabled' : '') + ' onchange="_orgOnContextKindChange()" style="' + inputStyle + 'margin-bottom:10px">' + kindOptions + '</select>' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t('organizations.context.key_label') + '</label>' +
    '<input id="orgCtxKey" type="text" value="' + _orgpEsc(ctx ? (ctx.ctx_key || '') : '') + '" placeholder="' +
    _orgpEsc(I18N.t('organizations.context.key_placeholder')) + '" style="' + inputStyle + 'margin-bottom:10px">' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t('organizations.context.title_label') + '</label>' +
    '<input id="orgCtxTitle" type="text" value="' + _orgpEsc(ctx ? (ctx.title || '') : '') + '" style="' + inputStyle + 'margin-bottom:10px">' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t('organizations.context.content_label') + '</label>' +
    '<textarea id="orgCtxContent" rows="8" oninput="_orgUpdateContentSize(\'orgCtxContent\',\'orgCtxSize\')" style="' + inputStyle + 'resize:vertical;margin-bottom:4px">' +
    _orgpEsc(ctx ? (ctx.content || '') : '') + '</textarea>' +
    '<div id="orgCtxSize" style="font-size:11px;color:var(--t3);margin-bottom:10px"></div>' +
    '<div style="display:flex;gap:10px;margin-bottom:10px">' +
    '<div style="flex:1"><label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' +
    I18N.t('organizations.context.owner_label') + '</label>' +
    '<select id="orgCtxOwner" style="' + inputStyle + '">' + _orgMemberOptions(ctx ? ctx.owner_id : 0) + '</select></div>' +
    '<div style="flex:1"><label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' +
    I18N.t('organizations.context.valid_until_label') + '</label>' +
    '<input id="orgCtxValidUntil" type="date" value="' + _orgpEsc(String((ctx && ctx.valid_until) || '').slice(0, 10)) + '" style="' + inputStyle + '"></div>' +
    '</div>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px">' +
    '<button onclick="document.getElementById(\'orgContextEditorOverlay\').remove()" style="padding:8px 14px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' +
    I18N.t('common.cancel') + '</button>' +
    '<button id="orgCtxSave" onclick="_orgSaveContext(' + (isEdit ? "'" + String(ctx.id || '') + "'" : "''") + ')" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' +
    I18N.t(isEdit ? 'organizations.context.save' : 'organizations.context.create_confirm') + '</button></div>',
    {width: 640});
  _orgUpdateContentSize('orgCtxContent', 'orgCtxSize');
  _orgOnContextKindChange();
}

function _orgOnContextKindChange() {
  var kindEl = document.getElementById('orgCtxKind');
  var keyEl = document.getElementById('orgCtxKey');
  if (!kindEl || !keyEl) return;
  if (kindEl.value === 'brief') {
    keyEl.value = 'brief';
    keyEl.disabled = true;
  } else if (!keyEl.value || keyEl.value === 'brief') {
    keyEl.disabled = false;
    keyEl.value = 'default';
  } else {
    keyEl.disabled = false;
  }
}

function _orgSaveContext(ctxId) {
  var org = _orgWorkbench.org || {};
  if (!_orgCanEditContent(org)) {
    app.showToast(I18N.t('organizations.context.readonly'));
    return;
  }
  function value(id) {
    var el = document.getElementById(id);
    return el ? String(el.value || '').trim() : '';
  }
  var title = value('orgCtxTitle');
  var content = document.getElementById('orgCtxContent') ? document.getElementById('orgCtxContent').value : '';
  if (!title) { app.showToast(I18N.t('organizations.context.title_required')); return; }
  if (!content.trim()) { app.showToast(I18N.t('organizations.context.content_required')); return; }
  if (_orgUtf8Bytes(content) > ORG_CONTENT_LIMIT_BYTES) {
    app.showToast(I18N.t('organizations.errors.content_too_large'));
    return;
  }
  var payload = {
    title: title,
    content: content,
    owner_id: value('orgCtxOwner'),
    valid_until: value('orgCtxValidUntil')
  };
  var editId = String(ctxId || '');
  if (!editId) {
    payload.kind = value('orgCtxKind') || 'brief';
    payload.key = value('orgCtxKey') || '';
  }
  var button = document.getElementById('orgCtxSave');
  if (button) { button.disabled = true; button.textContent = I18N.t('organizations.context.saving'); }
  var request = editId
    ? API.orgContext.update(org.id, editId, payload)
    : API.orgContext.create(org.id, payload);
  request.then(function() {
    var overlay = document.getElementById('orgContextEditorOverlay');
    if (overlay) overlay.remove();
    app.showToast(I18N.t('organizations.context.save_success'));
    _orgReloadContexts().then(_renderContentTab);
  }).catch(function(e) {
    app.showToast(_orgErrorText(e));
    if (button) {
      button.disabled = false;
      button.textContent = I18N.t(editId ? 'organizations.context.save' : 'organizations.context.create_confirm');
    }
  });
}

function _orgConfirmPublishContext(ctxId) {
  var org = _orgWorkbench.org || {};
  var ctx = _orgFindContext(ctxId);
  if (!ctx || !_orgCanPublish(org)) {
    app.showToast(I18N.t('organizations.context.readonly'));
    return;
  }
  app.showConfirm(I18N.t('organizations.context.publish_confirm', {title: _orgpEsc(ctx.title || '')}), function() {
    API.orgContext.publish(org.id, String(ctxId)).then(function() {
      app.showToast(I18N.t('organizations.context.publish_success'));
      _orgReloadContexts().then(_renderContentTab);
    }).catch(function(e) {
      app.showToast(_orgErrorText(e));
    });
  }, {confirmText: I18N.t('organizations.context.publish')});
}

function _orgConfirmDeprecateContext(ctxId) {
  var org = _orgWorkbench.org || {};
  var ctx = _orgFindContext(ctxId);
  if (!ctx || !_orgCanPublish(org)) {
    app.showToast(I18N.t('organizations.context.readonly'));
    return;
  }
  app.showConfirm(I18N.t('organizations.context.deprecate_confirm', {title: _orgpEsc(ctx.title || '')}), function() {
    API.orgContext.deprecate(org.id, String(ctxId)).then(function() {
      app.showToast(I18N.t('organizations.context.deprecate_success'));
      _orgReloadContexts().then(_renderContentTab);
    }).catch(function(e) {
      app.showToast(_orgErrorText(e));
    });
  }, {danger: true, confirmText: I18N.t('organizations.context.deprecate')});
}

function _orgOpenKnowledgeEditor(knowledgeId) {
  var org = _orgWorkbench.org || {};
  if (!_orgCanEditContent(org)) {
    app.showToast(I18N.t('organizations.knowledge.readonly'));
    return;
  }
  var item = knowledgeId ? _orgFindKnowledge(knowledgeId) : null;
  var isEdit = !!item;
  var inputStyle = 'width:100%;padding:8px 10px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:12px;box-sizing:border-box';
  _orgModal('orgKnowledgeEditorOverlay',
    '<div style="font-size:15px;font-weight:600;color:var(--t1);margin-bottom:14px">' +
    I18N.t(isEdit ? 'organizations.knowledge.edit_title' : 'organizations.knowledge.create_title') + '</div>' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t('organizations.knowledge.name_label') + '</label>' +
    '<input id="orgKnName" type="text" value="' + _orgpEsc(item ? (item.name || '') : '') + '" style="' + inputStyle + 'margin-bottom:10px">' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t('organizations.knowledge.description_label') + '</label>' +
    '<input id="orgKnDescription" type="text" value="' + _orgpEsc(item ? (item.description || '') : '') + '" style="' + inputStyle + 'margin-bottom:10px">' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t('organizations.knowledge.content_label') + '</label>' +
    '<textarea id="orgKnContent" rows="7" oninput="_orgUpdateContentSize(\'orgKnContent\',\'orgKnSize\')" style="' + inputStyle + 'resize:vertical;margin-bottom:4px">' +
    _orgpEsc(item ? (item.content || '') : '') + '</textarea>' +
    '<div id="orgKnSize" style="font-size:11px;color:var(--t3);margin-bottom:10px"></div>' +
    '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px">' +
    '<div style="flex:1;min-width:140px"><label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' +
    I18N.t('organizations.knowledge.kind_label') + '</label>' +
    '<input id="orgKnKind" type="text" value="' + _orgpEsc(item ? (item.kind || '') : '') + '"' +
    (isEdit ? ' disabled title="' + _orgpEsc(I18N.t('organizations.knowledge.kind_label')) + '"' : '') +
    ' style="' + inputStyle + '"></div>' +
    '<div style="flex:1;min-width:140px"><label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' +
    I18N.t('organizations.knowledge.applies_to_label') + '</label>' +
    '<input id="orgKnAppliesTo" type="text" value="' + _orgpEsc(item ? (item.applies_to || '') : '') + '" style="' + inputStyle + '"></div>' +
    '</div>' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t('organizations.knowledge.tags_label') + '</label>' +
    '<input id="orgKnTags" type="text" value="' + _orgpEsc(item ? (item.tags || []).join(', ') : '') + '" placeholder="' +
    _orgpEsc(I18N.t('organizations.knowledge.tags_placeholder')) + '" style="' + inputStyle + 'margin-bottom:10px">' +
    '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">' +
    '<div style="flex:1;min-width:140px"><label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' +
    I18N.t('organizations.knowledge.valid_until_label') + '</label>' +
    '<input id="orgKnValidUntil" type="date" value="' + _orgpEsc(String((item && item.valid_until) || '').slice(0, 10)) + '" style="' + inputStyle + '"></div>' +
    '<div style="flex:1;min-width:140px"><label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' +
    I18N.t('organizations.knowledge.review_interval_label') + '</label>' +
    '<input id="orgKnReviewInterval" type="number" min="0" value="' + Number((item && item.review_interval_days) || 0) + '" style="' + inputStyle + '"></div>' +
    '</div>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px">' +
    '<button onclick="document.getElementById(\'orgKnowledgeEditorOverlay\').remove()" style="padding:8px 14px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' +
    I18N.t('common.cancel') + '</button>' +
    '<button id="orgKnSave" onclick="_orgSaveKnowledge(' + (isEdit ? Number(item.id) : 0) + ')" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' +
    I18N.t(isEdit ? 'common.save' : 'organizations.knowledge.create') + '</button></div>',
    {width: 640});
  _orgUpdateContentSize('orgKnContent', 'orgKnSize');
}

function _orgSaveKnowledge(knowledgeId) {
  var org = _orgWorkbench.org || {};
  if (!_orgCanEditContent(org)) {
    app.showToast(I18N.t('organizations.knowledge.readonly'));
    return;
  }
  function value(id) {
    var el = document.getElementById(id);
    return el ? String(el.value || '').trim() : '';
  }
  var contentEl = document.getElementById('orgKnContent');
  var content = contentEl ? contentEl.value : '';
  var name = value('orgKnName');
  if (!name) { app.showToast(I18N.t('organizations.knowledge.name_required')); return; }
  if (!content.trim()) { app.showToast(I18N.t('organizations.knowledge.content_required')); return; }
  if (_orgUtf8Bytes(content) > ORG_CONTENT_LIMIT_BYTES) {
    app.showToast(I18N.t('organizations.errors.content_too_large'));
    return;
  }
  var tags = value('orgKnTags').split(',').map(function(tag) { return tag.trim(); }).filter(function(tag) { return !!tag; });
  var payload = {
    name: name,
    description: value('orgKnDescription'),
    content: content,
    tags: tags,
    applies_to: value('orgKnAppliesTo'),
    valid_until: value('orgKnValidUntil'),
    review_interval_days: Number(value('orgKnReviewInterval')) || 0
  };
  // 后端 KnowledgeUpdateBody 不含 kind（类型不可变更），仅创建时提交
  if (!Number(knowledgeId || 0)) payload.kind = value('orgKnKind');
  var button = document.getElementById('orgKnSave');
  if (button) { button.disabled = true; button.textContent = I18N.t('organizations.context.saving'); }
  var request = Number(knowledgeId || 0)
    ? API.orgKnowledge.update(org.id, Number(knowledgeId), payload)
    : API.orgKnowledge.create(org.id, payload);
  request.then(function() {
    var overlay = document.getElementById('orgKnowledgeEditorOverlay');
    if (overlay) overlay.remove();
    app.showToast(I18N.t('organizations.knowledge.save_success'));
    var cached = _orgWorkbench.knowledgeContentCache || {};
    if (Number(knowledgeId || 0)) delete cached[Number(knowledgeId)];
    _orgReloadKnowledge();
  }).catch(function(e) {
    app.showToast(_orgErrorText(e));
    if (button) {
      button.disabled = false;
      button.textContent = I18N.t(Number(knowledgeId || 0) ? 'common.save' : 'organizations.knowledge.create');
    }
  });
}

function _orgConfirmPublishKnowledge(knowledgeId) {
  var org = _orgWorkbench.org || {};
  var item = _orgFindKnowledge(knowledgeId);
  if (!item || !_orgCanPublish(org)) {
    app.showToast(I18N.t('organizations.knowledge.readonly'));
    return;
  }
  app.showConfirm(I18N.t('organizations.knowledge.publish_confirm', {name: _orgpEsc(item.name || '')}), function() {
    API.orgKnowledge.publish(org.id, Number(knowledgeId)).then(function() {
      app.showToast(I18N.t('organizations.knowledge.publish_success'));
      _orgReloadKnowledge();
    }).catch(function(e) {
      app.showToast(_orgErrorText(e));
    });
  }, {confirmText: I18N.t('organizations.knowledge.publish')});
}

function _orgConfirmDeprecateKnowledge(knowledgeId) {
  var org = _orgWorkbench.org || {};
  var item = _orgFindKnowledge(knowledgeId);
  if (!item || !_orgCanPublish(org)) {
    app.showToast(I18N.t('organizations.knowledge.readonly'));
    return;
  }
  app.showConfirm(I18N.t('organizations.knowledge.deprecate_confirm', {name: _orgpEsc(item.name || '')}), function() {
    API.orgKnowledge.deprecate(org.id, Number(knowledgeId)).then(function() {
      app.showToast(I18N.t('organizations.knowledge.deprecate_success'));
      _orgReloadKnowledge();
    }).catch(function(e) {
      app.showToast(_orgErrorText(e));
    });
  }, {danger: true, confirmText: I18N.t('organizations.knowledge.deprecate')});
}

function _orgConfirmArchiveKnowledge(knowledgeId) {
  var org = _orgWorkbench.org || {};
  var item = _orgFindKnowledge(knowledgeId);
  if (!item || !_orgCanPublish(org)) {
    app.showToast(I18N.t('organizations.knowledge.readonly'));
    return;
  }
  app.showConfirm(I18N.t('organizations.knowledge.archive_confirm', {name: _orgpEsc(item.name || '')}), function() {
    API.orgKnowledge.archive(org.id, Number(knowledgeId)).then(function() {
      app.showToast(I18N.t('organizations.knowledge.archive_success'));
      _orgReloadKnowledge();
    }).catch(function(e) {
      app.showToast(_orgErrorText(e));
    });
  }, {danger: true, confirmText: I18N.t('organizations.knowledge.archive')});
}

function _orgOpenSettings() {
  var org = _orgWorkbench.org || {};
  if (!_orgCanWrite(org.role)) {
    app.showToast(I18N.t("organizations.workbench.readonly"));
    return;
  }
  // 编辑组织入口收敛在组织详情页（Vue organizations 视图），这里直接跳当前组织详情
  app.nav('organizations', org.id);
}

function _orgRetryProvision() {
  var org = _orgWorkbench.org || {};
  if (!_orgCanWrite(org.role)) {
    app.showToast(I18N.t("organizations.workbench.readonly"));
    return;
  }
  var btn = document.getElementById('orgProvisionRetry');
  if (btn) { btn.disabled = true; btn.textContent = I18N.t("organizations.workbench.provision.retrying"); }
  API.post('/orgs/' + org.id + '/provision/retry', {}).then(function() {
    app.showToast(I18N.t("organizations.workbench.provision.retry_started"));
    _orgpLoadBase(org.id);
  }).catch(function(e) {
    app.showToast(_orgErrorText(e));
    if (btn) { btn.disabled = false; btn.textContent = I18N.t("organizations.workbench.provision.retry"); }
  });
}

function _orgCopy(field) {
  var org = _orgWorkbench.org || {};
  var text = org[field] || '';
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text);
    } else {
      var input = document.createElement('input');
      input.value = text;
      document.body.appendChild(input);
      input.select();
      document.execCommand('copy');
      input.remove();
    }
    app.showToast(I18N.t("organizations.workbench.copied"));
  } catch (e) {
    app.showToast(I18N.t("organizations.workbench.copy_failed"));
  }
}

function _orgOpenOrgRepo() {
  var org = _orgWorkbench.org || {};
  if (!org.org_repo_key) return;
  window.open(_vcsUrl(org.org_repo_key, ''), '_blank', 'noopener');
}

function _orgOpenScene() {
  var org = _orgWorkbench.org || {};
  if (!org.org_scene_id) return;
  if (app && typeof app.syncOrgContext === "function") app.syncOrgContext(org.id);
  var parts = _orgRepoParts(org.org_repo_key || '');
  if (parts.owner && parts.name) {
    sessionStorage.setItem('currentRepo', JSON.stringify({
      fullName: parts.full, owner: parts.owner, name: parts.name, repoKey: org.org_repo_key || ''
    }));
  }
  // 组织场景是仓库级本地场景（custom-*），必须走 scene-detail-local；
  // scene-detail 是市场场景详情（参数为 market id），误用会 422。
  app.nav('scene-detail-local', org.org_scene_id);
}

function _orgRepoPanel() {
  var org = _orgWorkbench.org || {};
  var canWrite = _orgCanWrite(org.role);
  var html = '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap">' +
    '<div><div style="font-size:15px;font-weight:600;color:var(--t1)">' + I18N.t("organizations.workbench.repos.title") + '</div>' +
    '<div style="font-size:12px;color:var(--t3);margin-top:3px">' + I18N.t("organizations.workbench.repos.subtitle") + '</div></div>';
  if (canWrite) {
    var orgId = String(org.id == null ? '' : org.id);
    html += '<div style="display:flex;gap:8px;align-items:center">' +
      '<button id="orgBindRepoBtn" onclick="_showOrgRepoPicker(\'' + orgId + '\', function(){ _orgpLoadRepos(); })" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">＋ ' + I18N.t("organizations.workbench.repos.picker_add") + '</button>' +
      '</div>';
  }
  html += '</div><div id="orgRepoList"></div>';
  return html;
}

function _orgpLoadRepos() {
  var org = _orgWorkbench.org || {};
  _orgPanel(_orgRepoPanel());
  var request = API.orgRepos ? API.orgRepos.list(org.id) : API.get('/orgs/' + org.id + '/repos');
  request.then(function(data) {
    var repos = (data && data.repos) || [];
    _orgWorkbench.repos = repos;
    var list = document.getElementById('orgRepoList');
    if (!list) return;
    if (!repos.length) {
      list.innerHTML = '<div style="border:1px dashed var(--b);border-radius:12px;padding:34px;text-align:center;color:var(--t3)">' + I18N.t("organizations.workbench.repos.empty") + '</div>';
      return;
    }
    list.innerHTML = repos.map(function(repo, idx) {
      var protectedRepo = !!repo.is_org_repo;
      var actions = '';
      if (_orgCanWrite(org.role)) {
        if (!protectedRepo) {
          actions = '<div style="display:flex;gap:6px;flex-wrap:wrap">' +
            '<button onclick="_orgOpenTransferDialog(' + idx + ')" style="padding:5px 9px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--a);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.repos.transfer") + '</button>' +
            '<button onclick="_orgUnbindRepo(' + idx + ')" style="padding:5px 9px;border:1px solid var(--r);border-radius:7px;background:transparent;color:var(--r);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.repos.unbind") + '</button></div>';
        } else {
          actions = '<span title="' + _orgpEsc(I18N.t("organizations.workbench.repos.protected_hint")) + '" style="font-size:11px;color:var(--t3)">' + I18N.t("organizations.workbench.repos.protected") + '</span>';
        }
      } else {
        actions = '<span style="font-size:11px;color:var(--t3)">' + I18N.t("organizations.workbench.readonly") + '</span>';
      }
      return '<div style="border:1px solid var(--b);border-radius:12px;padding:14px;margin-bottom:10px;display:flex;align-items:center;gap:12px;justify-content:space-between;flex-wrap:wrap">' +
        '<div style="min-width:0;flex:1">' +
        '<div style="font-size:13px;font-weight:600;color:var(--t1);word-break:break-word">' + _orgpEsc(repo.full_name || _orgRepoParts(repo.repo_key).full || repo.repo_key || '') +
        (repo.is_org_repo ? ' <span style="font-size:10px;padding:2px 6px;border-radius:999px;background:#e8f1ff;color:#1d5fb8">' + I18N.t("organizations.workbench.repos.org_repo") + '</span>' : '') + '</div>' +
        '<div style="font-size:11px;color:var(--t3);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin-top:3px;word-break:break-all">' + _orgpEsc(repo.repo_key || '') + '</div>' +
        '<div style="font-size:11px;color:var(--t3);margin-top:4px">' + I18N.t("organizations.workbench.repos.linked_at") + ': ' + _orgpEsc(repo.linked_at || '—') + '</div>' +
        '</div>' + actions + '</div>';
    }).join('');
  }).catch(function(e) {
    var list = document.getElementById('orgRepoList');
    if (list) list.innerHTML = '<div style="color:var(--r);padding:20px">' + _orgpEsc(_orgErrorText(e)) + '</div>';
  });
}

function _orgBindRepo() {
  var org = _orgWorkbench.org || {};
  var input = document.getElementById('orgBindRepoKey');
  var btn = document.getElementById('orgBindRepoBtn');
  var repoKey = input ? input.value.trim() : '';
  if (!repoKey) {
    app.showToast(I18N.t("organizations.workbench.repos.bind_required"));
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = I18N.t("organizations.workbench.repos.binding"); }
  var request = API.orgRepos ? API.orgRepos.bind(org.id, repoKey) : API.post('/orgs/' + org.id + '/repos', {repo_key: repoKey});
  request.then(function() {
    app.showToast(I18N.t("organizations.workbench.repos.bind_success"));
    _orgpLoadRepos();
  }).catch(function(e) {
    app.showToast(_orgErrorText(e));
    if (btn) { btn.disabled = false; btn.textContent = I18N.t("organizations.workbench.repos.bind"); }
  });
}

function _orgUnbindRepo(index) {
  var org = _orgWorkbench.org || {};
  var repo = _orgWorkbench.repos[index];
  if (!repo) return;
  if (!_orgCanWrite(org.role)) {
    app.showToast(I18N.t("organizations.workbench.readonly"));
    return;
  }
  var fullName = repo.full_name || _orgRepoParts(repo.repo_key).full || repo.repo_key;
  app.showConfirm(I18N.t("organizations.workbench.repos.unbind_confirm", {name: _orgpEsc(fullName)}), function() {
    var request = API.orgRepos ? API.orgRepos.unbind(org.id, repo.repo_key) : API.del('/orgs/' + org.id + '/repos/' + encodeURIComponent(repo.repo_key) + '?confirm=true');
    request.then(function() {
      app.showToast(I18N.t("organizations.workbench.repos.unbind_success"));
      _orgpLoadRepos();
    }).catch(function(e) {
      app.showToast(_orgErrorText(e));
    });
  }, {danger: true, confirmText: I18N.t("organizations.workbench.repos.unbind")});
}

function _orgOpenTransferDialog(index) {
  var org = _orgWorkbench.org || {};
  var repo = _orgWorkbench.repos[index];
  if (!repo || !_orgCanWrite(org.role)) return;
  _orgWorkbench.transferRepoIndex = index;
  API.get('/orgs').then(function(orgs) {
    var candidates = (orgs || []).filter(function(item) {
      return !item.is_personal && String(item.id) !== String(org.id) && _orgCanWrite(item.role);
    });
    if (!candidates.length) {
      app.showToast(I18N.t("organizations.workbench.repos.no_transfer_target"));
      return;
    }
    var overlay = document.createElement('div');
    overlay.id = 'orgTransferOverlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:1002;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;padding:20px';
    overlay.innerHTML = '<div style="width:420px;max-width:94vw;background:var(--c);border-radius:12px;padding:20px;box-shadow:0 12px 40px rgba(0,0,0,.16)">' +
      '<div style="font-size:15px;font-weight:600;color:var(--t1);margin-bottom:8px">' + I18N.t("organizations.workbench.repos.transfer_title") + '</div>' +
      '<div style="font-size:12px;color:var(--t3);margin-bottom:12px;word-break:break-all">' + _orgpEsc(repo.full_name || repo.repo_key || '') + '</div>' +
      '<select id="orgTransferTarget" style="width:100%;padding:9px 10px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box">' +
      candidates.map(function(item) {
        return '<option value="' + Number(item.id || 0) + '">' + _orgpEsc(item.name || item.slug || item.id) + '</option>';
      }).join('') + '</select>' +
      '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">' +
      '<button id="orgTransferCancel" style="padding:8px 14px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' + I18N.t("common.cancel") + '</button>' +
      '<button id="orgTransferNext" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' + I18N.t("organizations.workbench.repos.transfer_next") + '</button>' +
      '</div></div>';
    document.body.appendChild(overlay);
    document.getElementById('orgTransferCancel').onclick = function() { overlay.remove(); };
    overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
    document.getElementById('orgTransferNext').onclick = _orgConfirmTransfer;
  }).catch(function(e) {
    app.showToast(_orgErrorText(e));
  });
}

function _orgConfirmTransfer() {
  var org = _orgWorkbench.org || {};
  var repo = _orgWorkbench.repos[_orgWorkbench.transferRepoIndex];
  var select = document.getElementById('orgTransferTarget');
  var targetOrgId = select ? Number(select.value || 0) : 0;
  if (!repo || !targetOrgId) {
    app.showToast(I18N.t("organizations.workbench.repos.transfer_target_required"));
    return;
  }
  var fullName = repo.full_name || _orgRepoParts(repo.repo_key).full || repo.repo_key;
  var overlay = document.getElementById('orgTransferOverlay');
  if (overlay) overlay.remove();
  app.showConfirm(I18N.t("organizations.workbench.repos.transfer_confirm", {name: _orgpEsc(fullName)}), function() {
    var request = API.orgRepos ? API.orgRepos.transfer(org.id, repo.repo_key, targetOrgId) :
      API.post('/orgs/' + org.id + '/repos/' + encodeURIComponent(repo.repo_key) + '/transfer', {target_org_id: targetOrgId});
    request.then(function() {
      app.showToast(I18N.t("organizations.workbench.repos.transfer_success"));
      _orgpLoadRepos();
    }).catch(function(e) {
      app.showToast(_orgErrorText(e));
    });
  }, {danger: true, confirmText: I18N.t("organizations.workbench.repos.transfer")});
}

function _loadOrgDemands() {
  var org = _orgWorkbench.org || {};
  var parts = _orgRepoParts(org.org_repo_key || '');
  if (!parts.owner || !parts.name) {
    _orgPanel('<div style="border:1px dashed var(--b);border-radius:12px;padding:34px;text-align:center;color:var(--t3)">' + I18N.t("organizations.workbench.demands.no_org_repo") + '</div>');
    return;
  }
  var reposPromise = _orgWorkbench.repos.length
    ? Promise.resolve({repos: _orgWorkbench.repos})
    : (API.orgRepos ? API.orgRepos.list(org.id) : API.get('/orgs/' + org.id + '/repos'));
  var dispatchesPromise = (API.orgDispatches
    ? API.orgDispatches.list(org.id, 50)
    : API.get('/orgs/' + org.id + '/dispatches?limit=50'));
  Promise.all([
    reposPromise,
    API.get('/issues/' + encodeURIComponent(parts.owner) + '/' + encodeURIComponent(parts.name) + '?per_page=50&recent_terminal=50'),
    dispatchesPromise.catch(function() { return {dispatches: []}; })
  ])
    .then(function(results) {
      _orgWorkbench.repos = (results[0] && results[0].repos) || [];
      var _dd = results[2] || {};
      _orgWorkbench.dispatches = _dd.dispatches || _dd.items || (Array.isArray(_dd) ? _dd : []);
      var data = results[1];
      var issues = (data && data.issues) || (Array.isArray(data) ? data : []);
      _orgWorkbench.issues = issues.filter(function(item) {
        return Number(item.issue_number || 0) > 0;
      });
      _orgRenderDemandList();
    }).catch(function(e) {
      _orgPanel('<div style="color:var(--r);padding:20px">' + _orgpEsc(_orgErrorText(e)) + '</div>');
    });
}


// Issue #2267：组织需求进度展示（intake → dispatch → aggregate）
function _orgDemandProgress(issue) {
  var n = Number(issue.issue_number || 0);
  var all = _orgWorkbench.dispatches || [];
  var mine = all.filter(function(d) { return Number(d.org_issue_number || 0) === n; });
  // dispatch 列表按 id DESC（最新在前），最新批次取 mine[0]
  var latest = mine.length ? mine[0] : null;
  var dstatus = String((latest && latest.status) || '');
  var taskStatus = String(issue.task_status || '').toLowerCase();
  var stage = String(issue.stage || '').toLowerCase();
  var terminal = ['completed', 'needs_review', 'failed', 'cancelled', 'partial_failed'];
  var intakeState = (taskStatus === 'queued' || taskStatus === 'running'
    || taskStatus === 'dispatched' || taskStatus === 'processing')
    ? 'running'
    : (stage && stage !== 'issue' ? 'done' : (mine.length ? 'done' : 'waiting'));
  var dispatchState = mine.length ? (terminal.indexOf(dstatus) >= 0 ? 'done' : 'running') : 'waiting';
  var aggState = (mine.length && terminal.indexOf(dstatus) >= 0) ? 'done' : (mine.length ? 'waiting' : 'waiting');
  if (dstatus === 'failed' || dstatus === 'partial_failed') { aggState = 'failed'; }
  if (String(issue.status) === 'cancelled') { intakeState = dispatchState = aggState = 'failed'; }
  function dot(state, label) {
    var color = state === 'done' ? 'var(--g,#34c759)' : state === 'running' ? 'var(--a)' : state === 'failed' ? 'var(--r)' : 'var(--t3)';
    var text = state === 'done' ? I18N.t("organizations.workbench.progress.done")
      : state === 'running' ? I18N.t("organizations.workbench.progress.running")
      : state === 'failed' ? I18N.t("organizations.workbench.progress.failed")
      : I18N.t("organizations.workbench.progress.waiting");
    return '<span style="display:inline-flex;align-items:center;gap:5px;font-size:11px;color:' + color + '">' +
      '<span style="width:8px;height:8px;border-radius:50%;background:' + color + ';display:inline-block"></span>' +
      label + ' · ' + text + '</span>';
  }
  var arrow = '<span style="color:var(--t3);font-size:11px">→</span>';
  var count = mine.length
    ? I18N.t("organizations.workbench.progress.dispatches", {n: mine.length}) +
      (latest ? '（' + _orgpEsc(dstatus || '-') + '）' : '')
    : I18N.t("organizations.workbench.progress.none");
  return '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px">' +
    dot(intakeState, I18N.t("organizations.workbench.progress.intake")) + arrow +
    dot(dispatchState, I18N.t("organizations.workbench.progress.dispatch")) + arrow +
    dot(aggState, I18N.t("organizations.workbench.progress.aggregate")) +
    '<span style="font-size:11px;color:var(--t3)">· ' + count + '</span></div>';
}

function _orgRenderDemandList() {
  var org = _orgWorkbench.org || {};
  var canWrite = _orgCanWrite(org.role);
  var issues = _orgWorkbench.issues || [];
  var html = '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap">' +
    '<div><div style="font-size:15px;font-weight:600;color:var(--t1)">' + I18N.t("organizations.workbench.demands.title") + '</div>' +
    '<div style="font-size:12px;color:var(--t3);margin-top:3px">' + I18N.t("organizations.workbench.demands.subtitle") + '</div></div>';
  if (canWrite) {
    html += '<button onclick="_orgShowCreateDemandForm()" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' + I18N.t("organizations.workbench.demands.create") + '</button>';
  }
  html += '</div>';
  if (!issues.length) {
    html += '<div style="border:1px dashed var(--b);border-radius:12px;padding:34px;text-align:center;color:var(--t3)">' + I18N.t("organizations.workbench.demands.empty") + '</div>';
  } else {
    html += issues.map(function(issue, idx) {
      var status = issue.status || issue.task_status || 'active';
      var action = canWrite
        ? '<button onclick="_orgOpenDispatchWizard(' + idx + ')" style="padding:6px 11px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--a);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.demands.dispatch") + '</button>'
        : '<span style="font-size:11px;color:var(--t3)">' + I18N.t("organizations.workbench.readonly") + '</span>';
      var prog = _orgDemandProgress(issue);
      return '<div style="border:1px solid var(--b);border-radius:12px;padding:14px;margin-bottom:10px;display:flex;align-items:flex-start;gap:12px;justify-content:space-between;flex-wrap:wrap">' +
        '<div style="min-width:0;flex:1"><div style="font-size:13px;font-weight:600;color:var(--t1);word-break:break-word">#' + Number(issue.issue_number || 0) + ' ' + _orgpEsc(issue.title || '') + '</div>' +
        '<div style="font-size:11px;color:var(--t3);margin-top:5px">' + _orgpEsc(issue.scene_id || org.org_scene_id || '') +
          ' · ' + I18N.t("organizations.workbench.progress.stage") + ': ' + _orgpEsc(issue.stage || issue.owner_agent || '-') +
          ' · ' + I18N.t("organizations.workbench.progress.task") + ': ' + _orgpEsc(issue.task_status || '-') +
          ' · ' + _orgStatusBadge(status) + '</div>' +
        prog + '</div>' +
        action + '</div>';
    }).join('');
  }
  _orgPanel(html);
}

function _orgModal(id, html, opts) {
  var old = document.getElementById(id);
  if (old) old.remove();
  var width = Number((opts || {}).width || 520);
  var overlay = document.createElement('div');
  overlay.id = id;
  overlay.style.cssText = 'position:fixed;inset:0;z-index:1002;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;padding:20px';
  overlay.innerHTML = '<div style="width:' + width + 'px;max-width:94vw;max-height:88vh;overflow-y:auto;background:var(--c);border-radius:12px;padding:20px;box-shadow:0 12px 40px rgba(0,0,0,.16)">' + html + '</div>';
  document.body.appendChild(overlay);
  overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
  return overlay;
}

function _orgShowCreateDemandForm() {
  var org = _orgWorkbench.org || {};
  if (!_orgCanWrite(org.role)) {
    app.showToast(I18N.t("organizations.workbench.readonly"));
    return;
  }
  _orgModal('orgCreateDemandOverlay',
    '<div style="font-size:15px;font-weight:600;color:var(--t1);margin-bottom:14px">' + I18N.t("organizations.workbench.demands.create_title") + '</div>' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t("organizations.workbench.demands.title_label") + '</label>' +
    '<input id="orgDemandTitle" type="text" style="width:100%;padding:9px 10px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box;margin-bottom:12px">' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t("organizations.workbench.demands.desc_label") + '</label>' +
    '<textarea id="orgDemandDescription" rows="5" style="width:100%;padding:9px 10px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:13px;box-sizing:border-box;resize:vertical;margin-bottom:12px"></textarea>' +
    '<div style="font-size:11px;color:var(--t3);margin-bottom:16px">scene_id: ' + _orgpEsc(org.org_scene_id || '—') + '</div>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px">' +
    '<button onclick="document.getElementById(\'orgCreateDemandOverlay\').remove()" style="padding:8px 14px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' + I18N.t("common.cancel") + '</button>' +
    '<button id="orgCreateDemandBtn" onclick="_createOrgIssue()" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' + I18N.t("organizations.workbench.demands.create_confirm") + '</button>' +
    '</div>');
}

function _createOrgIssue() {
  var org = _orgWorkbench.org || {};
  var titleInput = document.getElementById('orgDemandTitle');
  var descInput = document.getElementById('orgDemandDescription');
  var title = titleInput ? titleInput.value.trim() : '';
  var description = descInput ? descInput.value.trim() : '';
  if (!title) {
    app.showToast(I18N.t("organizations.workbench.demands.title_required"));
    return;
  }
  var parts = _orgRepoParts(org.org_repo_key || '');
  if (!parts.owner || !parts.name) {
    app.showToast(I18N.t("organizations.workbench.demands.no_org_repo"));
    return;
  }
  var btn = document.getElementById('orgCreateDemandBtn');
  if (btn) { btn.disabled = true; btn.textContent = I18N.t("organizations.workbench.demands.creating"); }
  API.post('/issues/' + encodeURIComponent(parts.owner) + '/' + encodeURIComponent(parts.name), {
    repo_key: org.org_repo_key || '',
    title: title,
    description: description,
    scene_id: org.org_scene_id || '',
    is_subtask_mode: 0
  }).then(function() {
    var overlay = document.getElementById('orgCreateDemandOverlay');
    if (overlay) overlay.remove();
    app.showToast(I18N.t("organizations.workbench.demands.create_success"));
    _loadOrgDemands();
  }).catch(function(e) {
    app.showToast(_orgErrorText(e));
    if (btn) { btn.disabled = false; btn.textContent = I18N.t("organizations.workbench.demands.create_confirm"); }
  });
}

function _orgOpenDispatchWizard(index) {
  var org = _orgWorkbench.org || {};
  var issue = _orgWorkbench.issues[index];
  if (!issue || !_orgCanWrite(org.role)) return;
  if (!_orgWorkbench.repos.length) {
    app.showToast(I18N.t("organizations.workbench.demands.repos_required"));
    return;
  }
  _orgWorkbench.pendingIssueIndex = index;
  // Issue #2236：每次打开向导重置携带选择，避免上一次选择串场
  _orgWorkbench.dispatchSelection = {ctxIds: [], knowledgeIds: []};
  // 上一次读取失败的知识清掉失败标记与空正文，重新打开向导时重试
  var failed = _orgWorkbench.knowledgeContentFailed || {};
  var contentCache = _orgWorkbench.knowledgeContentCache || {};
  Object.keys(failed).forEach(function(id) {
    delete failed[id];
    delete contentCache[id];
  });
  _orgWorkbench.dispatchKnowledgeFilters = {kind: '', tag: '', q: ''};
  // 组织自动仓库不作为下派目标（同仓库自派没有意义），其余已关联仓库默认勾选
  var repoOptions = _orgWorkbench.repos.filter(function(repo) {
    return String(repo.repo_key || '') !== String(org.org_repo_key || '');
  });
  if (!repoOptions.length) {
    app.showToast(I18N.t("organizations.workbench.demands.repos_required"));
    return;
  }
  var targets = repoOptions.map(function(repo, repoIndex) {
    return '<label style="display:flex;align-items:flex-start;gap:9px;padding:9px 0;border-bottom:1px solid var(--b);font-size:12px;color:var(--t1)">' +
      '<input class="org-dispatch-target" type="checkbox" value="' + _orgpEsc(repo.repo_key || '') + '" checked style="margin-top:2px">' +
      '<span><b>' + _orgpEsc(repo.full_name || repo.repo_key || '') + '</b><br><span style="font-size:10px;color:var(--t3)">' + _orgpEsc(repo.repo_key || '') + '</span></span>' +
      '</label>';
  }).join('');
  _orgModal('orgDispatchWizardOverlay',
    '<div style="font-size:15px;font-weight:600;color:var(--t1);margin-bottom:6px">' + I18N.t("organizations.workbench.demands.dispatch_title") + '</div>' +
    '<div style="font-size:12px;color:var(--t3);margin-bottom:12px">#' + Number(issue.issue_number || 0) + ' ' + _orgpEsc(issue.title || '') + '</div>' +
    '<div style="font-size:12px;font-weight:600;color:var(--t2);margin-bottom:4px">' + I18N.t("organizations.workbench.demands.targets_label") + '</div>' +
    '<div style="max-height:160px;overflow-y:auto;border:1px solid var(--b);border-radius:8px;padding:2px 10px;margin-bottom:12px">' + targets + '</div>' +
    '<label style="display:block;font-size:12px;color:var(--t2);margin-bottom:5px">' + I18N.t("organizations.workbench.demands.context_label") + '</label>' +
    '<textarea id="orgDispatchContext" rows="3" oninput="_orgRefreshDispatchPreview()" placeholder="' + _orgpEsc(I18N.t("organizations.workbench.demands.context_placeholder")) + '" style="width:100%;padding:9px 10px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:12px;box-sizing:border-box;resize:vertical;margin-bottom:12px"></textarea>' +
    '<div id="orgDispatchContextPicker"><div style="padding:16px;color:var(--t3);font-size:12px">' + I18N.t("organizations.loading") + '</div></div>' +
    '<div id="orgDispatchKnowledgePicker"><div style="padding:16px;color:var(--t3);font-size:12px">' + I18N.t("organizations.loading") + '</div></div>' +
    '<div id="orgDispatchPreview"></div>' +
    '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">' +
    '<button onclick="document.getElementById(\'orgDispatchWizardOverlay\').remove()" style="padding:8px 14px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' + I18N.t("common.cancel") + '</button>' +
    '<button id="orgDispatchCreateBtn" onclick="_createOrgDispatch()" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' + I18N.t("organizations.workbench.demands.dispatch_confirm") + '</button>' +
    '</div>', {width: 640});
  _orgLoadDispatchOptions();
}

function _orgSelectableContexts(list) {
  return (list || []).filter(function(item) {
    return _orgLifecycle(item.lifecycle_status) === 'active';
  });
}

function _orgSelectableKnowledge(list) {
  return (list || []).filter(function(item) {
    return _orgLifecycle(item.lifecycle_status) === 'active';
  });
}

// 下派预览纯函数：上下文列表自带正文，知识正文由调用方合并进 item.content。
function _orgBuildDispatchContext(input) {
  input = input || {};
  var contexts = input.contexts || [];
  var knowledge = input.knowledge || [];
  var contextIds = (input.contextIds || []).map(String);
  var knowledgeIds = (input.knowledgeIds || []).map(Number);
  var limit = Number(input.limit || ORG_DISPATCH_CONTEXT_LIMIT_BYTES);
  var selectedContexts = contexts.filter(function(item) { return contextIds.indexOf(String(item.id || '')) >= 0; });
  var selectedKnowledge = knowledge.filter(function(item) { return knowledgeIds.indexOf(Number(item.id)) >= 0; });
  var contextBytes = 0;
  var contextRefs = selectedContexts.map(function(item) {
    var bytes = _orgUtf8Bytes(item.content || '');
    contextBytes += bytes;
    return {type: 'context', id: String(item.id || ''), title: item.title || '', kind: item.kind || '', bytes: bytes};
  });
  var knowledgeBytes = 0;
  var hasUnknown = false;
  var knowledgeRefs = selectedKnowledge.map(function(item) {
    // 正文缺失（读取中/失败）时不能按 0 字节计入，否则会低估大小、绕过上限
    var unknown = !!item.bytes_unknown;
    var bytes = unknown ? 0 : _orgUtf8Bytes(item.content || '');
    if (unknown) hasUnknown = true;
    knowledgeBytes += bytes;
    return {type: 'knowledge', id: Number(item.id), title: item.name || '', tags: item.tags || [],
      bytes: bytes, bytes_unknown: unknown};
  });
  var text = String(input.text || '');
  var textBytes = _orgUtf8Bytes(text);
  var tags = [];
  selectedKnowledge.forEach(function(item) {
    (item.tags || []).forEach(function(tag) {
      if (tags.indexOf(tag) < 0) tags.push(tag);
    });
  });
  var total = contextBytes + knowledgeBytes + textBytes;
  return {
    text: text,
    org_context_ids: selectedContexts.map(function(item) { return String(item.id || ''); }),
    knowledge_ids: selectedKnowledge.map(function(item) { return Number(item.id); }),
    knowledge_tags: tags,
    estimated_bytes: total,
    over_limit: total > limit,
    has_unknown: hasUnknown,
    limit: limit,
    parts: [
      {key: 'context', bytes: contextBytes, count: selectedContexts.length},
      {key: 'knowledge', bytes: knowledgeBytes, count: selectedKnowledge.length},
      {key: 'text', bytes: textBytes, count: text.trim() ? 1 : 0},
    ],
    references: contextRefs.concat(knowledgeRefs),
  };
}

function _orgDispatchPayloadContext(preview) {
  var p = preview || {};
  var text = String(p.text || '');
  var ctxIds = p.org_context_ids || [];
  var knIds = p.knowledge_ids || [];
  // 无任何携带内容时保持纯字符串，兼容历史 dispatch payload 读取。
  if (!text && !ctxIds.length && !knIds.length) return '';
  return {
    text: text,
    org_context_ids: ctxIds,
    knowledge_ids: knIds,
    knowledge_tags: p.knowledge_tags || [],
    estimated_bytes: Number(p.estimated_bytes || 0),
  };
}

function _orgCutText(text, max) {
  var s = String(text || '');
  return s.length > max ? s.slice(0, max) + '…' : s;
}

function _orgLoadDispatchOptions() {
  var org = _orgWorkbench.org || {};
  var ctxReq = API.orgContext
    ? API.orgContext.list(org.id, {status: 'active'})
    : API.get('/orgs/' + org.id + '/context?status=active');
  var knReq = API.orgKnowledge
    ? API.orgKnowledge.list(org.id, {status: 'active'})
    : API.get('/orgs/' + org.id + '/knowledge?status=active');
  Promise.all([
    ctxReq.then(function(d) { return (d && d.contexts) || []; }).catch(function() { return []; }),
    knReq.then(function(d) { return (d && d.knowledge) || []; }).catch(function() { return []; }),
  ]).then(function(results) {
    _orgWorkbench.dispatchContexts = _orgSelectableContexts(results[0]);
    _orgWorkbench.dispatchKnowledge = _orgSelectableKnowledge(results[1]);
    _orgWorkbench.dispatchContextMeta = {};
    _orgWorkbench.dispatchKnowledgeMeta = {};
    _orgWorkbench.dispatchContexts.forEach(function(item) {
      _orgWorkbench.dispatchContextMeta[String(item.id || '')] = item;
    });
    _orgWorkbench.dispatchKnowledge.forEach(function(item) {
      _orgWorkbench.dispatchKnowledgeMeta[Number(item.id)] = item;
    });
    _orgRenderDispatchContextPicker();
    _orgRenderDispatchKnowledgePicker();
    _orgRefreshDispatchPreview();
  });
}

function _orgRenderDispatchContextPicker() {
  var el = document.getElementById('orgDispatchContextPicker');
  if (!el) return;
  var items = _orgWorkbench.dispatchContexts || [];
  var selection = _orgWorkbench.dispatchSelection || {ctxIds: [], knowledgeIds: []};
  var html = '<div style="font-size:12px;font-weight:600;color:var(--t2);margin-bottom:4px">' +
    I18N.t('organizations.dispatch.contexts_label') +
    ' <span style="font-weight:400;color:var(--t3)">' + I18N.t('organizations.dispatch.contexts_hint') + '</span></div>';
  if (!items.length) {
    html += '<div style="border:1px dashed var(--b);border-radius:8px;padding:12px;text-align:center;color:var(--t3);font-size:12px;margin-bottom:12px">' +
      I18N.t('organizations.dispatch.no_active_context') + '</div>';
    el.innerHTML = html;
    return;
  }
  html += '<div style="border:1px solid var(--b);border-radius:8px;padding:2px 10px;margin-bottom:12px;max-height:160px;overflow-y:auto">';
  ['brief', 'policy', 'decision'].forEach(function(kind) {
    var group = items.filter(function(item) { return String(item.kind || '') === kind; });
    if (!group.length) return;
    html += '<div style="font-size:11px;color:var(--t3);padding:7px 0 2px">' + I18N.t('organizations.context.kind_' + kind) + '</div>';
    group.forEach(function(item) {
      var id = String(item.id || '');
      var checked = selection.ctxIds.indexOf(id) >= 0;
      html += '<label style="display:flex;align-items:flex-start;gap:8px;padding:5px 0;font-size:12px;color:var(--t1)">' +
        '<input class="org-dispatch-ctx" type="checkbox" value="' + id + '"' + (checked ? ' checked' : '') +
        ' onchange="_orgOnDispatchSelectionChange()" style="margin-top:2px">' +
        '<span style="min-width:0"><b>' + _orgpEsc(item.title || '') + '</b> ' + _orgLifecycleBadge(item.lifecycle_status) +
        '<span style="font-size:10px;color:var(--t3);margin-left:6px">' + _orgFormatBytes(_orgUtf8Bytes(item.content || '')) + '</span>' +
        '<br><span style="font-size:10px;color:var(--t3)">' + _orgpEsc(_orgCutText(item.content || '', 80)) + '</span></span></label>';
    });
  });
  el.innerHTML = html + '</div>';
}

function _orgRenderDispatchKnowledgePicker() {
  var el = document.getElementById('orgDispatchKnowledgePicker');
  if (!el) return;
  var filters = _orgWorkbench.dispatchKnowledgeFilters || {};
  var inputStyle = 'padding:6px 8px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--t1);font-size:12px;box-sizing:border-box';
  var html = '<div style="font-size:12px;font-weight:600;color:var(--t2);margin-bottom:4px">' +
    I18N.t('organizations.dispatch.knowledge_label') +
    ' <span style="font-weight:400;color:var(--t3)">' + I18N.t('organizations.dispatch.knowledge_hint') + '</span></div>' +
    '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">' +
    '<input id="orgDispatchKnKind" type="text" value="' + _orgpEsc(filters.kind || '') + '" placeholder="' +
    _orgpEsc(I18N.t('organizations.dispatch.knowledge_filter_kind')) + '" oninput="_orgOnDispatchKnowledgeFilter()" style="width:90px;' + inputStyle + '">' +
    '<input id="orgDispatchKnTag" type="text" value="' + _orgpEsc(filters.tag || '') + '" placeholder="' +
    _orgpEsc(I18N.t('organizations.dispatch.knowledge_filter_tag')) + '" oninput="_orgOnDispatchKnowledgeFilter()" style="width:110px;' + inputStyle + '">' +
    '<input id="orgDispatchKnQ" type="text" value="' + _orgpEsc(filters.q || '') + '" placeholder="' +
    _orgpEsc(I18N.t('organizations.dispatch.knowledge_filter_q')) + '" oninput="_orgOnDispatchKnowledgeFilter()" style="width:170px;' + inputStyle + '">' +
    '<span id="orgDispatchKnCount" style="font-size:11px;color:var(--t3);align-self:center"></span></div>' +
    '<div id="orgDispatchKnowledgeList" style="border:1px solid var(--b);border-radius:8px;padding:2px 10px;margin-bottom:12px;max-height:160px;overflow-y:auto"></div>';
  el.innerHTML = html;
  _orgRenderDispatchKnowledgeList();
}

var _orgDispatchKnTimer = null;
function _orgOnDispatchKnowledgeFilter() {
  clearTimeout(_orgDispatchKnTimer);
  _orgDispatchKnTimer = setTimeout(function() {
    function read(id) {
      var node = document.getElementById(id);
      return node ? String(node.value || '').trim() : '';
    }
    _orgWorkbench.dispatchKnowledgeFilters = {
      kind: read('orgDispatchKnKind'),
      tag: read('orgDispatchKnTag'),
      q: read('orgDispatchKnQ'),
    };
    _orgRenderDispatchKnowledgeList();
  }, 300);
}

function _orgFilterDispatchKnowledge() {
  var filters = _orgWorkbench.dispatchKnowledgeFilters || {};
  var kind = String(filters.kind || '').toLowerCase();
  var tag = String(filters.tag || '').toLowerCase();
  var q = String(filters.q || '').toLowerCase();
  return (_orgWorkbench.dispatchKnowledge || []).filter(function(item) {
    if (kind && String(item.kind || '').toLowerCase() !== kind) return false;
    if (tag) {
      var hitTag = (item.tags || []).some(function(t) { return String(t).toLowerCase() === tag; });
      if (!hitTag) return false;
    }
    if (q) {
      var blob = [item.name || '', item.description || '', item.kind || '', (item.tags || []).join(',')].join(' ').toLowerCase();
      if (blob.indexOf(q) < 0) return false;
    }
    return true;
  });
}

function _orgRenderDispatchKnowledgeList() {
  var list = document.getElementById('orgDispatchKnowledgeList');
  if (!list) return;
  var selection = _orgWorkbench.dispatchSelection || {ctxIds: [], knowledgeIds: []};
  var items = _orgFilterDispatchKnowledge();
  var count = document.getElementById('orgDispatchKnCount');
  if (count) {
    count.textContent = I18N.t('organizations.dispatch.selected_count', {n: selection.knowledgeIds.length});
  }
  if (!items.length) {
    list.innerHTML = '<div style="padding:12px;text-align:center;color:var(--t3);font-size:12px">' +
      I18N.t('organizations.dispatch.no_active_knowledge') + '</div>';
    return;
  }
  list.innerHTML = items.map(function(item) {
    var id = Number(item.id);
    var checked = selection.knowledgeIds.indexOf(id) >= 0;
    var tags = (item.tags || []).map(function(t) {
      return '<span style="padding:0 6px;border-radius:999px;background:var(--bg);color:var(--t2);font-size:10px">' + _orgpEsc(t) + '</span>';
    }).join(' ');
    return '<label style="display:flex;align-items:flex-start;gap:8px;padding:6px 0;border-bottom:1px solid var(--b);font-size:12px;color:var(--t1)">' +
      '<input class="org-dispatch-kn" type="checkbox" value="' + id + '"' + (checked ? ' checked' : '') +
      ' onchange="_orgOnDispatchSelectionChange()" style="margin-top:2px">' +
      '<span style="min-width:0"><b>' + _orgpEsc(item.name || '') + '</b>' +
      (item.kind ? ' <span style="font-size:10px;color:var(--t3)">' + _orgpEsc(item.kind) + '</span>' : '') +
      (tags ? ' ' + tags : '') +
      '<br><span style="font-size:10px;color:var(--t3)">' + _orgpEsc(_orgCutText(item.description || '', 70)) + '</span></span></label>';
  }).join('');
}

function _orgCheckedIds(selector) {
  var ids = [];
  var nodes = document.querySelectorAll ? document.querySelectorAll(selector) : [];
  for (var i = 0; i < nodes.length; i++) {
    if (nodes[i].checked) ids.push(Number(nodes[i].value));
  }
  return ids;
}

function _orgCheckedStringIds(selector) {
  var ids = [];
  var nodes = document.querySelectorAll ? document.querySelectorAll(selector) : [];
  for (var i = 0; i < nodes.length; i++) {
    if (nodes[i].checked) ids.push(String(nodes[i].value || ''));
  }
  return ids;
}

function _orgOnDispatchSelectionChange() {
  var selection = _orgWorkbench.dispatchSelection || {ctxIds: [], knowledgeIds: []};
  var ctxIds = _orgCheckedStringIds('.org-dispatch-ctx');
  var visibleIds = {};
  var visibleSelected = [];
  var nodes = document.querySelectorAll ? document.querySelectorAll('.org-dispatch-kn') : [];
  for (var i = 0; i < nodes.length; i++) {
    var id = Number(nodes[i].value);
    visibleIds[id] = true;
    if (nodes[i].checked) visibleSelected.push(id);
  }
  // 过滤后不可见的知识保留原选择，避免筛选动作丢弃已选项
  var kept = (selection.knowledgeIds || []).filter(function(id) { return !visibleIds[Number(id)]; });
  _orgWorkbench.dispatchSelection = {
    ctxIds: ctxIds,
    knowledgeIds: kept.concat(visibleSelected),
  };
  var count = document.getElementById('orgDispatchKnCount');
  if (count) {
    count.textContent = I18N.t('organizations.dispatch.selected_count', {n: _orgWorkbench.dispatchSelection.knowledgeIds.length});
  }
  _orgRefreshDispatchPreview();
}

function _orgLoadKnowledgeContents(ids, done) {
  var org = _orgWorkbench.org || {};
  var cache = _orgWorkbench.knowledgeContentCache || (_orgWorkbench.knowledgeContentCache = {});
  var failed = _orgWorkbench.knowledgeContentFailed || (_orgWorkbench.knowledgeContentFailed = {});
  var pending = (ids || []).map(Number).filter(function(id) { return cache[id] === undefined; });
  if (!pending.length) {
    if (done) done();
    return;
  }
  var index = 0;
  function pump() {
    if (index >= pending.length) {
      if (done) done();
      return;
    }
    var batch = pending.slice(index, index + 4);
    index += 4;
    Promise.all(batch.map(function(id) {
      var request = API.orgKnowledge
        ? API.orgKnowledge.get(org.id, id)
        : API.get('/orgs/' + org.id + '/knowledge/' + id);
      return request.then(function(item) {
        cache[id] = (item && item.content) || '';
        delete failed[id];
        if (item && item.name) _orgWorkbench.dispatchKnowledgeMeta[id] = item;
      }).catch(function() {
        // 读取失败：正文为空但大小未知，不得当作 0 字节计入预览
        cache[id] = '';
        failed[id] = true;
      });
    })).then(pump);
  }
  pump();
}

function _orgKnowledgeContentState(id) {
  var key = Number(id);
  var cache = _orgWorkbench.knowledgeContentCache || {};
  var failed = _orgWorkbench.knowledgeContentFailed || {};
  if (failed[key]) return 'failed';
  if (cache[key] === undefined) return 'missing';
  return 'ready';
}

// 汇总选中知识的正文就绪情况：missing=仍在校验前读取，failed=读取失败（大小未知）
function _orgDispatchContentStates(selection) {
  var ids = ((selection || {}).knowledgeIds || []);
  var states = {missing: [], failed: []};
  ids.forEach(function(id) {
    var s = _orgKnowledgeContentState(id);
    if (s === 'missing') states.missing.push(Number(id));
    else if (s === 'failed') states.failed.push(Number(id));
  });
  return states;
}

function _orgDispatchSelectionPreview() {
  var selection = _orgWorkbench.dispatchSelection || {ctxIds: [], knowledgeIds: []};
  var cache = _orgWorkbench.knowledgeContentCache || {};
  var failed = _orgWorkbench.knowledgeContentFailed || {};
  var contexts = (selection.ctxIds || []).map(function(id) {
    return _orgWorkbench.dispatchContextMeta[Number(id)] || {id: Number(id), title: '#' + id, content: '', kind: ''};
  });
  var knowledge = (selection.knowledgeIds || []).map(function(id) {
    var meta = _orgWorkbench.dispatchKnowledgeMeta[Number(id)] || {id: Number(id), name: '#' + id, tags: []};
    return {
      id: Number(id),
      name: meta.name || '',
      tags: meta.tags || [],
      content: cache[Number(id)] || '',
      bytes_unknown: !!failed[Number(id)],
    };
  });
  var textEl = document.getElementById('orgDispatchContext');
  return _orgBuildDispatchContext({
    contexts: contexts,
    knowledge: knowledge,
    contextIds: selection.ctxIds || [],
    knowledgeIds: selection.knowledgeIds || [],
    text: textEl ? textEl.value : '',
    limit: ORG_DISPATCH_CONTEXT_LIMIT_BYTES,
  });
}

function _orgRefreshDispatchPreview() {
  var el = document.getElementById('orgDispatchPreview');
  if (!el) return;
  var selection = _orgWorkbench.dispatchSelection || {ctxIds: [], knowledgeIds: []};
  var states = _orgDispatchContentStates(selection);
  if (states.missing.length) {
    // 正文未取回：先禁用提交，避免同步重算按 0 字节放行超限下派
    var button = document.getElementById('orgDispatchCreateBtn');
    if (button) button.disabled = true;
    el.innerHTML = '<div style="padding:10px 12px;border:1px dashed var(--b);border-radius:10px;color:var(--t3);font-size:12px">' +
      I18N.t('organizations.dispatch.preview_loading') + '</div>';
    _orgLoadKnowledgeContents(states.missing, _orgRenderDispatchPreviewHtml);
    return;
  }
  _orgRenderDispatchPreviewHtml();
}

function _orgRenderDispatchPreviewHtml() {
  var el = document.getElementById('orgDispatchPreview');
  if (!el) return;
  var preview = _orgDispatchSelectionPreview();
  var button = document.getElementById('orgDispatchCreateBtn');
  var blocked = !!preview.over_limit || !!preview.has_unknown;
  if (button) button.disabled = blocked;
  var over = !!preview.over_limit;
  var border = over ? '#f3b8b8' : 'var(--b)';
  var background = over ? '#fdecec' : 'var(--c)';
  var ctxBytes = 0;
  var knBytes = 0;
  var textBytes = 0;
  (preview.parts || []).forEach(function(part) {
    if (part.key === 'context') ctxBytes = part.bytes;
    else if (part.key === 'knowledge') knBytes = part.bytes;
    else if (part.key === 'text') textBytes = part.bytes;
  });
  var refs = (preview.references || []).map(function(ref) {
    return '<li style="margin:2px 0">' + (ref.type === 'context'
      ? I18N.t('organizations.dispatch.preview_context')
      : I18N.t('organizations.dispatch.preview_knowledge')) + ' · ' + _orgpEsc(ref.title) +
      ' <span style="color:var(--t3)">(' + (ref.bytes_unknown
        ? I18N.t('organizations.dispatch.preview_unknown_size')
        : _orgFormatBytes(ref.bytes)) + ')</span></li>';
  }).join('');
  el.innerHTML = '<div style="border:1px solid ' + border + ';background:' + background + ';border-radius:10px;padding:10px 12px;margin-bottom:12px">' +
    '<div style="font-size:12px;font-weight:600;color:var(--t1)">' + I18N.t('organizations.dispatch.preview_title') + '</div>' +
    '<div style="font-size:12px;color:' + (over ? 'var(--r)' : 'var(--t2)') + ';margin-top:5px">' +
    I18N.t('organizations.dispatch.preview_total', {
      size: _orgFormatBytes(preview.estimated_bytes), limit: _orgFormatBytes(preview.limit)
    }) + '</div>' +
    '<div style="font-size:11px;color:var(--t3);margin-top:3px">' +
    I18N.t('organizations.dispatch.preview_context') + ': ' + _orgFormatBytes(ctxBytes) + ' · ' +
    I18N.t('organizations.dispatch.preview_knowledge') + ': ' + _orgFormatBytes(knBytes) + ' · ' +
    I18N.t('organizations.dispatch.preview_text') + ': ' + _orgFormatBytes(textBytes) + ' · ' +
    I18N.t('organizations.dispatch.preview_refs', {n: (preview.references || []).length}) + '</div>' +
    (over ? '<div style="color:var(--r);font-size:12px;margin-top:6px">' +
      I18N.t('organizations.dispatch.preview_over', {
        size: _orgFormatBytes(preview.estimated_bytes), limit: _orgFormatBytes(preview.limit)
      }) + '</div>' : '') +
    (preview.has_unknown ? '<div style="color:var(--r);font-size:12px;margin-top:6px">' +
      I18N.t('organizations.dispatch.preview_unknown_hint') + '</div>' : '') +
    (refs ? '<div style="margin-top:7px"><div style="font-size:11px;color:var(--t3)">' +
      I18N.t('organizations.dispatch.references_label') + '</div><ul style="margin:4px 0 0 18px;padding:0;font-size:11px;color:var(--t2)">' +
      refs + '</ul></div>' : '') +
    '<div style="font-size:10px;color:var(--t3);margin-top:7px">' + I18N.t('organizations.dispatch.limit_hint') + '</div>' +
    '</div>';
}

function _createOrgDispatch() {
  var org = _orgWorkbench.org || {};
  var issue = _orgWorkbench.issues[_orgWorkbench.pendingIssueIndex];
  var selected = [];
  var nodes = document.querySelectorAll ? document.querySelectorAll('.org-dispatch-target:checked') : [];
  for (var i = 0; i < nodes.length; i++) selected.push(nodes[i].value);
  if (!issue || !selected.length) {
    app.showToast(I18N.t("organizations.workbench.demands.target_required"));
    return;
  }
  // Issue #2236：正文未就绪/读取失败时不得提交（否则大小按 0 计会绕过 64KB 阻断）
  var contentStates = _orgDispatchContentStates(_orgWorkbench.dispatchSelection || {});
  if (contentStates.missing.length) {
    app.showToast(I18N.t('organizations.dispatch.preview_loading'));
    return;
  }
  if (contentStates.failed.length) {
    app.showToast(I18N.t('organizations.dispatch.preview_unknown_hint'));
    return;
  }
  // Issue #2236：超限阻断提交；payload 使用结构化 context（无选择时保持纯字符串）
  var preview = _orgDispatchSelectionPreview();
  if (preview.over_limit || preview.has_unknown) {
    app.showToast(I18N.t(preview.has_unknown
      ? 'organizations.dispatch.preview_unknown_hint'
      : 'organizations.dispatch.preview_over', {
      size: _orgFormatBytes(preview.estimated_bytes), limit: _orgFormatBytes(preview.limit)
    }));
    return;
  }
  var btn = document.getElementById('orgDispatchCreateBtn');
  if (btn) { btn.disabled = true; btn.textContent = I18N.t("organizations.workbench.demands.dispatching"); }
  var payload = {
    org_issue_number: Number(issue.issue_number || 0),
    targets: selected,
    title: issue.title || '',
    description: issue.description || '',
    context: _orgDispatchPayloadContext(preview),
    scene_id: org.org_scene_id || ''
  };
  var request = API.orgDispatches ? API.orgDispatches.create(org.id, payload) :
    API.post('/orgs/' + org.id + '/dispatches', payload);
  request.then(function(dispatch) {
    var overlay = document.getElementById('orgDispatchWizardOverlay');
    if (overlay) overlay.remove();
    app.showToast(I18N.t("organizations.workbench.demands.dispatch_success"));
    _orgWorkbench.dispatch = dispatch || null;
    _orgSetTab('dispatches', dispatch || null);
  }).catch(function(e) {
    app.showToast(_orgErrorText(e));
    if (btn) { btn.disabled = false; btn.textContent = I18N.t("organizations.workbench.demands.dispatch_confirm"); }
  });
}

function _orgDispatchPanel() {
  var org = _orgWorkbench.org || {};
  var canWrite = _orgCanWrite(org.role);
  return '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap">' +
    '<div><div style="font-size:15px;font-weight:600;color:var(--t1)">' + I18N.t("organizations.workbench.dispatches.title") + '</div>' +
    '<div style="font-size:12px;color:var(--t3);margin-top:3px">' + I18N.t("organizations.workbench.dispatches.subtitle") + '</div></div>' +
    '<div style="display:flex;gap:8px"><button onclick="_loadOrgDispatches()" style="padding:8px 12px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' + I18N.t("common.refresh") + '</button>' +
    (canWrite ? '<button onclick="_orgSetTab(\'demands\')" style="padding:8px 12px;border:none;border-radius:8px;background:var(--a);color:#fff;cursor:pointer;font-size:12px">' + I18N.t("organizations.workbench.demands.dispatch") + '</button>' : '') +
    '</div></div><div id="orgDispatchList"></div>';
}

function _loadOrgDispatches() {
  var org = _orgWorkbench.org || {};
  _orgPanel(_orgDispatchPanel());
  var request = API.orgDispatches ? API.orgDispatches.list(org.id, 50) :
    API.get('/orgs/' + org.id + '/dispatches?limit=50');
  request.then(function(data) {
    var items = (data && data.items) || [];
    _orgWorkbench.dispatches = items;
    var list = document.getElementById('orgDispatchList');
    if (!list) return;
    if (!items.length) {
      list.innerHTML = '<div style="border:1px dashed var(--b);border-radius:12px;padding:34px;text-align:center;color:var(--t3)">' + I18N.t("organizations.workbench.dispatches.empty") + '</div>';
      return;
    }
    list.innerHTML = items.map(function(item, idx) {
      var st = item.status || 'pending';
      return '<div onclick="_orgOpenDispatch(' + idx + ')" style="border:1px solid var(--b);border-radius:12px;padding:14px;margin-bottom:10px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;background:' +
        (st === 'failed' ? '#fffafa' : st === 'partial_failed' ? '#fffdf7' : 'var(--c)') + '">' +
        '<div><div style="font-size:13px;font-weight:600;color:var(--t1)">Dispatch #' + String(item.dispatch_id || item.id || '') + '</div>' +
        '<div style="font-size:11px;color:var(--t3);margin-top:4px">' + I18N.t("organizations.workbench.dispatches.org_issue") + ' #' + Number(item.org_issue_number || 0) + ' · ' +
        Number(item.target_count || (item.targets_requested || []).length || 0) + ' ' + I18N.t("organizations.workbench.dispatches.targets") + ' · ' + _orgpEsc(item.created_at || '') + '</div></div>' +
        '<div>' + _orgStatusBadge(st) + '</div></div>';
    }).join('');
  }).catch(function(e) {
    var list = document.getElementById('orgDispatchList');
    if (list) list.innerHTML = '<div style="color:var(--r);padding:20px">' + _orgpEsc(_orgErrorText(e)) + '</div>';
  });
}

function _orgOpenDispatch(index) {
  var item = _orgWorkbench.dispatches[index];
  if (!item) return;
  _showOrgDispatchDetail(item.dispatch_id || item.id);
}

function _orgTargetRow(target, index, canWrite) {
  var status = target.status || 'pending';
  var failed = status === 'failed' || status === 'timeout_failed';
  var repoKey = target.repo_key || '';
  var issueLink = target.child_issue_number
    ? '<a href="' + _orgpEsc(_vcsUrl(repoKey, '/issues/' + Number(target.child_issue_number))) + '" target="_blank" rel="noopener" style="color:var(--a)">#' + Number(target.child_issue_number) + '</a>'
    : '—';
  var prLink = target.pr_number
    ? '<a href="' + _orgpEsc(_vcsUrl(repoKey, '/pull/' + Number(target.pr_number))) + '" target="_blank" rel="noopener" style="color:var(--a)">#' + Number(target.pr_number) + '</a>'
    : '—';
  var retry = canWrite && failed
    ? '<button onclick="_retryOrgDispatchTarget(' + index + ')" style="padding:5px 9px;border:1px solid var(--b);border-radius:7px;background:var(--c);color:var(--a);cursor:pointer;font-size:11px">' + I18N.t("organizations.workbench.dispatch.retry") + '</button>'
    : '';
  return '<tr class="' + _orgStatusClass(status) + '" style="background:' + (failed ? '#fffafa' : (status === 'partial_failed' ? '#fffdf7' : 'transparent')) + '">' +
    '<td style="padding:10px 8px;border-bottom:1px solid var(--b);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;word-break:break-all">' + _orgpEsc(repoKey) + '</td>' +
    '<td style="padding:10px 8px;border-bottom:1px solid var(--b);font-size:12px">' + issueLink + '</td>' +
    '<td style="padding:10px 8px;border-bottom:1px solid var(--b);font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">' + (target.child_task_id ? _orgpEsc(target.child_task_id) : '—') + '</td>' +
    '<td style="padding:10px 8px;border-bottom:1px solid var(--b);font-size:12px">' + prLink + '</td>' +
    '<td style="padding:10px 8px;border-bottom:1px solid var(--b)">' + _orgStatusBadge(status) + '</td>' +
    '<td style="padding:10px 8px;border-bottom:1px solid var(--b);font-size:11px;color:' + (failed ? 'var(--r)' : 'var(--t3)') + ';max-width:260px;word-break:break-word">' + _orgpEsc(target.error || '—') + '</td>' +
    '<td style="padding:10px 8px;border-bottom:1px solid var(--b)">' + retry + '</td></tr>';
}

function _renderOrgDispatchDetail(dispatch) {
  var org = _orgWorkbench.org || {};
  if (!dispatch) return '<div style="color:var(--t3);padding:20px">' + I18N.t("organizations.workbench.dispatches.not_found") + '</div>';
  var status = dispatch.status || 'pending';
  var canWrite = _orgCanWrite(org.role);
  var inFlight = ['dispatching', 'running', 'aggregating'].indexOf(status) >= 0;
  var targets = dispatch.targets || [];
  var warning = (status === 'failed' || status === 'partial_failed')
    ? '<div style="border:1px solid ' + (status === 'failed' ? '#f3b8b8' : '#f4ce8e') + ';background:' + (status === 'failed' ? '#fdecec' : '#fff4e5') + ';color:' + (status === 'failed' ? '#c62828' : '#b45309') + ';border-radius:9px;padding:10px 12px;font-size:12px;margin-bottom:12px">' +
      I18N.t(status === 'failed' ? "organizations.workbench.dispatches.failed_hint" : "organizations.workbench.dispatches.partial_hint") + '</div>'
    : '';
  var cancelBtn = canWrite && inFlight
    ? '<button onclick="_cancelOrgDispatch()" style="padding:7px 12px;border:1px solid var(--r);border-radius:8px;background:transparent;color:var(--r);cursor:pointer;font-size:12px">' + I18N.t("organizations.workbench.dispatch.cancel") + '</button>'
    : '';
  var rows = targets.length ? targets.map(function(target, idx) {
    return _orgTargetRow(target, idx, canWrite);
  }).join('') : '<tr><td colspan="7" style="padding:24px;text-align:center;color:var(--t3)">' + I18N.t("organizations.workbench.dispatches.no_targets") + '</td></tr>';
  return '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px;flex-wrap:wrap">' +
    '<div><button onclick="_orgSetTab(\'dispatches\')" style="padding:0;border:none;background:transparent;color:var(--a);cursor:pointer;font-size:13px">' + I18N.t("organizations.workbench.dispatches.back") + '</button>' +
    '<div style="font-size:16px;font-weight:700;color:var(--t1);margin-top:8px">Dispatch #' + String(dispatch.dispatch_id || dispatch.id || '') + '</div>' +
    '<div style="font-size:12px;color:var(--t3);margin-top:4px">' + I18N.t("organizations.workbench.dispatches.org_issue") + ' #' + Number(dispatch.org_issue_number || 0) + ' · ' + _orgpEsc(dispatch.created_at || '') + '</div></div>' +
    '<div style="display:flex;gap:8px;align-items:center">' + _orgStatusBadge(status) +
    '<button onclick="_orgRefreshDispatch()" style="padding:7px 12px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' + I18N.t("common.refresh") + '</button>' + cancelBtn + '</div></div>' +
    warning +
    '<div style="overflow-x:auto;border:1px solid var(--b);border-radius:12px"><table style="width:100%;border-collapse:collapse;min-width:920px">' +
    '<thead><tr style="background:var(--bg);color:var(--t3);font-size:11px;text-align:left">' +
    '<th style="padding:9px 8px">' + I18N.t("organizations.workbench.dispatch.repo") + '</th>' +
    '<th style="padding:9px 8px">' + I18N.t("organizations.workbench.dispatch.child_issue") + '</th>' +
    '<th style="padding:9px 8px">' + I18N.t("organizations.workbench.dispatch.task") + '</th>' +
    '<th style="padding:9px 8px">PR</th><th style="padding:9px 8px">' + I18N.t("organizations.workbench.dispatch.status") + '</th>' +
    '<th style="padding:9px 8px">' + I18N.t("organizations.workbench.dispatch.error") + '</th><th style="padding:9px 8px"></th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table></div>';
}

function _showOrgDispatchDetail(dispatchId, preset) {
  var org = _orgWorkbench.org || {};
  if (preset) {
    _orgWorkbench.dispatch = preset;
    _orgPanel(_renderOrgDispatchDetail(preset));
    return;
  }
  _orgPanel('<div style="padding:24px;color:var(--t3)">' + I18N.t("organizations.loading") + '</div>');
  var request = API.orgDispatches ? API.orgDispatches.detail(org.id, dispatchId) :
    API.get('/orgs/' + org.id + '/dispatches/' + dispatchId);
  request.then(function(dispatch) {
    _orgWorkbench.dispatch = dispatch;
    _orgPanel(_renderOrgDispatchDetail(dispatch));
  }).catch(function(e) {
    _orgPanel('<div style="color:var(--r);padding:20px">' + _orgpEsc(_orgErrorText(e)) + '</div>');
  });
}

function _orgRefreshDispatch() {
  var dispatch = _orgWorkbench.dispatch || {};
  var id = dispatch.dispatch_id || dispatch.id;
  if (id) _showOrgDispatchDetail(id);
}

function _cancelOrgDispatch() {
  var org = _orgWorkbench.org || {};
  var dispatch = _orgWorkbench.dispatch || {};
  var id = dispatch.dispatch_id || dispatch.id;
  if (!id || !_orgCanWrite(org.role)) return;
  app.showConfirm(I18N.t("organizations.workbench.dispatches.cancel_confirm", {id: id}), function() {
    var request = API.orgDispatches ? API.orgDispatches.cancel(org.id, id) :
      API.post('/orgs/' + org.id + '/dispatches/' + id + '/cancel', {});
    request.then(function(result) {
      if (result && result.ok === false) {
        app.showToast(I18N.t("organizations.workbench.dispatches.cancel_partial"));
      } else {
        app.showToast(I18N.t("organizations.workbench.dispatches.cancel_success"));
      }
      _showOrgDispatchDetail(id);
    }).catch(function(e) {
      app.showToast(_orgErrorText(e));
    });
  }, {danger: true, confirmText: I18N.t("organizations.workbench.dispatch.cancel")});
}

function _retryOrgDispatchTarget(index) {
  var org = _orgWorkbench.org || {};
  var dispatch = _orgWorkbench.dispatch || {};
  var target = (dispatch.targets || [])[index];
  var id = dispatch.dispatch_id || dispatch.id;
  if (!target || !id || !_orgCanWrite(org.role)) return;
  var request = API.orgDispatches ? API.orgDispatches.retryTarget(org.id, id, target.repo_key) :
    API.post('/orgs/' + org.id + '/dispatches/' + id + '/targets/' + encodeURIComponent(target.repo_key) + '/retry', {});
  request.then(function() {
    app.showToast(I18N.t("organizations.workbench.dispatches.retry_started"));
    _showOrgDispatchDetail(id);
  }).catch(function(e) {
    app.showToast(_orgErrorText(e));
  });
}


function renderOrgPipeline(orgId) {
  var pc = document.getElementById('pageContent');
  if (!pc) return;
  var id = String(orgId == null ? '' : orgId).trim();
  if (!id) { app.nav('organizations'); return; }
  if (typeof app !== 'undefined' && app && typeof app.syncOrgContext === 'function') {
    app.syncOrgContext(id);
  }
  _orgWorkbench.initialTab = 'overview';
  _orgWorkbench.contentTab = 'context';
  pc.innerHTML = '<div id="orgWorkbench" style="padding:20px;max-width:1240px;margin:0 auto">' +
    '<div style="color:var(--t3);padding:32px;text-align:center">' + I18N.t("organizations.loading") + '</div></div>';
  _orgpLoadBase(id);
}

function _orgpLoadBase(orgId) {
  Promise.all([
    API.get('/orgs/' + orgId),
    API.get('/orgs/' + orgId + '/members')
  ]).then(function(results) {
    _orgWorkbench.org = results[0] || {};
    _orgWorkbench.members = results[1] || [];
    _orgWorkbench.activeTab = 'overview';
    _orgRenderWorkspace();
  }).catch(function(e) {
    var root = document.getElementById('orgWorkbench');
    if (root) root.innerHTML = '<div style="color:var(--r);padding:24px">' +
      I18N.t("organizations.load_failed", {msg: _orgpEsc(_orgErrorText(e))}) + '</div>';
  });
}

function _orgRenderWorkspace() {
  var root = document.getElementById('orgWorkbench');
  var org = _orgWorkbench.org;
  if (!root || !org) return;
  var orgId = String(org.id == null ? '' : org.id);
  var badge = org.is_personal ? '<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:var(--c);color:var(--t2);margin-left:6px">' + I18N.t("organizations.badge_personal") + '</span>' : '';
  root.innerHTML = '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:16px;flex-wrap:wrap">' +
    '<div>' +
    '<a onclick="app.nav(\'organizations\', \'' + orgId + '\')" style="cursor:pointer;color:var(--a);font-size:13px">' + I18N.t("organizations.pipeline.back_detail") + '</a>' +
    '<div style="display:flex;align-items:center;gap:10px;margin-top:10px">' +
    '<div style="width:44px;height:44px;border-radius:12px;background:var(--c);display:flex;align-items:center;justify-content:center;font-size:20px">🏢</div>' +
    '<div><div style="font-weight:700;font-size:18px;color:var(--t1)">' + _orgpEsc(org.name || '') + badge + '</div>' +
    '<div style="font-size:12px;color:var(--t3)">' + _orgpEsc(org.slug || '') + ' · ' + _orgWorkbench.members.length + ' ' + I18N.t("organizations.members") + '</div></div></div>' +
    '<div style="font-size:13px;color:var(--t2);margin-top:8px;max-width:760px">' + _orgpEsc(org.description || I18N.t("organizations.detail.no_description")) + '</div>' +
    '</div>' +
    '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
    '<button onclick="_orgRefresh()" style="padding:8px 14px;border:1px solid var(--b);border-radius:9px;background:var(--c);color:var(--t1);cursor:pointer;font-size:12px">' + I18N.t("common.refresh") + '</button>' +
    '</div></div>' +
    '<div id="orgSummaryCards">' + _orgRenderCards() + '</div>' +
    '<div id="orgTabs" style="display:flex;gap:6px;border-bottom:1px solid var(--b);margin:18px 0 14px;overflow-x:auto"></div>' +
    '<div id="orgTabPanel"></div>';
  _orgSetTab(_orgWorkbench.initialTab || 'overview');
}

function _orgRefresh() {
  if (_orgWorkbench.org && _orgWorkbench.org.id != null) {
    var deepTab = _orgParseContentTab(window.location.hash);
    _orgWorkbench.contentTab = deepTab || 'context';
    _orgWorkbench.initialTab = deepTab ? 'context' : 'overview';
    _orgpLoadBase(String(_orgWorkbench.org.id));
  }
}

function _orgOverviewPanel() {
  return '<div style="display:flex;flex-direction:column;gap:12px">' +
    '<div style="border:1px solid var(--b);border-radius:12px;padding:16px;font-size:13px;color:var(--t2);line-height:1.9">' +
    I18N.t("organizations.pipeline.hint") + '</div></div>';
}

window.renderOrgPipeline = renderOrgPipeline;


// ===== Issue #2263：关联仓库多选列表 =====
var _orgRepoPickerState = {orgId: '', repos: [], selected: {}, onDone: null};

function _showOrgRepoPicker(orgId, onDone) {
  var overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.25);backdrop-filter:blur(3px);z-index:1002;opacity:0;transition:opacity .2s';
  var dlg = document.createElement('div');
  dlg.style.cssText = 'position:fixed;left:50%;top:50%;transform:translate(-50%,-50%) scale(1.03);background:var(--c,#fff);border-radius:12px;padding:20px;max-width:560px;width:92%;box-shadow:0 8px 34px rgba(0,0,0,.16);font-family:-apple-system,BlinkMacSystemFont,sans-serif;transition:transform .2s cubic-bezier(.4,0,.2,1)';
  dlg.innerHTML =
    '<div style="font-size:15px;font-weight:600;margin-bottom:4px">' + I18N.t("organizations.workbench.repos.picker_title") + '</div>' +
    '<div style="font-size:12px;color:var(--t3);margin-bottom:12px">' + I18N.t("organizations.workbench.repos.picker_hint") + '</div>' +
    '<input id="orgRepoPickerSearch" type="text" placeholder="' + I18N.t("organizations.workbench.repos.picker_search") + '" ' +
      'style="width:100%;padding:8px 10px;border:1px solid var(--b);border-radius:8px;background:var(--c);color:var(--t1);font-size:12px;box-sizing:border-box;margin-bottom:10px" oninput="_orgRepoPickerFilter()">' +
    '<div id="orgRepoPickerList" style="max-height:320px;overflow-y:auto;border:1px solid var(--b);border-radius:8px;padding:4px">' +
      '<div style="padding:18px;text-align:center;color:var(--t3);font-size:12px">' + I18N.t("organizations.loading") + '</div></div>' +
    '<div style="display:flex;gap:8px;justify-content:space-between;align-items:center;margin-top:12px">' +
      '<div style="font-size:11px;color:var(--t3)" id="orgRepoPickerCount"></div>' +
      '<div style="display:flex;gap:8px">' +
        '<button id="_orgRepoPickerCancel" style="padding:8px 14px;border:none;border-radius:8px;background:rgba(0,0,0,.06);font-size:12px;cursor:pointer;color:var(--t1)">' + I18N.t("common.cancel") + '</button>' +
        '<button id="_orgRepoPickerOK" style="padding:8px 14px;border:none;border-radius:8px;background:var(--a);color:#fff;font-size:12px;cursor:pointer;font-weight:600">' + I18N.t("organizations.workbench.repos.picker_confirm") + '</button>' +
      '</div></div>';
  overlay.appendChild(dlg);
  document.body.appendChild(overlay);
  requestAnimationFrame(function() { overlay.style.opacity = '1'; dlg.style.transform = 'translate(-50%,-50%) scale(1)'; });
  function _close() { overlay.style.opacity = '0'; dlg.style.transform = 'translate(-50%,-50%) scale(1.03)'; setTimeout(function() { overlay.remove(); }, 200); }
  document.getElementById('_orgRepoPickerCancel').onclick = _close;
  overlay.onclick = function(e) { if (e.target === overlay) _close(); };

  _orgRepoPickerState = {orgId: orgId, repos: [], selected: {}, onDone: onDone || null};
  document.getElementById('_orgRepoPickerOK').onclick = function() {
    var keys = Object.keys(_orgRepoPickerState.selected).filter(function(k) { return _orgRepoPickerState.selected[k]; });
    if (!keys.length) { app.showToast(I18N.t("organizations.workbench.repos.picker_none")); return; }
    var btn = document.getElementById('_orgRepoPickerOK');
    btn.disabled = true;
    btn.textContent = I18N.t("organizations.workbench.repos.picker_binding");
    API.post('/orgs/' + orgId + '/repos/batch', {repo_keys: keys}).then(function(res) {
      _close();
      var bound = (res && res.bound) || 0;
      var failed = (res && res.failed) || 0;
      if (failed) {
        var firstErr = ((res.results || []).filter(function(r) { return !r.ok; })[0] || {});
        app.showToast(I18N.t("organizations.workbench.repos.picker_partial",
          {ok: bound, failed: failed, msg: firstErr.message || firstErr.error_code || ''}));
      } else {
        app.showToast(I18N.t("organizations.workbench.repos.picker_success", {n: bound}));
      }
      if (_orgRepoPickerState.onDone) { _orgRepoPickerState.onDone(); }
      else if (typeof _orgpLoadRepos === 'function') { _orgpLoadRepos(); }
    }).catch(function(e) {
      btn.disabled = false;
      btn.textContent = I18N.t("organizations.workbench.repos.picker_confirm");
      app.showToast((e && e.message) || String(e));
    });
  };

  API.get('/orgs/' + orgId + '/repos/available').then(function(d) {
    _orgRepoPickerState.repos = (d && d.repos) || [];
    _orgRepoPickerRender();
  }).catch(function(e) {
    var list = document.getElementById('orgRepoPickerList');
    if (list) list.innerHTML = '<div style="padding:18px;text-align:center;color:var(--r);font-size:12px">' + _orgpEsc((e && e.message) || e) + '</div>';
  });
}

function _orgRepoPickerRender() {
  var list = document.getElementById('orgRepoPickerList');
  if (!list) return;
  var qEl = document.getElementById('orgRepoPickerSearch');
  var q = qEl ? String(qEl.value || '').trim().toLowerCase() : '';
  var repos = (_orgRepoPickerState.repos || []).filter(function(r) {
    if (!q) return true;
    return String(r.full_name || '').toLowerCase().indexOf(q) >= 0 ||
      String(r.repo_key || '').toLowerCase().indexOf(q) >= 0;
  });
  if (!repos.length) {
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3);font-size:12px">' +
      I18N.t("organizations.workbench.repos.picker_empty") + '</div>';
    _orgRepoPickerUpdateCount();
    return;
  }
  list.innerHTML = repos.map(function(r) {
    var key = String(r.repo_key || '');
    var bound = !!r.bound_here;
    var checked = !!_orgRepoPickerState.selected[key];
    var note = bound
      ? '<span style="font-size:10px;padding:1px 6px;border-radius:4px;background:var(--bg);color:var(--t3);margin-left:6px">' + I18N.t("organizations.workbench.repos.picker_bound") + '</span>'
      : (r.from_personal ? '<span style="font-size:10px;padding:1px 6px;border-radius:4px;background:var(--bg);color:var(--t3);margin-left:6px">' + I18N.t("organizations.workbench.repos.picker_from_personal") + '</span>' : '');
    return '<label style="display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:7px;cursor:' + (bound ? 'default' : 'pointer') + ';' + (bound ? 'opacity:.65;' : '') + '">' +
      '<input type="checkbox" data-repo-key="' + _orgpEsc(key) + '" ' + (checked ? 'checked' : '') + ' ' + (bound ? 'disabled' : '') +
        ' onchange="_orgRepoPickerToggle(this)" style="width:15px;height:15px;accent-color:var(--a)">' +
      '<div style="flex:1;min-width:0"><div style="font-size:13px;color:var(--t1)">' + _orgpEsc(r.full_name || key) + note + '</div>' +
      '<div style="font-size:11px;color:var(--t3);word-break:break-all">' + _orgpEsc(key) + '</div></div></label>';
  }).join('');
  _orgRepoPickerUpdateCount();
}

function _orgRepoPickerToggle(el) {
  var key = el && el.getAttribute ? el.getAttribute('data-repo-key') : '';
  if (!key) return;
  _orgRepoPickerState.selected[key] = !!el.checked;
  _orgRepoPickerUpdateCount();
}

function _orgRepoPickerFilter() { _orgRepoPickerRender(); }

function _orgRepoPickerUpdateCount() {
  var countEl = document.getElementById('orgRepoPickerCount');
  if (!countEl) return;
  var n = Object.keys(_orgRepoPickerState.selected || {}).filter(function(k) { return _orgRepoPickerState.selected[k]; }).length;
  countEl.textContent = I18N.t("organizations.workbench.repos.picker_count", {n: n});
}
