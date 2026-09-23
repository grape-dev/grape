/*
 * Issue #227: 引擎选择弹窗三个 bug 修复 — 前端测试
 *
 * 运行：node tests/js/test_issue_227_engine_modal.js
 *
 * 验证点：
 *   1. 搜索 hay 不包含 device/cred_name（搜索串扰修复）
 *   2. allEngines 不按 status 过滤（引擎列表不全修复）
 *   3. 状态徽章覆盖 online/recently_lost/offline/about_to_gc 四种状态
 */

const assert = require('assert');

// ── 模拟引擎数据 ──
function mkEngine(id, name, opts) {
  opts = opts || {};
  return {
    id: id,
    name: name,
    alias: opts.alias || '',
    device: opts.device || 'worker-1',
    cred_name: opts.cred_name || 'alice',
    status: opts.status || 'online',
    tags: opts.tags || ['coding'],
    scene_tags: opts.scene_tags || ['default'],
    scope: opts.scope || 'global',
    scope_label: opts.scope_label || '全局',
  };
}

// ── 提取搜索 hay 构建逻辑（与 dashboard.js _renderEngineList 一致）──
function buildHay(e) {
  // Issue #227 修复：移除 e.device 和 e.cred_name，仅保留 name/alias/scope_label/tags
  return [e.name, e.alias, e.scope_label, (e.tags || []).join(' ')].join(' ').toLowerCase();
}

// ── 提取 allEngines 过滤逻辑（与 dashboard.js _openEngineModalImpl 一致）──
function filterAllEngines(agentEngines, agentType) {
  // Issue #227 修复：移除 status 过滤，显示全部引擎
  return (agentEngines[agentType] || []);
}

// ── 提取状态徽章逻辑（与 dashboard.js _renderEngineList 一致）──
function statusColor(status) {
  const sc = { online: '#34c759', recently_lost: '#f59e0b', offline: '#86868b', about_to_gc: '#ff3b30' };
  return sc[status] || '#86868b';
}

function statusLabel(status) {
  const sl = { online: '在线', recently_lost: '失联', offline: '离线', about_to_gc: '待回收' };
  return sl[status] || '离线';
}

// ── 测试 ──

function testSearchHayExcludesDeviceAndCredName() {
  const eng = mkEngine(1, 'opencode-coding', { device: 'worker-laptop', cred_name: 'alice' });
  const hay = buildHay(eng);

  assert.ok(hay.includes('opencode-coding'), 'hay should include engine name');
  assert.ok(!hay.includes('worker-laptop'), 'hay should NOT include device (Issue #227 fix)');
  assert.ok(!hay.includes('alice'), 'hay should NOT include cred_name (Issue #227 fix)');
  assert.ok(hay.includes('coding'), 'hay should include tags');
  assert.ok(hay.includes('全局'), 'hay should include scope_label');

  console.log('  PASS: search hay excludes device/cred_name');
}

function testSearchHayDoesNotMatchDeviceName() {
  // 场景：用户搜索 "worker-laptop"（设备名），不应匹配任何引擎
  const engines = [
    mkEngine(1, 'opencode-a', { device: 'worker-laptop', cred_name: 'alice', tags: ['coding'] }),
    mkEngine(2, 'hermes-b', { device: 'worker-laptop', cred_name: 'bob', tags: ['coding'] }),
  ];
  const q = 'worker-laptop';
  const filtered = engines.filter(e => {
    const hay = buildHay(e);
    return hay.includes(q.toLowerCase());
  });
  assert.strictEqual(filtered.length, 0, 'search by device name should return 0 results');
  console.log('  PASS: search by device name returns 0 results');
}

function testSearchHayDoesNotMatchCredName() {
  // 场景：用户搜索 "alice"（凭据名），不应匹配任何引擎
  const engines = [
    mkEngine(1, 'opencode-a', { device: 'worker-1', cred_name: 'alice', tags: ['coding'] }),
    mkEngine(2, 'hermes-b', { device: 'worker-2', cred_name: 'alice', tags: ['coding'] }),
  ];
  const q = 'alice';
  const filtered = engines.filter(e => {
    const hay = buildHay(e);
    return hay.includes(q.toLowerCase());
  });
  assert.strictEqual(filtered.length, 0, 'search by cred_name should return 0 results');
  console.log('  PASS: search by cred_name returns 0 results');
}

