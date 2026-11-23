/*
 * Issue #3355 — 切片7：i18n 契约（凭据/参数/搜索词条进 zh/en 双包 + 缓存版本刷新）
 *
 * Seam（用户可见行为）：
 *   英文用户不再看到中文回退——本单新增的全部用户可见文案进入 plugins.json
 *   zh/en 双包；语言包缓存版本 +1（老客户端全端失效刷新一次）。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const FE = path.resolve(__dirname, '../../src/api/frontend');
const read = (p) => fs.readFileSync(path.join(FE, p), 'utf8');
const zh = JSON.parse(read('shell/locales/zh/plugins.json'));
const en = JSON.parse(read('shell/locales/en/plugins.json'));
const SHELL = read('shell/index.html');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

function flat(obj, prefix, out) {
  out = out || {};
  Object.keys(obj || {}).forEach(function (k) {
    const key = prefix ? prefix + '.' + k : k;
    if (obj[k] && typeof obj[k] === 'object' && !Array.isArray(obj[k])) flat(obj[k], key, out);
    else out[key] = obj[k];
  });
  return out;
}

const KEYS = [
  'cred_title', 'cred_hint', 'cred_configured', 'cred_placeholder',
  'cred_placeholder_configured', 'cred_none_declared', 'cred_free_add',
  'cred_free_key_ph', 'cred_show', 'cred_hide', 'cred_save', 'cred_saving',
  'cred_saved', 'cred_save_failed', 'cred_nothing',
  'param_title', 'param_hint', 'param_none', 'param_col_key', 'param_col_type',
  'param_col_desc', 'param_col_value', 'param_save', 'param_saving',
  'param_saved', 'param_save_failed', 'param_nothing', 'param_src_personal',
  'param_src_personal_tag', 'search_match', 'detail.close',
  // 复审返工（QA 202045541 P1-2/P1-3/P3-8）
  'cred_free_remove', 'cred_free_pending', 'cred_saved_session', 'detail_forbidden',
];

check('新增词条全部进入 zh/en 双包（不缺键）', function () {
  const zhFlat = flat(zh);
  const enFlat = flat(en);
  KEYS.forEach(function (k) {
    assert.ok(zhFlat[k] !== undefined, 'zh plugins.json 缺 ' + k);
    assert.ok(enFlat[k] !== undefined, 'en plugins.json 缺 ' + k);
  });
});

check('占位符插值契约：{{msg}} 成对出现在双包', function () {
  ;['cred_save_failed', 'param_save_failed'].forEach(function (k) {
    assert.ok(/\{\{msg\}\}/.test(zh[k]), 'zh ' + k + ' 应含 {{msg}}');
    assert.ok(/\{\{msg\}\}/.test(en[k]), 'en ' + k + ' 应含 {{msg}}');
  });
});

check('语言包缓存版本刷新（56 → 57，老客户端失效刷新一次）', function () {
  const m = SHELL.match(/__I18N_CACHE_VER__ ?= ?'(\d+)'/);
  assert.ok(m, 'shell/index.html 应有 __I18N_CACHE_VER__');
  assert.ok(parseInt(m[1], 10) >= 57, '缓存版本应 ≥57（当前 ' + m[1] + '）');
});

console.log('\n#3355 i18n 契约通过 (' + passed + '/3)');
