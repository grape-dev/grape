/*
 * Issue #1564: 场景保存失败按技能 scope 冲突 violations[] 标红 — 前端回归测试
 *
 * 运行：node tests/js/test_issue_1564_scene_scope_violation.js
 *
 * 覆盖（QA 审查 R4）：
 *   A. _indexScopeViolations 建索引：node_id 优先、无 node_id 回退 node_tag、
 *      跳过无键条目、空输入返回空对象
 *   B. _scopeViolationHint 文案：命中 i18n key 用翻译；未命中用第三参 fallback，
 *      不再渲染原始 key（i18n.js:t() 未命中返回 key 本身，`||` 回退不可达）
 *   C. _clearNodeScopeViolation 清红：删除索引项、清除卡片边框/阴影、移除提示节点
 *   D. 保存 400 → 重渲染标红分支（编辑/创建两处静态断言）
 *   E. 卡片渲染：data-node-id、红框样式、violation 提示节点
 *   F. i18n：zh/en dashboard.json 均定义 skill_scope_mismatch
 *   G. dashboard.js 无语法错误
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const DASHBOARD_JS = path.join(ROOT, 'src/api/static/js/pages/dashboard.js');
const ZH_DASH = path.join(ROOT, 'src/api/static/locales/zh/dashboard.json');
const EN_DASH = path.join(ROOT, 'src/api/static/locales/en/dashboard.json');

function readDashboardSrc() { return fs.readFileSync(DASHBOARD_JS, 'utf8'); }

// ── 源码切片求值：抽取 #1564 新增的三个纯函数，不依赖浏览器 ──
function loadViolationUtils(win, doc, I18N, skillEsc) {
  const src = readDashboardSrc();
  const START = 'function _indexScopeViolations';
  const END = 'function _renderEditFlowCards';
  const i0 = src.indexOf(START);
  const i1 = src.indexOf(END, i0);
  assert.ok(i0 >= 0 && i1 > i0, 'dashboard.js 应包含 _indexScopeViolations 区块');
  const slice = src.slice(i0, i1);
  const fn = new Function('window', 'document', 'I18N', '_skillEsc',
    slice + '\nreturn { _indexScopeViolations: _indexScopeViolations, ' +
    '_scopeViolationHint: _scopeViolationHint, ' +
    '_clearNodeScopeViolation: _clearNodeScopeViolation };');
  return fn(win, doc, I18N, skillEsc);
}

// 与 i18n.js:t() 一致的语义：命中返回翻译，未命中返回 fallback（有则）或 key 本身
function makeI18N(translated) {
  return {
    t: function (key, vars, fallback) {
      if (translated && Object.prototype.hasOwnProperty.call(translated, key)) {
        return translated[key];
      }
      return fallback !== undefined ? fallback : key;
    },
  };
}

function identityEsc(s) { return String(s == null ? '' : s); }

function makeDoc(card) {
  return { _card: card || null, querySelector: function () { return this._card; } };
}

function makeCard() {
  const hint = { removed: false, remove: function () { this.removed = true; } };
  return {
    style: { border: '1px solid red', boxShadow: '0 0 0 3px red' },
    _hint: hint,
    querySelector: function (sel) {
      return sel === '.ap-nd-violation' ? this._hint : null;
    },
  };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch (e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

console.log('Issue #1564: 场景技能 scope 冲突前端标红 — 回归测试\n');

// ═══ A. _indexScopeViolations ═══

test('A1. node_id 优先：node_id 与 node_tag 不同时以 node_id 为键', () => {
  const u = loadViolationUtils({}, makeDoc(), makeI18N({}), identityEsc);
  const v = { node_id: 'coding-node', node_tag: 'coding', skill: 's' };
  const map = u._indexScopeViolations([v]);
  assert.ok(map['coding-node'], '应以 node_id 建索引');
  assert.strictEqual(map['coding'], undefined, '不应再以 node_tag 建索引');
  assert.strictEqual(map['coding-node'], v, '索引应指向原始 violation 对象');
});

test('A2. 无 node_id 时回退 node_tag 建索引', () => {
  const u = loadViolationUtils({}, makeDoc(), makeI18N({}), identityEsc);
  const v = { node_tag: 'qa-tag', skill: 's' };
  const map = u._indexScopeViolations([v]);
  assert.strictEqual(map['qa-tag'], v, '回退应以 node_tag 为键');
});

test('A3. 无 node_id/node_tag 的条目被跳过', () => {
  const u = loadViolationUtils({}, makeDoc(), makeI18N({}), identityEsc);
  const map = u._indexScopeViolations([{ skill: 's' }, null, {}]);
  assert.deepStrictEqual(Object.keys(map), [], '无键条目不应进入索引');
});

test('A4. 空/undefined 输入返回空对象', () => {
  const u = loadViolationUtils({}, makeDoc(), makeI18N({}), identityEsc);
  assert.deepStrictEqual(u._indexScopeViolations(undefined), {});
  assert.deepStrictEqual(u._indexScopeViolations([]), {});
});

// ═══ B. _scopeViolationHint ═══

test('B1. 命中 i18n key 时使用翻译，并带技能/actual/expected', () => {
  const u = loadViolationUtils({}, makeDoc(),
    makeI18N({ 'dashboard.skill_scope_mismatch': 'Skill scope mismatch' }), identityEsc);
  const hint = u._scopeViolationHint(
    { skill: 'my-skill', actual_scope: 'user', expected_scope: 'repo' });
  assert.ok(hint.indexOf('Skill scope mismatch') !== -1, '应使用翻译文案');
  assert.ok(hint.indexOf('my-skill') !== -1 && hint.indexOf('user') !== -1 &&
    hint.indexOf('repo') !== -1, '应包含技能与双方 scope');
});

test('B2. 未命中 i18n key 时用第三参 fallback，不渲染原始 key', () => {
  const u = loadViolationUtils({}, makeDoc(), makeI18N({}), identityEsc);
  const hint = u._scopeViolationHint({ skill: 's', actual_scope: 'user' });
  assert.ok(hint.indexOf('dashboard.skill_scope_mismatch') === -1,
    '不得渲染原始 i18n key（i18n.js:t() 未命中返回 key）');
  assert.ok(hint.indexOf('技能 scope 不匹配') !== -1, '应回退到中文兜底文案');
});

test('B3. 空 violation 返回空串', () => {
  const u = loadViolationUtils({}, makeDoc(), makeI18N({}), identityEsc);
  assert.strictEqual(u._scopeViolationHint(null), '');
});

// ═══ C. _clearNodeScopeViolation ═══

test('C1. 清红：删除索引项、清边框/阴影、移除提示节点', () => {
  const win = { _sceneNodeViolations: { coding: { skill: 's' }, qa: { skill: 'q' } } };
  const card = makeCard();
  const u = loadViolationUtils(win, makeDoc(card), makeI18N({}), identityEsc);
  u._clearNodeScopeViolation('coding');
  assert.strictEqual('coding' in win._sceneNodeViolations, false, '应删除该节点索引项');
  assert.ok('qa' in win._sceneNodeViolations, '不应误删其他节点');
  assert.strictEqual(card.style.border, '', '应清除边框');
  assert.strictEqual(card.style.boxShadow, '', '应清除外阴影');
  assert.strictEqual(card._hint.removed, true, '应移除 violation 提示节点');
});

test('C2. 无对应卡片时不抛错，仍清理索引项', () => {
  const win = { _sceneNodeViolations: { coding: {} } };
  const u = loadViolationUtils(win, makeDoc(null), makeI18N({}), identityEsc);
  assert.doesNotThrow(() => u._clearNodeScopeViolation('coding'));
  assert.strictEqual('coding' in win._sceneNodeViolations, false);
});

test('C3. window 无 _sceneNodeViolations 时不抛错', () => {
  const u = loadViolationUtils({}, makeDoc(null), makeI18N({}), identityEsc);
  assert.doesNotThrow(() => u._clearNodeScopeViolation('coding'));
});

// ═══ D. 保存 400 → 重渲染标红分支（静态断言） ═══

test('D1. 编辑/创建两处保存 400 均读取 violations 并建立索引', () => {
  const src = readDashboardSrc();
  const m = src.match(/var _viols = \(e && e\.detail && e\.detail\.violations\) \|\| \[\];/g) || [];
  assert.strictEqual(m.length, 2, '编辑/创建两处 catch 应各有一处 violations 读取，实际 ' + m.length);
  const idx = src.match(/window\._sceneNodeViolations = _indexScopeViolations\(_viols\);/g) || [];
  assert.strictEqual(idx.length, 2, '两处 400 分支均应建立节点索引，实际 ' + idx.length);
});

test('D2. 400 分支按 violations 重渲染卡片并提前返回（不落到通用失败 toast）', () => {
  const src = readDashboardSrc();
  const blocks = src.match(/if \(_viols\.length\) \{[\s\S]*?return;\s*\}/g) || [];
  assert.strictEqual(blocks.length, 2, '应有两处 _viols.length 标红分支，实际 ' + blocks.length);
  blocks.forEach((b, i) => {
    assert.ok(/_renderEditFlowCards\(/.test(b), '分支 ' + (i + 1) + ' 应重渲染流程卡片');
    assert.ok(/skill_scope_mismatch/.test(b), '分支 ' + (i + 1) + ' 应展示 scope 不匹配 toast');
  });
});

test('D3. 保存成功后清空标红索引（edit/create 各自路径）', () => {
  const src = readDashboardSrc();
  const m = src.match(/window\._sceneNodeViolations = \{\};/g) || [];
  assert.ok(m.length >= 3, '编辑进入/创建进入/保存成功应重置标红索引，实际 ' + m.length);
});

// ═══ E. 卡片渲染标红 ═══

test('E1. 卡片按节点索引标红并输出 data-node-id 与提示节点', () => {
  const src = readDashboardSrc();
  assert.ok(/const _violations = window\._sceneNodeViolations \|\| \{\};/.test(src),
    '渲染时应读取标红索引');
  assert.ok(/const _violation = _violations\[tag\];/.test(src), '应按节点 tag 命中冲突');
  assert.ok(/border:1\.5px solid #ff3b30/.test(src), '冲突节点应加红色边框');
  assert.ok(/class="ap-nd" data-node-id="\$\{tag\}"/.test(src), '卡片应带 data-node-id 供清红定位');
  assert.ok(/class="ap-nd-violation"/.test(src), '冲突节点应渲染提示节点');
});

// ═══ F. i18n 文案 ═══

test('F1. zh/en dashboard.json 均定义 skill_scope_mismatch', () => {
  const zh = JSON.parse(fs.readFileSync(ZH_DASH, 'utf8'));
  const en = JSON.parse(fs.readFileSync(EN_DASH, 'utf8'));
  assert.ok(zh.skill_scope_mismatch && zh.skill_scope_mismatch.length > 0,
    'zh/dashboard.json 应定义 skill_scope_mismatch');
  assert.ok(en.skill_scope_mismatch && en.skill_scope_mismatch.length > 0,
    'en/dashboard.json 应定义 skill_scope_mismatch');
});

// ═══ G. 语法 ═══

test('G1. dashboard.js 无语法错误（node Function 解析）', () => {
  new Function(readDashboardSrc());
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);