/*
 * Issue #4172 — 项目页交互优化源码级断言
 * （随 Issue #3941「只看已托管」勾选框方案落地后同步改写：
 *   范围切换按钮已由 hosted-only checkbox 取代，空态改为引导取消勾选。）
 *
 * 覆盖：
 *   1. 右侧操作区顺序：刷新 → 只看已托管勾选框 → 搜索 → 新建项目
 *   2. 勾选框以 v-model 绑定 onlyHosted，且点击即时生效（无切换按钮残留）
 *   3. 勾选/搜索共用 resetPage 重置分页
 *   4. 无托管仓库空态引导「取消勾选」而非已删除的「全部仓库」按钮
 *   5. 空态三态判定：仅无已托管且有未托管候选时引导，搜索/平台零结果走通用空态
 *   6. zh/en locale 文案同步（勾选语义 tooltip + 单段空态文案）
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '../..');
const VIEW = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/ReposView.vue'),
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

check('右侧操作区顺序为刷新 → 只看已托管勾选框 → 搜索 → 新建项目', function () {
  const start = VIEW.indexOf('project-section-header-right');
  assert.ok(start >= 0, '应有右侧操作区');
  const section = VIEW.slice(start, VIEW.indexOf('</div>', start));
  const iScope = section.indexOf('project-hosted-filter');
  const iRefresh = section.indexOf('@click="refresh"');
  const iSearch = section.indexOf('project-search');
  const iNew = section.indexOf('project-new-btn');
  assert.ok(iScope >= 0 && iRefresh >= 0 && iSearch >= 0 && iNew >= 0, '四个控件都应存在');
  assert.ok(iRefresh < iScope, '刷新应在勾选框之前');
  assert.ok(iScope < iSearch, '勾选框应在搜索框之前');
  assert.ok(iSearch < iNew, '新建项目应在搜索框之后');
});

check('范围切换已改为勾选框（无 toggleScope 按钮残留）', function () {
  assert.ok(!/@click="toggleScope"/.test(VIEW), '不应残留范围切换按钮');
  assert.ok(!/function toggleScope/.test(VIEW), '不应残留 toggleScope 死代码');
  assert.ok(/class="project-hosted-filter"/.test(VIEW), '应有 hosted 勾选框容器');
  assert.ok(/<input\s+v-model="onlyHosted"\s+type="checkbox"\s+@change="resetPage"\s*\/>/.test(VIEW),
    '勾选框应以 v-model 绑定 onlyHosted 并在变更时 resetPage');
});

check('勾选/搜索共用 resetPage 重置分页', function () {
  assert.ok(/function resetPage\(\) \{\s*repoPage\.value = 0\s*\}/.test(VIEW),
    '应存在 resetPage() 且将 repoPage 归零');
  assert.ok(/@input="resetPage"/.test(VIEW), '搜索输入应调用 resetPage');
});

check('无托管仓库空态引导取消勾选（不再有「全部仓库」按钮）', function () {
  assert.ok(!/lobby-empty-link/.test(VIEW), '空态不应再有可点击「全部仓库」按钮');
  assert.ok(
    /\{\{\s*showHostedEmpty\s*\?\s*t\('repos\.lobby\.empty_hosted_desc'\)\s*:\s*t\('repos\.lobby\.empty_desc'\)\s*\}\}/.test(VIEW),
    '空态文案应随三态判定切换为单段 desc'
  );
});

check('空态三态判定：仅无已托管且有未托管候选时引导空态', function () {
  assert.ok(
    /const showHostedEmpty = computed\([\s\S]{0,160}hostedCount\.value === 0[\s\S]{0,120}hasUnhostedRepos\.value/.test(
      VIEW
    ),
    '仅在无已托管且有未托管候选时展示引导空态'
  );
  assert.ok(
    /const hasUnhostedRepos = computed\(\(\) => repos\.value\.some\(\(r\) => !r\.is_hosted\)\)/.test(VIEW),
    '应存在 hasUnhostedRepos 判定'
  );
  assert.ok(
    /\{\{\s*showHostedEmpty\s*\?\s*t\('repos\.lobby\.empty_hosted_title'\)\s*:\s*t\('repos\.lobby\.empty_title'\)\s*\}\}/.test(VIEW),
    '空态标题应随三态判定变化，搜索/平台零结果不误报'
  );
});

check('zh locale 文案同步（勾选语义 tooltip + 单段空态文案）', function () {
  const l = ZH.lobby;
  assert.strictEqual(l.scope_hosted, '只看已托管', 'scope_hosted 文案');
  assert.strictEqual(
    l.scope_hosted_title,
    '勾选时仅展示已托管仓库，取消勾选展示全部仓库',
    'hosted tooltip 应描述勾选效果'
  );
  assert.ok(l.empty_hosted_desc, '空态文案应为单段 empty_hosted_desc');
  assert.ok(/取消勾选/.test(l.empty_hosted_desc), '空态应引导取消勾选');
  assert.ok(!('empty_hosted_desc_prefix' in l), '旧空态前后缀应移除');
  assert.ok(!('scope_all' in l), 'scope_all 已随切换按钮移除');
});

check('en locale 文案同步（勾选语义 tooltip + 单段空态文案）', function () {
  const l = EN.lobby;
  assert.strictEqual(l.scope_hosted, 'Hosted only', 'scope_hosted 文案');
  assert.strictEqual(
    l.scope_hosted_title,
    'When checked, only hosted repositories are shown; uncheck to show all repositories',
    'hosted tooltip 应描述勾选效果'
  );
  assert.ok(l.empty_hosted_desc, '空态文案应为单段 empty_hosted_desc');
  assert.ok(/uncheck/i.test(l.empty_hosted_desc), '空态应引导 uncheck');
  assert.ok(!('empty_hosted_desc_prefix' in l), '旧空态前后缀应移除');
  assert.ok(!('scope_all' in l), 'scope_all 已随切换按钮移除');
});

console.log('\n#4172 项目页交互优化全部通过 (' + passed + '/7)');
