/*
 * Issue #407 — 场景页签闲置返回不丢编辑内容（草稿持久化，源码级断言）
 *
 * 迁移说明：原测试沙箱执行 legacy dashboard.js；Vue 迁移后为 saveDashDraft/clearDashDraft。
 *   1. 草稿 key 含 owner/name/mode/sceneId（按场景 + 编辑/新建模式隔离）
 *   2. 编辑输入即写草稿；保存成功/取消编辑后清除
 *   3. 重新进入时按 key 回填草稿（编辑模式恢复）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const VIEW = fs.readFileSync(path.resolve(__dirname, '../../src/api/frontend/app/src/views/DashboardView.vue'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('草稿 key 含 owner/name/mode/sceneId', function () {
  assert.ok(/'dashDraft_' \+ owner \+ '\/' \+ name \+ '_' \+ mode/.test(VIEW), 'key 应含 owner/name/mode');
  assert.ok(/\? '_' \+ sceneId : ''/.test(VIEW) || /sceneId \? '_'/.test(VIEW), '应有 sceneId 隔离');
});

check('输入即写草稿 / 保存或取消后清除', function () {
  assert.ok(/function saveDashDraft\(/.test(VIEW), '应有 saveDashDraft');
  assert.ok(/function clearDashDraft\(/.test(VIEW), '应有 clearDashDraft');
  const n = (VIEW.match(/saveDashDraft\(/g) || []).length;
  assert.ok(n >= 2, '保存草稿应在多处调用（编辑/新建），实际 ' + n);
  assert.ok(
    /clearDashDraft\(dashOwner\.value,\s*dashName\.value,\s*'edit'/.test(VIEW),
    '保存/退出编辑后应清 edit 草稿',
  );
  assert.ok(
    /clearDashDraft\(dashOwner\.value,\s*dashName\.value,\s*'create'\)/.test(VIEW),
    '创建成功后应清 create 草稿',
  );
});

check('重新进入按 key 回填', function () {
  assert.ok(/applyDraftNodes\(/.test(VIEW), '应能回填编辑节点');
  assert.ok(/function restoreDashDraft\(/.test(VIEW), '应有 restoreDashDraft');
  assert.ok(/sessionStorage\.getItem\(draftKey\(owner, name, mode, sceneId\)\)/.test(VIEW), '应按同构 key 读取草稿');
});

console.log('\n#407-dash 全部通过 (' + passed + '/3)');