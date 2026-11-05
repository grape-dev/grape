/*
 * Issue #697：判重门禁 — 前端回归测试（prompt 侧规则已按运维决定回退）
 *
 * 运行：node tests/frontend/test_issue_697_dedup_template.js
 *
 * 验证点：
 *   T1. issue_patrol 模板指令回到 4 步原文（不再内置根因三要素/分级判定）
 *   T2. i18n 模板描述回到原文（不再声称双维度判重）
 *   T3. findings 渲染仍保留 suspected_duplicate 徽标（派发门禁的可见提示）
 *   T4. zh/en 徽标文案齐备
 *
 * 为什么锁「不含」：prompt 侧规则回退是明确决策，防止后续有人又把判重规则
 * 塞回模板/描述里，导致自动化任务的行为被悄悄改掉。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const _STATIC = path.join(__dirname, '..', '..', 'src', 'api', 'static');
const _js = fs.readFileSync(path.join(_STATIC, 'js', 'pages', 'autopilot.js'), 'utf8');
const _zh = JSON.parse(fs.readFileSync(path.join(_STATIC, 'locales', 'zh', 'autopilot.json'), 'utf8'));
const _en = JSON.parse(fs.readFileSync(path.join(_STATIC, 'locales', 'en', 'autopilot.json'), 'utf8'));

// 提取 issue_patrol 模板对象（与页面同构）
const _m = _js.match(/\{id:'issue_patrol'[^}]*instruction:'((?:[^'\\]|\\.)*)'\}/);
assert.ok(_m, 'issue_patrol 模板存在');
const instruction = _m[1].replace(/\\n/g, '\n').replace(/\\'/g, "'");

let n = 0;
function T(name, fn) { n++; fn(); console.log(`ok ${n} - ${name}`); }

T('T1 模板指令为回退后的 4 步原文，不含判重门禁文案', () => {
  assert.ok(instruction.includes('1. 列出所有打开的 Issue'));
  assert.ok(instruction.includes('4. 汇总巡检报告'));
  for (const kw of ['根因三要素', '疑似重复', '级联关闭', 'exact_duplicate']) {
    assert.ok(!instruction.includes(kw), `模板不应再含: ${kw}`);
  }
});

T('T2 i18n 模板描述回到原文', () => {
  assert.ok(!_zh.tpl_issue_patrol_d.includes('根因'));
  assert.strictEqual(_zh.tpl_issue_patrol_d, '每日检查stale issue，提醒更新或关闭');
  assert.ok(!_en.tpl_issue_patrol_d.toLowerCase().includes('root cause'));
});

T('T3 findings 渲染保留 suspected_duplicate 徽标', () => {
  assert.ok(_js.includes("f.dedup_level === 'suspected_duplicate'"));
});

T('T4 zh/en 徽标文案齐备', () => {
  assert.ok(_zh.findings.suspected_dup_badge && _zh.findings.suspected_dup_badge.includes('疑似重复'));
  assert.ok(_en.findings.suspected_dup_badge && _en.findings.suspected_dup_badge.toLowerCase().includes('suspected'));
});

console.log(`\n${n} passing`);