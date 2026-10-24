/*
 * Issue #407 遗留缺口: ChatAgent 输入草稿持久化 — 回归测试
 *
 * 运行：node tests/js/test_issue_407_chat_draft.js
 *
 * 覆盖（验收标准 1/2/3 + PR #598 不回归）：
 *   A. 草稿存/取/清（会话粒度）
 *     1. _saveChatDraft 写入 chatDraft_<convId> 带 ts；_restoreChatDraft 命中返回文本
 *     2. _restoreChatDraft 无草稿返回 null；JSON 损坏返回 null 不抛错
 *     3. _clearChatDraft 清除后返回 null
 *     4. 草稿键会话粒度隔离（conv-a / conv-b 互不串扰）
 *     5. 空文本保存 = 清除键（手动清空输入框场景）
 *     6. 无 convId 不保存（尚未选中会话，避免跨会话串扰）
 *     7. _onChatInputDraft 入口：经 _currentConvId 保存（input 事件路径）
 *   B. 过期降级
 *     8. 草稿 ts 超 1 小时 → _restoreChatDraft 返回 null 并清除键
 *     9. 草稿 ts 未超 1 小时（边界 3599999ms）→ 正常返回
 *    10. 老格式草稿（无 ts）→ 不按过期处理，正常返回
 *   C. 最后所在会话记忆（chatLastConv）
 *    11. _saveChatLastConv 写入带 ts；_getChatLastConv 命中返回 convId
 *    12. _saveChatLastConv(null) 清除记忆；超 1 小时过期返回 null 并清除
 *   D. 集成点（静态断言）
 *    13. chatInput textarea oninput 挂接 _onChatInputDraft
 *    14. switchConversation 消息渲染完成后回填草稿 + 自适应高度
 *    15. switchConversation 切换前保存旧会话草稿并清空输入框（防串扰）
 *    16. loadConversations 自动选中优先级：显式跳转 > 最后会话 > 第一个
 *    17. doSend 发送后清除草稿；发送失败恢复草稿
 *    18. _doDeleteConv 删除会话时清除对应草稿键 + 作废会话记忆
 *    19. newConversation / 首发建会话 记忆新会话
 *   E. PR #598 不回归（静态断言）
 *    20. app.js visibilitychange 守卫条件保持 !window._dashboardEditing 原样
 *    21. dashboard.js 草稿工具函数原样保留
 *    22. chat_task_ 键空间不受影响（与 chatDraft_ 正交）
 *    23. chat.js 无语法错误（node Function 解析）
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const CHAT_JS = path.join(ROOT, 'src/api/static/js/pages/chat.js');
const DASHBOARD_JS = path.join(ROOT, 'src/api/static/js/pages/dashboard.js');
const APP_JS = path.join(ROOT, 'src/api/static/js/app.js');

// ── 提取 chat.js 草稿工具函数（源码切片求值，不依赖浏览器）──
// 切片含 _currentConvId 引用（_onChatInputDraft），注入桩值
function extractDraftUtils(sessionStorageMock, currentConvId) {
  const src = fs.readFileSync(CHAT_JS, 'utf8');
  const START = 'var _CHAT_DRAFT_TTL';
  const END = '\n/**';
  const i0 = src.indexOf(START);
  const i1 = src.indexOf(END, i0);
  assert.ok(i0 >= 0 && i1 > i0, 'chat.js 应包含草稿工具函数区块');
  const slice = src.slice(i0, i1);
  const full = 'var _currentConvId = ' + JSON.stringify(currentConvId || null) + ';\n' + slice;
  const fn = new Function('sessionStorage', 'Date', 'JSON', full + '\nreturn { _chatDraftKey: _chatDraftKey, _saveChatDraft: _saveChatDraft, _restoreChatDraft: _restoreChatDraft, _clearChatDraft: _clearChatDraft, _onChatInputDraft: _onChatInputDraft, _saveChatLastConv: _saveChatLastConv, _getChatLastConv: _getChatLastConv };');
  return fn(sessionStorageMock, Date, JSON);
}

function extractDraftUtilsWithDate(sessionStorageMock, currentConvId, now) {
  const src = fs.readFileSync(CHAT_JS, 'utf8');
  const START = 'var _CHAT_DRAFT_TTL';
  const END = '\n/**';
  const i0 = src.indexOf(START);
  const i1 = src.indexOf(END, i0);
  assert.ok(i0 >= 0 && i1 > i0, 'chat.js 应包含草稿工具函数区块');
  const slice = src.slice(i0, i1);
  const full = 'var _currentConvId = ' + JSON.stringify(currentConvId || null) + ';\n' + slice;
  const FakeDate = makeFakeDate(now, Date);
  const fn = new Function('sessionStorage', 'Date', 'JSON', full + '\nreturn { _chatDraftKey: _chatDraftKey, _saveChatDraft: _saveChatDraft, _restoreChatDraft: _restoreChatDraft, _clearChatDraft: _clearChatDraft, _onChatInputDraft: _onChatInputDraft, _saveChatLastConv: _saveChatLastConv, _getChatLastConv: _getChatLastConv };');
  return fn(sessionStorageMock, FakeDate, JSON);
}

function makeSessionStorage() {
  return {
    _s: {},
    getItem(k) { return (k in this._s) ? this._s[k] : null; },
    setItem(k, v) { this._s[k] = String(v); },
    removeItem(k) { delete this._s[k]; },
    clear() { this._s = {}; },
  };
}

// ── 时间控制：可注入当前时间的 Date 替身 ──
function makeFakeDate(now, realDateClass) {
  function FakeDate(...args) {
    if (args.length === 0) return new realDateClass(now);
    return new realDateClass(...args);
  }
  FakeDate.now = () => now;
  FakeDate.parse = realDateClass.parse;
  FakeDate.UTC = realDateClass.UTC;
  return FakeDate;
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  \u2713 ' + name); }
  catch (e) { failed++; console.log('  \u2717 ' + name + '\n    ' + e.message); }
}

function readChatSrc() { return fs.readFileSync(CHAT_JS, 'utf8'); }
function readDashboardSrc() { return fs.readFileSync(DASHBOARD_JS, 'utf8'); }
function readAppSrc() { return fs.readFileSync(APP_JS, 'utf8'); }

console.log('Issue #407 遗留缺口: ChatAgent 输入草稿持久化 — 回归测试\n');

// ═══ A. 草稿存/取/清（会话粒度） ═══

test('A1. _saveChatDraft 写入 chatDraft_<convId> 带 ts；_restoreChatDraft 命中返回文本', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss, 'conv-1');
  const before = Date.now();
  u._saveChatDraft('conv-1', '未发送的草稿文本');
  const raw = JSON.parse(ss.getItem('chatDraft_conv-1'));
  assert.ok(raw.ts >= before, '草稿对象应带 ts（保存时注入）');
  assert.strictEqual(raw.text, '未发送的草稿文本', '草稿文本应完整保存');
  assert.strictEqual(u._restoreChatDraft('conv-1'), '未发送的草稿文本', '恢复应返回草稿文本');
});

test('A2. _restoreChatDraft: 无草稿返回 null；JSON 损坏返回 null 不抛错', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss, 'conv-1');
  assert.strictEqual(u._restoreChatDraft('conv-x'), null, '无草稿应返回 null');
  ss.setItem('chatDraft_conv-bad', '{not-json');
  let got = null;
  assert.doesNotThrow(() => { got = u._restoreChatDraft('conv-bad'); },
    'JSON 损坏不应抛错（try/catch 降级）');
  assert.strictEqual(got, null, 'JSON 损坏应返回 null');
});

test('A3. _clearChatDraft: 清除后恢复返回 null', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss, 'conv-1');
  u._saveChatDraft('conv-1', '待清除');
  assert.ok(u._restoreChatDraft('conv-1'), '清除前应能恢复');
  u._clearChatDraft('conv-1');
  assert.strictEqual(u._restoreChatDraft('conv-1'), null, '清除后应返回 null');
});

test('A4. 草稿键会话粒度隔离 — conv-a / conv-b 互不串扰', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss, 'conv-a');
  u._saveChatDraft('conv-a', '会话A草稿');
  u._saveChatDraft('conv-b', '会话B草稿');
  assert.strictEqual(u._chatDraftKey('conv-a'), 'chatDraft_conv-a', '键名应为 chatDraft_<convId>');
  assert.strictEqual(u._restoreChatDraft('conv-a'), '会话A草稿', 'A 会话恢复 A 草稿');
  assert.strictEqual(u._restoreChatDraft('conv-b'), '会话B草稿', 'B 会话恢复 B 草稿');
  u._clearChatDraft('conv-a');
  assert.strictEqual(u._restoreChatDraft('conv-a'), null, '清除 A 不影响 B');
  assert.strictEqual(u._restoreChatDraft('conv-b'), '会话B草稿', 'B 草稿不受 A 清除影响');
});

test('A5. 空文本保存 = 清除键（手动清空输入框场景）', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss, 'conv-1');
  u._saveChatDraft('conv-1', '先有内容');
  u._saveChatDraft('conv-1', '');
  assert.strictEqual(ss.getItem('chatDraft_conv-1'), null, '空文本保存应移除键（不残留空草稿）');
  assert.strictEqual(u._restoreChatDraft('conv-1'), null, '清空后恢复应返回 null');
});

test('A6. 无 convId 不保存（尚未选中会话，避免跨会话串扰）', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss, null);
  u._saveChatDraft(null, '不应保存');
  const keys = Object.keys(ss._s).filter(k => k.startsWith('chatDraft_'));
  assert.strictEqual(keys.length, 0, 'sessionStorage 不应出现任何 chatDraft_ 键');
});

test('A7. _onChatInputDraft 入口：经 _currentConvId 保存（input 事件路径）', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss, 'conv-live');
  u._onChatInputDraft({ value: '输入的未发送文本' });
  assert.strictEqual(u._restoreChatDraft('conv-live'), '输入的未发送文本',
    'input 事件应把当前会话草稿落盘');
});

// ═══ B. 过期降级 ═══

test('B1. 草稿 ts 超 1 小时 → _restoreChatDraft 返回 null 并清除键', () => {
  const ss = makeSessionStorage();
  const now = Date.now();
  const u = extractDraftUtilsWithDate(ss, 'conv-1', now);
  u._saveChatDraft('conv-1', '将过期');
  const uLater = extractDraftUtilsWithDate(ss, 'conv-1', now + 3600000 + 1000);
  const got = uLater._restoreChatDraft('conv-1');
  assert.strictEqual(got, null, '过期草稿不应回填（返回 null）');
  assert.strictEqual(ss.getItem('chatDraft_conv-1'), null, '过期草稿键应被清除（不残留）');
});

test('B2. 草稿 ts 未超 1 小时（边界 3599999ms）→ 正常返回', () => {
  const ss = makeSessionStorage();
  const now = Date.now();
  const u = extractDraftUtilsWithDate(ss, 'conv-1', now);
  u._saveChatDraft('conv-1', '未过期');
  const uSoon = extractDraftUtilsWithDate(ss, 'conv-1', now + 3599999);
  assert.strictEqual(uSoon._restoreChatDraft('conv-1'), '未过期',
    '1 小时内（含边界 3599999ms）草稿应正常返回');
});

test('B3. 老格式草稿（无 ts）→ 不按过期处理，正常返回', () => {
  const ss = makeSessionStorage();
  ss.setItem('chatDraft_conv-legacy', JSON.stringify({ text: 'legacy' }));
  const u = extractDraftUtils(ss, 'conv-legacy');
  assert.strictEqual(u._restoreChatDraft('conv-legacy'), 'legacy',
    '无 ts 的老格式草稿应正常返回（不误清）');
});

// ═══ C. 最后所在会话记忆（chatLastConv） ═══

test('C1. _saveChatLastConv 写入带 ts；_getChatLastConv 命中返回 convId', () => {
  const ss = makeSessionStorage();
  const u = extractDraftUtils(ss, 'conv-9');
  u._saveChatLastConv('conv-9');
  const raw = JSON.parse(ss.getItem('chatLastConv'));
  assert.ok(raw.ts > 0, 'chatLastConv 应带 ts');
  assert.strictEqual(u._getChatLastConv(), 'conv-9', '记忆应返回最后所在会话 ID');
});

test('C2. _saveChatLastConv(null) 清除记忆；超 1 小时过期返回 null 并清除', () => {
  const ss = makeSessionStorage();
  const now = Date.now();
  const u = extractDraftUtilsWithDate(ss, 'conv-9', now);
  u._saveChatLastConv('conv-9');
  u._saveChatLastConv(null);
  assert.strictEqual(ss.getItem('chatLastConv'), null, '传 null 应清除记忆键');
  // 过期路径
  u._saveChatLastConv('conv-9');
  const uLater = extractDraftUtilsWithDate(ss, 'conv-9', now + 3600000 + 1000);
  assert.strictEqual(uLater._getChatLastConv(), null, '超 1 小时记忆应返回 null');
  assert.strictEqual(ss.getItem('chatLastConv'), null, '过期记忆键应被清除');
});

// ═══ D. 集成点（静态断言） ═══

test('D1. chatInput textarea oninput 挂接 _onChatInputDraft', () => {
  const src = readChatSrc();
  assert.ok(
    /<textarea id="chatInput"[^>]*oninput="_autoResizeTextarea\(this\);_onChatInputDraft\(this\)"/.test(src),
    'chatInput oninput 应调用 _onChatInputDraft（保存草稿）'
  );
});

test('D2. switchConversation 消息渲染完成后回填草稿 + 自适应高度', () => {
  const src = readChatSrc();
  assert.ok(
    /var _draft = input \? _restoreChatDraft\(convId\) : null;\s*\n\s*if \(_draft\) \{\s*\n\s*input\.value = _draft;\s*\n\s*_autoResizeTextarea\(input\);/.test(src),
    'switchConversation 应在渲染后回填草稿并自适应高度'
  );
});

test('D3. switchConversation 切换前保存旧会话草稿并清空输入框（防串扰）', () => {
  const src = readChatSrc();
  assert.ok(
    /if \(_currentConvId !== convId\) \{\s*\n\s*var _prevInput = document\.getElementById\('chatInput'\);\s*\n\s*if \(_prevInput\) \{\s*\n\s*if \(_currentConvId\) _saveChatDraft\(_currentConvId, _prevInput\.value\);\s*\n\s*_prevInput\.value = '';\s*\n\s*_resetTextareaHeight\(_prevInput\);/.test(src),
    'switchConversation 切换前应保存旧会话草稿并清空输入框'
  );
  assert.ok(/_saveChatLastConv\(convId\);/.test(src), 'switchConversation 应记忆当前会话');
});

test('D4. loadConversations 自动选中优先级：显式跳转 > 最后会话 > 第一个', () => {
  const src = readChatSrc();
  assert.ok(
    /const _lastConv = _getChatLastConv\(\);\s*\n\s*const target = _openConvId && convs\.some\(c => c\.id === _openConvId\)\s*\n\s*\? _openConvId\s*\n\s*: \(_lastConv && convs\.some\(c => c\.id === _lastConv\) \? _lastConv : convs\[0\]\.id\);/.test(src),
    '自动选中应优先 _openConvId > 最后所在会话 > 第一个会话'
  );
});

test('D5. doSend 发送后清除草稿；发送失败恢复草稿', () => {
  const src = readChatSrc();
  assert.ok(
    /_clearVideo\(\);\s*\n\s*\/\/ Issue #407: 已发送文本不再作为草稿（避免回填已发送内容）\s*\n\s*_clearChatDraft\(_currentConvId\);/.test(src),
    'doSend 清空输入后应调用 _clearChatDraft'
  );
  assert.ok(
    /\/\/ Issue #407: 发送失败 → 恢复草稿（文本未上服务器，不能丢）\s*\n\s*var _sendInput = document\.getElementById\('chatInput'\);\s*\n\s*if \(_sendInput && _currentConvId && !_sendInput\.value\) \{\s*\n\s*_sendInput\.value = msg;/.test(src),
    'submit 失败应把文本回填输入框并重建草稿'
  );
});

test('D6. _doDeleteConv 删除会话时清除对应草稿键 + 作废会话记忆', () => {
  const src = readChatSrc();
  assert.ok(
    /function _doDeleteConv\(convId\) \{\s*\n\s*_closeChatDelPopover\(\);\s*\n\s*\/\/ Issue #407: 删除会话同时清除其未发送草稿（会话消失草稿不残留）\s*\n\s*_clearChatDraft\(convId\);/.test(src),
    '_doDeleteConv 应在删除前清除该会话草稿键'
  );
  assert.ok(
    /\/\/ Issue #407: 当前会话被删 → 作废最后会话记忆（闲置返回不选中已删会话）\s*\n\s*_saveChatLastConv\(null\);/.test(src),
    '删除当前会话应作废 chatLastConv 记忆'
  );
});

test('D7. newConversation / 首发建会话 / switchConversation 均记忆当前会话', () => {
  const src = readChatSrc();
  const m1 = src.match(/_saveChatLastConv\(_currentConvId\);/g) || [];
  const m2 = src.match(/_saveChatLastConv\(convId\);/g) || [];
  assert.ok(m1.length + m2.length >= 3,
    'newConversation / 首发建会话（_currentConvId）+ switchConversation（convId）合计应 ≥3 处，实际 ' + (m1.length + m2.length));
});

// ═══ E. PR #598 不回归（静态断言） ═══

test('E1. app.js visibilitychange 守卫保留 _dashboardEditing（PR #598 不回归）并扩展 _chatEditing', () => {
  const src = readAppSrc();
  assert.ok(
    /Date\.now\(\)\s*-\s*_hiddenSince\s*>\s*60000\s*&&\s*!\(window\._dashboardEditing\s*\|\|\s*window\._chatEditing\)/.test(src),
    '守卫应同时覆盖 _dashboardEditing（PR #598 不回归）与 _chatEditing（附件保活扩展）'
  );
  assert.ok(src.includes('window._dashboardEditing'), '_dashboardEditing 守卫保留（dashboard 不回归）');
});

test('E2. dashboard.js 草稿工具函数原样保留（_saveDashDraft/_restoreDashDraft/_clearDashDraft/_getDashEditMode）', () => {
  const src = readDashboardSrc();
  ['function _saveDashDraft', 'function _restoreDashDraft', 'function _clearDashDraft', 'function _getDashEditMode', 'function _saveDashEditMode', 'function _clearDashEditMode'].forEach(fn => {
    assert.ok(src.includes(fn), 'dashboard.js 应保留 ' + fn);
  });
});

test('E3. chat_task_ 键空间不受影响（与 chatDraft_ 正交）', () => {
  const src = readChatSrc();
  assert.ok(src.includes("'chat_task_' + convId"), '既有 chat_task_<convId> 持久化模式应原样保留');
  assert.ok(src.includes("'chatDraft_' + convId"), '新增 chatDraft_<convId> 键前缀独立');
});

test('E4. chat.js 无语法错误（node Function 解析）', () => {
  new Function(readChatSrc());
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
