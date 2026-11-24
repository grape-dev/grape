/*
 * Issue #3396 — 引擎「角色标签」编辑器联动行为回归（node 看护）
 *
 * 为什么要有这个文件：2026-11-20 之前这里只有 Python 侧对源码做正则断言，
 * 谁把联动改回「all 与角色互斥/不联动」都能过。本用例直接执行
 * views/runtime-tag-link.js 的纯函数，按用户裁定的四条规则逐条验行为：
 *   1) 勾全能 → 后面角色全部勾上
 *   2) 后面全部勾上 → 自动勾全能
 *   3) 任一取消 → 全能取消
 *   4) 保存时全能成立只下发 ['all']
 * 并覆盖「场景过滤后只剩 5 个角色」的情形（例如博客开发场景没有 coding）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '../..');
const MODULE = path.join(REPO, 'src/api/frontend/app/src/views/runtime-tag-link.js');
const VIEW = path.join(REPO, 'src/api/frontend/app/src/views/RuntimesView.vue');
const ZH = path.join(REPO, 'src/api/frontend/shell/locales/zh/runtimes.json');

// 纯 ESM 模块 → 去掉 export 关键字后在宿主 realm 求值（无任何依赖）
// 注意不能用 vm：跨 realm 的数组原型不同，deepStrictEqual 会误报
function loadLink() {
  const src = fs.readFileSync(MODULE, 'utf8').replace(/^export /gm, '');
  return new Function(
    src +
      '\nreturn { ALL_ROLES, normalizeRoles, initialRoles, visibleRoles, isAllChecked, isChecked, toggle, toPayload, selectedVisible, droppedByScene };'
  )();
}

const L = loadLink();
const SIX = ['vod', 'issue', 'coding', 'qa', 'sre', 'chat'];
const FIVE_NO_CODING = ['vod', 'issue', 'qa', 'sre', 'chat']; // 场景过滤掉 coding
const OPTS_SIX = ['all'].concat(SIX);
const OPTS_FIVE = ['all'].concat(FIVE_NO_CODING);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

// ── 规则 1：勾全能 → 角色全勾 ───────────────────────────────────────────
check('规则1 六档场景：勾全能后六个角色全勾', function () {
  const vis = L.visibleRoles(OPTS_SIX);
  assert.deepStrictEqual(vis, SIX);
  const tags = L.toggle([], 'all', true, vis);
  assert.deepStrictEqual(tags, SIX);
  SIX.forEach(function (r) {
    assert.strictEqual(L.isChecked(tags, r, vis), true, r + ' 应当被勾上');
  });
  assert.strictEqual(L.isChecked(tags, 'all', vis), true);
});

check('规则1 五档场景（无 coding）：勾全能只勾当前可见五个', function () {
  const vis = L.visibleRoles(OPTS_FIVE);
  assert.deepStrictEqual(vis, FIVE_NO_CODING);
  const tags = L.toggle([], 'all', true, vis);
  assert.deepStrictEqual(tags, FIVE_NO_CODING, '不应凭空勾上场景没提供的 coding');
  assert.strictEqual(L.isChecked(tags, 'all', vis), true, '五档场景下全能必须能被点亮');
});

// ── 规则 2：全勾 → 自动全能 ─────────────────────────────────────────────
check('规则2 六档场景：逐个勾满后全能自动成立', function () {
  const vis = L.visibleRoles(OPTS_SIX);
  let tags = [];
  SIX.forEach(function (r) {
    tags = L.toggle(tags, r, true, vis);
  });
  assert.deepStrictEqual(tags, SIX);
  assert.strictEqual(L.isChecked(tags, 'all', vis), true);
});

check('规则2 五档场景：勾满可见五档后全能成立（此前固定六档会永远点不亮）', function () {
  const vis = L.visibleRoles(OPTS_FIVE);
  let tags = [];
  FIVE_NO_CODING.forEach(function (r) {
    tags = L.toggle(tags, r, true, vis);
  });
  assert.strictEqual(L.isChecked(tags, 'all', vis), true);
});

// ── 规则 3：取消任一 → 全能取消 ─────────────────────────────────────────
check('规则3 六档场景：取消 QA 后全能取消、其余保持', function () {
  const vis = L.visibleRoles(OPTS_SIX);
  const tags = L.toggle(SIX, 'qa', false, vis);
  assert.deepStrictEqual(tags, ['vod', 'issue', 'coding', 'sre', 'chat']);
  assert.strictEqual(L.isChecked(tags, 'all', vis), false);
  assert.strictEqual(L.isChecked(tags, 'qa', vis), false);
  assert.strictEqual(L.isChecked(tags, 'sre', vis), true);
});

check('规则3 五档场景：取消最后一个可见角色后全能取消', function () {
  const vis = L.visibleRoles(OPTS_FIVE);
  const tags = L.toggle(FIVE_NO_CODING, 'chat', false, vis);
  assert.strictEqual(L.isChecked(tags, 'all', vis), false);
});

check('规则3 取消全能 → 全部角色清空', function () {
  const vis = L.visibleRoles(OPTS_SIX);
  assert.deepStrictEqual(L.toggle(SIX, 'all', false, vis), []);
});

// ── 规则 4：保存只下发 ['all'] ──────────────────────────────────────────
check('规则4 六档全勾 → 载荷只含全能', function () {
  const vis = L.visibleRoles(OPTS_SIX);
  assert.deepStrictEqual(L.toPayload(SIX, vis), ['all']);
});

check('规则4 五档全勾 → 载荷只含全能（全能=不受角色限制的既有语义）', function () {
  const vis = L.visibleRoles(OPTS_FIVE);
  assert.deepStrictEqual(L.toPayload(FIVE_NO_CODING, vis), ['all']);
});

check('规则4 部分勾选 → 载荷为已勾角色，且不含 all', function () {
  const vis = L.visibleRoles(OPTS_SIX);
  const tags = L.toggle(SIX, 'qa', false, vis);
  const payload = L.toPayload(tags, vis);
  assert.deepStrictEqual(payload, ['vod', 'issue', 'coding', 'sre', 'chat']);
  assert.strictEqual(payload.indexOf('all'), -1);
});

check('规则4 空标签 → 载荷为空数组（fail-closed）', function () {
  const vis = L.visibleRoles(OPTS_SIX);
  assert.deepStrictEqual(L.toPayload([], vis), []);
});

// ── 边界：可见集合为空、后端回 all、派生行过滤 ──────────────────────────
check('边界 可见角色为空时全能不成立、勾全能不产生角色', function () {
  const vis = L.visibleRoles(['all']);
  assert.deepStrictEqual(vis, []);
  assert.strictEqual(L.isAllChecked([], vis), false);
  assert.deepStrictEqual(L.toggle([], 'all', true, vis), []);
  assert.deepStrictEqual(L.toPayload([], vis), []);
});

check('边界 后端回 all → 模型展开为全部角色', function () {
  assert.deepStrictEqual(L.initialRoles(['all']), SIX);
});

check('边界 派生行（engine:/scene:/scope:）不进入模型与载荷', function () {
  const tags = ['chat', 'engine:hermes', 'scene:default', 'scope:user'];
  const model = L.initialRoles(tags);
  assert.deepStrictEqual(model, ['chat']);
  const vis = L.visibleRoles(OPTS_SIX);
  assert.deepStrictEqual(L.toPayload(model, vis), ['chat']);
});

// ── 防回退：Vue 必须委托模块，不能把规则再抄回去 ────────────────────────
check('看护 RuntimesView.vue 通过 runtime-tag-link.js 委托联动规则', function () {
  const view = fs.readFileSync(VIEW, 'utf8');
  assert.ok(view.indexOf("from './runtime-tag-link.js'") >= 0, '必须 import 纯函数模块');
  assert.ok(/toggleRoleTag\(edAgentTags\.value/.test(view), '勾选必须走 toggleRoleTag');
  assert.ok(/toPayload\(edAgentTags\.value/.test(view), '保存必须走 toPayload');
  assert.ok(view.indexOf("const ALL_ROLES = [") < 0, '不得再内联角色全集');
  assert.ok(view.indexOf('agents.length === 6') < 0, '不得再写死「六档折叠」判定');
});

check('看护 角色描述文案不含「仅」', function () {
  const zh = JSON.parse(fs.readFileSync(ZH, 'utf8'));
  const desc = zh.tag.single_desc;
  assert.strictEqual(desc, '{{label}} 角色');
  assert.strictEqual(desc.indexOf('仅'), -1);
});

console.log('\n' + passed + ' passed');

// ── 2026-11-24 裁定：切场景后保存 = 按当前场景收窄（界面看不见的角色不进载荷）──────
const OPTS_B = ['all', 'issue', 'chat']; // 博客开发场景：没有 qa

check('收窄 场景外角色不再进载荷（原先会静默保留）', function () {
  const vis = L.visibleRoles(OPTS_B);
  assert.deepStrictEqual(L.toPayload(['qa'], vis), []);
  assert.deepStrictEqual(L.droppedByScene(['qa'], vis), ['qa']);
});

check('收窄 场景内角色正常保留', function () {
  const vis = L.visibleRoles(OPTS_B);
  assert.deepStrictEqual(L.toPayload(['issue'], vis), ['issue']);
  assert.deepStrictEqual(L.droppedByScene(['issue'], vis), []);
});

check('收窄 全勾可见角色=全能 → 不收窄（下发 all）', function () {
  const vis = L.visibleRoles(OPTS_B);
  assert.deepStrictEqual(L.toPayload(SIX, vis), ['all']);
  assert.deepStrictEqual(L.droppedByScene(SIX, vis), []);
});

check('收窄 混合场景内外的已勾角色 → 只留场景内，并报出被移除项', function () {
  const vis = L.visibleRoles(OPTS_B);
  assert.deepStrictEqual(L.selectedVisible(['qa', 'issue'], vis), ['issue']);
  assert.deepStrictEqual(L.droppedByScene(['qa', 'issue'], vis), ['qa']);
});

