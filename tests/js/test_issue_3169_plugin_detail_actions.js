/*
 * Issue #3169 — 插件市场 · Slice 2：详情页 + 点赞切换 + 版本族/回滚 + 安装（源码级断言）
 *
 * Seam（用户可见行为 → !1666 契约实查口径）：
 *   1. 卡片点击 → 详情页：GET /api/plugins/by-id/{id}（含 manifest 展示）
 *   2. 点赞双向切换：POST /api/plugins/{plugin_id}/like —— ⚠️ 契约实查路径无 by-id
 *      （Issue 原文笔误已纠正），liked/like_count 回写
 *   3. 版本族：GET /api/plugins/by-id/{id}/versions；生效版本切换 POST /api/plugins/by-id/{id}/rollback
 *   4. 安装：POST /api/plugins/install（body=manifest 清单透传）；已安装清单 GET /api/plugins/installed；
 *      卸载 DELETE /api/plugins/{name}
 *   5. 详情抽屉关闭回列表（closeDetail 复位列表态；#3355 抽屉化后不再使用 hash）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');
const VUE = read('app/src/views/PluginMarketView.vue');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}
function bodyAfter(marker) {
  const i = VUE.indexOf(marker);
  assert.ok(i >= 0, '未找到 ' + marker);
  return VUE.slice(i, i + 2000);
}

check('详情契约：GET /api/plugins/by-id/{id} 拉取 manifest 详情', function () {
  const b = bodyAfter("function loadDetail(");
  assert.ok(/\/plugins\/by-id\/'/.test(b), '应调 /plugins/by-id/');
  assert.ok(/encodeURIComponent/.test(b), 'id 应编码');
});

check('点赞契约：POST /api/plugins/{id}/like（实查无 by-id 前缀）', function () {
  assert.ok(!/\/plugins\/by-id\/[^'"']*\/like/.test(VUE), '不得使用 /by-id/{id}/like（契约实查已纠正）');
  const b = bodyAfter("function onLike(");
  assert.ok(/'\/plugins\/' \+ p\.id \+ '\/like'/.test(b), "应拼 '/plugins/' + id + '/like'");
  assert.ok(/r\.liked/.test(b), '应回写 liked 状态（双向切换）');
  assert.ok(/like_count/.test(b), '应回写点赞计数');
});

check('版本族契约：GET /api/plugins/by-id/{id}/versions', function () {
  const b = bodyAfter("function loadVersions(");
  assert.ok(/\/plugins\/by-id\/'/.test(b), '应调 /plugins/by-id/{id}/versions');
  assert.ok(/versions/.test(b), '应取版本族列表');
});

check('回滚契约：POST /api/plugins/by-id/{id}/rollback（body.version 目标版本号）', function () {
  const b = bodyAfter("function doRollback(");
  assert.ok(/'\/plugins\/by-id\/'/.test(b), '应调 /plugins/by-id/{id}/rollback');
  assert.ok(/version\s*:/.test(b), 'body 应带 version 字段');
});

check('安装契约：POST /api/plugins/install（manifest 清单透传）', function () {
  const b = bodyAfter("function doInstall(");
  assert.ok(/API\.post\('\/plugins\/install'/.test(b), '应调 POST /plugins/install');
  assert.ok(/manifest/i.test(b), 'body 应透传 manifest 清单');
});

check('已安装契约：GET /api/plugins/installed；卸载 DELETE /api/plugins/{name}', function () {
  assert.ok(/API\.get\('\/plugins\/installed'\)/.test(VUE), '应调 GET /plugins/installed');
  const b = bodyAfter("function doUninstall(");
  assert.ok(/API\.del\('\/plugins\/'/.test(b), "应调 DELETE '/plugins/{name}'");
});

check('详情返回：#3355 抽屉化后关闭仅复位抽屉态（列表保持，不再整页跳转/改 hash）', function () {
  // #3355：详情由整页替换改为右侧抽屉——backToList 语义被 closeDetail 取代
  const b = bodyAfter("function closeDetail(");
  assert.ok(/detail\.value = null/.test(b), '应清空详情状态');
  assert.ok(!/loadPlugins\(\)/.test(b), '关闭抽屉不得重拉列表');
});

check('详情渲染：manifest + 版本族 + 安装按钮在模板中消费', function () {
  const tpl = VUE.slice(0, VUE.indexOf('<script'));
  assert.ok(/manifest/.test(tpl), '详情应展示 manifest');
  assert.ok(/versions\b/.test(tpl), '详情应展示版本族列表');
  assert.ok(/doInstall|tp\('install'\)/.test(tpl), '模板应有安装按钮');
  assert.ok(/rollback/.test(tpl), '模板应有回滚入口');
});

console.log('\n#3169 Slice 2 全部通过 (' + passed + '/8)');
