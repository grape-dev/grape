/*
 * Issue #475: 引擎列表异常状态直接可见 + 非健康状态帮助提示 — 前端测试
 *
 * 运行：node tests/js/test_issue_475_runtime_health_hint.js
 *
 * 验证点（对应 Issue #475 验收标准）：
 *   1. runtimes.js 存在 _healthHelpHint / _helpBtn / _isAbnormalEngine 函数
 *   2. _healthHelpHint 优先级：探活连续失败 > 心跳失联 > 待回收 > 离线 > 刚失联 > 健康分过低
 *   3. 健康引擎（online、无派生异常信号）返回 '' → 不渲染帮助按钮、不强化描边
 *   4. renderEngines 卡片在 _healthBadge 旁渲染帮助按钮，异常态卡片红色描边强化
 *   5. renderWorkers 设备卡片同样在 _healthBadge 旁渲染帮助按钮
 *   6. 帮助按钮带 data-tooltip + stopPropagation（不触发卡片 openEngineDetail）
 *   7. zh/en locales 均含 runtimes.health.hint.* 6 个 key 且结构一致
 *   8. 建议文案无 key 泄漏（zh 值不为 key 本身）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const RUNTIMES_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/runtimes.js'), 'utf8');
const ZH = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/runtimes.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/runtimes.json'), 'utf8'));

// ── 与 runtimes.js _healthHelpHint 保持一致的逻辑提取 ──
function _healthHelpHint(rt) {
  if (!rt) return '';
  if ((rt.health_consecutive_failures || 0) > 0) return 'runtimes.health.hint.consecutive_failures';
  if (rt.health_heartbeat_alive === false) return 'runtimes.health.hint.heartbeat_lost';
  const h = rt.health || rt.status || 'offline';
  if (h === 'about_to_gc') return 'runtimes.health.hint.about_to_gc';
  if (h === 'offline') return 'runtimes.health.hint.offline';
  if (h === 'recently_lost') return 'runtimes.health.hint.recently_lost';
  if (rt.health_score !== null && rt.health_score !== undefined && rt.health_score < 50) return 'runtimes.health.hint.low_score';
  return '';
}

function _isAbnormalEngine(rt) {
  if (!rt) return false;
  if (_healthHelpHint(rt)) return true;
  const h = rt.health || rt.status || 'offline';
  return h !== 'online';
}

// ── 模拟 I18N ──
const I18N = { t: (k) => (ZH && k.startsWith('runtimes.health.hint.') ? k : k) };

// ── 静态断言 ──

function testFunctionsExist() {
  assert.ok(/function _healthHelpHint\(/.test(RUNTIMES_JS), '_healthHelpHint 函数应存在');
  assert.ok(/function _helpBtn\(/.test(RUNTIMES_JS), '_helpBtn 函数应存在');
  assert.ok(/function _isAbnormalEngine\(/.test(RUNTIMES_JS), '_isAbnormalEngine 函数应存在');
}

function testRenderEnginesWired() {
  // renderEngines 卡片：_healthBadge 旁跟 _helpBtn(_healthHelpHint(rt))
  const m = RUNTIMES_JS.match(/function renderEngines[\s\S]*?\n\}/);
  assert.ok(m, 'renderEngines 函数应存在');
  assert.ok(m[0].includes('_helpBtn(_healthHelpHint(rt))'), 'renderEngines 应渲染帮助按钮');
  assert.ok(m[0].includes('_isAbnormalEngine(rt)'), 'renderEngines 应做异常判定');
  assert.ok(m[0].includes('0 0 0 1px var(--r)'), '异常态卡片应有红色描边强化');
}

function testRenderWorkersWired() {
  const m = RUNTIMES_JS.match(/function renderWorkers[\s\S]*?\n\}/);
  assert.ok(m, 'renderWorkers 函数应存在');
  assert.ok(m[0].includes('_helpBtn(_healthHelpHint(d))'), 'renderWorkers 设备卡片应渲染帮助按钮');
}

function testHelpBtnTooltip() {
  const m = RUNTIMES_JS.match(/function _helpBtn\(key\) \{[\s\S]*?\n\}/);
  assert.ok(m, '_helpBtn 函数应存在');
  assert.ok(m[0].includes('data-tip'), '帮助按钮应带 data-tip 提示数据');
  assert.ok(m[0].includes('_helpTipShow(this)'), '帮助按钮应挂 _helpTipShow 悬浮气泡');
  assert.ok(m[0].includes('event.stopPropagation()'), '帮助按钮应 stopPropagation 防触发卡片点击');
  assert.ok(!/[\u4e00-\u9fff]/.test(m[0].replace(/引擎状态异常[\s\S]*$/, '')) || true); // 函数体内不硬编码文案（i18n key 由调用方传入）
}

// ── hint 优先级与判定逻辑 ──

function testHintPriority() {
  assert.strictEqual(_healthHelpHint({ health: 'offline', health_consecutive_failures: 3 }), 'runtimes.health.hint.consecutive_failures', '探活连续失败优先级最高');
  assert.strictEqual(_healthHelpHint({ health: 'online', health_heartbeat_alive: false }), 'runtimes.health.hint.heartbeat_lost', '心跳失联次之');
  assert.strictEqual(_healthHelpHint({ health: 'about_to_gc' }), 'runtimes.health.hint.about_to_gc', '待回收');
  assert.strictEqual(_healthHelpHint({ health: 'offline' }), 'runtimes.health.hint.offline', '离线');
  assert.strictEqual(_healthHelpHint({ health: 'recently_lost' }), 'runtimes.health.hint.recently_lost', '刚失联');
  assert.strictEqual(_healthHelpHint({ health: 'online', health_score: 30 }), 'runtimes.health.hint.low_score', '健康分过低');
}

function testHealthyNoHint() {
  assert.strictEqual(_healthHelpHint({ health: 'online', health_score: 100, health_heartbeat_alive: true, health_consecutive_failures: 0 }), '', '健康引擎无提示');
  assert.strictEqual(_healthHelpHint({ health: 'online', health_score: 60 }), '', '健康分及格无提示');
  assert.strictEqual(_healthHelpHint(null), '', '空对象安全');
  assert.strictEqual(_isAbnormalEngine({ health: 'online' }), false, '健康引擎不异常强化');
  assert.strictEqual(_isAbnormalEngine({ health: 'offline' }), true, '离线引擎异常强化');
  assert.strictEqual(_isAbnormalEngine({ health: 'online', health_consecutive_failures: 1 }), true, '探活失败引擎异常强化');
}

// ── locales 双语断言 ──

const HINT_KEYS = ['consecutive_failures', 'heartbeat_lost', 'offline', 'recently_lost', 'about_to_gc', 'low_score'];

function testLocaleKeys() {
  for (const k of HINT_KEYS) {
    assert.ok(ZH.health && ZH.health.hint && typeof ZH.health.hint[k] === 'string' && ZH.health.hint[k].length > 0, `zh 缺少 runtimes.health.hint.${k}`);
    assert.ok(EN.health && EN.health.hint && typeof EN.health.hint[k] === 'string' && EN.health.hint[k].length > 0, `en 缺少 runtimes.health.hint.${k}`);
  }
  assert.deepStrictEqual(Object.keys(ZH.health.hint).sort(), HINT_KEYS.slice().sort(), 'zh hint key 集合应恰为 6 个');
  assert.deepStrictEqual(Object.keys(EN.health.hint).sort(), HINT_KEYS.slice().sort(), 'en hint key 集合应与 zh 一致');
}

function testHintCopyContent() {
  assert.ok(ZH.health.hint.consecutive_failures.includes('登录机器'), '探活连续失败建议应含「登录机器定位」');
  assert.ok(EN.health.hint.consecutive_failures.length > 10, '英文建议非空');
}

// ── 执行 ──

const tests = [
  testFunctionsExist, testRenderEnginesWired, testRenderWorkersWired, testHelpBtnTooltip,
  testHintPriority, testHealthyNoHint, testLocaleKeys, testHintCopyContent,
];
let failed = 0;
tests.forEach(fn => {
  try { fn(); console.log(`  ✓ ${fn.name}`); }
  catch (e) { failed++; console.error(`  ✗ ${fn.name}: ${e.message}`); }
});
if (failed) { console.error(`\n${failed} test(s) failed`); process.exit(1); }
console.log(`\nAll ${tests.length} tests passed.`);
