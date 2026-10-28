// 引擎引导简化形态 + 横幅退场 + 排队 chip（Q1-Q12）
// 运行：node tests/js/test_engine_guide_simplified.js
// 对象：本轮简化后的前端形态（聚光引导已删、横幅+脉冲+自动开面板、
// 侧栏进入引擎页横幅退场、密码条排队 chip）。源码结构守卫 + i18n 对齐。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..', '..');

const appSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/app.js'), 'utf-8');
const rtSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/runtimes.js'), 'utf-8');
function loadLocale(lang, mod) {
  return JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales', lang, mod + '.json'), 'utf-8'));
}
function lookup(pack, key) {
  let cur = pack;
  for (const p of key.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[p];
  }
  return cur;
}

let pass = 0;
function ok(cond, name) {
  assert.ok(cond, name);
  pass++;
  console.log('  ✓ ' + name);
}

// ── Q1: 聚光引导机制已删（无遮罩/状态机/持久跳过键/箭头气泡）──
{
  ok(!rtSrc.includes('function _rtSpotlight'), 'Q1: _rtSpotlight 函数已删');
  ok(!rtSrc.includes('rtGuideMask') && !rtSrc.includes('rtGuideTip'), 'Q1b: 遮罩/气泡元素已删');
  ok(!rtSrc.includes('rt_guide_done_'), 'Q1c: 聚光持久跳过键已删（无跨设备状态残留）');
  ok(!rtSrc.includes('rtTipBounce'), 'Q1d: 箭头弹跳 keyframes 已删');
  // 脉冲 keyframes 保留（非打断形态），但仅 box-shadow
  const kfIdx = rtSrc.indexOf('@keyframes rtGuidePulse');
  const kfEnd = rtSrc.indexOf('@media', kfIdx);
  const kf = kfIdx >= 0 ? rtSrc.slice(kfIdx, kfEnd > 0 ? kfEnd : kfIdx + 300) : '';
  ok(kfIdx >= 0 && /box-shadow/.test(kf) && !/\btransform\b/.test(kf), 'Q1e: 脉冲保留且仅 box-shadow');
  ok(rtSrc.includes("(prefers-reduced-motion: reduce){.rt-guide-pulse{animation:none}}"), 'Q1f: reduced-motion 停用脉冲');
}

// ── Q2: 简化版指引（needs_worker 门控的按钮脉冲，会话级）──
{
  const fn = rtSrc.slice(rtSrc.indexOf('window._rtMaybePulseAddBtn'), rtSrc.indexOf('function _addDeviceBtnHtml'));
  ok(fn.includes("d.needs_worker") && fn.includes("rt-guide-pulse"), 'Q2: needs_worker 触发按钮脉冲');
  ok(fn.includes("_rtPulseDone"), 'Q2b: 会话级去重（点开面板后不再加回）');
  ok(fn.includes("_currentView !== 'runtimes' || _runtimeTab !== 'engines'"), 'Q2c: 仅引擎页 engines 标签');
  ok(!fn.includes('localStorage'), 'Q2d: 无持久记忆（会话变量，刷新即重置——与退场靠真实状态一致）');
}

// ── Q3: 横幅跳转直达（自动开面板）──
{
  ok(appSrc.includes("window._rtOpenPanelOnEnter = true"), 'Q3: _gotoEnginePage 设定自动开面板标记');
  ok(rtSrc.includes("if (window._rtOpenPanelOnEnter)") && rtSrc.includes("openAddDevicePanel()"), 'Q3b: 渲染完成后消费标记展开面板');
  ok(appSrc.includes("_dismissEngineBanner();\n    window._rtOpenPanelOnEnter = true"), 'Q3c: 跳转前先撤横幅');
}

// ── Q4: 侧栏菜单进引擎页横幅退场（与横幅按钮跳转行为一致）──
{
  const navFn = appSrc.slice(appSrc.indexOf('function nav(view, param)'), appSrc.indexOf('function nav(view, param)') + 800);
  ok(navFn.includes("if (view === 'runtimes') _dismissEngineBanner()"), 'Q4: nav(runtimes) 统一退场在场横幅');
}

