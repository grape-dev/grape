/*
 * 市场场景应用后详情页不再显示「场景不存在」（源码级断言）
 *
 * 迁移说明：原测试断言 legacy app.js 的 renderSceneDetailLocal / _navAfterApply 内部形态；
 * Vue 迁移后语义等价实现为：详情恒走 API、应用后串行切仓库再 nav 到新场景详情。
 *   1. 详情数据恒走 API（GET /scenes/market/{id}），无本地快照捷径
 *   2. 应用成功后按返回的 scene_id 导航到 scene-detail-local
 *   3. 跨仓库时先 selectRepoAndNav(...).then(再 nav)，不并发（避免拿到旧仓库数据）
 *   4. 无 scene_id（后端只回消息）时回 dash，不硬跳详情
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/SceneMarket.vue'), 'utf8');

function fnBody(name) {
  const i = VIEW.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未找到 ' + name);
  let d = 0,
    started = false;
  for (let j = i; j < VIEW.length; j++) {
    if (VIEW[j] === '{') {
      d++;
      started = true;
    } else if (VIEW[j] === '}') {
      d--;
      if (started && d === 0) return VIEW.slice(i, j + 1);
    }
  }
  throw new Error(name + ' 括号不闭合');
}

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('详情恒走 API，无本地快照捷径', function () {
  const b = fnBody('loadDetailData');
  assert.ok(/G\.API\.get\('\/scenes\/market\/' \+ id\)/.test(b), '应按 id 拉详情');
  assert.ok(!/Promise\.resolve\(\s*data\s*\)/.test(VIEW), '不应存在复用旧数据的快照捷径');
});

check('应用成功按新 scene_id 进详情', function () {
  const b = fnBody('doApplyScene');
  assert.ok(/r\.scene_id/.test(b), '应读取后端返回的 scene_id');
  assert.ok(/G\.app\.nav\('scene-detail-local', sceneIdLocal\)/.test(b), '应导航到新场景详情');
});

check('跨仓库串行：先切仓库再 nav', function () {
  const b = fnBody('doApplyScene');
  const i = b.indexOf('selectRepoAndNav(repoFull).then(');
  assert.ok(i > 0, '应先切仓库');
  assert.ok(
    b.indexOf("G.app.nav('scene-detail-local', sceneIdLocal)", i) > i,
    'nav 必须在切仓库的 then 里（串行）',
  );
});

check('无 scene_id 回工作台', function () {
  const b = fnBody('doApplyScene');
  const i = b.indexOf('if (!sceneIdLocal)');
  assert.ok(i > 0, '应有兜底分支');
  assert.ok(/G\.app\.nav\('dash'\)/.test(b.slice(i, i + 260)), '无 id 应回 dash');
});

console.log('\nscene_apply 全部通过 (' + passed + '/4)');