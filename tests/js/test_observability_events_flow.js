/*
 * 事件视图 S2 — 时间轴流 + 服务端关键字检索 + 游标「加载更多」+ 新事件提示
 *
 * 迁移说明：原测试沙箱执行 legacy observability.js；Vue 迁移后为 evCursor/evNew/事件流函数。
 *   1. 游标分页：请求带 before=<next_cursor>，响应写回 evCursor；重载清空游标
 *   2. 「加载更多」按游标追加（不重排已有事件）
 *   3. 30s 软刷新命中新事件时进 evNew，页头出现「N 条新事件」入口，点击 applyNewEvents 落地
 *   4. 关键字检索与服务端过滤参数一并下发（q/source/state）
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

check('游标：请求带 before，响应写回 next_cursor', function () {
  assert.ok(/const evCursor = ref\(null\)/.test(VIEW), '应有 evCursor');
  assert.ok(/'&before=' \+ encodeURIComponent\(evCursor\.value\)/.test(VIEW), '请求应带 before 游标');
  assert.ok(/evCursor\.value = d\.next_cursor \|\| null/.test(VIEW), '应写回 next_cursor');
  assert.ok(/evCursor\.value = null/.test(VIEW), '重载应清空游标');
});

check('加载更多按游标追加', function () {
  assert.ok(/append/.test(VIEW), '应有 append 语义');
  const i = VIEW.indexOf("const curQ = append && evCursor.value");
  assert.ok(i > 0, 'append 时才带游标');
});

check('新事件提示与落地', function () {
  assert.ok(/const evNew = ref\(\[\]\)/.test(VIEW), '应有 evNew');
  assert.ok(/class="obv-evnew"/.test(VIEW), '应有新事件提示条');
  assert.ok(/applyNewEvents\(\)/.test(VIEW), '点击应落地新事件');
  assert.ok(/ev_new_events/.test(VIEW), '提示文案走 i18n');
});

check('检索参数下发', function () {
  assert.ok(/evQ|evSource|evState/.test(VIEW), '应有检索/筛选状态');
  assert.ok(/'&q='|'&source='|'&state='/.test(VIEW), '应把检索条件拼进请求');
});

console.log('\n事件流 S2 全部通过 (' + passed + '/4)');