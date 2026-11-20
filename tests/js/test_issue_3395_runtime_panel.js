/*
 * Issue #3395 — 切片E：Runtime 详情插件面板显隐
 *
 * Seam：
 *   1. flag=0 时引擎详情「插件」段（installed_plugins / desired_plugins）v-if 不渲染；
 *   2. 不渲染时不再发起 /plugins/agent/installed 等请求（loadRtPlugins 短路）；
 *   3. flag=1 与现状一致。
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
function segAfter(marker, len) {
  const i = VUE.indexOf(marker);
  assert.ok(i >= 0, '未找到 ' + marker);
  return VUE.slice(i, i + (len || 500));
}

check('RuntimesView 从统一 helper 读取开关', function () {
  assert.ok(
    /import\s*\{[^}]*pluginsEnabled[^}]*\}\s*from\s*['"]@\/lib\/features['"]/.test(VUE),
    '应 import { pluginsEnabled } from @/lib/features'
  );
  assert.ok(/const\s+showPlugins\s*=\s*pluginsEnabled\(\)/.test(VUE), '应有 showPlugins 开关常量');
});

check('插件面板区块带 v-if="showPlugins"（flag=0 不渲染）', function () {
  const i = tpl.indexOf('rtv-ed-sec rtv-ed-last');
  assert.ok(i >= 0, '应保留插件面板区块（flag=1 可用）');
  const start = tpl.lastIndexOf('<div', i);
  const end = tpl.indexOf('>', i);
  const openTag = tpl.slice(start, end + 1);
  assert.ok(/v-if="showPlugins"/.test(openTag), '插件面板开标签应带 v-if="showPlugins"：' + openTag);
  assert.ok(/label\.plugin_panel/.test(tpl), 'flag=1 仍展示插件面板标题');
});

check('插件数据加载在 flag=0 时短路（不打插件 API）', function () {
  const seg = segAfter('function loadRtPlugins(', 500);
  assert.ok(/showPlugins/.test(seg), 'loadRtPlugins 应在开关关闭时提前返回');
});

console.log('\n#3395 Runtime 插件面板 全部通过 (' + passed + '/3)');