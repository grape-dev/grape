/*
 * Issue #114 / PR #115 — 流水线页面搜索（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy pipeline.js；Vue 迁移后为 query + applySearch + filtered。
 *   1. 搜索框 v-model 绑定 query，Enter / change 触发 applySearch
 *   2. 过滤作用在流水线集合上（不是只存状态）
 *   3. 关键字大小写不敏感（含名称/仓库字段）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/Pipeline.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('搜索框绑定与触发', function () {
  assert.ok(/v-model="query"/.test(VIEW), '搜索框应 v-model 到 query');
  assert.ok(/@keyup\.enter="applySearch"/.test(VIEW), 'Enter 应触发搜索');
  assert.ok(/@change="applySearch"/.test(VIEW), 'change 应触发搜索');
  assert.ok(/function applySearch\(/.test(VIEW), '应有 applySearch');
});

check('过滤接线到集合（haystack 拼多字段后 includes）', function () {
  const i = VIEW.indexOf('allPipes.value.filter(');
  assert.ok(i > 0, '应对流水线集合 filter');
  const b = VIEW.slice(i, i + 900);
  assert.ok(/haystack/.test(b), '应把多字段拼成 haystack');
  assert.ok(/return haystack\.includes\(q\)/.test(b), '应做包含匹配');
  assert.ok(/appliedQuery/.test(VIEW), '应用后的查询值应有独立状态');
});

check('大小写不敏感', function () {
  assert.ok(/toLowerCase\(\)/.test(VIEW), '应小写化比较');
  const i = VIEW.indexOf('query');
  assert.ok(i > 0, '应有 query 状态');
});

console.log('\n#114 全部通过 (' + passed + '/3)');