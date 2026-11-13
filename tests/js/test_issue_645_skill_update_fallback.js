/*
 * Issue #645 — 技能「上传更新」文案回退（源码级断言）
 *
 * 迁移说明：原实现用 I18N.t(key, null, '上传更新') 第三参兜底；Vue 版改为
 * tt2(key) + KEY2_DEFAULTS 表实现同样语义（缺键时给中文兜底，绝不显示原始 key）。
 *   1. tt2 命中语言包返回译文；未命中回退 KEY2_DEFAULTS[key]
 *   2. update_skill 的兜底文案是「上传更新」
 *   3. 模板通过 tt2 取值（不直接 G.I18N.t，避免显示 skills.action.update_skill）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/SkillsView.vue'), 'utf8');

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

check('tt2 = 语言包命中 + 表兜底', function () {
  const b = fnBody('tt2');
  assert.ok(/G\.I18N\.t\(full\)/.test(b), '应先查语言包');
  assert.ok(/v !== full/.test(b), '应判断是否命中（未命中返回 key 本身）');
  assert.ok(/KEY2_DEFAULTS\[key\]/.test(b), '未命中应回退兜底表');
});

check('update_skill 兜底文案是「上传更新」', function () {
  assert.ok(/'action\.update_skill': '上传更新'/.test(VIEW), '兜底表应有该键');
});

check('模板用 tt2 取值（不直出原始 key）', function () {
  assert.ok(/tt2\('action\.update_skill'\)/.test(VIEW), '模板应通过 tt2 取文案');
  assert.ok(!/\{\{\s*G\.I18N\.t\('skills\.action\.update_skill'\)/.test(VIEW), '不应直接 t() 出原始 key');
});

console.log('\n#645 全部通过 (' + passed + '/3)');