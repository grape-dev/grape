/*
 * Issue #335: runtime 详情页 skills 列表搜索过滤 — 前端测试
 *
 * 运行：node tests/js/test_issue_335_skill_search.js
 *
 * 验证点：
 *   1. 关键词过滤（名称匹配、描述匹配、不区分大小写）
 *   2. 清空关键词恢复完整列表
 *   3. 无匹配时空态提示（skill.no_match ≠ empty.no_local_skill）
 *   4. 刷新技能后保持搜索态重渲染（缓存写入 + 按当前 q 过滤）
 *   5. i18n zh/en key 完整且占位符一致
 *   6. runtimes.js 模板含搜索框（id=engineSkillSearch）且绑定 oninput
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const RUNTIMES_JS = path.join(ROOT, 'src/api/static/js/pages/runtimes.js');
const ZH = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/runtimes.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/runtimes.json'), 'utf8'));

// ── 与 runtimes.js _renderEngineSkillItems 保持一致的逻辑提取 ──
// 守卫：无技能 → 全量空态；过滤：name + description 不区分大小写 includes
function filterSkills(skills, details, q) {
  if (!skills || !skills.length) return [];
  const query = String(q || '').trim().toLowerCase();
  if (!query) return skills.slice();
  return skills.filter(s => {
    const detail = (details || []).find(d => d.name === s);
    const desc = detail ? (detail.description || '') : '';
    return (s + ' ' + desc).toLowerCase().includes(query);
  });
}

function mkSkills() {
  return ['coding', 'qa-review', 'DevOps-Deploy', 'docs-writer'];
}
function mkDetails() {
  return [
    { name: 'coding', description: 'Write and review code' },
    { name: 'qa-review', description: '代码审查与测试' },
    { name: 'DevOps-Deploy', description: '部署与发布' },
    { name: 'docs-writer', description: 'Write documentation' },
  ];
}

// ── 测试 ──

function testFilterByName() {
  const filtered = filterSkills(mkSkills(), mkDetails(), 'coding');
  assert.strictEqual(filtered.length, 1, 'name match should filter to 1');
  assert.strictEqual(filtered[0], 'coding');
  console.log('  PASS: filter by skill name');
}

function testFilterByDescription() {
  const filtered = filterSkills(mkSkills(), mkDetails(), 'documentation');
  assert.strictEqual(filtered.length, 1, 'description match should filter to 1');
  assert.strictEqual(filtered[0], 'docs-writer');
  console.log('  PASS: filter by description');
}

function testFilterCaseInsensitive() {
  const lower = filterSkills(mkSkills(), mkDetails(), 'devops');
  const upper = filterSkills(mkSkills(), mkDetails(), 'DEVOPS');
  assert.strictEqual(lower.length, 1);
  assert.strictEqual(upper.length, 1, 'search should be case-insensitive');
  assert.strictEqual(lower[0], 'DevOps-Deploy');
  assert.strictEqual(upper[0], 'DevOps-Deploy');
  console.log('  PASS: filter is case-insensitive');
}

function testFilterChineseDescription() {
  const filtered = filterSkills(mkSkills(), mkDetails(), '部署');
  assert.strictEqual(filtered.length, 1);
  assert.strictEqual(filtered[0], 'DevOps-Deploy');
  console.log('  PASS: filter by Chinese description');
}

function testEmptyQueryRestoresFullList() {
  const full = filterSkills(mkSkills(), mkDetails(), '');
  assert.strictEqual(full.length, 4, 'empty query should restore full list');
  const whitespace = filterSkills(mkSkills(), mkDetails(), '   ');
  assert.strictEqual(whitespace.length, 4, 'whitespace-only query should restore full list');
  console.log('  PASS: clearing query restores full list');
}

function testNoMatchReturnsEmpty() {
  const filtered = filterSkills(mkSkills(), mkDetails(), 'nonexistent-xyz');
  assert.strictEqual(filtered.length, 0, 'no-match query should return empty (for empty-state render)');
  console.log('  PASS: no-match query returns empty list');
}

function testNullSafety() {
  const filtered = filterSkills(null, null, 'x');
  assert.strictEqual(filtered.length, 0, 'null skills should not throw');
  const full = filterSkills(null, null, '');
  assert.strictEqual(full.length, 0);
  const noDetails = filterSkills(['a', 'b'], undefined, 'a');
  assert.strictEqual(noDetails.length, 1, 'missing details should fall back to name-only match');
  console.log('  PASS: null/undefined inputs handled');
}

// ── 源码静态断言 ──
function readSrc() { return fs.readFileSync(RUNTIMES_JS, 'utf8'); }

function testTemplateHasSearchInput() {
  const src = readSrc();
  assert.ok(src.includes('id="engineSkillSearch"'), 'template must contain engineSkillSearch input');
  assert.ok(/engineSkillSearch[^>]*oninput="_filterEngineSkills/.test(src), 'search input must bind oninput to _filterEngineSkills');
  console.log('  PASS: detail modal template contains search input with oninput binding');
}

function testTemplateEmptyStateUsesNoMatchKey() {
  const src = readSrc();
  assert.ok(src.includes("runtimes.skill.no_match"), 'filtered-empty state must use runtimes.skill.no_match');
  assert.ok(src.includes("runtimes.empty.no_local_skill"), 'no-skills state must keep runtimes.empty.no_local_skill');
  console.log('  PASS: two distinct empty states (no_local_skill vs no_match)');
}

function testRefreshKeepsQuery() {
  const src = readSrc();
  // _pollSkillScan 成功分支应写缓存并按当前关键词重渲染（_renderEngineSkillItems 内部读取 _engineSkillQuery）
  assert.ok(/_engineSkillCache\[runtimeId\]\s*=/.test(src), 'refresh success should update _engineSkillCache');
  const pollSuccess = src.slice(src.indexOf('function _pollSkillScan'), src.indexOf('function openSkillDetail'));
  assert.ok(pollSuccess.includes('_renderEngineSkillItems'), 'refresh success should re-render via _renderEngineSkillItems');
  assert.ok(/function _renderEngineSkillItems[\s\S]*?_engineSkillQuery/.test(src), '_renderEngineSkillItems must apply current _engineSkillQuery');
  console.log('  PASS: refresh-skills keeps search query and re-filters');
}

function testI18nKeysPresent() {
  const zhSkill = ZH.skill || {};
  const enSkill = EN.skill || {};
  for (const k of ['search_placeholder', 'no_match', 'match_count_paren']) {
    assert.ok(typeof zhSkill[k] === 'string' && zhSkill[k].length > 0, `zh runtimes.json must have skill.${k}`);
    assert.ok(typeof enSkill[k] === 'string' && enSkill[k].length > 0, `en runtimes.json must have skill.${k}`);
  }
  console.log('  PASS: i18n keys present in zh/en (search_placeholder, no_match, match_count_paren)');
}

function testI18nPlaceholderParity() {
  // match_count_paren 带 {count} 占位符，zh/en 必须一致
  const count = (s) => (String(s).match(/\{count\}/g) || []).length;
  assert.strictEqual(count(ZH.skill.match_count_paren), 1, 'zh match_count_paren must contain {count}');
  assert.strictEqual(count(EN.skill.match_count_paren), 1, 'en match_count_paren must contain {count}');
  console.log('  PASS: {count} placeholder parity between zh/en');
}

// ── 运行 ──
console.log('Issue #335: runtime detail skill search tests\n');

let passed = 0, failed = 0;
const tests = [
  testFilterByName,
  testFilterByDescription,
  testFilterCaseInsensitive,
  testFilterChineseDescription,
  testEmptyQueryRestoresFullList,
  testNoMatchReturnsEmpty,
  testNullSafety,
  testTemplateHasSearchInput,
  testTemplateEmptyStateUsesNoMatchKey,
  testRefreshKeepsQuery,
  testI18nKeysPresent,
  testI18nPlaceholderParity,
];

for (const t of tests) {
  try {
    t();
    passed++;
  } catch (e) {
    console.log('  FAIL:', e.message);
    failed++;
  }
}

console.log('\n' + passed + ' passed, ' + failed + ' failed');
if (failed > 0) process.exit(1);
