/*
 * Issue #4172 — 项目页交互优化源码级断言
 *
 * 覆盖：
 *   1. 右侧操作区顺序：范围切换 → 刷新 → 搜索 → 新建项目
 *   2. 范围切换按钮文案 = 点击后的目标视图（与当前状态相反）
 *   3. 范围切换 tooltip 描述点击效果，zh/en 同步
 *   4. 无托管仓库空态的「全部仓库」为可点击元素，点击切到全部仓库视图
 *   5. 全部仓库为 0 条时仍展示通用空态
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

check('右侧操作区顺序为范围切换 → 刷新 → 搜索 → 新建项目', function () {
  const start = VIEW.indexOf('project-section-header-right');
  assert.ok(start >= 0, '应有右侧操作区');
  const section = VIEW.slice(start, VIEW.indexOf('</div>', start));
  const iScope = section.indexOf('toggleScope');
  const iRefresh = section.indexOf('@click="refresh"');
  const iSearch = section.indexOf('project-search');
  const iNew = section.indexOf('project-new-btn');
  assert.ok(iScope >= 0 && iRefresh >= 0 && iSearch >= 0 && iNew >= 0, '四个控件都应存在');
  assert.ok(iScope < iRefresh, '范围切换应在刷新之前');
  assert.ok(iRefresh < iSearch, '刷新应在搜索框之前');
  assert.ok(iSearch < iNew, '新建项目应在搜索框之后');
});

check('范围切换文案 = 点击后目标视图（与当前相反）', function () {
  assert.ok(
    /\{\{\s*onlyHosted\s*\?\s*t\('repos\.lobby\.scope_all'\)\s*:\s*t\('repos\.lobby\.scope_hosted'\)\s*\}\}/.test(
      VIEW
    ),
    'onlyHosted=true 应显示「全部仓库」，false 显示「只看已托管」'
  );
  assert.ok(
    /onlyHosted\s*\?\s*t\('repos\.lobby\.scope_all_title'\)\s*:\s*t\('repos\.lobby\.scope_hosted_title'\)/.test(
      VIEW
    ),
    'tooltip 应与目标视图一致'
  );
});

check('范围切换点击即时生效且重置分页', function () {
  assert.ok(/function toggleScope\(\)/.test(VIEW), '应有 toggleScope');
  const i = VIEW.indexOf('function toggleScope()');
  const body = VIEW.slice(i, VIEW.indexOf('}', i) + 1);
  assert.ok(body.indexOf('onlyHosted.value = !onlyHosted.value') >= 0, '应切换 onlyHosted');
  assert.ok(body.indexOf('repoPage.value = 0') >= 0, '应重置分页');
});

check('无托管仓库空态「全部仓库」可点击并切到全部仓库', function () {
  assert.ok(/class="lobby-empty-link"[^>]*@click="showAllRepos"/.test(VIEW), '空态应有可点击「全部仓库」');
  assert.ok(
    /\{\{\s*showHostedEmpty\s*\?\s*t\('repos\.lobby\.empty_hosted_title'\)\s*:\s*t\('repos\.lobby\.empty_title'\)\s*\}\}/.test(
      VIEW
    ),
    '空态标题应随是否有可切换候选变化'
  );
  assert.ok(/function showAllRepos\(\)/.test(VIEW), '应有 showAllRepos');
  const i = VIEW.indexOf('function showAllRepos()');
  const body = VIEW.slice(i, VIEW.indexOf('}', i) + 1);
  assert.ok(body.indexOf('onlyHosted.value = false') >= 0, '点击后应切到全部仓库');
});

check('全部仓库为 0 条时仍显示通用空态', function () {
  assert.ok(
    /const showHostedEmpty = computed\([\s\S]{0,160}hostedCount\.value === 0[\s\S]{0,120}hasUnhostedRepos\.value/.test(
      VIEW
    ),
    '仅在无已托管且有未托管候选时展示引导空态'
  );
  assert.ok(
    /const hasUnhostedRepos = computed\(\(\) => repos\.value\.some\(\(r\) => !r\.is_hosted\)\)/.test(
      VIEW
    ),
    '应基于 repos 判断是否存在未托管候选'
  );
});

check('zh locale 文案同步（按钮语义反转 + 点击效果 tooltip + 空态分段）', function () {
  const l = ZH.lobby;
  assert.strictEqual(l.scope_all, '全部仓库', 'scope_all 文案');
  assert.strictEqual(l.scope_hosted, '只看已托管', 'scope_hosted 文案');
  assert.strictEqual(l.scope_all_title, '点击查看全部仓库（含未托管）', 'all tooltip 应描述点击效果');
  assert.strictEqual(l.scope_hosted_title, '点击只看已托管仓库', 'hosted tooltip 应描述点击效果');
  assert.ok(l.empty_hosted_desc_prefix && l.empty_hosted_desc_suffix, '空态文案应拆为前后缀');
  assert.ok(!('empty_hosted_desc' in l), '旧整段空态文案应移除');
});

check('en locale 文案同步（按钮语义反转 + 点击效果 tooltip + 空态分段）', function () {
  const l = EN.lobby;
  assert.strictEqual(l.scope_all, 'All repositories', 'scope_all 文案');
  assert.strictEqual(l.scope_hosted, 'Hosted only', 'scope_hosted 文案');
  assert.strictEqual(
    l.scope_all_title,
    'Click to view all repositories (including unhosted)',
    'all tooltip 应描述点击效果'
  );
  assert.strictEqual(
    l.scope_hosted_title,
    'Click to show hosted repositories only',
    'hosted tooltip 应描述点击效果'
  );
  assert.ok(l.empty_hosted_desc_prefix && l.empty_hosted_desc_suffix, '空态文案应拆为前后缀');
  assert.ok(!('empty_hosted_desc' in l), '旧整段空态文案应移除');
});

console.log('\n#4172 项目页交互优化全部通过 (' + passed + '/7)');