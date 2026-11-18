/*
 * 筛选条分组竖线守护（2026-11-18 用户实测：「点『个人』后 全部 仓库 个人 | 个人 两个个人之间没有竖线了」）
 *
 * 回归背景：范围组（全部/仓库/个人）与归属组（本机/个人/全局/仓库）之间那根竖线原先挂在
 * `availTypes.length` 上——类型档由数据派生，某些范围下（如只看个人时只剩 e2e-mock-engine
 * 这种没有类型关键字的引擎）类型档为空，竖直就跟着消失，范围组与归属组的「个人」贴在一起，
 * 读起来像重复项而不是两组不同维度。
 *
 * 口径：竖线跟随「它右边那一组」的渲染条件——归属组竖线挂 availScopes/engModalAvailScopes，
 * 范围→类型竖线挂 availTypes（类型组为空时不需要）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const RT = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/RuntimesView.vue'), 'utf8');
const DB = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

// 取某段源码里「最后一个竖线 span 的 v-if 条件」
function lastSepCond(src, head) {
  const i = src.indexOf(head);
  assert.ok(i > 0, '找不到锚点: ' + head);
  const before = src.slice(Math.max(0, i - 600), i);
  const re = /<span v-if="([^"]+)" class="(?:rtv-chip-sep|dbv-engsep)"><\/span>/g;
  const hits = [...before.matchAll(re)];
  assert.ok(hits.length, '锚点前没有竖线 span: ' + head);
  return hits[hits.length - 1][1];
}

check('运行时页：范围组→类型组竖线挂 availTypes', function () {
  const c = lastSepCond(RT, 'v-for="ty in availTypes"');
  assert.strictEqual(c, 'availTypes.length');
});

check('运行时页：类型/范围组→归属组竖线挂 availScopes（不得再挂 availTypes）', function () {
  const c = lastSepCond(RT, 'v-for="s in availScopes"');
  assert.strictEqual(
    c,
    'availScopes.length',
    '归属组竖线必须跟随 availScopes；挂 availTypes 会在类型档为空时整根消失（两个「个人」贴一起）',
  );
});

check('场景编辑器实例面板：归属组竖线挂 engModalAvailScopes', function () {
  const c = lastSepCond(DB, 'v-for="s in engModalAvailScopes"');
  assert.strictEqual(c, 'engModalAvailScopes.length');
});

check('两根竖线不得共用 availTypes 条件（反断言）', function () {
  const rtSepConds = [...RT.matchAll(/<span v-if="([^"]+)" class="rtv-chip-sep"><\/span>/g)].map((m) => m[1]);
  assert.ok(
    rtSepConds.some((c) => c === 'availScopes.length'),
    '运行时页至少有一根竖线由归属组条件驱动，实际: ' + JSON.stringify(rtSepConds),
  );
});

console.log('\n筛选条分组竖线守护通过 (' + passed + '/4)');
