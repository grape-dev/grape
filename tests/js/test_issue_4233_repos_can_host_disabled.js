/*
 * Issue #4233 — 无托管权限（can_host=false）项目卡片禁用态（源码级断言）
 *
 * 覆盖：
 *   1. can_host=false 分支渲染 disabled 按钮，不再绑定 hostNoPermission / 不发请求
 *   2. 禁用文案复用 repos.card.no_permission_host，并用 title 说明权限要求
 *   3. 禁用态样式：cursor:not-allowed、hover 不出现实心高亮
 *   4. is_hosted / can_host 两个分支外观与行为零回归
 *   5. 死代码 hostNoPermission 已移除
 *   6. zh/en 词条同步（no_permission_host + no_permission_host_title）
 *   7. 凭据刷新后列表重取（缓存失效 + 挂载后静默重拉 /repos/member）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/ReposView.vue'),
  'utf8'
);
const ACCOUNT = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/AccountView.vue'),
  'utf8'
);
const ZH = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/zh/repos.json'), 'utf8')
);
const EN = JSON.parse(
  fs.readFileSync(path.join(ROOT, 'src/api/frontend/shell/locales/en/repos.json'), 'utf8')
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

check('can_host=false 分支渲染 disabled 按钮且不绑定 click 处理', function () {
  assert.ok(
    /<button\s+v-else\s+class="project-card-btn project-card-btn-outline project-card-btn-disabled"\s+:title="t\('repos\.card\.no_permission_host_title'\)"\s+disabled\s+@click\.stop\s*>/.test(
      VIEW
    ),
    'v-else 分支应为 disabled 按钮，带禁用类与 title'
  );
  const i = VIEW.indexOf('project-card-btn-disabled');
  const branch = VIEW.slice(i, VIEW.indexOf('</button>', i));
  assert.ok(branch.indexOf('hostAndSelect') < 0, '禁用分支不应再绑定 hostAndSelect');
  assert.ok(branch.indexOf('hostNoPermission') < 0, '禁用分支不应再绑定 hostNoPermission');
  assert.ok(branch.indexOf('G.API') < 0 && branch.indexOf('showToast') < 0, '禁用分支不应发请求/弹 toast');
});

check('禁用文案复用 no_permission_host，title 说明权限要求', function () {
  assert.ok(
    /\{\{\s*t\('repos\.card\.no_permission_host'\)\s*\}\}/.test(VIEW),
    '按钮文案应复用 repos.card.no_permission_host'
  );
  assert.ok(
    /:title="t\('repos\.card\.no_permission_host_title'\)"/.test(VIEW),
    '应通过 title 说明无托管权限原因'
  );
});

check('禁用态样式与可点击态区分（cursor:not-allowed，hover 无实心高亮）', function () {
  const i = VIEW.indexOf('.project-card-btn-disabled');
  assert.ok(i >= 0, '应有 .project-card-btn-disabled 样式');
  const body = VIEW.slice(i, i + 400);
  assert.ok(/cursor:\s*not-allowed/.test(body), '禁用态应使用 cursor:not-allowed');
  assert.ok(
    /\.project-card-btn-disabled:hover\s*\{[\s\S]{0,200}background:\s*transparent/.test(body),
    '禁用态 hover 不应出现实心高亮'
  );
});

check('is_hosted 分支保持进入项目（外观/行为零回归）', function () {
  assert.ok(
    /<button\s+v-if="r\.is_hosted"\s+class="project-card-btn project-card-btn-outline"\s+@click\.stop="selectRepo\(r\)"\s*>[\s\S]{0,80}t\('repos\.card\.enter_project'\)[\s\S]{0,10}→/.test(
      VIEW
    ),
    'is_hosted 分支应保持 selectRepo + 进入项目→'
  );
});

check('can_host 分支保持加入并托管（外观/行为零回归）', function () {
  assert.ok(
    /<button\s+v-else-if="r\.can_host"\s+class="project-card-btn project-card-btn-outline"\s+@click\.stop="hostAndSelect\(r, \$event\)"\s*>[\s\S]{0,80}t\('repos\.card\.join_and_host'\)[\s\S]{0,10}→/.test(
      VIEW
    ),
    'can_host 分支应保持 hostAndSelect + 加入并托管→'
  );
});

check('死代码 hostNoPermission 已移除', function () {
  assert.ok(VIEW.indexOf('hostNoPermission') < 0, 'hostNoPermission 不应再出现在 ReposView.vue');
});

check('zh 词条同步（无权限文案 + 权限要求 title）', function () {
  const c = ZH.card;
  assert.ok(c.no_permission_host && c.no_permission_host.length > 0, 'no_permission_host 应存在');
  assert.ok(
    c.no_permission_host_title && c.no_permission_host_title.indexOf('Maintainer') >= 0,
    'zh title 应说明需 Maintainer 及以上权限'
  );
});

check('en 词条同步（无权限文案 + 权限要求 title）', function () {
  const c = EN.card;
  assert.ok(c.no_permission_host && c.no_permission_host.length > 0, 'no_permission_host 应存在');
  assert.ok(
    c.no_permission_host_title && c.no_permission_host_title.indexOf('Maintainer') >= 0,
    'en title 应说明需 Maintainer 及以上权限'
  );
});

check('凭据刷新后列表重取（缓存失效 + 挂载后静默重拉）', function () {
  const i = ACCOUNT.indexOf('function refreshRepoCreds()');
  assert.ok(i >= 0, '应有 refreshRepoCreds');
  const body = ACCOUNT.slice(i, ACCOUNT.indexOf('\n}', i));
  assert.ok(body.indexOf('repo_member_cache') >= 0, '凭据变更应清 repo_member_cache');
  assert.ok(body.indexOf('_clearCachedReposData') >= 0, '凭据变更应清内存仓库缓存');
  assert.ok(
    /if \(props\.initialRepos\)[\s\S]{0,320}loadProjects\(\{ silent: true \}\)/.test(VIEW),
    '带首屏种子的挂载必须静默重拉 /repos/member，避免用缓存挡住刚获权限的用户'
  );
  assert.ok(/G\.API\.get\('\/repos\/member'\)/.test(VIEW), '列表数据应来自 /repos/member');
});

console.log('\n#4233 can_host 禁用态全部通过 (' + passed + '/9)');