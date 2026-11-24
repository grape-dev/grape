/*
 * Issue #3941「我的项目」托管筛选改为勾选框（源码级断言）
 *
 * 运行：node tests/js/test_issue_3941_hosted_filter_checkbox.js
 *
 * 锁 4 件事：
 *   1. 并列切换按钮已移除，改为 .project-hosted-filter 下的 checkbox；
 *   2. onlyHosted 默认勾选（默认只看已托管），过滤沿用 is_hosted；
 *   3. 勾选/搜索都走 resetPage 重置分页；
 *   4. zh/en lobby 文案只保留一套 scope_hosted*，无重复键、无「点击切换」措辞，
 *      空态不再引导已删除的「全部仓库」按钮。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
const VIEW = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/app/src/views/ReposView.vue'),
  'utf8'
);
const ZH = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/shell/locales/zh/repos.json'),
  'utf8'
);
const EN = fs.readFileSync(
  path.join(ROOT, 'src/api/frontend/shell/locales/en/repos.json'),
  'utf8'
);

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log('  ✓ ' + name);
}

function lobbyOf(text) {
  const parsed = JSON.parse(text);
  assert.ok(parsed.lobby, 'repos.json 应包含 lobby 对象');
  return parsed.lobby;
}

function countKey(text, key) {
  const re = new RegExp('"' + key + '"\\s*:', 'g');
  return (text.match(re) || []).length;
}

check('托管筛选为 .project-hosted-filter 下的 checkbox', function () {
  assert.ok(/class="project-hosted-filter"/.test(VIEW), '应渲染 .project-hosted-filter 容器');
  assert.ok(/type="checkbox"/.test(VIEW), '控件应为 type="checkbox"');
  assert.ok(/v-model="onlyHosted"/.test(VIEW), 'checkbox 应绑定 onlyHosted');
  assert.ok(/@change="resetPage"/.test(VIEW), '勾选变化应重置分页');
});

check('并列切换按钮已移除', function () {
  assert.ok(!/@click="onlyHosted = !onlyHosted"/.test(VIEW), '不应残留「全部仓库/只看已托管」切换按钮');
});

check('默认只看已托管且沿用 is_hosted 过滤', function () {
  assert.ok(/const onlyHosted = ref\(true\)/.test(VIEW), 'onlyHosted 默认应为 true');
  assert.ok(/if \(onlyHosted\.value\) list = list\.filter\(\(r\) => r\.is_hosted\)/.test(VIEW),
    '过滤应沿用 is_hosted');
});

check('搜索与勾选共用 resetPage 重置分页', function () {
  assert.ok(/function resetPage\(\) \{\s*repoPage\.value = 0\s*\}/.test(VIEW),
    '应存在 resetPage() 且将 repoPage 归零');
  assert.ok(/@input="resetPage"/.test(VIEW), '搜索输入应调用 resetPage');
  assert.ok(!/function onSearch\(/.test(VIEW), '重复的 onSearch 应已合并移除');
  assert.ok(!/function onHostedFilterChange\(/.test(VIEW), '重复的 onHostedFilterChange 应已合并移除');
});

check('空态三态判定保留：搜索/平台零结果不误报「还没有已托管仓库」', function () {
  assert.ok(
    /const showHostedEmpty = computed\([\s\S]{0,160}hostedCount\.value === 0[\s\S]{0,120}hasUnhostedRepos\.value/.test(VIEW),
    '仅在无已托管且有未托管候选时展示引导空态'
  );
  assert.ok(
    /\{\{\s*showHostedEmpty\s*\?\s*t\('repos\.lobby\.empty_hosted_title'\)\s*:\s*t\('repos\.lobby\.empty_title'\)\s*\}\}/.test(VIEW),
    '空态标题应按三态判定切换'
  );
});

check('zh lobby 只保留一套 scope_hosted*，无重复键/无点击切换措辞', function () {
  assert.strictEqual(countKey(ZH, 'scope_hosted'), 1, 'zh 应只有一个 scope_hosted 键');
  assert.strictEqual(countKey(ZH, 'scope_hosted_title'), 1, 'zh 应只有一个 scope_hosted_title 键');
  assert.strictEqual(countKey(ZH, 'scope_all'), 0, 'zh 不应残留无引用的 scope_all');
  assert.strictEqual(countKey(ZH, 'scope_all_title'), 0, 'zh 不应残留无引用的 scope_all_title');
  const lobby = lobbyOf(ZH);
  assert.ok(!/点击切换|点击回到|点击/.test(lobby.scope_hosted_title),
    'zh scope_hosted_title 不应含「点击切换」措辞');
});

check('en lobby 只保留一套 scope_hosted*，无重复键/无点击切换措辞', function () {
  assert.strictEqual(countKey(EN, 'scope_hosted'), 1, 'en 应只有一个 scope_hosted 键');
  assert.strictEqual(countKey(EN, 'scope_hosted_title'), 1, 'en 应只有一个 scope_hosted_title 键');
  assert.strictEqual(countKey(EN, 'scope_all'), 0, 'en 不应残留无引用的 scope_all');
  assert.strictEqual(countKey(EN, 'scope_all_title'), 0, 'en 不应残留无引用的 scope_all_title');
  const lobby = lobbyOf(EN);
  assert.ok(!/click to switch|click to show|click/i.test(lobby.scope_hosted_title),
    'en scope_hosted_title 不应含 click to switch 措辞');
});

check('空态不再引导已删除的「全部仓库」按钮', function () {
  const zh = lobbyOf(ZH);
  const en = lobbyOf(EN);
  assert.ok(!/点右上角/.test(zh.empty_hosted_desc), 'zh 空态不应再引导右上角按钮');
  assert.ok(/取消勾选/.test(zh.empty_hosted_desc), 'zh 空态应引导取消勾选「只看已托管」');
  assert.ok(!/top right/i.test(en.empty_hosted_desc), 'en 空态不应再引导右上角按钮');
  assert.ok(/uncheck/i.test(en.empty_hosted_desc), 'en 空态应引导 uncheck「Hosted only」');
});

check('zh/en lobby 键集合一致', function () {
  assert.deepStrictEqual(Object.keys(lobbyOf(ZH)).sort(), Object.keys(lobbyOf(EN)).sort(),
    'zh/en lobby 键应一一对应');
});

console.log('\nIssue #3941 hosted filter 源码断言全部通过 (' + passed + '/9)');
