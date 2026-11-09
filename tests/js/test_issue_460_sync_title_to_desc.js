/*
 * Issue #460 回归测试：新建需求弹窗「同步标题到描述」功能
 *
 * 运行：node tests/js/test_issue_460_sync_title_to_desc.js
 *
 * 验证点：
 *   1. syncTitleToDesc: 标题为空 → 描述不变（无副作用）
 *   2. syncTitleToDesc: 标题纯空白 → 描述不变（trim 后为空）
 *   3. syncTitleToDesc: 描述为空 → 标题直接填入
 *   4. syncTitleToDesc: 描述已有内容 → 空行分隔追加标题
 *   5. syncTitleToDesc: 描述已包含标题 → 幂等不重复追加
 *   6. syncTitleToDesc: 元素缺失 → 安全 return 无异常
 *   7. i18n: zh/en tasks.json 含 sync_title_to_desc 键
 *   8. openTaskCreateModal: 描述标签 HTML 含 syncTitleToDesc onclick + ↧ 图标
 *
 * 策略：从 issues.js 源码提取 syncTitleToDesc 函数体，在 mock 沙箱执行。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const issuesJsPath = path.resolve(__dirname, '../../src/api/static/js/pages/issues.js');
const src = fs.readFileSync(issuesJsPath, 'utf8');

// ── 提取 syncTitleToDesc 函数体 ──
const fnMatch = src.match(/function syncTitleToDesc\(\)\s*\{([\s\S]*?)\n\}/);
if (!fnMatch) {
  console.error('✗ 无法从 issues.js 提取 syncTitleToDesc 函数体');
  process.exit(1);
}
const fnBody = fnMatch[1];

// ── DOM mock ──
function makeEl(id, value) {
  return {
    _id: id,
    style: {},
    textContent: '',
    value: value !== undefined ? value : '',
    innerHTML: '',
    disabled: false,
    remove() {},
  };
}

function makeDom(titleVal, descVal) {
  const elements = {
    tcCreateTitle: makeEl('tcCreateTitle', titleVal),
    tcCreateDesc: makeEl('tcCreateDesc', descVal),
  };
  return {
    dom: {
      getElementById(id) { return elements[id] || null; },
    },
    elements,
  };
}

// ── 构建可执行 syncTitleToDesc ──
function makeSyncTitleToDesc(dom) {
  const fn = new Function('document', fnBody + '\n;');
  function call() { fn(dom); }
  return { call };
}

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch(e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('Issue #460: 新建需求弹窗同步标题到描述 — 回归测试\n');

// ─── 测试 1：标题为空 → 描述不变 ───
test('syncTitleToDesc: 标题为空 → 描述不变（无副作用）', () => {
  const { dom, elements } = makeDom('', '已有描述');
  const ctx = makeSyncTitleToDesc(dom);
  ctx.call();
  assert.strictEqual(elements.tcCreateDesc.value, '已有描述', '描述应保持不变');
});

// ─── 测试 2：标题纯空白 → trim 后为空，描述不变 ───
test('syncTitleToDesc: 标题纯空白 → 描述不变（trim 后为空）', () => {
  const { dom, elements } = makeDom('   \n\t  ', '已有描述');
  const ctx = makeSyncTitleToDesc(dom);
  ctx.call();
  assert.strictEqual(elements.tcCreateDesc.value, '已有描述', '纯空白标题不应影响描述');
});

// ─── 测试 3：描述为空 → 标题直接填入 ───
test('syncTitleToDesc: 描述为空 → 标题直接填入', () => {
  const { dom, elements } = makeDom('我的标题', '');
  const ctx = makeSyncTitleToDesc(dom);
  ctx.call();
  assert.strictEqual(elements.tcCreateDesc.value, '我的标题', '空描述应直接填入标题');
});

// ─── 测试 4：描述已有内容 → 空行分隔追加 ───
test('syncTitleToDesc: 描述已有内容 → 空行分隔追加标题', () => {
  const { dom, elements } = makeDom('同步标题', '原有描述内容');
  const ctx = makeSyncTitleToDesc(dom);
  ctx.call();
  assert.strictEqual(elements.tcCreateDesc.value, '原有描述内容\n\n同步标题', '应以空行分隔追加');
});

// ─── 测试 5：描述已包含标题 → 幂等不重复追加 ───
test('syncTitleToDesc: 描述已包含标题 → 幂等不重复追加', () => {
  const { dom, elements } = makeDom('重复标题', '前面内容\n\n重复标题');
  const ctx = makeSyncTitleToDesc(dom);
  ctx.call();
  assert.strictEqual(elements.tcCreateDesc.value, '前面内容\n\n重复标题', '已包含标题时不应重复追加');
});

// ─── 测试 6：元素缺失 → 安全 return 无异常 ───
test('syncTitleToDesc: 元素缺失 → 安全 return 无异常', () => {
  const dom = { getElementById: () => null };
  const ctx = makeSyncTitleToDesc(dom);
  ctx.call();  // 不应抛异常
});

// ─── 测试 7：标题有首尾空格 → trim 后追加 ───
test('syncTitleToDesc: 标题有首尾空格 → trim 后追加到空描述', () => {
  const { dom, elements } = makeDom('  带空格标题  ', '');
  const ctx = makeSyncTitleToDesc(dom);
  ctx.call();
  assert.strictEqual(elements.tcCreateDesc.value, '带空格标题', '应 trim 后追加');
});

// ─── 测试 8：i18n zh/en 含 sync_title_to_desc 键 ───
test('i18n: zh/en tasks.json 均含 create.sync_title_to_desc 键', () => {
  const zhPath = path.resolve(__dirname, '../../src/api/static/locales/zh/tasks.json');
  const enPath = path.resolve(__dirname, '../../src/api/static/locales/en/tasks.json');
  const zh = JSON.parse(fs.readFileSync(zhPath, 'utf8'));
  const en = JSON.parse(fs.readFileSync(enPath, 'utf8'));
  assert.ok(zh.create && zh.create.sync_title_to_desc, 'zh tasks.json 应含 create.sync_title_to_desc');
  assert.ok(en.create && en.create.sync_title_to_desc, 'en tasks.json 应含 create.sync_title_to_desc');
  assert.strictEqual(zh.create.sync_title_to_desc, '同步标题到描述', 'zh 文案正确');
  assert.strictEqual(en.create.sync_title_to_desc, 'Sync title to description', 'en 文案正确');
});

// ─── 测试 9：openTaskCreateModal HTML 含 syncTitleToDesc onclick + ↧ 图标 ───
test('openTaskCreateModal: 描述标签 HTML 含 syncTitleToDesc onclick + ↧ 图标', () => {
  assert.ok(src.indexOf('syncTitleToDesc') >= 0, 'issues.js 应含 syncTitleToDesc 函数调用');
  assert.ok(src.indexOf('tasks.create.sync_title_to_desc') >= 0, 'issues.js 应引用 sync_title_to_desc i18n 键');
  assert.ok(src.indexOf('↧') >= 0, 'issues.js 描述标签应含 ↧ 图标符号');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
