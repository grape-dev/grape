/*
 * Issue #3395 — 切片B：shell 侧边栏入口与 #plugins 深链守卫
 *
 * Seam（用户可见行为）：
 *   1. app.js 从服务端注入的 window.__FEATURES__ 判定 plugins 是否开启；
 *   2. VIEWS 路由表在 flag=0 时过滤掉 'plugins'（刷新恢复无效 hash 回退默认视图）；
 *   3. nav('plugins') 在 flag=0 时回退默认视图（浏览器前进/后退 popstate 兜底）；
 *   4. 侧边栏「🧩 插件市场」整块由服务端 flag=0 时摘除（Python 侧已断言），
 *      app.js 不再对已不存在的 nav-plugins 做硬依赖。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const SHELL = path.resolve(__dirname, '../../src/api/frontend/shell');
const APP = fs.readFileSync(path.join(SHELL, 'js/app.js'), 'utf8');
const INDEX = fs.readFileSync(path.join(SHELL, 'index.html'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('app.js 从 window.__FEATURES__ 读取 plugins 开关（不自行解析 env/cookie）', function () {
  assert.ok(/__FEATURES__/.test(APP), 'app.js 应读取 window.__FEATURES__');
  assert.ok(/plugins/.test(APP), '应针对 plugins 判定');
});

check('VIEWS 路由表在 flag=0 时过滤 plugins（#plugins 深链不恢复）', function () {
  const i = APP.indexOf('const VIEWS');
  assert.ok(i >= 0, '未找到 VIEWS 定义');
  const seg = APP.slice(i, i + 900);
  assert.ok(/'plugins'/.test(seg), 'VIEWS 基表仍含 plugins（flag=1 时可用）');
  assert.ok(/filter/.test(seg), 'VIEWS 应经 filter 按开关收敛');
});

check('nav() 在 plugins 关闭时回退默认视图（popstate 兜底）', function () {
  const i = APP.indexOf('function nav(view, param)');
  assert.ok(i >= 0, '未找到 nav 定义');
  const seg = APP.slice(i, i + 500);
  assert.ok(
    /view === 'plugins'[^]*?view = 'dash'|view = 'dash'[^]*?view === 'plugins'/.test(seg),
    "nav 应在 plugins off 时把 view 回退为 'dash'"
  );
});

check('index.html 仍保留 #plugins 视图 chunk 与 nav-plugins（flag=1 零变化）', function () {
  assert.ok(/id="nav-plugins"/.test(INDEX), 'flag=1 时侧边栏插件入口应在 HTML 中');
  assert.ok(/viewjs\/plugins\.js/.test(INDEX), '插件市场 chunk 应继续加载（仅显隐，不下线代码）');
});

console.log('\n#3395 shell 入口/深链守卫 全部通过 (' + passed + '/4)');