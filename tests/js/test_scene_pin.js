/**
 * Issue #1735: 项目场景列表「置顶」前端静态断言。
 *
 * 运行：node tests/js/test_scene_pin.js
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const dashSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/dashboard.js'), 'utf8').replace(/\r/g, '');
const indexHtml = fs.readFileSync(path.join(ROOT, 'src/api/static/index.html'), 'utf8');
const zhJson = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/dashboard.json'), 'utf8'));
const enJson = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/dashboard.json'), 'utf8'));

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

function cardSection() {
  const start = dashSrc.indexOf('function _renderLocalSceneCard');
  assert(start >= 0, '_renderLocalSceneCard 未找到');
  const end = dashSrc.indexOf('\n// ── 滚动按钮显隐', start);
  assert(end > start, '_renderLocalSceneCard 边界未找到');
  return dashSrc.slice(start, end);
}

console.log('Issue #1735 场景列表置顶 — 前端静态断言\n');

test('dashboard.js 可被 new Function 解析', () => {
  new Function(dashSrc);
});

test('置顶按钮 onclick 内包含 stopPropagation', () => {
  const section = cardSection();
  const idx = section.indexOf('_toggleScenePin');
  assert(idx >= 0, '置顶按钮未调用 _toggleScenePin');
  const onclick = section.slice(section.lastIndexOf('onclick=', idx), idx);
  assert(onclick.includes('event.stopPropagation()'), '置顶按钮 onclick 缺少 event.stopPropagation()');
});

test('_toggleScenePin 存在并调用 /pin 端点', () => {
  assert(dashSrc.includes('window._toggleScenePin = function'), '缺少 window._toggleScenePin');
  const start = dashSrc.indexOf('window._toggleScenePin = function');
  const end = dashSrc.indexOf('\n};', start);
  const body = dashSrc.slice(start, end);
  assert(body.includes("'/pin?repo='") || body.includes('"/pin?repo="') || body.includes("+ '/pin?repo='"),
    '_toggleScenePin 未调用 /pin 端点');
  assert(body.includes('_sceneReload'), '成功后应调用 _sceneReload 局部刷新');
  assert(!body.includes('.then(function() {\n      s.is_pinned'), '不应做乐观更新');
});

test('_renderLocalSceneCard 出现 is_pinned（徽章 + 按钮）', () => {
  const section = cardSection();
  const hits = (section.match(/is_pinned/g) || []).length;
  assert(hits >= 2, `is_pinned 在卡片渲染中出现 ${hits} 次，应至少 2 次（徽章+按钮）`);
  assert(section.includes('pinnedBadge'), '缺少置顶药丸徽章');
  assert(section.includes('is-pinned'), '缺少置顶行/按钮样式类');
});

test('index.html dashboard.js 缓存版本已递增（>=113）', () => {
  const m = indexHtml.match(/dashboard\.js\?v=(\d+)/);
  assert(m, '未找到 dashboard.js 版本号');
  assert(parseInt(m[1], 10) >= 113, `dashboard.js 版本应 >=113，实际 ${m[1]}`);
});

test('zh/en 四个 i18n 键齐全且 zh 为中文', () => {
  ['scene_pin', 'scene_pinned', 'scene_unpin', 'scene_pin_failed'].forEach(k => {
    assert(typeof zhJson[k] === 'string' && zhJson[k], 'zh 缺少 ' + k);
    assert(typeof enJson[k] === 'string' && enJson[k], 'en 缺少 ' + k);
  });
  assert(zhJson.scene_pin === '置顶', 'zh scene_pin 应为「置顶」');
  assert(zhJson.scene_pinned === '已置顶', 'zh scene_pinned 应为「已置顶」');
  assert(zhJson.scene_unpin === '取消置顶', 'zh scene_unpin 应为「取消置顶」');
});

console.log(`\n结果: ${passed} passed, ${failed} failed`);
if (failed) process.exit(1);