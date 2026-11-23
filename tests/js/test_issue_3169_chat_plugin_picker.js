/*
 * Issue #3169 — 插件市场 · Slice 4：Chat 插件多选（源码级断言）
 *
 * Seam（用户可见行为 → !1666 契约实查口径）：
 *   1. Chat 输入区 skill 选择器旁新增插件按钮 → picker 浮层（复用既有 chat-picker 模式）
 *   2. 插件列表数据源：GET /api/plugins/installed（C1 注册表；失败降级 GET /api/plugins 市场列表）
 *   3. 多选：点击项在选中集合中增删（非 skill 的单选 saveSkillConfig 模式）
 *   4. 多选结果随会话上下文提交：/chat/submit body 带 plugins 字段（选中插件名数组）
 *   5. 选中外化：输入区插件按钮带选中计数徽标
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const VUE = fs.readFileSync(path.join(FE, 'app/src/views/ChatView.vue'), 'utf8');
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
  return VUE.slice(i, i + (len || 3000));
}

check('输入区 skill 按钮旁有插件按钮（chatPluginBtn，触发 plugin picker）', function () {
  assert.ok(/id="chatPluginBtn"/.test(tpl), '应有 chatPluginBtn');
  assert.ok(/togglePicker\('plugin'\)/.test(tpl), '点击应 togglePicker plugin');
  // 位置紧邻 chatSkillBtn（按钮区相邻，位于 skill 选择器旁）
  const si = tpl.indexOf('id="chatSkillBtn"');
  const pi = tpl.indexOf('id="chatPluginBtn"');
  assert.ok(si >= 0 && pi > si && pi - si < 1500, '插件按钮应紧邻 skill 按钮（DOM 相邻）');
});

check('picker 浮层支持 plugin 分支（多选列表）', function () {
  const seg = tpl.slice(tpl.indexOf("pickerOpen === 'skill'") >= 0 ? tpl.indexOf("pickerOpen === 'skill'") - 400 : 0);
  assert.ok(/pickerOpen === 'plugin'/.test(tpl), "模板应有 pickerOpen === 'plugin' 分支");
  assert.ok(/chat-picker-item/.test(tpl), '复用 chat-picker-item 行样式');
  // 多选：列表项点击 togglePlugin（不是 save*Config 单选）
  assert.ok(/togglePlugin\(/.test(tpl), '列表项应调 togglePlugin（多选增删）');
});

check('插件按钮带选中计数徽标（选中外化）', function () {
  const pi = tpl.indexOf('id="chatPluginBtn"');
  const seg = tpl.slice(pi, pi + 1400);
  assert.ok(/selectedPlugins\.length|selectedPluginsCount/.test(seg), '按钮上应显示选中数量');
});

check('数据源：GET /api/plugins/installed（失败降级市场列表）', function () {
  const b = bodyAfter("function loadPluginItems(");
  assert.ok(/API\.get\('\/plugins\/installed'\)/.test(b), '主数据源应为 /plugins/installed');
  assert.ok(/API\.get\('\/plugins\?/.test(b) || /API\.get\('\/plugins'/.test(b), '应含降级市场列表分支');
});

check('多选状态：selectedPlugins 集合 + togglePlugin 增删', function () {
  const b = bodyAfter("const selectedPlugins");
  assert.ok(/ref\(\[\]\)/.test(b.slice(0, 200)) || /= \[\]/.test(b.slice(0, 200)), 'selectedPlugins 应为数组');
  const tb = bodyAfter("function togglePlugin(");
  assert.ok(/indexOf/.test(tb), '应查重（indexOf/includes）');
  assert.ok(/splice|filter/.test(tb), '重复点击应移除（toggle）');
  assert.ok(/push/.test(tb), '新选中应加入集合');
});

check('提交契约：/chat/submit body 携带 plugins 字段（选中插件名数组）', function () {
  const b = bodyAfter("function doSend(", 3500);
  assert.ok(/plugins/.test(b) && /selectedPlugins/.test(b), 'submit body 应带 plugins 字段');
  const ui = b.indexOf("API.post('/chat/submit'");
  const seg = b.slice(0, ui);
  assert.ok(/plugins/.test(seg), 'plugins 应在 /chat/submit body 构造段内');
});
console.log('\n#3169 Slice 4 全部通过 (' + passed + '/6)');
