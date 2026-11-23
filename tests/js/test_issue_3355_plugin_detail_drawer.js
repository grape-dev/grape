/*
 * Issue #3355 — 切片1：插件详情抽屉化（P3-1）
 *
 * Seam（用户可见行为）：插件市场详情不再是整页替换，而是右侧抽屉浮层；
 * 关闭抽屉后列表筛选/滚动状态保持不变（列表不卸载、不重拉）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');
const MARKET = read('app/src/views/PluginMarketView.vue');
const MARKET_TPL = MARKET.slice(0, MARKET.indexOf('<script'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('详情不再是整页替换：列表不随 detail 卸载', function () {
  assert.ok(!/v-if="!detail"/.test(MARKET_TPL), '列表不应再以 v-if="!detail" 整页分支包裹');
  assert.ok(!/det-back/.test(MARKET_TPL), '不应保留整页详情返回按钮');
  assert.ok(!/function backToList\(/.test(MARKET), '不应再保留 backToList 整页返回');
});

check('详情抽屉：右侧自研 overlay（遮罩 + 右缘面板 + 点击遮罩关闭）', function () {
  assert.ok(/pm__drawer-mask/.test(MARKET_TPL), '应有抽屉遮罩层');
  assert.ok(/pm__drawer\b/.test(MARKET_TPL), '应有抽屉面板容器');
  assert.ok(/@click\.self="closeDetail"/.test(MARKET_TPL), '点击遮罩空白处应关闭抽屉');
  assert.ok(/pm__drawer-mask\s*{/.test(MARKET), '遮罩应有样式（自研，不引组件库）');
  assert.ok(/\.pm__drawer\s*{/.test(MARKET), '抽屉面板应有样式');
  assert.ok(/right:\s*0/.test(MARKET), '抽屉应贴右缘（右侧抽屉）');
});

check('抽屉开关走 detailOpen 状态，openDetail 打开抽屉', function () {
  assert.ok(/detailOpen = ref\(/.test(MARKET), '应存在 detailOpen 抽屉开关状态');
  const i = MARKET.indexOf('function openDetail(');
  assert.ok(i >= 0, '应存在 openDetail');
  const seg = MARKET.slice(i, i + 500);
  assert.ok(/detailOpen\.value = true/.test(seg), 'openDetail 应打开抽屉');
});

check('关闭仅复位抽屉态：不重拉列表（筛选/滚动保持）', function () {
  const i = MARKET.indexOf('function closeDetail(');
  assert.ok(i >= 0, '应存在 closeDetail');
  const seg = MARKET.slice(i, i + 600);
  assert.ok(/detailOpen\.value = false/.test(seg), 'closeDetail 应关闭抽屉');
  assert.ok(!/loadPlugins\(\)/.test(seg), '关闭抽屉不得重拉列表');
  assert.ok(/'plugins:' \+ tab\.value/.test(seg), '应复位视图哨兵到列表态');
});

console.log('\n#3355 详情抽屉契约通过 (' + passed + '/4)');
