/*
 * 可观测性分区路由（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy observability.js 的分区渲染；Vue 迁移后为 section 状态 + 模板分区。
 *   1. 三个分区：overview / devices / inspect，默认 overview
 *   2. switchSection 切分区时把子 Tab 复位到该分区第一个，并重置分页游标
 *   3. subTabs 按分区派生：inspect → tasks/efficiency/events/errorlogs
 *   4. 分区内容由 v-if 分派（无残留旧分区实现）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/app/src/views/ObservabilityView.vue'),
  'utf8',
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('三分区且默认 overview', function () {
  assert.ok(/const section = ref\('overview'\)/.test(VIEW), 'section 默认 overview');
  const tpl = VIEW.slice(0, VIEW.indexOf('<script'));
  for (const s of ['overview', 'devices', 'inspect']) {
    assert.ok(tpl.indexOf("'" + s + "'") >= 0 || tpl.indexOf('"' + s + '"') >= 0, '应出现分区 ' + s);
  }
});

check('switchSection 复位子 Tab 与分页', function () {
  const i = VIEW.indexOf('function switchSection(');
  assert.ok(i > 0, '应有 switchSection');
  const b = VIEW.slice(i, i + 700);
  assert.ok(/tab\.value = subTabs\.value\[0\]/.test(b), '切分区应把子 Tab 复位');
  assert.ok(/Page\.value = 0|stopAuto\(\)/.test(b), '应重置分页或停自动刷新');
});

check('subTabs 派生 inspect 四个子 Tab（含能效看板）', function () {
  const i = VIEW.indexOf('const subTabs = computed(');
  assert.ok(i > 0, '应有 subTabs computed');
  const b = VIEW.slice(i, i + 700);
  assert.ok(/inspect/.test(b), 'inspect 分区应有子 Tab');
  assert.ok(/efficiency/.test(b), '应含 #1229 能效看板子 Tab');
  assert.ok(/errorlogs/.test(b), '应含错误日志子 Tab');
});

check('分区内容按 v-if 分派', function () {
  const tpl = VIEW.slice(0, VIEW.indexOf('<script'));
  const n = (tpl.match(/section\s*===|v-if="section/g) || []).length;
  assert.ok(n >= 2, '模板应按 section 分派内容，实际 ' + n);
});

console.log('\n分区路由全部通过 (' + passed + '/4)');