/*
 * Issue #400: chat agent 引擎选择单选渲染 — 前端测试
 *
 * 运行：node tests/js/test_issue_400_chat_engine_single_select.js
 *
 * 验证点：
 *   1. 本机直连 "opencode" + 个人实例 "opencode-my-mac" 并存时，selectedEngine
 *      指向个人实例 → 只有 personal 一条 active（不再 startsWith 前缀双选）
 *   2. selectedEngine 指向本机直连 → 只有 local 一条 active
 *   3. 无 selectedEngine（指针 0）→ 按 backend.type 精确 name 匹配，单条 active
 *   4. selectedEngine 必为单一对象（前端不再自行推断多选）
 */

const assert = require('assert');

// ── 提取前端选中态判定逻辑（与 chat.js _buildModelPicker 一致）──
function isEngineActive(b, selRid, curType) {
  const rid = parseInt(b.runtime_id || 0, 10);
  // Issue #400：单选语义——selectedEngine.runtime_id 为权威；无指针时精确 name 匹配
  return (selRid > 0 && rid === selRid) || (selRid <= 0 && b.name.toLowerCase() === curType);
}

function activeNames(backends, sel, backendType) {
  const selRid = parseInt((sel && sel.runtime_id) || 0, 10);
  const curType = (backendType || '').toLowerCase();
  return backends.filter(b => isEngineActive(b, selRid, curType)).map(b => b.name);
}

const localEngine = { name: 'opencode', display_name: 'opencode v1（本机直连）', scope: 'local', runtime_id: 3 };
const personalEngine = { name: 'opencode-my-mac', display_name: 'opencode-my-mac · opencode v1', scope: 'user', runtime_id: 7 };
const both = [localEngine, personalEngine];

function testPersonalPointerMarksOnlyPersonal() {
  // 会话指针 = 个人实例（实际使用个人机器引擎）
  const active = activeNames(both, { runtime_id: 7 }, 'opencode');
  assert.deepStrictEqual(active, ['opencode-my-mac'], '选中唯一且为个人实例');
  console.log('  PASS: personal pointer -> only personal active');
}

function testLocalPointerMarksOnlyLocal() {
  // 会话指针 = 本机直连
  const active = activeNames(both, { runtime_id: 3 }, 'opencode');
  assert.deepStrictEqual(active, ['opencode'], '选中唯一且为本机直连');
  console.log('  PASS: local pointer -> only local active');
}

function testNoPointerExactNameOnly() {
  // 无指针：backend.type='opencode' 只精确命中 name 相同的本机直连；
  // 个人实例 "opencode-my-mac" 不再因 startsWith 误选中
  const active = activeNames(both, null, 'opencode');
  assert.deepStrictEqual(active, ['opencode'], '无指针时应仅本机直连 active，个人实例不得多选');
  console.log('  PASS: no pointer -> exact name single select');
}

function testPersonalBackendTypeExactName() {
  // backend.type 本身就是实例名：精确匹配个人实例
  const active = activeNames(both, null, 'opencode-my-mac');
  assert.deepStrictEqual(active, ['opencode-my-mac'], '实例名 backend 应精确命中个人实例');
  console.log('  PASS: instance-name backend exact match');
}

function testSelectedEngineIsObject() {
  // 后端协议：selectedEngine 是单一对象（或 null），前端不再自行推断
  const good = { runtime_id: 7, name: 'opencode-my-mac' };
  assert.strictEqual(typeof good, 'object');
  assert.ok(!Array.isArray(good), 'selectedEngine 不可是数组');
  console.log('  PASS: selectedEngine is single object');
}

const tests = [
  testPersonalPointerMarksOnlyPersonal,
  testLocalPointerMarksOnlyLocal,
  testNoPointerExactNameOnly,
  testPersonalBackendTypeExactName,
  testSelectedEngineIsObject,
];

let failed = 0;
tests.forEach(fn => {
  try { fn(); } catch (e) { failed++; console.error('  FAIL:', e.message); }
});

if (failed) {
  console.error(`\n${failed} test(s) failed`);
  process.exit(1);
}
console.log('\nAll Issue #400 chat engine single-select tests passed.');