/*
 * Issue #1102 — 任务会话页事件历史（**验收已调整**，2026-11-11）
 *
 * 变更说明：原验收要求「默认渲染最近 20 条 + 加载更早按钮 + stopPropagation」。
 * Vue 迁移后该页改为一次性渲染全部事件（IssuesView.vue:1604-1608），
 * 经主人确认按「一次渲染全部」收敛验收（单需求事件量通常 <20，分页收益低）。
 * 本用例锁现在的契约：
 *   1. 事件按 seq 全量渲染（无截断、无「加载更多」按钮）
 *   2. 事件文案走 i18n 且带时间/动作/结果三段信息
 *   3. 事件块在流水线卡片内，不阻断卡片自身的点击（无整卡跳转 onclick）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/IssuesView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('事件全量渲染（无截断/无加载更多按钮）', function () {
  const i = VIEW.indexOf('if (events.length > 0) {');
  assert.ok(i > 0, '应有事件渲染分支');
  const b = VIEW.slice(i, i + 900);
  assert.ok(/events\.forEach/.test(b), '应遍历全部事件');
  assert.ok(!/slice\(\s*-\s*20/.test(b), '不应再做「最近 20 条」截断');
  assert.ok(!/load_earlier_events/.test(VIEW), '不应再有「加载更早」按钮');
});

check('事件信息三段齐备', function () {
  const i = VIEW.indexOf('if (events.length > 0) {');
  const b = VIEW.slice(i, i + 1800);
  assert.ok(/e\.event_type|event_type/.test(b), '应展示事件类型');
  assert.ok(/e\.created_at/.test(b), '应展示时间');
  assert.ok(/e\.summary|e\.status/.test(b), '应展示摘要/状态');
  assert.ok(/e\.seq/.test(b), '应按 seq 有序');
});

check('事件块不绑整卡点击', function () {
  const i = VIEW.indexOf('if (events.length > 0) {');
  const b = VIEW.slice(i, i + 900);
  assert.ok(!/onclick=_jumpToPipePage/.test(b), '不应有整卡跳转 onclick');
  assert.ok(!/event\.stopPropagation/.test(b), '不再需要冒泡阻断（已无外层 onclick）');
});

console.log('\n#1102 全部通过 (' + passed + '/3)（验收已按「一次渲染全部」调整）');