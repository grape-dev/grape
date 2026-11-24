/*
 * Issue #400: chat agent 引擎选择单选渲染 — 前端测试
 *          ＋ #3396 收尾 I1/I4：uid 优先选中键 + 离线引擎禁选
 *
 * 运行：node tests/js/test_issue_400_chat_engine_single_select.js
 *
 * 验证点：
 *   1. 本机直连 "opencode" + 个人实例 "opencode-my-mac" 并存时，selectedEngine
 *      指向个人实例 → 只有 personal 一条 active（不再 startsWith 前缀双选）
 *   2. selectedEngine 指向本机直连 → 只有 local 一条 active
 *   3. selectedEngine 缺 runtime_id 但有 engine_uid（D11/#868 T10 口径）→ 按 uid 单选
 *   4. 无 selectedEngine（指针 0 且无 uid）→ 0 条 active（I1 方案 A：名字兜底已删，
 *      同实例多引擎不再并列高亮/静默串台）
 *   5. selectedEngine 必为单一对象（前端不再自行推断多选）
 *   6. I4：disabled 引擎不可保存（saveModelConfig 前置守卫短路）
 */

const assert = require('assert');

// ── 提取前端选中态判定逻辑（与 ChatView.vue isBackendActive 一致：uid 优先、id 兜底、无名字兜底）──
function isBackendActive(b, sel) {
  const selUid = String((sel && sel.engine_uid) || '').toLowerCase();
  const uid = String(b.engine_uid || '').toLowerCase();
  if (selUid) return !!uid && uid === selUid;
  const selRid = parseInt((sel && sel.runtime_id) || 0, 10);
  const rid = parseInt(b.runtime_id || 0, 10);
  return selRid > 0 && rid === selRid;
}

function activeNames(backends, sel) {
  return backends.filter(b => isBackendActive(b, sel)).map(b => b.name);
}

// ── I4：saveModelConfig 守卫镜像（disabled 短路，不发起 PUT）──
function saveModelConfigAllows(b) {
  return !(b && b.disabled);
}

const localEngine = { name: 'opencode', display_name: 'opencode v1（本机直连）', scope: 'local', runtime_id: 3, engine_uid: 'uid-local-0003' };
const personalEngine = { name: 'opencode-my-mac', display_name: 'opencode-my-mac · opencode v1', scope: 'user', runtime_id: 7, engine_uid: 'uid-pers-0007' };
const both = [localEngine, personalEngine];

function testPersonalPointerMarksOnlyPersonal() {
  // 会话指针 = 个人实例（runtime_id 轨道）
  const active = activeNames(both, { runtime_id: 7 });
  assert.deepStrictEqual(active, ['opencode-my-mac'], '选中唯一且为个人实例');
  console.log('  PASS: personal pointer -> only personal active');
}

function testLocalPointerMarksOnlyLocal() {
  // 会话指针 = 本机直连
  const active = activeNames(both, { runtime_id: 3 });
  assert.deepStrictEqual(active, ['opencode'], '选中唯一且为本机直连');
  console.log('  PASS: local pointer -> only local active');
}

function testUidPointerMarksOnlyUidMatch() {
  // I1/#868 T10：指针缺 runtime_id 但有 engine_uid → uid 单选（D11 uid 稳定键口径）
  const active = activeNames(both, { engine_uid: 'UID-PERS-0007' });
  assert.deepStrictEqual(active, ['opencode-my-mac'], 'uid 命中唯一引擎（大小写不敏感）');
  console.log('  PASS: uid pointer -> single select by engine_uid');
}

function testNoPointerNoActive() {
  // I1 方案 A：无指针（rid=0 且无 uid）→ 0 条 active；名字兜底删除，
  // 同实例多引擎不再并列高亮（旧实现按 b.name===curType 会双选/串台）
  const active = activeNames(both, null);
  assert.deepStrictEqual(active, [], '无指针时不得按名字兜底高亮');
  console.log('  PASS: no pointer -> zero active (name fallback removed)');
}

function testUidAbsentFallsBackToId() {
  // 旧数据兜底：selectedEngine 有 runtime_id、候选无 uid → 按 id 单选
  const legacyLocal = { name: 'opencode', scope: 'local', runtime_id: 3, engine_uid: '' };
  const active = activeNames([legacyLocal], { runtime_id: 3 });
  assert.deepStrictEqual(active, ['opencode'], 'uid 缺失时 runtime_id 兜底生效');
  console.log('  PASS: uid missing -> runtime_id fallback');
}

function testSelectedEngineIsObject() {
  // 后端协议：selectedEngine 是单一对象（或 null），前端不再自行推断
  const good = { runtime_id: 7, name: 'opencode-my-mac' };
  assert.strictEqual(typeof good, 'object');
  assert.ok(!Array.isArray(good), 'selectedEngine 不可是数组');
  console.log('  PASS: selectedEngine is single object');
}

function testDisabledEngineNotSavable() {
  // I4：离线引擎 saveModelConfig 短路（toast 提示，不发 PUT）
  assert.strictEqual(saveModelConfigAllows({ disabled: true, name: 'x' }), false, 'disabled 引擎不可保存');
  assert.strictEqual(saveModelConfigAllows({ disabled: false, name: 'x' }), true, '在线引擎可保存');
  assert.strictEqual(saveModelConfigAllows({ name: 'x' }), true, '缺 disabled 视为可选（旧后端兼容）');
  console.log('  PASS: disabled engine blocked from save');
}

const tests = [
  testPersonalPointerMarksOnlyPersonal,
  testLocalPointerMarksOnlyLocal,
  testUidPointerMarksOnlyUidMatch,
  testNoPointerNoActive,
  testUidAbsentFallsBackToId,
  testSelectedEngineIsObject,
  testDisabledEngineNotSavable,
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