// ── Q5: 密码条排队可见（抑制时挂 chip，处理完自动带走+补位）──
{
  const fn = appSrc.slice(appSrc.indexOf('function _checkEngineBanner'), appSrc.indexOf('function _dismissEngineBanner'));
  ok(fn.includes('engineBannerQueued'), 'Q5: 抑制分支挂排队 chip');
  ok(fn.includes("I18N.t('app.login.engine_banner_queued_hint')"), 'Q5b: chip 文案走 i18n');
  ok(appSrc.includes("setTimeout(function() { _checkEngineBanner(); }, 100)"), 'Q5c: 密码条退出后补位检查');
}

// ── Q6: i18n 对齐（zh/en）──
{
  const zh = { app: loadLocale('zh', 'app') }, en = { app: loadLocale('en', 'app') };
  ok(typeof lookup(zh, 'app.login.ob_engine_banner_hint') === 'string' && lookup(zh, 'app.login.ob_engine_banner_hint').length <= 50, 'Q6: zh 横幅单句简短');
  ok(lookup(zh, 'app.login.ob_engine_banner_hint') === lookup(en, 'app.login.ob_engine_banner_hint') || typeof lookup(en, 'app.login.ob_engine_banner_hint') === 'string', 'Q6b: en 对齐');
  ok(typeof lookup(zh, 'app.login.engine_banner_queued_hint') === 'string', 'Q6c: zh 排队 chip 键');
  ok(typeof lookup(en, 'app.login.engine_banner_queued_hint') === 'string', 'Q6d: en 排队 chip 键');
  ok(lookup(zh, 'app.login.ob_engine_banner_hint_sub') === undefined, 'Q6e: 两级文案次句键已删（回归单句）');
  const rz = { runtimes: loadLocale('zh', 'runtimes') };
  ok(lookup(rz, 'runtimes.credential.intro_what') === undefined, 'Q6f: 面板 intro 键已删');
  ok(!('guide' in rz.runtimes) || !('spot_add_device' in (rz.runtimes.guide || {})), 'Q6g: 聚光 i18n 死键已删');
}

// ── Q7: 旧测试文件已删（对象不复存在）──
{
  ok(!fs.existsSync(path.join(ROOT, 'tests/js/test_engine_onboarding_guide.js')), 'Q7: 聚光引导测试已删');
  ok(!fs.existsSync(path.join(ROOT, 'tests/js/test_guide_visual.js')), 'Q7b: 聚光视觉测试已删');
  ok(!fs.existsSync(path.join(ROOT, 'tests/js/test_engine_cognitive_layer.js')), 'Q7c: 认知层测试已删（面板 intro 回退）');
}

// ── Q8: 安装命令 base_url 修复（上轮提交遗留守卫仍在）──
{
  const fn = rtSrc.slice(rtSrc.indexOf('function doCreateCredentialInPanel'));
  ok(fn.includes('grape_server_base') && fn.includes('server_base_url'), 'Q8: 面板命令经 health/缓存解析');
}

// ── Q9: 判定 SQL（后端，跨文件由 pytest 覆盖，此处锁前端不再引用已删符号）──
{
  ok(!appSrc.includes('_rtGuideMarkDone') && !appSrc.includes('_maybeShowWorkerGuide'), 'Q9: app.js 无已删引导符号残留');
  ok(!rtSrc.includes('window._rtGuideStage'), 'Q9b: 状态机变量已删');
}

// ── Q10: 非仓库视图不弹引擎横幅（上下文对齐，Issue：用户设置页弹"添加设备"）──
{
  const fn = appSrc.slice(appSrc.indexOf('function _checkEngineBanner'), appSrc.indexOf('function _dismissEngineBanner'));
  // 视图守卫：NO_REPO_VIEWS 单一事实源（含 account/settings/repos/tasks/event 等）
  // 整体排除，不再只排 runtimes——currentRepo() 是持久化"最近仓库"，不代表当前视图。
  ok(fn.includes("NO_REPO_VIEWS.indexOf(window._currentView) >= 0"), 'Q10: 非仓库视图守卫（NO_REPO_VIEWS 单一事实源）');
  ok(!fn.includes("_currentView === 'runtimes'"), 'Q10b: 旧的单视图守卫应已替换');
  // 单一事实源：清单字面量只允许出现在常量定义处一次，app.js 内无第二份拷贝
  const listLit = appSrc.split("['repos', 'settings', 'tasks', 'runtimes'").length - 1;
  ok(listLit === 1, 'Q10c: 非仓库视图清单字面量全文件仅 1 份（历史 4 份拷贝已收敛）');
}

