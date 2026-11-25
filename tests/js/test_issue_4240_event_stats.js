/*
 * Issue #4240 Agent 事件统计卡与总量口径（前端回归）
 *
 * 运行：node tests/js/test_issue_4240_event_stats.js
 *
 * 验证点：
 *   T1 状态集合单一来源：模板 v-for 渲染五张状态卡，不再重复写五段硬编码
 *   T2 totalEvents = 五状态之和；未知状态不计入状态卡
 *   T3 rawTotal：raw=0 保留 0；raw 缺省/null 回退 totalEvents
 *   T4 窄屏：.ag__stats 允许换行
 *   T5 zh/en 词条齐平（pending/completed/running/failed/cancelled/raw_total）
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = path.join(ROOT, 'src/api/frontend/app/src/views/AgentView.vue');
const src = fs.readFileSync(VIEW, 'utf8');
const tplEnd = src.indexOf('<script setup>');
const tpl = src.slice(0, tplEnd);
const script = src.slice(tplEnd);
const zh = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/zh/agents.json'), 'utf8')
);
const en = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/en/agents.json'), 'utf8')
);

const STATUSES = ['pending', 'completed', 'running', 'failed', 'cancelled'];

// 截取统计口径 computed 片段，用 computed 桩件真实执行取值逻辑
const statsStart = script.indexOf('const es = computed(function () {');
const statsEnd = script.indexOf('const highlightId = computed');
const statsSrc = statsStart >= 0 && statsEnd > statsStart ? script.slice(statsStart, statsEnd) : '';

function makeStats(metrics) {
  const computed = (fn) => ({
    get value() {
      return fn();
    },
  });
  const m = { value: metrics };
  const factory = new Function(
    'computed',
    'm',
    statsSrc + '\n; return { totalEvents: totalEvents, rawTotal: rawTotal, statusCards: statusCards };'
  );
  return factory(computed, m);
}

let passed = 0;
let failed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log('  \u2713 ' + name);
  } catch (e) {
    failed++;
    console.log('  \u2717 ' + name + '\n    ' + e.message);
  }
}

console.log('Issue #4240：Agent 事件统计卡前端回归');

test('T1 模板 v-for 状态卡 + 仅一张总量卡', () => {
  assert.ok(/v-for="card in statusCards"/.test(tpl), '状态卡未走 v-for');
  assert.ok(/:key="card.key"/.test(tpl), '状态卡缺 :key');
  assert.ok(/t\('agents\.event\.' \+ card\.key\)/.test(tpl), '状态卡未用动态词条');
  // 模板只应有「总量卡 + v-for 状态卡」两个 .ag__st，不得再逐个硬编码状态卡
  const cardCount = (tpl.match(/class="ag__st"/g) || []).length;
  assert.strictEqual(cardCount, 2, '状态卡数量异常（应为总量卡 + v-for 一份）: ' + cardCount);
  STATUSES.forEach((s) => {
    assert.ok(
      tpl.indexOf("t('agents.event." + s + "')") < 0,
      s + ' 仍硬编码在模板，未走 statusCards'
    );
  });
  assert.ok(/agents\.event\.raw_total/.test(tpl), '总量卡缺原始总数');
});

test('T1b script 不再逐个状态写 computed', () => {
  STATUSES.forEach((s) => {
    const re = new RegExp('const ' + s + ' = computed');
    assert.ok(!re.test(script), s + ' 仍是独立 computed，状态集合非单一来源');
  });
});

test('T2 总量 = 五状态之和，未知状态不计入', () => {
  const stats = makeStats({
    event_stats: { pending: 1, completed: 2, running: 3, failed: 4, cancelled: 5, weird: 100 },
    total_events_raw: 115,
  });
  assert.strictEqual(stats.totalEvents.value, 15);
  assert.strictEqual(stats.statusCards.value.length, 5);
  assert.deepStrictEqual(
    stats.statusCards.value.map((c) => c.key),
    STATUSES
  );
  assert.deepStrictEqual(
    stats.statusCards.value.map((c) => c.count),
    [1, 2, 3, 4, 5]
  );
});

test('T3 raw=0 保留 0，不被回退覆盖', () => {
  const stats = makeStats({ event_stats: { completed: 5 }, total_events_raw: 0 });
  assert.strictEqual(stats.totalEvents.value, 5);
  assert.strictEqual(stats.rawTotal.value, 0);
});

test('T3b raw 缺省/null 回退状态之和', () => {
  const undef = makeStats({ event_stats: { pending: 1, completed: 5 } });
  assert.strictEqual(undef.rawTotal.value, 6);
  const nul = makeStats({ event_stats: { pending: 1, completed: 5 }, total_events_raw: null });
  assert.strictEqual(nul.rawTotal.value, 6);
});

test('T3c metrics 缺 event_stats 时全为 0', () => {
  const stats = makeStats({});
  assert.strictEqual(stats.totalEvents.value, 0);
  assert.strictEqual(stats.rawTotal.value, 0);
});

test('T4 .ag__stats 窄屏可换行', () => {
  const m = src.match(/\.ag__stats \{([\s\S]*?)\n\}/);
  assert.ok(m, '未找到 .ag__stats 样式');
  assert.ok(/flex-wrap:\s*wrap/.test(m[1]), '.ag__stats 缺 flex-wrap: wrap');
});

test('T5 zh/en 词条齐平', () => {
  const keys = STATUSES.concat(['total', 'raw_total']);
  keys.forEach((k) => {
    assert.ok(zh.event && zh.event[k] !== undefined, 'zh 缺 agents.event.' + k);
    assert.ok(en.event && en.event[k] !== undefined, 'en 缺 agents.event.' + k);
  });
  const zk = keys.filter((k) => zh.event[k] !== undefined).sort();
  const ek = keys.filter((k) => en.event[k] !== undefined).sort();
  assert.deepStrictEqual(zk, ek, 'zh/en 状态词条不一致');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);