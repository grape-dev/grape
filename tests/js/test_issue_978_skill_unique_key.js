/*
 * Issue #978: 场景编辑技能下拉三档同名技能无法区分 — 前端回归测试
 *
 * 运行：node tests/js/test_issue_978_skill_unique_key.js
 *
 * 验证点：
 *   1. _skillKeyOf：skill_id 优先（'id:<skill_id>'），缺失时回退 (scope, skill_name) 复合键
 *   2. 同名三档（official/repo/user）生成互不相同的唯一键
 *   3. 选中高亮逻辑：唯一键精确命中仅勾选一行；旧裸 skill_name 值仅在该名字唯一时回退命中
 *   4. 缓存结构：_skillPickCache 以唯一键索引 {name, display}，同名不互相覆盖
 *   5. 行元素携带 data-key（选中回写 data-current 为唯一键）
 *   6. 后端契约注释（scenes.py）与接口文档（help/api/06-scenes.md）固化唯一键
 *   7. 保存 payload 仍只提交 skill（skill_name），不改保存语义
 *   8. dashboard.js 无语法错误
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const DASHBOARD_JS = path.join(ROOT, 'src/api/static/js/pages/dashboard.js');
const SCENES_PY = path.join(ROOT, 'src/api/routes/scenes.py');
const SCENES_MD = path.join(ROOT, 'help/api/06-scenes.md');

// ── 从 dashboard.js 提取真实 _skillKeyOf 实现并求值（测试真实代码而非副本）──
const src = fs.readFileSync(DASHBOARD_JS, 'utf8');
const m = src.match(/function _skillKeyOf\(s\) \{[\s\S]*?\n\}/);
assert.ok(m, 'dashboard.js 应定义 _skillKeyOf 函数');
const _skillKeyOf = new Function('return ' + m[0])();

function mkSkill(skill_id, skill_name, scope, display_name) {
  return { skill_id: skill_id, skill_name: skill_name, display_name: display_name || skill_name, scope: scope, owner_id: '', description: '' };
}

// ── 测试 ──

function testSkillIdPreferred() {
  const s = mkSkill(42, 'same-name', 'user', '个人版');
  assert.strictEqual(_skillKeyOf(s), 'id:42', '有 skill_id 时唯一键应为 id:<skill_id>');
  console.log('  PASS: skill_id 优先，键格式 id:<skill_id>');
}

function testFallbackScopeName() {
  const s = { skill_name: 'same-name', scope: 'repo' };
  assert.strictEqual(_skillKeyOf(s), 'repo:same-name', 'skill_id 缺失时回退 (scope, skill_name)');
  const noScope = { skill_name: 'x' };
  assert.strictEqual(_skillKeyOf(noScope), 'official:x', 'scope 缺失按 official 兜底');
  assert.strictEqual(_skillKeyOf(null), '', 'null 安全返回空串');
  console.log('  PASS: skill_id 缺失回退 (scope, skill_name) 复合键');
}

function testSameNameThreeScopesDistinctKeys() {
  const official = mkSkill(1, 'same-name', 'official');
  const repo = mkSkill(2, 'same-name', 'repo');
  const user = mkSkill(3, 'same-name', 'user');
  const keys = [_skillKeyOf(official), _skillKeyOf(repo), _skillKeyOf(user)];
  assert.strictEqual(new Set(keys).size, 3, '同名三档唯一键应互不相同');
  console.log('  PASS: 同名三档（official/repo/user）唯一键互不相同');
}

// ── 选中高亮逻辑（与 renderList 中 sel 判定保持一致的纯逻辑复刻）──
function isSel(s, current, nameCount) {
  return (_skillKeyOf(s) === current) || (!!current && current === s.skill_name && nameCount[s.skill_name] === 1);
}

function testHighlightOnlyOneRow() {
  const skills = [mkSkill(1, 'same-name', 'official'), mkSkill(2, 'same-name', 'repo'), mkSkill(3, 'same-name', 'user')];
  const nameCount = {};
  skills.forEach(s => { nameCount[s.skill_name] = (nameCount[s.skill_name] || 0) + 1; });
  // 用户选「个人技能」行 → data-current = 'id:3'
  const current = 'id:3';
  const selRows = skills.filter(s => isSel(s, current, nameCount));
  assert.strictEqual(selRows.length, 1, '唯一键命中应仅勾选一行');
  assert.strictEqual(selRows[0].scope, 'user', '命中的应是个人技能行');
  console.log('  PASS: 选中唯一键 → 仅该行勾选，同名其他档不勾选');
}

function testLegacyNameValueNoMultiSelect() {
  const skills = [mkSkill(1, 'same-name', 'official'), mkSkill(2, 'same-name', 'repo'), mkSkill(3, 'same-name', 'user')];
  const nameCount = {};
  skills.forEach(s => { nameCount[s.skill_name] = (nameCount[s.skill_name] || 0) + 1; });
  // 旧值（初始渲染写入的裸 skill_name）在同名多行时不得命中任何行
  const selRows = skills.filter(s => isSel(s, 'same-name', nameCount));
  assert.strictEqual(selRows.length, 0, '同名多行时旧裸 skill_name 不得同时勾选多行');
  // 名字唯一时旧值仍可命中（向后兼容）
  const single = [mkSkill(9, 'unique-name', 'official')];
  assert.ok(isSel(single[0], 'unique-name', { 'unique-name': 1 }), '名字唯一时旧值回退命中');
  console.log('  PASS: 旧裸 skill_name 值仅在名字唯一时命中，同名多行不再全勾选');
}

function testCacheStructureUniqueKeyIndexed() {
  // 源码断言：缓存以 _skillKeyOf 唯一键索引，value 为 {name, display}
  assert.ok(/cache\[_skillKeyOf\(s\)\] = \{ name: s\.skill_name, display: s\.display_name \|\| s\.skill_name \}/.test(src),
    '_openSkillDropdown 缓存应以唯一键索引 {name, display}');
  console.log('  PASS: _skillPickCache 以唯一键索引 {name, display}，同名不互相覆盖');
}

function testRowCarriesDataKey() {
  assert.ok(/data-key="' \+ _skillEsc\(_skillKeyOf\(s\)\) \+ '"/.test(src), '下拉行元素应携带 data-key（唯一键）');
  assert.ok(/btn\.setAttribute\('data-current', key \|\| nm\);/.test(src), '选中后 data-current 应回写唯一键');
  console.log('  PASS: 行元素携带 data-key，选中回写 data-current 为唯一键');
}

function testSelectAgentSkillAcceptsKey() {
  const matches = src.match(/window\.selectAgentSkill = function\(agentType, skillName, skillKey\)/g) || [];
  assert.strictEqual(matches.length, 2, '两处 selectAgentSkill（编辑+创建）均应接受 skillKey 参数');
  console.log('  PASS: 两处 selectAgentSkill 实现均接受唯一键参数');
}

function testEchoFlattenUsesNameField() {
  assert.ok(/if \(_c\[k\] && _c\[k\]\.name\) skillDisplayMap\[_c\[k\]\.name\] = _c\[k\]\.display;/.test(src),
    '保存后回显展平映射应读缓存条目的 name/display 字段');
  console.log('  PASS: 回显展平映射适配新缓存结构（name → display）');
}

function testBackendContractComment() {
  const py = fs.readFileSync(SCENES_PY, 'utf8');
  assert.ok(py.includes('(skill_id, scope, skill_name)'), 'scenes.py 应固化 (skill_id, scope, skill_name) 唯一键契约注释');
  assert.ok(/skill_name alone MUST NOT be used as a selection\/cache key/.test(py),
    'OpenAPI description 应声明 skill_name 不得单独作选中/缓存 key');
  console.log('  PASS: 后端契约（scenes.py + OpenAPI description）固化唯一键');
}

function testHelpDocContract() {
  const md = fs.readFileSync(SCENES_MD, 'utf8');
  assert.ok(md.includes('`skill_id / skill_name / display_name / scope / owner_id`'), 'help/api/06-scenes.md 应记录技能字段');
  assert.ok(md.includes('`(skill_id, scope, skill_name)` 共同构成前端选择的唯一键'), '接口文档应声明唯一键契约');
  console.log('  PASS: 接口文档（help/api/06-scenes.md）固化唯一键契约');
}

function testSavePayloadUnchanged() {
  // 保存 payload 显式列举字段，skill 仍为 skill_name（不改保存语义）
  const payloadPattern = /\{\s*id:\s*n\.id,\s*tag:\s*n\.tag,\s*skill:\s*n\.skill\s*\|\|\s*''[^}]*\}/g;
  const matches = src.match(payloadPattern) || [];
  assert.ok(matches.length >= 2, '应至少有 2 处 nodesPayload');
  matches.forEach((p, i) => {
    assert.ok(!/skill_key|skill_id/.test(p), `第 ${i + 1} 处 nodesPayload 不应含唯一键字段（不改保存语义）: ${p}`);
  });
  console.log('  PASS: 保存 payload 仍只提交 skill（skill_name），不改保存语义');
}

function testNoSyntaxErrors() {
  new Function(src);
  console.log('  PASS: dashboard.js 无语法错误');
}

// ── 运行 ──
console.log('Issue #978: 技能下拉三档同名技能唯一键回归测试\n');

let passed = 0, failed = 0;
const tests = [
  testSkillIdPreferred,
  testFallbackScopeName,
  testSameNameThreeScopesDistinctKeys,
  testHighlightOnlyOneRow,
  testLegacyNameValueNoMultiSelect,
  testCacheStructureUniqueKeyIndexed,
  testRowCarriesDataKey,
  testSelectAgentSkillAcceptsKey,
  testEchoFlattenUsesNameField,
  testBackendContractComment,
  testHelpDocContract,
  testSavePayloadUnchanged,
  testNoSyntaxErrors,
];

for (const t of tests) {
  try { t(); passed++; } catch (e) { console.log('  FAIL:', e.message); failed++; }
}
console.log('\n' + passed + ' passed, ' + failed + ' failed');
if (failed > 0) process.exit(1);
