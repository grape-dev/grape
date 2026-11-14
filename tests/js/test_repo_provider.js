/*
 * 平台归属判定的回归：不能再出现「非 github 即 gitcode」的二分。
 *
 * 运行：node tests/js/test_repo_provider.js
 *
 * 背景（2026-11-14 实锤）：托管请求的 provider 只认 github:/gitcode: 两个前缀，
 * gitee 仓库因此不带 provider 进 /repos/host，后端退到按 token 可用性兜底挑到
 * github，用 GitHub token 去查 gitee 仓库 → 404「明明我是管理员却托管失败」。
 */
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const root = path.resolve(__dirname, '../..');
const LIB = path.join(root, 'src/api/frontend/app/src/lib/repo-provider.js');
const APP = path.join(root, 'src/api/frontend/shell/js/app.js');

async function loadLib() {
  const code = fs.readFileSync(LIB, 'utf8').replace(/^export\s+/gm, '');
  const url = 'data:text/javascript;base64,' + Buffer.from(code + '\nexport { providerFromRepoKey, repoKeyOf, platformChoices };\n').toString('base64');
  return import(url);
}

(async () => {
  const { providerFromRepoKey, repoKeyOf, platformChoices } = await loadLib();

  // 1. 任何在册平台都能取到（含 gitee/gitlab/未来新平台）
  assert.strictEqual(providerFromRepoKey('gitee:yang-aiwu-new/demo'), 'gitee');
  assert.strictEqual(providerFromRepoKey('gitcode:a/b'), 'gitcode');
  assert.strictEqual(providerFromRepoKey('github:a/b'), 'github');
  assert.strictEqual(providerFromRepoKey('gitlab:f/g/r'), 'gitlab');
  assert.strictEqual(providerFromRepoKey('codearts:x/y'), 'codearts');

  // 2. 没有前缀 → 空串（交给后端的缓存/提示判定，不瞎猜）
  assert.strictEqual(providerFromRepoKey('owner/repo'), '');
  assert.strictEqual(providerFromRepoKey(''), '');
  assert.strictEqual(providerFromRepoKey(null), '');

  // 3. 不是前缀段的 ':' 不能当平台（URL、含空格的奇怪串）
  assert.strictEqual(providerFromRepoKey('http://x/y'), '');
  assert.strictEqual(providerFromRepoKey(':owner/repo'), '');
  assert.strictEqual(providerFromRepoKey('Owner:repo/x'), '');

  // 4. 旧壳的 stripRepoPrefix 也要剥 gitee（原来只写死 gitcode|github|gitlab）
  const src = fs.readFileSync(APP, 'utf8');
  const snippet = src.slice(src.indexOf('function stripRepoPrefix'), src.indexOf('window.stripRepoPrefix'));
  const stripRepoPrefix = new Function(snippet + '\nreturn stripRepoPrefix;')();
  assert.strictEqual(stripRepoPrefix('gitee:a/b'), 'a/b');
  assert.strictEqual(stripRepoPrefix('gitcode:a/b'), 'a/b');
  assert.strictEqual(stripRepoPrefix('gitlab:x/y/z'), 'x/y/z');
  assert.strictEqual(stripRepoPrefix('a/b'), 'a/b');
  assert.strictEqual(stripRepoPrefix('weird:name'), 'weird:name');
  assert.strictEqual(stripRepoPrefix(''), '');

  // 5. 旧壳侧边栏不得再按「非 github 即 gitcode」分组
  assert.ok(!/gitcodeRepos|githubRepos/.test(src), '侧边栏仍存在二分分组变量');
  assert.ok(/__REPO_PLATFORMS__/.test(src) || /groups\[p\]/.test(src), '侧边栏未按前缀分组');

  // 6. 建仓响应拼 repo_key：gitee/gitlab 不能再被写成 gitcode 前缀
  assert.strictEqual(repoKeyOf({ provider: 'gitee', full_name: 'o/r' }), 'gitee:o/r');
  assert.strictEqual(repoKeyOf({ provider: 'github', full_name: 'o/r' }), 'github:o/r');
  assert.strictEqual(repoKeyOf({ repo_key: 'gitlab:x/y', provider: 'github' }), 'gitlab:x/y');
  assert.strictEqual(repoKeyOf({ full_name: 'o/r' }), '', '无 provider 线索时不得默认 gitcode');

  // 7. 一键创建页的平台清单：服务端 providers ∪ 仓库前缀，保序去重，名字走 labelOf
  const label = (k) => ({ gitcode: 'GitCode', github: 'GitHub', gitee: 'Gitee', gitlab: 'GitLab' }[k] || k);
  assert.deepStrictEqual(
    platformChoices(['gitee', 'gitcode'], [{ repo_key: 'github:a/b' }, { repo_key: 'gitee:c/d' }], label),
    [{ key: 'gitee', name: 'Gitee' }, { key: 'gitcode', name: 'GitCode' }, { key: 'github', name: 'GitHub' }]
  );
  // 纯 PAT 平台（没有任何身份绑定）也必须出现在清单里 —— 这就是本次实锤的主症状
  assert.deepStrictEqual(platformChoices(['gitee'], [], label), [{ key: 'gitee', name: 'Gitee' }]);

  console.log('test_repo_provider: 7 groups passed');
})();