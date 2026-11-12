/*
 * Issue #341 — 需求中心深链 #tasks/<issue号> 直达详情（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy issues.js；Vue 迁移后在 loadTasks 的 deeplink 分支。
 *   1. 消费 window._deeplinkIssueNumber，命中需求后打开详情
 *   2. 命中后清空标记（一次性，刷新不重复弹出）
 *   3. 未命中 → deeplink_not_found 提示并回到列表
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

check('消费 _deeplinkIssueNumber', function () {
  assert.ok(/_deeplinkIssueNumber/.test(VIEW), '应读取深链需求号');
  const i = VIEW.indexOf('_deeplinkIssueNumber');
  assert.ok(i > 0, '应有具体分支');
});

check('命中后打开详情并清标记', function () {
  const ii = VIEW.indexOf('const deeplinkIssueNumber');
  const b = VIEW.slice(ii, ii + 1600);
  assert.ok(/openDetail|openIssue|selectTask|detail\.value =/.test(b), '命中应打开详情');
  assert.ok(/_deeplinkIssueNumber = null/.test(b), '命中后应清标记（一次性）');
});

check('未命中给提示（不静默）', function () {
  const hits = [...VIEW.matchAll(/deeplink_not_found/g)].map((m) => m.index);
  assert.ok(hits.length >= 1, '应使用 deeplink_not_found 文案');
  const near = hits.some((i) => /showToast/.test(VIEW.slice(Math.max(0, i - 140), i + 140)));
  assert.ok(near, '未命中应 showToast 提示');
});

console.log('\n#341 全部通过 (' + passed + '/3)');