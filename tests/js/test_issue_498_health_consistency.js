/*
 * Issue #498: 引擎健康「在线」与「健康异常」两口径一致呈现 — 前端测试
 *
 * 运行：node tests/js/test_issue_498_health_consistency.js
 *
 * 验证点（对应 Issue #498 验收标准）：
 *   1. runtimes.js 的 _healthHelpHint / _isAbnormalEngine / _abnormalBorderColor
 *      优先采用后端统一口径 health_abnormal / health_reason（旧缓存字段缺失回退老逻辑）
 *   2. 在线 + health_abnormal=false + 旧偶发失败（cf 已衰减为 0）→ 不异常、无提示、无红框
 *   3. 在线 + health_reason 有值 → 帮助提示 key = runtimes.health.hint.<reason>
 *   4. 小样本豁免透传：health_abnormal=false 时即使旧字段 cf>0 也不判异常（后端口径优先）
 *   5. worker_version_stale 独立琥珀标签：_workerVersionStaleBadge 渲染且不参与异常判定
 *   6. zh/en locales 均含 runtimes.label.worker_version_stale(_hint)
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const RUNTIMES_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/runtimes.js'), 'utf8');
const ZH = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/runtimes.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/runtimes.json'), 'utf8'));

// ── 与 runtimes.js 保持一致的逻辑提取（Issue #498 版本）──
function _healthHelpHint(rt) {
  if (!rt) return '';
  const h = rt.health || rt.status || 'offline';
  if (h === 'about_to_gc') return 'runtimes.health.hint.about_to_gc';
  if (h === 'offline') return 'runtimes.health.hint.offline';
  if (h === 'recently_lost') return 'runtimes.health.hint.recently_lost';
  if (rt.health_reason) return 'runtimes.health.hint.' + rt.health_reason;
  if (rt.health_abnormal === false) return '';
  if ((rt.health_consecutive_failures || 0) > 0) return 'runtimes.health.hint.consecutive_failures';
  if (rt.health_heartbeat_alive === false) return 'runtimes.health.hint.heartbeat_lost';
  if (rt.health_score !== null && rt.health_score !== undefined && rt.health_score < 50) return 'runtimes.health.hint.low_score';
  return '';
}

function _isAbnormalEngine(rt) {
  if (!rt) return false;
  const h = rt.health || rt.status || 'offline';
  if (h !== 'online') return true;
  if (rt.health_abnormal !== undefined && rt.health_abnormal !== null) {
    return rt.health_abnormal === true;
  }
  return (rt.health_consecutive_failures || 0) > 0
    || rt.health_heartbeat_alive === false
    || (rt.health_score !== null && rt.health_score !== undefined && rt.health_score < 50);
}

function _abnormalBorderColor(rt) {
  const h = rt.health || rt.status || 'offline';
  if (h === 'about_to_gc' || h === 'offline') return 'var(--r)';
  if (rt.health_abnormal !== undefined && rt.health_abnormal !== null) {
    return rt.health_abnormal === true ? 'var(--r)' : '';
  }
  if ((rt.health_consecutive_failures || 0) > 0 || rt.health_heartbeat_alive === false) return 'var(--r)';
  return '';
}

// ── 静态断言：runtimes.js 已接入后端统一口径 + worker 版本标签 ──

function testBackendFlagWired() {
  assert.ok(/rt\.health_reason/.test(RUNTIMES_JS), '_healthHelpHint 应消费后端 health_reason');
  assert.ok(/rt\.health_abnormal/.test(RUNTIMES_JS), '异常判定应消费后端 health_abnormal');
  assert.ok(/function _workerVersionStaleBadge\(/.test(RUNTIMES_JS), '_workerVersionStaleBadge 函数应存在');
  assert.ok(/_workerVersionStaleBadge\(rt\)/.test(RUNTIMES_JS), '引擎卡片应渲染 worker 版本落后标签');
}

// ── 口径一致性（runtime 13031 场景）──

function testHealedEngineNotAbnormal() {
  // 自愈后的 13031：在线 + 后端判定健康（cf 衰减清零）
  const rt = { health: 'online', health_abnormal: false, health_reason: '',
               health_score: 100, health_consecutive_failures: 0,
               health_failed_tasks: 1, health_last_failure_at: '2026-10-26T01:13:00+00:00' };
  assert.strictEqual(_healthHelpHint(rt), '', '自愈后无帮助提示');
  assert.strictEqual(_isAbnormalEngine(rt), false, '自愈后不判异常（口径一致：不再仅显示在线却标异常）');
  assert.strictEqual(_abnormalBorderColor(rt), '', '自愈后无红色描边');
}

function testAbnormalOnlineShowsReason() {
  // 在线但健康异常 → 异常可见 + 原因透出
  const rt = { health: 'online', health_abnormal: true, health_reason: 'consecutive_failures',
               health_score: 25, health_consecutive_failures: 5 };
  assert.strictEqual(_healthHelpHint(rt), 'runtimes.health.hint.consecutive_failures', '异常原因 key 透出');
  assert.strictEqual(_isAbnormalEngine(rt), true, '在线 + 后端异常 → 判异常');
  assert.strictEqual(_abnormalBorderColor(rt), 'var(--r)', '红色描边强化');
}

function testSmallSampleExemptWinsLegacySignals() {
  // 小样本豁免：后端 abnormal=false 时旧字段（cf>0/低分）不再触发前端异常（旧缓存字段可能滞后）
  const rt = { health: 'online', health_abnormal: false, health_reason: '',
               health_score: 90, health_consecutive_failures: 1 };
  assert.strictEqual(_isAbnormalEngine(rt), false, '后端口径优先：小样本单次失败不判异常');
  assert.strictEqual(_healthHelpHint(rt), '', '无提示');
}

function testLegacyFallbackKept() {
  // 旧缓存无 health_abnormal 字段 → 回退老逻辑（向后兼容）
  const rt = { health: 'online', health_score: 90, health_consecutive_failures: 1 };
  assert.strictEqual(_isAbnormalEngine(rt), true, '旧缓存回退：cf>0 仍判异常');
  assert.strictEqual(_healthHelpHint(rt), 'runtimes.health.hint.consecutive_failures');
  const healthy = { health: 'online', health_score: 100, health_consecutive_failures: 0 };
  assert.strictEqual(_isAbnormalEngine(healthy), false, '旧缓存健康引擎不异常');
}

function testStateHintsStillPriority() {
  assert.strictEqual(_healthHelpHint({ health: 'offline', health_reason: 'low_score' }),
    'runtimes.health.hint.offline', '离线状态提示优先于健康分口径');
}

function testWorkerVersionStaleIndependent() {
  // worker_version_stale 不参与异常判定（独立提示）
  const rt = { health: 'online', health_abnormal: false, health_reason: '',
               health_score: 100, worker_version_stale: true };
  assert.strictEqual(_isAbnormalEngine(rt), false, '版本落后不触发异常强化');
  assert.strictEqual(_abnormalBorderColor(rt), '', '版本落后不触发红框');
}

// ── locales 双语断言 ──

function testLocaleKeys() {
  for (const k of ['worker_version_stale', 'worker_version_stale_hint']) {
    assert.ok(typeof ZH.label[k] === 'string' && ZH.label[k].length > 0, `zh 缺少 runtimes.label.${k}`);
    assert.ok(typeof EN.label[k] === 'string' && EN.label[k].length > 0, `en 缺少 runtimes.label.${k}`);
  }
  assert.ok(ZH.label.worker_version_stale_hint.includes('健康异常无关'), 'zh 提示应说明与引擎健康异常无关');
}

// ── 执行 ──

const tests = [
  testBackendFlagWired, testHealedEngineNotAbnormal, testAbnormalOnlineShowsReason,
  testSmallSampleExemptWinsLegacySignals, testLegacyFallbackKept,
  testStateHintsStillPriority, testWorkerVersionStaleIndependent, testLocaleKeys,
];
let failed = 0;
tests.forEach(fn => {
  try { fn(); console.log(`  ✓ ${fn.name}`); }
  catch (e) { failed++; console.error(`  ✗ ${fn.name}: ${e.message}`); }
});
if (failed) { console.error(`\n${failed} test(s) failed`); process.exit(1); }
console.log(`\nAll ${tests.length} tests passed.`);
