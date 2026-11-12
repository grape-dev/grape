/*
 * Issue #406 — 技能展示名 + 「VOD 技能内置 SRE 能力」联动（源码级断言）
 *
 * 2026-11-11 修复：Vue 版丢失了「选 VOD CLI 声音分析技能后禁用 SRE 节点」的联动，本次补回。
 *   1. 技能展示：skill_display_name（有则优先）→ skill 名 → 占位文案
 *   2. 选技能后：applySreLockForVodSkill 命中 VOD CLI 技能 → sre.enabled = false + toast
 *   3. 切回普通技能 → 仅当是被该规则关掉的才恢复（lockedByVodSkill 标记）
 *   4. 只对 vod 节点生效（其它节点选技能不受影响）
 *   5. 文案走 i18n（zh/en 都有 sre_locked_by_vod_skill）
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

check('技能展示名优先级', function () {
  assert.ok(/node\.skill_display_name \|\| node\.skill/.test(VIEW), '展示应优先 skill_display_name');
  assert.ok(/select_skill/.test(VIEW), '未选应有占位文案');
});

check('选中技能后触发 SRE 联动', function () {
  const b = fnBody('pickSkill');
  assert.ok(/applySreLockForVodSkill\(node, skillName\)/.test(b), 'pickSkill 应调用联动函数');
});

check('联动只对 vod 节点、按技能名判定、带恢复标记', function () {
  const b = fnBody('applySreLockForVodSkill');
  assert.ok(/tag !== 'vod'/.test(b), '只对 vod 节点生效');
  assert.ok(/VOD_SKILL_LOCKS_SRE\.test/.test(b), '应按技能名正则判定');
  assert.ok(/sre\.enabled = false/.test(b), '命中应关闭 SRE');
  assert.ok(/lockedByVodSkill/.test(b), '应记录锁定来源以便恢复');
  assert.ok(/sre\.enabled = true/.test(b), '切回普通技能应恢复');
});

check('提示文案 zh/en 齐备', function () {
  for (const lg of ['zh', 'en']) {
    const d = JSON.parse(
      fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales', lg, 'dashboard.json'), 'utf8'),
    );
    assert.ok(d.sre_locked_by_vod_skill, lg + ' 缺 sre_locked_by_vod_skill');
  }
});

console.log('\n#406 全部通过 (' + passed + '/4)');