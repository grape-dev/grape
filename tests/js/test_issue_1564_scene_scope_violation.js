/*
 * Issue #1564 — 场景保存失败按 skill scope 冲突 violations[] 标红
 *
 * 2026-11-11 修复：迁移后 saveSceneEdit 的 catch 只给一条通用 toast，
 * 本次按旧 dashboard.js 规格补回：
 *   A. violations[] 建索引：node_id 优先、回退 node_tag、跳过无键条目
 *   B. 提示文案：i18n skill_scope_mismatch（第三参兜底），带 skill（actual → expected）
 *   C. 选技能即清除该节点标红（就地修正后不再红）
 *   D. 卡片：scope-bad 类（红框 + 红色光晕）+ 卡片内 violation 提示块
 *   E. 保存 400 有 violations → 走标红分支；无 violations → 原通用失败 toast
 *   F. zh/en dashboard.json 均有 skill_scope_mismatch
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');

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

check('violations 建索引：node_id 优先、回退 node_tag', function () {
  const b = fnBody('resetScopeViolations');
  assert.ok(/v\.node_id \|\| v\.node_tag/.test(b), '应按 node_id 优先、回退 node_tag 建键');
  assert.ok(/if \(k\) scopeViolations\[k\] = v/.test(b), '无键条目应跳过');
});

check('提示文案带 skill 与 scope 迁移方向', function () {
  const b = fnBody('scopeViolationHint');
  assert.ok(/skill_scope_mismatch/.test(b), '应使用 i18n 键');
  assert.ok(/v\.actual_scope/.test(b) && /v\.expected_scope/.test(b), '应展示 actual → expected');
});

check('选技能清除该节点标红', function () {
  const b = fnBody('pickSkill');
  assert.ok(/delete scopeViolations\[node\.id\]/.test(b), 'pickSkill 后应清掉该节点 violations');
});

check('卡片标红：scope-bad 类 + 提示块', function () {
  assert.ok(/'scope-bad': !!scopeViolationOf\(node\)/.test(VIEW), '卡片应绑定 scope-bad 类');
  assert.ok(/\.ap-nd\.scope-bad \{[\s\S]{0,160}var\(--r\)/.test(VIEW), '应有红框样式');
  assert.ok(/class="ap-nd-violation"/.test(VIEW), '卡片内应有 violation 提示块');
});

check('保存失败分支：有 violations 走标红，否则通用 toast', function () {
  const i = VIEW.indexOf('function saveSceneEdit(');
  const b = VIEW.slice(i, i + 4200);
  assert.ok(/e\.detail && e\.detail\.violations/.test(b), '应从 400 detail 取 violations');
  assert.ok(/resetScopeViolations\(viols\)/.test(b), '有 violations 应重建索引');
  assert.ok(/skill_scope_mismatch/.test(b), '提示用 skill_scope_mismatch');
  assert.ok(/save_failed/.test(b), '无 violations 仍应走通用失败提示');
});

check('zh/en 均有 skill_scope_mismatch', function () {
  for (const lg of ['zh', 'en']) {
    const d = JSON.parse(
      fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales', lg, 'dashboard.json'), 'utf8'),
    );
    const v = JSON.stringify(d);
    assert.ok(v.indexOf('skill_scope_mismatch') >= 0, lg + ' 缺该键');
  }
});

console.log('\n#1564 全部通过 (' + passed + '/6)');