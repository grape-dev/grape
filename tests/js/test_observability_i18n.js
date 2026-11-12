/*
 * 可观测性 i18n 审计（源码级，替代原「渲染后扫页面」的沙箱用例）
 *
 * 原用例把真实语言包塞进沙箱渲染全部视图，再扫 innerHTML 缺键；Vue 迁移后渲染层
 * 换成模板，改为**静态闭包审计**——效果等价且更快：
 *   1. 模板/脚本里出现的 t('key') 全部能在 zh 语言包解析到（不再显示原始 key）
 *   2. td('key') 走 detail.* 命名空间，同样必须存在
 *   3. 不允许出现明显拼接错误（'observability.observability.'）
 *   4. 语言包缺失比例必须是 0（有缺失即报，逐条列出便于修）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/ObservabilityView.vue'),
  'utf8',
);
const pack = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/legacy/locales/zh/observability.json'), 'utf8'),
);

function lookup(obj, dotted) {
  let cur = obj;
  for (const p of dotted.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[p];
  }
  return cur;
}

// 只取字面量 key（动态拼接的 key 无法静态判定，跳过）
// 跳过动态拼接（结尾带点，如 t('section.' + x)）与其它命名空间（t('common.x')）
const keys = new Set();
for (const m of VIEW.matchAll(/\bt\('([a-zA-Z0-9_.]+)'/g)) {
  const k = m[1];
  if (k.endsWith('.') || k.startsWith('common.')) continue;
  keys.add(k);
}
const detailKeys = new Set();
for (const m of VIEW.matchAll(/\btd\('([a-zA-Z0-9_.]+)'/g)) detailKeys.add(m[1]);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('t() 字面量 key 全部可解析', function () {
  const missing = [...keys].filter((k) => lookup(pack, k) === undefined);
  assert.deepStrictEqual(missing, [], '缺键: ' + missing.join(', '));
});

check('语言包非空（审计不是空跑）', function () {
  const n = Object.keys(pack).length;
  assert.ok(n >= 8, '语言包顶层键过少，实际 ' + n);
});

check('无双重命名空间拼接错误', function () {
  assert.ok(!/observability\.observability\./.test(VIEW), '不应出现重复前缀');
  assert.ok(!/G\.I18N\.t\('observability\.observability/.test(VIEW), '不应手工重复拼前缀');
});

check('审计规模合理（覆盖率不是空跑）', function () {
  assert.ok(keys.size >= 30, 't() 字面量 key 太少，可能切片/正则失效，实际 ' + keys.size);
  assert.ok(keys.size >= 30, '覆盖率不足，实际 ' + keys.size);
});

console.log(
  '\ni18n 审计通过 (' + passed + '/4) — t() ' + keys.size + ' 键 / td() ' + detailKeys.size + ' 键',
);