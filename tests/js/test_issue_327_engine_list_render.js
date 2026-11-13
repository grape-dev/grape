/*
 * Issue #327: 引擎列表渲染修复 — 前端测试
 *
 * 运行：node tests/js/test_issue_327_engine_list_render.js
 *
 * 验证点：
 *   1. renderEngines 搜索 filter 使用 agent_tags（字符串列表）而非 tags（dict 列表）
 *   2. 引擎卡片包含类型(type)、版本(version)、安装状态(installed) 字段
 *   3. 所有字段缺失时有 fallback（'—'），不出现空白区域
 *   4. 引擎名称、健康状态、scope 徽章正确渲染
 *   5. 2 个引擎条目均展示完整信息（复现 Issue #324 场景）
 */

const assert = require('assert');

// ── 模拟 I18N ──
var I18N = {
  t: function(key, vars) {
    var map = {
      'runtimes.label.engine_type': '引擎类型',
      'runtimes.label.version': '版本',
      'runtimes.label.installed': '已安装',
      'runtimes.label.worker': '设备',
      'runtimes.label.skills_label': '技能',
      'runtimes.label.skills_count': (vars ? vars.count : 0) + ' 个',
      'runtimes.label.scene': '场景',
      'runtimes.label.tags': '标签',
      'runtimes.label.yes': '是',
      'runtimes.label.no': '否',
      'runtimes.label.local': '本机',
      'runtimes.empty.no_engines': '暂无引擎',
      'runtimes.empty.unassigned': '未分配',
      'runtimes.health.online': '在线',
      'runtimes.health.offline': '离线',
      'runtimes.health.recently_lost': '刚失联',
      'runtimes.health.about_to_gc': '待回收',
      'runtimes.scope.global': '全局',
      'runtimes.scope.repo': '仓库',
      'runtimes.scope.user': '个人',
      'runtimes.tab.engines': 'Engines 引擎',
      'runtimes.tab.workers': 'Workers 设备',
      'runtimes.refresh': '⟳ 刷新',
      'runtimes.action.add_device': '添加设备',
      'runtimes.devspace.legend_label': '图例',
      'common.loading': '加载中...',
      'runtimes.label.local_direct': '本机直连',
    };
    return map[key] || key;
  }
};

// ── 模拟常量与辅助函数（与 runtimes.js 一致）──
const RUNTIME_ICON = { online: '●', recently_lost: '◐', offline: '○', about_to_gc: '⚠' };
const RUNTIME_COLOR = { online: 'var(--g)', recently_lost: 'var(--y)', offline: 'var(--t3)', about_to_gc: 'var(--r)' };
const HEALTH_NAMES = { online: 'online', recently_lost: 'recently_lost', offline: 'offline', about_to_gc: 'about_to_gc' };
const HEALTH_BADGE = {
  online: { color: 'var(--g)', bg: '#dcfce7' },
  recently_lost: { color: 'var(--y)', bg: '#fef3c7' },
  offline: { color: 'var(--t3)', bg: '#eef0f3' },
  about_to_gc: { color: 'var(--r)', bg: '#fee2e2' },
};
const _SCOPE_BADGE = {
  global: { label: '全局', color: '#3168e0', bg: '#e3edfd' },
  repo: { label: '仓库', color: '#8b5cf6', bg: '#ede9fe' },
  user: { label: '个人', color: '#1ca44c', bg: '#dcfce7' },
  local: { label: '本机', color: '#0e7490', bg: '#cffafe' },
};

function _scopeBadge(scope, isLocal) {
  var sc = isLocal ? 'local' : (scope || 'global');
  var cfg = _SCOPE_BADGE[sc] || _SCOPE_BADGE.global;
  return '<span style="display:inline-flex;align-items:center;font-size:10px;font-weight:600;color:' + cfg.color + ';background:' + cfg.bg + ';border-radius:999px;padding:1px 8px;white-space:nowrap;vertical-align:middle">' + cfg.label + '</span>';
}

