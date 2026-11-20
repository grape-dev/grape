/*
 * Issue #3169 — 插件市场前端四件套 · Slice 1：市场页视图 + 壳层四处接线（源码级断言）
 *
 * Seam（用户可见行为 → 壳层接线契约 + 列表 API 契约）：
 *   1. 侧边栏「插件市场」入口可见可点 → index.html 有 nav-plugins 锚点（app.nav('plugins')）
 *   2. 点击后视图 chunk 被加载 → index.html 加载 /static-vue/viewjs/plugins.js，
 *      app.js VIEWS 含 'plugins'、NO_REPO_VIEWS 含 'plugins'（无仓库也可浏览市场）、
 *      nav 分发存在 view === 'plugins' 分支并调 renderPlugins
 *   3. 构建产物接线 → vite.config.js input 含 plugins → entries/plugins.js、viewCssMap 含 plugins
 *   4. 视图契约 → PluginMarketView.vue 存在，列表消费 GET /api/plugins
 *      （q/category_id/sort/page/page_size/scope/repo 全参）+ GET /api/plugins/categories
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('PluginMarketView.vue 存在', function () {
  assert.ok(fs.existsSync(path.join(FE, 'app/src/views/PluginMarketView.vue')), '缺 PluginMarketView.vue');
});

check('入口 entries/plugins.js 挂 window.renderPlugins（老壳契约，fresh mount）', function () {
  const src = read('app/src/views/entries/plugins.js');
  assert.ok(/window\.renderPlugins\s*=/.test(src), '应挂 window.renderPlugins');
  assert.ok(/PluginMarketView\.vue/.test(src), '应 import PluginMarketView');
});

check('index.html 加载 viewjs/plugins.js', function () {
  const html = read('shell/index.html');
  assert.ok(/\/static-vue\/viewjs\/plugins\.js\?v=\d+/.test(html), '应加载 viewjs/plugins.js');
});

check('index.html 侧边栏有插件市场入口（对齐 skills：同分组、可点导航）', function () {
  const html = read('shell/index.html');
  assert.ok(/id="nav-plugins"/.test(html), '应有 nav-plugins 锚点');
  assert.ok(/app\.nav\('plugins'\)/.test(html), '点击应触发 nav plugins');
});

check('app.js VIEWS 注册表含 plugins（hash 路由 #plugins 可用）', function () {
  const js = read('shell/js/app.js');
  const m = js.match(/const VIEWS = \[([^\]]+)\]/);
  assert.ok(m, '未找到 VIEWS 常量');
  assert.ok(m[1].split(',').some((v) => v.trim().replace(/'/g, '') === 'plugins'), "VIEWS 应含 'plugins'");
});

check('app.js NO_REPO_VIEWS 含 plugins（无仓库上下文可进市场，repo scope 页内自行降级）', function () {
  const js = read('shell/js/app.js');
  const m = js.match(/const NO_REPO_VIEWS = \[([^\]]+)\]/);
  assert.ok(m, '未找到 NO_REPO_VIEWS 常量');
  assert.ok(m[1].split(',').some((v) => v.trim().replace(/'/g, '') === 'plugins'), "NO_REPO_VIEWS 应含 'plugins'");
});

check('app.js nav 渲染分发 view === plugins → renderPlugins', function () {
  const js = read('shell/js/app.js');
  assert.ok(/view === 'plugins'/.test(js), "应有 view === 'plugins' 分支");
  // #3395：前面新增了「plugins off 回退 dash」守卫，渲染分支需按内容定位（不按首个出现）
  const branch = js.match(/view === 'plugins'[\s\S]{0,400}renderPlugins/);
  assert.ok(branch, '分支内应调 renderPlugins');
});

check('vite.config.js input 注册 plugins 入口 + viewCssMap 有 plugins', function () {
  const cfg = read('app/vite.config.js');
  assert.ok(/['"]?plugins['"]?:\s*fileURLToPath\([^)]*entries\/plugins\.js/.test(cfg), 'input 应含 plugins → entries/plugins.js');
  assert.ok(/plugins:\s*\/\^.*\.css\$\/,/.test(cfg), 'viewCssMap 应含 plugins 条目');
});

check('PluginMarketView 列表契约：GET /api/plugins 带 q/category_id/sort/page/page_size/scope 参数', function () {
  const vue = read('app/src/views/PluginMarketView.vue');
  assert.ok(/API\.get\('\/plugins\?'|API\.get\('\/plugins'\+|"\/plugins\?"/.test(vue) || /API\.get\(\s*'\/plugins[^']*'/.test(vue), '应调 GET /plugins（API base=/api）');
  const loadBody = vue.slice(vue.indexOf('function loadPlugins'));
  for (const p of ['q=', 'category_id=', 'sort=', 'page=', 'page_size=', 'scope=']) {
    assert.ok(loadBody.includes(p), 'loadPlugins 应带参数 ' + p);
  }
  if (loadBody.indexOf('repo=') < 0) {
    assert.ok(/repo_key|currentRepo/.test(loadBody), 'repo scope 应带仓库上下文');
  }
});

check('PluginMarketView 分类契约：GET /api/plugins/categories', function () {
  const vue = read('app/src/views/PluginMarketView.vue');
  assert.ok(/\/plugins\/categories/.test(vue), '应调 GET /api/plugins/categories');
});

check('i18n 模块接线：MODULES 含 plugins，zh/en 语言包齐备', function () {
  const i18n = read('shell/js/i18n.js');
  assert.ok(/'plugins'/.test(i18n), "i18n MODULES 应含 'plugins'");
  assert.ok(fs.existsSync(path.join(FE, 'shell/locales/zh/plugins.json')), '缺 zh/plugins.json');
  assert.ok(fs.existsSync(path.join(FE, 'shell/locales/en/plugins.json')), '缺 en/plugins.json');
});

console.log('\n#3169 Slice 1 全部通过 (' + passed + '/11)');
