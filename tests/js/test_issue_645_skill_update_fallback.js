/*
 * Issue #645 回归测试：技能卡片「上传更新」弹窗/toast 的 i18n 防御性加固
 *
 * 运行：node tests/js/test_issue_645_skill_update_fallback.js
 *
 * 验证点：
 *   1. skills.js 中 6 处更新相关 I18N.t 调用均使用第三参 fallback（不再用 ||'兜底'）
 *   2. showSkillUpdate 调用传 display_name || s.name（与卡片标题口径一致）
 *   3. zh/en skills.json 均补齐 6 个更新相关键（action.update_skill/confirm_update + update.title/tip + toast.update_success/failed）
 *   4. i18n t() 语义模拟：键存在时返回当前语言文案（zh/en 切换正确）；键缺失时返回 fallback 而非键名
 *   5. skills.js 无语法错误（node 解析）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const SKILLS_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/skills.js'), 'utf8');
const ZH = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/skills.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/skills.json'), 'utf8'));

// ── 与 i18n.js t(key, vars, fallback) 保持一致的语义提取 ──
// Lookup order: current lang pack → default lang pack → fallback → key itself
function lookup(pack, key) {
  const parts = key.split('.');
  let cur = pack;
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[p];
  }
  return cur;
}
function t(packs, lang, key, vars, fallback) {
  let val = packs[lang] ? lookup(packs[lang], key) : undefined;
  if (val === undefined && lang !== 'zh' && packs.zh) val = lookup(packs.zh, key);
  if (val === undefined || val === null) return fallback !== undefined ? fallback : key;
  return val;
}

// ── 测试 ──

function testSixCallsUseThirdArgFallback() {
  const pairs = [
    ["I18N.t('skills.action.update_skill', null, '上传更新')", "I18N.t('skills.action.update_skill')||"],
    ["I18N.t('skills.update.title', null, '上传更新技能')", "I18N.t('skills.update.title')||"],
    ["I18N.t('skills.update.tip', null, ", "I18N.t('skills.update.tip')||"],
    ["I18N.t('skills.action.confirm_update', null, '确认覆盖更新')", "I18N.t('skills.action.confirm_update')||"],
    ["I18N.t('skills.toast.update_success', null, '更新成功')", "I18N.t('skills.toast.update_success')||"],
    ["I18N.t('skills.toast.update_failed', null, '更新失败')", "I18N.t('skills.toast.update_failed')||"]
  ];
  for (const [good, bad] of pairs) {
    assert.ok(SKILLS_JS.indexOf(good.slice(0, good.indexOf(',', good.indexOf(',') + 1))) >= 0,
      'skills.js 应包含第三参 fallback 调用: ' + good);
    assert.ok(SKILLS_JS.indexOf(bad) < 0, 'skills.js 不应再用 || 兜底: ' + bad);
  }
  console.log('  PASS: 6 处更新相关 I18N.t 均改为第三参 fallback');
}

function testShowSkillUpdateUsesDisplayName() {
  assert.ok(SKILLS_JS.indexOf("showSkillUpdate(${s.id},'${(s.display_name || s.name || '')") >= 0,
    'showSkillUpdate 应传 s.display_name || s.name（与卡片标题口径一致，并转义单引号）');
  console.log('  PASS: showSkillUpdate 传 display_name || s.name');
}

function testLocaleKeysExist() {
  const cases = [
    [ZH, 'action.update_skill', '上传更新'],
    [ZH, 'action.confirm_update', '确认覆盖更新'],
    [ZH, 'update.title', '上传更新技能'],
    [EN, 'action.update_skill', 'Upload Update'],
    [EN, 'action.confirm_update', 'Confirm Overwrite Update'],
    [EN, 'update.title', 'Upload Skill Update']
  ];
  for (const [pack, key] of cases) {
    const v = lookup(pack, key);
    assert.ok(typeof v === 'string' && v.length > 0, '键应存在且非空: ' + key);
  }
  assert.ok(typeof lookup(ZH, 'update.tip') === 'string' && lookup(ZH, 'update.tip').length > 10);
  assert.ok(typeof lookup(EN, 'update.tip') === 'string' && lookup(EN, 'update.tip').length > 10);
  assert.ok(typeof lookup(ZH, 'toast.update_success') === 'string');
  assert.ok(typeof lookup(EN, 'toast.update_success') === 'string');
  assert.ok(typeof lookup(ZH, 'toast.update_failed') === 'string');
  assert.ok(typeof lookup(EN, 'toast.update_failed') === 'string');
  console.log('  PASS: zh/en skills.json 6 个更新相关键齐全');
}

function testLangSwitchAndFallbackSemantics() {
  // i18n.js 的 pack 结构为 { skills: <skills.json 内容>, ... }，lookup 用全限定键
  const packsOf = (zh, en) => ({ zh: { skills: zh }, en: { skills: en } });
  // 键存在：zh/en 切换文案正确
  assert.strictEqual(t(packsOf(ZH, EN), 'zh', 'skills.action.update_skill'), '上传更新');
  assert.strictEqual(t(packsOf(ZH, EN), 'en', 'skills.action.update_skill'), 'Upload Update');
  assert.strictEqual(t(packsOf(ZH, EN), 'zh', 'skills.update.title'), '上传更新技能');
  assert.strictEqual(t(packsOf(ZH, EN), 'en', 'skills.update.title'), 'Upload Skill Update');
  // 模拟键缺失（en 缺、zh 也缺）：返回 fallback 而非键名
  const brokenEn = JSON.parse(JSON.stringify(EN)); delete brokenEn.action.update_skill; delete brokenEn.update; delete brokenEn.toast.update_failed;
  const brokenZh = JSON.parse(JSON.stringify(ZH)); delete brokenZh.action.update_skill; delete brokenZh.update; delete brokenZh.toast.update_failed;
  assert.strictEqual(t(packsOf(brokenZh, brokenEn), 'en', 'skills.action.update_skill', null, '上传更新'), '上传更新');
  assert.strictEqual(t(packsOf(brokenZh, brokenEn), 'zh', 'skills.update.title', null, '上传更新技能'), '上传更新技能');
  assert.strictEqual(t(packsOf(brokenZh, brokenEn), 'en', 'skills.toast.update_failed', null, '更新失败'), '更新失败');
  // 无 fallback 时才退化为键名（旧行为，用于对照）
  assert.strictEqual(t(packsOf(brokenZh, brokenEn), 'zh', 'skills.action.update_skill'), 'skills.action.update_skill');
  console.log('  PASS: zh/en 切换文案正确；键缺失时显示 fallback 而非键名');
}

function testSyntax() {
  new Function(SKILLS_JS.replace(/^#!.*$/m, ''));
  console.log('  PASS: skills.js 语法解析通过');
}

// ── 执行 ──
console.log('Issue #645: skill update i18n fallback');
testSixCallsUseThirdArgFallback();
testShowSkillUpdateUsesDisplayName();
testLocaleKeysExist();
testLangSwitchAndFallbackSemantics();
testSyntax();
console.log('\nAll tests passed.');
