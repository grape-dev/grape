/*
 * Issue #3355 — 切片6：场景节点插件 display_name 展示 + 保存后标签不丢失
 *
 * Seam（用户可见行为）：
 *   场景节点卡片显示其引用插件的中文名（display_name，缺失回落 name，映射取
 *   installed ∪ 市场列表）；场景编辑保存不丢节点 plugins 字段（序列化/负载/
 *   编辑节点/草稿全链携带）；保存成功 1.5s 自动 reload（防缺字段导致插件标签
 *   渲染丢失，对齐老版线 dashboard.js:1864 行为）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');
const DASH = read('app/src/views/DashboardView.vue');
const DASH_TPL = DASH.slice(0, DASH.indexOf('<script'));
const zhDash = JSON.parse(read('shell/locales/zh/dashboard.json'));
const enDash = JSON.parse(read('shell/locales/en/dashboard.json'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

function fnSeg(name, len) {
  const i = DASH.indexOf('function ' + name + '(');
  assert.ok(i >= 0, '应存在 ' + name);
  return DASH.slice(i, i + (len || 1200));
}

check('节点卡片渲染插件标签区（display_name 展示）', function () {
  assert.ok(/ap-nd-plugtag/.test(DASH_TPL), '节点卡应有插件标签');
  assert.ok(/nodePlugins\(node\)/.test(DASH_TPL), '应按节点 plugins 渲染');
  assert.ok(/pluginLabel\(/.test(DASH_TPL), '标签应走 display_name 映射');
});

check('display_name 映射：installed ∪ 市场列表（缺失回落 name）', function () {
  const seg = fnSeg('pluginLabel', 300);
  assert.ok(/pluginNameMap\.value\[name\] \|\| name/.test(seg), '缺失时回落 name');
  const seg2 = fnSeg('loadPluginNameMap', 1200);
  assert.ok(/\/plugins\/installed/.test(seg2), '映射应取 installed 清单');
  assert.ok(/\/plugins\?page=1&page_size=100/.test(seg2), '映射应并入市场列表补缺');
  const seg3 = fnSeg('applyPluginNames', 500);
  assert.ok(/display_name \|\| p\.name/.test(seg3), '映射值应为 display_name 回落 name');
});

check('保存链携带 plugins：serializeNodes / buildNodesPayload / buildEditNodes / applyDraftNodes', function () {
  const ser = fnSeg('serializeNodes', 700);
  assert.ok(/plugins: n\.plugins \|\| \[\]/.test(ser), '草稿序列化不得丢 plugins');
  const payload = fnSeg('buildNodesPayload', 700);
  assert.ok(/plugins: n\.plugins \|\| \[\]/.test(payload), '保存负载不得丢 plugins');
  const build = fnSeg('buildEditNodes', 1800);
  assert.ok(/plugins: node\.plugins \|\| \[\]/.test(build), '编辑节点应继承原 plugins');
  const draft = fnSeg('applyDraftNodes', 700);
  assert.ok(/dn\.plugins \|\| \[\]/.test(draft), '草稿恢复应回填 plugins');
});

check('保存成功后 1.5s 自动 reload（防插件标签渲染丢失）', function () {
  const seg = fnSeg('saveSceneEdit', 2400);
  assert.ok(/setTimeout\(/.test(seg), '应延时 reload');
  assert.ok(/1500/.test(seg), '延时应为 1500ms');
  assert.ok(/location\.reload\(\)/.test(seg), '应整页 reload（对齐老版线行为）');
});

check('i18n：插件标签区词条进入 zh/en 双包', function () {
  assert.ok(zhDash.label_plugins, 'zh dashboard.json 缺 label_plugins');
  assert.ok(enDash.label_plugins, 'en dashboard.json 缺 label_plugins');
});

// ── 复审返工（QA 202045541 P1-1）：禁用节点不得清空插件绑定 ──
check('禁用/重启节点保留 plugins 绑定（禁用不清空、不写草稿丢失）', function () {
  const seg = fnSeg('toggleAgent', 900);
  assert.ok(/node\.enabled = !node\.enabled/.test(seg), '仍应切换启用态');
  assert.ok(!/node\.plugins\s*=\s*\[\]/.test(seg), '禁用节点不得清空 plugins（Spec 未要求，且会持久化丢失）');
  assert.ok(!/node\.plugins\s*=/.test(seg), 'toggleAgent 不应改写 plugins 字段');
});

console.log('\n#3355 场景节点插件契约通过 (' + passed + '/6)');
