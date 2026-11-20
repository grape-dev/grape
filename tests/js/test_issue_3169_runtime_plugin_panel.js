/*
 * Issue #3169 — 插件市场 · Slice 5：Runtime 引擎详情插件面板（源码级断言）
 *
 * Seam（用户可见行为 → !1666 契约实查口径）：
 *   1. 引擎详情弹窗新增「插件」段：installed_plugins（版本/指纹/自检状态）
 *   2. 数据源：GET /api/plugins/agent/installed?runtime_id={id}
 *      （B5 物化/引擎自报/C1 注册表降级链，条目含 name/version/fingerprint/health）
 *   3. desired_plugins 协商状态：由 runtime 详情接口下发（角色标签种子 ∪ 控制台远装期望，
 *      与后端 derive_plugin_health 同源），前端对比 installed 名单：
 *      missing = desired - installed；healthy = missing 为空（多装 extra 不扣健康，对齐后端口径）
 *   4. 打开详情时加载（renderEngineDetail 链路）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const VUE = fs.readFileSync(path.join(FE, 'app/src/views/RuntimesView.vue'), 'utf8');
const tpl = VUE.slice(0, VUE.indexOf('<script'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}
function bodyAfter(marker, len) {
  const i = VUE.indexOf(marker);
  assert.ok(i >= 0, '未找到 ' + marker);
  return VUE.slice(i, i + (len || 4000));
}

check('引擎详情弹窗有插件段（installed + desired 协商）', function () {
  assert.ok(/rtv-ed-sec/.test(tpl), '引擎详情应有分区结构');
  assert.ok(/rtv-plugin/.test(tpl), '应有插件面板专属区块（rtv-plugin*）');
});

check('模板展示 installed_plugins：名称/版本/指纹/自检状态四列', function () {
  assert.ok(/rtvPluginInstalled/.test(tpl), '应消费 installed 列表状态');
  for (const k of ['.name', '.version', 'fingerprint', 'health']) {
    assert.ok(tpl.includes(k), '插件行应展示 ' + k);
  }
});

check('模板展示 desired_plugins 协商状态（missing/healthy 徽标）', function () {
  assert.ok(/rtvPluginDesired|desiredPlugins/.test(tpl), '应展示期望插件集');
  assert.ok(/rtvPluginHealth[^]*missing|pluginMissing|rtvPluginMissing/.test(tpl), '应展示缺失协商态');
  assert.ok(/rtvPluginHealth|pluginHealth/.test(tpl), '应展示自检健康态');
});

check('数据源契约：GET /api/plugins/agent/installed?runtime_id={id}&repo_key=', function () {
  const b = bodyAfter("function loadRtPlugins(");
  assert.ok(/\/plugins\/agent\/installed/.test(b), '应调 agent/installed');
  assert.ok(/runtime_id/.test(b), 'runtime_id 参数必带');
  assert.ok(/repo_key=/.test(b), '应带 repo_key（JWT 空 repo_key 会被 403）');
});

check('desired 数据源：runtime 详情接口下发 desired_plugins（Spec 四.4）', function () {
  const b = bodyAfter("function loadRtPlugins(");
  assert.ok(/desired_plugins/.test(b), '应消费详情下发的 desired_plugins');
  assert.ok(/settings\/agent-runtimes/.test(b), '列表项缺字段时应回查 runtime 详情');
});

check('协商状态派生：missing = desired - installed；healthy = missing 为空', function () {
  const b = bodyAfter("function loadRtPlugins(");
  assert.ok(/filter|includes|indexOf/.test(b), '应做集合差运算');
  // missing 判定 + healthy 判定
  assert.ok(/missing/.test(b), '应产出 missing 集合');
  assert.ok(/length === 0|length == 0|\.length\b/.test(b), 'healthy 应以 missing 为空判定');
});

check('打开引擎详情时加载插件数据（renderEngineDetail 链路）', function () {
  const b = bodyAfter("function renderEngineDetail(");
  assert.ok(/loadRtPlugins\(/.test(b), 'renderEngineDetail 应触发插件数据加载');
});

console.log('\n#3169 Slice 5 全部通过 (' + passed + '/7)');
