// Issue #1208 v2 审查整改：selector 节点摘要不得为 none——徽章摘要必须展示具体标签。
// 2026-11-13 M2 移植：断言目标重定向到 Vue shell（DashboardView.vue + shell locales）。
// 沿用 node 源断言模式（无 DOM，不执行逻辑）。
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '../../src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');
const zh = fs.readFileSync(path.join(__dirname, '../../src/api/frontend/shell/locales/zh/dashboard.json'), 'utf8');

let failures = [];
let count = 0;
function assertContains(name, needle) {
  count++;
  if (src.indexOf(needle) === -1) failures.push(`missing: ${name} -> ${needle}`);
}
function assertNotContains(name, needle) {
  count++;
  if (src.indexOf(needle) !== -1) failures.push(`forbidden: ${name} -> ${needle}`);
}
function section(start, end) {
  const i = src.indexOf(start);
  const j = src.indexOf(end, i + start.length);
  return i >= 0 && j >= 0 ? src.slice(i, j) : '';
}
const badge = section('function engineBadgeHtml(node)', 'function engineBadgeState');

// 1. 摘要徽章：标签命中 N 个引擎（含离线）· M 离线；不展示不符台数；完整标签清单进 title
assertContains('labels kept for tooltip', 'const labels = selTags.map(engSelectorTagLabel)');
assertContains('hit count in summary', "t('dv2.sum_tag_hit', { total: total, hit: hit })");
assertContains('zero-hit wording', "t('dv2.sum_zero')");
assertNotContains('no 按标签 prefix', "'按标签 · ");
assertNotContains('no mismatch count in summary', "台不符'");
assertContains('full labels in tooltip', "t('dv2.sum_labels') + labels.join('、')");
assertContains('online count in summary', "(off ? t('dv2.sum_offline', { off: off }) : '')");
assertContains('offline count in summary', "t('dv2.sum_offline', { off: off })");
assertContains('hit total includes offline', 'const total = hit + off');
assertContains('scope label map', "repo: t('dv2.sel_scope_repo')");

// 2. selector 节点摘要不得为 none：selector 分支在实例徽章兜底之前 return，
//    且徽章 HTML 不含 'none' 占位文案
assertContains('selector branch precedes fallback', 'if (selTags.length || (selModeSaved && !hasInstances)) {');
assertContains('empty selector -> broadcast badge', "t('dv2.sum_broadcast', { total: total, hit: hit })");
assertContains('broadcast header', "t('selector_broadcast')");
count++;
if (badge.indexOf('none') >= 0) failures.push('forbidden: no none placeholder in badge');

// 3. 徽章兜底仍存在（uid/id 模式复用既有三态徽章）
assertContains('fallback badge reused', 'const st = engineBadgeState(ids, tag)');

// 4. 服务端同源计数（后端权威 hit/off/miss 缓存 + 空标签广播）
assertContains('server hit cache', 'selHitCache[key]');
assertContains('server hit fetch', "post('/scenes/engine-selector-preview'");
assertContains('backend matched consumed', '(d.matched || []).length');
assertContains('badge key carries repo', "agentType + '|' + selTags.slice().sort().join(',') + '|' + selHitRepo.value");

// 零命中文案（更通顺的表述）
count++;
if (zh.indexOf('标签暂无命中引擎——仍可保存（引擎上线后自动进池），在此之前任务不会派发') === -1) failures.push('missing: zero-hit wording (zh locale)');
// dv2.* 徽章词包（G-18 迁移后徽章文案的 zh 词条在位）
['sum_tag_hit', 'sum_broadcast', 'sum_offline', 'sum_zero', 'sum_labels', 'sel_scope_global', 'sel_scope_user', 'sel_scope_repo'].forEach(function (k) {
  count++;
  if (zh.indexOf('"' + k + '"') === -1) failures.push('missing: dv2 badge key (zh locale) -> ' + k);
});
if (failures.length) {
  console.error('FAIL tests/js/test_issue_1208_selector_summary.js');
  failures.forEach(f => console.error('  ' + f));
  process.exit(1);
}
console.log(`PASS tests/js/test_issue_1208_selector_summary.js (${count} assertions)`);
