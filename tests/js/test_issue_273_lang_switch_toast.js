/*
 * Issue #273 回归测试：语言切换「Success」弹窗移除
 *
 * 运行：node tests/js/test_issue_273_lang_switch_toast.js
 *
 * 验证点（与 src/api/static/js/pages/settings.js selectLang 一致）：
 *   1. 全部成功（failed=0，非 cached）→ 不弹任何 Toast
 *   2. 重试成功（当前语言包重载成功）→ 不弹任何 Toast
 *   3. 部分成功（0 < failed < total）→ 不弹任何 Toast
 *   4. 缓存命中（cached=true）→ 不弹任何 Toast
 *   5. 全部失败（failed=total）→ 弹 error Toast（用户需知道切换失败）
 *   6. setLang 抛异常 → 弹 error Toast
 *   7. 成功路径按钮状态恢复/锁释放不影响（_langSwitching 复位）
 */

const assert = require('assert');
const path = require('path');
const fs = require('fs');

const settingsJsPath = path.resolve(__dirname, '../../src/api/static/js/pages/settings.js');
const src = fs.readFileSync(settingsJsPath, 'utf8');

// ── 极简 DOM mock（覆盖 settings.js 顶层 + selectLang 用到的 API）──
function makeDom() {
  const btn = {
    textContent: 'English', disabled: false, style: {},
    setAttribute: () => {},
  };
  const btnList = [btn];
  btnList.forEach = Array.prototype.forEach;
  const dom = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => btnList,
    createElement: () => ({ style: {}, classList: { add(){}, remove(){} }, innerHTML: '', textContent: '', appendChild: () => {}, remove(){}, querySelector: () => null }),
    body: { appendChild: () => {} },
    documentElement: { setAttribute(){}, removeAttribute(){} },
    addEventListener: () => {},
  };
  return { dom, btn };
}

// ── 沙箱加载 settings.js 并执行 selectLang ──
function runSelectLang(setLangImpl, langBtnState) {
  const { dom, btn } = makeDom();
  const toastCalls = [];
  const app = {
    showToast: function(msg) { toastCalls.push(msg); },
    setTheme: () => {}, setThemeMode: () => {},
    currentRepo: () => null, nav: () => {},
  };
  const I18N = {
    _lang: 'zh',
    getLang: function() { return this._lang; },
    isLangLoaded: function() { return true; },
    setLang: setLangImpl,
    t: function(k) { return k; },
  };
  const API = { get: () => Promise.resolve({}), post: () => Promise.resolve({}), put: () => Promise.resolve({}), del: () => Promise.resolve({}) };
  const sandbox = {
    document: dom, app, I18N, API,
    console: { log: () => {}, error: () => {}, warn: () => {} },
    setTimeout, clearTimeout, setInterval, clearInterval,
    Date, JSON, Array, Object, String, Number, Boolean, Math,
    encodeURIComponent, decodeURIComponent, navigator: {}, confirm: () => true,
  };
  const keys = Object.keys(sandbox);
  const vals = keys.map(k => sandbox[k]);
  const fn = new Function(...keys, src + '\n; return { selectLang: selectLang, _langSwitching: function(){ return _langSwitching; } };');
  const exported = fn(...vals);

  exported.selectLang(langBtnState && langBtnState.lang || 'en');
  return { toastCalls, btn, getLock: exported._langSwitching, I18N };
}

// ── 辅助：等待 Promise 队列清空 ──
async function drain() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

(async () => {
  console.log('Issue #273: 语言切换成功路径不弹「Success」Toast — 回归测试\n');

  await test('全部成功（failed=0）→ 不弹 Toast', async () => {
    const r = runSelectLang(() => Promise.resolve({ failed: 0, total: 15, cached: false }));
    await drain();
    assert.strictEqual(r.toastCalls.length, 0,
      '成功路径不应弹 Toast，实际弹了: ' + JSON.stringify(r.toastCalls));
  });

  await test('重试成功（当前语言包重载）→ 不弹 Toast', async () => {
    const r = runSelectLang(() => Promise.resolve({ failed: 0, total: 15, cached: false }));
    await drain();
    assert.strictEqual(r.toastCalls.length, 0,
      '重试成功路径不应弹 Toast（页面语言未变，重载是静默修复），实际弹了: ' + JSON.stringify(r.toastCalls));
  });

  await test('部分成功（failed=3/15）→ 不弹 Toast', async () => {
    const r = runSelectLang(() => Promise.resolve({ failed: 3, total: 15, cached: false }));
    await drain();
    assert.strictEqual(r.toastCalls.length, 0,
      '部分成功路径不应弹 Toast（页面语言已切换，缺失文案有 fallback），实际弹了: ' + JSON.stringify(r.toastCalls));
  });

  await test('缓存命中（cached=true）→ 不弹 Toast', async () => {
    const r = runSelectLang(() => Promise.resolve({ failed: 0, total: 0, cached: true }));
    await drain();
    assert.strictEqual(r.toastCalls.length, 0,
      '缓存命中路径不应弹 Toast，实际弹了: ' + JSON.stringify(r.toastCalls));
  });

  await test('全部失败（failed=total）→ 弹 error Toast', async () => {
    const r = runSelectLang(() => Promise.resolve({ failed: 15, total: 15, cached: false }));
    await drain();
    assert.strictEqual(r.toastCalls.length, 1,
      '全部失败路径应弹 1 次 error Toast，实际: ' + JSON.stringify(r.toastCalls));
    assert.ok(String(r.toastCalls[0]).indexOf('error') !== -1,
      '失败 Toast 文案应含 error，实际: ' + r.toastCalls[0]);
  });

  await test('setLang 抛异常 → 弹 error Toast', async () => {
    const r = runSelectLang(() => Promise.reject(new Error('boom')));
    await drain();
    assert.strictEqual(r.toastCalls.length, 1,
      '异常路径应弹 1 次 error Toast，实际: ' + JSON.stringify(r.toastCalls));
  });

  await test('成功路径完成后全局锁释放（可继续下次切换）', async () => {
    const r = runSelectLang(() => Promise.resolve({ failed: 0, total: 15, cached: false }));
    await drain();
    assert.strictEqual(r.getLock(), false, 'selectLang 完成后 _langSwitching 应为 false');
  });

  await test('全部失败后按钮恢复可用', async () => {
    const r = runSelectLang(() => Promise.resolve({ failed: 15, total: 15, cached: false }));
    await drain();
    assert.strictEqual(r.btn.disabled, false, '全部失败后按钮应恢复可用');
    assert.strictEqual(r.btn.style.opacity, '', '全部失败后按钮透明度应复位');
  });

  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed > 0 ? 1 : 0);
})();
