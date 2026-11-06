/*
 * Issue #1167: Agent 运行时页筛选命中 0 条时筛选栏/✕ 清除消失 — 前端测试
 *
 * 运行：node tests/js/test_issue_1167_runtime_empty_filter.js
 *
 * 验证点：
 *   1. _rtHasActiveFilters 能识别全部筛选态（范围/类型/场景/标签/搜索）
 *   2. 有筛选且数据为空时，空态渲染「筛选无结果」文案 + 可点击清除入口（复用 _rtClearFilters）
 *   3. 无筛选且数据为空时，保持原有「组织无引擎」引导 + 添加设备按钮，不出现清除入口
 *   4. zh/en locales 均含 no_result_* / clear_filters 键
 */

'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const RUNTIMES_JS = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/runtimes.js'), 'utf8');
const ZH = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/runtimes.json'), 'utf8'));
const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/runtimes.json'), 'utf8'));

function makeT(locale) {
  return function (key) {
    const parts = String(key).replace(/^runtimes\./, '').split('.');
    let cur = locale;
    for (const p of parts) {
      if (cur == null || typeof cur !== 'object') return key;
      cur = cur[p];
    }
    return typeof cur === 'string' ? cur : key;
  };
}

function runHarness(harnessBody) {
  const pageContent = { innerHTML: '' };
  const el = () => ({
    innerHTML: '', textContent: '', style: {}, onclick: null,
    classList: { add() {}, remove() {} },
    appendChild() {}, remove() {}, insertAdjacentHTML() {}, setAttribute() {},
  });
  const noop = () => {};
  const doc = {
    getElementById: (id) => (id === 'pageContent' ? pageContent : el()),
    createElement: () => el(),
    addEventListener: noop,
    head: { appendChild: noop },
    body: el(),
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  const storage = { getItem: () => null, setItem: noop, removeItem: noop };
  const sandbox = {
    I18N: { t: makeT(ZH) },
    document: doc,
    app: { showToast: noop, currentRepo: () => ({ fullName: '' }), nav: noop },
    API: { get: () => Promise.resolve({}), post: () => Promise.resolve({}) },
    localStorage: storage,
    sessionStorage: storage,
    setTimeout: () => 0,
    clearTimeout: noop,
    setInterval: () => 0,
    clearInterval: noop,
    addEventListener: noop,
    removeEventListener: noop,
    confirm: () => true,
    alert: noop,
    location: { search: '', href: '' },
    console,
  };
  sandbox.window = sandbox;
  sandbox.__capture = () => pageContent.innerHTML;
  vm.createContext(sandbox);
  vm.runInContext(RUNTIMES_JS + '\n;(' + harnessBody + ')();', sandbox, {
    filename: 'runtimes.js + harness',
  });
  return { sandbox, html: pageContent.innerHTML };
}

function resetFilters() {
  _rtFilterScope = '';
  _rtFilterType = '';
  _rtFilterScene = [];
  _rtFilterTag = [];
  _runtimeSearch = '';
  _rtRange = 'all';
}

// 1. 筛选态识别
test_hasActiveFilters();
function test_hasActiveFilters() {
  assert.ok(/function _rtHasActiveFilters\(/.test(RUNTIMES_JS), '应新增 _rtHasActiveFilters 函数');
  const { sandbox } = runHarness(function () {
    __check = (cond, name) => { if (!cond) throw new Error('FAIL: ' + name); };
    resetFilters();
    __check(_rtHasActiveFilters() === false, '默认无筛选');
    _rtFilterScope = 'repo'; __check(_rtHasActiveFilters() === true, 'scope');
    resetFilters(); _rtFilterType = 'opencode'; __check(_rtHasActiveFilters() === true, 'type');
    resetFilters(); _rtFilterScene = ['dev']; __check(_rtHasActiveFilters() === true, 'scene');
    resetFilters(); _rtFilterTag = ['ops']; __check(_rtHasActiveFilters() === true, 'tag');
    resetFilters(); _runtimeSearch = 'abc'; __check(_rtHasActiveFilters() === true, 'search');
    resetFilters(); _rtRange = 'repo'; __check(_rtHasActiveFilters() === true, 'range');
    resetFilters();
  });
  assert.ok(sandbox, 'harness 应执行完成');
}

// 2. 有筛选 + 空数据 → 筛选无结果空态 + 清除入口
test_emptyWithFilterShowsClearEntry();
function test_emptyWithFilterShowsClearEntry() {
  const { html } = runHarness(function () {
    resetFilters();
    _runtimeTab = 'engines';
    _rtFilterType = 'opencode';
    _renderRuntimesSkeleton({ runtimes: [], devices: [], total: 0 });
    window.__capture = __capture;
  });
  assert.ok(html.includes(ZH.empty.no_result_title), '应渲染「筛选无结果」标题');
  assert.ok(html.includes(ZH.empty.no_result_sub), '应渲染「筛选无结果」说明');
  assert.ok(html.includes('_rtClearFilters()'), '应提供复用 _rtClearFilters 的清除入口');
  assert.ok(html.includes(ZH.empty.clear_filters), '清除入口应有文案');
  assert.ok(!html.includes(ZH.empty.no_engines_sub), '筛选无结果时不应误报「组织无引擎」');
  assert.ok(!html.includes('_rtMaybePulseAddBtn'), '筛选无结果时不应触发添加设备脉冲引导');
}

// 3. 无筛选 + 空数据 → 保持原空态引导
test_emptyWithoutFilterKeepsOrgGuide();
function test_emptyWithoutFilterKeepsOrgGuide() {
  const { html } = runHarness(function () {
    resetFilters();
    _runtimeTab = 'engines';
    _renderRuntimesSkeleton({ runtimes: [], devices: [], total: 0 });
    window.__capture = __capture;
  });
  assert.ok(html.includes(ZH.empty.no_engines_title), '应保留「暂无引擎」标题');
  assert.ok(html.includes(ZH.empty.no_engines_sub), '应保留「组织无引擎」说明');
  assert.ok(!html.includes(ZH.empty.no_result_title), '无筛选空态不应出现筛选无结果文案');
  assert.ok(!html.includes('_rtClearFilters()'), '无筛选空态不应出现清除入口');
}

// 4. locales 键完整且一致
testLocaleKeys();
function testLocaleKeys() {
  const keys = ['no_result_title', 'no_result_sub', 'clear_filters'];
  for (const k of keys) {
    assert.strictEqual(typeof ZH.empty[k], 'string', `zh empty.${k} 应为字符串`);
    assert.strictEqual(typeof EN.empty[k], 'string', `en empty.${k} 应为字符串`);
    assert.ok(ZH.empty[k].length > 0, `zh empty.${k} 不应为空`);
    assert.ok(EN.empty[k].length > 0, `en empty.${k} 不应为空`);
  }
  assert.notStrictEqual(ZH.empty.no_result_sub, ZH.empty.no_engines_sub, '两种空态文案语义应区分');
}

console.log('✅ Issue #1167 运行时筛选空态测试全部通过');