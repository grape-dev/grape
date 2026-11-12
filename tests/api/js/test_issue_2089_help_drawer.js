/*
 * Issue #2089 回归测试：帮助抽屉全屏/还原 + 新标签页打开 + 窄容器自适应
 *
 * 运行：node tests/js/test_issue_2089_help_drawer.js
 *
 * 验证点（前端契约，静态断言）：
 *   1. index.html #helpDrawer 头部含全屏/还原按钮（#helpFullscreenBtn）与新标签页按钮（#helpNewTabBtn）
 *   2. app.js 暴露 toggleHelpFullscreen / openHelpNewTab（app 返回对象 + onclick 绑定）
 *   3. openHelp 仍只创建一次 iframe（全屏切换不重建 iframe → 阅读位置保留）
 *   4. app.css 含 .help-drawer.fullscreen 全屏态（width:100vw 铺满）
 *   5. i18n.js zh/en 均含 app.help.fullscreen / exit_fullscreen / open_new_tab 文案
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..', '..');

function readFile(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

let failed = 0;
function check(name, fn) {
  try {
    fn();
    console.log('  ✓ ' + name);
  } catch (e) {
    failed++;
    console.error('  ✗ ' + name + '\n    ' + e.message);
  }
}

console.log('# Issue 2089: help drawer fullscreen + new-tab + narrow adaptation');

const indexHtml = readFile('src/api/static/index.html');
const appJs = readFile('src/api/static/js/app.js');
const appCss = readFile('src/api/static/css/app.css');
const i18nJs = readFile('src/api/static/js/i18n.js');

// 1. index.html 按钮组
check('index.html: 头部含全屏/还原按钮 #helpFullscreenBtn', () => {
  assert.ok(/id="helpFullscreenBtn"/.test(indexHtml), 'missing #helpFullscreenBtn');
});
check('index.html: 头部含新标签页按钮 #helpNewTabBtn', () => {
  assert.ok(/id="helpNewTabBtn"/.test(indexHtml), 'missing #helpNewTabBtn');
});
check('index.html: 按钮位于 .help-drawer-hdr 内', () => {
  const hdr = indexHtml.match(/<div class="help-drawer-hdr">[\s\S]*?<\/div>/);
  assert.ok(hdr, 'missing .help-drawer-hdr');
  assert.ok(/helpFullscreenBtn/.test(hdr[0]) && /helpNewTabBtn/.test(hdr[0]), 'buttons not in header');
});
check('index.html: 关闭按钮 onclick="app.closeHelp()" 保持', () => {
  assert.ok(/class="help-drawer-close" onclick="app\.closeHelp\(\)"/.test(indexHtml), 'close button regression');
});

// 2. app.js 行为
check('app.js: 导出 toggleHelpFullscreen', () => {
  assert.ok(/toggleHelpFullscreen/.test(appJs), 'toggleHelpFullscreen missing');
});
check('app.js: 导出 openHelpNewTab', () => {
  assert.ok(/openHelpNewTab/.test(appJs), 'openHelpNewTab missing');
});
check('app.js: toggle 切换 fullscreen 类而非重建 iframe', () => {
  const fn = appJs.match(/function toggleHelpFullscreen\(\)[\s\S]*?\n  \}/);
  assert.ok(fn, 'function toggleHelpFullscreen not found');
  assert.ok(/classList\.toggle\('fullscreen'\)/.test(fn[0]), 'should toggle CSS class, not rebuild iframe');
  assert.ok(!/innerHTML/.test(fn[0]), 'toggle must not touch body.innerHTML (would lose scroll position)');
});
check('app.js: openHelpNewTab 以 _blank 打开 /static/help.html 且带 noopener', () => {
  const fn = appJs.match(/function openHelpNewTab\(\)[\s\S]*?\n  \}/);
  assert.ok(fn, 'function openHelpNewTab not found');
  assert.ok(/window\.open\('\/static\/help\.html'[^)]*noopener/.test(fn[0]), 'should window.open /static/help.html with noopener');
});
check('app.js: openHelp 仍只创建一次 iframe（!body.querySelector(\'iframe\') 守卫）', () => {
  const fn = appJs.match(/function openHelp\(\)[\s\S]*?\n  \}/);
  assert.ok(fn, 'function openHelp not found');
  assert.ok(/querySelector\('iframe'\)/.test(fn[0]), 'iframe created-once guard missing');
});
check('app.js: toggleHelpFullscreen / openHelpNewTab 在 return 导出表中', () => {
  const ret = appJs.match(/return \{[\s\S]*\};/);
  assert.ok(ret, 'app return object not found');
  assert.ok(/\btoggleHelpFullscreen\b/.test(ret[0]), 'toggleHelpFullscreen not exported');
  assert.ok(/\bopenHelpNewTab\b/.test(ret[0]), 'openHelpNewTab not exported');
});

// 3. app.css 全屏态
check('app.css: .help-drawer.fullscreen 存在', () => {
  assert.ok(/\.help-drawer\.fullscreen\s*\{/.test(appCss), 'missing .help-drawer.fullscreen rule');
});
check('app.css: 全屏态 width:100vw 铺满（1440px 视口内容区 ≥1200px）', () => {
  const rule = appCss.match(/\.help-drawer\.fullscreen\s*\{[^}]*\}/);
  assert.ok(rule, 'fullscreen rule not found');
  assert.ok(/width:100vw/.test(rule[0]), 'fullscreen must be width:100vw');
});
check('app.css: 原抽屉宽度 520px 保留（还原后仍为原宽度）', () => {
  assert.ok(/\.help-drawer\{[^}]*width:520px/.test(appCss), 'base .help-drawer width:520px lost');
});
check('app.css: 头部按钮组 .help-drawer-hdr-actions 存在', () => {
  assert.ok(/\.help-drawer-hdr-actions\s*\{/.test(appCss), 'missing .help-drawer-hdr-actions style');
});

// 4. i18n
check('i18n.js: zh 含 fullscreen / exit_fullscreen / open_new_tab', () => {
  assert.ok(/fullscreen:\s*'全屏'/.test(i18nJs), 'zh fullscreen missing');
  assert.ok(/exit_fullscreen:\s*'退出全屏'/.test(i18nJs), 'zh exit_fullscreen missing');
  assert.ok(/open_new_tab:\s*'在新标签页打开'/.test(i18nJs), 'zh open_new_tab missing');
});
check('i18n.js: en 含 fullscreen / exit_fullscreen / open_new_tab', () => {
  assert.ok(/fullscreen:\s*'Fullscreen'/.test(i18nJs), 'en fullscreen missing');
  assert.ok(/exit_fullscreen:\s*'Exit fullscreen'/.test(i18nJs), 'en exit_fullscreen missing');
  assert.ok(/open_new_tab:\s*'Open in new tab'/.test(i18nJs), 'en open_new_tab missing');
});

// 5. 关闭行为与全屏态交互（Spec 风险表：关闭前先退出全屏态）
check('app.js: closeHelp 先退出全屏态（移除 fullscreen 类）再隐藏抽屉', () => {
  const fn = appJs.match(/function closeHelp\(\)[\s\S]*?\n  \}/);
  assert.ok(fn, 'function closeHelp not found');
  assert.ok(/classList\.remove\('fullscreen'\)/.test(fn[0]), 'closeHelp must remove fullscreen class');
  assert.ok(fn[0].indexOf("remove('fullscreen')") < fn[0].indexOf("classList.remove('show')"),
    'fullscreen removal must happen before/with hide (avoid visible closed drawer)');
});
check('app.css: .help-drawer.fullscreen 不含 transform:translateX(0)（关闭态依赖 transform:100% 隐藏，全屏规则不得覆盖为可见）', () => {
  const rule = appCss.match(/\.help-drawer\.fullscreen\s*\{[^}]*\}/);
  assert.ok(rule, 'fullscreen rule not found');
  assert.ok(!/transform\s*:\s*translateX\(0\)/.test(rule[0]), 'fullscreen rule must not pin translateX(0)');
});
check('i18n 双源同步：locales zh/en app.json 含三个新 key', () => {
  const zhApp = JSON.parse(readFile('src/api/static/locales/zh/app.json'));
  const enApp = JSON.parse(readFile('src/api/static/locales/en/app.json'));
  for (const k of ['fullscreen', 'exit_fullscreen', 'open_new_tab']) {
    assert.ok(zhApp.help && zhApp.help[k], 'zh app.json help.' + k + ' missing');
    assert.ok(enApp.help && enApp.help[k], 'en app.json help.' + k + ' missing');
  }
});

if (failed) {
  console.error('\nFAILED: ' + failed + ' check(s)');
  process.exit(1);
}
console.log('\nAll checks passed.');
