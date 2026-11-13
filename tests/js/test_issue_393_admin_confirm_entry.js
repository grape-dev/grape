/*
 * Issue #393 — 「管理员确认完成」内联入口（源码级断言）
 *
 * 迁移说明（2026-11-11）：原测试沙箱执行 legacy issues.js 的 _renderTaskActionsInline
 * 与 _isRepoAdmin393；Vue 迁移后改为 isRepoAdmin computed + 模板按钮。断言保留原语义：
 *   1. isRepoAdmin 读 repo_member_cache，repo_key 前缀(github:/gitcode:)与 bare/full_name 均可匹配
 *   2. 非 completed/cancelled 才渲染管理员确认按钮（两处入口：看板详情 + 详情页）
 *   3. 非 admin → disabled + admin_only_hint 提示；admin → 主色按钮
 *   4. 点击走 POST /issues/{id}/admin-confirm-done
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(
  path.resolve(__dirname, '../../src/api/frontend/app/src/views/IssuesView.vue'),
  'utf8',
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('isRepoAdmin 读 repo_member_cache 并归一化 repo_key', function () {
  const i = VIEW.indexOf('const isRepoAdmin = computed(');
  assert.ok(i > 0, '应存在 isRepoAdmin computed');
  const body = VIEW.slice(i, i + 900);
  assert.ok(body.indexOf("repo_member_cache") >= 0, '应读 localStorage 的 repo_member_cache');
  assert.ok(/replace\(\/\^\(github\|gitcode\):\//.test(body), '应去掉 github:/gitcode: 前缀');
  assert.ok(/r\.repo_key === rk/.test(body), '应按 repo_key 匹配');
  assert.ok(/r\.full_name === bare/.test(body), '应按 full_name 匹配');
});

check('管理员按钮只在非 completed/cancelled 渲染（两处入口）', function () {
  const n = (VIEW.match(/:disabled="!isRepoAdmin"/g) || []).length;
  assert.strictEqual(n, 2, '看板详情与详情页两处都应渲染管理员按钮，实际 ' + n);
  const gate = (VIEW.match(/status !== 'completed' && [^"]*status !== 'cancelled'/g) || []).length;
  assert.ok(gate >= 2, '两处入口都应以 completed/cancelled 为排除条件，实际 ' + gate);
});

check('非 admin → disabled + admin_only_hint', function () {
  assert.ok(/:disabled="!isRepoAdmin"/.test(VIEW), '非管理员应 disabled');
  assert.ok(/:title="isRepoAdmin \? '' : t\('admin_only_hint'\)"/.test(VIEW), '应给 admin_only_hint 提示');
});

check('点击调用 adminConfirmTaskDone → POST /admin-confirm-done', function () {
  assert.ok(/@click="adminConfirmTaskDone\(/.test(VIEW), '按钮应绑定 adminConfirmTaskDone');
  assert.ok(
    /\.post\('\/issues\/' \+ tk\.id \+ '\/admin-confirm-done'\)/.test(VIEW),
    '应 POST /issues/{id}/admin-confirm-done',
  );
});

console.log('\n#393 全部通过 (' + passed + '/4)');