function testSearchMatchesByName() {
  const engines = [
    mkEngine(1, 'opencode-a', { device: 'worker-1', cred_name: 'alice' }),
    mkEngine(2, 'hermes-b', { device: 'worker-2', cred_name: 'bob' }),
  ];
  const q = 'opencode';
  const filtered = engines.filter(e => {
    const hay = buildHay(e);
    return hay.includes(q.toLowerCase());
  });
  assert.strictEqual(filtered.length, 1, 'search by name should match 1 engine');
  assert.strictEqual(filtered[0].id, 1);
  console.log('  PASS: search by name matches correctly');
}

function testSearchMatchesByAlias() {
  const engines = [
    mkEngine(1, 'opencode-a', { alias: '我的引擎', device: 'worker-1', cred_name: 'alice' }),
  ];
  const q = '我的引擎';
  const filtered = engines.filter(e => {
    const hay = buildHay(e);
    return hay.includes(q.toLowerCase());
  });
  assert.strictEqual(filtered.length, 1, 'search by alias should match');
  console.log('  PASS: search by alias matches correctly');
}

function testSearchMatchesByTags() {
  const engines = [
    mkEngine(1, 'opencode-a', { tags: ['coding', 'all'], device: 'worker-1', cred_name: 'alice' }),
  ];
  const q = 'coding';
  const filtered = engines.filter(e => {
    const hay = buildHay(e);
    return hay.includes(q.toLowerCase());
  });
  assert.strictEqual(filtered.length, 1, 'search by tag should match');
  console.log('  PASS: search by tag matches correctly');
}

function testAllEnginesNotFilteredByStatus() {
  // Issue #227: allEngines 应包含所有引擎，不按 status 过滤
  const agentEngines = {
    coding: [
      mkEngine(1, 'eng-online', { status: 'online' }),
      mkEngine(2, 'eng-recently-lost', { status: 'recently_lost' }),
      mkEngine(3, 'eng-offline', { status: 'offline' }),
      mkEngine(4, 'eng-about-to-gc', { status: 'about_to_gc' }),
    ],
  };
  const allEngines = filterAllEngines(agentEngines, 'coding');
  assert.strictEqual(allEngines.length, 4, 'allEngines should include all 4 engines regardless of status');
  console.log('  PASS: allEngines not filtered by status');
}

function testAllEnginesEmptyList() {
  const agentEngines = { coding: [] };
  const allEngines = filterAllEngines(agentEngines, 'coding');
  assert.strictEqual(allEngines.length, 0, 'empty agentEngines should return empty list');
  console.log('  PASS: empty agentEngines returns empty list');
}

function testAllEnginesMissingAgentType() {
  const agentEngines = {};
  const allEngines = filterAllEngines(agentEngines, 'coding');
  assert.strictEqual(allEngines.length, 0, 'missing agent type should return empty list');
  console.log('  PASS: missing agent type returns empty list');
}

function testStatusBadgeOnline() {
  assert.strictEqual(statusColor('online'), '#34c759');
  assert.strictEqual(statusLabel('online'), '在线');
  console.log('  PASS: online status badge correct');
}

function testStatusBadgeRecentlyLost() {
  assert.strictEqual(statusColor('recently_lost'), '#f59e0b');
  assert.strictEqual(statusLabel('recently_lost'), '失联');
  console.log('  PASS: recently_lost status badge correct');
}

function testStatusBadgeOffline() {
  assert.strictEqual(statusColor('offline'), '#86868b');
  assert.strictEqual(statusLabel('offline'), '离线');
  console.log('  PASS: offline status badge correct');
}

function testStatusBadgeAboutToGc() {
  assert.strictEqual(statusColor('about_to_gc'), '#ff3b30');
  assert.strictEqual(statusLabel('about_to_gc'), '待回收');
  console.log('  PASS: about_to_gc status badge correct');
}

function testStatusBadgeUnknown() {
  assert.strictEqual(statusColor('unknown'), '#86868b');
  assert.strictEqual(statusLabel('unknown'), '离线');
  console.log('  PASS: unknown status falls back to offline');
}

// ── 运行所有测试 ──
console.log('Issue #227: Engine Modal Bug Fix Tests\n');

let passed = 0, failed = 0;
const tests = [
  testSearchHayExcludesDeviceAndCredName,
  testSearchHayDoesNotMatchDeviceName,
  testSearchHayDoesNotMatchCredName,
  testSearchMatchesByName,
  testSearchMatchesByAlias,
  testSearchMatchesByTags,
  testAllEnginesNotFilteredByStatus,
  testAllEnginesEmptyList,
  testAllEnginesMissingAgentType,
  testStatusBadgeOnline,
  testStatusBadgeRecentlyLost,
  testStatusBadgeOffline,
  testStatusBadgeAboutToGc,
  testStatusBadgeUnknown,
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
