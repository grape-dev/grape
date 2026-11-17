// Issue #1208 v3 用户手测反馈（弹窗 UI/一致性）源断言——Vue shell 版。
// 2026-11-13 M2 移植：断言目标从死树 static/js/pages/dashboard.js 重定向到
// frontend/app/src/views/DashboardView.vue + shell locales + shell index.html。
// 沿用 node 源断言模式（无 DOM，不执行逻辑）。
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../../src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');
const zh = fs.readFileSync(path.join(__dirname, '../../src/api/frontend/shell/locales/zh/dashboard.json'), 'utf8');
const en = fs.readFileSync(path.join(__dirname, '../../src/api/frontend/shell/locales/en/dashboard.json'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../../src/api/frontend/shell/index.html'), 'utf8');

let failures = [];
let count = 0;
function assertContains(name, hay, needle) {
  count++;
  if (hay.indexOf(needle) === -1) failures.push(`missing: ${name} -> ${needle}`);
}
function assertNotContains(name, hay, needle) {
  count++;
  if (hay.indexOf(needle) !== -1) failures.push(`forbidden: ${name} -> ${needle}`);
}
function section(start, end) {
  const i = src.indexOf(start);
  const j = src.indexOf(end, i + start.length);
  return i >= 0 && j >= 0 ? src.slice(i, j) : '';
}

// 1. 弹窗溢出：放大 + 内容区自滚（radio 行/底部按钮固定）
assertContains('modal enlarged', src, 'width: min(640px, calc(100vw - 32px));');
assertContains('modal height capped', src, 'height: min(90vh, 760px);');
assertContains('selector panel scrolls', src, '.dbv-selpanel {\n  flex: 1;\n  min-height: 0;\n  overflow-y: auto;');
assertContains('radios fixed', src, '.dbv-engmodes {\n  display: flex;');
assertContains('buttons fixed', src, '.dbv-engfoot');

// 2. 模式提示词在各自面板，radio 行无灰字
assertContains('tag hint lives in tag panel', src, "t('selector_tag_hint')");
assertContains('engine hint in engine panel', src, "t('selector_engine_hint')");
const radios = section('class="dbv-engmodes"', '══ 标签模式面板');
assertNotContains('radio block has no tag hint', radios, '按标签匹配：声明引擎标签');
assertNotContains('radio block has no engine hint', radios, '指定引擎实例：逐台点名具体实例');

// 3. 命中预览：命中前置 + 复用引擎卡片样式 + 只读 + 分层原因
assertContains('hit first sort', src, 'if (a.hit !== b.hit) return a.hit ? -1 : 1');
const prevRows = section('v-for="sec in previewSections"', '══ 实例模式面板');
assertContains('preview reuses tag badge', prevRows, 'ENG_TAG_COLORS[tg]');
assertContains('preview reuses scene badge', prevRows, 'engSceneDisplayName(st)');
assertContains('preview reuses scope badge', prevRows, 'SCOPE_LABELS_ALL[p.engine.scope]');
assertNotContains('preview card no checkbox', prevRows, 'type="checkbox"');
assertContains('preview layered reason label', src, "selector: 'selector_reason_selector'");
assertContains('preview stale reason', src, "stale: 'selector_reason_stale'");

// 4. 标签模式保留搜索框并即时过滤预览（只过滤展示，不影响命中）
assertContains('search row always shown', src, 'v-model="engineModal.q" class="dbv-engsearch"');
assertContains('search filters preview', src, 'return [p.engine.name, p.engine.alias].join');
assertContains('preview computed consumes q', src, "const q = (engineModal.q || '').toLowerCase().trim()");

// 5. 文案 i18n：关键文案走 t() 且有 zh/en 词条
['selector_mode_tag', 'selector_mode_engine', 'selector_hit_preview', 'selector_hit',
 'selector_group_engine', 'selector_group_scope', 'selector_group_scene',
 'selector_reason_selector', 'selector_reason_role', 'selector_reason_offline'].forEach(function(k) {
  if (k.indexOf('_reason_') >= 0) assertContains('reason key mapped ' + k, src, "'" + k + "'");
  else assertContains('i18n key used ' + k, src, "t('" + k + "')");
  assertContains('zh key ' + k, zh, '"' + k + '"');
  assertContains('en key ' + k, en, '"' + k + '"');
});

// 6. 前端预览与后端同源同尺：调用后端 preview 接口，失败降级本地匹配
assertContains('backend preview endpoint', src, "'/scenes/engine-selector-preview'");
assertContains('backend hit map consumed', src, 'engineModal.serverPreview[String(e.id)]');
assertContains('debounced preview request', src, 'setTimeout(requestServerPreview, 250)');
assertContains('local fallback retained', src, 'engSelectorPreview(vis, selTags, engineModal.agentType)');

// 7. 静态资源版本号 bump（Vue chunk 走 viewjs 入口）
assertContains('dashboard cache bumped', html, 'dashboard-view.js?v=74');  // 棘轮对齐 FROZEN：index.html bump 时同步（QA 复核 F2：硬编码 v=66 已过期）

// 8. 未命中列表：复用「指定引擎实例」列表排布（scope 分组/无复选框/行尾不命中原因）
assertContains('miss grouped by scope order', src, "['user', 'repo', 'global', 'local']");
assertContains('miss row has no checkbox', prevRows, 'dbv-prevrow');
assertContains('miss appends reason', prevRows, '✗ {{ reasonLabel(p.reason) }}');
assertContains('miss row help icon', prevRows, 'missReasonDetail(p)');
assertNotContains('preview rows no bare cards', src, '_renderPreviewCard');

// 9. scene: 勾选组显示场景展示名（与引擎卡片场景徽章同口径）
assertContains('scene group uses display name', src, 'return engSceneDisplayName(v)');

// 10. scene 勾选组顺序 = 场景市场注册顺序（未登记排末尾），不按字母排
assertContains('scene order follows market order', src, 'sceneTagsCache.value.forEach(function (x, i) {');
assertContains('scene order map by name', src, 'order[x.name] = i');
assertContains('scene market sort applied', src, 'scenes.sort(function (a, b) {');
assertNotContains('scene chips not alphabetically sorted', src, "scenes.sort()");

// 11. 命中但离线（前端归组展示，不改变后端 matched 语义）
assertContains('offline-hit group title', src, "t('selector_preview_offline_group')");
assertContains('offline-hit filter by reason', src, "p.reason === 'offline' || p.reason === 'stale'");
assertContains('offline mode card tint', src, '.dbv-prevrow.off {\n  background: #fffbeb;\n  border: 1px solid #fde68a;');
assertContains('offline recover hint', src, '恢复后可承接');
assertContains('header totals include offline', src, "t('selector_hit_count', { count: total })");
assertNotContains('offline-only header key removed', src, 'selector_offline_hit_count');

// 12. 三组折叠：命中/命中但离线/未命中，标题可点击折叠展开
assertContains('collapse state for 3 groups', src, 'collapse: { hit: false, offline: false, miss: false }');
assertContains('collapse toggle in template', src, 'engineModal.collapse[sec.key] = !engineModal.collapse[sec.key]');
assertContains('collapsed hint label', src, '（已折叠）');

// 13. 三组外层框 + 标题栏折叠（框线/底色/折叠箭头/计数右对齐）
assertContains('group boxed with border', src, '.dbv-secwrap {\n  border: 1px solid #e8e8ed;\n  border-radius: 12px;');
assertContains('group counts right aligned', src, '.dbv-seccount {\n  margin-left: auto;');
assertContains('group count unit', src, '{{ sec.list.length }} 台');
assertContains('hit box green tint', src, "border: '#bbf7d0', bg: '#f0fdf4'");
assertContains('offline box amber tint', src, "border: '#fde68a', bg: '#fffbeb'");
assertContains('miss box gray tint', src, "border: '#e8e8ed', bg: '#fafafa'");
assertContains('collapsed keeps body hidden', src, 'v-if="!engineModal.collapse[sec.key]"');

// 14. 命中徽章不重复打勾（i18n selector_hit 自带 ✓，勿再拼前缀）
assertContains('hit badge uses i18n value as-is', src, "t('selector_hit')");
assertNotContains('no double check prefix', src, "'✓ ' + t('selector_hit')");

// 15. 引擎类型展示名（hwcloud -> aishell）+ #902 默认角色清零
assertContains('engine type display map', src, "const ENG_TYPE_LABELS = { hwcloud: 'aishell' }");
assertContains('engine chips use display label', src, 'ENG_TYPE_LABELS[v] || v');
assertContains('frontend default roles cleared (#902)', src, 'const ENG_DEFAULT_ROLES = {}');
assertContains('zh offline group key', zh, '"selector_preview_offline_group"');
assertContains('en offline group key', en, '"selector_preview_offline_group"');

// 16. 清除已选特征按钮 + 折叠圆形图标
assertContains('clear tags handler', src, 'function clearSelTags()');
assertContains('clear tags button', src, "t('selector_clear_tags')");
assertContains('clear button shows count', src, '· {{ engineModal.selTags.length }}');
assertContains('collapse round icon', src, 'width: 24px;\n  height: 24px;\n  border-radius: 50%;');
assertContains('collapse icon filled glyphs', src, "? '▶' : '▼'");
assertContains('collapse icon tooltip', src, "engineModal.collapse[sec.key] ? t('dv2.expand_group') : t('dv2.collapse_group')");
assertContains('zh clear tags key', zh, '"selector_clear_tags"');
assertContains('en clear tags key', en, '"selector_clear_tags"');

// 17. 未命中原因浮层：可换行（max-width）+ 事件挂载 + 关闭时清理
assertContains('reason tip wrap', src, 'max-width: 320px;');
assertContains('reason tip word break', src, 'word-break: break-word;');
assertContains('reason tip show', src, 'function showReasonTip(ev, text)');
assertContains('reason tip hide', src, 'function hideReasonTip()');
assertContains('help icon wired', src, '@mouseenter="showReasonTip($event, missReasonDetail(p))"');
assertContains('modal close cleans tip', src, 'function closeEngineModal()');

// 18. 弹窗命中头与卡片缩略语同口径 + 保留括号说明
assertContains('header keeps note in parens', src, "t('selector_hit_note')");
assertContains('header shows online count', src, "' · ' + hitCount + ' 在线'");
assertContains('zh hit note key', zh, '"selector_hit_note"');
assertContains('en hit note key', en, '"selector_hit_note"');
if (failures.length) {
  console.error('FAIL tests/js/test_issue_1208_v3_selector_ui.js');
  failures.forEach(f => console.error('  ' + f));
  process.exit(1);
}
console.log(`PASS tests/js/test_issue_1208_v3_selector_ui.js (${count} assertions)`);
