/*
 * Issue #249: 引擎失联后场景设置引擎计数徽章未同步 — 前端测试
 *
 * 运行：node tests/js/test_issue_249_engine_count_sync.js
 *
 * 验证点：
 *   1. _filterAvailableEngineIds 仅保留 online/recently_lost 引擎
 *   2. offline/about_to_gc 引擎被过滤掉
 *   3. agentEngines 为空时退回原始 engineIds（fallback）
 *   4. agentType 不存在时退回原始 engineIds（fallback）
 *   5. engineIds 为空时返回空数组
 *   6. 引擎恢复后计数自动恢复
 */

const assert = require('assert');

// ── 模拟引擎数据 ──
function mkEngine(id, status) {
  return { id: id, name: 'eng-' + id, status: status || 'online', tags: ['coding'], scope: 'global' };
}

// ── 提取 _filterAvailableEngineIds 逻辑（与 dashboard.js 一致）──
function _filterAvailableEngineIds(engineIds, agentEngines, agentType) {
  if (!agentEngines || !agentEngines[agentType] || !agentEngines[agentType].length) {
    return engineIds || [];
  }
  const availMap = {};
  agentEngines[agentType].forEach(function(e) {
    if (e.status === 'online' || e.status === 'recently_lost') availMap[e.id] = true;
  });
  return (engineIds || []).filter(function(id) { return availMap[id]; });
}

// ── 测试 ──

function testFiltersOfflineEngines() {
  const agentEngines = {
    coding: [
      mkEngine(1, 'online'),
      mkEngine(2, 'offline'),
      mkEngine(3, 'about_to_gc'),
    ],
  };
  const result = _filterAvailableEngineIds([1, 2, 3], agentEngines, 'coding');
  assert.deepStrictEqual(result, [1], 'offline/about_to_gc engines should be filtered out');
  console.log('  PASS: offline/about_to_gc engines filtered out');
}

function testKeepsRecentlyLost() {
  const agentEngines = {
    coding: [
      mkEngine(1, 'online'),
      mkEngine(2, 'recently_lost'),
      mkEngine(3, 'offline'),
    ],
  };
  const result = _filterAvailableEngineIds([1, 2, 3], agentEngines, 'coding');
  assert.deepStrictEqual(result, [1, 2], 'recently_lost engines should be kept');
  console.log('  PASS: recently_lost engines kept');
}

function testAllOffline() {
  const agentEngines = {
    coding: [
      mkEngine(1, 'offline'),
      mkEngine(2, 'about_to_gc'),
    ],
  };
  const result = _filterAvailableEngineIds([1, 2], agentEngines, 'coding');
  assert.deepStrictEqual(result, [], 'all offline engines should return empty');
  console.log('  PASS: all offline engines return empty');
}

function testFallbackWhenAgentEnginesEmpty() {
  const result = _filterAvailableEngineIds([1, 2, 3], {}, 'coding');
  assert.deepStrictEqual(result, [1, 2, 3], 'empty agentEngines should fallback to original ids');
  console.log('  PASS: empty agentEngines fallback to original');
}

function testFallbackWhenAgentEnginesNull() {
  const result = _filterAvailableEngineIds([1, 2, 3], null, 'coding');
  assert.deepStrictEqual(result, [1, 2, 3], 'null agentEngines should fallback to original ids');
  console.log('  PASS: null agentEngines fallback to original');
}

function testFallbackWhenAgentTypeMissing() {
  const agentEngines = { sre: [mkEngine(1, 'online')] };
  const result = _filterAvailableEngineIds([1, 2, 3], agentEngines, 'coding');
  assert.deepStrictEqual(result, [1, 2, 3], 'missing agentType should fallback to original ids');
  console.log('  PASS: missing agentType fallback to original');
}

function testFallbackWhenAgentTypeEmptyList() {
  const agentEngines = { coding: [] };
  const result = _filterAvailableEngineIds([1, 2, 3], agentEngines, 'coding');
  assert.deepStrictEqual(result, [1, 2, 3], 'empty agentType list should fallback to original ids');
  console.log('  PASS: empty agentType list fallback to original');
}

function testEmptyEngineIds() {
  const agentEngines = { coding: [mkEngine(1, 'online')] };
  const result = _filterAvailableEngineIds([], agentEngines, 'coding');
  assert.deepStrictEqual(result, [], 'empty engineIds should return empty');
  console.log('  PASS: empty engineIds returns empty');
}

function testNullEngineIds() {
  const agentEngines = { coding: [mkEngine(1, 'online')] };
  const result = _filterAvailableEngineIds(null, agentEngines, 'coding');
  assert.deepStrictEqual(result, [], 'null engineIds should return empty');
  console.log('  PASS: null engineIds returns empty');
}

function testEngineRecoveryRestoresCount() {
  // 场景：引擎 2 从 offline 恢复到 online，计数应自动恢复
  const agentEnginesBefore = {
    coding: [mkEngine(1, 'online'), mkEngine(2, 'offline')],
  };
  const before = _filterAvailableEngineIds([1, 2], agentEnginesBefore, 'coding');
  assert.deepStrictEqual(before, [1], 'offline engine should not count');

  const agentEnginesAfter = {
    coding: [mkEngine(1, 'online'), mkEngine(2, 'online')],
  };
  const after = _filterAvailableEngineIds([1, 2], agentEnginesAfter, 'coding');
  assert.deepStrictEqual(after, [1, 2], 'recovered engine should count again');
  console.log('  PASS: engine recovery restores count');
}

function testEngineIdNotInAgentEngines() {
  // 场景：engineIds 包含一个不在 agentEngines 列表中的 ID（已删除的引擎）
  const agentEngines = { coding: [mkEngine(1, 'online')] };
  const result = _filterAvailableEngineIds([1, 99], agentEngines, 'coding');
  assert.deepStrictEqual(result, [1], 'engine id not in agentEngines should be filtered out');
  console.log('  PASS: unknown engine id filtered out');
}

function testAllOnline() {
  const agentEngines = {
    coding: [mkEngine(1, 'online'), mkEngine(2, 'online'), mkEngine(3, 'online')],
  };
  const result = _filterAvailableEngineIds([1, 2, 3], agentEngines, 'coding');
  assert.strictEqual(result.length, 3, 'all online engines should be kept');
  console.log('  PASS: all online engines kept');
}

// ── 运行所有测试 ──
console.log('Issue #249: Engine Count Sync Tests\n');

let passed = 0, failed = 0;
const tests = [
  testFiltersOfflineEngines,
  testKeepsRecentlyLost,
  testAllOffline,
  testFallbackWhenAgentEnginesEmpty,
  testFallbackWhenAgentEnginesNull,
  testFallbackWhenAgentTypeMissing,
  testFallbackWhenAgentTypeEmptyList,
  testEmptyEngineIds,
  testNullEngineIds,
  testEngineRecoveryRestoresCount,
  testEngineIdNotInAgentEngines,
  testAllOnline,
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
