// Issue #1208 前端源断言（引擎弹窗两模式 radio + 命中预览 + uid 输出）——Vue shell 版。
// 2026-11-13 M2 移植：断言目标从死树 static/js/pages/dashboard.js 重定向到
// frontend/app/src/views/DashboardView.vue（死树在新基线无消费者，原断言=守卫死代码）。
// 沿用 tests/js/test_issue_1011_autopilot_uid.js 的 node 源断言模式（无 DOM，不执行逻辑）。
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../../src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');

let failures = [];
let count = 0;
function assertContains(name, needle) {
  count++;
  if (src.indexOf(needle) === -1) failures.push(`missing: ${name} -> ${needle}`);
}
function section(start, end) {
  const i = src.indexOf(start);
  const j = src.indexOf(end, i + start.length);
  return i >= 0 && j >= 0 ? src.slice(i, j) : '';
}

// 1. 模式 radio：◉按标签匹配（新场景默认）/ ◉指定引擎实例（§2.5）
assertContains('mode radio tag', '<input type="radio" value="tag" v-model="engineModal.mode"');
assertContains('mode radio tag label', "t('selector_mode_tag')");
assertContains('mode radio engine', '<input type="radio" value="engine" v-model="engineModal.mode"');
assertContains('mode radio engine label', "t('selector_mode_engine')");
// 2026-11-18：改为「已存 selector（tags 是数组，含广播的空数组）→ 标签模式」，
// 原来用 selTags.length 会让广播节点重开时掉回实例面板（用户实测报障）
assertContains('new scene default engine mode', "engineModal.mode = Array.isArray(selTagsRaw) ? 'tag' : 'engine'");
assertContains('broadcast selector detected by array shape', "const selTagsRaw = ((node && node.engine_selector) || {}).tags");
assertContains(
  'no length-based mode switch',
  src.indexOf("engineModal.mode = selTags.length ? 'tag' : 'engine'") === -1 ? 'Array.isArray(selTagsRaw)' : 'OLD_EXPR_STILL_THERE',
);
// 2026-11-18 用户裁定：指定引擎实例是改造前的原始默认形态，放第一页
const iEng = src.indexOf('<input type="radio" value="engine" v-model="engineModal.mode"');
const iTag = src.indexOf('<input type="radio" value="tag" v-model="engineModal.mode"');
count++;
if (!(iEng >= 0 && iTag >= 0 && iEng < iTag)) failures.push('order: engine radio must come before tag radio');
// 2. 模式提示灰字在各自面板内（D19 对仗；tag hint 在标签面板、engine hint 在实例面板）
assertContains('hint tag line in tag panel', "t('selector_tag_hint')");
assertContains('hint engine line in engine panel', "t('selector_engine_hint')");
// 3. selector 复选组三组：engine: / scope:（UI 标签「范围」）/ scene:（D15 勿造新词）
assertContains('engine group', "prefix: 'engine',");
assertContains('scope group label i18n', "const SEL_SCOPE_LABELS = { global: t('dv2.sel_scope_global'), user: t('dv2.sel_scope_user'), repo: t('dv2.sel_scope_repo') }");
assertContains('scene group', "prefix: 'scene',");
// 4. 匹配语义组内 OR + 组间 AND（D14 客户端同语义）
assertContains('match groups every(AND)', '.every(function (p)');
assertContains('match group some(OR)', '.some(function (t)');
// 5. 命中预览分层标注（selector 排除/角色排除/健康）
assertContains('preview layered reasons', "reason: 'selector'");
assertContains('preview role reason', "reason: 'role'");
assertContains('preview offline reason', "reason: 'offline'");
assertContains('preview render call', 'engSelectorPreview(vis, selTags, engineModal.agentType)');
// 6. 模式切换：两模式各自保留自己的勾选，互不清空、互不派生（用户口径 2026-11-09）
const swBody = section('function onModeSwitch()', 'function toggleSelTag');
count++;
if (swBody.indexOf('engineModal.selTags = []') >= 0) failures.push('switch must not clear tag selection');
count++;
if (swBody.indexOf('engCurIds.value = []') >= 0) failures.push('switch must not clear engine selection');
count++;
if (swBody.indexOf('engSelectorSuggest') >= 0) failures.push('switch must not derive tags from bound engines');
count++;
if (swBody.indexOf('engSelectorMatchEngine') >= 0) failures.push('switch must not preselect engines from tags');
// 6b. 两模式弹窗同尺寸（实例模式不得缩回 460x600）
const modalCss = section('.dbv-engmodal {', '}');
count++;
if (modalCss.indexOf('460px') >= 0) failures.push('engine mode must not shrink modal to 460px');
assertContains('both modes 640 width', 'width: min(640px, calc(100vw - 32px));');
assertContains('both modes same height', 'height: min(90vh, 760px);');
// 7. uid 输出（D11）：全部选中引擎有 uid → node.engine_runtime_uids + ids 清空
assertContains('confirm writes selector', 'node.engine_selector = { tags: engineModal.selTags.slice().sort() }');
assertContains('selector mode clears pools', 'node.engine_runtime_ids = []');
assertContains('uid upgrade on confirm', 'node.engine_runtime_uids = selUids.map((u) => u.toLowerCase())');
assertContains('uid branch clears ids', 'if (selEngines.length && selUids.every((u) => u)) {');
assertContains('legacy fallback ids', 'node.engine_runtime_ids = engCurIds.value.slice()');
assertContains('uid echo precheck', 'uidSet.has(String(e.engine_uid).toLowerCase())');
// 8. 三模式字段全量透传（编辑/创建 draft + 保存 payload + 禁用清理）
assertContains('edit nodes carry uids/selector', 'engine_runtime_uids: node.engine_runtime_uids || a.engine_runtime_uids || []');
assertContains('nodes payload carries selector', 'engine_selector: n.engine_selector || {}');
assertContains('disable clears all three modes', 'node.engine_selector = {}')
// 9. selector 徽章（摘要展示人类可读维度标签：引擎/范围/场景）
assertContains('selector badge', "t('dv2.sum_tag_hit', { total: total, hit: hit })");
assertContains('selector badge label helper', 'function engSelectorTagLabel(tag)');
assertContains('badge entry', 'function engineBadgeHtml(node)');

if (failures.length) {
  console.error('FAIL tests/js/test_issue_1208_engine_mode_radio.js');
  failures.forEach(f => console.error('  ' + f));
  process.exit(1);
}
console.log(`PASS tests/js/test_issue_1208_engine_mode_radio.js (${count} assertions)`);
