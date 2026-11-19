/*
 * Issue #1310 — 引擎展示名（AgentView：卡片 + 事件表引擎列）
 *
 * 2026-11-19 产品裁定（A）：Agent 页头卡**不再**挂引擎 chip——它展示的是
 * 「该 agent 会话最近绑定的引擎」（devops_agent_session → agent_runtimes），
 * 多仓库共用同一 agent 时会显示别处会话残留的引擎，容易误读。
 * 引擎绑定/健康在「引擎」页与「事件」页各有完整入口。
 *
 * 因此原「engineText 优先级 / 无引擎占位 / 圆点映射」三条断言随 chip 一并作废，
 * 本文件改为守两条仍然成立的口径：
 *   1. 头卡不渲染引擎 chip（防止有人顺手加回裸展示）；
 *   2. 事件表「引擎」列仍在，空引擎显示 —（不是 undefined，也不是空白）。
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

check('头卡不再渲染引擎 chip', function () {
  assert.ok(VIEW.indexOf('ag__engine') < 0, 'Agent 头卡不应再有 ag__engine 区块');
  assert.ok(VIEW.indexOf('engineText') < 0, 'engineText computed 应随 chip 一起移除');
  assert.ok(VIEW.indexOf('engineDot') < 0, 'engineDot computed 应随 chip 一起移除');
});

check('事件表引擎列口径保留', function () {
  assert.ok(/agents\.event\.table_engine/.test(VIEW), '事件表应有引擎列标题');
  assert.ok(/ev\.backend \|\| '—'/.test(VIEW), '空引擎显示 —（不是 undefined）');
});

console.log('\n#1310 全部通过 (' + passed + '/2)');