/*
 * Issue #697：巡检重复判定双维度 — 前端模板回归测试
 *
 * 运行：node tests/frontend/test_issue_697_dedup_template.js
 *
 * 验证点：
 *   T1. issue_patrol 模板 instruction 含根因三要素（报错信息/根因描述/涉及命令与参数）
 *   T2. 模板含分级输出（完全重复/疑似重复/不重复）与 owner 确认要求
 *   T3. 模板含级联关闭守护（疑似重复不自动触发 resolve 级联）
 *   T4. findings 渲染含 suspected_duplicate 徽标（zh/en 文案齐备）
 *   T5. i18n zh/en 模板描述同步双维度判重语义
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

T('T1 模板指令含根因三要素', () => {
  for (const kw of ['报错信息', '根因描述', '涉及命令与参数']) {
    assert.ok(instruction.includes(kw), `缺少关键词: ${kw}`);
  }
});

T('T2 模板指令含分级输出与 owner 确认', () => {
  assert.ok(instruction.includes('完全重复'));
  assert.ok(instruction.includes('疑似重复'));
  assert.ok(instruction.includes('owner agent 确认'));
  assert.ok(/不重复|分级结论/.test(instruction));
});

T('T3 模板指令含级联关闭守护', () => {
  assert.ok(instruction.includes('级联关闭'));
  assert.ok(instruction.includes('已确认'));
});

T('T4 findings 渲染含 suspected_duplicate 徽标且 zh/en 文案齐备', () => {
  assert.ok(_js.includes("f.dedup_level === 'suspected_duplicate'"));
  assert.ok(_zh.findings.suspected_dup_badge && _zh.findings.suspected_dup_badge.includes('疑似重复'));
  assert.ok(_en.findings.suspected_dup_badge && _en.findings.suspected_dup_badge.toLowerCase().includes('suspected'));
});

T('T5 i18n 模板描述同步双维度语义', () => {
  assert.ok(_zh.tpl_issue_patrol_d.includes('根因'));
  assert.ok(_en.tpl_issue_patrol_d.toLowerCase().includes('root cause'));
});

console.log(`\n${n} passing`);
