/*
 * Issue #406: 添加场景 VOD skill 选「开发者声音分析（CLI版）」后禁用 SRE，skill 显示名退化为 vod-cli — 前端测试
 *
 * 运行：node tests/js/test_issue_406_skill_display_name.js
 *
 * 验证点：
 *   1. selectAgentSkill 选中时同步写 node.skill_display_name（取自 _skillPickCache）
 *   2. selectAgentSkill 清除选择（skillName=''）时同步清空 skill_display_name
 *   3. selectAgentSkill 缓存未命中时回退为 skillName 本身（不报错、不退化）
 *   4. selectAgentSkill 找不到 node 时安全返回（不抛错）
 *   5. toggleAgentInScene 禁用 agent 时 skill_display_name 与 skill 同清
 *   6. _renderEditFlowCards 的 skillDisplay 优先取 node.skill_display_name（静态断言）
 *   7. dashboard.js 中两处 selectAgentSkill 均写 skill_display_name（静态断言）
 *   8. _renderSceneEditMode editNodes 构建含 skill_display_name（静态断言）
 *   9. 保存 payload 不含 skill_display_name（静态断言，不改保存语义）
 *  10. dashboard.js 无语法错误（node 解析）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const DASHBOARD_JS = path.join(ROOT, 'src/api/static/js/pages/dashboard.js');

// ── 与 dashboard.js selectAgentSkill 保持一致的纯函数提取 ──
// 输入：editNodes 数组、_skillPickCache（agentType -> {skill_name: display_name}）、agentType、skillName
// 副作用：原地修改 editNodes 中匹配 node 的 skill / skill_display_name
function selectAgentSkill(editNodes, _skillPickCache, agentType, skillName) {
  const node = editNodes.find(n => n.id === agentType);
  if (!node) return;
  node.skill = skillName || '';
  var cache = (_skillPickCache || {})[agentType] || {};
  node.skill_display_name = skillName ? (cache[skillName] || skillName) : '';
}

// 与 toggleAgentInScene 保持一致（禁用分支）
function toggleAgentDisable(editNodes, agentType) {
  const node = editNodes.find(n => n.id === agentType);
  if (!node) return;
  node.enabled = false;
  node.skill = '';
  node.skill_display_name = '';
  node.engine_runtime_ids = [];
}

function mkNode(id, skill, displayName, enabled) {
  return { id: id, tag: id, skill: skill || '', skill_display_name: displayName || '', mode: 'emit', next: 'done', handoff: 'a2a', skip: [], enabled: enabled !== false, engine_runtime_ids: [] };
}

// ── 测试 ──

function testSelectWritesDisplayName() {
  const cache = { vod: { 'vod-cli': '开发者声音分析（CLI版）' } };
  const editNodes = ['vod', 'issue', 'coding', 'qa', 'sre'].map(at => mkNode(at));
  selectAgentSkill(editNodes, cache, 'vod', 'vod-cli');
  const vod = editNodes.find(n => n.id === 'vod');
  assert.strictEqual(vod.skill, 'vod-cli', 'node.skill 应为内部 id vod-cli');
  assert.strictEqual(vod.skill_display_name, '开发者声音分析（CLI版）', 'node.skill_display_name 应为缓存中的显示名');
  console.log('  PASS: 选中时同步写 skill_display_name（取自 _skillPickCache）');
}

function testSelectClearAlsoClearsDisplayName() {
  const cache = { vod: { 'vod-cli': '开发者声音分析（CLI版）' } };
  const editNodes = ['vod', 'issue', 'coding', 'qa', 'sre'].map(at => mkNode(at));
  // 先选中
  selectAgentSkill(editNodes, cache, 'vod', 'vod-cli');
  // 再清除（data-name='' 对应「清除选择」行）
  selectAgentSkill(editNodes, cache, 'vod', '');
  const vod = editNodes.find(n => n.id === 'vod');
  assert.strictEqual(vod.skill, '', '清除后 node.skill 应为空');
  assert.strictEqual(vod.skill_display_name, '', '清除后 node.skill_display_name 应同步为空');
  console.log('  PASS: 清除选择时同步清空 skill_display_name');
}

function testCacheMissFallsBackToSkillName() {
  // 下拉未打开过 / 缓存被重建后未命中 → 回退为 skillName 本身，不抛错、不退化为 undefined
  const editNodes = ['vod', 'issue', 'coding', 'qa', 'sre'].map(at => mkNode(at));
  selectAgentSkill(editNodes, {}, 'vod', 'vod-cli');
  const vod = editNodes.find(n => n.id === 'vod');
  assert.strictEqual(vod.skill, 'vod-cli');
  assert.strictEqual(vod.skill_display_name, 'vod-cli', '缓存未命中时 skill_display_name 回退为 skillName 本身');
  console.log('  PASS: 缓存未命中时回退为 skillName（不抛错、不退化）');
}

function testCacheNullSafe() {
  const editNodes = ['vod'].map(at => mkNode(at));
  selectAgentSkill(editNodes, null, 'vod', 'vod-cli');
  const vod = editNodes.find(n => n.id === 'vod');
  assert.strictEqual(vod.skill_display_name, 'vod-cli', '_skillPickCache 为 null 时回退为 skillName');
  console.log('  PASS: _skillPickCache 为 null 时安全回退');
}

function testNodeNotFoundSafe() {
  const editNodes = ['vod'].map(at => mkNode(at));
  // 不存在的 agentType → 不抛错
  assert.doesNotThrow(() => selectAgentSkill(editNodes, {}, 'nonexistent', 'foo'));
  console.log('  PASS: agentType 不匹配时安全返回（不抛错）');
}

// ── 核心回归场景：Issue #406 复现链路模拟 ──
function testReproIssue406_DisplayNameSurvivesToggle() {
  // 模拟创建模式：VOD 选 vod-cli → 禁用 SRE → VOD skill_display_name 不丢失
  const cache = { vod: { 'vod-cli': '开发者声音分析（CLI版）' } };
  const editNodes = ['vod', 'issue', 'coding', 'qa', 'sre'].map(at => mkNode(at));
  // 1. 用户为 VOD 选「开发者声音分析（CLI版）」
  selectAgentSkill(editNodes, cache, 'vod', 'vod-cli');
  const vodAfterSelect = editNodes.find(n => n.id === 'vod');
  assert.strictEqual(vodAfterSelect.skill_display_name, '开发者声音分析（CLI版）');
  // 2. 用户禁用 SRE
  toggleAgentDisable(editNodes, 'sre');
  // 3. 重渲染时 _renderEditFlowCards 读 node.skill_display_name（仍应为显示名，未退化为 vod-cli）
  const vodAfterToggle = editNodes.find(n => n.id === 'vod');
  const skillDisplay = vodAfterToggle.skill_display_name || vodAfterToggle.skill;
  assert.strictEqual(skillDisplay, '开发者声音分析（CLI版）', '禁用 SRE 后 VOD 显示名应保持，不应退化为 vod-cli');
  // 4. 保存 payload 的 skill 仍为内部 id（不改保存语义）
  assert.strictEqual(vodAfterToggle.skill, 'vod-cli', '保存 payload 的 skill 字段仍为内部 id');
  console.log('  PASS: Issue #406 复现链路 — 禁用 SRE 后 VOD 显示名保持「开发者声音分析（CLI版）」');
}

function testToggleClearsDisplayNameOnDisable() {
  const editNodes = ['sre'].map(at => mkNode(at, 'sre-skill-x', 'SRE 技能 X'));
  toggleAgentDisable(editNodes, 'sre');
  const sre = editNodes.find(n => n.id === 'sre');
  assert.strictEqual(sre.enabled, false);
  assert.strictEqual(sre.skill, '', '禁用后 skill 清空');
  assert.strictEqual(sre.skill_display_name, '', '禁用后 skill_display_name 同步清空');
  assert.deepStrictEqual(sre.engine_runtime_ids, [], '禁用后 engine_runtime_ids 清空');
  console.log('  PASS: toggleAgentInScene 禁用时 skill_display_name 与 skill 同清');
}

// ── 任意 skill 在启用/禁用其他角色后显示名不退化（验收第 2 条） ──
function testOtherAgentsToggleDontDegradeVOD() {
  const cache = {
    vod: { 'vod-cli': '开发者声音分析（CLI版）' },
    coding: { 'coding-default': '代码实现' },
    qa: { 'qa-review': '代码审查' }
  };
  const editNodes = ['vod', 'issue', 'coding', 'qa', 'sre'].map(at => mkNode(at));
  selectAgentSkill(editNodes, cache, 'vod', 'vod-cli');
  selectAgentSkill(editNodes, cache, 'coding', 'coding-default');
  selectAgentSkill(editNodes, cache, 'qa', 'qa-review');
  // 轮流禁用/启用其他角色
  ['coding', 'qa', 'sre'].forEach(at => {
    toggleAgentDisable(editNodes, at);
    // 重新启用
    const node = editNodes.find(n => n.id === at);
    node.enabled = true;
  });
  const vod = editNodes.find(n => n.id === 'vod');
  assert.strictEqual(vod.skill_display_name, '开发者声音分析（CLI版）', '其他角色开关后 VOD 显示名不退化');
  assert.strictEqual(vod.skill, 'vod-cli', '其他角色开关后 VOD skill 仍为内部 id');
  console.log('  PASS: 任意 skill 在启用/禁用其他角色后显示名不退化为内部 id');
}

// ── 源码静态断言 ──
function readSrc() { return fs.readFileSync(DASHBOARD_JS, 'utf8'); }

function testRenderEditFlowCardsPrioritizesDisplayName() {
  const src = readSrc();
  // _renderEditFlowCards 中 skillDisplay 必须优先取 node.skill_display_name
  assert.ok(/node\.skill_display_name \|\| node\.skill/.test(src), '_renderEditFlowCards 的 skillDisplay 应优先取 node.skill_display_name');
  console.log('  PASS: _renderEditFlowCards 优先取 node.skill_display_name');
}

function testBothSelectAgentSkillImplsWriteDisplayName() {
  const src = readSrc();
  // 两处 selectAgentSkill 实现都应写 node.skill_display_name
  const matches = src.match(/node\.skill_display_name = skillName \? \(cache\[skillName\] \|\| skillName\) : ''/g) || [];
  assert.strictEqual(matches.length, 2, 'selectAgentSkill 应有两处实现均写 node.skill_display_name（编辑模式 + 创建模式）');
  console.log('  PASS: 两处 selectAgentSkill 实现均写 skill_display_name（编辑 + 创建模式）');
}

function testBothToggleAgentInSceneClearDisplayName() {
  const src = readSrc();
  // 两处 toggleAgentInScene 禁用分支都应清 skill_display_name
  const matches = src.match(/node\.skill = ''; node\.skill_display_name = ''; node\.engine_runtime_ids = \[\];/g) || [];
  assert.strictEqual(matches.length, 2, 'toggleAgentInScene 应有两处实现均清 skill_display_name（编辑模式 + 创建模式）');
  console.log('  PASS: 两处 toggleAgentInScene 实现均清 skill_display_name（编辑 + 创建模式）');
}

function testRenderSceneEditModeInheritsDisplayName() {
  const src = readSrc();
  // _renderSceneEditMode editNodes 构建应继承 skill_display_name
  assert.ok(/skill_display_name: node\.skill_display_name \|\| ag\.skill_display_name \|\| ''/.test(src), '_renderSceneEditMode editNodes 应继承 skill_display_name');
  console.log('  PASS: _renderSceneEditMode editNodes 构建继承 skill_display_name');
}

function testCreateModeEditNodesInitHasDisplayName() {
  const src = readSrc();
  // 创建模式初始 editNodes 应含 skill_display_name: ''
  assert.ok(/skill: '', skill_display_name: '', mode: 'emit'/.test(src), '创建模式初始 editNodes 应含 skill_display_name: ""');
  console.log('  PASS: 创建模式初始 editNodes 含 skill_display_name: ""');
}

function testSavePayloadDoesNotIncludeDisplayName() {
  const src = readSrc();
  // 保存 payload（nodesPayload）显式列举字段，不含 skill_display_name
  // 匹配 "return { id: n.id, tag: n.tag, skill: n.skill || '', ..." 这类显式字段列表
  const payloadPattern = /\{\s*id:\s*n\.id,\s*tag:\s*n\.tag,\s*skill:\s*n\.skill\s*\|\|\s*''[^}]*\}/g;
  const matches = src.match(payloadPattern) || [];
  assert.ok(matches.length >= 2, '应至少有 2 处 nodesPayload（编辑保存 + 创建保存）');
  matches.forEach((m, i) => {
    assert.ok(!/skill_display_name/.test(m), `第 ${i + 1} 处 nodesPayload 不应含 skill_display_name（不改保存语义）: ${m}`);
  });
  console.log('  PASS: 保存 payload 不含 skill_display_name（保存语义不变，skill 仍为内部 id）');
}

function testNoSyntaxErrors() {
  const src = readSrc();
  // 用 new Function 解析整份 dashboard.js（不执行），仅校验语法
  new Function(src);
  console.log('  PASS: dashboard.js 无语法错误（node Function 解析）');
}

// ── 运行 ──
console.log('Issue #406: VOD skill display name regression tests\n');

let passed = 0, failed = 0;
const tests = [
  testSelectWritesDisplayName,
  testSelectClearAlsoClearsDisplayName,
  testCacheMissFallsBackToSkillName,
  testCacheNullSafe,
  testNodeNotFoundSafe,
  testReproIssue406_DisplayNameSurvivesToggle,
  testToggleClearsDisplayNameOnDisable,
  testOtherAgentsToggleDontDegradeVOD,
  testRenderEditFlowCardsPrioritizesDisplayName,
  testBothSelectAgentSkillImplsWriteDisplayName,
  testBothToggleAgentInSceneClearDisplayName,
  testRenderSceneEditModeInheritsDisplayName,
  testCreateModeEditNodesInitHasDisplayName,
  testSavePayloadDoesNotIncludeDisplayName,
  testNoSyntaxErrors,
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
