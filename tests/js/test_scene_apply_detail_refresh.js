/**
 * 市场场景应用后详情页「场景不存在」修复 — 前端回归测试（scene-apply-stale-detail-fix-design.md）。
 *
 * 运行：node tests/js/test_scene_apply_detail_refresh.js
 *
 * 根因：renderSceneDetailLocal 复用陈旧 window._dashboardData（data.agents ? Promise.resolve(data)）
 * 且 `dashData._scenes ||` 短路吞掉新鲜 scenesData；叠加 _navAfterApply 内异步切仓库未被 await 就 nav。
 * 验证点：
 *   1. 详情页数据恒走 API.get('/dashboard/…')（去快照捷径）。
 *   2. 场景判定 fresh（scenesData.scenes）优先、dashData._scenes 仅兜底。
 *   3. _navAfterApply 跨仓库分支串行：先切仓库再 app.nav('scene-detail-local')。
 *   4. _selectRepoAndNav 返回 Promise。
 *   5. 两文件语法完整。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
// 归一化 CRLF，避免 \r 干扰子串断言（源文件为 Windows 行尾）
const dashSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/dashboard.js'), 'utf8').replace(/\r/g, '');
const marketSrc = fs.readFileSync(path.join(ROOT, 'src/api/static/js/pages/scene-market.js'), 'utf8').replace(/\r/g, '');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('场景应用后详情页「场景不存在」修复 — 回归测试\n');

function fnBody(src, marker, len) {
  const idx = src.indexOf(marker);
  if (idx < 0) throw new Error(`未找到 ${marker}`);
  return src.substring(idx, idx + (len || 6000));
}

// ─── 测试 1：详情页数据恒走 /dashboard ───
test('renderSceneDetailLocal 无 Promise.resolve(data) 快照捷径', () => {
  const body = fnBody(dashSrc, 'function renderSceneDetailLocal', 3000);
  assert.ok(body.indexOf('data.agents ? Promise.resolve(data)') === -1,
    '不应再复用 window._dashboardData 快照（跳过请求）');
  assert.ok(body.indexOf("API.get('/dashboard/' + owner + '/' + name)") >= 0,
    '应恒请求 /dashboard/{owner}/{name} 权威数据');
});

// ─── 测试 2：场景判定 fresh 优先 ───
test('场景判定 fresh（scenesData.scenes）优先、_scenes 兜底', () => {
  const body = fnBody(dashSrc, 'function renderSceneDetailLocal', 3000);
  // 旧问题写法消失：dashData._scenes || (scenesData.scenes …)
  assert.ok(body.indexOf('dashData._scenes || (scenesData.scenes') === -1,
    '不应再让陈旧 _scenes 短路掉新鲜 scenesData');
  // 新写法：scenesData.scenes 有值优先，dashData._scenes 作兜底
  assert.ok(body.indexOf('scenesData.scenes && scenesData.scenes.length') >= 0,
    '应以 scenesData.scenes 非空为条件优先采用');
  // 顺序断言限定在 const scenes 赋值片段内（避免注释文本干扰）
  const assign = fnBody(body, 'const scenes = (', 600);
  const iFresh = assign.indexOf('scenesData.scenes');
  const iStale = assign.indexOf('dashData._scenes');
  assert.ok(iFresh >= 0 && iStale >= 0 && iFresh < iStale,
    'const scenes 赋值内 scenesData.scenes 应出现在 dashData._scenes 之前（fresh 优先）');
});

// ─── 测试 3：应用后跨仓库导航串行 ───
test('_navAfterApply 跨仓库先切仓库再进详情（.then 串行）', () => {
  const body = fnBody(marketSrc, 'var _navAfterApply');
  const iSel = body.indexOf('_selectRepoAndNav(repoFull');
  const iNav = body.indexOf("app.nav('scene-detail-local', sceneIdLocal);");
  assert.ok(iSel >= 0, '跨仓库分支应调用 _selectRepoAndNav');
  assert.ok(iNav >= 0, '应跳转场景详情页');
  const seg = body.substring(iSel, iNav + 60);
  assert.ok(seg.indexOf('.then') >= 0, 'nav 应在 _selectRepoAndNav(...).then(...) 内执行，避免切仓库未完成就渲染');
  // 同仓库分支 else 直接 nav（无多余等待），空白容忍
  assert.ok(/\}\s*else\s*\{\s*app\.nav\('scene-detail-local', sceneIdLocal\);/.test(body),
    '同仓库分支应 else 直接 nav');
});

// ─── 测试 4：_selectRepoAndNav 返回 Promise ───
test('_selectRepoAndNav 返回 Promise（return API.get(/repos/managed).then）', () => {
  const body = fnBody(marketSrc, 'function _selectRepoAndNav', 1200);
  assert.ok(body.indexOf("return API.get('/repos/managed').then") >= 0,
    '_selectRepoAndNav 应 return 异步链（可被调用方 .then 等待）');
  assert.ok(body.indexOf('app.switchRepo(fullName)') >= 0, '切换仓库逻辑保留');
});

// ─── 测试 5：语法完整 ───
test('dashboard.js / scene-market.js 语法完整（vm.Script 解析通过）', () => {
  const vm = require('vm');
  new vm.Script(dashSrc, { filename: 'dashboard.js' });
  new vm.Script(marketSrc, { filename: 'scene-market.js' });
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);