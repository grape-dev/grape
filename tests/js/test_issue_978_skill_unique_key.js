/*
 * Issue #978 — 技能选择唯一键（同名三档不再互相覆盖，源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy dashboard.js；Vue 迁移后为 skillKeyOf。
 *   1. 唯一键：skill_id 优先，回退 scope:skill_name
 *   2. 选中态比对用唯一键（不是 name）
 *   3. 选择回填按唯一键命中缓存（同名不同 scope 不串）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');

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

check('唯一键 = skill_id 优先，回退 scope:name', function () {
  const b = fnBody('skillKeyOf');
  assert.ok(/s\.skill_id \|\|/.test(b), '应优先 skill_id');
  assert.ok(/\?\(s\.scope \|\| 'official'\) \+ ':' \+ s\.skill_name|\(s\.scope \|\| 'official'\) \+ ':' \+ s\.skill_name/.test(b), '应回退 scope:name');
});

check('选中态按唯一键比对', function () {
  assert.ok(/skillKeyOf\(s\) === skillPanel\.currentKey/.test(VIEW), '选中态应比唯一键');
});

check('回填按唯一键命中缓存', function () {
  const i = VIEW.indexOf('function pickSkill(');
  const b = VIEW.slice(i, i + 900);
  assert.ok(/skillKey|cache\[/.test(b), '应按唯一键取缓存');
  assert.ok(/entry|fallback/.test(b), '应有 name 兜底路径');
});

console.log('\n#978 全部通过 (' + passed + '/3)');