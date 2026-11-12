/*
 * Issue #473 — 引擎/设备「范围筛选」（全部/仓库/个人）
 *
 * 2026-11-11 修复：该功能在 Vue 迁移时整块丢失（后端 range 参数一直在，前端不传），
 * 本次按旧 runtimes.js 规格补回。本用例断言「前端已接上 + 后端仍支持」：
 *   1. 前端有 rtRange 状态（默认 all）与三档选项（i18n rt_range_all/repo/user）
 *   2. engines 与 devices 两个 loader 都在非 all 时传 &range=
 *   3. chips 渲染 + 点击 setRange 触发重拉；清除筛选会重置 range
 *   4. zh/en locales 三档文案齐全
 *   5. 后端仍有 range 参数与 _rt_in_range 过滤
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/RuntimesView.vue'), 'utf8');
const locale = (lg) =>
  JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/frontend/legacy/locales', lg, 'runtimes.json'), 'utf8'));
const BACKEND = fs.readFileSync(path.join(ROOT, 'src/domains/mgmt/routes/settings.py'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('rtRange 状态默认 all + 三档选项', function () {
  assert.ok(/const rtRange = ref\('all'\)/.test(VIEW), '应有 rtRange 默认 all');
  assert.ok(/RT_RANGE_OPTIONS = \[/.test(VIEW), '应有范围选项表');
  for (const k of ['rt_range_all', 'rt_range_repo', 'rt_range_user']) {
    assert.ok(VIEW.indexOf(k) >= 0, '选项应使用 i18n 键 ' + k);
  }
});

check('两个 loader 非 all 时都传 range 参数', function () {
  const n = (VIEW.match(/qs \+= '&range=' \+ encodeURIComponent\(rtRange\.value\)/g) || []).length;
  assert.strictEqual(n, 2, 'engines 与 devices 两个请求都要带 range，实际 ' + n);
});

check('chips 渲染 + setRange 切档重拉 + clearFilters 重置', function () {
  assert.ok(/v-for="opt in RT_RANGE_OPTIONS"/.test(VIEW), '应渲染范围 chips');
  assert.ok(/@click="setRange\(opt\.key\)"/.test(VIEW), 'chip 点击应走 setRange');
  const i = VIEW.indexOf('function setRange(');
  assert.ok(i > 0, '应有 setRange');
  assert.ok(/resetPageAndReload\(\)/.test(VIEW.slice(i, i + 260)), '切档应重置分页并重拉');
  assert.ok(/function clearFilters\(\) \{\s*rtRange\.value = 'all'/.test(VIEW), '清除筛选应重置 range');
});

check('zh/en 三档文案齐全', function () {
  for (const lg of ['zh', 'en']) {
    const d = locale(lg);
    for (const k of ['rt_range_all', 'rt_range_repo', 'rt_range_user']) {
      assert.ok(d[k] && String(d[k]).trim().length, lg + ' 缺 ' + k);
    }
  }
});

check('后端仍有 range 参数与 _rt_in_range 过滤', function () {
  assert.ok(/def _rt_in_range\(/.test(BACKEND), '后端应有 _rt_in_range');
  assert.ok(/_rt_in_range\(/.test(BACKEND.slice(BACKEND.indexOf('def _rt_in_range(') + 200)), '应被实际调用');
});

console.log('\n#473 全部通过 (' + passed + '/5)');