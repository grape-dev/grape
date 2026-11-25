/*
 * Issue #4260 — 技能详情缺 id 时按字面量请求 /api/skills/by-id/undefined
 *
 * 运行：node tests/js/test_issue_4260_skill_missing_param.js
 *
 * 验证点（前端路由层 / hash 生成侧 / 错误文案 / i18n / 缓存版本）：
 *   1. _isMissingSkillParam 仅把 空/undefined/null 判为缺参，合法 id 与名称深链原样透传。
 *   2. nav() 的 skill 分支在取数前守卫：命缺参 → 提示 + nav('skills') + return，不发请求。
 *   3. _pushHistory 对 skill 缺参改写为 #skills，从源头消除半截 hash。
 *   4. catch 对 e.status===400 使用参数错误文案，404 仍走「加载失败: 技能不存在」。
 *   5. zh/en 语言包均有 common.invalid_param；index.html 缓存版本已 bump。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const SHELL = path.join(ROOT, 'src/api/frontend/shell');
const appJs = fs.readFileSync(path.join(SHELL, 'js/app.js'), 'utf8');
const i18nJs = fs.readFileSync(path.join(SHELL, 'js/i18n.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(SHELL, 'index.html'), 'utf8');
const zhCommon = JSON.parse(fs.readFileSync(path.join(SHELL, 'locales/zh/common.json'), 'utf8'));
const enCommon = JSON.parse(fs.readFileSync(path.join(SHELL, 'locales/en/common.json'), 'utf8'));

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

// 从 app.js 抽取真实守卫函数，避免测试自造判定逻辑
function extractFn(name) {
  const re = new RegExp('function ' + name + '\\(param\\) \\{[\\s\\S]*?\\n  \\}');
  const m = re.exec(appJs);
  assert.ok(m, 'app.js 应定义 ' + name + '()');
  return eval('(' + m[0] + ')'); // eslint-disable-line no-eval
}

check('T1 缺参判定仅命中三个哨兵（合法 id/名称不误伤）', function () {
  const miss = extractFn('_isMissingSkillParam');
  [undefined, null, '', 'undefined', 'null'].forEach(function (v) {
    assert.strictEqual(miss(v), true, '应判为缺参: ' + String(v));
  });
  ['0', '38', '123', 'huawei-ecs', 'my-skill', 'skill.name', 'NULL'].forEach(function (v) {
    assert.strictEqual(miss(v), false, '不得判为缺参: ' + v);
  });
});

check('T2 nav skill 分支守卫位于取数之前且直接回退技能列表', function () {
  const GUARD = "if (view === 'skill' && _isMissingSkillParam(param)) {";
  const idxs = [];
  let at = appJs.indexOf(GUARD);
  while (at >= 0) { idxs.push(at); at = appJs.indexOf(GUARD, at + 1); }
  assert.ok(idxs.length >= 2, 'skill 缺参判定应在 nav 与 _pushHistory 同时生效');
  const apiIdx = appJs.indexOf("API.get('/skills/by-id/' + encodeURIComponent(param) + _qs)");
  assert.ok(apiIdx > 0, 'skill 分支应保留 by-id 取数');
  const guardIdx = idxs.find(function (i) {
    return /nav\('skills'\)/.test(appJs.slice(i, i + 400));
  });
  assert.ok(guardIdx !== undefined, 'nav 应有回退 nav(\'skills\') 的 skill 缺参守卫');
  assert.ok(guardIdx < apiIdx, '守卫必须在取数之前（防止先发 undefined 请求）');
  const block = appJs.slice(guardIdx, guardIdx + 400);
  assert.ok(/nav\('skills'\)/.test(block), '守卫应回退 nav(\'skills\')');
  assert.ok(/return;/.test(block), '守卫应 return，不继续渲染/取数');
  assert.ok(/invalid_param/.test(block), '守卫应给轻提示（参数错误文案）');
});

check('T3 _pushHistory 对 skill 缺参改写为 #skills（消除半截 hash）', function () {
  const fnIdx = appJs.indexOf('function _pushHistory(view, param) {');
  assert.ok(fnIdx >= 0, '应存在 _pushHistory');
  const body = appJs.slice(fnIdx, fnIdx + 700);
  assert.ok(/view === 'skill' && _isMissingSkillParam\(param\)/.test(body),
    '_pushHistory 应对 skill 缺参做判定');
  assert.ok(/view = 'skills'/.test(body) && /param = null/.test(body),
    '_pushHistory 应把 view 改写为 skills 并清空 param');
});

check('T4 catch 按 400/404 分流文案（参数错误不再渲染成技能不存在）', function () {
  const start = appJs.indexOf("} else if (view === 'skill') {");
  const end = appJs.indexOf("} else if (view === 'skill-sub') {");
  assert.ok(start >= 0 && end > start, '应能定位 skill 分支');
  const branch = appJs.slice(start, end);
  assert.ok(/e\.status === 400/.test(branch), 'catch 应识别 400');
  assert.ok(/common\.invalid_param/.test(branch), '400 应使用 common.invalid_param');
  assert.ok(/common\.load_failed/.test(branch), '其余错误仍走 common.load_failed');
  assert.ok(branch.indexOf('common.invalid_param') < branch.indexOf('common.load_failed'),
    '400 分支应在通用加载失败文案之前分流');
});

check('T5 zh/en 语言包均含 common.invalid_param（含首屏内联包）', function () {
  assert.strictEqual(zhCommon.invalid_param, '缺少或无效的技能 ID');
  assert.strictEqual(enCommon.invalid_param, 'Missing or invalid skill ID');
  assert.ok(/invalid_param: '缺少或无效的技能 ID'/.test(i18nJs), '内联 zh 包应含 invalid_param');
  assert.ok(/invalid_param: 'Missing or invalid skill ID'/.test(i18nJs), '内联 en 包应含 invalid_param');
});

check('T6 index.html 缓存版本已 bump（app.js / i18n）', function () {
  const appVer = /\/static\/js\/app\.js\?v=(\d+)/.exec(indexHtml);
  assert.ok(appVer && Number(appVer[1]) >= 311, 'app.js 版本应 ≥311（当前 ' + (appVer && appVer[1]) + '）');
  const i18nVer = /__I18N_CACHE_VER__='(\d+)'/.exec(indexHtml);
  assert.ok(i18nVer && Number(i18nVer[1]) >= 91, 'i18n 缓存版本应 ≥91（当前 ' + (i18nVer && i18nVer[1]) + '）');
});

console.log('\n#4260 skill missing-param contract passed (' + passed + '/6)');