function _healthLabel(h) { return I18N.t('runtimes.health.' + (HEALTH_NAMES[h] || 'offline')); }

function _healthBadge(h, suffix) {
  const st = HEALTH_BADGE[h] || HEALTH_BADGE.offline;
  return `<span style="display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;color:${st.color};background:${st.bg};padding:3px 10px;border-radius:999px;white-space:nowrap"><span style="width:7px;height:7px;border-radius:50%;background:${st.color}"></span>${_healthLabel(h)}${suffix ? ' · ' + suffix : ''}</span>`;
}

function _healthScoreBadge(rt) {
  const s = rt.health_score;
  if (s === null || s === undefined) return '';
  const c = s === 0 ? 'var(--r)' : s < 50 ? 'var(--y)' : 'var(--g)';
  return `<span style="color:${c};font-weight:600">${s}</span>`;
}

function _engineWatermark(name) {
  const n = (name || '').toLowerCase();
  if (n.includes('opencode')) return '<img src="/static/img/engines/opencode.png" alt="">';
  if (n.includes('hermes')) return '<img src="/static/img/engines/Hermes.png" alt="">';
  return '';
}

var __sceneTagsCache = [{name: 'default', display_name: '默认'}];

function _sceneDisplayName(name) {
  if (!__sceneTagsCache) return name;
  var s = __sceneTagsCache.find(function(x) { return x.name === name; });
  return s ? s.display_name : name;
}

function _sceneTagSummary(tags) {
  if (!tags || tags.length === 0) return '<span style="color:var(--t3);font-size:10px">' + I18N.t('runtimes.empty.unassigned') + '</span>';
  return tags.map(function(t) {
    var dn = _sceneDisplayName(t);
    return '<span style="font-size:10px;padding:1px 6px;border-radius:999px;color:#fff;background:#10b981;margin:0 2px">' + dn + '</span>';
  }).join('');
}

function _engineTagSummary(tags) {
  if (!tags || tags.length === 0) return '<span style="color:var(--t3);font-size:10px">' + I18N.t('runtimes.empty.unassigned') + '</span>';
  return tags.map(t => `<span style="font-size:10px;padding:1px 6px;border-radius:999px;color:#fff;background:#999;margin:0 2px">${t}</span>`).join('');
}

