/*
 * Issue #3355 — 切片5：chat 插件选择器双源合并（P3-5）
 *
 * Seam（用户可见行为）：
 *   chat 插件选择器不再空列表——installed 为骨架 ∪ 市场列表按 name 去重合并
 *   （市场补充项标注来源徽标）；仅 installed 请求失败才整体降级市场全量，
 *   并在浮层提示「清单可能含未安装」；选定插件随会话提交保持现状。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');
const CHAT = read('app/src/views/ChatView.vue');
const CHAT_TPL = CHAT.slice(0, CHAT.indexOf('<script'));
const zhChat = JSON.parse(read('shell/locales/zh/chat.json'));
const enChat = JSON.parse(read('shell/locales/en/chat.json'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

function fnSeg(name, len) {
  const i = CHAT.indexOf('function ' + name + '(');
  assert.ok(i >= 0, '应存在 ' + name);
  return CHAT.slice(i, i + (len || 1600));
}

check('双源合并：installed 骨架 ∪ 市场列表，按 name 去重（installed 优先）', function () {
  const seg = fnSeg('mergePluginItems', 1400);
  assert.ok(/installed/.test(seg), '合并应以 installed 为骨架');
  assert.ok(/market/.test(seg), '应并入市场列表');
  assert.ok(/_source/.test(seg), '合并项应标注来源');
  assert.ok(/names\[/.test(seg) || /seen/.test(seg), '应按 name 去重');
});

check('数据源顺序：先 installed；市场列表补齐骨架', function () {
  const seg = fnSeg('loadPluginItems', 2200);
  const iInstalled = seg.indexOf("G.API.get('/plugins/installed')");
  const iMarket = seg.indexOf("G.API.get('/plugins?page=1&page_size=100')");
  assert.ok(iInstalled >= 0, '应拉 installed 清单');
  assert.ok(iMarket > iInstalled, '应拉市场列表补齐（在 installed 之后）');
  assert.ok(/mergePluginItems\(/.test(seg), '应走统一合并入口');
});

check('仅 installed 失败 → 整体降级市场全量 + degradedMarket 标记', function () {
  const seg = fnSeg('loadPluginItems', 2400);
  const iCatch = seg.indexOf('.catch(function () {');
  const iInstalled = seg.indexOf("G.API.get('/plugins/installed')");
  assert.ok(iCatch > iInstalled, 'installed 失败应进入降级分支');
  assert.ok(/degradedMarket\.value = true/.test(seg), '降级应置 degradedMarket 标记');
  assert.ok(/mergePluginItems\(\[\]/.test(seg), '降级时骨架为空、市场全量兜底');
  assert.ok(/degradedMarket\.value = false/.test(seg), '正常合并应复位降级标记');
});

check('浮层提示：降级时展示「清单可能含未安装」+ 来源徽标', function () {
  assert.ok(/degradedMarket/.test(CHAT_TPL), '模板应消费降级标记');
  assert.ok(/plugin\.degraded_hint/.test(CHAT_TPL), '应展示降级提示');
  assert.ok(/_source === 'installed'|_source !== 'market'/.test(CHAT_TPL), '条目应按来源显示徽标');
  assert.ok(/plugin\.source_market/.test(CHAT_TPL), '市场来源徽标');
  assert.ok(/plugin\.source_installed/.test(CHAT_TPL), '已安装来源徽标');
});

check('i18n：降级提示与来源徽标词条进入 zh/en 双包', function () {
  ;['plugin.degraded_hint', 'plugin.source_installed', 'plugin.source_market'].forEach(function (k) {
    const parts = k.split('.');
    assert.ok(zhChat.plugin && zhChat.plugin[parts[1]], 'zh chat.json 缺 ' + k);
    assert.ok(enChat.plugin && enChat.plugin[parts[1]], 'en chat.json 缺 ' + k);
  });
});

check('submitBody.plugins 保持现状（选定插件随会话提交，不做对话级记忆）', function () {
  assert.ok(
    /if \(showPlugins && selectedPlugins\.value\.length\)\s*submitBody\.plugins = selectedPlugins\.value\.slice\(\)/.test(
      CHAT
    ),
    '插件随会话提交语义不得改动（#3395 增加 showPlugins 开关守卫）'
  );
});

console.log('\n#3355 chat 选择器双源合并契约通过 (' + passed + '/6)');