// ── Q11: 导航进非仓库视图时隐藏在场横幅（隐藏≠跳过，不写 skip-key）──
{
  const i = appSrc.indexOf('window._currentView = view;');
  const navTail = appSrc.slice(i, i + 1000);
  ok(navTail.includes("_nonRepoViews.indexOf(view) >= 0"), 'Q11: nav 进入非仓库视图隐藏横幅');
  ok(navTail.includes("_hideEngineBannerOnly()"), 'Q11b: 隐藏走 _hideEngineBannerOnly（含 paddingTop 在场检查）');
  ok(navTail.includes("view !== 'runtimes'"), 'Q11c: runtimes 沿用既有 _dismissEngineBanner 语义');
  // 隐藏路径不得写 skip-key（否则"换个页面看看"变成永久跳过）
  const hideFn = navTail.slice(navTail.indexOf('if (view !== \'runtimes\''), navTail.indexOf('// 离开 repos'));
  ok(!hideFn.includes('localStorage'), 'Q11d: 隐藏不写 skip-key（返回仓库视图可重弹）');
  // 过期意图清理：改道去非仓库视图时作废"跳转自动开面板"标记
  ok(navTail.includes('window._rtOpenPanelOnEnter = false'), 'Q11e: nav 作废过期 _rtOpenPanelOnEnter 标记');
}

// ── Q11f: paddingTop 竞态双向闭合（两横幅共用 44px body padding）──
{
  const hideFn = appSrc.slice(appSrc.indexOf('function _hideEngineBannerOnly'), appSrc.indexOf('function _gotoEnginePage'));
  ok(hideFn.includes("document.getElementById('oauthCompletionBanner')"), 'Q11f: 隐藏引擎横幅时复位前查安全横幅在场');
  const dismissEng = appSrc.slice(appSrc.indexOf('function _dismissEngineBanner'), appSrc.indexOf('// 仅隐藏引擎横幅'));
  ok(dismissEng.includes("document.getElementById('oauthCompletionBanner')"), 'Q11g: 跳过引擎横幅时同样带在场检查');
  const dismissOb = appSrc.slice(appSrc.indexOf('function _dismissOAuthCompletion'), appSrc.indexOf('function _renderBindingGuide'));
  ok(dismissOb.includes("document.getElementById('engineBanner')"), 'Q11h: 安全横幅退出时对称查引擎横幅在场');
}

// ── Q11i: 双横幅互斥显式化（安全横幅显示前摘引擎横幅，不靠 z-index 遮挡）──
{
  const obShow = appSrc.slice(appSrc.indexOf('function _checkOAuthCompletion'), appSrc.indexOf('function _dismissOAuthCompletion'));
  ok(obShow.includes("document.getElementById('engineBanner')") && obShow.includes('_eb.remove()'), 'Q11i: oauth 横幅显示前显式摘 engineBanner');
}

// ── Q12: 返回仓库视图的补位链仍在（重弹不缺触发点）──
{
  ok(appSrc.includes('setTimeout(function() { _checkEngineBanner(); }, 100)'), 'Q12: 既有补位调用守卫在位');
  const dashSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/dashboard.js'), 'utf-8');
  ok(dashSrc.includes('app._checkEngineBanner()'), 'Q12b: 仓库工作台渲染时重查（返回 dash 即重弹）');
  // 零托管分支不再弹引擎引导（repos 项目大厅不超前引导接引擎）
  const lrFn = appSrc.slice(appSrc.indexOf('function renderReposFromData'), appSrc.indexOf('function renderRepoSelection'));
  ok(lrFn.includes('if (managedRepos.length > 0) _checkEngineBanner()'), 'Q12c: 零托管分支不调引擎引导（仅工作台分支调）');
}

console.log(`test_engine_guide_simplified: all assertions passed (Q1-Q12, ${pass} checks)`);
