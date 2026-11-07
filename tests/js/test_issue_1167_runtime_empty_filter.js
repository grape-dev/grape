/*
 * Issue #1167: Agent 运行时页筛选命中 0 条时筛选栏/✕ 清除消失 — 前端测试
 *
 * 运行：node tests/js/test_issue_1167_runtime_empty_filter.js
 *
 * 验证点（按 2026-11-06 用户反馈：Engines 筛选无结果空态对齐 Worker 页风格）：
 *   1. _rtHasActiveFilters 能识别全部筛选态（范围/类型/场景/标签/搜索）
 *   2. 有筛选且数据为空时走完整渲染：搜索框与筛选栏保留，列表区内联「暂无引擎」，
 *      筛选栏自带 ✕ 清除（复用 _rtClearFilters），不渲染「组织无引擎」引导
 *   3. 无筛选且数据为空时，保持原有「组织无引擎」引导 + 添加设备按钮
 *   4. 骨架中间帧（场景标签缓存未就绪）在有筛选时不渲染「组织无引擎」引导
 *   5. zh/en locales 复用 no_engines，不再含 no_result_* / clear_filters 冗余键
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

const EMPTY_RUNTIME_PAYLOAD = {
  runtimes: [], devices: [], total: 0,
  avail_types: ['opencode'], avail_scopes: ['local', 'user', 'repo', 'global'],
  avail_scenes: [], avail_tags: [], health_stats: null,
};

function buildHarness() {
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
    // /settings/agent-runtimes 返回空数据；其余端点返回 {}（如 engine-onboarding 探测）
    API: {
      get: (url) => Promise.resolve(
        String(url).indexOf('/settings/agent-runtimes') === 0 ? EMPTY_RUNTIME_PAYLOAD : {}
      ),
      post: () => Promise.resolve({}),
    },
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
    // worker-name.js 共享模块在页面脚本中独立加载，测试沙箱内以桩替代
    workerDisplayName: (d) => (d && (d.name || d.worker_name)) || '',
  };
  sandbox.window = sandbox;
  const resetFn = 'function resetFilters(){_rtFilterScope="";_rtFilterType="";_rtFilterScene=[];_rtFilterTag=[];_runtimeSearch="";_rtRange="all";}\n';
  const source = RUNTIMES_JS + '\n;(function(){' + resetFn + '(';
  vm.createContext(sandbox);
  return { sandbox, pageContent, sourcePrefix: source, sourceSuffix: ')();})();' };
}

// 同步执行（骨架渲染等同步路径）
function runHarness(harnessBody) {
  const h = buildHarness();
  vm.runInContext(h.sourcePrefix + harnessBody + h.sourceSuffix, h.sandbox, {
    filename: 'runtimes.js + harness',
  });
  return { sandbox: h.sandbox, html: h.pageContent.innerHTML };
}

// 异步执行（_loadRuntimesData 走 API.get().then 完整渲染路径）
async function runHarnessAsync(harnessBody) {
  const h = buildHarness();
  vm.runInContext(h.sourcePrefix + harnessBody + h.sourceSuffix, h.sandbox, {
    filename: 'runtimes.js + harness (async)',
  });
  await new Promise((r) => setImmediate(r));
  return { sandbox: h.sandbox, html: h.pageContent.innerHTML };
}

// 1. 筛选态识别
test_hasActiveFilters();
function test_hasActiveFilters() {
  assert.ok(/function _rtHasActiveFilters\(/.test(RUNTIMES_JS), '应保留 _rtHasActiveFilters 函数');
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

// 2. 有筛选 + 空数据 → 完整渲染：搜索框/筛选栏保留，内联「暂无引擎」，✕ 清除可达
test_emptyWithFilterRendersFullPage();
async function test_emptyWithFilterRendersFullPage() {
  const { html } = await runHarnessAsync(function () {
    resetFilters();
    window._currentView = 'runtimes';  // _loadRuntimesData 回调按当前视图门控
    __sceneTagsCache = [];            // 预热场景标签缓存，走完整渲染同步分支
    _runtimeTab = 'engines';
    _rtFilterType = 'opencode';
    _loadRuntimesData();
  });
  assert.ok(html.includes('runtimeSearchInput'), '应保留搜索框（Worker 页对齐）');
  assert.ok(html.includes('✕ 清除'), '筛选栏应保留 ✕ 清除入口');
  assert.ok(html.includes('_rtClearFilters()'), '清除入口应复用 _rtClearFilters');
  assert.ok(html.includes(ZH.empty.no_engines), '列表区应内联展示「暂无引擎」');
  assert.ok(!html.includes(ZH.empty.no_engines_sub), '筛选无结果时不应渲染「组织无引擎」引导');
  assert.ok(!html.includes(ZH.empty.worker_guide_title), '筛选无结果时不应渲染接入引导');
}

// 3. 无筛选 + 空数据 → 保持原「组织无引擎」引导
test_emptyWithoutFilterKeepsOrgGuide();
async function test_emptyWithoutFilterKeepsOrgGuide() {
  const { html } = await runHarnessAsync(function () {
    resetFilters();
    window._currentView = 'runtimes';  // _loadRuntimesData 回调按当前视图门控
    _runtimeTab = 'engines';
    _loadRuntimesData();
  });
  assert.ok(html.includes(ZH.empty.no_engines_title), '应保留「暂无引擎」标题');
  assert.ok(html.includes(ZH.empty.no_engines_sub), '应保留「组织无引擎」说明');
  assert.ok(!html.includes('runtimeSearchInput'), '真空态引导保持骨架渲染（无搜索框）');
  assert.ok(!html.includes('✕ 清除'), '无筛选时不应出现清除入口');
}

// 4. 骨架中间帧（场景标签缓存未就绪）+ 有筛选 → 工具栏 + 加载占位，不出组织引导
test_skeletonFilteredIntermediateFrame();
function test_skeletonFilteredIntermediateFrame() {
  const { html } = runHarness(function () {
    resetFilters();
    _runtimeTab = 'engines';
    _rtFilterType = 'opencode';
    _renderRuntimesSkeleton({ runtimes: [], devices: [], total: 0 });
  });
  assert.ok(html.includes('class="loading"'), '中间帧应渲染加载占位');
  assert.ok(!html.includes(ZH.empty.no_engines_sub), '有筛选时骨架不应渲染「组织无引擎」引导');
}

// 5. locales：复用 no_engines，移除冗余键
testLocaleKeys();
function testLocaleKeys() {
  for (const [name, locale] of [['zh', ZH], ['en', EN]]) {
    assert.strictEqual(typeof locale.empty.no_engines, 'string', `${name} empty.no_engines 应为字符串`);
    assert.ok(locale.empty.no_engines.length > 0, `${name} empty.no_engines 不应为空`);
    assert.strictEqual(typeof locale.empty.no_workers, 'string', `${name} empty.no_workers 应为字符串（Worker 页对称）`);
    for (const k of ['no_result_title', 'no_result_sub', 'clear_filters']) {
      assert.strictEqual(locale.empty[k], undefined, `${name} empty.${k} 冗余键应已移除`);
    }
  }
  assert.notStrictEqual(ZH.empty.no_engines, ZH.empty.no_workers, 'Engines 与 Workers 空态文案应各自独立');
}

console.log('✅ Issue #1167 运行时筛选空态测试全部通过');
