/*
 * Issue #292: 场景编辑页 Agent 技能下拉分组 + 搜索 — 前端测试
 *
 * 运行：node tests/js/test_issue_292_skill_dropdown.js
 *
 * 验证点：
 *   1. _skillGroupByScope 按 scope 分组且顺序 = 官方技能 → 仓库技能 → 个人技能
 *   2. 空组不返回
 *   3. 未知 scope 归 official 兜底前的行为（default: 'official'）
 *   4. _skillFilterByQuery 不区分大小写、命中名称/显示名/描述
 *   5. 清空关键字恢复完整列表（返回全量副本）
 *   6. 无匹配时分组结果为空（空结果态）
 */

const assert = require('assert');

// ── 与 dashboard.js 一致的实现（提取纯函数）──
const _SKILL_SCOPE_GROUPS = [
  { key: 'official', label: '官方技能' },
  { key: 'repo', label: '仓库技能' },
  { key: 'user', label: '个人技能' }
];

function _skillFilterByQuery(skills, q) {
  var query = (q || '').toLowerCase().trim();
  if (!query) return (skills || []).slice();
  return (skills || []).filter(function(s) {
    var hay = [s.skill_name, s.display_name, s.description].join(' ').toLowerCase();
    return hay.indexOf(query) !== -1;
  });
}

function _skillGroupByScope(skills) {
  var map = {};
  (skills || []).forEach(function(s) {
    var k = s.scope || 'official';
    if (!map[k]) map[k] = [];
    map[k].push(s);
  });
  return _SKILL_SCOPE_GROUPS.filter(function(g) { return map[g.key] && map[g.key].length; })
    .map(function(g) { return { key: g.key, label: g.label, items: map[g.key] }; });
}

// ── 测试数据 ──
function mk(name, display, scope, desc) {
  return { skill_name: name, display_name: display || name, scope: scope, description: desc || '' };
}

const SKILLS = [
  mk('vod', 'VOD 需求收敛', 'official', '声音收集'),
  mk('issue', 'Issue 管理', 'official', 'issue triage'),
  mk('repo-skill-a', '仓库技能A', 'repo', '团队共享'),
  mk('repo-skill-b', '仓库技能B', 'repo', ''),
  mk('my-skill', '我的技能', 'user', '个人上传'),
];

function testGroupOrderAndLabels() {
  const groups = _skillGroupByScope(SKILLS);
  assert.deepStrictEqual(groups.map(g => g.key), ['official', 'repo', 'user']);
  assert.deepStrictEqual(groups.map(g => g.label), ['官方技能', '仓库技能', '个人技能']);
  assert.deepStrictEqual(groups[0].items.map(s => s.skill_name), ['vod', 'issue']);
  assert.deepStrictEqual(groups[1].items.map(s => s.skill_name), ['repo-skill-a', 'repo-skill-b']);
  console.log('  PASS: 按 scope 分组，顺序 官方→仓库→个人，归属正确');
}

function testEmptyGroupOmitted() {
  const groups = _skillGroupByScope([mk('a', '', 'official'), mk('b', '', 'repo')]);
  assert.deepStrictEqual(groups.map(g => g.key), ['official', 'repo']);
  assert.strictEqual(_skillGroupByScope([]).length, 0);
  assert.strictEqual(_skillGroupByScope(null).length, 0);
  console.log('  PASS: 空组不渲染；空列表/空入参返回空');
}

function testUnknownScopeDefaultsOfficial() {
  const groups = _skillGroupByScope([mk('x', '', undefined)]);
  assert.deepStrictEqual(groups.map(g => g.key), ['official']);
  console.log('  PASS: 缺省 scope 归入 official');
}

function testFilterCaseInsensitive() {
  const r = _skillFilterByQuery(SKILLS, 'VOD');
  assert.deepStrictEqual(r.map(s => s.skill_name), ['vod']);
  const r2 = _skillFilterByQuery(SKILLS, 'issue');
  assert.deepStrictEqual(r2.map(s => s.skill_name), ['issue']);
  console.log('  PASS: 名称/显示名过滤不区分大小写');
}

function testFilterByDescription() {
  const r = _skillFilterByQuery(SKILLS, 'triage');
  assert.deepStrictEqual(r.map(s => s.skill_name), ['issue']);
  const r2 = _skillFilterByQuery(SKILLS, '团队');
  assert.deepStrictEqual(r2.map(s => s.skill_name), ['repo-skill-a']);
  console.log('  PASS: 描述关键字过滤');
}

function testFilterEmptyQueryRestoresAll() {
  const r = _skillFilterByQuery(SKILLS, '');
  assert.strictEqual(r.length, SKILLS.length);
  const r2 = _skillFilterByQuery(SKILLS, '   ');
  assert.strictEqual(r2.length, SKILLS.length);
  console.log('  PASS: 清空关键字恢复完整列表');
}

function testNoMatchEmptyState() {
  const filtered = _skillFilterByQuery(SKILLS, '不存在的关键字xyz');
  assert.strictEqual(filtered.length, 0);
  assert.strictEqual(_skillGroupByScope(filtered).length, 0);  // 渲染层显示"无匹配技能"
  console.log('  PASS: 无匹配 → 空结果态');
}

function testFilterThenGroupCombo() {
  const groups = _skillGroupByScope(_skillFilterByQuery(SKILLS, '技能'));
  // 命中：仓库技能A/B（名称）、我的技能（名称）；官方组被滤空不出现
  assert.deepStrictEqual(groups.map(g => g.key), ['repo', 'user']);
  console.log('  PASS: 过滤 + 分组组合，滤空分组不出现');
}

[testGroupOrderAndLabels, testEmptyGroupOmitted, testUnknownScopeDefaultsOfficial,
 testFilterCaseInsensitive, testFilterByDescription, testFilterEmptyQueryRestoresAll,
 testNoMatchEmptyState, testFilterThenGroupCombo].forEach(function(t) {
  t();
});
console.log('\nAll tests passed.');
