/*
 * Issue #3395 — 切片D：chat 插件多选控件显隐 + 提交体剔除 plugins
 *
 * Seam：
 *   1. flag=0 时输入区 #chatPluginBtn 与 plugin picker 用 v-if 不渲染；
 *   2. 提交体构造段在 flag=0 时不写入 plugins 字段（即使残留选中状态）；
 *   3. flag=1 与现状一致（按钮/picker/提交 plugins 均在）。
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
function segAfter(marker, len) {
  const i = VUE.indexOf(marker);
  assert.ok(i >= 0, '未找到 ' + marker);
  return VUE.slice(i, i + (len || 900));
}

check('ChatView 从统一 helper 读取开关', function () {
  assert.ok(
    /import\s*\{[^}]*pluginsEnabled[^}]*\}\s*from\s*['"]@\/lib\/features['"]/.test(VUE),
    '应 import { pluginsEnabled } from @/lib/features'
  );
  assert.ok(/const\s+showPlugins\s*=\s*pluginsEnabled\(\)/.test(VUE), '应有 showPlugins 开关常量');
});

check('插件按钮 #chatPluginBtn 带 v-if="showPlugins"', function () {
  const i = tpl.indexOf('id="chatPluginBtn"');
  assert.ok(i >= 0, '应保留插件按钮（flag=1 可用）');
  const start = tpl.lastIndexOf('<button', i);
  const end = tpl.indexOf('>', i);
  const openTag = tpl.slice(start, end + 1);
  assert.ok(/v-if="showPlugins"/.test(openTag), '插件按钮应带 v-if="showPlugins"：' + openTag);
});

check('plugin picker 分支在开关关闭时不可渲染', function () {
  const i = tpl.indexOf("pickerOpen === 'plugin'");
  assert.ok(i >= 0, '应保留 plugin picker 分支');
  const start = tpl.lastIndexOf('<div', i);
  const end = tpl.indexOf('>', i);
  const openTag = tpl.slice(start, end + 1);
  assert.ok(/showPlugins/.test(openTag), 'picker 分支应带 showPlugins 守卫：' + openTag);
});

check('提交体在 flag=0 时不写 plugins 字段', function () {
  const seg = segAfter('function doSend(', 3500);
  const i = seg.indexOf('submitBody.plugins');
  assert.ok(i >= 0, 'flag=1 仍应写 submitBody.plugins');
  const stmt = seg.slice(Math.max(0, i - 200), i + 80);
  assert.ok(/showPlugins/.test(stmt), 'plugins 赋值应受 showPlugins 守卫：' + stmt);
  assert.ok(/selectedPlugins\.value\.length/.test(stmt), '仍应仅在选中时携带');
});

check('插件数据加载在 flag=0 时短路（不产生无意义 API 调用）', function () {
  const seg = segAfter('function loadPluginItems(', 400);
  assert.ok(/showPlugins/.test(seg), 'loadPluginItems 应在开关关闭时提前返回');
});

console.log('\n#3395 chat 插件选择器/提交体 全部通过 (' + passed + '/5)');