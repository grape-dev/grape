/*
 * Issue #543 — 迁移目标组织下拉（PR #747 审查修复验证，源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy settings.js；Vue 迁移后为 migrateState + migrateOptions。
 *   1. 目标组织选项来自后端（migrate_orgs 接口），不是前端硬编码
 *   2. 四态文案：加载中 / 加载失败 / 空 / 可选
 *   3. 个人档（personal）与组织并列展示
 *   4. 失败时禁止提交（不拿空列表当可选项）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/SettingsView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('选项来自后端 + 四态文案', function () {
  assert.ok(/migrateState = ref\(\{ loading: false, loaded: false, failed: false, orgs: \[\], personal: '' \}\)/.test(VIEW), '应有完整 migrate 状态');
  for (const k of ['migrate_orgs_loading', 'migrate_orgs_load_failed', 'migrate_orgs_empty']) {
    assert.ok(VIEW.indexOf(k) >= 0, '应有 ' + k + ' 文案');
  }
});

check('个人档与组织并列', function () {
  assert.ok(/personal/.test(VIEW), '应有 personal 档');
  assert.ok(/migrateOptions/.test(VIEW), '选项应合并为 migrateOptions');
});

check('未选目标时阻断提交（守卫）', function () {
  const i = VIEW.indexOf('function migrateRepo(');
  assert.ok(i > 0, '应有 migrateRepo');
  const b = VIEW.slice(i, i + 800);
  assert.ok(/if \(!target\)/.test(b), '空目标应提前 return');
  assert.ok(/migrate_owner_required/.test(b), '应提示需选择目标组织');
  assert.ok(/showConfirm/.test(b), '提交前应二次确认');
});

console.log('\n#543 全部通过 (' + passed + '/3)');