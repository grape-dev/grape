/*
 * Issue #290 回归测试：需求中心详情页「查看 Issue」入口统一
 *
 * 运行：node tests/js/test_issue_290_view_issue.js
 *
 * 验证点（对应 Issue #290 验收标准）：
 *   1. _openBoardDetail 头部按钮（源码 L~363）：onclick URL 为
 *      _vcsUrl(repo_key, '/issues/' + issue_number)，无 is_pr 分支；文案为
 *      I18N.t('issues.detail.view_issue')，无硬编码「查看 PR」。
 *   2. _render 列表详情面板按钮（源码 L~976）：同上（变量 _taskSel）。
 *   3. is_pr=true 的卡片在两处入口下生成的 URL 仍为 /issues/{N}
 *      （行为断言：用与 app.js 一致的 _vcsUrl 实现）。
 *   4. 跨 provider：repo_key 为 github: 前缀时 URL 指向 github.com。
 *   5. _openPRDetail（PR 事件专属详情页）的「查看 PR ↗」按钮保持 /pull/ 不受影响。
 *   6. locales zh/en 的 issues.detail.view_issue 词条存在且语义正确。
 *
 * 策略：源码级断言（与 tests/js 现行范式一致）+ URL 构造行为断言。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '../..');
const issuesJsPath = path.join(ROOT, 'src/api/static/js/pages/issues.js');
const appJsPath = path.join(ROOT, 'src/api/static/js/app.js');
const src = fs.readFileSync(issuesJsPath, 'utf8');
const appSrc = fs.readFileSync(appJsPath, 'utf8');

let passed = 0, failed = 0;
function test(name, fn) {
  try { fn(); passed++; console.log('  ✓ ' + name); }
  catch (e) { failed++; console.log('  ✗ ' + name + '\n    ' + e.message); }
}

console.log('Issue #290: 需求中心「查看 Issue」入口统一 — 回归测试\n');

// ── 与 app.js 一致的真实 _vcsUrl（源码提取执行，避免实现漂移）──
function extractVcsUrl() {
  const m = appSrc.match(/function _vcsUrl\(repoKey, path\) \{[\s\S]*?\n\}/);
  if (!m) throw new Error('app.js 中未找到 _vcsUrl 函数');
  const fn = new Function('return ' + m[0])();
  return fn;
}
const _vcsUrl = extractVcsUrl();

// 源码行定位（容差：以关键片段搜索而非硬编码行号）
const boardBtnRe = /onclick="window\.open\('\$\{_vcsUrl\(t\.repo_key, '\/issues\/' \+ t\.issue_number\)\}','_blank'\)">\$\{I18N\.t\('issues\.detail\.view_issue'\)\}<\/button>/;
const listBtnRe = /onclick="window\.open\('\$\{_vcsUrl\(_taskSel\.repo_key, '\/issues\/' \+ _taskSel\.issue_number\)\}','_blank'\)">\$\{I18N\.t\('issues\.detail\.view_issue'\)\}<\/button>/;
// 旧代码形态（不应再出现在这两处按钮上）
const oldBoardBtnRe = /onclick="window\.open\('\$\{_vcsUrl\(t\.repo_key, \(t\.is_pr \? '\/pull\/' : '\/issues\/'\) \+ t\.issue_number\)\}','_blank'\)">\$\{t\.is_pr \? '查看 PR' : I18N\.t\('issues\.detail\.view_issue'\)\}<\/button>/;
const oldListBtnRe = /onclick="window\.open\('\$\{_vcsUrl\(_taskSel\.repo_key, \(_taskSel\.is_pr \? '\/pull\/' : '\/issues\/'\) \+ _taskSel\.issue_number\)\}','_blank'\)">\$\{_taskSel\.is_pr \? '查看 PR' : I18N\.t\('issues\.detail\.view_issue'\)\}<\/button>/;

// ─── 测试 1：_openBoardDetail 头部按钮统一指向 /issues/ 且文案走 i18n ───
test('_openBoardDetail 按钮统一 /issues/ + view_issue 文案', () => {
  assert.ok(boardBtnRe.test(src), '_openBoardDetail 按钮应为 _vcsUrl(t.repo_key, \'/issues/\' + t.issue_number) + I18N view_issue');
  assert.ok(!oldBoardBtnRe.test(src), '不应残留 is_pr 分支按钮（t 变量）');
});

// ─── 测试 2：_render 列表详情面板按钮统一指向 /issues/ 且文案走 i18n ───
test('_render 列表详情面板按钮统一 /issues/ + view_issue 文案', () => {
  assert.ok(listBtnRe.test(src), '_render 详情面板按钮应为 _vcsUrl(_taskSel.repo_key, \'/issues/\' + _taskSel.issue_number) + I18N view_issue');
  assert.ok(!oldListBtnRe.test(src), '不应残留 is_pr 分支按钮（_taskSel 变量）');
});

// ─── 测试 3：is_pr=true 卡片入口 URL 仍为 /issues/{N}（行为断言）───
test('is_pr=true 的卡片：入口 URL 仍为 https://gitcode.com/{owner}/{repo}/issues/{N}', () => {
  const t = { repo_key: 'gitcode:grape-dev/grape', issue_number: 290, is_pr: true };
  const url = _vcsUrl(t.repo_key, '/issues/' + t.issue_number);
  assert.strictEqual(url, 'https://gitcode.com/grape-dev/grape/issues/290', 'is_pr=true 时也应指向 /issues/290');
  assert.ok(url.indexOf('/pull/') === -1, 'URL 不应含 /pull/');
});

// ─── 测试 4：跨 provider github: 前缀链接正确 ───
test('跨 provider：github: 前缀 repo_key 生成 https://github.com/{owner}/{repo}/issues/{N}', () => {
  const t = { repo_key: 'github:octocat/hello-world', issue_number: 42, is_pr: true };
  const url = _vcsUrl(t.repo_key, '/issues/' + t.issue_number);
  assert.strictEqual(url, 'https://github.com/octocat/hello-world/issues/42', 'github 前缀应生成 github.com/issues 链接');
});

// ─── 测试 5：_openPRDetail 的「查看 PR ↗」按钮保持 /pull/ 不受影响 ───
test('_openPRDetail「查看 PR ↗」按钮保持 /pull/ 不变', () => {
  const m = src.match(/window\._openPRDetail = function\(t\) \{[\s\S]*?\n\};/);
  assert.ok(m, '应存在 _openPRDetail 函数');
  const body = m[0];
  assert.ok(body.indexOf("var _vcsPath = '/pull/' + t.issue_number;") >= 0, '_openPRDetail 应保留 /pull/ 路径');
  assert.ok(body.indexOf('查看 PR ↗') >= 0, '_openPRDetail 应保留「查看 PR ↗」按钮文案');
});

// ─── 测试 6：zh/en 词条 issues.detail.view_issue 存在且语义正确 ───
test('locales：zh「查看 Issue」/ en「View Issue」词条存在', () => {
  const zh = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/zh/issues.json'), 'utf8'));
  const en = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/api/static/locales/en/issues.json'), 'utf8'));
  assert.strictEqual(zh.detail && zh.detail.view_issue, '查看 Issue', 'zh issues.detail.view_issue 应为「查看 Issue」');
  assert.strictEqual(en.detail && en.detail.view_issue, 'View Issue', 'en issues.detail.view_issue 应为「View Issue」');
});

// ─── 测试 7：Out of Scope 保护——L1065 摘要行 is_pr 分支应保留 ───
test('摘要行 #N ↗ 链接的 is_pr 分支保留（Out of Scope）', () => {
  const summaryRowRe = /\$\{_vcsUrl\(t\.repo_key, \(t\.is_pr \? '\/pull\/' : '\/issues\/'\) \+ t\.issue_number\)\}/;
  assert.ok(summaryRowRe.test(src), '摘要行（_detailHtml 内 #N ↗ 链接）的 is_pr 分支应保留不动');
});

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
