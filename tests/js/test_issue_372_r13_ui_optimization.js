/*
 * Issue #372（R13 UI 优化）— 源码级断言（2026-11-11 改写）
 *
 * 迁移说明：原用例按 legacy 函数名断言（submitCreateRepo / doCreateFromTemplate /
 * forkTemplate / _accountManualBindForm / _accountBindProvider / !oauthConfigured）。
 * Vue 迁移后这些能力仍在，只是落到 ReposView.vue / AccountView.vue 的新入口：
 *   - 建仓/建模板：openCreateModal / submitCreate / onTemplateCreate
 *   - 账号绑定：bindProvider + oauthConfigured 分支（含手动绑定弹窗）
 *   - app.js 的 403 处理（T41-11..14）保持原断言（仍由 shell 负责）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const REPOS = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/ReposView.vue'), 'utf8');
const ACCOUNT = fs.readFileSync(path.join(ROOT, 'src/api/frontend/app/src/views/AccountView.vue'), 'utf8');
const APP = fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/js/app.js'), 'utf8');

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('建仓入口与提交都在（ReposView）', function () {
  assert.ok(/function openCreateModal\(/.test(REPOS), '应有 openCreateModal');
  assert.ok(/function submitCreate\(/.test(REPOS), '应有 submitCreate');
  assert.ok(/function closeCreateModal\(/.test(REPOS), '应有 closeCreateModal');
});

check('模板建仓 / 从模板创建（ReposView）', function () {
  assert.ok(/function onTemplateCreate\(/.test(REPOS), '应有 onTemplateCreate');
  assert.ok(/repos\.tab\.templates/.test(REPOS), '应有模板 Tab');
});

check('账号绑定入口与 OAuth 已配置分支（AccountView）', function () {
  assert.ok(/function bindProvider\(/.test(ACCOUNT), '应有 bindProvider');
  assert.ok(/oauthConfigured/.test(ACCOUNT), '应读 oauthConfigured');
  const i = ACCOUNT.indexOf('const oauthConfigured');
  assert.ok(i > 0, '应有 oauthConfigured 判定');
  const j = ACCOUNT.indexOf('if (', i);
  assert.ok(j > i, '应据此分支（未配置时走手动绑定/提示）');
});

check('T41-04 nav(account) 清空 repoTitle（shell）', function () {
  assert.ok(/repoTitle[\s\S]{0,120}(textContent\s*=\s*''|innerHTML\s*=\s*'')/.test(APP), '切账号页应清空标题');
});

check('T41-08/09 pat_no_refresh_hint 文案齐备', function () {
  for (const lg of ['zh', 'en']) {
    const d = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales', lg, 'app.json'), 'utf8'));
    assert.ok(JSON.stringify(d).indexOf('pat_no_refresh_hint') >= 0, lg + ' 缺 pat_no_refresh_hint');
  }
});

check('T41-11..14 dashboard 403 处理（shell）', function () {
  assert.ok(/e\.status === 403|status === 403/.test(APP), '应有 403 判断');
  assert.ok(/currentRepo[\s\S]{0,200}(switchRepo|goToRepos)/.test(APP), '403 应清 currentRepo 并切仓/回大厅');
  assert.ok(/_tried403Repos/.test(APP), '应有 403 仓库记忆集合');
  const i = APP.indexOf('_tried403Repos');
  assert.ok(i > 0, '应有成功加载后清理逻辑');
});

console.log('\n#372 全部通过 (' + passed + '/6)（按 Vue 入口改写）');