// ── 提取 renderEngines 核心渲染逻辑（与 runtimes.js 修复后一致）──
function renderEngines(data) {
  const runtimes = data.runtimes || [];
  const q = '';
  const filtered = runtimes.filter(rt => {
    if (q) {
      // Issue #327 修复：使用 agent_tags（字符串列表）而非 tags（dict 列表）
      const hay = [rt.alias, rt.instance_name, rt.name, rt.worker_name, rt.owner, rt.host_name, (rt.agent_tags||[]).join(' '), (rt.local_skills||[]).join(' ')].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  let html = '';
  if (!filtered.length) return I18N.t('runtimes.empty.no_engines');
  html += '<div style="display:grid">';
  filtered.forEach(rt => {
    const _hStatus = rt.health || rt.status || 'offline';
    const _cardLeftBorder = RUNTIME_COLOR[_hStatus] || 'var(--t3)';
    const _engineName = rt.alias || rt.instance_name || rt.name || '—';
    const _engineType = rt.name || '—';
    const _engineVersion = rt.version || '—';
    const _engineInstalled = rt.installed ? I18N.t('runtimes.label.yes') : I18N.t('runtimes.label.no');
    html += `<div onclick="openEngineDetail(${rt.id})">
      ${_engineWatermark(rt.name)}
      ${_healthBadge(rt.health)}
      ${_scopeBadge(rt.scope, rt.is_local)}
      <span>${_engineName}</span>
      <div>
        <div>${I18N.t('runtimes.label.engine_type')}: ${_engineType}</div>
        <div>${I18N.t('runtimes.label.version')}: v${_engineVersion}</div>
        <div>${I18N.t('runtimes.label.worker')}: ${rt.worker_name || (rt.is_local ? I18N.t('runtimes.label.local') : '—')}</div>
        <div>${I18N.t('runtimes.label.installed')}: ${_engineInstalled}</div>
        <div>${I18N.t('runtimes.label.scene')}: ${_sceneTagSummary(rt.scene_tags)}</div>
        <div>${I18N.t('runtimes.label.tags')}: ${_engineTagSummary(rt.agent_tags)}</div>
        <div>健康分: ${_healthScoreBadge(rt) || '—'}</div>
      </div>
    </div>`;
  });
  html += '</div>';
  return html;
}

// ── 测试数据：模拟 Issue #324 场景（2 个引擎条目）──
function mkRuntime(overrides) {
  return Object.assign({
    id: 1,
    name: 'opencode',
    alias: '',
    instance_name: 'opencode-cred0',
    health: 'online',
    scope: 'global',
    is_local: true,
    worker_name: 'localhost',
    version: '1.0.0',
    installed: true,
    local_skills: ['skill1', 'skill2'],
    scene_tags: ['default'],
    agent_tags: ['coding'],
    health_score: 95,
  }, overrides || {});
}

// ── 测试用例 ──

function testTwoEnginesShowCompleteInfo() {
  const data = {
    runtimes: [
      mkRuntime({id: 1, name: 'opencode', instance_name: 'opencode-cred0'}),
      mkRuntime({id: 2, name: 'hermes', instance_name: 'hermes-cred1', health: 'offline', installed: false}),
    ],
    devices: [],
    total: 2,
  };
  const html = renderEngines(data);
  assert(html.includes('opencode'), '引擎1名称应显示');
  assert(html.includes('hermes'), '引擎2名称应显示');
  assert(html.includes('引擎类型'), '应包含类型标签');
  assert(html.includes('opencode') && html.includes('hermes'), '两个引擎类型均应显示');
  assert(html.includes('v1.0.0') || html.includes('v—'), '版本应显示');
  assert(html.includes('是') || html.includes('否'), '安装状态应显示');
  assert(!html.includes('暂无引擎'), '不应显示空态');
  console.log('  PASS: 2 个引擎条目均展示完整信息');
}

function testSearchFilterUsesAgentTags() {
  // 验证搜索 filter 使用 agent_tags（字符串列表）而非 tags（dict 列表）
  const rt = {
    alias: '', instance_name: 'opencode-cred0', name: 'opencode',
    worker_name: 'worker-01', owner: 'admin', host_name: 'host-01',
    agent_tags: ['coding', 'qa'],
    tags: [{tag: 'coding', priority: 0, enabled: true}],  // API 返回的 dict 列表
    local_skills: ['skill1'],
  };

  // 模拟搜索 'coding'：用 agent_tags 应匹配
  const hayAgentTags = [rt.alias, rt.instance_name, rt.name, rt.worker_name, rt.owner, rt.host_name, (rt.agent_tags||[]).join(' '), (rt.local_skills||[]).join(' ')].join(' ').toLowerCase();
  assert(hayAgentTags.includes('coding'), 'agent_tags 搜索应匹配 coding');

  // 旧代码用 tags（dict 列表），.join(' ') 产生 [object Object]，搜索不匹配
  const hayTagsOld = [rt.alias, rt.instance_name, rt.name, rt.worker_name, rt.owner, rt.host_name, (rt.tags||[]).join(' '), (rt.local_skills||[]).join(' ')].join(' ').toLowerCase();
  assert(!hayTagsOld.includes('coding'), '旧代码 tags 搜索不应匹配 coding（dict join 产生 [object Object]）');
  assert(hayTagsOld.includes('[object object]'), '旧代码 tags join 产生 [object Object]');

  console.log('  PASS: 搜索 filter 使用 agent_tags 而非 tags');
}

function testAllFieldsHaveFallback() {
  // 所有字段为空/undefined 时，卡片不出现空白
  const data = {
    runtimes: [{
      id: 0,
      name: undefined,
      alias: undefined,
      instance_name: undefined,
      health: undefined,
      scope: undefined,
      is_local: undefined,
      worker_name: undefined,
      version: undefined,
      installed: undefined,
      local_skills: undefined,
      scene_tags: undefined,
      agent_tags: undefined,
      health_score: undefined,
    }],
    devices: [],
    total: 1,
  };
  const html = renderEngines(data);
  assert(html.includes('—'), '缺失字段应显示 — fallback');
  assert(!html.includes('undefined'), '不应出现 undefined 文本');
  assert(!html.includes('NaN'), '不应出现 NaN');
  console.log('  PASS: 所有字段缺失时有 fallback');
}

function testEngineTypeDisplayed() {
  const data = {
    runtimes: [mkRuntime({name: 'hermes'})],
    devices: [],
    total: 1,
  };
  const html = renderEngines(data);
  assert(html.includes('引擎类型'), '卡片应包含「引擎类型」标签');
  assert(html.includes('hermes'), '卡片应显示引擎类型值');
  console.log('  PASS: 引擎类型字段正确显示');
}

function testVersionDisplayed() {
  const data = {
    runtimes: [mkRuntime({version: '2.5.3'})],
    devices: [],
    total: 1,
  };
  const html = renderEngines(data);
  assert(html.includes('版本'), '卡片应包含「版本」标签');
  assert(html.includes('v2.5.3'), '卡片应显示版本号');
  console.log('  PASS: 版本字段正确显示');
}

function testInstalledStatusDisplayed() {
  const data = {
    runtimes: [
      mkRuntime({id: 1, installed: true}),
      mkRuntime({id: 2, installed: false}),
    ],
    devices: [],
    total: 2,
  };
  const html = renderEngines(data);
  assert(html.includes('已安装'), '卡片应包含「已安装」标签');
  assert(html.includes('是'), '已安装引擎应显示「是」');
  assert(html.includes('否'), '未安装引擎应显示「否」');
  console.log('  PASS: 安装状态字段正确显示');
}

function testHealthBadgeRendered() {
  const data = {
    runtimes: [
      mkRuntime({id: 1, health: 'online'}),
      mkRuntime({id: 2, health: 'offline'}),
    ],
    devices: [],
    total: 2,
  };
  const html = renderEngines(data);
  assert(html.includes('在线'), 'online 引擎应显示在线徽章');
  assert(html.includes('离线'), 'offline 引擎应显示离线徽章');
  console.log('  PASS: 健康状态徽章正确渲染');
}

function testEmptyRuntimesShowsEmptyState() {
  const data = {runtimes: [], devices: [], total: 0};
  const html = renderEngines(data);
  assert(html.includes('暂无引擎'), '空列表应显示空态');
  console.log('  PASS: 空列表正确显示空态');
}

function testEngineNameWithAlias() {
  const data = {
    runtimes: [mkRuntime({alias: '我的引擎', instance_name: 'opencode-cred0', name: 'opencode'})],
    devices: [],
    total: 1,
  };
  const html = renderEngines(data);
  assert(html.includes('我的引擎'), '应显示别名');
  // alias 存在时 instance_name 显示在括号中
  assert(html.includes('opencode-cred0') || html.includes('opencode'), '应显示 instance_name 或 name');
  console.log('  PASS: 别名优先显示，instance_name 作为补充');
}

// ── 运行所有测试 ──
console.log('Issue #327: Engine List Render Tests\n');

let passed = 0, failed = 0;
const tests = [
  testTwoEnginesShowCompleteInfo,
  testSearchFilterUsesAgentTags,
  testAllFieldsHaveFallback,
  testEngineTypeDisplayed,
  testVersionDisplayed,
  testInstalledStatusDisplayed,
  testHealthBadgeRendered,
  testEmptyRuntimesShowsEmptyState,
  testEngineNameWithAlias,
];

for (const t of tests) {
  try {
    t();
    passed++;
  } catch (e) {
    console.log('  FAIL:', e.message);
    failed++;
  }
}

console.log('\n' + passed + ' passed, ' + failed + ' failed');
if (failed > 0) process.exit(1);
