/*
 * Issue #3355 — 复审返工（QA 202045541 P2-6）：保存后 Runtime 面板即时反映
 *
 * Seam（用户可见行为）：
 *   在插件详情抽屉保存凭据/参数后，Runtime 视图已打开的插件面板无需手动 ⟳
 *   即重新拉取（跨视图事件钩子；视图未挂载时由挂载重拉覆盖，chat 选择器
 *   每次打开都会重拉）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');
const MARKET = read('app/src/views/PluginMarketView.vue');
const RUNTIMES = read('app/src/views/RuntimesView.vue');
const CHAT = read('app/src/views/ChatView.vue');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

function fnSeg(src, name, len) {
  // 兼容 `function foo(` 与生命周期钩子 `foo(function (` 两种写法
  let i = src.indexOf('function ' + name + '(');
  if (i < 0) i = src.indexOf(name + '(function');
  assert.ok(i >= 0, '应存在 ' + name);
  const rest = src.slice(i);
  const end = rest.indexOf('\nfunction ');
  const seg = end > 0 ? rest.slice(0, end) : rest;
  return len ? seg.slice(0, len) : seg;
}

const EVENT = 'grape:plugin-config-saved';

check('保存凭据/参数成功后广播跨视图刷新事件', function () {
  assert.ok(/function notifyPluginConfigSaved\(/.test(MARKET), '应有保存后广播助手');
  const notify = fnSeg(MARKET, 'notifyPluginConfigSaved');
  assert.ok(notify.indexOf(EVENT) >= 0, '事件名应为 ' + EVENT);
  assert.ok(/dispatchEvent/.test(notify), '应通过 window.dispatchEvent 广播');
  assert.ok(/notifyPluginConfigSaved\(p\.name\)/.test(fnSeg(MARKET, 'saveCreds')), 'saveCreds 成功应广播');
  assert.ok(/notifyPluginConfigSaved\(p\.name\)/.test(fnSeg(MARKET, 'saveParams')), 'saveParams 成功应广播');
});

check('Runtime 视图监听事件并重拉已打开的插件面板（挂载/卸载成对）', function () {
  assert.ok(/function onPluginConfigSaved\(/.test(RUNTIMES), '应有事件监听器');
  const on = fnSeg(RUNTIMES, 'onPluginConfigSaved');
  assert.ok(/loadRtPlugins\(/.test(on), '收到事件应重拉插件面板');
  assert.ok(/engineDetail\.value/.test(on), '仅在插件面板已打开时重拉');
  const mounted = fnSeg(RUNTIMES, 'onMounted');
  assert.ok(mounted.indexOf(EVENT) >= 0, 'onMounted 应注册监听');
  const unmount = fnSeg(RUNTIMES, 'onBeforeUnmount');
  assert.ok(unmount.indexOf(EVENT) >= 0, 'onBeforeUnmount 应移除监听');
});

check('chat 侧口径：选择器每次打开重拉（覆盖会话内即时反映）', function () {
  const seg = fnSeg(CHAT, 'togglePicker');
  assert.ok(/pickerOpen\.value === 'plugin'\) loadPluginItems\(\)/.test(seg), '每次打开插件选择器都应重拉');
});

console.log('\n#3355 Runtime 即时反映契约通过 (' + passed + '/3)');
