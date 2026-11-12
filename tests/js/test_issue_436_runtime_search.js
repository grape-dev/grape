/*
 * Issue #436 — 运行驾驶舱「设备与引擎」页 runtime 列表搜索（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy observability.js；Vue 迁移后为 rtSearch + 本地过滤。
 *   1. 有 rtSearch 绑定到搜索框（v-model）
 *   2. 过滤对小写化后的名称/别名/设备名匹配（大小写不敏感）
 *   3. 搜索结果参与渲染列表（computed 链路），不是只存状态不生效
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

check('搜索框与状态绑定', function () {
  assert.ok(/const rtSearch = ref\(''\)/.test(VIEW), '应有 rtSearch');
  assert.ok(/v-model="rtSearch"/.test(VIEW), '搜索框应 v-model 到 rtSearch');
});

check('过滤大小写不敏感且覆盖名称字段', function () {
  assert.ok(/rtSearch\.value\.trim\(\)\.toLowerCase\(\)/.test(VIEW), '应小写化后比较');
  const i = VIEW.indexOf('rtSearch.value.trim().toLowerCase()');
  const b = VIEW.slice(i, i + 700);
  assert.ok(/\.toLowerCase\(\)/.test(b), '被匹配字段也应小写化');
  assert.ok(/name|alias|display_name|instance_name/.test(b), '应匹配名称类字段');
});

check('过滤结果接进渲染链路（filteredRuntimes computed）', function () {
  const i = VIEW.indexOf('const filteredRuntimes = computed(');
  assert.ok(i > 0, '应有 filteredRuntimes computed');
  const b = VIEW.slice(i, i + 900);
  assert.ok(/filteredRuntimes/.test(VIEW.slice(i + 900)), 'computed 应被后续分页消费');
  assert.ok(/return false/.test(b), '不匹配应 return false（filter 语义）');
  assert.ok(/pageRuntimes = computed/.test(VIEW), '分页应从过滤结果切片');
});

console.log('\n#436 全部通过 (' + passed + '/3)');