/*
 * Issue #316 — 场景市场详情移除「仓库」Tab（源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy scene-market.js；Vue 迁移后 SceneMarket.vue 已无该 Tab。
 * 本用例锁「不再出现」这一验收结论，防止回归时偷偷加回：
 *   1. 详情区不渲染 repo/仓库 Tab
 *   2. 也不存在按 repo 切换的 tab 状态
 *   3. 场景信息仍以其它区块展示（不是整个人删掉）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/SceneMarket.vue'), 'utf8');
const TPL = VIEW.slice(0, VIEW.indexOf('<script'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('不存在仓库 Tab 的渲染', function () {
  assert.ok(!/repoTab|repo_tab/.test(VIEW), '不应有 repoTab 状态');
  assert.ok(!/tab.*['"]repo['"]/.test(TPL), '模板不应有 repo Tab 项');
  assert.ok(!/>\s*仓库\s*</.test(TPL), '不应出现「仓库」Tab 文案');
});

check('不存在按仓库切换的 tab 逻辑', function () {
  assert.ok(!/switchTab\(\s*['"]repo/i.test(VIEW), '不应有切到 repo 的 tab 逻辑');
});

check('场景信息仍正常展示（删的是 Tab 不是页面）', function () {
  assert.ok(/detail\.value|detailId|sm__detail/i.test(VIEW), '详情仍存在');
  assert.ok(/apply_scene|onApply/.test(VIEW), '应用入口仍存在');
});

console.log('\n#316 全部通过 (' + passed + '/3)');