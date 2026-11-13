/*
 * Issue #1310 — 引擎展示名（AgentView：卡片 + 事件表引擎列）
 *
 * 迁移说明：原测试沙箱执行 legacy agents.js/autopilot.js；Vue 迁移后为 engineText computed。
 *   1. 展示文本优先级：backend → backends 逗号拼接（不显示 undefined / null）
 *   2. 无引擎 → agents.engine.no_engine 占位，不空白
 *   3. 引擎圆点按引擎名映射（opencode/hermes/codearts/codex），未知引擎给空串不误导
 *   4. 事件表「引擎」列走同一口径
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/AgentView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('展示文本优先级与空值保护', function () {
  const i = VIEW.indexOf('const engineText = computed(');
  assert.ok(i > 0, '应有 engineText');
  const b = VIEW.slice(i, i + 400);
  assert.ok(/props\.data\.backend/.test(b), '优先 backend');
  assert.ok(/backends\.join\('\, '\)/.test(b), '其次 backends 拼接');
  assert.ok(/\|\| ''/.test(b), '空值应回落空串（不显示 undefined）');
});

check('无引擎有占位文案', function () {
  assert.ok(/agents\.engine\.no_engine/.test(VIEW), '无引擎应显示占位');
});

check('引擎圆点按名映射、未知不硬编码', function () {
  const i = VIEW.indexOf('const engineDot = computed(');
  const b = VIEW.slice(i, i + 700);
  for (const k of ['hermes', 'opencode', 'codearts', 'codex']) {
    assert.ok(b.indexOf(k) >= 0, '应识别 ' + k);
  }
  assert.ok(/return ''/.test(b), '未知引擎应返回空串');
});

check('事件表引擎列同口径', function () {
  assert.ok(/agents\.event\.table_engine/.test(VIEW), '事件表应有引擎列标题');
  assert.ok(/ev\.backend \|\| '—'/.test(VIEW), '空引擎显示 —（不是 undefined）');
});

console.log('\n#1310 全部通过 (' + passed + '/4)');