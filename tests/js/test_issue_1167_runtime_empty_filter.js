/*
 * Issue #1167 — 筛选无结果空态与 Worker 页对齐
 *
 * 2026-11-11 修复：迁移后只剩一句 empty.no_engines，本次按旧规格补回分叉：
 *   1. hasActiveFilters 覆盖全部筛选维度（scope/type/scene/tag/search/range），
 *      与 clearFilters 的重置范围一致
 *   2. 有筛选 + 空结果 → 列表区内联「暂无引擎」，筛选栏与 ✕ 清除保留
 *   3. 无筛选 + 真空 → 「暂无引擎」标题 + 副标题引导（no_engines_title/sub）
 *   4. 空态不再复用组织级引导文案混淆两种场景
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/RuntimesView.vue'), 'utf8');
const zh = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/zh/runtimes.json'), 'utf8'),
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('hasActiveFilters 覆盖六个筛选维度', function () {
  const i = VIEW.indexOf('const hasActiveFilters = computed(');
  assert.ok(i > 0, '应存在 hasActiveFilters');
  const b = VIEW.slice(i, i + 520);
  for (const k of ['filterScope.value', 'filterType.value', 'filterScene.value', 'filterTag.value', 'search.value', "rtRange.value !== 'all'"]) {
    assert.ok(b.indexOf(k) >= 0, '应覆盖 ' + k);
  }
});

check('清除筛选的重置范围与判定一致', function () {
  const i = VIEW.indexOf('function clearFilters(');
  const b = VIEW.slice(i, i + 420);
  for (const k of ['rtRange.value', 'filterType.value', 'filterScope.value', 'filterScene.value', 'filterTag.value', 'search.value']) {
    assert.ok(b.indexOf(k) >= 0, 'clearFilters 应重置 ' + k);
  }
});

check('空态按「有筛选/无筛选」分叉', function () {
  assert.ok(/v-if="hasActiveFilters"[\s\S]{0,120}t\('empty\.no_engines'\)/.test(VIEW), '有筛选应内联「暂无引擎」');
  assert.ok(/rtv-empty-guide-t[\s\S]{0,80}empty\.no_engines_title/.test(VIEW), '无筛选应显示标题引导');
  assert.ok(/rtv-empty-guide-s[\s\S]{0,80}empty\.no_engines_sub/.test(VIEW), '无筛选应显示副标题引导');
});

check('引导文案与筛选栏在无筛选场景下同时存在', function () {
  const i = VIEW.indexOf('rtv-empty-guide');
  assert.ok(i > 0, '应有引导块');
  assert.ok(VIEW.indexOf('class="rtv-filters"') < i, '筛选栏在引导块之前（不因空态隐藏）');
  assert.ok(zh.empty && zh.empty.no_engines_title && zh.empty.no_engines_sub, 'zh 引导文案应存在');
});

console.log('\n#1167 全部通过 (' + passed + '/4)');